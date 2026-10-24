param([switch]$DryRun)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) { throw 'Run setup.ps1 first.' }
if ($DryRun) {
    & '.\.venv\Scripts\python.exe' -m jingjing run --adapter ocr --dry-run
} else {
    & '.\.venv\Scripts\python.exe' -m jingjing run --adapter ocr
}
exit $LASTEXITCODE
