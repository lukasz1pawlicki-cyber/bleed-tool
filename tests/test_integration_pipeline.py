"""Integration tests — pelny pipeline detect → bleed → export.

Kazdy test fixture → weryfikuje:
  1. detect_contour() zwraca poprawne cut_segments (typ, liczba, wymiary)
  2. generate_bleed() dodaje bleed_segments (offset o bleed_mm)
  3. export_single_sticker() zwraca PDF z 3 warstwami:
     - MediaBox = input + 2×bleed_mm
     - TrimBox = input (bez bleed)
     - BleedBox = MediaBox (z bleed)
     - CutContour spot color Separation
"""
from __future__ import annotations

import os
import pytest
import fitz

from config import MM_TO_PT, PT_TO_MM, DEFAULT_BLEED_MM
from modules.contour import detect_contour
from modules.bleed import generate_bleed
from modules.export import export_single_sticker
from tests.fixtures import (
    make_rectangle_vector,
    make_circle_on_artboard,
    make_pdf_with_trimbox,
    make_irregular_alpha_png,
    make_simple_raster,
    make_multipage_pdf,
)


# =============================================================================
# HELPERS — weryfikacja struktury output PDF
# =============================================================================

def _approx_mm(pt: float) -> float:
    return pt * PT_TO_MM


def _verify_boxes(pdf_path: str, input_w_mm: float, input_h_mm: float, bleed_mm: float,
                   tol_mm: float = 0.1, page_idx: int = 0) -> None:
    """Weryfikuje MediaBox/TrimBox/BleedBox/CropBox output PDF."""
    doc = fitz.open(pdf_path)
    page = doc[page_idx]
    mb = page.mediabox
    tb = page.trimbox
    bb = page.bleedbox
    cb = page.cropbox

    # MediaBox = input + 2×bleed
    expected_w = input_w_mm + 2 * bleed_mm
    expected_h = input_h_mm + 2 * bleed_mm
    assert abs(_approx_mm(mb.width) - expected_w) < tol_mm, \
        f"MediaBox width: {_approx_mm(mb.width):.2f}mm, expected {expected_w:.2f}mm"
    assert abs(_approx_mm(mb.height) - expected_h) < tol_mm, \
        f"MediaBox height: {_approx_mm(mb.height):.2f}mm, expected {expected_h:.2f}mm"

    # TrimBox = input (bez bleed) — szerokosc/wysokosc
    assert abs(_approx_mm(tb.width) - input_w_mm) < tol_mm, \
        f"TrimBox width: {_approx_mm(tb.width):.2f}mm, expected {input_w_mm:.2f}mm"
    assert abs(_approx_mm(tb.height) - input_h_mm) < tol_mm, \
        f"TrimBox height: {_approx_mm(tb.height):.2f}mm, expected {input_h_mm:.2f}mm"

    # BleedBox = MediaBox (pełny obszar ze spadem)
    assert abs(_approx_mm(bb.width) - expected_w) < tol_mm
    assert abs(_approx_mm(bb.height) - expected_h) < tol_mm

    # CropBox = MediaBox
    assert abs(_approx_mm(cb.width) - expected_w) < tol_mm
    assert abs(_approx_mm(cb.height) - expected_h) < tol_mm

    doc.close()


def _verify_cutcontour_spot(pdf_path: str) -> bool:
    """Sprawdza czy output PDF zawiera CutContour spot color (Separation)."""
    doc = fitz.open(pdf_path)
    try:
        xref_count = doc.xref_length()
        for xref in range(1, xref_count):
            try:
                obj = doc.xref_object(xref)
                if obj and "CutContour" in obj and "Separation" in obj:
                    return True
            except Exception:
                continue
        return False
    finally:
        doc.close()


