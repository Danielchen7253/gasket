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
GOOGLE_CSE_KEY = os.getenv("GOOGLE_CSE_KEY", "")
GOOGLE_CSE_CX = os.getenv("GOOGLE_CSE_CX", "")

PRODUCT_TABLE = "refrigerator_products"
DISCOVERY_TABLE = "discovered_refrigerator_models"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36"

KNOWN_BRANDS = [
    "Arctic Air", "Beverage-Air", "Continental", "Delfield", "Everest", "Hoshizaki",
    "Migali", "Traulsen", "True", "Turbo Air", "Master-Bilt", "Nor-Lake", "Victory",
    "Randell", "Perlick", "Federal", "Leer", "Sub-Zero", "Samsung", "Whirlpool", "GE",
    "Frigidaire", "KitchenAid",
]
MARKET_SITES = ["webstaurantstore.com", "partstown.com", "katom.com", "ckitchen.com", "burkett.com"]
MANUFACTURER_DOMAINS = {
    "arcticairco.com", "beverage-air.com", "continentalrefrigerator.com", "delfield.com",
    "everestref.com", "hoshizakiamerica.com", "migali.com", "traulsen.com", "truemfg.com",
    "turboairinc.com", "master-bilt.com", "norlake.com", "victoryrefrigeration.com",
    "randell.com", "perlick.com", "subzero-wolf.com", "samsung.com", "whirlpool.com",
    "geappliances.com", "frigidaire.com", "kitchenaid.com",
}
PRODUCT_WORDS = ("refrigerator", "freezer", "cooler", "gasket", "door", "reach-in", "undercounter", "prep table", "merchandiser", "display case", "walk-in")
BAD_MODEL_WORDS = {
    "ABOUT", "ACCESS", "ACCOUNT", "ADD", "AIR", "BACK", "BUY", "CART", "CATALOG", "CHEF",
    "CLEAN", "COMMERCIAL", "CONTACT", "DETAILS", "DOOR", "DOWNLOAD", "FIND", "FOLD",
    "FREE", "FREIGHT", "GASKET", "GUIDE", "HARD", "HOME", "LOGIN", "MANUAL", "MODEL",
    "MONTH", "NEW", "ORDER", "OVER", "PARTS", "PIZZA", "PRICE", "PRODUCT", "REACH",
    "REFRIGERATOR", "RESULTS", "SALE", "SEARCH", "SERIES", "SERVICE", "SHEET", "SHOP",
    "SPEC", "STAINLESS", "USER", "VIDEO", "VIEW", "WARRANTY",
}
BASE_QUERIES = [
    'commercial refrigerator model number spec sheet',
    'commercial freezer model number parts manual',
    'refrigerator door gasket fits model',
]
MODEL_RE = re.compile(r"\b[A-Z0-9][A-Z0-9./_-]{2,31}\b")
TAG_RE = re.compile(r"<[^>]+>")
LINK_RE = re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
IMG_RE = re.compile(r'(?:murl|imgurl)[=:]["\']?([^"\'&>]+)', re.I)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def api_headers(prefer: str | None = None) -> dict[str, str]:
    h = {"apikey": SUPABASE_SERVICE_ROLE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}", "Content-Type": "application/json"}
    if prefer:
        h["Prefer"] = prefer
    return h


def clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = TAG_RE.sub(" ", value)
    return re.sub(r"\s+", " ", value).strip()


