"""Regresja: maska strefy bleedu dla from_source_cutpath=True nie ma dziur.

Historia buga: dla ścieżki z checkboxem "użyj linii cięcia z pliku",
poprzednia implementacja rasteryzowała bleed_segments (offset polygon)
przez PIL.ImageDraw.polygon z domyślną regułą even-odd. `offset_segments()`
w `modules/bleed.py` na wypukłych narożnikach produkuje self-intersection
loops (pętle refit Béziera) — polygon rysowany even-odd tworzył DZIURY
w masce w kształcie gwiazdek → użytkownik widział białe "iskierki"
w strefie bleedu (przykład: naklejki Anieli z folii błyszczącej, 2026-04-21).

Fix: rasteryzacja CUT polygon (zawsze clean) przez cv2.fillPoly + cv2.dilate
o bleed_pts pikseli (modules/export.py:_render_source_cutpath_layer).

Ten test konstruuje PDF z gwiazdkową stroke-only linią cięcia (ostre wypukłe
narożniki → offset na pewno self-intersecting) i weryfikuje, że maska alpha
output rastra jest CONNECTED (ciągły obszar bez wewnętrznych dziur).
"""
from __future__ import annotations

import math
from pathlib import Path

import fitz
import numpy as np
import pytest
from PIL import Image

from config import MM_TO_PT
from modules.bleed import generate_bleed
from modules.contour import detect_contour
from modules.export import export_single_sticker


def _make_star_cutpath_pdf(tmp_path: Path, n_points: int = 7,
                            r_outer_mm: float = 30, r_inner_mm: float = 15,
                            page_mm: float = 100) -> str:
    """PDF z rastrowym tłem + stroke-only gwiazdką (cutpath designer'a).

    Gwiazdka o ostrych narożnikach wypukłych — offset na pewno ma loops.
    Raster: gradient bez białych pikseli (żeby rozróżnić 'dziury w masce'
    od 'białe w źródle').
    """
    page_pt = page_mm * MM_TO_PT
    doc = fitz.open()
    page = doc.new_page(width=page_pt, height=page_pt)

    # Raster: solid pink (255, 100, 150) — bez white pixels
    W = 600
    img = np.full((W, W, 3), (255, 100, 150), dtype=np.uint8)
    # Gradient żeby nie był jednolity (ale bez białego)
    img[:, :, 0] = np.linspace(200, 255, W, dtype=np.uint8)[None, :]
    png_path = tmp_path / "bg.png"
    Image.fromarray(img, 'RGB').save(png_path)
    page.insert_image(fitz.Rect(0, 0, page_pt, page_pt), filename=str(png_path))

    # Gwiazdka — stroke-only path (jak CorelDraw)
    cx, cy = page_pt / 2, page_pt / 2
    r_out = r_outer_mm * MM_TO_PT
    r_in = r_inner_mm * MM_TO_PT
    pts = []
    for i in range(n_points * 2):
        a = i * math.pi / n_points - math.pi / 2
        r = r_out if i % 2 == 0 else r_in
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    shape = page.new_shape()
    shape.draw_polyline(pts + [pts[0]])
    shape.finish(color=(1, 0, 0), fill=None, width=1.0, closePath=True)
    shape.commit()

    path = tmp_path / "star_cutpath.pdf"
    doc.save(str(path))
    doc.close()
    return str(path)


def _extract_output_alpha_mask(pdf_path: str) -> np.ndarray:
    """Wyciąga alpha (SMask) z pierwszego obrazu output PDF jako ndarray 0/255."""
    doc = fitz.open(pdf_path)
    try:
        page = doc[0]
        images = page.get_images(full=True)
        assert images, "Output PDF nie ma żadnego obrazu"
        img_xref = images[0][0]
        smask_xref = images[0][1]
        assert smask_xref, "Obraz nie ma SMask — alpha mask zaginęła"
        pix = fitz.Pixmap(doc, smask_xref)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
        return arr
    finally:
        doc.close()


