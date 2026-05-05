"""End-to-end regression: workflow detect_contour → generate_bleed → export.

Pokrywa cały pipeline dla typowych zgłoszeń (Slowinski 2026-05-05) — zapobiega
regresji wszystkich 6 fixów wprowadzonych w commit 98c4925:

1. find_outermost_drawing tie-break: top-most layer wygrywa przy równej area
   (czarne tło Illustratora pod białym overlayem)
2. extract_native_cmyk acceptance: weryfikacja przeciw ICC-converted RGB
   (regex łapał czarne tło zamiast widocznego koloru)
3. check_edge_uniformity: detekcja niejednolitej krawędzi (sample 2px ring
   offset 2px od brzegu)
4. generate_bleed solid-fill = biały dla niejednolitej krawędzi z białym
   outermost (zamiast avg fioletowo-szarego zalewającego cały spad)
5. _iter_content_and_xobject_xrefs: BFS rekursja do Form XObjektów
6. overlay_edge_extensions: rysuje boki i narożniki bleed kolorami z
   get_drawings() — niezależne od głębokości XObjektów

Fixtures imitują struktury PDF z eksportów Illustrator/InDesign/Canva:
  - "stacked-bg": drawing[0] czarny full-page + drawing[1] biały full-page
  - "xobject-nested": grafika wewnątrz Form XObject (X2 → wewnętrzne pasy)
  - "non-uniform edge": boczne pasy + paski górny/dolny + środek biały
"""
from __future__ import annotations

import re

import fitz
import numpy as np

from config import MM_TO_PT
from modules.bleed import generate_bleed
from modules.contour import detect_contour
from modules.export import export_single_sticker


# =============================================================================
# FIXTURES — imitacje struktur z real-world plików
# =============================================================================

def _make_canva_stacked_bg(tmp_path, w_mm=80, h_mm=50,
                            edge_color=(0.5, 0.3, 0.7)):
    """Canva-style: drawing[0] czarny full-page + drawing[1] kolorowe full-page
    overlay. Top layer wyznacza widoczny kolor — fix tie-break musi to wybrać.
    """
    doc = fitz.open()
    w = w_mm * MM_TO_PT
    h = h_mm * MM_TO_PT
    page = doc.new_page(width=w, height=h)
    # Bottom: czarny full-page (artefakt Illustratora)
    page.draw_rect(fitz.Rect(0, 0, w, h), fill=(0, 0, 0), color=None, width=0)
    # Top: edge_color full-page (widoczny)
    page.draw_rect(fitz.Rect(0, 0, w, h), fill=edge_color, color=None, width=0)
    # Środek: biała grafika żeby strona nie była pusta
    page.draw_circle(fitz.Point(w / 2, h / 2), 5 * MM_TO_PT,
                     fill=(1, 1, 1), color=None, width=0)
    src = tmp_path / "canva_stacked.pdf"
    doc.save(str(src))
    doc.close()
    return str(src)


def _make_xobject_nested_nonuniform(tmp_path, w_mm=80, h_mm=50,
                                     left_color=(0.5, 0.3, 0.7),
                                     right_color=(0.5, 0.3, 0.7),
                                     top_color=(0.95, 0.5, 0.17),
                                     bottom_color=None):
    """Slowinski-p1-style: cała grafika w Form XObject; niejednolita krawędź.

    Każdy pas (lewy/prawy/górny/dolny) w innym kolorze. Środek biały.
    Bottom_color=None pomija dolny pasek (jak p1 — biały dół).
    """
    main_doc = fitz.open()
    w = w_mm * MM_TO_PT
    h = h_mm * MM_TO_PT
    main_page = main_doc.new_page(width=w, height=h)

    # Form XObject: cała grafika (drawing[1] biały + boczne pasy + górny pasek)
    form_doc = fitz.open()
    form_page = form_doc.new_page(width=w, height=h)
    # Białe tło
    form_page.draw_rect(fitz.Rect(0, 0, w, h), fill=(1, 1, 1), color=None, width=0)
    # Boczne pasy — Illustrator często rysuje z naddatkiem (wystaje poza stronę)
    bar_w = 10 * MM_TO_PT
    over = 5 * MM_TO_PT  # naddatek
    form_page.draw_rect(fitz.Rect(0, -over, bar_w, h + over),
                         fill=left_color, color=None, width=0)
    form_page.draw_rect(fitz.Rect(w - bar_w, -over, w, h + over),
                         fill=right_color, color=None, width=0)
    # Górny pasek pełnoszerokościowy
    form_page.draw_rect(fitz.Rect(0, 0, w, 5 * MM_TO_PT),
                         fill=top_color, color=None, width=0)
    if bottom_color is not None:
        form_page.draw_rect(fitz.Rect(0, h - 5 * MM_TO_PT, w, h),
                             fill=bottom_color, color=None, width=0)

    # Embed form_page jako XObject na main page (przez show_pdf_page)
    main_page.show_pdf_page(fitz.Rect(0, 0, w, h), form_doc, 0)
    form_doc.close()

    src = tmp_path / "xobject_nested.pdf"
    main_doc.save(str(src))
    main_doc.close()
    return str(src)


