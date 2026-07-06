$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = "C:\Users\joel7\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

if (!(Test-Path "$ProjectDir\.env")) {
  Write-Host "Missing .env file. Copy .env.example to .env and fill in SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY."
  exit 1
}

& $Python -m pip install -r "$ProjectDir\crawler_requirements.txt"
& $Python "$ProjectDir\market_discovery_crawler.py"
