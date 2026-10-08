import os
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

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

LIMIT = 50


def get_source_name(url):
    domain = urlparse(url).netloc.lower()

    if "partstown" in domain:
        return "Parts Town"
    if "partsfe" in domain:
        return "PartsFe"
    if "webstaurantstore" in domain:
        return "WebstaurantStore"
    if "heritageparts" in domain:
        return "Heritage Parts"
    if "allpointsfps" in domain:
        return "AllPoints"
    if "caseparts" in domain:
        return "Case Parts"
    if "gasketsrock" in domain:
        return "Gaskets Rock"
    if "commercialrefrigerationgaskets" in domain:
        return "Commercial Refrigeration Gaskets"

    return domain


def scrape_detail_page(url, brand=None):
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "lxml")

    page_title = soup.title.get_text(strip=True) if soup.title else ""
    page_text = soup.get_text("\n", strip=True)

    images = []
    for img in soup.find_all("img"):
        src = img.get("src")
        if src:
            images.append({
                "src": urljoin(url, src),
                "alt": img.get("alt", "")
            })

    tables = []
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if cells:
                rows.append(cells)
        if rows:
            tables.append(rows)

    links = []
    for a in soup.find_all("a"):
        href = a.get("href")
        if href:
            links.append({
                "text": a.get_text(" ", strip=True),
                "href": urljoin(url, href)
            })

    return {
        "scrape_type": "gasket_source_page",
        "brand": brand,
        "source_name": get_source_name(url),
        "source_url": url,
        "page_title": page_title,
        "page_text": page_text,
        "images": images,
        "tables": tables,
        "links": links,
        "scraped_at": datetime.utcnow().isoformat()
    }


def get_candidate_links():
    result = supabase.table("raw_scrapes") \
        .select("*") \
        .eq("status", "pending") \
        .limit(LIMIT) \
        .execute()

    rows = result.data or []

    candidates = []

    for row in rows:
        payload = row.get("raw_payload", {})

        if payload.get("scrape_type") in ["valuable_candidate_link", "candidate_link"]:
            candidates.append(row)

        if payload.get("scrape_type") == "candidate_links":
            for link in payload.get("links", []):
                url = link.get("url")
                if url:
                    candidates.append({
                        "id": row["id"],
                        "raw_payload": {
                            "scrape_type": "candidate_link",
                            "brand": payload.get("brand"),
                            "url": url,
                            "title": link.get("title"),
                            "source_parent_id": row["id"]
                        }
                    })

    return candidates


def already_scraped(url):
    result = supabase.table("raw_scrapes") \
        .select("id") \
        .eq("raw_payload->>source_url", url) \
        .limit(1) \
        .execute()

    return bool(result.data)


def save_source_page(payload):
    supabase.table("raw_scrapes").insert({
        "raw_payload": payload,
        "status": "pending"
    }).execute()


def mark_processed(row_id):
    supabase.table("raw_scrapes") \
        .update({"status": "processed"}) \
        .eq("id", row_id) \
        .execute()


def mark_error(row_id, message):
    supabase.table("raw_scrapes") \
        .update({
            "status": "error",
            "error_message": message
        }) \
        .eq("id", row_id) \
        .execute()


if __name__ == "__main__":
    candidates = get_candidate_links()

    print(f"找到候选链接数量: {len(candidates)}")

    for item in candidates:
        row_id = item["id"]
        payload = item["raw_payload"]

        url = payload.get("url") or payload.get("source_url")
        brand = payload.get("brand")

        if not url:
            mark_error(row_id, "missing url")
            continue

        if already_scraped(url):
            print("已抓过，跳过:", url)
            mark_processed(row_id)
            continue

        try:
            print("抓取详情页:", url)
            page_payload = scrape_detail_page(url, brand)
            save_source_page(page_payload)
            mark_processed(row_id)
            print("完成:", url)
            time.sleep(2)

        except Exception as e:
            print("失败:", url, str(e))
            mark_error(row_id, str(e))

    print("本轮完成")