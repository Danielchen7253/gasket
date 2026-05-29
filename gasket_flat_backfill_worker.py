import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
import httpx
from dotenv import load_dotenv


load_dotenv(Path(__file__).with_name(".env"))
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from ai_product_research import price_for_dimensions, refresh_quote_items
from gasket_enrichment_crawler import (
    MAX_SEARCH_URLS_PER_MODEL,
    SUPABASE_URL,
    canonical_part_number,
    candidate_urls,
    find_detail_from_search,
    is_valid_gasket_detail,
    public_search_results,
    supabase_headers,
)


PRODUCT_TABLE = "refrigerator_products"
FLAT_GASKET_TABLE = "refrigerator_product_gaskets"
CATALOG_TABLE = "gasket_catalog"
REPORT_PATH = Path(__file__).with_name("gasket_flat_backfill_report.json")
LOG_PATH = Path(__file__).with_name("gasket_flat_backfill.log")

PRODUCT_BATCH_SIZE = int(os.getenv("GASKET_BACKFILL_PRODUCT_BATCH_SIZE", "250"))
TARGET_WRITES = int(os.getenv("GASKET_BACKFILL_TARGET_WRITES", "500"))
MAX_PRODUCTS = int(os.getenv("GASKET_BACKFILL_MAX_PRODUCTS", "5000"))
DURATION_SECONDS = int(os.getenv("GASKET_BACKFILL_DURATION_SECONDS", "3600"))
START_AFTER_ID_ENV = os.getenv("GASKET_BACKFILL_START_AFTER_ID")
MIN_SCORE = float(os.getenv("GASKET_BACKFILL_MIN_SCORE", "55"))
SLEEP_BETWEEN_PRODUCTS = float(os.getenv("GASKET_BACKFILL_SLEEP_SECONDS", "0.1"))


def log(message: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat()} {message}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def rest_get(client: httpx.Client, table: str, params: dict[str, str]) -> list[dict]:
    response = client.get(
        f"{SUPABASE_URL.rstrip('/')}/rest/v1/{table}",
        headers=supabase_headers(),
        params=params,
    )
    response.raise_for_status()
    return response.json()


def get_product_batch(client: httpx.Client, after_id: int) -> list[dict]:
    return rest_get(
        client,
        PRODUCT_TABLE,
        {
            "select": "id,brand,equipment_model,door_count,door_positions,product_type",
            "id": f"gt.{after_id}",
            "brand": "not.is.null",
            "equipment_model": "not.is.null",
            "order": "id.asc",
            "limit": str(PRODUCT_BATCH_SIZE),
        },
    )


def existing_gasket_product_ids(client: httpx.Client, product_ids: list[int]) -> set[int]:
    if not product_ids:
        return set()
    ids = ",".join(str(item) for item in product_ids)
    rows = rest_get(
        client,
        FLAT_GASKET_TABLE,
        {
            "select": "refrigerator_product_id",
            "refrigerator_product_id": f"in.({ids})",
        },
    )
    return {int(row["refrigerator_product_id"]) for row in rows if row.get("refrigerator_product_id")}


def clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def door_for_product(product: dict) -> tuple[int, str, str]:
    positions = product.get("door_positions") or []
    if isinstance(positions, list) and len(positions) == 1:
        item = positions[0] or {}
        return 1, clean_text(item.get("key")) or "single_door", clean_text(item.get("label")) or "Single door"
    if int(product.get("door_count") or 0) == 1:
        return 1, "single_door", "Single door"
    return 1, "unspecified_door", "Door gasket"


def candidate_queries(client: httpx.Client, brand: str, model: str) -> list[tuple[str, str]]:
    urls = []
    seen = set()
    for source, url in candidate_urls(brand, model):
        key = (source, url)
        if key not in seen:
            seen.add(key)
            urls.append(key)
    try:
        for source, url in public_search_results(client, brand, model):
            key = (source, url)
            if key not in seen:
                seen.add(key)
                urls.append(key)
    except Exception as exc:
        log(f"public search skipped for {brand} {model}: {exc}")
    return urls[:MAX_SEARCH_URLS_PER_MODEL]


def find_best_detail(client: httpx.Client, product: dict) -> dict | None:
    brand = product["brand"]
    model = product["equipment_model"]
    best: dict | None = None
    for source_name, url in candidate_queries(client, brand, model):
        try:
            detail = find_detail_from_search(client, source_name, url, brand, model)
        except Exception as exc:
            log(f"search failed {brand} {model} via {source_name}: {exc}")
            continue
        if not detail or not is_valid_gasket_detail(detail):
            continue
        score = float(detail.get("confidence_score") or 0)
        if score < MIN_SCORE:
            continue
        if not best or score > float(best.get("confidence_score") or 0):
            best = detail
        if score >= 85 and (detail.get("gasket_part_number") or detail.get("dimensions_text")):
            break
    return best


