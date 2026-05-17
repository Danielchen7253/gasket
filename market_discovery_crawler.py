import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv


load_dotenv(Path(__file__).with_name(".env"))

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

PRODUCT_TABLE = "refrigerator_products"
DISCOVERY_TABLE = "discovered_refrigerator_models"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

MODEL_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,10}(?:[-/ ][A-Z0-9]{1,12}){0,5}\b")
YEAR_RE = re.compile(r"\b(19[8-9]\d|20[0-3]\d)\b")
BAD_MODEL_TOKENS = {
    "BUY", "NOW", "COM", "NET", "JPG", "JPEG", "PNG", "WEBP", "IMAGE",
    "IMAGES", "PIMAGE", "PIMAGES", "JANITORIAL", "RESTAURANT", "SORRY",
    "FRIDAY", "MODEL", "NUMBER",
}
PRODUCT_WORDS = {
    "REACH", "IN", "REFRIGERATOR", "REFRIGERATION", "FREEZER", "COOLER",
    "COMMERCIAL", "SECTION", "SOLID", "DOOR", "DOORS", "GLASS", "MERCHANDISER",
    "UNDERCOUNTER", "PREP", "TABLE", "BACK", "BAR", "WHITE", "BLACK",
    "STAINLESS", "STEEL", "ONE", "TWO", "THREE", "LEFT", "RIGHT",
}
PART_PAGE_TOKENS = ["gasket", "oem part", "replacement part", "door gasket", "fits"]

KNOWN_BRANDS = [
    "Arctic Air", "Beverage-Air", "Continental", "Delfield", "Everest", "Hoshizaki",
    "Migali", "Traulsen", "True", "Turbo Air", "Master-Bilt", "Nor-Lake", "Victory",
    "Randell", "Perlick", "Federal", "Leer", "Sub-Zero",
]

SEARCH_QUERIES = [
    '"commercial refrigerator" "model" "spec sheet"',
    '"reach-in refrigerator" "model" "spec sheet"',
    '"undercounter refrigerator" "model" "spec sheet"',
    '"refrigerated prep table" "model" "spec sheet"',
    '"commercial freezer" "model" "manual"',
    '"refrigerator gasket" "model" "fits"',
    '"replacement gasket" "refrigerator" "model"',
]


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    source_name: str


def supabase_headers(prefer: str | None = None) -> dict[str, str]:
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def safe_print(message: str) -> None:
    print(message.encode("ascii", "ignore").decode("ascii"), flush=True)


