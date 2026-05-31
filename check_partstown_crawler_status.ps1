$ErrorActionPreference = "Stop"

$work = "C:\Users\joel7\Documents\Codex\2026-05-16\files-mentioned-by-the-user-us\gasket_worktree"
$reportPath = Join-Path $work "partstown_raw_gasket_crawler_report.json"
$logPath = Join-Path $work "partstown_raw_gasket_crawler.log"

Set-Location $work

$crawlerProcesses = Get-Process -Name python,powershell -ErrorAction SilentlyContinue |
    Where-Object { $_.StartTime -ge (Get-Date).AddDays(-1) }

if (-not (Test-Path $reportPath)) {
    Write-Host "No report file found."
    exit 1
}

$before = Get-Content -Path $reportPath -Raw | ConvertFrom-Json
Start-Sleep -Seconds 30
$after = Get-Content -Path $reportPath -Raw | ConvertFrom-Json
$reportFile = Get-Item $reportPath

Write-Host "PartsTown crawler status"
Write-Host "------------------------"
Write-Host ("Process running: {0}" -f ($(if ($crawlerProcesses) { "YES" } else { "NO" })))
if ($crawlerProcesses) {
    $crawlerProcesses | Select-Object Id, ProcessName, StartTime, CPU | Format-Table -AutoSize
}

Write-Host ("Report last write: {0}" -f $reportFile.LastWriteTime)
Write-Host ("Models scanned: {0} -> {1}  (+{2})" -f $before.models_scanned, $after.models_scanned, ($after.models_scanned - $before.models_scanned))
Write-Host ("Raw rows found: {0} -> {1}  (+{2})" -f $before.raw_rows_found, $after.raw_rows_found, ($after.raw_rows_found - $before.raw_rows_found))
Write-Host ("Raw rows written: {0} -> {1}  (+{2})" -f $before.raw_rows_written, $after.raw_rows_written, ($after.raw_rows_written - $before.raw_rows_written))
Write-Host ("Errors: {0} -> {1}" -f $before.errors, $after.errors)

if (($after.models_scanned -gt $before.models_scanned) -or ($after.raw_rows_found -gt $before.raw_rows_found)) {
    Write-Host "Judgement: NORMAL - it is working."
} elseif ($crawlerProcesses) {
    Write-Host "Judgement: POSSIBLY WAITING/STUCK - process exists but counters did not move in 30 seconds."
} else {
    Write-Host "Judgement: STOPPED - crawler process is not running."
}

if (Test-Path $logPath) {
    Write-Host ""
    Write-Host "Recent log:"
    Get-Content -Path $logPath -Tail 10
}
