"""Fast product-image promotion for customer-facing page loads."""

import json
from html import unescape

from bs4 import BeautifulSoup

from product_image_search_crawler import (
    CANDIDATE_TABLE,
    MIN_PROMOTE_SCORE,
    SUPABASE_URL,
    USER_AGENT,
    get_existing_candidates,
    promote_best_image,
    score_candidate,
    search_google_cse,
    supabase_headers,
)


def save_fast_candidate(client, product: dict, candidate: dict) -> dict:
    score = score_candidate(product, candidate)
    row = {
        "refrigerator_product_id": product["id"],
        "image_url": candidate["image_url"],
        "page_url": candidate.get("page_url"),
        "source_name": candidate.get("source_name"),
        "image_title": candidate.get("title"),
        "image_width": candidate.get("image_width"),
        "image_height": candidate.get("image_height"),
        "match_score": score,
        "evidence": {
            "brand": product["brand"],
            "model": product["equipment_model"],
            "title": candidate.get("title"),
            "fast_path": True,
        },
    }
    response = client.post(
        f"{SUPABASE_URL}/rest/v1/{CANDIDATE_TABLE}?on_conflict=refrigerator_product_id,image_url",
        headers=supabase_headers("resolution=merge-duplicates,return=representation"),
        json=row,
    )
    response.raise_for_status()
    saved = response.json()
    return saved[0] if saved else row


def search_bing_images_strict(client, product: dict, limit: int = 8) -> list[dict]:
    brand = product["brand"]
    model = product["equipment_model"]
    queries = [
        f"{brand} {model}",
        f"{model} refrigerator",
        f"{brand} {model} refrigerator product",
    ]
    rows = []
    seen = set()
    for query in queries:
        response = client.get(
            "https://www.bing.com/images/search",
            params={"q": query, "form": "HDRSC2"},
            headers={"User-Agent": USER_AGENT},
            timeout=12,
        )
        if response.status_code >= 400:
            continue
        soup = BeautifulSoup(response.text, "html.parser")
        for item in soup.select("a.iusc"):
            metadata = item.get("m")
            if not metadata:
                continue
            try:
                parsed = json.loads(unescape(metadata))
            except json.JSONDecodeError:
                continue
            image_url = parsed.get("murl") or ""
            if not image_url or image_url in seen:
                continue
            candidate = {
                "image_url": image_url,
                "page_url": parsed.get("purl") or "",
                "source_name": "Bing Images Strict Search",
                "title": parsed.get("t") or f"{brand} {model}",
                "image_width": parsed.get("ow"),
                "image_height": parsed.get("oh"),
            }
            if score_candidate(product, candidate) < MIN_PROMOTE_SCORE:
                continue
            seen.add(image_url)
            rows.append(candidate)
            if len(rows) >= limit:
                return rows
    return rows


def quick_promote_product_image(client, product: dict, limit: int = 6) -> bool:
    saved = get_existing_candidates(client, product["id"], limit=limit)
    if promote_best_image(client, product, saved):
        return True

    raw_candidates = search_google_cse(client, product)[:limit]
    if not raw_candidates:
        raw_candidates = search_bing_images_strict(client, product, limit=limit)
    if not raw_candidates:
        return False

    saved = [save_fast_candidate(client, product, row) for row in raw_candidates]
    return promote_best_image(client, product, saved)
