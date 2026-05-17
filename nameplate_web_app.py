import html
import os
import re
import shutil
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote_plus, urlparse

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name('.env'))

SUPABASE_URL = os.environ['SUPABASE_URL'].rstrip('/')
SUPABASE_SERVICE_ROLE_KEY = os.environ['SUPABASE_SERVICE_ROLE_KEY']
ROOT = Path(__file__).resolve().parent
UPLOAD_DIR = ROOT / 'uploads' / 'nameplates'
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def esc(value):
    return html.escape('' if value is None else str(value), quote=True)


def headers(prefer=None):
    data = {
        'apikey': SUPABASE_SERVICE_ROLE_KEY,
        'Authorization': f'Bearer {SUPABASE_SERVICE_ROLE_KEY}',
        'Content-Type': 'application/json',
    }
    if prefer:
        data['Prefer'] = prefer
    return data


def normalize_model(value):
    return re.sub(r'\s+', '', (value or '').strip().upper())


def find_product(brand, model):
    brand_q = (brand or '').replace('*', '')
    model_q = (model or '').replace('*', '')
    endpoint = (
        f'{SUPABASE_URL}/rest/v1/refrigerator_products'
        '?select=*'
        f'&brand=ilike.*{brand_q}*'
        f'&equipment_model=ilike.*{model_q}*'
        '&limit=10'
    )
    with httpx.Client(timeout=30) as client:
        response = client.get(endpoint, headers=headers())
        response.raise_for_status()
        rows = response.json()
    if not rows:
        return None
    wanted = normalize_model(model)
    for row in rows:
        if normalize_model(row.get('equipment_model')) == wanted:
            return row
    return rows[0]


def get_gasket_specs(product_id):
    endpoint = (
        f'{SUPABASE_URL}/rest/v1/product_gasket_specs'
        f'?select=*&refrigerator_product_id=eq.{product_id}&limit=1'
    )
    with httpx.Client(timeout=30) as client:
        response = client.get(endpoint, headers=headers())
        response.raise_for_status()
        rows = response.json()
    return rows[0] if rows else None


def insert_request(customer_name, image_path, brand, model, product, notes):
    row = {
        'customer_name': customer_name,
        'nameplate_image_url': str(image_path) if image_path else None,
        'detected_brand': brand,
        'detected_model': model,
        'matched_refrigerator_product_id': product.get('id') if product else None,
        'match_score': 100 if product else None,
        'status': 'matched' if product else 'needs_research',
        'notes': notes,
    }
    endpoint = f'{SUPABASE_URL}/rest/v1/gasket_requests'
    with httpx.Client(timeout=30) as client:
        response = client.post(endpoint, headers=headers('return=representation'), json=row)
        response.raise_for_status()
        rows = response.json()
    return rows[0] if rows else row


def parse_content_disposition(value):
    result = {}
    for part in value.split(';'):
        part = part.strip()
        if '=' in part:
            key, raw = part.split('=', 1)
            result[key.lower()] = raw.strip().strip('"')
    return result


def parse_multipart(content_type, body):
    marker = 'boundary='
    if marker not in content_type:
        return {}, {}
    boundary = content_type.split(marker, 1)[1].strip().strip('"')
    boundary_bytes = ('--' + boundary).encode()
    fields = {}
    files = {}
    for raw_part in body.split(boundary_bytes):
        part = raw_part.strip(b'\r\n')
        if not part or part == b'--' or b'\r\n--':
            continue
        if b'\r\n\r\n' not in part:
            continue
        raw_headers, data = part.split(b'\r\n\r\n', 1)
        data = data.rstrip(b'\r\n')
        header_lines = raw_headers.decode('utf-8', errors='ignore').split('\r\n')
        part_headers = {}
        for line in header_lines:
            if ':' in line:
                key, value = line.split(':', 1)
                part_headers[key.lower().strip()] = value.strip()
        disp = parse_content_disposition(part_headers.get('content-disposition', ''))
        name = disp.get('name')
        if not name:
            continue
        filename = disp.get('filename')
        if filename:
            files[name] = {'filename': filename, 'content': data}
        else:
            fields[name] = data.decode('utf-8', errors='ignore').strip()
    return fields, files


def parse_urlencoded(body):
    fields = {}
    for item in body.decode('utf-8', errors='ignore').split('&'):
        if not item:
            continue
        key, _, value = item.partition('=')
        fields[unquote_plus(key)] = unquote_plus(value)
    return fields, {}


