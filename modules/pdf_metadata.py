"""
Bleed Tool — PDF Metadata (PDF/X-4 + FOGRA39 OutputIntent)
============================================================
Dodaje OutputIntent z ICC profilem FOGRA39 i metadata PDF/X-4
do pliku PDF. Ustawia TrimBox i BleedBox.

Dwa backendy:
  - PyMuPDF (xref manipulation) — default, zero extra deps
  - pikepdf — czystsze API, wymaga biblioteki pikepdf

Wybór przez config.PDF_METADATA_ENGINE lub env BLEED_PDF_METADATA_ENGINE.
"""

from __future__ import annotations

import datetime
import logging
import os
import platform

import fitz

from config import (
    MM_TO_PT,
    ICC_SEARCH_PATHS as _ICC_SEARCH_PATHS,
    PDF_METADATA_ENGINE,
    RGB_TO_CMYK_POSTPROCESS,
    RGB_TO_CMYK_RENDERING_INTENT,
)

log = logging.getLogger("bleed-tool")


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

    # --- Idempotencja: jesli OutputIntents juz jest, nie dodawaj drugi raz ---
    cat_xref = doc.pdf_catalog()
    try:
        existing_oi = doc.xref_get_key(cat_xref, "OutputIntents")
        # xref_get_key zwraca ('array', '[X 0 R]') lub ('null', 'null')
        if existing_oi and existing_oi[0] == "array" and "R" in existing_oi[1]:
            log.info("PDF/X-4: OutputIntents juz istnieje — pomijam (idempotencja)")
            _set_trim_bleed_boxes(doc, bleed_mm)
            return True
    except Exception:
        pass

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
    bleed_pts = bleed_mm * MM_TO_PT

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
    engine: str | None = None,
    rgb_to_cmyk: bool | None = None,
) -> str:
    """Dodaje PDF/X-4 metadata do pliku PDF na dysku.

    Args:
        input_path: ścieżka do pliku wejściowego
        output_path: ścieżka wyjściowa (None = nadpisz input)
        bleed_mm: wielkość bleed w mm
        icc_path: ścieżka do ICC profilu
        engine: "pymupdf" | "pikepdf" | None (None = config.PDF_METADATA_ENGINE)
        rgb_to_cmyk: wymusić postprocess RGB→CMYK przez Ghostscript
                     (None = config.RGB_TO_CMYK_POSTPROCESS)

    Returns:
        ścieżka do pliku wyjściowego
    """
    if output_path is None:
        output_path = input_path
    if engine is None:
        engine = PDF_METADATA_ENGINE
    if rgb_to_cmyk is None:
        rgb_to_cmyk = RGB_TO_CMYK_POSTPROCESS

    if engine == "pikepdf":
        try:
            result_path = _apply_pdfx4_pikepdf(input_path, output_path, bleed_mm, icc_path)
        except ImportError:
            log.warning("pikepdf niedostępne — fallback na PyMuPDF")
            result_path = _apply_pdfx4_pymupdf(input_path, output_path, bleed_mm, icc_path)
        except Exception as e:
            log.error(f"pikepdf engine failed: {e} — fallback na PyMuPDF")
            result_path = _apply_pdfx4_pymupdf(input_path, output_path, bleed_mm, icc_path)
    else:
        result_path = _apply_pdfx4_pymupdf(input_path, output_path, bleed_mm, icc_path)

    # Opcjonalny postprocess: Ghostscript RGB → CMYK
    if rgb_to_cmyk:
        try:
            from modules.ghostscript_bridge import pdf_to_cmyk, is_ghostscript_available
            if is_ghostscript_available():
                pdf_to_cmyk(
                    result_path,
                    output_pdf=result_path,
                    icc_path=icc_path if icc_path else _find_fogra39_icc(),
                    rendering_intent=RGB_TO_CMYK_RENDERING_INTENT,
                )
                log.info("PDF/X-4: RGB → CMYK postprocess OK")
            else:
                log.warning("RGB→CMYK: Ghostscript niedostępne — pomijam postprocess")
        except Exception as e:
            log.error(f"RGB→CMYK postprocess failed: {e} — zwracam oryginalny PDF")

    return result_path


