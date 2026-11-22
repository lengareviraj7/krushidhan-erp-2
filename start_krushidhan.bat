@echo off
title Krushidhan Agri-Input Shop ERP
cd /d "%~dp0"

:: Check if virtual environment exists
if not exist ".venv\Scripts\activate.bat" (
    echo [INFO] Virtual environment not found. Running initial setup...
    call setup_windows.bat
)

echo ====================================================
echo Starting Krushidhan Agri-Input Shop ERP...
echo Server running at http://127.0.0.1:8008
echo (Leave this window open while using the software)
echo ====================================================
echo.

call .venv\Scripts\activate.bat
python run.py

if %errorlevel% neq 0 (
    echo.
    echo Application stopped unexpectedly with error code %errorlevel%.
    pause
)
