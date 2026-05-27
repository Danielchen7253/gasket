import base64
from datetime import datetime, timezone
import html
import json
import mimetypes
import os
import re
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from dotenv import load_dotenv


load_dotenv(Path(__file__).with_name(".env"))

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_NAMEPLATE_MODEL = os.getenv("OPENAI_NAMEPLATE_MODEL", "gpt-4.1-mini")
SHOPIFY_STORE_DOMAIN = os.getenv("SHOPIFY_STORE_DOMAIN", "").strip()

ROOT = Path(__file__).resolve().parent
UPLOAD_DIR = ROOT / "uploads" / "customer_nameplates"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
BACKGROUND_REFRESHING: set[int] = set()

MODEL_ALIAS_OVERRIDES = {
    ("SUBZERO", "685592"): "685/S/2",
    ("SUBZERO", "68592"): "685/S/2",
    ("SUBZERO", "685S2"): "685/S/2",
}


def esc(value) -> str:
    return "" if value is None else html.escape(str(value), quote=True)


def money(value) -> str:
    return "TBD" if value in (None, "") else f"${float(value):,.2f}"


def normalize_model(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def model_variants(value: str) -> list[str]:
    raw = (value or "").strip().upper()
    compact = normalize_model(raw)
    variants = {raw, compact}
    if compact:
        variants.add(compact.replace("9", "S"))
        variants.add(compact.replace("S", "9"))
    slash_suffix = re.match(r"^(\d{2,4})[/-]?([A-Z0-9])[/-]?(\d)$", raw)
    if slash_suffix:
        base, middle, suffix = slash_suffix.groups()
        middle_options = {middle}
        if middle == "9":
            middle_options.add("S")
        if middle == "S":
            middle_options.add("9")
        for option in middle_options:
            variants.add(f"{base}/{option}/{suffix}")
            variants.add(f"{base}-{option}-{suffix}")
            variants.add(f"{base}{option}{suffix}")
    return [item for item in variants if item]


def canonical_model_for_brand(brand: str, model: str) -> str:
    return MODEL_ALIAS_OVERRIDES.get((normalize_model(brand), normalize_model(model)), model)


def model_similarity_score(wanted: str, candidate: str) -> float:
    wanted_norm = normalize_model(wanted)
    candidate_norm = normalize_model(candidate)
    if not wanted_norm or not candidate_norm:
        return 0
    if wanted_norm == candidate_norm:
        return 100
    wanted_aliases = set(model_variants(wanted))
    candidate_aliases = set(model_variants(candidate))
    if wanted_aliases & candidate_aliases:
        return 98
    wanted_digits = re.sub(r"\D", "", wanted_norm)
    candidate_digits = re.sub(r"\D", "", candidate_norm)
    if wanted_digits and wanted_digits == candidate_digits:
        return 92
    if wanted_digits and candidate_digits and (wanted_digits.startswith(candidate_digits) or candidate_digits.startswith(wanted_digits)):
        return 82
    if wanted_norm in candidate_norm or candidate_norm in wanted_norm:
        return 75
    return 0


def door_positions_for_count(count: int) -> list[dict]:
    layouts = {
        1: [("single_door", "Single Door")],
        2: [("left_door", "Left Door"), ("right_door", "Right Door")],
        3: [("top_door", "Top Door"), ("left_door", "Left Door"), ("right_door", "Right Door")],
        4: [
            ("upper_left_door", "Upper Left Door"),
            ("upper_right_door", "Upper Right Door"),
            ("lower_left_door", "Lower Left Door"),
            ("lower_right_door", "Lower Right Door"),
        ],
    }
    return [{"key": key, "label": label} for key, label in layouts.get(count, [])]


def infer_door_positions(product: dict) -> list[dict]:
    existing = product.get("door_positions")
    if isinstance(existing, list) and existing:
        return existing
    try:
        count = int(product.get("door_count") or 0)
    except Exception:
        count = 0
    if not count:
        count = estimated_gasket_quantity(product, [])
    return door_positions_for_count(max(1, min(4, count)))


def door_layout_name(positions: list[dict]) -> str:
    keys = [item.get("key") for item in positions]
    if keys == ["left_door", "right_door"]:
        return "side_by_side_2_door"
    if keys == ["top_door", "left_door", "right_door"]:
        return "top_over_2_door"
    if keys == ["upper_left_door", "upper_right_door", "lower_left_door", "lower_right_door"]:
        return "quad_4_door"
    return f"{len(positions)}_door"


def is_unconfirmed_new_product(product: dict) -> bool:
    return (
        product.get("data_status") == "customer_requested"
        and not product.get("product_image_url")
        and not product.get("door_layout_source")
    )


def supabase_headers(prefer: str | None = None) -> dict[str, str]:
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def parse_multipart(body: bytes, content_type: str) -> dict[str, dict]:
    match = re.search(r"boundary=(?P<boundary>[^;]+)", content_type or "")
    if not match:
        return {}
    boundary = ("--" + match.group("boundary").strip('"')).encode()
    fields = {}
    for part in body.split(boundary):
        part = part.strip(b"\r\n")
        if not part or part == b"--" or b"\r\n\r\n" not in part:
            continue
        raw_headers, data = part.split(b"\r\n\r\n", 1)
        data = data.rstrip(b"\r\n")
        header_text = raw_headers.decode("utf-8", errors="ignore")
        name_match = re.search(r'name="([^"]+)"', header_text)
        if not name_match:
            continue
        filename_match = re.search(r'filename="([^"]*)"', header_text)
        fields[name_match.group(1)] = {
            "filename": filename_match.group(1) if filename_match else "",
            "data": data,
            "text": data.decode("utf-8", errors="ignore").strip(),
        }
    return fields


def extract_json_object(value: str) -> dict:
    try:
        return json.loads(value)
    except Exception:
        match = re.search(r"\{.*\}", value or "", re.S)
        if not match:
            return {}
        try:
            return json.loads(match.group(0))
        except Exception:
            return {}


def identify_nameplate(image_bytes: bytes, filename: str) -> dict:
    if not OPENAI_API_KEY:
        raise RuntimeError("OpenAI key not configured")
    mime_type = mimetypes.guess_type(filename)[0] or "image/jpeg"
    encoded = base64.b64encode(image_bytes).decode("ascii")
    prompt = (
        "Read this refrigerator/freezer equipment nameplate. Return JSON only with keys: "
        "brand, model, serial_number, manufacturer, manufacture_date, refrigerant, voltage, raw_text, confidence. "
        "Use null for missing fields. The model is the equipment model number, not the serial number."
    )
    payload = {
        "model": OPENAI_NAMEPLATE_MODEL,
        "input": [{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": f"data:{mime_type};base64,{encoded}", "detail": "high"},
            ],
        }],
    }
    response = httpx.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    output_text = data.get("output_text")
    if not output_text:
        texts = []
        for item in data.get("output", []):
            for content in item.get("content", []):
                if content.get("text"):
                    texts.append(content["text"])
        output_text = "\n".join(texts)
    parsed = extract_json_object(output_text or "")
    parsed.setdefault("raw_text", output_text or "")
    return parsed


