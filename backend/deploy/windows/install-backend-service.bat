@echo off
:: RootMind backend - one-click install of the Windows service.
:: Installs the background service the RootMind desktop app talks to
:: (http://127.0.0.1:8000). Double-click this file; it requests
:: Administrator rights automatically.

:: --- self-elevate to Administrator ---
net session >nul 2>&1
if %errorlevel% NEQ 0 (
    echo Requesting administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"

echo ============================================
echo RootMind backend - service installation
echo ============================================
echo.

if not exist "tools\nssm.exe" (
    echo NSSM not found, fetching and verifying it...
    powershell -NoProfile -ExecutionPolicy Bypass -File ".\fetch-nssm.ps1"
    if errorlevel 1 (
        echo [ERROR] Could not fetch NSSM.
        pause
        exit /b 1
    )
)

powershell -NoProfile -ExecutionPolicy Bypass -File ".\_service-backend.ps1" -Install
if errorlevel 1 (
    echo [ERROR] Failed to install the RootMind backend service.
    pause
    exit /b 1
)

echo.
echo Starting service...
powershell -NoProfile -ExecutionPolicy Bypass -File ".\_service-backend.ps1" -Start

echo.
echo ============================================
echo Done. The RootMind desktop app can now connect.
echo ============================================
echo   Data:   %%ProgramData%%\RootMind (config.yaml, data\index.db, logs\)
echo   Manage: powershell -File _service-backend.ps1 -Status^|-Start^|-Stop^|-Uninstall
echo.
pause
