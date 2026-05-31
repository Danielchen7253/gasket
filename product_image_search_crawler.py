import json
import os
import re
import struct
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

from bs4 import BeautifulSoup
import httpx
from dotenv import load_dotenv

from trusted_sources import trusted_site_query


load_dotenv(Path(__file__).with_name(".env"))

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

SERPAPI_KEY = os.getenv("SERPAPI_KEY")
GOOGLE_CSE_KEY = os.getenv("GOOGLE_CSE_KEY") or os.getenv("GOOGLE_API_KEY")
GOOGLE_CSE_CX = os.getenv("GOOGLE_CSE_CX") or os.getenv("GOOGLE_CSE_ID")
BRAVE_SEARCH_API_KEY = os.getenv("BRAVE_SEARCH_API_KEY")
BING_SEARCH_API_KEY = os.getenv("BING_SEARCH_API_KEY")
BING_SEARCH_ENDPOINT = (os.getenv("BING_SEARCH_ENDPOINT") or "https://api.bing.microsoft.com/v7.0").rstrip("/")

PRODUCT_TABLE = "refrigerator_products"
CANDIDATE_TABLE = "product_image_candidates"
MIN_PROMOTE_SCORE = float(os.getenv("PRODUCT_IMAGE_MIN_PROMOTE_SCORE", "70"))
RECHECK_WEAK_IMAGES = os.getenv("PRODUCT_IMAGE_RECHECK_WEAK", "0") == "1"
PROMOTE_EXISTING_ONLY = os.getenv("PRODUCT_IMAGE_PROMOTE_EXISTING_ONLY", "0") == "1"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
BLOCKED_IMAGE_DOMAINS = {
    "xhamster.com",
    "xhamster.desi",
    "pornhub.com",
    "xvideos.com",
    "deviantart.com",
    "womensalphabet.com",
    "scribd.com",
    "yumpu.com",
    "sec.gov",
}
BAD_IMAGE_TOKENS = [
    "LOGO",
    "ICON",
    "GASKET",
    "SEAL",
    "PART",
    "PARTS",
    "DIAGRAM",
    "MANUAL",
    "THUMBNAIL",
    "PORN",
    "SEX",
    "ADULT",
    "NUDE",
    "BBW",
    "GALLERY",
    "DEVIANTART",
    "SCRIBD",
]
NON_PRODUCT_IMAGE_TOKENS = [
    "WATERFILTER",
    "AIRFILTER",
    "REPAIRHELP",
    "PARTSELECT",
    "PARTSDIRECT",
    "SEARSPARTSDIRECT",
    "YOUTUBE",
    "MAXRESDEFAULT",
    "CONSUMERREPORTS",
    "PDCATEGORY",
]


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


def url_domain(url: str | None) -> str:
    try:
        return urlparse(url or "").netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def is_blocked_domain(url: str | None) -> bool:
    domain = url_domain(url)
    return any(domain == blocked or domain.endswith("." + blocked) for blocked in BLOCKED_IMAGE_DOMAINS)


def is_displayable_image_url(client: httpx.Client, url: str | None, timeout: float = 5.0) -> bool:
    """Return True only when the stored URL currently resolves to an image-like response."""
    if not url or not str(url).startswith(("http://", "https://")):
        return False
    if is_blocked_domain(url):
        return False

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    }
    image_ext = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")

    def looks_like_image(response: httpx.Response) -> bool:
        if response.status_code < 200 or response.status_code >= 400:
            return False
        content_type = (response.headers.get("content-type") or "").lower()
        clean_url = str(response.url).split("?", 1)[0].lower()
        if "image/" in content_type:
            return True
        if clean_url.endswith(image_ext) and (not content_type or any(token in content_type for token in ["octet-stream", "binary"])):
            return True
        return False

    try:
        response = client.head(url, headers=headers, timeout=timeout, follow_redirects=True)
        if looks_like_image(response):
            return True
        if response.status_code in {403, 405} or response.status_code < 200 or response.status_code >= 400:
            response = client.get(
                url,
                headers={**headers, "Range": "bytes=0-4095"},
                timeout=timeout,
                follow_redirects=True,
            )
            return looks_like_image(response)
        return False
    except Exception:
        return False


def model_family_tokens(model: str) -> list[str]:
    value = normalized(model)
    tokens = [value] if value else []
    if len(value) > 2 and value[-2:].isdigit():
        tokens.append(value[:-2])
    if len(value) > 1 and value[-1].isdigit():
        tokens.append(value[:-1])
    if len(value) >= 6:
        tokens.append(value[:6])
    if len(value) >= 5:
        tokens.append(value[:5])
    if len(value) >= 3:
        tokens.append(value[:3])
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
    product_type_n = normalized(product.get("product_type") or "")
    if "FRENCHDOOR" in product_type_n and "FRENCHDOOR" in haystack:
        score += 18
    if "BOTTOMFREEZER" in product_type_n and "BOTTOMFREEZER" in haystack:
        score += 12
    if brand_n and brand_n in haystack and family_tokens and any(token and token in haystack for token in family_tokens[-3:]):
        score += 12
    if any(domain in haystack for domain in ["AJMADISON", "BESTBUY", "LOWES", "HOMEDEPOT", "CANADIANAPPLIANCE"]):
        score += 8
    if any(token in haystack for token in ["LOGO", "ICON", "GASKET", "PART", "THUMBNAIL"]):
        score -= 20
    if any(token in haystack for token in NON_PRODUCT_IMAGE_TOKENS):
        score -= 35
    if any(token in haystack for token in BAD_IMAGE_TOKENS):
        score -= 35
    if is_blocked_domain(candidate.get("image_url")) or is_blocked_domain(candidate.get("page_url")):
        score -= 80
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
        "Brave Image Search",
        "Bing Image Search API",
        "Bing Images Public Search",
        "Public Product Page",
        "Public Product Search Page",
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