def _apply_pdfx4_pymupdf(
    input_path: str,
    output_path: str,
    bleed_mm: float,
    icc_path: str | None,
) -> str:
    """Backend PyMuPDF — wyodrębniony żeby dispatcher mógł go wywołać z fallback."""
    doc = fitz.open(input_path)
    # Try/finally — apply_pdfx4 lub doc.save moga rzucic, bez try doc leakowal
    # po kazdym eksporcie konczacym sie bledem.
    try:
        apply_pdfx4(doc, bleed_mm=bleed_mm, icc_path=icc_path)
        if os.path.abspath(input_path) == os.path.abspath(output_path):
            doc.save(output_path, incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
        else:
            doc.save(output_path)
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return output_path


# =============================================================================
# pikepdf backend — alternatywny silnik zapisu PDF/X-4
# =============================================================================

def _apply_pdfx4_pikepdf(
    input_path: str,
    output_path: str,
    bleed_mm: float = 2.0,
    icc_path: str | None = None,
) -> str:
    """Backend pikepdf dla apply_pdfx4_to_file.

    Zalety vs PyMuPDF xref manipulation:
      - Czystsze API (pikepdf.Dictionary, pikepdf.Stream zamiast xref keys)
      - Wbudowana walidacja struktur PDF
      - Mniej podatne na błędy składniowe

    Wymaga: pip install pikepdf

    Raises:
        ImportError: gdy pikepdf nie jest zainstalowane.
    """
    import pikepdf

    if icc_path is None:
        icc_path = _find_fogra39_icc()

    pdf = pikepdf.open(input_path, allow_overwriting_input=(input_path == output_path))

    try:
        # --- 1. Ustaw TrimBox i BleedBox ---
        bleed_pts = bleed_mm * MM_TO_PT
        for page in pdf.pages:
            mb = page.MediaBox  # [x0 y0 x1 y1]
            x0, y0, x1, y1 = float(mb[0]), float(mb[1]), float(mb[2]), float(mb[3])
            trim = pikepdf.Array([
                x0 + bleed_pts, y0 + bleed_pts,
                x1 - bleed_pts, y1 - bleed_pts,
            ])
            bleed_box = pikepdf.Array([x0, y0, x1, y1])
            crop_box = pikepdf.Array([x0, y0, x1, y1])
            page.TrimBox = trim
            page.BleedBox = bleed_box
            page.CropBox = crop_box

        # --- 2. OutputIntent z ICC (jeśli profil znaleziony) ---
        if icc_path and os.path.isfile(icc_path):
            # Sprawdź idempotencję
            catalog = pdf.Root
            if "/OutputIntents" in catalog:
                existing = catalog.OutputIntents
                if len(existing) > 0:
                    log.info("PDF/X-4 [pikepdf]: OutputIntents już istnieje — pomijam (idempotencja)")
                    pdf.save(output_path)
                    return output_path

            with open(icc_path, "rb") as f:
                icc_data = f.read()

            # ICC stream z deflate (pikepdf sam kompresuje)
            icc_stream = pdf.make_stream(icc_data)
            icc_stream.N = 4
            icc_stream.Alternate = pikepdf.Name("/DeviceCMYK")

            # OutputIntent dict
            oi = pikepdf.Dictionary(
                Type=pikepdf.Name("/OutputIntent"),
                S=pikepdf.Name("/GTS_PDFX"),
                OutputConditionIdentifier=pikepdf.String("FOGRA39"),
                RegistryName=pikepdf.String("http://www.color.org"),
                OutputCondition=pikepdf.String("Coated FOGRA39 (ISO 12647-2:2004)"),
                Info=pikepdf.String("Coated FOGRA39 (ISO 12647-2:2004)"),
                DestOutputProfile=icc_stream,
            )
            catalog.OutputIntents = pikepdf.Array([oi])
            log.info("PDF/X-4 [pikepdf]: OutputIntent FOGRA39 dodany")
        else:
            log.warning("PDF/X-4 [pikepdf]: brak ICC — pomijam OutputIntent (TrimBox/BleedBox ustawione)")

        # --- 3. Metadata ---
        with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
            meta["pdf:Trapped"] = "False"
            meta["pdfxid:GTS_PDFXVersion"] = "PDF/X-4"
            meta["pdfxid:GTS_PDFXConformance"] = "PDF/X-4"
            if "dc:title" not in meta:
                meta["dc:title"] = "StickerPrep Output"
            if "dc:creator" not in meta:
                meta["dc:creator"] = ["StickerPrep / Bleed Tool"]

        pdf.save(output_path)
        log.info(f"PDF/X-4 [pikepdf]: zapisano {output_path}")
        return output_path

    finally:
        pdf.close()
