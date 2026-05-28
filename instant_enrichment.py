"""On-demand enrichment for the product a customer is actively checking."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
import threading
import time
from typing import Any

import httpx
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(ROOT, ".env"))
load_dotenv(os.path.join(os.path.dirname(ROOT), ".env"))

from ai_product_research import enrich_confirmed_product
from fast_image_patch import quick_promote_product_image
from product_image_search_crawler import is_displayable_image_url, supabase_headers
from trusted_sources import trusted_source_prompt

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
TASK_TABLE = "product_enrichment_tasks"

RUNNING: set[int] = set()

MODEL_ALIAS_OVERRIDES = {
    ("SUBZERO", "685592"): "685/S/2",
    ("SUBZERO", "68592"): "685/S/2",
    ("SUBZERO", "685S2"): "685/S/2",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact(value: Any) -> str:
    return str(value or "").strip()


def patch_product(client: httpx.Client, product_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    clean = {key: value for key, value in payload.items() if value not in (None, "")}
    if not clean:
        return get_product(client, product_id) or {}
    response = client.patch(
        f"{SUPABASE_URL}/rest/v1/refrigerator_products",
        params={"id": f"eq.{product_id}"},
        headers=supabase_headers("return=representation"),
        json=clean,
    )
    response.raise_for_status()
    rows = response.json()
    return rows[0] if rows else {}


def find_product(client: httpx.Client, brand: str, model: str) -> dict[str, Any] | None:
    alias = MODEL_ALIAS_OVERRIDES.get((normalize_model(brand), normalize_model(model)))
    if alias and normalize_model(alias) != normalize_model(model):
        product = find_product(client, brand, alias)
        if product:
            return product
    variants = model_variants(model)
    brand_variants = [brand, brand.upper(), brand.title()]
    for brand_value in brand_variants:
        for model_value in variants:
            response = client.get(
                f"{SUPABASE_URL}/rest/v1/refrigerator_products",
                params={
                    "select": "*",
                    "brand": f"ilike.{brand_value}",
                    "equipment_model": f"ilike.{model_value}",
                    "limit": "1",
                },
                headers=supabase_headers(),
            )
            response.raise_for_status()
            rows = response.json()
            if rows:
                return rows[0]
    return None


def create_product(client: httpx.Client, brand: str, model: str) -> dict[str, Any]:
    payload = {
        "brand": brand,
        "equipment_model": model,
        "data_status": "ai_nameplate_pending_customer_confirmation",
        "data_confidence": 70,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    response = client.post(
        f"{SUPABASE_URL}/rest/v1/refrigerator_products",
        headers=supabase_headers("return=representation"),
        json=payload,
    )
    if response.status_code == 409:
        product = find_product(client, brand, model)
        if product:
            return product
    response.raise_for_status()
    rows = response.json()
    return rows[0]


def normalize_model(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def model_variants(value: str) -> list[str]:
    raw = (value or "").strip().upper()
    compact = normalize_model(raw)
    variants = {raw, compact}
    if compact:
        variants.add(compact.replace("9", "S"))
        variants.add(compact.replace("S", "9"))
    slash_suffix = re.match(r"^(\d{2,4})[/-]?([A-Z0-9])[/-]?(\d)$", raw)
    if slash_suffix:
        base, middle, suffix = slash_suffix.groups()
        middle_options = {middle}
        if middle == "9":
            middle_options.add("S")
        if middle == "S":
            middle_options.add("9")
        for option in middle_options:
            variants.add(f"{base}/{option}-{suffix}")
            variants.add(f"{base}/{option}/{suffix}")
            variants.add(f"{base}{option}{suffix}")
    return [variant for variant in variants if variant]


def get_product(client: httpx.Client, product_id: int) -> dict[str, Any] | None:
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/refrigerator_products",
        params={"select": "*", "id": f"eq.{product_id}", "limit": "1"},
        headers=supabase_headers(),
    )
    response.raise_for_status()
    rows = response.json()
    return rows[0] if rows else None


def get_gasket_count(client: httpx.Client, product_id: int) -> int:
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/refrigerator_product_gaskets",
        params={"select": "id", "refrigerator_product_id": f"eq.{product_id}", "limit": "1"},
        headers={**supabase_headers(), "Prefer": "count=exact"},
    )
    response.raise_for_status()
    content_range = response.headers.get("content-range") or "*/0"
    return int(content_range.rsplit("/", 1)[-1] or 0)


def upsert_known_product_from_nameplate(
    client: httpx.Client,
    brand: str,
    model: str,
    nameplate_data: dict[str, Any],
    status: str = "ai_nameplate_pending_customer_confirmation",
) -> dict[str, Any]:
    product = find_product(client, brand, model)
    if not product:
        product = create_product(client, brand, model)

    payload = {
        "brand": brand,
        "equipment_model": model,
        "manufacturer": nameplate_data.get("manufacturer") or product.get("manufacturer"),
        "manufacture_date_text": nameplate_data.get("manufacture_date") or product.get("manufacture_date_text"),
        "data_status": product.get("data_status") or status,
        "data_confidence": product.get("data_confidence") or nameplate_data.get("confidence") or 70,
        "data_source_summary": product.get("data_source_summary") or "AI nameplate read; customer confirmation pending.",
        "updated_at": now_iso(),
    }
    if nameplate_data.get("product_type"):
        payload["product_type"] = nameplate_data["product_type"]
    return patch_product(client, product["id"], payload) or product


def missing_tasks(client: httpx.Client, product: dict[str, Any]) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    if not is_displayable_image_url(client, product.get("product_image_url"), timeout=3.0):
        tasks.append({"task_type": "product_image", "field_name": "product_image_url", "priority": 10})
    if not product.get("product_type") or not product.get("door_positions") or not product.get("door_count"):
        tasks.append({"task_type": "product_structure", "field_name": "product_type_and_door_layout", "priority": 20})
    if get_gasket_count(client, product["id"]) == 0:
        tasks.append({"task_type": "gasket_records", "field_name": "refrigerator_product_gaskets", "priority": 30})
    return tasks


def enqueue_tasks(client: httpx.Client, product_id: int, tasks: list[dict[str, Any]]) -> None:
    if not tasks:
        return
    rows = []
    for task in tasks:
        rows.append(
            {
                "refrigerator_product_id": product_id,
                "task_type": task["task_type"],
                "field_name": task.get("field_name"),
                "status": "pending",
                "priority": task.get("priority", 100),
                "updated_at": now_iso(),
            }
        )
    response = client.post(
        f"{SUPABASE_URL}/rest/v1/{TASK_TABLE}",
        params={"on_conflict": "refrigerator_product_id,task_type,field_name"},
        headers=supabase_headers("resolution=merge-duplicates,return=minimal"),
        json=rows,
    )
    if response.status_code in {404, 406}:
        return
    response.raise_for_status()


def mark_task(client: httpx.Client, product_id: int, task_type: str, status: str, error: str | None = None) -> None:
    payload: dict[str, Any] = {"status": status, "updated_at": now_iso()}
    if status == "running":
        payload["started_at"] = now_iso()
    if status in {"completed", "failed", "retry_later"}:
        payload["completed_at"] = now_iso()
    if error:
        payload["last_error"] = error[:1000]
    response = client.patch(
        f"{SUPABASE_URL}/rest/v1/{TASK_TABLE}",
        params={"refrigerator_product_id": f"eq.{product_id}", "task_type": f"eq.{task_type}"},
        headers=supabase_headers("return=minimal"),
        json=payload,
    )
    if response.status_code in {404, 406}:
        return
    response.raise_for_status()


def extract_json_object(value: str) -> dict[str, Any]:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", value or "", re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def promote_image_from_openai(
    client: httpx.Client,
    product: dict[str, Any],
    nameplate_data: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None
    brand = compact(product.get("brand"))
    model = compact(product.get("equipment_model"))
    preferred_sources = trusted_source_prompt()
    if not brand or not model:
        return None

    prompt = f"""
