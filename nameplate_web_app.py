import html
import json
import os
import re
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
SHOPIFY_STORE_DOMAIN = os.getenv("SHOPIFY_STORE_DOMAIN", "").strip()

ROOT = Path(__file__).resolve().parent
UPLOAD_DIR = ROOT / "uploads" / "customer_nameplates"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def esc(value) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def money(value) -> str:
    if value in (None, ""):
        return "TBD"
    return f"${float(value):,.2f}"


def supabase_headers(prefer: str | None = None) -> dict[str, str]:
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def normalize_model(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def parse_multipart(body: bytes, content_type: str) -> dict[str, dict]:
    match = re.search(r"boundary=(?P<boundary>[^;]+)", content_type or "")
    if not match:
        return {}
    boundary = ("--" + match.group("boundary").strip('"')).encode()
    fields: dict[str, dict] = {}
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
        name = name_match.group(1)
        fields[name] = {
            "filename": filename_match.group(1) if filename_match else "",
            "data": data,
            "text": data.decode("utf-8", errors="ignore").strip(),
        }
    return fields


def find_product(client: httpx.Client, brand: str, model: str) -> dict | None:
    brand_q = (brand or "").replace("*", "")
    model_q = (model or "").replace("*", "")
    endpoint = (
        f"{SUPABASE_URL}/rest/v1/refrigerator_products"
        "?select=*"
        f"&brand=ilike.*{brand_q}*"
        f"&equipment_model=ilike.*{model_q}*"
        "&limit=20"
    )
    response = client.get(endpoint, headers=supabase_headers())
    response.raise_for_status()
    rows = response.json()
    if not rows:
        return None
    wanted = normalize_model(model)
    for row in rows:
        if normalize_model(row.get("equipment_model", "")) == wanted:
            return row
    return rows[0]


def get_product(client: httpx.Client, product_id: int) -> dict | None:
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/refrigerator_products?select=*&id=eq.{product_id}&limit=1",
        headers=supabase_headers(),
    )
    response.raise_for_status()
    rows = response.json()
    return rows[0] if rows else None


def get_quote_items(client: httpx.Client, product_id: int) -> list[dict]:
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/refrigerator_product_quote_items"
        f"?select=*&refrigerator_product_id=eq.{product_id}&order=confidence_score.desc.nullslast",
        headers=supabase_headers(),
    )
    response.raise_for_status()
    return response.json()


def create_request(
    client: httpx.Client,
    customer_name: str | None,
    customer_email: str | None,
    customer_phone: str | None,
    upload_url: str | None,
    brand: str,
    model: str,
    product: dict | None,
) -> dict:
    payload = {
        "customer_name": customer_name,
        "customer_email": customer_email,
        "customer_phone": customer_phone,
        "nameplate_image_url": upload_url,
        "ocr_text": f"Demo input: {brand} {model}",
        "detected_brand": brand,
        "detected_model": model,
        "matched_refrigerator_product_id": product.get("id") if product else None,
        "match_score": 100 if product else 0,
        "status": "matched" if product else "needs_review",
    }
    response = client.post(
        f"{SUPABASE_URL}/rest/v1/gasket_requests",
        headers=supabase_headers("return=representation"),
        json=payload,
    )
    response.raise_for_status()
    rows = response.json()
    return rows[0] if rows else payload


def shopify_cart_url(items: list[dict]) -> str:
    variants = []
    for item in items:
        variant = item.get("shopify_variant_id")
        if variant:
            variants.append(f"{variant}:1")
    if not variants or not SHOPIFY_STORE_DOMAIN:
        return ""
    domain = SHOPIFY_STORE_DOMAIN.replace("https://", "").replace("http://", "").strip("/")
    return f"https://{domain}/cart/{','.join(variants)}"


