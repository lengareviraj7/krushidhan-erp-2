@echo off
title Krushidhan ERP - Windows Setup
echo ====================================================
echo      Krushidhan Agri-Input Shop ERP Setup
echo ====================================================
echo.

:: Check if Python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not added to PATH.
    echo Please download and install Python (3.10, 3.11, or 3.12) from https://www.python.org/
    echo IMPORTANT: Make sure to check "Add Python to PATH" during installation!
    pause
    exit /b 1
)

echo [1/3] Creating Python Virtual Environment (.venv)...
python -m venv .venv
if %errorlevel% neq 0 (
    echo [ERROR] Failed to create virtual environment.
    pause
    exit /b 1
)

echo [2/3] Activating environment and upgrading pip...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip

echo [3/3] Installing dependencies from requirements.txt...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] Dependency installation failed.
    pause
    exit /b 1
)

echo.
echo ====================================================
echo Setup complete successfully!
echo You can now launch the ERP anytime using start_krushidhan.bat
echo ====================================================
pause
