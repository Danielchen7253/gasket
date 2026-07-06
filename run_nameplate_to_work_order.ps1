$ErrorActionPreference = "Stop"

# Emergency-only utility, not part of the standard customer flow (upload → recognition → match → display).

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = "C:\Users\joel7\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

if ($args.Count -lt 1) {
  Write-Host "Usage: .\run_nameplate_to_work_order.ps1 <image_path> [--customer CustomerName] [--notes Notes]"
  exit 1
}

& $Python -m pip install -r "$ProjectDir\crawler_requirements.txt"
& $Python "$ProjectDir\nameplate_to_work_order.py" @args
