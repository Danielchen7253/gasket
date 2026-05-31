"""AI-driven product and gasket enrichment for confirmed nameplates."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from trusted_sources import trusted_source_prompt


load_dotenv(Path(__file__).with_name(".env"))

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_RESEARCH_API_KEY = os.getenv(
    "OPENAI_RESEARCH_API_KEY",
    os.getenv("OPENAI_PRODUCT_RESEARCH_API_KEY", OPENAI_API_KEY),
).strip()
AI_RESEARCH_MODEL = os.getenv("OPENAI_PRODUCT_RESEARCH_MODEL", "gpt-4.1")


RESEARCH_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "product": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "brand": {"type": ["string", "null"]},
                "model": {"type": ["string", "null"]},
                "manufacturer": {"type": ["string", "null"]},
                "product_type": {"type": ["string", "null"]},
                "door_count": {"type": ["integer", "null"]},
                "door_layout": {"type": ["string", "null"]},
                "door_positions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": True,
                        "properties": {
                            "key": {"type": ["string", "null"]},
                            "label": {"type": ["string", "null"]},
                        },
                    },
                },
                "product_image_url": {"type": ["string", "null"]},
                "product_image_source_url": {"type": ["string", "null"]},
                "lifecycle_status": {"type": ["string", "null"]},
                "official_product_url": {"type": ["string", "null"]},
                "manual_url": {"type": ["string", "null"]},
                "spec_sheet_url": {"type": ["string", "null"]},
                "model_year_start": {"type": ["integer", "null"]},
                "model_year_end": {"type": ["integer", "null"]},
                "confidence_score": {"type": ["number", "null"]},
                "source_summary": {"type": ["string", "null"]},
            },
        },
        "gaskets": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "door_index": {"type": ["integer", "null"]},
                    "door_position": {"type": ["string", "null"]},
                    "door_position_display": {"type": ["string", "null"]},
                    "gasket_name": {"type": ["string", "null"]},
                    "part_number": {"type": ["string", "null"]},
                    "universal_part_number": {"type": ["string", "null"]},
                    "width_in": {"type": ["number", "null"]},
                    "height_in": {"type": ["number", "null"]},
                    "dimensions_text": {"type": ["string", "null"]},
                    "gasket_color": {"type": ["string", "null"]},
                    "gasket_install_type": {"type": ["string", "null"]},
                    "gasket_profile": {"type": ["string", "null"]},
                    "gasket_image_url": {"type": ["string", "null"]},
                    "profile_image_url": {"type": ["string", "null"]},
                    "size_status": {"type": ["string", "null"]},
                    "source_name": {"type": ["string", "null"]},
                    "source_url": {"type": ["string", "null"]},
                    "evidence_summary": {"type": ["string", "null"]},
                    "confidence_score": {"type": ["number", "null"]},
                    "needs_customer_confirmation": {"type": ["boolean", "null"]},
                    "customer_confirmation_note": {"type": ["string", "null"]},
                },
                "required": ["door_index", "door_position", "door_position_display", "gasket_name"],
            },
        },
    },
    "required": ["product", "gaskets"],
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


def normalize_model(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def extract_json_object(value: str) -> dict[str, Any]:
    try:
        return json.loads(value)
    except Exception:
        match = re.search(r"\{.*\}", value or "", re.S)
        if not match:
            return {}
        try:
            return json.loads(match.group(0))
        except Exception:
            return {}


def as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def clamp_score(value: Any, default: float = 70.0) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        score = default
    if 0 < score <= 1:
        score *= 100
    return round(max(0, min(100, score)), 2)


def price_for_dimensions(width_in: float | None, height_in: float | None) -> float:
    if width_in and height_in:
        perimeter = 2 * (width_in + height_in)
        if perimeter < 98:
            return 45.0
        if perimeter < 117:
            return 68.0
        if perimeter < 146:
            return 90.0
    return 120.0


DIMENSION_PAIR_RE = re.compile(
    r"(?P<w>\d+(?:\.\d+)?(?:\s+\d+/\d+)?)\s*(?:\"|in|inch|inches)?\s*(?:x|×|X)\s*"
    r"(?P<h>\d+(?:\.\d+)?(?:\s+\d+/\d+)?)",
    re.I,
)


def parse_fractional_number(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip().replace("-", " ")
    try:
        return round(float(value), 3)
    except ValueError:
        pass
    parts = value.split()
    total = 0.0
    for part in parts:
        if "/" in part:
            try:
                numerator, denominator = part.split("/", 1)
                total += float(numerator) / float(denominator)
            except (ValueError, ZeroDivisionError):
                return None
        else:
            try:
                total += float(part)
            except ValueError:
                return None
    return round(total, 3) if total else None


def parse_dimensions_text(value: str | None) -> tuple[float | None, float | None]:
    match = DIMENSION_PAIR_RE.search(value or "")
    if not match:
        return None, None
    return parse_fractional_number(match.group("w")), parse_fractional_number(match.group("h"))


def clean_door_key(value: str | None, index: int) -> str:
    key = re.sub(r"[^a-z0-9_]+", "_", (value or "").lower()).strip("_")
    if not key or key in {"door", "door_1", "door_2", "door_3"}:
        return f"door_{index}"
    return key[:80]


def normalize_door_position(row: dict[str, Any], index: int, layout_hint: str = "") -> dict[str, str]:
    raw = " ".join(
        str(row.get(key) or "")
        for key in ("key", "label", "door_position", "door_position_display", "gasket_name")
    )
    text = normalize_model(raw)
    layout_text = normalize_model(layout_hint)
    has_left = "LEFT" in text
    has_right = "RIGHT" in text
    has_freezer = "FREEZER" in text
    has_drawer = "DRAWER" in text
    has_fresh_food = "FRESHFOOD" in text or "REFRIGERATOR" in text or "FRIDGE" in text

    if has_left and has_fresh_food:
        return {"key": "left_fresh_food_door", "label": "Left refrigerator door"}
    if has_right and has_fresh_food:
        return {"key": "right_fresh_food_door", "label": "Right refrigerator door"}
    if has_freezer and has_drawer:
        return {"key": "freezer_drawer", "label": "Freezer drawer"}
    if has_left and has_freezer:
        return {"key": "left_freezer_door", "label": "Left freezer door"}
    if has_right and has_freezer:
        return {"key": "right_freezer_door", "label": "Right freezer door"}
    if has_freezer:
        return {"key": "freezer_door", "label": "Freezer door"}
    if has_fresh_food and ("SIDEBYSIDE" in layout_text or "SIDE" in layout_text):
        return {"key": "fresh_food_door", "label": "Fresh food door"}
    if has_left:
        return {"key": "left_door", "label": "Left door"}
    if has_right:
        return {"key": "right_door", "label": "Right door"}
    if "SINGLE" in text or "FRONT" in text:
        return {"key": "single_door", "label": "Single door"}
    key = clean_door_key(row.get("key") or row.get("door_position"), index)
    label = row.get("label") or row.get("door_position_display") or key.replace("_", " ").title()
    return {"key": key, "label": label}


def dedupe_door_positions(product: dict[str, Any], positions: list[dict[str, str]]) -> list[dict[str, str]]:
    layout_hint = " ".join(
        str(product.get(key) or "")
        for key in ("door_layout", "product_type", "source_summary")
    )
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, row in enumerate(positions, start=1):
        item = normalize_door_position(row, index, layout_hint)
        if item["key"] in seen:
            continue
        seen.add(item["key"])
        normalized.append(item)

    keys = {item["key"] for item in normalized}
    if {"fresh_food_door", "freezer_door"}.issubset(keys):
        normalized = [item for item in normalized if item["key"] not in {"left_door", "right_door"}]
        keys = {item["key"] for item in normalized}
    if {"left_fresh_food_door", "right_fresh_food_door"}.issubset(keys):
        normalized = [item for item in normalized if item["key"] not in {"left_door", "right_door", "fresh_food_door"}]
        keys = {item["key"] for item in normalized}
    if "single_door" in keys and len(normalized) > 1:
        normalized = [item for item in normalized if item["key"] != "single_door"]

    try:
        count = int(product.get("door_count") or 0)
    except Exception:
        count = 0
    layout_text = normalize_model(layout_hint)
    if "SIDEBYSIDE" in layout_text and {"fresh_food_door", "freezer_door"}.issubset({item["key"] for item in normalized}):
        return [item for item in normalized if item["key"] in {"fresh_food_door", "freezer_door"}]
    if count and len(normalized) > count:
        return normalized[:count]
    return normalized


def default_door_positions(count: int, layout_hint: str = "") -> list[dict[str, str]]:
    hint = normalize_model(layout_hint)
    if count == 3 and ("FRENCH" in hint or "BOTTOMFREEZER" in hint or "FREEZERDRAWER" in hint):
        rows = [
            ("left_fresh_food_door", "Left refrigerator door"),
            ("right_fresh_food_door", "Right refrigerator door"),
            ("freezer_drawer", "Freezer drawer"),
        ]
    elif count == 2:
        rows = [("left_door", "Left Door"), ("right_door", "Right Door")]
    elif count == 1:
        rows = [("single_door", "Single Door")]
    elif count == 4:
        rows = [
            ("upper_left_door", "Upper Left Door"),
            ("upper_right_door", "Upper Right Door"),
            ("lower_left_door", "Lower Left Door"),
            ("lower_right_door", "Lower Right Door"),
        ]
    else:
        rows = [(f"door_{index}", f"Door {index}") for index in range(1, max(count, 0) + 1)]
    return [{"key": key, "label": label} for key, label in rows]


def reconcile_door_positions(product: dict[str, Any], positions: list[dict[str, str]]) -> list[dict[str, str]]:
    try:
        count = int(product.get("door_count") or 0)
    except Exception:
        count = 0
    positions = dedupe_door_positions(product, positions)
    if count and len(positions) >= count:
        return positions[:count]
    layout_hint = " ".join(
        str(product.get(key) or "")
        for key in ("door_layout", "product_type", "source_summary")
    )
    expected = default_door_positions(count, layout_hint) if count else []
    if not expected:
        return positions
    keys = {item.get("key") for item in positions}
    merged = list(positions)
    for item in expected:
        if item["key"] not in keys:
            merged.append(item)
    return dedupe_door_positions(product, merged if len(merged) >= len(expected) else positions)


def build_prompt(brand: str, model: str, nameplate_data: dict[str, Any] | None) -> str:
    nameplate = json.dumps(nameplate_data or {}, ensure_ascii=False)
    preferred_sources = trusted_source_prompt()
    return f"""
