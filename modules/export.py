"""
Sticker Toolkit — export.py
=============================
Eksport PDF — single sticker i full sheet.

Single sticker export (3 warstwy, w pełni wektorowe):
  1) Podkład bleed — RGB solid fill z offsetem konturu
  2) Oryginalna grafika wektorowa (show_pdf_page z rozszerzonym MediaBox)
     → elementy wychodzące poza stronę nadpisują solid fill
     → efekt: lokalnie dopasowany kolor bleed
  3) CutContour jako Separation spot color — Summa S3

Full sheet export (dwa PDF-y):
  - Print PDF: bleed fill + grafika per placement (bez CutContour)
  - Cut PDF: CutContour + FlexCut + marks (bez grafiki)
"""

from __future__ import annotations

import logging
import re as re_module

import fitz  # PyMuPDF
import numpy as np

from models import Sticker, Sheet, Placement, Mark, PanelLine
from config import (
    MM_TO_PT,
    PT_TO_MM,
    DEFAULT_BLEED_MM,
    CUTCONTOUR_STROKE_WIDTH_PT,
    FLEXCUT_STROKE_WIDTH_PT,
    SPOT_COLOR_CUTCONTOUR,
    SPOT_COLOR_FLEXCUT,
    SPOT_CMYK_CUTCONTOUR,
    SPOT_CMYK_FLEXCUT,
)

log = logging.getLogger(__name__)


# =============================================================================
# SEGMENTY → PDF PATH OPERATORS
# =============================================================================

def _segments_to_pdf_path_ops(
    segments: list, bleed_pts: float, out_h: float
) -> str:
    """Konwertuje segmenty ścieżki na operatory PDF.

    Transformacja fitz (y-down) → PDF (y-up):
      x_pdf = x_fitz + bleed_pts
      y_pdf = out_h - (y_fitz + bleed_pts)

    Obsługuje nieciągłe segmenty (po deduplikacji przy gap=0):
    wstawia moveTo gdy startpoint segmentu != endpoint poprzedniego.
    """
    def tx(x: float, y: float) -> tuple[float, float]:
        return x + bleed_pts, out_h - (y + bleed_pts)

    if not segments:
        return ""

    ops: list[str] = []
    last_ex: float | None = None
    last_ey: float | None = None

    for seg in segments:
        if seg[0] == 'l':
            sx, sy = tx(seg[1][0], seg[1][1])
            ex, ey = tx(seg[2][0], seg[2][1])
            # moveTo jeśli nieciągłość lub pierwszy segment
            if last_ex is None or abs(sx - last_ex) > 0.01 or abs(sy - last_ey) > 0.01:
                ops.append(f"{sx:.4f} {sy:.4f} m")
            ops.append(f"{ex:.4f} {ey:.4f} l")
            last_ex, last_ey = ex, ey
        elif seg[0] == 'c':
            sx, sy = tx(seg[1][0], seg[1][1])
            cx1, cy1 = tx(seg[2][0], seg[2][1])
            cx2, cy2 = tx(seg[3][0], seg[3][1])
            ex, ey = tx(seg[4][0], seg[4][1])
            if last_ex is None or abs(sx - last_ex) > 0.01 or abs(sy - last_ey) > 0.01:
                ops.append(f"{sx:.4f} {sy:.4f} m")
            ops.append(
                f"{cx1:.4f} {cy1:.4f} {cx2:.4f} {cy2:.4f} {ex:.4f} {ey:.4f} c"
            )
            last_ex, last_ey = ex, ey

    return "\n".join(ops)


# =============================================================================
# CONTENT STREAM BUILDERS
# =============================================================================

def build_rgb_fill_stream(
    segments: list,
    rgb: tuple[float, float, float],
    bleed_pts: float,
    out_h: float,
) -> bytes:
    """Buduje content stream: RGB fill z segmentów (warstwa 1 — bleed).

    Używa DeviceRGB (operator rg) — ten sam colorspace co oryginalna grafika.
    """
    r, g, b = rgb
    path_ops = _segments_to_pdf_path_ops(segments, bleed_pts, out_h)
    stream = f"{r:.6f} {g:.6f} {b:.6f} rg\n{path_ops}\nf"
    return stream.encode('ascii')


def build_cutcontour_stream(
    segments: list,
    bleed_pts: float,
    out_h: float,
    cs_name: str = "CS_CutContour",
) -> bytes:
    """Buduje content stream: CutContour stroke z segmentów (warstwa 3)."""
    path_ops = _segments_to_pdf_path_ops(segments, bleed_pts, out_h)
    stream = (
        f"/{cs_name} cs\n"
        f"/{cs_name} CS\n"
        f"1 SCN\n"
        f"{CUTCONTOUR_STROKE_WIDTH_PT} w\n"
        f"{path_ops}\n"
        f"S"
    )
    return stream.encode('ascii')


# =============================================================================
# ROZSZERZANIE CLIPPING PATHS W CONTENT STREAM
# =============================================================================

def expand_clip_paths(doc: fitz.Document, page: fitz.Page, bleed_pts: float) -> None:
    """Rozszerza clipping paths (W n / W* n) w content stream strony o bleed_pts.

    Illustrator osadza clip paths (W n) ograniczające rendering do artboardu.
    Aby elementy wychodzące poza stronę (np. białe napisy) były widoczne
    w strefie bleed, musimy rozszerzyć te clip paths.

    Obsługuje:
    - Prostokąty: `x y w h re W n` → rozszerzony o bleed_pts
    - Polygony: `x y m ... l ... h W n` → offset vertex normalnych na zewnątrz
    """
    page_xref = page.xref
    contents_info = doc.xref_get_key(page_xref, "Contents")
    xref_str = contents_info[1]

    # Zbierz xrefy content streamów
    xrefs = re_module.findall(r'(\d+)\s+\d+\s+R', xref_str)

    modified = False
    for xr_str in xrefs:
        xr = int(xr_str)
        stream = doc.xref_stream(xr)
        if not stream:
            continue

        text = stream.decode('latin-1', errors='replace')

        new_text = _expand_clips_in_stream(text, bleed_pts)
        if new_text != text:
            doc.update_stream(xr, new_text.encode('latin-1'))
            modified = True

    if modified:
        log.info(f"Rozszerzono clipping paths o {bleed_pts:.2f}pt")
    else:
        log.info("Brak clipping paths do rozszerzenia")


