# Gasket Match Center

Internal gasket matching web app and crawler tools.

## Render web service

Build command:

```bash
pip install -r requirements.txt
```

Start command:

```bash
python nameplate_web_app.py
```

Required environment variables:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `GOOGLE_API_KEY` optional for image search
- `GOOGLE_CSE_ID` optional for image search
- `OPENAI_API_KEY` fallback OpenAI key
- `OPENAI_NAMEPLATE_API_KEY` optional dedicated key for nameplate image reading
- `OPENAI_RESEARCH_API_KEY` optional dedicated key for product/gasket research
- `OPENAI_IMAGE_API_KEY` optional dedicated key for image-search fallback

## Render data pipeline

Use one Cron Job from this same repo instead of three independent jobs.

```bash
python data_pipeline_worker.py
```

Recommended schedule while building data fast:

```cron
*/5 * * * *
```

The pipeline runs in this order:

1. Discover new refrigerator brands and models.
2. Immediately backfill missing product images.
3. Create missing gasket-spec placeholders for new products.
4. Immediately backfill gasket data.
