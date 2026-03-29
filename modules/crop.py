"""
Bleed Tool — Crop (przycinanie grafiki do rozmiaru)
=====================================================
Przycinanie plików graficznych do zadanego rozmiaru (kwadrat/okrąg)
z możliwością przesunięcia (offset).
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import fitz
import numpy as np
from PIL import Image, ImageDraw

from config import DEFAULT_CROP_DPI

log = logging.getLogger("bleed-tool")

# Obsługiwane formaty rastrowe
_RASTER_EXT = {'.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp', '.webp'}


def apply_crop(
    file_path: str,
    target_size_mm: float,
    shape: str = "square",
    offset: tuple[float, float] = (0.5, 0.5),
    dpi: int = DEFAULT_CROP_DPI,
) -> str:
    """Przycina plik graficzny do kwadratu/okręgu o zadanym rozmiarze.

    Obraz jest skalowany tak, aby pokrył cały obszar crop (cover, nie contain).
    Offset (0-1, 0-1) definiuje pozycję — 0.5 = wycentrowany.

    Args:
        file_path: ścieżka do pliku źródłowego (PDF/SVG/PNG/JPG/...)
        target_size_mm: docelowy rozmiar w mm (kwadrat: bok, okrąg: średnica)
        shape: "square" lub "circle"
        offset: (x_ratio, y_ratio) — 0.0=lewo/góra, 0.5=środek, 1.0=prawo/dół
        dpi: rozdzielczość wyjściowa (domyślnie 300)

    Returns:
        Ścieżka do pliku tymczasowego z przyciętą grafiką (PNG)
    """
    if target_size_mm <= 0:
        raise ValueError(f"target_size_mm musi byc > 0, podano {target_size_mm}")
    if shape not in ("square", "circle", "rounded"):
        raise ValueError(f"Nieznany kształt crop: {shape}")
    if not (0.0 <= offset[0] <= 1.0 and 0.0 <= offset[1] <= 1.0):
        offset = (max(0.0, min(1.0, offset[0])), max(0.0, min(1.0, offset[1])))

    # Rozmiar crop w pikselach
    crop_size_px = int(round(target_size_mm / 25.4 * dpi))
    if crop_size_px < 1:
        crop_size_px = 1

    # Wczytaj obraz źródłowy jako PIL Image
    src_img = _load_source_image(file_path, dpi)

    # Skaluj aby pokryć crop area (cover) — ZAWSZE resize
    # aby proporcje panowania odpowiadały podglądowi canvas
    src_w, src_h = src_img.size
    scale = max(crop_size_px / src_w, crop_size_px / src_h)

    new_w = max(crop_size_px, int(round(src_w * scale)))
    new_h = max(crop_size_px, int(round(src_h * scale)))
    if new_w != src_w or new_h != src_h:
        src_img = src_img.resize((new_w, new_h), Image.LANCZOS)

    # Oblicz pozycję crop z offsetu
    pan_x = max(0, new_w - crop_size_px)
    pan_y = max(0, new_h - crop_size_px)
    x0 = int(round(offset[0] * pan_x))
    y0 = int(round(offset[1] * pan_y))

    # Clamp
    x0 = max(0, min(x0, new_w - crop_size_px))
    y0 = max(0, min(y0, new_h - crop_size_px))

    # Crop
    cropped = src_img.crop((x0, y0, x0 + crop_size_px, y0 + crop_size_px))
    src_img.close()

    # Dla okręgu/zaokrąglonego: dodaj alpha maskę
    if shape == "circle":
        cropped = _apply_circle_mask(cropped)
    elif shape == "rounded":
        cropped = _apply_rounded_rect_mask(cropped)

    # Zapisz do pliku tymczasowego
    tmp = tempfile.NamedTemporaryFile(
        suffix=".png", prefix="crop_", delete=False,
    )
    tmp.close()
    cropped.save(tmp.name, "PNG", dpi=(dpi, dpi))
    cropped.close()

    log.info(
        f"Crop: {os.path.basename(file_path)} → {crop_size_px}x{crop_size_px}px "
        f"({shape}, offset=({offset[0]:.2f},{offset[1]:.2f}), {dpi}DPI)"
    )
    return tmp.name


def load_preview_image(
    file_path: str,
    max_size: int = 400,
) -> Image.Image:
    """Wczytuje obraz do podglądu (niskie DPI, max_size px).

    Args:
        file_path: ścieżka do pliku
        max_size: maksymalny wymiar w px

    Returns:
        PIL Image w trybie RGB
    """
    ext = Path(file_path).suffix.lower()

    if ext in _RASTER_EXT:
        img = Image.open(file_path).convert("RGB")
    elif ext == '.svg':
        try:
            import cairosvg
            import io
            png_data = cairosvg.svg2png(url=file_path, output_width=max_size)
            img = Image.open(io.BytesIO(png_data)).convert("RGB")
            return img
        except Exception:
            return Image.new("RGB", (max_size, max_size), (200, 200, 200))
    else:
        # PDF / AI
        try:
            doc = fitz.open(file_path)
            page = doc[0]
            zoom = max_size / max(page.rect.width, page.rect.height)
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            doc.close()
            return img
        except Exception:
            return Image.new("RGB", (max_size, max_size), (200, 200, 200))

    # Skaluj do max_size
    w, h = img.size
    if max(w, h) > max_size:
        scale = max_size / max(w, h)
        img = img.resize(
            (int(w * scale), int(h * scale)),
            Image.LANCZOS,
        )
    return img


def _load_source_image(file_path: str, dpi: int) -> Image.Image:
    """Wczytuje plik źródłowy jako PIL Image (RGB lub RGBA)."""
    ext = Path(file_path).suffix.lower()

    if ext in _RASTER_EXT:
        img = Image.open(file_path)
        if img.mode not in ('RGB', 'RGBA'):
            img = img.convert('RGB')
        return img

    if ext == '.svg':
        try:
            import cairosvg
            import io
            # Render SVG na docelowe DPI (szerokie)
            png_data = cairosvg.svg2png(url=file_path, dpi=dpi)
            img = Image.open(io.BytesIO(png_data))
            if img.mode not in ('RGB', 'RGBA'):
                img = img.convert('RGB')
            return img
        except Exception as e:
            raise ValueError(f"Nie udalo sie wczytac SVG: {e}")

    # PDF / AI
    try:
        doc = fitz.open(file_path)
        page = doc[0]
        pix_per_pt = dpi / 72.0
        mat = fitz.Matrix(pix_per_pt, pix_per_pt)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        doc.close()
        return img
    except Exception as e:
        raise ValueError(f"Nie udalo sie wczytac PDF: {e}")


def _apply_circle_mask(img: Image.Image) -> Image.Image:
    """Nakłada okrągłą maskę alpha na kwadratowy obraz."""
    size = img.size[0]  # kwadratowy
    rgba = img.convert("RGBA")

    # Utwórz maskę: białe koło na czarnym tle
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size - 1, size - 1), fill=255)

    rgba.putalpha(mask)
    return rgba


def _apply_rounded_rect_mask(img: Image.Image) -> Image.Image:
    """Nakłada maskę zaokrąglonego kwadratu na obraz. Promień = 15% boku."""
    size = img.size[0]  # kwadratowy
    rgba = img.convert("RGBA")
    r = int(size * 0.15)

    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=r, fill=255)

    rgba.putalpha(mask)
    return rgba
