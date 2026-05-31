"""Promote existing product image candidates into refrigerator product main images.

For each product with a brand/model and no main product image:
- choose the best usable candidate from product_image_candidates;
- write it to refrigerator_products.product_image_url;
- mark the candidate as selected;
- if no usable candidate exists, delete that product's candidate rows so crawlers can try again.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

from product_image_search_crawler import (
    CANDIDATE_TABLE,
    PRODUCT_TABLE,
    image_quality_score,
    is_usable_image,
    supabase_headers,
)


load_dotenv(Path(__file__).with_name(".env"))

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
BATCH_SIZE = int(os.getenv("IMAGE_BACKFILL_BATCH_SIZE", "100"))
LIMIT = int(os.getenv("IMAGE_BACKFILL_LIMIT", "0"))
DRY_RUN = os.getenv("IMAGE_BACKFILL_DRY_RUN", "0") == "1"
DELETE_NO_USABLE = os.getenv("IMAGE_BACKFILL_DELETE_NO_USABLE", "1") == "1"


def fetch_products_without_images(client: httpx.Client, last_id: int, limit: int) -> list[dict]:
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/{PRODUCT_TABLE}",
        headers=supabase_headers(),
        params={
            "select": "id,brand,equipment_model,product_image_url,product_image_confidence,product_image_verified",
            "product_image_url": "is.null",
            "brand": "not.is.null",
            "equipment_model": "not.is.null",
            "id": f"gt.{last_id}",
            "order": "id.asc",
            "limit": limit,
        },
    )
    response.raise_for_status()
    return [
        row
        for row in response.json()
        if (row.get("brand") or "").strip()
        and (row.get("equipment_model") or "").strip()
        and row.get("product_image_verified") is not True
    ]


def fetch_candidates(client: httpx.Client, product_id: int) -> list[dict]:
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/{CANDIDATE_TABLE}",
        headers=supabase_headers(),
        params={
            "select": "*",
            "refrigerator_product_id": f"eq.{product_id}",
            "order": "match_score.desc.nullslast",
            "limit": 100,
        },
    )
    response.raise_for_status()
    return response.json()


def select_best_candidate(candidates: list[dict]) -> dict | None:
    usable = [candidate for candidate in candidates if is_usable_image(candidate)]
    if not usable:
        return None
    return max(usable, key=image_quality_score)


def promote_candidate(client: httpx.Client, product: dict, candidate: dict) -> None:
    if DRY_RUN:
        return
    response = client.patch(
        f"{SUPABASE_URL}/rest/v1/{PRODUCT_TABLE}",
        headers=supabase_headers("return=minimal"),
        params={"id": f"eq.{product['id']}"},
        json={
            "product_image_url": candidate["image_url"],
            "product_image_confidence": candidate.get("match_score"),
            "product_image_source_url": candidate.get("page_url"),
        },
    )
    response.raise_for_status()

    response = client.patch(
        f"{SUPABASE_URL}/rest/v1/{CANDIDATE_TABLE}",
        headers=supabase_headers("return=minimal"),
        params={"id": f"eq.{candidate['id']}"},
        json={"is_selected": True},
    )
    response.raise_for_status()


def delete_product_candidates(client: httpx.Client, product_id: int) -> int:
    if DRY_RUN:
        return 0
    response = client.delete(
        f"{SUPABASE_URL}/rest/v1/{CANDIDATE_TABLE}",
        headers=supabase_headers("return=representation"),
        params={"refrigerator_product_id": f"eq.{product_id}"},
    )
    response.raise_for_status()
    return len(response.json() or [])


def main() -> None:
    if os.getenv("PRODUCT_IMAGE_BATCH_ENABLED", "0") != "1":
        print("product image candidate batch promotion is disabled; images are filled only on customer lookup")
        return
    checked = 0
    promoted = 0
    deleted_products = 0
    deleted_candidates = 0
    no_candidates = 0
    skipped_no_usable = 0
    last_id = 0

    with httpx.Client(timeout=30) as client:
        while True:
            if LIMIT and checked >= LIMIT:
                break
            batch_limit = min(BATCH_SIZE, LIMIT - checked) if LIMIT else BATCH_SIZE
            products = fetch_products_without_images(client, last_id, batch_limit)
            if not products:
                break

            for product in products:
                last_id = max(last_id, int(product["id"]))
                checked += 1
                candidates = fetch_candidates(client, product["id"])
                if not candidates:
                    no_candidates += 1
                    continue

                best = select_best_candidate(candidates)
                if best:
                    promote_candidate(client, product, best)
                    promoted += 1
                    print(
                        f"promoted product={product['id']} {product['brand']} {product['equipment_model']} "
                        f"score={best.get('match_score')} image={best.get('image_url')}"
                    )
                    continue

                skipped_no_usable += 1
                if DELETE_NO_USABLE:
                    deleted = len(candidates) if DRY_RUN else delete_product_candidates(client, product["id"])
                    deleted_products += 1
                    deleted_candidates += deleted
                    action = "would delete" if DRY_RUN else "deleted"
                    print(
                        f"{action} unusable candidates product={product['id']} "
                        f"{product['brand']} {product['equipment_model']} count={deleted}"
                    )

    mode = "DRY RUN" if DRY_RUN else "LIVE"
    print(
        f"{mode} done: checked={checked}, promoted={promoted}, no_candidates={no_candidates}, "
        f"no_usable={skipped_no_usable}, deleted_products={deleted_products}, "
        f"deleted_candidates={deleted_candidates}"
    )


if __name__ == "__main__":
    main()