def search_brave_images(client: httpx.Client, product: dict) -> list[dict]:
    if not BRAVE_SEARCH_API_KEY:
        return []
    query = f'{product["brand"]} {product["equipment_model"]} refrigerator product image'
    try:
        response = client.get(
            "https://api.search.brave.com/res/v1/images/search",
            params={"q": query, "count": 10, "safesearch": "strict"},
            headers={"Accept": "application/json", "X-Subscription-Token": BRAVE_SEARCH_API_KEY},
            timeout=30,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"Brave image search skipped for {product['brand']} {product['equipment_model']}: {exc.__class__.__name__}")
        return []
    rows = []
    for item in response.json().get("results", [])[:10]:
        image = item.get("properties") or {}
        thumbnail = item.get("thumbnail") or {}
        rows.append(
            {
                "image_url": image.get("url") or thumbnail.get("src") or item.get("url"),
                "page_url": item.get("url"),
                "source_name": "Brave Image Search",
                "title": item.get("title"),
                "image_width": image.get("width"),
                "image_height": image.get("height"),
            }
        )
    return [row for row in rows if row.get("image_url")]


def search_brave_pages(client: httpx.Client, product: dict) -> list[dict]:
    if not BRAVE_SEARCH_API_KEY:
        return []
    query = f'{product["brand"]} {product["equipment_model"]} refrigerator product'
    try:
        response = client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": 10, "safesearch": "strict"},
            headers={"Accept": "application/json", "X-Subscription-Token": BRAVE_SEARCH_API_KEY},
            timeout=30,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"Brave web search skipped for {product['brand']} {product['equipment_model']}: {exc.__class__.__name__}")
        return []
    rows = []
    for item in (response.json().get("web") or {}).get("results", [])[:10]:
        page_url = item.get("url")
        if not page_url:
            continue
        rows.append({"page_url": page_url, "source_name": "Brave Web Result Page", "title": item.get("title")})
    return rows


def search_bing_api_images(client: httpx.Client, product: dict) -> list[dict]:
    if not BING_SEARCH_API_KEY:
        return []
    query = f'{product["brand"]} {product["equipment_model"]} refrigerator product image'
    try:
        response = client.get(
            f"{BING_SEARCH_ENDPOINT}/images/search",
            params={"q": query, "count": 10, "safeSearch": "Strict", "mkt": "en-US"},
            headers={"Ocp-Apim-Subscription-Key": BING_SEARCH_API_KEY},
            timeout=30,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"Bing Image Search API skipped for {product['brand']} {product['equipment_model']}: {exc.__class__.__name__}")
        return []
    rows = []
    for item in response.json().get("value", [])[:10]:
        rows.append(
            {
                "image_url": item.get("contentUrl") or item.get("thumbnailUrl"),
                "page_url": item.get("hostPageUrl"),
                "source_name": "Bing Image Search API",
                "title": item.get("name"),
                "image_width": item.get("width"),
                "image_height": item.get("height"),
            }
        )
    return [row for row in rows if row.get("image_url")]


def search_bing_api_pages(client: httpx.Client, product: dict) -> list[dict]:
    if not BING_SEARCH_API_KEY:
        return []
    query = f'{product["brand"]} {product["equipment_model"]} refrigerator product'
    try:
        response = client.get(
            f"{BING_SEARCH_ENDPOINT}/search",
            params={"q": query, "count": 10, "safeSearch": "Strict", "mkt": "en-US"},
            headers={"Ocp-Apim-Subscription-Key": BING_SEARCH_API_KEY},
            timeout=30,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"Bing Web Search API skipped for {product['brand']} {product['equipment_model']}: {exc.__class__.__name__}")
        return []
    rows = []
    for item in (response.json().get("webPages") or {}).get("value", [])[:10]:
        page_url = item.get("url")
        if not page_url:
            continue
        rows.append({"page_url": page_url, "source_name": "Bing Web Result Page", "title": item.get("name")})
    return rows


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
        try:
            message = (exc.response.json().get("error") or {}).get("message")
        except Exception:
            message = None
        if message:
            hint = f"{hint}; Google says: {message}"
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
    query = f'{product["brand"]} {product["equipment_model"]} refrigerator product ({trusted_site_query(45)})'
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


