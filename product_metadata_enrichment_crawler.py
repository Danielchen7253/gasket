import os
import re
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv


load_dotenv(Path(__file__).with_name(".env"))

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
MODEL_TABLE = "refrigerator_products"
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "12"))

DATE_RE = re.compile(
    r"\b(19|20)\d{2}[-/](0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])\b"
)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def supabase_headers(prefer: str | None = None) -> dict[str, str]:
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def get_pending_products(client: httpx.Client, limit: int) -> list[dict]:
    endpoint = (
        f"{SUPABASE_URL.rstrip('/')}/rest/v1/{MODEL_TABLE}"
        "?select=id,brand,equipment_model,product_image_url,manufacture_date"
        "&or=(product_image_url.is.null,manufacture_date.is.null)"
        f"&limit={limit}"
    )
    response = client.get(endpoint, headers=supabase_headers())
    response.raise_for_status()
    return response.json()


def update_product(client: httpx.Client, product_id: int, patch: dict) -> None:
    if not patch:
        return
    endpoint = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{MODEL_TABLE}?id=eq.{product_id}"
    response = client.patch(
        endpoint,
        headers=supabase_headers("return=minimal"),
        json=patch,
    )
    response.raise_for_status()


def fetch(client: httpx.Client, url: str) -> str:
    response = client.get(url, follow_redirects=True, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return response.text


def search_urls(brand: str, model: str) -> list[str]:
    query = quote_plus(f"{brand} {model} refrigerator")
    return [
        f"https://www.webstaurantstore.com/search/{query}.html",
        f"https://www.partstown.com/search?q={query}",
        f"https://www.google.com/search?q={query}",
    ]


def extract_links(base_url: str, html: str, brand: str, model: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    host = urlparse(base_url).netloc.lower()
    links = []
    tokens = [brand.lower(), model.lower(), model.lower().replace("-", "")]
    for a in soup.select("a[href]"):
        href = a.get("href")
        if not href:
            continue
        absolute = urljoin(base_url, href).split("#")[0]
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"}:
            continue
        if parsed.netloc.lower() != host:
            continue
        label = clean_text(a.get_text(" ")).lower()
        haystack = f"{label} {absolute.lower()}"
        if any(token and token in haystack for token in tokens):
            links.append(absolute)
    return list(dict.fromkeys(links[:6]))


def extract_image(base_url: str, html: str, brand: str, model: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    model_l = model.lower()

    og = soup.select_one('meta[property="og:image"], meta[name="og:image"]')
    if og and og.get("content"):
        return urljoin(base_url, og["content"])

    scored = []
    for img in soup.select("img[src], img[data-src], img[data-original]"):
        src = img.get("src") or img.get("data-src") or img.get("data-original")
        if not src:
            continue
        alt = clean_text(img.get("alt", "")).lower()
        haystack = f"{alt} {src.lower()}"
        score = 0
        if model_l in haystack:
            score += 5
        if brand.lower() in haystack:
            score += 2
        if any(token in haystack for token in ["refrigerator", "freezer", "fridge", "cooler"]):
            score += 2
        if "logo" in haystack or "icon" in haystack:
            score -= 5
        if score > 0:
            scored.append((score, urljoin(base_url, src)))

    scored.sort(reverse=True)
    return scored[0][1] if scored else None


def extract_manufacture_date(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    text = clean_text(soup.get_text(" "))
    lowered = text.lower()
    if not any(token in lowered for token in ["manufacture date", "manufactured", "production date"]):
        return None
    match = DATE_RE.search(text)
    if not match:
        return None
    return match.group(0).replace("/", "-")


def enrich_one(client: httpx.Client, product: dict) -> dict:
    brand = product["brand"]
    model = product["equipment_model"]
    best_image = None
    manufacture_date = None

    for search_url in search_urls(brand, model):
        try:
            html = fetch(client, search_url)
        except Exception as exc:
            print(f"skip search {brand} {model}: {exc}")
            continue

        best_image = best_image or extract_image(search_url, html, brand, model)
        manufacture_date = manufacture_date or extract_manufacture_date(html)

        for link in extract_links(search_url, html, brand, model):
            try:
                detail_html = fetch(client, link)
            except Exception:
                continue
            best_image = best_image or extract_image(link, detail_html, brand, model)
            manufacture_date = manufacture_date or extract_manufacture_date(detail_html)
            if best_image and manufacture_date:
                break

        if best_image and manufacture_date:
            break

    patch = {}
    if best_image and not product.get("product_image_url"):
        patch["product_image_url"] = best_image
    if manufacture_date and not product.get("manufacture_date"):
        patch["manufacture_date"] = manufacture_date
    return patch


def main() -> None:
    limit = int(os.getenv("PRODUCT_META_LIMIT", "100"))
    updated = 0
    with httpx.Client(
        headers={"User-Agent": "RefrigeratorProductResearchBot/0.1"},
        timeout=HTTP_TIMEOUT,
    ) as client:
        products = get_pending_products(client, limit)
        print(f"enriching product metadata for {len(products)} products")
        for product in products:
            patch = enrich_one(client, product)
            if patch:
                update_product(client, product["id"], patch)
                updated += 1
    print(f"updated product rows: {updated}")
    print("done")


if __name__ == "__main__":
    main()
