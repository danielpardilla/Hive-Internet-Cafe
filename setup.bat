@echo off
REM ============================================================
REM  ICMS-ASI — Setup Script (Windows)
REM  Run this ONCE on the Admin Laptop to install dependencies
REM ============================================================

echo.
echo  ===================================================
echo   ICMS-ASI Setup — Internet Cafe Management System
echo  ===================================================
echo.

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found! Install Python 3.10+ from python.org
    pause
    exit /b 1
)

REM Create virtual environment
echo [1/4] Creating virtual environment...
python -m venv venv
call venv\Scripts\activate.bat

REM Install dependencies
echo [2/4] Installing dependencies...
pip install -r requirements.txt

REM Copy .env if not exists
echo [3/4] Setting up environment file...
if not exist .env (
    copy .env.example .env
    echo  -> Created .env from template. Edit it to add your GEMINI_API_KEY.
) else (
    echo  -> .env already exists, skipping.
)

REM Create uploads folder
echo [4/4] Creating uploads directory...
if not exist uploads mkdir uploads

echo.
echo  ===================================================
echo   Setup complete!
echo.
echo   To START the server:
echo     venv\Scripts\activate
echo     python app.py
echo.
echo   Admin laptop URL : http://localhost:5000
echo   Client LAN URL   : http://^<ADMIN_IP^>:5000
echo.
echo   Find your IP: ipconfig ^| findstr "IPv4"
echo  ===================================================
echo.
pause
