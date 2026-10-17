@echo off
rem saywell end-to-end test on Windows, one command: see scripts\windows\e2e.ps1 for the parameters.
rem Runs Windows PowerShell 5.1 (always installed); the script also runs under pwsh 7.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0e2e.ps1" %*
exit /b %ERRORLEVEL%
