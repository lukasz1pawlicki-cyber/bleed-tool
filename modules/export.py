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
import os
import re as re_module
import tempfile

import fitz  # PyMuPDF
import numpy as np
from PIL import Image as PILImage

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

def inject_page_boundary_clip(
    doc: fitz.Document, page: fitz.Page, bleed_pts: float
) -> None:
    """Wstrzykuje clip path ograniczający rendering do CropBox + bleed.

    Dla plików z TrimBox != MediaBox: content stream zawiera geometrię
    poza TrimBox (markery cięcia, tło). Po set_cropbox() viewport się zmienia,
    ale content stream nie. Ta funkcja dodaje clip path na początku strumienia,
    który maskuje wszystko poza CropBox rozszerzonym o bleed_pts.

    UWAGA: content stream używa współrzędnych MediaBox, nie page.rect.
    page.rect po set_cropbox() jest znormalizowany do (0,0), ale surowe
    bajty strumienia nadal używają oryginalnych współrzędnych.
    """
    # Użyj cropbox — surowe współrzędne w przestrzeni MediaBox
    cb = page.cropbox
    x0 = cb.x0 - bleed_pts
    y0 = cb.y0 - bleed_pts
    x1 = cb.x1 + bleed_pts
    y1 = cb.y1 + bleed_pts
    w = x1 - x0
    h = y1 - y0

    # PDF clip path: rectangle → W n (clip + end path)
    clip_stream = f"q\n{x0:.4f} {y0:.4f} {w:.4f} {h:.4f} re W n\n"

    # Prepend to first content stream
    page_xref = page.xref
    contents_info = doc.xref_get_key(page_xref, "Contents")
    xref_str = contents_info[1]
    xrefs = re_module.findall(r'(\d+)\s+\d+\s+R', xref_str)

    if xrefs:
        first_xr = int(xrefs[0])
        stream = doc.xref_stream(first_xr)
        if stream:
            text = stream.decode('latin-1', errors='replace')
            new_text = clip_stream + text
            doc.update_stream(first_xr, new_text.encode('latin-1'))
            log.info(
                f"Wstrzyknięto boundary clip: ({x0:.1f}, {y0:.1f}, "
                f"{x1:.1f}, {y1:.1f})pt"
            )


def convert_black_to_100k(doc: fitz.Document, page: fitz.Page) -> None:
    """Zamienia kolory czarne na czarny 100% K w content streamach strony.

    Konwertuje:
      - DeviceGray:  '0 g' → '0 0 0 1 k',  '0 G' → '0 0 0 1 K'
      - DeviceRGB:   '0 0 0 rg' → '0 0 0 1 k',  '0 0 0 RG' → '0 0 0 1 K'
      - DeviceCMYK rich black: K≥0.85 z CMY → '0 0 0 1 k/K'
      - Shadingi DeviceN[CMY] nakładające CMY na czarne tło → usunięte
    Próg „czarnego": wartości ≤ 0.1 (gray/RGB), K ≥ 0.85 (CMYK).
    """
    page_xref = page.xref
    contents_info = doc.xref_get_key(page_xref, "Contents")
    xrefs = re_module.findall(r'(\d+)\s+\d+\s+R', contents_info[1])
    if not xrefs:
        return

    count = 0
    for xr_str in xrefs:
        xr = int(xr_str)
        stream = doc.xref_stream(xr)
        if not stream:
            continue
        text = stream.decode('latin-1', errors='replace')
        new_text, n = _convert_black_in_stream(text)
        if n > 0:
            doc.update_stream(xr, new_text.encode('latin-1'))
            count += n

    # Przetworz Form XObjects (mogą zawierać kolory czarne)
    xobj_info = doc.xref_get_key(page_xref, "Resources/XObject")
    if xobj_info[0] != 'null':
        xobj_xrefs = re_module.findall(r'(\d+)\s+\d+\s+R', xobj_info[1])
        for xr_str in xobj_xrefs:
            xr = int(xr_str)
            stream = doc.xref_stream(xr)
            if not stream:
                continue
            text = stream.decode('latin-1', errors='replace')
            new_text, n = _convert_black_in_stream(text)
            if n > 0:
                doc.update_stream(xr, new_text.encode('latin-1'))
                count += n

    # Zamień Separation /All na /Black (registration → pure K)
    count += _convert_separation_all_to_black(doc, page)

    if count:
        log.info(f"Czarny → 100%% K: zamieniono {count} operacji kolorów")


def _convert_separation_all_to_black(doc: fitz.Document, page: fitz.Page) -> int:
    """Zamienia Separation /All na Separation /Black w zasobach strony.

    /Separation /All = registration (C+M+Y+K = tint) → rich black.
    /Separation /Black = tylko K = tint → pure K black.
    """
    count = 0
    page_xref = page.xref
    cs_info = doc.xref_get_key(page_xref, "Resources/ColorSpace")
    if cs_info[0] == 'null':
        return 0

    for m in re_module.finditer(r'/(\w+)\s+(\d+)\s+\d+\s+R', cs_info[1]):
        cs_name = m.group(1)
        cs_xref = int(m.group(2))
        cs_obj = doc.xref_object(cs_xref)

        if '/Separation' in cs_obj and '/All' in cs_obj:
            # Zamień /All na /Black w definicji colorspace
            new_obj = cs_obj.replace('/All', '/Black', 1)
            # Zmień alternate space na DeviceCMYK z tintTransform: tint → 0 0 0 tint
            # Prostszy sposób: zamień tylko nazwę /All → /Black
            new_obj_str = new_obj.strip()
            try:
                doc.update_object(cs_xref, new_obj_str)
                log.info(f"Colorspace /{cs_name}: /Separation /All → /Black")
                count += 1
            except Exception as e:
                log.warning(f"Nie udało się zmienić /{cs_name} /All → /Black: {e}")

    return count