def domain_of(url: str) -> str:
    host = urlparse(url or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def normalize_model(model: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (model or "").upper())


def brand_in_text(brand: str, text: str) -> bool:
    if len(brand) <= 3:
        return re.search(rf"(?<![A-Za-z0-9]){re.escape(brand)}(?![A-Za-z0-9])", text, re.I) is not None
    return brand.lower() in text.lower()


def valid_model(model: str) -> bool:
    model = model.strip(" .,/|:;()[]{}")
    norm = normalize_model(model)
    if len(norm) < 4 or len(norm) > 28 or not any(ch.isdigit() for ch in norm):
        return False
    if norm in BAD_MODEL_WORDS or model.upper() in BAD_MODEL_WORDS:
        return False
    if model.upper().startswith("AIR-") or model.upper().endswith("-USER") or "-USER-" in model.upper():
        return False
    if re.fullmatch(r"\d{4,}", norm):
        return False
    if re.search(r"(MONTH|MODEL|MANUAL|PARTS|DETAIL|SERVICE|PRODUCT|SEARCH|USER|CATALOG)", norm):
        return False
    return True


def product_type(text: str) -> str | None:
    t = text.lower()
    if "freezer" in t:
        return "freezer"
    if "walk-in" in t or "walk in" in t:
        return "walk-in cooler"
    if "cooler" in t:
        return "cooler"
    if "refrigerator" in t or "fridge" in t:
        return "refrigerator"
    return None


def classify_source(url: str, search_type: str) -> str:
    domain = domain_of(url)
    low = (url or "").lower()
    if search_type == "image":
        return "image_search"
    if domain in MANUFACTURER_DOMAINS:
        return "manufacturer"
    if "manual" in low or "spec" in low or low.endswith(".pdf"):
        return "manual_or_spec"
    if any(x in domain for x in ("partstown", "parts", "gasket", "webstaurantstore")):
        return "parts_site"
    if domain in MARKET_SITES or any(x in domain for x in MARKET_SITES):
        return "dealer"
    return "public_web"


def score_candidate(brand: str, model: str, title: str, snippet: str, url: str, source_kind: str, image_url: str | None) -> int:
    text = f"{title} {snippet} {url}".lower()
    score = {"manufacturer": 52, "manual_or_spec": 48, "parts_site": 40, "dealer": 35, "image_search": 26, "public_web": 15}.get(source_kind, 10)
    if brand.lower() in title.lower():
        score += 8
    if model.lower() in title.lower() or normalize_model(model) in normalize_model(title):
        score += 10
    if any(word in text for word in PRODUCT_WORDS):
        score += 9
    if "gasket" in text and ("fits" in text or "door" in text):
        score += 8
    if "manual" in text or "spec" in text or url.lower().endswith(".pdf"):
        score += 10
    if image_url:
        score += 8
    if source_kind == "manufacturer":
        score += 10
    return max(1, min(99, score))


def google_search(client: httpx.Client, query: str, per_query: int, search_type: str) -> list[dict]:
    if not GOOGLE_CSE_KEY or not GOOGLE_CSE_CX:
        return []
    params = {"key": GOOGLE_CSE_KEY, "cx": GOOGLE_CSE_CX, "q": query, "num": min(10, per_query), "safe": "off"}
    if search_type == "image":
        params["searchType"] = "image"
    try:
        r = client.get("https://www.googleapis.com/customsearch/v1", params=params)
        if r.status_code >= 400:
            print(f"google search failed {r.status_code}: {r.text[:160]}")
            return []
        rows = []
        for item in r.json().get("items", []):
            link = item.get("link") or ""
            image_url = link if search_type == "image" else None
            rows.append({"url": link, "title": item.get("title") or "", "snippet": item.get("snippet") or "", "search_type": search_type, "image_url": image_url})
        return rows
    except Exception as exc:
        print(f"google search skipped: {exc}")
        return []


def fallback_search(client: httpx.Client, query: str, per_query: int) -> list[dict]:
    rows = []
    for url, kind in [
        (f"https://www.bing.com/search?q={quote_plus(query)}&count={per_query}", "web"),
        (f"https://www.bing.com/images/search?q={quote_plus(query)}&count={per_query}", "image"),
    ]:
        try:
            r = client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
            if r.status_code >= 400:
                continue
            body = r.text
            for href, title in LINK_RE.findall(body):
                if href.startswith("http") and "bing.com" not in href:
                    rows.append({"url": href, "title": clean_text(title), "snippet": clean_text(title), "search_type": kind, "image_url": None})
            if kind == "image":
                for image_url in IMG_RE.findall(body)[:20]:
                    rows.append({"url": image_url, "title": image_url, "snippet": image_url, "search_type": "image", "image_url": image_url})
        except Exception as exc:
            print(f"fallback skipped: {exc}")
    return rows[:per_query * 3]


def search(client: httpx.Client, query: str, per_query: int) -> list[dict]:
    rows = google_search(client, query, per_query, "web")
    rows += google_search(client, query, min(5, per_query), "image")
    if not rows:
        rows = fallback_search(client, query, per_query)
    seen = set()
    deduped = []
    for row in rows:
        key = (row.get("url"), row.get("image_url"))
        if row.get("url") and key not in seen:
            seen.add(key)
            deduped.append(row)
    return deduped[:per_query * 3]


def extract_candidates(result: dict, query_brand: str | None) -> list[dict]:
    text = f"{result['title']} {result['snippet']} {result['url']}"
    if not any(word in text.lower() for word in PRODUCT_WORDS):
        return []
    brands = [query_brand] if query_brand else [brand for brand in KNOWN_BRANDS if brand_in_text(brand, text)]
    found = {}
    for brand in filter(None, brands):
        if not brand_in_text(brand, text):
            continue
        for raw in MODEL_RE.findall(text.upper()):
            model = raw.strip("._-")
            if valid_model(model):
                found[(brand.lower(), normalize_model(model))] = {"brand": brand, "model": model}
    return list(found.values())[:10]


def build_queries(limit: int) -> list[tuple[str, str | None]]:
    priority = [
        ('"True" reach-in refrigerator model gasket', "True"),
        ('"Beverage-Air" refrigerator model gasket', "Beverage-Air"),
        ('"Traulsen" refrigerator model door gasket', "Traulsen"),
        ('"Turbo Air" refrigerator model door gasket', "Turbo Air"),
        ('"Sub-Zero" refrigerator model door gasket', "Sub-Zero"),
    ]
    queries = [(q, None) for q in BASE_QUERIES]
    for brand in KNOWN_BRANDS:
        queries.extend([
            (f'"{brand}" refrigerator model spec sheet', brand),
            (f'"{brand}" freezer model manual', brand),
            (f'"{brand}" door gasket fits model', brand),
        ])
        for site in MARKET_SITES:
            queries.append((f'site:{site} "{brand}" refrigerator model', brand))
    rotation = int(os.getenv("DISCOVERY_QUERY_ROTATION", str(int(time.time() // 1200))))
    if queries:
        offset = rotation % len(queries)
        queries = queries[offset:] + queries[:offset]
    merged = priority + queries
    seen = set()
    out = []
    for item in merged:
        if item[0] not in seen:
            seen.add(item[0])
            out.append(item)
    return out[:limit]


def save_discovery(client: httpx.Client, row: dict) -> dict | None:
    r = client.post(f"{SUPABASE_URL}/rest/v1/{DISCOVERY_TABLE}?on_conflict=normalized_brand,normalized_model,source_url", headers=api_headers("resolution=merge-duplicates,return=representation"), json=row)
    if r.status_code >= 400:
        print(f"save discovery failed: {r.text[:220]}")
        return None
    data = r.json()
    return data[0] if data else None


def find_existing(client: httpx.Client, brand: str, model: str) -> dict | None:
    r = client.get(f"{SUPABASE_URL}/rest/v1/{PRODUCT_TABLE}?select=id,brand,equipment_model&brand=eq.{quote_plus(brand)}&equipment_model=eq.{quote_plus(model)}&limit=1", headers=api_headers())
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def promote(client: httpx.Client, best: dict, aggregate_score: int) -> int | None:
    existing = find_existing(client, best["discovered_brand"], best["discovered_model"])
    row = {
        "brand": best["discovered_brand"],
        "equipment_model": best["discovered_model"],
        "manufacturer": best["discovered_brand"],
        "product_type": best.get("product_type"),
        "official_product_url": best.get("official_product_url"),
        "spec_sheet_url": best.get("spec_sheet_url"),
        "manual_url": best.get("manual_url"),
        "lifecycle_status": "unknown",
        "data_confidence": aggregate_score,
        "last_discovered_at": now_iso(),
        "data_status": "pending",
    }
    if best.get("product_image_url"):
        row["product_image_url"] = best["product_image_url"]
        row["product_image_source_url"] = best.get("source_url")
        row["product_image_confidence"] = min(90, aggregate_score)
    row = {k: v for k, v in row.items() if v is not None}
    if existing:
        r = client.patch(f"{SUPABASE_URL}/rest/v1/{PRODUCT_TABLE}?id=eq.{existing['id']}", headers=api_headers("return=minimal"), json=row)
        r.raise_for_status()
        return existing["id"]
    r = client.post(f"{SUPABASE_URL}/rest/v1/{PRODUCT_TABLE}?on_conflict=brand,equipment_model", headers=api_headers("resolution=ignore-duplicates,return=representation"), json=row)
    r.raise_for_status()
    saved = r.json()
    return saved[0]["id"] if saved else None


def aggregate(client: httpx.Client, saved_rows: list[dict]) -> tuple[int, int]:
    groups = defaultdict(list)
    for row in saved_rows:
        groups[(row["normalized_brand"], row["normalized_model"])].append(row)
    promoted = high = 0
    promote_score = int(os.getenv("DISCOVERY_PROMOTE_SCORE", "80"))
    high_score = int(os.getenv("DISCOVERY_HIGH_CONFIDENCE_SCORE", "65"))
    min_sources = int(os.getenv("DISCOVERY_MIN_INDEPENDENT_SOURCES", "2"))
    for rows in groups.values():
        domains = {domain_of(r.get("source_url", "")) for r in rows if r.get("source_url")}
        kinds = {r.get("evidence", {}).get("source_kind", "public_web") for r in rows}
        best = max(rows, key=lambda r: int(r.get("confidence_score") or 0))
        best_score = int(best.get("confidence_score") or 0)
        has_authority = "manufacturer" in kinds or "manual_or_spec" in kinds
        has_image = any(r.get("product_image_url") for r in rows)
        aggregate_score = int(min(99, best_score * 0.58 + min(len(domains), 4) * 10 + (12 if has_authority else 0) + (5 if has_image else 0)))
        product_id = None
        status = "pending"
        if aggregate_score >= promote_score and (len(domains) >= min_sources or has_authority):
            product_id = promote(client, best, aggregate_score)
            status = "promoted" if product_id else "auto_ready"
            promoted += 1 if product_id else 0
        elif aggregate_score >= high_score:
            status = "high_confidence"
            high += 1
        ids = ",".join(str(r["id"]) for r in rows if r.get("id"))
        if ids:
            payload = {"review_status": status, "confidence_score": aggregate_score}
            if product_id:
                payload["promoted_product_id"] = product_id
            r = client.patch(f"{SUPABASE_URL}/rest/v1/{DISCOVERY_TABLE}?id=in.({ids})", headers=api_headers("return=minimal"), json=payload)
            r.raise_for_status()
    return promoted, high


def main() -> None:
    query_limit = int(os.getenv("DISCOVERY_QUERY_LIMIT", "16"))
    per_query = int(os.getenv("DISCOVERY_RESULTS_PER_QUERY", "10"))
    sleep_seconds = float(os.getenv("DISCOVERY_SLEEP_SECONDS", "0.5"))
    saved_rows = []
    with httpx.Client(timeout=35) as client:
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
                        "product_type": product_type(f"{result['title']} {result['snippet']}"),
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
            if sleep_seconds:
                time.sleep(sleep_seconds)
        promoted, high = aggregate(client, saved_rows)
    print(f"found {len(saved_rows)} candidate evidence rows")
    print(f"promoted {promoted} product models")
    print(f"kept {high} high-confidence candidate groups")
    print("done")


if __name__ == "__main__":
    main()
