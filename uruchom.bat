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
    echo   WAZNE: Zaznacz "Add Python to PATH" podczas instalacji!
    echo.
    pause
    exit /b 1
)

:: Sprawdz zaleznosci (w tym scipy — wymagane przez outer bleed w FlexCut)
python -c "import PyQt6, fitz, numpy, scipy, PIL, cv2, cairosvg" >nul 2>&1
if errorlevel 1 (
    echo   Instaluje brakujace zaleznosci...
    echo.
    python -m pip install --upgrade pip >nul 2>&1
    python -m pip install --upgrade -r requirements.txt
    echo.
    :: Sprawdz ponownie
    python -c "import PyQt6, fitz, numpy, scipy, PIL, cv2, cairosvg" >nul 2>&1
    if errorlevel 1 (
        echo   BLAD: Instalacja bibliotek nie powiodla sie!
        echo   Sprobuj recznie:  python -m pip install -r requirements.txt
        echo.
        pause
        exit /b 1
    )
    echo   Biblioteki zainstalowane pomyslnie.
    echo.
)

:: Uruchom GUI
echo   Uruchamiam...
echo.
python bleed_app.py
if errorlevel 1 (
    echo.
    echo   BLAD: Program zakonczyl sie z bledem!
    echo.
    pause
    exit /b 1
)
exit
