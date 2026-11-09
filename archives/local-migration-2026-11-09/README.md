# Local project source preservation — 2026-11-09

This is a source-only backup on codex/local-project-migration-2026-11-09. The repository root retains the existing remote baseline. New local source is archived rather than deployed.

SOURCE_MANIFEST.json maps original local paths to saved files and records credential/contact sanitization. SOURCE_SNAPSHOTS.json records each dirty Git workspace base HEAD, changed/untracked paths and tracked missing files. To reconstruct a workspace, create a separate clone at its recorded local HEAD (where available remotely), then apply its archived snapshot overlay. Missing-file records describe the original workspace and do not authorize deleting files. Unchanged files remain in existing GitHub history.

Historical unique tools are under unique/. Runtime data, customer exports, real .env, credentials, encrypted backups, browser profiles, dependencies and generated build outputs are intentionally excluded. Any required private configuration must be supplied securely. No application tests/builds, messages, crawls, database migrations or deployments were executed.


## Recovery modes

For base_plus_overlay, clone the verified base HEAD and apply the snapshot overlay. For full_source_snapshot, use the archived current application text/config tree; the old local base commit is not reachable from GitHub. ASSET_REFERENCES.json identifies existing remote Git blobs and stored_at locations for preserved brand/icon assets. Source hashes normalize text line endings. This is source recovery, not verified runtime or business-data recovery.
