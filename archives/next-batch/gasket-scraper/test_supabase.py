import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

data = {
    "raw_payload": {
        "source": "local_test",
        "message": "hello raw_scrapes"
    },
    "status": "pending"
}

result = supabase.table("raw_scrapes").insert(data).execute()

print(result)