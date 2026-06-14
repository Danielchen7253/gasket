import base64
from datetime import datetime, timezone
import hashlib
import hmac
import html
import json
import mimetypes
import os
import re
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from dotenv import load_dotenv


load_dotenv(Path(__file__).with_name(".env"))

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_NAMEPLATE_API_KEY = os.getenv("OPENAI_NAMEPLATE_API_KEY", OPENAI_API_KEY).strip()
OPENAI_NAMEPLATE_MODEL = os.getenv("OPENAI_NAMEPLATE_MODEL", "gpt-4.1-mini")
SHOPIFY_STORE_DOMAIN = os.getenv("SHOPIFY_STORE_DOMAIN", "").strip()
SHOPIFY_STOREFRONT_ACCESS_TOKEN = os.getenv("SHOPIFY_STOREFRONT_ACCESS_TOKEN", "").strip()
SHOPIFY_ADMIN_ACCESS_TOKEN = os.getenv("SHOPIFY_ADMIN_ACCESS_TOKEN", "").strip()
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-10").strip()
GOOGLE_PLACES_BROWSER_KEY = os.getenv("GOOGLE_PLACES_BROWSER_KEY", os.getenv("GOOGLE_MAPS_BROWSER_KEY", "")).strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
ADMIN_SESSION_SECRET = os.getenv("ADMIN_SESSION_SECRET", ADMIN_PASSWORD or SUPABASE_SERVICE_ROLE_KEY).strip()
ADMIN_COOKIE_NAME = "gasket_admin_session"
ADMIN_SESSION_SECONDS = 60 * 60 * 12

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


def google_places_loader() -> str:
    if not GOOGLE_PLACES_BROWSER_KEY:
        return ""
    return f"""<script src="https://maps.googleapis.com/maps/api/js?key={esc(GOOGLE_PLACES_BROWSER_KEY)}&libraries=places&callback=initShippingAutocomplete" async defer></script>"""


def money(value) -> str:
    return "TBD" if value in (None, "") else f"${float(value):,.2f}"


def customer_gasket_size(item: dict) -> str:
    width = item.get("width_in")
    height = item.get("height_in")
    if width not in (None, "") and height not in (None, ""):
        return f'{float(width):g}" x {float(height):g}"'
    dimensions = (item.get("dimensions_text") or "").strip()
    if dimensions:
        match = re.search(
            r'(\d+(?:\.\d+)?(?:-\d+/\d+|/\d+)?|\d+\s+\d+/\d+)\s*(?:"|in|inch|inches)?\s*[x×]\s*(\d+(?:\.\d+)?(?:-\d+/\d+|/\d+)?|\d+\s+\d+/\d+)\s*(?:"|in|inch|inches)?',
            dimensions,
            re.IGNORECASE,
        )
        if match:
            return f'{match.group(1).strip()}" x {match.group(2).strip()}"'
        blocked = ["not publicly", "official", "partsdr", "partselect", "confirm", "oem"]
        if not any(token in dimensions.lower() for token in blocked):
            return dimensions
    perimeter = item.get("perimeter_in")
    if perimeter not in (None, ""):
        return f'Perimeter {float(perimeter):g}"'
    return "Size to confirm"


def shopify_variant_gid(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if value.startswith("gid://shopify/ProductVariant/"):
        return value
    if value.isdigit():
        return f"gid://shopify/ProductVariant/{value}"
    return value


def shopify_variant_for_quote_item(item: dict) -> str | None:
    price = float(item.get("final_price_usd") or item.get("base_price_usd") or 0)
    perimeter = float(item.get("perimeter_in") or 0)
    if price <= 45.01 or (perimeter and perimeter < 98):
        keys = ["SHOPIFY_VARIANT_GASKET_45", "SHOPIFY_VARIANT_UNDER_98"]
    elif price <= 68.01 or (perimeter and perimeter < 117):
        keys = ["SHOPIFY_VARIANT_GASKET_68", "SHOPIFY_VARIANT_UNDER_117"]
    elif price <= 90.01 or (perimeter and perimeter < 146):
        keys = ["SHOPIFY_VARIANT_GASKET_90", "SHOPIFY_VARIANT_UNDER_146"]
    else:
        keys = ["SHOPIFY_VARIANT_GASKET_120", "SHOPIFY_VARIANT_UNDER_190"]
    for key in keys:
        variant = shopify_variant_gid(os.getenv(key))
        if variant:
            return variant
    return None


def shopify_ready() -> bool:
    return bool(SHOPIFY_STORE_DOMAIN and (SHOPIFY_STOREFRONT_ACCESS_TOKEN or SHOPIFY_ADMIN_ACCESS_TOKEN))


def quote_item_attributes(product: dict, item: dict) -> list[dict[str, str]]:
    return [
        {"key": "Brand", "value": str(product.get("brand") or "")},
        {"key": "Model", "value": str(product.get("equipment_model") or "")},
        {"key": "Door position", "value": str(item.get("door_position_display") or item.get("door_position") or "")},
        {"key": "Size", "value": customer_gasket_size(item)},
        {"key": "Quoted price", "value": money(item.get("final_price_usd"))},
        {"key": "Product record ID", "value": str(product.get("id") or "")},
        {"key": "Door key", "value": str(item.get("door_position") or "")},
    ]


def split_customer_name(full_name: str) -> tuple[str, str]:
    parts = (full_name or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def shipping_attributes(customer_info: dict | None) -> list[dict]:
    info = customer_info or {}
    labels = {
        "customer_email": "Customer email",
        "customer_name": "Customer name",
        "customer_phone": "Customer phone",
        "shipping_address1": "Shipping address",
        "shipping_address2": "Shipping address 2",
        "shipping_city": "Shipping city",
        "shipping_state": "Shipping state",
        "shipping_zip": "Shipping ZIP",
        "shipping_country": "Shipping country",
    }
    return [{"key": label, "value": str(info.get(key) or "")} for key, label in labels.items()]


def shopify_shipping_address(customer_info: dict | None) -> dict:
    info = customer_info or {}
    first_name, last_name = split_customer_name(str(info.get("customer_name") or ""))
    address = {
        "firstName": first_name,
        "lastName": last_name,
        "phone": str(info.get("customer_phone") or ""),
        "address1": str(info.get("shipping_address1") or ""),
        "address2": str(info.get("shipping_address2") or ""),
        "city": str(info.get("shipping_city") or ""),
        "province": str(info.get("shipping_state") or ""),
        "zip": str(info.get("shipping_zip") or ""),
        "country": str(info.get("shipping_country") or "United States"),
    }
    return {key: value for key, value in address.items() if value}


def create_shopify_draft_order(client: httpx.Client, product: dict, quote_items: list[dict], customer_info: dict | None = None) -> str:
    if not SHOPIFY_STORE_DOMAIN or not SHOPIFY_ADMIN_ACCESS_TOKEN:
        raise ValueError("Shopify Admin API is not configured.")
    customer_email = str((customer_info or {}).get("customer_email") or "").strip()
    shipping_address = shopify_shipping_address(customer_info)
    line_items = []
    for item in quote_items:
        variant_id = shopify_variant_for_quote_item(item)
        if not variant_id:
            raise ValueError(f"Missing Shopify variant for {money(item.get('final_price_usd'))} gasket.")
        line_items.append(
            {
                "variantId": variant_id,
                "quantity": 1,
                "customAttributes": quote_item_attributes(product, item),
            }
        )
    mutation = """
    mutation DraftOrderCreate($input: DraftOrderInput!) {
      draftOrderCreate(input: $input) {
        draftOrder { id invoiceUrl }
        userErrors { field message }
      }
    }
    """
    endpoint = f"https://{SHOPIFY_STORE_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    response = client.post(
        endpoint,
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": SHOPIFY_ADMIN_ACCESS_TOKEN,
        },
        json={
            "query": mutation,
            "variables": {
                "input": {
                    **({"email": customer_email} if customer_email else {}),
                    **({"shippingAddress": shipping_address} if shipping_address else {}),
                    "lineItems": line_items,
                    "customAttributes": [
                        {"key": "Source", "value": "Gasket nameplate match"},
                        {"key": "Product record ID", "value": str(product.get("id") or "")},
                        {"key": "Brand", "value": str(product.get("brand") or "")},
                        {"key": "Model", "value": str(product.get("equipment_model") or "")},
                    ] + shipping_attributes(customer_info),
                }
            },
        },
    )
    response.raise_for_status()
    payload = response.json()
    errors = payload.get("errors") or payload.get("data", {}).get("draftOrderCreate", {}).get("userErrors") or []
    if errors:
        raise ValueError(json.dumps(errors, ensure_ascii=False))
    invoice_url = payload.get("data", {}).get("draftOrderCreate", {}).get("draftOrder", {}).get("invoiceUrl")
    if not invoice_url:
        raise ValueError("Shopify did not return a payment URL.")
    return invoice_url


def create_shopify_cart(client: httpx.Client, product: dict, quote_items: list[dict], customer_info: dict | None = None) -> str:
    if not shopify_ready():
        raise ValueError("Shopify is not configured.")
    customer_email = str((customer_info or {}).get("customer_email") or "").strip()
    customer_phone = str((customer_info or {}).get("customer_phone") or "").strip()
    if not SHOPIFY_STOREFRONT_ACCESS_TOKEN:
        return create_shopify_draft_order(client, product, quote_items, customer_info)
    lines = []
    for item in quote_items:
        variant_id = shopify_variant_for_quote_item(item)
        if not variant_id:
            raise ValueError(f"Missing Shopify variant for {money(item.get('final_price_usd'))} gasket.")
        lines.append(
            {
                "merchandiseId": variant_id,
                "quantity": 1,
                "attributes": quote_item_attributes(product, item),
            }
        )
    mutation = """
    mutation CartCreate($input: CartInput!) {
      cartCreate(input: $input) {
        cart { checkoutUrl }
        userErrors { field message }
      }
    }
    """
    endpoint = f"https://{SHOPIFY_STORE_DOMAIN}/api/{SHOPIFY_API_VERSION}/graphql.json"
    response = client.post(
        endpoint,
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Storefront-Access-Token": SHOPIFY_STOREFRONT_ACCESS_TOKEN,
        },
        json={
            "query": mutation,
            "variables": {
                "input": {
                    "lines": lines,
                    **({"buyerIdentity": {**({"email": customer_email} if customer_email else {}), **({"phone": customer_phone} if customer_phone else {})}} if (customer_email or customer_phone) else {}),
                    "attributes": [
                        {"key": "Source", "value": "Gasket nameplate match"},
                        {"key": "Product record ID", "value": str(product.get("id") or "")},
                        {"key": "Brand", "value": str(product.get("brand") or "")},
                        {"key": "Model", "value": str(product.get("equipment_model") or "")},
                    ] + shipping_attributes(customer_info),
                }
            },
        },
    )
    response.raise_for_status()
    payload = response.json()
    errors = payload.get("errors") or payload.get("data", {}).get("cartCreate", {}).get("userErrors") or []
    if errors:
        raise ValueError(json.dumps(errors, ensure_ascii=False))
    checkout_url = payload.get("data", {}).get("cartCreate", {}).get("cart", {}).get("checkoutUrl")
    if not checkout_url:
        raise ValueError("Shopify did not return a checkout URL.")
    return checkout_url


def admin_signature(expires: int) -> str:
    secret = ADMIN_SESSION_SECRET.encode("utf-8")
    return hmac.new(secret, str(expires).encode("utf-8"), hashlib.sha256).hexdigest()


def make_admin_cookie() -> str:
    expires = int(time.time()) + ADMIN_SESSION_SECONDS
    return f"{expires}:{admin_signature(expires)}"


def parse_cookies(cookie_header: str | None) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in (cookie_header or "").split(";"):
        if "=" not in part:
            continue
        key, value = part.strip().split("=", 1)
        cookies[key] = value
    return cookies


def is_admin_authenticated(cookie_header: str | None) -> bool:
    if not ADMIN_PASSWORD:
        return False
    token = parse_cookies(cookie_header).get(ADMIN_COOKIE_NAME, "")
    if ":" not in token:
        return False
    expires_text, signature = token.split(":", 1)
    try:
        expires = int(expires_text)
    except ValueError:
        return False
    if expires < int(time.time()):
        return False
    return hmac.compare_digest(signature, admin_signature(expires))


def normalize_model(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def normalize_alias(value: str) -> str:
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


def door_positions_for_count(count: int, layout_hint: str = "") -> list[dict]:
    hint = normalize_model(layout_hint)
    if count == 3 and ("FRENCH" in hint or "BOTTOMFREEZER" in hint or "FREEZERDRAWER" in hint):
        rows = [
            ("left_fresh_food_door", "Left refrigerator door"),
            ("right_fresh_food_door", "Right refrigerator door"),
            ("freezer_drawer", "Freezer drawer"),
        ]
    else:
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
        rows = layouts.get(count, [])
    return [{"key": key, "label": label} for key, label in rows]


def infer_door_positions(product: dict) -> list[dict]:
    existing = product.get("door_positions")
    try:
        count = int(product.get("door_count") or 0)
    except Exception:
        count = 0
    if not count:
        count = estimated_gasket_quantity(product, [])
    layout_hint = " ".join(str(product.get(key) or "") for key in ("door_layout", "product_type", "data_source_summary"))
    expected = door_positions_for_count(max(1, min(4, count)), layout_hint)
    if isinstance(existing, list) and existing:
        keys = {item.get("key") for item in existing if isinstance(item, dict)}
        merged = list(existing)
        for item in expected:
            if item["key"] not in keys:
                merged.append(item)
        return merged
    return expected


def door_layout_name(positions: list[dict]) -> str:
    keys = [item.get("key") for item in positions]
    if keys == ["left_fresh_food_door", "right_fresh_food_door", "freezer_drawer"]:
        return "french_door_bottom_freezer"
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
    if not OPENAI_NAMEPLATE_API_KEY:
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
    response = None
    for attempt in range(3):
        response = httpx.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {OPENAI_NAMEPLATE_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=60,
        )
        if response.status_code != 429:
            break
        time.sleep(1.5 * (attempt + 1))
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


def fallback_nameplate_data(error: Exception | str, brand: str = "", model: str = "") -> dict:
    return {
        "brand": brand or None,
        "model": model or None,
        "serial_number": None,
        "manufacturer": None,
        "manufacture_date": None,
        "refrigerant": None,
        "voltage": None,
        "raw_text": "",
        "confidence": 0,
        "recognition_error": str(error),
    }


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

    def best_match(rows: list[dict]) -> dict | None:
        exact = [row for row in rows if normalize_model(row.get("equipment_model", "")) == wanted]
        candidates = exact or rows
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda row: (
                int(bool(row.get("product_image_url"))),
                int(bool(row.get("door_positions")) or bool(row.get("door_count"))),
                int(row.get("id") or 0),
            ),
        )

    for extra_filter in filters:
        endpoint = f"{SUPABASE_URL}/rest/v1/refrigerator_products?select=*{extra_filter}&limit=20"
        response = client.get(endpoint, headers=supabase_headers())
        response.raise_for_status()
        rows = response.json()
        if not rows:
            continue
        for row in rows:
            if normalize_model(row.get("equipment_model", "")) == wanted:
                if brand_q:
                    sibling_endpoint = (
                        f"{SUPABASE_URL}/rest/v1/refrigerator_products?select=*"
                        f"&brand=ilike.*{brand_q}*&limit=300"
                    )
                    sibling_response = client.get(sibling_endpoint, headers=supabase_headers())
                    sibling_response.raise_for_status()
                    sibling = best_match(sibling_response.json())
                    if sibling and normalize_model(sibling.get("equipment_model", "")) == wanted:
                        return sibling
                return row
        return best_match(rows) or rows[0]

    if brand_q and wanted:
        endpoint = (
            f"{SUPABASE_URL}/rest/v1/refrigerator_products?select=*"
            f"&brand=ilike.*{brand_q}*&limit=300"
        )
        response = client.get(endpoint, headers=supabase_headers())
        response.raise_for_status()
        rows = response.json()
        exact = best_match(rows)
        if exact and normalize_model(exact.get("equipment_model", "")) == wanted:
            return exact

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


def selected_quote_items(all_items: list[dict], selected_positions: list[str]) -> list[dict]:
    selected = {value for value in selected_positions if value}
    if not selected:
        return list(all_items)
    chosen = [item for item in all_items if str(item.get("door_position") or "") in selected]
    return chosen or list(all_items)


def checkout_customer_info(fields: dict[str, list[str]]) -> dict:
    keys = [
        "customer_email",
        "customer_name",
        "customer_phone",
        "shipping_address1",
        "shipping_address2",
        "shipping_city",
        "shipping_state",
        "shipping_zip",
        "shipping_country",
    ]
    return {key: ((fields.get(key) or [""])[0] or "").strip() for key in keys}


def missing_checkout_customer_fields(customer_info: dict) -> list[str]:
    required = {
        "customer_email": "email",
        "customer_name": "name",
        "customer_phone": "phone",
        "shipping_address1": "shipping address",
    }
    return [label for key, label in required.items() if not str(customer_info.get(key) or "").strip()]


def render_checkout_error(message: str, product_id: int | None = None) -> bytes:
    back = f"/preview?product_id={product_id}" if product_id else "/"
    return page(
        "Checkout Not Ready",
        f"""<section><h2>Checkout is not ready</h2><p class="muted">{esc(message)}</p><p><a class="button" href="{esc(back)}">Back to quote</a></p></section>""",
    )


def handle_checkout_post(handler, raw_body: bytes) -> None:
    fields = parse_qs(raw_body.decode("utf-8", errors="replace"))
    query_fields = parse_qs(urlparse(handler.path).query)
    product_id = int((fields.get("product_id") or query_fields.get("product_id") or ["0"])[0] or "0")
    selected_positions = fields.get("door_position") or []
    customer_info = checkout_customer_info(fields)
    missing_customer_fields = missing_checkout_customer_fields(customer_info)
    wants_json = (fields.get("ajax") or [""])[0] == "1" or handler.headers.get("X-Requested-With") == "fetch"

    def send_checkout_json(status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    if not product_id:
        if wants_json:
            send_checkout_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Missing product record. Please start the match again."})
            return
        handler.send_html(render_checkout_error("Missing product record. Please start the match again."), HTTPStatus.BAD_REQUEST)
        return
    if missing_customer_fields:
        message = "Please enter " + ", ".join(missing_customer_fields) + " before checkout."
        if wants_json:
            send_checkout_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": message, "missing_fields": missing_customer_fields})
            return
        handler.send_html(render_checkout_error(message, product_id), HTTPStatus.BAD_REQUEST)
        return
    with httpx.Client(timeout=30) as client:
        product = get_product(client, product_id)
        if not product:
            if wants_json:
                send_checkout_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Product record was not found. Please start the match again."})
                return
            handler.send_html(render_checkout_error("Product record was not found. Please start the match again.", product_id), HTTPStatus.NOT_FOUND)
            return
        quote_items = get_quote_items(client, product_id)
        chosen = selected_quote_items(quote_items, selected_positions)
        if not chosen:
            if wants_json:
                send_checkout_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Please select at least one gasket before checkout."})
                return
            handler.send_html(render_checkout_error("Please select at least one gasket before checkout.", product_id), HTTPStatus.BAD_REQUEST)
            return
        try:
            checkout_url = create_shopify_cart(client, product, chosen, customer_info)
            try:
                create_customer_order_record(client, product, chosen, customer_info, checkout_url)
            except Exception as order_exc:
                print(f"internal customer order save failed for product {product_id}: {order_exc}", flush=True)
        except Exception as exc:
            if wants_json:
                send_checkout_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return
            handler.send_html(render_checkout_error(str(exc), product_id), HTTPStatus.BAD_REQUEST)
            return
    if wants_json:
        send_checkout_json(HTTPStatus.OK, {"ok": True, "checkout_url": checkout_url, "customer_email": customer_info.get("customer_email")})
        return
    handler.redirect(checkout_url)


def get_evidence_package(client: httpx.Client, product_id: int) -> dict | None:
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/product_evidence_packages",
        params={
            "select": "*",
            "refrigerator_product_id": f"eq.{product_id}",
            "limit": "1",
        },
        headers=supabase_headers(),
    )
    if response.status_code in {404, 406}:
        return None
    response.raise_for_status()
    rows = response.json()
    return rows[0] if rows else None


def get_evidence_items(client: httpx.Client, product_id: int) -> list[dict]:
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/product_evidence_items",
        params={
            "select": "*",
            "refrigerator_product_id": f"eq.{product_id}",
            "order": "confidence_score.desc.nullslast,created_at.desc",
            "limit": "50",
        },
        headers=supabase_headers(),
    )
    if response.status_code in {404, 406}:
        return []
    response.raise_for_status()
    return response.json()


def get_recent_evidence_packages(client: httpx.Client, limit: int = 30) -> list[dict]:
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/product_evidence_packages",
        params={
            "select": "*,refrigerator_products(id,brand,equipment_model,product_type,product_image_url,updated_at,last_enriched_at,last_discovered_at,data_status,data_confidence,door_count,door_layout,door_positions,manufacturer,lifecycle_status)",
            "order": "updated_at.desc",
            "limit": str(limit),
        },
        headers=supabase_headers(),
    )
    if response.status_code in {404, 406}:
        return []
    response.raise_for_status()
    return response.json()


def find_brand_alias(client: httpx.Client, raw_value: str) -> str | None:
    normalized = normalize_alias(raw_value)
    if not normalized:
        return None
    try:
        response = client.get(
            f"{SUPABASE_URL}/rest/v1/brand_aliases",
            params={
                "select": "canonical_brand,confidence_score",
                "alias_normalized": f"eq.{normalized}",
                "is_active": "eq.true",
                "order": "confidence_score.desc",
                "limit": "1",
            },
            headers=supabase_headers(),
        )
        if response.status_code >= 400:
            return None
        rows = response.json()
        if rows:
            return rows[0].get("canonical_brand")
    except Exception:
        return None
    return None


