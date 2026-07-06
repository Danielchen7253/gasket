import csv
import os
import re
import time
from dataclasses import dataclass
from typing import Iterable
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv


load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

TABLE_NAME = "refrigerator_products"

MODEL_RE = re.compile(r"\b[A-Z0-9]{2,12}(?:[-/][A-Z0-9]{1,12}){0,6}\b")
SOURCE_FILE = Path(__file__).with_name("brand_sources.csv")


@dataclass(frozen=True)
class Source:
    brand: str
    url: str


def load_sources() -> list[Source]:
    with SOURCE_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return [
            Source(row["brand"].strip(), row["url"].strip())
            for row in reader
            if row.get("brand") and row.get("url")
        ]


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def looks_like_refrigeration_page(text: str) -> bool:
    haystack = text.lower()
    keywords = [
        "refrigerator",
        "freezer",
        "refrigeration",
        "reach-in",
        "undercounter",
        "prep table",
        "merchandiser",
        "door gasket",
        "gasket",
    ]
    return any(keyword in haystack for keyword in keywords)


def is_plausible_model(value: str) -> bool:
    model = value.strip().upper()
    if len(model) < 4 or len(model) > 35:
        return False
    if not re.search(r"[A-Z]", model) or not re.search(r"\d", model):
        return False
    if re.fullmatch(r"\d+(?:ST|ND|RD|TH|IN|FT|CU|AM|PM)?", model):
        return False
    blocked = {
        "COVID-19",
        "HTTP/1",
        "HTML5",
        "H1",
        "H2",
        "H3",
        "H4",
        "H5",
        "H6",
        "SEO",
        "404",
        "403",
    }
    return model not in blocked


def extract_product_image(base_url: str, html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    og_image = soup.select_one('meta[property="og:image"], meta[name="og:image"]')
    if og_image and og_image.get("content"):
        return urljoin(base_url, og_image["content"])

    for img in soup.select("img[src], img[data-src], img[data-original]"):
        src = img.get("src") or img.get("data-src") or img.get("data-original")
        alt = clean_text(img.get("alt", "")).lower()
        if not src:
            continue
        haystack = f"{alt} {src.lower()}"
        if any(token in haystack for token in ["refrigerator", "freezer", "fridge", "cooler"]):
            return urljoin(base_url, src)
    return None


def fetch(client: httpx.Client, url: str) -> str:
    response = client.get(url, follow_redirects=True, timeout=30)
    response.raise_for_status()
    return response.text


def extract_links(base_url: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(base_url).netloc.lower()
    links: list[str] = []
    for a in soup.select("a[href]"):
        href = a.get("href")
        if not href:
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"}:
            continue
        if parsed.netloc.lower() != base_host:
            continue
        if any(token in absolute.lower() for token in ["refriger", "fridge", "freezer", "gasket", "reach"]):
            links.append(absolute.split("#")[0])
    return sorted(set(links))


def extract_models(html: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    text = clean_text(soup.get_text(" "))
    if not looks_like_refrigeration_page(text):
        return set()

    models = set()
    for match in MODEL_RE.findall(text.upper()):
        model = clean_text(match)
        if not is_plausible_model(model):
            continue
        models.add(model)
    return models


def crawl_source(client: httpx.Client, source: Source, max_pages: int = 50) -> Iterable[dict]:
    seen_pages = set()
    queue = [source.url]

    while queue and len(seen_pages) < max_pages:
        url = queue.pop(0)
        if url in seen_pages:
            continue
        seen_pages.add(url)

        try:
            html = fetch(client, url)
        except Exception as exc:
            print(f"skip {url}: {exc}")
            continue

        product_image_url = extract_product_image(url, html)
        for model in extract_models(html):
            row = {
                "brand": source.brand,
                "equipment_model": model,
                "data_status": "pending",
            }
            if product_image_url:
                row["product_image_url"] = product_image_url
            yield row

        for link in extract_links(url, html):
            if link not in seen_pages and len(queue) < max_pages:
                queue.append(link)

        time.sleep(1)


def chunked(items: list[dict], size: int = 100) -> Iterable[list[dict]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def supabase_headers(prefer: str | None = None) -> dict[str, str]:
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def upsert_rows(rows: list[dict]) -> None:
    endpoint = (
        f"{SUPABASE_URL.rstrip('/')}%s"
        % f"/rest/v1/{TABLE_NAME}?on_conflict=brand,equipment_model"
    )
    with httpx.Client(timeout=60) as client:
        for batch in chunked(rows):
            response = client.post(
                endpoint,
                headers=supabase_headers("resolution=ignore-duplicates,return=minimal"),
                json=batch,
            )
            response.raise_for_status()


def main() -> None:
    headers = {
        "User-Agent": "CommercialGasketsResearchBot/0.1 (+contact: your-email@example.com)"
    }

    all_rows = []
    seen = set()
    with httpx.Client(headers=headers) as client:
        for source in load_sources():
            print(f"crawling {source.brand}: {source.url}")
            for row in crawl_source(client, source):
                key = (row["brand"].lower(), row["equipment_model"].upper())
                if key in seen:
                    continue
                seen.add(key)
                all_rows.append(row)

    print(f"found {len(all_rows)} unique brand/model rows")
    upsert_rows(all_rows)

    print("done")


if __name__ == "__main__":
    main()