You are researching refrigerator door gaskets for a customer quote workflow.

Confirmed nameplate:
Brand: {brand}
Model: {model}
Nameplate JSON: {nameplate}

Use web search. Search the preferred source list first, then expand to the wider web only if preferred sources do not provide enough useful data.
Preferred source list:
{preferred_sources}

Cross-check manufacturer pages, parts distributors, manuals, and appliance parts sites when available.
Return JSON only. Do not include markdown.

Rules:
- The returned brand and model must match the confirmed nameplate.
- Give the most complete source-backed answer now. Do not estimate gasket dimensions.
- Do not invent fake exact dimensions. Use size_status: "official", "cross_reference", or "unknown".
- Door positions must be customer understandable and specific: left fresh food door, right fresh food door, freezer drawer, left door, right door, etc.
- One door position equals one gasket quote item. A 3-door French door unit should return 3 gasket rows.
- The gaskets array must contain one row for every known door position. If an exact dimension is not public, leave width_in, height_in, and dimensions_text null.
- Do not return empty gaskets when a parts site, manual, or exploded diagram identifies a gasket for this exact model.
- Only use size_status "official" when the source gives the exact gasket dimensions or exact OEM part fit for this exact model and door position.
- If dimensions are not public, keep width_in, height_in, and dimensions_text null.
- Include sources. Do not infer dimensions from same-family parts.
- Do not spend effort on product images unless they are already obvious from a source page.

