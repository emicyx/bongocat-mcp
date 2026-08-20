@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title bongocat-mcp stopper

rem Prefer the project virtualenv; fall back to system Python when missing.
rem Both --stop commands just print a notice when nothing is running.
set "VPY=%~dp0.venv\Scripts\python.exe"
if not exist "%VPY%" set "VPY=python"

"%VPY%" dashboard.py --stop
"%VPY%" mver-mirror.py --stop

echo.
echo Press any key to close this window...
pause >nul
