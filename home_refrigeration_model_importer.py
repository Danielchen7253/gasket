"""Import home refrigerator/freezer models from broad appliance parts catalogs.

Broad appliance brands have many non-refrigeration models in parts catalogs.
This importer only accepts models that match refrigerator/freezer prefix rules
for each brand, then writes brand + model rows into refrigerator_products.

It does not enrich product images, gasket records, prices, or call AI.
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

PARTSTOWN_HOME_BRANDS = {
    "bosch": "Bosch",
    "frigidaire": "Frigidaire",
    "ge-appliance": "GE",
    "haier": "Haier",
    "hisense": "Hisense",
    "kenmore": "Kenmore",
    "kitchenaid": "KitchenAid",
    "lg-appliances": "LG",
    "maytag": "Maytag",
    "samsung": "Samsung",
    "whirlpool": "Whirlpool",
}

# Conservative refrigerator/freezer model prefix rules. These reduce pollution
# from laundry, dishwasher, range, microwave, and HVAC models in broad catalogs.
BRAND_REFRIGERATION_PATTERNS = {
    "Bosch": re.compile(r"^(B10|B11|B18|B20|B21|B22|B24|B26|B30|KAN|KAD|KIF|KIR)", re.I),
    "Frigidaire": re.compile(
        r"^(FFTR|FFHT|FFHS|FFSS|FFHB|FFHN|FFHI|FFET|FFBN|FFFC|FFFU|FFU|"
        r"FGTR|FGHT|FGHS|FGHC|FGHB|FGHN|FGHF|FGUS|FPRU|FPBC|FPBG|"
        r"FRS|FRT|FRSS|FRFS|FRFN|FRFG|FRQG|LFTR|LFHT|LFSS|LGHB|LGHS|"
        r"GLRS|GLHS|PLHS|EI23|EI26|E23|E32|CF|MFU|MFC|LFFH)",
        re.I,
    ),
    "GE": re.compile(
        r"^(GFE|GFD|GNE|GYE|GDE|GDS|GIE|GSE|GSH|GSL|GSS|GTE|GTH|GTR|GTS|"
        r"PFE|PFD|PFS|PSE|PSH|PSC|PSS|PYE|PFSS|CFCP|CFE|CYE|CZS|"
        r"ZIC|ZIF|ZIR|ZIS|ZISS|ZFS|ZIC|TBX|TFX|TPX|CTX|GCE|GCG|GCGS)",
        re.I,
    ),
    "Haier": re.compile(r"^(HA|HB|HC|HF|HFD|HFW|HNSE|HRC|HRF|HRQ|HRT|HSE|HT|QHE|QJS|QSS|QNE|QRS)", re.I),
    "Hisense": re.compile(r"^(HR|HF|HBM|HRT|HRF|HRS|RB|RF|RR|RS|RT|BCD)", re.I),
    "Kenmore": re.compile(r"^(106|111|253|363|464|596|628|720|795)\d", re.I),
    "KitchenAid": re.compile(
        r"^(KBFA|KBFC|KBFO|KBFS|KBFN|KBR|KBSN|KSSC|KSSS|KSBS|KSCS|KSR|"
        r"KTR|KUIS|KUID|KRM|KRFF|KRF|KRS|KRSC|KRMF|KURL|KURR|KUBL|KUBR)",
        re.I,
    ),
    "LG": re.compile(
        r"^(GR|GM|LBN|LDC|LFC|LFD|LFX|LFXS|LFCS|LMX|LMXC|LMXS|LMC|LRB|"
        r"LRC|LRD|LRF|LRM|LRS|LRSC|LSC|LSFD|LSFX|LSMX|LSRS|LSXS|LTCS|LTC|LRT)",
        re.I,
    ),
    "Maytag": re.compile(r"^(AFD|ARB|BRF|GB|GC|GZ|MBF|MFI|MFF|MFT|MFW|MQU|MQF|MSB|MSD|MZD|M1B|M1TX|M8RX)", re.I),
    "Samsung": re.compile(r"^(RF|RFG|RFH|RFS|RH|RB|RS|RSG|RT|SR|BRF|BESPOKE)", re.I),
    "Whirlpool": re.compile(
        r"^(WRF|WRR|WRS|WRT|WRX|WRB|WZF|WZC|WSF|WSR|WUR|WUI|WUB|"
        r"GI|GC|GD|GS|GR|ED|ET|ER|EL|EB|EV|EH|3E|4E|5E|7E|8E|"
        r"ARC|ART|ARB|ETR|ETT|EDR|EDT)",
        re.I,
    ),
}

MODEL_LOC_RE = re.compile(r"<loc>\s*https://www\.partstown\.com/m/([^/<]+)/([^<]+)</loc>", re.I)
SITEMAP_LOC_RE = re.compile(r"<loc>\s*([^<]+)\s*</loc>", re.I)
BAD_MODEL_TOKENS = {
    "ACCESSORIES",
    "CATALOG",
    "CONTACT",
    "DISHWASHER",
    "DRYER",
    "GASKET",
    "LAUNDRY",
    "MANUAL",
    "MICROWAVE",
    "OVEN",
    "PARTS",
    "RANGE",
    "SERVICE",
    "WASHER",
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
    return value.strip(" .,/|:;()[]{}").upper()


def valid_model(brand: str, model: str) -> bool:
    norm = normalize_model(model)
    if len(norm) < 4 or len(norm) > 34:
        return False
    if not any(ch.isdigit() for ch in norm):
        return False
    if model.upper() in BAD_MODEL_TOKENS or any(token in norm for token in BAD_MODEL_TOKENS):
        return False
    if any(token in norm for token in ("HTTP", "WWW", "COM", "CATALOG", "MANUAL", "PARTS")):
        return False
    pattern = BRAND_REFRIGERATION_PATTERNS.get(brand)
    return bool(pattern and pattern.match(model))


def product_type_for_model(brand: str, model: str) -> str:
    norm = normalize_model(model)
    if brand in {"Frigidaire", "Whirlpool"} and ("FFFU" in norm or "WZF" in norm or "WZC" in norm):
        return "freezer"
    if brand == "Bosch" and ("B18IF" in norm or "B18IF9" in norm):
        return "freezer"
    return "residential refrigerator/freezer"


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


def parse_partstown_sitemap(content: bytes) -> Iterable[dict[str, object]]:
    text = gzip.decompress(content).decode("utf-8", "replace")
    seen: set[tuple[str, str]] = set()
    for slug, raw_model in MODEL_LOC_RE.findall(text):
        brand = PARTSTOWN_HOME_BRANDS.get(slug.lower())
        if not brand:
            continue
        model = clean_model(raw_model)
        if not valid_model(brand, model):
            continue
        key = (brand.lower(), normalize_model(model))
        if key in seen:
            continue
        seen.add(key)
        yield {
            "brand": brand,
            "equipment_model": model,
            "manufacturer": brand,
            "product_type": product_type_for_model(brand, model),
            "lifecycle_status": "unknown",
            "data_status": "home_parts_catalog",
            "data_confidence": 58,
            "last_discovered_at": now_iso(),
            "data_source_summary": f"Imported from Partstown appliance model catalog with refrigerator/freezer prefix filter: https://www.partstown.com/m/{slug}/{raw_model}",
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
    limit = int(os.getenv("HOME_PARTS_IMPORT_LIMIT", "0"))
    batch_size = int(os.getenv("HOME_PARTS_IMPORT_BATCH_SIZE", "500"))
    sleep_seconds = float(os.getenv("HOME_PARTS_IMPORT_SLEEP_SECONDS", "0.05"))
    parsed = inserted = skipped_duplicates = scanned = 0
    batch: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()

    with httpx.Client(timeout=60, follow_redirects=True) as client:
        sitemap_urls = fetch_partstown_model_sitemaps(client)
        print(f"loaded {len(sitemap_urls)} Partstown model sitemaps")
        print(f"home appliance brand filters: {len(PARTSTOWN_HOME_BRANDS)}")
        for sitemap_url in sitemap_urls:
            print(f"scan {sitemap_url}")
            response = client.get(sitemap_url, headers={"User-Agent": USER_AGENT})
            response.raise_for_status()
            scanned += 1
            for row in parse_partstown_sitemap(response.content):
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
        f"sitemaps_scanned={scanned}, parsed_home_refrigeration_models={parsed}, "
        f"inserted_new_products={inserted}, skipped_duplicates={skipped_duplicates}"
    )


if __name__ == "__main__":
    main()