JSON shape:
{{
  "product": {{
    "brand": "...",
    "model": "...",
    "manufacturer": "...",
    "product_type": "...",
    "door_count": 3,
    "door_layout": "french_door_3",
    "door_positions": [
      {{"key": "left_fresh_food_door", "label": "Left refrigerator door"}}
    ],
    "product_image_url": "...",
    "product_image_source_url": "...",
    "lifecycle_status": "active|discontinued|unknown",
    "official_product_url": "...",
    "manual_url": "...",
    "spec_sheet_url": "...",
    "model_year_start": null,
    "model_year_end": null,
    "confidence_score": 0,
    "source_summary": "short source summary"
  }},
  "gaskets": [
    {{
      "door_index": 1,
      "door_position": "left_fresh_food_door",
      "door_position_display": "Left refrigerator door",
      "gasket_name": "Left refrigerator door gasket",
      "part_number": "...",
      "universal_part_number": "...",
      "width_in": null,
      "height_in": null,
      "dimensions_text": null,
      "gasket_color": "Gray",
      "gasket_install_type": "magnetic push-in gasket",
      "gasket_profile": "multi-bellows magnetic push-in",
      "gasket_image_url": "...",
      "profile_image_url": "...",
      "size_status": "official|cross_reference|unknown",
      "source_name": "...",
      "source_url": "...",
      "evidence_summary": "...",
      "confidence_score": 0,
      "needs_customer_confirmation": true,
      "customer_confirmation_note": "Confirm dimensions before production."
    }}
  ]
}}
"""


def build_gasket_followup_prompt(
    brand: str,
    model: str,
    product: dict[str, Any],
    nameplate_data: dict[str, Any] | None,
) -> str:
    product_json = json.dumps(product or {}, ensure_ascii=False)
    nameplate = json.dumps(nameplate_data or {}, ensure_ascii=False)
    preferred_sources = trusted_source_prompt()
    return f"""
