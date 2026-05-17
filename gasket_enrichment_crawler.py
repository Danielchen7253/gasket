import os
import re
import time
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv


load_dotenv(Path(__file__).with_name(".env"))

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "12"))
CRAWL_DELAY = float(os.getenv("CRAWL_DELAY", "0"))

MODEL_TABLE = "refrigerator_products"
DETAIL_TABLE = "gasket_details"
SPEC_TABLE = "product_gasket_specs"
PART_TABLE = "gasket_parts"
APPLICATION_TABLE = "refrigerator_gasket_applications"

DIMENSION_RE = re.compile(
    r"(?P<w>\d+(?:\s+\d+/\d+)?|\d+/\d+|\d+(?:\.\d+)?)\s*(?:\"|in\.?|inch(?:es)?)?\s*[xX]\s*"
    r"(?P<h>\d+(?:\s+\d+/\d+)?|\d+/\d+|\d+(?:\.\d+)?)\s*(?:\"|in\.?|inch(?:es)?)?",
    re.I,
)
PART_RE = re.compile(r"\b(?:part(?:\s*#|\s*number)?|mpn|sku)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-./]{2,30})", re.I)
BAD_PART_NUMBERS = {
    "REPLACES",
    "NERS",
    "TOWN",
    "SPIN",
    "ORDER",
    "THAT",
    "ENSURES",
    "FOR",
    "RUIN",
}


@dataclass(frozen=True)
class SearchTarget:
    name: str
    url_template: str


SEARCH_TARGETS = [
    SearchTarget("WebstaurantStore", "https://www.webstaurantstore.com/search/{query}.html"),
    SearchTarget("Parts Town", "https://www.partstown.com/search?q={query}"),
    SearchTarget("Restaurant Cooler Gaskets", "https://restaurantcoolergaskets.com/catalogsearch/result/?q={query}"),
    SearchTarget("Cooler Gaskets", "https://www.coolergaskets.com/search?q={query}"),
    SearchTarget("Gaskets Unlimited", "https://www.gasketsunlimited.com/search?q={query}"),
    SearchTarget("PartsFe", "https://partsfe.com/search?q={query}"),
    SearchTarget("PartsFPS", "https://www.partsfps.com/search?keyword={query}"),
]

PUBLIC_SEARCH_LIMIT = int(os.getenv("GASKET_PUBLIC_SEARCH_LIMIT", "8"))
DETAIL_LINK_LIMIT = int(os.getenv("GASKET_DETAIL_LINK_LIMIT", "8"))
MIN_CANDIDATE_SCORE = float(os.getenv("GASKET_MIN_CANDIDATE_SCORE", "35"))
MAX_SEARCH_URLS_PER_MODEL = int(os.getenv("GASKET_MAX_SEARCH_URLS_PER_MODEL", "24"))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


