"""
Sticker Toolkit — clear_varnish.py
====================================
Selektywny lakier UV (spot UV / Clear varnish) dla naklejek.

Modul umozliwia wybor poszczegolnych elementow graficznych na stronie
i wygenerowanie osobnego PDF z wypelnieniem spot color "Clear"
pokrywajacym tylko wybrane elementy.

Wzorzec analogiczny do White underprint w export.py:
  - Separation colorspace "Clear" z alternate CMYK
  - Content stream z fill w spot color
  - Osobny plik PDF (*_clear.pdf)
"""

from __future__ import annotations

import logging
import os

import fitz  # PyMuPDF
import numpy as np

from config import SPOT_COLOR_CLEAR, SPOT_CMYK_CLEAR, MM_TO_PT
from modules.export import setup_separation_colorspace, inject_content_stream
from modules.contour import extract_path_segments

log = logging.getLogger("bleed-tool")


# =============================================================================
# ANALIZA ELEMENTOW STRONY
# =============================================================================

def get_page_elements(
    doc: fitz.Document,
    page_index: int,
    exclude_idx: int | None = None,
) -> list[dict]:
    """Pobiera elementy graficzne ze strony PDF.

    Wywoluje page.get_drawings() i filtruje element o indeksie exclude_idx
    (najczesciej outermost/background drawing).

    Args:
        doc: otwarty dokument PDF
        page_index: indeks strony (0-based)
        exclude_idx: indeks drawingu do pominiecia (tlo/kontur zewnetrzny)

    Returns:
        Lista slownikow z polami: index, rect, fill, items, segments
    """
    page = doc[page_index]
    drawings = page.get_drawings()

    elements: list[dict] = []
    for i, drw in enumerate(drawings):
        if exclude_idx is not None and i == exclude_idx:
            continue

        rect = drw.get("rect", fitz.Rect())
        fill = drw.get("fill")
        items = drw.get("items", [])

        # Ekstrakcja segmentow sciezki (linie + krzywe Bezier)
        try:
            segments = extract_path_segments(items)
        except Exception:
            segments = []

        elements.append({
            "index": i,
            "rect": rect,
            "fill": fill,
            "items": items,
            "segments": segments,
        })

    log.info(
        f"Strona {page_index}: {len(drawings)} drawingów, "
        f"{len(elements)} elementów (exclude_idx={exclude_idx})"
    )
    return elements


# =============================================================================
# HIT TEST — WYKRYWANIE ELEMENTU POD KURSOREM
# =============================================================================

def hit_test(
    elements: list[dict],
    x_pt: float,
    y_pt: float,
) -> int | None:
    """Znajduje element zawierajacy punkt (x_pt, y_pt).

    Wspolrzedne w ukladzie fitz (punkty, y-down).
    Gdy wiele elementow naklada sie na punkt, zwraca ten o najmniejszym
    polu (najbardziej specyficzny).

    Args:
        elements: lista elementow z get_page_elements()
        x_pt: wspolrzedna X w punktach
        y_pt: wspolrzedna Y w punktach

    Returns:
        Indeks w liscie elements lub None jesli brak trafienia
    """
    hits: list[tuple[float, int]] = []

    for idx, elem in enumerate(elements):
        rect = elem["rect"]
        if rect.contains(fitz.Point(x_pt, y_pt)):
            area = rect.width * rect.height
            hits.append((area, idx))

    if not hits:
        return None

    # Najmniejsze pole = najbardziej specyficzny element
    hits.sort(key=lambda h: h[0])
    return hits[0][1]


# =============================================================================
# CONTENT STREAM — CLEAR FILL Z ITEMS (SINGLE STICKER)
# =============================================================================

def _items_to_pdf_path_ops(
    items: list,
    bleed_pts: float,
    out_h: float,
) -> str:
    """Konwertuje items[] z PyMuPDF drawing na operatory sciezki PDF.

    Transformacja fitz (y-down) -> PDF (y-up):
      x_pdf = x + bleed_pts
      y_pdf = out_h - (y + bleed_pts)

    Obsluguje typy: m (moveTo), l (lineTo), c (curveTo), re (rectangle), h (close).
    """
    def tx(x: float, y: float) -> tuple[float, float]:
        return x + bleed_pts, out_h - (y + bleed_pts)

    ops: list[str] = []

    for item in items:
        kind = item[0]

        if kind == "m":
            # moveTo: ('m', Point)
            pt = item[1]
            px, py = tx(pt.x, pt.y)
            ops.append(f"{px:.4f} {py:.4f} m")

        elif kind == "l":
            # lineTo: ('l', p1, p2) — p1=start (ignorowany jesli ciagly), p2=end
            p2 = item[2]
            px, py = tx(p2.x, p2.y)
            ops.append(f"{px:.4f} {py:.4f} l")

        elif kind == "c":
            # curveTo: ('c', p0, p1, p2, p3)
            # p0 = start (ignorowany jesli ciagly), p1,p2 = control, p3 = end
            p1 = item[2]
            p2 = item[3]
            p3 = item[4]
            c1x, c1y = tx(p1.x, p1.y)
            c2x, c2y = tx(p2.x, p2.y)
            ex, ey = tx(p3.x, p3.y)
            ops.append(
                f"{c1x:.4f} {c1y:.4f} {c2x:.4f} {c2y:.4f} {ex:.4f} {ey:.4f} c"
            )

        elif kind == "re":
            # rectangle: ('re', Rect)
            rect = item[1]
            # Konwersja Rect na 4 linie (moveTo + 3 lineTo + close)
            x0, y0 = tx(rect.x0, rect.y0)
            x1, y1 = tx(rect.x1, rect.y0)
            x2, y2 = tx(rect.x1, rect.y1)
            x3, y3 = tx(rect.x0, rect.y1)
            ops.append(f"{x0:.4f} {y0:.4f} m")
            ops.append(f"{x1:.4f} {y1:.4f} l")
            ops.append(f"{x2:.4f} {y2:.4f} l")
            ops.append(f"{x3:.4f} {y3:.4f} l")
            ops.append("h")

        elif kind == "h":
            # closePath
            ops.append("h")

    return "\n".join(ops)


