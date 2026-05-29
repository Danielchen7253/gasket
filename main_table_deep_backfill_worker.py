"""Independent-field backfill for refrigerator_products.

This worker only fills main-table product fields from brand/model evidence.
Each target field group runs independently with its own timeout, so a slow
image search cannot block door-layout, manual-link, or lifecycle work.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
load_dotenv(ROOT.parent / ".env")

from fast_image_patch import quick_promote_product_image
from product_image_search_crawler import (
    USER_AGENT,
    is_displayable_image_url,
    search_bing_api_pages,
    search_brave_pages,
    search_duckduckgo_pages,
    search_duckduckgo_pages_wide,
    supabase_headers,
)
from trusted_sources import TRUSTED_SOURCE_DOMAINS


SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
PRODUCT_TABLE = "refrigerator_products"
REPORT_PATH = ROOT / "main_table_deep_backfill_report.json"
LOG_PATH = ROOT / "main_table_deep_backfill.log"

PRODUCT_BATCH_SIZE = int(os.getenv("MAIN_DEEP_BATCH_SIZE", "40"))
RAW_FETCH_SIZE = int(os.getenv("MAIN_DEEP_RAW_FETCH_SIZE", "250"))
FIELD_TIMEOUT_SECONDS = float(os.getenv("MAIN_DEEP_FIELD_TIMEOUT_SECONDS", "25"))
PRODUCT_TIMEOUT_SECONDS = float(os.getenv("MAIN_DEEP_PRODUCT_TIMEOUT_SECONDS", "75"))
SLEEP_BETWEEN_PRODUCTS = float(os.getenv("MAIN_DEEP_SLEEP_SECONDS", "0.5"))
MAX_PRODUCTS_PER_RUN = int(os.getenv("MAIN_DEEP_MAX_PRODUCTS_PER_RUN", "0"))
RUN_FOREVER = os.getenv("MAIN_DEEP_RUN_FOREVER", "1") == "1"
SOURCE_FETCH_LIMIT = int(os.getenv("MAIN_DEEP_SOURCE_FETCH_LIMIT", "5"))
PRIORITY_BRANDS = [
    item.strip()
    for item in os.getenv(
        "MAIN_DEEP_PRIORITY_BRANDS",
        "Whirlpool,GE,General Electric,Frigidaire,LG,Samsung,Sub-Zero,True,Turbo Air,"
        "Beverage-Air,Traulsen,Delfield,Viking,KitchenAid,Maytag,Bosch,Thermador,"
        "Miele,Fisher & Paykel,Haier,Arctic Air,Everest,Continental Refrigerator",
    ).split(",")
    if item.strip()
]

TARGET_SELECT = (
    "id,brand,equipment_model,manufacturer,product_type,manufacture_date,"
    "manufacture_date_text,product_image_url,product_image_confidence,"
    "product_image_source_url,product_image_verified,official_product_url,"
    "spec_sheet_url,manual_url,lifecycle_status,lifecycle_evidence_url,"
    "model_year_start,model_year_end,data_confidence,data_source_summary,"
    "door_count,door_layout,door_positions,door_layout_confidence,"
    "door_layout_source,last_enriched_at,updated_at,data_status"
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalized(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def clean(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split()).strip()
    return text or None


def log(message: str) -> None:
    line = f"{now_iso()} {message}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def read_report() -> dict[str, Any]:
    if not REPORT_PATH.exists():
        return {}
    try:
        return json.loads(REPORT_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def write_report(report: dict[str, Any]) -> None:
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def headers(prefer: str | None = None) -> dict[str, str]:
    return supabase_headers(prefer)


def patch_product(product_id: int, payload: dict[str, Any]) -> bool:
    clean_payload = {key: value for key, value in payload.items() if value not in (None, "", [])}
    if not clean_payload:
        return False
    clean_payload["updated_at"] = now_iso()
    with httpx.Client(timeout=20) as client:
        response = client.patch(
            f"{SUPABASE_URL}/rest/v1/{PRODUCT_TABLE}",
            params={"id": f"eq.{product_id}"},
            headers=headers("return=minimal"),
            json=clean_payload,
        )
        response.raise_for_status()
    return True


def mark_attempted(product_id: int) -> None:
    patch_product(product_id, {"last_enriched_at": now_iso()})


def product_missing_any(row: dict[str, Any]) -> bool:
    return any(
        [
            not row.get("product_image_url"),
            not row.get("manufacture_date"),
            not row.get("door_count"),
            not row.get("door_layout"),
            not row.get("door_positions"),
            not is_url(row.get("official_product_url")),
            not is_url(row.get("spec_sheet_url")),
            not is_url(row.get("manual_url")),
            not is_url(row.get("lifecycle_evidence_url")),
            not row.get("last_enriched_at"),
        ]
    )


BAD_MODEL_TOKENS = {
    "ADOBE",
    "AUX",
    "CAMERA",
    "COLORSPACE",
    "DECODE",
    "DEVICERGB",
    "EXIF",
    "FALSE",
    "FILTER",
    "IMAGE",
    "INDEXED",
    "INDESIGN",
    "JPEG",
    "LENGTH",
    "METADATA",
    "OUTLINES",
    "PDF",
    "PHOTOSHOP",
    "STREAM",
    "SUBTYPE",
    "TEXT",
    "THUMB",
    "TIFF",
    "TRAPPED",
    "TYPE",
    "UUID",
    "XMP",
    "XML",
    "XMLNS",
    "XOBJECT",
}


def plausible_model(value: str | None) -> bool:
    raw = clean(value) or ""
    compact = normalized(raw)
    if len(compact) < 3 or len(compact) > 28:
        return False
    if re.search(r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}", raw, re.I):
        return False
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}.*", raw):
        return False
    upper_words = set(re.findall(r"[A-Z]{2,}", raw.upper()))
    if upper_words & BAD_MODEL_TOKENS:
        return False
    if raw.count("/") >= 2:
        return False
    if "/" in raw and not re.search(r"\d", raw):
        return False
    if re.fullmatch(r"\d{1,3}[-/]\d{1,3}(?:T\d+)?", raw):
        return False
    if not re.search(r"\d", compact):
        return False
    if not re.search(r"[A-Z]", compact):
        return len(compact) >= 5
    return True


def get_products(after_id: int, limit: int) -> tuple[list[dict[str, Any]], int]:
    with httpx.Client(timeout=30) as client:
        response = client.get(
            f"{SUPABASE_URL}/rest/v1/{PRODUCT_TABLE}",
            params={
                "select": TARGET_SELECT,
                "brand": "not.is.null",
                "equipment_model": "not.is.null",
                "data_status": "in.(parts_catalog,home_parts_catalog,ai_structured,manual_research_structured)",
                "id": f"gt.{after_id}",
                "order": "id.asc",
                "limit": str(max(limit, RAW_FETCH_SIZE)),
            },
            headers=headers(),
        )
        response.raise_for_status()
        rows = response.json()
    last_raw_id = int(rows[-1]["id"]) if rows else after_id
    usable = [
        row
        for row in rows
        if row.get("brand")
        and row.get("equipment_model")
        and plausible_model(row.get("equipment_model"))
        and product_missing_any(row)
    ]
    return usable[:limit], last_raw_id


def get_next_batch(report: dict[str, Any]) -> list[dict[str, Any]]:
    priority_rows = get_priority_brand_batch(PRODUCT_BATCH_SIZE)
    if priority_rows:
        report["priority_brand_batches"] = int(report.get("priority_brand_batches") or 0) + 1
        write_report(report)
        return priority_rows

    after_id = int(report.get("last_seen_id") or 0)
    for _ in range(20):
        rows, last_raw_id = get_products(after_id, PRODUCT_BATCH_SIZE)
        if rows:
            return rows
        if last_raw_id <= after_id:
            break
        report["last_seen_id"] = last_raw_id
        report["skipped_unusable_model_rows"] = int(report.get("skipped_unusable_model_rows") or 0) + (last_raw_id - after_id)
        write_report(report)
        after_id = last_raw_id
    report["last_seen_id"] = 0
    write_report(report)
    rows, _ = get_products(0, PRODUCT_BATCH_SIZE)
    return rows


def get_priority_brand_batch(limit: int) -> list[dict[str, Any]]:
    if not PRIORITY_BRANDS:
        return []
    gathered: list[dict[str, Any]] = []
    with httpx.Client(timeout=30) as client:
        for brand in PRIORITY_BRANDS:
            if len(gathered) >= limit:
                break
            response = client.get(
                f"{SUPABASE_URL}/rest/v1/{PRODUCT_TABLE}",
                params={
                    "select": TARGET_SELECT,
                    "brand": f"ilike.{brand}",
                    "equipment_model": "not.is.null",
                    "data_status": "in.(parts_catalog,home_parts_catalog,ai_structured,manual_research_structured)",
                    "order": "last_enriched_at.asc.nullsfirst,id.asc",
                    "limit": str(max(8, min(40, limit))),
                },
                headers=headers(),
            )
            response.raise_for_status()
            for row in response.json():
                if (
                    row.get("brand")
                    and row.get("equipment_model")
                    and plausible_model(row.get("equipment_model"))
                    and product_missing_any(row)
                ):
                    gathered.append(row)
                    if len(gathered) >= limit:
                        break
    return gathered


def url_domain(url: str | None) -> str:
    try:
        return urlparse(url or "").netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def is_url(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def source_domain_rank(url: str | None) -> int:
    domain = url_domain(url)
    if not domain:
        return 0
    if domain in TRUSTED_SOURCE_DOMAINS:
        return 20
    if any(domain.endswith("." + item) for item in TRUSTED_SOURCE_DOMAINS):
        return 18
    if any(token in domain for token in ["manualslib", "searspartsdirect", "partselect", "repairclinic", "appliancepartspros", "partstown"]):
        return 14
    return 4


def model_in_text(product: dict[str, Any], text: str) -> bool:
    model = normalized(product.get("equipment_model"))
    haystack = normalized(text)
    return bool(model and model in haystack)


def search_source_pages(product: dict[str, Any]) -> list[dict[str, str]]:
    pages: list[dict[str, str]] = []
    seen: set[str] = set()
    with httpx.Client(timeout=25, headers={"User-Agent": USER_AGENT}) as client:
        for fn in (search_brave_pages, search_bing_api_pages, search_duckduckgo_pages, search_duckduckgo_pages_wide):
            try:
                for page in fn(client, product):
                    url = page.get("page_url")
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    pages.append(page)
                    if len(pages) >= 12:
                        return pages
            except Exception as exc:
                log(f"source page search skipped {product['brand']} {product['equipment_model']} via {fn.__name__}: {exc}")
    pages.sort(
        key=lambda row: (
            model_in_text(product, f"{row.get('title')} {row.get('page_url')}"),
            source_domain_rank(row.get("page_url")),
        ),
        reverse=True,
    )
    return pages


def fetch_page_text(url: str) -> tuple[str, str]:
    with httpx.Client(timeout=12, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if "pdf" in content_type or url.lower().split("?", 1)[0].endswith(".pdf"):
            return "", str(response.url)
        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = " ".join(soup.get_text(" ").split())
        return text[:20000], str(response.url)


def page_link_candidates(product: dict[str, Any]) -> dict[str, Any]:
    if all(product.get(key) for key in ("official_product_url", "spec_sheet_url", "manual_url", "lifecycle_evidence_url")):
        return {}

    pages = search_source_pages(product)
    payload: dict[str, Any] = {}
    source_bits: list[str] = []
    for page in pages[:SOURCE_FETCH_LIMIT]:
        url = page.get("page_url") or ""
        title = clean(page.get("title")) or ""
        lower = f"{title} {url}".lower()
        if not model_in_text(product, f"{title} {url}") and source_domain_rank(url) < 10:
            continue
        if not payload.get("official_product_url") and source_domain_rank(url) >= 10 and not any(token in lower for token in ["manual", "pdf", "parts", "gasket"]):
            payload["official_product_url"] = url
        if not payload.get("manual_url") and any(token in lower for token in ["manual", "owner", "installation", "service-manual", "use-and-care"]):
            payload["manual_url"] = url
        if not payload.get("spec_sheet_url") and any(token in lower for token in ["spec", "specification", "cut-sheet", "cutsheet", "submittal", "brochure"]):
            payload["spec_sheet_url"] = url
        if not payload.get("lifecycle_evidence_url") and source_domain_rank(url) >= 10:
            payload["lifecycle_evidence_url"] = url
        source_bits.append(f"{title} {url}")

        try:
            text, final_url = fetch_page_text(url)
        except Exception:
            continue
        text_lower = text.lower()
        if not payload.get("manual_url") and ("manual" in text_lower or "installation instructions" in text_lower):
            payload["manual_url"] = final_url
        if not payload.get("spec_sheet_url") and ("specifications" in text_lower or "spec sheet" in text_lower):
            payload["spec_sheet_url"] = final_url
        if not payload.get("official_product_url") and source_domain_rank(final_url) >= 10 and model_in_text(product, text[:5000]):
            payload["official_product_url"] = final_url
        if not payload.get("lifecycle_evidence_url") and source_domain_rank(final_url) >= 10:
            payload["lifecycle_evidence_url"] = final_url
        if "discontinued" in text_lower or "no longer available" in text_lower:
            payload["lifecycle_status"] = "discontinued"
            payload["lifecycle_evidence_url"] = payload.get("lifecycle_evidence_url") or final_url
        elif any(token in text_lower for token in ["add to cart", "in stock", "available for purchase"]):
            payload.setdefault("lifecycle_status", "active")
            payload["lifecycle_evidence_url"] = payload.get("lifecycle_evidence_url") or final_url

        year_match = re.search(r"\b(19[8-9]\d|20[0-3]\d)\b", text)
        if year_match and not product.get("manufacture_date_text"):
            payload["manufacture_date_text"] = f"Source evidence year: {year_match.group(1)}"

    if payload:
        existing_summary = clean(product.get("data_source_summary")) or ""
        payload["data_source_summary"] = clean(
            f"{existing_summary} Main table source links backfilled from web search."
        )[:900]
    return payload


def infer_door_structure(product: dict[str, Any]) -> dict[str, Any]:
    if product.get("door_count") and product.get("door_layout") and product.get("door_positions"):
        return {}
    text = normalized(
        " ".join(
            str(product.get(key) or "")
            for key in ("brand", "equipment_model", "product_type", "data_source_summary", "official_product_url", "manual_url")
        )
    )

    count = None
    layout = None
    positions: list[dict[str, str]] = []
    confidence = 0
    source = "heuristic_brand_model_product_type"

    if "FRENCH" in text or "FRENCHDOOR" in text:
        count = 3
        layout = "french_door_3"
        positions = [
            {"key": "left_fresh_food_door", "label": "Left refrigerator door"},
            {"key": "right_fresh_food_door", "label": "Right refrigerator door"},
            {"key": "freezer_drawer", "label": "Freezer drawer"},
        ]
        confidence = 76
    elif "SIDEBYSIDE" in text or "SIDE BY SIDE" in text:
        count = 2
        layout = "side_by_side_2"
        positions = [
            {"key": "left_door", "label": "Left door"},
            {"key": "right_door", "label": "Right door"},
        ]
        confidence = 72
    elif "TOPFREEZER" in text or "TOPMOUNT" in text:
        count = 2
        layout = "top_freezer_2"
        positions = [
            {"key": "fresh_food_door", "label": "Fresh food door"},
            {"key": "freezer_door", "label": "Freezer door"},
        ]
        confidence = 68
    elif "BOTTOMFREEZER" in text or "BOTTOMMOUNT" in text or "FREEZERDRAWER" in text:
        count = 2
        layout = "bottom_freezer_2"
        positions = [
            {"key": "fresh_food_door", "label": "Fresh food door"},
            {"key": "freezer_drawer", "label": "Freezer drawer"},
        ]
        confidence = 68
    elif any(token in text for token in ["REACHIN", "BEVERAGECOOLER", "MERCHANDISER", "DISPLAYCASE", "UNDERCOUNTER"]):
        model = normalized(product.get("equipment_model"))
        if re.search(r"(?:^|[^0-9])3(?:D|DR|DOOR|R)(?:[^0-9]|$)", model):
            count = 3
        elif re.search(r"(?:^|[^0-9])2(?:D|DR|DOOR|R)(?:[^0-9]|$)", model):
            count = 2
        elif re.search(r"(?:^|[^0-9])1(?:D|DR|DOOR|R)(?:[^0-9]|$)", model):
            count = 1
        if count:
            layout = f"commercial_{count}_door"
            labels = ["Left door", "Center door", "Right door"] if count == 3 else ["Left door", "Right door"] if count == 2 else ["Single door"]
            positions = [{"key": label.lower().replace(" ", "_"), "label": label} for label in labels]
            confidence = 62

    if not count or not positions:
        return {}
    payload: dict[str, Any] = {}
    if not product.get("door_count"):
        payload["door_count"] = count
    if not product.get("door_layout"):
        payload["door_layout"] = layout
    if not product.get("door_positions"):
        payload["door_positions"] = positions
    payload["door_layout_confidence"] = max(float(product.get("door_layout_confidence") or 0), confidence)
    payload["door_layout_source"] = source
    payload["door_layout_updated_at"] = now_iso()
    return payload


def promote_image(product: dict[str, Any]) -> dict[str, Any]:
    if product.get("product_image_url") and float(product.get("product_image_confidence") or 0) >= 80:
        return {}
    with httpx.Client(timeout=35) as client:
        if product.get("product_image_url") and is_displayable_image_url(client, product.get("product_image_url"), timeout=4):
            return {}
        promoted = quick_promote_product_image(client, product, limit=6)
    if promoted:
        return {"product_image_url": "promoted"}
    return {}


def enrich_source_links(product: dict[str, Any]) -> dict[str, Any]:
    payload = page_link_candidates(product)
    if payload:
        patch_product(int(product["id"]), payload)
    return payload


def patch_one_from_source(product: dict[str, Any], field_name: str) -> dict[str, Any]:
    if is_url(product.get(field_name)):
        return {}
    payload = page_link_candidates(product)
    if field_name not in payload:
        return {}
    single = {field_name: payload[field_name]}
    if field_name == "lifecycle_evidence_url" and payload.get("lifecycle_status"):
        single["lifecycle_status"] = payload["lifecycle_status"]
    patch_product(int(product["id"]), single)
    return single


def enrich_door_structure(product: dict[str, Any]) -> dict[str, Any]:
    payload = infer_door_structure(product)
    if payload:
        patch_product(int(product["id"]), payload)
    return payload


def patch_one_from_door_structure(product: dict[str, Any], field_name: str) -> dict[str, Any]:
    if product.get(field_name):
        return {}
    payload = infer_door_structure(product)
    if field_name not in payload:
        return {}
    single = {field_name: payload[field_name]}
    if field_name in {"door_count", "door_layout", "door_positions"}:
        for meta_field in ("door_layout_confidence", "door_layout_source", "door_layout_updated_at"):
            if payload.get(meta_field):
                single[meta_field] = payload[meta_field]
    patch_product(int(product["id"]), single)
    return single


def enrich_manufacture_date(product: dict[str, Any]) -> dict[str, Any]:
    if product.get("manufacture_date") or product.get("manufacture_date_text"):
        return {}
    payload = page_link_candidates(product)
    date_text = payload.get("manufacture_date_text")
    if not date_text:
        return {}
    patch_product(int(product["id"]), {"manufacture_date_text": date_text})
    return {"manufacture_date_text": date_text}


FIELD_TASKS = {
    "product_image": promote_image,
    "manufacture_date": enrich_manufacture_date,
    "door_count": lambda product: patch_one_from_door_structure(product, "door_count"),
    "door_layout": lambda product: patch_one_from_door_structure(product, "door_layout"),
    "door_positions": lambda product: patch_one_from_door_structure(product, "door_positions"),
    "official_product_url": lambda product: patch_one_from_source(product, "official_product_url"),
    "spec_sheet_url": lambda product: patch_one_from_source(product, "spec_sheet_url"),
    "manual_url": lambda product: patch_one_from_source(product, "manual_url"),
    "lifecycle_evidence_url": lambda product: patch_one_from_source(product, "lifecycle_evidence_url"),
}


def needed_tasks(product: dict[str, Any]) -> dict[str, Any]:
    tasks = {}
    if not product.get("product_image_url"):
        tasks["product_image"] = FIELD_TASKS["product_image"]
    for field_name in ("official_product_url", "spec_sheet_url", "manual_url", "lifecycle_evidence_url"):
        if not is_url(product.get(field_name)):
            tasks[field_name] = FIELD_TASKS[field_name]
    for field_name in ("door_count", "door_layout", "door_positions"):
        if not product.get(field_name):
            tasks[field_name] = FIELD_TASKS[field_name]
    if not product.get("manufacture_date") and not product.get("manufacture_date_text"):
        tasks["manufacture_date"] = FIELD_TASKS["manufacture_date"]
    return tasks


def process_product(product: dict[str, Any], report: dict[str, Any]) -> None:
    product_id = int(product["id"])
    report["last_seen_id"] = product_id
    report["products_scanned"] += 1
    mark_attempted(product_id)

    tasks = needed_tasks(product)
    if not tasks:
        report["products_already_complete"] += 1
        write_report(report)
        return

    started = time.monotonic()
    executor = ThreadPoolExecutor(max_workers=len(tasks), thread_name_prefix=f"main-fill-{product_id}")
    futures = {name: executor.submit(fn, product) for name, fn in tasks.items()}
    try:
        for name, future in futures.items():
            remaining = max(1.0, min(FIELD_TIMEOUT_SECONDS, PRODUCT_TIMEOUT_SECONDS - (time.monotonic() - started)))
            try:
                payload = future.result(timeout=remaining)
                report["field_attempts"][name] = report["field_attempts"].get(name, 0) + 1
                if payload:
                    report["field_success"][name] = report["field_success"].get(name, 0) + 1
            except TimeoutError:
                report["field_timeouts"][name] = report["field_timeouts"].get(name, 0) + 1
                log(f"timeout {name} for {product.get('brand')} {product.get('equipment_model')} #{product_id}")
            except Exception as exc:
                report["field_errors"][name] = report["field_errors"].get(name, 0) + 1
                log(f"error {name} for {product.get('brand')} {product.get('equipment_model')} #{product_id}: {exc}")
            write_report(report)
            if time.monotonic() - started >= PRODUCT_TIMEOUT_SECONDS:
                break
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    report["recent_products"] = (
        report.get("recent_products", [])
        + [
            {
                "id": product_id,
                "brand": product.get("brand"),
                "model": product.get("equipment_model"),
                "tasks": list(tasks.keys()),
                "seconds": round(time.monotonic() - started, 2),
            }
        ]
    )[-25:]
    write_report(report)
    log(f"processed #{product_id} {product.get('brand')} {product.get('equipment_model')} tasks={','.join(tasks)}")


def initial_report() -> dict[str, Any]:
    prior = read_report()
    return {
        "started_at": now_iso(),
        "finished_at": None,
        "last_seen_id": int(prior.get("last_seen_id") or 0),
        "products_scanned": 0,
        "products_already_complete": 0,
        "field_attempts": {},
        "field_success": {},
        "field_timeouts": {},
        "field_errors": {},
        "recent_products": [],
    }


def main() -> None:
    report = initial_report()
    write_report(report)
    processed = 0
    while True:
        rows = get_next_batch(report)
        if not rows:
            report["finished_at"] = now_iso()
            write_report(report)
            log("all currently discoverable missing main-table fields processed")
            if RUN_FOREVER:
                time.sleep(300)
                continue
            break

        for product in rows:
            process_product(product, report)
            processed += 1
            if SLEEP_BETWEEN_PRODUCTS:
                time.sleep(SLEEP_BETWEEN_PRODUCTS)
            if MAX_PRODUCTS_PER_RUN and processed >= MAX_PRODUCTS_PER_RUN:
                report["finished_at"] = now_iso()
                write_report(report)
                log("max products per run reached")
                return


if __name__ == "__main__":
    main()
