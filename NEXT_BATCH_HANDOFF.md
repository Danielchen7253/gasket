# Next batch source preservation — 2026-10-07

Branch: `codex/next-batch-migration-2026-10-07`

## Local origins

- `C:\Users\joel7\gasket_scraper`
- `C:\Users\joel7\Documents\Codex\2026-06-14\new-chat\work`
- `C:\Users\joel7`

## Recovery and scope

Clone this repository and check out the migration branch. Existing application files retain the previous core migration baseline. Additions under archives/next-batch are historical or local variants and are not automatically integrated into the application. New standalone repositories contain source snapshots and, where needed, separate variants. Identical relative-path/content copies were deduplicated; distinct versions are preserved.

SOURCE_MANIFEST.json maps original local source paths to this repository. Source content is copied, except environment templates and explicitly recorded credential/contact sanitization. Real .env files, customer records, databases/dumps, browser profiles, temporary captures, dependencies and generated build exports are excluded. Source history from old local-only Git repositories is not pushed wholesale, because it can contain secrets.

No production build, live database migration, crawl, customer messaging or deployment was run. Restoring code does not restore credentials, Windows DPAPI-protected connection stores or business data. Archived operational scripts may require their original project context and replacement private configuration; do not run them blindly.
