import csv
import html
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

GASKET_GUY_BASE = "https://shop.gasketguy.com"

OUT_COLUMNS = [
    "source_site",
    "source_category_url",
    "source_product_url",
    "source_product_id",
    "profile_code",
    "product_title",
    "cross_section_image_url",
    "cross_section_image_formula",
    "product_image_url",
    "image_type",
    "color",
    "magnet_code",
    "compatible_manufacturers",
    "compatible_part_numbers",
    "gasket_size_text",
    "size_source_note",
    "material",
    "mounting_method",
    "temperature_application",
    "description",
    "evidence_summary",
    "confidence_score",
    "data_status",
    "scraped_at",
    "target_table_hint",
]


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def clean_text(value: str) -> str:
    value = re.sub(r"<script\b.*?</script>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<style\b.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = value.replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def abs_url(url: str, base: str = GASKET_GUY_BASE) -> str:
    if not url:
        return ""
    url = html.unescape(url)
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return base + url
    return url


def normalize_color(value: str) -> str:
    t = (value or "").lower()
    if "grey" in t or "gray" in t:
        return "gray"
    if "black" in t:
        return "black"
    if "white" in t:
        return "white"
    if "clear" in t:
        return "clear"
    return clean_text(value)


def extract_labeled_from_text(text: str, label: str) -> str:
    labels = "Commonly fits Manufacturers|Compatible With|Magnet|Color|Other Notes|Profile Variation|TECH TIP"
    pattern = rf"{re.escape(label)}\s*:?\s*(.*?)(?=\s+(?:{labels})\s*:|$)"
    match = re.search(pattern, text, flags=re.I)
    return match.group(1).strip(" :;") if match else ""


def extract_product_links_from_gasket_guy(page_html: str) -> list[dict]:
    results = []
    matches = list(re.finditer(r"<article class=\"card\b[^>]*data-product-id=\"(\d+)\"", page_html, flags=re.I))
    for idx, match in enumerate(matches):
        block = page_html[match.start() : matches[idx + 1].start() if idx + 1 < len(matches) else len(page_html)]
        title_match = re.search(r"<h[45] class=\"card-title\">\s*<a href=\"([^\"]+)\"[^>]*>(.*?)</a>", block, flags=re.I | re.S)
        if not title_match:
            continue
        title = clean_text(title_match.group(2))
        if "gasket" not in title.lower():
            continue
        if any(skip in title.lower() for skip in ["strip curtain", "heater wire", "cutting board"]):
            continue
        results.append({"id": match.group(1), "url": abs_url(title_match.group(1)), "title": title})
    return results


def parse_gasket_guy_detail(url: str, category_url: str, product_id: str, title_hint: str) -> dict | None:
    page = fetch(url)
    title_match = re.search(r"<h1[^>]*class=\"productView-title\"[^>]*>(.*?)</h1>", page, flags=re.I | re.S)
    title = clean_text(title_match.group(1)) if title_match else title_hint
    if "gasket" not in title.lower():
        return None
    profile_match = re.search(r"\bGU\s*[-]?\s*(\d{2,5})\b", title, flags=re.I)
    profile_code = f"GU{profile_match.group(1)}" if profile_match else ""

    image_matches = re.findall(
        r"data-image-gallery-new-image-url=\"([^\"]+)\"|data-image-gallery-zoom-image-url=\"([^\"]+)\"",
        page,
        flags=re.I,
    )
    image_urls = []
    for first, second in image_matches:
        url_value = abs_url(first or second)
        if url_value and url_value not in image_urls:
            image_urls.append(url_value)
    if not image_urls:
        image_urls = [abs_url(m) for m in re.findall(r"<meta property=\"og:image\" content=\"([^\"]+)\"", page, flags=re.I)]

    cad_images = [u for u in image_urls if re.search(r"(?:_cad|cad__|cs_cad|cross|profile)", u, flags=re.I)]
    cross_section_image = cad_images[0] if cad_images else (image_urls[1] if len(image_urls) > 1 else (image_urls[0] if image_urls else ""))
    product_image = image_urls[0] if image_urls else ""
    if not cross_section_image:
        return None

    desc_match = re.search(r"<div class=\"toggle-content is-open\" id=\"tabDescription-content\"[^>]*>(.*?)</div>", page, flags=re.I | re.S)
    description_html = desc_match.group(1) if desc_match else ""
    description = clean_text(description_html)
    manufacturers = extract_labeled_from_text(description, "Commonly fits Manufacturers")
    compatible = extract_labeled_from_text(description, "Compatible With")
    magnet = extract_labeled_from_text(description, "Magnet")
    color = normalize_color(extract_labeled_from_text(description, "Color"))

    if not profile_code:
        profile_from_desc = re.search(r"Profile\s+GU\s*[-]?\s*(\d{2,5})", description, flags=re.I)
        profile_code = f"GU{profile_from_desc.group(1)}" if profile_from_desc else ""

    material = "PVC commercial refrigeration gasket"
    if re.search(r"santoprene", f"{title} {description}", flags=re.I):
        material = "Santoprene commercial refrigeration gasket"

    mounting = ""
    if re.search(r"push\s*in|push-in", f"{title} {description}", flags=re.I):
        mounting = "push-in"
    elif re.search(r"screw", f"{title} {description}", flags=re.I):
        mounting = "screw-in"

    compatible_numbers = "; ".join(dict.fromkeys(re.findall(r"[A-Z]{0,4}\d[A-Z0-9.-]*", compatible.upper())))

    return {
        "source_site": "Gasket Guy",
        "source_category_url": category_url,
        "source_product_url": url,
        "source_product_id": product_id,
        "profile_code": profile_code,
        "product_title": title,
        "cross_section_image_url": cross_section_image,
        "cross_section_image_formula": f'=IMAGE("{cross_section_image}")',
        "product_image_url": product_image,
        "image_type": "cross-section CAD/profile image",
        "color": color,
        "magnet_code": magnet,
        "compatible_manufacturers": manufacturers,
        "compatible_part_numbers": compatible_numbers,
        "gasket_size_text": "custom door gasket size; order by measured outside-edge width and height",
        "size_source_note": "Product page does not expose fixed finished size; profile/cross-section dimensions, when available, are shown in the cross-section image.",
        "material": material,
        "mounting_method": mounting,
        "temperature_application": "commercial refrigeration / cooler / freezer",
        "description": description[:900],
        "evidence_summary": "Commercial gasket profile image and product metadata parsed from public product detail page.",
        "confidence_score": "80" if profile_code and color else "70",
        "data_status": "candidate",
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "target_table_hint": "gasket_catalog.profile_image_url / profile_drawing_url",
    }


def main() -> int:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("work/cross_section_images_100.csv")
    rows = []
    seen_urls = set()
    category_pages = [f"{GASKET_GUY_BASE}/cooler-gaskets/?page={i}" for i in range(1, 6)]
    candidates = []
    for category_url in category_pages:
        page = fetch(category_url)
        for product in extract_product_links_from_gasket_guy(page):
            product["category_url"] = category_url
            if product["url"] not in seen_urls:
                seen_urls.add(product["url"])
                candidates.append(product)
    for product in candidates:
        if len(rows) >= 100:
            break
        try:
            row = parse_gasket_guy_detail(product["url"], product["category_url"], product["id"], product["title"])
            if row:
                rows.append(row)
        except Exception as exc:
            print(f"warn failed {product['url']}: {exc}", file=sys.stderr)
        time.sleep(0.4)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=OUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"rows={len(rows)} path={out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