# =============================================================================
# HELPERY — sampling kolorów i metadata
# =============================================================================

def _render_rgb(pdf_path: str, dpi: int = 300) -> np.ndarray:
    doc = fitz.open(pdf_path)
    pix = doc[0].get_pixmap(dpi=dpi)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, 3
    )
    doc.close()
    return arr


def _bleed_corner_avg(arr: np.ndarray, top: bool, left: bool,
                       bleed_px: int = 18) -> tuple[float, float, float]:
    """Średnia z prostokąta narożnika bleed area — środek 10x10 pikseli."""
    half = bleed_px // 2
    pad = max(2, half - 5)
    y_slice = slice(pad, half + 5) if top else slice(-half - 5, -pad)
    x_slice = slice(pad, half + 5) if left else slice(-half - 5, -pad)
    return tuple(float(c) for c in arr[y_slice, x_slice, :].mean(axis=(0, 1)))


def _bleed_edge_avg(arr: np.ndarray, side: str,
                     bleed_px: int = 18) -> tuple[float, float, float]:
    """Średnia z paska bleed po danej stronie (środek edge bez narożników)."""
    h, w = arr.shape[:2]
    half = bleed_px // 2
    pad = max(2, half - 5)
    if side == 'top':
        sl = (slice(pad, half + 5), slice(w // 3, 2 * w // 3))
    elif side == 'bottom':
        sl = (slice(-half - 5, -pad), slice(w // 3, 2 * w // 3))
    elif side == 'left':
        sl = (slice(h // 3, 2 * h // 3), slice(pad, half + 5))
    elif side == 'right':
        sl = (slice(h // 3, 2 * h // 3), slice(-half - 5, -pad))
    else:
        raise ValueError(side)
    return tuple(float(c) for c in arr[sl[0], sl[1], :].mean(axis=(0, 1)))


def _is_color(rgb, target, tol=40):
    """Czy RGB jest blisko target (tolerancja 40/255 per kanał)."""
    return all(abs(int(c) - int(t)) <= tol for c, t in zip(rgb, target))


def _count_images(pdf_path: str) -> int:
    doc = fitz.open(pdf_path)
    n = sum(len(doc[p].get_images()) for p in range(doc.page_count))
    doc.close()
    return n


def _has_cutcontour(pdf_path: str) -> bool:
    doc = fitz.open(pdf_path)
    has = False
    for xref in range(1, doc.xref_length()):
        try:
            obj = doc.xref_object(xref)
            if 'CutContour' in obj:
                has = True
                break
        except Exception:
            continue
    doc.close()
    return has


def _has_pdfx_metadata(pdf_path: str) -> bool:
    doc = fitz.open(pdf_path)
    metadata = doc.metadata
    has_xmp = doc.get_xml_metadata() is not None
    has_output_intent = False
    for xref in range(1, doc.xref_length()):
        try:
            obj = doc.xref_object(xref)
            if '/OutputIntent' in obj or '/GTS_PDFX' in obj:
                has_output_intent = True
                break
        except Exception:
            continue
    doc.close()
    return has_xmp and has_output_intent


# =============================================================================
# WORKFLOW REGRESSION TESTS
# =============================================================================

def test_workflow_canva_stacked_picks_top_layer_color(tmp_path):
    """Canva-style: tie-break wybiera top layer, edge_color = top fill.

    Regresja: find_outermost_drawing.
    """
    src = _make_canva_stacked_bg(tmp_path, edge_color=(0.06, 0.45, 0.5))
    stickers = detect_contour(src)
    s = stickers[0]
    generate_bleed(s, 2.0)
    # Top layer = drawing[1] teal — edge_rgb musi być teal, nie czarne
    r, g, b = s.edge_color_rgb
    assert (round(r, 2), round(g, 2), round(b, 2)) == (0.06, 0.45, 0.5)


def test_workflow_canva_stacked_full_export_correct_color(tmp_path):
    """Eksport Canva-style: spad jednolity kolor top layer (nie czarny)."""
    src = _make_canva_stacked_bg(tmp_path, edge_color=(0.06, 0.45, 0.5))
    stickers = detect_contour(src)
    s = stickers[0]
    generate_bleed(s, 2.0)
    out = str(tmp_path / "canva_out.pdf")
    export_single_sticker(s, out, bleed_mm=2.0)
    # Wektor zachowany
    assert _count_images(out) == 0
    # Bleed area na wszystkich 4 bokach: teal (15, 115, 128) ±tol
    arr = _render_rgb(out)
    teal = (15, 115, 128)
    for side in ('top', 'bottom', 'left', 'right'):
        edge = _bleed_edge_avg(arr, side)
        assert _is_color(edge, teal), \
            f"{side} bleed = {edge}, oczekiwane teal {teal}"


def test_workflow_xobject_nested_nonuniform_4_edges_colored(tmp_path):
    """Slowinski p1: grafika w XObjekcie + niejednolita krawędź.

    overlay_edge_extensions musi pokryć wszystkie 4 boki bleed kolorami
    krawędzi (niezależnie od głębokości XObjektów).
    """
    src = _make_xobject_nested_nonuniform(
        tmp_path,
        left_color=(0.5, 0.3, 0.7),
        right_color=(0.5, 0.3, 0.7),
        top_color=(0.95, 0.5, 0.17),
        bottom_color=(0.95, 0.5, 0.17),
    )
    stickers = detect_contour(src)
    s = stickers[0]
    generate_bleed(s, 2.0)
    out = str(tmp_path / "xobject_out.pdf")
    export_single_sticker(s, out, bleed_mm=2.0)

    assert _count_images(out) == 0, "Wektorowy input MUSI dać wektorowy output"
    arr = _render_rgb(out)

    # Boki: lewy/prawy fioletowe, górny/dolny pomarańczowe
    purple = (128, 76, 178)
    orange = (242, 127, 43)
    assert _is_color(_bleed_edge_avg(arr, 'left'), purple), \
        "lewy bleed niefioletowy"
    assert _is_color(_bleed_edge_avg(arr, 'right'), purple), \
        "prawy bleed niefioletowy"
    assert _is_color(_bleed_edge_avg(arr, 'top'), orange), \
        "górny bleed niepomarańczowy"
    assert _is_color(_bleed_edge_avg(arr, 'bottom'), orange), \
        "dolny bleed niepomarańczowy"


def test_workflow_xobject_nested_4_corners_filled(tmp_path):
    """Narożniki bleed area MUSZĄ być kolorowe (nie białe) gdy source
    ma kolor w narożnikach. Regresja: corner extensions w overlay_*."""
    src = _make_xobject_nested_nonuniform(
        tmp_path,
        left_color=(0.5, 0.3, 0.7),
        right_color=(0.5, 0.3, 0.7),
        top_color=(0.95, 0.5, 0.17),
        bottom_color=(0.95, 0.5, 0.17),
    )
    stickers = detect_contour(src)
    s = stickers[0]
    generate_bleed(s, 2.0)
    out = str(tmp_path / "corners_out.pdf")
    export_single_sticker(s, out, bleed_mm=2.0)
    arr = _render_rgb(out)

    # Wszystkie 4 narożniki muszą mieć color (nie biały):
    # górne pomarańczowe (top bar wygrywa nad bocznym), dolne pomarańczowe
    # (bottom bar wygrywa nad bocznym).
    for top in (True, False):
        for left in (True, False):
            corner = _bleed_corner_avg(arr, top=top, left=left)
            white_dist = sum(abs(c - 255) for c in corner)
            label = ('TL' if top and left else
                     'TR' if top else
                     'BL' if left else 'BR')
            assert white_dist > 100, (
                f"{label} narożnik pozostał biały: {corner} "
                f"— corner extensions nie zadziałały"
            )


def test_workflow_xobject_nested_corners_match_source(tmp_path):
    """Narożniki output mają TEN SAM kolor co narożniki source (lokalnie)."""
    src = _make_xobject_nested_nonuniform(
        tmp_path,
        left_color=(0.5, 0.3, 0.7),
        right_color=(0.5, 0.3, 0.7),
        top_color=(0.95, 0.5, 0.17),
        bottom_color=(0.95, 0.5, 0.17),
    )
    stickers = detect_contour(src)
    s = stickers[0]
    generate_bleed(s, 2.0)
    out = str(tmp_path / "match_out.pdf")
    export_single_sticker(s, out, bleed_mm=2.0)

    # Source corners (przed bleed)
    src_arr = _render_rgb(src)
    out_arr = _render_rgb(out)

    for top in (True, False):
        for left in (True, False):
            # Source corner = dokładnie róg etykiety (5x5 px from edge)
            sy = slice(0, 5) if top else slice(-5, None)
            sx = slice(0, 5) if left else slice(-5, None)
            src_corner = tuple(
                float(c) for c in src_arr[sy, sx, :].mean(axis=(0, 1))
            )
            # Output corner = środek narożnika bleed area (poza CropBox)
            out_corner = _bleed_corner_avg(out_arr, top=top, left=left)
            assert _is_color(src_corner, out_corner, tol=40), (
                f"Output corner nie pasuje do source: "
                f"out={out_corner}, src={src_corner}"
            )


def test_workflow_output_dimensions_equal_trim_plus_bleed(tmp_path):
    """Output PDF MediaBox = trim + 2 × bleed na każdej osi."""
    bleed_mm = 2.0
    src = _make_xobject_nested_nonuniform(tmp_path, w_mm=80, h_mm=50)
    stickers = detect_contour(src)
    s = stickers[0]
    generate_bleed(s, bleed_mm)
    out = str(tmp_path / "dims_out.pdf")
    export_single_sticker(s, out, bleed_mm=bleed_mm)

    doc = fitz.open(out)
    page = doc[0]
    out_w_mm = page.rect.width / MM_TO_PT
    out_h_mm = page.rect.height / MM_TO_PT
    doc.close()

    expected_w = s.width_mm + 2 * bleed_mm
    expected_h = s.height_mm + 2 * bleed_mm
    assert abs(out_w_mm - expected_w) < 0.5, \
        f"Output width {out_w_mm:.2f}mm ≠ {expected_w:.2f}mm"
    assert abs(out_h_mm - expected_h) < 0.5, \
        f"Output height {out_h_mm:.2f}mm ≠ {expected_h:.2f}mm"


def test_workflow_cutcontour_spot_color_in_output(tmp_path):
    """Output zawiera CutContour spot color (Separation)."""
    src = _make_xobject_nested_nonuniform(tmp_path)
    stickers = detect_contour(src)
    s = stickers[0]
    generate_bleed(s, 2.0)
    out = str(tmp_path / "cut_out.pdf")
    export_single_sticker(s, out, bleed_mm=2.0)
    assert _has_cutcontour(out), \
        "Output nie ma CutContour spot color — Summa S3 nie wykryje linii cięcia"


def test_workflow_pdfx4_metadata_in_output(tmp_path):
    """Output ma PDF/X-4 OutputIntent + XMP metadata (FOGRA39)."""
    src = _make_xobject_nested_nonuniform(tmp_path)
    stickers = detect_contour(src)
    s = stickers[0]
    generate_bleed(s, 2.0)
    out = str(tmp_path / "pdfx_out.pdf")
    export_single_sticker(s, out, bleed_mm=2.0)
    assert _has_pdfx_metadata(out), \
        "Output bez PDF/X-4 OutputIntent — RIP może odrzucić"


def test_workflow_uniform_edge_uses_avg_color(tmp_path):
    """Jednolita krawędź → check_edge_uniformity=True → solid-fill = avg
    krawędzi (klasyczna ścieżka, bez overlay extensions)."""
    src = _make_canva_stacked_bg(tmp_path, edge_color=(0.06, 0.45, 0.5))
    stickers = detect_contour(src)
    s = stickers[0]
    generate_bleed(s, 2.0)
    assert s.edge_uniform is True
    # edge_rgb musi być teal, nie biały
    r, g, b = s.edge_color_rgb
    assert (r, g, b) != (1.0, 1.0, 1.0)


def test_workflow_nonuniform_edge_uses_white_solid_fill(tmp_path):
    """Niejednolita krawędź + biały outermost → solid-fill = biały
    (lokalne kolory pochodzą z overlay_edge_extensions, nie z avg)."""
    src = _make_xobject_nested_nonuniform(tmp_path)
    stickers = detect_contour(src)
    s = stickers[0]
    generate_bleed(s, 2.0)
    assert s.edge_uniform is False
    # solid-fill biały (top layer outermost)
    r, g, b = s.edge_color_rgb
    assert r > 0.95 and g > 0.95 and b > 0.95, \
        f"Niejednolita krawędź — solid-fill powinien być biały, jest {(r, g, b)}"


def test_workflow_full_pipeline_no_image_objects(tmp_path):
    """Hard rule: vector input ⇒ vector output. Liczba osadzonych obrazów = 0."""
    fixtures = [
        _make_canva_stacked_bg(tmp_path),
        _make_xobject_nested_nonuniform(tmp_path),
    ]
    for i, src in enumerate(fixtures):
        stickers = detect_contour(src)
        s = stickers[0]
        generate_bleed(s, 2.0)
        out = str(tmp_path / f"vec_{i}.pdf")
        export_single_sticker(s, out, bleed_mm=2.0)
        assert _count_images(out) == 0, \
            f"Fixture {i}: wektorowy input dał output z osadzonym rastrem"


def test_workflow_multipage_pdf_each_page_independent(tmp_path):
    """Multi-page PDF (jak Slowinski 6sztuk): każda strona przetwarzana
    osobno, każda dostaje swój output z poprawnym spadem."""
    # Dwustronicowy fixture: p0 jednolity teal, p1 niejednolity
    doc = fitz.open()
    w = 80 * MM_TO_PT
    h = 50 * MM_TO_PT

    # Strona 0: stacked-bg z teal overlay
    p0 = doc.new_page(width=w, height=h)
    p0.draw_rect(fitz.Rect(0, 0, w, h), fill=(0, 0, 0))
    p0.draw_rect(fitz.Rect(0, 0, w, h), fill=(0.06, 0.45, 0.5))

    # Strona 1: stacked-bg + boczne pasy fioletowe
    p1 = doc.new_page(width=w, height=h)
    p1.draw_rect(fitz.Rect(0, 0, w, h), fill=(0, 0, 0))
    p1.draw_rect(fitz.Rect(0, 0, w, h), fill=(1, 1, 1))
    p1.draw_rect(fitz.Rect(0, 0, 10 * MM_TO_PT, h), fill=(0.5, 0.3, 0.7))
    p1.draw_rect(fitz.Rect(w - 10 * MM_TO_PT, 0, w, h), fill=(0.5, 0.3, 0.7))

    src = str(tmp_path / "multi.pdf")
    doc.save(src)
    doc.close()

    stickers = detect_contour(src)
    assert len(stickers) == 2

    # p0 — uniform teal
    generate_bleed(stickers[0], 2.0)
    assert stickers[0].edge_uniform is True
    out0 = str(tmp_path / "multi_p0.pdf")
    export_single_sticker(stickers[0], out0, bleed_mm=2.0)
    assert _count_images(out0) == 0

    # p1 — non-uniform (boczne pasy)
    generate_bleed(stickers[1], 2.0)
    assert stickers[1].edge_uniform is False
    out1 = str(tmp_path / "multi_p1.pdf")
    export_single_sticker(stickers[1], out1, bleed_mm=2.0)
    assert _count_images(out1) == 0

    # Sprawdź lokalne kolory — p0 cały teal, p1 lewy bleed fioletowy
    arr0 = _render_rgb(out0)
    teal = (15, 115, 128)
    assert _is_color(_bleed_edge_avg(arr0, 'top'), teal)
    assert _is_color(_bleed_edge_avg(arr0, 'left'), teal)

    arr1 = _render_rgb(out1)
    purple = (128, 76, 178)
    assert _is_color(_bleed_edge_avg(arr1, 'left'), purple)
    assert _is_color(_bleed_edge_avg(arr1, 'right'), purple)


def test_workflow_small_drawing_does_not_create_false_extension(tmp_path):
    """Mały drawing wewnątrz strony (nie dotykający krawędzi) NIE generuje
    extension. Inaczej małe elementy logo/grafiki tworzyłyby fałszywe
    pasy w bleed area."""
    doc = fitz.open()
    w = 80 * MM_TO_PT
    h = 50 * MM_TO_PT
    page = doc.new_page(width=w, height=h)
    # Tło białe full-page
    page.draw_rect(fitz.Rect(0, 0, w, h), fill=(1, 1, 1))
    # Małe czerwone logo w środku — NIE dotyka krawędzi
    page.draw_rect(
        fitz.Rect(w / 3, h / 3, 2 * w / 3, 2 * h / 3),
        fill=(1, 0, 0)
    )
    src = tmp_path / "small_drawing.pdf"
    doc.save(str(src))
    doc.close()

    stickers = detect_contour(str(src))
    s = stickers[0]
    generate_bleed(s, 2.0)
    out = str(tmp_path / "small_out.pdf")
    export_single_sticker(s, out, bleed_mm=2.0)

    # Bleed area MUSI być biały (logo środkowe nie powinno generować
    # ekspansji bleedu w żadną stronę).
    arr = _render_rgb(out)
    for side in ('top', 'bottom', 'left', 'right'):
        edge = _bleed_edge_avg(arr, side)
        white_dist = sum(abs(c - 255) for c in edge)
        assert white_dist < 80, \
            f"{side} bleed nie biały: {edge} (logo wewnętrzne nie powinno expandować)"


def test_workflow_single_full_page_color_uniform_bleed(tmp_path):
    """Jeden pełnostronicowy fill (klasyczna naklejka jednokolorowa) →
    spad jednolity koloru tła."""
    doc = fitz.open()
    w = 80 * MM_TO_PT
    h = 50 * MM_TO_PT
    page = doc.new_page(width=w, height=h)
    page.draw_rect(fitz.Rect(0, 0, w, h), fill=(0.2, 0.7, 0.3))
    page.draw_circle(fitz.Point(w / 2, h / 2), 5 * MM_TO_PT, fill=(1, 1, 1))
    src = tmp_path / "single_color.pdf"
    doc.save(str(src))
    doc.close()

    stickers = detect_contour(str(src))
    s = stickers[0]
    generate_bleed(s, 2.0)
    out = str(tmp_path / "single_out.pdf")
    export_single_sticker(s, out, bleed_mm=2.0)

    assert _count_images(out) == 0
    arr = _render_rgb(out)
    green = (51, 178, 76)
    for side in ('top', 'bottom', 'left', 'right'):
        edge = _bleed_edge_avg(arr, side)
        assert _is_color(edge, green, tol=50), \
            f"{side} bleed = {edge}, oczekiwane green {green}"