def admin_product_query_parts(raw_query: str, client: httpx.Client | None = None) -> tuple[str, dict[str, str], list[str]]:
    filters: dict[str, str] = {}
    search_terms: list[str] = []
    applied: list[str] = []
    alias_brand = find_brand_alias(client, raw_query) if client else None
    if alias_brand and alias_brand.lower() != (raw_query or "").strip().lower():
        filters["brand"] = f"eq.{alias_brand}"
        applied.append(f"品牌别名：{raw_query} → {alias_brand}")
        return "", filters, applied
    for token in re.split(r"\s+", (raw_query or "").strip()):
        if not token:
            continue
        if token in {"缺图片", "无图片", "没有图片"}:
            filters["product_image_url"] = "is.null"
            applied.append("缺图片")
            continue
        if token in {"有图片", "已有图片"}:
            filters["product_image_url"] = "not.is.null"
            applied.append("有图片")
            continue
        if token.lower() in {"commercial", "商用", "商用冰箱", "商用制冷"}:
            filters["market_category"] = "eq.commercial"
            applied.append("商用")
            continue
        if token.lower() in {"residential", "民用", "家用", "家用冰箱"}:
            filters["market_category"] = "eq.residential"
            applied.append("家用")
            continue
        if token.lower() in {"unknown", "未知", "未分类"}:
            filters["market_category"] = "eq.unknown"
            applied.append("未分类")
            continue
        sector_map = {
            "restaurant": "restaurant",
            "饭店": "restaurant",
            "餐厅": "restaurant",
            "restaurant_kitchen": "restaurant",
            "supermarket": "supermarket",
            "商超": "supermarket",
            "超市": "supermarket",
            "medical": "medical",
            "医疗": "medical",
            "bar": "bar",
            "酒吧": "bar",
        }
        lower_token = token.lower()
        if lower_token in sector_map:
            filters["commercial_sector"] = f"eq.{sector_map[lower_token]}"
            applied.append(f"行业：{sector_map[lower_token]}")
            continue
        category_map = {
            "refrigerator": "refrigerator",
            "冷藏": "refrigerator",
            "冷藏柜": "refrigerator",
            "freezer": "freezer",
            "冷冻": "freezer",
            "冷冻柜": "freezer",
            "dual": "dual_temp",
            "dual_temp": "dual_temp",
            "两用": "dual_temp",
            "冷藏冷冻": "dual_temp",
            "display": "display_case",
            "display_case": "display_case",
            "展示": "display_case",
            "展示柜": "display_case",
            "prep_table": "prep_table",
            "备餐台": "prep_table",
            "bar_cooler": "bar_cooler",
            "吧台柜": "bar_cooler",
            "walk_in": "walk_in",
            "步入式": "walk_in",
            "ice": "ice_machine",
            "制冰": "ice_machine",
            "制冰机": "ice_machine",
        }
        if lower_token in category_map:
            filters["equipment_category"] = f"eq.{category_map[lower_token]}"
            applied.append(f"设备：{category_map[lower_token]}")
            continue
        door_match = re.fullmatch(r"(\d{1,2})门", token)
        if door_match:
            filters["door_count"] = f"eq.{door_match.group(1)}"
            applied.append(token)
            continue
        if token in {"缺资料", "资料完整"}:
            applied.append(f"{token}（当前页辅助筛选）")
            continue
        search_terms.append(token)
    search_text = " ".join(search_terms).strip()
    if search_text:
        applied.append(f"关键词：{search_text}")
    return search_text, filters, applied


def get_admin_products_page(client: httpx.Client, raw_query: str = "", page_num: int = 1, per_page: int = 50) -> dict:
    per_page = max(10, min(100, per_page or 50))
    page_num = max(1, page_num or 1)
    offset = (page_num - 1) * per_page
    search_text, filters, applied = admin_product_query_parts(raw_query, client)
    params = {
        "select": "id,brand,equipment_model,product_type,product_image_url,updated_at,last_enriched_at,last_discovered_at,data_status,data_confidence,door_count,door_layout,door_positions,manufacturer,lifecycle_status,market_category,commercial_sector,equipment_category,equipment_form,temperature_application,classification_confidence,classification_source,classified_at",
        "order": "brand.asc.nullslast,equipment_model.asc.nullslast,id.asc",
        "limit": str(per_page),
        "offset": str(offset),
    }
    params.update(filters)
    if search_text:
        safe_search = re.sub(r"[^A-Za-z0-9 ._/-]+", " ", search_text).strip()
        if safe_search:
            params["or"] = (
                f"(brand.ilike.*{safe_search}*,"
                f"equipment_model.ilike.*{safe_search}*,"
                f"product_type.ilike.*{safe_search}*,"
                f"manufacturer.ilike.*{safe_search}*)"
            )
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/refrigerator_products",
        params=params,
        headers=supabase_headers("count=exact"),
    )
    response.raise_for_status()
    total = parse_content_range_total(response.headers.get("content-range"))
    total_pages = max(1, (total + per_page - 1) // per_page) if total else 1
    return {
        "rows": response.json(),
        "total": total,
        "page": page_num,
        "per_page": per_page,
        "total_pages": total_pages,
        "query": raw_query or "",
        "applied": applied,
    }


def admin_nav(active: str = "orders") -> str:
    items = [
        ("orders", "订单列表", "/ADMIN"),
        ("products", "产品数据库", "/ADMIN?view=products"),
        ("gasket_catalog", "密封条数据库", "/ADMIN?view=gasket_catalog"),
        ("product_gaskets", "关联数据库", "/ADMIN?view=product_gaskets"),
    ]
    links = [
        f"""<a class="button {'active' if key == active else ''}" href="{href}">{label}</a>"""
        for key, label, href in items
    ]
    return f"""<div class="admin-actions admin-nav-row"><div class="admin-nav-left">{''.join(links)}</div><a class="button logout" href="/ADMIN/logout">退出登录</a></div>"""


def admin_page_bounds(page_num: int, per_page: int) -> tuple[int, int, int]:
    per_page = max(10, min(100, per_page or 50))
    page_num = max(1, page_num or 1)
    offset = (page_num - 1) * per_page
    return page_num, per_page, offset


def admin_pagination_html(base_view: str, query_text: str, page_num: int, per_page: int, total: int, total_pages: int, applied_text: str) -> str:
    shown_from = (page_num - 1) * per_page + 1 if total else 0
    shown_to = min(total, page_num * per_page)
    full_page_capacity = per_page * total_pages if total else 0
    last_page_count = total - per_page * (total_pages - 1) if total else 0

    def page_url(target_page: int, target_per_page: int | None = None) -> str:
        params = {
            "view": base_view,
            "q": query_text,
            "page": str(max(1, target_page)),
            "per_page": str(target_per_page or per_page),
        }
        return "/ADMIN?" + urlencode(params)

    prev_link = page_url(page_num - 1) if page_num > 1 else ""
    next_link = page_url(page_num + 1) if page_num < total_pages else ""
    if total_pages <= 9:
        page_numbers = list(range(1, total_pages + 1))
    else:
        page_numbers = sorted({1, 2, max(1, page_num - 1), page_num, min(total_pages, page_num + 1), total_pages - 1, total_pages})
    page_links = []
    previous_number = 0
    for number in page_numbers:
        if previous_number and number - previous_number > 1:
            page_links.append("<span class='admin-page-gap'>...</span>")
        if number == page_num:
            page_links.append(f"<span class='admin-page-link active'>{number}</span>")
        else:
            page_links.append(f"<a class='admin-page-link' href='{esc(page_url(number))}'>{number}</a>")
        previous_number = number
    return f"""
<div class="admin-pagination">
<div class="admin-result-summary">
<strong>{esc(applied_text)}</strong>：共 <strong>{esc(total)}</strong> 条；每页 <strong>{esc(per_page)}</strong> 条；第 <strong>{esc(page_num)}</strong> / <strong>{esc(total_pages)}</strong> 页；本页显示 <strong>{esc(shown_from)}-{esc(shown_to)}</strong> 条。<span class="muted">分页核对：{esc(per_page)} × {esc(total_pages)} = {esc(full_page_capacity)} 个位置；最后一页实际 {esc(last_page_count)} 条。</span>
</div>
<div class="admin-page-controls">
{f"<a class='admin-page-link' href='{esc(prev_link)}'>上一页</a>" if prev_link else "<span class='admin-page-link disabled'>上一页</span>"}
{''.join(page_links)}
{f"<a class='admin-page-link' href='{esc(next_link)}'>下一页</a>" if next_link else "<span class='admin-page-link disabled'>下一页</span>"}
</div>
</div>"""


def get_admin_gasket_catalog_page(client: httpx.Client, raw_query: str = "", page_num: int = 1, per_page: int = 50) -> dict:
    page_num, per_page, offset = admin_page_bounds(page_num, per_page)
    query_text = (raw_query or "").strip()
    params = {
        "select": "id,primary_part_number,universal_part_numbers,part_name,gasket_color,install_type,profile_type,profile_name,profile_family,width_in,height_in,perimeter_in,dimensions_text,compatible_brands,compatible_equipment_models,compatible_door_positions,cross_check_score,confidence_score,data_status,is_verified,source_name,source_url,updated_at",
        "order": "updated_at.desc.nullslast,id.desc",
        "limit": str(per_page),
        "offset": str(offset),
    }
    if query_text:
        safe_search = re.sub(r"[^A-Za-z0-9 ._/-]+", " ", query_text).strip()
        if safe_search:
            params["or"] = (
                f"(primary_part_number.ilike.*{safe_search}*,"
                f"part_name.ilike.*{safe_search}*,"
                f"profile_type.ilike.*{safe_search}*,"
                f"profile_name.ilike.*{safe_search}*,"
                f"profile_family.ilike.*{safe_search}*,"
                f"gasket_color.ilike.*{safe_search}*,"
                f"install_type.ilike.*{safe_search}*,"
                f"dimensions_text.ilike.*{safe_search}*)"
            )
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/gasket_catalog",
        params=params,
        headers=supabase_headers("count=exact"),
    )
    response.raise_for_status()
    total = parse_content_range_total(response.headers.get("content-range"))
    total_pages = max(1, (total + per_page - 1) // per_page) if total else 1
    return {
        "rows": response.json(),
        "total": total,
        "page": page_num,
        "per_page": per_page,
        "total_pages": total_pages,
        "query": query_text,
        "applied": [f"关键词：{query_text}"] if query_text else ["全部密封条数据库"],
    }


def get_admin_product_gaskets_page(client: httpx.Client, raw_query: str = "", page_num: int = 1, per_page: int = 50) -> dict:
    page_num, per_page, offset = admin_page_bounds(page_num, per_page)
    query_text = (raw_query or "").strip()
    params = {
        "select": "*,refrigerator_products(id,brand,equipment_model,product_type,door_count,door_layout),gasket_catalog(id,primary_part_number,profile_name,profile_type,color,gasket_color)",
        "order": "updated_at.desc.nullslast,id.desc",
        "limit": str(per_page),
        "offset": str(offset),
    }
    if query_text:
        safe_search = re.sub(r"[^A-Za-z0-9 ._/-]+", " ", query_text).strip()
        if safe_search:
            params["or"] = (
                f"(door_position.ilike.*{safe_search}*,"
                f"door_position_display.ilike.*{safe_search}*,"
                f"part_number.ilike.*{safe_search}*,"
                f"universal_part_number.ilike.*{safe_search}*,"
                f"gasket_name.ilike.*{safe_search}*,"
                f"gasket_profile.ilike.*{safe_search}*,"
                f"gasket_color.ilike.*{safe_search}*,"
                f"dimensions_text.ilike.*{safe_search}*)"
            )
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/refrigerator_product_gaskets",
        params=params,
        headers=supabase_headers("count=exact"),
    )
    response.raise_for_status()
    total = parse_content_range_total(response.headers.get("content-range"))
    total_pages = max(1, (total + per_page - 1) // per_page) if total else 1
    return {
        "rows": response.json(),
        "total": total,
        "page": page_num,
        "per_page": per_page,
        "total_pages": total_pages,
        "query": query_text,
        "applied": [f"关键词：{query_text}"] if query_text else ["全部关联数据库"],
    }


def get_gasket_catalog_record(client: httpx.Client, catalog_id: int) -> dict | None:
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/gasket_catalog",
        params={"select": "*", "id": f"eq.{catalog_id}", "limit": "1"},
        headers=supabase_headers(),
    )
    response.raise_for_status()
    rows = response.json()
    return rows[0] if rows else None


def get_product_gasket_record(client: httpx.Client, record_id: int) -> dict | None:
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/refrigerator_product_gaskets",
        params={
            "select": "*,refrigerator_products(*),gasket_catalog(*)",
            "id": f"eq.{record_id}",
            "limit": "1",
        },
        headers=supabase_headers(),
    )
    response.raise_for_status()
    rows = response.json()
    return rows[0] if rows else None


def get_catalog_applications(client: httpx.Client, catalog_id: int, limit: int = 50) -> list[dict]:
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/refrigerator_product_gaskets",
        params={
            "select": "id,door_position,door_position_display,part_number,dimensions_text,confidence_score,fit_status,refrigerator_products(id,brand,equipment_model)",
            "gasket_catalog_id": f"eq.{catalog_id}",
            "order": "confidence_score.desc.nullslast,id.desc",
            "limit": str(limit),
        },
        headers=supabase_headers(),
    )
    response.raise_for_status()
    return response.json()


def save_inferred_door_layout(client: httpx.Client, product: dict, positions: list[dict]) -> None:
    if not positions:
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


def get_latest_request_for_product(client: httpx.Client, product_id: int) -> dict | None:
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/gasket_requests",
        params={
            "select": "*",
            "matched_refrigerator_product_id": f"eq.{product_id}",
            "order": "created_at.desc",
            "limit": "1",
        },
        headers=supabase_headers(),
    )
    if response.status_code in {404, 406}:
        return None
    response.raise_for_status()
    rows = response.json()
    return rows[0] if rows else None


def get_latest_request_for_checkout(client: httpx.Client, product_id: int, customer_email: str | None = None) -> dict | None:
    params = {
        "select": "*",
        "matched_refrigerator_product_id": f"eq.{product_id}",
        "order": "created_at.desc",
        "limit": "1",
    }
    email = (customer_email or "").strip()
    if email:
        params["customer_email"] = f"eq.{email}"
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/gasket_requests",
        params=params,
        headers=supabase_headers(),
    )
    if response.status_code in {404, 406}:
        return None
    response.raise_for_status()
    rows = response.json()
    if rows:
        return rows[0]
    if email:
        return get_latest_request_for_product(client, product_id)
    return None


def customer_order_quote_snapshot(item: dict) -> dict:
    return {
        "door_position": item.get("door_position"),
        "door_position_display": item.get("door_position_display"),
        "part_number": item.get("part_number"),
        "universal_part_number": item.get("universal_part_number"),
        "dimensions_text": item.get("dimensions_text"),
        "width_in": item.get("width_in"),
        "height_in": item.get("height_in"),
        "perimeter_in": item.get("perimeter_in"),
        "color": item.get("color"),
        "gasket_type": item.get("gasket_type"),
        "profile_type": item.get("profile_type"),
        "gasket_image_url": item.get("gasket_image_url"),
        "cross_section_image_url": item.get("cross_section_image_url"),
        "confidence_score": item.get("confidence_score"),
        "source_name": item.get("source_name"),
        "source_url": item.get("source_url"),
        "final_price_usd": item.get("final_price_usd"),
        "base_price_usd": item.get("base_price_usd"),
        "customer_size": customer_gasket_size(item),
    }


def customer_order_product_snapshot(product: dict) -> dict:
    keys = [
        "id",
        "brand",
        "equipment_model",
        "manufacturer",
        "product_type",
        "door_count",
        "door_layout",
        "door_positions",
        "product_image_url",
        "product_image_source_url",
        "product_image_confidence",
        "lifecycle_status",
        "data_status",
        "data_confidence",
        "data_source_summary",
        "official_product_url",
        "manual_url",
        "spec_sheet_url",
        "updated_at",
        "last_enriched_at",
    ]
    return {key: product.get(key) for key in keys}


def create_customer_order_record(
    client: httpx.Client,
    product: dict,
    quote_items: list[dict],
    customer_info: dict,
    checkout_url: str,
) -> dict | None:
    request = get_latest_request_for_checkout(client, int(product["id"]), customer_info.get("customer_email"))
    quote_snapshot = [customer_order_quote_snapshot(item) for item in quote_items]
    subtotal = sum(float(item.get("final_price_usd") or item.get("base_price_usd") or 0) for item in quote_items)
    payload = {
        "order_status": "checkout_created",
        "payment_status": "pending",
        "fulfillment_status": "not_started",
        "checkout_provider": "shopify",
        "checkout_url": checkout_url,
        "customer_name": customer_info.get("customer_name"),
        "customer_email": customer_info.get("customer_email"),
        "customer_phone": customer_info.get("customer_phone"),
        "shipping_address": {
            "address1": customer_info.get("shipping_address1"),
            "address2": customer_info.get("shipping_address2"),
            "city": customer_info.get("shipping_city"),
            "state": customer_info.get("shipping_state"),
            "zip": customer_info.get("shipping_zip"),
            "country": customer_info.get("shipping_country") or "United States",
        },
        "refrigerator_product_id": product.get("id"),
        "gasket_request_id": request.get("id") if request else None,
        "brand": product.get("brand"),
        "equipment_model": product.get("equipment_model"),
        "nameplate_image_url": request.get("nameplate_image_url") if request else None,
        "selected_door_positions": [str(item.get("door_position") or "") for item in quote_items if item.get("door_position")],
        "quote_items": quote_snapshot,
        "product_snapshot": customer_order_product_snapshot(product),
        "subtotal_usd": round(subtotal, 2),
        "currency": "USD",
    }
    response = client.post(
        f"{SUPABASE_URL}/rest/v1/customer_orders",
        headers=supabase_headers("return=representation"),
        json=payload,
    )
    if response.status_code in {404, 406}:
        print("customer_orders table is not available; checkout continued without internal order record", flush=True)
        return None
    response.raise_for_status()
    rows = response.json()
    return rows[0] if rows else None


def pdf_safe(value) -> str:
    text = "" if value is None else str(value)
    return text.encode("latin-1", "replace").decode("latin-1")


def pdf_escape(value) -> str:
    return pdf_safe(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def wrap_pdf_text(value, width: int = 92) -> list[str]:
    text = re.sub(r"\s+", " ", pdf_safe(value)).strip()
    if not text:
        return [""]
    words = text.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            if current:
                lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


def build_pdf(lines: list[str]) -> bytes:
    max_lines = 46
    pages = [lines[index:index + max_lines] for index in range(0, len(lines), max_lines)] or [[]]
    objects: list[bytes] = []

    def add_object(data: bytes) -> int:
        objects.append(data)
        return len(objects)

    catalog_id = add_object(b"<< /Type /Catalog /Pages 2 0 R >>")
    pages_id = add_object(b"")
    font_id = add_object(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids: list[int] = []
    for page_lines in pages:
        content_lines = ["BT", "/F1 10 Tf", "48 760 Td", "14 TL"]
        for line in page_lines:
            content_lines.append(f"({pdf_escape(line)}) Tj")
            content_lines.append("T*")
        content_lines.append("ET")
        content = "\n".join(content_lines).encode("latin-1", "replace")
        content_id = add_object(
            f"<< /Length {len(content)} >>\nstream\n".encode("latin-1") + content + b"\nendstream"
        )
        page_id = add_object(
            f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>".encode("latin-1")
        )
        page_ids.append(page_id)
    objects[pages_id - 1] = f"<< /Type /Pages /Kids [{' '.join(f'{page_id} 0 R' for page_id in page_ids)}] /Count {len(page_ids)} >>".encode("latin-1")

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode("latin-1"))
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref_pos = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("latin-1"))
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("latin-1"))
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("latin-1")
    )
    return bytes(output)


def render_quote_pdf(product: dict, quote_items: list[dict], request: dict | None) -> bytes:
    nameplate_data = (request or {}).get("nameplate_data") or {}
    if not nameplate_data and request:
        nameplate_data = {
            "brand": request.get("detected_brand"),
            "model": request.get("detected_model"),
            "raw_text": request.get("ocr_text"),
        }
    positions = infer_door_positions(product)
    lines = [
        "Refrigerator Door Gasket Quote",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "Refrigerator",
        f"Brand: {product.get('brand') or ''}",
        f"Model: {product.get('equipment_model') or ''}",
        f"Product type: {product.get('product_type') or ''}",
        f"Door count: {product.get('door_count') or len(positions) or ''}",
        f"Door layout: {', '.join(item.get('label') or item.get('key') or '' for item in positions)}",
        f"Status: {product.get('lifecycle_status') or product.get('data_status') or ''}",
        f"Product image: {product.get('product_image_url') or 'Loading'}",
        "",
        "Nameplate",
        f"OpenAI brand: {nameplate_data.get('brand') or product.get('brand') or ''}",
        f"OpenAI model: {nameplate_data.get('model') or product.get('equipment_model') or ''}",
        f"Serial: {nameplate_data.get('serial_number') or ''}",
        f"Manufacturer: {nameplate_data.get('manufacturer') or product.get('manufacturer') or ''}",
        f"Voltage: {nameplate_data.get('voltage') or ''}",
        f"Refrigerant: {nameplate_data.get('refrigerant') or ''}",
        "",
        "Gasket options",
    ]
    if quote_items:
        total = 0.0
        for item in quote_items:
            price = float(item.get("final_price_usd") or 0)
            total += price
            lines.extend(
                [
                    "",
                    f"Door: {item.get('door_position_display') or item.get('door_position') or ''}",
                    f"Part number: {item.get('part_number') or item.get('universal_part_number') or ''}",
                    f"Size: {customer_gasket_size(item)}",
                    f"Color: {item.get('color') or ''}",
                    f"Install type: {item.get('gasket_type') or item.get('install_type') or ''}",
                    f"Confidence: {item.get('confidence_score') or ''}%",
                    f"Price: {money(price)}",
                ]
            )
        lines.extend(["", f"Selected-all total: {money(total)}"])
    else:
        lines.append("Gasket records are still loading for this model.")
    return build_pdf([line for raw in lines for line in wrap_pdf_text(raw)])


