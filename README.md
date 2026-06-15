# Gasket Match Center

Internal gasket matching web app and data-maintenance tools.

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
- `SERPAPI_KEY` optional Google Images fallback
- `BRAVE_SEARCH_API_KEY` optional Brave web/image search fallback
- `BING_SEARCH_API_KEY` optional Bing web/image search fallback
- `BING_SEARCH_ENDPOINT` optional Bing endpoint, defaults to `https://api.bing.microsoft.com/v7.0`
- `OPENAI_API_KEY` fallback OpenAI key
- `OPENAI_NAMEPLATE_API_KEY` optional dedicated key for nameplate image reading
- `OPENAI_RESEARCH_API_KEY` optional dedicated key for product/gasket research
- `OPENAI_IMAGE_API_KEY` optional dedicated key for image-search fallback

## Render data pipeline

The data pipeline is a light maintenance job. Customer-triggered lookups handle
product and gasket images first, so batch jobs should not spend paid API budget
filling images in bulk.

```bash
python data_pipeline_worker.py
```

Recommended schedule while building data fast:

```cron
*/5 * * * *
```

The pipeline runs in this order:

1. Discover new refrigerator brands and models.
2. Create or refresh missing gasket-spec placeholders for new products.
3. Refresh customer quote records from current product and gasket data.
4. Leave product and gasket images to the customer lookup flow.
