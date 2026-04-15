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


def pdf_to_cmyk(
    input_pdf: str,
    output_pdf: str | None = None,
    icc_path: str | None = None,
    rendering_intent: str = "RelativeColorimetric",
    preserve_spot_colors: bool = True,
    timeout_sec: int = 120,
) -> str:
    """Konwertuje PDF z RGB → CMYK przez Ghostscript (ColorConversionStrategy).

    Zachowuje wektor (nie rasteryzuje — w przeciwieństwie do starszych podejść
    z pdfwrite+DEVICE=tiff32nb). Kolory RGB są konwertowane do CMYK przy użyciu
    ICC profilu (FOGRA39). Spot colors (CutContour, FlexCut) są zachowywane
    jeśli preserve_spot_colors=True — wymagane dla linii cięcia.

    Args:
        input_pdf: ścieżka do wejściowego PDF (dowolny color space)
        output_pdf: ścieżka wyjściowa (None = tmp file)
        icc_path: ścieżka do ICC profilu CMYK (FOGRA39). None = bez explicit ICC
                  (GS użyje wbudowanego profilu Default CMYK).
        rendering_intent: "Perceptual" | "RelativeColorimetric" (default) |
                          "Saturation" | "AbsoluteColorimetric"
        preserve_spot_colors: jeśli True, spot colors (Separation) nie są
                              konwertowane do CMYK alternate (zachowane dla RIP)
        timeout_sec: limit czasu dla GS (default 120s, bo duże PDF = wolniej)

    Returns:
        ścieżka do wyjściowego PDF.

    Raises:
        FileNotFoundError: gdy input lub Ghostscript nie istnieje
        RuntimeError: gdy konwersja się nie powiodła
    """
    if not os.path.exists(input_pdf):
        raise FileNotFoundError(f"Plik PDF nie istnieje: {input_pdf}")

    gs_bin = find_ghostscript()
    if gs_bin is None:
        raise FileNotFoundError(
            "Ghostscript nie jest zainstalowany — wymagany dla RGB→CMYK konwersji."
        )

    if output_pdf is None:
        tmp = tempfile.NamedTemporaryFile(suffix="_cmyk.pdf", delete=False)
        output_pdf = tmp.name
        tmp.close()

    # Budowa command-line
    cmd = [
        gs_bin,
        "-dNOPAUSE",
        "-dBATCH",
        "-dQUIET",
        "-sDEVICE=pdfwrite",
        "-dPDFSETTINGS=/prepress",
        # Color conversion
        "-sColorConversionStrategy=CMYK",
        "-sProcessColorModel=DeviceCMYK",
        f"-sRenderingIntent={rendering_intent}",
        # Zachowaj metadane (TrimBox/BleedBox)
        "-dPreserveAnnots=true",
        "-dPreserveEPSInfo=true",
    ]

    # Spot colors: Separation colors (CutContour, FlexCut) muszą zostać
    # zachowane — RIP plotera czyta je po nazwie spot
    if preserve_spot_colors:
        cmd.extend([
            "-dOverrideICC=false",
            "-sColorConversionStrategyForImages=CMYK",
        ])

    # ICC profil — Destination dla konwersji RGB→CMYK
    if icc_path and os.path.isfile(icc_path):
        cmd.append(f"-sOutputICCProfile={icc_path}")

    cmd.extend([
        f"-sOutputFile={output_pdf}",
        input_pdf,
    ])

    log.info(
        f"Konwersja RGB → CMYK (Ghostscript): {os.path.basename(input_pdf)} "
        f"[intent={rendering_intent}, spot={preserve_spot_colors}]"
    )
    log.debug(f"Ghostscript cmd: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        if os.path.exists(output_pdf):
            os.unlink(output_pdf)
        raise RuntimeError(
            f"Konwersja RGB → CMYK przekroczyła limit czasu ({timeout_sec}s)"
        )
    except Exception as e:
        if os.path.exists(output_pdf):
            os.unlink(output_pdf)
        raise RuntimeError(f"Błąd uruchomienia Ghostscript: {e}") from e

    if result.returncode != 0:
        stderr = result.stderr.strip()
        if os.path.exists(output_pdf):
            os.unlink(output_pdf)
        raise RuntimeError(
            f"Ghostscript RGB→CMYK zakończył się błędem (kod {result.returncode}): "
            f"{stderr or '(brak szczegółów)'}"
        )

    log.info(f"RGB → CMYK OK: {output_pdf}")
    return output_pdf


def is_ghostscript_available() -> bool:
    """True jeśli Ghostscript jest dostępny w PATH."""
    return find_ghostscript() is not None