def _expand_clips_in_stream(text: str, bleed_pts: float) -> str:
    """Parsuje content stream i rozszerza wszystkie clip paths o bleed_pts."""
    lines = text.split('\n')
    result: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        # Sprawdź czy ta linia to "W n" lub "W* n" (clip operator)
        if line in ('W n', 'W* n'):
            clip_expanded = _try_expand_clip(result, line, bleed_pts)
            if clip_expanded:
                result.extend(clip_expanded)
            else:
                result.append(lines[i])
            i += 1
            continue

        result.append(lines[i])
        i += 1

    return '\n'.join(result)


def _try_expand_clip(
    preceding_lines: list[str], clip_op: str, bleed_pts: float
) -> list[str] | None:
    """Próbuje rozszerzyć clip path z preceding_lines.

    Zwraca listę linii zastępujących (od początku ścieżki do clip_op włącznie),
    lub None jeśli nie udało się rozpoznać wzorca.
    """
    path_lines: list[str] = []
    idx = len(preceding_lines) - 1

    while idx >= 0:
        pl = preceding_lines[idx].strip()
        path_lines.insert(0, pl)

        # Prostokąt: "x y w h re"
        if pl.endswith(' re'):
            expanded = _expand_rect_clip(pl, bleed_pts)
            if expanded:
                del preceding_lines[idx:]
                return [expanded, clip_op]
            break

        # Zamknięcie ścieżki: "h"
        if pl == 'h':
            path_start = idx
            while path_start > 0:
                path_start -= 1
                prev = preceding_lines[path_start].strip()
                if prev.endswith(' m'):
                    break
                parts = prev.split()
                if parts and parts[-1] not in ('m', 'l', 'c', 'h'):
                    path_start += 1
                    break

            polygon_lines = [
                preceding_lines[j].strip()
                for j in range(path_start, len(preceding_lines))
            ]
            expanded_polygon = _expand_polygon_clip(polygon_lines, bleed_pts)
            if expanded_polygon:
                del preceding_lines[path_start:]
                return expanded_polygon + [clip_op]
            break

        parts = pl.split()
        if parts and parts[-1] in ('m', 'l', 'c'):
            idx -= 1
            continue

        break

    return None


def _expand_rect_clip(rect_line: str, bleed_pts: float) -> str | None:
    """Rozszerza prostokątny clip 'x y w h re' o bleed_pts."""
    parts = rect_line.strip().split()
    if len(parts) != 5 or parts[4] != 're':
        return None

    try:
        x, y, w, h = float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3])
    except ValueError:
        return None

    if h < 0:
        # h ujemne (Illustrator pattern: y=top, h=-height)
        new_x = x - bleed_pts
        new_y = y + bleed_pts
        new_w = w + 2 * bleed_pts
        new_h = h - 2 * bleed_pts
    else:
        new_x = x - bleed_pts
        new_y = y - bleed_pts
        new_w = w + 2 * bleed_pts
        new_h = h + 2 * bleed_pts

    return f"{new_x:.6f} {new_y:.6f} {new_w:.6f} {new_h:.6f} re"


def _expand_polygon_clip(polygon_lines: list[str], bleed_pts: float) -> list[str] | None:
    """Rozszerza polygonalny clip path (m/l/h) o bleed_pts na zewnątrz."""
    vertices: list[tuple[float, float]] = []
    for pl in polygon_lines:
        parts = pl.split()
        if not parts:
            continue
        op = parts[-1]
        if op == 'm' and len(parts) >= 3:
            vertices.append((float(parts[0]), float(parts[1])))
        elif op == 'l' and len(parts) >= 3:
            vertices.append((float(parts[0]), float(parts[1])))
        elif op == 'h':
            pass
        elif op == 'c':
            # Krzywe w clip path — za skomplikowane
            return None

    if len(vertices) < 3:
        return None

    # Offset na zewnątrz (normal-based)
    poly = np.array(vertices, dtype=np.float64)
    n = len(poly)
    normals = np.zeros_like(poly)

    for j in range(n):
        prev_pt = poly[(j - 1) % n]
        next_pt = poly[(j + 1) % n]
        tangent = next_pt - prev_pt
        normal = np.array([-tangent[1], tangent[0]])
        length = np.linalg.norm(normal)
        if length > 1e-8:
            normal /= length
        normals[j] = normal

    centroid = poly.mean(axis=0)
    test_vec = poly[0] - centroid
    if np.dot(test_vec, normals[0]) < 0:
        normals = -normals

    expanded = poly + normals * bleed_pts

    result: list[str] = []
    result.append(f"{expanded[0][0]:.6f} {expanded[0][1]:.6f} m")
    for j in range(1, n):
        result.append(f"{expanded[j][0]:.6f} {expanded[j][1]:.6f} l")
    result.append("h")

    return result


# =============================================================================
# SEPARATION COLORSPACE + CONTENT STREAM INJECTION
# =============================================================================

