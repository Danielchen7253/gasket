import os
import re
from pathlib import Path

import httpx
from dotenv import load_dotenv


load_dotenv(Path(__file__).with_name(".env"))

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

DETAIL_TABLE = "gasket_details"
SPEC_TABLE = "product_gasket_specs"

BAD_PART_NUMBERS = {
    "REPLACE",
    "REPLACES",
    "NERS",
    "TOWN",
    "SPIN",
    "ORDER",
    "THAT",
    "ENSURES",
    "FOR",
    "RUIN",
    "SEARCH",
    "RESULT",
    "RESULTS",
    "MODEL",
    "NUMBER",
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


def canonical_part_number(value: str | None) -> str | None:
    if not value:
        return None
    part = re.sub(r"\s+", "", value.strip().upper()).strip(".,;:")
    if len(part) < 3 or len(part) > 40:
        return None
    if not re.search(r"\d", part):
        return None
    if part in BAD_PART_NUMBERS:
        return None
    if re.fullmatch(r"(?:COM|NET|JPG|PNG|HTTP|HTTPS|BUY|NOW|MODEL|NUMBER)", part):
        return None
    return part


def normalize_dimension_pair(width: float | None, height: float | None) -> tuple[float | None, float | None]:
    if not width or not height:
        return width, height
    if width > 120 or height > 120:
        return round(width / 25.4, 3), round(height / 25.4, 3)
    return width, height


def sanitized_detail(detail: dict) -> dict:
    cleaned = dict(detail)
    part_number = canonical_part_number(cleaned.get("gasket_part_number"))
    universal_part_number = canonical_part_number(cleaned.get("universal_part_number")) or part_number
    cleaned["gasket_part_number"] = part_number
    cleaned["universal_part_number"] = universal_part_number
    cleaned["width_in"], cleaned["height_in"] = normalize_dimension_pair(
        cleaned.get("width_in"),
        cleaned.get("height_in"),
    )
    return cleaned


def is_valid_gasket_detail(detail: dict) -> bool:
    name = (detail.get("gasket_name") or "").lower()
    if any(token in name for token in ["cutting board", "hinge", "caster", "shelf"]):
        return False
    return bool(
        detail.get("dimensions_text")
        or detail.get("width_in")
        or detail.get("height_in")
        or detail.get("gasket_part_number")
        or "gasket" in name
    )


def dimension_key(detail: dict) -> str | None:
    width = detail.get("width_in")
    height = detail.get("height_in")
    if not width or not height:
        return None
    ordered = sorted([round(float(width), 2), round(float(height), 2)])
    return f"{ordered[0]}x{ordered[1]}"


def rank_details(details: list[dict]) -> list[dict]:
    dim_sources: dict[str, set[str]] = {}
    part_sources: dict[str, set[str]] = {}
    for detail in details:
        source = detail.get("source_name") or detail.get("source_url") or "unknown"
        dim = dimension_key(detail)
        part = canonical_part_number(detail.get("gasket_part_number"))
        if dim:
            dim_sources.setdefault(dim, set()).add(source)
        if part:
            part_sources.setdefault(part, set()).add(source)

    ranked = []
    for detail in details:
        adjusted = float(detail.get("confidence_score") or 0)
        evidence_count = 1
        dim = dimension_key(detail)
        part = canonical_part_number(detail.get("gasket_part_number"))
        if dim:
            evidence_count = max(evidence_count, len(dim_sources.get(dim, set())))
            adjusted += min(18, (len(dim_sources.get(dim, set())) - 1) * 9)
        if part:
            evidence_count = max(evidence_count, len(part_sources.get(part, set())))
            adjusted += min(12, (len(part_sources.get(part, set())) - 1) * 6)
        if detail.get("width_in") and detail.get("height_in"):
            adjusted += 5
        if detail.get("profile_image_url"):
            adjusted += 5

        row = dict(detail)
        row["rank_score"] = round(min(100, adjusted), 2)
        row["evidence_count"] = evidence_count
        ranked.append(row)

    return sorted(
        ranked,
        key=lambda row: (float(row.get("rank_score") or 0), float(row.get("confidence_score") or 0)),
        reverse=True,
    )


def get_details_for_product(client: httpx.Client, product_id: int) -> list[dict]:
    endpoint = (
        f"{SUPABASE_URL}/rest/v1/{DETAIL_TABLE}"
        f"?select=*&refrigerator_product_id=eq.{product_id}"
        "&order=confidence_score.desc"
    )
    response = client.get(endpoint, headers=supabase_headers())
    response.raise_for_status()
    details = []
    for row in response.json():
        cleaned = sanitized_detail(row)
        if is_valid_gasket_detail(cleaned):
            details.append(cleaned)
    return rank_details(details)


def refresh_product_gasket_spec(client: httpx.Client, product_id: int) -> None:
    details = get_details_for_product(client, product_id)
    if not details:
        row = {
            "refrigerator_product_id": product_id,
            "doors": [],
            "source_summary": [],
            "confidence_score": 0,
            "data_status": "missing",
            "is_verified": False,
        }
    else:
        best = details[0]
        sources = []
        seen_sources = set()
        doors = []
        for detail in details[:8]:
            source_key = (detail.get("source_name"), detail.get("source_url"))
            if source_key not in seen_sources:
                seen_sources.add(source_key)
                sources.append({"source_name": detail.get("source_name"), "source_url": detail.get("source_url")})
            doors.append(
                {
                    "door_position": detail.get("door_position"),
                    "width_in": detail.get("width_in"),
                    "height_in": detail.get("height_in"),
                    "dimensions_text": detail.get("dimensions_text"),
                    "market_price_usd": detail.get("market_price_usd"),
                    "part_number": detail.get("gasket_part_number"),
                    "universal_part_number": detail.get("universal_part_number"),
                    "gasket_part_id": detail.get("gasket_part_id"),
                    "gasket_name": detail.get("gasket_name"),
                    "gasket_profile": detail.get("gasket_profile"),
                    "gasket_image_url": detail.get("gasket_image_url"),
                    "profile_image_url": detail.get("profile_image_url"),
                    "source_url": detail.get("source_url"),
                    "source_name": detail.get("source_name"),
                    "confidence_score": detail.get("rank_score") or detail.get("confidence_score"),
                    "source_confidence_score": detail.get("confidence_score"),
                    "evidence_count": detail.get("evidence_count"),
                    "is_verified": detail.get("is_verified") or False,
                }
            )

        verified = any(detail.get("is_verified") for detail in details)
        row = {
            "refrigerator_product_id": product_id,
            "primary_gasket_part_id": best.get("gasket_part_id"),
            "primary_part_number": best.get("gasket_part_number"),
            "universal_part_number": best.get("universal_part_number"),
            "gasket_name": best.get("gasket_name"),
            "gasket_profile": best.get("gasket_profile"),
            "doors": doors,
            "source_summary": sources,
            "best_source_url": best.get("source_url"),
            "best_source_name": best.get("source_name"),
            "confidence_score": best.get("rank_score") or best.get("confidence_score") or 0,
            "data_status": "verified" if verified else "candidate",
            "is_verified": verified,
        }

    endpoint = f"{SUPABASE_URL}/rest/v1/{SPEC_TABLE}?on_conflict=refrigerator_product_id"
    response = client.post(
        endpoint,
        headers=supabase_headers("resolution=merge-duplicates,return=minimal"),
        json=row,
    )
    response.raise_for_status()
    refresh_product_quote_items(client, product_id)


def refresh_product_quote_items(client: httpx.Client, product_id: int) -> None:
    response = client.post(
        f"{SUPABASE_URL}/rest/v1/rpc/refresh_product_quote_items",
        headers=supabase_headers(),
        json={"p_product_id": product_id},
    )
    if response.status_code == 404:
        print("quote item refresh skipped: refresh_product_quote_items RPC not found")
        return
    response.raise_for_status()


def refresh_existing_specs(client: httpx.Client, limit: int) -> int:
    endpoint = (
        f"{SUPABASE_URL}/rest/v1/{SPEC_TABLE}"
        "?select=refrigerator_product_id"
        "&or=(data_status.eq.missing,confidence_score.lt.60)"
        "&order=confidence_score.asc.nullsfirst"
        f"&limit={limit}"
    )
    response = client.get(endpoint, headers=supabase_headers())
    response.raise_for_status()
    refreshed = 0
    for row in response.json():
        refresh_product_gasket_spec(client, row["refrigerator_product_id"])
        refreshed += 1
    return refreshed


def main() -> None:
    limit = int(os.getenv("GASKET_SPEC_REFRESH_LIMIT", os.getenv("ENRICH_LIMIT", "100")))
    with httpx.Client(timeout=60) as client:
        refreshed = refresh_existing_specs(client, limit)
    print(f"refreshed existing gasket specs: {refreshed}")


if __name__ == "__main__":
    main()