# Regex dla operatorów kolorów (na końcu linii)
_NUM = r'[\d.]+(?:[eE][+-]?\d+)?'
_RE_GRAY_FILL   = re_module.compile(rf'^(\s*)({_NUM})\s+g\s*$')
_RE_GRAY_STROKE = re_module.compile(rf'^(\s*)({_NUM})\s+G\s*$')
_RE_RGB_FILL    = re_module.compile(rf'^(\s*)({_NUM})\s+({_NUM})\s+({_NUM})\s+rg\s*$')
_RE_RGB_STROKE  = re_module.compile(rf'^(\s*)({_NUM})\s+({_NUM})\s+({_NUM})\s+RG\s*$')
_RE_CMYK_FILL   = re_module.compile(rf'^(\s*)({_NUM})\s+({_NUM})\s+({_NUM})\s+({_NUM})\s+k\s*$')
_RE_CMYK_STROKE = re_module.compile(rf'^(\s*)({_NUM})\s+({_NUM})\s+({_NUM})\s+({_NUM})\s+K\s*$')

_BLACK_THRESH = 0.1  # Wartości ≤ tego uznajemy za „czarny" (gray/RGB)
_K_RICH_THRESH = 0.85  # CMYK: K ≥ tego z jakimkolwiek CMY → rich black


def _convert_black_in_stream(text: str) -> tuple[str, int]:
    """Zamienia czarne kolory na 100% K w jednym content stream."""
    lines = text.split('\n')
    count = 0
    result = []

    for line in lines:
        new_line = line

        # DeviceGray fill: '0 g' → '0 0 0 1 k'
        m = _RE_GRAY_FILL.match(line)
        if m:
            val = float(m.group(2))
            if val <= _BLACK_THRESH:
                new_line = f"{m.group(1)}0 0 0 1 k"
                count += 1

        # DeviceGray stroke: '0 G' → '0 0 0 1 K'
        if new_line is line:
            m = _RE_GRAY_STROKE.match(line)
            if m:
                val = float(m.group(2))
                if val <= _BLACK_THRESH:
                    new_line = f"{m.group(1)}0 0 0 1 K"
                    count += 1

        # DeviceRGB fill: '0 0 0 rg' → '0 0 0 1 k'
        if new_line is line:
            m = _RE_RGB_FILL.match(line)
            if m:
                r, g, b = float(m.group(2)), float(m.group(3)), float(m.group(4))
                if r <= _BLACK_THRESH and g <= _BLACK_THRESH and b <= _BLACK_THRESH:
                    new_line = f"{m.group(1)}0 0 0 1 k"
                    count += 1

        # DeviceRGB stroke: '0 0 0 RG' → '0 0 0 1 K'
        if new_line is line:
            m = _RE_RGB_STROKE.match(line)
            if m:
                r, g, b = float(m.group(2)), float(m.group(3)), float(m.group(4))
                if r <= _BLACK_THRESH and g <= _BLACK_THRESH and b <= _BLACK_THRESH:
                    new_line = f"{m.group(1)}0 0 0 1 K"
                    count += 1

        # DeviceCMYK fill: rich black → pure K
        if new_line is line:
            m = _RE_CMYK_FILL.match(line)
            if m:
                c, mk, y, kk = (float(m.group(i)) for i in range(2, 6))
                if kk >= _K_RICH_THRESH and (c > 0.01 or mk > 0.01 or y > 0.01):
                    new_line = f"{m.group(1)}0 0 0 1 k"
                    count += 1

        # DeviceCMYK stroke: rich black → pure K
        if new_line is line:
            m = _RE_CMYK_STROKE.match(line)
            if m:
                c, mk, y, kk = (float(m.group(i)) for i in range(2, 6))
                if kk >= _K_RICH_THRESH and (c > 0.01 or mk > 0.01 or y > 0.01):
                    new_line = f"{m.group(1)}0 0 0 1 K"
                    count += 1

        result.append(new_line)

    return '\n'.join(result), count


