"""
Sticker Toolkit — file_loader.py
==================================
Abstrakcja wczytywania plików wejściowych.

Centralizuje:
  - detekcję typu pliku na podstawie rozszerzenia
  - konwersję do PDF (EPS → Ghostscript, SVG → cairosvg)
  - walidację wymiarów SVG z nazwy pliku

Nie zajmuje się:
  - ekstrakcją konturu (to contour.py)
  - ekstrakcją SVG clipPath (to svg_convert.py)
  - samym renderingiem rastrów (to contour.py._detect_raster)

Cel: wyciągnięcie routing'u formatów z detect_contour() do osobnej warstwy.
"""

from __future__ import annotations

import logging
import os
from enum import Enum

from modules.ghostscript_bridge import eps_to_pdf
from modules.svg_convert import svg_to_pdf, parse_size_from_filename

log = logging.getLogger(__name__)


# Obsługiwane rozszerzenia per typ pliku
RASTER_EXT = ('.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp', '.webp')
EPS_EXT = ('.eps', '.epsf')
SVG_EXT = ('.svg',)
PDF_EXT = ('.pdf', '.ai')  # AI zapisywane jako PDF


class FileType(Enum):
    """Typ pliku rozpoznany z rozszerzenia."""
    PDF = "pdf"
    EPS = "eps"
    SVG = "svg"
    RASTER = "raster"
    UNKNOWN = "unknown"


def detect_type(path: str) -> FileType:
    """Detekcja typu pliku na podstawie rozszerzenia (case-insensitive)."""
    lower = path.lower()
    if lower.endswith(RASTER_EXT):
        return FileType.RASTER
    if lower.endswith(EPS_EXT):
        return FileType.EPS
    if lower.endswith(SVG_EXT):
        return FileType.SVG
    if lower.endswith(PDF_EXT):
        return FileType.PDF
    return FileType.UNKNOWN


def to_pdf(path: str) -> tuple[str, str | None]:
    """Konwertuje plik EPS/SVG do tymczasowego PDF.

    Dla PDF/AI zwraca bez konwersji. Dla rastrowych rzuca ValueError
    (raster nie przechodzi przez pipeline PDF — ma własną ścieżkę).

    Returns:
        (pdf_path, tmp_pdf):
          - pdf_path: finalna ścieżka do PDF (oryginalna lub tmp)
          - tmp_pdf: ścieżka do tmp (do usunięcia przez konsumenta) lub None
              gdy nie było konwersji (oryginalny PDF).

    Raises:
        FileNotFoundError: gdy plik nie istnieje
        ValueError: gdy format nieobsługiwany lub raster
                    (raster ma osobną ścieżkę, nie PDF)
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Plik nie istnieje: {path}")

    ftype = detect_type(path)

    if ftype == FileType.PDF:
        return path, None

    if ftype == FileType.EPS:
        log.info("Plik EPS — konwersja do PDF przez Ghostscript")
        tmp_pdf = eps_to_pdf(path)
        return tmp_pdf, tmp_pdf

    if ftype == FileType.SVG:
        # SVG wymaga wymiarów z nazwy pliku
        size = parse_size_from_filename(path)
        if size is None:
            raise ValueError(
                f"Brak wymiarów w nazwie pliku SVG (wymagany format np. '50x50'): "
                f"{path}"
            )
        w_mm, h_mm = size
        tmp_pdf = svg_to_pdf(path, target_w_mm=w_mm, target_h_mm=h_mm)
        return tmp_pdf, tmp_pdf

    if ftype == FileType.RASTER:
        raise ValueError(
            f"Plik rastrowy ({path}) ma osobną ścieżkę przetwarzania — "
            f"użyj contour._detect_raster() zamiast to_pdf()"
        )

    raise ValueError(
        f"Nieobsługiwany format pliku. Wymagany PDF, EPS, SVG lub obraz rastrowy: "
        f"{path}"
    )


def svg_dimensions_from_name(path: str) -> tuple[float, float] | None:
    """Zwraca wymiary mm z nazwy pliku SVG lub None."""
    return parse_size_from_filename(path)