def build_clear_fill_stream(
    elements: list[dict],
    selected_indices: set[int],
    bleed_pts: float,
    out_h: float,
    cs_name: str,
) -> bytes:
    """Buduje content stream: Clear fill dla wybranych elementow.

    Dla kazdego wybranego elementu generuje sciezke PDF z wypelnieniem
    spot color "Clear" (lakier UV).

    Args:
        elements: lista elementow z get_page_elements()
        selected_indices: zbiór indeksow (w liscie elements) do pokrycia
        bleed_pts: wielkosc bledu w punktach
        out_h: wysokosc strony wyjsciowej w punktach
        cs_name: nazwa zasobu colorspace (np. "CS_Clear")

    Returns:
        Bajty content streamu PDF
    """
    all_ops: list[str] = []
    all_ops.append(f"/{cs_name} cs")
    all_ops.append("1 scn")

    for idx in sorted(selected_indices):
        if idx < 0 or idx >= len(elements):
            log.warning(f"Clear: indeks {idx} poza zakresem ({len(elements)} elementow)")
            continue

        elem = elements[idx]
        items = elem.get("items", [])
        if not items:
            continue

        path_ops = _items_to_pdf_path_ops(items, bleed_pts, out_h)
        if path_ops:
            all_ops.append(path_ops)
            all_ops.append("f")

    # Jesli nic nie wygenerowano (poza naglowkiem cs), zwroc pusty stream
    if len(all_ops) <= 2:
        return b""

    stream = "\n".join(all_ops)
    return stream.encode("ascii")


# =============================================================================
# CONTENT STREAM — CLEAR FILL NA ARKUSZU (SHEET)
# =============================================================================

def build_sheet_clear_stream(
    placement,
    elements: list[dict],
    selected_indices: set[int],
    sheet_h_pt: float,
    bleed_mm: float,
    cs_name: str,
) -> bytes:
    """Buduje content stream: Clear fill dla jednego placement na arkuszu.

    Analogicznie do _build_sheet_white_fill_stream() w export.py,
    z ta roznica ze pokrywa tylko wybrane elementy (nie cala naklejke).

    Args:
        placement: obiekt Placement (sticker, x_mm, y_mm, rotation_deg)
        elements: lista elementow dla tego stickera
        selected_indices: zbiór indeksow wybranych elementow
        sheet_h_pt: wysokosc arkusza w punktach
        bleed_mm: wielkosc bleedu w mm
        cs_name: nazwa zasobu colorspace

    Returns:
        Bajty content streamu PDF
    """
    sticker = placement.sticker
    bleed_pts = bleed_mm * MM_TO_PT
    px = placement.x_mm * MM_TO_PT
    py = placement.y_mm * MM_TO_PT

    if placement.rotation_deg == 90:
        sticker_w_pt = sticker.page_height_pt
        sticker_h_pt = sticker.page_width_pt
    else:
        sticker_w_pt = sticker.page_width_pt
        sticker_h_pt = sticker.page_height_pt

    out_w = sticker_w_pt + 2 * bleed_pts
    out_h = sticker_h_pt + 2 * bleed_pts

    ops: list[str] = []
    ops.append(f"/{cs_name} cs")
    ops.append("1 scn")
    ops.append("q")  # save graphics state

    if placement.rotation_deg == 90:
        tx = px + out_h
        ty = py
        ops.append(f"0 1 -1 0 {tx:.4f} {ty:.4f} cm")
    else:
        ops.append(f"1 0 0 1 {px:.4f} {py:.4f} cm")

    # Sciezki wybranych elementow
    has_content = False
    for idx in sorted(selected_indices):
        if idx < 0 or idx >= len(elements):
            continue

        elem = elements[idx]
        items = elem.get("items", [])
        if not items:
            continue

        path_ops = _items_to_pdf_path_ops(items, bleed_pts, out_h)
        if path_ops:
            ops.append(path_ops)
            ops.append("f")
            has_content = True

    ops.append("Q")  # restore graphics state

    if not has_content:
        return b""

    return "\n".join(ops).encode("ascii")