Find one usable main product photo URL for this refrigerator.

Brand: {brand}
Model: {model}
Nameplate JSON: {json.dumps(nameplate_data or {}, ensure_ascii=False)}

Preferred source list. Search these first before wider web:
{preferred_sources}

Rules:
- Return only a real refrigerator product photo, not a logo, icon, gasket, part photo, manual cover, diagram, or placeholder.
- Prefer manufacturer, retailer, appliance parts, or archived product pages.
- The product must match the exact confirmed model or a clearly compatible same-family model.
- If no reliable photo exists, return null image_url and explain why.
- Return JSON only.
"""
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "image_url": {"type": ["string", "null"]},
            "source_url": {"type": ["string", "null"]},
            "title": {"type": ["string", "null"]},
            "confidence_score": {"type": ["number", "null"]},
            "evidence_summary": {"type": ["string", "null"]},
        },
        "required": ["image_url", "source_url", "title", "confidence_score", "evidence_summary"],
    }
    errors = []
    for model_name, tool_type in [
        (os.getenv("OPENAI_PRODUCT_RESEARCH_MODEL", "gpt-4.1"), "web_search"),
        (os.getenv("OPENAI_PRODUCT_RESEARCH_MODEL", "gpt-4.1"), "web_search_preview"),
        ("gpt-4.1-mini", "web_search_preview"),
    ]:
        response = httpx.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model_name,
                "tools": [{"type": tool_type}],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "product_image_result",
                        "schema": schema,
                        "strict": False,
                    }
                },
                "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
            },
            timeout=90,
        )
        if response.status_code >= 400:
            errors.append(f"{model_name}/{tool_type}: {response.status_code} {response.text[:200]}")
            continue
        data = response.json()
        output_text = data.get("output_text")
        if not output_text:
            texts = []
            for item in data.get("output", []):
                for content in item.get("content", []):
                    if content.get("text"):
                        texts.append(content["text"])
            output_text = "\n".join(texts)
        result = extract_json_object(output_text or "{}")
        image_url = compact(result.get("image_url"))
        source_url = compact(result.get("source_url"))
        title = compact(result.get("title"))
        if not image_url and source_url.startswith(("http://", "https://")):
            try:
                from product_image_search_crawler import extract_page_images, image_quality_score, score_candidate

                page_images = extract_page_images(client, {"page_url": source_url, "title": title})
                scored = []
                for candidate in page_images:
                    candidate["match_score"] = score_candidate(product, candidate)
                    haystack = re.sub(
                        r"[^A-Z0-9]",
                        "",
                        " ".join(
                            compact(candidate.get(key))
                            for key in ["image_url", "page_url", "title", "source_name"]
                        ).upper(),
                    )
                    if any(token in haystack for token in ["LOGO", "ICON", "GASKET", "SEAL", "PART", "DIAGRAM", "MANUAL"]):
                        continue
                    if candidate["match_score"] >= 45:
                        scored.append(candidate)
                if scored:
                    best = max(scored, key=image_quality_score)
                    image_url = compact(best.get("image_url"))
                    source_url = compact(best.get("page_url")) or source_url
                    title = compact(best.get("title")) or title
            except Exception as exc:
                print(f"OpenAI image page extraction skipped for {brand} {model}: {exc}", flush=True)
        if not image_url.startswith(("http://", "https://")):
            return None
        haystack = re.sub(r"[^A-Z0-9]", "", " ".join([
            image_url,
            source_url,
            title,
        ]).upper())
        if any(token in haystack for token in ["LOGO", "ICON", "GASKET", "SEAL", "PART", "DIAGRAM", "MANUAL"]):
            return None
        if not is_displayable_image_url(client, image_url):
            return None
        return {
            "product_image_url": image_url,
            "product_image_source_url": source_url or result.get("source_url"),
            "product_image_confidence": max(60, min(100, float(result.get("confidence_score") or 70))),
            "data_source_summary": product.get("data_source_summary") or result.get("evidence_summary"),
            "updated_at": now_iso(),
        }
    if errors:
        raise RuntimeError("OpenAI image search failed: " + " | ".join(errors))
    return None


def run_ai_structure_task(product_id: int, nameplate_data: dict[str, Any]) -> None:
    with httpx.Client(timeout=45) as client:
        product = get_product(client, product_id)
        if not product:
            return
        try:
            mark_task(client, product_id, "product_structure", "running")
            mark_task(client, product_id, "gasket_records", "running")
            enrich_confirmed_product(client, product, nameplate_data, force=True)
            mark_task(client, product_id, "product_structure", "completed")
            mark_task(client, product_id, "gasket_records", "completed")
        except Exception as exc:
            mark_task(client, product_id, "product_structure", "retry_later", str(exc))
            mark_task(client, product_id, "gasket_records", "retry_later", str(exc))
            print(f"instant AI enrichment failed for product {product_id}: {exc}", flush=True)


def run_image_task(product_id: int, nameplate_data: dict[str, Any] | None = None) -> None:
    with httpx.Client(timeout=120) as client:
        product = get_product(client, product_id)
        if not product:
            return
        if is_displayable_image_url(client, product.get("product_image_url"), timeout=3.0):
            return
        try:
            mark_task(client, product_id, "product_image", "running")
            if product.get("product_image_url"):
                patch_product(
                    client,
                    product_id,
                    {
                        "product_image_url": None,
                        "product_image_source_url": None,
                        "product_image_confidence": None,
                        "updated_at": now_iso(),
                    },
                )
                product = get_product(client, product_id) or product
            ok = quick_promote_product_image(client, product, limit=6)
            if not ok:
                ai_payload = promote_image_from_openai(client, product, nameplate_data)
                if ai_payload:
                    response = client.patch(
                        f"{SUPABASE_URL}/rest/v1/refrigerator_products",
                        params={"id": f"eq.{product_id}"},
                        headers=supabase_headers("return=minimal"),
                        json={key: value for key, value in ai_payload.items() if value not in (None, "")},
                    )
                    response.raise_for_status()
                    ok = True
            try:
                client.delete(
                    f"{SUPABASE_URL}/rest/v1/product_image_candidates",
                    params={"refrigerator_product_id": f"eq.{product_id}"},
                    headers=supabase_headers("return=minimal"),
                )
            except Exception as exc:
                print(f"candidate cleanup skipped for product {product_id}: {exc}", flush=True)
            mark_task(
                client,
                product_id,
                "product_image",
                "completed" if ok else "retry_later",
                None if ok else "No reliable product image found by CSE/Bing/OpenAI page extraction.",
            )
        except Exception as exc:
            mark_task(client, product_id, "product_image", "retry_later", str(exc))
            print(f"instant image enrichment failed for product {product_id}: {exc}", flush=True)


def start_instant_enrichment(product_id: int, nameplate_data: dict[str, Any] | None = None) -> None:
    if product_id in RUNNING:
        return
    RUNNING.add(product_id)

    def supervisor() -> None:
        try:
            with httpx.Client(timeout=20) as client:
                product = get_product(client, product_id)
                if not product:
                    return
                tasks = missing_tasks(client, product)
                enqueue_tasks(client, product_id, tasks)

            threads: list[threading.Thread] = []
            if any(task["task_type"] == "product_image" for task in tasks):
                threads.append(threading.Thread(target=run_image_task, args=(product_id, nameplate_data or {}), daemon=True))
            if any(task["task_type"] in {"product_structure", "gasket_records"} for task in tasks):
                threads.append(threading.Thread(target=run_ai_structure_task, args=(product_id, nameplate_data or {}), daemon=True))
            for thread in threads:
                thread.start()
        finally:
            RUNNING.discard(product_id)

    threading.Thread(target=supervisor, daemon=True).start()


def wait_for_core_result(product_id: int, max_seconds: float = 10.0) -> dict[str, Any]:
    """Wait briefly for product structure or gasket records without blocking on images."""
    deadline = time.time() + max_seconds
    latest: dict[str, Any] = {"product": None, "gasket_count": 0}
    while True:
        with httpx.Client(timeout=15) as client:
            product = get_product(client, product_id)
            gasket_count = get_gasket_count(client, product_id) if product else 0
        latest = {"product": product, "gasket_count": gasket_count}
        if product and (gasket_count > 0 or product.get("door_positions") or product.get("door_count")):
            return latest
        if time.time() >= deadline:
            return latest
        time.sleep(1)