def render_page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)}</title>
  <style>
    :root {{
      --ink:#17202a; --muted:#687385; --line:#dbe2ea; --soft:#f4f7fa;
      --brand:#0a6f78; --brand-dark:#07555c; --paper:#fff; --warn:#9f4b12;
    }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:Arial, Helvetica, sans-serif; color:var(--ink); background:#eef3f6; }}
    header {{ background:#0f1d24; color:white; padding:22px 28px; }}
    header strong {{ display:block; font-size:22px; }}
    header span {{ color:#c9d5dd; font-size:14px; }}
    main {{ max-width:1180px; margin:0 auto; padding:22px; }}
    section {{ background:var(--paper); border:1px solid var(--line); border-radius:8px; padding:20px; margin-bottom:18px; }}
    h1 {{ margin:0 0 8px; font-size:34px; letter-spacing:0; }}
    h2 {{ margin:0 0 14px; font-size:20px; }}
    h3 {{ margin:0 0 8px; font-size:13px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }}
    p {{ color:var(--muted); line-height:1.55; }}
    .hero {{ display:grid; grid-template-columns:1.05fr .95fr; gap:22px; align-items:center; }}
    .upload {{ border:1px solid var(--line); background:var(--soft); border-radius:8px; padding:16px; }}
    .grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }}
    .result-grid {{ display:grid; grid-template-columns:1fr 240px 1fr; gap:18px; align-items:start; }}
    label {{ display:block; font-size:13px; color:var(--muted); margin-bottom:6px; }}
    input {{ width:100%; border:1px solid var(--line); border-radius:6px; padding:10px; font:inherit; background:white; }}
    button,.button {{ border:0; border-radius:6px; background:var(--brand); color:white; min-height:40px; padding:0 16px; font-weight:700; cursor:pointer; text-decoration:none; display:inline-flex; align-items:center; justify-content:center; }}
    button:hover,.button:hover {{ background:var(--brand-dark); }}
    .secondary {{ background:#25313a; }}
    .muted {{ color:var(--muted); }}
    .warning {{ color:var(--warn); }}
    .photo {{ width:100%; height:320px; object-fit:contain; border:1px solid var(--line); border-radius:8px; background:#f8fafc; }}
    .plate {{ width:100%; height:190px; object-fit:contain; border:1px solid var(--line); border-radius:8px; background:#f8fafc; }}
    .facts {{ display:grid; grid-template-columns:130px 1fr; gap:8px 12px; }}
    .facts div:nth-child(odd) {{ color:var(--muted); }}
    .summary {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; }}
    .metric {{ border:1px solid var(--line); border-radius:8px; padding:12px; background:#fbfdfe; }}
    .metric span {{ display:block; color:var(--muted); font-size:12px; }}
    .metric strong {{ font-size:24px; }}
    .items {{ display:grid; gap:12px; }}
    .item {{ display:grid; grid-template-columns:34px 98px 1fr 150px 120px; gap:12px; align-items:center; border:1px solid var(--line); border-radius:8px; padding:12px; background:white; }}
    .item img {{ width:98px; height:78px; object-fit:contain; border:1px solid var(--line); border-radius:6px; background:#fafafa; }}
    .price strong {{ display:block; font-size:24px; }}
    .price small {{ color:var(--muted); }}
    .checkout {{ position:sticky; bottom:0; background:rgba(238,243,246,.94); border:1px solid var(--line); border-radius:8px; padding:12px; display:flex; justify-content:space-between; gap:12px; align-items:center; }}
    @media(max-width:860px) {{ .hero,.result-grid,.grid,.summary,.item {{ grid-template-columns:1fr; }} .item {{ align-items:start; }} }}
  </style>
</head>
<body>
  <header><strong>Refrigerator Door Gasket Match</strong><span>Upload a nameplate, confirm the model, and quote the right gasket.</span></header>
  <main>{body}</main>
  <script>
    function updateTotal() {{
      let total = 0;
      let selected = 0;
      document.querySelectorAll('[data-price]').forEach(function(box) {{
        if (box.checked) {{
          total += Number(box.dataset.price || 0);
          selected += 1;
        }}
      }});
      const totalEl = document.getElementById('selected-total');
      const countEl = document.getElementById('selected-count');
      if (totalEl) totalEl.textContent = '$' + total.toFixed(2);
      if (countEl) countEl.textContent = selected;
    }}
    document.addEventListener('change', updateTotal);
    window.addEventListener('load', updateTotal);
  </script>
</body>
</html>""".encode("utf-8")


def render_home(message: str = "") -> bytes:
    msg = f"<p class='warning'>{esc(message)}</p>" if message else ""
    body = f"""
<section class="hero">
  <div>
    <h1>Find the Right Refrigerator Door Gasket Fast</h1>
    <p>Upload the equipment nameplate or type the brand and model. The demo reads the live database, returns the refrigerator summary, and builds selectable gasket quote items.</p>
    <div class="summary">
      <div class="metric"><span>Step 1</span><strong>Upload</strong></div>
      <div class="metric"><span>Step 2</span><strong>Match</strong></div>
      <div class="metric"><span>Step 3</span><strong>Pay</strong></div>
    </div>
  </div>
  <form class="upload" method="post" action="/match" enctype="multipart/form-data">
    <h2>Upload nameplate</h2>
    {msg}
    <div class="grid">
      <div><label>Nameplate photo</label><input type="file" name="nameplate" accept="image/*"></div>
      <div><label>Customer name</label><input name="customer_name" placeholder="Restaurant or customer"></div>
      <div><label>Brand</label><input name="brand" placeholder="Sub-Zero" value="Sub-Zero"></div>
      <div><label>Model</label><input name="equipment_model" placeholder="685/S/2" value="685/S/2"></div>
      <div><label>Email</label><input name="customer_email" placeholder="customer@email.com"></div>
      <div><label>Phone</label><input name="customer_phone" placeholder="Phone number"></div>
    </div>
    <div style="margin-top:14px"><button type="submit">Match gasket</button></div>
    <p class="muted">Demo note: real OCR will replace manual brand/model input later.</p>
  </form>
</section>
"""
    return render_page("Gasket Match Demo", body)


def render_no_match(brand: str, model: str, upload_url: str | None) -> bytes:
    plate = f"<img class='plate' src='{esc(upload_url)}' alt='Uploaded nameplate'>" if upload_url else ""
    body = f"""
<section>
  <h2>We need to review this model</h2>
  <p>No database product matched <strong>{esc(brand)} {esc(model)}</strong>. The request can still be saved for manual review.</p>
  {plate}
  <p><a class="button" href="/">Try another nameplate</a></p>
</section>
"""
    return render_page("No match", body)


def render_result(product: dict, quote_items: list[dict], request: dict | None, upload_url: str | None) -> bytes:
    product_image = product.get("product_image_url")
    product_image_html = (
        f"<img class='photo' src='{esc(product_image)}' alt='Refrigerator product image'>"
        if product_image
        else "<div class='photo' style='display:flex;align-items:center;justify-content:center;color:#687385'>Product image pending</div>"
    )
    plate_html = (
        f"<img class='plate' src='{esc(upload_url)}' alt='Uploaded nameplate'>"
        if upload_url
        else "<div class='plate' style='display:flex;align-items:center;justify-content:center;color:#687385'>Nameplate photo</div>"
    )
    item_rows = []
    selected_for_shopify = []
    for index, item in enumerate(quote_items):
        price = float(item.get("final_price_usd") or 0)
        market = item.get("market_price_usd")
        checked = "checked" if index == 0 else ""
        if item.get("shopify_variant_id"):
            selected_for_shopify.append(item)
        image = item.get("gasket_image_url")
        image_html = f"<img src='{esc(image)}' alt='Gasket image'>" if image else "<div class='muted'>No gasket image</div>"
        title = item.get("gasket_name") or "Door gasket"
        dims = item.get("dimensions_text") or f"{item.get('width_in') or '-'} x {item.get('height_in') or '-'} in"
        item_rows.append(
            f"""
            <label class="item">
              <input type="checkbox" data-price="{esc(price)}" {checked}>
              {image_html}
              <div>
                <strong>{esc(title)}</strong>
                <p>{esc(dims)}<br>Perimeter: {esc(item.get('perimeter_in') or 'TBD')} in<br>Source: {esc(item.get('source_name'))}</p>
              </div>
              <div class="price">
                <strong>{money(price)}</strong>
                <small>{'Market: ' + money(market) if market else 'Calculated by size'}</small>
              </div>
              <div>
                <small class="muted">Part</small><br>
                <strong>{esc(item.get('part_number') or item.get('universal_part_number') or 'TBD')}</strong>
              </div>
            </label>
            """
        )
    shopify_url = shopify_cart_url(selected_for_shopify)
    checkout = (
        f"<a class='button secondary' href='{esc(shopify_url)}'>Pay with Shopify</a>"
        if shopify_url
        else "<button class='secondary' type='button' disabled title='Add Shopify variant IDs to enable checkout'>Shopify link pending</button>"
    )
    body = f"""
<section>
  <h2>Matched refrigerator</h2>
  <div class="result-grid">
    <div>
      <h3>Refrigerator image</h3>
      {product_image_html}
    </div>
    <div>
      <h3>Nameplate</h3>
      {plate_html}
    </div>
    <div>
      <h3>Nameplate summary</h3>
      <div class="facts">
        <div>Brand</div><div><strong>{esc(product.get('brand'))}</strong></div>
        <div>Model</div><div><strong>{esc(product.get('equipment_model'))}</strong></div>
        <div>Manufacturer</div><div>{esc(product.get('manufacturer'))}</div>
        <div>Production date</div><div>{esc(product.get('manufacture_date_text') or product.get('manufacture_date') or 'Pending')}</div>
        <div>Request ID</div><div>{esc(request.get('id') if request else 'Preview')}</div>
      </div>
    </div>
  </div>
</section>
<section>
  <h2>Gasket quote</h2>
  <div class="summary">
    <div class="metric"><span>Required gaskets</span><strong>{len(quote_items)}</strong></div>
    <div class="metric"><span>Selected</span><strong id="selected-count">0</strong></div>
    <div class="metric"><span>Total</span><strong id="selected-total">$0.00</strong></div>
  </div>
  <div style="height:14px"></div>
  <div class="items">{''.join(item_rows) if item_rows else '<p class=\"muted\">No quote items yet.</p>'}</div>
</section>
<div class="checkout">
  <div><strong>Ready to order?</strong><br><span class="muted">Select one gasket or all required gaskets.</span></div>
  {checkout}
</div>
"""
    return render_page("Matched Gasket Quote", body)


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
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/preview":
            params = parse_qs(parsed.query)
            product_id = int(params.get("product_id", ["3093"])[0])
            with httpx.Client(timeout=30) as client:
                product = get_product(client, product_id)
                if not product:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                quote_items = get_quote_items(client, product_id)
            self.send_html(render_result(product, quote_items, None, None))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/match":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        fields = parse_multipart(body, self.headers.get("Content-Type", ""))
        if not fields:
            fields = {k: {"text": v[0], "filename": "", "data": b""} for k, v in parse_qs(body.decode("utf-8", errors="ignore")).items()}
        brand = fields.get("brand", {}).get("text", "").strip()
        model = fields.get("equipment_model", {}).get("text", "").strip()
        if not brand or not model:
            self.send_html(render_home("Please enter brand and model for this demo."), HTTPStatus.BAD_REQUEST)
            return
        upload_url = None
        file_field = fields.get("nameplate")
        if file_field and file_field.get("filename") and file_field.get("data"):
            suffix = Path(file_field["filename"]).suffix or ".jpg"
            saved_name = f"{uuid.uuid4().hex}{suffix}"
            saved_path = UPLOAD_DIR / saved_name
            saved_path.write_bytes(file_field["data"])
            upload_url = f"/uploads/customer_nameplates/{saved_name}"
        with httpx.Client(timeout=30) as client:
            product = find_product(client, brand, model)
            request = create_request(
                client,
                fields.get("customer_name", {}).get("text") or None,
                fields.get("customer_email", {}).get("text") or None,
                fields.get("customer_phone", {}).get("text") or None,
                upload_url,
                brand,
                model,
                product,
            )
            if not product:
                self.send_html(render_no_match(brand, model, upload_url))
                return
            quote_items = get_quote_items(client, product["id"])
        self.send_html(render_result(product, quote_items, request, upload_url))


def main() -> None:
    port = int(os.getenv("PORT") or os.getenv("CUSTOMER_DEMO_PORT", "8010"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Customer homepage demo running at http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
