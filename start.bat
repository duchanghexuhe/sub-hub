@echo off
rem ASCII-only launcher: all logic and messages live in scripts\start.ps1
rem (cmd mis-parses multi-byte UTF-8 batch content, so this shim stays ASCII;
rem  PowerShell 5.1 reads the BOM'd UTF-8 ps1 correctly)
setlocal
cd /d "%~dp0"
where powershell >nul 2>&1
if errorlevel 1 (
    echo [ERROR] PowerShell not found.
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" %*
set "EC=%ERRORLEVEL%"
if not "%EC%"=="0" pause
exit /b %EC%