def find_product(client: httpx.Client, brand: str, model: str) -> dict | None:
    brand_q = (brand or "").replace("*", "")
    model = canonical_model_for_brand(brand, model)
    model_q = (model or "").replace("*", "")
    if not model_q:
        return None
    variants = model_variants(model_q)
    filters = [
        f"&brand=ilike.*{brand_q}*&equipment_model=ilike.*{variant.replace('*', '')}*" if brand_q else ""
        for variant in variants
    ] + [
        f"&equipment_model=ilike.*{variant.replace('*', '')}*" for variant in variants
    ]
    wanted = normalize_model(model)
    for extra_filter in filters:
        endpoint = f"{SUPABASE_URL}/rest/v1/refrigerator_products?select=*{extra_filter}&limit=20"
        response = client.get(endpoint, headers=supabase_headers())
        response.raise_for_status()
        rows = response.json()
        if not rows:
            continue
        for row in rows:
            if normalize_model(row.get("equipment_model", "")) == wanted:
                return row
        return rows[0]

    digits = re.sub(r"\D", "", wanted)
    if digits:
        base_digits = digits[:3] if len(digits) >= 3 else digits
        brand_filter = f"&brand=ilike.*{brand_q}*" if brand_q else ""
        endpoint = (
            f"{SUPABASE_URL}/rest/v1/refrigerator_products?select=*"
            f"{brand_filter}&equipment_model=ilike.*{base_digits}*&limit=50"
        )
        response = client.get(endpoint, headers=supabase_headers())
        response.raise_for_status()
        candidates = response.json()
        if candidates:
            ranked = sorted(
                candidates,
                key=lambda row: model_similarity_score(model, row.get("equipment_model", "")),
                reverse=True,
            )
            if model_similarity_score(model, ranked[0].get("equipment_model", "")) >= 82:
                return ranked[0]
    return None


def get_product(client: httpx.Client, product_id: int) -> dict | None:
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/refrigerator_products?select=*&id=eq.{product_id}&limit=1",
        headers=supabase_headers(),
    )
    response.raise_for_status()
    rows = response.json()
    return rows[0] if rows else None


def create_product_from_confirmed_model(client: httpx.Client, brand: str, model: str) -> dict:
    payload = {
        "brand": brand,
        "equipment_model": model,
        "data_status": "customer_requested",
        "last_discovered_at": datetime.now(timezone.utc).isoformat(),
    }
    response = client.post(
        f"{SUPABASE_URL}/rest/v1/refrigerator_products",
        headers=supabase_headers("return=representation"),
        json=payload,
    )
    response.raise_for_status()
    rows = response.json()
    return rows[0]


def dimension_key(item: dict) -> str:
    if not item.get("width_in") or not item.get("height_in"):
        return ""
    values = sorted([round(float(item["width_in"]), 2), round(float(item["height_in"]), 2)])
    return f"{values[0]}x{values[1]}"


def is_customer_visible_gasket(item: dict) -> bool:
    name = (item.get("gasket_name") or "").lower()
    source = (item.get("source_name") or "").lower()
    image = (item.get("gasket_image_url") or "").lower()
    if "search result" in name or "logo" in image:
        return False
    has_size = bool(item.get("width_in") and item.get("height_in"))
    has_part = bool(item.get("part_number") or item.get("universal_part_number"))
    has_structured_ai_door = item.get("data_status") == "ai_structured" and bool(item.get("door_position_display"))
    if not has_size and not has_part and not has_structured_ai_door:
        return False
    if source == "restaurant cooler gaskets" and not has_size:
        return False
    return True


def quote_score(item: dict) -> float:
    score = float(item.get("confidence_score") or 0)
    perimeter = float(item.get("perimeter_in") or 0)
    score += min(12, perimeter / 18) if perimeter else 0
    source = (item.get("source_name") or "").lower()
    if "parts town" in source:
        score += 6
    elif "webstaurant" in source:
        score += 3
    if item.get("part_number") or item.get("universal_part_number"):
        score += 4
    return score


def customer_quote_items(items: list[dict]) -> list[dict]:
    grouped = {}
    for item in items:
        if not is_customer_visible_gasket(item):
            continue
        key = dimension_key(item) or item.get("part_number") or item.get("universal_part_number") or item.get("gasket_name")
        if key not in grouped or quote_score(item) > quote_score(grouped[key]):
            grouped[key] = item
    candidates = sorted(grouped.values(), key=quote_score, reverse=True)
    return candidates[:1]


def get_quote_items(client: httpx.Client, product_id: int) -> list[dict]:
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/refrigerator_product_gaskets"
        f"?select=*&refrigerator_product_id=eq.{product_id}&order=door_index.asc",
        headers=supabase_headers(),
    )
    response.raise_for_status()
    return [row for row in response.json() if is_customer_visible_gasket(row)]


