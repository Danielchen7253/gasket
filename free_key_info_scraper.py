"""Free web scraper for critical refrigerator door and gasket data.

This worker deliberately avoids paid APIs. It only visits known manufacturer
and parts-site pages directly, then writes source-backed door layout and gasket
dimension data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
load_dotenv(ROOT.parent / ".env")

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

REPORT_PATH = ROOT / "free_key_info_scraper_report.json"
LOG_PATH = ROOT / "free_key_info_scraper.log"

LIMIT = int(os.getenv("FREE_KEY_INFO_LIMIT", "20"))
WRITE_ENABLED = os.getenv("FREE_KEY_INFO_WRITE", "1") == "1"
REQUEST_DELAY = float(os.getenv("FREE_KEY_INFO_DELAY_SECONDS", "0.4"))
HTTP_TIMEOUT = float(os.getenv("FREE_KEY_INFO_HTTP_TIMEOUT", "18"))
MAX_URLS_PER_PRODUCT = int(os.getenv("FREE_KEY_INFO_MAX_URLS_PER_PRODUCT", "8"))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
)

DIMENSION_RE = re.compile(
    r"(?P<w>\d+(?:\.\d+)?(?:\s+\d+/\d+)?|\d+/\d+)\s*(?:\"|in\.?|inch(?:es)?)?\s*"
    r"(?:x|×|X)\s*(?P<h>\d+(?:\.\d+)?(?:\s+\d+/\d+)?|\d+/\d+)\s*(?:\"|in\.?|inch(?:es)?)?",
    re.I,
)
PART_PATTERNS = [
    re.compile(r"\b(?:part(?:\s*#|\s*number)?|mpn|mfg\s*#|sku)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-./]{2,30})", re.I),
    re.compile(r"\b(?:OEM|replaces?)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-./]{2,30})", re.I),
]
BAD_PART_NUMBERS = {
    "MODEL",
    "NUMBER",
    "RESULT",
    "RESULTS",
    "SEARCH",
    "ORDER",
    "GASKET",
    "REFRIGERATOR",
    "FREEZER",
    "INCH",
    "INCHES",
    "REPLACE",
    "REPLACES",
}

BRAND_SLUGS = {
    "AHT Cooling Systems": "aht-cooling-systems",
    "Arctic Air": "arctic-air",
    "Atosa": "atosa",
    "Avantco": "avantco",
    "Beverage-Air": "beverage-air",
    "Continental": "continental-refrigeration",
    "Continental Refrigerator": "continental-refrigeration",
    "Delfield": "delfield",
    "Everest": "everest",
    "Frigidaire": "frigidaire",
    "GE": "ge-appliance",
    "Glastender": "glastender",
    "Hoshizaki": "hoshizaki",
    "Hussmann": "hussmann",
    "KitchenAid": "kitchenaid",
    "LG": "lg-appliances",
    "Manitowoc Ice": "manitowoc-ice",
    "Master-Bilt": "master-bilt",
    "Maytag": "maytag",
    "Nor-Lake": "norlake",
    "Perlick": "perlick",
    "Randell": "randell",
    "Samsung": "samsung",
    "Scotsman": "scotsman",
    "Sub-Zero": "sub-zero",
    "Traulsen": "traulsen",
    "True": "true",
    "Turbo Air": "turbo-air",
    "Victory": "victory",
    "Whirlpool": "whirlpool",
}


@dataclass(frozen=True)
class Source:
    name: str
    url_template: str


DIRECT_SOURCES = [
    Source("Parts Town model page", "https://www.partstown.com/m/{brand_slug}/{model}"),
    Source("Parts Town search", "https://www.partstown.com/search?q={query}"),
    Source("WebstaurantStore search", "https://www.webstaurantstore.com/search/{query}.html"),
    Source("PartsFPS search", "https://www.partsfps.com/search?keyword={query}"),
    Source("PartsFe search", "https://partsfe.com/search?q={query}"),
    Source("PartSelect model page", "https://www.partselect.com/Models/{model}/"),
    Source("AppliancePartsPros search", "https://www.appliancepartspros.com/search.aspx?model={model}"),
    Source("Sears PartsDirect search", "https://www.searspartsdirect.com/search?q={query}"),
    Source("RepairClinic search", "https://www.repairclinic.com/Search?query={query}"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    line = f"{now_iso()} {message}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def write_report(report: dict[str, Any]) -> None:
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def supabase_headers(prefer: str | None = None) -> dict[str, str]:
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalized(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def fractional_to_float(value: str) -> float | None:
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


def canonical_part_number(value: str | None) -> str | None:
    if not value:
        return None
    part = re.sub(r"\s+", "", value.upper()).strip(".,;:()[]{}")
    if len(part) < 3 or len(part) > 35:
        return None
    if not re.search(r"\d", part):
        return None
    if part in BAD_PART_NUMBERS:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", part):
        return None
    return part


def part_number_from_url(url: str, model: str) -> str | None:
    if "/m/" in urlparse(url).path.lower():
        return None
    slug = urlparse(url).path.rstrip("/").split("/")[-1].upper()
    if normalized(slug) == normalized(model):
        return None
    match = re.search(r"(\d{5,})$", slug)
    if match:
        return canonical_part_number(match.group(1))
    return canonical_part_number(slug)


def plausible_dimension(width: float | None, height: float | None) -> bool:
    if not width or not height:
        return False
    small, large = sorted([width, height])
    return 8 <= small <= 80 and 12 <= large <= 95


def price_for_dimensions(width: float | None, height: float | None) -> float | None:
    if not width or not height:
        return None
    perimeter = 2 * (width + height)
    if perimeter < 98:
        return 45.0
    if perimeter < 117:
        return 68.0
    if perimeter < 146:
        return 90.0
    if perimeter < 190:
        return 120.0
    return 120.0


def door_position_from_context(context: str, index: int) -> tuple[str, str]:
    text = normalized(context)
    if "LEFT" in text and ("FRESHFOOD" in text or "REFRIGERATOR" in text or "FRIDGE" in text):
        return "left_fresh_food_door", "Left refrigerator door"
    if "RIGHT" in text and ("FRESHFOOD" in text or "REFRIGERATOR" in text or "FRIDGE" in text):
        return "right_fresh_food_door", "Right refrigerator door"
    if "FREEZER" in text and "DRAWER" in text:
        return "freezer_drawer", "Freezer drawer"
    if "LEFT" in text and "FREEZER" in text:
        return "left_freezer_door", "Left freezer door"
    if "RIGHT" in text and "FREEZER" in text:
        return "right_freezer_door", "Right freezer door"
    if "FREEZER" in text:
        return "freezer_door", "Freezer door"
    if "LEFT" in text:
        return "left_door", "Left door"
    if "RIGHT" in text:
        return "right_door", "Right door"
    if "FRESHFOOD" in text or "REFRIGERATOR" in text:
        return "fresh_food_door", "Fresh food door"
    return f"door_{index}", f"Door {index}"


def infer_door_layout(text: str) -> dict[str, Any] | None:
    haystack = normalized(text)
    lower = text.lower()
    if "FRENCHDOOR" in haystack or ("french door" in lower and ("drawer" in lower or "bottom freezer" in lower)):
        return {
            "door_count": 3,
            "door_layout": "french_door_3",
            "door_positions": [
                {"key": "left_fresh_food_door", "label": "Left refrigerator door"},
                {"key": "right_fresh_food_door", "label": "Right refrigerator door"},
                {"key": "freezer_drawer", "label": "Freezer drawer"},
            ],
            "confidence": 78,
        }
    if "SIDEBYSIDE" in haystack or "side-by-side" in lower or "side by side" in lower:
        return {
            "door_count": 2,
            "door_layout": "side_by_side_2",
            "door_positions": [
                {"key": "fresh_food_door", "label": "Fresh food door"},
                {"key": "freezer_door", "label": "Freezer door"},
            ],
            "confidence": 75,
        }
    match = re.search(r"\b(1|2|3|4|5|6)\s*(?:door|doors)\b", lower)
    if match:
        count = int(match.group(1))
        if count == 2:
            positions = [
                {"key": "left_door", "label": "Left door"},
                {"key": "right_door", "label": "Right door"},
            ]
        elif count == 1:
            positions = [{"key": "single_door", "label": "Single door"}]
        else:
            positions = [{"key": f"door_{i}", "label": f"Door {i}"} for i in range(1, count + 1)]
        return {
            "door_count": count,
            "door_layout": f"{count}_door",
            "door_positions": positions,
            "confidence": 66,
        }
    return None


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
    return [item for item in variants if item]


def build_urls(brand: str, model: str) -> list[tuple[str, str]]:
    urls: list[tuple[str, str]] = []
    seen: set[str] = set()
    brand_slug = BRAND_SLUGS.get(brand, re.sub(r"[^a-z0-9]+", "-", brand.lower()).strip("-"))
    for variant in model_variants(model)[:4]:
        query = quote_plus(f"{brand} {variant} door gasket")
        data = {
            "brand_slug": brand_slug,
            "model": quote_plus(variant),
            "query": query,
        }
        for source in DIRECT_SOURCES:
            url = source.url_template.format(**data)
            if url in seen:
                continue
            seen.add(url)
            urls.append((source.name, url))
    return urls[:MAX_URLS_PER_PRODUCT]


def fetch(client: httpx.Client, url: str) -> str:
    response = client.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        timeout=HTTP_TIMEOUT,
        follow_redirects=True,
    )
    response.raise_for_status()
    return response.text


def same_host(base_url: str, candidate: str) -> bool:
    return urlparse(base_url).netloc.lower() == urlparse(candidate).netloc.lower()


def is_search_page(url: str) -> bool:
    parsed = urlparse(url)
    haystack = f"{parsed.path}?{parsed.query}".lower()
    return any(token in haystack for token in ["/search", "search?", "catalogsearch", "keyword=", "query=", "q="])


def detail_links(base_url: str, html: str, brand: str, model: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    model_norm = normalized(model)
    candidates: list[tuple[int, str]] = []
    for link in soup.select("a[href]"):
        href = link.get("href") or ""
        absolute = urljoin(base_url, href).split("#")[0]
        if not absolute.startswith(("http://", "https://")) or not same_host(base_url, absolute):
            continue
        label = clean_text(link.get_text(" "))
        haystack = f"{label} {absolute}"
        norm = normalized(haystack)
        score = 0
        if model_norm and model_norm in norm:
            score += 40
        if "GASKET" in norm or "DOORSEAL" in norm:
            score += 30
        if normalized(brand) in norm:
            score += 10
        if score >= 40 and not is_search_page(absolute):
            candidates.append((score, absolute))
    candidates.sort(reverse=True)
    return list(dict.fromkeys(url for _, url in candidates[:5]))


def extract_part_number(context: str) -> str | None:
    for pattern in PART_PATTERNS:
        match = pattern.search(context)
        if match:
            part = canonical_part_number(match.group(1))
            if part:
                return part
    return None


def extract_gaskets(source_name: str, url: str, html: str, brand: str, model: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    soup = BeautifulSoup(html, "html.parser")
    title = clean_text(soup.title.get_text(" ")) if soup.title else ""
    text = clean_text(soup.get_text(" "))
    combined = f"{title} {text}"
    norm = normalized(combined)
    if normalized(model) not in norm:
        return [], None
    if "GASKET" not in norm and "DOORSEAL" not in norm and "DOORSEALS" not in norm:
        return [], infer_door_layout(combined)

    rows: list[dict[str, Any]] = []
    seen_dims: set[tuple[float, float, str | None, str]] = set()
    for index, match in enumerate(DIMENSION_RE.finditer(text), start=1):
        width = fractional_to_float(match.group("w"))
        height = fractional_to_float(match.group("h"))
        if not plausible_dimension(width, height):
            continue
        start = max(0, match.start() - 260)
        end = min(len(text), match.end() + 260)
        context = text[start:end]
        if "gasket" not in context.lower() and "door seal" not in context.lower():
            continue
        key, label = door_position_from_context(context, len(rows) + 1)
        part = extract_part_number(context) or extract_part_number(combined[:2500]) or part_number_from_url(url, model)
        dim_key = (
            round(float(width), 3),
            round(float(height), 3),
            part,
            key if key not in {f"door_{len(rows) + 1}", f"door_{index}"} else "generic",
        )
        if dim_key in seen_dims:
            continue
        seen_dims.add(dim_key)
        confidence = 68
        if part:
            confidence += 10
        if key != f"door_{len(rows) + 1}":
            confidence += 6
        if source_name.lower().startswith(("parts town", "partselect", "appliancepartspros")):
            confidence += 4
        rows.append(
            {
                "door_index": len(rows) + 1,
                "door_position": key,
                "door_position_display": label,
                "gasket_name": f"{label} gasket",
                "part_number": part,
                "universal_part_number": part,
                "width_in": width,
                "height_in": height,
                "perimeter_in": round(2 * (float(width) + float(height)), 3),
                "dimensions_text": match.group(0),
                "size_status": "cross_reference",
                "source_name": source_name,
                "source_url": url,
                "evidence_summary": f"Parsed from source-backed page for {brand} {model}.",
                "confidence_score": min(95, confidence),
                "needs_customer_confirmation": True,
                "base_price_usd": price_for_dimensions(width, height),
                "final_price_usd": price_for_dimensions(width, height),
                "pricing_note": "Priced from gasket perimeter size rule.",
                "data_status": "free_web_cross_reference",
                "is_verified": False,
            }
        )
    return rows, infer_door_layout(combined)


def fetch_candidates(client: httpx.Client, limit: int) -> list[dict[str, Any]]:
    product_ids = [item.strip() for item in os.getenv("FREE_KEY_INFO_PRODUCT_IDS", "").split(",") if item.strip()]
    if product_ids:
        response = client.get(
            f"{SUPABASE_URL}/rest/v1/refrigerator_products",
            headers=supabase_headers(),
            params={
                "select": "id,brand,equipment_model,product_type,door_count,door_layout,door_positions,last_enriched_at,updated_at",
                "id": f"in.({','.join(product_ids)})",
                "order": "id.asc",
            },
        )
        response.raise_for_status()
        return response.json()

    params = {
        "select": "id,brand,equipment_model,product_type,door_count,door_layout,door_positions,last_enriched_at,updated_at",
        "brand": "not.is.null",
        "equipment_model": "not.is.null",
        "or": "(door_count.is.null,door_positions.eq.[])",
        "order": "last_enriched_at.asc.nullsfirst,updated_at.asc.nullsfirst,id.asc",
        "limit": str(limit),
    }
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/refrigerator_products",
        headers=supabase_headers(),
        params=params,
    )
    response.raise_for_status()
    rows = response.json()
    if rows:
        return rows
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/refrigerator_products",
        headers=supabase_headers(),
        params={
            "select": "id,brand,equipment_model,product_type,door_count,door_layout,door_positions,last_enriched_at,updated_at",
            "brand": "not.is.null",
            "equipment_model": "not.is.null",
            "order": "last_enriched_at.asc.nullsfirst,updated_at.asc.nullsfirst,id.asc",
            "limit": str(limit),
        },
    )
    response.raise_for_status()
    return response.json()


def update_product_layout(client: httpx.Client, product_id: int, layout: dict[str, Any], source_url: str) -> None:
    existing_response = client.get(
        f"{SUPABASE_URL}/rest/v1/refrigerator_products",
        headers=supabase_headers(),
        params={"select": "door_count,door_layout,door_positions", "id": f"eq.{product_id}", "limit": "1"},
    )
    existing_response.raise_for_status()
    existing_rows = existing_response.json()
    existing = existing_rows[0] if existing_rows else {}
    if existing.get("door_count") and existing.get("door_layout") and existing.get("door_positions"):
        return
    payload = {
        "door_count": layout["door_count"],
        "door_layout": layout["door_layout"],
        "door_positions": layout["door_positions"],
        "door_layout_confidence": layout["confidence"],
        "door_layout_source": f"free_web:{source_url[:180]}",
        "door_layout_updated_at": now_iso(),
        "last_enriched_at": now_iso(),
        "updated_at": now_iso(),
    }
    response = client.patch(
        f"{SUPABASE_URL}/rest/v1/refrigerator_products?id=eq.{product_id}",
        headers=supabase_headers("return=minimal"),
        json=payload,
    )
    response.raise_for_status()


def mark_product_scanned(client: httpx.Client, product_id: int) -> None:
    response = client.patch(
        f"{SUPABASE_URL}/rest/v1/refrigerator_products?id=eq.{product_id}",
        headers=supabase_headers("return=minimal"),
        json={"last_enriched_at": now_iso(), "updated_at": now_iso()},
    )
    response.raise_for_status()


def replace_gaskets(client: httpx.Client, product_id: int, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    delete_response = client.delete(
        f"{SUPABASE_URL}/rest/v1/refrigerator_product_gaskets"
        f"?refrigerator_product_id=eq.{product_id}&or=(data_status.is.null,data_status.neq.verified)",
        headers=supabase_headers("return=minimal"),
    )
    delete_response.raise_for_status()
    payload = []
    for index, row in enumerate(rows, start=1):
        item = dict(row)
        item.pop("perimeter_in", None)
        item["refrigerator_product_id"] = product_id
        item["door_index"] = index
        item["updated_at"] = now_iso()
        payload.append(item)
    response = client.post(
        f"{SUPABASE_URL}/rest/v1/refrigerator_product_gaskets",
        headers=supabase_headers("return=minimal"),
        json=payload,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"insert gaskets failed {response.status_code}: {response.text[:500]}")
    response.raise_for_status()


def refresh_quote_items(client: httpx.Client, product_id: int) -> None:
    response = client.post(
        f"{SUPABASE_URL}/rest/v1/rpc/refresh_product_quote_items",
        headers=supabase_headers(),
        json={"p_product_id": product_id},
    )
    if response.status_code != 404:
        response.raise_for_status()


def main() -> None:
    report: dict[str, Any] = {
        "started_at": now_iso(),
        "limit": LIMIT,
        "write_enabled": WRITE_ENABLED,
        "processed": 0,
        "layout_updated": 0,
        "gasket_updated": 0,
        "not_found": 0,
        "errors": 0,
        "recent": [],
    }
    write_report(report)
    with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        products = fetch_candidates(client, LIMIT)
        log(f"loaded {len(products)} free key-info candidates")
        for product in products:
            product_id = int(product["id"])
            brand = product.get("brand") or ""
            model = product.get("equipment_model") or ""
            label = f"{brand} {model} #{product_id}"
            report["processed"] += 1
            best_layout = None
            best_layout_url = None
            best_gaskets: list[dict[str, Any]] = []
            best_gasket_score = 0.0
            try:
                for source_name, url in build_urls(brand, model):
                    try:
                        html = fetch(client, url)
                    except Exception:
                        continue
                    if not is_search_page(url):
                        gaskets, layout = extract_gaskets(source_name, url, html, brand, model)
                        if layout and (not best_layout or layout["confidence"] > best_layout["confidence"]):
                            best_layout = layout
                            best_layout_url = url
                        if gaskets:
                            score = sum(float(row.get("confidence_score") or 0) for row in gaskets)
                            if score > best_gasket_score:
                                best_gaskets = gaskets
                                best_gasket_score = score
                    for detail_url in detail_links(url, html, brand, model):
                        try:
                            detail_html = fetch(client, detail_url)
                        except Exception:
                            continue
                        detail_gaskets, detail_layout = extract_gaskets(source_name, detail_url, detail_html, brand, model)
                        if detail_layout and (not best_layout or detail_layout["confidence"] > best_layout["confidence"]):
                            best_layout = detail_layout
                            best_layout_url = detail_url
                        if detail_gaskets:
                            score = sum(float(row.get("confidence_score") or 0) for row in detail_gaskets)
                            if score > best_gasket_score:
                                best_gaskets = detail_gaskets
                                best_gasket_score = score
                    if best_layout and best_gaskets:
                        break
                    if REQUEST_DELAY:
                        time.sleep(REQUEST_DELAY)

                if WRITE_ENABLED and best_layout:
                    update_product_layout(client, product_id, best_layout, best_layout_url or "free_web")
                    report["layout_updated"] += 1
                if WRITE_ENABLED and best_gaskets:
                    replace_gaskets(client, product_id, best_gaskets)
                    refresh_quote_items(client, product_id)
                    report["gasket_updated"] += 1
                if WRITE_ENABLED:
                    mark_product_scanned(client, product_id)
                if not best_layout and not best_gaskets:
                    report["not_found"] += 1
                report["recent"].append(
                    {
                        "product": label,
                        "layout": bool(best_layout),
                        "gaskets": len(best_gaskets),
                        "layout_source": best_layout_url,
                        "gasket_source": best_gaskets[0]["source_url"] if best_gaskets else None,
                        "gasket_preview": [
                            {
                                "door": row.get("door_position_display"),
                                "part": row.get("part_number"),
                                "size": row.get("dimensions_text"),
                                "width_in": row.get("width_in"),
                                "height_in": row.get("height_in"),
                                "confidence": row.get("confidence_score"),
                            }
                            for row in best_gaskets[:4]
                        ],
                    }
                )
                log(f"{label}: layout={bool(best_layout)} gaskets={len(best_gaskets)}")
                write_report(report)
            except Exception as exc:
                report["errors"] += 1
                report["recent"].append({"product": label, "error": str(exc)[:400]})
                log(f"error {label}: {exc}")
                write_report(report)
    report["finished_at"] = now_iso()
    write_report(report)
    log(
        "done "
        f"processed={report['processed']} layout_updated={report['layout_updated']} "
        f"gasket_updated={report['gasket_updated']} errors={report['errors']}"
    )


if __name__ == "__main__":
    main()