def page(title, body):
    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)}</title>
  <style>
    :root {{ --ink:#17202a; --muted:#607080; --line:#d8e0e7; --soft:#f3f6f8; --accent:#0c766e; --steel:#263746; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:Arial, Helvetica, sans-serif; color:var(--ink); background:var(--soft); line-height:1.45; }}
    header {{ background:var(--steel); color:#fff; padding:22px 24px; }}
    header h1 {{ margin:0; font-size:24px; }}
    header p {{ margin:6px 0 0; color:#cbd5e1; }}
    main {{ max-width:1120px; margin:0 auto; padding:24px; }}
    section {{ background:#fff; border:1px solid var(--line); border-radius:8px; padding:22px; margin-bottom:18px; }}
    h2 {{ margin:0 0 12px; color:var(--steel); }}
    label {{ display:block; margin:0 0 6px; color:var(--muted); font-size:13px; font-weight:700; }}
    input, textarea {{ width:100%; border:1px solid var(--line); border-radius:6px; min-height:42px; padding:10px; font:inherit; }}
    textarea {{ min-height:90px; }}
    .grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; }}
    .button, button {{ display:inline-flex; align-items:center; justify-content:center; min-height:44px; padding:0 18px; border:0; border-radius:6px; background:var(--accent); color:#fff; font-weight:800; text-decoration:none; cursor:pointer; }}
    .muted {{ color:var(--muted); }}
    .hero {{ display:grid; grid-template-columns:1.1fr .9fr; gap:24px; align-items:center; }}
    .hero h2 {{ font-size:44px; line-height:1.05; }}
    .card-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; }}
    .card {{ border:1px solid var(--line); border-radius:8px; padding:16px; background:#fbfcfe; }}
    .kv {{ display:grid; grid-template-columns:180px 1fr; gap:8px 12px; }}
    .kv div:nth-child(odd) {{ color:var(--muted); }}
    .product-image {{ width:100%; max-height:320px; min-height:220px; object-fit:contain; border:1px solid var(--line); border-radius:8px; background:#f8fafc; }}
    table {{ width:100%; border-collapse:collapse; }}
    th, td {{ border-top:1px solid var(--line); padding:10px; text-align:left; vertical-align:top; }}
    th {{ color:var(--muted); font-size:12px; text-transform:uppercase; }}
    @media(max-width:760px) {{ .grid,.hero,.card-grid {{ grid-template-columns:1fr; }} .hero h2 {{ font-size:34px; }} }}
  </style>
</head>
<body>
  <header><h1>FixPro24 Gasket Match</h1><p>Upload a refrigerator nameplate or enter a model number. We help confirm the right door gasket.</p></header>
  <main>{body}</main>
</body>
</html>'''.encode('utf-8')


def render_home(message=''):
    notice = f'<p style="color:#9a3412;font-weight:700">{esc(message)}</p>' if message else ''
    body = f'''
<section class="hero">
  <div>
    <h2>Find the Right Refrigerator Door Gasket Fast</h2>
    <p class="muted">Customers do not need to know the part number. Start with a nameplate photo, brand, or model. We confirm the fit before the gasket is made.</p>
    <div class="card-grid">
      <div class="card"><strong>Send a photo</strong><p class="muted">Upload the nameplate from the job site.</p></div>
      <div class="card"><strong>We check the model</strong><p class="muted">We look for the gasket option that fits your unit.</p></div>
      <div class="card"><strong>Order with confidence</strong><p class="muted">Matched gaskets can move to Shopify checkout.</p></div>
    </div>
  </div>
  <section style="margin:0">
    <h2>Start a match</h2>
    {notice}
    <form method="post" action="/upload" enctype="multipart/form-data">
      <div class="grid">
        <div><label>Nameplate photo</label><input type="file" name="nameplate" accept="image/*"></div>
        <div><label>Customer name</label><input name="customer_name" placeholder="ABC Restaurant"></div>
        <div><label>Brand</label><input name="brand" placeholder="True, Traulsen, Sub-Zero" required></div>
        <div><label>Model</label><input name="equipment_model" placeholder="T-49, D2R, 685/S/2" required></div>
      </div>
      <div style="margin-top:12px"><label>Notes</label><textarea name="notes" placeholder="Door count, urgency, measurements, phone, email..."></textarea></div>
      <div style="margin-top:14px"><button type="submit">Find Matching Gasket</button></div>
    </form>
  </section>
</section>
<section>
  <h2>Common Questions</h2>
  <div class="card-grid">
    <div class="card"><strong>How long does it take?</strong><p class="muted">Stock items can ship fast. Custom-made gaskets usually take 3-15 days.</p></div>
    <div class="card"><strong>What if I do not know the size?</strong><p class="muted">Send a nameplate and door photo. We will ask for any missing measurement.</p></div>
    <div class="card"><strong>Can I install it myself?</strong><p class="muted">Most replacement gaskets are DIY friendly with basic installation guidance.</p></div>
  </div>
</section>
'''
    return page('Find the Right Refrigerator Door Gasket Fast', body)


def render_result(upload_url, product, spec, request):
    if not product:
        return page('Match needs review', '''
<section><h2>We need to review this model</h2><p class="muted">We saved your request, but this model was not found in the current database. We will review it manually.</p><p><a class="button" href="/">Start another match</a></p></section>
''')
    image = product.get('product_image_url')
    image_html = f'<img class="product-image" src="{esc(image)}" alt="Refrigerator image">' if image else '<div class="product-image" style="display:grid;place-items:center;color:#607080">No product image yet</div>'
    doors = []
    if spec:
        doors_data = spec.get('doors') or []
        if isinstance(doors_data, list):
            for door in doors_data:
                doors.append(f"<tr><td>{esc(door.get('door_position') or 'Door')}</td><td>{esc(door.get('dimensions_text') or '')}</td><td>{esc(door.get('gasket_part_number') or '')}</td></tr>")
    if not doors:
        doors.append('<tr><td>Main door</td><td>Size needs confirmation</td><td>Pending</td></tr>')
    body = f'''
<section>
  <h2>Possible Match</h2>
  <p class="muted">Request ID: {esc(request.get('id'))}</p>
  <div class="grid">
    <div>{image_html}</div>
    <div class="kv">
      <div>Brand</div><div><strong>{esc(product.get('brand'))}</strong></div>
      <div>Model</div><div><strong>{esc(product.get('equipment_model'))}</strong></div>
      <div>Manufacturer</div><div>{esc(product.get('manufacturer'))}</div>
      <div>Production date</div><div>{esc(product.get('manufacture_date_text') or product.get('manufacture_date'))}</div>
      <div>Status</div><div>{esc(product.get('data_status'))}</div>
    </div>
  </div>
</section>
<section>
  <h2>Matched Gasket Options</h2>
  <table><thead><tr><th>Door</th><th>Size</th><th>Part number</th></tr></thead><tbody>{''.join(doors)}</tbody></table>
  <p class="muted">If anything is unclear, we will ask for the missing photo or measurement before production.</p>
  <p><a class="button" href="/">Start another match</a></p>
</section>
'''
    return page('Possible Match', body)


class Handler(BaseHTTPRequestHandler):
    def send_html(self, data, status=HTTPStatus.OK):
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/':
            self.send_html(render_home())
            return
        if parsed.path.startswith('/uploads/'):
            target = (ROOT / parsed.path.lstrip('/')).resolve()
            if not str(target).startswith(str((ROOT / 'uploads').resolve())) or not target.exists():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data = target.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header('Content-Type', 'image/jpeg')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if urlparse(self.path).path != '/upload':
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get('Content-Length', '0') or '0')
        body = self.rfile.read(length)
        content_type = self.headers.get('Content-Type', '')
        if content_type.startswith('multipart/form-data'):
            fields, files = parse_multipart(content_type, body)
        else:
            fields, files = parse_urlencoded(body)
        brand = (fields.get('brand') or '').strip()
        model = (fields.get('equipment_model') or '').strip()
        customer_name = (fields.get('customer_name') or '').strip() or None
        notes = (fields.get('notes') or '').strip() or None
        if not brand or not model:
            self.send_html(render_home('Please enter brand and model.'), HTTPStatus.BAD_REQUEST)
            return
        saved_path = None
        file_item = files.get('nameplate')
        if file_item and file_item.get('content'):
            suffix = Path(file_item.get('filename') or '').suffix or '.jpg'
            saved_name = f'{uuid.uuid4().hex}{suffix}'
            saved_path = UPLOAD_DIR / saved_name
            saved_path.write_bytes(file_item['content'])
        product = find_product(brand, model)
        request = insert_request(customer_name, saved_path, brand, model, product, notes)
        spec = get_gasket_specs(product['id']) if product else None
        self.send_html(render_result('/uploads/nameplates/' + saved_path.name if saved_path else '', product, spec, request))


def main():
    port = int(os.getenv('PORT') or os.getenv('NAMEPLATE_WEB_PORT', '8000'))
    host = os.getenv('NAMEPLATE_WEB_HOST', '0.0.0.0')
    server = ThreadingHTTPServer((host, port), Handler)
    print(f'FixPro24 gasket match running on {host}:{port}')
    server.serve_forever()


if __name__ == '__main__':
    main()