def setup_separation_colorspace(
    doc: fitz.Document,
    page: fitz.Page,
    spot_name: str = SPOT_COLOR_CUTCONTOUR,
    cmyk_alternate: tuple = SPOT_CMYK_CUTCONTOUR,
) -> str:
    """Tworzy Separation colorspace i rejestruje w zasobach strony.

    Args:
        doc: dokument PDF
        page: strona PDF
        spot_name: nazwa spot color (np. "CutContour", "FlexCut")
        cmyk_alternate: kolor CMYK alternate (c, m, y, k) 0-1

    Returns:
        Nazwa zasobu colorspace (np. "CS_CutContour")
    """
    c, m, y, k = cmyk_alternate

    func_xref = doc.get_new_xref()
    func_dict = (
        "<<"
        "/FunctionType 2"
        "/Domain [0 1]"
        f"/C0 [0 0 0 0]"
        f"/C1 [{c} {m} {y} {k}]"
        "/N 1"
        ">>"
    )
    doc.update_object(func_xref, func_dict)

    cs_xref = doc.get_new_xref()
    cs_array = f"[/Separation /{spot_name} /DeviceCMYK {func_xref} 0 R]"
    doc.update_object(cs_xref, cs_array)

    cs_resource_name = f"CS_{spot_name}"
    page_xref = page.xref
    res_info = doc.xref_get_key(page_xref, "Resources")

    if res_info[0] == "xref":
        match = re_module.match(r"(\d+)\s+\d+\s+R", res_info[1])
        if match:
            res_xref = int(match.group(1))
            # Sprawdź czy istnieje już ColorSpace
            existing_cs = doc.xref_get_key(res_xref, "ColorSpace")
            if existing_cs[0] == "dict":
                # Dodaj do istniejącego dict
                cs_dict = existing_cs[1].rstrip(">>")
                cs_dict += f"/{cs_resource_name} {cs_xref} 0 R>>"
                doc.xref_set_key(res_xref, "ColorSpace", cs_dict)
            else:
                doc.xref_set_key(
                    res_xref, "ColorSpace",
                    f"<</{cs_resource_name} {cs_xref} 0 R>>",
                )
        else:
            raise ValueError(f"Nie można rozwiązać Resources xref: {res_info[1]}")
    else:
        doc.xref_set_key(
            page_xref, "Resources/ColorSpace",
            f"<</{cs_resource_name} {cs_xref} 0 R>>",
        )

    log.info(f"Separation colorspace /{cs_resource_name} ({spot_name}) zarejestrowany")
    return cs_resource_name


def inject_content_stream(
    doc: fitz.Document, page: fitz.Page, stream_bytes: bytes
) -> None:
    """Dodaje content stream do strony jako nowy xref."""
    xref = doc.get_new_xref()
    doc.update_object(xref, "<<>>")
    doc.update_stream(xref, stream_bytes)

    page_xref = page.xref
    contents = doc.xref_get_key(page_xref, "Contents")

    if contents[0] == "array":
        arr_str = contents[1].rstrip("]") + f" {xref} 0 R]"
        doc.xref_set_key(page_xref, "Contents", arr_str)
    elif contents[0] == "xref":
        existing_xref = contents[1]
        doc.xref_set_key(page_xref, "Contents", f"[{existing_xref} {xref} 0 R]")
    elif contents[0] == "null":
        doc.xref_set_key(page_xref, "Contents", f"[{xref} 0 R]")
    else:
        doc.xref_set_key(page_xref, "Contents", f"[{xref} 0 R]")


# =============================================================================
# SINGLE STICKER EXPORT
# =============================================================================

def export_single_sticker(
    sticker: Sticker,
    output_path: str,
    bleed_mm: float = DEFAULT_BLEED_MM,
) -> dict:
    """Eksportuje pojedynczą naklejkę z bleedem i CutContour.

    3 warstwy (w pełni wektorowe):
      1) Podkład bleed — RGB solid fill z offsetem konturu
      2) Oryginalna grafika wektorowa (show_pdf_page z rozszerzonym MediaBox)
      3) CutContour jako Separation spot color

    Args:
        sticker: Sticker z wypełnionymi polami konturu i bleed
        output_path: ścieżka do pliku wyjściowego
        bleed_mm: wielkość bleed w mm

    Returns:
        dict z informacjami o wygenerowanym PDF
    """
    if sticker.bleed_segments is None:
        raise ValueError("Sticker nie ma bleed_segments — uruchom generate_bleed() najpierw")
    if sticker.edge_color_rgb is None:
        raise ValueError("Sticker nie ma edge_color_rgb — uruchom generate_bleed() najpierw")
    if sticker.pdf_doc is None and sticker.raster_path is None:
        raise ValueError("Sticker nie ma otwartego pdf_doc ani raster_path")

    bleed_pts = bleed_mm * MM_TO_PT
    page_w = sticker.page_width_pt
    page_h = sticker.page_height_pt

    out_w = page_w + 2 * bleed_pts
    out_h = page_h + 2 * bleed_pts

    log.info(
        f"Export: {sticker.source_path} → {output_path} "
        f"({out_w * 25.4/72:.1f}×{out_h * 25.4/72:.1f}mm z bleedem)"
    )

    # Tworzenie PDF wyjściowego
    doc_out = fitz.open()
    out_page = doc_out.new_page(width=out_w, height=out_h)

    # --- WARSTWA 1: Podkład bleed (RGB solid fill) ---
    bleed_stream = build_rgb_fill_stream(
        sticker.bleed_segments, sticker.edge_color_rgb, bleed_pts, out_h
    )
    inject_content_stream(doc_out, out_page, bleed_stream)
    log.info("Warstwa 1: podkład bleed (wektorowy RGB fill) — OK")

    # --- WARSTWA 2: Oryginalna grafika ---
    if sticker.raster_path is not None:
        # Raster: insert_image do target rect (z bleedem)
        target_rect = fitz.Rect(bleed_pts, bleed_pts, bleed_pts + page_w, bleed_pts + page_h)
        out_page.insert_image(target_rect, filename=sticker.raster_path)
        log.info("Warstwa 2: grafika rastrowa (insert_image) — OK")
    else:
        # Wektor: show_pdf_page z rozszerzonym MediaBox
        doc_src = sticker.pdf_doc
        src_page = doc_src[sticker.page_index]

        # Rozszerz clipping paths w content stream źródłowego PDF
        expand_clip_paths(doc_src, src_page, bleed_pts)

        # Usuń CropBox/TrimBox/ArtBox/BleedBox PRZED set_mediabox
        # (PyMuPDF auto-adjustuje CropBox błędnie przy ujemnych współrzędnych)
        src_xref = src_page.xref
        for box in ("CropBox", "TrimBox", "ArtBox", "BleedBox"):
            doc_src.xref_set_key(src_xref, box, "null")

        expanded_rect = fitz.Rect(
            -bleed_pts, -bleed_pts,
            page_w + bleed_pts, page_h + bleed_pts,
        )
        src_page.set_mediabox(expanded_rect)

        target_rect = fitz.Rect(0, 0, out_w, out_h)
        out_page.show_pdf_page(target_rect, doc_src, sticker.page_index)
        log.info("Warstwa 2: grafika wektorowa — OK")

    # --- WARSTWA 3: CutContour spot color ---
    cs_name = setup_separation_colorspace(doc_out, out_page)
    cut_stream = build_cutcontour_stream(
        sticker.cut_segments, bleed_pts, out_h, cs_name
    )
    inject_content_stream(doc_out, out_page, cut_stream)
    log.info("Warstwa 3: CutContour — OK")

    # Zapis
    doc_out.save(output_path)
    doc_out.close()

    log.info(f"Zapisano: {output_path}")

    return {
        'source_path': sticker.source_path,
        'output_path': output_path,
        'page_size_mm': (sticker.width_mm, sticker.height_mm),
        'output_size_mm': (out_w * 25.4 / 72.0, out_h * 25.4 / 72.0),
        'bleed_mm': bleed_mm,
        'num_cut_segments': len(sticker.cut_segments),
        'num_bleed_segments': len(sticker.bleed_segments),
        'edge_color_rgb': sticker.edge_color_rgb,
        'edge_color_cmyk': sticker.edge_color_cmyk,
    }


