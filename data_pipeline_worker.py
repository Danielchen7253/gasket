import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

import gasket_enrichment_crawler
import market_discovery_crawler
import product_image_search_crawler


load_dotenv(Path(__file__).with_name(".env"))

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]


def supabase_headers(prefer: str | None = None) -> dict[str, str]:
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def run_step(name: str, func) -> None:
    print(f"starting {name}")
    try:
        func()
    except Exception as exc:
        print(f"{name} failed: {exc}")
    print(f"finished {name}")


def ensure_gasket_placeholders(limit: int) -> None:
    endpoint = (
        f"{SUPABASE_URL}/rest/v1/refrigerator_products"
        "?select=id"
        "&data_status=in.(pending,missing)"
        f"&limit={limit}"
    )
    with httpx.Client(timeout=30) as client:
        response = client.get(endpoint, headers=supabase_headers())
        response.raise_for_status()
        products = response.json()
        if not products:
            print("no new products need gasket placeholders")
            return

        rows = [
            {
                "refrigerator_product_id": product["id"],
                "doors": [],
                "source_summary": [],
                "confidence_score": 0,
                "data_status": "missing",
            }
            for product in products
        ]
        response = client.post(
            f"{SUPABASE_URL}/rest/v1/product_gasket_specs?on_conflict=refrigerator_product_id",
            headers=supabase_headers("resolution=ignore-duplicates,return=minimal"),
            json=rows,
        )
        response.raise_for_status()
        print(f"prepared gasket placeholders for {len(rows)} products")


def main() -> None:
    os.environ.setdefault("DISCOVERY_QUERY_LIMIT", "8")
    os.environ.setdefault("DISCOVERY_RESULTS_PER_QUERY", "8")
    os.environ.setdefault("DISCOVERY_SLEEP_SECONDS", "0.5")
    os.environ.setdefault("PRODUCT_IMAGE_LIMIT", "120")
    os.environ.setdefault("ENRICH_LIMIT", "120")
    os.environ.setdefault("CRAWL_DELAY", "0.2")

    cycles = int(os.getenv("PIPELINE_CYCLES", "1"))
    pause_seconds = float(os.getenv("PIPELINE_PAUSE_SECONDS", "0"))

    for index in range(cycles):
        print(f"pipeline cycle {index + 1}/{cycles}")
        run_step("model discovery", market_discovery_crawler.main)
        run_step("product image backfill", product_image_search_crawler.main)
        ensure_gasket_placeholders(int(os.getenv("ENRICH_LIMIT", "120")))
        run_step("gasket data backfill", gasket_enrichment_crawler.main)
        if index + 1 < cycles and pause_seconds:
            time.sleep(pause_seconds)

    print("pipeline done")


if __name__ == "__main__":
    main()