def expand_clip_paths(
    doc: fitz.Document, page: fitz.Page, bleed_pts: float,
    rect_only: bool = False,
) -> None:
    """Rozszerza clipping paths (W n / W* n) w content stream strony o bleed_pts.

    Illustrator osadza clip paths (W n) ograniczające rendering do artboardu.
    Aby elementy wychodzące poza stronę (np. białe napisy) były widoczne
    w strefie bleed, musimy rozszerzyć te clip paths.

    Args:
        rect_only: jeśli True, rozszerza TYLKO prostokątne clipy (re W n)
            ORAZ pierwszy napotkany polygon clip (zewnętrzny kontur naklejki).
            Wewnętrzne polygon clipy (dekoracyjne, np. Cyclonic) są pomijane.

    Obsługuje:
    - Prostokąty: `x y w h re W n` → rozszerzony o bleed_pts
    - Polygony/krzywe: `x y m ... l/c ... h W n` → offset (jeśli rect_only=False
      lub pierwszy polygon w rect_only mode)
    """
    page_xref = page.xref
    contents_info = doc.xref_get_key(page_xref, "Contents")
    xref_str = contents_info[1]

    # Zbierz xrefy content streamów
    xrefs = re_module.findall(r'(\d+)\s+\d+\s+R', xref_str)
    if not xrefs:
        return

    modified = False
    first_polygon_expanded = False
    for xr_str in xrefs:
        xr = int(xr_str)
        stream = doc.xref_stream(xr)
        if not stream:
            continue

        text = stream.decode('latin-1', errors='replace')

        new_text, did_expand_polygon = _expand_clips_in_stream(
            text, bleed_pts, rect_only=rect_only,
            first_polygon_expanded=first_polygon_expanded,
        )
        if did_expand_polygon:
            first_polygon_expanded = True
        if new_text != text:
            doc.update_stream(xr, new_text.encode('latin-1'))
            modified = True

    if modified:
        log.info(f"Rozszerzono clipping paths o {bleed_pts:.2f}pt")


def _expand_clips_in_stream(
    text: str, bleed_pts: float, rect_only: bool = False,
    first_polygon_expanded: bool = False,
) -> tuple[str, bool]:
    """Parsuje content stream i rozszerza clip paths o bleed_pts.

    Po rozszerzeniu clip path, szuka macierzy transformacji obrazu (cm + Do)
    i rozszerza ją o bleed_pts, żeby obraz pokrywał rozszerzony clip.

    Args:
        rect_only: jeśli True, rozszerza prostokąty (re) + PIERWSZY polygon clip
            (zewnętrzny kontur naklejki — np. parallelogram w Asset 9).
            Wewnętrzne polygon clipy są pomijane.
        first_polygon_expanded: jeśli True, pierwszy polygon został już rozszerzony
            w poprzednim content streamie — dalsze polygony pomijane.

    Returns:
        (new_text, did_expand_polygon): nowy tekst i czy polygon został rozszerzony.
    """
    lines = text.split('\n')
    result: list[str] = []
    i = 0
    clip_was_expanded = False
    did_expand_polygon = False
    _polygon_done = first_polygon_expanded  # czy pierwszy polygon już obsłużony

    while i < len(lines):
        line = lines[i].strip()

        # Wzorzec 1: "W n" lub "W* n" na jednej linii
        if line in ('W n', 'W* n'):
            # Dla rect_only: rozszerzaj prostokąty zawsze,
            # a pierwszy polygon też (chyba że _polygon_done)
            effective_rect_only = rect_only and _polygon_done
            clip_expanded = _try_expand_clip(result, line, bleed_pts, rect_only=effective_rect_only)
            if clip_expanded:
                result.extend(clip_expanded)
                clip_was_expanded = True
                # Sprawdź czy to był polygon (nie rect)
                if rect_only and not _polygon_done:
                    # Jeśli rozszerzono nie-rect clip → to był polygon
                    # Sprawdź: jeśli _try_expand_clip z rect_only=True by to odrzucił,
                    # to był polygon
                    _polygon_done = True
                    did_expand_polygon = True
            else:
                result.append(lines[i])
            i += 1
            continue

        # Wzorzec 2: "W" lub "W*" na osobnej linii, "n" na następnej
        if line in ('W', 'W*') and i + 1 < len(lines) and lines[i + 1].strip() == 'n':
            clip_op = line + ' n'
            effective_rect_only = rect_only and _polygon_done
            clip_expanded = _try_expand_clip(result, clip_op, bleed_pts, rect_only=effective_rect_only)
            if clip_expanded:
                result.extend(clip_expanded)
                clip_was_expanded = True
                if rect_only and not _polygon_done:
                    _polygon_done = True
                    did_expand_polygon = True
            else:
                result.append(lines[i])
                result.append(lines[i + 1])
            i += 2
            continue

        # Po rozszerzeniu clip: rozszerz macierz transformacji obrazu (cm)
        # TYLKO dla raster-only PDF (nie rect_only) — tam cm definiuje rozmiar obrazu
        if clip_was_expanded and not rect_only and line.endswith(' cm'):
            expanded_cm = _expand_image_matrix(line, bleed_pts)
            if expanded_cm:
                result.append(expanded_cm)
                log.info(f"Rozszerzono macierz obrazu: {line.strip()} → {expanded_cm.strip()}")
                clip_was_expanded = False
                i += 1
                continue

        result.append(lines[i])
        i += 1

    return '\n'.join(result), did_expand_polygon


