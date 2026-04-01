"""
Sticker Toolkit — ghostscript_bridge.py
========================================
Konwersja EPS → PDF za pomocą Ghostscript.

Ghostscript musi być zainstalowany w systemie:
  - macOS:   brew install ghostscript
  - Linux:   apt install ghostscript
  - Windows: https://ghostscript.com/releases/gsdnld.html

Funkcje:
  - find_ghostscript() → ścieżka do binarki GS lub None
  - is_eps()           → czy plik to EPS
  - eps_to_pdf()       → konwersja EPS → PDF (temp file)
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import tempfile

log = logging.getLogger(__name__)

# Rozszerzenia EPS
_EPS_EXT = ('.eps', '.epsf')


def find_ghostscript() -> str | None:
    """Szuka binarki Ghostscript w PATH.

    Sprawdza: gs (macOS/Linux), gswin64c, gswin32c (Windows).

    Returns:
        Ścieżka do binarki lub None jeśli nie znaleziono.
    """
    candidates = ['gs', 'gswin64c', 'gswin32c']
    if platform.system() == 'Windows':
        # Na Windows priorytet dla wersji 64-bit
        candidates = ['gswin64c', 'gswin32c', 'gs']

    for name in candidates:
        path = shutil.which(name)
        if path:
            log.debug(f"Ghostscript znaleziony: {path}")
            return path

    log.debug("Ghostscript nie znaleziony w PATH")
    return None


def is_eps(file_path: str) -> bool:
    """Sprawdza czy plik ma rozszerzenie EPS (.eps lub .epsf).

    Args:
        file_path: ścieżka do pliku

    Returns:
        True jeśli plik to EPS
    """
    return file_path.lower().endswith(_EPS_EXT)


def eps_to_pdf(eps_path: str) -> str:
    """Konwertuje plik EPS na PDF za pomocą Ghostscript.

    Używa -dEPSCrop do przycięcia do BoundingBox (bez nadmiaru białego tła).
    Wynik zapisywany do pliku tymczasowego.

    Args:
        eps_path: ścieżka do pliku EPS

    Returns:
        Ścieżka do tymczasowego pliku PDF (delete=False — konsument odpowiada
        za usunięcie).

    Raises:
        FileNotFoundError: jeśli plik EPS nie istnieje lub Ghostscript
                           nie jest zainstalowany
        RuntimeError: jeśli konwersja się nie powiodła (non-zero exit code)
    """
    if not os.path.exists(eps_path):
        raise FileNotFoundError(f"Plik EPS nie istnieje: {eps_path}")

    gs_bin = find_ghostscript()
    if gs_bin is None:
        raise FileNotFoundError(
            "Ghostscript nie jest zainstalowany. "
            "Zainstaluj: brew install ghostscript (macOS) / "
            "apt install ghostscript (Linux) / "
            "https://ghostscript.com/releases/gsdnld.html (Windows)"
        )

    # Plik tymczasowy na wynik
    tmp = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
    tmp_path = tmp.name
    tmp.close()

    cmd = [
        gs_bin,
        '-dNOPAUSE',
        '-dBATCH',
        '-dQUIET',
        '-sDEVICE=pdfwrite',
        '-dEPSCrop',
        f'-sOutputFile={tmp_path}',
        eps_path,
    ]

    log.info(f"Konwersja EPS → PDF: {os.path.basename(eps_path)}")
    log.debug(f"Ghostscript cmd: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        # Sprzątanie pliku tymczasowego
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise RuntimeError(
            f"Konwersja EPS → PDF przekroczyła limit czasu (60s): {eps_path}"
        )
    except Exception as e:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise RuntimeError(f"Błąd uruchomienia Ghostscript: {e}") from e

    if result.returncode != 0:
        stderr = result.stderr.strip()
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise RuntimeError(
            f"Ghostscript zakończył się błędem (kod {result.returncode}): "
            f"{stderr or '(brak szczegółów)'}"
        )

    log.info(f"EPS → PDF OK: {tmp_path}")
    return tmp_path
