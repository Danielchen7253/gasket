"""Backfill only critical quote data: door structure and gasket sizes.

This worker intentionally does not fill product images. Images are resolved
only when a customer actually looks up a specific model.
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


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
load_dotenv(ROOT.parent / ".env")
os.environ.setdefault("AI_RESEARCH_WRITE_PRODUCT_IMAGE", "0")

from ai_product_research import enrich_confirmed_product, refresh_quote_items, supabase_headers


SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
REPORT_PATH = ROOT / "key_info_backfill_report.json"
LOG_PATH = ROOT / "key_info_backfill.log"

LIMIT = int(os.getenv("KEY_INFO_BACKFILL_LIMIT", "10"))
SLEEP_SECONDS = float(os.getenv("KEY_INFO_BACKFILL_SLEEP_SECONDS", "1.0"))
MIN_GASKET_ROWS = int(os.getenv("KEY_INFO_MIN_GASKET_ROWS", "1"))
PRIORITY_BRANDS = [
    item.strip()
    for item in os.getenv(
        "KEY_INFO_PRIORITY_BRANDS",
        "Whirlpool,GE,General Electric,Frigidaire,LG,Samsung,Sub-Zero,True,Turbo Air,"
        "Beverage-Air,Traulsen,Delfield,Viking,KitchenAid,Maytag,Bosch,Thermador,Miele,"
        "Fisher & Paykel,Haier,Arctic Air,Everest,Continental Refrigerator",
    ).split(",")
    if item.strip()
]
DIMENSION_PAIR_RE = re.compile(r"\b\d+(?:\.\d+)?(?:\s+\d+/\d+)?\s*(?:\"|in|inch|inches)?\s*[xX×]\s*\d+", re.I)

PRODUCT_SELECT = (
    "id,brand,equipment_model,manufacturer,product_type,door_count,door_layout,"
    "door_positions,data_status,data_confidence,last_enriched_at,updated_at"
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    line = f"{now_iso()} {message}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def write_report(report: dict[str, Any]) -> None:
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def normalized(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def plausible_model(value: str | None) -> bool:
    compact = normalized(value)
    if len(compact) < 3 or len(compact) > 30:
        return False
    if not any(ch.isdigit() for ch in compact):
        return False
    bad = {"IMAGE", "PDF", "XML", "JPEG", "METADATA", "STREAM", "XOBJECT"}
    return compact not in bad


def _append_unique(rows: list[dict[str, Any]], row: dict[str, Any], limit: int) -> None:
    if len(rows) >= limit:
        return
    if row.get("id") in {item.get("id") for item in rows}:
        return
    if plausible_model(row.get("equipment_model")):
        rows.append(row)


def _fetch_products(client: httpx.Client, params: dict[str, str], limit: int) -> list[dict[str, Any]]:
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/refrigerator_products",
        headers=supabase_headers(),
        params={
            "select": PRODUCT_SELECT,
            "brand": "not.is.null",
            "equipment_model": "not.is.null",
            "order": "last_enriched_at.asc.nullsfirst,updated_at.asc.nullsfirst,id.asc",
            "limit": str(max(limit * 3, 30)),
            **params,
        },
    )
    response.raise_for_status()
    return response.json()


def fetch_candidate_products(client: httpx.Client, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for brand in PRIORITY_BRANDS:
        if len(rows) >= limit:
            break
        for filters in ({"door_count": "is.null"}, {"door_positions": "eq.[]"}):
            for row in _fetch_products(client, {"brand": f"ilike.{brand}", **filters}, max(3, limit // 2)):
                _append_unique(rows, row, limit)
                if len(rows) >= limit:
                    break
            if len(rows) >= limit:
                break

    queries = [
        {"door_count": "is.null"},
        {"door_positions": "eq.[]"},
    ]
    for filters in queries:
        if len(rows) >= limit:
            break
        for row in _fetch_products(client, filters, limit):
            _append_unique(rows, row, limit)
    return rows


def gasket_state(client: httpx.Client, product_id: int) -> dict[str, int]:
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/refrigerator_product_gaskets",
        headers=supabase_headers(),
        params={
            "select": "id,width_in,height_in,dimensions_text,part_number",
            "refrigerator_product_id": f"eq.{product_id}",
            "limit": "20",
        },
    )
    response.raise_for_status()
    rows = response.json()
    with_numeric_size = [
        row
        for row in rows
        if (row.get("width_in") and row.get("height_in"))
        or DIMENSION_PAIR_RE.search(str(row.get("dimensions_text") or ""))
    ]
    return {"rows": len(rows), "with_numeric_size": len(with_numeric_size)}


def should_enrich(client: httpx.Client, product: dict[str, Any]) -> bool:
    if not product.get("door_count") or not product.get("door_layout") or not product.get("door_positions"):
        return True
    state = gasket_state(client, int(product["id"]))
    if state["rows"] < max(MIN_GASKET_ROWS, int(product.get("door_count") or 0)):
        return True
    return state["with_numeric_size"] < state["rows"]


def main() -> None:
    report = {
        "started_at": now_iso(),
        "limit": LIMIT,
        "processed": 0,
        "updated": 0,
        "skipped": 0,
        "errors": 0,
        "recent": [],
    }
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        products = fetch_candidate_products(client, LIMIT)
        log(f"loaded {len(products)} key-info candidates")
        for product in products:
            label = f"{product.get('brand')} {product.get('equipment_model')} #{product.get('id')}"
            report["processed"] += 1
            try:
                if not should_enrich(client, product):
                    report["skipped"] += 1
                    continue
                log(f"enriching {label}")
                before = gasket_state(client, int(product["id"]))
                enrich_confirmed_product(client, product, force=True)
                refresh_quote_items(client, int(product["id"]))
                after = gasket_state(client, int(product["id"]))
                report["updated"] += 1
                report["recent"].append({"product": label, "before": before, "after": after})
                write_report(report)
                if SLEEP_SECONDS:
                    time.sleep(SLEEP_SECONDS)
            except Exception as exc:
                report["errors"] += 1
                report["recent"].append({"product": label, "error": str(exc)[:500]})
                log(f"error {label}: {exc}")
                write_report(report)
                if "insufficient_quota" in str(exc) or "exceeded your current quota" in str(exc).lower():
                    report["stopped_reason"] = "OpenAI API quota is unavailable"
                    write_report(report)
                    log("stopping key-info backfill because OpenAI quota is unavailable")
                    break
    report["finished_at"] = now_iso()
    write_report(report)
    log(f"done processed={report['processed']} updated={report['updated']} errors={report['errors']}")


if __name__ == "__main__":
    main()
