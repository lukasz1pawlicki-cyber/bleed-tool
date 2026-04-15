"""Testy output naming convention (models.build_output_name)."""
from models import build_output_name


def test_basic_single_page():
    assert build_output_name("logo.pdf", 100.0, 50.0, 2.0) == "logo_PRINT_100x50mm_bleed2mm.pdf"


def test_rounds_dimensions():
    # 99.8mm → 100mm, 49.7mm → 50mm
    assert build_output_name("a.pdf", 99.8, 49.7, 2.0) == "a_PRINT_100x50mm_bleed2mm.pdf"


def test_multipage_suffix():
    # page_index=0 → _p1, page_index=1 → _p2
    assert build_output_name("doc.pdf", 80, 40, 3.0, page_index=0) == \
        "doc_p1_PRINT_80x40mm_bleed3mm.pdf"
    assert build_output_name("doc.pdf", 80, 40, 3.0, page_index=1) == \
        "doc_p2_PRINT_80x40mm_bleed3mm.pdf"


def test_strips_extension_and_dir():
    # Pelna sciezka → tylko stem
    assert build_output_name("/tmp/input/foo.svg", 30, 30, 2) == \
        "foo_PRINT_30x30mm_bleed2mm.pdf"


def test_handles_dots_in_name():
    # Plik z kropka w nazwie — splitext usuwa TYLKO ostatnie rozszerzenie
    assert build_output_name("logo.v2.png", 40, 40, 2) == \
        "logo.v2_PRINT_40x40mm_bleed2mm.pdf"