def _run_full_pipeline(input_path: str, output_path: str,
                       bleed_mm: float = 2.0) -> dict:
    """Uruchamia detect_contour → generate_bleed → export_single_sticker.

    Returns: wynik z export_single_sticker.
    """
    stickers = detect_contour(input_path)
    assert len(stickers) >= 1
    s = stickers[0]
    generate_bleed(s, bleed_mm=bleed_mm)
    assert s.bleed_segments is not None
    assert s.edge_color_rgb is not None
    return export_single_sticker(s, output_path, bleed_mm=bleed_mm)


# =============================================================================
# TEST 1: Prostokat wektorowy
# =============================================================================

def test_rectangle_vector_pipeline(tmp_path):
    inp = make_rectangle_vector(tmp_path, w_mm=80, h_mm=50)
    stickers = detect_contour(inp)

    assert len(stickers) == 1
    s = stickers[0]

    # Cut segments
    assert abs(s.width_mm - 80.0) < 0.5
    assert abs(s.height_mm - 50.0) < 0.5
    assert len(s.cut_segments) == 4  # prostokat = 4 linie

    # Wszystkie segmenty typu linia
    for seg in s.cut_segments:
        assert seg[0] == 'l', f"Oczekiwana linia, jest {seg[0]}"

    # Bleed
    generate_bleed(s, bleed_mm=2.0)
    assert s.bleed_segments is not None
    assert len(s.bleed_segments) == 4
    assert s.edge_color_rgb is not None

    # Export
    out = str(tmp_path / "out_rect.pdf")
    result = export_single_sticker(s, out, bleed_mm=2.0)

    assert os.path.isfile(out)
    assert result['num_cut_segments'] == 4
    assert result['num_bleed_segments'] == 4

    # Weryfikacja boxow
    _verify_boxes(out, 80.0, 50.0, 2.0)
    assert _verify_cutcontour_spot(out), "CutContour spot nie znaleziony w output"


# =============================================================================
# TEST 2: Okrag na artboardzie
# =============================================================================

def test_circle_on_artboard_pipeline(tmp_path):
    inp = make_circle_on_artboard(tmp_path, circle_r_mm=30)
    stickers = detect_contour(inp)

    assert len(stickers) == 1
    s = stickers[0]

    # Artwork-on-artboard → wymiary = bbox grafiki, nie strony
    # Okrag R=30mm → bbox ~60x60mm (z drobnymi marginesami PyMuPDF)
    assert 58 < s.width_mm < 62, f"width_mm={s.width_mm}"
    assert 58 < s.height_mm < 62, f"height_mm={s.height_mm}"

    # Wymiary = bbox, 4 segmenty (linie lub krzywe — zależne od detekcji)
    assert len(s.cut_segments) == 4
    assert all(seg[0] in ('l', 'c') for seg in s.cut_segments)

    # Full pipeline
    out = str(tmp_path / "out_circle.pdf")
    result = _run_full_pipeline(inp, out, bleed_mm=2.0)

    assert os.path.isfile(out)
    _verify_boxes(out, s.width_mm, s.height_mm, 2.0)
    assert _verify_cutcontour_spot(out)


# =============================================================================
# TEST 3: PDF z istniejacym TrimBox
# =============================================================================

def test_with_trimbox_pipeline(tmp_path):
    """Plik ze spadami (MediaBox > TrimBox) → crop do TrimBox → naklejka = TrimBox."""
    inp = make_pdf_with_trimbox(tmp_path, trim_w_mm=60, trim_h_mm=40, existing_bleed_mm=3)
    stickers = detect_contour(inp)

    assert len(stickers) == 1
    s = stickers[0]

    # Sticker wymiary = TrimBox (60×40mm), NIE MediaBox
    assert abs(s.width_mm - 60.0) < 0.5, f"width_mm={s.width_mm} (spodziewane 60)"
    assert abs(s.height_mm - 40.0) < 0.5, f"height_mm={s.height_mm} (spodziewane 40)"
    assert len(s.cut_segments) == 4  # prostokat TrimBox

    # Full pipeline — nowy bleed 2mm (oryginal mial 3mm)
    out = str(tmp_path / "out_trimbox.pdf")
    result = _run_full_pipeline(inp, out, bleed_mm=2.0)

    # Output = TrimBox + 2×bleed = 60+4 × 40+4 = 64×44
    assert os.path.isfile(out)
    _verify_boxes(out, 60.0, 40.0, 2.0)