# =============================================================================
# HELPER: przygotowanie źródłowego PDF do show_pdf_page
# =============================================================================

def _prepare_source_for_embedding(sticker: Sticker, bleed_mm: float) -> fitz.Document:
    """Przygotowuje źródłowy PDF do osadzenia w arkuszu.

    Wyodrębnia pojedynczą stronę do nowego dokumentu z:
      - Rozszerzonymi clip paths
      - Rozszerzonym MediaBox
      - Usuniętymi CropBox/TrimBox/ArtBox/BleedBox

    Zwraca nowy jednostronicowy dokument (strona 0).
    WAŻNE: caller musi zamknąć zwrócony dokument.
    """
    bleed_pts = bleed_mm * MM_TO_PT

    # Wyodrębnij pojedynczą stronę do nowego dokumentu
    # (dla wielostronicowych PDF — unikamy problemów z show_pdf_page)
    doc_single = fitz.open()
    doc_single.insert_pdf(sticker.pdf_doc, from_page=sticker.page_index, to_page=sticker.page_index)
    page_copy = doc_single[0]

    # Rozszerz clipping paths
    expand_clip_paths(doc_single, page_copy, bleed_pts)

    # Usuń CropBox itp. przed set_mediabox
    xref = page_copy.xref
    for box in ("CropBox", "TrimBox", "ArtBox", "BleedBox"):
        doc_single.xref_set_key(xref, box, "null")

    # Rozszerz MediaBox
    page_w = sticker.page_width_pt
    page_h = sticker.page_height_pt
    expanded_rect = fitz.Rect(
        -bleed_pts, -bleed_pts,
        page_w + bleed_pts, page_h + bleed_pts,
    )
    page_copy.set_mediabox(expanded_rect)

    return doc_single


# =============================================================================
# SHEET EXPORT — PRINT PDF
# =============================================================================

def _build_sheet_bleed_fill_stream(
    placement: Placement,
    sheet_h_pt: float,
    bleed_mm: float,
) -> bytes:
    """Buduje content stream: bleed fill dla jednego placement na arkuszu.

    Pozycja naklejki jest w mm, konwertujemy na pt.
    Koordynaty PDF: y-up od dolnej krawędzi.
    """
    sticker = placement.sticker
    if sticker.bleed_segments is None or sticker.edge_color_rgb is None:
        return b""

    bleed_pts = bleed_mm * MM_TO_PT
    px = placement.x_mm * MM_TO_PT  # pozycja w pt na arkuszu
    py = placement.y_mm * MM_TO_PT

    # Rozmiar naklejki z bleedem w pt
    if placement.rotation_deg == 90:
        sticker_w_pt = sticker.page_height_pt
        sticker_h_pt = sticker.page_width_pt
    else:
        sticker_w_pt = sticker.page_width_pt
        sticker_h_pt = sticker.page_height_pt

    out_w = sticker_w_pt + 2 * bleed_pts
    out_h = sticker_h_pt + 2 * bleed_pts

    r, g, b = sticker.edge_color_rgb

    # Transformacja segmentów bleed do pozycji na arkuszu
    # Segmenty są w fitz coords (y-down), musimy:
    # 1. Przeliczyć do PDF coords naklejki (y-up, z bleed offset)
    # 2. Przeliczyć do pozycji na arkuszu (translation + optional rotation)
    ops: list[str] = []
    ops.append(f"{r:.6f} {g:.6f} {b:.6f} rg")
    ops.append("q")  # save graphics state

    # Transformacja: translate do pozycji na arkuszu (PDF y-up)
    # px, py to lewy-dolny róg naklejki w pt (już w PDF coords)
    if placement.rotation_deg == 90:
        # Rotation 90°: translate + rotate
        # Po rotacji 90° CCW: nowy x=stary y, nowy y=-stary x
        # W PDF: cm matrix [cos sin -sin cos tx ty]
        # 90° CCW: [0 1 -1 0 tx ty]
        tx = px + out_h  # po rotacji, origin przesuwa się
        ty = py
        ops.append(f"0 1 -1 0 {tx:.4f} {ty:.4f} cm")
    else:
        ops.append(f"1 0 0 1 {px:.4f} {py:.4f} cm")

    # Teraz rysujemy w lokalnym coordinate system naklejki
    # Segmenty bleed w fitz coords → PDF coords lokalne
    for seg in sticker.bleed_segments:
        if seg[0] == 'l':
            start_x = seg[1][0] + bleed_pts
            start_y = out_h - (seg[1][1] + bleed_pts)
            end_x = seg[2][0] + bleed_pts
            end_y = out_h - (seg[2][1] + bleed_pts)
        elif seg[0] == 'c':
            start_x = seg[1][0] + bleed_pts
            start_y = out_h - (seg[1][1] + bleed_pts)

    # Bardziej bezpośredni sposób: użyj _segments_to_pdf_path_ops
    path_ops = _segments_to_pdf_path_ops(sticker.bleed_segments, bleed_pts, out_h)
    ops.append(path_ops)
    ops.append("f")
    ops.append("Q")  # restore graphics state

    return "\n".join(ops).encode('ascii')


