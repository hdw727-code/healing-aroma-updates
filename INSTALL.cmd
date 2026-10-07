@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0INSTALL_APP.ps1" %*
set "aroma_install_result=%ERRORLEVEL%"
echo.
pause
exit /b %aroma_install_result%
