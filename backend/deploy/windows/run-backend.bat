@echo off
REM Runs the RootMind backend REST API in the FOREGROUND on
REM http://127.0.0.1:8000 so the desktop app can connect. For a permanent
REM auto-start setup, use install-backend-service.bat instead.
echo Starting RootMind backend (http://127.0.0.1:8000) ...
cd /d "%~dp0..\.."
set "PYEXE=python"
if exist ".venv\Scripts\python.exe" set "PYEXE=.venv\Scripts\python.exe"
set "PYTHONPATH=%CD%\src;%PYTHONPATH%"
"%PYEXE%" -m rootmind.main api --host 127.0.0.1 --port 8000 %*
