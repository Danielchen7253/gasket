"""Import refrigerator/freezer model numbers from parts-site model catalogs.

This importer is deliberately narrow: it only writes brand + model rows into
refrigerator_products. It does not enrich images, gasket specs, or call AI.
"""

from __future__ import annotations

from datetime import datetime, timezone
import gzip
import os
from pathlib import Path
import re
import time
from typing import Iterable
from urllib.parse import unquote

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
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36"

# Strong refrigeration / ice / cold-side equipment brands in Partstown model sitemaps.
PARTSTOWN_REFRIGERATION_BRANDS = {
    "aht-cooling-systems": "AHT Cooling Systems",
    "arctic-air": "Arctic Air",
    "atosa": "Atosa",
    "avantco": "Avantco",
    "beverage-air": "Beverage-Air",
    "continental-refrigeration": "Continental",
    "delfield": "Delfield",
    "everest": "Everest",
    "glastender": "Glastender",
    "hoshizaki": "Hoshizaki",
    "hussmann": "Hussmann",
    "ice-o-matic": "Ice-O-Matic",
    "icetro": "Icetro",
    "kelvinator": "Kelvinator",
    "manitowoc-ice": "Manitowoc Ice",
    "master-bilt": "Master-Bilt",
    "norlake": "Nor-Lake",
    "perlick": "Perlick",
    "perlick-residential": "Perlick",
    "randell": "Randell",
    "scotsman": "Scotsman",
    "traulsen": "Traulsen",
    "true": "True",
    "turbo-air": "Turbo Air",
    "victory": "Victory",
}

# Broad household appliance brands. These are optional because their parts-site
# model catalogs often contain non-refrigeration equipment too.
PARTSTOWN_BROAD_APPLIANCE_BRANDS = {
    "frigidaire": "Frigidaire",
    "ge-appliance": "GE",
    "kitchenaid": "KitchenAid",
    "lg-appliances": "LG",
    "maytag": "Maytag",
    "samsung": "Samsung",
    "whirlpool": "Whirlpool",
}

MODEL_LOC_RE = re.compile(r"<loc>\s*https://www\.partstown\.com/m/([^/<]+)/([^<]+)</loc>", re.I)
SITEMAP_LOC_RE = re.compile(r"<loc>\s*([^<]+)\s*</loc>", re.I)
BAD_MODEL_TOKENS = {
    "ABOUT",
    "ACCESSORIES",
    "CATALOG",
    "CONTACT",
    "GASKET",
    "MANUAL",
    "MODEL",
    "PARTS",
    "PRODUCT",
    "REFRIGERATOR",
    "SEARCH",
    "SERVICE",
    "SITEMAP",
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


def normalize_model(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def clean_model(raw: str) -> str:
    value = unquote(raw).split("?")[0].strip()
    return value.strip(" .,/|:;()[]{}")


def valid_model(model: str) -> bool:
    norm = normalize_model(model)
    if len(norm) < 3 or len(norm) > 32:
        return False
    if not any(ch.isdigit() for ch in norm):
        return False
    if norm in BAD_MODEL_TOKENS or model.upper() in BAD_MODEL_TOKENS:
        return False
    if re.fullmatch(r"\d{5,}", norm):
        return False
    if any(token in norm for token in ("HTTP", "WWW", "COM", "PARTS", "MANUAL", "CATALOG")):
        return False
    return True


def partstown_brand_map() -> dict[str, str]:
    brands = dict(PARTSTOWN_REFRIGERATION_BRANDS)
    if os.getenv("PARTS_IMPORT_INCLUDE_BROAD_APPLIANCE", "0") == "1":
        brands.update(PARTSTOWN_BROAD_APPLIANCE_BRANDS)
    return brands


def fetch_partstown_model_sitemaps(client: httpx.Client) -> list[str]:
    response = client.get(
        "https://www.partstown.com/sitemap.xml",
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )
    response.raise_for_status()
    urls = []
    for loc in SITEMAP_LOC_RE.findall(response.text):
        if "/sitemaps/models-com-" in loc and "hvacmodels" not in loc:
            urls.append(loc)
    return sorted(set(urls), key=lambda url: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", url)])


def parse_partstown_sitemap(content: bytes, brand_map: dict[str, str]) -> Iterable[dict[str, str]]:
    text = gzip.decompress(content).decode("utf-8", "replace")
    seen: set[tuple[str, str]] = set()
    for slug, raw_model in MODEL_LOC_RE.findall(text):
        brand = brand_map.get(slug.lower())
        if not brand:
            continue
        model = clean_model(raw_model)
        if not valid_model(model):
            continue
        key = (brand.lower(), normalize_model(model))
        if key in seen:
            continue
        seen.add(key)
        yield {
            "brand": brand,
            "equipment_model": model.upper(),
            "manufacturer": brand,
            "product_type": "refrigeration equipment",
            "lifecycle_status": "unknown",
            "data_status": "parts_catalog",
            "data_confidence": 55,
            "last_discovered_at": now_iso(),
            "data_source_summary": f"Imported from Partstown model catalog: https://www.partstown.com/m/{slug}/{raw_model}",
        }


def insert_products(client: httpx.Client, rows: list[dict[str, object]]) -> int:
    if not rows:
        return 0
    response = client.post(
        f"{SUPABASE_URL}/rest/v1/{PRODUCT_TABLE}",
        params={"on_conflict": "brand,equipment_model"},
        headers=headers("resolution=ignore-duplicates,return=representation"),
        json=rows,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"insert failed {response.status_code}: {response.text[:500]}")
    return len(response.json())


def main() -> None:
    limit = int(os.getenv("PARTS_IMPORT_LIMIT", "0"))
    batch_size = int(os.getenv("PARTS_IMPORT_BATCH_SIZE", "500"))
    sleep_seconds = float(os.getenv("PARTS_IMPORT_SLEEP_SECONDS", "0.1"))
    brand_map = partstown_brand_map()
    scanned = 0
    parsed = 0
    inserted = 0
    skipped_duplicates = 0
    batch: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()

    with httpx.Client(timeout=60, follow_redirects=True) as client:
        sitemap_urls = fetch_partstown_model_sitemaps(client)
        print(f"loaded {len(sitemap_urls)} Partstown model sitemaps")
        print(f"brand filters: {len(brand_map)}")
        for sitemap_url in sitemap_urls:
            print(f"scan {sitemap_url}")
            response = client.get(sitemap_url, headers={"User-Agent": USER_AGENT})
            response.raise_for_status()
            scanned += 1
            for row in parse_partstown_sitemap(response.content, brand_map):
                key = (str(row["brand"]).lower(), normalize_model(str(row["equipment_model"])))
                if key in seen:
                    skipped_duplicates += 1
                    continue
                seen.add(key)
                parsed += 1
                batch.append(row)
                if len(batch) >= batch_size:
                    inserted += insert_products(client, batch)
                    batch.clear()
                    print(f"  parsed={parsed} inserted={inserted}")
                if limit and parsed >= limit:
                    break
            if limit and parsed >= limit:
                break
            if sleep_seconds:
                time.sleep(sleep_seconds)
        if batch:
            inserted += insert_products(client, batch)

    print(
        "done: "
        f"sitemaps_scanned={scanned}, parsed_models={parsed}, "
        f"inserted_new_products={inserted}, skipped_duplicates={skipped_duplicates}"
    )


if __name__ == "__main__":
    main()
