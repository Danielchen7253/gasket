# Local project source preservation — 2026-11-09

This is a source-only backup on codex/local-project-migration-2026-11-09. The repository root retains the existing remote baseline. New local source is archived rather than deployed.

SOURCE_MANIFEST.json maps original local paths to saved files and records credential/contact sanitization. SOURCE_SNAPSHOTS.json records each dirty Git workspace base HEAD, changed/untracked paths and tracked missing files. To reconstruct a workspace, create a separate clone at its recorded local HEAD (where available remotely), then apply its archived snapshot overlay. Missing-file records describe the original workspace and do not authorize deleting files. Unchanged files remain in existing GitHub history.

Historical unique tools are under unique/. Runtime data, customer exports, real .env, credentials, encrypted backups, browser profiles, dependencies and generated build outputs are intentionally excluded. Any required private configuration must be supplied securely. No application tests/builds, messages, crawls, database migrations or deployments were executed.


## Complete recovery modes

For recovery_mode=base_plus_overlay, clone the verified base HEAD and copy the snapshot overlay to its root. For recovery_mode=full_source_snapshot, the archived directory contains the current application text/config source because the local base commit was not reachable from GitHub. Restore remaining unchanged assets via ASSET_REFERENCES.json using the referenced repository Git blob. Newly preserved brand/icon assets have stored_at paths. Private runtime configuration and business data require separate protected backups. No production acceptance is implied.
