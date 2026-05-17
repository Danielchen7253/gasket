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
- `OPENAI_API_KEY` optional for OCR/nameplate extraction

## Render cron jobs

Create separate Cron Jobs from this same repo.

Market discovery:

```bash
python market_discovery_crawler.py
```

Product image enrichment:

```bash
python product_image_search_crawler.py
```

Gasket enrichment:

```bash
python gasket_enrichment_crawler.py
```

Recommended schedule while building data fast:

- Discovery: every 20 minutes
- Product images: every 30 minutes
- Gasket enrichment: every 30 minutes
