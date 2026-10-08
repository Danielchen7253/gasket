import os
import time
from datetime import datetime
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

BRANDS = [
    "True",
    "Turbo Air",
    "Beverage-Air",
    "Traulsen",
    "Hoshizaki",
    "Delfield",
    "Everest",
    "Atosa"
]


def search_partstown(brand):
    url = f"https://www.partstown.com/search?q={quote_plus(brand + ' gasket')}"

    response = requests.get(url, headers=HEADERS, timeout=30)

    print("状态码:", response.status_code)

    soup = BeautifulSoup(response.text, "lxml")

    results = []

    for a in soup.find_all("a", href=True):

        href = a["href"]
        text = a.get_text(" ", strip=True)

        if "/parts/" not in href:
            continue

        full_url = urljoin("https://www.partstown.com", href)

        results.append({
            "title": text,
            "url": full_url
        })

    return results


def save_result(brand, item):
    payload = {
        "scrape_type": "candidate_link",
        "brand": brand,
        "source_name": "Parts Town",
        "source_url": item["url"],
        "page_title": item["title"],
        "scraped_at": datetime.utcnow().isoformat()
    }

    supabase.table("raw_scrapes").insert({
        "raw_payload": payload,
        "status": "pending"
    }).execute()


if __name__ == "__main__":

    for brand in BRANDS:

        print(f"\n开始抓品牌: {brand}")

        try:
            links = search_partstown(brand)

            print("找到链接数量:", len(links))

            for item in links:
                save_result(brand, item)

            time.sleep(3)

        except Exception as e:
            print("失败:", brand, str(e))

    print("\n全部完成")