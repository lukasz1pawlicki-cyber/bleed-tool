"""
Bleed Tool — PDF Metadata (PDF/X-4 + FOGRA39 OutputIntent)
============================================================
Dodaje OutputIntent z ICC profilem FOGRA39 i metadata PDF/X-4
do pliku PDF. Ustawia TrimBox i BleedBox.

Implementacja czysto na PyMuPDF (xref manipulation) — bez pikepdf.
"""

from __future__ import annotations

import datetime
import logging
import os
import platform

import fitz

log = logging.getLogger("bleed-tool")

# Ścieżki do ICC profilu FOGRA39 (szukane w kolejności)
from config import ICC_SEARCH_PATHS as _ICC_SEARCH_PATHS


def _find_fogra39_icc() -> str | None:
    """Szuka pliku ICC profilu FOGRA39 na dysku."""
    for path in _ICC_SEARCH_PATHS:
        if os.path.isfile(path):
            log.info(f"ICC FOGRA39 znaleziony: {path}")
            return path
    log.warning(
        "ICC FOGRA39 nie znaleziony. Szukano w:\n"
        + "\n".join(f"  - {p}" for p in _ICC_SEARCH_PATHS)
    )
    return None


def _build_xmp_metadata(doc: fitz.Document) -> str:
    """Buduje XMP metadata z deklaracją PDF/X-4."""
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    # Zachowaj istniejące metadata
    meta = doc.metadata or {}
    title = meta.get("title", "") or "StickerPrep Output"
    creator = meta.get("creator", "") or "StickerPrep / Bleed Tool"
    producer = meta.get("producer", "") or f"PyMuPDF {fitz.version[0]}"

    xmp = f"""<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about=""
        xmlns:dc="http://purl.org/dc/elements/1.1/"
        xmlns:xmp="http://ns.adobe.com/xap/1.0/"
        xmlns:pdfx="http://ns.adobe.com/pdfx/1.3/"
        xmlns:pdfxid="http://www.npes.org/pdfx/ns/id/"
        xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/"
        xmlns:pdf="http://ns.adobe.com/pdf/1.3/">
      <dc:title>
        <rdf:Alt>
          <rdf:li xml:lang="x-default">{title}</rdf:li>
        </rdf:Alt>
      </dc:title>
      <dc:creator>
        <rdf:Seq>
          <rdf:li>{creator}</rdf:li>
        </rdf:Seq>
      </dc:creator>
      <xmp:CreatorTool>{creator}</xmp:CreatorTool>
      <xmp:CreateDate>{now}</xmp:CreateDate>
      <xmp:ModifyDate>{now}</xmp:ModifyDate>
      <pdf:Producer>{producer}</pdf:Producer>
      <pdf:Trapped>False</pdf:Trapped>
      <pdfxid:GTS_PDFXVersion>PDF/X-4</pdfxid:GTS_PDFXVersion>
      <pdfxid:GTS_PDFXConformance>PDF/X-4</pdfxid:GTS_PDFXConformance>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""
    return xmp


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
        # Nadal ustaw TrimBox/BleedBox (przydatne nawet bez PDF/X-4)
        _set_trim_bleed_boxes(doc, bleed_mm)
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
    doc.xref_set_key(icc_xref, "Filter", "/FlateDecode")

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
    _set_trim_bleed_boxes(doc, bleed_mm)

    # --- 5. Metadata: zachowaj istniejące + dodaj Trapped ---
    existing = doc.metadata or {}
    merged = {
        "title": existing.get("title", "") or "StickerPrep Output",
        "author": existing.get("author", ""),
        "subject": existing.get("subject", ""),
        "keywords": existing.get("keywords", ""),
        "creator": existing.get("creator", "") or "StickerPrep / Bleed Tool",
        "producer": existing.get("producer", "") or f"PyMuPDF {fitz.version[0]}",
        "creationDate": existing.get("creationDate", ""),
        "modDate": existing.get("modDate", ""),
        "trapped": "false",
    }
    doc.set_metadata(merged)

    # --- 6. XMP metadata z deklaracją PDF/X-4 ---
    xmp = _build_xmp_metadata(doc)
    doc.set_xml_metadata(xmp)

    log.info("PDF/X-4: OutputIntent FOGRA39 dodany, XMP ustawione, TrimBox/BleedBox OK")
    return True


def _set_trim_bleed_boxes(doc: fitz.Document, bleed_mm: float) -> None:
    """Ustawia TrimBox i BleedBox na każdej stronie dokumentu."""
    from config import MM_TO_PT as mm_to_pt
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
        # CropBox = MediaBox — Xerox RIP wykrywa rozmiar papieru z CropBox
        doc.xref_set_key(page_xref, "CropBox",
                         f"[{mediabox.x0:.4f} {mediabox.y0:.4f} "
                         f"{mediabox.x1:.4f} {mediabox.y1:.4f}]")


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
