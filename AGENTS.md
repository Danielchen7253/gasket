# Gasket Cloud handoff

Independent Python gasket matching/research/data worker project. Keep it independent from BIGDB until a separate migration decision is made. Read README.md and relevant schema/worker files before changes.

Main web entry: nameplate_web_app.py. Other root Python modules are crawler/enrichment/data pipeline tools; SQL files define schema; product/model CSV data must be distinguished from actual customer records.

Create an isolated Python environment, then install with python -m pip install -r requirements.txt. Additional crawler_requirements.txt is separate; inspect the selected worker imports before installing. Start: python nameplate_web_app.py. No configured build/lint/test commands or test runner have been confirmed; do not invent passing test results. Syntax/import validation does not establish crawler, live database or customer-flow readiness.

Environment: .env.example contains names only; existing README lists SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY plus optional Google settings. Never commit keys or customer data. Schema/seed changes must target an explicitly isolated database; do not run crawlers, background workers or production migrations during migration preparation.

Deployment: existing README describes Render. Do not alter production deployment or services. Preserve old gasket_scraper and historical worker source; compare before moving/merging. The local daily_database_digest.py deletion is recorded, not propagated in this preparation.
