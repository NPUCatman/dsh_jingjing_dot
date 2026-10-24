$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create Python virtual environment.' }
}
& '.\.venv\Scripts\python.exe' -m pip install -r requirements-wechat.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Please keep this error output.' }
if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
if (-not (Test-Path -LiteralPath 'config.json')) { Copy-Item -LiteralPath 'config.example.json' -Destination 'config.json' }
& '.\.venv\Scripts\python.exe' -m jingjing check
exit $LASTEXITCODE