def test_source_cutpath_mask_has_no_holes(tmp_path: Path):
    """Offset gwiazdki NIE może produkować dziur w alpha mask.

    Regresja: przed fixem PIL.polygon z even-odd tworzył dziury gdzie
    offset polygon self-intersect na ostrych wierzchołkach gwiazdki.
    """
    src = _make_star_cutpath_pdf(tmp_path, n_points=7)

    stickers = detect_contour(src, use_source_cutpath=True)
    assert len(stickers) == 1
    s = stickers[0]
    assert s.from_source_cutpath is True, \
        "Gwiazdka stroke-only powinna trafić na ścieżkę from_source_cutpath"

    generate_bleed(s, bleed_mm=2.0)
    out = str(tmp_path / "star_out.pdf")
    export_single_sticker(s, out, bleed_mm=2.0)

    # Maska output: musi być CONNECTED (jeden składnik) i bez wewnętrznych dziur
    mask = _extract_output_alpha_mask(out)
    assert mask.size > 0
    # Binarize (SMask 1-bit daje już 0/255, ale na wszelki wypadek)
    binary = (mask > 127).astype(np.uint8)

    # Label connected components maski i jej dopełnienia
    import cv2
    n_fg, _ = cv2.connectedComponents(binary)
    assert n_fg == 2, (
        f"Maska ma {n_fg - 1} składników zamiast 1 "
        "— kontur cięcia rozpadł się na kawałki"
    )

    # DZIURY WEWNĘTRZNE: komponenty tła (inv mask), których bbox NIE dotyka
    # brzegu obrazu. Background-at-edges jest OK (duża gwiazdka może podzielić
    # tło poza sticker na kilka kawałków swoimi ostrymi punktami). Dziury
    # WEWNĄTRZ maski to bug iskierek.
    inv = 1 - binary
    n_inv, labels = cv2.connectedComponents(inv)
    H, W = binary.shape
    internal_holes = 0
    for comp_id in range(1, n_inv):
        ys, xs = np.where(labels == comp_id)
        touches_edge = (xs.min() == 0 or xs.max() == W - 1 or
                        ys.min() == 0 or ys.max() == H - 1)
        if not touches_edge:
            internal_holes += 1
    assert internal_holes == 0, (
        f"Maska ma {internal_holes} wewnętrznych dziur — "
        "offset polygon self-intersections produkują iskierki w output"
    )


def test_source_cutpath_output_no_white_pixels_in_bleed(tmp_path: Path):
    """Rastr źródła nie ma białych pikseli → bleed zone też nie powinna ich mieć.

    Jeśli bleed zone pokazuje białe piksele, to znaczy że alpha mask miała
    dziury (bug iskierek) LUB że raster miał białe piksele poza alpha=False
    composite (page-out-of-bounds przy render).
    """
    src = _make_star_cutpath_pdf(tmp_path, n_points=7)
    s = detect_contour(src, use_source_cutpath=True)[0]
    generate_bleed(s, bleed_mm=2.0)
    out = str(tmp_path / "star_out.pdf")
    export_single_sticker(s, out, bleed_mm=2.0)

    # Renderujemy output, potem sprawdzamy wewnątrz maski (visible pixels)
    # czy NIE MA pikseli bliskich białemu (które by znaczyły dziury w masce).
    doc = fitz.open(out)
    try:
        pix = doc[0].get_pixmap(dpi=300, alpha=True)  # RGBA
        rgba = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 4)
    finally:
        doc.close()

    visible = rgba[:, :, 3] > 127  # piksele widoczne przez alpha
    rgb = rgba[:, :, :3]
    near_white = np.all(rgb > 240, axis=-1)  # prawie biały
    # Białe piksele widoczne przez alpha = iskierki
    white_visible = visible & near_white
    n_white = int(white_visible.sum())
    # Tło jest całkowicie różowe/czerwone — zero białych w visible zone
    assert n_white < 50, (
        f"Bleed/sticker zawiera {n_white} białych pikseli — "
        "prawdopodobnie dziury w alpha mask (bug iskierek)"
    )
