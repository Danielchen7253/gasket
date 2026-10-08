import os
from urllib.parse import quote_plus
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_KEY")
)

BRANDS = [
    "True",
    "Turbo Air",
    "Beverage-Air",
    "Traulsen",
    "Hoshizaki",
    "Everest",
    "Atosa",
    "Delfield",
    "Arctic Air",
    "Avantco"
]

EQUIPMENT_TYPES = [
    "reach in refrigerator",
    "reach in freezer",
    "glass door merchandiser",
    "prep table",
    "undercounter refrigerator",
    "sandwich prep table",
    "pizza prep table",
    "worktop refrigerator",
    "chef base",
    "bar cooler"
]

TARGET_SITES = [
    "partstown.com",
    "partsfe.com",
    "heritageparts.com",
    "webstaurantstore.com",
    "allpointsfps.com",
    "caseparts.com",
    "gasketsrock.com",
    "commercialrefrigerationgaskets.com"
]


def build_search_url(site, keyword):
    return f"https://www.google.com/search?q={quote_plus('site:' + site + ' ' + keyword)}"


tasks = []

for brand in BRANDS:
    for equipment_type in EQUIPMENT_TYPES:
        for site in TARGET_SITES:
            keyword = f"{brand} {equipment_type} gasket"

            tasks.append({
                "brand": brand,
                "equipment_type": equipment_type,
                "target_site": site,
                "search_keyword": keyword,
                "search_url": build_search_url(site, keyword),
                "task_status": "pending"
            })

result = supabase.table("crawl_tasks").insert(tasks).execute()

print(f"已生成任务数量: {len(tasks)}")
print(result)