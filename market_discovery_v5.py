import re
from urllib.parse import urlparse

import httpx

import market_discovery_v3 as base

BRAND_DOMAINS = {
    "Arctic Air": ["arcticairco.com"],
    "Beverage-Air": ["beverage-air.com"],
    "Continental": ["continentalrefrigerator.com"],
    "Delfield": ["delfield.com"],
    "Everest": ["everestref.com"],
    "Hoshizaki": ["hoshizakiamerica.com"],
    "Migali": ["migali.com"],
    "Traulsen": ["traulsen.com"],
    "True": ["truemfg.com"],
    "Turbo Air": ["turboairinc.com"],
    "Master-Bilt": ["master-bilt.com"],
    "Nor-Lake": ["norlake.com"],
    "Victory": ["victoryrefrigeration.com"],
    "Randell": ["randell.com"],
    "Perlick": ["perlick.com"],
    "Sub-Zero": ["subzero-wolf.com"],
    "Whirlpool": ["whirlpool.com"],
    "GE": ["geappliances.com"],
    "Frigidaire": ["frigidaire.com"],
    "KitchenAid": ["kitchenaid.com"],
}

SITEMAP_CACHE: dict[str, list[str]] = {}


def brand_from_query(query: str) -> str | None:
    for brand in base.KNOWN_BRANDS:
        if base.brand_in_text(brand, query):
            return brand
    return None


def fetch_text(client: httpx.Client, url: str) -> str:
    try:
        r = client.get(url, headers={"User-Agent": base.USER_AGENT}, follow_redirects=True)
        if r.status_code >= 400:
            return ""
        return r.text
    except Exception:
        return ""


def sitemap_locations(xml: str) -> list[str]:
    return [x.strip() for x in re.findall(r"<loc>\s*([^<]+)\s*</loc>", xml, re.I) if x.strip()]


def likely_product_url(url: str) -> bool:
    low = url.lower()
    if not any(word in low for word in ("product", "refriger", "freezer", "cooler", "reach", "undercounter", "gasket", "spec", "manual")):
        return False
    if any(blocked in low for blocked in ("blog", "news", "privacy", "warranty", "contact", "about", "category", "tag/")):
        return False
    return True


def urls_for_domain(client: httpx.Client, domain: str) -> list[str]:
    if domain in SITEMAP_CACHE:
        return SITEMAP_CACHE[domain]
    roots = [f"https://www.{domain}/sitemap.xml", f"https://{domain}/sitemap.xml"]
    urls: list[str] = []
    child_sitemaps: list[str] = []
    for root in roots:
        xml = fetch_text(client, root)
        if not xml:
            continue
        locs = sitemap_locations(xml)
        child_sitemaps.extend([loc for loc in locs if loc.lower().endswith(".xml")])
        urls.extend([loc for loc in locs if likely_product_url(loc)])
        if locs:
            break
    for child in child_sitemaps[:8]:
        low = child.lower()
        if not any(word in low for word in ("product", "page", "post", "sitemap")):
            continue
        xml = fetch_text(client, child)
        urls.extend([loc for loc in sitemap_locations(xml) if likely_product_url(loc)])
        if len(urls) >= 80:
            break
    deduped = []
    seen = set()
    for url in urls:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    SITEMAP_CACHE[domain] = deduped[:120]
    return SITEMAP_CACHE[domain]


def sitemap_search(client: httpx.Client, brand: str | None, per_query: int) -> list[dict]:
    if not brand:
        return []
    rows: list[dict] = []
    for domain in BRAND_DOMAINS.get(brand, []):
        for url in urls_for_domain(client, domain):
            path = urlparse(url).path.replace("-", " ").replace("_", " ").replace("/", " ")
            title = f"{brand} {path}"
            rows.append({
                "url": url,
                "title": title,
                "snippet": f"{brand} product page from official sitemap: {url}",
                "search_type": "web",
                "image_url": None,
            })
            if len(rows) >= per_query * 2:
                return rows
    return rows


def search(client, query: str, per_query: int) -> list[dict]:
    brand = brand_from_query(query)
    rows = base.google_search(client, query, per_query, "web")
    rows.extend(sitemap_search(client, brand, per_query))
    if not rows:
        rows = base.fallback_search(client, query, per_query)
    seen = set()
    out = []
    for row in rows:
        key = row.get("url")
        if key and key not in seen:
            seen.add(key)
            row["image_url"] = None
            row["search_type"] = "web"
            out.append(row)
    return out[:per_query * 3]


def main() -> None:
    base.search = search
    base.main()


if __name__ == "__main__":
    main()
