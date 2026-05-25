"""Fast product-image promotion for customer-facing page loads."""

from product_image_search_crawler import (
    CANDIDATE_TABLE,
    SUPABASE_URL,
    get_existing_candidates,
    promote_best_image,
    score_candidate,
    search_bing_images,
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


def quick_promote_product_image(client, product: dict, limit: int = 6) -> bool:
    saved = get_existing_candidates(client, product["id"], limit=limit)
    if promote_best_image(client, product, saved):
        return True

    raw_candidates = search_bing_images(client, product)[:limit]
    if not raw_candidates:
        raw_candidates = search_google_cse(client, product)[:limit]
    if not raw_candidates:
        return False

    saved = [save_fast_candidate(client, product, row) for row in raw_candidates]
    return promote_best_image(client, product, saved)
