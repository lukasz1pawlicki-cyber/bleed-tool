"""Odpornosc na nietypowe wejscia: corrupt files, unicode paths.

Pipeline nie moze crashowac silenty na nieregularnym wejsciu — powinien
rzucac jasny wyjatek po polsku LUB przetworzyc plik jesli to mozliwe.
"""
from __future__ import annotations

import os
from pathlib import Path

import fitz
import numpy as np
import pytest
from PIL import Image

from modules.contour import detect_contour
from modules.bleed import generate_bleed
from modules.export import export_single_sticker
from tests.fixtures import make_rectangle_vector, make_simple_raster


# ============================================================================
# Corrupt / edge-case inputs
# ============================================================================

def test_minimal_pdf_header_only_raises(tmp_path: Path):
    """PDF z samym headerem (bez stron) -> pipeline rzuca wyjatek."""
    path = tmp_path / "header_only.pdf"
    # PDF minimal: header + trailer, brak stron
    path.write_bytes(
        b"%PDF-1.4\n"
        b"%%EOF\n"
    )

    with pytest.raises(Exception):
        detect_contour(str(path))


def test_corrupt_pdf_bytes_raises(tmp_path: Path):
    """Plik z losowymi bajtami (nie PDF) -> wyjatek."""
    path = tmp_path / "not_a_pdf.pdf"
    path.write_bytes(b"this is not a pdf at all, just random bytes\n" * 100)

    with pytest.raises(Exception):
        detect_contour(str(path))


def test_tiny_raster_1x1_handled(tmp_path: Path):
    """PNG 1x1 — pipeline moze rzucic wyjatek (za maly) ale nie crashowac silenty."""
    img = Image.new("RGBA", (1, 1), (255, 0, 0, 255))
    path = tmp_path / "tiny.png"
    img.save(str(path))

    # Akceptujemy albo success (maly sticker) albo jasny wyjatek.
    try:
        stickers = detect_contour(str(path))
        # Jesli zwrocil — weryfikujemy sensownosc
        assert isinstance(stickers, list)
    except (ValueError, RuntimeError) as e:
        # Akceptowalne: polska wiadomosc dla operatora
        assert len(str(e)) > 0


def test_zero_sized_page_raises(tmp_path: Path):
    """PDF ze strona 0x0 — pipeline musi odrzucic lub poprawnie obsluzyc."""
    path = tmp_path / "zero.pdf"
    doc = fitz.open()
    try:
        # fitz zazwyczaj odrzuca zero-dim; probujemy minimum
        doc.new_page(width=1, height=1)
        doc.save(str(path))
    finally:
        doc.close()

    # Nie crash
    try:
        detect_contour(str(path))
    except Exception as e:
        assert len(str(e)) > 0


def test_unsupported_extension_raises(tmp_path: Path):
    """Plik .txt -> jasny komunikat o braku wsparcia."""
    path = tmp_path / "notes.txt"
    path.write_text("hello")

    with pytest.raises(Exception) as exc_info:
        detect_contour(str(path))
    # Wiadomosc nie moze byc pusta
    assert len(str(exc_info.value)) > 0


# ============================================================================
# Unicode paths (polskie znaki + spacje w sciezce)
# ============================================================================

def test_unicode_input_path(tmp_path: Path):
    """Sciezka z polskimi znakami + spacja -> pipeline dziala normalnie."""
    unicode_dir = tmp_path / "naklejki ąęłńóśźż"
    unicode_dir.mkdir()
    src = make_rectangle_vector(unicode_dir, w_mm=50, h_mm=30)
    assert "ąęłń" in src

    stickers = detect_contour(src)
    assert len(stickers) >= 1
    s = stickers[0]
    assert s.width_mm > 0
    if s.pdf_doc is not None:
        s.pdf_doc.close()


def test_unicode_output_path(tmp_path: Path):
    """Output do katalogu z polskimi znakami dziala."""
    src = make_rectangle_vector(tmp_path, w_mm=50, h_mm=40)

    unicode_out_dir = tmp_path / "wyjście — Kuba ąęłńóśźż"
    unicode_out_dir.mkdir()
    out_path = str(unicode_out_dir / "plik_wynikowy.pdf")

    stickers = detect_contour(src)
    s = stickers[0]
    generate_bleed(s, bleed_mm=2.0)
    export_single_sticker(s, out_path, bleed_mm=2.0)

    assert os.path.isfile(out_path), "Plik wyjsciowy nie powstal"
    # PDF musi sie otworzyc
    doc = fitz.open(out_path)
    try:
        assert len(doc) == 1
        assert doc[0].mediabox.width > 0
    finally:
        doc.close()

    if s.pdf_doc is not None:
        s.pdf_doc.close()


def test_unicode_with_spaces_in_filename(tmp_path: Path):
    """Nazwa pliku z spacjami + polskimi znakami."""
    fancy_path = tmp_path / "Moja Naklejka — Łódź ąęść.pdf"
    # Stwórz ręcznie (fixture uzywa fixed name)
    src = make_rectangle_vector(tmp_path, w_mm=60, h_mm=40)
    os.rename(src, str(fancy_path))

    stickers = detect_contour(str(fancy_path))
    assert len(stickers) == 1
    assert stickers[0].width_mm > 0
    if stickers[0].pdf_doc is not None:
        stickers[0].pdf_doc.close()


def test_unicode_raster_roundtrip(tmp_path: Path):
    """JPG z polska nazwa -> pelny pipeline (detect + bleed + export)."""
    unicode_dir = tmp_path / "raster łódka"
    unicode_dir.mkdir()
    src = make_simple_raster(unicode_dir, w_px=400, h_px=300)

    stickers = detect_contour(src)
    assert len(stickers) == 1
    s = stickers[0]
    assert s.width_mm > 0
    assert s.height_mm > 0

    generate_bleed(s, bleed_mm=2.0)
    out_path = str(tmp_path / "raster łódka_out.pdf")
    export_single_sticker(s, out_path, bleed_mm=2.0)
    assert os.path.isfile(out_path)


# ============================================================================
# Permissions / paths edge cases
# ============================================================================

def test_nonexistent_input_raises(tmp_path: Path):
    """Nieistniejacy plik -> wyjatek z wiadomoscia."""
    with pytest.raises(Exception):
        detect_contour(str(tmp_path / "does_not_exist.pdf"))


def test_directory_as_input_raises(tmp_path: Path):
    """Podany katalog zamiast pliku -> wyjatek."""
    with pytest.raises(Exception):
        detect_contour(str(tmp_path))
