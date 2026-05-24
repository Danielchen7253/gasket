import os
from pathlib import Path

import httpx
from dotenv import load_dotenv


load_dotenv(Path(__file__).with_name(".env"))

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]


def headers(prefer: str | None = None) -> dict[str, str]:
    data = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        data["Prefer"] = prefer
    return data


def get_product_ids(client: httpx.Client, limit: int) -> list[int]:
    product_ids: list[int] = []
    offset = 0
    page_size = 1000
    while len(product_ids) < limit:
        take = min(page_size, limit - len(product_ids))
        response = client.get(
            f"{SUPABASE_URL}/rest/v1/product_gasket_specs"
            "?select=refrigerator_product_id"
            "&order=updated_at.desc.nullslast"
            f"&limit={take}&offset={offset}",
            headers=headers(),
        )
        response.raise_for_status()
        rows = response.json()
        if not rows:
            break
        product_ids.extend(row["refrigerator_product_id"] for row in rows)
        offset += len(rows)
        if len(rows) < take:
            break
    return product_ids


def refresh_quote_items(client: httpx.Client, product_id: int) -> int:
    response = client.post(
        f"{SUPABASE_URL}/rest/v1/rpc/refresh_product_quote_items",
        headers=headers(),
        json={"p_product_id": product_id},
    )
    response.raise_for_status()
    data = response.json()
    return int(data) if isinstance(data, int) else 0


def main() -> None:
    limit = int(os.getenv("QUOTE_BACKFILL_LIMIT", "250"))
    with httpx.Client(timeout=60) as client:
        product_ids = get_product_ids(client, limit)
        total = 0
        for product_id in product_ids:
            total += refresh_quote_items(client, product_id)
    print(f"refreshed quote items for {len(product_ids)} products; affected rows {total}")


if __name__ == "__main__":
    main()
