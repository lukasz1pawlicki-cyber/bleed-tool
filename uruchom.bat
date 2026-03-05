@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo.
echo   Bleed Tool
echo   ==========
echo.

:: Sprawdz Python
python --version >nul 2>&1
if errorlevel 1 (
    echo   BLAD: Nie znaleziono Python!
    echo   Zainstaluj Python 3.10+ z https://www.python.org/downloads/
    pause
    exit /b 1
)

:: Sprawdz zaleznosci
python -c "import customtkinter, fitz, numpy, PIL" >nul 2>&1
if errorlevel 1 (
    echo   Instaluje brakujace zaleznosci...
    pip install -r requirements.txt
    echo.
)

:: Uruchom GUI
echo   Uruchamiam...
echo.
python bleed_app.py
if errorlevel 1 (
    echo.
    echo   GUI zamkniete z bledem.
    pause
)