def _build_sheet_cutcontour_stream(
    placement: Placement,
    sheet_h_pt: float,
    bleed_mm: float,
    cs_name: str,
    segments_override: list | None = None,
    flexcut_h_mm: list[float] | None = None,
    flexcut_v_mm: list[float] | None = None,
) -> bytes:
    """Buduje content stream: CutContour stroke dla jednego placement.

    Args:
        placement: Placement z naklejką
        sheet_h_pt: wysokość arkusza w pt
        bleed_mm: bleed w mm
        cs_name: nazwa colorspace
        segments_override: jeśli podane, używa tych segmentów zamiast sticker.cut_segments
            (do użycia z deduplikacją — segmenty już przefiltrowane)
        flexcut_h_mm: pozycje FlexCut poziomych (do filtrowania, gdy brak segments_override)
        flexcut_v_mm: pozycje FlexCut pionowych (do filtrowania, gdy brak segments_override)
    """
    sticker = placement.sticker
    if not sticker.cut_segments:
        return b""

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

    if segments_override is not None:
        # Segmenty już przefiltrowane (deduplikacja + FlexCut)
        segments = segments_override
    else:
        # Filtruj segmenty — usun te które leżą na linii FlexCut
        segments = sticker.cut_segments
        if flexcut_h_mm or flexcut_v_mm:
            segments = _filter_segments_on_flexcut(
                segments, placement, sticker, bleed_mm,
                flexcut_h_mm or [], flexcut_v_mm or [],
            )

    if not segments:
        return b""

    ops: list[str] = []
    ops.append(f"/{cs_name} cs")
    ops.append(f"/{cs_name} CS")
    ops.append("1 SCN")
    ops.append(f"{CUTCONTOUR_STROKE_WIDTH_PT} w")
    ops.append("q")

    if placement.rotation_deg == 90:
        tx = px + out_h
        ty = py
        ops.append(f"0 1 -1 0 {tx:.4f} {ty:.4f} cm")
    else:
        ops.append(f"1 0 0 1 {px:.4f} {py:.4f} cm")

    path_ops = _segments_to_pdf_path_ops(segments, bleed_pts, out_h)
    ops.append(path_ops)
    ops.append("S")
    ops.append("Q")

    return "\n".join(ops).encode('ascii')


def _seg_to_sheet_mm(
    seg, placement: Placement, sticker: Sticker,
) -> tuple[float, float, float, float] | None:
    """Konwertuje punkty segmentu z pt (page coords) na mm (sheet coords).

    Obsługuje rotację 90° placement'u.
    Returns: (sx, sy, ex, ey) w mm sheet coords, lub None.
    """
    pt_to_mm = sticker.width_mm / sticker.page_width_pt if sticker.page_width_pt > 0 else 0
    px_mm = placement.x_mm
    py_mm = placement.y_mm

    if seg[0] == 'l':
        _, start, end = seg
        lx0, ly0 = start[0] * pt_to_mm, start[1] * pt_to_mm
        lx1, ly1 = end[0] * pt_to_mm, end[1] * pt_to_mm
    elif seg[0] == 'c':
        _, p0, _, _, p3 = seg
        lx0, ly0 = p0[0] * pt_to_mm, p0[1] * pt_to_mm
        lx1, ly1 = p3[0] * pt_to_mm, p3[1] * pt_to_mm
    else:
        return None

    # Rotacja 90° — lokalne (x,y) → sheet (y, w-x), gdzie w = sticker.width_mm
    if placement.rotation_deg == 90:
        w = sticker.width_mm
        sx = px_mm + ly0
        sy = py_mm + (w - lx0)
        ex = px_mm + ly1
        ey = py_mm + (w - lx1)
    else:
        sx = px_mm + lx0
        sy = py_mm + ly0
        ex = px_mm + lx1
        ey = py_mm + ly1

    return (sx, sy, ex, ey)


def _make_seg_key(sx: float, sy: float, ex: float, ey: float,
                  tolerance_mm: float = 0.3) -> tuple:
    """Tworzy klucz do deduplikacji segmentów.

    Zaokrągla koordynaty do wielokrotności tolerance_mm i normalizuje
    kierunek (mniejszy punkt pierwszy), aby A→B i B→A były traktowane jako ten sam segment.
    """
    # Zaokrąglij do siatki
    def snap(v):
        return round(v / tolerance_mm) * tolerance_mm

    p1 = (snap(sx), snap(sy))
    p2 = (snap(ex), snap(ey))

    # Normalizuj kierunek — mniejszy punkt pierwszy
    if p1 > p2:
        p1, p2 = p2, p1
    return (p1, p2)