def save_inferred_door_layout(client: httpx.Client, product: dict, positions: list[dict]) -> None:
    if product.get("door_positions") or not positions:
        return
    payload = {
        "door_count": len(positions),
        "door_layout": door_layout_name(positions),
        "door_positions": positions,
        "door_layout_confidence": 55,
        "door_layout_source": "model_number_inference",
        "door_layout_updated_at": datetime.now(timezone.utc).isoformat(),
    }
    response = client.patch(
        f"{SUPABASE_URL}/rest/v1/refrigerator_products?id=eq.{product['id']}",
        headers=supabase_headers("return=minimal"),
        json=payload,
    )
    response.raise_for_status()


def trigger_background_refresh(product_id: int, need_image: bool, need_gaskets: bool) -> None:
    if os.getenv("DISABLE_BACKGROUND_CRAWLERS", "1") == "1":
        try:
            from instant_enrichment import start_instant_enrichment

            if need_image or need_gaskets:
                start_instant_enrichment(product_id)
        except Exception as exc:
            print(f"instant enrichment trigger failed for {product_id}: {exc}")
        return
    if product_id in BACKGROUND_REFRESHING:
        return
    if not need_image and not need_gaskets:
        return
    BACKGROUND_REFRESHING.add(product_id)

    def worker() -> None:
        try:
            with httpx.Client(timeout=60) as client:
                product = get_product(client, product_id)
                if not product:
                    return
                if need_image and not product.get("product_image_url"):
                    try:
                        from product_image_search_crawler import (
                            get_existing_candidates,
                            promote_best_image,
                            quick_promote_product_image,
                            search_google_cse,
                            search_public_web_images,
                            search_serpapi,
                            upsert_candidate,
                        )

                        promoted = quick_promote_product_image(client, product)
                        if not promoted:
                            saved = get_existing_candidates(client, product_id)
                            promoted = promote_best_image(client, product, saved)
                        if not promoted:
                            raw = []
                            raw.extend(search_serpapi(client, product))
                            raw.extend(search_google_cse(client, product))
                            if not raw:
                                raw.extend(search_public_web_images(client, product))
                            saved = [upsert_candidate(client, product, row) for row in raw[:20]]
                            promote_best_image(client, product, saved)
                    except Exception as exc:
                        print(f"background image refresh failed for {product_id}: {exc}")
                if need_gaskets:
                    try:
                        from gasket_spec_refresher import refresh_product_gasket_spec

                        refresh_product_gasket_spec(client, product_id)
                    except Exception as exc:
                        print(f"background gasket refresh failed for {product_id}: {exc}")
        finally:
            BACKGROUND_REFRESHING.discard(product_id)

    threading.Thread(target=worker, daemon=True).start()


def estimated_gasket_quantity(product: dict, quote_items: list[dict]) -> int:
    model_text = product.get("equipment_model", "") or ""
    slash_match = re.search(r"/([234])$", model_text)
    if slash_match:
        return int(slash_match.group(1))
    model = normalize_model(model_text)
    brand = (product.get("brand") or "").lower()
    number_match = re.search(r"(\d{2,3})", model)
    if not number_match:
        return max(1, len(quote_items))
    number = int(number_match.group(1))
    if "true" in brand or model.startswith(("T", "TS", "TA", "TR")):
        if number >= 65:
            return 3
        if number >= 40:
            return 2
        return 1
    if number >= 65:
        return 3
    if number >= 40:
        return 2
    return 1


def create_request(client: httpx.Client, customer: dict, upload_url: str | None, brand: str, model: str, product: dict | None, nameplate_data: dict) -> dict:
    confidence = nameplate_data.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
    except Exception:
        confidence = None
    payload = {
        "customer_name": customer.get("customer_name"),
        "customer_email": customer.get("customer_email"),
        "customer_phone": customer.get("customer_phone"),
        "nameplate_image_url": upload_url,
        "ocr_text": nameplate_data.get("raw_text") or f"OpenAI nameplate input: {brand} {model}",
        "detected_brand": brand,
        "detected_model": model,
        "matched_refrigerator_product_id": product.get("id") if product else None,
        "match_score": confidence if confidence is not None else (100 if product else 0),
        "status": "matched" if product else "needs_review",
    }
    response = client.post(
        f"{SUPABASE_URL}/rest/v1/gasket_requests",
        headers=supabase_headers("return=representation"),
        json=payload,
    )
    response.raise_for_status()
    rows = response.json()
    saved = rows[0] if rows else payload
    saved["nameplate_data"] = nameplate_data
    return saved


