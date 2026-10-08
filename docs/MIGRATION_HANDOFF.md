# Gasket migration preparation

Based on existing GitHub main `03ad7db6fcc04b0d9bf5a6127fa037ccf6f2d9dc`. The original local deletion of daily_database_digest.py is not propagated; the existing tracked code is retained. Extra local scraper/worker directories remain separate and require comparison.

No live worker, database call, dependency install or production deployment was run. Build/test/lint status remains unverified; no automated test runner has been confirmed. Existing requirements.txt pins its three dependencies, while crawler dependencies are separate.

## Deletion review — second phase

The original checkout records only an uncommitted deletion of daily_database_digest.py. No deletion commit or explicit rationale was found in Git history. Latest tracked history includes f376547 (digest wording), 3957563 (digest improvement), c6de7d1 (email digest job). A textual search found no importer reference in the retained repository; external scheduled-job dependencies cannot be excluded from that absence. The migration tree retains the remote file unchanged. gasket_scraper stays separate; no crawler or real database job was executed. Render deployment branch is still unverified.
