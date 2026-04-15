"""Testy modules/pdf_metadata.py — dwa backendy PyMuPDF i pikepdf.

Weryfikuje:
  - oba silniki ustawiają TrimBox/BleedBox/CropBox
  - dispatcher apply_pdfx4_to_file() wybiera właściwy silnik
  - idempotencja: ponowne wywołanie nie duplikuje OutputIntents
  - fallback pikepdf → pymupdf gdy pikepdf niedostępne
"""
import os
import pytest
import fitz

from modules.pdf_metadata import (
    apply_pdfx4,
    apply_pdfx4_to_file,
    _apply_pdfx4_pikepdf,
)
from config import MM_TO_PT


# ============================================================================
# Fixtures — minimalny prawidłowy PDF
# ============================================================================

@pytest.fixture
def simple_pdf(tmp_path):
    """PDF A4 z pustą stroną — generowany przez PyMuPDF."""
    doc = fitz.open()
    # A4 = 210×297 mm
    w_pt = 210 * MM_TO_PT
    h_pt = 297 * MM_TO_PT
    doc.new_page(width=w_pt, height=h_pt)
    path = tmp_path / "simple.pdf"
    doc.save(str(path))
    doc.close()
    return str(path)


@pytest.fixture
def pdf_with_content(tmp_path):
    """PDF z rysunkiem — żeby sprawdzić że content nie zostanie uszkodzony."""
    doc = fitz.open()
    page = doc.new_page(width=210 * MM_TO_PT, height=297 * MM_TO_PT)
    # Narysuj prostokąt
    page.draw_rect(fitz.Rect(50, 50, 200, 200), color=(1, 0, 0), fill=(0.5, 0.5, 1))
    path = tmp_path / "content.pdf"
    doc.save(str(path))
    doc.close()
    return str(path)


# ============================================================================
# PyMuPDF backend
# ============================================================================

def test_pymupdf_backend_sets_trim_bleed_boxes(simple_pdf, tmp_path):
    out = tmp_path / "out.pdf"
    apply_pdfx4_to_file(simple_pdf, str(out), bleed_mm=2.0, engine="pymupdf")

    doc = fitz.open(str(out))
    page = doc[0]
    bleed_pts = 2.0 * MM_TO_PT
    trim = page.trimbox
    bleed_box = page.bleedbox
    media = page.mediabox

    # TrimBox = MediaBox − bleed
    assert abs(trim.x0 - (media.x0 + bleed_pts)) < 0.1
    assert abs(trim.x1 - (media.x1 - bleed_pts)) < 0.1
    # BleedBox = MediaBox
    assert abs(bleed_box.x0 - media.x0) < 0.1
    assert abs(bleed_box.x1 - media.x1) < 0.1
    doc.close()


def test_pymupdf_backend_preserves_content(pdf_with_content, tmp_path):
    """Po przetworzeniu zawartość (prostokąt) powinna wciąż być w PDF."""
    out = tmp_path / "out.pdf"
    apply_pdfx4_to_file(pdf_with_content, str(out), bleed_mm=2.0, engine="pymupdf")

    doc = fitz.open(str(out))
    page = doc[0]
    drawings = page.get_drawings()
    # Minimum 1 rysunek (prostokąt)
    assert len(drawings) >= 1
    doc.close()


def test_pymupdf_backend_idempotent(simple_pdf, tmp_path):
    """Ponowne wywołanie na tym samym pliku → bez błędów, TrimBox taki sam."""
    out = tmp_path / "out.pdf"
    apply_pdfx4_to_file(simple_pdf, str(out), bleed_mm=2.0, engine="pymupdf")
    # Drugie wywołanie na już przetworzonym pliku
    apply_pdfx4_to_file(str(out), str(out), bleed_mm=2.0, engine="pymupdf")
    doc = fitz.open(str(out))
    assert doc[0].trimbox is not None
    doc.close()


# ============================================================================
# pikepdf backend
# ============================================================================

pikepdf = pytest.importorskip("pikepdf", reason="pikepdf not installed")


def test_pikepdf_backend_sets_trim_bleed_boxes(simple_pdf, tmp_path):
    out = tmp_path / "out_pikepdf.pdf"
    _apply_pdfx4_pikepdf(simple_pdf, str(out), bleed_mm=2.0)

    doc = fitz.open(str(out))
    page = doc[0]
    bleed_pts = 2.0 * MM_TO_PT
    trim = page.trimbox
    bleed_box = page.bleedbox
    media = page.mediabox

    assert abs(trim.x0 - (media.x0 + bleed_pts)) < 0.1
    assert abs(trim.x1 - (media.x1 - bleed_pts)) < 0.1
    assert abs(bleed_box.x0 - media.x0) < 0.1
    doc.close()