def _expand_image_matrix(cm_line: str, bleed_pts: float) -> str | None:
    """Rozszerza macierz transformacji obrazu o bleed_pts.

    Macierz: 'sx 0 0 sy tx ty cm' (tylko skala + translacja, bez rotacji/skew).
    Rozszerza obraz o bleed_pts na każdą stronę: zwiększa skalę i przesuwa origin.
    """
    parts = cm_line.strip().split()
    if len(parts) != 7 or parts[6] != 'cm':
        return None

    try:
        a, b, c, d, tx, ty = [float(p) for p in parts[:6]]
    except ValueError:
        return None

    # Tylko proste macierze (skala + translacja): b=0, c=0
    if abs(b) > 0.001 or abs(c) > 0.001:
        return None

    # sx = a (skala X), sy = d (skala Y)
    # Rozszerz: obraz jest większy o 2*bleed na każdą oś, przesunięty o -bleed
    new_a = a + 2 * bleed_pts
    new_d = d + 2 * bleed_pts
    new_tx = tx - bleed_pts
    new_ty = ty - bleed_pts

    return f"{new_a:.5f} {b:.5f} {c:.5f} {new_d:.5f} {new_tx:.4f} {new_ty:.4f} cm"


def _try_expand_clip(
    preceding_lines: list[str], clip_op: str, bleed_pts: float,
    rect_only: bool = False,
) -> list[str] | None:
    """Próbuje rozszerzyć clip path z preceding_lines.

    Zwraca listę linii zastępujących (od początku ścieżki do clip_op włącznie),
    lub None jeśli nie udało się rozpoznać wzorca.

    Args:
        rect_only: jeśli True, rozszerza tylko prostokąty (re), pomija krzywe/polygony.

    Obsługuje:
    - Prostokąty: x y w h re W n
    - Ścieżki z 'h' (explicit close): m ... l/c ... h W n (jeśli rect_only=False)
    - Ścieżki bez 'h' (implicit close): m ... l/c ... W n (jeśli rect_only=False)
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
            if rect_only:
                break  # Pomijaj ścieżki (tylko prostokąty)
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
        if not parts:
            break

        op = parts[-1]

        # moveTo = początek ścieżki → ścieżka bez explicit 'h' (implicit close)
        if op == 'm':
            if rect_only:
                break  # Pomijaj ścieżki (tylko prostokąty)
            polygon_lines = [
                preceding_lines[j].strip()
                for j in range(idx, len(preceding_lines))
            ]
            # Dodaj 'h' do zamknięcia ścieżki
            polygon_lines.append('h')
            expanded_polygon = _expand_polygon_clip(polygon_lines, bleed_pts)
            if expanded_polygon:
                del preceding_lines[idx:]
                return expanded_polygon + [clip_op]
            break

        if op in ('l', 'c'):
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
    """Rozszerza clip path (m/l/c/h) o bleed_pts na zewnątrz.

    Obsługuje zarówno polygony (m/l/h) jak i ścieżki z krzywymi Bézier (m/c/h).
    Używa offset_segments z bleed.py do precyzyjnego offsetu.
    """
    from modules.bleed import offset_segments

    # Parsuj ścieżkę → segmenty [('l', start, end), ('c', p0, p1, p2, p3)]
    segments: list = []
    has_curves = False
    current_pos = None
    first_pos = None

    for pl in polygon_lines:
        parts = pl.split()
        if not parts:
            continue
        op = parts[-1]

        if op == 'm' and len(parts) >= 3:
            x, y = float(parts[0]), float(parts[1])
            current_pos = np.array([x, y])
            first_pos = current_pos.copy()
        elif op == 'l' and len(parts) >= 3:
            x, y = float(parts[0]), float(parts[1])
            end = np.array([x, y])
            if current_pos is not None:
                segments.append(('l', current_pos.copy(), end.copy()))
            current_pos = end
        elif op == 'c' and len(parts) >= 7:
            has_curves = True
            x1, y1 = float(parts[0]), float(parts[1])
            x2, y2 = float(parts[2]), float(parts[3])
            x3, y3 = float(parts[4]), float(parts[5])
            p1 = np.array([x1, y1])
            p2 = np.array([x2, y2])
            p3 = np.array([x3, y3])
            if current_pos is not None:
                segments.append(('c', current_pos.copy(), p1, p2, p3))
            current_pos = p3.copy()
        elif op == 'h':
            # Zamknij ścieżkę
            if current_pos is not None and first_pos is not None:
                dist = np.linalg.norm(current_pos - first_pos)
                if dist > 0.01:
                    segments.append(('l', current_pos.copy(), first_pos.copy()))

    if len(segments) < 2:
        return None

    # Offset segmentów na zewnątrz
    try:
        expanded_segs = offset_segments(segments, bleed_pts)
    except Exception as e:
        log.warning(f"Offset clip path failed: {e}")
        return None

    if not expanded_segs:
        return None

    # Rekonstrukcja PDF path operators
    result: list[str] = []
    first_seg = expanded_segs[0]
    start_pt = first_seg[1]
    result.append(f"{start_pt[0]:.6f} {start_pt[1]:.6f} m")

    for seg in expanded_segs:
        if seg[0] == 'l':
            end = seg[2]
            result.append(f"{end[0]:.6f} {end[1]:.6f} l")
        elif seg[0] == 'c':
            p1, p2, p3 = seg[2], seg[3], seg[4]
            result.append(
                f"{p1[0]:.6f} {p1[1]:.6f} {p2[0]:.6f} {p2[1]:.6f} "
                f"{p3[0]:.6f} {p3[1]:.6f} c"
            )
    result.append("h")

    log.info(f"Rozszerzono clip path ({len(segments)} seg, curves={has_curves})")
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

def _render_bleed_mask(
    bleed_segments: list,
    img_w: int, img_h: int,
    page_w_pt: float, page_h_pt: float,
    bleed_pts: float,
) -> np.ndarray:
    """Rysuje bleed_segments jako wypełnioną maskę alpha (gładki kształt).

    Przelicza współrzędne pt → px i rysuje wypełniony polygon/Bézier
    na bitmapie img_w × img_h. Zwraca tablicę uint8 (h, w) z wartościami 0/255.
    """
    from PIL import ImageDraw

    mask = PILImage.new("L", (img_w, img_h), 0)
    draw = ImageDraw.Draw(mask)

    # Przelicznik pt → px (expanded canvas: bleed_pts offset)
    px_per_pt_x = img_w / (page_w_pt + 2 * bleed_pts)
    px_per_pt_y = img_h / (page_h_pt + 2 * bleed_pts)

    # Zbierz punkty ścieżki — flatten Bézier do polyline
    points = []
    for seg in bleed_segments:
        seg_type = seg[0]
        if seg_type == 'l':
            # Linia: ('l', start_pt, end_pt) — współrzędne w pt, origin = (0,0)
            # W expanded canvas: przesunięcie o bleed_pts
            sx = (seg[1][0] + bleed_pts) * px_per_pt_x
            sy = (seg[1][1] + bleed_pts) * px_per_pt_y
            ex = (seg[2][0] + bleed_pts) * px_per_pt_x
            ey = (seg[2][1] + bleed_pts) * px_per_pt_y
            points.append((sx, sy))
            points.append((ex, ey))
        elif seg_type == 'c':
            # Bézier: ('c', p0, p1, p2, p3) — flatten do 20 punktów
            p0, p1, p2, p3 = seg[1], seg[2], seg[3], seg[4]
            for i in range(21):
                t = i / 20.0
                t2 = t * t
                t3 = t2 * t
                mt = 1 - t
                mt2 = mt * mt
                mt3 = mt2 * mt
                x = mt3 * p0[0] + 3 * mt2 * t * p1[0] + 3 * mt * t2 * p2[0] + t3 * p3[0]
                y = mt3 * p0[1] + 3 * mt2 * t * p1[1] + 3 * mt * t2 * p2[1] + t3 * p3[1]
                px_x = (x + bleed_pts) * px_per_pt_x
                px_y = (y + bleed_pts) * px_per_pt_y
                points.append((px_x, px_y))

    if len(points) >= 3:
        draw.polygon(points, fill=255)

    return np.array(mask)


def _fill_transparent_pixels(rgba: np.ndarray, max_grow_px: int) -> np.ndarray:
    """Wypełnia przezroczyste piksele kolorami z najbliższego opaque sąsiada.

    Iteracyjna dylatacja 4-sąsiadowa — nearest-neighbor (bez uśredniania).
    Każdy nowy piksel kopiuje kolor z jednego sąsiada → jednolite kolory.
    Dylatacja ograniczona do max_grow_px iteracji — piksele dalej niż
    max_grow_px od treści pozostają przezroczyste.

    Zwraca tablicę RGBA (h, w, 4) uint8 — niewypełnione piksele mają alpha=0.
    """
    h, w = rgba.shape[:2]
    result = rgba[:, :, :3].copy()
    filled = rgba[:, :, 3] > 128

    for _ in range(max_grow_px):
        if filled.all():
            break

        new_filled = filled.copy()
        new_result = result.copy()
        unfilled = ~filled

        # Sprawdź 4 kierunki — kopiuj kolor z pierwszego dostępnego sąsiada
        for dy, dx in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
            neighbor_ok = np.zeros_like(filled)
            neighbor_rgb = np.zeros_like(result)

            sy = slice(max(0, -dy), h + min(0, -dy))
            sx = slice(max(0, -dx), w + min(0, -dx))
            ty = slice(max(0, dy), h + min(0, dy))
            tx = slice(max(0, dx), w + min(0, dx))

            neighbor_ok[ty, tx] = filled[sy, sx]
            neighbor_rgb[ty, tx] = result[sy, sx]

            can_fill = unfilled & neighbor_ok & (~new_filled)
            new_result[can_fill] = neighbor_rgb[can_fill]
            new_filled |= can_fill

        result = new_result
        filled = new_filled

    # RGBA — niewypełnione piksele przezroczyste
    alpha_out = np.where(filled, 255, 0).astype(np.uint8)
    return np.dstack([result, alpha_out])


def _create_edge_extended_image(img: PILImage.Image, bleed_px: int) -> PILImage.Image:
    """Tworzy obraz z rozciągniętymi krawędziami (edge clamping).

    Każdy piksel krawędzi jest rozciągany na zewnątrz o bleed_px pikseli.
    Daje efekt lokalnie dopasowanego koloru bleed.
    """
    arr = np.array(img.convert("RGB"))
    h, w = arr.shape[:2]
    bp = bleed_px

    new_h = h + 2 * bp
    new_w = w + 2 * bp
    ext = np.zeros((new_h, new_w, 3), dtype=arr.dtype)

    # Środek — oryginał
    ext[bp:bp + h, bp:bp + w] = arr

    # Górna krawędź — powtórzenie pierwszego wiersza
    ext[:bp, bp:bp + w] = arr[0:1, :]
    # Dolna krawędź — powtórzenie ostatniego wiersza
    ext[bp + h:, bp:bp + w] = arr[-1:, :]
    # Lewa krawędź — powtórzenie pierwszej kolumny
    ext[bp:bp + h, :bp] = arr[:, 0:1]
    # Prawa krawędź — powtórzenie ostatniej kolumny
    ext[bp:bp + h, bp + w:] = arr[:, -1:]

    # Narożniki — piksel narożny
    ext[:bp, :bp] = arr[0, 0]
    ext[:bp, bp + w:] = arr[0, -1]
    ext[bp + h:, :bp] = arr[-1, 0]
    ext[bp + h:, bp + w:] = arr[-1, -1]

    return PILImage.fromarray(ext)


def export_single_sticker(
    sticker: Sticker,
    output_path: str,
    bleed_mm: float = DEFAULT_BLEED_MM,
    black_100k: bool = False,
    cutcontour: bool = True,
) -> dict:
    """Eksportuje pojedynczą naklejkę z bleedem i opcjonalnym CutContour.

    2-3 warstwy (w pełni wektorowe):
      1) Podkład bleed — RGB solid fill z offsetem konturu
      2) Oryginalna grafika wektorowa (show_pdf_page z rozszerzonym MediaBox)
      3) CutContour jako Separation spot color (opcjonalnie)

    Args:
        sticker: Sticker z wypełnionymi polami konturu i bleed
        output_path: ścieżka do pliku wyjściowego
        black_100k: zamiana czarnych kolorów na 100%% K
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

    # --- WARSTWA 1+2: Bleed + grafika ---
    _is_raster_only_pdf = (
        sticker.raster_path is None
        and sticker.pdf_doc is not None
        and sticker.outermost_drawing_idx is None
    )

    # Sprawdź czy raster-only PDF z clip path powinien iść ścieżką wektorową
    _use_vector_for_raster_pdf = False
    if _is_raster_only_pdf:
        # Sprawdź czy content stream zawiera clip path z krzywymi (np. okrągła naklejka)
        # Jeśli tak — ścieżka wektorowa da lepszy wynik (expand clip + show_pdf_page)
        _page_tmp = sticker.pdf_doc[sticker.page_index]
        _xref_tmp = _page_tmp.xref
        _ci = sticker.pdf_doc.xref_get_key(_xref_tmp, "Contents")
        _xrefs_tmp = re_module.findall(r'(\d+)\s+\d+\s+R', _ci[1])
        for _xr in _xrefs_tmp:
            _stream = sticker.pdf_doc.xref_stream(int(_xr))
            if _stream:
                _text = _stream.decode('latin-1', errors='replace')
                # Szukaj wzorca: krzywe Bézier + clip (c ... W* n lub W n)
                # Uwaga: content stream może mieć \r\n lub \n
                _text_norm = _text.replace('\r\n', '\n')
                if ' c\n' in _text_norm and ('W*' in _text_norm or 'W n' in _text_norm):
                    _use_vector_for_raster_pdf = True
                    log.info("Raster-only PDF z clip path krzywych → ścieżka wektorowa")
                    break

    if sticker.raster_path is not None or (_is_raster_only_pdf and not _use_vector_for_raster_pdf):
        # Raster / raster-only PDF: bleed via edge-clamping lub dilation
        if sticker.raster_path is not None:
            # Plik rastrowy (PNG/JPG/TIFF)
            src_pil = PILImage.open(sticker.raster_path)
            has_raster_alpha = src_pil.mode in ('RGBA', 'LA', 'PA')

            if has_raster_alpha:
                # Obraz z alpha — expanded canvas + dilation + bleed mask
                # 1) Dylatacja wypełnia kolory bleed (pełny zasięg)
                # 2) Maska z bleed_segments ogranicza do gładkiego kształtu
                rgba = np.array(src_pil.convert('RGBA'))
                bleed_px = max(1, round(bleed_pts * rgba.shape[1] / page_w))
                h_r, w_r = rgba.shape[:2]
                new_h = h_r + 2 * bleed_px
                new_w = w_r + 2 * bleed_px
                expanded = np.zeros((new_h, new_w, 4), dtype=np.uint8)
                expanded[bleed_px:bleed_px + h_r, bleed_px:bleed_px + w_r] = rgba

                # Dylatacja — wypełnij kolory wystarczająco daleko
                fill_range = bleed_px * 3
                rgba_filled = _fill_transparent_pixels(expanded, fill_range)
                rgb_filled = rgba_filled[:, :, :3]

                # Maska z bleed_segments — gładki kształt (okrąg/polygon)
                bleed_mask = _render_bleed_mask(
                    sticker.bleed_segments, new_w, new_h,
                    page_w, page_h, bleed_pts,
                )
                result_rgba = np.dstack([rgb_filled, bleed_mask])
                ext_img = PILImage.fromarray(result_rgba, "RGBA")
                log.info(
                    f"Raster alpha: bleed via dilation + mask "
                    f"({w_r}x{h_r} -> {new_w}x{new_h}, bleed_px={bleed_px})"
                )
            else:
                # Obraz bez alpha — prostokątny edge-clamping
                src_img = src_pil.convert("RGB")
                bleed_px = max(1, round(bleed_pts * src_img.width / page_w))
                ext_img = _create_edge_extended_image(src_img, bleed_px)
                src_img.close()

            src_pil.close()
        else:
            # Raster-only PDF: render z alpha
            page_src = sticker.pdf_doc[sticker.page_index]
            pix_per_pt = 300.0 / 72.0  # render na 300 DPI
            mat = fitz.Matrix(pix_per_pt, pix_per_pt)
            pix = page_src.get_pixmap(matrix=mat, alpha=True)
            rgba = np.frombuffer(
                pix.samples, dtype=np.uint8
            ).reshape(pix.height, pix.width, 4)

            has_transparency = np.any(rgba[:, :, 3] < 250)
            if has_transparency:
                # Grafika z przezroczystością — expanded canvas + dilation + mask
                bleed_px = max(1, round(bleed_pts * pix.width / page_w))
                h, w = rgba.shape[:2]
                new_h = h + 2 * bleed_px
                new_w = w + 2 * bleed_px
                expanded = np.zeros((new_h, new_w, 4), dtype=np.uint8)
                expanded[bleed_px:bleed_px + h, bleed_px:bleed_px + w] = rgba
                fill_range = bleed_px * 3
                rgba_filled = _fill_transparent_pixels(expanded, fill_range)
                rgb_filled = rgba_filled[:, :, :3]

                bleed_mask = _render_bleed_mask(
                    sticker.bleed_segments, new_w, new_h,
                    page_w, page_h, bleed_pts,
                )
                result_rgba = np.dstack([rgb_filled, bleed_mask])
                ext_img = PILImage.fromarray(result_rgba, "RGBA")
                log.info(
                    f"Raster-only PDF: bleed via dilation + mask "
                    f"({w}x{h} -> {new_w}x{new_h}, bleed_px={bleed_px})"
                )
            else:
                # Brak przezroczystości: prostokątny edge-clamping
                src_img = PILImage.fromarray(rgba[:, :, :3], "RGB")
                bleed_px = max(1, round(bleed_pts * src_img.width / page_w))
                ext_img = _create_edge_extended_image(src_img, bleed_px)
                src_img.close()

        # Zapisz do pliku tymczasowego i wstaw na pełną stronę
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.close()
        ext_img.save(tmp.name)
        ext_img.close()

        full_rect = fitz.Rect(0, 0, out_w, out_h)
        out_page.insert_image(full_rect, filename=tmp.name)
        os.unlink(tmp.name)
        log.info("Warstwa 1+2: bleed + grafika rastrowa — OK")
    else:
        # Wektor PDF: solid fill + show_pdf_page
        bleed_stream = build_rgb_fill_stream(
            sticker.bleed_segments, sticker.edge_color_rgb, bleed_pts, out_h
        )
        inject_content_stream(doc_out, out_page, bleed_stream)
        log.info("Warstwa 1: podkład bleed (wektorowy RGB fill) — OK")

        doc_src = sticker.pdf_doc
        src_page = doc_src[sticker.page_index]

        # Zapamiętaj CropBox (surowe współrzędne) PRZED usunięciem
        src_cropbox = src_page.cropbox

        # Czarny 100% K: zamiana kolorów przed osadzeniem
        if black_100k:
            convert_black_to_100k(doc_src, src_page)

        # Ogranicz rendering do CropBox + bleed (maskuje markery cięcia)
        inject_page_boundary_clip(doc_src, src_page, bleed_pts)

        # Rozszerz clipping paths:
        # - Raster-only z clip path: rozszerz WSZYSTKIE clipy (w tym krzywe)
        # - Zwykłe wektorowe PDF: rozszerz TYLKO prostokątne clipy (artboard clips)
        #   żeby nie psuć wewnętrznych clip paths (np. elementy loga Cyclonic)
        if _use_vector_for_raster_pdf:
            expand_clip_paths(doc_src, src_page, bleed_pts)
        else:
            expand_clip_paths(doc_src, src_page, bleed_pts, rect_only=True)

        # Usuń CropBox/TrimBox/ArtBox/BleedBox PRZED set_mediabox
        src_xref = src_page.xref
        for box in ("CropBox", "TrimBox", "ArtBox", "BleedBox"):
            doc_src.xref_set_key(src_xref, box, "null")

        # Expanded MediaBox wokół CropBox (nie wokół 0,0)
        expanded_rect = fitz.Rect(
            src_cropbox.x0 - bleed_pts, src_cropbox.y0 - bleed_pts,
            src_cropbox.x1 + bleed_pts, src_cropbox.y1 + bleed_pts,
        )
        src_page.set_mediabox(expanded_rect)

        target_rect = fitz.Rect(0, 0, out_w, out_h)
        out_page.show_pdf_page(target_rect, doc_src, sticker.page_index)
        log.info("Warstwa 2: grafika wektorowa — OK")

    # --- WARSTWA 3: CutContour spot color (opcjonalna) ---
    if cutcontour:
        cs_name = setup_separation_colorspace(doc_out, out_page)
        cut_stream = build_cutcontour_stream(
            sticker.cut_segments, bleed_pts, out_h, cs_name
        )
        inject_content_stream(doc_out, out_page, cut_stream)
        log.info("Warstwa 3: CutContour — OK")
    else:
        log.info("Warstwa 3: CutContour — pominięta (sam spad)")

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

