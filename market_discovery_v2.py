import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv


load_dotenv(Path(__file__).with_name(".env"))

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

PRODUCT_TABLE = "refrigerator_products"
DISCOVERY_TABLE = "discovered_refrigerator_models"

PROMOTE_SCORE = float(os.getenv("DISCOVERY_PROMOTE_SCORE", "80"))
HIGH_CONFIDENCE_SCORE = float(os.getenv("DISCOVERY_HIGH_CONFIDENCE_SCORE", "65"))
MIN_INDEPENDENT_SOURCES = int(os.getenv("DISCOVERY_MIN_INDEPENDENT_SOURCES", "2"))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

KNOWN_BRANDS = [
    "Arctic Air",
    "Beverage-Air",
    "Continental",
    "Delfield",
    "Everest",
    "Hoshizaki",
    "Migali",
    "Traulsen",
    "True",
    "Turbo Air",
    "Master-Bilt",
    "Nor-Lake",
    "Victory",
    "Randell",
    "Perlick",
    "Federal",
    "Leer",
    "Sub-Zero",
    "Samsung",
    "Whirlpool",
    "GE",
    "Frigidaire",
    "KitchenAid",
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

DIRECT_SEARCH_TARGETS = [
    ("WebstaurantStore", "https://www.webstaurantstore.com/search/{query}.html"),
    ("Parts Town", "https://www.partstown.com/search?q={query}"),
    ("KaTom", "https://www.katom.com/search?w={query}"),
    ("CKitchen", "https://www.ckitchen.com/search/?q={query}"),
    ("Burkett", "https://www.burkett.com/search?q={query}"),
]

MODEL_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,12}(?:[-/ ][A-Z0-9]{1,14}){0,5}\b")
MODEL_CONTEXT_RE = re.compile(
    r"\b(?:model|models|model\s*#|model\s*number|m/n|sku)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/. ]{2,35})",
    re.I,
)
YEAR_RE = re.compile(r"\b(19[8-9]\d|20[0-3]\d)\b")

BAD_MODEL_TOKENS = {
    "BUY",
    "NOW",
    "COM",
    "NET",
    "JPG",
    "JPEG",
    "PNG",
    "WEBP",
    "IMAGE",
    "IMAGES",
    "PIMAGE",
    "PIMAGES",
    "JANITORIAL",
    "RESTAURANT",
    "SORRY",
    "FRIDAY",
    "MODEL",
    "NUMBER",
    "MONTH",
    "OVER",
    "REACH",
    "EZ",
    "CLEAN",
    "HARD",
    "GET",
    "SERIES",
    "CHEF",
    "PIZZA",
    "FOLD",
    "PRODUCT",
    "DETAILS",
    "MANUAL",
    "PARTS",
    "SERVICE",
    "USER",
}

PRODUCT_WORDS = {
    "REACH",
    "IN",
    "REFRIGERATOR",
    "REFRIGERATION",
    "FREEZER",
    "COOLER",
    "COMMERCIAL",
    "SECTION",
    "SOLID",
    "DOOR",
    "DOORS",
    "GLASS",
    "MERCHANDISER",
    "UNDERCOUNTER",
    "PREP",
    "TABLE",
    "BACK",
    "BAR",
    "WHITE",
    "BLACK",
    "STAINLESS",
    "STEEL",
    "ONE",
    "TWO",
    "THREE",
    "LEFT",
    "RIGHT",
}


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
    print(message.encode("ascii", "ignore").decode("ascii"))


