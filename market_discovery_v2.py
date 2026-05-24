import html
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import quote_plus, urlparse

import httpx
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

PRODUCT_TABLE = "refrigerator_products"
DISCOVERY_TABLE = "discovered_refrigerator_models"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

KNOWN_BRANDS = [
    "Arctic Air", "Beverage-Air", "Continental", "Delfield", "Everest",
    "Hoshizaki", "Migali", "Traulsen", "True", "Turbo Air", "Master-Bilt",
    "Nor-Lake", "Victory", "Randell", "Perlick", "Federal", "Leer",
    "Sub-Zero", "Samsung", "Whirlpool", "GE", "Frigidaire", "KitchenAid",
]

MARKET_SITES = [
    "webstaurantstore.com",
    "partstown.com",
    "katom.com",
    "ckitchen.com",
    "burkett.com",
]

BASE_QUERIES = [
    'commercial refrigerator model number spec sheet',
    'commercial freezer model number parts manual',
    'refrigerator door gasket fits model',
    'walk in cooler gasket model refrigerator',
]

BAD_MODEL_WORDS = {
    "ABOUT", "ACCESS", "ACCOUNT", "ADD", "AIR", "BACK", "BUY", "CART", "CATALOG",
    "CHEF", "CLEAN", "COMMERCIAL", "CONTACT", "DETAILS", "DOOR", "DOWNLOAD",
    "FIND", "FOLD", "FREE", "FREIGHT", "GASKET", "GUIDE", "HARD", "HOME",
    "LOGIN", "MANUAL", "MODEL", "MONTH", "NEW", "ORDER", "OVER", "PARTS",
    "PIZZA", "PRICE", "PRODUCT", "REACH", "REFRIGERATOR", "RESULTS", "SALE",
    "SEARCH", "SERIES", "SERVICE", "SHEET", "SHOP", "SPEC", "STAINLESS", "USER",
    "VIDEO", "VIEW", "WARRANTY",
}

PRODUCT_WORDS = (
    "refrigerator", "freezer", "cooler", "gasket", "door", "reach-in",
    "undercounter", "prep table", "merchandiser", "display case", "walk-in",
)

MANUFACTURER_DOMAINS = {
    "arcticairco.com", "beverage-air.com", "continentalrefrigerator.com",
    "delfield.com", "everestref.com", "hoshizakiamerica.com", "migali.com",
    "traulsen.com", "truemfg.com", "turboairinc.com", "master-bilt.com",
    "norlake.com", "victoryrefrigeration.com", "randell.com", "perlick.com",
    "subzero-wolf.com", "samsung.com", "whirlpool.com", "geappliances.com",
    "frigidaire.com", "kitchenaid.com",
}

MODEL_RE = re.compile(r"\b[A-Z0-9][A-Z0-9./_-]{2,31}\b")
TAG_RE = re.compile(r"<[^>]+>")
LINK_RE = re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
IMG_RE = re.compile(r'(?:murl|imgurl)[=:]["\']?([^"\'&>]+)', re.I)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def headers(prefer: str | None = None) -> dict[str, str]:
    data = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        data["Prefer"] = prefer
    return data


def clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = TAG_RE.sub(" ", value)
    return re.sub(r"\s+", " ", value).strip()


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def brand_in_text(brand: str, text: str) -> bool:
    if len(brand) <= 3:
        return re.search(rf"(?<![A-Za-z0-9]){re.escape(brand)}(?![A-Za-z0-9])", text, re.I) is not None
    return brand.lower() in text.lower()