def _deduplicate_cut_segments(
    placements: list[Placement],
    flexcut_h_mm: list[float],
    flexcut_v_mm: list[float],
    bleed_mm: float,
    tolerance_mm: float = 0.3,
) -> list[tuple[Placement, list]]:
    """Deduplikuje segmenty CutContour nakładające się między placementami.

    Gdy gap=0, sąsiednie naklejki mają wspólne krawędzie — te same segmenty
    są generowane dwukrotnie. Ta funkcja zachowuje tylko pierwszą kopię.

    Pipeline:
      1. Dla każdego placement: konwertuj segmenty do sheet coords (mm)
      2. Filtruj segmenty na liniach FlexCut (FlexCut ma priorytet)
      3. Deduplikuj: segment o tych samych współrzędnych (w tolerancji)
         emitowany jest tylko raz (pierwszy placement wygrywa)

    Returns:
        Lista (placement, filtered_segments) — segmenty w oryginalnych
        page-local coords, ale zdeduplikowane.
    """
    seen_keys: set[tuple] = set()
    result: list[tuple[Placement, list]] = []

    for placement in placements:
        sticker = placement.sticker
        if not sticker.cut_segments:
            result.append((placement, []))
            continue

        # Filtruj FlexCut (istniejąca logika)
        segments = sticker.cut_segments
        if flexcut_h_mm or flexcut_v_mm:
            segments = _filter_segments_on_flexcut(
                segments, placement, sticker, bleed_mm,
                flexcut_h_mm, flexcut_v_mm,
            )

        # Deduplikacja
        unique_segments = []
        for seg in segments:
            coords = _seg_to_sheet_mm(seg, placement, sticker)
            if coords is None:
                unique_segments.append(seg)
                continue

            sx, sy, ex, ey = coords
            key = _make_seg_key(sx, sy, ex, ey, tolerance_mm)

            if key not in seen_keys:
                seen_keys.add(key)
                unique_segments.append(seg)
            else:
                log.debug(
                    f"Deduplikacja: pominięto segment "
                    f"({sx:.1f},{sy:.1f})→({ex:.1f},{ey:.1f})mm"
                )

        result.append((placement, unique_segments))

    # Statystyki
    total_orig = sum(len(p.sticker.cut_segments) for p in placements if p.sticker.cut_segments)
    total_dedup = sum(len(segs) for _, segs in result)
    removed = total_orig - total_dedup
    if removed > 0:
        log.info(
            f"Deduplikacja CutContour: {total_orig} → {total_dedup} segmentów "
            f"(usunięto {removed} duplikatów)"
        )

    return result


def _filter_segments_on_flexcut(
    segments: list,
    placement: Placement,
    sticker: Sticker,
    bleed_mm: float,
    flexcut_h_mm: list[float],
    flexcut_v_mm: list[float],
    tolerance_mm: float = 0.5,
) -> list:
    """Filtruje segmenty CutContour — usuwa te leżące na liniach FlexCut.

    Segment jest usuwany jeśli oba jego punkty końcowe (start, end) mają
    tę samą współrzędną Y (segment poziomy) lub X (segment pionowy)
    pokrywającą się z pozycją linii FlexCut.
    """
    if not flexcut_h_mm and not flexcut_v_mm:
        return segments

    pt_to_mm = sticker.width_mm / sticker.page_width_pt if sticker.page_width_pt > 0 else 0
    px_mm = placement.x_mm
    py_mm = placement.y_mm

    def seg_to_sheet_mm(seg):
        """Konwertuje punkty segmentu z pt (page coords) na mm (sheet coords)."""
        if seg[0] == 'l':
            _, start, end = seg
            sx = px_mm + start[0] * pt_to_mm
            sy = py_mm + start[1] * pt_to_mm
            ex = px_mm + end[0] * pt_to_mm
            ey = py_mm + end[1] * pt_to_mm
            return (sx, sy, ex, ey)
        elif seg[0] == 'c':
            _, p0, _, _, p3 = seg
            sx = px_mm + p0[0] * pt_to_mm
            sy = py_mm + p0[1] * pt_to_mm
            ex = px_mm + p3[0] * pt_to_mm
            ey = py_mm + p3[1] * pt_to_mm
            return (sx, sy, ex, ey)
        return None

    filtered = []
    for seg in segments:
        coords = seg_to_sheet_mm(seg)
        if coords is None:
            filtered.append(seg)
            continue

        sx, sy, ex, ey = coords
        skip = False

        # Segment poziomy? (oba punkty mają podobne Y)
        if abs(sy - ey) < tolerance_mm:
            avg_y = (sy + ey) / 2
            for fy in flexcut_h_mm:
                if abs(avg_y - fy) < tolerance_mm:
                    skip = True
                    break

        # Segment pionowy? (oba punkty mają podobne X)
        if not skip and abs(sx - ex) < tolerance_mm:
            avg_x = (sx + ex) / 2
            for fx in flexcut_v_mm:
                if abs(avg_x - fx) < tolerance_mm:
                    skip = True
                    break

        if not skip:
            filtered.append(seg)

    return filtered


def _build_marks_stream(marks: list[Mark], sheet_h_pt: float) -> bytes:
    """Buduje content stream: znaczniki rejestracji (czarne prostokąty/krzyżyki)."""
    if not marks:
        return b""

    ops: list[str] = []
    ops.append("0 0 0 rg")  # Czarny fill
    ops.append("0 0 0 RG")  # Czarny stroke

    for mark in marks:
        x = mark.x_mm * MM_TO_PT
        y = mark.y_mm * MM_TO_PT  # PDF y-up, ale mark.y_mm jest od dołu
        w = mark.width_mm * MM_TO_PT
        h = mark.height_mm * MM_TO_PT

        if mark.mark_type == "opos_rectangle":
            # Wypełniony czarny prostokąt
            ops.append(f"{x:.4f} {y:.4f} {w:.4f} {h:.4f} re")
            ops.append("f")

        elif mark.mark_type == "crosshair":
            # Krzyżyk (stroke)
            cx = x + w / 2
            cy = y + h / 2
            ops.append("0.5 w")  # szerokość linii
            # Horizontal
            ops.append(f"{x:.4f} {cy:.4f} m")
            ops.append(f"{x + w:.4f} {cy:.4f} l")
            ops.append("S")
            # Vertical
            ops.append(f"{cx:.4f} {y:.4f} m")
            ops.append(f"{cx:.4f} {y + h:.4f} l")
            ops.append("S")

        elif mark.mark_type == "crop_mark":
            # L-shaped crop marks w narożnikach
            ops.append("0.5 w")
            # Horizontal
            ops.append(f"{x:.4f} {y:.4f} m")
            ops.append(f"{x + w:.4f} {y:.4f} l")
            ops.append("S")
            # Vertical
            ops.append(f"{x:.4f} {y:.4f} m")
            ops.append(f"{x:.4f} {y + h:.4f} l")
            ops.append("S")

    return "\n".join(ops).encode('ascii')