def normalized(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def normalize_model(value: str) -> str:
    model = clean_text(value).upper().replace(" ", "-")
    model = re.sub(r"-{2,}", "-", model)
    return model.strip("-")


def source_name(url: str) -> str:
    host = urlparse(url).netloc.lower().replace("www.", "")
    return host or "public web"


def looks_like_refrigeration(text: str) -> bool:
    haystack = text.lower()
    return any(token in haystack for token in [
        "refrigerator", "refrigeration", "freezer", "cooler", "reach-in",
        "undercounter", "prep table", "merchandiser", "back bar", "gasket",
    ])


def is_plausible_model(model: str) -> bool:
    value = normalize_model(model)
    if len(value) < 3 or len(value) > 35:
        return False
    if not re.search(r"[A-Z]", value) or not re.search(r"\d", value):
        return False
    parts = set(re.split(r"[-/ ]+", value))
    if parts & BAD_MODEL_TOKENS:
        return False
    if "/" in value and len(value) > 14:
        return False
    if value.count("-") > 5:
        return False
    if value.startswith(("COM-", "NET-", "JPG-", "PNG-", "BUY-NOW-", "MODEL-NUMBER-")):
        return False
    blocked = {"COVID-19", "HTML5", "HTTP-1", "404", "403", "120V", "115V", "208V", "230V", "60HZ", "1-PHASE"}
    return value not in blocked


def brand_aliases(brand: str) -> list[str]:
    aliases = [brand, brand.replace("-", " "), brand.replace("-", "")]
    if brand == "Beverage-Air": aliases.append("Beverage Air")
    if brand == "Sub-Zero": aliases.append("Sub Zero")
    return sorted(set(aliases), key=len, reverse=True)


def brand_adjacent_models(brand: str, *values: str) -> set[str]:
    text = clean_text(" ".join(value for value in values if value))
    models = set()
    for alias in brand_aliases(brand):
        match = re.search(re.escape(alias), text, flags=re.IGNORECASE)
        if not match:
            continue
        after = text[match.end():match.end() + 100]
        tokens = re.findall(r"[A-Za-z0-9]+(?:[-/][A-Za-z0-9]+)*", after)
        for token in tokens[:6]:
            token_u = normalize_model(token)
            if token_u in PRODUCT_WORDS or not re.search(r"\d", token_u):
                continue
            if is_plausible_model(token_u):
                models.add(token_u)
                break
    return models


def guess_brand(text: str, url: str) -> str | None:
    haystack = f"{text} {url}".lower()
    for brand in KNOWN_BRANDS:
        if brand.lower() in haystack:
            return brand
    host = urlparse(url).netloc.lower().replace("www.", "").split(".")[0]
    return {
        "continentalrefrigerator": "Continental", "truemfg": "True",
        "beverage-air": "Beverage-Air", "turboairinc": "Turbo Air",
        "hoshizakiamerica": "Hoshizaki", "traulsen": "Traulsen",
        "arcticairco": "Arctic Air",
    }.get(host)


def product_type_from_text(text: str) -> str | None:
    haystack = text.lower()
    checks = [
        ("prep_table", ["prep table", "sandwich", "pizza prep"]),
        ("undercounter_refrigerator", ["undercounter refrigerator", "under counter refrigerator"]),
        ("reach_in_refrigerator", ["reach-in refrigerator", "reach in refrigerator"]),
        ("reach_in_freezer", ["reach-in freezer", "reach in freezer"]),
        ("back_bar_refrigerator", ["back bar", "bar refrigerator"]),
        ("merchandiser", ["merchandiser", "display refrigerator", "glass door"]),
        ("freezer", ["freezer"]),
        ("refrigerator", ["refrigerator", "refrigeration"]),
    ]
    for product_type, tokens in checks:
        if any(token in haystack for token in tokens):
            return product_type
    return None


def lifecycle_from_text(text: str) -> str:
    haystack = text.lower()
    if any(token in haystack for token in ["discontinued", "obsolete", "no longer available", "replaced by", "legacy model"]):
        return "discontinued"
    if any(token in haystack for token in ["add to cart", "in stock", "available", "current model", "product details"]):
        return "active"
    return "unknown"


def find_years(text: str) -> tuple[int | None, int | None]:
    years = sorted({int(match) for match in YEAR_RE.findall(text)})
    return (years[0], years[-1]) if years else (None, None)


def unwrap_bing_url(url: str) -> str:
    parsed = urlparse(url)
    if "bing.com" not in parsed.netloc or not parsed.path.startswith("/ck/"):
        return url
    target = parse_qs(parsed.query).get("u", [""])[0]
    if target.startswith("a1"):
        target = target[2:]
    try:
        return unquote(target) if target else url
    except ValueError:
        return url


def search_bing_web(client: httpx.Client, query: str, limit: int) -> list[SearchResult]:
    try:
        response = client.get("https://www.bing.com/search", params={"q": query, "count": str(limit)}, headers={"User-Agent": USER_AGENT}, timeout=30)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        safe_print(f"search skipped: {query}: {exc.__class__.__name__}")
        return []
    soup = BeautifulSoup(response.text, "html.parser")
    results = []
    for item in soup.select("li.b_algo")[:limit]:
        link = item.select_one("h2 a[href]")
        if not link:
            continue
        url = unwrap_bing_url(link.get("href") or "")
        if not url.startswith(("http://", "https://")):
            continue
        results.append(SearchResult(clean_text(link.get_text(" ", strip=True)), url, clean_text(item.get_text(" ", strip=True)), "Bing Web Search"))
    return results


def fetch_page(client: httpx.Client, url: str) -> tuple[str, str]:
    response = client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=25)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if "pdf" in content_type.lower() or str(response.url).lower().endswith(".pdf"):
        return str(response.url), ""
    return str(response.url), response.text


def extract_image(base_url: str, soup: BeautifulSoup) -> str | None:
    for selector, attr in [('meta[property="og:image"]', "content"), ('meta[name="twitter:image"]', "content"), ('meta[property="og:image:secure_url"]', "content")]:
        tag = soup.select_one(selector)
        if tag and tag.get(attr):
            return urljoin(base_url, tag[attr])
    for img in soup.select("img[src], img[data-src], img[data-original]")[:30]:
        src = img.get("src") or img.get("data-src") or img.get("data-original")
        alt = img.get("alt") or ""
        if not src:
            continue
        haystack = normalized(f"{src} {alt}")
        if any(token in haystack for token in ["LOGO", "ICON", "SPRITE"]):
            continue
        if any(token in haystack for token in ["REFRIGERATOR", "FREEZER", "COOLER", "PRODUCT"]):
            return urljoin(base_url, src)
    return None


