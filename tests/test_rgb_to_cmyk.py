"""Testy postprocessu RGB→CMYK przez Ghostscript.

Wymagane narzędzie: gs (ghostscript) w PATH.
Gdy brak — testy są skipowane, ale sprawdzają że:
  - config.RGB_TO_CMYK_POSTPROCESS istnieje
  - is_ghostscript_available() zwraca False
  - pdf_to_cmyk() rzuca FileNotFoundError
  - apply_pdfx4_to_file(rgb_to_cmyk=True) nie crashuje bez gs (tylko logs warning)
"""
import os
import pytest
import fitz

from modules.ghostscript_bridge import (
    pdf_to_cmyk,
    is_ghostscript_available,
    find_ghostscript,
)
from modules.pdf_metadata import apply_pdfx4_to_file
from config import MM_TO_PT


GS_AVAILABLE = is_ghostscript_available()


# ============================================================================
# Config flags
# ============================================================================

def test_config_has_rgb_to_cmyk_flags():
    import config
    assert hasattr(config, "RGB_TO_CMYK_POSTPROCESS")
    assert isinstance(config.RGB_TO_CMYK_POSTPROCESS, bool)
    assert hasattr(config, "RGB_TO_CMYK_RENDERING_INTENT")
    assert config.RGB_TO_CMYK_RENDERING_INTENT in (
        "Perceptual", "RelativeColorimetric",
        "Saturation", "AbsoluteColorimetric",
    )


def test_is_ghostscript_available_returns_bool():
    result = is_ghostscript_available()
    assert isinstance(result, bool)
    # Musi być zgodne z find_ghostscript()
    assert result == (find_ghostscript() is not None)


# ============================================================================
# pdf_to_cmyk — error handling bez GS
# ============================================================================

def test_pdf_to_cmyk_raises_when_gs_missing(tmp_path, monkeypatch):
    """Gdy gs niedostępne, pdf_to_cmyk() rzuca FileNotFoundError."""
    # Wymuś False
    import modules.ghostscript_bridge as gb
    monkeypatch.setattr(gb, "find_ghostscript", lambda: None)

    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF")  # minimal dummy
    with pytest.raises(FileNotFoundError, match="Ghostscript"):
        pdf_to_cmyk(str(pdf))


def test_pdf_to_cmyk_raises_when_input_missing():
    with pytest.raises(FileNotFoundError, match="Plik PDF nie istnieje"):
        pdf_to_cmyk("/nonexistent/path/file.pdf")


# ============================================================================
# apply_pdfx4_to_file z rgb_to_cmyk=True
# ============================================================================

@pytest.fixture
def simple_pdf(tmp_path):
    doc = fitz.open()
    doc.new_page(width=210 * MM_TO_PT, height=297 * MM_TO_PT)
    path = tmp_path / "simple.pdf"
    doc.save(str(path))
    doc.close()
    return str(path)


def test_apply_pdfx4_with_cmyk_does_not_crash_without_gs(
    simple_pdf, tmp_path, monkeypatch
):
    """Gdy gs niedostępne, postprocess jest pomijany ale PDF jest wygenerowany."""
    import modules.ghostscript_bridge as gb
    monkeypatch.setattr(gb, "find_ghostscript", lambda: None)

    out = tmp_path / "out.pdf"
    result = apply_pdfx4_to_file(simple_pdf, str(out), rgb_to_cmyk=True)
    # PDF powstał (postprocess pominięty, base PDF/X-4 OK)
    assert os.path.isfile(result)


def test_apply_pdfx4_without_cmyk_is_default(simple_pdf, tmp_path):
    """rgb_to_cmyk=False nie próbuje używać GS."""
    out = tmp_path / "out.pdf"
    result = apply_pdfx4_to_file(simple_pdf, str(out), rgb_to_cmyk=False)
    assert os.path.isfile(result)


# ============================================================================
# Live GS tests — tylko gdy Ghostscript w PATH
# ============================================================================

@pytest.mark.skipif(not GS_AVAILABLE, reason="Ghostscript not in PATH")
def test_pdf_to_cmyk_live(simple_pdf, tmp_path):
    """Prawdziwa konwersja (wymaga zainstalowanego gs)."""
    out = tmp_path / "out_cmyk.pdf"
    result = pdf_to_cmyk(str(simple_pdf), str(out))
    assert os.path.isfile(result)
    # Sprawdź że to wciąż PDF
    doc = fitz.open(result)
    assert doc.page_count == 1
    doc.close()


@pytest.mark.skipif(not GS_AVAILABLE, reason="Ghostscript not in PATH")
def test_apply_pdfx4_with_cmyk_live(simple_pdf, tmp_path):
    """Pełen pipeline: PDF/X-4 + RGB→CMYK."""
    out = tmp_path / "out_full.pdf"
    result = apply_pdfx4_to_file(simple_pdf, str(out), rgb_to_cmyk=True)
    assert os.path.isfile(result)
    doc = fitz.open(result)
    assert doc[0].trimbox is not None
    doc.close()
