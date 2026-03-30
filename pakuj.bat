@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo.
echo   Bleed Tool — Tworzenie paczki dla testera
echo   ============================================
echo.

set DIST=dist\bleed-tool

:: Usun poprzednia paczke
if exist "%DIST%" (
    echo   Usuwam poprzednia paczke...
    rmdir /s /q "%DIST%"
)
mkdir "%DIST%"
mkdir "%DIST%\modules"

:: Kopiuj pliki Python
echo   Kopiuje pliki...
copy /y bleed_app.py "%DIST%\" >nul
copy /y bleed_cli.py "%DIST%\" >nul
copy /y config.py "%DIST%\" >nul
copy /y models.py "%DIST%\" >nul

:: Kopiuj wszystkie moduly
for %%f in (modules\*.py) do copy /y "%%f" "%DIST%\modules\" >nul

:: Kopiuj launchery i docs
copy /y uruchom.bat "%DIST%\" >nul
copy /y uruchom.command "%DIST%\" >nul
copy /y requirements.txt "%DIST%\" >nul
copy /y INSTALACJA.txt "%DIST%\" >nul

echo.
echo   ====================================
echo   Gotowe! Paczka w folderze: %DIST%\
echo   ====================================
echo.
echo   Zawartosc:
dir /b "%DIST%"
echo.
echo   modules\:
dir /b "%DIST%\modules"
echo.
echo   Skopiuj folder "%DIST%" na pendrive lub spakuj do ZIP.
echo   Na komputerze testera: dwuklik uruchom.bat
echo.

pause