def extract_links(base_url: str, soup: BeautifulSoup) -> tuple[str | None, str | None]:
    spec_sheet = manual = None
    for link in soup.select("a[href]"):
        href = link.get("href") or ""
        text = clean_text(link.get_text(" ", strip=True)).lower()
        absolute = urljoin(base_url, href)
        haystack = f"{href.lower()} {text}"
        if not spec_sheet and any(token in haystack for token in ["spec", "specification", "sell sheet", "cut sheet"]): spec_sheet = absolute
        if not manual and any(token in haystack for token in ["manual", "owner", "installation", "service"]): manual = absolute
    return spec_sheet, manual


def extract_candidates(result: SearchResult, final_url: str, html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser") if html else BeautifulSoup("", "html.parser")
    page_title = clean_text(soup.title.get_text(" ", strip=True)) if soup.title else result.title
    text = clean_text(soup.get_text(" ", strip=True)) if html else result.snippet
    evidence_text = clean_text(" ".join([page_title, result.snippet, text[:1500]]))
    if any(token in f"{page_title} {result.title}".lower() for token in PART_PAGE_TOKENS):
        return []
    if not looks_like_refrigeration(evidence_text):
        return []
    brand = guess_brand(evidence_text, final_url)
    if not brand:
        return []
    models = brand_adjacent_models(brand, page_title, result.title) or brand_adjacent_models(brand, result.snippet)
    image_url = extract_image(final_url, soup) if html else None
    spec_sheet, manual = extract_links(final_url, soup) if html else (None, None)
    product_type = product_type_from_text(evidence_text)
    lifecycle = lifecycle_from_text(evidence_text)
    year_start, year_end = find_years(evidence_text)
    rows = []
    for model in sorted(models)[:8]:
        confidence = 30
        if model and normalized(model) in normalized(" ".join([page_title, result.title])): confidence += 35
        elif model and normalized(model) in normalized(result.snippet): confidence += 20
        if product_type: confidence += 10
        if image_url: confidence += 8
        if spec_sheet or manual: confidence += 8
        if normalized(brand) in normalized(final_url): confidence += 4
        if any(token in final_url.lower() for token in ["product", "products", "spec", "manual"]): confidence += 5
        rows.append({
            "discovered_brand": brand, "discovered_model": model,
            "normalized_brand": normalized(brand), "normalized_model": normalized(model),
            "source_url": final_url, "source_name": source_name(final_url), "page_title": page_title,
            "evidence_text": evidence_text[:1200], "product_type": product_type,
            "product_image_url": image_url, "official_product_url": final_url,
            "spec_sheet_url": spec_sheet, "manual_url": manual,
            "lifecycle_status": lifecycle, "lifecycle_evidence_url": final_url if lifecycle != "unknown" else None,
            "model_year_start": year_start, "model_year_end": year_end,
            "confidence_score": min(100, max(0, confidence)),
            "review_status": "auto_ready" if confidence >= 92 else "pending",
            "evidence": {"search_title": result.title, "search_snippet": result.snippet, "query_source": result.source_name},
        })
    return rows


def get_existing_product(client: httpx.Client, brand: str, model: str) -> dict | None:
    endpoint = f"{SUPABASE_URL}/rest/v1/{PRODUCT_TABLE}?select=id,brand,equipment_model,product_image_url,product_image_confidence&brand=ilike.{brand}&equipment_model=ilike.{model}&limit=1"
    response = client.get(endpoint, headers=supabase_headers())
    response.raise_for_status()
    rows = response.json()
    return rows[0] if rows else None


def upsert_discovery(client: httpx.Client, row: dict) -> dict:
    endpoint = f"{SUPABASE_URL}/rest/v1/{DISCOVERY_TABLE}?on_conflict=normalized_brand,normalized_model,source_url"
    row = dict(row)
    row["last_seen_at"] = datetime.now(timezone.utc).isoformat()
    response = client.post(endpoint, headers=supabase_headers("resolution=merge-duplicates,return=representation"), json=row)
    response.raise_for_status()
    saved = response.json()
    return saved[0] if saved else row


def promote_discovery(client: httpx.Client, discovery: dict) -> int | None:
    if float(discovery.get("confidence_score") or 0) < 92:
        return None
    existing = get_existing_product(client, discovery["discovered_brand"], discovery["discovered_model"])
    now = datetime.now(timezone.utc).isoformat()
    product_row = {
        "brand": discovery["discovered_brand"], "equipment_model": discovery["discovered_model"],
        "manufacturer": discovery["discovered_brand"], "product_type": discovery.get("product_type"),
        "official_product_url": discovery.get("official_product_url"), "spec_sheet_url": discovery.get("spec_sheet_url"),
        "manual_url": discovery.get("manual_url"), "lifecycle_status": discovery.get("lifecycle_status") or "unknown",
        "lifecycle_evidence_url": discovery.get("lifecycle_evidence_url"), "model_year_start": discovery.get("model_year_start"),
        "model_year_end": discovery.get("model_year_end"), "data_confidence": discovery.get("confidence_score"),
        "last_discovered_at": now, "last_enriched_at": now, "data_status": "pending",
    }
    if discovery.get("product_image_url"):
        product_row["product_image_url"] = discovery["product_image_url"]
        product_row["product_image_source_url"] = discovery.get("source_url")
        product_row["product_image_confidence"] = min(90, float(discovery.get("confidence_score") or 0))
    if existing:
        response = client.patch(f"{SUPABASE_URL}/rest/v1/{PRODUCT_TABLE}?id=eq.{existing['id']}", headers=supabase_headers("return=minimal"), json={k:v for k,v in product_row.items() if v is not None})
        response.raise_for_status()
        return existing["id"]
    response = client.post(f"{SUPABASE_URL}/rest/v1/{PRODUCT_TABLE}?on_conflict=brand,equipment_model", headers=supabase_headers("resolution=ignore-duplicates,return=representation"), json={k:v for k,v in product_row.items() if v is not None})
    response.raise_for_status()
    saved = response.json()
    return saved[0]["id"] if saved else None


def update_discovery_promotion(client: httpx.Client, discovery_id: int, product_id: int | None) -> None:
    response = client.patch(f"{SUPABASE_URL}/rest/v1/{DISCOVERY_TABLE}?id=eq.{discovery_id}", headers=supabase_headers("return=minimal"), json={"promoted_product_id": product_id, "review_status": "promoted" if product_id else "pending"})
    response.raise_for_status()


def build_queries(limit: int) -> list[str]:
    queries = list(SEARCH_QUERIES)
    for brand in KNOWN_BRANDS:
        queries.append(f'"{brand}" "refrigerator" "model"')
        queries.append(f'"{brand}" "freezer" "spec sheet"')
    return queries[:limit]


def main() -> None:
    query_limit = int(os.getenv("DISCOVERY_QUERY_LIMIT", "12"))
    results_per_query = int(os.getenv("DISCOVERY_RESULTS_PER_QUERY", "8"))
    sleep_seconds = float(os.getenv("DISCOVERY_SLEEP_SECONDS", "1.5"))
    discovered_count = saved_count = promoted_count = 0
    seen_urls = set()
    seen_brand_models = set()
    with httpx.Client(timeout=30) as client:
        for query in build_queries(query_limit):
            safe_print(f"searching: {query}")
            for result in search_bing_web(client, query, results_per_query):
                if result.url in seen_urls: continue
                seen_urls.add(result.url)
                try:
                    final_url, html = fetch_page(client, result.url)
                except httpx.HTTPError as exc:
                    safe_print(f"skip page {result.url}: {exc.__class__.__name__}")
                    continue
                for row in extract_candidates(result, final_url, html):
                    key = (row["normalized_brand"], row["normalized_model"], row["source_url"])
                    if key in seen_brand_models: continue
                    seen_brand_models.add(key)
                    discovered_count += 1
                    saved = upsert_discovery(client, row)
                    saved_count += 1
                    product_id = promote_discovery(client, saved)
                    if product_id:
                        promoted_count += 1
                        update_discovery_promotion(client, saved["id"], product_id)
                time.sleep(sleep_seconds)
    safe_print(f"discovered candidates: {discovered_count}")
    safe_print(f"saved candidates: {saved_count}")
    safe_print(f"promoted products: {promoted_count}")
    safe_print("done")


if __name__ == "__main__":
    main()
