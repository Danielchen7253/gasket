import csv
import html
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


BASE_URL = "https://shop.gasketguy.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

OUT_COLUMNS = [
    "source_name",
    "source_url",
    "source_product_id",
    "raw_title",
    "part_name",
    "market_category",
    "equipment_category",
    "brand_raw",
    "canonical_brand",
    "commercial_fit_manufacturers",
    "gasket_profile_code",
    "oem_part_number",
    "normalized_part_number",
    "compatible_oem_part_numbers",
    "alternate_part_numbers",
    "compatible_brands",
    "compatible_models",
    "door_position_raw",
    "door_position_normalized",
    "width_in",
    "height_in",
    "dimensions_text",
    "color",
    "material",
    "profile_type",
    "mounting_method",
    "magnetic",
    "temperature_application",
    "price_usd",
    "stock_status",
    "image_url",
    "description",
    "evidence_summary",
    "confidence_score",
    "data_status",
    "scraped_at",
    "target_table_hint",
]


BRAND_ALIASES = {
    "SILVERKING": "Silver King",
    "TRUE": "True",
    "TRAULSEN": "Traulsen",
    "BEVERAGEAIR": "Beverage-Air",
    "DELFIELD": "Delfield",
    "HOSHIZAKI": "Hoshizaki",
    "TURBOAIR": "Turbo Air",
    "MASTERBILT": "Master-Bilt",
    "NORLAKE": "Norlake",
    "VICTORY": "Victory",
    "KASON": "Kason",
    "RANDALL": "Randell",
    "RANDELL": "Randell",
    "CONTINENTAL": "Continental",
    "FEDERAL": "Federal",
    "HUSSMANN": "Hussmann",
    "ANTHONY": "Anthony",
}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def clean_text(value: str) -> str:
    value = re.sub(r"<script\b.*?</script>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<style\b.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def normalize(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def canonical_brand(value: str) -> str:
    clean = value.strip()
    return BRAND_ALIASES.get(normalize(clean), clean)


def first_brand(manufacturers: str) -> str:
    if not manufacturers:
        return ""
    parts = re.split(r"[,;/&]+|\band\b", manufacturers, flags=re.I)
    return canonical_brand(parts[0].strip()) if parts else ""


def abs_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return BASE_URL + url
    return url


def extract_labeled(text: str, label: str) -> str:
    pattern = rf"{re.escape(label)}\s*:?\s*(.*?)(?=\s+(?:Commonly fits Manufacturers|Compatible With|Magnet|Color|Other Notes|Profile Variation|TECH TIP)\s*:|$)"
    match = re.search(pattern, text, flags=re.I)
    return match.group(1).strip(" :;\u00a0") if match else ""


def infer_color(text: str) -> str:
    t = text.lower()
    for color in ["gray", "grey", "white", "black", "beige", "clear"]:
        if re.search(rf"\b{color}\b", t):
            return "gray" if color == "grey" else color
    return ""


def infer_profile(title: str, desc: str) -> str:
    match = re.search(r"\bGU\s*[-]?\s*(\d{2,5})\b", f"{title} {desc}", flags=re.I)
    return f"GU{match.group(1)}" if match else ""


def split_compatible_numbers(value: str) -> str:
    if not value:
        return ""
    tokens = re.findall(r"[A-Z]*\d[A-Z0-9.-]*", value.upper())
    return "; ".join(dict.fromkeys(tokens))


def extract_products(page_html: str) -> list[dict]:
    products = []
    matches = list(re.finditer(r"<article class=\"card\b[^>]*data-product-id=\"(\d+)\"", page_html, flags=re.I))
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else min(len(page_html), match.end() + 12000)
        block = page_html[start:end]
        text = clean_text(block)

        title_match = re.search(r"<h[45] class=\"card-title\">\s*<a href=\"([^\"]+)\"[^>]*>(.*?)</a>", block, flags=re.I | re.S)
        if not title_match:
            continue
        source_url = abs_url(html.unescape(title_match.group(1)))
        title = clean_text(title_match.group(2))
        desc_match = re.search(r"<div class=\"card-desciption\">\s*<p>(.*?)</p>", block, flags=re.I | re.S)
        description = clean_text(desc_match.group(1)) if desc_match else ""

        if "gasket" not in f"{title} {description}".lower():
            continue
        if any(term in title.lower() for term in ["strip curtain", "curtain kit", "heater wire", "cutting board"]):
            continue

        image_match = re.search(r"<img class=\"card-image[^>]+data-src=\"([^\"]+)\"", block, flags=re.I)
        image_url = abs_url(html.unescape(image_match.group(1))) if image_match else ""
        price_match = re.search(r"<span[^>]+class=\"price price--withoutTax\">\\?\$([0-9,]+\.[0-9]{2})</span>", block, flags=re.I)

        manufacturers = extract_labeled(description, "Commonly fits Manufacturers")
        compatible_with = extract_labeled(description, "Compatible With")
        magnet = extract_labeled(description, "Magnet")
        raw_color = extract_labeled(description, "Color")
        color = infer_color(raw_color) or infer_color(f"{title} {description}")
        profile = infer_profile(title, description)
        brand = first_brand(manufacturers)
        compatible_numbers = split_compatible_numbers(compatible_with)
        primary_part = profile or f"GG-{match.group(1)}"

        products.append(
            {
                "source_name": "Gasket Guy",
                "source_url": source_url,
                "source_product_id": match.group(1),
                "raw_title": title,
                "part_name": title,
                "market_category": "commercial",
                "equipment_category": "refrigerator/freezer/cooler",
                "brand_raw": brand,
                "canonical_brand": brand,
                "commercial_fit_manufacturers": manufacturers,
                "gasket_profile_code": profile,
                "oem_part_number": primary_part,
                "normalized_part_number": normalize(primary_part),
                "compatible_oem_part_numbers": compatible_numbers,
                "alternate_part_numbers": compatible_numbers,
                "compatible_brands": "; ".join(dict.fromkeys([b.strip() for b in re.split(r"[,;/]+", manufacturers) if b.strip()])),
                "compatible_models": "",
                "door_position_raw": "commercial cooler/refrigerator door",
                "door_position_normalized": "commercial_door",
                "width_in": "",
                "height_in": "",
                "dimensions_text": "",
                "color": color,
                "material": "commercial refrigeration gasket; magnet noted" if magnet else "commercial refrigeration gasket",
                "profile_type": profile,
                "mounting_method": "",
                "magnetic": "yes" if magnet else "",
                "temperature_application": "commercial_refrigeration",
                "price_usd": price_match.group(1).replace(",", "") if price_match else "",
                "stock_status": "choose_options",
                "image_url": image_url,
                "description": description,
                "evidence_summary": "Public Gasket Guy commercial cooler gasket category listing.",
                "confidence_score": "75",
                "data_status": "candidate",
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "target_table_hint": "gasket_catalog; refrigerator_product_gaskets after model/application enrichment",
            }
        )
    return products


def main() -> int:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("work/commercial_gasket_import_sample.csv")
    rows = []
    seen = set()
    pages = [f"{BASE_URL}/cooler-gaskets/?page={i}" for i in range(1, 6)]
    for url in pages:
        if len(rows) >= 100:
            break
        page = fetch(url)
        for row in extract_products(page):
            key = row["source_url"] or row["source_product_id"]
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
            if len(rows) >= 100:
                break
        time.sleep(1)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=OUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows[:100])

    print(f"rows={min(len(rows), 100)} path={out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
