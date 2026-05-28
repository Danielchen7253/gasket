"""Comprehensive background backfill for refrigerator product records.

This worker is intentionally product-centric: each product is checked for
missing customer-facing data, then independent enrichers fill only the gaps.
One slow or failed source cannot block the other fields for the same product
or the next product in the batch.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import time
from typing import Any

import httpx
from dotenv import load_dotenv

from ai_product_research import enrich_confirmed_product, refresh_quote_items
from fast_image_patch import quick_promote_product_image
from product_image_search_crawler import is_displayable_image_url, supabase_headers


load_dotenv(Path(__file__).with_name(".env"))

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

PRODUCT_SELECT = (
    "id,brand,equipment_model,manufacturer,product_type,product_image_url,"
    "product_image_source_url,product_image_confidence,door_count,door_layout,"
    "door_positions,lifecycle_status,data_status,data_confidence,"
    "data_source_summary,last_enriched_at,updated_at"
)


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


def patch_product(client: httpx.Client, product_id: int, payload: dict[str, Any]) -> None:
    clean = {key: value for key, value in payload.items() if value is not None}
    if not clean:
        return
    response = client.patch(
        f"{SUPABASE_URL}/rest/v1/refrigerator_products",
        params={"id": f"eq.{product_id}"},
        headers=headers("return=minimal"),
        json=clean,
    )
    response.raise_for_status()


def get_products(client: httpx.Client, limit: int) -> list[dict[str, Any]]:
    """Fetch products most likely to need useful customer-facing enrichment."""
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/refrigerator_products",
        params={
            "select": PRODUCT_SELECT,
            "brand": "not.is.null",
            "equipment_model": "not.is.null",
            "order": "last_enriched_at.asc.nullsfirst,updated_at.asc.nullsfirst,id.asc",
            "limit": str(limit),
        },
        headers=headers(),
    )
    response.raise_for_status()
    return [row for row in response.json() if row.get("brand") and row.get("equipment_model")]


def gasket_rows(client: httpx.Client, product_id: int) -> list[dict[str, Any]]:
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/refrigerator_product_gaskets",
        params={
            "select": (
                "id,door_position,door_position_display,part_number,width_in,height_in,"
                "dimensions_text,final_price_usd,confidence_score,data_status"
            ),
            "refrigerator_product_id": f"eq.{product_id}",
            "order": "door_index.asc,id.asc",
        },
        headers=headers(),
    )
    response.raise_for_status()
    return response.json()


def quote_item_rows(client: httpx.Client, product_id: int) -> list[dict[str, Any]]:
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/refrigerator_product_quote_items",
        params={
            "select": "refrigerator_product_id,door_position,dimensions_text,final_price_usd,confidence_score",
            "refrigerator_product_id": f"eq.{product_id}",
            "limit": "20",
        },
        headers=headers(),
    )
    if response.status_code == 404:
        return []
    response.raise_for_status()
    return response.json()


def expected_door_count(product: dict[str, Any]) -> int:
    try:
        count = int(product.get("door_count") or 0)
    except (TypeError, ValueError):
        count = 0
    positions = product.get("door_positions") or []
    if isinstance(positions, list):
        count = max(count, len(positions))
    return count


def image_missing_or_dead(client: httpx.Client, product: dict[str, Any]) -> bool:
    confidence = float(product.get("product_image_confidence") or 0)
    if confidence >= 80 and product.get("product_image_url"):
        return False
    return not is_displayable_image_url(client, product.get("product_image_url"), timeout=3.0)


def structure_missing(product: dict[str, Any]) -> bool:
    return not all(
        [
            product.get("product_type"),
            product.get("door_count"),
            product.get("door_layout"),
            product.get("door_positions"),
        ]
    )


def gaskets_missing_or_incomplete(product: dict[str, Any], gaskets: list[dict[str, Any]], quote_items: list[dict[str, Any]]) -> bool:
    expected = expected_door_count(product)
    if not gaskets:
        return True
    if expected and len(gaskets) < expected:
        return True
    if expected and len(quote_items) < expected:
        return True
    seen_labels: set[str] = set()
    for row in gaskets:
        label_key = str(row.get("door_position_display") or row.get("door_position") or "").strip().lower()
        if label_key and label_key in seen_labels:
            return True
        if label_key:
            seen_labels.add(label_key)
        if not row.get("door_position_display"):
            return True
        if not row.get("part_number") and not row.get("dimensions_text"):
            return True
        if not row.get("final_price_usd"):
            return True
    return False


def needs_ai_research(product: dict[str, Any], gaskets: list[dict[str, Any]], quote_items: list[dict[str, Any]]) -> bool:
    return structure_missing(product) or gaskets_missing_or_incomplete(product, gaskets, quote_items)


def run_image_step(client: httpx.Client, product: dict[str, Any]) -> bool:
    if not image_missing_or_dead(client, product):
        return False
    return bool(quick_promote_product_image(client, product, limit=int(os.getenv("COMPREHENSIVE_IMAGE_LIMIT", "8"))))


def run_ai_step(client: httpx.Client, product: dict[str, Any]) -> bool:
    updated = enrich_confirmed_product(client, product, force=True)
    return bool(updated)


def summarize_product(product: dict[str, Any]) -> str:
    return f"{product.get('brand')} {product.get('equipment_model')} #{product.get('id')}"


def process_product(client: httpx.Client, product: dict[str, Any], ai_budget: dict[str, int]) -> dict[str, Any]:
    product_id = int(product["id"])
    stats = {
        "id": product_id,
        "image_promoted": False,
        "ai_enriched": False,
        "ai_deferred": False,
        "quote_refreshed": False,
        "errors": [],
    }

    patch_product(
        client,
        product_id,
        {"last_enriched_at": now_iso()},
    )

    try:
        stats["image_promoted"] = run_image_step(client, product)
    except Exception as exc:  # keep the rest of the product moving
        stats["errors"].append(f"image: {exc}")

    gaskets: list[dict[str, Any]] = []
    quote_items: list[dict[str, Any]] = []
    try:
        gaskets = gasket_rows(client, product_id)
        quote_items = quote_item_rows(client, product_id)
    except Exception as exc:
        stats["errors"].append(f"read gasket state: {exc}")

    if needs_ai_research(product, gaskets, quote_items):
        if ai_budget["remaining"] > 0:
            try:
                ai_budget["remaining"] -= 1
                stats["ai_enriched"] = run_ai_step(client, product)
            except Exception as exc:
                stats["errors"].append(f"ai: {exc}")
        else:
            stats["ai_deferred"] = True

    try:
        refresh_quote_items(client, product_id)
        stats["quote_refreshed"] = True
    except Exception as exc:
        print(f"  quote refresh skipped: {exc}", flush=True)

    try:
        latest = get_product_by_id(client, product_id) or product
        if image_missing_or_dead(client, latest):
            stats["image_promoted"] = run_image_step(client, latest) or stats["image_promoted"]
    except Exception as exc:
        stats["errors"].append(f"second image pass: {exc}")

    patch_product(
        client,
        product_id,
        {"last_enriched_at": now_iso()},
    )
    return stats


def get_product_by_id(client: httpx.Client, product_id: int) -> dict[str, Any] | None:
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/refrigerator_products",
        params={"select": PRODUCT_SELECT, "id": f"eq.{product_id}", "limit": "1"},
        headers=headers(),
    )
    response.raise_for_status()
    rows = response.json()
    return rows[0] if rows else None


def main() -> None:
    limit = int(os.getenv("COMPREHENSIVE_BACKFILL_LIMIT", "25"))
    max_ai = int(os.getenv("COMPREHENSIVE_BACKFILL_MAX_AI", "0"))
    sleep_seconds = float(os.getenv("COMPREHENSIVE_BACKFILL_SLEEP_SECONDS", "1.5"))
    ai_budget = {"remaining": max_ai}

    print(f"comprehensive backfill starting: limit={limit}, max_ai={max_ai}")
    started = time.time()
    totals = {"processed": 0, "image_promoted": 0, "ai_enriched": 0, "quote_refreshed": 0, "errors": 0}

    with httpx.Client(timeout=90, follow_redirects=True) as client:
        products = get_products(client, limit)
        print(f"loaded {len(products)} products")
        for product in products:
            print(f"processing {summarize_product(product)}")
            result = process_product(client, product, ai_budget)
            totals["processed"] += 1
            totals["image_promoted"] += int(bool(result["image_promoted"]))
            totals["ai_enriched"] += int(bool(result["ai_enriched"]))
            totals["quote_refreshed"] += int(bool(result["quote_refreshed"]))
            totals["errors"] += int(bool(result["errors"]))
            if result["errors"]:
                print(f"  partial: {' | '.join(result['errors'])}")
            elif result.get("ai_deferred"):
                print("  completed current pass; AI enrichment deferred by run budget")
            else:
                print("  completed")
            if sleep_seconds:
                time.sleep(sleep_seconds)

    elapsed = round(time.time() - started, 1)
    print(f"comprehensive backfill done in {elapsed}s: {totals}")


if __name__ == "__main__":
    main()
