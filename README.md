# FixPro24 Gasket Match

Customer-facing refrigerator door gasket matching site and upload workflow.

## What This App Does

- Shows a customer homepage for refrigerator gasket matching.
- Lets customers start with a nameplate photo or brand/model.
- Queries Supabase-backed product and gasket records.
- Prepares a match result that can later connect to Shopify checkout.

## Render Deployment

Use these settings on Render:

- Runtime: `Python 3`
- Build Command: `pip install -r requirements.txt`
- Start Command: `python nameplate_web_app.py`

Required environment variables:

```text
SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=
```

Optional environment variables:

```text
GOOGLE_API_KEY=
GOOGLE_CSE_ID=
```

Do not commit `.env` or any private API keys.

## Local Run

```powershell
python nameplate_web_app.py
```

Then open:

```text
http://127.0.0.1:8000/
```
