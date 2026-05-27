import json
import os
import re
import struct
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from bs4 import BeautifulSoup
import httpx
from dotenv import load_dotenv


load_dotenv(Path(__file__).with_name(".env"))

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

SERPAPI_KEY = os.getenv("SERPAPI_KEY")
GOOGLE_CSE_KEY = os.getenv("GOOGLE_CSE_KEY") or os.getenv("GOOGLE_API_KEY")
GOOGLE_CSE_CX = os.getenv("GOOGLE_CSE_CX") or os.getenv("GOOGLE_CSE_ID")

PRODUCT_TABLE = "refrigerator_products"
CANDIDATE_TABLE = "product_image_candidates"
MIN_PROMOTE_SCORE = float(os.getenv("PRODUCT_IMAGE_MIN_PROMOTE_SCORE", "70"))
RECHECK_WEAK_IMAGES = os.getenv("PRODUCT_IMAGE_RECHECK_WEAK", "0") == "1"
PROMOTE_EXISTING_ONLY = os.getenv("PRODUCT_IMAGE_PROMOTE_EXISTING_ONLY", "0") == "1"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


def supabase_headers(prefer: str | None = None) -> dict[str, str]:
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def normalized(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def model_family_tokens(model: str) -> list[str]:
    value = normalized(model)
    tokens = [value] if value else []
    if len(value) > 2 and value[-2:].isdigit():
        tokens.append(value[:-2])
    if len(value) > 1 and value[-1].isdigit():
        tokens.append(value[:-1])
    return [token for index, token in enumerate(tokens) if token and token not in tokens[:index]]


def get_products(client: httpx.Client, limit: int) -> list[dict]:
    missing_endpoint = (
        f"{SUPABASE_URL}/rest/v1/{PRODUCT_TABLE}"
        "?select=id,brand,equipment_model,product_image_url,product_image_confidence,product_image_verified"
        "&product_image_url=is.null"
        "&order=id.asc"
        f"&limit={limit}"
    )
    response = client.get(missing_endpoint, headers=supabase_headers())
    response.raise_for_status()
    rows = response.json()
    if rows or not RECHECK_WEAK_IMAGES:
        return [row for row in rows if row.get("product_image_verified") is not True]

    weak_endpoint = (
        f"{SUPABASE_URL}/rest/v1/{PRODUCT_TABLE}"
        "?select=id,brand,equipment_model,product_image_url,product_image_confidence,product_image_verified"
        f"&product_image_confidence=lt.{MIN_PROMOTE_SCORE}"
        "&order=product_image_confidence.asc.nullsfirst"
        f"&limit={limit}"
    )
    response = client.get(weak_endpoint, headers=supabase_headers())
    response.raise_for_status()
    return [row for row in response.json() if row.get("product_image_verified") is not True]


def score_candidate(product: dict, candidate: dict) -> float:
    brand = product["brand"]
    model = product["equipment_model"]
    brand_n = normalized(brand)
    model_n = normalized(model)
    family_tokens = [token for token in model_family_tokens(model) if token != model_n]
    haystack = normalized(
        " ".join(
            str(candidate.get(key) or "")
            for key in ["title", "image_url", "page_url", "source_name"]
        )
    )

    score = 0.0
    if brand_n and brand_n in haystack:
        score += 30
    if model_n and model_n in haystack:
        score += 45
    elif any(token and token in haystack for token in family_tokens):
        score += 38
    if any(token in haystack for token in ["REFRIGERATOR", "FREEZER", "FRIDGE", "COOLER"]):
        score += 10
    if any(token in haystack for token in ["FRENCHDOOR", "BOTTOMFREEZER", "SIDEBYSIDE", "REACHIN"]):
        score += 6
    if any(token in haystack for token in ["LOGO", "ICON", "GASKET", "PART", "THUMBNAIL"]):
        score -= 20
    if any(token in haystack for token in ["SMALL", "THUMB", "TINY"]):
        score -= 10
    if "MEDIUM" in haystack:
        score -= 5

    width = int(candidate.get("image_width") or 0)
    height = int(candidate.get("image_height") or 0)
    shortest_side = min(width, height) if width and height else 0
    longest_side = max(width, height) if width and height else 0
    if shortest_side >= 250:
        score += 6
    if shortest_side >= 600:
        score += 8
    if shortest_side >= 900 or longest_side >= 1200:
        score += 7
    if shortest_side and shortest_side < 400:
        score -= 12
    if candidate.get("source_name") in {
        "SerpApi Google Images",
        "Google Custom Search",
        "Bing Images Public Search",
        "Public Product Page",
    }:
        score += 5
    return max(0, min(100, score))


def image_dimensions_from_bytes(data: bytes) -> tuple[int | None, int | None]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])

    if data.startswith(b"\xff\xd8"):
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            index += 2
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(data):
                break
            length = struct.unpack(">H", data[index:index + 2])[0]
            if marker in range(0xC0, 0xC4) or marker in range(0xC5, 0xC8) or marker in range(0xC9, 0xCC) or marker in range(0xCD, 0xD0):
                if index + 7 <= len(data):
                    height, width = struct.unpack(">HH", data[index + 3:index + 7])
                    return width, height
            index += length

    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8X" and len(data) >= 30:
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
            return width, height
        if chunk == b"VP8 " and len(data) >= 30:
            width, height = struct.unpack("<HH", data[26:30])
            return width & 0x3FFF, height & 0x3FFF

    return None, None