def normalized(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def normalize_model(value: str) -> str:
    model = clean_text(value).upper().replace(" ", "-")
    model = re.sub(r"-{2,}", "-", model)
    return model.strip("-./ ")


def source_name(url: str) -> str:
    host = urlparse(url).netloc.lower().replace("www.", "")
    return host or "public web"


def source_domain(url: str) -> str:
    host = source_name(url)
    parts = host.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def source_kind(url: str, title: str = "", text: str = "") -> str:
    haystack = f"{url} {title} {text}".lower()
    host = urlparse(url).netloc.lower()
    manufacturer_hosts = [
        "truemfg",
        "traulsen",
        "beverage-air",
        "turboairinc",
        "hoshizakiamerica",
        "continentalrefrigerator",
        "arcticairco",
        "delfield",
        "everestref",
        "migali",
        "master-bilt",
        "norlake",
        "perlick",
        "subzero",
    ]
    if any(token in host for token in manufacturer_hosts):
        return "manufacturer"
    if ".pdf" in haystack or any(token in haystack for token in ["manual", "spec sheet", "specification", "service manual"]):
        return "manual_or_spec"
    if any(token in host for token in ["partstown", "webstaurantstore", "partsfe", "partsfps"]):
        return "parts_site"
    if any(token in haystack for token in ["dealer", "restaurant equipment", "product details", "add to cart"]):
        return "dealer"
    if "image" in haystack:
        return "image_search"
    return "public_web"


def source_weight(kind: str) -> int:
    return {
        "manufacturer": 35,
        "manual_or_spec": 35,
        "parts_site": 25,
        "dealer": 20,
        "image_search": 10,
        "public_web": 8,
    }.get(kind, 5)


def looks_like_refrigeration(text: str) -> bool:
    haystack = text.lower()
    return any(
        token in haystack
        for token in [
            "refrigerator",
            "refrigeration",
            "freezer",
            "cooler",
            "reach-in",
            "undercounter",
            "prep table",
            "merchandiser",
            "back bar",
            "gasket",
        ]
    )


def is_plausible_model(model: str) -> bool:
    value = normalize_model(model)
    if len(value) < 3 or len(value) > 35:
        return False
    if not re.search(r"[A-Z]", value) or not re.search(r"\d", value):
        return False
    parts = set(re.split(r"[-/ ]+", value))
    if parts & BAD_MODEL_TOKENS:
        return False
    if parts and parts <= PRODUCT_WORDS:
        return False
    if len(parts & PRODUCT_WORDS) >= max(2, len(parts) - 1):
        return False
    if "/" in value and len(value) > 14:
        return False
    if value.count("-") > 5:
        return False
    if re.fullmatch(r"\d{3,4}", value):
        return False
    if re.fullmatch(r"\d+(?:V|HZ|PHASE|BTU|W)", value):
        return False
    if value.startswith(("MONTH-", "REACH-", "EZ-", "GET-", "FOLD")):
        return False
    if value.startswith("AIR-") or value.endswith("-USER") or "-USER-" in value:
        return False
    return value not in {"COVID-19", "HTML5", "HTTP-1", "404", "403", "120V", "115V", "208V", "230V", "60HZ", "1-PHASE"}


def brand_aliases(brand: str) -> list[str]:
    aliases = [brand, brand.replace("-", " "), brand.replace("-", "")]
    if brand == "Beverage-Air":
        aliases.append("Beverage Air")
    if brand == "Sub-Zero":
        aliases.append("Sub Zero")
    return sorted(set(aliases), key=len, reverse=True)


def guess_brand(text: str, url: str) -> str | None:
    haystack = f"{text} {url}".lower()
    for brand in sorted(KNOWN_BRANDS, key=len, reverse=True):
        brand_l = brand.lower()
        if len(brand_l) <= 3:
            if re.search(rf"(?<![a-z0-9]){re.escape(brand_l)}(?![a-z0-9])", haystack):
                return brand
            continue
        if brand_l in haystack:
            return brand
    host = urlparse(url).netloc.lower().replace("www.", "").split(".")[0]
    host_map = {
        "continentalrefrigerator": "Continental",
        "truemfg": "True",
        "beverage-air": "Beverage-Air",
        "turboairinc": "Turbo Air",
        "hoshizakiamerica": "Hoshizaki",
        "traulsen": "Traulsen",
        "arcticairco": "Arctic Air",
    }
    return host_map.get(host)


def brand_adjacent_models(brand: str, *values: str) -> set[str]:
    text = clean_text(" ".join(value for value in values if value))
    models = set()
    for alias in brand_aliases(brand):
        match = re.search(re.escape(alias), text, flags=re.IGNORECASE)
        if not match:
            continue
        after = text[match.end():match.end() + 140]
        tokens = re.findall(r"[A-Za-z0-9]+(?:[-/][A-Za-z0-9]+)*", after)
        for token in tokens[:8]:
            candidate = normalize_model(token)
            if candidate in PRODUCT_WORDS or not re.search(r"\d", candidate):
                continue
            if is_plausible_model(candidate):
                models.add(candidate)
                break
    return models


def context_models(*values: str) -> set[str]:
    text = clean_text(" ".join(value for value in values if value))
    models = set()
    for match in MODEL_CONTEXT_RE.findall(text.upper()):
        candidate = re.split(
            r"\b(?:REFRIGERATOR|FREEZER|COOLER|DOOR|GASKET|PARTS|MANUAL|SPEC)\b",
            match,
            maxsplit=1,
        )[0]
        candidate = normalize_model(candidate)
        if is_plausible_model(candidate):
            models.add(candidate)
    return models


def title_models(brand: str, *values: str) -> set[str]:
    text = clean_text(" ".join(value for value in values if value))
    for alias in brand_aliases(brand):
        text = re.sub(re.escape(alias), " ", text, flags=re.IGNORECASE)
    models = set()
    for match in MODEL_RE.findall(text.upper()):
        model = normalize_model(match)
        if is_plausible_model(model):
            models.add(model)
    return models


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
    if not years:
        return None, None
    return years[0], years[-1]


def unwrap_bing_url(url: str) -> str:
    parsed = urlparse(url)
    if "bing.com" not in parsed.netloc or not parsed.path.startswith("/ck/"):
        return url
    target = parse_qs(parsed.query).get("u", [""])[0]
    if target.startswith("a1"):
        target = target[2:]
    return unquote(target) if target else url


def search_bing_web(client: httpx.Client, query: str, limit: int) -> list[SearchResult]:
    try:
        response = client.get(
            "https://www.bing.com/search",
            params={"q": query, "count": str(limit)},
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
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
        title = clean_text(link.get_text(" ", strip=True))
        snippet = clean_text(item.get_text(" ", strip=True))
        results.append(SearchResult(title, url, snippet, "Bing Web Search"))
    direct_results = search_direct_market_sites(client, query, limit)
    if results:
        return (results + direct_results)[: limit * 2]
    results = search_duckduckgo_web(client, query, limit)
    if results:
        return (results + direct_results)[: limit * 2]
    image_results = search_bing_images(client, query, limit)
    return (direct_results + image_results)[: limit * 2]


def search_direct_market_sites(client: httpx.Client, query: str, limit: int) -> list[SearchResult]:
    results = []
    seen = set()
    query_url = quote_plus(query.replace('"', ""))
    for target_name, template in DIRECT_SEARCH_TARGETS:
        url = template.format(query=query_url)
        try:
            response = client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=25)
            response.raise_for_status()
        except httpx.HTTPError:
            continue
        soup = BeautifulSoup(response.text, "html.parser")
        for link in soup.select("a[href]"):
            href = link.get("href") or ""
            absolute = urljoin(str(response.url), href).split("#")[0]
            if not absolute.startswith(("http://", "https://")) or absolute in seen:
                continue
            label = clean_text(link.get_text(" ", strip=True))
            haystack = f"{label} {absolute}".lower()
            if not any(brand.lower() in haystack for brand in KNOWN_BRANDS):
                continue
            if not looks_like_refrigeration(haystack):
                continue
            seen.add(absolute)
            results.append(SearchResult(label, absolute, haystack[:500], f"{target_name} Direct Search"))
            if len(results) >= limit:
                return results
    return results


def search_duckduckgo_web(client: httpx.Client, query: str, limit: int) -> list[SearchResult]:
    try:
        response = client.get(
            "https://duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        safe_print(f"duckduckgo skipped: {query}: {exc.__class__.__name__}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    results = []
    seen = set()
    for link in soup.select("a.result__a")[:limit]:
        url = link.get("href") or ""
        parsed = urlparse(url)
        if "duckduckgo.com" in parsed.netloc:
            url = unquote(parse_qs(parsed.query).get("uddg", [""])[0])
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        title = clean_text(link.get_text(" ", strip=True))
        parent = link.find_parent()
        snippet = clean_text(parent.get_text(" ", strip=True)) if parent else title
        results.append(SearchResult(title, url, snippet, "DuckDuckGo Web Search"))
    return results


def search_bing_images(client: httpx.Client, query: str, limit: int) -> list[SearchResult]:
    try:
        response = client.get(
            "https://www.bing.com/images/search",
            params={"q": query, "form": "HDRSC2"},
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        safe_print(f"image search skipped: {query}: {exc.__class__.__name__}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    results = []
    seen = set()
    for item in soup.select("a.iusc"):
        metadata = item.get("m")
        if not metadata:
            continue
        try:
            parsed = json.loads(unescape(metadata))
        except json.JSONDecodeError:
            continue
        page_url = parsed.get("purl") or ""
        if not page_url.startswith(("http://", "https://")) or page_url in seen:
            continue
        seen.add(page_url)
        title = clean_text(parsed.get("t") or "")
        snippet = clean_text(" ".join([title, parsed.get("desc") or "", parsed.get("murl") or ""]))
        results.append(SearchResult(title, page_url, snippet, "Bing Image Search"))
        if len(results) >= limit:
            break
    return results


def fetch_page(client: httpx.Client, url: str) -> tuple[str, str]:
    response = client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=25)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if "pdf" in content_type.lower() or str(response.url).lower().endswith(".pdf"):
        return str(response.url), ""
    return str(response.url), response.text


def extract_image(base_url: str, soup: BeautifulSoup) -> str | None:
    for selector, attr in [
        ('meta[property="og:image"]', "content"),
        ('meta[name="twitter:image"]', "content"),
        ('meta[property="og:image:secure_url"]', "content"),
    ]:
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
    spec_sheet = None
    manual = None
    for link in soup.select("a[href]"):
        href = link.get("href") or ""
        text = clean_text(link.get_text(" ", strip=True)).lower()
        absolute = urljoin(base_url, href)
        haystack = f"{href.lower()} {text}"
        if not spec_sheet and any(token in haystack for token in ["spec", "specification", "sell sheet", "cut sheet"]):
            spec_sheet = absolute
        if not manual and any(token in haystack for token in ["manual", "owner", "installation", "service"]):
            manual = absolute
    return spec_sheet, manual


def extract_candidates(result: SearchResult, final_url: str, html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser") if html else BeautifulSoup("", "html.parser")
    page_title = clean_text(soup.title.get_text(" ", strip=True)) if soup.title else result.title
    text = clean_text(soup.get_text(" ", strip=True)) if html else result.snippet
    evidence_text = clean_text(" ".join([page_title, result.snippet, text[:1500]]))
    if not looks_like_refrigeration(evidence_text):
        return []

    brand = guess_brand(evidence_text, final_url)
    if not brand:
        return []

    models = brand_adjacent_models(brand, page_title, result.title, result.snippet)
    models.update(context_models(page_title, result.title, result.snippet))
    if len(models) < 3:
        models.update(title_models(brand, page_title, result.title))
    if not models:
        return []

    image_url = extract_image(final_url, soup) if html else None
    spec_sheet, manual = extract_links(final_url, soup) if html else (None, None)
    product_type = product_type_from_text(evidence_text)
    lifecycle = lifecycle_from_text(evidence_text)
    year_start, year_end = find_years(evidence_text)
    kind = source_kind(final_url, page_title, evidence_text)

    rows = []
    for model in sorted(models)[:8]:
        confidence = 30 + source_weight(kind)
        if normalized(model) in normalized(" ".join([page_title, result.title])):
            confidence += 35
        elif normalized(model) in normalized(result.snippet):
            confidence += 20
        if product_type:
            confidence += 10
        if image_url:
            confidence += 8
        if spec_sheet or manual:
            confidence += 8
        if normalized(brand) in normalized(final_url):
            confidence += 4
        if any(token in final_url.lower() for token in ["product", "products", "spec", "manual"]):
            confidence += 5
        if any(token in normalized(model) for token in ["PDF", "HTML"]):
            confidence -= 15

        rows.append(
            {
                "discovered_brand": brand,
                "discovered_model": model,
                "normalized_brand": normalized(brand),
                "normalized_model": normalized(model),
                "source_url": final_url,
                "source_name": source_name(final_url),
                "page_title": page_title,
                "evidence_text": evidence_text[:1200],
                "product_type": product_type,
                "product_image_url": image_url,
                "official_product_url": final_url,
                "spec_sheet_url": spec_sheet,
                "manual_url": manual,
                "lifecycle_status": lifecycle,
                "lifecycle_evidence_url": final_url if lifecycle != "unknown" else None,
                "model_year_start": year_start,
                "model_year_end": year_end,
                "confidence_score": min(100, max(0, confidence)),
                "review_status": "pending",
                "evidence": {
                    "search_title": result.title,
                    "search_snippet": result.snippet,
                    "query_source": result.source_name,
                    "source_kind": kind,
                    "source_domain": source_domain(final_url),
                },
            }
        )
    return rows


def get_existing_product(client: httpx.Client, brand: str, model: str) -> dict | None:
    endpoint = (
        f"{SUPABASE_URL}/rest/v1/{PRODUCT_TABLE}"
        "?select=id,brand,equipment_model,product_image_url,product_image_confidence"
        f"&brand=ilike.{brand}"
        f"&equipment_model=ilike.{model}"
        "&limit=1"
    )
    response = client.get(endpoint, headers=supabase_headers())
    response.raise_for_status()
    rows = response.json()
    return rows[0] if rows else None


def upsert_discovery(client: httpx.Client, row: dict) -> dict:
    endpoint = f"{SUPABASE_URL}/rest/v1/{DISCOVERY_TABLE}?on_conflict=normalized_brand,normalized_model,source_url"
    row = dict(row)
    row["last_seen_at"] = datetime.now(timezone.utc).isoformat()
    response = client.post(
        endpoint,
        headers=supabase_headers("resolution=merge-duplicates,return=representation"),
        json=row,
    )
    response.raise_for_status()
    saved = response.json()
    return saved[0] if saved else row


def aggregate_evidence(client: httpx.Client, discovery: dict) -> dict:
    endpoint = (
        f"{SUPABASE_URL}/rest/v1/{DISCOVERY_TABLE}"
        "?select=id,source_url,source_name,confidence_score,evidence,product_image_url,spec_sheet_url,manual_url,official_product_url"
        f"&normalized_brand=eq.{discovery['normalized_brand']}"
        f"&normalized_model=eq.{discovery['normalized_model']}"
    )
    response = client.get(endpoint, headers=supabase_headers())
    response.raise_for_status()
    rows = response.json()
    domains = {source_domain(row.get("source_url") or "") for row in rows if row.get("source_url")}
    kinds = set()
    best_score = float(discovery.get("confidence_score") or 0)
    has_manufacturer_or_manual = False
    has_product_image = bool(discovery.get("product_image_url"))
    has_spec_or_manual = bool(discovery.get("spec_sheet_url") or discovery.get("manual_url"))
    for row in rows:
        best_score = max(best_score, float(row.get("confidence_score") or 0))
        evidence = row.get("evidence") or {}
        kind = evidence.get("source_kind") or source_kind(row.get("source_url") or "")
        kinds.add(kind)
        if kind in {"manufacturer", "manual_or_spec"}:
            has_manufacturer_or_manual = True
        has_product_image = has_product_image or bool(row.get("product_image_url"))
        has_spec_or_manual = has_spec_or_manual or bool(row.get("spec_sheet_url") or row.get("manual_url"))

    independent_sources = len([domain for domain in domains if domain])
    aggregate_score = best_score
    if independent_sources >= 2:
        aggregate_score += 20
    if independent_sources >= 3:
        aggregate_score += 15
    if has_manufacturer_or_manual:
        aggregate_score += 20
    if has_spec_or_manual:
        aggregate_score += 8
    if has_product_image:
        aggregate_score += 5
    return {
        "source_count": len(rows),
        "independent_sources": independent_sources,
        "source_kinds": sorted(kinds),
        "aggregate_score": round(min(100, aggregate_score), 2),
        "has_manufacturer_or_manual": has_manufacturer_or_manual,
        "has_spec_or_manual": has_spec_or_manual,
        "has_product_image": has_product_image,
    }


def is_auto_promotable(aggregate: dict) -> bool:
    return (
        float(aggregate.get("aggregate_score") or 0) >= PROMOTE_SCORE
        and (
            int(aggregate.get("independent_sources") or 0) >= MIN_INDEPENDENT_SOURCES
            or aggregate.get("has_manufacturer_or_manual")
        )
    )


def update_discovery_review(client: httpx.Client, discovery: dict) -> dict:
    aggregate = aggregate_evidence(client, discovery)
    if is_auto_promotable(aggregate):
        status = "auto_ready"
    elif float(aggregate["aggregate_score"]) >= HIGH_CONFIDENCE_SCORE:
        status = "high_confidence"
    else:
        status = "pending"
    evidence = dict(discovery.get("evidence") or {})
    evidence["cross_check"] = aggregate
    endpoint = f"{SUPABASE_URL}/rest/v1/{DISCOVERY_TABLE}?id=eq.{discovery['id']}"
    response = client.patch(
        endpoint,
        headers=supabase_headers("return=minimal"),
        json={"confidence_score": aggregate["aggregate_score"], "review_status": status, "evidence": evidence},
    )
    response.raise_for_status()
    updated = dict(discovery)
    updated["confidence_score"] = aggregate["aggregate_score"]
    updated["review_status"] = status
    updated["evidence"] = evidence
    return updated


def promote_discovery(client: httpx.Client, discovery: dict) -> int | None:
    aggregate = (discovery.get("evidence") or {}).get("cross_check") or aggregate_evidence(client, discovery)
    if not is_auto_promotable(aggregate):
        return None

    existing = get_existing_product(client, discovery["discovered_brand"], discovery["discovered_model"])
    now = datetime.now(timezone.utc).isoformat()
    product_row = {
        "brand": discovery["discovered_brand"],
        "equipment_model": discovery["discovered_model"],
        "manufacturer": discovery["discovered_brand"],
        "product_type": discovery.get("product_type"),
        "official_product_url": discovery.get("official_product_url"),
        "spec_sheet_url": discovery.get("spec_sheet_url"),
        "manual_url": discovery.get("manual_url"),
        "lifecycle_status": discovery.get("lifecycle_status") or "unknown",
        "lifecycle_evidence_url": discovery.get("lifecycle_evidence_url"),
        "model_year_start": discovery.get("model_year_start"),
        "model_year_end": discovery.get("model_year_end"),
        "data_confidence": aggregate["aggregate_score"],
        "last_discovered_at": now,
        "last_enriched_at": now,
        "data_status": "pending",
    }
    if discovery.get("product_image_url"):
        product_row["product_image_url"] = discovery["product_image_url"]
        product_row["product_image_source_url"] = discovery.get("source_url")
        product_row["product_image_confidence"] = min(90, float(aggregate["aggregate_score"]))

    if existing:
        endpoint = f"{SUPABASE_URL}/rest/v1/{PRODUCT_TABLE}?id=eq.{existing['id']}"
        response = client.patch(endpoint, headers=supabase_headers("return=minimal"), json={k: v for k, v in product_row.items() if v is not None})
        response.raise_for_status()
        return existing["id"]

    endpoint = f"{SUPABASE_URL}/rest/v1/{PRODUCT_TABLE}?on_conflict=brand,equipment_model"
    response = client.post(
        endpoint,
        headers=supabase_headers("resolution=ignore-duplicates,return=representation"),
        json={k: v for k, v in product_row.items() if v is not None},
    )
    response.raise_for_status()
    saved = response.json()
    return saved[0]["id"] if saved else None


def update_discovery_promotion(client: httpx.Client, discovery_id: int, product_id: int | None) -> None:
    status = "promoted" if product_id else "pending"
    endpoint = f"{SUPABASE_URL}/rest/v1/{DISCOVERY_TABLE}?id=eq.{discovery_id}"
    response = client.patch(endpoint, headers=supabase_headers("return=minimal"), json={"promoted_product_id": product_id, "review_status": status})
    response.raise_for_status()


def build_queries(limit: int) -> list[str]:
    queries = list(SEARCH_QUERIES)
    for brand in KNOWN_BRANDS:
        queries.extend(
            [
                f'"{brand}" "refrigerator" "model"',
                f'"{brand}" "freezer" "spec sheet"',
                f'"{brand}" "parts manual" refrigerator model',
                f'"{brand}" "owner manual" freezer model',
                f'"{brand}" "door gasket" "fits"',
                f'site:partstown.com "{brand}" refrigerator gasket',
                f'site:webstaurantstore.com "{brand}" refrigerator gasket',
            ]
        )
    if queries:
        rotation = int(os.getenv("DISCOVERY_QUERY_ROTATION", str(int(time.time() // 1200))))
        offset = rotation % len(queries)
        queries = queries[offset:] + queries[:offset]
    return queries[:limit]


def main() -> None:
    query_limit = int(os.getenv("DISCOVERY_QUERY_LIMIT", "16"))
    results_per_query = int(os.getenv("DISCOVERY_RESULTS_PER_QUERY", "10"))
    sleep_seconds = float(os.getenv("DISCOVERY_SLEEP_SECONDS", "0.5"))
    discovered_count = 0
    saved_count = 0
    promoted_count = 0
    seen_urls = set()
    seen_brand_models = set()

    with httpx.Client(timeout=30) as client:
        for query in build_queries(query_limit):
            safe_print(f"searching: {query}")
            for result in search_bing_web(client, query, results_per_query):
                if result.url in seen_urls:
                    continue
                seen_urls.add(result.url)
                try:
                    final_url, html = fetch_page(client, result.url)
                except httpx.HTTPError as exc:
                    safe_print(f"skip page {result.url}: {exc.__class__.__name__}")
                    continue
                for row in extract_candidates(result, final_url, html):
                    key = (row["normalized_brand"], row["normalized_model"], row["source_url"])
                    if key in seen_brand_models:
                        continue
                    seen_brand_models.add(key)
                    discovered_count += 1
                    saved = upsert_discovery(client, row)
                    saved_count += 1
                    saved = update_discovery_review(client, saved)
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