def page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<style>
body{{margin:0;font-family:Arial,Helvetica,sans-serif;background:#eef3f6;color:#17202a}}
.app-header{{background:white;border-bottom:1px solid #dbe2ea;box-shadow:0 8px 22px rgba(15,29,36,.06)}}
.app-header-inner{{max-width:1180px;margin:0 auto;padding:14px 22px;display:flex;align-items:center;gap:12px}}
.app-logo{{width:38px;height:38px;border-radius:8px;background:#0a6f78;color:white;display:flex;align-items:center;justify-content:center;font-weight:900;letter-spacing:.02em}}
.app-title{{font-size:18px;font-weight:800;color:#17202a;line-height:1.1}}
.app-subtitle{{font-size:12px;color:#687385;margin-top:3px}}
main{{max-width:1180px;margin:0 auto;padding:22px}}
.app-footer{{max-width:1180px;margin:0 auto;padding:0 22px 24px}}
.app-footer-inner{{background:white;border:1px solid #dbe2ea;border-radius:8px;padding:20px;color:#17202a}}
.app-footer-inner strong{{display:block;font-size:18px;margin-bottom:4px}}
section,.checkout{{background:white;border:1px solid #dbe2ea;border-radius:8px;padding:20px;margin-bottom:18px}}
h1{{font-size:34px;margin:0 0 8px}} h2{{font-size:20px;margin:0 0 14px}} p{{color:#687385;line-height:1.55}}
.hero,.result-grid{{display:grid;grid-template-columns:1fr 1fr;gap:22px}} .grid,.summary{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}
.upload-row{{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;align-items:end}}
label{{display:block;font-size:13px;color:#687385;margin-bottom:6px}} input{{width:100%;border:1px solid #dbe2ea;border-radius:6px;padding:10px}}
button,.button{{border:0;border-radius:6px;background:#0a6f78;color:white;min-height:40px;padding:0 16px;font-weight:700;text-decoration:none;display:inline-flex;align-items:center}}
.metric{{border:1px solid #dbe2ea;border-radius:8px;padding:12px;background:#fbfdfe}} .metric span,.muted{{color:#687385}} .metric strong{{font-size:24px}}
.photo{{width:100%;height:320px;object-fit:contain;border:1px solid #dbe2ea;border-radius:8px;background:#f8fafc}}
.plate{{width:100%;height:190px;object-fit:contain;border:1px solid #dbe2ea;border-radius:8px;background:#f8fafc}}
.model-confirm-input{{border:2px solid #d93025!important;background:#fffafa!important;box-shadow:0 0 0 3px rgba(217,48,37,.12)}}
.model-check-notice{{margin-top:8px;border:2px solid #d93025;background:#fff1f0;color:#5f1410;border-radius:8px;padding:10px;font-size:13px;line-height:1.4}}
.model-check-notice strong{{display:block;margin-bottom:4px;color:#3b0906}}
.image-open{{display:block;width:100%;padding:0;border:0;background:transparent;cursor:zoom-in}}
.image-viewer{{position:fixed;inset:0;background:rgba(7,16,22,.88);display:none;z-index:9999}}
.image-viewer.is-open{{display:block}}
.image-viewer-tools{{position:absolute;top:18px;right:18px;display:flex;gap:8px;z-index:2}}
.image-viewer-tools button{{min-width:44px;background:#fff;color:#0f1d24;border-radius:6px}}
.image-viewer-stage{{height:100%;overflow:hidden;display:flex;align-items:center;justify-content:center;cursor:grab}}
.image-viewer-stage:active{{cursor:grabbing}}
.image-viewer-stage img{{max-width:none;max-height:none;transform-origin:center center;user-select:none;pointer-events:none}}
.facts{{display:grid;grid-template-columns:140px 1fr;gap:8px 12px}} .facts div:nth-child(odd){{color:#687385}}
.item{{display:grid;grid-template-columns:34px 98px 1fr 150px 120px;gap:12px;align-items:center;border:1px solid #dbe2ea;border-radius:8px;padding:12px}}
.item img{{width:98px;height:78px;object-fit:contain;border:1px solid #dbe2ea;border-radius:6px}} .price strong{{font-size:24px;display:block}}
.loading{{display:flex;align-items:center;justify-content:center;color:#687385;background:linear-gradient(90deg,#f8fafc,#eef3f6,#f8fafc);background-size:220% 100%;animation:pulse 1.4s ease-in-out infinite}}
@keyframes pulse{{0%{{background-position:0 0}}100%{{background-position:220% 0}}}}
@media(max-width:860px){{.hero,.result-grid,.grid,.summary,.item{{grid-template-columns:1fr}}}}
@media(max-width:860px){{.upload-row{{grid-template-columns:1fr}}}}
</style></head><body><header class="app-header"><div class="app-header-inner"><div class="app-logo">GM</div><div><div class="app-title">{esc(title)}</div><div class="app-subtitle">Refrigerator door gasket identification</div></div></div></header><main>{body}</main><footer class="app-footer"><div class="app-footer-inner"><strong>Ready to order?</strong><span class="muted">Select the gasket solution for this refrigerator.</span></div></footer>
<script>function updateTotal(){{let t=0,c=0;document.querySelectorAll('[data-price]').forEach(b=>{{if(b.checked){{t+=Number(b.dataset.price||0);c++}}}});let a=document.getElementById('selected-total'),n=document.getElementById('selected-count');if(a)a.textContent='$'+t.toFixed(2);if(n)n.textContent=c}}function fmt(s){{let m=Math.floor(s/60),r=s%60;return String(m).padStart(2,'0')+':'+String(r).padStart(2,'0')}}function startLoadingTimers(){{let start=Date.now();setInterval(()=>{{let s=Math.floor((Date.now()-start)/1000);document.querySelectorAll('[data-loading-label]').forEach(el=>{{el.textContent=el.getAttribute('data-loading-label')+' '+fmt(s)}})}},1000)}}function startUploadFeedback(){{let f=document.getElementById('upload');if(!f)return;f.addEventListener('submit',()=>{{let b=f.querySelector('button[type=\"submit\"]');if(b){{b.disabled=true;b.textContent='Reading nameplate...'}}let n=document.createElement('div');n.className='upload-working';n.innerHTML='<strong>Reading nameplate</strong><br><span>AI is extracting the refrigerator model. This usually takes a few seconds.</span>';f.appendChild(n)}})}}function initImageViewer(){{let viewer=document.getElementById('image-viewer'),img=document.getElementById('image-viewer-img');if(!viewer||!img)return;let scale=1,x=0,y=0,drag=false,sx=0,sy=0;function apply(){{img.style.transform='translate('+x+'px,'+y+'px) scale('+scale+')'}}document.querySelectorAll('[data-image-viewer-src]').forEach(btn=>btn.addEventListener('click',()=>{{img.src=btn.getAttribute('data-image-viewer-src');scale=1;x=0;y=0;apply();viewer.classList.add('is-open');viewer.setAttribute('aria-hidden','false')}}));viewer.querySelector('[data-close-viewer]')?.addEventListener('click',()=>{{viewer.classList.remove('is-open');viewer.setAttribute('aria-hidden','true')}});viewer.querySelector('[data-zoom=\"in\"]')?.addEventListener('click',()=>{{scale=Math.min(5,scale+.25);apply()}});viewer.querySelector('[data-zoom=\"out\"]')?.addEventListener('click',()=>{{scale=Math.max(.5,scale-.25);apply()}});viewer.querySelector('.image-viewer-stage')?.addEventListener('pointerdown',e=>{{drag=true;sx=e.clientX-x;sy=e.clientY-y}});window.addEventListener('pointermove',e=>{{if(!drag)return;x=e.clientX-sx;y=e.clientY-sy;apply()}});window.addEventListener('pointerup',()=>drag=false);window.addEventListener('keydown',e=>{{if(e.key==='Escape')viewer.classList.remove('is-open')}})}}document.addEventListener('change',updateTotal);window.addEventListener('load',updateTotal);window.addEventListener('load',startLoadingTimers);window.addEventListener('load',startUploadFeedback);window.addEventListener('load',initImageViewer);function pollProductStatus(){{let el=document.querySelector('[data-refresh-product]');if(!el)return;let id=el.getAttribute('data-refresh-product');let wantsImage=el.getAttribute('data-needs-image')==='1';let wantsGasket=el.getAttribute('data-needs-gasket')==='1';if(!wantsImage&&!wantsGasket)return;setInterval(async()=>{{try{{let r=await fetch('/product-status?product_id='+encodeURIComponent(id),{{cache:'no-store'}});let d=await r.json();if((wantsImage&&d.product_image_url)||(wantsGasket&&d.quote_item_count>0))window.location.reload();}}catch(e){{}}}},2000)}}window.addEventListener('load',pollProductStatus)</script>
</body></html>""".encode("utf-8")


def render_home(message: str = "") -> bytes:
    warning = f"<p style='color:#9f4b12'>{esc(message)}</p>" if message else ""
    return page("Gasket Match", f"""
<section><form id="upload" method="post" action="/read-nameplate" enctype="multipart/form-data"><h2>Upload nameplate</h2>{warning}
<div class="upload-row"><div><label>Nameplate photo</label><input type="file" name="nameplate" accept="image/*"></div><button type="submit">Read nameplate</button></div>
<div class="grid"><div><label>Brand fallback</label><input name="brand"></div><div><label>Model fallback</label><input name="equipment_model"></div></div>
<p class="muted">You can correct the brand or model before matching the database.</p></form></section>""")


def render_confirm_nameplate(upload_url: str, customer: dict, nameplate_data: dict, fallback_brand: str = "", fallback_model: str = "") -> bytes:
    brand = nameplate_data.get("brand") or fallback_brand
    model = nameplate_data.get("model") or fallback_model
    raw_text = nameplate_data.get("raw_text") or ""
    model_notice = """
<div class="model-check-notice">
<strong>Important: confirm the model number exactly.</strong>
AI can misread characters such as 8/S, 1/I, 0/O. The red model box must match the nameplate before you continue.
</div>"""
    return page("Confirm Nameplate", f"""
<section><h2>Confirm nameplate information</h2>
<p>Check the uploaded nameplate against the information below. If anything is wrong, edit it before matching the database.</p>
<div class="result-grid"><div><h3>Nameplate photo</h3><button class="image-open" type="button" data-image-viewer-src="{esc(upload_url)}"><img class="photo" src="{esc(upload_url)}" alt="Uploaded nameplate"></button><p class="muted">Click the nameplate photo to zoom and drag.</p></div>
<form method="post" action="/match" enctype="multipart/form-data"><h3>Read information</h3>
<input type="hidden" name="upload_url" value="{esc(upload_url)}">
<input type="hidden" name="customer_name" value="{esc(customer.get('customer_name') or '')}">
<input type="hidden" name="customer_email" value="{esc(customer.get('customer_email') or '')}">
<input type="hidden" name="customer_phone" value="{esc(customer.get('customer_phone') or '')}">
<div class="grid"><div><label>Brand</label><input name="brand" value="{esc(brand or '')}"></div><div><label>Model</label><input class="model-confirm-input" name="equipment_model" value="{esc(model or '')}">{model_notice}</div><div><label>Serial</label><input name="serial_number" value="{esc(nameplate_data.get('serial_number') or '')}"></div><div><label>Manufacturer</label><input name="manufacturer" value="{esc(nameplate_data.get('manufacturer') or '')}"></div><div><label>Manufacture date</label><input name="manufacture_date" value="{esc(nameplate_data.get('manufacture_date') or '')}"></div><div><label>Refrigerant</label><input name="refrigerant" value="{esc(nameplate_data.get('refrigerant') or '')}"></div><div><label>Voltage</label><input name="voltage" value="{esc(nameplate_data.get('voltage') or '')}"></div></div>
<label>Raw text</label><textarea name="raw_text" style="width:100%;min-height:110px;border:1px solid #dbe2ea;border-radius:6px;padding:10px">{esc(raw_text)}</textarea>
<p><button type="submit">Confirm and match gasket records</button> <a class="button" href="/">Upload another</a></p>
</form></div></section>
<div class="image-viewer" id="image-viewer" aria-hidden="true">
<div class="image-viewer-tools"><button type="button" data-zoom="out">-</button><button type="button" data-zoom="in">+</button><button type="button" data-close-viewer>Close</button></div>
<div class="image-viewer-stage"><img id="image-viewer-img" alt="Nameplate enlarged"></div>
</div>""")


def render_no_match(brand: str, model: str, upload_url: str | None, nameplate_data: dict) -> bytes:
    plate = f"<img class='plate' src='{esc(upload_url)}' alt='Uploaded nameplate'>" if upload_url else ""
    return page("No Match", f"""
<section><h2>&#25105;&#20204;&#27491;&#22312;&#21152;&#36733;&#36164;&#26009;</h2>
<p class="muted">&#24050;&#25910;&#21040;&#35813;&#20912;&#31665;&#22411;&#21495;&#65292;&#31995;&#32479;&#27491;&#22312;&#21305;&#37197;&#20135;&#21697;&#22270;&#29255;&#12289;&#38376;&#20301;&#21644;&#23494;&#23553;&#26465;&#36164;&#26009;&#12290;</p>
{plate}<div class="facts"><div>Brand read</div><div><strong>{esc(brand or 'Not found')}</strong></div><div>Model read</div><div><strong>{esc(model or 'Not found')}</strong></div><div>Serial</div><div>{esc(nameplate_data.get('serial_number') or 'Not found')}</div><div>Raw text</div><div>{esc(nameplate_data.get('raw_text') or '')}</div></div>
<p><a class="button" href="/">Try another nameplate</a></p></section>""")


def render_result(product: dict, quote_items: list[dict], request: dict | None, upload_url: str | None) -> bytes:
    nameplate_data = (request or {}).get("nameplate_data") or {}
    pending_new_product = is_unconfirmed_new_product(product)
    positions = [] if pending_new_product else infer_door_positions(product)
    quantity = 0 if pending_new_product else (len(positions) or estimated_gasket_quantity(product, quote_items))
    trigger_background_refresh(product["id"], not product.get("product_image_url"), not quote_items)
    product_img = product.get("product_image_url")
    needs_image = not bool(product_img)
    needs_gasket = not bool(quote_items)
    loading_banner = "<section><h2>&#25105;&#20204;&#27491;&#22312;&#21152;&#36733;&#36164;&#26009;</h2></section>" if needs_image or needs_gasket else ""
    product_html = f"<img class='photo' src='{esc(product_img)}' alt='Refrigerator product image'>" if product_img else "<div class='photo loading'><span data-loading-label='鍥剧墖姝ｅ湪鍔犺浇'>鍥剧墖姝ｅ湪鍔犺浇 00:00</span></div>"
    plate_html = f"<img class='plate' src='{esc(upload_url)}' alt='Uploaded nameplate'>" if upload_url else "<div class='plate muted'>Nameplate photo</div>"
    rows = []
    primary_item = quote_items[0] if quote_items else None
    if pending_new_product and not primary_item:
        rows.append("""<div class="item"><input type="checkbox" disabled><div class="loading" style="width:98px;height:78px;border:1px solid #dbe2ea;border-radius:6px"><span data-loading-label="瀵嗗皝鏉¤祫鏂欐鍦ㄥ姞杞?>瀵嗗皝鏉¤祫鏂欐鍦ㄥ姞杞?00:00</span></div><div><strong>瀵嗗皝鏉¤祫鏂欐鍦ㄥ姞杞?/strong></div><div class="price"><strong>Loading</strong></div><div></div></div>""")
    for index, position in enumerate(positions or door_positions_for_count(quantity), start=1):
        item = primary_item
        door_label = position.get("label") or f"Door {index}"
        door_key = position.get("key") or f"door_{index}"
        if not item:
            rows.append(f"""<div class="item"><input type="checkbox" disabled><div class="loading" style="width:98px;height:78px;border:1px solid #dbe2ea;border-radius:6px"><span data-loading-label="瀵嗗皝鏉¤祫鏂欐鍦ㄥ姞杞?>瀵嗗皝鏉¤祫鏂欐鍦ㄥ姞杞?00:00</span></div><div><strong>{esc(door_label)} Gasket</strong></div><div class="price"><strong>Loading</strong></div><div><small class="muted">Door</small><br><strong>{esc(door_key)}</strong></div></div>""")
            continue
        price = float(item.get("final_price_usd") or 0)
        line_price = price
        checked = "checked"
        image = item.get("gasket_image_url")
        image_html = f"<img src='{esc(image)}' alt='Gasket image'>" if image else "<div class='muted'>No gasket image</div>"
        dims = item.get("dimensions_text") or f"{item.get('width_in') or '-'} x {item.get('height_in') or '-'} in"
        part_number = item.get("part_number") or item.get("universal_part_number")
        part_html = f"<div><small class='muted'>Part</small><br><strong>{esc(part_number)}</strong></div>" if part_number else "<div></div>"
        rows.append(f"""<label class="item"><input type="checkbox" name="door_position" value="{esc(door_key)}" data-price="{line_price}" {checked}>{image_html}<div><strong>{esc(door_label)} Gasket</strong><p>{esc(dims)}<br>Perimeter: {esc(item.get('perimeter_in') or 'TBD')} in<br>Source: {esc(item.get('source_name'))}</p></div><div class="price"><strong>{money(line_price)}</strong><small>each selected door</small></div>{part_html}</label>""")
    summary_html = "" if pending_new_product else f"""<div class="summary"><div class="metric"><span>Required gaskets</span><strong>{quantity}</strong></div><div class="metric"><span>Selected</span><strong id="selected-count">0</strong></div><div class="metric"><span>Total</span><strong id="selected-total">$0.00</strong></div></div>"""
    return page("Matched Gasket Quote", f"""
<div data-refresh-product="{esc(product['id'])}" data-needs-image="{1 if needs_image else 0}" data-needs-gasket="{1 if needs_gasket else 0}" hidden></div>
{loading_banner}<section><h2>Matched refrigerator</h2><div class="result-grid"><div><h3>Refrigerator image</h3>{product_html}</div><div><h3>Nameplate</h3>{plate_html}</div><div><h3>Nameplate summary</h3><div class="facts"><div>OpenAI brand</div><div><strong>{esc(nameplate_data.get('brand') or product.get('brand'))}</strong></div><div>OpenAI model</div><div><strong>{esc(nameplate_data.get('model') or product.get('equipment_model'))}</strong></div><div>Serial</div><div>{esc(nameplate_data.get('serial_number') or 'Not found')}</div><div>Brand</div><div><strong>{esc(product.get('brand'))}</strong></div><div>Model</div><div><strong>{esc(product.get('equipment_model'))}</strong></div></div></div></div></section>
<section><h2>Gasket quote</h2>{summary_html}<div>{''.join(rows) if rows else '<p class="muted">No quote items yet.</p>'}</div></section>""")



def render_result(product: dict, quote_items: list[dict], request: dict | None, upload_url: str | None) -> bytes:
    nameplate_data = (request or {}).get("nameplate_data") or {}
    pending_new_product = is_unconfirmed_new_product(product)
    positions = [] if pending_new_product else infer_door_positions(product)
    quantity = 0 if pending_new_product else (len(positions) or estimated_gasket_quantity(product, quote_items))
    trigger_background_refresh(product["id"], not product.get("product_image_url"), not quote_items)
    product_img = product.get("product_image_url")
    needs_image = not bool(product_img)
    needs_gasket = not bool(quote_items)
    loading_banner = "<section><h2>&#25105;&#20204;&#27491;&#22312;&#21152;&#36733;&#36164;&#26009;</h2></section>" if needs_image or needs_gasket else ""
    product_loading = "&#22270;&#29255;&#27491;&#22312;&#21152;&#36733;"
    gasket_loading = "&#23494;&#23553;&#26465;&#36164;&#26009;&#27491;&#22312;&#21152;&#36733;"
    product_html = f"<img class='photo' src='{esc(product_img)}' alt='Refrigerator product image'>" if product_img else f"<div class='photo loading'><span data-loading-label='{product_loading}'>{product_loading} 00:00</span></div>"
    plate_html = f"<img class='plate' src='{esc(upload_url)}' alt='Uploaded nameplate'>" if upload_url else "<div class='plate muted'>Nameplate photo</div>"

    rows = []
    primary_item = quote_items[0] if quote_items else None
    if pending_new_product and not primary_item:
        rows.append(f"""<div class="item"><input type="checkbox" disabled><div class="loading" style="width:98px;height:78px;border:1px solid #dbe2ea;border-radius:6px"><span data-loading-label="{gasket_loading}">{gasket_loading} 00:00</span></div><div><strong>{gasket_loading}</strong></div><div class="price"><strong>Loading</strong></div><div></div></div>""")

    for index, position in enumerate(positions or door_positions_for_count(quantity), start=1):
        item = primary_item
        door_label = position.get("label") or f"Door {index}"
        door_key = position.get("key") or f"door_{index}"
        if not item:
            rows.append(f"""<div class="item"><input type="checkbox" disabled><div class="loading" style="width:98px;height:78px;border:1px solid #dbe2ea;border-radius:6px"><span data-loading-label="{gasket_loading}">{gasket_loading} 00:00</span></div><div><strong>{esc(door_label)} Gasket</strong></div><div class="price"><strong>Loading</strong></div><div><small class="muted">Door</small><br><strong>{esc(door_key)}</strong></div></div>""")
            continue
        price = float(item.get("final_price_usd") or 0)
        line_price = price
        image = item.get("gasket_image_url")
        image_html = f"<img src='{esc(image)}' alt='Gasket image'>" if image else "<div class='muted'>No gasket image</div>"
        dims = item.get("dimensions_text") or f"{item.get('width_in') or '-'} x {item.get('height_in') or '-'} in"
        perimeter = item.get("perimeter_in")
        perimeter_html = f"<br>Perimeter: {esc(perimeter)} in" if perimeter not in (None, "") else ""
        part_number = item.get("part_number") or item.get("universal_part_number")
        part_html = f"<div><small class='muted'>Part</small><br><strong>{esc(part_number)}</strong></div>" if part_number else "<div></div>"
        rows.append(f"""<label class="item"><input type="checkbox" name="door_position" value="{esc(door_key)}" data-price="{line_price}" checked>{image_html}<div><strong>{esc(door_label)} Gasket</strong><p>{esc(dims)}{perimeter_html}<br>Source: {esc(item.get('source_name'))}</p></div><div class="price"><strong>{money(line_price)}</strong><small>each selected door</small></div>{part_html}</label>""")

    summary_html = "" if pending_new_product else f"""<div class="summary"><div class="metric"><span>Required gaskets</span><strong>{quantity}</strong></div><div class="metric"><span>Selected</span><strong id="selected-count">0</strong></div><div class="metric"><span>Total</span><strong id="selected-total">$0.00</strong></div></div>"""
    rows_html = "".join(rows) if rows else f"""<div class="item"><input type="checkbox" disabled><div class="loading" style="width:98px;height:78px;border:1px solid #dbe2ea;border-radius:6px"><span data-loading-label="{gasket_loading}">{gasket_loading} 00:00</span></div><div><strong>{gasket_loading}</strong></div><div class="price"><strong>Loading</strong></div><div></div></div>"""
    return page("Matched Gasket Quote", f"""
<div data-refresh-product="{esc(product['id'])}" data-needs-image="{1 if needs_image else 0}" data-needs-gasket="{1 if needs_gasket else 0}" hidden></div>
{loading_banner}<section><h2>Matched refrigerator</h2><div class="result-grid"><div><h3>Refrigerator image</h3>{product_html}</div><div><h3>Nameplate</h3>{plate_html}</div><div><h3>Nameplate summary</h3><div class="facts"><div>OpenAI brand</div><div><strong>{esc(nameplate_data.get('brand') or product.get('brand'))}</strong></div><div>OpenAI model</div><div><strong>{esc(nameplate_data.get('model') or product.get('equipment_model'))}</strong></div><div>Serial</div><div>{esc(nameplate_data.get('serial_number') or 'Not found')}</div><div>Brand</div><div><strong>{esc(product.get('brand'))}</strong></div><div>Model</div><div><strong>{esc(product.get('equipment_model'))}</strong></div></div></div></div></section>
<section><h2>Gasket quote</h2>{summary_html}<div>{rows_html}</div></section>""")


class Handler(BaseHTTPRequestHandler):
    def do_HEAD(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

    def send_html(self, data: bytes, status: int = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_html(render_home())
            return
        if parsed.path in {"/read-nameplate", "/match"}:
            self.send_html(render_home("Upload a nameplate photo to start a new match."))
            return
        if parsed.path.startswith("/uploads/"):
            target = (ROOT / parsed.path.lstrip("/")).resolve()
            if not str(target).startswith(str((ROOT / "uploads").resolve())) or not target.exists():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data = target.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/preview":
            product_id = int(parse_qs(parsed.query).get("product_id", ["39"])[0])
            with httpx.Client(timeout=30) as client:
                product = get_product(client, product_id)
                if not product:
                    self.send_html(page("Product Not Found", "<section><h2>Product not found</h2><p class='muted'>This product record is not available yet.</p><p><a class='button' href='/'>Start a new match</a></p></section>"), HTTPStatus.NOT_FOUND)
                    return
                positions = [] if is_unconfirmed_new_product(product) else infer_door_positions(product)
                if positions:
                    save_inferred_door_layout(client, product, positions)
                    product["door_positions"] = positions
                    product["door_count"] = len(positions)
                self.send_html(render_result(product, get_quote_items(client, product_id), None, None))
            return
        if parsed.path == "/product-status":
            product_id = int(parse_qs(parsed.query).get("product_id", ["0"])[0])
            with httpx.Client(timeout=30) as client:
                product = get_product(client, product_id)
                quote_items = get_quote_items(client, product_id) if product else []
            data = {
                "product_image_url": product.get("product_image_url") if product else None,
                "quote_item_count": len(quote_items),
            }
            payload = json.dumps(data).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_html(page("Page Not Found", "<section><h2>Page not found</h2><p class='muted'>Start from the upload page and match a refrigerator nameplate.</p><p><a class='button' href='/'>Go to upload</a></p></section>"), HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in {"/read-nameplate", "/match"}:
            self.send_html(page("Page Not Found", "<section><h2>Page not found</h2><p class='muted'>Start from the upload page and match a refrigerator nameplate.</p><p><a class='button' href='/'>Go to upload</a></p></section>"), HTTPStatus.NOT_FOUND)
            return
        fields = parse_multipart(self.rfile.read(int(self.headers.get("Content-Length", "0"))), self.headers.get("Content-Type", ""))
        brand = fields.get("brand", {}).get("text", "").strip()
        model = fields.get("equipment_model", {}).get("text", "").strip()
        upload_url = fields.get("upload_url", {}).get("text", "").strip() or None
        nameplate_data = {}
        file_field = fields.get("nameplate")
        customer = {key: fields.get(key, {}).get("text") or None for key in ("customer_name", "customer_email", "customer_phone")}
        if path == "/read-nameplate":
            if not (file_field and file_field.get("filename") and file_field.get("data")):
                self.send_html(render_home("Please upload a nameplate photo first."), HTTPStatus.BAD_REQUEST)
                return
            saved_name = f"{uuid.uuid4().hex}{Path(file_field['filename']).suffix or '.jpg'}"
            (UPLOAD_DIR / saved_name).write_bytes(file_field["data"])
            upload_url = f"/uploads/customer_nameplates/{saved_name}"
            try:
                nameplate_data = identify_nameplate(file_field["data"], file_field["filename"])
            except Exception as exc:
                self.send_html(render_home(f"Nameplate recognition failed: {exc}"), HTTPStatus.BAD_REQUEST)
                return
            prefill_brand = nameplate_data.get("brand") or brand
            prefill_model = nameplate_data.get("model") or model
            if prefill_brand and prefill_model:
                try:
                    from instant_enrichment import start_instant_enrichment, upsert_known_product_from_nameplate

                    with httpx.Client(timeout=20) as client:
                        product = upsert_known_product_from_nameplate(client, prefill_brand, prefill_model, nameplate_data)
                        start_instant_enrichment(product["id"], nameplate_data)
                except Exception as exc:
                    print(f"instant pre-enrichment failed for {prefill_brand} {prefill_model}: {exc}")
            self.send_html(render_confirm_nameplate(upload_url, customer, nameplate_data, brand, model))
            return

        nameplate_data = {
            "brand": brand or None,
            "model": model or None,
            "serial_number": fields.get("serial_number", {}).get("text") or None,
            "manufacturer": fields.get("manufacturer", {}).get("text") or None,
            "manufacture_date": fields.get("manufacture_date", {}).get("text") or None,
            "refrigerant": fields.get("refrigerant", {}).get("text") or None,
            "voltage": fields.get("voltage", {}).get("text") or None,
            "raw_text": fields.get("raw_text", {}).get("text") or "",
            "confidence": 100,
        }
        if not brand or not model:
            self.send_html(render_confirm_nameplate(upload_url or "", customer, nameplate_data, brand, model), HTTPStatus.BAD_REQUEST)
            return
        with httpx.Client(timeout=30) as client:
            product = find_product(client, brand, model)
            if not product:
                product = create_product_from_confirmed_model(client, brand, model)
            try:
                from instant_enrichment import start_instant_enrichment, upsert_known_product_from_nameplate, wait_for_core_result

                product = upsert_known_product_from_nameplate(client, brand, model, nameplate_data, status="customer_confirmed")
                start_instant_enrichment(product["id"], nameplate_data)
                waited = wait_for_core_result(product["id"], max_seconds=10)
                if waited.get("product"):
                    product = waited["product"]
            except Exception as exc:
                print(f"instant enrichment start failed for {brand} {model}: {exc}")
            request = create_request(client, customer, upload_url, brand, model, product, nameplate_data)
            positions = [] if is_unconfirmed_new_product(product) else infer_door_positions(product)
            if positions:
                save_inferred_door_layout(client, product, positions)
                product["door_positions"] = positions
                product["door_count"] = len(positions)
            self.send_html(render_result(product, get_quote_items(client, product["id"]), request, upload_url))


def main() -> None:
    port = int(os.getenv("PORT") or os.getenv("CUSTOMER_DEMO_PORT", "8010"))
    print(f"Nameplate web app running on port {port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
