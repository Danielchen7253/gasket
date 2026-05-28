"""Fast product-image promotion for customer-facing page loads."""

import json
from difflib import SequenceMatcher
from html import unescape

from bs4 import BeautifulSoup

from product_image_search_crawler import (
    CANDIDATE_TABLE,
    MIN_PROMOTE_SCORE,
    SUPABASE_URL,
    USER_AGENT,
    get_existing_candidates,
    is_displayable_image_url,
    normalized,
    promote_best_image,
    score_candidate,
    search_bing_api_images,
    search_brave_images,
    search_google_cse,
    search_serpapi,
    search_direct_product_pages,
    search_public_web_images,
    supabase_headers,
)


def model_similarity(left: str | None, right: str | None) -> float:
    left_norm = normalized(left or "")
    right_norm = normalized(right or "")
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    prefix = 0
    for a, b in zip(left_norm, right_norm):
        if a != b:
            break
        prefix += 1
    ratio = SequenceMatcher(None, left_norm, right_norm).ratio()
    prefix_bonus = min(prefix / max(len(left_norm), len(right_norm)), 1.0) * 0.18
    return min(1.0, ratio + prefix_bonus)


def save_fast_candidate(client, product: dict, candidate: dict, score_override: float | None = None) -> dict:
    score = score_override if score_override is not None else score_candidate(product, candidate)
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
            "representative_image": candidate.get("representative_image") is True,
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


def promote_sibling_model_image(client, product: dict) -> bool:
    brand = product.get("brand") or ""
    model = product.get("equipment_model") or ""
    if not brand or not model:
        return False
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/refrigerator_products",
        headers=supabase_headers(),
        params={
            "select": "id,brand,equipment_model,product_type,door_count,door_layout,product_image_url,product_image_source_url,product_image_confidence",
            "brand": f"ilike.{brand}",
            "product_image_url": "not.is.null",
            "limit": "80",
            "order": "updated_at.desc",
        },
    )
    response.raise_for_status()
    best = None
    best_score = 0.0
    for row in response.json():
        if row.get("id") == product.get("id"):
            continue
        image_url = row.get("product_image_url")
        if not image_url:
            continue
        similarity = model_similarity(model, row.get("equipment_model"))
        if similarity < 0.86:
            continue
        same_style = bool(row.get("door_layout") and product.get("door_layout") and row.get("door_layout") == product.get("door_layout"))
        same_doors = bool(row.get("door_count") and product.get("door_count") and row.get("door_count") == product.get("door_count"))
        score = similarity * 100 + (4 if same_style else 0) + (2 if same_doors else 0)
        if score > best_score:
            best = row
            best_score = score
    if not best:
        return False
    image_url = best.get("product_image_url")
    if not is_displayable_image_url(client, image_url, timeout=3.0):
        return False
    candidate = {
        "image_url": image_url,
        "page_url": best.get("product_image_source_url") or "",
        "source_name": "Same Brand Similar Model Image",
        "title": f"{best.get('brand')} {best.get('equipment_model')} representative product image",
        "representative_image": True,
    }
    saved = save_fast_candidate(client, product, candidate, score_override=max(MIN_PROMOTE_SCORE, min(92, best_score)))
    return promote_best_image(client, product, [saved])


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


def product_style_terms(product: dict) -> list[str]:
    text = normalized(
        " ".join(
            str(product.get(key) or "")
            for key in ["product_type", "door_layout", "door_positions", "data_source_summary"]
        )
    )
    terms = []
    if "SIDEBYSIDE" in text or product.get("door_count") == 2:
        terms.append("side-by-side")
    if "FRENCH" in text:
        terms.append("french door")
    if "BOTTOM" in text or "FREEZERDRAWER" in text:
        terms.append("bottom freezer")
    if "TOPFREEZER" in text:
        terms.append("top freezer")
    if "REACHIN" in text or "COMMERCIAL" in text:
        terms.append("reach-in")
    if not terms:
        terms.append("refrigerator")
    return terms


def matches_product_style(product: dict, candidate: dict) -> bool:
    haystack = normalized(
        " ".join(
            str(candidate.get(key) or "")
            for key in ["title", "image_url", "page_url", "source_name"]
        )
    )
    brand = normalized(product.get("brand") or "")
    if brand and brand not in haystack:
        return False
    terms = product_style_terms(product)
    if "side-by-side" in terms:
        return "SIDEBYSIDE" in haystack or "SIDE" in haystack
    if "french door" in terms:
        return "FRENCHDOOR" in haystack or "FRENCH" in haystack
    if "bottom freezer" in terms:
        return "BOTTOMFREEZER" in haystack or ("BOTTOM" in haystack and "FREEZER" in haystack)
    if "top freezer" in terms:
        return "TOPFREEZER" in haystack or ("TOP" in haystack and "FREEZER" in haystack)
    if "reach-in" in terms:
        return "REACHIN" in haystack or "COMMERCIAL" in haystack
    return "REFRIGERATOR" in haystack or "FREEZER" in haystack


def search_representative_bing_images(client, product: dict, limit: int = 8) -> list[dict]:
    brand = product["brand"]
    style = product_style_terms(product)[0]
    queries = [
        f"{brand} {style} refrigerator product image",
        f"{brand} 25 cu ft {style} refrigerator",
        f"{brand} {style} refrigerator Lowes product image",
    ]
    rows = []
    seen = set()
    for query in queries:
        response = client.get(
            "https://www.bing.com/images/search",
            params={"q": query, "form": "HDRSC2", "safeSearch": "Strict"},
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
                "source_name": "Bing Representative Product Image",
                "title": parsed.get("t") or f"{brand} {style} refrigerator",
                "image_width": parsed.get("ow"),
                "image_height": parsed.get("oh"),
                "representative_image": True,
            }
            if not matches_product_style(product, candidate):
                continue
            if not is_displayable_image_url(client, image_url, timeout=3.0):
                continue
            seen.add(image_url)
            rows.append(candidate)
            if len(rows) >= limit:
                return rows
    return rows


def quick_promote_product_image(client, product: dict, limit: int = 6) -> bool:
    if promote_sibling_model_image(client, product):
        return True

    saved = get_existing_candidates(client, product["id"], limit=limit)
    if promote_best_image(client, product, saved):
        return True

    def strong(rows):
        return sorted(
            [row for row in rows if score_candidate(product, row) >= MIN_PROMOTE_SCORE],
            key=lambda row: score_candidate(product, row),
            reverse=True,
        )

    raw_candidates = strong(search_serpapi(client, product))[:limit]
    if not raw_candidates:
        raw_candidates = strong(search_google_cse(client, product))[:limit]
    if not raw_candidates:
        raw_candidates = strong(search_brave_images(client, product))[:limit]
    if not raw_candidates:
        raw_candidates = strong(search_bing_api_images(client, product))[:limit]
    if not raw_candidates:
        raw_candidates = strong(search_direct_product_pages(client, product))[:limit]
    if not raw_candidates:
        raw_candidates = strong(search_bing_images_strict(client, product, limit=limit))[:limit]
    if not raw_candidates:
        raw_candidates = strong(search_public_web_images(client, product))[:limit]
    if not raw_candidates:
        representative = search_representative_bing_images(client, product, limit=limit)[:limit]
        if not representative:
            return False
        saved = []
        for row in representative:
            score = max(MIN_PROMOTE_SCORE, score_candidate(product, row), 72)
            saved.append(save_fast_candidate(client, product, row, score_override=score))
        return promote_best_image(client, product, saved)

    saved = [save_fast_candidate(client, product, row) for row in raw_candidates]
    return promote_best_image(client, product, saved)
