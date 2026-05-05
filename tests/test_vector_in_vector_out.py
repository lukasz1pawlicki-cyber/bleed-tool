"""Hard rule: vector input ⇒ vector output (NEVER rasterize).

Wektorowy plik wejściowy (PDF/AI/SVG/EPS bez osadzonych obrazów na tej stronie)
MUSI dać wektorowy plik wyjściowy. Rasteryzacja jako fallback dla edge cases
(niejednolita krawędź, multi-warstwowe tła, Form XObjects) jest niedopuszczalna —
park maszynowy (Mimaki UCJV 300, Summa S3, JWEI CTE-1606H) wymaga wektora,
a klient płaci za "przygotowanie naklejek" w Illustratorze; rastrowy output
to regresja względem ręcznego workflowu.

Incident 2026-05-05 (Slowinski p2): zgłoszenie "spad zrasteryzowany".
Po naprawie expand_*-funkcje recursują do Form XObjects, a solid-fill dla
niejednolitej krawędzi z białym outermost zostaje BIAŁY (top layer) zamiast
avg-em krawędzi.
"""
import fitz

from config import MM_TO_PT
from modules.bleed import generate_bleed
from modules.contour import detect_contour
from modules.export import export_single_sticker


def _count_images_in_output(pdf_path: str) -> int:
    """Liczy osadzone obrazy (rasterowe XObjects) na stronie 0 outputu."""
    doc = fitz.open(pdf_path)
    n = len(doc[0].get_images())
    doc.close()
    return n


def _make_simple_vector_pdf(tmp_path, w_mm=80, h_mm=50, fill=(0.06, 0.45, 0.5)):
    """Plik czysto wektorowy — pełnostronicowy prostokąt + kontur."""
    doc = fitz.open()
    w = w_mm * MM_TO_PT
    h = h_mm * MM_TO_PT
    page = doc.new_page(width=w, height=h)
    page.draw_rect(fitz.Rect(0, 0, w, h), fill=fill, color=None, width=0)
    page.draw_circle(fitz.Point(w / 2, h / 2), 5 * MM_TO_PT,
                     fill=(1, 1, 1), color=None, width=0)
    src = tmp_path / "vec_input.pdf"
    doc.save(str(src))
    doc.close()
    return str(src)


def _make_nonuniform_vector_pdf(tmp_path):
    """Wektorowy plik z niejednolitą krawędzią — boczne pasy + środkowe sektory."""
    doc = fitz.open()
    w = 80 * MM_TO_PT
    h = 50 * MM_TO_PT
    page = doc.new_page(width=w, height=h)
    # White overlay (outermost top)
    page.draw_rect(fitz.Rect(0, 0, w, h), fill=(1, 1, 1), color=None, width=0)
    # Boczne pasy fioletowe
    page.draw_rect(fitz.Rect(0, 0, 10 * MM_TO_PT, h),
                   fill=(0.5, 0.3, 0.7), color=None, width=0)
    page.draw_rect(fitz.Rect(w - 10 * MM_TO_PT, 0, w, h),
                   fill=(0.5, 0.3, 0.7), color=None, width=0)
    # Pasek górny czarny
    page.draw_rect(fitz.Rect(0, 0, w, 5 * MM_TO_PT),
                   fill=(0, 0, 0), color=None, width=0)
    src = tmp_path / "vec_nonuniform.pdf"
    doc.save(str(src))
    doc.close()
    return str(src)


def test_uniform_vector_input_stays_vector(tmp_path):
    """Plik wektorowy z jednolitym tłem → 0 osadzonych obrazów w output."""
    src = _make_simple_vector_pdf(tmp_path)
    stickers = detect_contour(src)
    s = stickers[0]
    generate_bleed(s, 2.0)
    output_path = str(tmp_path / "uniform_out.pdf")
    export_single_sticker(s, output_path, bleed_mm=2.0)
    assert _count_images_in_output(output_path) == 0, (
        "Wektorowy input z jednolitą krawędzią NIE może produkować rastra"
    )


def test_nonuniform_vector_input_stays_vector(tmp_path):
    """Niejednolita krawędź NIE wymusza raster path — output wektorowy."""
    src = _make_nonuniform_vector_pdf(tmp_path)
    stickers = detect_contour(src)
    s = stickers[0]
    generate_bleed(s, 2.0)
    assert s.edge_uniform is False  # sanity check — non-uniform detected
    output_path = str(tmp_path / "nonuniform_out.pdf")
    export_single_sticker(s, output_path, bleed_mm=2.0)
    assert _count_images_in_output(output_path) == 0, (
        "Wektorowy input z niejednolitą krawędzią NIE może być rasteryzowany "
        "— rasteryzacja wektora jako fallback jest niedopuszczalna"
    )


def test_vector_input_drawings_preserved(tmp_path):
    """Output wektorowy zachowuje drawings ze source (sanity)."""
    src = _make_simple_vector_pdf(tmp_path)
    stickers = detect_contour(src)
    s = stickers[0]
    generate_bleed(s, 2.0)
    output_path = str(tmp_path / "preserved_out.pdf")
    export_single_sticker(s, output_path, bleed_mm=2.0)
    doc = fitz.open(output_path)
    drawings = doc[0].get_drawings()
    doc.close()
    # Output musi mieć ≥1 drawing (przynajmniej CutContour spot color)
    assert len(drawings) >= 1