def pdf_filename(product: dict) -> str:
    name = f"gasket-quote-{product.get('brand') or ''}-{product.get('equipment_model') or ''}.pdf"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "gasket-quote.pdf"


def page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<style>
body{{margin:0;font-family:Arial,Helvetica,sans-serif;background:#eef3f6;color:#17202a}}
:root{{--page-max:1180px;--page-pad:22px;--page-shell:1224px}}
*,*::before,*::after{{box-sizing:border-box}}
.app-header{{max-width:var(--page-shell);margin:0 auto;padding:14px var(--page-pad) 0}}
.app-header-inner{{background:white;border:1px solid #dbe2ea;border-radius:8px;padding:10px 14px;display:flex;align-items:center;gap:10px;box-shadow:0 8px 22px rgba(15,29,36,.05)}}
.app-logo{{width:30px;height:30px;border-radius:7px;background:#0a6f78;color:white;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:900;letter-spacing:.02em;text-decoration:none;flex:0 0 auto}}
.app-logo:hover{{background:#075e66;color:white}}
.app-title{{font-size:15px;font-weight:800;color:#17202a;line-height:1.1}}
.app-subtitle{{font-size:11px;color:#687385;margin-top:2px}}
main{{max-width:var(--page-shell);margin:0 auto;padding:22px var(--page-pad)}}
.app-footer{{max-width:var(--page-shell);margin:0 auto;padding:0 var(--page-pad) 24px}}
.app-footer-inner{{background:white;border:1px solid #dbe2ea;border-radius:8px;padding:20px;color:#17202a}}
.app-footer-inner strong{{display:block;font-size:18px;margin-bottom:4px}}
section,.checkout{{background:white;border:1px solid #dbe2ea;border-radius:8px;padding:20px;margin-bottom:18px}}
h1{{font-size:34px;margin:0 0 8px}} h2{{font-size:20px;margin:0 0 14px}} p{{color:#687385;line-height:1.55}}
.hero,.result-grid{{display:grid;grid-template-columns:1fr 1fr;gap:22px}} .grid,.summary{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}
.upload-row{{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;align-items:end}}
label{{display:block;font-size:13px;color:#687385;margin-bottom:6px}} input{{width:100%;border:1px solid #dbe2ea;border-radius:6px;padding:10px}}
button,.button{{border:0;border-radius:6px;background:#0a6f78;color:white;min-height:40px;padding:0 16px;font-weight:700;text-decoration:none;display:inline-flex;align-items:center}}
.admin-actions{{display:flex;align-items:center;gap:14px 18px;flex-wrap:wrap;width:100%;margin-bottom:18px}}
.admin-nav-left{{display:flex;align-items:center;gap:12px;flex-wrap:wrap}}
.admin-actions .logout{{margin-left:auto}}
.admin-actions .active{{background:#0d1f2a}}
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
</style></head><body><header class="app-header"><div class="app-header-inner"><a class="app-logo" href="/" aria-label="Home">GM</a><div><div class="app-title">{esc(title)}</div><div class="app-subtitle">冰箱门封条识别与订单系统</div></div></div></header><main>{body}</main><footer class="app-footer"><div class="app-footer-inner"><strong>Ready to order?</strong><span class="muted">Select the gasket solution for this refrigerator.</span></div></footer>
<script>function updateTotal(){{let t=0,c=0;document.querySelectorAll('[data-price]').forEach(b=>{{if(b.checked){{t+=Number(b.dataset.price||0);c++}}}});let a=document.getElementById('selected-total'),n=document.getElementById('selected-count');if(a)a.textContent='$'+t.toFixed(2);if(n)n.textContent=c}}function fmt(s){{let m=Math.floor(s/60),r=s%60;return String(m).padStart(2,'0')+':'+String(r).padStart(2,'0')}}function startLoadingTimers(){{let start=Date.now();setInterval(()=>{{let s=Math.floor((Date.now()-start)/1000);document.querySelectorAll('[data-loading-label]').forEach(el=>{{el.textContent=el.getAttribute('data-loading-label')+' '+fmt(s)}})}},1000)}}function startUploadFeedback(){{let f=document.getElementById('upload');if(!f)return;f.addEventListener('submit',()=>{{let b=f.querySelector('button[type=\"submit\"]');if(b){{b.disabled=true;b.textContent='Reading nameplate...'}}let n=document.createElement('div');n.className='upload-working';n.innerHTML='<strong>Reading nameplate</strong><br><span>AI is extracting the refrigerator model. This usually takes a few seconds.</span>';f.appendChild(n)}})}}function initImageViewer(){{let viewer=document.getElementById('image-viewer'),img=document.getElementById('image-viewer-img');if(!viewer||!img)return;let scale=1,x=0,y=0,drag=false,sx=0,sy=0;function apply(){{img.style.transform='translate('+x+'px,'+y+'px) scale('+scale+')'}}document.querySelectorAll('[data-image-viewer-src]').forEach(btn=>btn.addEventListener('click',()=>{{img.src=btn.getAttribute('data-image-viewer-src');scale=1;x=0;y=0;apply();viewer.classList.add('is-open');viewer.setAttribute('aria-hidden','false')}}));viewer.querySelector('[data-close-viewer]')?.addEventListener('click',()=>{{viewer.classList.remove('is-open');viewer.setAttribute('aria-hidden','true')}});viewer.querySelector('[data-zoom=\"in\"]')?.addEventListener('click',()=>{{scale=Math.min(5,scale+.25);apply()}});viewer.querySelector('[data-zoom=\"out\"]')?.addEventListener('click',()=>{{scale=Math.max(.5,scale-.25);apply()}});viewer.querySelector('.image-viewer-stage')?.addEventListener('pointerdown',e=>{{drag=true;sx=e.clientX-x;sy=e.clientY-y}});window.addEventListener('pointermove',e=>{{if(!drag)return;x=e.clientX-sx;y=e.clientY-sy;apply()}});window.addEventListener('pointerup',()=>drag=false);window.addEventListener('keydown',e=>{{if(e.key==='Escape')viewer.classList.remove('is-open')}})}}document.addEventListener('change',updateTotal);window.addEventListener('load',updateTotal);window.addEventListener('load',startLoadingTimers);window.addEventListener('load',startUploadFeedback);window.addEventListener('load',initImageViewer);function pollProductStatus(){{let el=document.querySelector('[data-refresh-product]');if(!el)return;let id=el.getAttribute('data-refresh-product');let wantsImage=el.getAttribute('data-needs-image')==='1';let wantsGasket=el.getAttribute('data-needs-gasket')==='1';if(!wantsImage&&!wantsGasket)return;setInterval(async()=>{{try{{let r=await fetch('/product-status?product_id='+encodeURIComponent(id),{{cache:'no-store'}});let d=await r.json();if((wantsImage&&d.product_image_url)||(wantsGasket&&d.quote_item_count>0))window.location.reload();}}catch(e){{}}}},2000)}}window.addEventListener('load',pollProductStatus)</script>
<script src="https://crm-8t7y.onrender.com/chat/widget.js"></script>
</body></html>""".encode("utf-8")


def render_home(message: str = "", stats: dict | None = None) -> bytes:
    warning = f"<p style='color:#9f4b12'>{esc(message)}</p>" if message else ""
    stats = stats or {}
    stats_html = f"""
<section><h2>Fit database coverage</h2>
<div class="summary">
<div class="metric"><span>Refrigerator models</span><strong data-public-stat="product_total">{esc(stats.get('product_total') or '...')}</strong></div>
<div class="metric"><span>Door gasket records</span><strong data-public-stat="quote_items">{esc(stats.get('quote_items') or '...')}</strong></div>
<div class="metric"><span>Known profile references</span><strong data-public-stat="known_profiles">{esc(stats.get('known_profiles') or '...')}</strong></div>
</div>
<p class="muted">Our matching database grows from real nameplate searches, product records, gasket dimensions, profile references, and confirmed order history.</p>
</section>
<script>
fetch('/public-stats', {{cache:'no-store'}}).then(r=>r.json()).then(data=>{{
  document.querySelectorAll('[data-public-stat]').forEach(el=>{{
    const key=el.getAttribute('data-public-stat');
    if(data && data[key] !== undefined && data[key] !== null) el.textContent=data[key];
  }});
}}).catch(()=>{{}});
</script>"""
    return page("Gasket Match", f"""
<section><form id="upload" method="post" action="/read-nameplate" enctype="multipart/form-data"><h2>Upload nameplate</h2>{warning}
<div class="upload-row"><div><label>Nameplate photo</label><input type="file" name="nameplate" accept="image/*"></div><button type="submit">Read nameplate</button></div>
<div class="grid"><div><label>Brand fallback</label><input name="brand"></div><div><label>Model fallback</label><input name="equipment_model"></div></div>
<p class="muted">You can correct the brand or model before matching the database.</p></form></section>{stats_html}""")


def render_confirm_nameplate(upload_url: str, customer: dict, nameplate_data: dict, fallback_brand: str = "", fallback_model: str = "") -> bytes:
    brand = nameplate_data.get("brand") or fallback_brand
    model = nameplate_data.get("model") or fallback_model
    raw_text = nameplate_data.get("raw_text") or ""
    recognition_notice = ""
    if nameplate_data.get("recognition_error"):
        recognition_notice = """
<div class="model-check-notice">
<strong>Please type the brand and model from the nameplate.</strong>
Automatic reading is unavailable right now. The uploaded photo is saved; enter the exact brand and model, then continue matching.
</div>"""
    model_notice = """
<div class="model-check-notice">
<strong>Important: confirm the model number exactly.</strong>
AI can misread characters such as 8/S, 1/I, 0/O. The red model box must match the nameplate before you continue.
</div>"""
    return page("Confirm Nameplate", f"""
<section><h2>Confirm nameplate information</h2>
<p>Check the uploaded nameplate against the information below. If anything is wrong, edit it before matching the database.</p>{recognition_notice}
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
        dims = customer_gasket_size(item)
        rows.append(f"""<label class="item"><input type="checkbox" name="door_position" value="{esc(door_key)}" data-price="{line_price}" {checked}>{image_html}<div><strong>{esc(door_label)} Gasket</strong><p>{esc(dims)}</p></div><div class="price"><strong>{money(line_price)}</strong><small>each selected door</small></div><div></div></label>""")
    summary_html = "" if pending_new_product else f"""<div class="summary"><div class="metric"><span>Required gaskets</span><strong>{quantity}</strong></div><div class="metric"><span>Selected</span><strong id="selected-count">0</strong></div><div class="metric"><span>Total</span><strong id="selected-total">$0.00</strong></div></div>"""
    return page("Matched Gasket Quote", f"""
<div data-refresh-product="{esc(product['id'])}" data-needs-image="{1 if needs_image else 0}" data-needs-gasket="{1 if needs_gasket else 0}" hidden></div>
{loading_banner}<section><h2>Matched refrigerator</h2><div class="result-grid"><div><h3>Refrigerator image</h3>{product_html}</div><div><h3>Nameplate</h3>{plate_html}</div><div><h3>Nameplate summary</h3><div class="facts"><div>OpenAI brand</div><div><strong>{esc(nameplate_data.get('brand') or product.get('brand'))}</strong></div><div>OpenAI model</div><div><strong>{esc(nameplate_data.get('model') or product.get('equipment_model'))}</strong></div><div>Serial</div><div>{esc(nameplate_data.get('serial_number') or 'Not found')}</div><div>Brand</div><div><strong>{esc(product.get('brand'))}</strong></div><div>Model</div><div><strong>{esc(product.get('equipment_model'))}</strong></div></div></div></div></section>
<section><h2>Gasket quote</h2><form method="post" action="/checkout"><input type="hidden" name="product_id" value="{esc(product['id'])}">{summary_html}<div>{''.join(rows) if rows else '<p class="muted">No quote items yet.</p>'}</div><p><button type="submit">Checkout selected gaskets</button></p></form></section>""")



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
    item_by_key = {str(item.get("door_position") or ""): item for item in quote_items}
    used_item_ids: set[str] = set()
    if pending_new_product and not quote_items:
        rows.append(f"""<div class="item"><input type="checkbox" disabled><div class="loading" style="width:98px;height:78px;border:1px solid #dbe2ea;border-radius:6px"><span data-loading-label="{gasket_loading}">{gasket_loading} 00:00</span></div><div><strong>{gasket_loading}</strong></div><div class="price"><strong>Loading</strong></div><div></div></div>""")

    for index, position in enumerate(positions or door_positions_for_count(quantity), start=1):
        door_label = position.get("label") or f"Door {index}"
        door_key = position.get("key") or f"door_{index}"
        item = item_by_key.get(str(door_key))
        if not item and len(quote_items) == len(positions or []):
            item = quote_items[index - 1]
        if not item:
            rows.append(f"""<div class="item"><input type="checkbox" disabled><div class="loading" style="width:98px;height:78px;border:1px solid #dbe2ea;border-radius:6px"><span data-loading-label="{gasket_loading}">{gasket_loading} 00:00</span></div><div><strong>{esc(door_label)} Gasket</strong></div><div class="price"><strong>Loading</strong></div><div><small class="muted">Door</small><br><strong>{esc(door_key)}</strong></div></div>""")
            continue
        used_item_ids.add(str(item.get("id") or id(item)))
        price = float(item.get("final_price_usd") or 0)
        line_price = price
        image = item.get("gasket_image_url")
        image_html = f"<img src='{esc(image)}' alt='Gasket image'>" if image else "<div class='muted'>No gasket image</div>"
        dims = customer_gasket_size(item)
        rows.append(f"""<label class="item"><input type="checkbox" name="door_position" value="{esc(door_key)}" data-price="{line_price}" checked>{image_html}<div><strong>{esc(door_label)} Gasket</strong><p>{esc(dims)}</p></div><div class="price"><strong>{money(line_price)}</strong><small>each selected door</small></div><div></div></label>""")
    for item in quote_items:
        item_id = str(item.get("id") or id(item))
        if item_id in used_item_ids:
            continue
        door_key = str(item.get("door_position") or f"gasket_{item_id}")
        door_label = item.get("door_position_display") or door_key
        price = float(item.get("final_price_usd") or 0)
        image = item.get("gasket_image_url")
        image_html = f"<img src='{esc(image)}' alt='Gasket image'>" if image else "<div class='muted'>No gasket image</div>"
        rows.append(f"""<label class="item"><input type="checkbox" name="door_position" value="{esc(door_key)}" data-price="{price}" checked>{image_html}<div><strong>{esc(door_label)} Gasket</strong><p>{esc(customer_gasket_size(item))}</p></div><div class="price"><strong>{money(price)}</strong><small>each selected door</small></div><div></div></label>""")

    summary_html = "" if pending_new_product else f"""<div class="summary"><div class="metric"><span>Required gaskets</span><strong>{quantity}</strong></div><div class="metric"><span>Selected</span><strong id="selected-count">0</strong></div><div class="metric"><span>Total</span><strong id="selected-total">$0.00</strong></div></div>"""
    rows_html = "".join(rows) if rows else f"""<div class="item"><input type="checkbox" disabled><div class="loading" style="width:98px;height:78px;border:1px solid #dbe2ea;border-radius:6px"><span data-loading-label="{gasket_loading}">{gasket_loading} 00:00</span></div><div><strong>{gasket_loading}</strong></div><div class="price"><strong>Loading</strong></div><div></div></div>"""
    return page("Matched Gasket Quote", f"""
<style>
.checkout-actions{{display:flex;justify-content:flex-end;margin-top:18px}}
.shipping-panel{{display:none;margin-top:18px;border:1px solid #dbe2ea;background:#fbfdfe;border-radius:8px;padding:16px}}
.shipping-panel.is-open{{display:block}}
.shipping-panel h3{{margin-top:0}}
.shipping-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}
.shipping-grid .wide{{grid-column:span 2}}
.checkout-error{{display:none;margin-top:12px;border:1px solid #f2b8b5;background:#fff1f0;color:#5f1410;border-radius:8px;padding:12px}}
@media(max-width:760px){{.checkout-actions{{display:block}}.checkout-actions button{{width:100%;justify-content:center}}.shipping-grid{{grid-template-columns:1fr}}.shipping-grid .wide{{grid-column:auto}}}}
</style>
<div data-refresh-product="{esc(product['id'])}" data-needs-image="{1 if needs_image else 0}" data-needs-gasket="{1 if needs_gasket else 0}" hidden></div>
{loading_banner}<section><h2>Matched refrigerator</h2><div class="result-grid"><div><h3>Refrigerator image</h3>{product_html}</div><div><h3>Nameplate</h3>{plate_html}</div><div><h3>Nameplate summary</h3><div class="facts"><div>OpenAI brand</div><div><strong>{esc(nameplate_data.get('brand') or product.get('brand'))}</strong></div><div>OpenAI model</div><div><strong>{esc(nameplate_data.get('model') or product.get('equipment_model'))}</strong></div><div>Serial</div><div>{esc(nameplate_data.get('serial_number') or 'Not found')}</div><div>Brand</div><div><strong>{esc(product.get('brand'))}</strong></div><div>Model</div><div><strong>{esc(product.get('equipment_model'))}</strong></div></div></div></div></section>
<section><h2>Gasket quote</h2><form class="checkout-form" method="post" action="/checkout?product_id={esc(product['id'])}" data-product-id="{esc(product['id'])}"><input type="hidden" name="product_id" value="{esc(product['id'])}">{summary_html}<div>{rows_html}</div><div class="shipping-panel" data-shipping-panel><h3>Shipping information</h3><div class="shipping-grid"><label>Name<input data-required-check name="customer_name" autocomplete="name"></label><label>Phone<input data-required-check name="customer_phone" autocomplete="tel"></label><label>Email<input data-required-check type="email" name="customer_email" autocomplete="email"></label><label class="wide">Shipping address<input data-required-check data-address-autocomplete name="shipping_address1" placeholder="Shipping address" autocomplete="shipping street-address"></label><input type="hidden" name="shipping_address2" data-address-line2><input type="hidden" name="shipping_city" data-address-city><input type="hidden" name="shipping_state" data-address-state><input type="hidden" name="shipping_zip" data-address-zip><input type="hidden" name="shipping_country" value="United States" data-address-country></div></div><div class="checkout-actions"><button type="submit" data-checkout-button>Purchase selected gaskets</button></div><div class="checkout-error" data-checkout-error></div></form></section>
<script>
document.querySelectorAll('.checkout-form').forEach(form=>form.addEventListener('submit',async event=>{{
  if(!window.fetch)return;
  event.preventDefault();
  const button=form.querySelector('button[type="submit"]');
  const shippingPanel=form.querySelector('[data-shipping-panel]');
  const errorBox=form.querySelector('[data-checkout-error]');
  if(errorBox){{errorBox.style.display='none';errorBox.textContent='';}}
  if(shippingPanel&&!shippingPanel.classList.contains('is-open')){{
    shippingPanel.classList.add('is-open');
    if(button)button.textContent='Continue to payment';
    shippingPanel.scrollIntoView({{behavior:'smooth',block:'center'}});
    return;
  }}
  const missing=[...form.querySelectorAll('[data-required-check]')].find(input=>!input.value.trim()||!input.checkValidity());
  if(missing){{
    missing.focus();
    missing.reportValidity?.();
    if(errorBox){{errorBox.textContent='Please complete the shipping information before checkout.';errorBox.style.display='block';}}
    return;
  }}
  if(button){{button.disabled=true;button.textContent='Preparing checkout...';}}
  try{{
    const data=new FormData(form);
    data.set('ajax','1');
    if(!data.get('product_id'))data.set('product_id',form.dataset.productId||'');
    const response=await fetch(form.action,{{method:'POST',body:new URLSearchParams(data),headers:{{'X-Requested-With':'fetch'}}}});
    const payload=await response.json();
    if(!response.ok||!payload.ok)throw new Error(payload.error||'Checkout is not ready.');
    window.location.href=payload.checkout_url;
  }}catch(error){{
    if(errorBox){{errorBox.textContent=error.message;errorBox.style.display='block';}}
  }}finally{{
    if(button){{button.disabled=false;button.textContent='Purchase selected gaskets';}}
  }}
}}));
window.initShippingAutocomplete=function(){{
  if(!window.google?.maps?.places)return;
  document.querySelectorAll('[data-address-autocomplete]').forEach(input=>{{
    const form=input.closest('form');
    const autocomplete=new google.maps.places.Autocomplete(input,{{types:['address'],componentRestrictions:{{country:['us']}},fields:['address_components','formatted_address']}});
    autocomplete.addListener('place_changed',()=>{{
      const place=autocomplete.getPlace();
      const parts={{street_number:'',route:'',subpremise:'',locality:'',administrative_area_level_1:'',postal_code:'',country:''}};
      (place.address_components||[]).forEach(component=>{{
        component.types.forEach(type=>{{if(type in parts)parts[type]=type==='administrative_area_level_1'?component.short_name:component.long_name;}});
      }});
      const street=[parts.street_number,parts.route].filter(Boolean).join(' ');
      const line2=parts.subpremise?('Unit '+parts.subpremise):'';
      input.value=place.formatted_address||[street,line2,parts.locality,parts.administrative_area_level_1,parts.postal_code].filter(Boolean).join(', ');
      form.querySelector('[data-address-line2]').value=line2;
      form.querySelector('[data-address-city]').value=parts.locality;
      form.querySelector('[data-address-state]').value=parts.administrative_area_level_1;
      form.querySelector('[data-address-zip]').value=parts.postal_code;
      form.querySelector('[data-address-country]').value=parts.country||'United States';
    }});
  }});
}};
</script>{google_places_loader()}""")


def render_evidence_package(package: dict) -> str:
    if not package:
        return ""
    missing = package.get("missing_fields") or []
    items = package.get("items") or []
    missing_text = ", ".join([item.get("label") or item.get("field_name") or "" for item in missing[:6]]) or "None"
    rows = []
    for item in sorted(items, key=lambda row: float(row.get("confidence_score") or 0), reverse=True)[:6]:
        rows.append(
            f"""<div class="metric"><span>{esc(item.get('source_name') or item.get('evidence_type') or '资料来源')}</span><strong>{esc(item.get('confidence_score') or 0)}%</strong><p>{esc(item.get('supports_value') or item.get('field_name') or '')}</p></div>"""
        )
    rows_html = "".join(rows) if rows else "<p class='muted'>资料仍在收集中。</p>"
    return f"""
<section><h2>产品资料证据包</h2>
<div class="summary"><div class="metric"><span>状态</span><strong>{esc(zh_status(package.get('status') or 'collecting'))}</strong></div><div class="metric"><span>完整度</span><strong>{esc(package.get('completeness_score') or 0)}%</strong></div><div class="metric"><span>置信度</span><strong>{esc(package.get('overall_confidence') or 0)}%</strong></div></div>
<p class="muted">缺少或仍在补充的资料：{esc(missing_text)}</p>
<div class="grid">{rows_html}</div></section>"""


def compact_json(value) -> str:
    if value in (None, ""):
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except TypeError:
        return str(value)


def short_datetime(value) -> str:
    text = str(value or "")
    return text.replace("T", " ")[:19]


def zh_status(value) -> str:
    mapping = {
        "checkout_created": "已生成付款链接",
        "pending": "待付款",
        "paid": "已付款",
        "not_started": "未开始",
        "in_production": "生产中",
        "ready_to_ship": "待发货",
        "fulfilled": "已完成",
        "cancelled": "已取消",
        "matched": "已匹配",
        "needs_review": "需人工复核",
        "collecting": "资料收集中",
        "complete": "已完成",
        "missing": "缺少资料",
    }
    text = str(value or "")
    return mapping.get(text, text)


def get_recent_customer_orders(client: httpx.Client, limit: int = 100) -> list[dict]:
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/customer_orders",
        params={
            "select": "*",
            "order": "created_at.desc",
            "limit": str(limit),
        },
        headers=supabase_headers(),
    )
    if response.status_code in {404, 406}:
        return []
    response.raise_for_status()
    return response.json()


def get_customer_order(client: httpx.Client, order_id: int) -> dict | None:
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/customer_orders",
        params={"select": "*", "id": f"eq.{order_id}", "limit": "1"},
        headers=supabase_headers(),
    )
    if response.status_code in {404, 406}:
        return None
    response.raise_for_status()
    rows = response.json()
    return rows[0] if rows else None


def parse_content_range_total(value: str | None) -> int:
    match = re.search(r"/(\d+|\*)$", value or "")
    if not match or match.group(1) == "*":
        return 0
    return int(match.group(1))


def supabase_count(client: httpx.Client, table: str, filters: dict[str, str] | None = None, select_field: str = "*") -> int:
    params = {"select": select_field, "limit": "1"}
    if filters:
        params.update(filters)
    try:
        response = client.get(
            f"{SUPABASE_URL}/rest/v1/{table}",
            params=params,
            headers=supabase_headers("count=exact"),
        )
        if response.status_code >= 400:
            return 0
        return parse_content_range_total(response.headers.get("content-range"))
    except Exception:
        return 0


def get_recent_searches(client: httpx.Client, limit: int = 8) -> list[dict]:
    try:
        response = client.get(
            f"{SUPABASE_URL}/rest/v1/gasket_requests",
            params={
                "select": "detected_brand,detected_model,status,created_at,matched_refrigerator_product_id",
                "order": "created_at.desc",
                "limit": str(limit),
            },
            headers=supabase_headers(),
        )
        if response.status_code >= 400:
            return []
        return response.json()
    except Exception:
        return []


def count_known_profiles(client: httpx.Client) -> int:
    profile_values = set()
    try:
        response = client.get(
            f"{SUPABASE_URL}/rest/v1/refrigerator_product_quote_items",
            params={"select": "gasket_profile,profile_image_url,gasket_install_type", "limit": "5000"},
            headers=supabase_headers(),
        )
        if response.status_code < 400:
            for row in response.json():
                for key in ("gasket_profile", "profile_image_url", "gasket_install_type"):
                    value = str(row.get(key) or "").strip().lower()
                    if value and value not in {"unknown", "n/a", "not listed", "not publicly listed"}:
                        profile_values.add(value)
    except Exception:
        pass
    return len(profile_values)


def get_database_stats(client: httpx.Client) -> dict:
    product_total = supabase_count(client, "refrigerator_products")
    product_images = supabase_count(client, "refrigerator_products", {"product_image_url": "not.is.null"})
    commercial_products = supabase_count(client, "refrigerator_products", {"market_category": "eq.commercial"})
    residential_products = supabase_count(client, "refrigerator_products", {"market_category": "eq.residential"})
    unknown_market_products = supabase_count(client, "refrigerator_products", {"market_category": "eq.unknown"})
    quote_items = supabase_count(client, "refrigerator_product_quote_items", select_field="refrigerator_product_id")
    quote_items_with_size = supabase_count(client, "refrigerator_product_quote_items", {"dimensions_text": "not.is.null"}, "refrigerator_product_id")
    trusted_products = supabase_count(client, "refrigerator_products", {"data_confidence": "gte.100"})
    trusted_gaskets = supabase_count(client, "refrigerator_product_quote_items", {"confidence_score": "gte.100"}, "refrigerator_product_id")
    customer_orders = supabase_count(client, "customer_orders")
    gasket_parts = supabase_count(client, "gasket_catalog")
    recent_searches = get_recent_searches(client)
    known_profiles = count_known_profiles(client)
    return {
        "product_total": product_total,
        "product_images": product_images,
        "product_image_rate": round((product_images / product_total * 100), 1) if product_total else 0,
        "commercial_products": commercial_products,
        "residential_products": residential_products,
        "unknown_market_products": unknown_market_products,
        "quote_items": quote_items,
        "quote_items_with_size": quote_items_with_size,
        "quote_size_rate": round((quote_items_with_size / quote_items * 100), 1) if quote_items else 0,
        "trusted_products": trusted_products,
        "trusted_gaskets": trusted_gaskets,
        "customer_orders": customer_orders,
        "gasket_parts": gasket_parts,
        "known_profiles": known_profiles,
        "recent_searches": recent_searches,
    }


def get_home_database_stats(client: httpx.Client) -> dict:
    product_total = supabase_count(client, "refrigerator_products")
    quote_items = supabase_count(client, "refrigerator_product_quote_items", select_field="refrigerator_product_id")
    return {
        "product_total": product_total,
        "quote_items": quote_items,
        "known_profiles": count_known_profiles(client),
    }


def shipping_address_text(order: dict) -> str:
    address = order.get("shipping_address") or {}
    if not isinstance(address, dict):
        return str(address or "")
    line1 = address.get("address1") or ""
    line2 = address.get("address2") or ""
    city_line = ", ".join([part for part in [address.get("city"), address.get("state"), address.get("zip")] if part])
    country = address.get("country") or ""
    return "\n".join([part for part in [line1, line2, city_line, country] if part])


def render_admin_orders_dashboard(orders: list[dict]) -> bytes:
    rows = []
    for order in orders:
        quote_items = order.get("quote_items") or []
        if not isinstance(quote_items, list):
            quote_items = []
        selected = ", ".join(
            [
                str(item.get("door_position_display") or item.get("door_position") or "")
                for item in quote_items[:4]
                if isinstance(item, dict)
            ]
        ) or ", ".join(order.get("selected_door_positions") or [])
        search_blob = " ".join(
            str(part or "")
            for part in [
                order.get("id"),
                order.get("customer_name"),
                order.get("customer_phone"),
                order.get("customer_email"),
                order.get("brand"),
                order.get("equipment_model"),
                order.get("payment_status"),
                order.get("fulfillment_status"),
                selected,
            ]
        ).lower()
        payment_status = str(order.get("payment_status") or "").lower()
        order_status = str(order.get("order_status") or "").lower()
        is_paid = payment_status in {"paid", "complete", "completed", "success", "succeeded"}
        is_confirmed = order_status in {"confirmed", "verified", "approved", "ready", "production_ready"}
        rows.append(
            f"""<tr data-order-row data-search="{esc(search_blob)}" data-payment="{'paid' if is_paid else 'unpaid'}" data-confirmation="{'confirmed' if is_confirmed else 'unconfirmed'}" data-created="{esc(order.get('created_at'))}">
<td><a href="/ADMIN?order_id={esc(order.get('id'))}">#{esc(order.get('id'))}</a><br><span class="muted">{esc(short_datetime(order.get('created_at')))}</span></td>
<td><strong>{esc(order.get('customer_name'))}</strong><br>{esc(order.get('customer_phone'))}<br>{esc(order.get('customer_email'))}</td>
<td><strong>{esc(order.get('brand'))}</strong><br>{esc(order.get('equipment_model'))}</td>
<td>{esc(selected)}</td>
<td><strong>{money(order.get('subtotal_usd'))}</strong><br>{esc(order.get('currency') or 'USD')}</td>
<td>{esc(zh_status(order.get('payment_status')))}<br><span class="muted">{esc(zh_status(order.get('fulfillment_status')))}</span></td>
<td><a href="{esc(order.get('checkout_url'))}" target="_blank" rel="noopener">Shopify付款链接</a></td>
</tr>"""
        )
    rows_html = "\n".join(rows) if rows else "<tr><td colspan='7'>暂无客户订单。</td></tr>"
    return page("后台订单", f"""
<style>
.admin-table{{width:100%;border-collapse:collapse;background:white}}
.admin-table th,.admin-table td{{border:1px solid #dbe2ea;padding:10px;text-align:left;vertical-align:top;font-size:13px}}
.admin-table th{{background:#f8fafc;color:#687385}}
.admin-actions{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px}}
.admin-actions .logout{{margin-left:auto}}
.admin-actions .active{{background:#0d1f2a}}
.admin-filter{{border:1px solid #dbe2ea;border-radius:8px;background:#fff;margin:0 0 14px;overflow:hidden}}
.admin-filter summary{{cursor:pointer;display:flex;align-items:center;justify-content:space-between;padding:12px 14px;font-weight:700;color:#0d1f2a;list-style:none}}
.admin-filter summary::-webkit-details-marker{{display:none}}
.admin-filter summary::after{{content:"+";font-size:18px;color:#087b83}}
.admin-filter[open] summary::after{{content:"-"}}
.admin-filter-body{{border-top:1px solid #e7edf3;padding:14px;display:grid;gap:8px;max-width:520px}}
.admin-filter-body label{{font-size:13px;color:#687385}}
.admin-filter-body input{{width:100%;box-sizing:border-box;border:1px solid #ccd6e2;border-radius:8px;padding:11px 12px;font-size:15px}}
.admin-filter-row{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:4px}}
.admin-filter-title{{font-size:13px;color:#687385;min-width:62px}}
.admin-filter-chip{{border:1px solid #ccd6e2;background:#f8fafc;color:#0d1f2a;border-radius:999px;padding:8px 12px;font-size:13px;cursor:pointer}}
.admin-filter-chip.active{{border-color:#087b83;background:#e7f7f8;color:#05656b;font-weight:700}}
.admin-filter-clear{{border-color:#d8a4a4;background:#fff7f7;color:#8a2828}}
.admin-filter-count{{font-size:13px;color:#687385}}
</style>
<section><h2>后台订单</h2>
<p class="muted">工作人员查看客户订单、生产资料和后续联系记录。</p>
{admin_nav('orders')}
<details class="admin-filter">
<summary>筛选订单</summary>
<div class="admin-filter-body">
<label for="admin-order-search">搜索订单号、客户、电话、邮箱、品牌或型号</label>
<input id="admin-order-search" type="search" placeholder="输入关键词筛选订单">
<div class="admin-filter-row" aria-label="付款状态筛选">
<span class="admin-filter-title">付款</span>
<button class="admin-filter-chip active" type="button" data-filter-group="payment" data-filter-value="all">全部</button>
<button class="admin-filter-chip" type="button" data-filter-group="payment" data-filter-value="unpaid">未付款</button>
<button class="admin-filter-chip" type="button" data-filter-group="payment" data-filter-value="paid">已付款</button>
</div>
<div class="admin-filter-row" aria-label="确认状态筛选">
<span class="admin-filter-title">确认</span>
<button class="admin-filter-chip active" type="button" data-filter-group="confirmation" data-filter-value="all">全部</button>
<button class="admin-filter-chip" type="button" data-filter-group="confirmation" data-filter-value="unconfirmed">未确认</button>
<button class="admin-filter-chip" type="button" data-filter-group="confirmation" data-filter-value="confirmed">已确认</button>
</div>
<div class="admin-filter-row" aria-label="时间范围筛选">
<span class="admin-filter-title">时间</span>
<button class="admin-filter-chip active" type="button" data-filter-group="days" data-filter-value="all">全部</button>
<button class="admin-filter-chip" type="button" data-filter-group="days" data-filter-value="1">最近1天</button>
<button class="admin-filter-chip" type="button" data-filter-group="days" data-filter-value="3">最近3天</button>
<button class="admin-filter-chip" type="button" data-filter-group="days" data-filter-value="7">最近7天</button>
<button class="admin-filter-chip" type="button" data-filter-group="days" data-filter-value="30">最近30天</button>
<button class="admin-filter-chip admin-filter-clear" type="button" data-filter-reset>清空筛选</button>
</div>
<div class="admin-filter-count" id="admin-order-count"></div>
</div>
</details>
<table class="admin-table"><thead><tr><th>订单</th><th>客户</th><th>冰箱</th><th>选择的密封条</th><th>金额</th><th>状态</th><th>付款链接</th></tr></thead><tbody>{rows_html}</tbody></table>
<script>
(() => {{
  const input = document.getElementById('admin-order-search');
  const count = document.getElementById('admin-order-count');
  const rows = Array.from(document.querySelectorAll('[data-order-row]'));
  const filters = {{payment:'all', confirmation:'all', days:'all'}};
  const setActive = (group, value) => {{
    document.querySelectorAll(`[data-filter-group="${{group}}"]`).forEach(button => {{
      button.classList.toggle('active', button.dataset.filterValue === value);
    }});
  }};
  const rowInDays = (row, days) => {{
    if (days === 'all') return true;
    const created = Date.parse(row.dataset.created || '');
    if (!created) return false;
    const limit = Number(days) * 24 * 60 * 60 * 1000;
    return Date.now() - created <= limit;
  }};
  const update = () => {{
    const q = (input.value || '').trim().toLowerCase();
    let visible = 0;
    rows.forEach(row => {{
      const textOk = !q || (row.dataset.search || '').includes(q);
      const paymentOk = filters.payment === 'all' || row.dataset.payment === filters.payment;
      const confirmationOk = filters.confirmation === 'all' || row.dataset.confirmation === filters.confirmation;
      const daysOk = rowInDays(row, filters.days);
      const ok = textOk && paymentOk && confirmationOk && daysOk;
      row.style.display = ok ? '' : 'none';
      if (ok) visible += 1;
    }});
    count.textContent = q ? `显示 ${{visible}} / ${{rows.length}} 个订单` : `共 ${{rows.length}} 个订单`;
  }};
  input?.addEventListener('input', update);
  document.querySelectorAll('[data-filter-group]').forEach(button => {{
    button.addEventListener('click', () => {{
      const group = button.dataset.filterGroup;
      const value = button.dataset.filterValue;
      filters[group] = value;
      setActive(group, value);
      update();
    }});
  }});
  document.querySelector('[data-filter-reset]')?.addEventListener('click', () => {{
    input.value = '';
    Object.keys(filters).forEach(group => {{
      filters[group] = 'all';
      setActive(group, 'all');
    }});
    update();
  }});
  update();
}})();
</script>
</section>""")


def render_admin_order(order: dict, product: dict | None, request: dict | None, current_quote_items: list[dict]) -> bytes:
    quote_items = order.get("quote_items") or []
    if not isinstance(quote_items, list):
        quote_items = []
    product_snapshot = order.get("product_snapshot") or {}
    if not isinstance(product_snapshot, dict):
        product_snapshot = {}
    gasket_rows = []
    for item in quote_items:
        if not isinstance(item, dict):
            continue
        gasket_rows.append(
            f"""<tr>
<td>{esc(item.get('door_position_display') or item.get('door_position'))}</td>
<td>{esc(item.get('part_number') or item.get('universal_part_number'))}</td>
<td>{esc(item.get('customer_size') or item.get('dimensions_text'))}</td>
<td>{esc(item.get('color'))}</td>
<td>{esc(item.get('gasket_type') or item.get('profile_type'))}</td>
<td>{esc(item.get('confidence_score'))}%</td>
<td>{money(item.get('final_price_usd'))}</td>
<td>{esc(item.get('source_name'))}<br><span class="muted">{esc(item.get('source_url'))}</span></td>
</tr>"""
        )
    current_rows = []
    for item in current_quote_items:
        current_rows.append(
            f"""<tr><td>{esc(item.get('door_position_display') or item.get('door_position'))}</td><td>{esc(item.get('part_number') or item.get('universal_part_number'))}</td><td>{esc(customer_gasket_size(item))}</td><td>{esc(item.get('confidence_score'))}%</td><td>{money(item.get('final_price_usd'))}</td></tr>"""
        )
    customer_rows = "".join(
        [
            f"<div>{esc(label)}</div><div><strong>{esc(value)}</strong></div>"
            for label, value in [
                ("姓名", order.get("customer_name")),
                ("电话", order.get("customer_phone")),
                ("邮箱", order.get("customer_email")),
                ("收货地址", shipping_address_text(order)),
            ]
        ]
    )
    product_rows = "".join(
        [
            f"<div>{esc(label)}</div><div><strong>{esc(value)}</strong></div>"
            for label, value in [
                ("品牌", order.get("brand") or product_snapshot.get("brand")),
                ("型号", order.get("equipment_model") or product_snapshot.get("equipment_model")),
                ("产品类型", (product or {}).get("product_type") or product_snapshot.get("product_type")),
                ("门数", (product or {}).get("door_count") or product_snapshot.get("door_count")),
                ("门位结构", (product or {}).get("door_layout") or product_snapshot.get("door_layout")),
                ("生命周期", (product or {}).get("lifecycle_status") or product_snapshot.get("lifecycle_status")),
                ("资料置信度", (product or {}).get("data_confidence") or product_snapshot.get("data_confidence")),
            ]
        ]
    )
    image = (product or {}).get("product_image_url") or product_snapshot.get("product_image_url")
    image_html = f"<img class='photo' src='{esc(image)}' alt='产品图片'>" if image else "<div class='photo loading'>缺少产品图片</div>"
    plate = order.get("nameplate_image_url") or (request or {}).get("nameplate_image_url")
    plate_html = f"<img class='plate' src='{esc(plate)}' alt='铭牌图片'>" if plate else "<div class='plate loading'>未关联铭牌图片</div>"
    gasket_rows_html = "".join(gasket_rows) if gasket_rows else "<tr><td colspan='8'>暂无客户选择的密封条快照。</td></tr>"
    current_rows_html = "".join(current_rows) if current_rows else "<tr><td colspan='5'>当前数据库暂无密封条记录。</td></tr>"
    product_id = order.get("refrigerator_product_id")
    return page("后台订单详情", f"""
<style>
pre{{white-space:pre-wrap;max-height:240px;overflow:auto;background:#f8fafc;border:1px solid #dbe2ea;border-radius:6px;padding:8px}}
.admin-table{{width:100%;border-collapse:collapse;background:white}}
.admin-table th,.admin-table td{{border:1px solid #dbe2ea;padding:10px;text-align:left;vertical-align:top;font-size:13px}}
.admin-table th{{background:#f8fafc;color:#687385}}
.admin-actions{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px}}
.admin-actions .logout{{margin-left:auto}}
</style>
<section><div class="admin-actions"><a class="button" href="/ADMIN">返回订单列表</a><a class="button" href="/ADMIN?view=products">产品数据库</a><a class="button" href="/ADMIN?view=gasket_catalog">密封条数据库</a><a class="button" href="/ADMIN?view=product_gaskets">关联数据库</a>{f' <a class="button" href="/ADMIN?product_id={esc(product_id)}">产品数据库记录</a> <a class="button" href="/preview?product_id={esc(product_id)}">客户预览页</a>' if product_id else ''}<a class="button" href="{esc(order.get('checkout_url'))}" target="_blank" rel="noopener">Shopify付款链接</a><a class="button logout" href="/ADMIN/logout">退出登录</a></div>
<h2>订单 #{esc(order.get('id'))}</h2>
<div class="summary"><div class="metric"><span>付款状态</span><strong>{esc(zh_status(order.get('payment_status')))}</strong></div><div class="metric"><span>生产状态</span><strong>{esc(zh_status(order.get('fulfillment_status')))}</strong></div><div class="metric"><span>金额</span><strong>{money(order.get('subtotal_usd'))}</strong></div></div>
</section>
<section><h2>客户与冰箱资料</h2><div class="result-grid"><div>{image_html}<br>{plate_html}</div><div><h3>客户资料</h3><div class="facts">{customer_rows}</div></div><div><h3>产品资料</h3><div class="facts">{product_rows}</div></div></div></section>
<section><h2>生产用密封条快照</h2><table class="admin-table"><thead><tr><th>门位</th><th>配件号</th><th>尺寸</th><th>颜色</th><th>类型/截面</th><th>置信度</th><th>价格</th><th>来源</th></tr></thead><tbody>{gasket_rows_html}</tbody></table></section>
<section><h2>当前数据库密封条记录</h2><table class="admin-table"><thead><tr><th>门位</th><th>配件号</th><th>尺寸</th><th>置信度</th><th>价格</th></tr></thead><tbody>{current_rows_html}</tbody></table></section>
<section><h2>内部原始资料</h2><div class="grid"><div><h3>产品快照</h3><pre>{esc(compact_json(product_snapshot))}</pre></div><div><h3>客户请求记录</h3><pre>{esc(compact_json(request or {}))}</pre></div></div></section>""")


def render_admin_dashboard(products_page: dict, stats: dict | None = None) -> bytes:
    stats = stats or {}
    products = products_page.get("rows") or []
    rows = []
    for product in products:
        product_id = product.get("id")
        missing = []
        if not product.get("product_image_url"):
            missing.append("主图")
        if not product.get("product_type"):
            missing.append("产品类型")
        if not product.get("door_count") and not product.get("door_positions"):
            missing.append("门数/门位")
        if not product.get("lifecycle_status"):
            missing.append("在售状态")
        missing_text = ", ".join(missing[:4]) or "无"
        image_state = "已有" if product.get("product_image_url") else "缺少"
        search_blob = " ".join(
            str(part or "")
            for part in [
                product_id,
                product.get("brand"),
                product.get("equipment_model"),
                product.get("product_type"),
                product.get("door_layout"),
                product.get("market_category"),
                product.get("commercial_sector"),
                product.get("equipment_category"),
                product.get("equipment_form"),
                product.get("temperature_application"),
                product.get("data_status"),
                missing_text,
                image_state,
            ]
        ).lower()
        missing_state = "missing" if missing else "complete"
        image_filter_state = "has-image" if product.get("product_image_url") else "missing-image"
        door_count_value = product.get("door_count") or ""
        completeness_value = int(round((4 - len(missing)) / 4 * 100))
        confidence_value = product.get("data_confidence") or 0
        rows.append(
            f"""<tr data-product-row data-search="{esc(search_blob)}" data-image="{image_filter_state}" data-missing="{missing_state}" data-door-count="{esc(door_count_value)}" data-completeness="{esc(completeness_value)}" data-confidence="{esc(confidence_value)}">
<td><a href="/ADMIN?product_id={esc(product_id)}">{esc(product_id)}</a></td>
<td><strong>{esc(product.get('brand'))}</strong><br>{esc(product.get('equipment_model'))}</td>
<td>{esc(product.get('market_category'))}<br><span class="muted">{esc(product.get('commercial_sector'))}</span><br><span class="muted">{esc(product.get('classification_confidence'))}%</span></td>
<td>{esc(product.get('product_type') or '')}<br><span class="muted">{esc(product.get('door_layout') or '')}</span></td>
<td>{esc(product.get('equipment_category'))}<br><span class="muted">{esc(product.get('equipment_form'))}</span><br><span class="muted">{esc(product.get('temperature_application'))}</span></td>
<td>{esc(zh_status(product.get('data_status')))}</td>
<td>{esc(completeness_value)}%</td>
<td>{esc(confidence_value)}%</td>
<td>{esc(image_state)}</td>
<td>{esc(missing_text)}</td>
<td><span class="muted">产品更新</span><br>{esc(product.get('updated_at') or '')}<br><span class="muted">最后补全</span><br>{esc(product.get('last_enriched_at') or '')}<br><span class="muted">发现时间</span><br>{esc(product.get('last_discovered_at') or '')}</td>
</tr>"""
        )
    rows_html = "\n".join(rows) if rows else "<tr><td colspan='11'>没有找到匹配的产品型号。</td></tr>"
    query_text = products_page.get("query") or ""
    page_num = int(products_page.get("page") or 1)
    per_page = int(products_page.get("per_page") or 50)
    total = int(products_page.get("total") or 0)
    total_pages = int(products_page.get("total_pages") or 1)
    shown_from = (page_num - 1) * per_page + 1 if total else 0
    shown_to = min(total, page_num * per_page)
    full_page_capacity = per_page * total_pages if total else 0
    last_page_count = total - per_page * (total_pages - 1) if total else 0
    applied_text = "；".join(products_page.get("applied") or []) or "全部产品"
    def product_page_url(target_page: int, target_per_page: int | None = None) -> str:
        params = {
            "view": "products",
            "q": query_text,
            "page": str(max(1, target_page)),
            "per_page": str(target_per_page or per_page),
        }
        return "/ADMIN?" + urlencode(params)
    prev_link = product_page_url(page_num - 1) if page_num > 1 else ""
    next_link = product_page_url(page_num + 1) if page_num < total_pages else ""
    page_links = []
    if total_pages <= 9:
        page_numbers = list(range(1, total_pages + 1))
    else:
        page_numbers = sorted({1, 2, max(1, page_num - 1), page_num, min(total_pages, page_num + 1), total_pages - 1, total_pages})
    previous_number = 0
    for number in page_numbers:
        if previous_number and number - previous_number > 1:
            page_links.append("<span class='admin-page-gap'>...</span>")
        if number == page_num:
            page_links.append(f"<span class='admin-page-link active'>{number}</span>")
        else:
            page_links.append(f"<a class='admin-page-link' href='{esc(product_page_url(number))}'>{number}</a>")
        previous_number = number
    pagination_html = f"""
<div class="admin-pagination">
<div class="admin-result-summary">
<strong>{esc(applied_text)}</strong>：共 <strong>{esc(total)}</strong> 条；每页 <strong>{esc(per_page)}</strong> 条；第 <strong>{esc(page_num)}</strong> / <strong>{esc(total_pages)}</strong> 页；本页显示 <strong>{esc(shown_from)}-{esc(shown_to)}</strong> 条。<span class="muted">分页核对：{esc(per_page)} × {esc(total_pages)} = {esc(full_page_capacity)} 个位置；最后一页实际 {esc(last_page_count)} 条。</span>
</div>
<div class="admin-page-controls">
{f"<a class='admin-page-link' href='{esc(prev_link)}'>上一页</a>" if prev_link else "<span class='admin-page-link disabled'>上一页</span>"}
{''.join(page_links)}
{f"<a class='admin-page-link' href='{esc(next_link)}'>下一页</a>" if next_link else "<span class='admin-page-link disabled'>下一页</span>"}
</div>
</div>"""
    recent_search_buttons = []
    for item in stats.get("recent_searches") or []:
        label = " ".join([str(item.get("detected_brand") or "").strip(), str(item.get("detected_model") or "").strip()]).strip()
        if label:
            recent_search_buttons.append(f"""<button type="button" class="admin-filter-chip" data-product-query="{esc(label)}">{esc(label)}</button>""")
    recent_search_html = "".join(recent_search_buttons) or "<span class='muted'>暂无最近搜索记录</span>"
    stats_html = f"""
<section><details class="admin-filter">
<summary>数据库看板</summary>
<div class="admin-filter-body" style="max-width:none">
<div class="summary">
<div class="metric"><span>产品型号</span><strong>{esc(stats.get('product_total'))}</strong></div>
<div class="metric"><span>商用型号</span><strong>{esc(stats.get('commercial_products'))}</strong></div>
<div class="metric"><span>家用型号</span><strong>{esc(stats.get('residential_products'))}</strong></div>
<div class="metric"><span>未分类型号</span><strong>{esc(stats.get('unknown_market_products'))}</strong></div>
<div class="metric"><span>已有主图</span><strong>{esc(stats.get('product_images'))}</strong><span class="muted">{esc(stats.get('product_image_rate'))}%</span></div>
<div class="metric"><span>密封条记录</span><strong>{esc(stats.get('quote_items'))}</strong></div>
<div class="metric"><span>有尺寸记录</span><strong>{esc(stats.get('quote_items_with_size'))}</strong><span class="muted">{esc(stats.get('quote_size_rate'))}%</span></div>
<div class="metric"><span>横截面/型材参考</span><strong>{esc(stats.get('known_profiles'))}</strong></div>
<div class="metric"><span>100%可信资料</span><strong>{esc(stats.get('trusted_products'))}</strong><span class="muted">产品</span></div>
<div class="metric"><span>100%可信密封条</span><strong>{esc(stats.get('trusted_gaskets'))}</strong></div>
<div class="metric"><span>内部订单</span><strong>{esc(stats.get('customer_orders'))}</strong></div>
<div class="metric"><span>通用密封条库</span><strong>{esc(stats.get('gasket_parts'))}</strong></div>
</div>
<details class="admin-filter" style="margin-top:14px">
<summary>最近搜索型号</summary>
<div class="admin-filter-body"><div class="admin-filter-row">{recent_search_html}</div></div>
</details>
</div>
</details>
</section>"""
    return page("后台产品数据库", f"""
<style>
.admin-table{{width:100%;border-collapse:collapse;background:white}}
.admin-table th,.admin-table td{{border:1px solid #dbe2ea;padding:10px;text-align:left;vertical-align:top;font-size:13px}}
.admin-table th{{background:#f8fafc;color:#687385}}
.admin-actions{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px}}
.admin-actions .logout{{margin-left:auto}}
.admin-actions .active{{background:#0d1f2a}}
.admin-filter{{border:1px solid #dbe2ea;border-radius:8px;background:#fff;margin:0 0 14px;overflow:hidden}}
.admin-filter summary{{cursor:pointer;display:flex;align-items:center;justify-content:space-between;padding:12px 14px;font-weight:700;color:#0d1f2a;list-style:none}}
.admin-filter summary::-webkit-details-marker{{display:none}}
.admin-filter summary::after{{content:"+";font-size:18px;color:#087b83}}
.admin-filter[open] summary::after{{content:"-"}}
.admin-filter-body{{border-top:1px solid #e7edf3;padding:14px;display:grid;gap:8px;max-width:560px}}
.admin-filter-body label{{font-size:13px;color:#687385}}
.admin-filter-body input{{width:100%;box-sizing:border-box;border:1px solid #ccd6e2;border-radius:8px;padding:11px 12px;font-size:15px}}
.admin-search-line{{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center}}
.admin-search-button{{border:0;background:#0a6f78;color:#fff;border-radius:8px;min-height:42px;padding:0 18px;font-weight:700;cursor:pointer}}
.admin-filter-row{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:4px}}
.admin-filter-title{{font-size:13px;color:#687385;min-width:62px}}
.admin-filter-chip{{border:1px solid #ccd6e2;background:#f8fafc;color:#0d1f2a;border-radius:999px;padding:8px 12px;font-size:13px;cursor:pointer}}
.admin-filter-chip.active{{border-color:#087b83;background:#e7f7f8;color:#05656b;font-weight:700}}
.admin-filter-clear{{border-color:#d8a4a4;background:#fff7f7;color:#8a2828}}
.admin-filter-help{{font-size:13px;color:#687385;line-height:1.55}}
.admin-filter-count{{font-size:13px;color:#687385}}
.admin-pagination{{display:grid;gap:10px;background:#fff;border:1px solid #dbe2ea;border-radius:8px;padding:12px 14px;margin:0 0 12px}}
.admin-result-summary{{font-size:14px;color:#334155;line-height:1.5}}
.admin-page-controls{{display:flex;gap:7px;align-items:center;flex-wrap:wrap}}
.admin-page-link{{border:1px solid #ccd6e2;background:#f8fafc;color:#0d1f2a;border-radius:7px;padding:7px 10px;text-decoration:none;font-size:13px}}
.admin-page-link.active{{background:#0a6f78;color:#fff;border-color:#0a6f78;font-weight:800}}
.admin-page-link.disabled{{opacity:.45}}
.admin-page-gap{{color:#687385;padding:0 2px}}
.admin-page-size{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
.admin-page-size select{{border:1px solid #ccd6e2;border-radius:8px;padding:10px 12px;background:white}}
</style>
<section><h2>后台产品数据库</h2>
<p class="muted">内部查看产品资料证据、字段完整度、置信度和补全时间。</p>
{admin_nav('products')}
<div class="admin-filter-body" style="max-width:none;margin-bottom:12px">
<form method="get" action="/ADMIN">
<input type="hidden" name="view" value="products">
<input type="hidden" name="page" value="1">
<label for="admin-product-search">搜索产品数据库</label>
<div class="admin-search-line"><input id="admin-product-search" name="q" type="search" value="{esc(query_text)}" placeholder="例如：Whirlpool WRF535SMHZ03；缺图片 3门 True；或：完整度<80 置信度>70"><button class="admin-search-button" type="submit" id="admin-product-search-button">搜索</button></div>
<div class="admin-page-size"><span class="admin-filter-help">每页显示</span><select name="per_page" onchange="this.form.submit()">
<option value="25" {"selected" if per_page == 25 else ""}>25 条</option>
<option value="50" {"selected" if per_page == 50 else ""}>50 条</option>
<option value="100" {"selected" if per_page == 100 else ""}>100 条</option>
</select></div>
</form>
<div class="admin-filter-help">支持：品牌/型号关键词、商用、家用、饭店、商超、医疗、冷藏柜、冷冻柜、两用、展示柜、步入式、缺图片、有图片、1门/2门/3门/4门。</div>
</div>
</section>
{stats_html}
<section>
<details class="admin-filter">
<summary>筛选产品资料</summary>
<div class="admin-filter-body">
<div class="admin-filter-row" aria-label="图片状态筛选">
<span class="admin-filter-title">图片</span>
<button class="admin-filter-chip active" type="button" data-product-filter-group="image" data-filter-value="all">全部</button>
<button class="admin-filter-chip" type="button" data-product-filter-group="image" data-filter-value="has-image">已有图片</button>
<button class="admin-filter-chip" type="button" data-product-filter-group="image" data-filter-value="missing-image">缺少图片</button>
</div>
<div class="admin-filter-row" aria-label="资料状态筛选">
<span class="admin-filter-title">资料</span>
<button class="admin-filter-chip active" type="button" data-product-filter-group="missing" data-filter-value="all">全部</button>
<button class="admin-filter-chip" type="button" data-product-filter-group="missing" data-filter-value="complete">资料完整</button>
<button class="admin-filter-chip" type="button" data-product-filter-group="missing" data-filter-value="missing">缺少资料</button>
<button class="admin-filter-chip admin-filter-clear" type="button" data-product-filter-reset>清空筛选</button>
</div>
<details class="admin-filter" style="margin-top:8px">
<summary>快捷搜索链接</summary>
<div class="admin-filter-body">
<div class="admin-filter-row">
<button type="button" class="admin-filter-chip" data-product-query="缺图片">缺图片</button>
<button type="button" class="admin-filter-chip" data-product-query="有图片">有图片</button>
<button type="button" class="admin-filter-chip" data-product-query="缺资料">缺资料</button>
<button type="button" class="admin-filter-chip" data-product-query="资料完整">资料完整</button>
<button type="button" class="admin-filter-chip" data-product-query="商用">商用</button>
<button type="button" class="admin-filter-chip" data-product-query="家用">家用</button>
<button type="button" class="admin-filter-chip" data-product-query="饭店">饭店</button>
<button type="button" class="admin-filter-chip" data-product-query="商超">商超</button>
<button type="button" class="admin-filter-chip" data-product-query="医疗">医疗</button>
<button type="button" class="admin-filter-chip" data-product-query="冷藏柜">冷藏柜</button>
<button type="button" class="admin-filter-chip" data-product-query="冷冻柜">冷冻柜</button>
<button type="button" class="admin-filter-chip" data-product-query="两用">两用</button>
<button type="button" class="admin-filter-chip" data-product-query="展示柜">展示柜</button>
<button type="button" class="admin-filter-chip" data-product-query="步入式">步入式</button>
<button type="button" class="admin-filter-chip" data-product-query="1门">1门</button>
<button type="button" class="admin-filter-chip" data-product-query="2门">2门</button>
<button type="button" class="admin-filter-chip" data-product-query="3门">3门</button>
<button type="button" class="admin-filter-chip" data-product-query="4门">4门</button>
<button type="button" class="admin-filter-chip" data-product-query="8门">8门</button>
<button type="button" class="admin-filter-chip" data-product-query="完整度<80">完整度&lt;80</button>
<button type="button" class="admin-filter-chip" data-product-query="置信度<70">置信度&lt;70</button>
<button type="button" class="admin-filter-chip" data-product-query="Whirlpool">Whirlpool</button>
<button type="button" class="admin-filter-chip" data-product-query="True">True</button>
<button type="button" class="admin-filter-chip" data-product-query="Sub-Zero">Sub-Zero</button>
</div>
</div>
</details>
<div class="admin-filter-count" id="admin-product-count"></div>
</div>
</details>
{pagination_html}
<table class="admin-table"><thead><tr><th>ID</th><th>产品</th><th>市场/行业</th><th>类型/结构</th><th>设备分类</th><th>状态</th><th>完整度</th><th>置信度</th><th>图片</th><th>缺少资料</th><th>时间</th></tr></thead><tbody>{rows_html}</tbody></table>
{pagination_html}
<script>
(() => {{
  const input = document.getElementById('admin-product-search');
  const searchButton = document.getElementById('admin-product-search-button');
  const form = input?.form;
  const serverQuery = (input?.value || '').trim().toLowerCase();
  const count = document.getElementById('admin-product-count');
  const rows = Array.from(document.querySelectorAll('[data-product-row]'));
  const filters = {{image:'all', missing:'all'}};
  const matchNumber = (actual, operator, expected) => {{
    const value = Number(actual || 0);
    const target = Number(expected || 0);
    if (operator === '<') return value < target;
    if (operator === '<=') return value <= target;
    if (operator === '>') return value > target;
    if (operator === '>=') return value >= target;
    return value === target;
  }};
  const textFilterOk = (row, query) => {{
    if (!query) return true;
    const search = row.dataset.search || '';
    const aliases = {{
      '商用': 'commercial',
      '商用冰箱': 'commercial',
      '商用制冷': 'commercial',
      '家用': 'residential',
      '民用': 'residential',
      '家用冰箱': 'residential',
      '未分类': 'unknown',
      '未知': 'unknown',
      '饭店': 'restaurant',
      '餐厅': 'restaurant',
      '商超': 'supermarket',
      '超市': 'supermarket',
      '医疗': 'medical',
      '酒吧': 'bar',
      '冷藏': 'refrigerator',
      '冷藏柜': 'refrigerator',
      '冷冻': 'freezer',
      '冷冻柜': 'freezer',
      '两用': 'dual_temp',
      '冷藏冷冻': 'dual_temp',
      '展示': 'display_case',
      '展示柜': 'display_case',
      '备餐台': 'prep_table',
      '吧台柜': 'bar_cooler',
      '步入式': 'walk_in',
      '制冰': 'ice_machine',
      '制冰机': 'ice_machine'
    }};
    const tokens = query.split(/\\s+/).filter(Boolean);
    return tokens.every(token => {{
      token = aliases[token] || token;
      if (['缺图片','无图片','没有图片'].includes(token)) return row.dataset.image === 'missing-image';
      if (['有图片','已有图片'].includes(token)) return row.dataset.image === 'has-image';
      if (['缺资料','缺少资料'].includes(token)) return row.dataset.missing === 'missing';
      if (['资料完整','完整资料'].includes(token)) return row.dataset.missing === 'complete';
      const doorMatch = token.match(/^(\\d+)\\s*门$/);
      if (doorMatch) return String(row.dataset.doorCount || '') === doorMatch[1];
      const completenessMatch = token.match(/^(?:完整度|complete|completeness)(<=|>=|<|>|=)(\\d+)$/i);
      if (completenessMatch) return matchNumber(row.dataset.completeness, completenessMatch[1], completenessMatch[2]);
      const confidenceMatch = token.match(/^(?:置信度|confidence)(<=|>=|<|>|=)(\\d+)$/i);
      if (confidenceMatch) return matchNumber(row.dataset.confidence, confidenceMatch[1], confidenceMatch[2]);
      const brandMatch = token.match(/^品牌[:：]?(.+)$/);
      if (brandMatch) return search.includes(brandMatch[1].toLowerCase());
      const modelMatch = token.match(/^型号[:：]?(.+)$/);
      if (modelMatch) return search.includes(modelMatch[1].toLowerCase());
      return search.includes(token);
    }});
  }};
  const setActive = (group, value) => {{
    document.querySelectorAll(`[data-product-filter-group="${{group}}"]`).forEach(button => {{
      button.classList.toggle('active', button.dataset.filterValue === value);
    }});
  }};
  const update = () => {{
    const typedQuery = (input.value || '').trim().toLowerCase();
    const q = typedQuery === serverQuery ? '' : typedQuery;
    let visible = 0;
    rows.forEach(row => {{
      const textOk = textFilterOk(row, q);
      const imageOk = filters.image === 'all' || row.dataset.image === filters.image;
      const missingOk = filters.missing === 'all' || row.dataset.missing === filters.missing;
      const ok = textOk && imageOk && missingOk;
      row.style.display = ok ? '' : 'none';
      if (ok) visible += 1;
    }});
    count.textContent = q ? `显示 ${{visible}} / ${{rows.length}} 条产品资料` : `共 ${{rows.length}} 条产品资料`;
  }};
  input?.addEventListener('input', update);
  input?.addEventListener('keydown', event => {{
    if (event.key === 'Enter') {{
      update();
    }}
  }});
  searchButton?.addEventListener('click', update);
  document.querySelectorAll('[data-product-query]').forEach(button => {{
    button.addEventListener('click', () => {{
      input.value = button.dataset.productQuery || '';
      update();
      form?.submit();
      input.focus();
    }});
  }});
  document.querySelectorAll('[data-product-filter-group]').forEach(button => {{
    button.addEventListener('click', () => {{
      const group = button.dataset.productFilterGroup;
      const value = button.dataset.filterValue;
      filters[group] = value;
      setActive(group, value);
      update();
    }});
  }});
  document.querySelector('[data-product-filter-reset]')?.addEventListener('click', () => {{
    input.value = '';
    Object.keys(filters).forEach(group => {{
      filters[group] = 'all';
      setActive(group, 'all');
    }});
    update();
    form?.submit();
  }});
  update();
}})();
</script>
</section>""")


def render_admin_gasket_catalog(catalog_page: dict) -> bytes:
    rows = []
    for item in catalog_page.get("rows") or []:
        brands = ", ".join((item.get("compatible_brands") or [])[:4])
        models = ", ".join((item.get("compatible_equipment_models") or [])[:4])
        positions = ", ".join((item.get("compatible_door_positions") or [])[:4])
        part_aliases = ", ".join(item.get("universal_part_numbers") or [])
        dimensions = item.get("dimensions_text") or ""
        if not dimensions and item.get("width_in") and item.get("height_in"):
            dimensions = f'{item.get("width_in")}" x {item.get("height_in")}"'
        rows.append(
            f"""<tr>
<td><a href="/ADMIN?gasket_catalog_id={esc(item.get('id'))}">#{esc(item.get('id'))}</a></td>
<td><strong>{esc(item.get('primary_part_number'))}</strong><br><span class="muted">{esc(part_aliases)}</span></td>
<td>{esc(item.get('part_name'))}<br><span class="muted">{esc(item.get('profile_name') or item.get('profile_type') or item.get('profile_family'))}</span></td>
<td>{esc(dimensions)}<br><span class="muted">{esc(item.get('gasket_color') or item.get('color'))}</span></td>
<td>{esc(item.get('install_type'))}<br><span class="muted">{esc(positions)}</span></td>
<td>{esc(brands)}<br><span class="muted">{esc(models)}</span></td>
<td>{esc(item.get('cross_check_score') or item.get('confidence_score'))}%<br><span class="muted">{esc(zh_status(item.get('data_status')))}</span></td>
<td>{esc(item.get('source_name'))}<br><span class="muted">{esc(short_datetime(item.get('updated_at')))}</span></td>
</tr>"""
        )
    rows_html = "\n".join(rows) if rows else "<tr><td colspan='8'>没有找到密封条数据库记录。</td></tr>"
    query_text = catalog_page.get("query") or ""
    page_num = int(catalog_page.get("page") or 1)
    per_page = int(catalog_page.get("per_page") or 50)
    total = int(catalog_page.get("total") or 0)
    total_pages = int(catalog_page.get("total_pages") or 1)
    applied_text = "；".join(catalog_page.get("applied") or []) or "全部密封条数据库"
    pagination_html = admin_pagination_html("gasket_catalog", query_text, page_num, per_page, total, total_pages, applied_text)
    return page("后台密封条数据库", f"""
<style>
.admin-table{{width:100%;border-collapse:collapse;background:white}}
.admin-table th,.admin-table td{{border:1px solid #dbe2ea;padding:10px;text-align:left;vertical-align:top;font-size:13px}}
.admin-table th{{background:#f8fafc;color:#687385}}
.admin-actions{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px}}
.admin-actions .logout{{margin-left:auto}}
.admin-actions .active{{background:#0d1f2a}}
.admin-filter-body{{border:1px solid #dbe2ea;border-radius:8px;background:#fff;padding:14px;display:grid;gap:8px;max-width:none;margin-bottom:12px}}
.admin-filter-body label{{font-size:13px;color:#687385}}
.admin-search-line{{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center}}
.admin-search-line input{{width:100%;box-sizing:border-box;border:1px solid #ccd6e2;border-radius:8px;padding:11px 12px;font-size:15px}}
.admin-search-button{{border:0;background:#0a6f78;color:#fff;border-radius:8px;min-height:42px;padding:0 18px;font-weight:700;cursor:pointer}}
.admin-page-size{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
.admin-page-size select{{border:1px solid #ccd6e2;border-radius:8px;padding:10px 12px;background:white}}
.admin-filter-help{{font-size:13px;color:#687385;line-height:1.55}}
.admin-pagination{{display:grid;gap:10px;background:#fff;border:1px solid #dbe2ea;border-radius:8px;padding:12px 14px;margin:0 0 12px}}
.admin-result-summary{{font-size:14px;color:#334155;line-height:1.5}}
.admin-page-controls{{display:flex;gap:7px;align-items:center;flex-wrap:wrap}}
.admin-page-link{{border:1px solid #ccd6e2;background:#f8fafc;color:#0d1f2a;border-radius:7px;padding:7px 10px;text-decoration:none;font-size:13px}}
.admin-page-link.active{{background:#0a6f78;color:#fff;border-color:#0a6f78;font-weight:800}}
.admin-page-link.disabled{{opacity:.45}}
.admin-page-gap{{color:#687385;padding:0 2px}}
.profile-visual-card{{width:190px;display:grid;gap:7px}}
.profile-visual-svg,.profile-visual-img{{width:170px;height:94px;border:1px solid #dbe2ea;border-radius:8px;background:#f8fafc;display:block;object-fit:contain}}
.profile-visual-meta{{display:grid;gap:2px;font-size:12px;line-height:1.25}}
.profile-visual-meta strong{{font-size:13px;color:#0d1f2a}}
.profile-visual-meta span{{color:#687385}}
</style>
<section><h2>后台密封条数据库</h2>
<p class="muted">管理标准密封条本体：配件号、替代号、横截面、安装方式、尺寸和可适配范围。</p>
{admin_nav('gasket_catalog')}
<div class="admin-filter-body">
<form method="get" action="/ADMIN">
<input type="hidden" name="view" value="gasket_catalog">
<input type="hidden" name="page" value="1">
<label for="admin-gasket-catalog-search">搜索密封条数据库</label>
<div class="admin-search-line"><input id="admin-gasket-catalog-search" name="q" type="search" value="{esc(query_text)}" placeholder="输入配件号、横截面、颜色、安装方式或尺寸"><button class="admin-search-button" type="submit">搜索</button></div>
<div class="admin-page-size"><span class="admin-filter-help">每页显示</span><select name="per_page" onchange="this.form.submit()">
<option value="25" {"selected" if per_page == 25 else ""}>25 条</option>
<option value="50" {"selected" if per_page == 50 else ""}>50 条</option>
<option value="100" {"selected" if per_page == 100 else ""}>100 条</option>
</select></div>
</form>
<div class="admin-filter-help">这里是一条密封条本体一行。多个冰箱型号、多个门位可以共同指向同一条目录记录。</div>
</div>
{pagination_html}
<table class="admin-table"><thead><tr><th>ID</th><th>配件号/替代号</th><th>名称/横截面</th><th>尺寸/颜色</th><th>安装/门位</th><th>适配品牌型号</th><th>评分</th><th>来源/时间</th></tr></thead><tbody>{rows_html}</tbody></table>
{pagination_html}
</section>""")


def render_admin_product_gaskets(gaskets_page: dict) -> bytes:
    rows = []
    for item in gaskets_page.get("rows") or []:
        product = item.get("refrigerator_products") or {}
        catalog = item.get("gasket_catalog") or {}
        dimensions = item.get("dimensions_text") or ""
        if not dimensions and item.get("width_in") and item.get("height_in"):
            dimensions = f'{item.get("width_in")}" x {item.get("height_in")}"'
        rows.append(
            f"""<tr>
<td><a href="/ADMIN?product_gasket_id={esc(item.get('id'))}">#{esc(item.get('id'))}</a></td>
<td><a href="/ADMIN?product_id={esc(product.get('id') or item.get('refrigerator_product_id'))}"><strong>{esc(product.get('brand'))}</strong><br>{esc(product.get('equipment_model'))}</a></td>
<td>{esc(item.get('door_position_display') or item.get('door_position'))}<br><span class="muted">门序 {esc(item.get('door_index'))}</span></td>
<td>{esc(item.get('part_number') or item.get('universal_part_number'))}<br><span class="muted">{esc(item.get('gasket_name'))}</span></td>
<td>{esc(dimensions)}<br><span class="muted">{esc(item.get('gasket_color'))}</span></td>
<td>{esc(item.get('gasket_install_type'))}<br><span class="muted">{esc(item.get('gasket_profile') or catalog.get('profile_name') or catalog.get('profile_type'))}</span></td>
<td>{f'<a href="/ADMIN?gasket_catalog_id={esc(catalog.get("id"))}">目录 #{esc(catalog.get("id"))}</a>' if catalog.get('id') else '<span class="muted">未关联目录</span>'}</td>
<td>{esc(item.get('confidence_score'))}%<br><span class="muted">{esc(zh_status(item.get('data_status') or item.get('fit_status')))}</span></td>
<td>{money(item.get('final_price_usd'))}<br><span class="muted">{esc(short_datetime(item.get('updated_at')))}</span></td>
</tr>"""
        )
    rows_html = "\n".join(rows) if rows else "<tr><td colspan='9'>没有找到关联数据库记录。</td></tr>"
    query_text = gaskets_page.get("query") or ""
    page_num = int(gaskets_page.get("page") or 1)
    per_page = int(gaskets_page.get("per_page") or 50)
    total = int(gaskets_page.get("total") or 0)
    total_pages = int(gaskets_page.get("total_pages") or 1)
    applied_text = "；".join(gaskets_page.get("applied") or []) or "全部关联数据库"
    pagination_html = admin_pagination_html("product_gaskets", query_text, page_num, per_page, total, total_pages, applied_text)
    return page("后台关联数据库", f"""
<style>
.admin-table{{width:100%;border-collapse:collapse;background:white}}
.admin-table th,.admin-table td{{border:1px solid #dbe2ea;padding:10px;text-align:left;vertical-align:top;font-size:13px}}
.admin-table th{{background:#f8fafc;color:#687385}}
.admin-actions{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px}}
.admin-actions .logout{{margin-left:auto}}
.admin-actions .active{{background:#0d1f2a}}
.admin-filter-body{{border:1px solid #dbe2ea;border-radius:8px;background:#fff;padding:14px;display:grid;gap:8px;max-width:none;margin-bottom:12px}}
.admin-filter-body label{{font-size:13px;color:#687385}}
.admin-search-line{{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center}}
.admin-search-line input{{width:100%;box-sizing:border-box;border:1px solid #ccd6e2;border-radius:8px;padding:11px 12px;font-size:15px}}
.admin-search-button{{border:0;background:#0a6f78;color:#fff;border-radius:8px;min-height:42px;padding:0 18px;font-weight:700;cursor:pointer}}
.admin-page-size{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
.admin-page-size select{{border:1px solid #ccd6e2;border-radius:8px;padding:10px 12px;background:white}}
.admin-filter-help{{font-size:13px;color:#687385;line-height:1.55}}
.admin-pagination{{display:grid;gap:10px;background:#fff;border:1px solid #dbe2ea;border-radius:8px;padding:12px 14px;margin:0 0 12px}}
.admin-result-summary{{font-size:14px;color:#334155;line-height:1.5}}
.admin-page-controls{{display:flex;gap:7px;align-items:center;flex-wrap:wrap}}
.admin-page-link{{border:1px solid #ccd6e2;background:#f8fafc;color:#0d1f2a;border-radius:7px;padding:7px 10px;text-decoration:none;font-size:13px}}
.admin-page-link.active{{background:#0a6f78;color:#fff;border-color:#0a6f78;font-weight:800}}
.admin-page-link.disabled{{opacity:.45}}
.admin-page-gap{{color:#687385;padding:0 2px}}
.profile-visual-card{{width:190px;display:grid;gap:7px}}
.profile-visual-svg,.profile-visual-img{{width:170px;height:94px;border:1px solid #dbe2ea;border-radius:8px;background:#f8fafc;display:block;object-fit:contain}}
.profile-visual-meta{{display:grid;gap:2px;font-size:12px;line-height:1.25}}
.profile-visual-meta strong{{font-size:13px;color:#0d1f2a}}
.profile-visual-meta span{{color:#687385}}
</style>
<section><h2>后台关联数据库</h2>
<p class="muted">管理冰箱型号、门位、成品密封条和密封条横截面之间的关系。这里是产品数据库和密封条数据库之间的关联表。</p>
{admin_nav('product_gaskets')}
<div class="admin-filter-body">
<form method="get" action="/ADMIN">
<input type="hidden" name="view" value="product_gaskets">
<input type="hidden" name="page" value="1">
<label for="admin-product-gasket-search">搜索关联数据库</label>
<div class="admin-search-line"><input id="admin-product-gasket-search" name="q" type="search" value="{esc(query_text)}" placeholder="输入门位、配件号、尺寸、颜色或横截面"><button class="admin-search-button" type="submit">搜索</button></div>
<div class="admin-page-size"><span class="admin-filter-help">每页显示</span><select name="per_page" onchange="this.form.submit()">
<option value="25" {"selected" if per_page == 25 else ""}>25 条</option>
<option value="50" {"selected" if per_page == 50 else ""}>50 条</option>
<option value="100" {"selected" if per_page == 100 else ""}>100 条</option>
</select></div>
</form>
<div class="admin-filter-help">这里是一门一行。三门冰箱应该显示三条：左门、右门、冷冻抽屉。</div>
</div>
{pagination_html}
<table class="admin-table"><thead><tr><th>ID</th><th>产品型号</th><th>门位</th><th>配件号</th><th>尺寸/颜色</th><th>安装/截面</th><th>目录关联</th><th>状态</th><th>价格/时间</th></tr></thead><tbody>{rows_html}</tbody></table>
{pagination_html}
</section>""")


def render_admin_gasket_catalog_detail(item: dict, applications: list[dict]) -> bytes:
    fields = [
        ("主配件号", item.get("primary_part_number")),
        ("标准编码", item.get("gasket_code")),
        ("名称", item.get("part_name")),
        ("横截面族", item.get("profile_family")),
        ("横截面名称", item.get("profile_name") or item.get("profile_type")),
        ("安装方式", item.get("mounting_method") or item.get("install_type")),
        ("颜色", item.get("color") or item.get("gasket_color")),
        ("尺寸", item.get("dimensions_text")),
        ("宽", item.get("width_in")),
        ("高", item.get("height_in")),
        ("周长", item.get("perimeter_in")),
        ("交叉印证分", item.get("cross_check_score")),
        ("置信度", item.get("confidence_score")),
        ("状态", zh_status(item.get("data_status"))),
        ("来源", item.get("source_name")),
        ("来源链接", item.get("source_url")),
    ]
    field_rows = "".join([f"<div>{esc(label)}</div><div><strong>{esc(value)}</strong></div>" for label, value in fields])
    app_rows = []
    for row in applications:
        product = row.get("refrigerator_products") or {}
        app_rows.append(
            f"""<tr><td><a href="/ADMIN?product_gasket_id={esc(row.get('id'))}">#{esc(row.get('id'))}</a></td><td><a href="/ADMIN?product_id={esc(product.get('id'))}">{esc(product.get('brand'))} {esc(product.get('equipment_model'))}</a></td><td>{esc(row.get('door_position_display') or row.get('door_position'))}</td><td>{esc(row.get('part_number'))}</td><td>{esc(row.get('dimensions_text'))}</td><td>{esc(row.get('confidence_score'))}%</td></tr>"""
        )
    app_rows_html = "".join(app_rows) if app_rows else "<tr><td colspan='6'>暂无产品门位关联。</td></tr>"
    return page("密封条数据库详情", f"""
<style>
pre{{white-space:pre-wrap;max-height:260px;overflow:auto;background:#f8fafc;border:1px solid #dbe2ea;border-radius:6px;padding:8px}}
.admin-table{{width:100%;border-collapse:collapse;background:white}}
.admin-table th,.admin-table td{{border:1px solid #dbe2ea;padding:10px;text-align:left;vertical-align:top;font-size:13px}}
.admin-table th{{background:#f8fafc;color:#687385}}
.admin-actions{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px}}
.admin-actions .logout{{margin-left:auto}}
</style>
<section>{admin_nav('gasket_catalog')}<h2>密封条数据库 #{esc(item.get('id'))}</h2><div class="facts">{field_rows}</div></section>
<section><h2>适配产品门位</h2><table class="admin-table"><thead><tr><th>关联ID</th><th>产品型号</th><th>门位</th><th>配件号</th><th>尺寸</th><th>置信度</th></tr></thead><tbody>{app_rows_html}</tbody></table></section>
<section><h2>完整原始记录</h2><pre>{esc(compact_json(item))}</pre></section>""")


def render_admin_product_gasket_detail(item: dict) -> bytes:
    product = item.get("refrigerator_products") or {}
    catalog = item.get("gasket_catalog") or {}
    fields = [
        ("产品", f"{product.get('brand') or ''} {product.get('equipment_model') or ''}".strip()),
        ("产品ID", item.get("refrigerator_product_id")),
        ("门位", item.get("door_position_display") or item.get("door_position")),
        ("门序", item.get("door_index")),
        ("配件号", item.get("part_number")),
        ("通用配件号", item.get("universal_part_number")),
        ("目录ID", item.get("gasket_catalog_id")),
        ("尺寸", item.get("dimensions_text")),
        ("宽", item.get("width_in")),
        ("高", item.get("height_in")),
        ("周长", item.get("perimeter_in")),
        ("颜色", item.get("gasket_color")),
        ("安装方式", item.get("gasket_install_type")),
        ("横截面", item.get("gasket_profile")),
        ("置信度", item.get("confidence_score")),
        ("价格", money(item.get("final_price_usd"))),
        ("状态", zh_status(item.get("data_status") or item.get("fit_status"))),
        ("来源", item.get("source_name")),
        ("来源链接", item.get("source_url")),
    ]
    field_rows = "".join([f"<div>{esc(label)}</div><div><strong>{esc(value)}</strong></div>" for label, value in fields])
    return page("关联数据库详情", f"""
<style>
pre{{white-space:pre-wrap;max-height:260px;overflow:auto;background:#f8fafc;border:1px solid #dbe2ea;border-radius:6px;padding:8px}}
.admin-actions{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px}}
.admin-actions .logout{{margin-left:auto}}
</style>
<section>{admin_nav('product_gaskets')}<h2>关联数据库 #{esc(item.get('id'))}</h2><div class="facts">{field_rows}</div>
<p><a class="button" href="/ADMIN?product_id={esc(item.get('refrigerator_product_id'))}">查看产品</a> {f'<a class="button" href="/ADMIN?gasket_catalog_id={esc(item.get("gasket_catalog_id"))}">查看密封条数据库</a>' if item.get('gasket_catalog_id') else ''}</p></section>
<section><h2>关联产品快照</h2><pre>{esc(compact_json(product))}</pre></section>
<section><h2>关联目录快照</h2><pre>{esc(compact_json(catalog))}</pre></section>
<section><h2>完整原始记录</h2><pre>{esc(compact_json(item))}</pre></section>""")


def profile_type_zh(value: str | None) -> str:
    mapping = {
        "dart": "Dart 箭头卡槽",
        "push_in": "Push-in 压入式",
        "screw_in": "Screw-in 螺丝固定",
        "snap_in": "Snap-in 卡扣式",
        "special": "特殊型",
    }
    return mapping.get((value or "").lower(), value or "")


def profile_dimension_summary(item: dict) -> str:
    pairs = [
        ("整体宽", item.get("overall_width_in") or item.get("cross_section_width_in")),
        ("整体高", item.get("overall_height_in") or item.get("cross_section_height_in")),
        ("底座宽", item.get("base_width_in")),
        ("底座高", item.get("base_height_in")),
        ("Dart深", item.get("dart_depth_in")),
        ("Dart头宽", item.get("dart_head_width_in") or item.get("dart_width_in")),
        ("螺丝边宽", item.get("screw_flange_width_in")),
        ("卡槽宽", item.get("snap_track_width_in")),
        ("气囊宽", item.get("compression_bulb_width_in")),
        ("气囊高", item.get("compression_bulb_height_in")),
        ("磁条腔宽", item.get("magnet_cavity_width_in") or item.get("magnet_width_in")),
        ("磁条腔高", item.get("magnet_cavity_height_in")),
    ]
    rendered = [f"{label} {value}\"" for label, value in pairs if value not in (None, "")]
    if rendered:
        return "；".join(rendered)
    detailed = item.get("detailed_dimensions")
    if isinstance(detailed, dict) and detailed:
        return compact_json(detailed)
    return "待补详细尺寸"


def profile_visual(item: dict) -> str:
    profile_type = (item.get("profile_type") or "").lower()
    code = esc(item.get("profile_code") or "")
    label = esc(profile_type_zh(profile_type) or "Profile")
    width = esc(item.get("overall_width_in") or item.get("cross_section_width_in") or "")
    height = esc(item.get("overall_height_in") or item.get("cross_section_height_in") or "")
    image_url = item.get("profile_image_url")
    if image_url:
        visual = f'<img class="profile-visual-img" src="{esc(image_url)}" alt="{code}">'
    elif profile_type == "dart":
        visual = """
<svg viewBox="0 0 170 94" class="profile-visual-svg" role="img" aria-label="Dart profile">
  <path d="M26 22h86c18 0 30 13 30 30s-12 30-30 30H26z" fill="#e9f2f6" stroke="#0d1f2a" stroke-width="4"/>
  <path d="M38 82l18-27h28l18 27z" fill="#0a6f78" stroke="#0d1f2a" stroke-width="4"/>
  <rect x="106" y="34" width="24" height="35" rx="8" fill="#b9c6cf" stroke="#0d1f2a" stroke-width="3"/>
</svg>"""
    elif profile_type == "push_in":
        visual = """
<svg viewBox="0 0 170 94" class="profile-visual-svg" role="img" aria-label="Push-in profile">
  <path d="M24 28h95c17 0 29 11 29 27s-12 27-29 27H24z" fill="#e9f2f6" stroke="#0d1f2a" stroke-width="4"/>
  <path d="M38 82V58h46v24z" fill="#0a6f78" stroke="#0d1f2a" stroke-width="4"/>
  <circle cx="118" cy="55" r="17" fill="#c9d6dd" stroke="#0d1f2a" stroke-width="3"/>
</svg>"""
    elif profile_type == "screw_in":
        visual = """
<svg viewBox="0 0 170 94" class="profile-visual-svg" role="img" aria-label="Screw-in profile">
  <path d="M20 34h120c15 0 24 9 24 24s-9 24-24 24H20z" fill="#e9f2f6" stroke="#0d1f2a" stroke-width="4"/>
  <rect x="20" y="66" width="92" height="16" fill="#0a6f78" stroke="#0d1f2a" stroke-width="4"/>
  <circle cx="46" cy="74" r="5" fill="#fff" stroke="#0d1f2a" stroke-width="3"/>
  <circle cx="86" cy="74" r="5" fill="#fff" stroke="#0d1f2a" stroke-width="3"/>
</svg>"""
    elif profile_type == "snap_in":
        visual = """
<svg viewBox="0 0 170 94" class="profile-visual-svg" role="img" aria-label="Snap-in profile">
  <path d="M26 26h92c17 0 29 12 29 29s-12 29-29 29H26z" fill="#e9f2f6" stroke="#0d1f2a" stroke-width="4"/>
  <path d="M45 84V62h18l10 12 10-12h18v22z" fill="#0a6f78" stroke="#0d1f2a" stroke-width="4"/>
</svg>"""
    else:
        visual = """
<svg viewBox="0 0 170 94" class="profile-visual-svg" role="img" aria-label="Special profile">
  <path d="M22 48c0-18 13-28 31-28h34c17 0 28 10 28 26 0 19 35 8 35 29 0 7-5 12-13 12H22z" fill="#e9f2f6" stroke="#0d1f2a" stroke-width="4"/>
  <path d="M36 86V62h36l14 24z" fill="#0a6f78" stroke="#0d1f2a" stroke-width="4"/>
  <circle cx="106" cy="52" r="15" fill="#c9d6dd" stroke="#0d1f2a" stroke-width="3"/>
</svg>"""
    size = f'{width}" x {height}"' if width and height else "尺寸待补"
    return f"""<div class="profile-visual-card">{visual}<div class="profile-visual-meta"><strong>{code}</strong><span>{label}</span><span>{esc(size)}</span></div></div>"""


def finished_gasket_dimension_summary(item: dict) -> str:
    if item.get("dimensions_text"):
        return str(item.get("dimensions_text"))
    if item.get("width_in") and item.get("height_in"):
        return f'{item.get("width_in")}" x {item.get("height_in")}"'
    return ""


def finished_gasket_side_summary(item: dict) -> str:
    pairs = [
        ("上", item.get("top_side_in")),
        ("下", item.get("bottom_side_in")),
        ("左", item.get("left_side_in")),
        ("右", item.get("right_side_in")),
    ]
    rendered = [f'{label} {value}"' for label, value in pairs if value not in (None, "")]
    return " / ".join(rendered) if rendered else "四边尺寸待补"


def finished_gasket_image_links(item: dict) -> str:
    links = []
    if item.get("gasket_image_url"):
        links.append(f'<a href="{esc(item.get("gasket_image_url"))}" target="_blank" rel="noopener">密封条图</a>')
    if item.get("dimensioned_gasket_image_url"):
        links.append(f'<a href="{esc(item.get("dimensioned_gasket_image_url"))}" target="_blank" rel="noopener">尺寸图</a>')
    if item.get("profile_diagram_image_url"):
        links.append(f'<a href="{esc(item.get("profile_diagram_image_url"))}" target="_blank" rel="noopener">横截面图</a>')
    return " / ".join(links) if links else "<span class='muted'>图片待补</span>"


def get_admin_gasket_catalog_page(client: httpx.Client, raw_query: str = "", page_num: int = 1, per_page: int = 50) -> dict:
    page_num, per_page, offset = admin_page_bounds(page_num, per_page)
    query_text = (raw_query or "").strip()
    params = {
        "select": "id,profile_code,profile_name,profile_family,profile_type,profile_style,style_code,mounting_method,magnetic,material,color,profile_image_url,cross_section_width_in,cross_section_height_in,dart_width_in,lip_width_in,magnet_width_in,bulb_count,overall_width_in,overall_height_in,base_width_in,base_height_in,dart_depth_in,dart_head_width_in,screw_flange_width_in,snap_track_width_in,compression_bulb_width_in,compression_bulb_height_in,magnet_cavity_width_in,magnet_cavity_height_in,detailed_dimensions,manufacturing_notes,common_use_cases,coverage_notes,estimated_market_share_pct,tested_finished_gasket_count,linked_application_count,source_name,source_url,confidence_score,data_status,updated_at",
        "order": "updated_at.desc.nullslast,id.desc",
        "limit": str(per_page),
        "offset": str(offset),
    }
    if query_text:
        safe_search = re.sub(r"[^A-Za-z0-9 ._/-]+", " ", query_text).strip()
        if safe_search:
            params["or"] = (
                f"(profile_code.ilike.*{safe_search}*,"
                f"profile_name.ilike.*{safe_search}*,"
                f"profile_family.ilike.*{safe_search}*,"
                f"profile_type.ilike.*{safe_search}*,"
                f"profile_style.ilike.*{safe_search}*,"
                f"style_code.ilike.*{safe_search}*,"
                f"mounting_method.ilike.*{safe_search}*,"
                f"material.ilike.*{safe_search}*,"
                f"color.ilike.*{safe_search}*,"
                f"common_use_cases.ilike.*{safe_search}*)"
            )
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/gasket_profiles",
        params=params,
        headers=supabase_headers("count=exact"),
    )
    response.raise_for_status()
    total = parse_content_range_total(response.headers.get("content-range"))
    total_pages = max(1, (total + per_page - 1) // per_page) if total else 1
    return {
        "rows": response.json(),
        "total": total,
        "page": page_num,
        "per_page": per_page,
        "total_pages": total_pages,
        "query": query_text,
        "applied": [f"关键词：{query_text}"] if query_text else ["全部密封条横截面"],
    }


def get_admin_product_gaskets_page(client: httpx.Client, raw_query: str = "", page_num: int = 1, per_page: int = 50) -> dict:
    page_num, per_page, offset = admin_page_bounds(page_num, per_page)
    query_text = (raw_query or "").strip()
    params = {
        "select": "*,gasket_profiles(id,profile_code,profile_name,profile_type,profile_style,mounting_method,color,estimated_market_share_pct),gasket_finished_gaskets(id,finished_gasket_code,part_number,dimensions_text,width_in,height_in,perimeter_in,top_side_in,bottom_side_in,left_side_in,right_side_in,inner_width_in,inner_height_in,outer_width_in,outer_height_in,corner_type,weld_count,gasket_image_url,dimensioned_gasket_image_url,profile_diagram_image_url,dimension_source,dimension_confidence_score,color,price_usd),refrigerator_products(id,brand,equipment_model,product_type,door_count,door_layout)",
        "order": "updated_at.desc.nullslast,id.desc",
        "limit": str(per_page),
        "offset": str(offset),
    }
    if query_text:
        safe_search = re.sub(r"[^A-Za-z0-9 ._/-]+", " ", query_text).strip()
        if safe_search:
            params["or"] = (
                f"(application_brand.ilike.*{safe_search}*,"
                f"application_model.ilike.*{safe_search}*,"
                f"application_door_position.ilike.*{safe_search}*,"
                f"application_label.ilike.*{safe_search}*,"
                f"evidence_summary.ilike.*{safe_search}*,"
                f"source_name.ilike.*{safe_search}*)"
            )
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/gasket_profile_applications",
        params=params,
        headers=supabase_headers("count=exact"),
    )
    response.raise_for_status()
    total = parse_content_range_total(response.headers.get("content-range"))
    total_pages = max(1, (total + per_page - 1) // per_page) if total else 1
    return {
        "rows": response.json(),
        "total": total,
        "page": page_num,
        "per_page": per_page,
        "total_pages": total_pages,
        "query": query_text,
        "applied": [f"关键词：{query_text}"] if query_text else ["全部横截面适配关联"],
    }


def get_gasket_catalog_record(client: httpx.Client, catalog_id: int) -> dict | None:
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/gasket_profiles",
        params={"select": "*", "id": f"eq.{catalog_id}", "limit": "1"},
        headers=supabase_headers(),
    )
    response.raise_for_status()
    rows = response.json()
    return rows[0] if rows else None


def get_product_gasket_record(client: httpx.Client, record_id: int) -> dict | None:
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/gasket_profile_applications",
        params={
            "select": "*,gasket_profiles(*),gasket_finished_gaskets(*),refrigerator_products(*)",
            "id": f"eq.{record_id}",
            "limit": "1",
        },
        headers=supabase_headers(),
    )
    response.raise_for_status()
    rows = response.json()
    return rows[0] if rows else None


def get_catalog_applications(client: httpx.Client, catalog_id: int, limit: int = 50) -> list[dict]:
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/gasket_profile_applications",
        params={
            "select": "id,application_brand,application_model,application_door_position,application_label,evidence_summary,match_confidence,data_status,gasket_finished_gaskets(id,part_number,finished_gasket_code,dimensions_text,width_in,height_in,perimeter_in,top_side_in,bottom_side_in,left_side_in,right_side_in,dimensioned_gasket_image_url,price_usd),refrigerator_products(id,brand,equipment_model)",
            "gasket_profile_id": f"eq.{catalog_id}",
            "order": "match_confidence.desc.nullslast,id.desc",
            "limit": str(limit),
        },
        headers=supabase_headers(),
    )
    response.raise_for_status()
    return response.json()


def render_admin_gasket_catalog(catalog_page: dict) -> bytes:
    rows = []
    for item in catalog_page.get("rows") or []:
        dimensions = profile_dimension_summary(item)
        counts = f"成品 {item.get('tested_finished_gasket_count') or 0} / 关联 {item.get('linked_application_count') or 0}"
        rows.append(
            f"""<tr>
<td><a href="/ADMIN?gasket_catalog_id={esc(item.get('id'))}">#{esc(item.get('id'))}</a></td>
<td><strong>{esc(item.get('profile_code'))}</strong><br><span class="muted">{esc(item.get('profile_name'))}</span></td>
<td>{profile_visual(item)}</td>
<td>{esc(profile_type_zh(item.get('profile_type')))}<br><span class="muted">市场占比 {esc(item.get('estimated_market_share_pct'))}%</span></td>
<td>{esc(item.get('profile_style') or item.get('profile_family'))}<br><span class="muted">{esc(item.get('style_code'))}</span></td>
<td>{esc(dimensions)}</td>
<td>{esc(item.get('mounting_method'))}<br><span class="muted">{'磁性' if item.get('magnetic') else '非磁性或待确认'} / {esc(item.get('material'))} / {esc(item.get('color'))}</span></td>
<td>{esc(counts)}<br><span class="muted">{esc(item.get('common_use_cases'))}</span></td>
<td>{esc(item.get('source_name'))}<br><span class="muted">{esc(short_datetime(item.get('updated_at')))}</span></td>
</tr>"""
        )
    rows_html = "\n".join(rows) if rows else "<tr><td colspan='9'>没有找到密封条横截面记录。</td></tr>"
    query_text = catalog_page.get("query") or ""
    page_num = int(catalog_page.get("page") or 1)
    per_page = int(catalog_page.get("per_page") or 50)
    total = int(catalog_page.get("total") or 0)
    total_pages = int(catalog_page.get("total_pages") or 1)
    applied_text = "；".join(catalog_page.get("applied") or []) or "全部密封条横截面"
    pagination_html = admin_pagination_html("gasket_catalog", query_text, page_num, per_page, total, total_pages, applied_text)
    return page("后台密封条数据库", f"""
<style>
.admin-table{{width:100%;border-collapse:collapse;background:white}}
.admin-table th,.admin-table td{{border:1px solid #dbe2ea;padding:10px;text-align:left;vertical-align:top;font-size:13px}}
.admin-table th{{background:#f8fafc;color:#687385}}
.admin-filter-body{{border:1px solid #dbe2ea;border-radius:8px;background:#fff;padding:14px;display:grid;gap:8px;max-width:none;margin-bottom:12px}}
.admin-filter-body label{{font-size:13px;color:#687385}}
.admin-search-line{{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center}}
.admin-search-line input{{width:100%;box-sizing:border-box;border:1px solid #ccd6e2;border-radius:8px;padding:11px 12px;font-size:15px}}
.admin-search-button{{border:0;background:#0a6f78;color:#fff;border-radius:8px;min-height:42px;padding:0 18px;font-weight:700;cursor:pointer}}
.admin-page-size{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
.admin-page-size select{{border:1px solid #ccd6e2;border-radius:8px;padding:10px 12px;background:white}}
.admin-filter-help{{font-size:13px;color:#687385;line-height:1.55}}
.admin-pagination{{display:grid;gap:10px;background:#fff;border:1px solid #dbe2ea;border-radius:8px;padding:12px 14px;margin:0 0 12px}}
.admin-result-summary{{font-size:14px;color:#334155;line-height:1.5}}
.admin-page-controls{{display:flex;gap:7px;align-items:center;flex-wrap:wrap}}
.admin-page-link{{border:1px solid #ccd6e2;background:#f8fafc;color:#0d1f2a;border-radius:7px;padding:7px 10px;text-decoration:none;font-size:13px}}
.admin-page-link.active{{background:#0a6f78;color:#fff;border-color:#0a6f78;font-weight:800}}
.admin-page-link.disabled{{opacity:.45}}
.admin-page-gap{{color:#687385;padding:0 2px}}
.profile-visual-card{{width:190px;display:grid;gap:7px}}
.profile-visual-svg,.profile-visual-img{{width:170px;height:94px;border:1px solid #dbe2ea;border-radius:8px;background:#f8fafc;display:block;object-fit:contain}}
.profile-visual-meta{{display:grid;gap:2px;font-size:12px;line-height:1.25}}
.profile-visual-meta strong{{font-size:13px;color:#0d1f2a}}
.profile-visual-meta span{{color:#687385}}
</style>
<section><h2>后台密封条数据库</h2>
<p class="muted">这里保存的是密封条横截面本体：类型、样式、详细截面尺寸、制造说明和覆盖场景。成品尺寸与冰箱适配关系在关联数据库中管理。</p>
{admin_nav('gasket_catalog')}
<div class="admin-filter-body">
<form method="get" action="/ADMIN">
<input type="hidden" name="view" value="gasket_catalog">
<input type="hidden" name="page" value="1">
<label for="admin-gasket-catalog-search">搜索密封条数据库</label>
<div class="admin-search-line"><input id="admin-gasket-catalog-search" name="q" type="search" value="{esc(query_text)}" placeholder="输入 profile 编码、Dart、Push-in、样式、材质、颜色或用途"><button class="admin-search-button" type="submit">搜索</button></div>
<div class="admin-page-size"><span class="admin-filter-help">每页显示</span><select name="per_page" onchange="this.form.submit()">
<option value="25" {"selected" if per_page == 25 else ""}>25 条</option>
<option value="50" {"selected" if per_page == 50 else ""}>50 条</option>
<option value="100" {"selected" if per_page == 100 else ""}>100 条</option>
</select></div>
</form>
<div class="admin-filter-help">目标是沉淀 30-50 种高频横截面，覆盖美国商用冰箱维修市场的大部分需求。</div>
</div>
{pagination_html}
<table class="admin-table"><thead><tr><th>ID</th><th>Profile</th><th>样式图片</th><th>横截面类型</th><th>横截面样式</th><th>详细尺寸</th><th>安装/材料</th><th>成品/适配</th><th>来源/时间</th></tr></thead><tbody>{rows_html}</tbody></table>
{pagination_html}
</section>""")


def render_admin_product_gaskets(gaskets_page: dict) -> bytes:
    rows = []
    for item in gaskets_page.get("rows") or []:
        product = item.get("refrigerator_products") or {}
        profile = item.get("gasket_profiles") or {}
        finished = item.get("gasket_finished_gaskets") or {}
        dimensions = finished_gasket_dimension_summary(finished)
        side_dimensions = finished_gasket_side_summary(finished)
        product_label = f"{product.get('brand') or item.get('application_brand') or ''} {product.get('equipment_model') or item.get('application_model') or ''}".strip()
        rows.append(
            f"""<tr>
<td><a href="/ADMIN?product_gasket_id={esc(item.get('id'))}">#{esc(item.get('id'))}</a></td>
<td>{f'<a href="/ADMIN?product_id={esc(product.get("id"))}"><strong>{esc(product_label)}</strong></a>' if product.get('id') else f'<strong>{esc(product_label)}</strong>'}</td>
<td>{esc(item.get('application_label') or item.get('application_door_position'))}</td>
<td>{esc(finished.get('part_number') or finished.get('finished_gasket_code'))}<br><span class="muted">{esc(dimensions)}</span></td>
<td>{esc(side_dimensions)}<br><span class="muted">周长 {esc(finished.get('perimeter_in'))}\"</span></td>
<td>{finished_gasket_image_links(finished)}<br><span class="muted">{esc(finished.get('dimension_source'))} / {esc(finished.get('dimension_confidence_score'))}%</span></td>
<td><a href="/ADMIN?gasket_catalog_id={esc(profile.get('id'))}">{esc(profile.get('profile_code'))}</a><br><span class="muted">{esc(profile_type_zh(profile.get('profile_type')))}</span></td>
<td>{esc(profile.get('profile_style') or profile.get('profile_name'))}<br><span class="muted">{esc(profile.get('mounting_method'))}</span></td>
<td>{money(finished.get('price_usd'))}<br><span class="muted">{esc(item.get('match_confidence'))}% / {esc(zh_status(item.get('data_status')))}</span></td>
<td>{esc(item.get('source_name'))}<br><span class="muted">{esc(short_datetime(item.get('updated_at')))}</span></td>
</tr>"""
        )
    rows_html = "\n".join(rows) if rows else "<tr><td colspan='10'>没有找到关联数据库记录。</td></tr>"
    query_text = gaskets_page.get("query") or ""
    page_num = int(gaskets_page.get("page") or 1)
    per_page = int(gaskets_page.get("per_page") or 50)
    total = int(gaskets_page.get("total") or 0)
    total_pages = int(gaskets_page.get("total_pages") or 1)
    applied_text = "；".join(gaskets_page.get("applied") or []) or "全部横截面适配关联"
    pagination_html = admin_pagination_html("product_gaskets", query_text, page_num, per_page, total, total_pages, applied_text)
    return page("后台关联数据库", f"""
<style>
.admin-table{{width:100%;border-collapse:collapse;background:white}}
.admin-table th,.admin-table td{{border:1px solid #dbe2ea;padding:10px;text-align:left;vertical-align:top;font-size:13px}}
.admin-table th{{background:#f8fafc;color:#687385}}
.admin-filter-body{{border:1px solid #dbe2ea;border-radius:8px;background:#fff;padding:14px;display:grid;gap:8px;max-width:none;margin-bottom:12px}}
.admin-filter-body label{{font-size:13px;color:#687385}}
.admin-search-line{{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center}}
.admin-search-line input{{width:100%;box-sizing:border-box;border:1px solid #ccd6e2;border-radius:8px;padding:11px 12px;font-size:15px}}
.admin-search-button{{border:0;background:#0a6f78;color:#fff;border-radius:8px;min-height:42px;padding:0 18px;font-weight:700;cursor:pointer}}
.admin-page-size{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
.admin-page-size select{{border:1px solid #ccd6e2;border-radius:8px;padding:10px 12px;background:white}}
.admin-filter-help{{font-size:13px;color:#687385;line-height:1.55}}
.admin-pagination{{display:grid;gap:10px;background:#fff;border:1px solid #dbe2ea;border-radius:8px;padding:12px 14px;margin:0 0 12px}}
.admin-result-summary{{font-size:14px;color:#334155;line-height:1.5}}
.admin-page-controls{{display:flex;gap:7px;align-items:center;flex-wrap:wrap}}
.admin-page-link{{border:1px solid #ccd6e2;background:#f8fafc;color:#0d1f2a;border-radius:7px;padding:7px 10px;text-decoration:none;font-size:13px}}
.admin-page-link.active{{background:#0a6f78;color:#fff;border-color:#0a6f78;font-weight:800}}
.admin-page-link.disabled{{opacity:.45}}
.admin-page-gap{{color:#687385;padding:0 2px}}
</style>
<section><h2>后台关联数据库</h2>
<p class="muted">这里记录“某个横截面做成某个成品密封条，并适配某个冰箱型号/门位”。这是产品数据库和密封条数据库之间的桥。</p>
{admin_nav('product_gaskets')}
<div class="admin-filter-body">
<form method="get" action="/ADMIN">
<input type="hidden" name="view" value="product_gaskets">
<input type="hidden" name="page" value="1">
<label for="admin-product-gasket-search">搜索关联数据库</label>
<div class="admin-search-line"><input id="admin-product-gasket-search" name="q" type="search" value="{esc(query_text)}" placeholder="输入品牌、型号、门位、来源或证据关键词"><button class="admin-search-button" type="submit">搜索</button></div>
<div class="admin-page-size"><span class="admin-filter-help">每页显示</span><select name="per_page" onchange="this.form.submit()">
<option value="25" {"selected" if per_page == 25 else ""}>25 条</option>
<option value="50" {"selected" if per_page == 50 else ""}>50 条</option>
<option value="100" {"selected" if per_page == 100 else ""}>100 条</option>
</select></div>
</form>
<div class="admin-filter-help">同一种横截面可以做成很多不同长宽的成品密封条，也可以适配多个品牌型号。</div>
</div>
{pagination_html}
<table class="admin-table"><thead><tr><th>ID</th><th>冰箱型号</th><th>门位</th><th>成品密封条</th><th>四边尺寸</th><th>图片/尺寸来源</th><th>横截面</th><th>样式/安装</th><th>价格/匹配</th><th>来源/时间</th></tr></thead><tbody>{rows_html}</tbody></table>
{pagination_html}
</section>""")


def render_admin_gasket_catalog_detail(item: dict, applications: list[dict]) -> bytes:
    fields = [
        ("Profile 编码", item.get("profile_code")),
        ("Profile 名称", item.get("profile_name")),
        ("横截面类型", profile_type_zh(item.get("profile_type"))),
        ("横截面样式", item.get("profile_style") or item.get("profile_family")),
        ("样式编码", item.get("style_code")),
        ("详细尺寸", profile_dimension_summary(item)),
        ("安装方式", item.get("mounting_method")),
        ("磁性", "是" if item.get("magnetic") else "否/待确认"),
        ("材料", item.get("material")),
        ("颜色", item.get("color")),
        ("市场占比", f"{item.get('estimated_market_share_pct')}%" if item.get("estimated_market_share_pct") is not None else ""),
        ("已做成品数量", item.get("tested_finished_gasket_count")),
        ("已关联应用数量", item.get("linked_application_count")),
        ("常见用途", item.get("common_use_cases")),
        ("覆盖说明", item.get("coverage_notes")),
        ("制造说明", item.get("manufacturing_notes")),
        ("置信度", item.get("confidence_score")),
        ("状态", zh_status(item.get("data_status"))),
        ("来源", item.get("source_name")),
        ("来源链接", item.get("source_url")),
    ]
    field_rows = "".join([f"<div>{esc(label)}</div><div><strong>{esc(value)}</strong></div>" for label, value in fields])
    app_rows = []
    for row in applications:
        product = row.get("refrigerator_products") or {}
        finished = row.get("gasket_finished_gaskets") or {}
        product_label = f"{product.get('brand') or row.get('application_brand') or ''} {product.get('equipment_model') or row.get('application_model') or ''}".strip()
        app_rows.append(
            f"""<tr><td><a href="/ADMIN?product_gasket_id={esc(row.get('id'))}">#{esc(row.get('id'))}</a></td><td>{f'<a href="/ADMIN?product_id={esc(product.get("id"))}">{esc(product_label)}</a>' if product.get('id') else esc(product_label)}</td><td>{esc(row.get('application_label') or row.get('application_door_position'))}</td><td>{esc(finished.get('part_number') or finished.get('finished_gasket_code'))}</td><td>{esc(finished_gasket_dimension_summary(finished))}</td><td>{esc(finished_gasket_side_summary(finished))}</td><td>{finished_gasket_image_links(finished)}</td><td>{esc(row.get('match_confidence'))}%</td></tr>"""
        )
    app_rows_html = "".join(app_rows) if app_rows else "<tr><td colspan='8'>暂无冰箱门位适配关联。</td></tr>"
    return page("密封条数据库详情", f"""
<style>
pre{{white-space:pre-wrap;max-height:260px;overflow:auto;background:#f8fafc;border:1px solid #dbe2ea;border-radius:6px;padding:8px}}
.admin-table{{width:100%;border-collapse:collapse;background:white}}
.admin-table th,.admin-table td{{border:1px solid #dbe2ea;padding:10px;text-align:left;vertical-align:top;font-size:13px}}
.admin-table th{{background:#f8fafc;color:#687385}}
</style>
<section>{admin_nav('gasket_catalog')}<h2>密封条横截面 #{esc(item.get('id'))}</h2><div class="facts">{field_rows}</div></section>
<section><h2>这个横截面做过哪些冰箱密封条</h2><table class="admin-table"><thead><tr><th>关联ID</th><th>冰箱型号</th><th>门位</th><th>成品/配件号</th><th>宽高尺寸</th><th>四边尺寸</th><th>图片</th><th>匹配分</th></tr></thead><tbody>{app_rows_html}</tbody></table></section>
<section><h2>完整原始记录</h2><pre>{esc(compact_json(item))}</pre></section>""")


def render_admin_product_gasket_detail(item: dict) -> bytes:
    product = item.get("refrigerator_products") or {}
    profile = item.get("gasket_profiles") or {}
    finished = item.get("gasket_finished_gaskets") or {}
    product_label = f"{product.get('brand') or item.get('application_brand') or ''} {product.get('equipment_model') or item.get('application_model') or ''}".strip()
    fields = [
        ("冰箱型号", product_label),
        ("产品ID", item.get("refrigerator_product_id")),
        ("门位", item.get("application_label") or item.get("application_door_position")),
        ("成品密封条", finished.get("part_number") or finished.get("finished_gasket_code")),
        ("成品尺寸", finished_gasket_dimension_summary(finished)),
        ("上边尺寸", finished.get("top_side_in")),
        ("下边尺寸", finished.get("bottom_side_in")),
        ("左边尺寸", finished.get("left_side_in")),
        ("右边尺寸", finished.get("right_side_in")),
        ("内宽", finished.get("inner_width_in")),
        ("内高", finished.get("inner_height_in")),
        ("外宽", finished.get("outer_width_in")),
        ("外高", finished.get("outer_height_in")),
        ("周长", finished.get("perimeter_in")),
        ("焊角类型", finished.get("corner_type")),
        ("焊点数量", finished.get("weld_count")),
        ("成品颜色", finished.get("color")),
        ("密封条图片", finished.get("gasket_image_url")),
        ("尺寸标注图片", finished.get("dimensioned_gasket_image_url")),
        ("横截面图片", finished.get("profile_diagram_image_url")),
        ("尺寸来源", finished.get("dimension_source")),
        ("尺寸置信度", finished.get("dimension_confidence_score")),
        ("成品价格样本", money(finished.get("price_usd"))),
        ("横截面ID", item.get("gasket_profile_id")),
        ("横截面编码", profile.get("profile_code")),
        ("横截面类型", profile_type_zh(profile.get("profile_type"))),
        ("横截面样式", profile.get("profile_style") or profile.get("profile_name")),
        ("安装方式", profile.get("mounting_method")),
        ("匹配分", item.get("match_confidence")),
        ("状态", zh_status(item.get("data_status"))),
        ("证据摘要", item.get("evidence_summary")),
        ("来源", item.get("source_name")),
        ("来源链接", item.get("source_url")),
    ]
    field_rows = "".join([f"<div>{esc(label)}</div><div><strong>{esc(value)}</strong></div>" for label, value in fields])
    product_link = f'<a class="button" href="/ADMIN?product_id={esc(product.get("id"))}">查看产品</a>' if product.get("id") else ""
    profile_link = f'<a class="button" href="/ADMIN?gasket_catalog_id={esc(profile.get("id"))}">查看密封条数据库</a>' if profile.get("id") else ""
    return page("关联数据库详情", f"""
<style>
pre{{white-space:pre-wrap;max-height:260px;overflow:auto;background:#f8fafc;border:1px solid #dbe2ea;border-radius:6px;padding:8px}}
</style>
<section>{admin_nav('product_gaskets')}<h2>关联数据库 #{esc(item.get('id'))}</h2><div class="facts">{field_rows}</div>
<p>{product_link} {profile_link}</p></section>
<section><h2>产品快照</h2><pre>{esc(compact_json(product))}</pre></section>
<section><h2>横截面快照</h2><pre>{esc(compact_json(profile))}</pre></section>
<section><h2>成品密封条快照</h2><pre>{esc(compact_json(finished))}</pre></section>
<section><h2>完整原始记录</h2><pre>{esc(compact_json(item))}</pre></section>""")


def render_admin_login(message: str = "") -> bytes:
    warning = f"<p style='color:#9f4b12'>{esc(message)}</p>" if message else ""
    config_note = "" if ADMIN_PASSWORD else "<p style='color:#9f4b12'>后台密码尚未配置。</p>"
    return page("后台登录", f"""
<section style="max-width:520px;margin:0 auto"><h2>后台登录</h2>
<p class="muted">工作人员查看订单、生产资料、产品证据和数据库。</p>
{warning}{config_note}
<form method="post" action="/ADMIN/login">
<label>密码</label><input type="password" name="password" autocomplete="current-password" autofocus>
<p><button type="submit">进入后台</button> <a class="button" href="/">返回网站</a></p>
</form></section>""")


def render_admin_product(product: dict, package: dict | None, items: list[dict], quote_items: list[dict]) -> bytes:
    fields = [
        ("品牌", product.get("brand")),
        ("型号", product.get("equipment_model")),
        ("生产厂家", product.get("manufacturer")),
        ("产品类型", product.get("product_type")),
        ("门数", product.get("door_count")),
        ("门位结构", product.get("door_layout")),
        ("生命周期", product.get("lifecycle_status")),
        ("资料状态", zh_status(product.get("data_status"))),
        ("资料置信度", product.get("data_confidence")),
        ("创建时间", product.get("created_at")),
        ("更新时间", product.get("updated_at")),
        ("最后发现时间", product.get("last_discovered_at")),
        ("最后补全时间", product.get("last_enriched_at")),
        ("门位更新时间", product.get("door_layout_updated_at")),
        ("图片置信度", product.get("product_image_confidence")),
        ("图片来源", product.get("product_image_source_url")),
        ("官网链接", product.get("official_product_url")),
        ("手册链接", product.get("manual_url")),
        ("规格书链接", product.get("spec_sheet_url")),
    ]
    field_rows = "".join([f"<div>{esc(label)}</div><div><strong>{esc(value)}</strong></div>" for label, value in fields])
    image = product.get("product_image_url")
    image_html = f"<img class='photo' src='{esc(image)}' alt='产品图片'>" if image else "<div class='photo loading'>缺少产品图片</div>"
    package_html = render_evidence_package(package or {}) if package else "<section><h2>产品资料证据包</h2><p class='muted'>暂无证据包。</p></section>"
    item_rows = []
    for item in items:
        item_rows.append(
            f"""<tr><td>{esc(item.get('field_name'))}</td><td>{esc(item.get('evidence_type'))}</td><td>{esc(item.get('source_name'))}<br><span class="muted">{esc(item.get('source_url'))}</span></td><td>{esc(item.get('supports_value'))}</td><td>{esc(item.get('confidence_score'))}%</td><td><pre>{esc(compact_json(item.get('evidence_json')))}</pre></td></tr>"""
        )
    quote_rows = []
    for quote in quote_items:
        quote_rows.append(
            f"""<tr><td>{esc(quote.get('door_position_display') or quote.get('door_position'))}</td><td>{esc(quote.get('part_number') or quote.get('universal_part_number'))}</td><td>{esc(quote.get('dimensions_text'))}</td><td>{esc(quote.get('confidence_score'))}%</td><td>{money(quote.get('final_price_usd'))}</td><td>{esc(quote.get('source_name'))}</td></tr>"""
        )
    item_rows_html = "".join(item_rows) if item_rows else "<tr><td colspan='6'>暂无证据明细。</td></tr>"
    quote_rows_html = "".join(quote_rows) if quote_rows else "<tr><td colspan='6'>暂无密封条报价记录。</td></tr>"
    return page("后台产品详情", f"""
<style>
pre{{white-space:pre-wrap;max-height:180px;overflow:auto;background:#f8fafc;border:1px solid #dbe2ea;border-radius:6px;padding:8px}}
.admin-table{{width:100%;border-collapse:collapse;background:white}}
.admin-table th,.admin-table td{{border:1px solid #dbe2ea;padding:10px;text-align:left;vertical-align:top;font-size:13px}}
.admin-table th{{background:#f8fafc;color:#687385}}
</style>
<section>{admin_nav('products')}<p><a class="button" href="/ADMIN?view=products">返回产品数据库</a> <a class="button" href="/preview?product_id={esc(product.get('id'))}">客户预览页</a></p>
<h2>{esc(product.get('brand'))} {esc(product.get('equipment_model'))}</h2><div class="result-grid"><div>{image_html}</div><div class="facts">{field_rows}</div></div></section>
{package_html}
<section><h2>证据明细</h2><table class="admin-table"><thead><tr><th>字段</th><th>类型</th><th>来源</th><th>支持的值</th><th>置信度</th><th>JSON</th></tr></thead><tbody>{item_rows_html}</tbody></table></section>
<section><h2>密封条记录</h2><table class="admin-table"><thead><tr><th>门位</th><th>配件号</th><th>尺寸</th><th>置信度</th><th>价格</th><th>来源</th></tr></thead><tbody>{quote_rows_html}</tbody></table></section>""")


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

    def send_pdf(self, data: bytes, filename: str, status: int = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location: str, cookie: str | None = None) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_html(render_home())
            return
        if parsed.path == "/public-stats":
            data = {}
            try:
                with httpx.Client(timeout=8) as client:
                    data = get_home_database_stats(client)
            except Exception:
                data = {"product_total": 0, "quote_items": 0, "known_profiles": 0}
            payload = json.dumps(data).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path.lower() == "/admin":
            if not is_admin_authenticated(self.headers.get("Cookie")):
                self.send_html(render_admin_login())
                return
            query = parse_qs(parsed.query)
            order_id = int(query.get("order_id", ["0"])[0] or "0")
            product_id = int(query.get("product_id", ["0"])[0] or "0")
            gasket_catalog_id = int(query.get("gasket_catalog_id", ["0"])[0] or "0")
            product_gasket_id = int(query.get("product_gasket_id", ["0"])[0] or "0")
            view = (query.get("view", ["orders"])[0] or "orders").lower()
            with httpx.Client(timeout=30) as client:
                if order_id:
                    order = get_customer_order(client, order_id)
                    if not order:
                        self.send_html(page("后台订单不存在", "<section><h2>订单不存在</h2><p><a class='button' href='/ADMIN'>返回订单列表</a></p></section>"), HTTPStatus.NOT_FOUND)
                        return
                    product = get_product(client, int(order["refrigerator_product_id"])) if order.get("refrigerator_product_id") else None
                    request = None
                    if order.get("gasket_request_id"):
                        request_response = client.get(
                            f"{SUPABASE_URL}/rest/v1/gasket_requests",
                            params={"select": "*", "id": f"eq.{order['gasket_request_id']}", "limit": "1"},
                            headers=supabase_headers(),
                        )
                        request_response.raise_for_status()
                        request_rows = request_response.json()
                        request = request_rows[0] if request_rows else None
                    current_quote_items = get_quote_items(client, int(order["refrigerator_product_id"])) if order.get("refrigerator_product_id") else []
                    self.send_html(render_admin_order(order, product, request, current_quote_items))
                    return
                if product_id:
                    product = get_product(client, product_id)
                    if not product:
                        self.send_html(page("后台产品不存在", "<section><h2>产品不存在</h2><p><a class='button' href='/ADMIN?view=products'>返回产品数据库</a></p></section>"), HTTPStatus.NOT_FOUND)
                        return
                    self.send_html(
                        render_admin_product(
                            product,
                            get_evidence_package(client, product_id),
                            get_evidence_items(client, product_id),
                            get_quote_items(client, product_id),
                        )
                    )
                    return
                if gasket_catalog_id:
                    catalog_record = get_gasket_catalog_record(client, gasket_catalog_id)
                    if not catalog_record:
                        self.send_html(page("密封条数据库不存在", "<section><h2>密封条数据库不存在</h2><p><a class='button' href='/ADMIN?view=gasket_catalog'>返回密封条数据库</a></p></section>"), HTTPStatus.NOT_FOUND)
                        return
                    self.send_html(render_admin_gasket_catalog_detail(catalog_record, get_catalog_applications(client, gasket_catalog_id)))
                    return
                if product_gasket_id:
                    gasket_record = get_product_gasket_record(client, product_gasket_id)
                    if not gasket_record:
                        self.send_html(page("关联数据库不存在", "<section><h2>关联数据库不存在</h2><p><a class='button' href='/ADMIN?view=product_gaskets'>返回关联数据库</a></p></section>"), HTTPStatus.NOT_FOUND)
                        return
                    self.send_html(render_admin_product_gasket_detail(gasket_record))
                    return
                if view == "products":
                    raw_product_query = (query.get("q") or [""])[0].strip()
                    try:
                        product_page = int((query.get("page") or ["1"])[0] or "1")
                    except ValueError:
                        product_page = 1
                    try:
                        product_per_page = int((query.get("per_page") or ["50"])[0] or "50")
                    except ValueError:
                        product_per_page = 50
                    self.send_html(
                        render_admin_dashboard(
                            get_admin_products_page(client, raw_product_query, product_page, product_per_page),
                            get_database_stats(client),
                        )
                    )
                    return
                if view == "gasket_catalog":
                    raw_gasket_query = (query.get("q") or [""])[0].strip()
                    try:
                        gasket_page = int((query.get("page") or ["1"])[0] or "1")
                    except ValueError:
                        gasket_page = 1
                    try:
                        gasket_per_page = int((query.get("per_page") or ["50"])[0] or "50")
                    except ValueError:
                        gasket_per_page = 50
                    self.send_html(render_admin_gasket_catalog(get_admin_gasket_catalog_page(client, raw_gasket_query, gasket_page, gasket_per_page)))
                    return
                if view == "product_gaskets":
                    raw_gasket_query = (query.get("q") or [""])[0].strip()
                    try:
                        gasket_page = int((query.get("page") or ["1"])[0] or "1")
                    except ValueError:
                        gasket_page = 1
                    try:
                        gasket_per_page = int((query.get("per_page") or ["50"])[0] or "50")
                    except ValueError:
                        gasket_per_page = 50
                    self.send_html(render_admin_product_gaskets(get_admin_product_gaskets_page(client, raw_gasket_query, gasket_page, gasket_per_page)))
                    return
                self.send_html(render_admin_orders_dashboard(get_recent_customer_orders(client)))
            return
        if parsed.path.lower() == "/admin/logout":
            self.redirect("/", f"{ADMIN_COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
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
        if parsed.path.startswith("/static/"):
            target = (ROOT / parsed.path.lstrip("/")).resolve()
            if not str(target).startswith(str((ROOT / "static").resolve())) or not target.exists():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data = target.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream")
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
        if parsed.path == "/quote-pdf":
            product_id = int(parse_qs(parsed.query).get("product_id", ["0"])[0] or "0")
            with httpx.Client(timeout=30) as client:
                product = get_product(client, product_id) if product_id else None
                if not product:
                    self.send_html(page("Product Not Found", "<section><h2>Product not found</h2><p><a class='button' href='/'>Start a new match</a></p></section>"), HTTPStatus.NOT_FOUND)
                    return
                quote_items = get_quote_items(client, product_id)
                request = get_latest_request_for_product(client, product_id)
            self.send_pdf(render_quote_pdf(product, quote_items, request), pdf_filename(product))
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
        if path.lower() == "/admin/login":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            fields = parse_qs(body)
            password = (fields.get("password") or [""])[0]
            if ADMIN_PASSWORD and hmac.compare_digest(password, ADMIN_PASSWORD):
                cookie = f"{ADMIN_COOKIE_NAME}={make_admin_cookie()}; Path=/; Max-Age={ADMIN_SESSION_SECONDS}; HttpOnly; SameSite=Lax"
                self.redirect("/ADMIN", cookie)
                return
            self.send_html(render_admin_login("密码错误。"), HTTPStatus.UNAUTHORIZED)
            return
        if path == "/checkout":
            handle_checkout_post(self, self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            return
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
                nameplate_data = fallback_nameplate_data(exc, brand, model)
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
                from instant_enrichment import start_instant_enrichment, upsert_known_product_from_nameplate, wait_for_customer_result

                product = upsert_known_product_from_nameplate(client, brand, model, nameplate_data, status="customer_confirmed")
                start_instant_enrichment(product["id"], nameplate_data)
                waited = wait_for_customer_result(
                    product["id"],
                    max_seconds=float(os.getenv("CUSTOMER_ENRICH_WAIT_SECONDS", "25")),
                )
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
            quote_items = get_quote_items(client, product["id"])
            try:
                from product_evidence import build_evidence_package, persist_evidence_package

                evidence_package = build_evidence_package(product, quote_items, nameplate_data, "customer_confirmed")
                persist_evidence_package(client, evidence_package)
            except Exception as exc:
                print(f"evidence package persistence skipped for {brand} {model}: {exc}", flush=True)
            self.send_html(render_result(product, quote_items, request, upload_url))


def main() -> None:
    port = int(os.getenv("PORT") or os.getenv("CUSTOMER_DEMO_PORT", "8010"))
    print(f"Nameplate web app running on port {port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