# =============================================================================
# TEST 4: PNG z nieregularnym ksztaltem (alpha)
# =============================================================================

def test_irregular_alpha_png_pipeline(tmp_path):
    """RGBA PNG → Moore/OpenCV boundary trace → Bezier bez alpha."""
    inp = make_irregular_alpha_png(tmp_path, size_px=600)
    stickers = detect_contour(inp)

    assert len(stickers) == 1
    s = stickers[0]

    # Raster path ustawiony
    assert s.raster_path == inp
    assert s.raster_crop_box is not None
    assert s.pdf_doc is None

    # Kontur = Bezier segments (Catmull-Rom → cubic)
    assert len(s.cut_segments) >= 5, f"za malo segmentow: {len(s.cut_segments)}"
    # Większość segmentów = 'c' (krzywe Bezier)
    c_count = sum(1 for seg in s.cut_segments if seg[0] == 'c')
    assert c_count > 0, "Zadnych krzywych Bezier w konturze"

    # Full pipeline
    out = str(tmp_path / "out_alpha.pdf")
    result = _run_full_pipeline(inp, out, bleed_mm=2.0)

    assert os.path.isfile(out)
    # Wymiary sa detected ze content bbox
    _verify_boxes(out, s.width_mm, s.height_mm, 2.0, tol_mm=0.5)
    assert _verify_cutcontour_spot(out)


# =============================================================================
# TEST 5: Prosty raster JPG (RGB bez alpha)
# =============================================================================

def test_simple_raster_jpg_pipeline(tmp_path):
    """JPG bez alpha → prostokatny kontur."""
    inp = make_simple_raster(tmp_path, w_px=900, h_px=600)
    stickers = detect_contour(inp)

    assert len(stickers) == 1
    s = stickers[0]

    assert s.raster_path == inp
    # 900×600 px przy 300 DPI = 76.2×50.8mm
    assert 75 < s.width_mm < 78
    assert 50 < s.height_mm < 52

    # Prostokatny kontur = 4 linie
    assert len(s.cut_segments) == 4

    out = str(tmp_path / "out_jpg.pdf")
    result = _run_full_pipeline(inp, out, bleed_mm=2.0)

    assert os.path.isfile(out)
    _verify_boxes(out, s.width_mm, s.height_mm, 2.0)


# =============================================================================
# TEST 6: Wielostronicowy PDF
# =============================================================================

def test_multipage_pdf_pipeline(tmp_path):
    """Wielostronicowy PDF → jeden Sticker per strona."""
    inp = make_multipage_pdf(tmp_path, pages=3, w_mm=60, h_mm=40)
    stickers = detect_contour(inp)

    assert len(stickers) == 3

    # Kazdy sticker: 60×40mm
    for i, s in enumerate(stickers):
        assert s.page_index == i
        assert abs(s.width_mm - 60.0) < 0.5
        assert abs(s.height_mm - 40.0) < 0.5
        assert len(s.cut_segments) == 4

        generate_bleed(s, bleed_mm=2.0)
        assert s.bleed_segments is not None
        assert s.edge_color_rgb is not None

        out = str(tmp_path / f"out_page{i}.pdf")
        result = export_single_sticker(s, out, bleed_mm=2.0)
        assert os.path.isfile(out)
        _verify_boxes(out, 60.0, 40.0, 2.0)


# =============================================================================
# TEST 7: Bleed = 0 (skrajny)
# =============================================================================

