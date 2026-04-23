"""Regresja: pliki z OCG layers (BDC/EMC) nie daja pustego outputu.

Commit dc3cce0: Fix — source-cutpath pusty output dla plikow z OCG layers.

Plik z Illustratora ma typowo strukture:
    /OC /Layer_Cut BDC
        q ... m l l S Q    <- cut line
    EMC
    /OC /Layer_Graphics BDC
        q ... m l l f Q    <- grafika
    EMC

Poprzednia impl usuwala cale `q..Q` blok zawierajacy S -> psulo to BDC/EMC
pairing i PDF renderowal sie jako pusty (brak widocznej grafiki).

Test: PDF z warstwa OCG + stroke line + fill shape. Po source-cutpath
output nie moze byc pusty (minimum X% non-white pixels).
"""
from __future__ import annotations

from pathlib import Path

import fitz
import numpy as np
import pikepdf
import pytest

from config import MM_TO_PT
from modules.export import _replace_strokes_with_nop


def _make_pdf_with_ocg_layers(tmp_path: Path, page_mm: float = 80) -> str:
    """PDF z dwoma warstwami OCG: CutLine (stroke) + Graphics (fill).

    Symuluje strukture eksportu z Illustratora. Uzywamy fitz do utworzenia
    strony, potem nadpisujemy content stream przez xref.
    """
    page_pt = page_mm * MM_TO_PT
    inner = page_pt - 40
    cut_max = page_pt - 25

    doc = fitz.open()
    page = doc.new_page(width=page_pt, height=page_pt)
    # Narysuj placeholder zeby wymusic utworzenie content stream
    shape = page.new_shape()
    shape.draw_rect(fitz.Rect(0, 0, 1, 1))
    shape.finish(fill=(1, 1, 1), color=(1, 1, 1))
    shape.commit()

    # Surowy content z BDC/EMC
    content_str = (
        "/OC /Layer_Graphics BDC\n"
        "q\n"
        "0.2 0.5 0.9 rg\n"
        f"20 20 {inner:.2f} {inner:.2f} re\n"
        "f\n"
        "Q\n"
        "EMC\n"
        "/OC /Layer_CutLine BDC\n"
        "q\n"
        "1 0 0 RG\n"
        "1 w\n"
        f"25 25 m {cut_max:.2f} 25 l {cut_max:.2f} {cut_max:.2f} l 25 {cut_max:.2f} l h S\n"
        "Q\n"
        "EMC\n"
    )

    # Nadpisz content stream przez xref
    contents_xref = page.get_contents()[0]
    doc.update_stream(contents_xref, content_str.encode("ascii"))

    # Dodaj Properties z OCG do page resources
    page_xref = page.xref
    doc.xref_set_key(
        page_xref, "Resources",
        "<< /Properties << "
        "/Layer_Graphics << /Type /OCG /Name (Graphics) >> "
        "/Layer_CutLine << /Type /OCG /Name (CutLine) >> "
        ">> >>",
    )

    path = str(tmp_path / "ocg_layers.pdf")
    doc.save(path)
    doc.close()
    return path


def test_ocg_bdc_emc_balance_after_stroke_removal(tmp_path: Path):
    """Po surgical removal BDC/EMC pary sa nadal balanced (liczba BDC = EMC)."""
    src = _make_pdf_with_ocg_layers(tmp_path)

    pdf = pikepdf.open(src)
    page = pdf.pages[0]
    ops = list(pikepdf.parse_content_stream(page))

    # Sanity: zrodlo ma 2 BDC + 2 EMC + 1 S
    n_bdc_src = sum(1 for _, op in ops if str(op) == "BDC")
    n_emc_src = sum(1 for _, op in ops if str(op) == "EMC")
    n_stroke_src = sum(1 for _, op in ops if str(op) in ("S", "s"))
    assert n_bdc_src == 2, f"Zrodlo ma {n_bdc_src} BDC, oczekiwano 2"
    assert n_emc_src == 2
    assert n_stroke_src >= 1

    cleaned_ops = _replace_strokes_with_nop(ops)

    n_bdc = sum(1 for _, op in cleaned_ops if str(op) == "BDC")
    n_emc = sum(1 for _, op in cleaned_ops if str(op) == "EMC")
    n_stroke = sum(1 for _, op in cleaned_ops if str(op) in ("S", "s"))

    assert n_bdc == n_bdc_src, "BDC zachowane"
    assert n_emc == n_emc_src, "EMC zachowane"
    assert n_bdc == n_emc, "BDC/EMC balanced"
    assert n_stroke == 0, "Stroke usuniety"

    pdf.close()


def test_ocg_rendered_output_not_empty(tmp_path: Path):
    """Po surgical removal S->n PDF renderuje sie i ma widoczna grafike.

    Kluczowy test: poprzednio output byl pusty (brak non-white pixels).
    """
    src = _make_pdf_with_ocg_layers(tmp_path)

    # Zastosuj surgical removal (symuluje co robi _render_source_cutpath_layer)
    pdf = pikepdf.open(src)
    page = pdf.pages[0]
    ops = list(pikepdf.parse_content_stream(page))
    cleaned_ops = _replace_strokes_with_nop(ops)
    cleaned_bytes = pikepdf.unparse_content_stream(cleaned_ops)
    page.Contents = pdf.make_stream(cleaned_bytes)
    out_path = str(tmp_path / "ocg_cleaned.pdf")
    pdf.save(out_path)
    pdf.close()

    # Renderuj cleaned PDF i sprawdz ze grafika (fill) nadal widoczna
    doc = fitz.open(out_path)
    try:
        pix = doc[0].get_pixmap(dpi=150, alpha=False)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    finally:
        doc.close()

    # Non-white = grafika widoczna. Pusty PDF mialby ~100% bialych pikseli.
    is_white = np.all(arr > 240, axis=-1)
    non_white_ratio = 1.0 - float(is_white.mean())
    assert non_white_ratio > 0.05, (
        f"Output PDF prawie pusty: tylko {non_white_ratio*100:.1f}% non-white pixeli. "
        "Regresja: surgical removal zniszczyla grafike (nie tylko stroke)."
    )


def test_ocg_without_stroke_passthrough(tmp_path: Path):
    """Plik OCG bez zadnej linii stroke -> cleaned identyczny (ops unchanged)."""
    page_pt = 80 * MM_TO_PT
    inner = page_pt - 20
    content_str = (
        "/OC /Layer_G BDC\n"
        "q\n 0.3 0.6 0.1 rg\n"
        f"10 10 {inner:.2f} {inner:.2f} re\n"
        "f\n Q\nEMC\n"
    )
    doc = fitz.open()
    page = doc.new_page(width=page_pt, height=page_pt)
    shape = page.new_shape()
    shape.draw_rect(fitz.Rect(0, 0, 1, 1))
    shape.finish(fill=(1, 1, 1), color=(1, 1, 1))
    shape.commit()
    doc.update_stream(page.get_contents()[0], content_str.encode("ascii"))
    doc.xref_set_key(
        page.xref, "Resources",
        "<< /Properties << /Layer_G << /Type /OCG /Name (G) >> >> >>",
    )
    path = str(tmp_path / "ocg_no_stroke.pdf")
    doc.save(path)
    doc.close()

    pdf = pikepdf.open(path)
    ops = list(pikepdf.parse_content_stream(pdf.pages[0]))
    cleaned = _replace_strokes_with_nop(ops)
    assert len(cleaned) == len(ops)
    for (_, a), (_, b) in zip(ops, cleaned):
        assert str(a) == str(b), "Plik bez stroke nie powinien byc modyfikowany"
    pdf.close()
