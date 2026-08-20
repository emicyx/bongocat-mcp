@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
title bongocat-mcp packager

rem ==== 1. locate / create the project virtualenv ====
set "VPY=%CD%\.venv\Scripts\python.exe"
if not exist "%VPY%" (
    echo [Setup] Creating virtual environment .venv ...
    py -3 -m venv .venv
    if errorlevel 1 (
        echo [Error] Failed to create the virtual environment.
        pause
        exit /b 1
    )
)

rem ==== 2. dependencies + pyinstaller ====
"%VPY%" -m pip show pyinstaller >nul 2>nul
if errorlevel 1 (
    echo [Setup] Installing PyInstaller - first build only ...
    "%VPY%" -m pip install "pyinstaller>=6"
    if errorlevel 1 (
        echo [Error] Failed to install PyInstaller. Check network and retry.
        pause
        exit /b 1
    )
)

rem ==== 3. build (onedir, output kept inside packaging\) ====
echo [Build] Running PyInstaller ...
"%VPY%" -m PyInstaller --noconfirm --clean ^
    --distpath "packaging\dist" --workpath "packaging\build" ^
    "packaging\bongocat-mcp.spec"
if errorlevel 1 (
    echo [Error] Build failed.
    pause
    exit /b 1
)

rem ==== 4. ship the end-user readme next to the exe ====
copy /y "packaging\app-readme.md" "packaging\dist\bongocat-mcp\README.md" >nul

rem ==== 5. zip the app folder for distribution ====
powershell -NoProfile -Command "Compress-Archive -Path 'packaging\dist\bongocat-mcp' -DestinationPath 'packaging\dist\bongocat-mcp-windows-x64.zip' -Force"
if errorlevel 1 (
    echo [Error] Failed to create the zip archive.
    pause
    exit /b 1
)

echo.
echo ==============================================
echo  Build OK
echo  App folder: packaging\dist\bongocat-mcp\
echo  Zip:        packaging\dist\bongocat-mcp-windows-x64.zip
echo ==============================================
echo Press any key to close this window...
pause >nul
