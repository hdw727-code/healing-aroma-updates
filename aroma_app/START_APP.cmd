@echo off
setlocal
cd /d "%~dp0"
set "AROMA_APP_PATH=%~dp0"
set "AROMA_START_OPTION=%~1"
powershell.exe -NoLogo -NoProfile -Command "$basePath = $env:AROMA_APP_PATH; $startOptions = @(); if ($env:AROMA_START_OPTION -in @('--check','--no-browser')) { $startOptions = @($env:AROMA_START_OPTION) }; $candidates = @((Join-Path $basePath 'runtime\python\python.exe')); if ($env:AROMA_PYTHON) { $candidates += $env:AROMA_PYTHON }; $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue; if ($pythonCommand -and $pythonCommand.Source -notlike '*WindowsApps*') { $candidates += $pythonCommand.Source }; $candidates += ($env:USERPROFILE + '\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'); foreach ($pythonPath in $candidates) { if (Test-Path -LiteralPath $pythonPath -PathType Leaf) { & $pythonPath -I -c 'import sys, sqlite3, http.server, ssl; assert sys.version_info >= (3,10)' 2>$null; if ($LASTEXITCODE -eq 0) { & $pythonPath -I (Join-Path $basePath 'app_start.py') @startOptions; exit $LASTEXITCODE } } }; $pyCommand = Get-Command py.exe -ErrorAction SilentlyContinue; if ($pyCommand) { & $pyCommand.Source -3 -I -c 'import sys, sqlite3, http.server, ssl; assert sys.version_info >= (3,10)' 2>$null; if ($LASTEXITCODE -eq 0) { & $pyCommand.Source -3 -I (Join-Path $basePath 'app_start.py') @startOptions; exit $LASTEXITCODE } }; Start-Process -FilePath (Join-Path $basePath 'START_HERE.html'); Write-Host 'A supported Python runtime was not found. The local start guide is open. Nothing was installed.'; exit 3"
if errorlevel 1 pause
endlocal
