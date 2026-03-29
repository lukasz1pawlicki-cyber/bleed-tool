"""
Bleed Tool — PDF Metadata (PDF/X-4 + FOGRA39 OutputIntent)
============================================================
Dodaje OutputIntent z ICC profilem FOGRA39 i metadata PDF/X-4
do pliku PDF. Ustawia TrimBox i BleedBox.

Implementacja czysto na PyMuPDF (xref manipulation) — bez pikepdf.
"""

from __future__ import annotations

import logging
import os
import platform

import fitz

log = logging.getLogger("bleed-tool")

# Ścieżki do ICC profilu FOGRA39 (szukane w kolejności)
_ICC_SEARCH_PATHS = [
    # macOS — Adobe
    "/Library/Application Support/Adobe/Color/Profiles/Recommended/CoatedFOGRA39.icc",
    # macOS — system
    "/Library/ColorSync/Profiles/CoatedFOGRA39.icc",
    # Windows
    os.path.expandvars(r"%WINDIR%\System32\spool\drivers\color\CoatedFOGRA39.icc"),
    # Linux
    "/usr/share/color/icc/ghostscript/CoatedFOGRA39.icc",
    "/usr/share/ghostscript/icc/CoatedFOGRA39.icc",
    # Lokalny katalog projektu
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "profiles", "CoatedFOGRA39.icc"),
]


def _find_fogra39_icc() -> str | None:
    """Szuka pliku ICC profilu FOGRA39 na dysku."""
    for path in _ICC_SEARCH_PATHS:
        if os.path.isfile(path):
            return path
    return None


def apply_pdfx4(
    doc: fitz.Document,
    bleed_mm: float = 2.0,
    icc_path: str | None = None,
) -> bool:
    """Dodaje OutputIntent FOGRA39 i metadata PDF/X-4 do otwartego dokumentu.

    Modyfikuje dokument in-place (nie zapisuje — caller musi zrobić doc.save()).

    Args:
        doc: otwarty fitz.Document (PDF)
        bleed_mm: wielkość bleed w mm (do BleedBox)
        icc_path: ścieżka do ICC profilu (None = automatycznie szukaj)

    Returns:
        True jeśli OutputIntent dodany, False jeśli brak profilu ICC
    """
    # Znajdź ICC profil
    if icc_path is None:
        icc_path = _find_fogra39_icc()
    if icc_path is None or not os.path.isfile(icc_path):
        log.warning("PDF/X-4: nie znaleziono profilu ICC FOGRA39 — pomijam OutputIntent")
        return False

    # Wczytaj ICC profil
    with open(icc_path, "rb") as f:
        icc_data = f.read()

    # --- 1. Utwórz ICC stream ---
    icc_xref = doc.get_new_xref()
    doc.update_object(icc_xref, "<<>>")
    doc.update_stream(icc_xref, icc_data)
    doc.xref_set_key(icc_xref, "N", "4")  # CMYK
    doc.xref_set_key(icc_xref, "Alternate", "/DeviceCMYK")

    # --- 2. Utwórz OutputIntent dict ---
    oi_xref = doc.get_new_xref()
    doc.update_object(oi_xref, "<<>>")
    doc.xref_set_key(oi_xref, "Type", "/OutputIntent")
    doc.xref_set_key(oi_xref, "S", "/GTS_PDFX")
    doc.xref_set_key(oi_xref, "OutputConditionIdentifier", "(FOGRA39)")
    doc.xref_set_key(oi_xref, "RegistryName", "(http://www.color.org)")
    doc.xref_set_key(oi_xref, "OutputCondition",
                     "(Coated FOGRA39 \\(ISO 12647-2:2004\\))")
    doc.xref_set_key(oi_xref, "Info",
                     "(Coated FOGRA39 \\(ISO 12647-2:2004\\))")
    doc.xref_set_key(oi_xref, "DestOutputProfile", f"{icc_xref} 0 R")

    # --- 3. Dodaj OutputIntents do katalogu ---
    cat_xref = doc.pdf_catalog()
    doc.xref_set_key(cat_xref, "OutputIntents", f"[{oi_xref} 0 R]")

    # --- 4. Ustaw TrimBox i BleedBox na każdej stronie ---
    mm_to_pt = 72.0 / 25.4
    bleed_pts = bleed_mm * mm_to_pt

    for page in doc:
        page_xref = page.xref
        mediabox = page.mediabox

        # TrimBox = MediaBox zmniejszony o bleed (kontur cięcia)
        trim = fitz.Rect(
            mediabox.x0 + bleed_pts,
            mediabox.y0 + bleed_pts,
            mediabox.x1 - bleed_pts,
            mediabox.y1 - bleed_pts,
        )
        # BleedBox = MediaBox (cała strona z bleedem)
        bleed_box = mediabox

        doc.xref_set_key(page_xref, "TrimBox",
                         f"[{trim.x0:.4f} {trim.y0:.4f} {trim.x1:.4f} {trim.y1:.4f}]")
        doc.xref_set_key(page_xref, "BleedBox",
                         f"[{bleed_box.x0:.4f} {bleed_box.y0:.4f} "
                         f"{bleed_box.x1:.4f} {bleed_box.y1:.4f}]")

    # --- 5. Metadata: Trapped ---
    # PDF/X-4 wymaga klucza Trapped w Info dict
    info = doc.metadata
    doc.set_metadata({
        "trapped": "false",
    })

    log.info(f"PDF/X-4: OutputIntent FOGRA39 dodany, TrimBox/BleedBox ustawione")
    return True


def apply_pdfx4_to_file(
    input_path: str,
    output_path: str | None = None,
    bleed_mm: float = 2.0,
    icc_path: str | None = None,
) -> str:
    """Dodaje PDF/X-4 metadata do pliku PDF na dysku.

    Args:
        input_path: ścieżka do pliku wejściowego
        output_path: ścieżka wyjściowa (None = nadpisz input)
        bleed_mm: wielkość bleed w mm
        icc_path: ścieżka do ICC profilu

    Returns:
        ścieżka do pliku wyjściowego
    """
    if output_path is None:
        output_path = input_path

    doc = fitz.open(input_path)
    apply_pdfx4(doc, bleed_mm=bleed_mm, icc_path=icc_path)
    doc.save(output_path)
    doc.close()
    return output_path