# =============================================================================
# EKSPORT — POJEDYNCZA NAKLEJKA
# =============================================================================

def export_single_clear(
    sticker,
    selected_indices: set[int],
    elements: list[dict],
    output_path: str,
    bleed_mm: float,
) -> str:
    """Eksportuje osobny PDF z lakierem UV (spot color Clear) dla pojedynczej naklejki.

    Tworzy plik PDF z Separation colorspace "Clear" pokrywajacym
    tylko wybrane elementy graficzne.

    Args:
        sticker: obiekt Sticker z polami page_width_pt, page_height_pt
        selected_indices: zbiór indeksow wybranych elementow
        elements: lista elementow z get_page_elements()
        output_path: sciezka do pliku wyjsciowego
        bleed_mm: wielkosc bleedu w mm

    Returns:
        Sciezka do zapisanego pliku PDF
    """
    bleed_pts = bleed_mm * MM_TO_PT
    out_w = sticker.page_width_pt + 2 * bleed_pts
    out_h = sticker.page_height_pt + 2 * bleed_pts

    log.info(
        f"Export Clear PDF: {sticker.source_path}, "
        f"{len(selected_indices)} elementow, bleed={bleed_mm}mm"
    )

    doc = fitz.open()
    page = doc.new_page(width=out_w, height=out_h)

    # Setup Separation colorspace "Clear"
    cs_clear = setup_separation_colorspace(
        doc, page, SPOT_COLOR_CLEAR, SPOT_CMYK_CLEAR,
    )

    # Content stream z wybranymi elementami
    clear_stream = build_clear_fill_stream(
        elements, selected_indices, bleed_pts, out_h, cs_clear,
    )
    if clear_stream:
        inject_content_stream(doc, page, clear_stream)

    # PDF/X-4 metadata
    try:
        from modules.pdf_metadata import apply_pdfx4
        apply_pdfx4(doc, bleed_mm=bleed_mm)
    except Exception as e:
        log.warning(f"PDF/X-4 metadata (clear): {e}")

    doc.save(output_path, deflate=True, garbage=3)
    doc.close()
    log.info(f"Clear PDF zapisany: {output_path}")

    return output_path


# =============================================================================
# EKSPORT — ARKUSZ (SHEET)
# =============================================================================

def export_sheet_clear(
    sheet,
    selections: dict[str, set[int]],
    elements_cache: dict[str, list[dict]],
    output_path: str,
    bleed_mm: float,
) -> str:
    """Eksportuje osobny PDF z lakierem UV (spot color Clear) dla arkusza.

    Iteruje placements na arkuszu i dla kazdej naklejki naklada
    Clear fill na wybrane elementy.

    Args:
        sheet: obiekt Sheet z placements
        selections: slownik {"{source_path}:{page_index}" -> set indeksow}
        elements_cache: slownik {"{source_path}:{page_index}" -> list elementow}
        output_path: sciezka do pliku wyjsciowego
        bleed_mm: wielkosc bleedu w mm

    Returns:
        Sciezka do zapisanego pliku PDF
    """
    sheet_w_pt = sheet.width_mm * MM_TO_PT
    sheet_h_pt = sheet.height_mm * MM_TO_PT

    log.info(
        f"Export sheet Clear PDF: {sheet.width_mm:.0f}x{sheet.height_mm:.0f}mm, "
        f"{len(sheet.placements)} naklejek"
    )

    doc_out = fitz.open()
    out_page = doc_out.new_page(width=sheet_w_pt, height=sheet_h_pt)

    # Setup Clear Separation
    cs_clear = setup_separation_colorspace(
        doc_out, out_page, SPOT_COLOR_CLEAR, SPOT_CMYK_CLEAR,
    )

    # Iteracja po placements
    for placement in sheet.placements:
        sticker = placement.sticker
        key = f"{sticker.source_path}:{sticker.page_index}"

        sel = selections.get(key)
        if not sel:
            continue

        elems = elements_cache.get(key)
        if not elems:
            log.warning(f"Clear: brak elementow w cache dla {key}")
            continue

        clear_stream = build_sheet_clear_stream(
            placement, elems, sel, sheet_h_pt, bleed_mm, cs_clear,
        )
        if clear_stream:
            inject_content_stream(doc_out, out_page, clear_stream)

    # PDF/X-4 metadata
    try:
        from modules.pdf_metadata import apply_pdfx4
        apply_pdfx4(doc_out, bleed_mm=bleed_mm)
    except Exception as e:
        log.warning(f"PDF/X-4 metadata (sheet clear): {e}")

    doc_out.save(output_path, deflate=True, garbage=3)
    doc_out.close()
    log.info(f"Sheet Clear PDF zapisany: {output_path}")

    return output_path