def _strip_cutcontour_streams(doc: fitz.Document, page: fitz.Page) -> None:
    """Usuwa strumienie zawierające CutContour ze strony.

    Dla plików bleed_ output: CutContour jest ostatnim content streamem.
    Wykrywamy go po obecności 'CutContour' w bajtach strumienia.
    """
    contents_info = doc.xref_get_key(page.xref, "Contents")
    if contents_info[0] == "array":
        xrefs = [int(x) for x in re_module.findall(r'(\d+)\s+\d+\s+R', contents_info[1])]
        filtered = []
        for xref in xrefs:
            try:
                sd = doc.xref_stream(xref)
                if sd is None or b"CutContour" not in sd:
                    filtered.append(xref)
                else:
                    log.debug(f"_strip_cutcontour_streams: usunięto xref {xref} (CutContour)")
            except Exception:
                filtered.append(xref)
        if len(filtered) < len(xrefs):
            if filtered:
                arr = " ".join(f"{x} 0 R" for x in filtered)
                doc.xref_set_key(page.xref, "Contents", f"[{arr}]")
            else:
                doc.xref_set_key(page.xref, "Contents", "null")
    elif contents_info[0] == "xref":
        xref_str = contents_info[1]
        m = re_module.search(r'(\d+)\s+\d+\s+R', xref_str)
        if m:
            xref = int(m.group(1))
            try:
                sd = doc.xref_stream(xref)
                if sd and b"CutContour" in sd:
                    doc.xref_set_key(page.xref, "Contents", "null")
                    log.debug(f"_strip_cutcontour_streams: usunięto xref {xref} (CutContour)")
            except Exception:
                pass