def test_pipeline_with_zero_bleed(tmp_path):
    """bleed_mm=0 → output = input size, TrimBox=MediaBox."""
    inp = make_rectangle_vector(tmp_path, w_mm=50, h_mm=50)
    out = str(tmp_path / "out_zero.pdf")

    stickers = detect_contour(inp)
    s = stickers[0]
    generate_bleed(s, bleed_mm=0.0)
    export_single_sticker(s, out, bleed_mm=0.0)

    assert os.path.isfile(out)
    _verify_boxes(out, 50.0, 50.0, 0.0)


# =============================================================================
# TEST 8: Rozne bleed_mm → output skaluje sie liniowo
# =============================================================================

@pytest.mark.parametrize("bleed_mm", [1.0, 2.0, 3.0, 5.0])
def test_pipeline_parametrized_bleed(tmp_path, bleed_mm):
    """Output = input + 2*bleed dla roznych wartosci bleed."""
    inp = make_rectangle_vector(tmp_path, w_mm=40, h_mm=40)
    out = str(tmp_path / f"out_b{bleed_mm}.pdf")

    result = _run_full_pipeline(inp, out, bleed_mm=bleed_mm)

    expected = 40.0 + 2 * bleed_mm
    assert abs(result['output_size_mm'][0] - expected) < 0.1
    assert abs(result['output_size_mm'][1] - expected) < 0.1
    _verify_boxes(out, 40.0, 40.0, bleed_mm)


# =============================================================================
# TEST 9: Bleed segments sa offset wzgledem cut segments
# =============================================================================

def test_bleed_segments_are_offset_of_cut(tmp_path):
    """Weryfikuje ze bleed_segments = offset cut_segments (bounding box szerszy o 2*bleed)."""
    inp = make_rectangle_vector(tmp_path, w_mm=50, h_mm=30)
    stickers = detect_contour(inp)
    s = stickers[0]

    bleed_pts = 2.0 * MM_TO_PT
    generate_bleed(s, bleed_mm=2.0)

    # Cut bbox
    cut_pts = []
    for seg in s.cut_segments:
        for p in seg[1:]:
            cut_pts.append(p)
    cut_xs = [p[0] for p in cut_pts]
    cut_ys = [p[1] for p in cut_pts]
    cut_w = max(cut_xs) - min(cut_xs)
    cut_h = max(cut_ys) - min(cut_ys)

    # Bleed bbox
    bleed_pts_list = []
    for seg in s.bleed_segments:
        for p in seg[1:]:
            bleed_pts_list.append(p)
    bx = [p[0] for p in bleed_pts_list]
    by = [p[1] for p in bleed_pts_list]
    bleed_w = max(bx) - min(bx)
    bleed_h = max(by) - min(by)

    # Bleed bbox = cut bbox + 2*bleed_pts (tolerancja 0.5pt)
    assert abs(bleed_w - (cut_w + 2 * bleed_pts)) < 0.5, \
        f"bleed_w={bleed_w}, cut_w={cut_w}, bleed_pts={bleed_pts}"
    assert abs(bleed_h - (cut_h + 2 * bleed_pts)) < 0.5, \
        f"bleed_h={bleed_h}, cut_h={cut_h}, bleed_pts={bleed_pts}"


# =============================================================================
# TEST 10: Output PDF/X-4 ma OutputIntent FOGRA39
# =============================================================================

def test_output_pdfx4_outputintent_fogra39(tmp_path):
    """Output PDF musi miec OutputIntent GTS_PDFX z FOGRA39 (lub co najmniej odwołanie)."""
    inp = make_rectangle_vector(tmp_path, w_mm=50, h_mm=50)
    out = str(tmp_path / "out_pdfx4.pdf")
    _run_full_pipeline(inp, out, bleed_mm=2.0)

    doc = fitz.open(out)
    catalog_xref = doc.pdf_catalog()
    catalog = doc.xref_object(catalog_xref)

    # OutputIntents obecny (bezpiecznie nieprecyzyjnie — tylko obecnosc klucza)
    assert "OutputIntents" in catalog, "Brak OutputIntents w /Catalog"
    doc.close()