def _build_flexcut_stream(
    panel_lines: list[PanelLine],
    sheet_w_mm: float,
    sheet_h_mm: float,
    cs_name: str,
    bleed_mm: float = 0.0,
) -> bytes:
    """Buduje content stream: FlexCut linie jako spot color.

    FlexCut to ciągła linia w spot color "FlexCut" — ploter (Summa S3 / FlexiSign)
    sam realizuje perforację na podstawie swoich ustawień. W PDF rysujemy zwykłą linię.

    Pozycje linii FlexCut (panel_lines) są w sheet coords — oś placement'ów
    (content origin). W PDF rendering CutContour jest przesunięty o +bleed_mm
    (bo segmenty rysowane z bleed offset w local coords). Żeby FlexCut
    pokrywał się z krawędziami CutContour, dodajemy bleed_mm do pozycji.
    """
    if not panel_lines:
        return b""

    bleed_pts = bleed_mm * MM_TO_PT

    ops: list[str] = []
    ops.append(f"/{cs_name} cs")
    ops.append(f"/{cs_name} CS")
    ops.append("1 SCN")
    ops.append(f"{FLEXCUT_STROKE_WIDTH_PT} w")

    for line in panel_lines:
        if line.bridge_length_mm <= 0:
            continue  # Nie FlexCut

        if line.axis == "horizontal":
            y_pt = (line.position_mm + bleed_mm) * MM_TO_PT
            x0_pt = (line.start_mm + bleed_mm) * MM_TO_PT
            x1_pt = (line.end_mm + bleed_mm) * MM_TO_PT
            ops.append(f"{x0_pt:.4f} {y_pt:.4f} m")
            ops.append(f"{x1_pt:.4f} {y_pt:.4f} l")
            ops.append("S")
        elif line.axis == "vertical":
            x_pt = (line.position_mm + bleed_mm) * MM_TO_PT
            y0_pt = (line.start_mm + bleed_mm) * MM_TO_PT
            y1_pt = (line.end_mm + bleed_mm) * MM_TO_PT
            ops.append(f"{x_pt:.4f} {y0_pt:.4f} m")
            ops.append(f"{x_pt:.4f} {y1_pt:.4f} l")
            ops.append("S")

    stream = "\n".join(ops)
    return stream.encode('ascii') if ops else b""


# =============================================================================
# SHEET EXPORT — PRINT + CUT PDF
# =============================================================================

def export_sheet_print(
    sheet: Sheet,
    output_path: str,
    bleed_mm: float = DEFAULT_BLEED_MM,
) -> str:
    """Eksportuje print PDF arkusza (bleed fills + grafika + marks).

    Każda naklejka na arkuszu:
      1) Bleed fill (solid RGB) w pozycji placement
      2) Grafika wektorowa (show_pdf_page) w pozycji placement

    Args:
        sheet: Sheet z placements
        output_path: ścieżka do pliku wyjściowego
        bleed_mm: wielkość bleed w mm

    Returns:
        output_path
    """
    bleed_pts = bleed_mm * MM_TO_PT
    sheet_w_pt = sheet.width_mm * MM_TO_PT
    sheet_h_pt = sheet.height_mm * MM_TO_PT

    log.info(
        f"Export print PDF: {sheet.width_mm:.0f}×{sheet.height_mm:.0f}mm, "
        f"{len(sheet.placements)} naklejek"
    )

    doc_out = fitz.open()
    out_page = doc_out.new_page(width=sheet_w_pt, height=sheet_h_pt)

    # Białe tło (domyślne)

    for i, placement in enumerate(sheet.placements):
        sticker = placement.sticker

        if sticker.bleed_segments is None or sticker.edge_color_rgb is None:
            log.warning(f"Placement {i}: brak bleed — pomijam fill")
            continue

        # 1) Bleed fill
        bleed_stream = _build_sheet_bleed_fill_stream(placement, sheet_h_pt, bleed_mm)
        if bleed_stream:
            inject_content_stream(doc_out, out_page, bleed_stream)

        # 2) Grafika (wektorowa lub rastrowa)
        if sticker.raster_path is not None:
            # Raster: insert_image
            sticker_w = sticker.page_width_pt + 2 * bleed_pts
            sticker_h = sticker.page_height_pt + 2 * bleed_pts

            px = placement.x_mm * MM_TO_PT
            py = placement.y_mm * MM_TO_PT

            # Rect dla obrazu (bez bleed — obraz wewnątrz bleed fill)
            img_w = sticker.page_width_pt
            img_h = sticker.page_height_pt

            if placement.rotation_deg == 90:
                # Rotacja 90°: image rect obrócony
                img_rect = fitz.Rect(
                    px + bleed_pts,
                    sheet_h_pt - py - sticker_w + bleed_pts,
                    px + bleed_pts + img_h,
                    sheet_h_pt - py - bleed_pts,
                )
                out_page.insert_image(img_rect, filename=sticker.raster_path, rotate=90)
            else:
                img_rect = fitz.Rect(
                    px + bleed_pts,
                    sheet_h_pt - py - sticker_h + bleed_pts,
                    px + bleed_pts + img_w,
                    sheet_h_pt - py - bleed_pts,
                )
                out_page.insert_image(img_rect, filename=sticker.raster_path)

        elif sticker.pdf_doc is not None:
            # Wektor: show_pdf_page z prepared source
            prepared_doc = _prepare_source_for_embedding(sticker, bleed_mm)

            sticker_w = sticker.page_width_pt + 2 * bleed_pts
            sticker_h = sticker.page_height_pt + 2 * bleed_pts

            px = placement.x_mm * MM_TO_PT
            py = placement.y_mm * MM_TO_PT

            if placement.rotation_deg == 90:
                # Dla rotacji 90°: show_pdf_page z obróconą ramką
                # PDF y-up: py od dołu strony
                target_rect = fitz.Rect(
                    px, sheet_h_pt - py - sticker_w,
                    px + sticker_h, sheet_h_pt - py,
                )
                out_page.show_pdf_page(target_rect, prepared_doc, 0, rotate=90)
            else:
                target_rect = fitz.Rect(
                    px, sheet_h_pt - py - sticker_h,
                    px + sticker_w, sheet_h_pt - py,
                )
                out_page.show_pdf_page(target_rect, prepared_doc, 0)

            prepared_doc.close()

    # Marks (czarne prostokąty/krzyżyki — na wierzchu, po grafice)
    if sheet.marks:
        marks_stream = _build_marks_stream(sheet.marks, sheet_h_pt)
        if marks_stream:
            inject_content_stream(doc_out, out_page, marks_stream)

    doc_out.save(output_path)
    doc_out.close()
    log.info(f"Print PDF zapisany: {output_path}")
    return output_path


