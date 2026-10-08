import os
import time
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_KEY")
)

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

LIMIT = 800


def get_pending_tasks():

    result = supabase.table("crawl_tasks") \
        .select("*") \
        .eq("task_status", "pending") \
        .limit(LIMIT) \
        .execute()

    return result.data or []


def save_raw(payload):

    supabase.table("raw_scrapes").insert({
        "raw_payload": payload,
        "status": "pending"
    }).execute()


def mark_completed(task_id):

    supabase.table("crawl_tasks") \
        .update({
            "task_status": "completed",
            "updated_at": datetime.utcnow().isoformat()
        }) \
        .eq("id", task_id) \
        .execute()


def mark_error(task_id, message):

    supabase.table("crawl_tasks") \
        .update({
            "task_status": "error",
            "updated_at": datetime.utcnow().isoformat()
        }) \
        .eq("id", task_id) \
        .execute()

    print("错误:", message)


def scrape_search_page(task):

    url = task["search_url"]

    print("抓取:", url)

    response = requests.get(
        url,
        headers=HEADERS,
        timeout=30
    )

    soup = BeautifulSoup(response.text, "lxml")

    page_text = soup.get_text("\n", strip=True)

    links = []

    for a in soup.find_all("a", href=True):

        href = a["href"]

        if href.startswith("http"):

            links.append({
                "title": a.get_text(" ", strip=True),
                "url": href
            })

    payload = {
        "scrape_type": "search_result_page",
        "brand": task["brand"],
        "equipment_type": task["equipment_type"],
        "target_site": task["target_site"],
        "search_keyword": task["search_keyword"],
        "search_url": task["search_url"],
        "page_text": page_text,
        "links": links,
        "scraped_at": datetime.utcnow().isoformat()
    }

    return payload


if __name__ == "__main__":

    tasks = get_pending_tasks()

    print("本轮任务数量:", len(tasks))

    for task in tasks:

        try:

            payload = scrape_search_page(task)

            save_raw(payload)

            mark_completed(task["id"])

            print("完成:", task["search_keyword"])

            time.sleep(2)

        except Exception as e:

            mark_error(task["id"], str(e))

    print("本轮结束")