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
    if not has_size and not has_part:
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
        f"{SUPABASE_URL}/rest/v1/refrigerator_product_quote_items"
        f"?select=*&refrigerator_product_id=eq.{product_id}&order=confidence_score.desc.nullslast",
        headers=supabase_headers(),
    )
    response.raise_for_status()
    return customer_quote_items(response.json())


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
                            search_google_cse,
                            search_public_web_images,
                            search_serpapi,
                            upsert_candidate,
                        )

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
header{{background:#0f1d24;color:white;padding:22px 28px}} main{{max-width:1180px;margin:0 auto;padding:22px}}
section,.checkout{{background:white;border:1px solid #dbe2ea;border-radius:8px;padding:20px;margin-bottom:18px}}
h1{{font-size:34px;margin:0 0 8px}} h2{{font-size:20px;margin:0 0 14px}} p{{color:#687385;line-height:1.55}}
.hero,.result-grid{{display:grid;grid-template-columns:1fr 1fr;gap:22px}} .grid,.summary{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}
label{{display:block;font-size:13px;color:#687385;margin-bottom:6px}} input{{width:100%;border:1px solid #dbe2ea;border-radius:6px;padding:10px}}
button,.button{{border:0;border-radius:6px;background:#0a6f78;color:white;min-height:40px;padding:0 16px;font-weight:700;text-decoration:none;display:inline-flex;align-items:center}}
.metric{{border:1px solid #dbe2ea;border-radius:8px;padding:12px;background:#fbfdfe}} .metric span,.muted{{color:#687385}} .metric strong{{font-size:24px}}
.photo{{width:100%;height:320px;object-fit:contain;border:1px solid #dbe2ea;border-radius:8px;background:#f8fafc}}
.plate{{width:100%;height:190px;object-fit:contain;border:1px solid #dbe2ea;border-radius:8px;background:#f8fafc}}
.facts{{display:grid;grid-template-columns:140px 1fr;gap:8px 12px}} .facts div:nth-child(odd){{color:#687385}}
.item{{display:grid;grid-template-columns:34px 98px 1fr 150px 120px;gap:12px;align-items:center;border:1px solid #dbe2ea;border-radius:8px;padding:12px}}
.item img{{width:98px;height:78px;object-fit:contain;border:1px solid #dbe2ea;border-radius:6px}} .price strong{{font-size:24px;display:block}}
.loading{{display:flex;align-items:center;justify-content:center;color:#687385;background:linear-gradient(90deg,#f8fafc,#eef3f6,#f8fafc);background-size:220% 100%;animation:pulse 1.4s ease-in-out infinite}}
@keyframes pulse{{0%{{background-position:0 0}}100%{{background-position:220% 0}}}}
@media(max-width:860px){{.hero,.result-grid,.grid,.summary,.item{{grid-template-columns:1fr}}}}
</style></head><body><header><strong>Refrigerator Door Gasket Match</strong></header><main>{body}</main>
<script>function updateTotal(){{let t=0,c=0;document.querySelectorAll('[data-price]').forEach(b=>{{if(b.checked){{t+=Number(b.dataset.price||0);c++}}}});let a=document.getElementById('selected-total'),n=document.getElementById('selected-count');if(a)a.textContent='$'+t.toFixed(2);if(n)n.textContent=c}}document.addEventListener('change',updateTotal);window.addEventListener('load',updateTotal);function pollProductStatus(){{let el=document.querySelector('[data-refresh-product]');if(!el)return;let id=el.getAttribute('data-refresh-product');let wantsImage=el.getAttribute('data-needs-image')==='1';let wantsGasket=el.getAttribute('data-needs-gasket')==='1';if(!wantsImage&&!wantsGasket)return;setInterval(async()=>{{try{{let r=await fetch('/product-status?product_id='+encodeURIComponent(id),{{cache:'no-store'}});let d=await r.json();if((wantsImage&&d.product_image_url)||(wantsGasket&&d.quote_item_count>0))window.location.reload();}}catch(e){{}}}},5000)}}window.addEventListener('load',pollProductStatus)</script>
</body></html>""".encode("utf-8")


def render_home(message: str = "") -> bytes:
    warning = f"<p style='color:#9f4b12'>{esc(message)}</p>" if message else ""
    return page("Gasket Match", f"""
<section class="hero"><div><h1>Find the Right Refrigerator Door Gasket Fast</h1>
<p>Upload the equipment nameplate. We read it first, you confirm the details, then the site matches the live database.</p>
<div class="summary"><div class="metric"><span>Step 1</span><strong>Upload</strong></div><div class="metric"><span>Step 2</span><strong>Confirm</strong></div><div class="metric"><span>Step 3</span><strong>Match</strong></div></div>
</div><form method="post" action="/read-nameplate" enctype="multipart/form-data"><h2>Upload nameplate</h2>{warning}
<div class="grid"><div><label>Nameplate photo</label><input type="file" name="nameplate" accept="image/*"></div><div><label>Brand fallback</label><input name="brand"></div><div><label>Model fallback</label><input name="equipment_model"></div><div><label>Customer name</label><input name="customer_name"></div><div><label>Email</label><input name="customer_email"></div><div><label>Phone</label><input name="customer_phone"></div></div>
<p><button type="submit">Read nameplate</button></p><p class="muted">You can correct the brand or model before matching the database.</p></form></section>""")


def render_confirm_nameplate(upload_url: str, customer: dict, nameplate_data: dict, fallback_brand: str = "", fallback_model: str = "") -> bytes:
    brand = nameplate_data.get("brand") or fallback_brand
    model = nameplate_data.get("model") or fallback_model
    raw_text = nameplate_data.get("raw_text") or ""
    return page("Confirm Nameplate", f"""
<section><h2>Confirm nameplate information</h2>
<p>Check the uploaded nameplate against the information below. If anything is wrong, edit it before matching the database.</p>
<div class="result-grid"><div><h3>Nameplate photo</h3><img class="photo" src="{esc(upload_url)}" alt="Uploaded nameplate"></div>
<form method="post" action="/match" enctype="multipart/form-data"><h3>Read information</h3>
<input type="hidden" name="upload_url" value="{esc(upload_url)}">
<input type="hidden" name="customer_name" value="{esc(customer.get('customer_name') or '')}">
<input type="hidden" name="customer_email" value="{esc(customer.get('customer_email') or '')}">
<input type="hidden" name="customer_phone" value="{esc(customer.get('customer_phone') or '')}">
<div class="grid"><div><label>Brand</label><input name="brand" value="{esc(brand or '')}"></div><div><label>Model</label><input name="equipment_model" value="{esc(model or '')}"></div><div><label>Serial</label><input name="serial_number" value="{esc(nameplate_data.get('serial_number') or '')}"></div><div><label>Manufacturer</label><input name="manufacturer" value="{esc(nameplate_data.get('manufacturer') or '')}"></div><div><label>Manufacture date</label><input name="manufacture_date" value="{esc(nameplate_data.get('manufacture_date') or '')}"></div><div><label>Refrigerant</label><input name="refrigerant" value="{esc(nameplate_data.get('refrigerant') or '')}"></div><div><label>Voltage</label><input name="voltage" value="{esc(nameplate_data.get('voltage') or '')}"></div></div>
<label>Raw text</label><textarea name="raw_text" style="width:100%;min-height:110px;border:1px solid #dbe2ea;border-radius:6px;padding:10px">{esc(raw_text)}</textarea>
<p><button type="submit">Confirm and match database</button> <a class="button" href="/">Upload another</a></p>
</form></div></section>""")


def render_no_match(brand: str, model: str, upload_url: str | None, nameplate_data: dict) -> bytes:
    plate = f"<img class='plate' src='{esc(upload_url)}' alt='Uploaded nameplate'>" if upload_url else ""
    return page("No Match", f"""
<section><h2>Database has no record for this model yet</h2>
<p>OpenAI read the nameplate, but no database product matched <strong>{esc(brand)} {esc(model)}</strong>.</p>
{plate}<div class="facts"><div>Brand read</div><div><strong>{esc(brand or 'Not found')}</strong></div><div>Model read</div><div><strong>{esc(model or 'Not found')}</strong></div><div>Serial</div><div>{esc(nameplate_data.get('serial_number') or 'Not found')}</div><div>Raw text</div><div>{esc(nameplate_data.get('raw_text') or '')}</div></div>
<p><a class="button" href="/">Try another nameplate</a></p></section>""")


def render_result(product: dict, quote_items: list[dict], request: dict | None, upload_url: str | None) -> bytes:
    nameplate_data = (request or {}).get("nameplate_data") or {}
    positions = infer_door_positions(product)
    quantity = len(positions) or estimated_gasket_quantity(product, quote_items)
    trigger_background_refresh(product["id"], not product.get("product_image_url"), not quote_items)
    product_img = product.get("product_image_url")
    needs_image = not bool(product_img)
    needs_gasket = not bool(quote_items)
    product_html = f"<img class='photo' src='{esc(product_img)}' alt='Refrigerator product image'>" if product_img else "<div class='photo loading'>Loading product image...</div>"
    plate_html = f"<img class='plate' src='{esc(upload_url)}' alt='Uploaded nameplate'>" if upload_url else "<div class='plate muted'>Nameplate photo</div>"
    rows = []
    primary_item = quote_items[0] if quote_items else None
    for index, position in enumerate(positions or door_positions_for_count(quantity), start=1):
        item = primary_item
        door_label = position.get("label") or f"Door {index}"
        door_key = position.get("key") or f"door_{index}"
        if not item:
            rows.append(f"""<div class="item"><input type="checkbox" disabled><div class="loading" style="width:98px;height:78px;border:1px solid #dbe2ea;border-radius:6px">Loading</div><div><strong>{esc(door_label)} Gasket</strong><p>Reading gasket data for this door...</p></div><div class="price"><strong>Loading</strong><small>Price pending</small></div><div><small class="muted">Door</small><br><strong>{esc(door_key)}</strong></div></div>""")
            continue
        price = float(item.get("final_price_usd") or 0)
        line_price = price
        checked = "checked"
        image = item.get("gasket_image_url")
        image_html = f"<img src='{esc(image)}' alt='Gasket image'>" if image else "<div class='muted'>No gasket image</div>"
        dims = item.get("dimensions_text") or f"{item.get('width_in') or '-'} x {item.get('height_in') or '-'} in"
        rows.append(f"""<label class="item"><input type="checkbox" name="door_position" value="{esc(door_key)}" data-price="{line_price}" {checked}>{image_html}<div><strong>{esc(door_label)} Gasket</strong><p>{esc(dims)}<br>Perimeter: {esc(item.get('perimeter_in') or 'TBD')} in<br>Source: {esc(item.get('source_name'))}</p></div><div class="price"><strong>{money(line_price)}</strong><small>each selected door</small></div><div><small class="muted">Part</small><br><strong>{esc(item.get('part_number') or item.get('universal_part_number') or 'TBD')}</strong></div></label>""")
    return page("Matched Gasket Quote", f"""
<div data-refresh-product="{esc(product['id'])}" data-needs-image="{1 if needs_image else 0}" data-needs-gasket="{1 if needs_gasket else 0}" hidden></div>
<section><h2>Matched refrigerator</h2><div class="result-grid"><div><h3>Refrigerator image</h3>{product_html}</div><div><h3>Nameplate</h3>{plate_html}</div><div><h3>Nameplate summary</h3><div class="facts"><div>OpenAI brand</div><div><strong>{esc(nameplate_data.get('brand') or product.get('brand'))}</strong></div><div>OpenAI model</div><div><strong>{esc(nameplate_data.get('model') or product.get('equipment_model'))}</strong></div><div>Serial</div><div>{esc(nameplate_data.get('serial_number') or 'Not found')}</div><div>Brand</div><div><strong>{esc(product.get('brand'))}</strong></div><div>Model</div><div><strong>{esc(product.get('equipment_model'))}</strong></div></div></div></div></section>
<section><h2>Gasket quote</h2><div class="summary"><div class="metric"><span>Required gaskets</span><strong>{quantity}</strong></div><div class="metric"><span>Selected</span><strong id="selected-count">0</strong></div><div class="metric"><span>Total</span><strong id="selected-total">$0.00</strong></div></div><div>{''.join(rows) if rows else '<p class="muted">No quote items yet.</p>'}</div></section>
<div class="checkout"><strong>Ready to order?</strong><br><span class="muted">Select the exact door gaskets you want to replace.</span></div>""")


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
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                positions = infer_door_positions(product)
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
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in {"/read-nameplate", "/match"}:
            self.send_error(HTTPStatus.NOT_FOUND)
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
            request = create_request(client, customer, upload_url, brand, model, product, nameplate_data)
            if not product:
                self.send_html(render_no_match(brand, model, upload_url, nameplate_data))
                return
            positions = infer_door_positions(product)
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