def export_sheet_cut(
    sheet: Sheet,
    output_path: str,
    bleed_mm: float = DEFAULT_BLEED_MM,
    plotter: str = "summa_s3",
) -> str:
    """Eksportuje cut PDF arkusza (CutContour + FlexCut + marks, BEZ grafiki).

    Zawartość:
      - CutContour dla każdej naklejki (kontur cięcia)
      - FlexCut linie paneli (perforacja)
      - Full cut linie paneli (pełne cięcie)
      - Znaczniki rejestracji

    Args:
        sheet: Sheet z placements, panel_lines, marks
        output_path: ścieżka do pliku wyjściowego
        bleed_mm: wielkość bleed w mm
        plotter: nazwa plotera

    Returns:
        output_path
    """
    bleed_pts = bleed_mm * MM_TO_PT
    sheet_w_pt = sheet.width_mm * MM_TO_PT
    sheet_h_pt = sheet.height_mm * MM_TO_PT

    log.info(
        f"Export cut PDF: {sheet.width_mm:.0f}×{sheet.height_mm:.0f}mm, "
        f"{len(sheet.placements)} naklejek, {len(sheet.panel_lines)} linii paneli, "
        f"{len(sheet.marks)} markerów"
    )

    doc_out = fitz.open()
    out_page = doc_out.new_page(width=sheet_w_pt, height=sheet_h_pt)

    # Setup spot colors
    cs_cutcontour = setup_separation_colorspace(
        doc_out, out_page, SPOT_COLOR_CUTCONTOUR, SPOT_CMYK_CUTCONTOUR
    )

    # FlexCut spot color (jeśli są linie FlexCut)
    has_flexcut = any(pl.bridge_length_mm > 0 for pl in sheet.panel_lines)
    cs_flexcut = None
    if has_flexcut:
        cs_flexcut = setup_separation_colorspace(
            doc_out, out_page, SPOT_COLOR_FLEXCUT, SPOT_CMYK_FLEXCUT
        )

    # Pozycje FlexCut (do usuwania CutContour na liniach FlexCut)
    # Przy flex_gap=0 FlexCut pokrywa się z CutContour — nie filtrujemy
    # (CutContour = pełne cięcie, FlexCut = perforacja — oba współistnieją)
    fc_gap = getattr(sheet, '_flexcut_gap_mm', None)
    if fc_gap is not None and fc_gap == 0.0:
        flexcut_h = []
        flexcut_v = []
    else:
        flexcut_h = getattr(sheet, '_flexcut_h_lines_mm', [])
        flexcut_v = getattr(sheet, '_flexcut_v_lines_mm', [])

    # Deduplikacja CutContour: filtruje FlexCut + usuwa zduplikowane segmenty
    # (gdy gap<0, sąsiednie naklejki mają wspólne krawędzie)
    deduped = _deduplicate_cut_segments(
        sheet.placements, flexcut_h, flexcut_v, bleed_mm,
    )

    for placement, segments in deduped:
        cut_stream = _build_sheet_cutcontour_stream(
            placement, sheet_h_pt, bleed_mm, cs_cutcontour,
            segments_override=segments,
        )
        if cut_stream:
            inject_content_stream(doc_out, out_page, cut_stream)

    # FlexCut linie paneli (ciągłe linie w spot color FlexCut)
    if cs_flexcut and sheet.panel_lines:
        flexcut_stream = _build_flexcut_stream(
            sheet.panel_lines, sheet.width_mm, sheet.height_mm, cs_flexcut,
            bleed_mm=bleed_mm,
        )
        if flexcut_stream:
            inject_content_stream(doc_out, out_page, flexcut_stream)

    # Marks
    if sheet.marks:
        marks_stream = _build_marks_stream(sheet.marks, sheet_h_pt)
        if marks_stream:
            inject_content_stream(doc_out, out_page, marks_stream)

    doc_out.save(output_path)
    doc_out.close()
    log.info(f"Cut PDF zapisany: {output_path}")
    return output_path


def export_sheet(
    sheet: Sheet,
    print_output_path: str,
    cut_output_path: str,
    bleed_mm: float = DEFAULT_BLEED_MM,
    plotter: str = "summa_s3",
) -> tuple[str, str]:
    """Eksportuje oba PDF-y arkusza (print + cut).

    Returns:
        (print_path, cut_path)
    """
    print_path = export_sheet_print(sheet, print_output_path, bleed_mm)
    cut_path = export_sheet_cut(sheet, cut_output_path, bleed_mm, plotter)
    return print_path, cut_path
