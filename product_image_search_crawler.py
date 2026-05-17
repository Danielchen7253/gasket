import json
import os
import re
import struct
from html import unescape
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name('.env'))

SUPABASE_URL = os.environ['SUPABASE_URL'].rstrip('/')
SUPABASE_SERVICE_ROLE_KEY = os.environ['SUPABASE_SERVICE_ROLE_KEY']
PRODUCT_TABLE = 'refrigerator_products'
CANDIDATE_TABLE = 'product_image_candidates'
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0 Safari/537.36'


def headers(prefer=None):
    h = {'apikey': SUPABASE_SERVICE_ROLE_KEY, 'Authorization': f'Bearer {SUPABASE_SERVICE_ROLE_KEY}', 'Content-Type': 'application/json'}
    if prefer:
        h['Prefer'] = prefer
    return h


def norm(value):
    return re.sub(r'[^A-Z0-9]', '', (value or '').upper())


def get_products(client, limit):
    endpoint = (f'{SUPABASE_URL}/rest/v1/{PRODUCT_TABLE}'
        '?select=id,brand,equipment_model,product_image_url,product_image_confidence,product_image_verified'
        '&or=(product_image_url.is.null,product_image_confidence.lt.96)'
        f'&limit={limit}')
    r = client.get(endpoint, headers=headers())
    r.raise_for_status()
    return [row for row in r.json() if row.get('product_image_verified') is not True]


def image_size_from_bytes(data):
    if data.startswith(b'\x89PNG') and len(data) >= 24:
        return struct.unpack('>II', data[16:24])
    if data.startswith(b'\xff\xd8'):
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            length = int.from_bytes(data[i + 2:i + 4], 'big')
            if 0xC0 <= marker <= 0xC3 or 0xC5 <= marker <= 0xC7 or 0xC9 <= marker <= 0xCB or 0xCD <= marker <= 0xCF:
                return int.from_bytes(data[i + 7:i + 9], 'big'), int.from_bytes(data[i + 5:i + 7], 'big')
            i += 2 + max(length, 2)
    return None, None


def score(product, row):
    hay = norm(' '.join(str(row.get(k) or '') for k in ['title','image_url','page_url','source_name']))
    score = 0
    if norm(product['brand']) in hay: score += 30
    if norm(product['equipment_model']) in hay: score += 45
    if any(t in hay for t in ['REFRIGERATOR','FREEZER','COOLER','FRIDGE']): score += 10
    if any(t in hay for t in ['LOGO','ICON','GASKET','PART','THUMBNAIL']): score -= 25
    w, h = row.get('width_px') or 0, row.get('height_px') or 0
    if min(w, h) >= 300: score += 10
    if max(w, h) >= 900: score += 8
    return max(0, min(100, score))


def search_bing_images(client, product, limit=12):
    query = f'{product["brand"]} {product["equipment_model"]} refrigerator product image'
    try:
        r = client.get('https://www.bing.com/images/search', params={'q': query, 'form': 'HDRSC2'}, headers={'User-Agent': USER_AGENT}, timeout=30)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        print(f'image search skipped {query}: {exc.__class__.__name__}', flush=True)
        return []
    soup = BeautifulSoup(r.text, 'html.parser')
    rows = []
    seen = set()
    for item in soup.select('a.iusc'):
        raw = item.get('m')
        if not raw:
            continue
        try:
            data = json.loads(unescape(raw))
        except json.JSONDecodeError:
            continue
        image_url = data.get('murl') or ''
        if not image_url.startswith(('http://','https://')) or image_url in seen:
            continue
        seen.add(image_url)
        width = int(data.get('w') or 0)
        height = int(data.get('h') or 0)
        rows.append({'title': data.get('t') or query, 'image_url': image_url, 'page_url': data.get('purl'), 'source_name': 'Bing Images', 'width_px': width, 'height_px': height})
        if len(rows) >= limit:
            break
    return rows


def upsert_candidate(client, product, row):
    row = dict(row)
    row['refrigerator_product_id'] = product['id']
    row['match_score'] = score(product, row)
    endpoint = f'{SUPABASE_URL}/rest/v1/{CANDIDATE_TABLE}?on_conflict=refrigerator_product_id,image_url'
    r = client.post(endpoint, headers=headers('resolution=merge-duplicates,return=representation'), json=row)
    r.raise_for_status()
    data = r.json()
    return data[0] if data else row


def promote_best(client, product, candidates):
    if not candidates:
        return False
    best = max(candidates, key=lambda x: float(x.get('match_score') or 0))
    if float(best.get('match_score') or 0) < 70:
        return False
    patch = {
        'product_image_url': best.get('image_url'),
        'product_image_source_url': best.get('page_url'),
        'product_image_confidence': best.get('match_score'),
    }
    r = client.patch(f'{SUPABASE_URL}/rest/v1/{PRODUCT_TABLE}?id=eq.{product["id"]}', headers=headers('return=minimal'), json=patch)
    r.raise_for_status()
    return True


def main():
    limit = int(os.getenv('PRODUCT_IMAGE_LIMIT', '100'))
    saved = promoted = 0
    with httpx.Client(timeout=30) as client:
        products = get_products(client, limit)
        print(f'searching product images for {len(products)} products', flush=True)
        for product in products:
            rows = search_bing_images(client, product)
            candidates = [upsert_candidate(client, product, row) for row in rows]
            saved += len(candidates)
            if promote_best(client, product, candidates):
                promoted += 1
    print(f'saved candidates: {saved}', flush=True)
    print(f'promoted product images: {promoted}', flush=True)
    print('done', flush=True)


if __name__ == '__main__':
    main()