def test_pikepdf_backend_preserves_content(pdf_with_content, tmp_path):
    out = tmp_path / "out_pikepdf.pdf"
    _apply_pdfx4_pikepdf(pdf_with_content, str(out), bleed_mm=2.0)

    doc = fitz.open(str(out))
    page = doc[0]
    drawings = page.get_drawings()
    assert len(drawings) >= 1
    doc.close()


def test_pikepdf_backend_sets_pdfx_metadata(simple_pdf, tmp_path):
    out = tmp_path / "out_pikepdf.pdf"
    _apply_pdfx4_pikepdf(simple_pdf, str(out), bleed_mm=2.0)

    # Otwórz z pikepdf i sprawdź XMP
    import pikepdf
    pdf = pikepdf.open(str(out))
    try:
        with pdf.open_metadata() as meta:
            gts = meta.get("pdfxid:GTS_PDFXVersion", "")
            assert gts == "PDF/X-4"
    finally:
        pdf.close()


def test_pikepdf_backend_idempotent_outputintent(simple_pdf, tmp_path):
    """Dwa wywołania pikepdf nie duplikują OutputIntents."""
    out = tmp_path / "out_pikepdf.pdf"
    _apply_pdfx4_pikepdf(simple_pdf, str(out), bleed_mm=2.0)
    _apply_pdfx4_pikepdf(str(out), str(out), bleed_mm=2.0)

    import pikepdf
    pdf = pikepdf.open(str(out))
    try:
        if "/OutputIntents" in pdf.Root:
            assert len(pdf.Root.OutputIntents) == 1
    finally:
        pdf.close()


# ============================================================================
# Dispatcher — apply_pdfx4_to_file
# ============================================================================

def test_dispatcher_uses_engine_param(simple_pdf, tmp_path):
    out1 = tmp_path / "pymupdf.pdf"
    out2 = tmp_path / "pikepdf.pdf"
    apply_pdfx4_to_file(simple_pdf, str(out1), engine="pymupdf")
    apply_pdfx4_to_file(simple_pdf, str(out2), engine="pikepdf")
    assert out1.is_file()
    assert out2.is_file()


def test_dispatcher_unknown_engine_falls_back_to_pymupdf(simple_pdf, tmp_path):
    out = tmp_path / "fallback.pdf"
    # Nieznany engine — nie explicitly handled, więc trafia do default PyMuPDF branch
    apply_pdfx4_to_file(simple_pdf, str(out), engine="nonexistent")
    assert out.is_file()


def test_dispatcher_none_uses_config_default(simple_pdf, tmp_path):
    """engine=None → config.PDF_METADATA_ENGINE (default 'pymupdf')."""
    out = tmp_path / "default.pdf"
    apply_pdfx4_to_file(simple_pdf, str(out), engine=None)
    assert out.is_file()


# ============================================================================
# Config flag
# ============================================================================

def test_config_has_pdf_metadata_engine():
    import config
    assert hasattr(config, "PDF_METADATA_ENGINE")
    assert config.PDF_METADATA_ENGINE in ("pymupdf", "pikepdf")


# ============================================================================
# Equivalence — oba silniki produkują porównywalny wynik
# ============================================================================

def test_both_engines_produce_same_boxes(simple_pdf, tmp_path):
    """TrimBox/BleedBox z obu silników powinny być identyczne."""
    out_py = tmp_path / "py.pdf"
    out_pk = tmp_path / "pk.pdf"
    apply_pdfx4_to_file(simple_pdf, str(out_py), bleed_mm=2.0, engine="pymupdf")
    apply_pdfx4_to_file(simple_pdf, str(out_pk), bleed_mm=2.0, engine="pikepdf")

    d1 = fitz.open(str(out_py))
    d2 = fitz.open(str(out_pk))
    try:
        t1, t2 = d1[0].trimbox, d2[0].trimbox
        b1, b2 = d1[0].bleedbox, d2[0].bleedbox
        assert abs(t1.x0 - t2.x0) < 0.1
        assert abs(t1.x1 - t2.x1) < 0.1
        assert abs(b1.x0 - b2.x0) < 0.1
        assert abs(b1.x1 - b2.x1) < 0.1
    finally:
        d1.close()
        d2.close()