The previous product research did not return usable gasket rows.

Confirmed refrigerator:
Brand: {brand}
Model: {model}
Known product JSON: {product_json}
Nameplate JSON: {nameplate}

Use web search and return JSON only with the same schema. Search these preferred sources first:
{preferred_sources}

{{"product": <same or improved product object>, "gaskets": [ ... ]}}

Focus only on refrigerator door gasket information:
- Search manufacturer parts, Sears PartsDirect, PartSelect, RepairClinic, AppliancePartsPros, PartsDr, Parts Town,
  WebstaurantStore, and manuals/exploded diagrams.
- Return one gasket row per actual door position.
- If a part number is known but dimensions are not public, return the part number and leave width_in, height_in, and dimensions_text null.
- Do not estimate dimensions from same-family structure.
- Do not use generic category dimensions unless the source clearly matches this exact model or compatible family.
"""


def _call_openai_research(prompt: str) -> dict[str, Any]:
    if not OPENAI_RESEARCH_API_KEY:
        raise RuntimeError("OpenAI key not configured")
    response = None
    errors = []
    attempts = [
        (AI_RESEARCH_MODEL, "web_search"),
        (AI_RESEARCH_MODEL, "web_search_preview"),
        ("gpt-4.1-mini", "web_search_preview"),
    ]
    for model_name, tool_type in attempts:
        payload = {
            "model": model_name,
            "tools": [{"type": tool_type}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "refrigerator_gasket_research",
                    "schema": RESEARCH_JSON_SCHEMA,
                    "strict": False,
                }
            },
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                }
            ],
        }
        response = httpx.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {OPENAI_RESEARCH_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=120,
        )
        if response.status_code < 400:
            break
        errors.append(f"{model_name}/{tool_type}: {response.status_code} {response.text[:300]}")
        response = None
    if response is None:
        raise RuntimeError("OpenAI product research failed: " + " | ".join(errors))
    response.raise_for_status()
    data = response.json()
    output_text = data.get("output_text")
    if not output_text:
        texts = []
        for item in data.get("output", []):
            for content in item.get("content", []):
                if content.get("text"):
                    texts.append(content["text"])
        output_text = "\n".join(texts)
    parsed = extract_json_object(output_text or "")
    parsed["_raw_output"] = output_text or ""
    return parsed


def request_ai_research(brand: str, model: str, nameplate_data: dict[str, Any] | None = None) -> dict[str, Any]:
    prompt = build_prompt(brand, model, nameplate_data)
    research = _call_openai_research(prompt)
    if research.get("gaskets"):
        return research
    product = research.get("product") or {"brand": brand, "model": model}
    followup = build_gasket_followup_prompt(brand, model, product, nameplate_data)
    gasket_research = _call_openai_research(followup)
    if gasket_research.get("gaskets"):
        if not gasket_research.get("product"):
            gasket_research["product"] = product
        else:
            merged_product = dict(product)
            merged_product.update({k: v for k, v in (gasket_research.get("product") or {}).items() if v not in (None, "")})
            gasket_research["product"] = merged_product
        return gasket_research
    return research


def valid_research_for_product(research: dict[str, Any], brand: str, model: str) -> bool:
    product = research.get("product") or {}
    if normalize_model(product.get("model")) and normalize_model(product.get("model")) != normalize_model(model):
        return False
    gaskets = research.get("gaskets") or []
    return isinstance(gaskets, list)


def normalize_research(research: dict[str, Any], brand: str, model: str) -> dict[str, Any]:
    product = dict(research.get("product") or {})
    product["brand"] = brand
    product["model"] = model
    raw_positions = product.get("door_positions") or []
    gaskets = [dict(row) for row in (research.get("gaskets") or []) if isinstance(row, dict)]

    positions = []
    if isinstance(raw_positions, list):
        for index, row in enumerate(raw_positions, start=1):
            if not isinstance(row, dict):
                continue
            positions.append(normalize_door_position(row, index, " ".join(str(product.get(k) or "") for k in ("door_layout", "product_type", "source_summary"))))

    if not positions:
        for index, row in enumerate(gaskets, start=1):
            positions.append(normalize_door_position(row, index, " ".join(str(product.get(k) or "") for k in ("door_layout", "product_type", "source_summary"))))

    if positions:
        positions = reconcile_door_positions(product, positions)
        product["door_positions"] = positions
        product["door_count"] = len(positions)
        if not product.get("door_layout"):
            product["door_layout"] = "_".join([str(len(positions)), "door"])

    if not positions:
        positions = reconcile_door_positions(product, [])
        if positions:
            product["door_positions"] = positions
            product["door_count"] = len(positions)
            if not product.get("door_layout"):
                product["door_layout"] = "_".join([str(len(positions)), "door"])

    if not gaskets and positions:
        gaskets = [
            {
                "door_index": index,
                "door_position": position["key"],
                "door_position_display": position["label"],
                "gasket_name": f"{position['label']} gasket",
                "gasket_install_type": "magnetic gasket",
                "size_status": "unknown",
                "source_name": "AI structured product match",
                "source_url": product.get("official_product_url") or product.get("manual_url"),
                "evidence_summary": "Product door position identified; gasket detail still requires confirmation.",
                "confidence_score": 45,
                "needs_customer_confirmation": True,
                "customer_confirmation_note": "Confirm dimensions before production.",
            }
            for index, position in enumerate(positions, start=1)
        ]

    if positions:
        aligned_gaskets: list[dict[str, Any]] = []
        used_indexes: set[int] = set()
        for position_index, position in enumerate(positions, start=1):
            match_index = None
            for gasket_index, row in enumerate(gaskets):
                if gasket_index in used_indexes:
                    continue
                row_position = normalize_door_position(
                    row,
                    gasket_index + 1,
                    " ".join(str(product.get(k) or "") for k in ("door_layout", "product_type", "source_summary")),
                )
                if row_position["key"] == position["key"]:
                    match_index = gasket_index
                    break
            if match_index is None:
                fallback_index = position_index - 1
                if fallback_index < len(gaskets) and fallback_index not in used_indexes:
                    match_index = fallback_index
                else:
                    for gasket_index in range(len(gaskets)):
                        if gasket_index not in used_indexes:
                            match_index = gasket_index
                            break
            if match_index is not None and match_index < len(gaskets):
                row = dict(gaskets[match_index])
                used_indexes.add(match_index)
            else:
                row = {
                    "gasket_install_type": "magnetic gasket",
                    "size_status": "unknown",
                    "source_name": "Door layout reconciliation",
                    "source_url": product.get("official_product_url") or product.get("manual_url"),
                    "evidence_summary": "Door position is required by the product door layout; gasket detail still needs source confirmation.",
                    "confidence_score": 45,
                    "needs_customer_confirmation": True,
                    "customer_confirmation_note": "Confirm dimensions and profile before production.",
                }
            row["door_index"] = position_index
            row["door_position"] = position["key"]
            row["door_position_display"] = position["label"]
            row["gasket_name"] = row.get("gasket_name") or f"{position['label']} gasket"
            aligned_gaskets.append(row)
        gaskets = aligned_gaskets

    normalized_gaskets = []

    used_gasket_keys: set[str] = set()
    for index, row in enumerate(gaskets, start=1):
        door_index = as_int(row.get("door_index")) or index
        key = clean_door_key(row.get("door_position"), door_index)
        label = row.get("door_position_display") or key.replace("_", " ").title()
        if key in used_gasket_keys:
            label_key = clean_door_key(label, door_index)
            key = label_key if label_key not in used_gasket_keys else f"{key}_{door_index}"
        used_gasket_keys.add(key)
        width = as_float(row.get("width_in"))
        height = as_float(row.get("height_in"))
        if not (width and height):
            parsed_width, parsed_height = parse_dimensions_text(row.get("dimensions_text"))
            width = width or parsed_width
            height = height or parsed_height
        base_price = price_for_dimensions(width, height)
        normalized_gaskets.append(
            {
                "door_index": door_index,
                "door_position": key,
                "door_position_display": label,
                "gasket_name": row.get("gasket_name") or f"{label} gasket",
                "part_number": row.get("part_number"),
                "universal_part_number": row.get("universal_part_number") or row.get("part_number"),
                "width_in": width,
                "height_in": height,
                "perimeter_in": round(2 * (width + height), 3) if width and height else None,
                "dimensions_text": row.get("dimensions_text") if (width and height or row.get("dimensions_text")) else None,
                "gasket_color": row.get("gasket_color"),
                "gasket_install_type": row.get("gasket_install_type"),
                "gasket_profile": row.get("gasket_profile"),
                "gasket_image_url": row.get("gasket_image_url"),
                "profile_image_url": row.get("profile_image_url"),
                "size_status": row.get("size_status") or ("official" if width and height else "unknown"),
                "source_name": row.get("source_name") or "AI web research",
                "source_url": row.get("source_url") or product.get("official_product_url") or product.get("manual_url"),
                "evidence_summary": row.get("evidence_summary"),
                "confidence_score": clamp_score(row.get("confidence_score"), 75),
                "needs_customer_confirmation": row.get("needs_customer_confirmation", True),
                "customer_confirmation_note": row.get("customer_confirmation_note") or "Confirm dimensions before production.",
                "base_price_usd": base_price,
                "market_price_usd": as_float(row.get("market_price_usd")),
                "final_price_usd": base_price,
                "pricing_note": "Priced from gasket perimeter size rule.",
                "data_status": "ai_structured",
                "is_verified": False,
            }
        )

    unique_gaskets: list[dict[str, Any]] = []
    seen_display_keys: set[str] = set()
    for row in sorted(normalized_gaskets, key=lambda item: float(item.get("confidence_score") or 0), reverse=True):
        display_key = normalize_model(row.get("door_position_display") or row.get("door_position"))
        if display_key and display_key in seen_display_keys:
            continue
        if display_key:
            seen_display_keys.add(display_key)
        unique_gaskets.append(row)
    unique_gaskets.sort(key=lambda item: int(item.get("door_index") or 999))

    return {"product": product, "gaskets": unique_gaskets, "_raw_output": research.get("_raw_output")}


def update_product(client: httpx.Client, product_id: int, research: dict[str, Any]) -> dict[str, Any]:
    product = research["product"]
    now = datetime.now(timezone.utc).isoformat()
    existing_response = client.get(
        f"{SUPABASE_URL}/rest/v1/refrigerator_products",
        headers=supabase_headers(),
        params={
            "id": f"eq.{product_id}",
            "select": "data_status,data_confidence,data_source_summary,door_count,door_layout,door_positions,door_layout_confidence,door_layout_source,product_type",
            "limit": "1",
        },
    )
    existing_response.raise_for_status()
    existing_rows = existing_response.json()
    existing = existing_rows[0] if existing_rows else {}
    new_confidence = clamp_score(product.get("confidence_score"), 80)
    new_door_confidence = clamp_score(product.get("door_layout_confidence") or product.get("confidence_score"), 80)
    existing_confidence = float(existing.get("data_confidence") or 0)
    existing_door_confidence = float(existing.get("door_layout_confidence") or existing_confidence or 0)
    existing_status = str(existing.get("data_status") or "")
    existing_source = str(existing.get("door_layout_source") or "")
    protect_existing_layout = (
        existing_status.startswith("manual_")
        or existing_source.startswith("manual_")
    )
    payload = {
        "manufacturer": product.get("manufacturer"),
        "product_type": product.get("product_type"),
        "official_product_url": product.get("official_product_url"),
        "spec_sheet_url": product.get("spec_sheet_url"),
        "manual_url": product.get("manual_url"),
        "lifecycle_status": product.get("lifecycle_status") or "unknown",
        "lifecycle_evidence_url": product.get("lifecycle_evidence_url") or product.get("official_product_url"),
        "model_year_start": as_int(product.get("model_year_start")),
        "model_year_end": as_int(product.get("model_year_end")),
        "data_status": "ai_structured",
        "data_confidence": new_confidence,
        "last_enriched_at": now,
        "data_source_summary": product.get("source_summary") or "AI structured web research.",
        "door_count": as_int(product.get("door_count")),
        "door_layout": product.get("door_layout"),
        "door_positions": product.get("door_positions") or [],
        "door_layout_confidence": new_door_confidence,
        "door_layout_source": "ai_structured_web_research",
        "door_layout_updated_at": now,
        "updated_at": now,
    }
    if existing_status.startswith("manual_") and existing_confidence >= new_confidence:
        payload["data_status"] = existing.get("data_status")
        payload["data_confidence"] = existing.get("data_confidence")
        payload["data_source_summary"] = existing.get("data_source_summary") or payload["data_source_summary"]
    if protect_existing_layout:
        payload["product_type"] = existing.get("product_type") or payload["product_type"]
        payload["door_count"] = existing.get("door_count")
        payload["door_layout"] = existing.get("door_layout")
        payload["door_positions"] = existing.get("door_positions")
        payload["door_layout_confidence"] = existing.get("door_layout_confidence")
        payload["door_layout_source"] = existing.get("door_layout_source")
        payload["door_layout_updated_at"] = now
    if product.get("product_image_url") and os.getenv("AI_RESEARCH_WRITE_PRODUCT_IMAGE", "1") == "1":
        payload.update(
            {
                "product_image_url": product.get("product_image_url"),
                "product_image_source_url": product.get("product_image_source_url") or product.get("official_product_url"),
                "product_image_confidence": clamp_score(product.get("product_image_confidence") or product.get("confidence_score"), 78),
            }
        )
    clean_payload = {key: value for key, value in payload.items() if value not in (None, "")}
    response = client.patch(
        f"{SUPABASE_URL}/rest/v1/refrigerator_products?id=eq.{product_id}",
        headers=supabase_headers("return=representation"),
        json=clean_payload,
    )
    response.raise_for_status()
    rows = response.json()
    return rows[0] if rows else {}


def upsert_product_gasket_spec(client: httpx.Client, product_id: int, research: dict[str, Any]) -> None:
    gaskets = research["gaskets"]
    if not gaskets:
        return
    best = max(gaskets, key=lambda row: float(row.get("confidence_score") or 0))
    sources = []
    seen = set()
    for row in gaskets:
        key = (row.get("source_name"), row.get("source_url"))
        if key in seen:
            continue
        seen.add(key)
        sources.append({"source_name": row.get("source_name"), "source_url": row.get("source_url")})
    doors = []
    for row in gaskets:
        doors.append(
            {
                "door_position": row.get("door_position"),
                "door_position_display": row.get("door_position_display"),
                "gasket_name": row.get("gasket_name"),
                "part_number": row.get("part_number"),
                "universal_part_number": row.get("universal_part_number"),
                "width_in": row.get("width_in"),
                "height_in": row.get("height_in"),
                "dimensions_text": row.get("dimensions_text"),
                "gasket_color": row.get("gasket_color"),
                "gasket_install_type": row.get("gasket_install_type"),
                "gasket_profile": row.get("gasket_profile"),
                "gasket_image_url": row.get("gasket_image_url"),
                "profile_image_url": row.get("profile_image_url"),
                "source_url": row.get("source_url"),
                "source_name": row.get("source_name"),
                "confidence_score": row.get("confidence_score"),
                "size_status": row.get("size_status"),
                "needs_customer_confirmation": row.get("needs_customer_confirmation"),
                "customer_confirmation_note": row.get("customer_confirmation_note"),
                "evidence_summary": row.get("evidence_summary"),
                "is_verified": False,
            }
        )
    payload = {
        "refrigerator_product_id": product_id,
        "primary_part_number": best.get("part_number"),
        "universal_part_number": best.get("universal_part_number"),
        "gasket_name": best.get("gasket_name"),
        "gasket_profile": best.get("gasket_profile"),
        "doors": doors,
        "source_summary": sources,
        "best_source_url": best.get("source_url"),
        "best_source_name": best.get("source_name"),
        "confidence_score": best.get("confidence_score"),
        "data_status": "ai_structured",
        "is_verified": False,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    response = client.post(
        f"{SUPABASE_URL}/rest/v1/product_gasket_specs?on_conflict=refrigerator_product_id",
        headers=supabase_headers("resolution=merge-duplicates,return=minimal"),
        json=payload,
    )
    response.raise_for_status()


def replace_flat_gaskets(client: httpx.Client, product_id: int, research: dict[str, Any]) -> None:
    gaskets = research["gaskets"]
    if not gaskets:
        return
    response = client.delete(
        f"{SUPABASE_URL}/rest/v1/refrigerator_product_gaskets"
        f"?refrigerator_product_id=eq.{product_id}&or=(data_status.is.null,data_status.neq.verified)",
        headers=supabase_headers("return=minimal"),
    )
    response.raise_for_status()
    flat_columns = [
        "refrigerator_product_id",
        "door_index",
        "door_position",
        "door_position_display",
        "gasket_name",
        "part_number",
        "universal_part_number",
        "width_in",
        "height_in",
        "dimensions_text",
        "gasket_color",
        "gasket_install_type",
        "gasket_profile",
        "gasket_image_url",
        "profile_image_url",
        "size_status",
        "source_name",
        "source_url",
        "evidence_summary",
        "confidence_score",
        "needs_customer_confirmation",
        "customer_confirmation_note",
        "base_price_usd",
        "market_price_usd",
        "final_price_usd",
        "pricing_note",
        "data_status",
        "is_verified",
        "updated_at",
    ]
    rows = []
    for row in gaskets:
        item = {column: row.get(column) for column in flat_columns}
        item["refrigerator_product_id"] = product_id
        item["updated_at"] = datetime.now(timezone.utc).isoformat()
        rows.append(item)
    response = client.post(
        f"{SUPABASE_URL}/rest/v1/refrigerator_product_gaskets",
        headers=supabase_headers("return=minimal"),
        json=rows,
    )
    response.raise_for_status()


def refresh_quote_items(client: httpx.Client, product_id: int) -> None:
    response = client.post(
        f"{SUPABASE_URL}/rest/v1/rpc/refresh_product_quote_items",
        headers=supabase_headers(),
        json={"p_product_id": product_id},
    )
    if response.status_code != 404:
        response.raise_for_status()


def enrich_confirmed_product(
    client: httpx.Client,
    product: dict[str, Any],
    nameplate_data: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    product_id = product["id"]
    if not force and product.get("data_status") == "ai_structured" and product.get("product_image_url"):
        return product
    brand = product.get("brand") or (nameplate_data or {}).get("brand") or ""
    model = product.get("equipment_model") or (nameplate_data or {}).get("model") or ""
    research = request_ai_research(brand, model, nameplate_data)
    if not valid_research_for_product(research, brand, model):
        raise RuntimeError("AI research did not match confirmed model")
    normalized = normalize_research(research, brand, model)
    updated = update_product(client, product_id, normalized)
    replace_flat_gaskets(client, product_id, normalized)
    try:
        from product_evidence import build_evidence_package, persist_evidence_package

        evidence_package = build_evidence_package(updated or product, normalized.get("gaskets") or [], nameplate_data or {}, "ai_research_completed")
        persist_evidence_package(client, evidence_package)
    except Exception as exc:
        print(f"evidence package persistence skipped for {brand} {model}: {exc}", flush=True)
    try:
        upsert_product_gasket_spec(client, product_id, normalized)
    except Exception as exc:
        print(f"compat product_gasket_specs upsert skipped for {brand} {model}: {exc}", flush=True)
    try:
        refresh_quote_items(client, product_id)
    except Exception as exc:
        print(f"quote item refresh skipped for {brand} {model}: {exc}", flush=True)
    return updated or product
