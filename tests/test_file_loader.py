"""Testy modules/file_loader.py — detekcja typu i routing formatów."""
import pytest

from modules.file_loader import FileType, detect_type, to_pdf


# ============================================================================
# detect_type — routing na podstawie rozszerzenia
# ============================================================================

def test_detect_raster_formats():
    assert detect_type("img.png") == FileType.RASTER
    assert detect_type("photo.jpg") == FileType.RASTER
    assert detect_type("photo.jpeg") == FileType.RASTER
    assert detect_type("scan.tiff") == FileType.RASTER
    assert detect_type("scan.tif") == FileType.RASTER
    assert detect_type("pic.bmp") == FileType.RASTER
    assert detect_type("web.webp") == FileType.RASTER


def test_detect_pdf_and_ai():
    assert detect_type("doc.pdf") == FileType.PDF
    assert detect_type("artwork.ai") == FileType.PDF  # AI = PDF inside


def test_detect_eps():
    assert detect_type("file.eps") == FileType.EPS
    assert detect_type("file.epsf") == FileType.EPS


def test_detect_svg():
    assert detect_type("logo.svg") == FileType.SVG


def test_detect_unknown():
    assert detect_type("file.txt") == FileType.UNKNOWN
    assert detect_type("file.docx") == FileType.UNKNOWN
    assert detect_type("noextension") == FileType.UNKNOWN


def test_detect_case_insensitive():
    assert detect_type("IMG.PNG") == FileType.RASTER
    assert detect_type("Doc.PDF") == FileType.PDF
    assert detect_type("Logo.SVG") == FileType.SVG


# ============================================================================
# to_pdf — konwersja/pass-through
# ============================================================================

def test_to_pdf_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        to_pdf("/nonexistent/path/to/file.pdf")


def test_to_pdf_unsupported_format_raises(tmp_path):
    bad = tmp_path / "file.txt"
    bad.write_text("x")
    with pytest.raises(ValueError, match="Nieobsługiwany format"):
        to_pdf(str(bad))


def test_to_pdf_raster_raises(tmp_path):
    """Raster ma osobną ścieżkę — to_pdf() nie powinno go akceptować."""
    raster = tmp_path / "img.png"
    raster.write_bytes(b"\x89PNG\r\n\x1a\n")  # minimal PNG header
    with pytest.raises(ValueError, match="rastrowy"):
        to_pdf(str(raster))


def test_to_pdf_svg_no_dimensions_raises(tmp_path):
    """SVG bez wymiarów w nazwie → ValueError."""
    svg = tmp_path / "logo.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    with pytest.raises(ValueError, match="wymiarów"):
        to_pdf(str(svg))


def test_to_pdf_pdf_passthrough(tmp_path):
    """Oryginalny PDF → zwracany bez konwersji (tmp_pdf = None)."""
    pdf = tmp_path / "doc.pdf"
    # Minimalny poprawny PDF
    pdf.write_bytes(
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Count 0/Kids[]>>endobj\n"
        b"xref\n0 3\n"
        b"0000000000 65535 f\n"
        b"0000000009 00000 n\n"
        b"0000000053 00000 n\n"
        b"trailer<</Size 3/Root 1 0 R>>\n"
        b"startxref\n99\n%%EOF\n"
    )
    result_path, tmp = to_pdf(str(pdf))
    assert result_path == str(pdf)
    assert tmp is None
