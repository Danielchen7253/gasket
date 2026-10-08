import os
from datetime import datetime
from urllib.parse import quote_plus

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

BRANDS = [
    "True",
    "Turbo Air",
    "Beverage-Air",
    "Traulsen",
    "Hoshizaki",
    "Delfield",
    "Everest",
    "Norlake",
    "Avantco",
    "Atosa",
    "Arctic Air",
    "Migali",
    "Koolmore",
    "Maxx Cold",
    "MoTak"
]

KEYWORDS = [
    "commercial refrigerator gasket",
    "door gasket",
    "replacement gasket",
    "refrigerator door seal",
    "freezer door gasket",
    "parts"
]


def build_search_urls(brand):
    urls = []

    for keyword in KEYWORDS:
        query = f"{brand} {keyword}"

        urls.append({
            "engine": "google_search_url",
            "url": "https://www.google.com/search?q=" + quote_plus(query),
            "query": query
        })

        urls.append({
            "engine": "bing_search_url",
            "url": "https://www.bing.com/search?q=" + quote_plus(query),
            "query": query
        })

    return urls


def save_brand_seed(brand):
    payload = {
        "scrape_type": "brand_seed",
        "market": "US",
        "category": "commercial_refrigeration_gasket",
        "brand": brand,
        "search_urls": build_search_urls(brand),
        "created_by": "brand_seed_scraper",
        "scraped_at": datetime.utcnow().isoformat()
    }

    result = supabase.table("raw_scrapes").insert({
        "raw_payload": payload,
        "status": "pending"
    }).execute()

    print(f"Saved brand seed: {brand}")
    return result


if __name__ == "__main__":
    for brand in BRANDS:
        save_brand_seed(brand)

    print("完成：美国商用制冷品牌种子已经写入 raw_scrapes")