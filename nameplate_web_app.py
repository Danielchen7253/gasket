import base64
from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import html
import json
import mimetypes
import os
import re
import secrets
import time
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlencode

import httpx
from dotenv import load_dotenv


load_dotenv(Path(__file__).with_name(".env"))

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_NAMEPLATE_MODEL = os.getenv("OPENAI_NAMEPLATE_MODEL", "gpt-4.1-mini")
SHOPIFY_STORE_DOMAIN = os.getenv("SHOPIFY_STORE_DOMAIN", "").strip()
SHOPIFY_PAYMENT_TEMPLATE = os.getenv("SHOPIFY_PAYMENT_TEMPLATE", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
ADMIN_SESSION_SECRET = os.getenv("ADMIN_SESSION_SECRET", SUPABASE_SERVICE_ROLE_KEY).strip()
ADMIN_COOKIE_NAME = "gasket_admin"
ADMIN_SESSION_MAX_AGE_SECONDS = 8 * 3600
ADMIN_PAGE_SIZE = 20

ROOT = Path(__file__).resolve().parent
UPLOAD_DIR = ROOT / "uploads" / "customer_nameplates"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
BACKGROUND_REFRESHING: set[int] = set()
MATCH_JOBS_REFRESHING: set[int] = set()
MATCH_JOB_TABLE = "match_jobs"
MATCH_STATUS_PENDING = "pending"
MATCH_STATUS_DONE = "done"
MATCH_STATUS_FAILED = "failed"
MATCH_STATUS_VERIFY = "verify"
MATCH_STATUS_STUCK = "stuck"
MATCH_STAGES = [
    "image_fill",
    "door_layout_fill",
    "gasket_fill",
    "verify",
]
MATCH_TASK_STAGES = [
    MATCH_STATUS_PENDING,
    "image_fill",
    "door_layout_fill",
    "gasket_fill",
    MATCH_STATUS_VERIFY,
    MATCH_STATUS_DONE,
    MATCH_STATUS_FAILED,
    MATCH_STATUS_STUCK,
]
MATCH_STAGE_TIMEOUT_SECONDS = 120
MATCH_STAGE_TIMEOUTS = {
    "image_fill": 120,
    "door_layout_fill": 90,
    "gasket_fill": 180,
    MATCH_STATUS_VERIFY: 60,
}
MATCH_STAGE_MAX_RETRIES = 3

_MATCH_JOB_COLUMNS: set[str] | None = None

STAGE_STATUS_MESSAGE: dict[str, str] = {
    "image_fill": "System is loading product image",
    "door_layout_fill": "System is loading door layout",
    "gasket_fill": "System is loading gasket candidates",
    "verify": "System is validating and preparing quote",
}


def match_job_columns(client: httpx.Client) -> set[str]:
    global _MATCH_JOB_COLUMNS
    if _MATCH_JOB_COLUMNS is not None:
        return _MATCH_JOB_COLUMNS
    if not table_exists(client, MATCH_JOB_TABLE):
        _MATCH_JOB_COLUMNS = {
            "id",
            "request_id",
            "refrigerator_product_id",
            "brand",
            "equipment_model",
            "job_status",
            "pipeline_stage",
            "missing_fields",
            "last_error",
            "last_heartbeat_at",
            "next_retry_at",
            "retry_count",
            "max_retries",
            "started_at",
            "ended_at",
            "created_at",
            "updated_at",
        }
        return _MATCH_JOB_COLUMNS
    endpoint = (
        f"{SUPABASE_URL}/rest/v1/information_schema.columns?select=column_name"
        "&table_schema=eq.public&table_name=eq.match_jobs"
    )
    try:
        response = client.get(endpoint, headers=supabase_headers())
        response.raise_for_status()
        rows = response.json()
        if isinstance(rows, list):
            _MATCH_JOB_COLUMNS = {row.get("column_name") for row in rows if row.get("column_name")}
            return _MATCH_JOB_COLUMNS
    except Exception:
        pass
    _MATCH_JOB_COLUMNS = {
        "id",
        "request_id",
        "refrigerator_product_id",
        "brand",
        "equipment_model",
        "job_status",
        "pipeline_stage",
        "missing_fields",
        "last_error",
        "last_heartbeat_at",
        "next_retry_at",
        "retry_count",
        "max_retries",
        "started_at",
        "ended_at",
        "created_at",
        "updated_at",
    }
    return _MATCH_JOB_COLUMNS


def _filter_job_payload(payload: dict, columns: set[str]) -> dict:
    return {key: value for key, value in payload.items() if key in columns}


def update_job_retry_backoff(attempt: int) -> int:
    return min(10, max(2, 2 ** attempt))


def _remaining_stage_seconds(job: dict | None) -> int | None:
    if not job:
        return None
    if job.get("job_status") == MATCH_STATUS_DONE:
        return 0
    stage = job.get("pipeline_stage") if isinstance(job.get("pipeline_stage"), str) else None
    timeout = MATCH_STAGE_TIMEOUTS.get(stage or "", None)
    if not timeout:
        return None
    start_at = parse_iso_datetime(job.get("pipeline_stage_started_at") or job.get("last_heartbeat_at"))
    if not start_at:
        return timeout
    remaining = int((start_at + timedelta(seconds=timeout) - datetime.now(timezone.utc)).total_seconds())
    return remaining if remaining > 0 else 0


def _set_stage_started_if_needed(
    client: httpx.Client, *,
    product_id: int | None,
    request_id: str | None,
    next_stage: str,
    payload: dict,
) -> dict:
    columns = match_job_columns(client)
    existing = fetch_match_job(client, product_id=product_id, request_id=request_id)
    previous_stage = existing.get("pipeline_stage") if existing else None
    now = now_iso()
    if previous_stage != next_stage and "pipeline_stage_started_at" in columns:
        payload["pipeline_stage_started_at"] = now
        if "pipeline_stage_error_count" in columns:
            payload["pipeline_stage_error_count"] = 0
    return payload


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def generate_request_id() -> str:
    return str((uuid.uuid4().int % 9_000_000_000_000) + 1_000_000_000_000)


def esc(value) -> str:
    return "" if value is None else html.escape(str(value), quote=True)


def money(value) -> str:
    return "TBD" if value in (None, "") else f"${float(value):,.2f}"


def rest_url(table: str, params: dict[str, object] | None = None) -> str:
    query = urlencode([(k, v) for k, v in (params or {}).items() if v is not None], doseq=True)
    return f"{SUPABASE_URL}/rest/v1/{table}" + (f"?{query}" if query else "")


def content_range_total(response: httpx.Response) -> int | None:
    content_range = response.headers.get("Content-Range", "")
    if "/" not in content_range:
        return None
    try:
        total = content_range.split("/")[-1]
        return None if total == "*" else int(total)
    except Exception:
        return None


def fetch_rows(
    client: httpx.Client,
    table: str,
    *,
    select: str = "*",
    order: str | None = None,
    limit: int = 50,
    offset: int | None = None,
    filters: dict[str, object] | None = None,
    extra_params: list[tuple[str, object]] | None = None,
) -> list[dict]:
    params: list[tuple[str, object]] = [("select", select), ("limit", limit)]
    if order:
        params.append(("order", order))
    if offset is not None:
        params.append(("offset", offset))
    if extra_params:
        params.extend(extra_params)
    for key, value in (filters or {}).items():
        if value is None or value == "":
            continue
        params.append((key, value))
    response = client.get(rest_url(table, dict(params)), headers=supabase_headers())
    response.raise_for_status()
    rows = response.json()
    return rows if isinstance(rows, list) else []


def count_rows(
    client: httpx.Client,
    table: str,
    *,
    filters: dict[str, object] | None = None,
    extra_params: list[tuple[str, object]] | None = None,
) -> int:
    params: dict[str, object] = {"select": "id", "limit": 1}
    for key, value in (filters or {}).items():
        if value is None or value == "":
            continue
        params[key] = value
    if extra_params:
        for key, value in extra_params:
            if value is None or value == "":
                continue
            params[key] = value
    response = client.get(
        rest_url(table, params),
        headers={**supabase_headers(), "Prefer": "count=exact"},
    )
    if response.status_code >= 400:
        return 0
    total = content_range_total(response)
    if total is not None:
        return total
    rows = response.json()
    return len(rows) if isinstance(rows, list) else 0


def parse_cookie_header(header_value: str | None) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in (header_value or "").split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        cookies[key.strip()] = value.strip()
    return cookies


def admin_signature(message: str) -> str:
    key = (ADMIN_SESSION_SECRET or SUPABASE_SERVICE_ROLE_KEY).encode("utf-8")
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()


def admin_session_token() -> str:
    issued_at = str(int(datetime.now(timezone.utc).timestamp()))
    return f"{issued_at}.{admin_signature(issued_at)}"


def admin_session_valid(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    issued_at, signature = token.split(".", 1)
    if not issued_at.isdigit():
        return False
    expected = admin_signature(issued_at)
    if not secrets.compare_digest(signature, expected):
        return False
    age = int(datetime.now(timezone.utc).timestamp()) - int(issued_at)
    return 0 <= age <= ADMIN_SESSION_MAX_AGE_SECONDS


def is_admin_authenticated(handler: BaseHTTPRequestHandler) -> bool:
    cookies = parse_cookie_header(handler.headers.get("Cookie"))
    return admin_session_valid(cookies.get(ADMIN_COOKIE_NAME))


def admin_page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<style>
body{{margin:0;font-family:Arial,Helvetica,sans-serif;background:#eef3f6;color:#17202a}}
header{{background:#11222a;color:#fff;padding:18px 24px;display:flex;align-items:center;justify-content:space-between}}
main{{max-width:1320px;margin:0 auto;padding:22px}}
section,.panel{{background:#fff;border:1px solid #dbe2ea;border-radius:10px;padding:18px;margin-bottom:16px}}
h1{{font-size:28px;margin:0}} h2{{font-size:20px;margin:0 0 12px}} h3{{font-size:16px;margin:0 0 8px}}
.muted{{color:#6b7280}} .row{{display:flex;gap:12px;flex-wrap:wrap;align-items:center}}
.toolbar{{display:flex;gap:10px;align-items:end;flex-wrap:wrap}}
label{{display:block;font-size:13px;color:#687385;margin-bottom:6px}}
input,select,textarea{{width:100%;box-sizing:border-box;border:1px solid #cfd8e3;border-radius:8px;padding:10px;background:#fff}}
button,a.button{{border:0;border-radius:8px;background:#0a6f78;color:#fff;min-height:40px;padding:0 16px;font-weight:700;text-decoration:none;display:inline-flex;align-items:center;justify-content:center;cursor:pointer}}
a.link{{color:#0a6f78;text-decoration:none;font-weight:700}}
.metric-grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}}
.metric{{border:1px solid #dbe2ea;border-radius:10px;padding:14px;background:#fbfdfe}}
.metric span{{display:block;color:#687385;font-size:13px;margin-bottom:4px}}
.metric strong{{font-size:26px}}
.split{{display:grid;grid-template-columns:1.2fr .8fr;gap:16px}}
.accordion summary{{cursor:pointer;font-weight:700;font-size:16px;list-style:none}}
.accordion summary::-webkit-details-marker{{display:none}}
.accordion[open]{{padding-bottom:12px}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
th,td{{border-bottom:1px solid #e5ebf2;padding:10px 8px;text-align:left;vertical-align:top}}
th{{color:#687385;font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
.badge{{display:inline-flex;align-items:center;padding:4px 8px;border-radius:999px;background:#e8f4f5;color:#0a6f78;font-size:12px;font-weight:700}}
.status-new{{background:#f1f5f9;color:#475569}}
.status-matched{{background:#ecfeff;color:#155e75}}
.status-needs_review{{background:#fff7ed;color:#9a3412}}
.status-customer_confirmed{{background:#ecfdf3;color:#166534}}
.status-unknown{{background:#f8fafc;color:#334155}}
.footer-actions{{display:flex;justify-content:flex-end;gap:10px;margin-top:14px}}
@media(max-width:980px){{.metric-grid,.split{{grid-template-columns:1fr}}}}
</style></head>
<body>
<header><div><strong>门封条后台管理</strong><div class="muted" style="color:#b9c4cc">订单列表 · 数据看板 · 数据库管理</div></div><div><a class="button" href="/admin/logout">退出登录</a></div></header>
<main>{body}</main>
</body></html>""".encode("utf-8")


def admin_login_response(next_path: str = "/admin", error: str = "") -> bytes:
    error_html = f"<p style='color:#b42318'>{esc(error)}</p>" if error else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>管理员登录</title>
<style>
body{{margin:0;font-family:Arial,Helvetica,sans-serif;background:#eef3f6;color:#17202a}}
main{{max-width:560px;margin:72px auto;padding:24px}}
.card{{background:#fff;border:1px solid #dbe2ea;border-radius:10px;padding:24px}}
label{{display:block;font-size:13px;color:#687385;margin-bottom:6px}}
input{{width:100%;border:1px solid #dbe2ea;border-radius:8px;padding:10px;box-sizing:border-box}}
button,a.button{{border:0;border-radius:8px;background:#0a6f78;color:#fff;min-height:40px;padding:0 16px;font-weight:700;text-decoration:none;display:inline-flex;align-items:center;justify-content:center}}
.row{{display:flex;gap:10px;align-items:center;flex-wrap:wrap}}
</style></head>
<body><main><div class="card">
<h1>后台登录</h1>
<p class="muted">输入管理员密码后进入后台。</p>
{error_html}
<form method="post" action="/admin/login">
<input type="hidden" name="next" value="{esc(next_path)}">
<label>管理员密码</label><input type="password" name="password" required>
<p class="row" style="margin-top:14px"><button type="submit">登录</button> <a class="button" href="/">返回前台</a></p>
</form>
</div></main></body></html>""".encode("utf-8")


def build_shopify_checkout_url(
    product: dict,
    selected_items: list[dict],
    shipping: dict[str, str] | None = None,
) -> str | None:
    template = SHOPIFY_PAYMENT_TEMPLATE.strip()
    if not template:
        return None
    payload = {
        "product_id": str(product.get("id") or ""),
        "brand": product.get("brand") or "",
        "model": product.get("equipment_model") or "",
        "items": json.dumps([{
            "door_key": item.get("door_key"),
            "part": item.get("part") or item.get("gasket") or item.get("part_number"),
            "price": item.get("line_price"),
        } for item in selected_items]),
        "total": str(sum(float(item.get("line_price") or 0) for item in selected_items)),
        "name": shipping.get("customer_name", "") if shipping else "",
        "email": shipping.get("customer_email", "") if shipping else "",
        "phone": shipping.get("customer_phone", "") if shipping else "",
        "address": shipping.get("shipping_address", "") if shipping else "",
    }
    url = template
    for key, value in payload.items():
        url = url.replace("{" + key + "}", str(value))
    return url


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
        3: [
            ("left_fresh_food_door", "Left fresh food door"),
            ("right_fresh_food_door", "Right fresh food door"),
            ("freezer_drawer", "Freezer drawer"),
        ],
        4: [
            ("left_fresh_food_door", "Left fresh food door"),
            ("right_fresh_food_door", "Right fresh food door"),
            ("top_freezer_door", "Top freezer door"),
            ("lower_freezer_door", "Lower freezer door"),
        ],
    }
    return [{"key": key, "label": label} for key, label in layouts.get(count, [])]


def infer_door_positions(product: dict) -> list[dict]:
    existing = product.get("door_positions")
    if isinstance(existing, list) and existing:
        return existing
    if product.get("door_count"):
        try:
            count = int(product.get("door_count") or 0)
            if count > 0:
                return door_positions_for_count(max(1, min(4, count)))
        except Exception:
            pass
    try:
        count = int(product.get("door_count") or 0)
    except Exception:
        count = 0
    if not count:
        count = estimated_gasket_quantity(product, [])
    return door_positions_for_count(max(1, min(4, count)))


def normalize_door_key(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\W+", "", value.strip().lower())


def required_positions_for_product(product: dict, quote_items: list[dict] | None = None) -> list[dict]:
    explicit = product.get("door_positions")
    if isinstance(explicit, list) and explicit:
        return explicit

    try:
        count = int(product.get("door_count") or 0)
    except Exception:
        count = 0
    if count > 0:
        return door_positions_for_count(max(1, min(4, count)))

    collected: list[str] = []
    if quote_items:
        for item in quote_items:
            raw = item.get("door_position") or item.get("door_position_display") or item.get("door_key")
            key = normalize_door_key(raw)
            if not key or key in collected:
                continue
            collected.append(key)

    mapping = {
        "leftfreshfooddoor": {"key": "left_fresh_food_door", "label": "Left fresh food door"},
        "rightfreshfooddoor": {"key": "right_fresh_food_door", "label": "Right fresh food door"},
        "freezerdrawer": {"key": "freezer_drawer", "label": "Freezer drawer"},
        "leftdoor": {"key": "left_door", "label": "Left Door"},
        "rightdoor": {"key": "right_door", "label": "Right Door"},
        "topfreezerdoor": {"key": "top_freezer_door", "label": "Top freezer door"},
        "lowerfreezerdoor": {"key": "lower_freezer_door", "label": "Lower freezer door"},
    }

    mapped: list[dict] = []
    for key in collected:
        if key in mapping:
            mapped.append(mapping[key])
            continue
        mapped.append({"key": key if "_" in key else key, "label": key.replace("_", " ").title() or "Door"})
    return mapped


def door_layout_name(positions: list[dict]) -> str:
    keys = [item.get("key") for item in positions]
    if keys == ["left_door", "right_door"]:
        return "side_by_side_2_door"
    if keys == ["left_fresh_food_door", "right_fresh_food_door", "freezer_drawer"]:
        return "top_over_2_door"
    if keys == ["left_fresh_food_door", "right_fresh_food_door", "top_freezer_door", "lower_freezer_door"]:
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
    base_payload = {
        "input": [{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": f"data:{mime_type};base64,{encoded}", "detail": "high"},
            ],
        }],
    }
    attempts = [OPENAI_NAMEPLATE_MODEL]
    if OPENAI_NAMEPLATE_MODEL != "gpt-4.1":
        attempts.append("gpt-4.1")
    response = None
    errors: list[str] = []
    for model_name in attempts:
        payload = dict(base_payload)
        payload["model"] = model_name
        for retry in range(3):
            response = httpx.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                json=payload,
                timeout=60,
            )
            if response.status_code < 500 and response.status_code != 429:
                break
            errors.append(
                f"{model_name} attempt {retry + 1}: {response.status_code} {response.text[:500]}"
            )
            if retry < 2:
                time.sleep(min(2 ** retry, 4))
        if response is not None and response.status_code < 400:
            break
    if response is None:
        raise RuntimeError("OpenAI nameplate recognition failed: " + " | ".join(errors))
    if response.status_code >= 400:
        raise RuntimeError(
            f"OpenAI nameplate recognition failed: {response.status_code} {response.text[:1000]}"
        )
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


def parse_match_input(fields: dict[str, dict] | None = None, body: str | None = None, content_type: str = "") -> dict[str, str]:
    if body and "application/json" in content_type.lower():
        try:
            data = json.loads(body)
        except Exception:
            data = {}
        return {
            "brand": (data.get("brand") or data.get("detected_brand") or "").strip(),
            "model": (data.get("model") or data.get("equipment_model") or data.get("detected_model") or "").strip(),
            "request_id": str(data.get("request_id", "") or "").strip(),
        }

    if not fields:
        return {"brand": "", "model": "", "request_id": ""}
    return {
        "brand": (fields.get("brand", {}).get("text", "") or "").strip(),
        "model": (fields.get("equipment_model", {}).get("text", "") or fields.get("model", {}).get("text", "") or "").strip(),
        "request_id": (fields.get("request_id", {}).get("text", "") or "").strip(),
    }


def parse_form_fields(body: bytes, content_type: str) -> dict[str, list[str]]:
    content_type = (content_type or "").lower()
    if "application/x-www-form-urlencoded" in content_type:
        text = body.decode("utf-8", errors="ignore")
        return parse_qs(text)
    return {}


def get_product_match_state(product: dict, quote_items: list[dict]) -> dict[str, object]:
    if not product:
        return {
            "is_ready": False,
            "needs": ["product_not_found"],
            "pipeline_stage": MATCH_STATUS_PENDING,
            "ready_for_checkout": False,
            "message": "No matching product found.",
            "missing_quote_positions": [],
        }

    required_positions = required_positions_for_product(product, quote_items)
    required_doors = len(required_positions) if required_positions else 0
    needs = []
    if not product.get("product_image_url"):
        needs.append("image")
    if required_doors == 0 and not product.get("door_positions") and not product.get("door_count"):
        needs.append("door_layout")
    quote_positions = [normalize_door_key(item.get("door_position") or item.get("door_position_display")) for item in (quote_items or []) if (item.get("door_position") or item.get("door_position_display"))]
    missing_positions: list[str] = []
    if required_doors > 0 and required_positions:
        present = set(filter(None, quote_positions))
        for position in required_positions:
            key = normalize_door_key(position.get("key") or position.get("label") or "")
            if key not in present:
                missing_positions.append(position.get("key") or position.get("label") or "door")
        if missing_positions:
            needs.append("gasket_items")
    elif not quote_items:
        needs.append("gasket_items")
    complete = not needs
    if complete:
        pipeline_stage = MATCH_STATUS_DONE
        progress_message = "Data ready"
    elif "image" in needs:
        pipeline_stage = "image_fill"
        progress_message = STAGE_STATUS_MESSAGE["image_fill"]
    elif "door_layout" in needs:
        pipeline_stage = "door_layout_fill"
        progress_message = STAGE_STATUS_MESSAGE["door_layout_fill"]
    elif "gasket_items" in needs:
        pipeline_stage = "gasket_fill"
        progress_message = STAGE_STATUS_MESSAGE["gasket_fill"]
    else:
        pipeline_stage = MATCH_STATUS_PENDING
        progress_message = "System is validating and preparing quote"
    status_message = {
        "image": STAGE_STATUS_MESSAGE["image_fill"],
        "door_layout": STAGE_STATUS_MESSAGE["door_layout_fill"],
        "gasket_items": STAGE_STATUS_MESSAGE["gasket_fill"],
    }
    message = ", ".join(status_message[key] for key in needs) if needs else "Data ready"
    if progress_message and progress_message not in message:
        message = progress_message
    return {
        "is_ready": complete,
        "needs": needs,
        "pipeline_stage": pipeline_stage,
        "ready_for_checkout": bool(product.get("brand") and product.get("equipment_model")),
        "message": message,
        "missing_quote_positions": missing_positions,
    }


def _job_status_payload(
    *, product_id: int, request_id: str | None, brand: str, model: str, stage: str, missing: list[str] | None = None
) -> dict[str, object]:
    return {
        "product_id": product_id,
        "request_id": request_id,
        "brand": brand,
        "model": model,
        "job_status": stage,
        "pipeline_stage": stage,
        "missing_fields": missing or [],
        "updated_at": now_iso(),
    }


def _pipeline_stage_label(stage: str) -> str:
    if stage in STAGE_STATUS_MESSAGE:
        return STAGE_STATUS_MESSAGE[stage]
    if stage == MATCH_STATUS_PENDING:
        return "System is waiting"
    if stage == MATCH_STATUS_DONE:
        return "Data ready"
    if stage == MATCH_STATUS_FAILED:
        return "Temporary issue, continuing recovery"
    return "System is loading"


def stage_summary_for_job(job: dict | None) -> dict[str, object]:
    if not job:
        return {}
    stage = job.get("pipeline_stage") or job.get("job_status") or MATCH_STATUS_PENDING
    return {
        "pipeline_stage": stage,
        "pipeline_stage_label": _pipeline_stage_label(stage),
        "countdown_seconds": _remaining_stage_seconds(job),
        "last_error_stage": job.get("last_error_stage"),
        "pipeline_stage_error_count": job.get("pipeline_stage_error_count"),
    }


def table_exists(client: httpx.Client, table: str) -> bool:
    endpoint = f"{SUPABASE_URL}/rest/v1/{table}?select=id&limit=1"
    try:
        response = client.get(endpoint, headers=supabase_headers())
        return response.status_code < 400
    except Exception:
        return False


def create_or_update_match_job(
    client: httpx.Client,
    *,
    product_id: int | None,
    brand: str,
    model: str,
    request_id: str | None = None,
    status: str = "pending",
    missing: list[str] | None = None,
    error: str | None = None,
    stage: str = "pending",
) -> int | None:
    if not table_exists(client, MATCH_JOB_TABLE):
        return None
    if status not in MATCH_TASK_STAGES:
        status = "pending"
    if stage not in MATCH_TASK_STAGES:
        stage = "pending"

    now = now_iso()
    cols = match_job_columns(client)
    missing = missing or []
    query = f"{SUPABASE_URL}/rest/v1/{MATCH_JOB_TABLE}?"
    job_rows = []
    if request_id:
        endpoint = f"{query}select=id&request_id=eq.{request_id}&limit=1"
        response = client.get(endpoint, headers=supabase_headers())
        if response.status_code == 400:
            response.raise_for_status()
        if response.status_code < 400:
            job_rows = response.json()
    if not job_rows and product_id:
        endpoint = f"{query}select=id&refrigerator_product_id=eq.{product_id}&request_id=is.null&order=updated_at.desc&limit=1"
        response = client.get(endpoint, headers=supabase_headers())
        response.raise_for_status()
        rows = response.json()
        if rows:
            job_rows = rows[:1]

    payload = {
        "request_id": int(request_id) if request_id and request_id.isdigit() else None,
        "refrigerator_product_id": product_id,
        "brand": brand,
        "equipment_model": model,
        "job_status": status,
        "pipeline_stage": stage,
        "missing_fields": missing,
        "last_error": error,
        "last_heartbeat_at": now,
        "updated_at": now,
    }
    payload = _set_stage_started_if_needed(
        client,
        product_id=product_id,
        request_id=request_id,
        next_stage=stage,
        payload=payload,
    )
    if status == MATCH_STATUS_FAILED and "pipeline_stage_error_count" in cols:
        payload["pipeline_stage_error_count"] = 0
    if status == MATCH_STATUS_DONE and "pipeline_stage_duration_ms" in cols:
        started = payload.get("pipeline_stage_started_at")
        ended = parse_iso_datetime(now)
        started_dt = parse_iso_datetime(started) if isinstance(started, str) else None
        if ended and started_dt:
            payload["pipeline_stage_duration_ms"] = max(0, int((ended - started_dt).total_seconds() * 1000))
    if status == MATCH_STATUS_DONE and not error:
        payload["ended_at"] = now
    if "started_at" in cols and request_id is None and not job_rows:
        payload.setdefault("retry_count", 0)
        payload.setdefault("max_retries", 3)
        payload.setdefault("next_retry_at", now)
        payload.setdefault("started_at", now)
    if not payload["request_id"]:
        payload.pop("request_id", None)
    if not job_rows and "pipeline_stage_started_at" in cols and "pipeline_stage_started_at" not in payload:
        payload["pipeline_stage_started_at"] = now
    payload = _filter_job_payload(payload, cols)

    if job_rows:
        job_id = job_rows[0]["id"]
        response = client.patch(
            f"{SUPABASE_URL}/rest/v1/{MATCH_JOB_TABLE}?id=eq.{job_id}",
            headers=supabase_headers("return=minimal"),
            json={k: v for k, v in payload.items() if v is not None},
        )
        response.raise_for_status()
        return job_id

    response = client.post(
        f"{SUPABASE_URL}/rest/v1/{MATCH_JOB_TABLE}",
        headers=supabase_headers("return=representation"),
        json=payload,
    )
    if response.status_code == 201:
        rows = response.json()
        if rows:
            return rows[0]["id"]
    return None


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def bump_match_job_retry(
    client: httpx.Client,
    *,
    product_id: int | None,
    request_id: str | None,
    brand: str,
    model: str,
    error: str,
    stage: str,
    hard_fail: bool = False,
) -> None:
    if not table_exists(client, MATCH_JOB_TABLE):
        return

    job = fetch_match_job(client, product_id=product_id, request_id=request_id)
    cols = match_job_columns(client)
    if not job:
        create_or_update_match_job(
            client,
            product_id=product_id,
            request_id=request_id,
            brand=brand,
            model=model,
            status=MATCH_STATUS_FAILED,
            stage=stage,
            error=error,
            missing=[],
        )
        return

    retry_count = int(job.get("retry_count") or 0) + 1
    max_retries = int(job.get("max_retries") or 3)
    now = now_iso()
    backoff_minutes = update_job_retry_backoff(retry_count)
    next_retry = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=backoff_minutes)
    next_status = MATCH_STATUS_FAILED if hard_fail and retry_count >= max_retries else MATCH_STATUS_PENDING
    error_count = int(job.get("pipeline_stage_error_count") or 0) + 1
    payload = {
        "job_status": next_status,
        "pipeline_stage": stage,
        "last_error": error,
        "pipeline_stage_error_count": error_count if "pipeline_stage_error_count" in cols else None,
        "last_error_stage": stage,
        "retry_count": retry_count,
        "max_retries": max_retries,
        "next_retry_at": next_retry.isoformat(),
        "last_heartbeat_at": now,
        "updated_at": now,
    }
    payload = _set_stage_started_if_needed(
        client,
        product_id=product_id,
        request_id=request_id,
        next_stage=stage,
        payload=payload,
    )
    payload = _filter_job_payload(payload, cols)
    response = client.patch(
        f"{SUPABASE_URL}/rest/v1/{MATCH_JOB_TABLE}?id=eq.{job['id']}",
        headers=supabase_headers("return=minimal"),
        json=payload,
    )
    response.raise_for_status()


def _is_stage_timed_out(job: dict | None, stage: str) -> bool:
    if not job:
        return True
    if job.get("pipeline_stage") != stage:
        return False
    heartbeat = parse_iso_datetime(job.get("pipeline_stage_started_at") or job.get("last_heartbeat_at"))
    timeout = MATCH_STAGE_TIMEOUTS.get(stage, MATCH_STAGE_TIMEOUT_SECONDS)
    if not heartbeat:
        return False
    return datetime.now(timezone.utc).replace(microsecond=0) > heartbeat + timedelta(seconds=timeout)


def _build_product_position_payload(product: dict, quote_items: list[dict]) -> list[dict]:
    positions = required_positions_for_product(product, quote_items)
    if positions:
        return positions
    if quote_items:
        return [{"key": normalize_door_key(item.get("door_position") or item.get("door_position_display")) or f"door_{i+1}",
                 "label": (item.get("door_position_display") or item.get("door_position") or f"Door {i+1}")} for i, item in enumerate(quote_items)]
    return []


def fetch_match_job(client: httpx.Client, product_id: int | None = None, request_id: str | None = None) -> dict | None:
    if not table_exists(client, MATCH_JOB_TABLE):
        return None
    if request_id:
        endpoint = f"{SUPABASE_URL}/rest/v1/{MATCH_JOB_TABLE}?select=*&request_id=eq.{request_id}&limit=1"
        response = client.get(endpoint, headers=supabase_headers())
        if response.status_code < 400:
            rows = response.json()
            if rows:
                return rows[0]
    if product_id:
        endpoint = (
            f"{SUPABASE_URL}/rest/v1/{MATCH_JOB_TABLE}?select=*&refrigerator_product_id=eq.{product_id}&"
            "order=updated_at.desc&limit=1"
        )
        response = client.get(endpoint, headers=supabase_headers())
        if response.status_code < 400:
            rows = response.json()
            if rows:
                return rows[0]
    return None


def run_match_job_background(product_id: int, brand: str, model: str, request_id: str | None = None) -> None:
    if product_id in MATCH_JOBS_REFRESHING:
        return
    MATCH_JOBS_REFRESHING.add(product_id)

    def _set_stage(
        client: httpx.Client,
        *,
        status: str,
        stage: str,
        missing: list[str],
        request_job_id: str | None,
        error: str | None = None,
    ) -> None:
        create_or_update_match_job(
            client,
            product_id=product_id,
            request_id=request_job_id,
            brand=brand,
            model=model,
            status=status,
            stage=stage,
            missing=missing,
            error=error,
        )

    def _run_stage(
        client: httpx.Client,
        *,
        product: dict,
        stage: str,
        request_job_id: str | None,
    ) -> bool:
        if stage == "image_fill":
            if product.get("product_image_url"):
                _set_stage(
                    client,
                    status="image_fill",
                    stage="image_fill",
                    missing=[],
                    request_job_id=request_job_id,
                )
                return True
            _set_stage(
                client,
                status="image_fill",
                stage="image_fill",
                missing=["image"],
                request_job_id=request_job_id,
            )
            try:
                from product_image_search_crawler import quick_promote_product_image

                changed = quick_promote_product_image(client, product)
                if changed:
                    refreshed = get_product(client, product_id)
                    return bool(refreshed and refreshed.get("product_image_url"))
                return False
            except Exception as exc:
                bump_match_job_retry(
                    client,
                    product_id=product_id,
                    request_id=request_job_id,
                    brand=brand,
                    model=model,
                    stage="image_fill",
                    error=f"image_fill failed: {exc}",
                )
                return False

        if stage == "door_layout_fill":
            if required_positions_for_product(product):
                _set_stage(
                    client,
                    status="door_layout_fill",
                    stage="door_layout_fill",
                    missing=[],
                    request_job_id=request_job_id,
                )
                return True
            _set_stage(
                client,
                status="door_layout_fill",
                stage="door_layout_fill",
                missing=["door_layout"],
                request_job_id=request_job_id,
            )
            try:
                positions = infer_door_positions(product)
                if positions:
                    save_inferred_door_layout(client, product, positions)
                    return True
                return False
            except Exception as exc:
                bump_match_job_retry(
                    client,
                    product_id=product_id,
                    request_id=request_job_id,
                    brand=brand,
                    model=model,
                    stage="door_layout_fill",
                    error=f"door_layout_fill failed: {exc}",
                )
                return False

        if stage == "gasket_fill":
            quote_items = get_quote_items(client, product_id)
            required_positions = required_positions_for_product(product, quote_items)
            _set_stage(
                client,
                status="gasket_fill",
                stage="gasket_fill",
                missing=["gasket_items"] if required_positions else [],
                request_job_id=request_job_id,
            )
            if required_positions and quote_items:
                return True
            try:
                from gasket_spec_refresher import refresh_product_gasket_spec

                refresh_product_gasket_spec(client, product_id)
                return True
            except Exception as exc:
                bump_match_job_retry(
                    client,
                    product_id=product_id,
                    request_id=request_job_id,
                    brand=brand,
                    model=model,
                    stage="gasket_fill",
                    error=f"gasket_fill failed: {exc}",
                )
                return False

        return False

    def worker() -> None:
        try:
            with httpx.Client(timeout=60) as client:
                existing = fetch_match_job(client, product_id=product_id, request_id=request_id)
                if existing and existing.get("job_status") == MATCH_STATUS_DONE:
                    return

                if existing and existing.get("job_status") == MATCH_STATUS_FAILED:
                    next_retry = existing.get("next_retry_at")
                    if next_retry:
                        next_retry_dt = parse_iso_datetime(str(next_retry))
                        if next_retry_dt and next_retry_dt > datetime.now(timezone.utc).replace(microsecond=0):
                            return

                create_or_update_match_job(
                    client,
                    product_id=product_id,
                    request_id=request_id,
                    brand=brand,
                    model=model,
                    status=MATCH_STATUS_PENDING,
                    stage=MATCH_STATUS_PENDING,
                    missing=[],
                )

                product = get_product(client, product_id)
                if not product:
                    return

                for _ in range(3):
                    quote_items = get_quote_items(client, product_id)
                    state = get_product_match_state(product, quote_items)
                    if state["is_ready"]:
                        create_or_update_match_job(
                            client,
                            product_id=product_id,
                            request_id=request_id,
                            brand=brand,
                            model=model,
                            status=MATCH_STATUS_DONE,
                            stage=MATCH_STATUS_DONE,
                            missing=state["needs"],
                        )
                        return

                    needs = state["needs"] or []
                    if "image" in needs:
                        next_stage = "image_fill"
                    elif "door_layout" in needs:
                        next_stage = "door_layout_fill"
                    elif "gasket_items" in needs:
                        next_stage = "gasket_fill"
                    else:
                        next_stage = MATCH_STATUS_VERIFY

                    if next_stage == MATCH_STATUS_VERIFY:
                        _set_stage(
                            client,
                            status=MATCH_STATUS_PENDING,
                            stage=state.get("pipeline_stage", MATCH_STATUS_VERIFY),
                            missing=needs,
                            request_job_id=request_id,
                        )
                        create_or_update_match_job(
                            client,
                            product_id=product_id,
                            request_id=request_id,
                            brand=brand,
                            model=model,
                            status=MATCH_STATUS_PENDING,
                            stage=state.get("pipeline_stage", MATCH_STATUS_VERIFY),
                            missing=needs,
                            error=None,
                        )
                        return

                    if existing and existing.get("pipeline_stage") == next_stage and not _is_stage_timed_out(
                        existing,
                        next_stage,
                    ):
                        # active stage, waiting for background work
                        _set_stage(
                            client,
                            status=MATCH_STATUS_PENDING,
                            stage=next_stage,
                            missing=needs,
                            request_job_id=request_id,
                            error="Waiting for enrichment pipeline to complete",
                        )
                        return

                    progressed = _run_stage(
                        client,
                        product=product,
                        stage=next_stage,
                        request_job_id=request_id,
                    )
                    product = get_product(client, product_id) or product
                    existing = fetch_match_job(client, product_id=product_id, request_id=request_id)
                    if not progressed:
                        return

        finally:
            MATCH_JOBS_REFRESHING.discard(product_id)

    threading.Thread(target=worker, daemon=True).start()


def match_product(
    client: httpx.Client,
    *,
    brand: str,
    model: str,
    request_id: str | None = None,
    create_if_missing: bool = True,
    auto_enrich: bool = False,
) -> tuple[dict | None, list[dict], dict]:
    product = find_product(client, brand, model)
    if not product and create_if_missing:
        product = create_product_from_confirmed_model(client, brand, model)
    if not product:
        return None, [], {
            "state": "no_product",
            "ready_for_checkout": False,
            "needs": ["product_not_found"],
            "job": None,
        }

    quote_items = get_quote_items(client, product["id"])
    if not quote_items and auto_enrich:
        run_match_job_background(product["id"], product.get("brand") or brand, product.get("equipment_model") or model, request_id)

    state = get_product_match_state(product, quote_items)
    if not state["is_ready"] and not quote_items and auto_enrich:
        run_match_job_background(product["id"], product.get("brand") or brand, product.get("equipment_model") or model, request_id)

    if auto_enrich:
        job_payload = fetch_match_job(client, product_id=product["id"], request_id=request_id)
        if not job_payload:
            create_or_update_match_job(
                client,
                product_id=product["id"],
                request_id=request_id,
                brand=product.get("brand") or brand,
                model=product.get("equipment_model") or model,
                status=MATCH_STATUS_DONE if state["is_ready"] else MATCH_STATUS_PENDING,
                missing=state["needs"],
                stage=state.get("pipeline_stage", "verify"),
            )
            job_payload = fetch_match_job(client, product_id=product["id"], request_id=request_id)
    else:
        job_payload = fetch_match_job(client, product_id=product["id"], request_id=request_id)
    response = {
        "state": "matched" if state["is_ready"] else "loading",
        "ready_for_checkout": state["ready_for_checkout"],
        "needs": state["needs"],
        "job": job_payload,
    }
    return product, quote_items, response


def build_match_payload(
    product: dict | None,
    quote_items: list[dict],
    state: dict,
) -> dict[str, object]:
    if not product:
        return {
            "state": "no_match",
            "product": None,
            "gasket_items": [],
            "needs": ["product_not_found"],
            "ready_for_checkout": False,
            "job": None,
            "message": "Model was not found in catalog yet. We are collecting the record now.",
            "countdown_seconds": 0,
        }
    job_payload = dict(state.get("job") or {})
    if not job_payload and product:
        with httpx.Client(timeout=30) as client:
            job_payload = fetch_match_job(client, product_id=product.get("id"), request_id=state.get("request_id"))
    if job_payload:
        job_payload.update(stage_summary_for_job(job_payload))
    countdown = stage_summary_for_job(job_payload or {}).get("countdown_seconds", 0) or 0
    return {
        "state": state.get("state", "loading"),
        "ready_for_checkout": bool(state.get("ready_for_checkout", False)),
        "needs": state.get("needs", []),
        "message": state.get("message", "Loading"),
        "countdown_seconds": countdown,
        "product": {
            "id": product.get("id"),
            "brand": product.get("brand"),
            "equipment_model": product.get("equipment_model"),
            "manufacturer": product.get("manufacturer"),
            "manufacture_date": product.get("manufacture_date"),
            "product_image_url": product.get("product_image_url"),
            "door_count": product.get("door_count"),
            "door_positions": product.get("door_positions") or infer_door_positions(product),
            "door_layout": product.get("door_layout"),
        },
        "gasket_items": [
            {
                "id": item.get("id"),
                "door_position": item.get("door_position"),
                "part_number": item.get("part_number") or item.get("universal_part_number"),
                "width_in": item.get("width_in"),
                "height_in": item.get("height_in"),
                "dimensions_text": item.get("dimensions_text"),
                "perimeter_in": item.get("perimeter_in"),
                "source_name": item.get("source_name"),
                "source_url": item.get("source_url"),
                "final_price_usd": item.get("final_price_usd") if item.get("final_price_usd") is not None else item.get("price_usd"),
                "gasket_image_url": item.get("gasket_image_url"),
                "confidence_score": item.get("confidence_score"),
            }
            for item in quote_items
        ],
        "job": job_payload or None,
    }


def send_json(self, data: dict[str, object], status: int = HTTPStatus.OK) -> None:
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    self.send_response(status)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(payload)))
    self.end_headers()
    self.wfile.write(payload)


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


def _clean_door_key(value: str) -> str:
    return re.sub(r"\s+", "", (value or "").strip().lower())


def _row_has_size_or_part(row: dict) -> bool:
    width = row.get("width_in") or row.get("width") or row.get("overall_width_in")
    height = row.get("height_in") or row.get("height") or row.get("overall_height_in")
    return bool(width and height) or bool(row.get("part_number") or row.get("universal_part_number") or row.get("gasket_part_number"))


def _to_quote_rows(row: dict) -> dict:
    normalized = dict(row)
    if "part_number" not in normalized and "gasket_part_number" in normalized:
        normalized["part_number"] = normalized["gasket_part_number"]
    if "width_in" not in normalized and normalized.get("width"):
        normalized["width_in"] = normalized["width"]
    if "height_in" not in normalized and normalized.get("height"):
        normalized["height_in"] = normalized["height"]
    if "door_position" not in normalized and normalized.get("door_position_display"):
        normalized["door_position"] = normalized["door_position_display"]
    if "door_position" not in normalized and normalized.get("door_key"):
        normalized["door_position"] = normalized["door_key"]
    return normalized


def get_quote_items(client: httpx.Client, product_id: int) -> list[dict]:
    quote_tables = [
        "refrigerator_product_quote_items",
        "refrigerator_product_gaskets",
    ]
    for table in quote_tables:
        endpoint = (
            f"{SUPABASE_URL}/rest/v1/{table}?select=*&refrigerator_product_id=eq.{product_id}"
            "&order=door_index.asc,door_position.asc,id.asc"
        )
        try:
            response = client.get(endpoint, headers=supabase_headers())
            if response.status_code == 404:
                continue
            response.raise_for_status()
            rows = [_to_quote_rows(row) for row in response.json()]
            filtered = [row for row in rows if is_customer_visible_gasket(row)]
            if filtered or rows:
                return filtered if filtered else rows
        except Exception:
            continue
    return []


def update_request_status(client: httpx.Client, request_id: int, status: str) -> None:
    if not request_id or not table_exists(client, "gasket_requests"):
        return
    endpoint = f"{SUPABASE_URL}/rest/v1/gasket_requests?id=eq.{request_id}"
    payload = {
        "status": status,
        "updated_at": now_iso(),
    }
    if status == "customer_confirmed":
        payload["customer_confirmed_at"] = now_iso()
    client.patch(endpoint, headers=supabase_headers("return=minimal"), json=payload)


def map_quote_items_to_positions(
    quote_items: list[dict], positions: list[dict]
) -> list[dict | None]:
    if not positions:
        return []
    if not quote_items:
        return [None for _ in positions]

    fallback_rows: list[dict] = []
    indexed: dict[str, list[dict]] = {}
    for item in quote_items:
        key = _clean_door_key(item.get("door_position") or item.get("door_position_display") or item.get("door_key") or "")
        if not key:
            fallback_rows.append(item)
        else:
            indexed.setdefault(key, []).append(item)

    for values in indexed.values():
        # keep highest confidence first
        values.sort(key=lambda i: float(i.get("confidence_score") or 0), reverse=True)
        indexed[key] = values

    mapped: list[dict | None] = []
    for position in positions:
        key = _clean_door_key(position.get("key") or "")
        candidates = indexed.get(key, [])
        if candidates:
            item = candidates.pop(0)
            indexed[key] = candidates
            mapped.append(item)
            continue

        if not indexed and fallback_rows:
            mapped.append(fallback_rows.pop(0))
            continue
        mapped.append(None)
    return mapped


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


def request_status_label(request: dict) -> str:
    status = (request.get("status") or "new").strip()
    labels = {
        "new": "新建",
        "system_candidate": "系统候选",
        "matched": "已匹配",
        "needs_review": "待复核",
        "customer_confirmed": "客户已确认",
        "staff_verified": "人工已确认",
        "factory_sent": "已发工厂",
    }
    return labels.get(status, status or "未知")


def product_status_label(product: dict) -> str:
    status = (product.get("data_status") or "").strip()
    labels = {
        "customer_requested": "客户新增",
        "system_candidate": "系统候选",
        "ai_structured": "AI 已结构化",
        "staff_verified": "人工已确认",
        "verified": "已验证",
    }
    return labels.get(status, status or "未知")


def patch_row(client: httpx.Client, table: str, row_id: int, payload: dict[str, object]) -> None:
    response = client.patch(
        f"{SUPABASE_URL}/rest/v1/{table}?id=eq.{row_id}",
        headers=supabase_headers("return=minimal"),
        json={k: v for k, v in payload.items() if v is not None},
    )
    response.raise_for_status()


def update_request_fields(client: httpx.Client, request_id: int, payload: dict[str, object]) -> None:
    if not request_id or not table_exists(client, "gasket_requests"):
        return
    payload = {**payload, "updated_at": now_iso()}
    if payload.get("status") == "customer_confirmed":
        payload["customer_confirmed_at"] = now_iso()
    if payload.get("status") == "staff_verified":
        payload["customer_confirmed_at"] = payload.get("customer_confirmed_at") or now_iso()
    patch_row(client, "gasket_requests", request_id, payload)


def update_product_fields(client: httpx.Client, product_id: int, payload: dict[str, object]) -> None:
    if not product_id or not table_exists(client, "refrigerator_products"):
        return
    payload = {**payload, "updated_at": now_iso()}
    patch_row(client, "refrigerator_products", product_id, payload)


def admin_order_matches(row: dict, q: str, status: str, days: int | None) -> bool:
    q = (q or "").strip().lower()
    if days:
        created_at = parse_iso_datetime(row.get("created_at") or row.get("updated_at"))
        if not created_at:
            return False
        if created_at < datetime.now(timezone.utc) - timedelta(days=days):
            return False
    if status:
        current = (row.get("status") or "").strip().lower()
        if status == "confirmed":
            if current != "customer_confirmed":
                return False
        elif status == "unconfirmed":
            if current == "customer_confirmed":
                return False
        elif current != status:
            return False
    if q:
        haystack = " ".join(
            str(row.get(field) or "")
            for field in (
                "customer_name",
                "customer_email",
                "customer_phone",
                "detected_brand",
                "detected_model",
                "ocr_text",
                "notes",
            )
        ).lower()
        if q not in haystack:
            return False
    return True


def admin_product_matches(row: dict, q: str, status: str, days: int | None) -> bool:
    q = (q or "").strip().lower()
    if days:
        created_at = parse_iso_datetime(row.get("updated_at") or row.get("created_at"))
        if not created_at:
            return False
        if created_at < datetime.now(timezone.utc) - timedelta(days=days):
            return False
    if status:
        current = (row.get("data_status") or "").strip().lower()
        if status == "with_image":
            if not row.get("product_image_url"):
                return False
        elif status == "verified":
            if not row.get("product_image_verified"):
                return False
        elif current != status:
            return False
    if q:
        haystack = " ".join(
            str(row.get(field) or "")
            for field in ("brand", "equipment_model", "manufacturer", "door_layout", "data_status")
        ).lower()
        if q not in haystack:
            return False
    return True


def fetch_admin_orders(
    client: httpx.Client,
    *,
    q: str = "",
    status: str = "",
    days: int | None = None,
    page: int = 1,
    page_size: int = ADMIN_PAGE_SIZE,
) -> tuple[list[dict], int]:
    filters: dict[str, object] = {}
    extra_params: list[tuple[str, object]] = []
    if days:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        filters["created_at"] = f"gte.{since}"
    if status:
        if status == "confirmed":
            filters["customer_confirmed_at"] = "not.is.null"
        elif status == "unconfirmed":
            filters["customer_confirmed_at"] = "is.null"
        elif status in {"new", "matched", "needs_review", "customer_confirmed", "staff_verified"}:
            filters["status"] = f"eq.{status}"
    q_safe = re.sub(r"[(),]", " ", q or "").strip()
    if q_safe:
        extra_params.append(
            (
                "or",
                f"(customer_name.ilike.*{q_safe}*,customer_email.ilike.*{q_safe}*,customer_phone.ilike.*{q_safe}*,detected_brand.ilike.*{q_safe}*,detected_model.ilike.*{q_safe}*,ocr_text.ilike.*{q_safe}*,notes.ilike.*{q_safe}*)",
            )
        )
    total = count_rows(client, "gasket_requests", filters=filters, extra_params=extra_params)
    rows = fetch_rows(
        client,
        "gasket_requests",
        order="updated_at.desc",
        limit=page_size,
        offset=max(0, (page - 1) * page_size),
        filters=filters,
        extra_params=extra_params,
    )
    return rows, total


def fetch_admin_products(
    client: httpx.Client,
    *,
    q: str = "",
    status: str = "",
    days: int | None = None,
    page: int = 1,
    page_size: int = ADMIN_PAGE_SIZE,
) -> tuple[list[dict], int]:
    filters: dict[str, object] = {}
    extra_params: list[tuple[str, object]] = []
    if days:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        filters["updated_at"] = f"gte.{since}"
    if status:
        if status == "with_image":
            filters["product_image_url"] = "not.is.null"
        elif status == "verified":
            filters["product_image_verified"] = "eq.true"
        else:
            filters["data_status"] = f"eq.{status}"
    q_safe = re.sub(r"[(),]", " ", q or "").strip()
    if q_safe:
        extra_params.append(
            (
                "or",
                f"(brand.ilike.*{q_safe}*,equipment_model.ilike.*{q_safe}*,manufacturer.ilike.*{q_safe}*,door_layout.ilike.*{q_safe}*,data_status.ilike.*{q_safe}*)",
            )
        )
    total = count_rows(client, "refrigerator_products", filters=filters, extra_params=extra_params)
    rows = fetch_rows(
        client,
        "refrigerator_products",
        order="updated_at.desc",
        limit=page_size,
        offset=max(0, (page - 1) * page_size),
        filters=filters,
        extra_params=extra_params,
    )
    return rows, total


def admin_dashboard_counts(client: httpx.Client) -> dict[str, int]:
    return {
        "products": count_rows(client, "refrigerator_products"),
        "products_with_image": count_rows(client, "refrigerator_products", filters={"product_image_url": "not.is.null"}),
        "products_verified": count_rows(client, "refrigerator_products", filters={"product_image_verified": "eq.true"}),
        "image_candidates": count_rows(client, "product_image_candidates"),
        "gasket_specs": count_rows(client, "product_gasket_specs"),
        "quote_items": count_rows(client, "refrigerator_product_quote_items"),
        "requests": count_rows(client, "gasket_requests"),
        "requests_system_candidate": count_rows(client, "gasket_requests", filters={"status": "eq.system_candidate"}),
        "requests_matched": count_rows(client, "gasket_requests", filters={"status": "eq.matched"}),
        "requests_confirmed": count_rows(client, "gasket_requests", filters={"status": "eq.customer_confirmed"}),
        "requests_staff_verified": count_rows(client, "gasket_requests", filters={"status": "eq.staff_verified"}),
        "requests_review": count_rows(client, "gasket_requests", filters={"status": "eq.needs_review"}),
        "match_jobs_pending": count_rows(client, MATCH_JOB_TABLE, filters={"job_status": "eq.pending"}),
        "match_jobs_failed": count_rows(client, MATCH_JOB_TABLE, filters={"job_status": "eq.failed"}),
        "match_jobs_stuck": count_rows(client, MATCH_JOB_TABLE, filters={"job_status": "eq.stuck"}),
    }


def admin_recent_brief(client: httpx.Client) -> dict[str, int]:
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    return {
        "new_requests_1h": count_rows(client, "gasket_requests", filters={"created_at": f"gte.{since}"}),
        "new_products_1h": count_rows(client, "refrigerator_products", filters={"created_at": f"gte.{since}"}),
        "updated_products_1h": count_rows(client, "refrigerator_products", filters={"updated_at": f"gte.{since}"}),
        "new_images_1h": count_rows(client, "product_image_candidates", filters={"created_at": f"gte.{since}"}),
        "new_quote_items_1h": count_rows(client, "refrigerator_product_quote_items", filters={"created_at": f"gte.{since}"}),
    }


def admin_recent_jobs(client: httpx.Client) -> list[dict]:
    return fetch_rows(client, MATCH_JOB_TABLE, order="updated_at.desc", limit=5)


def admin_request_actions_html(row: dict, redirect_path: str) -> str:
    request_id = row.get("id")
    if not request_id:
        return ""
    current = (row.get("status") or "").strip().lower()
    buttons = []
    if current != "customer_confirmed":
        buttons.append(f"""<form method="post" action="/admin/action" style="display:inline">
  <input type="hidden" name="table" value="requests">
  <input type="hidden" name="row_id" value="{esc(request_id)}">
  <input type="hidden" name="action" value="customer_confirmed">
  <input type="hidden" name="redirect" value="{esc(redirect_path)}">
  <button type="submit">客户确认</button>
</form>""")
    if current != "staff_verified":
        buttons.append(f"""<form method="post" action="/admin/action" style="display:inline">
  <input type="hidden" name="table" value="requests">
  <input type="hidden" name="row_id" value="{esc(request_id)}">
  <input type="hidden" name="action" value="staff_verified">
  <input type="hidden" name="redirect" value="{esc(redirect_path)}">
  <button type="submit">人工确认</button>
</form>""")
    if current != "factory_sent":
        buttons.append(f"""<form method="post" action="/admin/action" style="display:inline">
  <input type="hidden" name="table" value="requests">
  <input type="hidden" name="row_id" value="{esc(request_id)}">
  <input type="hidden" name="action" value="factory_sent">
  <input type="hidden" name="redirect" value="{esc(redirect_path)}">
  <button type="submit">发工厂</button>
</form>""")
    return "<div class='row'>" + "".join(buttons) + "</div>"


def admin_product_actions_html(row: dict, redirect_path: str) -> str:
    product_id = row.get("id")
    if not product_id:
        return ""
    buttons = []
    if not row.get("product_image_verified"):
        buttons.append(f"""<form method="post" action="/admin/action" style="display:inline">
  <input type="hidden" name="table" value="products">
  <input type="hidden" name="row_id" value="{esc(product_id)}">
  <input type="hidden" name="action" value="product_verified">
  <input type="hidden" name="redirect" value="{esc(redirect_path)}">
  <button type="submit">验证主图</button>
</form>""")
    if (row.get("data_status") or "") != "system_candidate":
        buttons.append(f"""<form method="post" action="/admin/action" style="display:inline">
  <input type="hidden" name="table" value="products">
  <input type="hidden" name="row_id" value="{esc(product_id)}">
  <input type="hidden" name="action" value="system_candidate">
  <input type="hidden" name="redirect" value="{esc(redirect_path)}">
  <button type="submit">标记候选</button>
</form>""")
    return "<div class='row'>" + "".join(buttons) + "</div>"


def admin_dashboard_page(client: httpx.Client, query: dict[str, list[str]]) -> bytes:
    order_q = (query.get("q", [""])[0] or "").strip()
    order_status = (query.get("status", [""])[0] or "").strip()
    order_days_raw = (query.get("days", [""])[0] or "").strip()
    order_page = max(1, int((query.get("page", ["1"])[0] or "1").strip() or "1"))
    product_q = (query.get("product_q", [""])[0] or "").strip()
    product_status = (query.get("product_status", [""])[0] or "").strip()
    product_days_raw = (query.get("product_days", [""])[0] or "").strip()
    product_page = max(1, int((query.get("product_page", ["1"])[0] or "1").strip() or "1"))
    order_days = int(order_days_raw) if order_days_raw.isdigit() else None
    product_days = int(product_days_raw) if product_days_raw.isdigit() else None

    counts = admin_dashboard_counts(client)
    brief = admin_recent_brief(client)
    recent_jobs = admin_recent_jobs(client)
    orders, order_total = fetch_admin_orders(client, q=order_q, status=order_status, days=order_days, page=order_page)
    products, product_total = fetch_admin_products(client, q=product_q, status=product_status, days=product_days, page=product_page)
    order_pages = max(1, (order_total + ADMIN_PAGE_SIZE - 1) // ADMIN_PAGE_SIZE) if order_total else 1
    product_pages = max(1, (product_total + ADMIN_PAGE_SIZE - 1) // ADMIN_PAGE_SIZE) if product_total else 1
    redirect_base = "/admin?" + urlencode({k: v[0] for k, v in query.items() if v}, doseq=False)
    recent_models = ", ".join(
        f"{esc(row.get('brand') or '')} {esc(row.get('equipment_model') or '')}"
        for row in products[:5]
        if row.get("brand") or row.get("equipment_model")
    )
    alert_html = ""
    if counts["match_jobs_stuck"] or counts["match_jobs_failed"]:
        alert_html = f"""
<section>
  <div class="panel" style="border-left:5px solid #b42318;background:#fff7f7">
    <strong>异常提醒</strong>
    <div class="muted">卡住任务 {counts["match_jobs_stuck"]} 条，失败任务 {counts["match_jobs_failed"]} 条。建议先处理这些任务再继续批量补齐。</div>
  </div>
</section>
"""
    body = f"""
<section>
  <div class="row" style="justify-content:space-between">
    <div>
      <h1>后台管理</h1>
      <div class="muted">订单列表默认展开，数据库管理折叠展示。</div>
    </div>
    <div class="row">
      <a class="button" href="/">返回前台</a>
      <a class="button" href="/admin/logout">退出登录</a>
    </div>
  </div>
</section>
<section class="metric-grid">
  <div class="metric"><span>型号总数</span><strong>{counts["products"]}</strong></div>
  <div class="metric"><span>已有主图</span><strong>{counts["products_with_image"]}</strong></div>
  <div class="metric"><span>已验证主图</span><strong>{counts["products_verified"]}</strong></div>
  <div class="metric"><span>门封候选</span><strong>{counts["quote_items"]}</strong></div>
</section>
<section class="metric-grid">
  <div class="metric"><span>图片候选</span><strong>{counts["image_candidates"]}</strong></div>
  <div class="metric"><span>密封条规格</span><strong>{counts["gasket_specs"]}</strong></div>
  <div class="metric"><span>订单总数</span><strong>{counts["requests"]}</strong></div>
  <div class="metric"><span>客户已确认</span><strong>{counts["requests_confirmed"]}</strong></div>
</section>
<section class="metric-grid">
  <div class="metric"><span>1小时新增订单</span><strong>{brief["new_requests_1h"]}</strong></div>
  <div class="metric"><span>1小时新增型号</span><strong>{brief["new_products_1h"]}</strong></div>
  <div class="metric"><span>1小时新增图片候选</span><strong>{brief["new_images_1h"]}</strong></div>
  <div class="metric"><span>1小时新增密封条</span><strong>{brief["new_quote_items_1h"]}</strong></div>
</section>
{alert_html}
<details class="accordion">
  <summary>任务监控</summary>
  <div style="margin-top:14px">
    <table>
      <thead><tr><th>时间</th><th>型号</th><th>阶段</th><th>状态</th><th>错误</th></tr></thead>
      <tbody>
        {''.join(f"<tr><td>{esc(row.get('updated_at') or row.get('created_at') or '')}</td><td>{esc(row.get('brand') or '')} {esc(row.get('equipment_model') or '')}</td><td>{esc(row.get('pipeline_stage') or row.get('job_status') or '')}</td><td>{esc(row.get('job_status') or '')}</td><td>{esc(row.get('last_error') or '')}</td></tr>" for row in recent_jobs) or "<tr><td colspan='5' class='muted'>暂无任务</td></tr>"}
      </tbody>
    </table>
  </div>
</details>
<details class="accordion" open>
  <summary>订单列表</summary>
  <div style="margin-top:14px">
    <form method="get" action="/admin" class="toolbar">
      <div style="min-width:260px;flex:1">
        <label>搜索订单</label><input name="q" value="{esc(order_q)}" placeholder="客户名 / 品牌 / 型号 / 备注">
      </div>
      <div style="min-width:180px">
        <label>快捷状态</label>
        <select name="status">
          <option value="">全部</option>
          <option value="new"{" selected" if order_status == "new" else ""}>新建</option>
          <option value="system_candidate"{" selected" if order_status == "system_candidate" else ""}>系统候选</option>
          <option value="matched"{" selected" if order_status == "matched" else ""}>已匹配</option>
          <option value="needs_review"{" selected" if order_status == "needs_review" else ""}>待复核</option>
          <option value="customer_confirmed"{" selected" if order_status == "customer_confirmed" else ""}>已确认</option>
          <option value="staff_verified"{" selected" if order_status == "staff_verified" else ""}>人工已确认</option>
          <option value="confirmed"{" selected" if order_status == "confirmed" else ""}>已确认(含时间)</option>
          <option value="unconfirmed"{" selected" if order_status == "unconfirmed" else ""}>未确认</option>
          <option value="factory_sent"{" selected" if order_status == "factory_sent" else ""}>已发工厂</option>
        </select>
      </div>
      <div style="min-width:160px">
        <label>最近几天</label>
        <select name="days">
          <option value="">全部</option>
          <option value="7"{" selected" if order_days_raw == "7" else ""}>7 天</option>
          <option value="30"{" selected" if order_days_raw == "30" else ""}>30 天</option>
          <option value="90"{" selected" if order_days_raw == "90" else ""}>90 天</option>
        </select>
      </div>
      <input type="hidden" name="product_q" value="{esc(product_q)}">
      <input type="hidden" name="product_status" value="{esc(product_status)}">
      <input type="hidden" name="product_days" value="{esc(product_days_raw)}">
      <button type="submit">搜索</button>
    </form>
    <p class="muted">共 {order_total} 条；每页 {ADMIN_PAGE_SIZE} 条；第 {order_page} / {order_pages} 页。</p>
    <table>
      <thead><tr><th>时间</th><th>客户</th><th>品牌 / 型号</th><th>状态</th><th>确认时间</th><th>操作</th><th>备注</th></tr></thead>
      <tbody>
        {''.join(f"<tr><td>{esc(row.get('created_at') or row.get('updated_at') or '')}</td><td>{esc(row.get('customer_name') or '')}<br><span class='muted'>{esc(row.get('customer_email') or '')}</span></td><td>{esc(row.get('detected_brand') or '')}<br>{esc(row.get('detected_model') or '')}</td><td><span class='badge status-{esc((row.get('status') or 'unknown').lower())}'>{esc(request_status_label(row))}</span></td><td>{esc(row.get('customer_confirmed_at') or '')}</td><td>{admin_request_actions_html(row, redirect_base)}</td><td>{esc(row.get('notes') or '')}</td></tr>" for row in orders) or "<tr><td colspan='7' class='muted'>暂无订单</td></tr>"}
      </tbody>
    </table>
    <div class="footer-actions">
      <span class="muted">当前页：{order_page} / {order_pages}</span>
    </div>
  </div>
</details>
<details class="accordion">
  <summary>数据库管理</summary>
  <div style="margin-top:14px">
    <form method="get" action="/admin" class="toolbar">
      <div style="min-width:260px;flex:1">
        <label>搜索型号</label><input name="product_q" value="{esc(product_q)}" placeholder="品牌 / 型号 / 厂家 / 门型">
      </div>
      <div style="min-width:180px">
        <label>快捷筛选</label>
        <select name="product_status">
          <option value="">全部</option>
          <option value="customer_requested"{" selected" if product_status == "customer_requested" else ""}>客户新增</option>
          <option value="system_candidate"{" selected" if product_status == "system_candidate" else ""}>系统候选</option>
          <option value="ai_structured"{" selected" if product_status == "ai_structured" else ""}>AI 已结构化</option>
          <option value="staff_verified"{" selected" if product_status == "staff_verified" else ""}>人工已确认</option>
          <option value="with_image"{" selected" if product_status == "with_image" else ""}>已有主图</option>
          <option value="verified"{" selected" if product_status == "verified" else ""}>已验证主图</option>
        </select>
      </div>
      <div style="min-width:160px">
        <label>最近几天</label>
        <select name="product_days">
          <option value="">全部</option>
          <option value="7"{" selected" if product_days_raw == "7" else ""}>7 天</option>
          <option value="30"{" selected" if product_days_raw == "30" else ""}>30 天</option>
          <option value="90"{" selected" if product_days_raw == "90" else ""}>90 天</option>
        </select>
      </div>
      <input type="hidden" name="q" value="{esc(order_q)}">
      <input type="hidden" name="status" value="{esc(order_status)}">
      <input type="hidden" name="days" value="{esc(order_days_raw)}">
      <button type="submit">搜索</button>
    </form>
    <p class="muted">共 {product_total} 条；每页 {ADMIN_PAGE_SIZE} 条；第 {product_page} / {product_pages} 页。</p>
    <table>
      <thead><tr><th>品牌</th><th>型号</th><th>门数</th><th>主图</th><th>验证</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>
        {''.join(f"<tr><td>{esc(row.get('brand') or '')}</td><td>{esc(row.get('equipment_model') or '')}</td><td>{esc(row.get('door_count') or '')}</td><td>{'有' if row.get('product_image_url') else '无'}</td><td>{'是' if row.get('product_image_verified') else '否'}</td><td>{esc(product_status_label(row))}</td><td>{admin_product_actions_html(row, redirect_base)}</td></tr>" for row in products) or "<tr><td colspan='7' class='muted'>暂无型号</td></tr>"}
      </tbody>
    </table>
    <p class="muted">最近显示的型号：{recent_models or '暂无'}</p>
  </div>
</details>
"""
    return admin_page("后台管理", body)


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
.upload-row{{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;align-items:end}}
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
@media(max-width:860px){{.upload-row{{grid-template-columns:1fr}}}}
</style>
</head>
<body>
<header><strong>Refrigerator Door Gasket Match</strong></header>
<main>{body}</main>
<script>
function updateTotal() {{
  let total = 0;
  let count = 0;
  document.querySelectorAll('[data-price]').forEach(input => {{
    if (input.checked) {{
      total += Number(input.dataset.price || 0);
      count += 1;
    }}
  }});
  const totalElement = document.getElementById('selected-total');
  const countElement = document.getElementById('selected-count');
  if (totalElement) totalElement.textContent = '$' + total.toFixed(2);
  if (countElement) countElement.textContent = String(count);
}}

function fmtSeconds(seconds) {{
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}}

function startLoadingTimers() {{
  const start = Date.now();
  setInterval(() => {{
    const elapsed = Math.floor((Date.now() - start) / 1000);
    document.querySelectorAll('[data-loading-label]').forEach(el => {{
      el.textContent = el.getAttribute('data-loading-label') + ' ' + fmtSeconds(elapsed);
    }});
  }}, 1000);
}}

function pollProductStatus() {{
  const el = document.querySelector('[data-refresh-product]');
  if (!el) return;
  const productId = el.getAttribute('data-refresh-product');
  const requestId = el.getAttribute('data-request-id') || '';
  const wantsImage = el.getAttribute('data-needs-image') === '1';
  const wantsGasket = el.getAttribute('data-needs-gasket') === '1';
  if (!wantsImage && !wantsGasket) return;

  setInterval(async () => {{
    try {{
      let url = '/api/match/status?product_id=' + encodeURIComponent(productId);
      if (requestId) {{
        url += '&request_id=' + encodeURIComponent(requestId);
      }}
      const response = await fetch(url, {{ cache: 'no-store' }});
      if (!response.ok) return;
      const data = await response.json();
      if (!data.job) return;
      if (data.job.job_status === 'done') {{
        window.location.reload();
        return;
      }}
      const imageReady = wantsImage && data.product && data.product.product_image_url;
      const gasketReady = wantsGasket && data.gasket_items && data.gasket_items.length > 0;
      if (imageReady || gasketReady) {{
        window.location.reload();
      }}
    }} catch (_) {{
      // polling failures are non-blocking
    }}
  }}, 2000);
}}

document.addEventListener('change', updateTotal);
window.addEventListener('load', updateTotal);
window.addEventListener('load', startLoadingTimers);
window.addEventListener('load', pollProductStatus);
</script>
</body></html>""".encode("utf-8")


def render_home(message: str = "") -> bytes:
    warning = f"<p style='color:#9f4b12'>{esc(message)}</p>" if message else ""
    return page("Gasket Match", f"""
<section class="hero"><div><h1>Find the Right Refrigerator Door Gasket Fast</h1>
<p>Upload the equipment nameplate. We read it first, you confirm the details, then the site matches the live database.</p>
<div class="summary"><div class="metric"><span>Step 1</span><strong>Upload</strong></div><div class="metric"><span>Step 2</span><strong>Confirm</strong></div><div class="metric"><span>Step 3</span><strong>Match</strong></div></div>
</div><form method="post" action="/read-nameplate" enctype="multipart/form-data"><h2>Upload nameplate</h2>{warning}
<div class="upload-row"><div><label>Nameplate photo</label><input type="file" name="nameplate" accept="image/*"></div><button type="submit">Read nameplate</button></div>
<div class="grid"><div><label>Brand fallback</label><input name="brand"></div><div><label>Model fallback</label><input name="equipment_model"></div><div><label>Customer name</label><input name="customer_name"></div></div>
<p class="muted">You can correct the brand or model before matching the database.</p></form></section>""")


def render_confirm_nameplate(
    upload_url: str,
    customer: dict,
    nameplate_data: dict,
    fallback_brand: str = "",
    fallback_model: str = "",
    request_id: str | None = None,
) -> bytes:
    brand = nameplate_data.get("brand") or fallback_brand
    model = nameplate_data.get("model") or fallback_model
    raw_text = nameplate_data.get("raw_text") or ""
    request_id = request_id or generate_request_id()
    return page("Confirm Nameplate", f"""
<section><h2>Confirm nameplate information</h2>
<p>Check the uploaded nameplate against the information below. If anything is wrong, edit it before matching the database.</p>
<div class="result-grid"><div><h3>Nameplate photo</h3><img class="photo" src="{esc(upload_url)}" alt="Uploaded nameplate"></div>
<form method="post" action="/match" enctype="multipart/form-data"><h3>Read information</h3>
<input type="hidden" name="upload_url" value="{esc(upload_url)}">
<input type="hidden" name="customer_name" value="{esc(customer.get('customer_name') or '')}">
<input type="hidden" name="customer_email" value="{esc(customer.get('customer_email') or '')}">
<input type="hidden" name="customer_phone" value="{esc(customer.get('customer_phone') or '')}">
<input type="hidden" name="request_id" value="{esc(request_id)}">
<div class="grid"><div><label>Brand</label><input name="brand" value="{esc(brand or '')}"></div><div><label>Model</label><input name="equipment_model" value="{esc(model or '')}"></div><div><label>Serial</label><input name="serial_number" value="{esc(nameplate_data.get('serial_number') or '')}"></div><div><label>Manufacturer</label><input name="manufacturer" value="{esc(nameplate_data.get('manufacturer') or '')}"></div><div><label>Manufacture date</label><input name="manufacture_date" value="{esc(nameplate_data.get('manufacture_date') or '')}"></div><div><label>Refrigerant</label><input name="refrigerant" value="{esc(nameplate_data.get('refrigerant') or '')}"></div><div><label>Voltage</label><input name="voltage" value="{esc(nameplate_data.get('voltage') or '')}"></div></div>
<label>Raw text</label><textarea name="raw_text" style="width:100%;min-height:110px;border:1px solid #dbe2ea;border-radius:6px;padding:10px">{esc(raw_text)}</textarea>
<p><button type="submit">Confirm and match database</button> <a class="button" href="/">Upload another</a></p>
</form></div></section>""")


def render_no_match(brand: str, model: str, upload_url: str | None, nameplate_data: dict) -> bytes:
    plate = f"<img class='plate' src='{esc(upload_url)}' alt='Uploaded nameplate'>" if upload_url else ""
    return page("No Match", f"""
<section><h2>&#25105;&#20204;&#27491;&#22312;&#21152;&#36733;&#36164;&#26009;</h2>
<p class="muted">&#24050;&#25910;&#21040;&#35813;&#20912;&#31665;&#22411;&#21495;&#65292;&#31995;&#32479;&#27491;&#22312;&#21305;&#37197;&#20135;&#21697;&#22270;&#29255;&#12289;&#38376;&#20301;&#21644;&#23494;&#23553;&#26465;&#36164;&#26009;&#12290;</p>
{plate}<div class="facts"><div>Brand read</div><div><strong>{esc(brand or 'Not found')}</strong></div><div>Model read</div><div><strong>{esc(model or 'Not found')}</strong></div><div>Serial</div><div>{esc(nameplate_data.get('serial_number') or 'Not found')}</div><div>Raw text</div><div>{esc(nameplate_data.get('raw_text') or '')}</div></div>
<p><a class="button" href="/">Try another nameplate</a></p></section>""")


def _render_result_legacy(
    product: dict,
    quote_items: list[dict],
    request: dict | None,
    upload_url: str | None,
    request_id: str | None = None,
) -> bytes:
    return render_result(product, quote_items, request, upload_url, request_id)


def render_result(
    product: dict,
    quote_items: list[dict],
    request: dict | None,
    upload_url: str | None,
    request_id: str | None = None,
    match_state: dict | None = None,
) -> bytes:
    nameplate_data = (request or {}).get("nameplate_data") or {}
    pending_new_product = is_unconfirmed_new_product(product)
    positions = [] if pending_new_product else infer_door_positions(product)
    quantity = 0 if pending_new_product else (len(positions) or estimated_gasket_quantity(product, quote_items))
    product_img = product.get("product_image_url")
    state = match_state or get_product_match_state(product, quote_items)
    needs_image = "image" in (state.get("needs") or [])
    needs_gasket = "gasket_items" in (state.get("needs") or [])
    stage_message = state.get("message") or _pipeline_stage_label(state.get("pipeline_stage", ""))
    loading_banner = "<section><h2>&#25105;&#20204;&#27491;&#22312;&#21152;&#36733;&#36164;&#26009;</h2></section>" if needs_image or needs_gasket else ""
    product_loading = "&#22270;&#29255;&#27491;&#22312;&#21152;&#36733;"
    gasket_loading = "&#23494;&#23553;&#26465;&#36164;&#26009;&#27491;&#22312;&#21152;&#36733;"
    product_html = f"<img class='photo' src='{esc(product_img)}' alt='Refrigerator product image'>" if product_img else f"<div class='photo loading'><span data-loading-label='{product_loading}'>{product_loading} 00:00</span></div>"
    plate_html = f"<img class='plate' src='{esc(upload_url)}' alt='Uploaded nameplate'>" if upload_url else "<div class='plate muted'>Nameplate photo</div>"

    rows = []
    primary_item = quote_items[0] if quote_items else None
    if pending_new_product and not primary_item:
        rows.append(f"""<div class="item"><input type="checkbox" disabled><div class="loading" style="width:98px;height:78px;border:1px solid #dbe2ea;border-radius:6px"><span data-loading-label="{gasket_loading}">{gasket_loading} 00:00</span></div><div><strong>{gasket_loading}</strong></div><div class="price"><strong>Loading</strong></div><div></div></div>""")

    position_rows = map_quote_items_to_positions(quote_items, positions or door_positions_for_count(quantity))
    for index, (position, item) in enumerate(zip(positions or door_positions_for_count(quantity), position_rows), start=1):
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
        rows.append(f"""<label class="item"><input type="checkbox" name="selected_doors" value="{esc(door_key)}" data-price="{line_price}" checked>{image_html}<div><strong>{esc(door_label)} Gasket</strong><p>{esc(dims)}{perimeter_html}<br>Source: {esc(item.get('source_name'))}</p></div><div class="price"><strong>{money(line_price)}</strong><small>each selected door</small></div>{part_html}</label>""")

    summary_html = "" if pending_new_product else f"""<div class="summary"><div class="metric"><span>Required gaskets</span><strong>{quantity}</strong></div><div class="metric"><span>Selected</span><strong id="selected-count">0</strong></div><div class="metric"><span>Total</span><strong id="selected-total">$0.00</strong></div></div>"""
    rows_html = "".join(rows) if rows else f"""<div class="item"><input type="checkbox" disabled><div class="loading" style="width:98px;height:78px;border:1px solid #dbe2ea;border-radius:6px"><span data-loading-label="{gasket_loading}">{gasket_loading} 00:00</span></div><div><strong>{gasket_loading}</strong></div><div class="price"><strong>Loading</strong></div><div></div></div>"""
    checkout_form = (
        f"""
<div data-refresh-product="{esc(product['id'])}" data-request-id="{esc(request_id or '')}" data-needs-image="{1 if needs_image else 0}" data-needs-gasket="{1 if needs_gasket else 0}" hidden></div>
{loading_banner}<form method="post" action="/checkout">
  <input type="hidden" name="product_id" value="{esc(product['id'])}">
  <input type="hidden" name="request_id" value="{esc(request_id or '')}">
  <input type="hidden" name="request_row_id" value="{esc(str(request.get('id') or '')) if request else ''}">
  <input type="hidden" name="match_stage_message" value="{esc(stage_message)}">
  <section><h2>Matched refrigerator</h2><div class="result-grid"><div><h3>Refrigerator image</h3>{product_html}</div><div><h3>Nameplate</h3>{plate_html}</div><div><h3>Nameplate summary</h3><div class="facts"><div>OpenAI brand</div><div><strong>{esc(nameplate_data.get('brand') or product.get('brand'))}</strong></div><div>OpenAI model</div><div><strong>{esc(nameplate_data.get('model') or product.get('equipment_model'))}</strong></div><div>Serial</div><div>{esc(nameplate_data.get('serial_number') or 'Not found')}</div><div>Brand</div><div><strong>{esc(product.get('brand'))}</strong></div><div>Model</div><div><strong>{esc(product.get('equipment_model'))}</strong></div></div></div></div></section>
  <section class="summary"><h2>Data quality</h2><p class="muted">{esc(stage_message)}</p></section>
  <section><h2>Gasket quote</h2>{summary_html}<div>{rows_html}</div></section>
  <div class="checkout"><strong>Ready to order?</strong><br><span class="muted">Select the gasket solution for this refrigerator.</span>
    <div style="margin-top:10px"><button type="submit">Continue to shipping</button></div>
  </div>
</form>""")
    return page("Matched Gasket Quote", checkout_form)


def render_checkout(
    product: dict,
    quote_items: list[dict],
    selected_doors: list[str],
    request_id: str | None,
    request_row_id: str | None = None,
    shipping: dict[str, str] | None = None,
    payment_link: str | None = None,
    selected_warning: str | None = None,
) -> bytes:
    selected_items = selected_checkout_items(quote_items, selected_doors, strict=False)

    total = sum(float(item.get("line_price", 0) or 0) for item in selected_items)
    has_shipping = bool(shipping)
    row_lines = "".join(
        f"<li><strong>{esc(item.get('gasket', 'Unknown'))}</strong> · {esc(item.get('door_key'))} · ${float(item.get('line_price') or 0):.2f}</li>"
        for item in selected_items
    )

    if has_shipping:
        name = esc(shipping.get("customer_name", ""))
        email = esc(shipping.get("customer_email", ""))
        phone = esc(shipping.get("customer_phone", ""))
        address = esc(shipping.get("shipping_address", ""))
        payment_button = (
            f"<a class='button' href='{esc(payment_link)}' target='_blank' rel='noopener'>Open payment page</a>"
            if payment_link else "<p class='muted'>Payment link not configured yet.</p>"
        )
        body = f"""
  <section>
    <h2>Checkout</h2>
    <p class='muted'>Customer: {name} · {email} · {phone}</p>
    <p><strong>Shipping address:</strong> {address}</p>
    <div><strong>Selected items</strong><ul>{row_lines}</ul></div>
    <p><strong>Total:</strong> ${total:.2f}</p>
    {payment_button}
  </section>
"""
    else:
        selected_inputs = "".join(
            f"<input type='hidden' name='selected_doors' value='{esc(item.get('door_key'))}'>"
            for item in selected_items
            if item.get("door_key")
        )
        warning_html = ""
        if selected_warning:
            warning_html = f"<p class='muted' style='color:#7a1f1f'>{esc(selected_warning)}</p>"
        body = f"""
  <section>
    <h2>Shipping details</h2>
    <p>Confirm the selected gasket options and shipping details. After submit, the payment button will appear.</p>
    {warning_html}
    <form method="post" action="/checkout">
      <input type="hidden" name="product_id" value="{esc(product.get('id'))}">
      <input type="hidden" name="request_id" value="{esc(request_id or '')}">
      <input type="hidden" name="request_row_id" value="{esc(request_row_id or '')}">
      <input type="hidden" name="step" value="shipping">
      {selected_inputs}
      <div class="grid">
        <div><label>Customer name</label><input name="customer_name" required></div>
        <div><label>Email</label><input name="customer_email" type="email" required></div>
        <div><label>Phone</label><input name="customer_phone"></div>
      </div>
      <div><label>Shipping address</label><textarea name="shipping_address" rows="3" required></textarea></div>
      <div><strong>Items</strong><ul>{row_lines}</ul><p><strong>Total: ${total:.2f}</strong></p></div>
      <button type="submit">Continue to payment</button>
    </form>
  </section>
"""
    return page("Checkout", f"""
<section><h2>Order summary</h2><div><strong>{esc(product.get('brand') or '')} {esc(product.get('equipment_model') or '')}</strong> · {esc(product.get('product_image_url') or '')}</div></section>
{body}
<p><a href="/">Start another match</a></p>
""")


def selected_checkout_items(quote_items: list[dict], selected_doors: list[str], strict: bool = False) -> list[dict]:
    selected_norm = [normalize_door_key(x) for x in (selected_doors or []) if x]
    selected_items: list[dict] = []
    seen: set[str] = set()

    for item in quote_items:
        key = normalize_door_key(item.get("door_position") or item.get("door_position_display") or item.get("door_key") or "")
        if not key:
            continue
        if selected_norm and key not in selected_norm:
            continue
        if key in seen:
            continue
        seen.add(key)
        row = dict(item)
        row["door_key"] = key
        row["line_price"] = float(item.get("final_price_usd") or 0)
        row["gasket"] = item.get("part_number") or item.get("gasket") or item.get("universal_part_number") or "TBD"
        selected_items.append(row)
        if not selected_norm:
            break

    if selected_norm and not selected_items:
        if strict:
            return []
        for item in quote_items:
            key = normalize_door_key(item.get("door_position") or item.get("door_position_display") or item.get("door_key") or "")
            if not key:
                continue
            if key in seen:
                continue
            seen.add(key)
            row = dict(item)
            row["door_key"] = key
            row["line_price"] = float(item.get("final_price_usd") or 0)
            row["gasket"] = item.get("part_number") or item.get("gasket") or item.get("universal_part_number") or "TBD"
            selected_items.append(row)
            if len(selected_items) >= len(selected_norm):
                break

    if not selected_items:
        if strict:
            return []
        for item in quote_items[:1]:
            selected_items.append({
                "door_key": normalize_door_key(item.get("door_position") or item.get("door_position_display") or ""),
                "line_price": float(item.get("final_price_usd") or 0),
                "gasket": item.get("part_number") or item.get("universal_part_number") or "TBD",
                "dimensions": item.get("dimensions_text") or f"{item.get('width_in') or '-'} x {item.get('height_in') or '-'} in",
            })
        if not selected_items:
            selected_items = [{
                "door_key": "unknown",
                "line_price": 0.0,
                "gasket": "TBD",
                "dimensions": "Pending",
            }]

    return selected_items


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

    def send_json(self, data: dict[str, object], status: int = HTTPStatus.OK) -> None:
        send_json(self, data, status)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_html(render_home())
            return
        if parsed.path in {"/admin", "/admin/"}:
            if not is_admin_authenticated(self):
                self.send_html(admin_login_response(next_path="/admin"))
                return
            with httpx.Client(timeout=30) as client:
                self.send_html(admin_dashboard_page(client, parse_qs(parsed.query)))
            return
        if parsed.path == "/admin/login":
            self.send_html(admin_login_response(next_path=(parse_qs(parsed.query).get("next", ["/admin"])[0] or "/admin")))
            return
        if parsed.path == "/admin/logout":
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", f"{ADMIN_COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
            self.end_headers()
            return
        if parsed.path == "/api/match/status":
            params = parse_qs(parsed.query)
            product_id = params.get("product_id", [""])[0]
            request_id = (params.get("request_id", [""])[0] or "").strip()
            with httpx.Client(timeout=30) as client:
                product_id_int = int(product_id) if str(product_id).isdigit() else None
                job = fetch_match_job(client, product_id=product_id_int, request_id=request_id or None)
                if not product_id_int and job and job.get("refrigerator_product_id"):
                    product_id = str(int(job.get("refrigerator_product_id")))
                    product_id_int = int(product_id)
                product = get_product(client, product_id_int) if product_id_int else None
                quote_items = get_quote_items(client, product_id_int) if product else []
                state = get_product_match_state(product, quote_items) if product else {
                    "state": "no_match",
                    "message": "No matching product found.",
                    "ready_for_checkout": False,
                    "needs": ["product_not_found"],
                    "pipeline_stage": MATCH_STATUS_PENDING,
                    "is_ready": False,
                }
                if product:
                    # If job lost or never started, continue the pipeline immediately.
                    if not job or job.get("job_status") in {"pending", None, "failed"}:
                        if state["needs"]:
                            run_match_job_background(
                                product["id"],
                                product.get("brand") or "",
                                product.get("equipment_model") or "",
                                request_id or None,
                            )
                    job = job or fetch_match_job(client, product_id=product.get("id"), request_id=request_id or None)
                job = job or fetch_match_job(
                    client,
                    product_id=product["id"] if product else None,
                    request_id=request_id or None,
                )
            state["job"] = job
            self.send_json(build_match_payload(product, quote_items, state))
            return
        if parsed.path == "/api/match":
            params = parse_qs(parsed.query)
            brand = (params.get("brand", [""])[0] or "").strip()
            model = (params.get("model", [""])[0] or "").strip()
            request_id = (params.get("request_id", [""])[0] or "").strip()
            if not brand or not model:
                self.send_json({"error": "brand and model are required"}, HTTPStatus.BAD_REQUEST)
                return
            with httpx.Client(timeout=30) as client:
                product, quote_items, match_state = match_product(
                    client,
                    brand=brand,
                    model=model,
                    request_id=request_id or None,
                    create_if_missing=True,
                    auto_enrich=True,
                )
                self.send_json(build_match_payload(product, quote_items, match_state))
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
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in {"/read-nameplate", "/match", "/api/match", "/checkout", "/admin/login", "/admin/action"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = self.headers.get("Content-Type", "")
        if path == "/admin/login":
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            parsed_fields = parse_form_fields(body, content_type)
            password = (parsed_fields.get("password", [""])[0] or "").strip()
            next_path = (parsed_fields.get("next", [""])[0] or "/admin").strip() or "/admin"
            if not ADMIN_PASSWORD:
                self.send_html(admin_login_response(next_path=next_path, error="管理员密码尚未配置。请先在 .env 中设置 ADMIN_PASSWORD。"), HTTPStatus.FORBIDDEN)
                return
            if not secrets.compare_digest(password, ADMIN_PASSWORD):
                self.send_html(admin_login_response(next_path=next_path, error="密码不正确。"), HTTPStatus.UNAUTHORIZED)
                return
            token = admin_session_token()
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", next_path if next_path.startswith("/admin") else "/admin")
            self.send_header("Set-Cookie", f"{ADMIN_COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={ADMIN_SESSION_MAX_AGE_SECONDS}")
            self.end_headers()
            return
        if path == "/admin/action":
            if not is_admin_authenticated(self):
                self.send_html(admin_login_response(next_path="/admin"), HTTPStatus.UNAUTHORIZED)
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            parsed_fields = parse_form_fields(body, content_type)
            table = (parsed_fields.get("table", [""])[0] or "").strip()
            action = (parsed_fields.get("action", [""])[0] or "").strip()
            row_id_raw = (parsed_fields.get("row_id", [""])[0] or "").strip()
            redirect_path = (parsed_fields.get("redirect", [""])[0] or "/admin").strip() or "/admin"
            if not row_id_raw.isdigit():
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            row_id = int(row_id_raw)
            with httpx.Client(timeout=30) as client:
                if table == "requests":
                    payload: dict[str, object] = {}
                    if action == "customer_confirmed":
                        payload["status"] = "customer_confirmed"
                    elif action == "staff_verified":
                        payload["status"] = "staff_verified"
                    elif action == "factory_sent":
                        payload["status"] = "factory_sent"
                        payload["factory_sent_at"] = now_iso()
                    else:
                        self.send_error(HTTPStatus.BAD_REQUEST)
                        return
                    update_request_fields(client, row_id, payload)
                elif table == "products":
                    payload = {}
                    if action == "product_verified":
                        payload["product_image_verified"] = True
                        payload["data_status"] = "verified"
                    elif action == "system_candidate":
                        payload["data_status"] = "system_candidate"
                    else:
                        self.send_error(HTTPStatus.BAD_REQUEST)
                        return
                    update_product_fields(client, row_id, payload)
                else:
                    self.send_error(HTTPStatus.BAD_REQUEST)
                    return
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", redirect_path)
            self.end_headers()
            return
        if path == "/api/match":
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8", errors="ignore")
            parsed = parse_match_input(body=raw_body, content_type=content_type)
            brand = parsed.get("brand", "")
            model = parsed.get("model", "")
            request_id = parsed.get("request_id")
            if not brand or not model:
                self.send_json({"error": "brand and model are required"}, HTTPStatus.BAD_REQUEST)
                return
            with httpx.Client(timeout=30) as client:
                product, quote_items, match_state = match_product(
                    client,
                    brand=brand,
                    model=model,
                    request_id=request_id or None,
                    create_if_missing=True,
                    auto_enrich=True,
                )
                state_payload = build_match_payload(product, quote_items, match_state)
                if product:
                    state_payload["gasket_items"] = [item for item in state_payload["gasket_items"]]
            self.send_json(state_payload)
            return
        if path == "/checkout":
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            parsed_fields = parse_form_fields(body, content_type)
            step = (parsed_fields.get("step", [""])[0] or "").lower().strip()
            product_id_raw = (parsed_fields.get("product_id", [""])[0] or "").strip()
            request_id = (parsed_fields.get("request_id", [""])[0] or "").strip()
            request_row_id = (parsed_fields.get("request_row_id", [""])[0] or "").strip()
            selected_doors = parsed_fields.get("selected_doors", [])
            if selected_doors:
                selected_doors = [item.strip() for item in selected_doors if item]
            if not product_id_raw.isdigit():
                self.send_html(render_home("Checkout requires a valid product id"), HTTPStatus.BAD_REQUEST)
                return
            product_id = int(product_id_raw)

            with httpx.Client(timeout=30) as client:
                product = get_product(client, product_id)
                if not product:
                    self.send_html(render_home("Product not found"), HTTPStatus.NOT_FOUND)
                    return
                quote_items = get_quote_items(client, product_id)

            if step == "shipping":
                shipping = {
                    "customer_name": (parsed_fields.get("customer_name", [""])[0] or "").strip(),
                    "customer_email": (parsed_fields.get("customer_email", [""])[0] or "").strip(),
                    "customer_phone": (parsed_fields.get("customer_phone", [""])[0] or "").strip(),
                    "shipping_address": (parsed_fields.get("shipping_address", [""])[0] or "").strip(),
                }
                payment_items = selected_checkout_items(quote_items, selected_doors, strict=True)
                if not payment_items:
                    self.send_html(
                        render_checkout(
                            product,
                            quote_items,
                            selected_doors,
                            request_id,
                            request_row_id=request_row_id,
                            selected_warning="Please select at least one gasket before continuing to payment.",
                        ),
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                if not shipping["customer_name"] or not shipping["customer_email"] or not shipping["shipping_address"]:
                    self.send_html(
                        render_checkout(
                            product,
                            quote_items,
                            selected_doors,
                            request_id,
                            request_row_id=request_row_id,
                            shipping=None,
                            selected_warning="Please complete customer name, email and shipping address.",
                        ),
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                with httpx.Client(timeout=30) as client:
                    if request_row_id.isdigit():
                        update_request_status(client, int(request_row_id), "customer_confirmed")
                    # persist a lightweight follow-up request for traceability
                    try:
                        create_request(
                            client,
                            {
                                "customer_name": shipping["customer_name"],
                                "customer_email": shipping["customer_email"],
                                "customer_phone": shipping["customer_phone"],
                            },
                            None,
                            product.get("brand") or "",
                            product.get("equipment_model") or "",
                            product,
                            {
                                "raw_text": "",
                                "confidence": 100,
                            },
                        )
                    except Exception:
                        pass
                payment_items = selected_checkout_items(quote_items, selected_doors, strict=True)
                payment_link = build_shopify_checkout_url(product, payment_items, shipping)
                self.send_html(render_checkout(product, quote_items, selected_doors, request_id, request_row_id, shipping, payment_link))
                return

            self.send_html(render_checkout(product, quote_items, selected_doors, request_id, request_row_id))
            return

        fields = parse_multipart(self.rfile.read(int(self.headers.get("Content-Length", "0"))), content_type)
        brand = fields.get("brand", {}).get("text", "").strip()
        model = fields.get("equipment_model", {}).get("text", "").strip()
        upload_url = fields.get("upload_url", {}).get("text", "").strip() or None
        nameplate_data = {}
        file_field = fields.get("nameplate")
        customer = {key: fields.get(key, {}).get("text") or None for key in ("customer_name", "customer_email", "customer_phone")}
        request_id = (fields.get("request_id", {}).get("text") or "").strip()
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
            self.send_html(render_confirm_nameplate(upload_url, customer, nameplate_data, brand, model, request_id or None))
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
            self.send_html(
                render_confirm_nameplate(upload_url or "", customer, nameplate_data, brand, model, request_id or None),
                HTTPStatus.BAD_REQUEST,
            )
            return
        with httpx.Client(timeout=30) as client:
            product, quote_items, match_state = match_product(
                client,
                brand=brand,
                model=model,
                request_id=request_id or None,
                create_if_missing=True,
                auto_enrich=True,
            )
            try:
                from ai_product_research import enrich_confirmed_product

                product = enrich_confirmed_product(client, product, nameplate_data, force=True)
            except Exception as exc:
                print(f"AI product research failed for {brand} {model}: {exc}")
            request = create_request(client, customer, upload_url, brand, model, product, nameplate_data)
            self.send_html(render_result(product, quote_items, request, upload_url, request_id=request_id or None))


def main() -> None:
    port = int(os.getenv("PORT") or os.getenv("CUSTOMER_DEMO_PORT", "8010"))
    print(f"Nameplate web app running on port {port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()

