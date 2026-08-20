@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title bongocat-mcp launcher

rem ==== 1. locate Python ====
set "PYCMD="
where py >nul 2>nul && set "PYCMD=py -3"
if not defined PYCMD (
    where python >nul 2>nul && set "PYCMD=python"
)
if not defined PYCMD (
    echo [Error] Python not found. Install Python 3.10+ and add it to PATH.
    pause
    exit /b 1
)

rem ==== 2. virtualenv + dependencies (first run only) ====
if not exist ".venv\Scripts\python.exe" (
    echo [Setup] Creating virtual environment .venv ...
    %PYCMD% -m venv .venv
    if errorlevel 1 (
        echo [Error] Failed to create the virtual environment.
        pause
        exit /b 1
    )
)
set "VPY=%~dp0.venv\Scripts\python.exe"
if not exist ".venv\.deps-ok" (
    echo [Setup] Installing dependencies - first run only, about 1-2 min ...
    "%VPY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [Error] Failed to install dependencies. Check network and retry.
        pause
        exit /b 1
    )
    type nul > ".venv\.deps-ok"
)

rem ==== 3. default config ====
if not exist "config.json" (
    copy /y "config.example.json" "config.json" >nul
    echo [Note] Created default config.json - edit it later in the dashboard.
)

rem ==== 4. start dashboard ====
rem dashboard.py relaunches itself as a hidden background process and opens
rem the browser automatically; running this script twice will not double-start it.
"%VPY%" dashboard.py

echo.
echo ==============================================
echo  bongocat-mcp is running in the background
echo  Dashboard: http://127.0.0.1:8766 (opens automatically)
echo  Stop:      run stop.bat  (or: python dashboard.py --stop)
echo  Note:      MCP clients (astrbot / ZCode / Claude /
echo             Codex plugins) spawn server.py themselves.
echo ==============================================
echo Press any key to close this window (background keeps running)...
pause >nul
