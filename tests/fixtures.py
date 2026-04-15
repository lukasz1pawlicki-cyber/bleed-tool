"""Generatory fixtur PDF/PNG dla testow integracyjnych.

Fixtury sa tworzone w pamieci i zapisywane do tmp_path.
Nie wprowadzamy binarnych plikow do repo.
"""
from __future__ import annotations

import os
from pathlib import Path

import fitz
import numpy as np
from PIL import Image

from config import MM_TO_PT


def make_rectangle_vector(tmp_path: Path, w_mm: float = 80, h_mm: float = 50,
                           color: tuple = (0.2, 0.5, 0.9)) -> str:
    """Prosta prostokatna naklejka wektorowa (solid fill).

    MediaBox = strona = naklejka (brak TrimBox).
    """
    doc = fitz.open()
    w_pt = w_mm * MM_TO_PT
    h_pt = h_mm * MM_TO_PT
    page = doc.new_page(width=w_pt, height=h_pt)
    shape = page.new_shape()
    shape.draw_rect(fitz.Rect(0, 0, w_pt, h_pt))
    shape.finish(fill=color, color=color)
    shape.commit()
    path = tmp_path / "rectangle_vector.pdf"
    doc.save(str(path))
    doc.close()
    return str(path)


def make_circle_on_artboard(tmp_path: Path, page_w_mm: float = 200, page_h_mm: float = 200,
                             circle_r_mm: float = 30,
                             color: tuple = (0.9, 0.2, 0.2)) -> str:
    """Okragla naklejka wewnatrz wiekszej strony (artwork-on-artboard).

    Strona 200x200mm, okrag o R=30mm w srodku.
    """
    doc = fitz.open()
    page_w_pt = page_w_mm * MM_TO_PT
    page_h_pt = page_h_mm * MM_TO_PT
    page = doc.new_page(width=page_w_pt, height=page_h_pt)

    cx = page_w_pt / 2
    cy = page_h_pt / 2
    r = circle_r_mm * MM_TO_PT

    shape = page.new_shape()
    shape.draw_circle(fitz.Point(cx, cy), r)
    shape.finish(fill=color, color=color)
    shape.commit()

    path = tmp_path / "circle_on_artboard.pdf"
    doc.save(str(path))
    doc.close()
    return str(path)


def make_pdf_with_trimbox(tmp_path: Path, trim_w_mm: float = 60, trim_h_mm: float = 40,
                           existing_bleed_mm: float = 3) -> str:
    """PDF z istniejacym TrimBox (plik ze spadami, np. z Canva).

    MediaBox = TrimBox + 2*existing_bleed na kazda strone.
    """
    doc = fitz.open()
    trim_w_pt = trim_w_mm * MM_TO_PT
    trim_h_pt = trim_h_mm * MM_TO_PT
    bleed_pt = existing_bleed_mm * MM_TO_PT
    media_w = trim_w_pt + 2 * bleed_pt
    media_h = trim_h_pt + 2 * bleed_pt

    page = doc.new_page(width=media_w, height=media_h)

    # Grafika wypelnia TRIM area
    shape = page.new_shape()
    shape.draw_rect(fitz.Rect(bleed_pt, bleed_pt, bleed_pt + trim_w_pt, bleed_pt + trim_h_pt))
    shape.finish(fill=(0.1, 0.7, 0.3), color=(0.1, 0.7, 0.3))
    shape.commit()

    path = tmp_path / "with_trimbox.pdf"
    doc.save(str(path))
    doc.close()

    # Ustaw TrimBox w xref (PyMuPDF nie ma bezposredniego API)
    doc2 = fitz.open(str(path))
    page_xref = doc2[0].xref
    doc2.xref_set_key(
        page_xref, "TrimBox",
        f"[{bleed_pt:.4f} {bleed_pt:.4f} {bleed_pt + trim_w_pt:.4f} {bleed_pt + trim_h_pt:.4f}]"
    )
    doc2.save(str(path), incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
    doc2.close()
    return str(path)


def make_irregular_alpha_png(tmp_path: Path, size_px: int = 600,
                              shape_color: tuple = (200, 50, 50, 255)) -> str:
    """PNG z nieregularnym ksztaltem na przezroczystym tle (RGBA).

    Rysuje gwiazde/wielokat z widoczna treścia.
    """
    img = Image.new("RGBA", (size_px, size_px), (0, 0, 0, 0))

    # Wielokat (5-ramienna gwiazda)
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)

    cx, cy = size_px / 2, size_px / 2
    r_outer = size_px * 0.4
    r_inner = r_outer * 0.5

    points = []
    import math
    for i in range(10):
        angle = math.pi / 2 + i * math.pi / 5
        r = r_outer if i % 2 == 0 else r_inner
        x = cx + r * math.cos(angle)
        y = cy - r * math.sin(angle)
        points.append((x, y))

    draw.polygon(points, fill=shape_color)

    path = tmp_path / "irregular_alpha.png"
    img.save(str(path), dpi=(300, 300))
    return str(path)


def make_simple_raster(tmp_path: Path, w_px: int = 900, h_px: int = 600,
                        color: tuple = (80, 140, 200)) -> str:
    """Prostokatne zdjecie JPG bez alpha (RGB)."""
    arr = np.full((h_px, w_px, 3), color, dtype=np.uint8)
    # Delikatny gradient dla urozmaicenia
    for y in range(h_px):
        factor = y / h_px
        arr[y, :, 0] = int(color[0] * (1 - factor * 0.3))
    img = Image.fromarray(arr)
    path = tmp_path / "simple_raster.jpg"
    img.save(str(path), dpi=(300, 300), quality=92)
    return str(path)


def make_multipage_pdf(tmp_path: Path, pages: int = 3,
                       w_mm: float = 60, h_mm: float = 40) -> str:
    """Wielostronicowy PDF, kazda strona — prostokatna naklejka w innym kolorze."""
    doc = fitz.open()
    w_pt = w_mm * MM_TO_PT
    h_pt = h_mm * MM_TO_PT

    colors = [
        (0.9, 0.1, 0.1),  # czerwony
        (0.1, 0.7, 0.2),  # zielony
        (0.2, 0.3, 0.9),  # niebieski
        (0.9, 0.7, 0.1),  # zolty
    ]

    for i in range(pages):
        page = doc.new_page(width=w_pt, height=h_pt)
        shape = page.new_shape()
        shape.draw_rect(fitz.Rect(0, 0, w_pt, h_pt))
        c = colors[i % len(colors)]
        shape.finish(fill=c, color=c)
        shape.commit()

    path = tmp_path / "multipage.pdf"
    doc.save(str(path))
    doc.close()
    return str(path)
