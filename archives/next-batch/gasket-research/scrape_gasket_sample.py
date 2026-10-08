import csv
import html
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


BASE_URL = "https://www.appliancepartspros.com"
START_URL = f"{BASE_URL}/refrigerator-gasket-seal.html"


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
    "brand_raw",
    "canonical_brand",
    "oem_part_number",
    "normalized_part_number",
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
    "FRIGIDAIRE": "Frigidaire",
    "WHIRLPOOL": "Whirlpool",
    "GE": "GE",
    "GENERAL ELECTRIC": "GE",
    "SAMSUNG": "Samsung",
    "LG": "LG",
    "ELECTROLUX": "Electrolux",
    "MAYTAG": "Maytag",
    "KENMORE": "Kenmore",
    "KITCHENAID": "KitchenAid",
    "AMANA": "Amana",
    "TRUE": "True",
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


def normalize_part_number(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def canonical_brand(value: str) -> str:
    key = normalize_part_number(value)
    return BRAND_ALIASES.get(key, value.strip())


def abs_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return BASE_URL + url
    return url


def infer_color(text: str) -> str:
    t = text.lower()
    for color in ["gray", "grey", "white", "black", "beige", "bisque"]:
        if re.search(rf"\b{color}\b", t):
            return "gray" if color == "grey" else color
    return ""


def infer_door_position(text: str) -> tuple[str, str]:
    t = text.lower()
    raw = ""
    norm = ""
    if "freezer" in t:
        raw = "freezer door"
        norm = "freezer"
    elif "fresh food" in t or "refrigerator" in t:
        raw = "refrigerator door"
        norm = "refrigerator"
    if "right hand" in t or "right-hand" in t:
        raw = (raw + " right").strip()
        norm = (norm + "_right").strip("_")
    if "left hand" in t or "left-hand" in t:
        raw = (raw + " left").strip()
        norm = (norm + "_left").strip("_")
    return raw, norm


def infer_dimensions(text: str) -> tuple[str, str, str]:
    patterns = [
        r"measures approximately\s+([\d.]+)\s*(?:by|x)\s*([\d.]+)\s*inches",
        r"measures\s+([\d.]+)\s*(?:by|x)\s*([\d.]+)\s*inches",
        r"([\d.]+)\s*(?:\"|inches)\s*(?:wide|w)\s*(?:by|x|and)\s*([\d.]+)\s*(?:\"|inches)\s*(?:long|high|h)?",
        r"([\d.]+)\s*(?:by|x)\s*([\d.]+)\s*(?:\"|inches)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            a, b = match.group(1), match.group(2)
            return a, b, f"{a} x {b} in"
    return "", "", ""


def infer_compatible_brands(text: str, brand: str) -> str:
    found = []
    for raw, canon in BRAND_ALIASES.items():
        label = canon
        if re.search(rf"\b{re.escape(label)}\b", text, flags=re.I) and label not in found:
            found.append(label)
    if brand and brand not in found:
        found.insert(0, brand)
    return "; ".join(found)


def extract_products(page_html: str) -> list[dict]:
    products = []
    matches = list(re.finditer(r"<div class=\"product-item-hold\">", page_html, flags=re.I))
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else min(len(page_html), match.end() + 12000)
        block = page_html[start:end]
        text = clean_text(block)

        item_id_match = re.search(r"'item_id':\s*'([^']+)'", block)
        item_name_match = re.search(r"'item_name':\s*'([^']+)'", block)
        item_brand_match = re.search(r"'item_brand':\s*'([^']+)'", block)
        item_price_match = re.search(r"'price':\s*([0-9.]+)", block)
        item_image_match = re.search(r"'ImageURL':\s*'([^']+)'", block)
        item_url_match = re.search(r"'ProductURL':\s*'([^']+)'", block)

        product_url = html.unescape(item_url_match.group(1)) if item_url_match else ""
        part_match = re.search(r"-([A-Za-z0-9]+)-ap\d+\.html", product_url)
        if not part_match:
            part_match = re.search(r"Part Number:\s*</?[^>]*>\s*([^<\s]+)", block, flags=re.I)
        part_number = html.unescape(part_match.group(1)).strip() if part_match else ""
        if not part_number or part_number.lower() in {"number"}:
            continue

        title_match = re.search(r"<h3[^>]*>\s*<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>", block, flags=re.I | re.S)
        if not title_match:
            title_match = re.search(r"<a[^>]+href=\"([^\"]+)\"[^>]*>\s*([^<]*(?:Gasket|Seal)[^<]*)\s*</a>", block, flags=re.I | re.S)
        product_url = product_url or (abs_url(title_match.group(1)) if title_match else "")
        title = html.unescape(item_name_match.group(1)) if item_name_match else (clean_text(title_match.group(2)) if title_match else "")

        brand_match = re.search(r"By:\s*</?[^>]*>\s*([^<\n\r]+)", block, flags=re.I)
        if not brand_match:
            brand_match = re.search(r"Genuine\s+([A-Za-z0-9 &.-]+?)\s+part", text, flags=re.I)
        brand_raw = html.unescape(item_brand_match.group(1)).title() if item_brand_match else (clean_text(brand_match.group(1)) if brand_match else "")
        brand = canonical_brand(brand_raw)

        price_match = re.search(r"\$([0-9,]+\.[0-9]{2})", text)
        stock = "in_stock" if re.search(r"\bIN STOCK\b|Ships Today", text, flags=re.I) else ""
        image_match = re.search(r"<img[^>]+(?:src|data-src)=\"([^\"]+)\"[^>]*(?:alt=\"([^\"]*)\")?", block, flags=re.I)
        image_url = html.unescape(item_image_match.group(1)) if item_image_match else (abs_url(html.unescape(image_match.group(1))) if image_match else "")

        desc_start = text.find(part_number)
        desc = text[desc_start + len(part_number) : desc_start + 900].strip() if desc_start >= 0 else text[:900]
        desc = re.sub(r"Do-It-Yourself Stories.*", "", desc, flags=re.I).strip()
        color = infer_color(f"{title} {desc}")
        width, height, dimensions = infer_dimensions(desc)
        raw_door, norm_door = infer_door_position(f"{title} {desc}")
        magnetic = "yes" if re.search(r"\bmagnet|magnetic\b", desc, flags=re.I) else ""
        material = "PVC/rubber flexible gasket" if re.search(r"\bPVC|rubber|flexible\b", desc, flags=re.I) else ""
        mount = "press-in / groove" if re.search(r"\bpress(?:es)? into|groove|slot|channel|barbed ridge\b", desc, flags=re.I) else ""
        temp_app = "freezer" if "freezer" in f"{title} {desc}".lower() else "refrigerator"

        products.append(
            {
                "source_name": "AppliancePartsPros",
                "source_url": product_url or START_URL,
                "source_product_id": html.unescape(item_id_match.group(1)) if item_id_match else part_number,
                "raw_title": title,
                "part_name": title,
                "brand_raw": brand_raw,
                "canonical_brand": brand,
                "oem_part_number": part_number.upper(),
                "normalized_part_number": normalize_part_number(part_number),
                "alternate_part_numbers": "",
                "compatible_brands": infer_compatible_brands(desc, brand),
                "compatible_models": "",
                "door_position_raw": raw_door,
                "door_position_normalized": norm_door,
                "width_in": width,
                "height_in": height,
                "dimensions_text": dimensions,
                "color": color,
                "material": material,
                "profile_type": "",
                "mounting_method": mount,
                "magnetic": magnetic,
                "temperature_application": temp_app,
                "price_usd": item_price_match.group(1) if item_price_match else (price_match.group(1).replace(",", "") if price_match else ""),
                "stock_status": stock,
                "image_url": image_url,
                "description": desc,
                "evidence_summary": "Public product listing parsed from refrigerator gasket/seal category.",
                "confidence_score": "70",
                "data_status": "candidate",
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "target_table_hint": "gasket_catalog; refrigerator_product_gaskets when model/application is known",
            }
        )
    return products


def main() -> int:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("work/gasket_import_sample.csv")
    pages = [START_URL] + [f"{BASE_URL}/refrigerator-gasket-seal-p{i}.html" for i in range(2, 33)]
    rows = []
    seen = set()
    for url in pages:
        if len(rows) >= 100:
            break
        page = fetch(url)
        for row in extract_products(page):
            key = row["normalized_part_number"]
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
        time.sleep(1)

    if len(rows) < 100:
        # AppliancePartsPros embeds many category records in one page, but only the first
        # listing page is reliably available. Keep a clean sample instead of fabricating rows.
        pass

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=OUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows[:100])

    print(f"rows={min(len(rows), 100)} path={out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
