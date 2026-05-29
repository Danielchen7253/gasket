"""Backfill stable fields on refrigerator_products from existing brand/model data.

This worker is intentionally conservative. It fills only fields that can be
derived safely from the existing brand, model, and import source:

- manufacturer
- product_type
- lifecycle_status
- data_confidence
- data_source_summary
- last_enriched_at

It does not invent product images, manuals, spec sheets, dates, or door layouts.
Those require source-specific evidence and should be handled by separate
targeted workers.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
from typing import Any

import httpx
from dotenv import load_dotenv


def load_environment() -> None:
    for path in [Path(__file__).with_name(".env"), Path(__file__).parent.parent / ".env"]:
        if path.exists():
            load_dotenv(path)
            return
    load_dotenv()


load_environment()

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

PRODUCT_TABLE = "refrigerator_products"
REPORT_PATH = Path(os.getenv("MAIN_BACKFILL_REPORT_PATH", "main_table_backfill_report.json"))
SELECT_COLUMNS = (
    "id,brand,equipment_model,manufacturer,product_type,lifecycle_status,"
    "data_status,data_confidence,data_source_summary,last_enriched_at"
)

ICE_BRANDS = {"Hoshizaki", "Manitowoc Ice", "Scotsman", "Ice-O-Matic", "Icetro"}
COMMERCIAL_REFRIGERATION_BRANDS = {
    "AHT Cooling Systems",
    "Arctic Air",
    "Atosa",
    "Avantco",
    "Beverage-Air",
    "Continental",
    "Delfield",
    "Everest",
    "Glastender",
    "Hussmann",
    "Kelvinator",
    "Master-Bilt",
    "Nor-Lake",
    "Perlick",
    "Randell",
    "Traulsen",
    "True",
    "Turbo Air",
    "Victory",
}
HOME_BRANDS = {
    "Bosch",
    "Frigidaire",
    "GE",
    "Haier",
    "Hisense",
    "Kenmore",
    "KitchenAid",
    "LG",
    "Maytag",
    "Samsung",
    "Whirlpool",
}


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


def normalize_model(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def infer_product_type(row: dict[str, Any]) -> str:
    brand = str(row.get("brand") or "").strip()
    model = normalize_model(row.get("equipment_model"))
    status = str(row.get("data_status") or "")

    if brand in ICE_BRANDS:
        return "ice machine"
    if brand == "Hussmann":
        return "commercial display refrigeration"
    if brand in HOME_BRANDS or status == "home_parts_catalog":
        if any(token in model for token in ("FFFU", "FFFC", "WZF", "WZC", "EV", "EH", "MFU", "MFC")):
            return "residential freezer"
        if model.startswith(("KUIS", "KUID")):
            return "residential ice maker"
        return "residential refrigerator/freezer"
    if brand in COMMERCIAL_REFRIGERATION_BRANDS or status == "parts_catalog":
        return "commercial refrigeration equipment"
    return "refrigeration equipment"


def default_confidence(row: dict[str, Any]) -> int:
    status = str(row.get("data_status") or "")
    if status == "home_parts_catalog":
        return 58
    if status == "parts_catalog":
        return 55
    if status in {"ai_structured", "manual_research_structured"}:
        return 75
    if status == "enriched":
        return 65
    return 50


def default_summary(row: dict[str, Any]) -> str:
    brand = row.get("brand") or ""
    model = row.get("equipment_model") or ""
    status = row.get("data_status") or "pending"
    return f"Main table baseline derived from existing brand/model record: {brand} {model}. Source status: {status}."


def patch_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    brand = str(row.get("brand") or "").strip()

    if brand and brand != row.get("brand"):
        payload["brand"] = brand
    if brand and not row.get("manufacturer"):
        payload["manufacturer"] = brand
    if not row.get("product_type"):
        payload["product_type"] = infer_product_type(row)
    if not row.get("lifecycle_status"):
        payload["lifecycle_status"] = "unknown"
    if row.get("data_confidence") is None:
        payload["data_confidence"] = default_confidence(row)
    if not row.get("data_source_summary"):
        payload["data_source_summary"] = default_summary(row)

    payload["last_enriched_at"] = now_iso()
    return payload


def needs_patch(row: dict[str, Any]) -> bool:
    return bool(
        not row.get("manufacturer")
        or not row.get("product_type")
        or not row.get("lifecycle_status")
        or row.get("data_confidence") is None
        or not row.get("data_source_summary")
    )


def fetch_batch(client: httpx.Client, last_id: int, limit: int) -> list[dict[str, Any]]:
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/{PRODUCT_TABLE}",
        params={
            "select": SELECT_COLUMNS,
            "id": f"gt.{last_id}",
            "brand": "not.is.null",
            "equipment_model": "not.is.null",
            "order": "id.asc",
            "limit": str(limit),
        },
        headers=headers(),
    )
    response.raise_for_status()
    return response.json()


def patch_row(client: httpx.Client, product_id: int, payload: dict[str, Any]) -> None:
    response = client.patch(
        f"{SUPABASE_URL}/rest/v1/{PRODUCT_TABLE}",
        params={"id": f"eq.{product_id}"},
        headers=headers("return=minimal"),
        json=payload,
    )
    response.raise_for_status()


def write_report(report: dict[str, Any]) -> None:
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    batch_size = int(os.getenv("MAIN_BACKFILL_BATCH_SIZE", "500"))
    max_rows = int(os.getenv("MAIN_BACKFILL_MAX_ROWS", "0"))
    duration_seconds = int(os.getenv("MAIN_BACKFILL_DURATION_SECONDS", "0"))
    sleep_seconds = float(os.getenv("MAIN_BACKFILL_SLEEP_SECONDS", "0.02"))
    started = time.time()
    last_id = int(os.getenv("MAIN_BACKFILL_START_AFTER_ID", "0"))
    scanned = patched = errors = 0
    examples: list[dict[str, Any]] = []

    report = {
        "status": "running",
        "started_at": now_iso(),
        "updated_at": now_iso(),
        "last_id": last_id,
        "scanned": scanned,
        "patched": patched,
        "errors": errors,
        "examples": examples,
    }
    write_report(report)

    with httpx.Client(timeout=60, follow_redirects=True) as client:
        while True:
            if duration_seconds and time.time() - started >= duration_seconds:
                report["status"] = "time_limit_reached"
                break
            if max_rows and scanned >= max_rows:
                report["status"] = "row_limit_reached"
                break

            rows = fetch_batch(client, last_id, batch_size)
            if not rows:
                report["status"] = "complete"
                break

            for row in rows:
                last_id = int(row["id"])
                scanned += 1
                if max_rows and scanned > max_rows:
                    report["status"] = "row_limit_reached"
                    break
                if duration_seconds and time.time() - started >= duration_seconds:
                    report["status"] = "time_limit_reached"
                    break

                if needs_patch(row):
                    payload = patch_payload(row)
                    try:
                        patch_row(client, last_id, payload)
                        patched += 1
                        if len(examples) < 10:
                            examples.append({"id": last_id, "brand": row.get("brand"), "model": row.get("equipment_model"), "patched": sorted(payload)})
                    except Exception as exc:
                        errors += 1
                        print(f"patch failed id={last_id}: {exc}", flush=True)

                if sleep_seconds:
                    time.sleep(sleep_seconds)

            report.update(
                {
                    "updated_at": now_iso(),
                    "last_id": last_id,
                    "scanned": scanned,
                    "patched": patched,
                    "errors": errors,
                    "elapsed_seconds": round(time.time() - started, 1),
                    "examples": examples,
                }
            )
            write_report(report)
            print(f"scanned={scanned} patched={patched} errors={errors} last_id={last_id}", flush=True)

            if max_rows and scanned >= max_rows:
                report["status"] = "row_limit_reached"
                break

    report.update(
        {
            "finished_at": now_iso(),
            "updated_at": now_iso(),
            "last_id": last_id,
            "scanned": scanned,
            "patched": patched,
            "errors": errors,
            "elapsed_seconds": round(time.time() - started, 1),
            "examples": examples,
        }
    )
    write_report(report)
    print(json.dumps(report, indent=2, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
