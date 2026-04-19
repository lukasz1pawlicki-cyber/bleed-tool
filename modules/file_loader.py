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
from modules.svg_convert import svg_to_pdf, parse_size_from_filename, _get_viewbox_size

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
        # SVG — preferuj wymiary z nazwy pliku. Gdy brak, użyj domyślnego
        # rozmiaru na podstawie aspect ratio z viewBox/intrinsic dimensions.
        # Operator może zmienić rozmiar w panelu ustawień przed eksportem.
        size = parse_size_from_filename(path)
        if size is None:
            size = _svg_default_dimensions_mm(path)
            w_mm, h_mm = size
            log.info(
                f"SVG bez wymiarów w nazwie — użyto domyślnego rozmiaru "
                f"{w_mm:.1f}x{h_mm:.1f}mm (aspect ratio z viewBox). "
                f"Dostosuj w panelu ustawień jeśli trzeba."
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


# Domyślny maksymalny wymiar SVG w mm gdy brak wymiarów w nazwie pliku.
# 80mm to typowa wielkość naklejki — operator i tak dostosowuje rozmiar
# w panelu ustawień GUI.
_DEFAULT_SVG_MAX_MM = 80.0


def _svg_default_dimensions_mm(svg_path: str) -> tuple[float, float]:
    """Oblicza domyślny rozmiar SVG w mm z aspect ratio viewBox.

    Używany gdy nazwa pliku SVG nie zawiera wymiarów (np. '50x50').
    Dłuższy bok ustawiany na _DEFAULT_SVG_MAX_MM, krótszy wg aspect ratio.
    Operator może zmienić rozmiar w panelu ustawień GUI.

    Fallback (gdy nie można odczytać viewBox): kwadrat _DEFAULT_SVG_MAX_MM.
    """
    vb_w, vb_h = _get_viewbox_size(svg_path)
    if not vb_w or not vb_h or vb_w <= 0 or vb_h <= 0:
        return (_DEFAULT_SVG_MAX_MM, _DEFAULT_SVG_MAX_MM)
    aspect = vb_w / vb_h
    if aspect >= 1.0:
        # Landscape/square — szerokość = max
        return (_DEFAULT_SVG_MAX_MM, _DEFAULT_SVG_MAX_MM / aspect)
    # Portrait — wysokość = max
    return (_DEFAULT_SVG_MAX_MM * aspect, _DEFAULT_SVG_MAX_MM)
