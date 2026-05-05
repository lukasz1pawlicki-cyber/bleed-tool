"""Regression: stacked full-page backgrounds (Illustrator/InDesign export).

Pliki z eksportu Illustratora/InDesign czasem mają strukturę:
  drawing[0] — pełnostronicowy prostokąt fill = czarne tło (artefakt RIP)
  drawing[1] — pełnostronicowy prostokąt fill = realny kolor naklejki (overlay)
  drawing[2..] — zawartość

`find_outermost_drawing` ma wybrać drawing rysowany NAJPÓŹNIEJ wśród
pełnostronicowych filled (= leżący na wierzchu = wizualnie widoczny).
Wcześniej brał pierwszy w kolejności i generował spad w niewłaściwym kolorze
(zgłoszenie Slowinski 2026-05-05: czarne spady zamiast białych/jasnoniebieskich).
"""
from pathlib import Path

import fitz

from config import MM_TO_PT
from modules.contour import detect_contour, find_outermost_drawing
from modules.bleed import generate_bleed


def _make_stacked_bg_pdf(tmp_path: Path,
                          bottom_fill: tuple,
                          top_fill: tuple,
                          name: str = "stacked.pdf") -> str:
    """PDF z dwoma pełnostronicowymi prostokątami fill (bottom → top)."""
    doc = fitz.open()
    w = 80 * MM_TO_PT
    h = 50 * MM_TO_PT
    page = doc.new_page(width=w, height=h)
    page.draw_rect(fitz.Rect(0, 0, w, h), fill=bottom_fill, color=None, width=0)
    page.draw_rect(fitz.Rect(0, 0, w, h), fill=top_fill, color=None, width=0)
    # Drobna zawartość żeby strona nie była pusta
    page.draw_circle(fitz.Point(w / 2, h / 2), 5 * MM_TO_PT,
                     fill=(0.5, 0.5, 0.5), color=None, width=0)
    path = tmp_path / name
    doc.save(str(path))
    doc.close()
    return str(path)


def _approx(a, b, tol=1e-3):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def test_top_layer_wins_over_bottom_at_equal_area(tmp_path):
    """Dwa pełnostronicowe drawings — wybierany jest TEN NA WIERZCHU (idx wyższy)."""
    src = _make_stacked_bg_pdf(tmp_path, bottom_fill=(0.0, 0.0, 0.0),
                                top_fill=(1.0, 1.0, 1.0))
    doc = fitz.open(src)
    page = doc[0]
    drawings = page.get_drawings()
    idx, drawing = find_outermost_drawing(drawings, page.rect)
    doc.close()
    # Top layer = drawing[1] biały
    assert idx == 1, f"Powinien wybrać overlay (idx=1), wybrał idx={idx}"
    assert _approx(drawing['fill'], (1.0, 1.0, 1.0))


def test_black_under_colored_overlay_picks_overlay(tmp_path):
    """Czarne tło pod kolorowym overlayem → kolor krawędzi = overlay."""
    teal = (0.06, 0.45, 0.5)
    src = _make_stacked_bg_pdf(tmp_path, bottom_fill=(0.0, 0.0, 0.0), top_fill=teal)
    stickers = detect_contour(src)
    assert len(stickers) == 1
    s = stickers[0]
    generate_bleed(s, 2.0)
    # edge_rgb powinno być teal, nie czarne
    r, g, b = s.edge_color_rgb
    assert _approx((r, g, b), teal), f"Spodziewane teal, otrzymano ({r}, {g}, {b})"


def test_black_under_white_overlay_picks_white(tmp_path):
    """Czarne tło pod białym overlayem (klasyczny case Slowinski) → biały."""
    src = _make_stacked_bg_pdf(tmp_path, bottom_fill=(0.0, 0.0, 0.0),
                                top_fill=(1.0, 1.0, 1.0))
    stickers = detect_contour(src)
    s = stickers[0]
    generate_bleed(s, 2.0)
    r, g, b = s.edge_color_rgb
    assert r > 0.95 and g > 0.95 and b > 0.95, \
        f"Spodziewane białe (>0.95), otrzymano ({r}, {g}, {b})"


def test_single_full_page_drawing_unchanged(tmp_path):
    """Regression: jeden pełnostronicowy drawing → zachowanie bez zmian (idx=0)."""
    doc = fitz.open()
    w = 80 * MM_TO_PT
    h = 50 * MM_TO_PT
    page = doc.new_page(width=w, height=h)
    page.draw_rect(fitz.Rect(0, 0, w, h), fill=(0.2, 0.7, 0.3), color=None, width=0)
    page.draw_circle(fitz.Point(w / 2, h / 2), 5 * MM_TO_PT,
                     fill=(0.5, 0.5, 0.5), color=None, width=0)
    src = tmp_path / "single_bg.pdf"
    doc.save(str(src))
    doc.close()

    doc = fitz.open(str(src))
    page = doc[0]
    drawings = page.get_drawings()
    idx, drawing = find_outermost_drawing(drawings, page.rect)
    doc.close()
    assert idx == 0
    assert _approx(drawing['fill'], (0.2, 0.7, 0.3))


def test_smaller_drawing_wins_over_bigger_full_page(tmp_path):
    """Regression: drawing mniejszy (artwork-on-artboard) wciąż wygrywa
    z pełnostronicowym tłem, niezależnie od tie-break."""
    doc = fitz.open()
    w = 200 * MM_TO_PT
    h = 200 * MM_TO_PT
    page = doc.new_page(width=w, height=h)
    # drawing[0] — pełnostronicowe białe tło
    page.draw_rect(fitz.Rect(0, 0, w, h), fill=(1.0, 1.0, 1.0), color=None, width=0)
    # drawing[1] — mała kolorowa naklejka w środku (NIE pełnostronicowa)
    page.draw_rect(fitz.Rect(50 * MM_TO_PT, 50 * MM_TO_PT,
                              150 * MM_TO_PT, 150 * MM_TO_PT),
                   fill=(0.9, 0.2, 0.2), color=None, width=0)
    src = tmp_path / "artwork_on_artboard.pdf"
    doc.save(str(src))
    doc.close()

    doc = fitz.open(str(src))
    page = doc[0]
    drawings = page.get_drawings()
    idx, drawing = find_outermost_drawing(drawings, page.rect)
    doc.close()
    # Tylko drawing[0] zawiera cały page_rect → musi wybrać 0,
    # niezależnie od tie-break (drawing[1] nie kandyduje).
    assert idx == 0