def search_duckduckgo_pages_wide(client: httpx.Client, product: dict) -> list[dict]:
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
        print(f"DuckDuckGo wide page search skipped for {product['brand']} {product['equipment_model']}: {exc.__class__.__name__}")
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
                "source_name": "DuckDuckGo Wide Result Page",
                "title": link.get_text(" ", strip=True),
            }
        )
    return rows


def first_srcset_url(value: str | None) -> str:
    if not value:
        return ""
    first = value.split(",", 1)[0].strip()
    return first.split(" ", 1)[0].strip()


def normalize_page_image_url(base_url: str, src: str | None) -> str:
    if not src:
        return ""
    src = unescape(src.strip())
    if not src or src.startswith("data:"):
        return ""
    if src.startswith("//"):
        return "https:" + src
    return urljoin(base_url, src)


def image_urls_from_text(text: str) -> list[str]:
    patterns = [
        r'https?:\\?/\\?/[^"\\\s<>]+?(?:\.(?:jpg|jpeg|png|webp|avif)|/img/|/images?/)[^"\\\s<>]*',
        r'"(?:image|imageUrl|image_url|thumbnail|thumbnailUrl|primaryImage|productImage)"\s*:\s*"([^"]+)"',
    ]
    rows = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            value = match.group(1) if match.groups() else match.group(0)
            value = value.replace("\\/", "/")
            if value.startswith("http"):
                rows.append(value)
    return rows


def direct_product_search_pages(product: dict) -> list[dict]:
    brand = product["brand"]
    model = product["equipment_model"]
    query = quote_plus(f"{brand} {model}")
    model_query = quote_plus(model)
    return [
        {
            "page_url": f"https://www.searspartsdirect.com/search?q={query}",
            "source_name": "Direct Sears Parts Search",
            "title": f"{brand} {model} refrigerator product search",
        },
        {
            "page_url": f"https://www.repairclinic.com/Shop-For-Parts?query={query}",
            "source_name": "Direct RepairClinic Search",
            "title": f"{brand} {model} refrigerator product search",
        },
        {
            "page_url": f"https://www.appliancepartspros.com/search.aspx?q={model_query}",
            "source_name": "Direct AppliancePartsPros Search",
            "title": f"{brand} {model} refrigerator product search",
        },
        {
            "page_url": f"https://www.easyapplianceparts.com/Search.aspx?SearchTerm={model_query}",
            "source_name": "Direct EasyApplianceParts Search",
            "title": f"{brand} {model} refrigerator product search",
        },
        {
            "page_url": f"https://www.partselect.com/Search?searchterm={model_query}",
            "source_name": "Direct PartSelect Search",
            "title": f"{brand} {model} refrigerator product search",
        },
    ]


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
        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-original")
            or img.get("data-lazy-src")
            or first_srcset_url(img.get("srcset") or img.get("data-srcset"))
        )
        alt = img.get("alt") or ""
        src = normalize_page_image_url(str(response.url), src)
        if not src:
            continue
        if any(token in normalized(src + " " + alt) for token in ["LOGO", "ICON", "SPRITE"]):
            continue
        image_urls.append(src)

    for image_url in image_urls_from_text(response.text)[:40]:
        image_urls.append(normalize_page_image_url(str(response.url), image_url))

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


def search_direct_product_pages(client: httpx.Client, product: dict) -> list[dict]:
    rows = []
    for page in direct_product_search_pages(product):
        rows.extend(extract_page_images(client, page))
        if len(rows) >= 12:
            break
    return rows[:12]


def search_public_web_images(client: httpx.Client, product: dict) -> list[dict]:
    rows = []
    rows.extend(search_brave_images(client, product))
    rows.extend(search_bing_api_images(client, product))
    rows.extend(search_bing_images(client, product))
    if any(score_candidate(product, row) >= MIN_PROMOTE_SCORE for row in rows):
        return rows[:10]
    rows.extend(search_direct_product_pages(client, product))
    if any(score_candidate(product, row) >= MIN_PROMOTE_SCORE for row in rows):
        return rows[:10]
    pages = search_brave_pages(client, product)[:5]
    if not pages:
        pages = search_bing_api_pages(client, product)[:5]
    if not pages:
        pages = search_duckduckgo_pages(client, product)[:5]
    if not pages:
        pages = search_duckduckgo_pages_wide(client, product)[:5]
    for page in pages:
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
    if is_blocked_domain(candidate.get("image_url")) or is_blocked_domain(candidate.get("page_url")):
        return False
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
    if any(token in haystack for token in BAD_IMAGE_TOKENS):
        return score >= 90
    if any(token in haystack for token in NON_PRODUCT_IMAGE_TOKENS):
        return False
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
    usable = sorted(usable, key=image_quality_score, reverse=True)
    best = None
    for candidate in usable:
        if is_displayable_image_url(client, candidate.get("image_url")):
            best = candidate
            break
    if not best:
        return False
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
    if os.getenv("PRODUCT_IMAGE_BATCH_ENABLED", "0") != "1":
        print("product image batch backfill is disabled; images are filled only on customer lookup")
        return
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
                raw_candidates.extend(search_brave_images(client, product))
                raw_candidates.extend(search_bing_api_images(client, product))
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
