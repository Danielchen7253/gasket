# Gasket migration preparation

Based on existing GitHub main `03ad7db6fcc04b0d9bf5a6127fa037ccf6f2d9dc`. The original local deletion of daily_database_digest.py is not propagated; the existing tracked code is retained. Extra local scraper/worker directories remain separate and require comparison.

No live worker, database call, dependency install or production deployment was run. Build/test/lint status remains unverified; no automated test runner has been confirmed. Existing requirements.txt pins its three dependencies, while crawler dependencies are separate.