def fraction_to_float(value: str) -> float | None:
    value = value.strip().replace('"', "")
    try:
        if " " in value:
            whole, frac = value.split(" ", 1)
            num, den = frac.split("/", 1)
            return float(whole) + float(num) / float(den)
        if "/" in value:
            num, den = value.split("/", 1)
            return float(num) / float(den)
        return float(value)
    except Exception:
        return None


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalized(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def model_variants(model: str) -> list[str]:
    base = clean_text(model).upper()
    variants = {
        base,
        base.replace("-", ""),
        base.replace("/", ""),
        base.replace(" ", ""),
        base.replace("-", " "),
        base.replace("/", " "),
    }
    if "-" not in base and len(base) > 3:
        match = re.match(r"^([A-Z]+)(\d.*)$", base)
        if match:
            variants.add(f"{match.group(1)}-{match.group(2)}")
    return [variant for variant in variants if variant]


def canonical_part_number(value: str | None) -> str | None:
    if not value:
        return None
    part = re.sub(r"\s+", "", value.strip().upper())
    part = part.strip(".,;:")
    if len(part) < 3 or len(part) > 40:
        return None
    if not re.search(r"\d", part):
        return None
    if part in BAD_PART_NUMBERS:
        return None
    if re.fullmatch(r"(?:COM|NET|JPG|PNG|HTTP|HTTPS|BUY|NOW|MODEL|NUMBER)", part):
        return None
    return part


def is_valid_gasket_detail(detail: dict) -> bool:
    name = (detail.get("gasket_name") or "").lower()
    if any(token in name for token in ["cutting board", "hinge", "caster", "shelf"]):
        return False
    if detail.get("gasket_part_number") and not canonical_part_number(detail["gasket_part_number"]):
        detail["gasket_part_number"] = None
    return bool(
        detail.get("dimensions_text")
        or detail.get("width_in")
        or detail.get("height_in")
        or detail.get("gasket_part_number")
        or "gasket" in name
    )


def fetch(client: httpx.Client, url: str) -> str:
    response = client.get(
        url,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    return response.text


def image_candidates(base_url: str, soup: BeautifulSoup) -> dict[str, str | None]:
    images = []
    for img in soup.select("img[src], img[data-src], img[data-original]"):
        src = img.get("src") or img.get("data-src") or img.get("data-original")
        alt = clean_text(img.get("alt", ""))
        if not src:
            continue
        images.append((alt.lower(), urljoin(base_url, src)))

    gasket_image = None
    profile_image = None
    refrigerator_image = None
    for alt, src in images:
        haystack = f"{alt} {src.lower()}"
        if not profile_image and any(token in haystack for token in ["profile", "cross", "section"]):
            profile_image = src
        elif not gasket_image and "gasket" in haystack:
            gasket_image = src
        elif not refrigerator_image and any(token in haystack for token in ["refrigerator", "freezer", "fridge"]):
            refrigerator_image = src

    return {
        "refrigerator_image_url": refrigerator_image,
        "gasket_image_url": gasket_image,
        "profile_image_url": profile_image,
    }


def detail_links(base_url: str, html: str, brand: str, model: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(base_url).netloc.lower()
    candidates = []
    tokens = [
        brand.lower(),
        model.lower(),
        model.lower().replace("-", ""),
        "gasket",
        "door-seal",
        "door seal",
    ]

    for a in soup.select("a[href]"):
        href = a.get("href")
        if not href:
            continue
        absolute = urljoin(base_url, href).split("#")[0]
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"}:
            continue
        if parsed.netloc.lower() != base_host:
            continue

        label = clean_text(a.get_text(" ")).lower()
        haystack = f"{label} {absolute.lower()}"
        score = sum(1 for token in tokens if token and token in haystack)
        if score >= 2 or ("gasket" in haystack and model.lower() in haystack):
            candidates.append((score, absolute))

    candidates.sort(reverse=True)
    return list(dict.fromkeys(url for _, url in candidates[:8]))


def extract_detail(source_name: str, url: str, html: str, brand: str = "", model: str = "") -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    title = clean_text(soup.title.get_text(" ")) if soup.title else None
    text = clean_text(soup.get_text(" "))
    lower = f"{title or ''} {text}".lower()
    if "gasket" not in lower and "door seal" not in lower:
        return None

    dimensions_text = None
    width_in = None
    height_in = None
    dim_match = DIMENSION_RE.search(text)
    if dim_match:
        dimensions_text = dim_match.group(0)
        width_in = fraction_to_float(dim_match.group("w"))
        height_in = fraction_to_float(dim_match.group("h"))

    part_number = None
    part_match = PART_RE.search(text)
    if part_match:
        part_number = canonical_part_number(part_match.group(1))

    images = image_candidates(url, soup)
    confidence = 0
    confidence += 30 if dimensions_text else 0
    confidence += 25 if part_number else 0
    confidence += 20 if images.get("gasket_image_url") else 0
    confidence += 15 if images.get("profile_image_url") else 0
    confidence += 10 if title and "gasket" in title.lower() else 0
    confidence += 15 if brand and brand.lower() in lower else 0
    confidence += 25 if model and normalized(model) in normalized(lower) else 0
    confidence += 10 if any(variant.lower() in lower for variant in model_variants(model)) else 0
    confidence += 5 if any(token in lower for token in ["fits", "compatible", "replacement"]) else 0

    if confidence < MIN_CANDIDATE_SCORE:
        return None

    return {
        "gasket_part_number": part_number,
        "universal_part_number": part_number,
        "gasket_name": title,
        "width_in": width_in,
        "height_in": height_in,
        "dimensions_text": dimensions_text,
        "source_url": url,
        "source_name": source_name,
        "confidence_score": min(100, confidence),
        **images,
    }


def candidate_urls(brand: str, model: str) -> list[tuple[str, str]]:
    urls = []
    seen = set()
    for variant in model_variants(model)[:6]:
        queries = [
            f"{brand} {variant} refrigerator door gasket",
            f"{brand} {variant} gasket",
            f"{brand} {variant} door seal",
            f"{brand} {variant} gasket dimensions",
            f"{brand} {variant} gasket profile",
            f"{brand} {variant} parts list gasket",
        ]
        for query_text in queries:
            query = quote_plus(query_text)
            for target in SEARCH_TARGETS:
                url = target.url_template.format(query=query)
                key = (target.name, url)
                if key not in seen:
                    seen.add(key)
                    urls.append(key)
    return urls


def unwrap_search_url(url: str) -> str:
    parsed = urlparse(url)
    if "bing.com" in parsed.netloc and parsed.path.startswith("/ck/"):
        target = parse_qs(parsed.query).get("u", [""])[0]
        if target.startswith("a1"):
            target = target[2:]
        return unquote(target) if target else url
    if "duckduckgo.com" in parsed.netloc:
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(target) if target else url
    return url


def public_search_results(client: httpx.Client, brand: str, model: str) -> list[tuple[str, str]]:
    results = []
    seen = set()
    queries = []
    for variant in model_variants(model)[:4]:
        queries.extend(
            [
                f'"{brand}" "{variant}" "door gasket"',
                f'"{brand}" "{variant}" "gasket"',
                f'"{brand}" "{variant}" "door seal"',
                f'"{brand}" "{variant}" "parts manual" gasket',
            ]
        )

    for query in queries[:PUBLIC_SEARCH_LIMIT]:
        try:
            response = client.get(
                "https://www.bing.com/search",
                params={"q": query, "count": "8"},
                headers={"User-Agent": USER_AGENT},
                timeout=HTTP_TIMEOUT,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            print(f"skip public search {query}: {exc.__class__.__name__}")
            continue

        soup = BeautifulSoup(response.text, "html.parser")
        for link in soup.select("li.b_algo h2 a[href], a[href]"):
            url = unwrap_search_url(link.get("href") or "")
            if not url.startswith(("http://", "https://")):
                continue
            host = urlparse(url).netloc.lower()
            if any(blocked in host for blocked in ["bing.com", "microsoft.com", "linkedin.com", "facebook.com"]):
                continue
            label = clean_text(link.get_text(" ", strip=True))
            haystack = f"{label} {url}".lower()
            if "gasket" not in haystack and "door-seal" not in haystack and "door seal" not in haystack and "parts" not in haystack:
                continue
            if url in seen:
                continue
            seen.add(url)
            results.append(("Public Web", url))
            if len(results) >= PUBLIC_SEARCH_LIMIT * 4:
                return results
    return results


def supabase_headers(prefer: str | None = None) -> dict[str, str]:
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def find_detail_from_search(
    client: httpx.Client,
    source_name: str,
    search_url: str,
    brand: str,
    model: str,
) -> dict | None:
    html = fetch(client, search_url)
    detail = extract_detail(source_name, search_url, html, brand, model)
    if detail and detail["confidence_score"] >= 60:
        return detail

    for link in detail_links(search_url, html, brand, model)[:DETAIL_LINK_LIMIT]:
        try:
            product_html = fetch(client, link)
        except Exception as exc:
            print(f"skip detail {link}: {exc}")
            continue
        product_detail = extract_detail(source_name, link, product_html, brand, model)
        if product_detail:
            return product_detail
        if CRAWL_DELAY:
            time.sleep(CRAWL_DELAY)

    return detail


def get_pending_models(client: httpx.Client, limit: int = 50) -> list[dict]:
    direct_endpoint = (
        f"{SUPABASE_URL.rstrip('/')}/rest/v1/{MODEL_TABLE}"
        "?select=id,brand,equipment_model,data_status"
        "&data_status=in.(pending,missing)"
        f"&limit={limit}"
    )
    response = client.get(direct_endpoint, headers=supabase_headers())
    response.raise_for_status()
    direct_rows = response.json()
    if direct_rows:
        return direct_rows

    specs_endpoint = (
        f"{SUPABASE_URL.rstrip('/')}/rest/v1/{SPEC_TABLE}"
        "?select=refrigerator_product_id"
        "&data_status=eq.missing"
        f"&limit={limit}"
    )
    response = client.get(specs_endpoint, headers=supabase_headers())
    response.raise_for_status()
    ids = [str(row["refrigerator_product_id"]) for row in response.json()]
    if not ids:
        return []

    endpoint = (
        f"{SUPABASE_URL.rstrip('/')}/rest/v1/{MODEL_TABLE}"
        f"?select=id,brand,equipment_model,data_status&id=in.({','.join(ids)})"
    )
    response = client.get(endpoint, headers=supabase_headers())
    response.raise_for_status()
    return response.json()


def upsert_detail(client: httpx.Client, detail: dict) -> dict:
    endpoint = (
        f"{SUPABASE_URL.rstrip('/')}/rest/v1/{DETAIL_TABLE}"
        "?on_conflict=refrigerator_product_id,source_url,gasket_part_number"
    )
    response = client.post(
        endpoint,
        headers=supabase_headers("resolution=merge-duplicates,return=representation"),
        json=detail,
    )
    response.raise_for_status()
    saved = response.json()
    return saved[0] if saved else detail


def upsert_gasket_part(client: httpx.Client, detail: dict) -> int | None:
    part = canonical_part_number(detail.get("gasket_part_number"))
    if not part:
        return None
    row = {
        "canonical_part_number": part,
        "universal_part_number": part,
        "part_name": detail.get("gasket_name"),
        "width_in": detail.get("width_in"),
        "height_in": detail.get("height_in"),
        "dimensions_text": detail.get("dimensions_text"),
        "gasket_profile": detail.get("gasket_profile"),
        "gasket_image_url": detail.get("gasket_image_url"),
        "profile_image_url": detail.get("profile_image_url"),
        "source_url": detail.get("source_url"),
        "source_name": detail.get("source_name"),
        "confidence_score": detail.get("confidence_score") or 0,
    }
    endpoint = (
        f"{SUPABASE_URL.rstrip('/')}/rest/v1/{PART_TABLE}"
        "?on_conflict=canonical_part_number"
    )
    response = client.post(
        endpoint,
        headers=supabase_headers("resolution=merge-duplicates,return=representation"),
        json=row,
    )
    response.raise_for_status()
    saved = response.json()
    return saved[0]["id"] if saved else None


def link_detail_to_part(client: httpx.Client, detail_id: int, part_id: int | None) -> None:
    if not part_id:
        return
    endpoint = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{DETAIL_TABLE}?id=eq.{detail_id}"
    response = client.patch(
        endpoint,
        headers=supabase_headers("return=minimal"),
        json={"gasket_part_id": part_id},
    )
    response.raise_for_status()


def upsert_application(client: httpx.Client, detail: dict, part_id: int | None) -> None:
    if not part_id:
        return
    row = {
        "refrigerator_product_id": detail["refrigerator_product_id"],
        "gasket_part_id": part_id,
        "gasket_detail_id": detail.get("id"),
        "door_position": detail.get("door_position"),
        "source_url": detail.get("source_url"),
        "source_name": detail.get("source_name"),
        "confidence_score": detail.get("confidence_score") or 0,
        "is_verified": detail.get("is_verified") or False,
    }
    endpoint = (
        f"{SUPABASE_URL.rstrip('/')}/rest/v1/{APPLICATION_TABLE}"
        "?on_conflict=refrigerator_product_id,gasket_part_id,door_position"
    )
    response = client.post(
        endpoint,
        headers=supabase_headers("resolution=merge-duplicates,return=minimal"),
        json=row,
    )
    if response.status_code >= 400:
        response = client.post(
            f"{SUPABASE_URL.rstrip('/')}/rest/v1/{APPLICATION_TABLE}",
            headers=supabase_headers("return=minimal"),
            json=row,
        )
    response.raise_for_status()


def get_details_for_product(client: httpx.Client, product_id: int) -> list[dict]:
    endpoint = (
        f"{SUPABASE_URL.rstrip('/')}/rest/v1/{DETAIL_TABLE}"
        f"?select=*&refrigerator_product_id=eq.{product_id}"
        "&order=confidence_score.desc"
    )
    response = client.get(endpoint, headers=supabase_headers())
    response.raise_for_status()
    return [row for row in response.json() if is_valid_gasket_detail(row)]


def refresh_product_gasket_spec(client: httpx.Client, product_id: int) -> None:
    details = get_details_for_product(client, product_id)
    if not details:
        row = {
            "refrigerator_product_id": product_id,
            "doors": [],
            "source_summary": [],
            "confidence_score": 0,
            "data_status": "missing",
        }
    else:
        best = details[0]
        sources = []
        seen_sources = set()
        doors = []
        for detail in details[:8]:
            source_key = (detail.get("source_name"), detail.get("source_url"))
            if source_key not in seen_sources:
                seen_sources.add(source_key)
                sources.append({"source_name": detail.get("source_name"), "source_url": detail.get("source_url")})
            doors.append(
                {
                    "door_position": detail.get("door_position"),
                    "width_in": detail.get("width_in"),
                    "height_in": detail.get("height_in"),
                    "dimensions_text": detail.get("dimensions_text"),
                    "part_number": detail.get("gasket_part_number"),
                    "universal_part_number": detail.get("universal_part_number"),
                    "gasket_part_id": detail.get("gasket_part_id"),
                    "gasket_name": detail.get("gasket_name"),
                    "gasket_profile": detail.get("gasket_profile"),
                    "gasket_image_url": detail.get("gasket_image_url"),
                    "profile_image_url": detail.get("profile_image_url"),
                    "source_url": detail.get("source_url"),
                    "source_name": detail.get("source_name"),
                    "confidence_score": detail.get("confidence_score"),
                    "is_verified": detail.get("is_verified") or False,
                }
            )
        row = {
            "refrigerator_product_id": product_id,
            "primary_gasket_part_id": best.get("gasket_part_id"),
            "primary_part_number": best.get("gasket_part_number"),
            "universal_part_number": best.get("universal_part_number"),
            "gasket_name": best.get("gasket_name"),
            "gasket_profile": best.get("gasket_profile"),
            "doors": doors,
            "source_summary": sources,
            "best_source_url": best.get("source_url"),
            "best_source_name": best.get("source_name"),
            "confidence_score": best.get("confidence_score") or 0,
            "data_status": "verified" if any(detail.get("is_verified") for detail in details) else "candidate",
            "is_verified": any(detail.get("is_verified") for detail in details),
        }

    endpoint = (
        f"{SUPABASE_URL.rstrip('/')}/rest/v1/{SPEC_TABLE}"
        "?on_conflict=refrigerator_product_id"
    )
    response = client.post(
        endpoint,
        headers=supabase_headers("resolution=merge-duplicates,return=minimal"),
        json=row,
    )
    response.raise_for_status()


def update_model_status(client: httpx.Client, model_id: int, status: str) -> None:
    endpoint = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{MODEL_TABLE}?id=eq.{model_id}"
    response = client.patch(
        endpoint,
        headers=supabase_headers("return=minimal"),
        json={"data_status": status},
    )
    response.raise_for_status()


def main() -> None:
    inserted = 0
    with httpx.Client(timeout=60) as client:
        models = get_pending_models(client, limit=int(os.getenv("ENRICH_LIMIT", "50")))
        print(f"enriching {len(models)} models")
        for model_row in models:
            model_id = model_row["id"]
            brand = model_row["brand"]
            equipment_model = model_row["equipment_model"]
            found_any = False

            search_urls = candidate_urls(brand, equipment_model)
            try:
                search_urls.extend(public_search_results(client, brand, equipment_model))
            except Exception as exc:
                print(f"skip public search for {brand} {equipment_model}: {exc}")

            seen_urls = set()
            deduped_urls = []
            for item in search_urls:
                if item[1] in seen_urls:
                    continue
                seen_urls.add(item[1])
                deduped_urls.append(item)

            for source_name, url in deduped_urls[:MAX_SEARCH_URLS_PER_MODEL]:
                try:
                    detail = find_detail_from_search(
                        client, source_name, url, brand, equipment_model
                    )
                except Exception as exc:
                    print(f"skip {brand} {equipment_model} via {source_name}: {exc}")
                    continue

                if not detail:
                    continue
                if not is_valid_gasket_detail(detail):
                    continue

                detail["refrigerator_product_id"] = model_id
                saved_detail = upsert_detail(client, detail)
                part_id = upsert_gasket_part(client, saved_detail)
                if part_id:
                    saved_detail["gasket_part_id"] = part_id
                    link_detail_to_part(client, saved_detail["id"], part_id)
                    upsert_application(client, saved_detail, part_id)
                inserted += 1
                found_any = True
                if CRAWL_DELAY:
                    time.sleep(CRAWL_DELAY)

            update_model_status(client, model_id, "enriched" if found_any else "not_found")
            refresh_product_gasket_spec(client, model_id)

    print(f"inserted detail rows: {inserted}")
    print("done")


if __name__ == "__main__":
    main()
