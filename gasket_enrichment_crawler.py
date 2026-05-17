import os
import re
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name('.env'))

SUPABASE_URL = os.environ['SUPABASE_URL'].rstrip('/')
SUPABASE_SERVICE_ROLE_KEY = os.environ['SUPABASE_SERVICE_ROLE_KEY']
HTTP_TIMEOUT = float(os.getenv('HTTP_TIMEOUT', '12'))
MODEL_TABLE = 'refrigerator_products'
DETAIL_TABLE = 'gasket_details'
SPEC_TABLE = 'product_gasket_specs'
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0 Safari/537.36'
DIMENSION_RE = re.compile(r'(\d+(?:\.\d+)?)\s*(?:"|in\.?|inch(?:es)?)?\s*[xX]\s*(\d+(?:\.\d+)?)\s*(?:"|in\.?|inch(?:es)?)?', re.I)
PART_RE = re.compile(r'\b(?:part(?:\s*#|\s*number)?|mpn|sku)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-./]{2,30})', re.I)

SEARCH_TARGETS = [
    ('WebstaurantStore', 'https://www.webstaurantstore.com/search/{query}.html'),
    ('Parts Town', 'https://www.partstown.com/search?q={query}'),
    ('Restaurant Cooler Gaskets', 'https://restaurantcoolergaskets.com/catalogsearch/result/?q={query}'),
]


def headers(prefer=None):
    h = {'apikey': SUPABASE_SERVICE_ROLE_KEY, 'Authorization': f'Bearer {SUPABASE_SERVICE_ROLE_KEY}', 'Content-Type': 'application/json'}
    if prefer:
        h['Prefer'] = prefer
    return h


def clean(value):
    return re.sub(r'\s+', ' ', value or '').strip()


def get_pending_models(client, limit):
    endpoint = (f'{SUPABASE_URL}/rest/v1/{SPEC_TABLE}'
        '?select=refrigerator_product_id,data_status'
        '&data_status=eq.missing'
        f'&limit={limit}')
    r = client.get(endpoint, headers=headers())
    r.raise_for_status()
    ids = [str(row['refrigerator_product_id']) for row in r.json()]
    if not ids:
        return []
    products = client.get(f'{SUPABASE_URL}/rest/v1/{MODEL_TABLE}?select=*&id=in.({",".join(ids)})', headers=headers())
    products.raise_for_status()
    return products.json()


def fetch(client, url):
    r = client.get(url, headers={'User-Agent': USER_AGENT}, follow_redirects=True, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return str(r.url), r.text


def detail_links(search_url, html, brand, model):
    soup = BeautifulSoup(html, 'html.parser')
    candidates = []
    tokens = [brand.lower(), model.lower(), 'gasket']
    for a in soup.select('a[href]'):
        href = a.get('href') or ''
        text = clean(a.get_text(' ', strip=True))
        absolute = urljoin(search_url, href)
        hay = f'{text} {href}'.lower()
        score = sum(1 for token in tokens if token and token in hay)
        if score >= 2 or ('gasket' in hay and model.lower() in hay):
            candidates.append((score, absolute))
    candidates.sort(reverse=True)
    return list(dict.fromkeys(url for _, url in candidates[:8]))


def parse_detail(source_name, url, html, brand, model):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean(soup.title.get_text(' ', strip=True)) if soup.title else ''
    text = clean(soup.get_text(' ', strip=True))
    hay = f'{title} {text[:3000]}'
    if model.lower() not in hay.lower() and brand.lower() not in hay.lower():
        return None
    dims = DIMENSION_RE.findall(hay)
    width = height = None
    dimensions_text = None
    if dims:
        width, height = dims[0]
        dimensions_text = f'{width} x {height}'
    part = None
    m = PART_RE.search(hay)
    if m:
        part = m.group(1).strip().upper()
    if not dims and not part and 'gasket' not in hay.lower():
        return None
    score = 55
    if brand.lower() in hay.lower(): score += 10
    if model.lower() in hay.lower(): score += 25
    if dims: score += 15
    if part: score += 10
    return {
        'source_name': source_name,
        'source_url': url,
        'gasket_name': title[:180] or 'Door gasket',
        'gasket_part_number': part,
        'dimensions_text': dimensions_text,
        'width_in': float(width) if width else None,
        'height_in': float(height) if height else None,
        'confidence_score': min(100, score),
        'raw_text_excerpt': hay[:1200],
    }


def candidate_urls(brand, model):
    query = quote_plus(f'{brand} {model} refrigerator door gasket')
    return [(name, template.format(query=query)) for name, template in SEARCH_TARGETS]


def insert_detail(client, product_id, detail):
    row = dict(detail)
    row['refrigerator_product_id'] = product_id
    endpoint = f'{SUPABASE_URL}/rest/v1/{DETAIL_TABLE}'
    r = client.post(endpoint, headers=headers('return=representation'), json={k:v for k,v in row.items() if v is not None})
    r.raise_for_status()
    return r.json()[0]


def upsert_spec(client, product_id, details):
    if details:
        best = max(details, key=lambda d: float(d.get('confidence_score') or 0))
        row = {
            'refrigerator_product_id': product_id,
            'data_status': 'candidate',
            'primary_gasket_part_number': best.get('gasket_part_number'),
            'primary_dimensions_text': best.get('dimensions_text'),
            'primary_width_in': best.get('width_in'),
            'primary_height_in': best.get('height_in'),
            'confidence_score': best.get('confidence_score'),
            'source_url': best.get('source_url'),
            'doors': [{'door_position': 'Main door', 'dimensions_text': best.get('dimensions_text'), 'gasket_part_number': best.get('gasket_part_number')}],
        }
    else:
        row = {'refrigerator_product_id': product_id, 'data_status': 'missing'}
    endpoint = f'{SUPABASE_URL}/rest/v1/{SPEC_TABLE}?on_conflict=refrigerator_product_id'
    r = client.post(endpoint, headers=headers('resolution=merge-duplicates,return=minimal'), json={k:v for k,v in row.items() if v is not None})
    r.raise_for_status()


def enrich_product(client, product):
    brand = product.get('brand') or ''
    model = product.get('equipment_model') or ''
    details = []
    for source_name, search_url in candidate_urls(brand, model):
        try:
            final, html = fetch(client, search_url)
        except httpx.HTTPError:
            continue
        links = detail_links(final, html, brand, model) or [final]
        for link in links[:5]:
            try:
                detail_url, detail_html = fetch(client, link)
                detail = parse_detail(source_name, detail_url, detail_html, brand, model)
                if detail:
                    details.append(insert_detail(client, product['id'], detail))
            except Exception:
                continue
    upsert_spec(client, product['id'], details)
    return len(details)


def main():
    limit = int(os.getenv('ENRICH_LIMIT', '50'))
    total = 0
    with httpx.Client(timeout=30) as client:
        models = get_pending_models(client, limit)
        print(f'enriching {len(models)} models', flush=True)
        for product in models:
            total += enrich_product(client, product)
    print(f'inserted detail rows: {total}', flush=True)
    print('done', flush=True)


if __name__ == '__main__':
    main()
