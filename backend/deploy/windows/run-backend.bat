@echo off
REM Runs the RootMind backend REST API in the FOREGROUND on
REM http://127.0.0.1:8000, from a source checkout, without a token. To run it
REM in the background as a Windows service, use the release build:
REM rootmind-backend.exe service install (see the README).
echo Starting RootMind backend (http://127.0.0.1:8000) ...
cd /d "%~dp0..\.."
set "PYEXE=python"
if exist ".venv\Scripts\python.exe" set "PYEXE=.venv\Scripts\python.exe"
set "PYTHONPATH=%CD%\src;%PYTHONPATH%"
"%PYEXE%" -m rootmind.main api --host 127.0.0.1 --port 8000 %*
