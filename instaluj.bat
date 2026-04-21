@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo.
echo   Bleed Tool - Instalacja zaleznosci
echo   ====================================
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

python --version
echo.

echo   Aktualizuje pip...
python -m pip install --upgrade pip
echo.

echo   Instaluje/aktualizuje wszystkie zaleznosci z requirements.txt...
echo   (PyMuPDF, numpy, scipy, Pillow, PyQt6, cairosvg, opencv, svglib, reportlab)
echo.
python -m pip install --upgrade -r requirements.txt
if errorlevel 1 (
    echo.
    echo   BLAD: Instalacja nie powiodla sie!
    echo   Sprawdz polaczenie internetowe i sprobuj ponownie.
    echo.
    pause
    exit /b 1
)

echo.
echo   Weryfikacja instalacji...
python -c "import PyQt6, fitz, numpy, scipy, PIL, cv2, cairosvg; print('  [OK] PyQt6', PyQt6.QtCore.PYQT_VERSION_STR); print('  [OK] PyMuPDF', fitz.version[0]); print('  [OK] numpy', numpy.__version__); print('  [OK] scipy', scipy.__version__); print('  [OK] Pillow', PIL.__version__); print('  [OK] opencv', cv2.__version__); print('  [OK] cairosvg', cairosvg.__version__)"
if errorlevel 1 (
    echo.
    echo   OSTRZEZENIE: Niektore biblioteki nie zaladowaly sie poprawnie.
    echo   Sprobuj uruchomic jeszcze raz lub skontaktuj sie z deweloperem.
    echo.
    pause
    exit /b 1
)

echo.
echo   ====================================
echo   Gotowe! Mozesz uruchomic program:
echo     uruchom.bat
echo   ====================================
echo.
pause
