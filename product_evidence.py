"""Build and optionally persist product evidence packages.

The evidence package is the product-level explanation layer. The main product
table stores the current best answer; this module explains why that answer is
trusted and what is still missing.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Any

import httpx


SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")


CORE_PRODUCT_FIELDS = [
    ("brand", "Brand"),
    ("equipment_model", "Model"),
    ("manufacturer", "Manufacturer"),
    ("product_type", "Product type"),
    ("door_count", "Door count"),
    ("door_positions", "Door positions"),
    ("product_image_url", "Product image"),
    ("lifecycle_status", "Lifecycle status"),
    ("official_product_url", "Official product page"),
    ("manual_url", "Manual"),
    ("spec_sheet_url", "Spec sheet"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def headers(prefer: str | None = None) -> dict[str, str]:
    result = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        result["Prefer"] = prefer
    return result


def present(value: Any) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, list) and not value:
        return False
    if isinstance(value, dict) and not value:
        return False
    return True


def clamp_score(value: Any, fallback: float = 60) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        score = fallback
    return max(0, min(100, round(score, 1)))


def field_value(product: dict[str, Any], nameplate: dict[str, Any], field: str) -> Any:
    if field == "brand":
        return product.get("brand") or nameplate.get("brand")
    if field == "equipment_model":
        return product.get("equipment_model") or nameplate.get("model")
    return product.get(field)


def build_evidence_package(
    product: dict[str, Any],
    quote_items: list[dict[str, Any]] | None = None,
    nameplate_data: dict[str, Any] | None = None,
    stage: str = "current_result",
) -> dict[str, Any]:
    quote_items = quote_items or []
    nameplate_data = nameplate_data or {}
    items: list[dict[str, Any]] = []

    if nameplate_data:
        items.append(
            {
                "evidence_type": "nameplate_ai_read",
                "source_type": "openai_vision",
                "source_name": "AI nameplate read",
                "field_name": "nameplate",
                "supports_value": f"{nameplate_data.get('brand') or ''} {nameplate_data.get('model') or ''}".strip(),
                "confidence_score": clamp_score(nameplate_data.get("confidence"), 70),
                "evidence_json": {
                    key: nameplate_data.get(key)
                    for key in [
                        "brand",
                        "model",
                        "serial_number",
                        "manufacturer",
                        "manufacture_date",
                        "refrigerant",
                        "voltage",
                    ]
                    if nameplate_data.get(key)
                },
            }
        )

    if product.get("data_status") in {"customer_confirmed", "ai_nameplate_pending_customer_confirmation", "customer_requested"}:
        items.append(
            {
                "evidence_type": "customer_confirmed_model",
                "source_type": "customer_confirmation",
                "source_name": "Customer confirmed nameplate form",
                "field_name": "brand_model",
                "supports_value": f"{product.get('brand') or ''} {product.get('equipment_model') or ''}".strip(),
                "confidence_score": 100 if product.get("data_status") == "customer_confirmed" else 80,
                "evidence_json": {
                    "brand": product.get("brand"),
                    "equipment_model": product.get("equipment_model"),
                    "data_status": product.get("data_status"),
                },
            }
        )

    if product.get("product_image_url"):
        items.append(
            {
                "evidence_type": "product_image",
                "source_type": "image_source",
                "source_name": "Selected product image",
                "source_url": product.get("product_image_source_url") or product.get("product_image_url"),
                "field_name": "product_image_url",
                "supports_value": product.get("product_image_url"),
                "confidence_score": clamp_score(product.get("product_image_confidence"), 70),
                "evidence_json": {
                    "image_url": product.get("product_image_url"),
                    "source_url": product.get("product_image_source_url"),
                    "verified": product.get("product_image_verified"),
                },
            }
        )

    if product.get("door_positions") or product.get("door_count"):
        items.append(
            {
                "evidence_type": "door_layout",
                "source_type": product.get("door_layout_source") or "product_profile",
                "source_name": "Door layout evidence",
                "field_name": "door_positions",
                "supports_value": product.get("door_layout") or str(product.get("door_count") or ""),
                "confidence_score": clamp_score(product.get("door_layout_confidence") or product.get("data_confidence"), 65),
                "evidence_json": {
                    "door_count": product.get("door_count"),
                    "door_layout": product.get("door_layout"),
                    "door_positions": product.get("door_positions"),
                    "source": product.get("door_layout_source"),
                },
            }
        )

    for item in quote_items:
        items.append(
            {
                "evidence_type": "gasket_match",
                "source_type": "parts_or_ai_research",
                "source_name": item.get("source_name") or "Gasket evidence",
                "source_url": item.get("source_url"),
                "field_name": "gasket_records",
                "supports_value": item.get("door_position_display") or item.get("door_position") or item.get("part_number"),
                "confidence_score": clamp_score(item.get("confidence_score"), 60),
                "evidence_json": {
                    "door_position": item.get("door_position"),
                    "door_position_display": item.get("door_position_display"),
                    "part_number": item.get("part_number") or item.get("universal_part_number"),
                    "dimensions_text": item.get("dimensions_text"),
                    "width_in": item.get("width_in"),
                    "height_in": item.get("height_in"),
                    "gasket_color": item.get("gasket_color"),
                    "gasket_install_type": item.get("gasket_install_type"),
                    "size_status": item.get("size_status"),
                    "evidence_summary": item.get("evidence_summary"),
                },
            }
        )

    missing_fields = [
        {"field_name": field, "label": label}
        for field, label in CORE_PRODUCT_FIELDS
        if not present(field_value(product, nameplate_data, field))
    ]
    if not quote_items:
        missing_fields.append({"field_name": "gasket_records", "label": "Gasket records"})

    required_count = len(CORE_PRODUCT_FIELDS) + 1
    completed_count = required_count - len(missing_fields)
    completeness_score = round(max(0, min(100, completed_count / required_count * 100)), 1)
    confidence_values = [float(item.get("confidence_score") or 0) for item in items if item.get("confidence_score") is not None]
    overall_confidence = round(sum(confidence_values) / len(confidence_values), 1) if confidence_values else 0
    if completeness_score < 50:
        status = "collecting_evidence"
    elif missing_fields:
        status = "partially_supported"
    else:
        status = "ready_for_quote"

    current_best_product = {
        "brand": product.get("brand") or nameplate_data.get("brand"),
        "equipment_model": product.get("equipment_model") or nameplate_data.get("model"),
        "manufacturer": product.get("manufacturer") or nameplate_data.get("manufacturer"),
        "product_type": product.get("product_type"),
        "door_count": product.get("door_count"),
        "door_layout": product.get("door_layout"),
        "door_positions": product.get("door_positions"),
        "product_image_url": product.get("product_image_url"),
        "lifecycle_status": product.get("lifecycle_status"),
        "data_confidence": product.get("data_confidence"),
        "data_source_summary": product.get("data_source_summary"),
    }
    current_best_gaskets = [
        {
            "door_position": item.get("door_position"),
            "door_position_display": item.get("door_position_display"),
            "part_number": item.get("part_number") or item.get("universal_part_number"),
            "dimensions_text": item.get("dimensions_text"),
            "confidence_score": item.get("confidence_score"),
            "final_price_usd": item.get("final_price_usd"),
        }
        for item in quote_items
    ]

    return {
        "refrigerator_product_id": product.get("id"),
        "brand": current_best_product.get("brand"),
        "equipment_model": current_best_product.get("equipment_model"),
        "stage": stage,
        "status": status,
        "overall_confidence": overall_confidence,
        "completeness_score": completeness_score,
        "missing_fields": missing_fields,
        "conflict_items": [],
        "current_best_product": current_best_product,
        "current_best_gaskets": current_best_gaskets,
        "items": items,
        "last_built_at": now_iso(),
    }


def persist_evidence_package(client: httpx.Client, package: dict[str, Any]) -> None:
    """Persist evidence if the optional evidence tables exist.

    The app can run before the migration is applied; in that case this quietly
    skips persistence and the page still displays the computed package.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY or not package.get("refrigerator_product_id"):
        return
    package_payload = {
        "refrigerator_product_id": package.get("refrigerator_product_id"),
        "brand": package.get("brand"),
        "equipment_model": package.get("equipment_model"),
        "stage": package.get("stage"),
        "status": package.get("status"),
        "overall_confidence": package.get("overall_confidence"),
        "completeness_score": package.get("completeness_score"),
        "profile_json": package,
        "current_best_product_json": package.get("current_best_product"),
        "current_best_gasket_json": package.get("current_best_gaskets"),
        "missing_fields": package.get("missing_fields"),
        "conflict_items": package.get("conflict_items"),
        "last_built_at": package.get("last_built_at"),
        "updated_at": now_iso(),
    }
    response = client.post(
        f"{SUPABASE_URL}/rest/v1/product_evidence_packages",
        params={"on_conflict": "refrigerator_product_id"},
        headers=headers("resolution=merge-duplicates,return=representation"),
        json=package_payload,
    )
    if response.status_code in {404, 406}:
        return
    response.raise_for_status()
    rows = response.json()
    package_id = rows[0].get("id") if rows else None
    if not package_id:
        return
    client.delete(
        f"{SUPABASE_URL}/rest/v1/product_evidence_items",
        params={"package_id": f"eq.{package_id}"},
        headers=headers("return=minimal"),
    )
    item_rows = []
    for item in package.get("items", []):
        item_rows.append(
            {
                "package_id": package_id,
                "refrigerator_product_id": package.get("refrigerator_product_id"),
                "evidence_type": item.get("evidence_type"),
                "source_type": item.get("source_type"),
                "source_name": item.get("source_name"),
                "source_url": item.get("source_url"),
                "field_name": item.get("field_name"),
                "supports_value": item.get("supports_value"),
                "confidence_score": item.get("confidence_score"),
                "evidence_json": item.get("evidence_json"),
                "conflicts": bool(item.get("conflicts")),
            }
        )
    if item_rows:
        response = client.post(
            f"{SUPABASE_URL}/rest/v1/product_evidence_items",
            headers=headers("return=minimal"),
            json=item_rows,
        )
        if response.status_code not in {404, 406}:
            response.raise_for_status()
