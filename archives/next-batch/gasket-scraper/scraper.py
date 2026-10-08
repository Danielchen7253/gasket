import os
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def scrape_page(url):
    headers = {
        "User-Agent": "Mozilla/5.0"
    }

    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "lxml")

    title = soup.title.get_text(strip=True) if soup.title else ""

    body_text = soup.get_text("\n", strip=True)

    images = []
    for img in soup.find_all("img"):
        src = img.get("src")
        if src:
            images.append({
                "src": urljoin(url, src),
                "alt": img.get("alt", "")
            })

    links = []
    for a in soup.find_all("a"):
        href = a.get("href")
        if href:
            links.append({
                "text": a.get_text(" ", strip=True),
                "href": urljoin(url, href)
            })

    return {
        "source_url": url,
        "scraped_at": datetime.utcnow().isoformat(),
        "title": title,
        "body_text": body_text,
        "images": images,
        "links": links
    }


def save_to_supabase(payload):
    result = supabase.table("raw_scrapes").insert({
        "raw_payload": payload,
        "status": "pending"
    }).execute()

    print(result)


if __name__ == "__main__":
    url = input("请输入要爬取的网址: ").strip()
    payload = scrape_page(url)
    save_to_supabase(payload)
    print("完成：网页原始数据已写入 raw_scrapes")