def build_flat_row(product: dict, detail: dict) -> dict:
    door_index, door_key, door_label = door_for_product(product)
    width = detail.get("width_in")
    height = detail.get("height_in")
    base_price = price_for_dimensions(width, height)
    part_number = canonical_part_number(detail.get("gasket_part_number"))
    now = datetime.now(timezone.utc).isoformat()
    dimensions_text = detail.get("dimensions_text")
    return {
        "refrigerator_product_id": product["id"],
        "door_index": door_index,
        "door_position": door_key,
        "door_position_display": door_label,
        "gasket_name": clean_text(detail.get("gasket_name")) or f"{door_label} gasket",
        "part_number": part_number,
        "universal_part_number": canonical_part_number(detail.get("universal_part_number")) or part_number,
        "width_in": width,
        "height_in": height,
        "dimensions_text": dimensions_text,
        "gasket_color": detail.get("gasket_color"),
        "gasket_install_type": detail.get("gasket_install_type"),
        "gasket_profile": detail.get("gasket_profile"),
        "gasket_image_url": detail.get("gasket_image_url"),
        "profile_image_url": detail.get("profile_image_url"),
        "size_status": "source_candidate" if dimensions_text or width or height else "unknown",
        "source_name": detail.get("source_name"),
        "source_url": detail.get("source_url"),
        "evidence_summary": clean_text(
            f"Found from {detail.get('source_name') or 'parts site'} for {product['brand']} {product['equipment_model']}."
        ),
        "confidence_score": float(detail.get("confidence_score") or 0),
        "needs_customer_confirmation": True,
        "customer_confirmation_note": "Confirm dimensions before production.",
        "base_price_usd": base_price,
        "market_price_usd": None,
        "final_price_usd": base_price,
        "pricing_note": "Priced from gasket perimeter size rule.",
        "data_status": "parts_site_candidate",
        "is_verified": False,
        "updated_at": now,
    }


def replace_nonverified_flat_gasket(client: httpx.Client, product_id: int, row: dict) -> None:
    delete_response = client.delete(
        f"{SUPABASE_URL.rstrip('/')}/rest/v1/{FLAT_GASKET_TABLE}"
        f"?refrigerator_product_id=eq.{product_id}&or=(data_status.is.null,data_status.neq.verified)",
        headers=supabase_headers("return=minimal"),
    )
    delete_response.raise_for_status()
    insert_response = client.post(
        f"{SUPABASE_URL.rstrip('/')}/rest/v1/{FLAT_GASKET_TABLE}",
        headers=supabase_headers("return=minimal"),
        json=row,
    )
    insert_response.raise_for_status()


