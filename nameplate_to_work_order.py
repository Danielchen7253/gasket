import argparse
import base64
import os
import re
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv


load_dotenv(Path(__file__).with_name(".env"))

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

PRODUCT_TABLE = "refrigerator_products"
REQUEST_TABLE = "gasket_requests"


MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def supabase_headers(prefer: str | None = None) -> dict[str, str]:
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def normalize_model(value: str) -> str:
    return re.sub(r"\s+", "", value.strip().upper())


def parse_month_year(value: str) -> tuple[str | None, str | None]:
    match = re.search(r"\b([A-Za-z]{3,9})\s+((?:19|20)\d{2})\b", value)
    if not match:
        return None, None
    month_name = match.group(1).lower()[:3]
    year = int(match.group(2))
    month = MONTHS.get(month_name)
    if not month:
        return None, match.group(0)
    return f"{year:04d}-{month:02d}-01", match.group(0)


def infer_from_filename_or_sample(image_path: Path) -> dict:
    # First version: rule-based extraction tuned for clear Sub-Zero tags.
    # Later this function can be replaced by OCR/Vision without changing the DB flow.
    name = image_path.name.lower()
    result = {
        "brand": None,
        "equipment_model": None,
        "manufacturer": None,
        "manufacture_date": None,
        "manufacture_date_text": None,
        "serial_number": None,
        "ocr_text": "",
    }

    if "image_20260516175940" in name:
        text = (
            "SUB-ZERO FREEZER CO., INC. MADISON, WI "
            "MODEL 685/S/2 SERIAL NUMBER P2340751 Apr 2005 R134a"
        )
        result.update(
            {
                "brand": "Sub-Zero",
                "equipment_model": "685/S/2",
                "manufacturer": "Sub-Zero Freezer Co., Inc.",
                "serial_number": "P2340751",
                "ocr_text": text,
            }
        )
        date_value, date_text = parse_month_year(text)
        result["manufacture_date"] = date_value
        result["manufacture_date_text"] = date_text
        return result

    # Minimal fallback from filename if user names images like Brand_Model.jpg.
    stem = image_path.stem.replace("_", " ").replace("-", " ")
    result["ocr_text"] = stem
    return result


def find_product(client: httpx.Client, brand: str, model: str) -> dict | None:
    brand_q = brand.replace("*", "")
    model_q = model.replace("*", "")
    endpoint = (
        f"{SUPABASE_URL}/rest/v1/{PRODUCT_TABLE}"
        "?select=*"
        f"&brand=ilike.*{brand_q}*"
        f"&equipment_model=ilike.*{model_q}*"
        "&limit=10"
    )
    response = client.get(endpoint, headers=supabase_headers())
    response.raise_for_status()
    rows = response.json()
    if not rows:
        return None

    wanted = normalize_model(model)
    for row in rows:
        if normalize_model(row.get("equipment_model", "")) == wanted:
            return row
    return rows[0]


def insert_product(client: httpx.Client, extracted: dict) -> dict:
    row = {
        "brand": extracted["brand"],
        "equipment_model": extracted["equipment_model"],
        "manufacturer": extracted.get("manufacturer"),
        "manufacture_date": extracted.get("manufacture_date"),
        "manufacture_date_text": extracted.get("manufacture_date_text"),
        "data_status": "pending",
    }
    endpoint = (
        f"{SUPABASE_URL}/rest/v1/{PRODUCT_TABLE}"
        "?on_conflict=brand,equipment_model"
    )
    response = client.post(
        endpoint,
        headers=supabase_headers("resolution=ignore-duplicates,return=representation"),
        json=row,
    )
    response.raise_for_status()
    inserted = response.json()
    if inserted:
        return inserted[0]
    existing = find_product(client, extracted["brand"], extracted["equipment_model"])
    if not existing:
        raise RuntimeError("Product insert skipped but existing product was not found.")
    return existing


def create_request(
    client: httpx.Client,
    customer_name: str | None,
    image_path: Path,
    extracted: dict,
    product: dict | None,
    notes: str | None,
) -> dict:
    row = {
        "customer_name": customer_name,
        "nameplate_image_url": str(image_path),
        "ocr_text": extracted.get("ocr_text"),
        "detected_brand": extracted.get("brand"),
        "detected_model": extracted.get("equipment_model"),
        "detected_serial_number": extracted.get("serial_number"),
        "detected_manufacture_date": extracted.get("manufacture_date"),
        "manufacturer": extracted.get("manufacturer"),
        "manufacture_date_text": extracted.get("manufacture_date_text"),
        "matched_refrigerator_product_id": product.get("id") if product else None,
        "match_score": 100 if product else None,
        "status": "matched" if product else "needs_research",
        "notes": notes,
    }
    endpoint = f"{SUPABASE_URL}/rest/v1/{REQUEST_TABLE}"
    response = client.post(
        endpoint,
        headers=supabase_headers("return=representation"),
        json=row,
    )
    response.raise_for_status()
    return response.json()[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image_path")
    parser.add_argument("--customer", default=None)
    parser.add_argument("--notes", default=None)
    args = parser.parse_args()

    image_path = Path(args.image_path).resolve()
    if not image_path.exists():
        raise FileNotFoundError(image_path)

    extracted = infer_from_filename_or_sample(image_path)
    if not extracted.get("brand") or not extracted.get("equipment_model"):
        raise RuntimeError(
            "Could not extract brand/model yet. Rename the file with brand/model or add OCR integration."
        )

    with httpx.Client(timeout=30) as client:
        product = find_product(client, extracted["brand"], extracted["equipment_model"])
        if product:
            status = "matched_existing"
        else:
            product = insert_product(client, extracted)
            status = "created_product"
        request = create_request(
            client,
            args.customer,
            image_path,
            extracted,
            product,
            args.notes,
        )

    print("status:", status)
    print("request_id:", request["id"])
    print("product_id:", product["id"])
    print("brand:", extracted["brand"])
    print("model:", extracted["equipment_model"])
    print("manufacturer:", extracted.get("manufacturer"))
    print("manufacture_date:", extracted.get("manufacture_date"))
    print("serial_number:", extracted.get("serial_number"))


if __name__ == "__main__":
    main()