def _prepare_source_for_embedding(sticker: Sticker, bleed_mm: float) -> fitz.Document:
    """Przygotowuje źródłowy PDF do osadzenia w arkuszu.

    Wyodrębnia pojedynczą stronę do nowego dokumentu z:
      - Rozszerzonymi clip paths
      - Rozszerzonym MediaBox
      - Usuniętymi CropBox/TrimBox/ArtBox/BleedBox

    Dla sticker.is_bleed_output=True: nie rozszerza MediaBox (bleed już w grafice),
    tylko usuwa CutContour ze strumieni.

    Zwraca nowy jednostronicowy dokument (strona 0).
    WAŻNE: caller musi zamknąć zwrócony dokument.
    """
    bleed_pts = bleed_mm * MM_TO_PT

    # Wyodrębnij pojedynczą stronę do nowego dokumentu
    # (dla wielostronicowych PDF — unikamy problemów z show_pdf_page)
    doc_single = fitz.open()
    doc_single.insert_pdf(sticker.pdf_doc, from_page=sticker.page_index, to_page=sticker.page_index)
    page_copy = doc_single[0]

    if getattr(sticker, 'is_bleed_output', False):
        # Plik bleed_ output — bleed już wbudowany w grafikę
        # Tylko usuń CutContour (nie pokazuj w print PDF) + wyczyść nadmiarowe boxy
        xref = page_copy.xref
        for box in ("CropBox", "TrimBox", "ArtBox", "BleedBox"):
            doc_single.xref_set_key(xref, box, "null")
        _strip_cutcontour_streams(doc_single, page_copy)
        return doc_single

    # Rozszerz clipping paths TYLKO dla raster-only PDF z clip path (np. okrągła naklejka)
    _is_raster_only = sticker.outermost_drawing_idx is None and sticker.raster_path is None
    if _is_raster_only:
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

    # Deduplikacja linii FlexCut — sub-arkusze mogą mieć wspólne krawędzie.
    # Maszyna nie może ciąć 2× w tym samym miejscu (overlap).
    # Klucz: (axis, round(position, 1), round(start, 1), round(end, 1))
    seen: set[tuple] = set()
    unique_lines: list = []
    for line in panel_lines:
        if line.bridge_length_mm <= 0:
            continue
        key = (line.axis, round(line.position_mm, 1),
               round(min(line.start_mm, line.end_mm), 1),
               round(max(line.start_mm, line.end_mm), 1))
        if key not in seen:
            seen.add(key)
            unique_lines.append(line)

    if not unique_lines:
        return b""

    if len(unique_lines) < len([l for l in panel_lines if l.bridge_length_mm > 0]):
        log.info(f"FlexCut deduplikacja: {len(panel_lines)} → {len(unique_lines)} linii")

    ops: list[str] = []
    ops.append(f"/{cs_name} cs")
    ops.append(f"/{cs_name} CS")
    ops.append("1 SCN")
    ops.append(f"{FLEXCUT_STROKE_WIDTH_PT} w")

    for line in unique_lines:
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