def upsert_catalog_candidate(client: httpx.Client, product: dict, row: dict) -> bool:
    part = row.get("part_number") or row.get("universal_part_number")
    if not part:
        return False
    existing = rest_get(
        client,
        CATALOG_TABLE,
        {"select": "id,compatible_equipment_models,compatible_brands,compatible_door_positions,applications", "primary_part_number": f"eq.{part}"},
    )
    application = {
        "refrigerator_product_id": product["id"],
        "brand": product["brand"],
        "equipment_model": product["equipment_model"],
        "door_position": row.get("door_position"),
        "source_name": row.get("source_name"),
        "source_url": row.get("source_url"),
        "confidence_score": row.get("confidence_score"),
    }
    payload = {
        "gasket_model": part,
        "primary_part_number": part,
        "universal_part_numbers": [part],
        "brand": product.get("brand"),
        "part_name": row.get("gasket_name"),
        "gasket_color": row.get("gasket_color"),
        "install_type": row.get("gasket_install_type"),
        "profile_type": row.get("gasket_profile"),
        "gasket_image_url": row.get("gasket_image_url"),
        "profile_image_url": row.get("profile_image_url"),
        "width_in": row.get("width_in"),
        "height_in": row.get("height_in"),
        "perimeter_in": round(2 * (row["width_in"] + row["height_in"]), 3)
        if row.get("width_in") and row.get("height_in")
        else None,
        "dimensions_text": row.get("dimensions_text"),
        "compatible_brands": [product.get("brand")],
        "compatible_equipment_models": [product.get("equipment_model")],
        "compatible_door_positions": [row.get("door_position")],
        "applications": [application],
        "source_name": row.get("source_name"),
        "source_url": row.get("source_url"),
        "evidence_summary": row.get("evidence_summary"),
        "cross_check_score": row.get("confidence_score"),
        "confidence_score": row.get("confidence_score"),
        "data_status": "parts_site_candidate",
        "is_verified": False,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if existing:
        current = existing[0]
        for list_key, value in [
            ("compatible_brands", product.get("brand")),
            ("compatible_equipment_models", product.get("equipment_model")),
            ("compatible_door_positions", row.get("door_position")),
        ]:
            values = [item for item in (current.get(list_key) or []) if item]
            if value and value not in values:
                values.append(value)
            payload[list_key] = values
        applications = current.get("applications") or []
        if not any(item.get("refrigerator_product_id") == product["id"] for item in applications if isinstance(item, dict)):
            applications.append(application)
        payload["applications"] = applications
        patch_response = client.patch(
            f"{SUPABASE_URL.rstrip('/')}/rest/v1/{CATALOG_TABLE}?id=eq.{current['id']}",
            headers=supabase_headers("return=minimal"),
            json=payload,
        )
        patch_response.raise_for_status()
        return True
    post_response = client.post(
        f"{SUPABASE_URL.rstrip('/')}/rest/v1/{CATALOG_TABLE}",
        headers=supabase_headers("return=minimal"),
        json=payload,
    )
    post_response.raise_for_status()
    return True


def write_report(report: dict) -> None:
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def start_after_id() -> int:
    if START_AFTER_ID_ENV not in (None, ""):
        return int(START_AFTER_ID_ENV)
    if not REPORT_PATH.exists():
        return 0
    try:
        report = json.loads(REPORT_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return 0
    return int(report.get("last_seen_id") or 0)


def main() -> None:
    started = time.monotonic()
    initial_after_id = start_after_id()
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "start_after_id": initial_after_id,
        "last_seen_id": initial_after_id,
        "products_scanned": 0,
        "products_without_existing_gasket": 0,
        "flat_gaskets_written": 0,
        "catalog_rows_touched": 0,
        "quote_items_refreshed": 0,
        "not_found": 0,
        "errors": 0,
        "recent_writes": [],
    }
    write_report(report)
    after_id = initial_after_id
    with httpx.Client(timeout=60) as client:
        while True:
            if time.monotonic() - started >= DURATION_SECONDS:
                log("duration limit reached")
                break
            if report["products_scanned"] >= MAX_PRODUCTS:
                log("product scan limit reached")
                break
            if report["flat_gaskets_written"] >= TARGET_WRITES:
                log("target write limit reached")
                break

            products = get_product_batch(client, after_id)
            if not products:
                log("no more products")
                break
            existing_ids = existing_gasket_product_ids(client, [int(row["id"]) for row in products])

            for product in products:
                after_id = int(product["id"])
                report["last_seen_id"] = after_id
                report["products_scanned"] += 1
                product_id = int(product["id"])
                if product_id in existing_ids:
                    continue
                brand = clean_text(product.get("brand"))
                model = clean_text(product.get("equipment_model"))
                if not brand or not model:
                    continue
                report["products_without_existing_gasket"] += 1
                try:
                    detail = find_best_detail(client, product)
                    if not detail:
                        report["not_found"] += 1
                        continue
                    row = build_flat_row(product, detail)
                    replace_nonverified_flat_gasket(client, product_id, row)
                    report["flat_gaskets_written"] += 1
                    try:
                        if upsert_catalog_candidate(client, product, row):
                            report["catalog_rows_touched"] += 1
                    except Exception as exc:
                        report["errors"] += 1
                        log(f"catalog upsert failed for {brand} {model}: {exc}")
                    try:
                        refresh_quote_items(client, product_id)
                        report["quote_items_refreshed"] += 1
                    except Exception as exc:
                        report["errors"] += 1
                        log(f"quote refresh failed for {brand} {model}: {exc}")
                    report["recent_writes"] = (
                        report["recent_writes"]
                        + [
                            {
                                "product_id": product_id,
                                "brand": brand,
                                "model": model,
                                "part_number": row.get("part_number"),
                                "dimensions": row.get("dimensions_text"),
                                "confidence": row.get("confidence_score"),
                                "source": row.get("source_name"),
                                "source_url": row.get("source_url"),
                            }
                        ]
                    )[-20:]
                    log(f"wrote gasket candidate for {brand} {model}")
                    if report["flat_gaskets_written"] >= TARGET_WRITES:
                        break
                except Exception as exc:
                    report["errors"] += 1
                    log(f"product failed {brand} {model}: {exc}")
                finally:
                    write_report(report)
                    if SLEEP_BETWEEN_PRODUCTS:
                        time.sleep(SLEEP_BETWEEN_PRODUCTS)

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_report(report)
    log(
        "done scanned={products_scanned} missing={products_without_existing_gasket} "
        "written={flat_gaskets_written} not_found={not_found} errors={errors}".format(**report)
    )


if __name__ == "__main__":
    main()