def probe_image_dimensions(client: httpx.Client, image_url: str) -> tuple[int | None, int | None]:
    try:
        response = client.get(
            image_url,
            headers={"User-Agent": USER_AGENT, "Range": "bytes=0-524287"},
            follow_redirects=True,
            timeout=20,
        )
        response.raise_for_status()
    except httpx.HTTPError:
        return None, None
    return image_dimensions_from_bytes(response.content[:524288])


def dimensions_from_url(image_url: str) -> tuple[int | None, int | None]:
    patterns = [
        r"(?<!\d)([1-9]\d{2,4})x([1-9]\d{2,4})(?!\d)",
        r"[?&]width=([1-9]\d{2,4})",
        r"(?:^|[,/_-])w[_-]?([1-9]\d{2,4})(?:[,/_-]|$)",
        r"width[_-]?([1-9]\d{2,4})",
    ]
    for pattern in patterns:
        match = re.search(pattern, image_url, re.IGNORECASE)
        if not match:
            continue
        if len(match.groups()) == 2:
            return int(match.group(1)), int(match.group(2))
        value = int(match.group(1))
        return value, value
    return None, None


def search_serpapi(client: httpx.Client, product: dict) -> list[dict]:
    if not SERPAPI_KEY:
        return []
    query = f'{product["brand"]} {product["equipment_model"]} refrigerator product image'
    response = client.get(
        "https://serpapi.com/search.json",
        params={
            "engine": "google_images",
            "q": query,
            "api_key": SERPAPI_KEY,
            "num": 10,
        },
        timeout=30,
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        print(f"SerpApi image search skipped for {product['brand']} {product['equipment_model']}: HTTP {exc.response.status_code}")
        return []
    data = response.json()
    rows = []
    for item in data.get("images_results", [])[:10]:
        rows.append(
            {
                "image_url": item.get("original") or item.get("thumbnail"),
                "page_url": item.get("link"),
                "source_name": "SerpApi Google Images",
                "title": item.get("title"),
                "image_width": item.get("original_width"),
                "image_height": item.get("original_height"),
            }
        )
    return [row for row in rows if row.get("image_url")]


def search_google_cse(client: httpx.Client, product: dict) -> list[dict]:
    if not GOOGLE_CSE_KEY or not GOOGLE_CSE_CX:
        return []
    query = f'{product["brand"]} {product["equipment_model"]} refrigerator'
    response = client.get(
        "https://www.googleapis.com/customsearch/v1",
        params={
            "key": GOOGLE_CSE_KEY,
            "cx": GOOGLE_CSE_CX,
            "q": query,
            "searchType": "image",
            "num": 10,
        },
        timeout=30,
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        hint = "check Custom Search JSON API enablement, key restrictions, and CSE image settings"
        print(f"Google image search skipped for {product['brand']} {product['equipment_model']}: HTTP {status}; {hint}")
        return []
    data = response.json()
    rows = []
    for item in data.get("items", [])[:10]:
        image = item.get("image") or {}
        rows.append(
            {
                "image_url": item.get("link"),
                "page_url": image.get("contextLink"),
                "source_name": "Google Custom Search",
                "title": item.get("title"),
                "image_width": image.get("width"),
                "image_height": image.get("height"),
            }
        )
    return [row for row in rows if row.get("image_url")]


def unwrap_duckduckgo_url(url: str) -> str:
    parsed = urlparse(url)
    if "duckduckgo.com" not in parsed.netloc:
        return url
    target = parse_qs(parsed.query).get("uddg", [""])[0]
    return unquote(target) if target else url


def search_duckduckgo_pages(client: httpx.Client, product: dict) -> list[dict]:
    query = f'{product["brand"]} {product["equipment_model"]} refrigerator product'
    try:
        response = client.get(
            "https://duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"DuckDuckGo page search skipped for {product['brand']} {product['equipment_model']}: {exc.__class__.__name__}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    rows = []
    seen = set()
    for link in soup.select("a.result__a")[:8]:
        page_url = unwrap_duckduckgo_url(link.get("href") or "")
        if not page_url or page_url in seen:
            continue
        seen.add(page_url)
        rows.append(
            {
                "page_url": page_url,
                "source_name": "DuckDuckGo Result Page",
                "title": link.get_text(" ", strip=True),
            }
        )
    return rows


def extract_page_images(client: httpx.Client, page: dict) -> list[dict]:
    page_url = page.get("page_url")
    if not page_url:
        return []
    try:
        response = client.get(
            page_url,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=20,
        )
        response.raise_for_status()
    except httpx.HTTPError:
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    image_urls = []
    for selector, attr in [
        ('meta[property="og:image"]', "content"),
        ('meta[name="twitter:image"]', "content"),
        ('meta[property="og:image:secure_url"]', "content"),
    ]:
        tag = soup.select_one(selector)
        if tag and tag.get(attr):
            image_urls.append(tag[attr])

    for img in soup.select("img")[:20]:
        src = img.get("src") or img.get("data-src") or img.get("data-original")
        alt = img.get("alt") or ""
        if not src:
            continue
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            parsed = urlparse(str(response.url))
            src = f"{parsed.scheme}://{parsed.netloc}{src}"
        if any(token in normalized(src + " " + alt) for token in ["LOGO", "ICON", "SPRITE"]):
            continue
        image_urls.append(src)

    rows = []
    seen = set()
    for image_url in image_urls:
        if not image_url.startswith(("http://", "https://")) or image_url in seen:
            continue
        seen.add(image_url)
        rows.append(
            {
                "image_url": image_url,
                "page_url": page_url,
                "source_name": "Public Product Page",
                "title": page.get("title"),
            }
        )
    return rows[:5]


def search_public_web_images(client: httpx.Client, product: dict) -> list[dict]:
    rows = []
    rows.extend(search_bing_images(client, product))
    if any(score_candidate(product, row) >= MIN_PROMOTE_SCORE for row in rows):
        return rows[:10]
    for page in search_duckduckgo_pages(client, product)[:5]:
        rows.extend(extract_page_images(client, page))
        if len(rows) >= 10:
            break
    return rows[:10]


def search_bing_images(client: httpx.Client, product: dict) -> list[dict]:
    query = f'{product["brand"]} {product["equipment_model"]} refrigerator product image'
    try:
        response = client.get(
            "https://www.bing.com/images/search",
            params={"q": query, "form": "HDRSC2"},
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"Bing image search skipped for {product['brand']} {product['equipment_model']}: {exc.__class__.__name__}")
        return []

    rows = []
    seen = set()
    soup = BeautifulSoup(response.text, "html.parser")
    for item in soup.select("a.iusc"):
        metadata = item.get("m")
        if not metadata:
            continue
        try:
            parsed = json.loads(unescape(metadata))
        except json.JSONDecodeError:
            continue
        image_url = parsed.get("murl") or ""
        page_url = parsed.get("purl") or ""
        if image_url in seen:
            continue
        seen.add(image_url)
        rows.append(
            {
                "image_url": image_url,
                "page_url": page_url,
                "source_name": "Bing Images Public Search",
                "title": parsed.get("t") or f'{product["brand"]} {product["equipment_model"]}',
                "image_width": parsed.get("ow"),
                "image_height": parsed.get("oh"),
            }
        )
        if len(rows) >= 10:
            break
    return rows


def upsert_candidate(client: httpx.Client, product: dict, candidate: dict) -> dict:
    if not candidate.get("image_width") or not candidate.get("image_height"):
        width, height = dimensions_from_url(candidate["image_url"])
        if not width or not height:
            width, height = probe_image_dimensions(client, candidate["image_url"])
        candidate["image_width"] = width
        candidate["image_height"] = height
    score = score_candidate(product, candidate)
    row = {
        "refrigerator_product_id": product["id"],
        "image_url": candidate["image_url"],
        "page_url": candidate.get("page_url"),
        "source_name": candidate.get("source_name"),
        "image_title": candidate.get("title"),
        "image_width": candidate.get("image_width"),
        "image_height": candidate.get("image_height"),
        "match_score": score,
        "evidence": {
            "brand": product["brand"],
            "model": product["equipment_model"],
            "title": candidate.get("title"),
        },
    }
    endpoint = f"{SUPABASE_URL}/rest/v1/{CANDIDATE_TABLE}?on_conflict=refrigerator_product_id,image_url"
    response = client.post(
        endpoint,
        headers=supabase_headers("resolution=merge-duplicates,return=representation"),
        json=row,
    )
    response.raise_for_status()
    saved = response.json()
    return saved[0] if saved else row


def get_existing_candidates(client: httpx.Client, product_id: int, limit: int = 20) -> list[dict]:
    endpoint = (
        f"{SUPABASE_URL}/rest/v1/{CANDIDATE_TABLE}"
        "?select=*"
        f"&refrigerator_product_id=eq.{product_id}"
        f"&match_score=gte.{MIN_PROMOTE_SCORE}"
        "&order=match_score.desc"
        f"&limit={limit}"
    )
    response = client.get(endpoint, headers=supabase_headers())
    response.raise_for_status()
    return response.json()


def is_usable_image(candidate: dict) -> bool:
    score = float(candidate.get("match_score") or 0)
    if score < MIN_PROMOTE_SCORE:
        return False

    width = int(candidate.get("image_width") or 0)
    height = int(candidate.get("image_height") or 0)
    if width and height and min(width, height) < 250:
        return False

    haystack = normalized(
        " ".join(
            str(candidate.get(key) or "")
            for key in ["image_title", "title", "image_url", "page_url", "source_name"]
        )
    )
    if any(token in haystack for token in ["LOGO", "ICON", "GASKET", "PART", "THUMBNAIL"]):
        return score >= 90
    return True


def image_quality_score(candidate: dict) -> float:
    text = normalized(
        " ".join(
            str(candidate.get(key) or "")
            for key in ["image_title", "title", "image_url", "page_url", "source_name"]
        )
    )
    score = float(candidate.get("match_score") or 0)
    if any(domain in text for domain in ["AJMADISON", "BESTBUY", "LOWES", "HOMEDEPOT", "CANADIANAPPLIANCE"]):
        score += 8
    if any(token in text for token in ["FRONTCLOSED", "FRONTCLOSE", "FRONT", "MAIN"]):
        score += 5
    if any(token in text for token in ["OPEN", "INTERIOR", "PARTS", "REVIEW"]):
        score -= 4
    width = int(candidate.get("image_width") or 0)
    height = int(candidate.get("image_height") or 0)
    if width and height:
        score += min(6, max(width, height) / 250)
    return score


def promote_best_image(client: httpx.Client, product: dict, candidates: list[dict]) -> bool:
    usable = [candidate for candidate in candidates if is_usable_image(candidate)]
    if not usable:
        return False
    best = max(usable, key=image_quality_score)
    best_score = float(best.get("match_score") or 0)
    current_score = float(product.get("product_image_confidence") or 0)
    current_candidate = {
        "match_score": current_score,
        "image_url": product.get("product_image_url"),
        "page_url": product.get("product_image_source_url"),
    }
    if (
        best_score < current_score
        or (
            best_score == current_score
            and product.get("product_image_url")
            and image_quality_score(best) <= image_quality_score(current_candidate)
        )
    ):
        return False

    endpoint = f"{SUPABASE_URL}/rest/v1/{PRODUCT_TABLE}?id=eq.{product['id']}"
    response = client.patch(
        endpoint,
        headers=supabase_headers("return=minimal"),
        json={
            "product_image_url": best["image_url"],
            "product_image_confidence": best["match_score"],
            "product_image_source_url": best.get("page_url"),
        },
    )
    response.raise_for_status()

    candidate_id = best.get("id")
    if candidate_id:
        response = client.patch(
            f"{SUPABASE_URL}/rest/v1/{CANDIDATE_TABLE}?id=eq.{candidate_id}",
            headers=supabase_headers("return=minimal"),
            json={"is_selected": True},
        )
        response.raise_for_status()
    return True


def main() -> None:
    limit = int(os.getenv("PRODUCT_IMAGE_LIMIT", "100"))
    with httpx.Client(timeout=30) as client:
        products = get_products(client, limit)
        print(f"searching product images for {len(products)} products")
        promoted = 0
        saved_count = 0
        for product in products:
            saved = get_existing_candidates(client, product["id"])
            promoted_this = promote_best_image(client, product, saved)
            if not promoted_this and not PROMOTE_EXISTING_ONLY:
                raw_candidates = []
                raw_candidates.extend(search_serpapi(client, product))
                raw_candidates.extend(search_google_cse(client, product))
                if not raw_candidates:
                    raw_candidates.extend(search_public_web_images(client, product))
                saved = [upsert_candidate(client, product, row) for row in raw_candidates]
                promoted_this = promote_best_image(client, product, saved)
            saved_count += len(saved)
            if promoted_this:
                promoted += 1
        print(f"saved candidates: {saved_count}")
        print(f"promoted product images: {promoted}")
        print("done")


if __name__ == "__main__":
    main()
