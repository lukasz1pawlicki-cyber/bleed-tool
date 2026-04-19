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
mkdir "%DIST%\gui"
mkdir "%DIST%\gui\resources"
mkdir "%DIST%\profiles"

:: Kopiuj pliki Python root
echo   Kopiuje pliki...
copy /y bleed_app.py "%DIST%\" >nul
copy /y bleed_cli.py "%DIST%\" >nul
copy /y config.py "%DIST%\" >nul
copy /y models.py "%DIST%\" >nul

:: Kopiuj wszystkie moduly (w tym crop_marks.py, svg_convert.py etc)
for %%f in (modules\*.py) do copy /y "%%f" "%DIST%\modules\" >nul

:: Kopiuj GUI (wszystkie .py + wszystkie resources)
for %%f in (gui\*.py) do copy /y "%%f" "%DIST%\gui\" >nul
for %%f in (gui\resources\*.qss) do copy /y "%%f" "%DIST%\gui\resources\" >nul

:: Kopiuj profiles (output profiles dla ploterow Summa/JWEI)
if exist "profiles\output_profiles.json" (
    copy /y profiles\output_profiles.json "%DIST%\profiles\" >nul
)
if exist "profiles\CoatedFOGRA39.icc" (
    copy /y profiles\CoatedFOGRA39.icc "%DIST%\profiles\" >nul
)

:: Kopiuj launchery i docs
copy /y uruchom.bat "%DIST%\" >nul
copy /y uruchom.command "%DIST%\" >nul
copy /y requirements.txt "%DIST%\" >nul
copy /y INSTALACJA.txt "%DIST%\" >nul
if exist CLAUDE.md copy /y CLAUDE.md "%DIST%\" >nul

:: ZIP paczke (powershell 5.1+ na Windows 10/11 ma Compress-Archive natywnie)
echo.
echo   Pakuje do ZIP...
set ZIP=dist\bleed-tool.zip
if exist "%ZIP%" del /q "%ZIP%"
powershell -NoProfile -Command "Compress-Archive -Path '%DIST%\*' -DestinationPath '%ZIP%' -CompressionLevel Optimal"
if errorlevel 1 (
    echo   OSTRZEZENIE: ZIP nie utworzono. Folder %DIST% zawiera kompletne pliki.
) else (
    for %%I in ("%ZIP%") do set ZIPSIZE=%%~zI
    echo   ZIP: %ZIP%
)

echo.
echo   ====================================
echo   Gotowe!
echo   ====================================
echo.
echo   Paczka w:
echo     %DIST%\           (folder, mozesz skopiowac na pendrive)
echo     %ZIP%             (ZIP — gotowe do wyslania)
echo.
echo   Zawartosc:
dir /b "%DIST%"
echo.
echo   modules\:
dir /b "%DIST%\modules"
echo.
echo   gui\:
dir /b "%DIST%\gui"
echo.
echo   profiles\:
dir /b "%DIST%\profiles" 2>nul
echo.
echo   Na komputerze testera: rozpakuj ZIP, dwuklik uruchom.bat
echo.

pause