def normalize_model(model: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", model.upper())


def valid_model(model: str) -> bool:
    model = model.strip(" .,/|:;()[]{}")
    norm = normalize_model(model)
    if len(norm) < 4 or len(norm) > 28:
        return False
    if not any(ch.isdigit() for ch in norm):
        return False
    if norm in BAD_MODEL_WORDS:
        return False
    if model.upper() in BAD_MODEL_WORDS:
        return False
    if model.upper().startswith("AIR-") or model.upper().endswith("-USER") or "-USER-" in model.upper():
        return False
    if re.fullmatch(r"\d{4,}", norm):
        return False
    if re.search(r"(MONTH|MODEL|MANUAL|PARTS|DETAIL|SERVICE|PRODUCT|SEARCH|USER)", norm):
        return False
    return True


def classify_source(url: str, search_type: str = "web") -> str:
    domain = domain_of(url)
    lower = url.lower()
    if search_type == "image":
        return "image_search"
    if domain in MANUFACTURER_DOMAINS:
        return "manufacturer"
    if "manual" in lower or "spec" in lower or lower.endswith(".pdf"):
        return "manual_or_spec"
    if any(site in domain for site in ("partstown", "parts", "gasket", "webstaurantstore")):
        return "parts_site"
    if domain in MARKET_SITES or any(site in domain for site in MARKET_SITES):
        return "dealer"
    return "public_web"


def product_type_from_text(text: str) -> str | None:
    lower = text.lower()
    if "freezer" in lower:
        return "freezer"
    if "walk-in" in lower or "walk in" in lower:
        return "walk-in cooler"
    if "cooler" in lower:
        return "cooler"
    if "refrigerator" in lower or "fridge" in lower:
        return "refrigerator"
    return None


def score_candidate(brand: str, model: str, title: str, snippet: str, url: str, source_kind: str, image_url: str | None) -> int:
    text = f"{title} {snippet} {url}".lower()
    score = {
        "manufacturer": 50,
        "manual_or_spec": 46,
        "parts_site": 38,
        "dealer": 34,
        "image_search": 24,
        "public_web": 14,
    }.get(source_kind, 10)
    if brand.lower() in title.lower():
        score += 8
    if model.lower() in title.lower() or normalize_model(model) in normalize_model(title):
        score += 10
    if any(word in text for word in PRODUCT_WORDS):
        score += 9
    if "gasket" in text and "fits" in text:
        score += 8
    if "manual" in text or "spec" in text or url.lower().endswith(".pdf"):
        score += 10
    if image_url:
        score += 8
    if source_kind == "manufacturer":
        score += 10
    return max(1, min(99, score))


def http_get(client: httpx.Client, url: str) -> str:
    try:
        r = client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        if r.status_code >= 400:
            print(f"skip {url}: {r.status_code}")
            return ""
        return r.text
    except Exception as exc:
        print(f"skip {url}: {exc}")
        return ""


def parse_search_results(page_html: str, search_type: str) -> list[dict]:
    rows: list[dict] = []
    for url, title_html in LINK_RE.findall(page_html):
        if not url.startswith("http"):
            continue
        if any(blocked in url for blocked in ("bing.com", "microsoft.com", "javascript:")):
            continue
        title = clean_text(title_html)
        if not title:
            continue
        rows.append({"url": url, "title": title, "snippet": title, "search_type": search_type, "image_url": None})
    if search_type == "image":
        for image_url in IMG_RE.findall(page_html)[:30]:
            rows.append({"url": image_url, "title": image_url, "snippet": image_url, "search_type": search_type, "image_url": image_url})
    return rows[:40]


def search(client: httpx.Client, query: str, per_query: int) -> list[dict]:
    encoded = quote_plus(query)
    urls = [
        (f"https://www.bing.com/search?q={encoded}&count={per_query}", "web"),
        (f"https://duckduckgo.com/html/?q={encoded}", "web"),
        (f"https://www.bing.com/images/search?q={encoded}&count={per_query}", "image"),
    ]
    rows: list[dict] = []
    seen = set()
    for url, kind in urls:
        body = http_get(client, url)
        for row in parse_search_results(body, kind):
            key = (row["url"], row["image_url"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
            if len(rows) >= per_query * 3:
                return rows
    return rows


def extract_candidates(result: dict, query_brand: str | None = None) -> list[dict]:
    text = f"{result['title']} {result['snippet']} {result['url']}"
    lower = text.lower()
    if not any(word in lower for word in PRODUCT_WORDS):
        return []
    brands = [query_brand] if query_brand else [brand for brand in KNOWN_BRANDS if brand_in_text(brand, text)]
    candidates: list[dict] = []
    for brand in filter(None, brands):
        if not brand_in_text(brand, text):
            continue
        for match in MODEL_RE.findall(text.upper()):
            model = match.strip("._-")
            if not valid_model(model):
                continue
            candidates.append({"brand": brand, "model": model})
    unique = {}
    for item in candidates:
        unique[(item["brand"].lower(), normalize_model(item["model"]))] = item
    return list(unique.values())[:8]


def build_queries(limit: int) -> list[tuple[str, str | None]]:
    queries: list[tuple[str, str | None]] = [(q, None) for q in BASE_QUERIES]
    for brand in KNOWN_BRANDS:
        brand_queries = [
            f'"{brand}" refrigerator model spec sheet',
            f'"{brand}" freezer model manual',
            f'"{brand}" door gasket fits model',
        ]
        for site in MARKET_SITES:
            brand_queries.append(f'site:{site} "{brand}" refrigerator model')
        queries.extend((q, brand) for q in brand_queries)
    rotation = int(os.getenv("DISCOVERY_QUERY_ROTATION", str(int(time.time() // 1200))))
    if queries:
        offset = rotation % len(queries)
        queries = queries[offset:] + queries[:offset]
    return queries[:limit]


def save_discovery(client: httpx.Client, row: dict) -> dict | None:
    endpoint = f"{SUPABASE_URL}/rest/v1/{DISCOVERY_TABLE}?on_conflict=normalized_brand,normalized_model,source_url"
    r = client.post(endpoint, headers=headers("resolution=merge-duplicates,return=representation"), json=row)
    if r.status_code >= 400:
        print(f"save discovery failed: {r.text[:300]}")
        return None
    data = r.json()
    return data[0] if data else None


def find_existing_product(client: httpx.Client, brand: str, model: str) -> dict | None:
    endpoint = f"{SUPABASE_URL}/rest/v1/{PRODUCT_TABLE}?select=id,brand,equipment_model&brand=eq.{quote_plus(brand)}&equipment_model=eq.{quote_plus(model)}&limit=1"
    r = client.get(endpoint, headers=headers())
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def promote_product(client: httpx.Client, best: dict, aggregate_score: int) -> int | None:
    existing = find_existing_product(client, best["discovered_brand"], best["discovered_model"])
    now = now_iso()
    row = {
        "brand": best["discovered_brand"],
        "equipment_model": best["discovered_model"],
        "manufacturer": best["discovered_brand"],
        "product_type": best.get("product_type"),
        "official_product_url": best.get("official_product_url"),
        "spec_sheet_url": best.get("spec_sheet_url"),
        "manual_url": best.get("manual_url"),
        "lifecycle_status": best.get("lifecycle_status") or "unknown",
        "data_confidence": aggregate_score,
        "last_discovered_at": now,
        "data_status": "pending",
    }
    if best.get("product_image_url"):
        row["product_image_url"] = best["product_image_url"]
        row["product_image_source_url"] = best.get("source_url")
        row["product_image_confidence"] = min(90, aggregate_score)
    row = {k: v for k, v in row.items() if v is not None}
    if existing:
        r = client.patch(f"{SUPABASE_URL}/rest/v1/{PRODUCT_TABLE}?id=eq.{existing['id']}", headers=headers("return=minimal"), json=row)
        r.raise_for_status()
        return existing["id"]
    r = client.post(f"{SUPABASE_URL}/rest/v1/{PRODUCT_TABLE}?on_conflict=brand,equipment_model", headers=headers("resolution=ignore-duplicates,return=representation"), json=row)
    r.raise_for_status()
    saved = r.json()
    return saved[0]["id"] if saved else None


def aggregate_and_promote(client: httpx.Client, saved_rows: list[dict]) -> tuple[int, int]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in saved_rows:
        groups[(row["normalized_brand"], row["normalized_model"])].append(row)
    promoted = 0
    high_confidence = 0
    promote_score = int(os.getenv("DISCOVERY_PROMOTE_SCORE", "80"))
    high_score = int(os.getenv("DISCOVERY_HIGH_CONFIDENCE_SCORE", "65"))
    min_sources = int(os.getenv("DISCOVERY_MIN_INDEPENDENT_SOURCES", "2"))
    for _, rows in groups.items():
        domains = {domain_of(row.get("source_url", "")) for row in rows if row.get("source_url")}
        kinds = {row.get("evidence", {}).get("source_kind", "public_web") for row in rows}
        best = max(rows, key=lambda row: int(row.get("confidence_score") or 0))
        best_score = int(best.get("confidence_score") or 0)
        has_authority = "manufacturer" in kinds or "manual_or_spec" in kinds
        has_image = any(row.get("product_image_url") for row in rows)
        aggregate_score = int(min(99, best_score * 0.58 + min(len(domains), 4) * 10 + (12 if has_authority else 0) + (5 if has_image else 0)))
        can_promote = aggregate_score >= promote_score and (len(domains) >= min_sources or has_authority)
        status = "pending"
        product_id = None
        if can_promote:
            product_id = promote_product(client, best, aggregate_score)
            status = "promoted" if product_id else "auto_ready"
            if product_id:
                promoted += 1
        elif aggregate_score >= high_score:
            status = "high_confidence"
            high_confidence += 1
        ids = ",".join(str(row["id"]) for row in rows if row.get("id"))
        if ids:
            payload = {"review_status": status, "confidence_score": aggregate_score}
            if product_id:
                payload["promoted_product_id"] = product_id
            r = client.patch(f"{SUPABASE_URL}/rest/v1/{DISCOVERY_TABLE}?id=in.({ids})", headers=headers("return=minimal"), json=payload)
            r.raise_for_status()
    return promoted, high_confidence


def main() -> None:
    query_limit = int(os.getenv("DISCOVERY_QUERY_LIMIT", "16"))
    per_query = int(os.getenv("DISCOVERY_RESULTS_PER_QUERY", "10"))
    sleep_seconds = float(os.getenv("DISCOVERY_SLEEP_SECONDS", "0.5"))
    saved_rows: list[dict] = []
    found = 0
    with httpx.Client(timeout=30) as client:
        for query, query_brand in build_queries(query_limit):
            print(f"search {query}")
            for result in search(client, query, per_query):
                for item in extract_candidates(result, query_brand):
                    source_kind = classify_source(result["url"], result.get("search_type", "web"))
                    score = score_candidate(item["brand"], item["model"], result["title"], result["snippet"], result["url"], source_kind, result.get("image_url"))
                    row = {
                        "discovered_brand": item["brand"],
                        "discovered_model": item["model"],
                        "normalized_brand": re.sub(r"[^A-Z0-9]", "", item["brand"].upper()),
                        "normalized_model": normalize_model(item["model"]),
                        "source_url": result["url"],
                        "source_name": domain_of(result["url"]),
                        "page_title": result["title"][:500],
                        "evidence_text": result["snippet"][:1000],
                        "product_type": product_type_from_text(f"{result['title']} {result['snippet']}"),
                        "product_image_url": result.get("image_url"),
                        "official_product_url": result["url"] if source_kind == "manufacturer" else None,
                        "spec_sheet_url": result["url"] if "spec" in result["url"].lower() else None,
                        "manual_url": result["url"] if "manual" in result["url"].lower() else None,
                        "confidence_score": score,
                        "review_status": "pending",
                        "evidence": {"query": query, "source_kind": source_kind, "search_type": result.get("search_type", "web")},
                        "last_seen_at": now_iso(),
                    }
                    saved = save_discovery(client, row)
                    if saved:
                        saved_rows.append(saved)
                        found += 1
            if sleep_seconds:
                time.sleep(sleep_seconds)
        promoted, high_confidence = aggregate_and_promote(client, saved_rows)
    print(f"found {found} candidate evidence rows")
    print(f"promoted {promoted} product models")
    print(f"kept {high_confidence} high-confidence candidate groups")
    print("done")


if __name__ == "__main__":
    main()
