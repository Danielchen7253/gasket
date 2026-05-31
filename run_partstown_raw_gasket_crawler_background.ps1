$ErrorActionPreference = "Stop"

$env:PARTSTOWN_RAW_LIMIT = "1000000"
$env:PARTSTOWN_RAW_MODEL_PAGE_LIMIT = "250000"
$env:PARTSTOWN_RAW_DELAY_SECONDS = "0.03"
$env:PARTSTOWN_RAW_HTTP_TIMEOUT = "20"

$python = "C:\Users\joel7\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$work = "C:\Users\joel7\Documents\Codex\2026-05-16\files-mentioned-by-the-user-us\gasket_worktree"

Set-Location $work
& $python "partstown_raw_gasket_crawler.py"
