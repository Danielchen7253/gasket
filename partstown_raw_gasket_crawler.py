"""Crawl raw refrigerator gasket data from PartsTown.

This crawler does not call OpenAI, SerpApi, Google, or any paid search API.
It writes only to parts_site_raw_gaskets so the data can be reviewed and
cleaned before touching the production product/gasket tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import json
import os
from pathlib import Path
import re
import time
from typing import Iterable, Any
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
load_dotenv(ROOT.parent / ".env")

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

REPORT_PATH = ROOT / "partstown_raw_gasket_crawler_report.json"
LOG_PATH = ROOT / "partstown_raw_gasket_crawler.log"

LIMIT = int(os.getenv("PARTSTOWN_RAW_LIMIT", "50"))
MODEL_PAGE_LIMIT = int(os.getenv("PARTSTOWN_RAW_MODEL_PAGE_LIMIT", str(max(50, LIMIT * 4))))
BATCH_SIZE = int(os.getenv("PARTSTOWN_RAW_BATCH_SIZE", "50"))
REQUEST_DELAY = float(os.getenv("PARTSTOWN_RAW_DELAY_SECONDS", "0.25"))
HTTP_TIMEOUT = float(os.getenv("PARTSTOWN_RAW_HTTP_TIMEOUT", "20"))
INCLUDE_BROAD_APPLIANCE = os.getenv("PARTSTOWN_RAW_INCLUDE_BROAD_APPLIANCE", "1") == "1"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
)

REFRIGERATION_BRANDS = {
    "aht-cooling-systems": "AHT Cooling Systems",
    "arctic-air": "Arctic Air",
    "atosa": "Atosa",
    "avantco": "Avantco",
    "beverage-air": "Beverage-Air",
    "continental-refrigeration": "Continental Refrigerator",
    "delfield": "Delfield",
    "everest": "Everest",
    "glastender": "Glastender",
    "hoshizaki": "Hoshizaki",
    "hussmann": "Hussmann",
    "ice-o-matic": "Ice-O-Matic",
    "icetro": "Icetro",
    "kelvinator": "Kelvinator",
    "manitowoc-ice": "Manitowoc Ice",
    "master-bilt": "Master-Bilt",
    "norlake": "Nor-Lake",
    "perlick": "Perlick",
    "randell": "Randell",
    "scotsman": "Scotsman",
    "traulsen": "Traulsen",
    "true": "True",
    "turbo-air": "Turbo Air",
    "victory": "Victory",
}

BROAD_APPLIANCE_BRANDS = {
    "frigidaire": "Frigidaire",
    "ge-appliance": "GE",
    "kitchenaid": "KitchenAid",
    "lg-appliances": "LG",
    "maytag": "Maytag",
    "samsung": "Samsung",
    "whirlpool": "Whirlpool",
}

MODEL_LOC_RE = re.compile(r"<loc>\s*(https://www\.partstown\.com/m/([^/<]+)/([^<]+))</loc>", re.I)
SITEMAP_LOC_RE = re.compile(r"<loc>\s*([^<]+)\s*</loc>", re.I)
DIMENSION_RE = re.compile(
    r"(?P<w>\d+(?:\.\d+)?(?:\s+\d+/\d+)?|\d+/\d+)\s*(?:\"|in\.?|inch(?:es)?)?\s*"
    r"(?:x|×|X)\s*(?P<h>\d+(?:\.\d+)?(?:\s+\d+/\d+)?|\d+/\d+)\s*(?:\"|in\.?|inch(?:es)?)?",
    re.I,
)
PART_RE = re.compile(
    r"\b(?:part(?:\s*#|\s*number)?|mpn|mfg\s*#|sku|oem)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-./]{2,35})",
    re.I,
)

BAD_PARTS = {
    "MODEL",
    "NUMBER",
    "ORDER",
    "RESULT",
    "RESULTS",
    "SEARCH",
    "GASKET",
    "REFRIGERATOR",
    "FREEZER",
    "REPLACEMENT",
}


@dataclass(frozen=True)
class ModelPage:
    brand_slug: str
    brand: str
    model: str
    url: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    line = f"{now_iso()} {message}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def write_report(report: dict[str, Any]) -> None:
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def headers(prefer: str | None = None) -> dict[str, str]:
    data = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        data["Prefer"] = prefer
    return data


def normalize(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def clean_model(raw: str) -> str:
    return unquote(raw).split("?")[0].strip(" .,/|:;()[]{}").upper()


def valid_model(model: str) -> bool:
    norm = normalize(model)
    return 3 <= len(norm) <= 35 and any(ch.isdigit() for ch in norm)


def brand_map() -> dict[str, str]:
    data = dict(REFRIGERATION_BRANDS)
    if INCLUDE_BROAD_APPLIANCE:
        data.update(BROAD_APPLIANCE_BRANDS)
    return data


def fraction_to_float(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip().replace("-", " ")
    try:
        return round(float(value), 3)
    except ValueError:
        pass
    total = 0.0
    for part in value.split():
        if "/" in part:
            try:
                numerator, denominator = part.split("/", 1)
                total += float(numerator) / float(denominator)
            except (ValueError, ZeroDivisionError):
                return None
        else:
            try:
                total += float(part)
            except ValueError:
                return None
    return round(total, 3) if total else None


def plausible_dimension(width: float | None, height: float | None) -> bool:
    if not width or not height:
        return False
    small, large = sorted([width, height])
    return 8 <= small <= 90 and 12 <= large <= 110


def canonical_part(value: str | None) -> str:
    if not value:
        return ""
    part = re.sub(r"\s+", "", value.upper()).strip(".,;:()[]{}")
    if len(part) < 3 or len(part) > 40:
        return ""
    if part in BAD_PARTS:
        return ""
    if not re.search(r"\d", part):
        return ""
    return part


def part_from_url(url: str) -> str:
    path = urlparse(url).path.lower()
    if "/m/" in path:
        return ""
    slug = urlparse(url).path.rstrip("/").split("/")[-1].upper()
    match = re.search(r"(\d{5,})$", slug)
    return canonical_part(match.group(1) if match else slug)


def fetch(client: httpx.Client, url: str) -> bytes:
    response = client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return response.content


def sitemap_urls(client: httpx.Client) -> list[str]:
    content = fetch(client, "https://www.partstown.com/sitemap.xml").decode("utf-8", "replace")
    urls = []
    for loc in SITEMAP_LOC_RE.findall(content):
        if "/sitemaps/models-com-" in loc and "hvacmodels" not in loc:
            urls.append(loc)
    return sorted(set(urls))


def parse_model_sitemap(content: bytes, brands: dict[str, str]) -> Iterable[ModelPage]:
    text = gzip.decompress(content).decode("utf-8", "replace")
    for url, brand_slug, raw_model in MODEL_LOC_RE.findall(text):
        brand = brands.get(brand_slug.lower())
        if not brand:
            continue
        model = clean_model(raw_model)
        if not valid_model(model):
            continue
        yield ModelPage(brand_slug=brand_slug, brand=brand, model=model, url=url)


def model_pages_from_sitemaps(client: httpx.Client, limit: int) -> list[ModelPage]:
    pages: list[ModelPage] = []
    seen: set[tuple[str, str]] = set()
    brands = brand_map()
    for sitemap_url in sitemap_urls(client):
        try:
            content = fetch(client, sitemap_url)
        except Exception as exc:
            log(f"skip sitemap {sitemap_url}: {exc}")
            continue
        for page in parse_model_sitemap(content, brands):
            key = (page.brand.lower(), normalize(page.model))
            if key in seen:
                continue
            seen.add(key)
            pages.append(page)
            if len(pages) >= limit:
                return pages
        if REQUEST_DELAY:
            time.sleep(REQUEST_DELAY)
    return pages


def same_host(base_url: str, url: str) -> bool:
    return urlparse(base_url).netloc.lower() == urlparse(url).netloc.lower()


def part_links(base_url: str, html: str, model: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls: list[tuple[int, str]] = []
    model_norm = normalize(model)
    for a in soup.select("a[href]"):
        href = a.get("href") or ""
        absolute = urljoin(base_url, href).split("#")[0]
        if not absolute.startswith(("http://", "https://")) or not same_host(base_url, absolute):
            continue
        text = clean_text(a.get_text(" "))
        haystack = f"{text} {absolute}"
        norm = normalize(haystack)
        score = 0
        if "GASKET" in norm or "DOORSEAL" in norm:
            score += 50
        if model_norm and model_norm in norm:
            score += 20
        if "/m/" not in urlparse(absolute).path.lower():
            score += 10
        if score >= 50:
            urls.append((score, absolute))
    urls.sort(reverse=True)
    return list(dict.fromkeys(url for _, url in urls[:12]))


def door_position(context: str) -> str:
    norm = normalize(context)
    if "LEFT" in norm and ("REFRIGERATOR" in norm or "FRESHFOOD" in norm):
        return "Left refrigerator door"
    if "RIGHT" in norm and ("REFRIGERATOR" in norm or "FRESHFOOD" in norm):
        return "Right refrigerator door"
    if "FREEZER" in norm and "DRAWER" in norm:
        return "Freezer drawer"
    if "FREEZER" in norm:
        return "Freezer door"
    if "LEFT" in norm:
        return "Left door"
    if "RIGHT" in norm:
        return "Right door"
    if "REFRIGERATOR" in norm or "FRESHFOOD" in norm:
        return "Refrigerator door"
    return ""


def extract_rows(page: ModelPage, url: str, html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    title = clean_text(soup.title.get_text(" ")) if soup.title else ""
    text = clean_text(soup.get_text(" "))
    combined = f"{title} {text}"
    norm = normalize(combined)
    if normalize(page.model) not in norm:
        return []
    if "GASKET" not in norm and "DOORSEAL" not in norm:
        return []

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    default_part = canonical_part(PART_RE.search(combined).group(1)) if PART_RE.search(combined) else ""
    default_part = default_part or part_from_url(url)

    matches = list(DIMENSION_RE.finditer(text))
    if not matches and not default_part:
        return []

    for index, match in enumerate(matches or [None], start=1):
        width = height = None
        dimensions_text = ""
        start = 0
        end = min(len(text), 900)
        if match:
            width = fraction_to_float(match.group("w"))
            height = fraction_to_float(match.group("h"))
            if not plausible_dimension(width, height):
                continue
            dimensions_text = match.group(0)
            start = max(0, match.start() - 300)
            end = min(len(text), match.end() + 300)
        context = text[start:end]
        if "gasket" not in context.lower() and "door seal" not in context.lower() and match:
            continue
        part_match = PART_RE.search(context)
        part = canonical_part(part_match.group(1) if part_match else "") or default_part
        if not part and not dimensions_text:
            continue
        dedupe = (dimensions_text, part, door_position(context))
        if dedupe in seen:
            continue
        seen.add(dedupe)
        confidence = 35
        if dimensions_text:
            confidence += 30
        if part:
            confidence += 20
        if door_position(context):
            confidence += 8
        if normalize(page.model) in normalize(context):
            confidence += 7
        rows.append(
            {
                "source_site": "PartsTown",
                "source_url": url,
                "brand": page.brand,
                "equipment_model": page.model,
                "normalized_brand": normalize(page.brand),
                "normalized_model": normalize(page.model),
                "part_number": part,
                "part_name": title[:300] or None,
                "is_gasket": True,
                "dimensions_text": dimensions_text or None,
                "width_in": width,
                "height_in": height,
                "door_position_text": door_position(context) or None,
                "raw_text": context[:3000] if context else combined[:3000],
                "confidence_score": min(100, confidence),
                "crawl_status": "raw",
                "parsed_at": now_iso(),
                "updated_at": now_iso(),
            }
        )
    return rows


def upsert_raw_rows(client: httpx.Client, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    sanitized = []
    for row in rows:
        item = dict(row)
        item["part_number"] = item.get("part_number") or ""
        item["normalized_model"] = item.get("normalized_model") or ""
        sanitized.append(item)
    response = client.post(
        f"{SUPABASE_URL}/rest/v1/parts_site_raw_gaskets",
        headers=headers("resolution=merge-duplicates,return=representation"),
        params={"on_conflict": "source_site,source_url,part_number,normalized_model"},
        json=sanitized,
    )
    response.raise_for_status()
    return len(response.json())


def crawl_model_page(client: httpx.Client, page: ModelPage) -> list[dict[str, Any]]:
    try:
        model_html = fetch(client, page.url).decode("utf-8", "replace")
    except Exception as exc:
        log(f"skip model {page.brand} {page.model}: {exc}")
        return []
    rows = extract_rows(page, page.url, model_html)
    for link in part_links(page.url, model_html, page.model):
        try:
            part_html = fetch(client, link).decode("utf-8", "replace")
        except Exception:
            continue
        rows.extend(extract_rows(page, link, part_html))
        if REQUEST_DELAY:
            time.sleep(REQUEST_DELAY)
    return rows


def main() -> None:
    report = {
        "started_at": now_iso(),
        "limit": LIMIT,
        "model_page_limit": MODEL_PAGE_LIMIT,
        "models_scanned": 0,
        "raw_rows_found": 0,
        "raw_rows_written": 0,
        "errors": 0,
        "recent": [],
    }
    write_report(report)
    batch: list[dict[str, Any]] = []
    with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        pages = model_pages_from_sitemaps(client, MODEL_PAGE_LIMIT)
        log(f"loaded {len(pages)} PartsTown model pages")
        for page in pages:
            if report["raw_rows_found"] >= LIMIT:
                break
            report["models_scanned"] += 1
            try:
                rows = crawl_model_page(client, page)
                if rows:
                    batch.extend(rows)
                    report["raw_rows_found"] += len(rows)
                    report["recent"].append(
                        {
                            "brand": page.brand,
                            "model": page.model,
                            "rows": len(rows),
                            "first": {
                                "part_number": rows[0].get("part_number"),
                                "dimensions_text": rows[0].get("dimensions_text"),
                                "source_url": rows[0].get("source_url"),
                            },
                        }
                    )
                    log(f"{page.brand} {page.model}: raw_rows={len(rows)}")
                if len(batch) >= BATCH_SIZE:
                    report["raw_rows_written"] += upsert_raw_rows(client, batch)
                    batch.clear()
                    write_report(report)
                if REQUEST_DELAY:
                    time.sleep(REQUEST_DELAY)
            except Exception as exc:
                report["errors"] += 1
                report["recent"].append({"brand": page.brand, "model": page.model, "error": str(exc)[:300]})
                log(f"error {page.brand} {page.model}: {exc}")
                write_report(report)
        if batch:
            report["raw_rows_written"] += upsert_raw_rows(client, batch)
    report["finished_at"] = now_iso()
    write_report(report)
    log(
        "done "
        f"models_scanned={report['models_scanned']} raw_rows_found={report['raw_rows_found']} "
        f"raw_rows_written={report['raw_rows_written']} errors={report['errors']}"
    )


if __name__ == "__main__":
    main()
