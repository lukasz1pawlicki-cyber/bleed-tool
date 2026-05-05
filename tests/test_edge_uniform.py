"""Regression: non-uniform edge handling (Slowinski p2 2026-05-05).

Etykiety z mieszanymi krawędziami (boczne pasy fioletowe + środkowe sektory
białe + paski czarne) potrzebują wektorowego rozszerzenia kolorów krawędzi
zamiast solid-fill jednym uśrednionym kolorem.

Pipeline:
  contour.find_outermost_drawing → bleed.generate_bleed →
  bleed.check_edge_uniformity (sample 2px ring offset 2px od brzegu) →
  sticker.edge_uniform = False
    → solid-fill = biały (z outermost overlay)
    → export.expand_edge_paths/expand_page_fills lokalnie nadrysują pasy
      kolorów krawędzi (z recursją do Form XObjects)
  Wynik: wektor zachowany, lokalne kolory krawędzi.
"""
import fitz

from config import MM_TO_PT
from modules.bleed import check_edge_uniformity, generate_bleed
from modules.contour import detect_contour


def _make_uniform_pdf(tmp_path, fill=(0.06, 0.45, 0.5)):
    """Pełnostronicowy jednolity prostokąt."""
    doc = fitz.open()
    w = 80 * MM_TO_PT
    h = 50 * MM_TO_PT
    page = doc.new_page(width=w, height=h)
    page.draw_rect(fitz.Rect(0, 0, w, h), fill=fill, color=None, width=0)
    page.draw_circle(fitz.Point(w / 2, h / 2), 5 * MM_TO_PT,
                     fill=(1, 1, 1), color=None, width=0)
    src = tmp_path / "uniform.pdf"
    doc.save(str(src))
    doc.close()
    return str(src)


def _make_nonuniform_pdf(tmp_path):
    """Etykieta z mieszanymi krawędziami — jak p2 Slowinski."""
    doc = fitz.open()
    w = 80 * MM_TO_PT
    h = 50 * MM_TO_PT
    page = doc.new_page(width=w, height=h)
    # Tło białe (pełna strona) — overlay top-most → outermost wybrane jako biały
    page.draw_rect(fitz.Rect(0, 0, w, h), fill=(1, 1, 1), color=None, width=0)
    # Pas po lewej — fioletowy (10mm szeroki, na całą wysokość)
    page.draw_rect(fitz.Rect(0, 0, 10 * MM_TO_PT, h),
                   fill=(0.5, 0.3, 0.7), color=None, width=0)
    # Pas po prawej — fioletowy
    page.draw_rect(fitz.Rect(w - 10 * MM_TO_PT, 0, w, h),
                   fill=(0.5, 0.3, 0.7), color=None, width=0)
    # Pasek górny — czarny
    page.draw_rect(fitz.Rect(0, 0, w, 5 * MM_TO_PT),
                   fill=(0, 0, 0), color=None, width=0)
    src = tmp_path / "nonuniform.pdf"
    doc.save(str(src))
    doc.close()
    return str(src)


def test_uniform_edge_passes_check(tmp_path):
    """Pełnostronicowe jednolite tło → check_edge_uniformity zwraca True."""
    src = _make_uniform_pdf(tmp_path)
    doc = fitz.open(src)
    page = doc[0]
    assert check_edge_uniformity(page) is True
    doc.close()


def test_nonuniform_edge_fails_check(tmp_path):
    """Mieszane krawędzie (czarne, fioletowe, białe) → False."""
    src = _make_nonuniform_pdf(tmp_path)
    doc = fitz.open(src)
    page = doc[0]
    assert check_edge_uniformity(page) is False
    doc.close()


def test_generate_bleed_sets_edge_uniform_flag_uniform(tmp_path):
    """Sticker.edge_uniform = True dla jednolitej krawędzi."""
    src = _make_uniform_pdf(tmp_path)
    stickers = detect_contour(src)
    s = stickers[0]
    generate_bleed(s, 2.0)
    assert s.edge_uniform is True


def test_generate_bleed_sets_edge_uniform_flag_nonuniform(tmp_path):
    """Sticker.edge_uniform = False dla mieszanej krawędzi (p2 Slowinski)."""
    src = _make_nonuniform_pdf(tmp_path)
    stickers = detect_contour(src)
    s = stickers[0]
    generate_bleed(s, 2.0)
    assert s.edge_uniform is False


def test_check_edge_uniformity_ignores_minor_noise(tmp_path):
    """Tła z lekkim antialiasing/noise nadal jednolite (false positives off)."""
    # Cyan tło — domyślny tolerance nie powinien tego sklasyfikować jako non-uniform
    src = _make_uniform_pdf(tmp_path, fill=(0.0, 0.7, 0.7))
    doc = fitz.open(src)
    assert check_edge_uniformity(doc[0]) is True
    doc.close()
