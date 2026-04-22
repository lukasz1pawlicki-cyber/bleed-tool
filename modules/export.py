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
    SPOT_COLOR_WHITE,
    CUT_CMYK_CUTCONTOUR,
    CUT_CMYK_FLEXCUT,
    CUT_CMYK_REGMARK,
    CUT_SUMMA_LAYERS,
    CUT_JWEI_LAYERS,
    SPOT_CMYK_CUTCONTOUR,
    SPOT_CMYK_FLEXCUT,
    SPOT_CMYK_WHITE,
    SPOT_COLOR_REGMARK,
    SPOT_CMYK_REGMARK,
    WHITE_INSET_MM,
)

log = logging.getLogger(__name__)


# =============================================================================
# HELPERY: bezpieczny tempfile + kontrola pamięci
# =============================================================================

# Limit megapikseli dla pojedynczego rastra w eksporcie.
# Próg dobrany dla maszyn produkcyjnych (minimum 4 GB wolnego RAM).
# A2 @ 300dpi ≈ 35 MP, A1 @ 300dpi ≈ 70 MP → domyślny limit pozwala na A1
# z kanałem alpha (70 MP × 4 B = 280 MB) + expanded canvas z bleedem.
_MAX_RASTER_MEGAPIXELS = int(os.environ.get("BLEED_MAX_RASTER_MP", "120"))


def _check_raster_memory(width: int, height: int, channels: int = 4,
                         context: str = "raster") -> None:
    """Sprawdza czy alokacja (width × height × channels) mieści się w limicie.

    Rzuca MemoryError z komunikatem PL dla operatora zanim nastąpi faktyczna
    alokacja numpy, która mogłaby zabić proces bez jasnego powodu.
    """
    megapixels = (width * height) / 1_000_000.0
    if megapixels > _MAX_RASTER_MEGAPIXELS:
        estimated_mb = (width * height * channels) / 1024 / 1024
        raise MemoryError(
            f"Zbyt duży obraz do eksportu ({context}): "
            f"{width}×{height}px = {megapixels:.0f} MP (≈{estimated_mb:.0f} MB). "
            f"Limit: {_MAX_RASTER_MEGAPIXELS} MP. "
            f"Zredukuj DPI lub format arkusza, albo zwiększ BLEED_MAX_RASTER_MP."
        )


class _SafeTempPng:
    """Context manager dla tymczasowego PNG — gwarantuje cleanup.

    Zwykłe `tempfile.NamedTemporaryFile(delete=False)` + `os.unlink(tmp.name)`
    na końcu nie chroni przed wyciekiem gdy pomiędzy save() a unlink() zostanie
    wyrzucony wyjątek. Context manager zapewnia usunięcie w finally.
    """

    def __init__(self, suffix: str = ".png"):
        self._suffix = suffix
        self.name: str = ""

    def __enter__(self) -> "_SafeTempPng":
        tmp = tempfile.NamedTemporaryFile(suffix=self._suffix, delete=False)
        tmp.close()
        self.name = tmp.name
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.name and os.path.exists(self.name):
            try:
                os.unlink(self.name)
            except OSError as e:
                log.warning(f"Nie udało się usunąć pliku tymczasowego {self.name}: {e}")


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


def build_cmyk_fill_stream(
    segments: list,
    cmyk: tuple[float, float, float, float],
    bleed_pts: float,
    out_h: float,
) -> bytes:
    """Buduje content stream: CMYK fill z segmentów (warstwa 1 — bleed).

    Używa DeviceCMYK (operator k) — prepress-ready.
    """
    c, m, y, k = cmyk
    path_ops = _segments_to_pdf_path_ops(segments, bleed_pts, out_h)
    stream = f"{c:.6f} {m:.6f} {y:.6f} {k:.6f} k\n{path_ops}\nf"
    return stream.encode('ascii')


def build_bleed_frame_stream(
    rgb: tuple[float, float, float],
    cmyk: tuple[float, float, float, float] | None,
    bleed_pts: float,
    out_w: float,
    out_h: float,
) -> bytes:
    """Ramka spadu: pokrywa TYLKO strefę bleed (poza trim, wewnątrz page).

    Even-odd fill: outer rect (pełna strona) minus inner rect (trim area).
    Nakładana PO grafice — maskuje artefakty na granicy (białe tło z Canva).
    """
    # Outer rect: full page
    # Inner rect: trim area (inset by bleed_pts)
    ix, iy = bleed_pts, bleed_pts
    iw, ih = out_w - 2 * bleed_pts, out_h - 2 * bleed_pts
    if cmyk is not None:
        c, m, y, k = cmyk
        color_op = f"{c:.6f} {m:.6f} {y:.6f} {k:.6f} k"
    else:
        r, g, b = rgb
        color_op = f"{r:.6f} {g:.6f} {b:.6f} rg"
    stream = (
        f"q\n"
        f"{color_op}\n"
        f"0 0 {out_w:.4f} {out_h:.4f} re\n"
        f"{ix:.4f} {iy:.4f} {iw:.4f} {ih:.4f} re\n"
        f"f*\n"
        f"Q"
    )
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


def build_white_fill_stream(
    segments: list,
    bleed_pts: float,
    out_h: float,
    cs_name: str = "CS_White",
) -> bytes:
    """Buduje content stream: White fill z segmentów (bialy poddruk).

    Solid fill w spot color "White" — nakładany WEWNATRZ konturu ciecia.
    Umieszczany miedzy warstwa bleed a grafika (drukarka drukuje bialy tusz
    pod grafika na przezroczystym/metalicznym podlozu).
    """
    path_ops = _segments_to_pdf_path_ops(segments, bleed_pts, out_h)
    if not path_ops:
        return b""
    stream = (
        f"/{cs_name} cs\n"
        f"1 scn\n"
        f"{path_ops}\n"
        f"f"
    )
    return stream.encode('ascii')


def _get_white_segments(bleed_segments: list, cut_segments: list | None = None) -> list:
    """Zwraca segmenty do white fill z insetem WHITE_INSET_MM od linii cięcia.

    Inset zapobiega wystaniu białego tuszu na krawędziach naklejki.
    Jeśli cut_segments podane i inset > 0 — oblicza offset do wewnątrz.
    Fallback: zwraca bleed_segments bez insetu.
    """
    if not bleed_segments:
        return []

    if WHITE_INSET_MM <= 0 or cut_segments is None or not cut_segments:
        return list(bleed_segments)

    inset_pt = WHITE_INSET_MM * MM_TO_PT
    try:
        from modules.bleed import flatten_segments_to_polyline, offset_polyline
        from modules.bleed import _fit_cubic_bezier

        polyline, boundaries = flatten_segments_to_polyline(cut_segments, 30)
        inset_poly = offset_polyline(polyline, -inset_pt)

        result = []
        for seg_idx, seg in enumerate(cut_segments):
            start_b = boundaries[seg_idx]
            end_b = (
                boundaries[seg_idx + 1]
                if seg_idx + 1 < len(boundaries)
                else boundaries[0]
            )

            if seg[0] == 'l':
                result.append((
                    'l',
                    inset_poly[start_b].copy(),
                    inset_poly[end_b].copy(),
                ))
            elif seg[0] == 'c':
                if end_b > start_b:
                    seg_pts = inset_poly[start_b:end_b + 1]
                else:
                    seg_pts = np.vstack([
                        inset_poly[start_b:],
                        inset_poly[:end_b + 1],
                    ])
                if len(seg_pts) < 2:
                    result.append((
                        'l',
                        inset_poly[start_b].copy(),
                        inset_poly[end_b].copy(),
                    ))
                else:
                    p1, p2 = _fit_cubic_bezier(seg_pts)
                    result.append((
                        'c',
                        seg_pts[0].copy(),
                        p1, p2,
                        seg_pts[-1].copy(),
                    ))

        log.info(f"White inset: {WHITE_INSET_MM}mm ({inset_pt:.2f}pt), {len(result)} segmentów")
        return result
    except Exception as e:
        log.warning(f"White inset nieudany ({e}), używam bleed_segments bez insetu")
        return list(bleed_segments)


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
    # Normalizacja: zamień \r\n na \n, kompresuj wielokrotne spacje
    text = text.replace('\r\n', '\n').replace('\r', '\n')
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


def expand_page_fills(
    doc: fitz.Document, page: fitz.Page, bleed_pts: float,
    page_w_pt: float, page_h_pt: float,
    edge_x0: float = 0.0, edge_y0: float = 0.0,
    edge_x1: float | None = None, edge_y1: float | None = None,
) -> None:
    """Rozszerza prostokątne fill-e tła o bleed_pts w content stream strony.

    Szuka `re f` lub `re f*` gdzie prostokąt odpowiada wymiarom trim
    (uwzględniając aktywną macierz skalowania). Gdy znajdzie — rozszerza
    o bleed_pts w tej samej przestrzeni koordynatów.

    Rozwiązuje problem Canva-style PDF gdzie tło (white + color fill)
    pokrywa dokładnie stronę, ale nie rozciąga się na bleed zone.

    Parametry edge_* opisują pozycję trim w oryginalnych page-coords
    (gdy CropBox jest offset od MediaBox). Domyślnie: origin (0,0)
    + (page_w_pt, page_h_pt) — dla stron bez offsetu CropBox.
    """
    if edge_x1 is None:
        edge_x1 = page_w_pt
    if edge_y1 is None:
        edge_y1 = page_h_pt

    page_xref = page.xref
    contents_info = doc.xref_get_key(page_xref, "Contents")
    xref_str = contents_info[1]
    xrefs = re_module.findall(r'(\d+)\s+\d+\s+R', xref_str)
    if not xrefs:
        return

    modified = False
    for xr_str in xrefs:
        xr = int(xr_str)
        stream = doc.xref_stream(xr)
        if not stream:
            continue
        text = stream.decode('latin-1', errors='replace')
        new_text = _expand_fills_in_stream(
            text, bleed_pts, edge_x0, edge_y0, edge_x1, edge_y1
        )
        if new_text != text:
            doc.update_stream(xr, new_text.encode('latin-1'))
            modified = True

    if modified:
        log.info(f"Rozszerzono fill-e tła o {bleed_pts:.2f}pt")


def _expand_fills_in_stream(
    text: str, bleed_pts: float,
    edge_x0: float, edge_y0: float, edge_x1: float, edge_y1: float,
) -> str:
    """Rozszerza `re f` tła w content stream.

    Śledzi macierz skalowania (cm). Gdy napotka `x y w h re` + `f`,
    sprawdza czy prostokąt dotyka krawędzi strony i ma pełną szerokość
    lub pełną wysokość. Rozszerza o bleed_pts TYLKO w kierunkach gdzie
    fill dotyka krawędzi strony.

    Obsługuje:
    - Pełnostronicowe fill-e (tło) — rozszerza we wszystkich kierunkach
    - Paski na krawędzi (np. żółty pasek na dole) — rozszerza na krawędziach
      które dotykają brzegu strony
    """
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    lines = text.split('\n')
    result: list[str] = []

    # Prosty stos skal (śledzenie nested cm)
    scale_stack: list[tuple[float, float]] = [(1.0, 1.0)]
    current_sx, current_sy = 1.0, 1.0

    for i, raw_line in enumerate(lines):
        line = raw_line.strip()

        # Track cm operations (simple scale only: sx 0 0 sy tx ty cm)
        if line.endswith(' cm'):
            parts = line.split()
            if len(parts) == 7:
                try:
                    a, b, c, d = float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3])
                    if abs(b) < 0.001 and abs(c) < 0.001 and abs(a) > 0.001 and abs(d) > 0.001:
                        current_sx *= abs(a)
                        current_sy *= abs(d)
                except ValueError:
                    pass

        # Track q/Q for scale stack
        if line == 'q':
            scale_stack.append((current_sx, current_sy))
        elif line == 'Q':
            if scale_stack:
                current_sx, current_sy = scale_stack.pop()

        # Check for re f pattern: previous line is `re`, this line is `f` or `f*`
        if line in ('f', 'f*') and result:
            prev = result[-1].strip()
            if prev.endswith(' re'):
                parts = prev.split()
                if len(parts) == 5:  # x y w h re
                    try:
                        x, y, w, h = float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3])
                        # Effective position/size in page pts
                        eff_w = abs(w * current_sx)
                        eff_h = abs(h * current_sy)
                        eff_x = x * current_sx
                        eff_y = y * current_sy
                        eff_x1 = eff_x + w * current_sx  # right edge
                        eff_y1 = eff_y + h * current_sy   # top edge

                        tol = 2.0  # tolerance in pt

                        # Które krawędzie dotyka fill?
                        touches_left = abs(eff_x - edge_x0) < tol
                        touches_bottom = abs(eff_y - edge_y0) < tol
                        touches_right = abs(eff_x1 - edge_x1) < tol
                        touches_top = abs(eff_y1 - edge_y1) < tol

                        # Fill musi mieć pełną szerokość LUB pełną wysokość
                        # i dotykać przynajmniej jednej krawędzi
                        trim_w = edge_x1 - edge_x0
                        trim_h = edge_y1 - edge_y0
                        full_width = abs(eff_w - trim_w) < tol and touches_left
                        full_height = abs(eff_h - trim_h) < tol and touches_bottom

                        if full_width or full_height:
                            dx = bleed_pts / current_sx
                            dy = bleed_pts / current_sy

                            new_x = x - dx if touches_left else x
                            new_y = y - dy if touches_bottom else y
                            new_w = w
                            if touches_left:
                                new_w += dx
                            if touches_right:
                                new_w += dx
                            new_h = h
                            if touches_bottom:
                                new_h += dy
                            if touches_top:
                                new_h += dy

                            result[-1] = f"{new_x:.6f} {new_y:.6f} {new_w:.6f} {new_h:.6f} re"
                            log.debug(
                                f"Fill rozszerzony: {x} {y} {w} {h} → "
                                f"{new_x:.2f} {new_y:.2f} {new_w:.2f} {new_h:.2f} "
                                f"(scale={current_sx:.3f}x{current_sy:.3f}, "
                                f"edges: L={touches_left} B={touches_bottom} "
                                f"R={touches_right} T={touches_top})"
                            )
                    except ValueError:
                        pass

        result.append(raw_line)

    return '\n'.join(result)


def expand_edge_paths(
    doc: fitz.Document, page: fitz.Page, bleed_pts: float,
    page_w_pt: float, page_h_pt: float,
    edge_x0: float = 0.0, edge_y0: float = 0.0,
    edge_x1: float | None = None, edge_y1: float | None = None,
) -> None:
    """Rozszerza wektorowe ścieżki dotykające krawędzi trimu o bleed_pts.

    Dla każdego punktu ścieżki (m/l/c) leżącego na krawędzi trimu
    (x ≈ edge_x0, x ≈ edge_x1, y ≈ edge_y0, y ≈ edge_y1) przesuwa go
    o bleed_pts na zewnątrz. Śledzi macierz transformacji (cm) żeby poprawnie
    mapować lokalne współrzędne na współrzędne page-native.

    UWAGA: content stream używa współrzędnych MediaBox (oryginalnych).
    Gdy CropBox/TrimBox jest offset od (0,0), należy podać edge_x0/y0/x1/y1
    w oryginalnych współrzędnych, bo inaczej funkcja wykryje fałszywe
    "krawędzie" na wewnętrznych pozycjach grafiki i skorumpuje content.

    Domyślnie (edge_x1=None) używa page_w_pt/page_h_pt jako krawędzi
    prawej/górnej z origin (0,0) — wsteczna kompatybilność dla wywołań
    na stronach bez offset CropBox.

    Obsługuje nieregularne kształty (strzałki, grafiki) dotykające brzegu.
    """
    if edge_x1 is None:
        edge_x1 = page_w_pt
    if edge_y1 is None:
        edge_y1 = page_h_pt

    page_xref = page.xref
    contents_info = doc.xref_get_key(page_xref, "Contents")
    xref_str = contents_info[1]
    xrefs = re_module.findall(r'(\d+)\s+\d+\s+R', xref_str)
    if not xrefs:
        return

    modified = False
    for xr_str in xrefs:
        xr = int(xr_str)
        stream = doc.xref_stream(xr)
        if not stream:
            continue
        text = stream.decode('latin-1', errors='replace')
        new_text = _expand_edge_paths_in_stream(
            text, bleed_pts, edge_x0, edge_y0, edge_x1, edge_y1
        )
        if new_text != text:
            doc.update_stream(xr, new_text.encode('latin-1'))
            modified = True

    if modified:
        log.info(f"Rozszerzono ścieżki krawędziowe o {bleed_pts:.2f}pt")


def _expand_edge_paths_in_stream(
    text: str, bleed_pts: float,
    edge_x0: float, edge_y0: float, edge_x1: float, edge_y1: float,
) -> str:
    """Rozszerza współrzędne ścieżek na krawędziach trimu w content stream.

    Śledzi macierz transformacji (cm) i stos q/Q. Dla operatorów m, l, c
    przesuwa współrzędne leżące na krawędzi trimu o bleed_pts na zewnątrz.

    Obsługuje multi-operator lines (np. 'q 1 0 0 1 tx ty cm').
    """
    tol = 1.0  # tolerancja w pt

    # Stan transformacji: (sx, sy, tx, ty) — uproszczony (bez obrotu/skew)
    state_stack: list[tuple[float, float, float, float]] = [(1.0, 1.0, 0.0, 0.0)]

    def cur():
        return state_stack[-1]

    def to_page(lx: float, ly: float) -> tuple[float, float]:
        sx, sy, tx, ty = cur()
        return (lx * sx + tx, ly * sy + ty)

    def from_page_x(px: float) -> float:
        sx, _, tx, _ = cur()
        return (px - tx) / sx if abs(sx) > 1e-8 else px

    def from_page_y(py: float) -> float:
        _, sy, _, ty = cur()
        return (py - ty) / sy if abs(sy) > 1e-8 else py

    def extend_coord(lx: float, ly: float) -> tuple[float, float]:
        """Przesuwa punkt na zewnątrz jeśli leży na krawędzi trimu."""
        px, py = to_page(lx, ly)
        new_lx, new_ly = lx, ly
        if abs(px - edge_x0) < tol:
            new_lx = from_page_x(edge_x0 - bleed_pts)
        elif abs(px - edge_x1) < tol:
            new_lx = from_page_x(edge_x1 + bleed_pts)
        if abs(py - edge_y0) < tol:
            new_ly = from_page_y(edge_y0 - bleed_pts)
        elif abs(py - edge_y1) < tol:
            new_ly = from_page_y(edge_y1 + bleed_pts)
        return (new_lx, new_ly)

    def fmt(v: float) -> str:
        """Formatuje liczbę — bez trailing zeros, max 6 miejsc."""
        s = f"{v:.6f}"
        if '.' in s:
            s = s.rstrip('0').rstrip('.')
        return s

    # Tokenizuj content stream
    # Zamiast dzielić po liniach, pracuj na tokenach (PDF operatory + operandy)
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    lines = text.split('\n')
    result: list[str] = []
    modified = False

    for raw_line in lines:
        line = raw_line.strip()
        new_line = raw_line

        # Obsługa 'q' — może być samodzielnie lub na początku linii
        # np. 'q' lub 'q 1 0 0 1 0 265 cm'
        stripped = line
        if stripped.startswith('q ') or stripped == 'q':
            state_stack.append(cur())
            stripped = stripped[1:].lstrip() if stripped.startswith('q ') else ''

        if stripped == 'Q' or line == 'Q':
            if len(state_stack) > 1:
                state_stack.pop()

        # Track cm transformations — szukaj '... cm' z 6 liczbami przed
        if stripped.endswith(' cm') or line.endswith(' cm'):
            target = stripped if stripped.endswith(' cm') else line
            parts = target.split()
            # Szukaj: a b c d e f cm — 7 elementów LUB więcej (q a b c d e f cm)
            if len(parts) >= 7 and parts[-1] == 'cm':
                try:
                    # Weź ostatnie 7 tokenów: 6 liczb + 'cm'
                    nums = parts[-7:-1]
                    a, b, c, d = float(nums[0]), float(nums[1]), float(nums[2]), float(nums[3])
                    e, f_ = float(nums[4]), float(nums[5])
                    if abs(b) < 0.001 and abs(c) < 0.001:
                        sx0, sy0, tx0, ty0 = cur()
                        state_stack[-1] = (
                            sx0 * a, sy0 * d,
                            sx0 * e + tx0, sy0 * f_ + ty0,
                        )
                except ValueError:
                    pass

        # Path operators: m (moveto), l (lineto)
        elif line.endswith(' m') or line.endswith(' l'):
            parts = line.split()
            if len(parts) == 3:
                try:
                    x, y = float(parts[0]), float(parts[1])
                    nx, ny = extend_coord(x, y)
                    if nx != x or ny != y:
                        op = parts[2]
                        indent = raw_line[:len(raw_line) - len(raw_line.lstrip())]
                        new_line = f"{indent}{fmt(nx)} {fmt(ny)} {op}"
                        modified = True
                except ValueError:
                    pass

        # Path operator: c (curveto) — 6 coords + operator
        elif line.endswith(' c'):
            parts = line.split()
            if len(parts) == 7:
                try:
                    coords = [float(p) for p in parts[:6]]
                    changed = False
                    new_coords = []
                    for i in range(0, 6, 2):
                        nx, ny = extend_coord(coords[i], coords[i + 1])
                        new_coords.extend([nx, ny])
                        if nx != coords[i] or ny != coords[i + 1]:
                            changed = True
                    if changed:
                        indent = raw_line[:len(raw_line) - len(raw_line.lstrip())]
                        c_str = ' '.join(fmt(v) for v in new_coords)
                        new_line = f"{indent}{c_str} c"
                        modified = True
                except ValueError:
                    pass

        # Track Q (restore) — może być na osobnej linii
        if line == 'Q':
            pass  # already handled above

        result.append(new_line)

    if modified:
        return '\n'.join(result)
    return text


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
    # Normalizacja line endings
    text = text.replace('\r\n', '\n').replace('\r', '\n')
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
    rgb_alternate: tuple | None = None,
    cmyk_alternate: tuple | None = None,
) -> str:
    """Tworzy Separation colorspace i rejestruje w zasobach strony.

    Dwa tryby alternate colorspace:
      - rgb_alternate (r,g,b):  DeviceRGB  — cut PDF (GoSign rozpoznaje po kolorze RGB)
      - cmyk_alternate (c,m,y,k): DeviceCMYK — print/white PDF (prepress, drukarka UV)

    Podaj DOKŁADNIE JEDEN z rgb_alternate / cmyk_alternate.

    Args:
        doc: dokument PDF
        page: strona PDF
        spot_name: nazwa spot color (np. "CutContour", "FlexCut")
        rgb_alternate: kolor RGB (r, g, b) 0-1 — dla cut PDF
        cmyk_alternate: kolor CMYK (c, m, y, k) 0-1 — dla print/white PDF

    Returns:
        Nazwa zasobu colorspace (np. "CS_CutContour")
    """
    func_xref = doc.get_new_xref()

    if rgb_alternate is not None:
        r, g, b = rgb_alternate
        func_dict = (
            "<<"
            "/FunctionType 2"
            "/Domain [0 1]"
            "/C0 [1 1 1]"
            f"/C1 [{r} {g} {b}]"
            "/N 1"
            ">>"
        )
        doc.update_object(func_xref, func_dict)
        cs_xref = doc.get_new_xref()
        cs_array = f"[/Separation /{spot_name} /DeviceRGB {func_xref} 0 R]"
    elif cmyk_alternate is not None:
        c, m, y, k = cmyk_alternate
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
    else:
        raise ValueError(f"Brak rgb_alternate ani cmyk_alternate dla spot '{spot_name}'")
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
                cs_dict = existing_cs[1].removesuffix(">>")
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


def _render_source_cutpath_layer(
    sticker, out_page: fitz.Page, bleed_pts: float, out_w: float, out_h: float
) -> None:
    """Vector embed source PDF clipped to bleed_segments + removed strokes.

    Implementacja:
      1. pikepdf otwiera source → parsuje content stream operatorami
      2. Usuwa stroke-only drawings (`S` / `s` bez `f`/`F`/`B`/`b` w danym q/Q)
      3. Zapisuje do temp PDF z dodanym clip path w content stream (bleed_segments)
      4. PyMuPDF show_pdf_page embeduje wyczyszczony source — clipping
         naturalnie ucina wszystko poza bleed_segments (PDF `W n` = hard clip)

    Brak rasteryzacji → brak AA artefaktów / inpaint fake content.
    """
    import pikepdf
    doc_src = sticker.pdf_doc
    bbox = sticker._src_cutpath_bbox
    if bbox is None:
        raise ValueError("from_source_cutpath: brak _src_cutpath_bbox")
    bb_x0, bb_y0, bb_x1, bb_y1 = bbox

    # Scale ratio (gdy user zadał target_height_mm)
    orig_w_pt = max(bb_x1 - bb_x0, 1e-6)
    orig_h_pt = max(bb_y1 - bb_y0, 1e-6)
    scale_x = sticker.page_width_pt / orig_w_pt
    scale_y = sticker.page_height_pt / orig_h_pt
    src_bleed_x = bleed_pts / scale_x
    src_bleed_y = bleed_pts / scale_y

    # === pikepdf: clean source (remove stroke-only) ===
    import io
    src_bytes = doc_src.tobytes()
    pike = pikepdf.open(io.BytesIO(src_bytes))
    pike_page = pike.pages[sticker.page_index]
    ops = list(pikepdf.parse_content_stream(pike_page))
    cleaned_ops = _remove_stroke_only_blocks(ops)
    cleaned_bytes_pdf = pikepdf.unparse_content_stream(cleaned_ops)
    pike_page.Contents = pike.make_stream(cleaned_bytes_pdf)
    buf = io.BytesIO()
    pike.save(buf)
    pike.close()
    cleaned_doc = fitz.open(stream=buf.getvalue(), filetype="pdf")

    # === Render cleaned source @ 300 DPI w obszarze bleed bbox ===
    # get_pixmap z clip_rect PRAWDZIWIE ogranicza render do bbox. Content spoza
    # (np. iskierki Canva w rogach strony) nie jest renderowany. W przeciwieństwie
    # do show_pdf_page+clip, który przy opakowaniu w polygon clip-path potrafił
    # przecieczać content spoza bbox (udokumentowane testami T1/T3).
    clip_rect = fitz.Rect(bb_x0 - src_bleed_x, bb_y0 - src_bleed_y,
                          bb_x1 + src_bleed_x, bb_y1 + src_bleed_y)
    RENDER_DPI = 300
    render_scale = RENDER_DPI / 72.0
    cleaned_page = cleaned_doc[sticker.page_index]
    pix = cleaned_page.get_pixmap(
        matrix=fitz.Matrix(render_scale, render_scale),
        clip=clip_rect, alpha=False,
    )
    w_px, h_px = pix.width, pix.height
    rgb_arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(h_px, w_px, 3)
    cleaned_doc.close()

    # === HARD alpha mask z bleed_segments polygon ===
    from PIL import ImageDraw as _ImageDraw
    px_per_pt = w_px / out_w
    py_per_pt = h_px / out_h
    polygon_px = []
    for s in sticker.bleed_segments:
        if s[0] == 'l':
            polygon_px.append(
                ((float(s[1][0]) + bleed_pts) * px_per_pt,
                 (float(s[1][1]) + bleed_pts) * py_per_pt))
            polygon_px.append(
                ((float(s[2][0]) + bleed_pts) * px_per_pt,
                 (float(s[2][1]) + bleed_pts) * py_per_pt))
        elif s[0] == 'c':
            p0, cp1, cp2, p3 = s[1], s[2], s[3], s[4]
            for t_step in range(17):
                t = t_step / 16.0
                mt = 1 - t
                x = (mt**3 * p0[0] + 3 * mt**2 * t * cp1[0]
                     + 3 * mt * t**2 * cp2[0] + t**3 * p3[0])
                y = (mt**3 * p0[1] + 3 * mt**2 * t * cp1[1]
                     + 3 * mt * t**2 * cp2[1] + t**3 * p3[1])
                polygon_px.append(
                    ((x + bleed_pts) * px_per_pt,
                     (y + bleed_pts) * py_per_pt))
    mask_pil = PILImage.new("L", (w_px, h_px), 0)
    if polygon_px:
        _ImageDraw.Draw(mask_pil).polygon(polygon_px, fill=255)

    rgba_arr = np.empty((h_px, w_px, 4), dtype=np.uint8)
    rgba_arr[:, :, :3] = rgb_arr
    rgba_arr[:, :, 3] = np.array(mask_pil)

    with _SafeTempPng(".png") as tmp:
        PILImage.fromarray(rgba_arr, 'RGBA').save(tmp.name)
        out_page.insert_image(
            fitz.Rect(0, 0, out_w, out_h),
            filename=tmp.name, keep_proportion=False,
        )
    log.info(
        f"Source cutpath: raster clip@bbox {w_px}×{h_px}px + polygon alpha. "
        f"Spoza bleed bbox nie renderowane (get_pixmap clip)."
    )


def _remove_stroke_only_blocks(ops):
    """Usuwa stroke-only drawing ops z pikepdf content stream.

    Każdy op to tuple (operands, Operator). Szukamy bloków `q ... Q` które
    TRANSITIVELY zawierają `S`/`s` (stroke) ale nie zawierają `f`/`F`/`B`/`b`
    (fill), `Do` (image/form reference) ani tekstu (`Tj`/`TJ`/`'`/`"`).
    Takie bloki to cut lines — usuwane w całości (włącznie z zagnieżdżonymi
    wrapperami samego setupu).
    """
    FILL_OPS = {'f', 'F', 'f*', 'B', 'B*', 'b', 'b*'}
    STROKE_OPS = {'S', 's'}
    # Operatory świadczące o "realnej" zawartości (poza stroke-only ścieżką)
    CONTENT_OPS = {'Do', 'sh', 'Tj', 'TJ', "'", '"'}
    result = []
    q_stack = []  # (index_w_result_dla_q, set_operatorów_transitywnie)
    for op in ops:
        operator = op[1]
        op_name = str(operator)

        if op_name == 'q':
            q_stack.append((len(result), set()))
            result.append(op)
        elif op_name == 'Q':
            if q_stack:
                q_idx, inner_names = q_stack.pop()
                has_stroke = bool(inner_names & STROKE_OPS)
                has_fill = bool(inner_names & FILL_OPS)
                has_content = bool(inner_names & CONTENT_OPS)
                if has_stroke and not has_fill and not has_content:
                    # Stroke-only → usuń cały blok q..Q
                    result = result[:q_idx]
                else:
                    result.append(op)
                # Propaguj TRANSITIVELY do rodzica — rodzic bez innej
                # zawartości też jest stroke-only orphan i powinien być usunięty.
                if q_stack:
                    q_stack[-1][1].update(inner_names)
            else:
                result.append(op)
        else:
            if q_stack:
                q_stack[-1][1].add(op_name)
            result.append(op)
    return result


def _fix_content_stream_newlines(doc: fitz.Document, page: fitz.Page) -> None:
    """Zapewnia newline na końcu KAŻDEGO content stream na stronie.

    Zapobiega konkatenacji operatorów przez PostScript RIP-y
    (np. 'qqqqqqq1.0' na Xerox). Dotyczy zarówno naszych streams
    (inject_content_stream) jak i PyMuPDF-owych (show_pdf_page, insert_image).
    """
    import re as _re
    contents = doc.xref_get_key(page.xref, "Contents")
    if contents[0] == "null":
        return
    xrefs = _re.findall(r"(\d+) 0 R", contents[1])
    if not xrefs and contents[0] == "xref":
        xrefs = _re.findall(r"(\d+)", contents[1])
    for xr_str in xrefs:
        xr = int(xr_str)
        try:
            stream = doc.xref_stream(xr)
            if stream and not stream.endswith(b"\n"):
                doc.update_stream(xr, stream + b"\n")
        except Exception as e:
            log.debug(f"_fix_content_stream_newlines: xref {xr} skip ({e})")


def inject_content_stream(
    doc: fitz.Document, page: fitz.Page, stream_bytes: bytes
) -> None:
    """Dodaje content stream do strony jako nowy xref.

    Każdy stream jest owinięty w q/Q (graphics state isolation)
    i zakończony newline — zapobiega konkatenacji operatorów
    przez RIP-y (np. 'qqqqq1.0' na Xerox PostScript).
    """
    # Zapewnij newline na początku i końcu + q/Q wrapper
    wrapped = b"q\n" + stream_bytes.rstrip() + b"\nQ\n"

    xref = doc.get_new_xref()
    doc.update_object(xref, "<<>>")
    doc.update_stream(xref, wrapped)

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


def inject_content_on_layer(
    doc: fitz.Document, page: fitz.Page, stream_bytes: bytes,
    layer_name: str,
) -> None:
    """Dodaje content stream jako Form XObject z warstwą OCG (Optional Content Group).

    GoSign rozpoznaje warstwy w PDF — markery na warstwie 'Regmark'
    są automatycznie traktowane jako registration marks, nie jako linie cięcia.

    Form XObject ma przypisany /OC (Optional Content) — GoSign widzi go
    jako osobną warstwę przy imporcie z separacją "by layer name".
    """
    page_w = page.rect.width
    page_h = page.rect.height

    # 1. Utwórz OCG (warstwę)
    ocg_xref = doc.add_ocg(layer_name, on=True)

    # 2. Skopiuj Resources strony do Form XObject (potrzebne np. dla /CS_CutContour)
    page_xref = page.xref
    res_key = doc.xref_get_key(page_xref, "Resources")
    if res_key[0] == "xref":
        res_ref = res_key[1]  # np. "5 0 R"
    else:
        res_ref = None

    # 3. Utwórz Form XObject z content streamem
    form_xref = doc.get_new_xref()
    res_part = f"/Resources {res_ref}" if res_ref else ""
    form_dict = (
        f"<</Type /XObject /Subtype /Form "
        f"/BBox [0 0 {page_w:.4f} {page_h:.4f}] "
        f"/OC {ocg_xref} 0 R "
        f"{res_part}>>"
    )
    doc.update_object(form_xref, form_dict)
    doc.update_stream(form_xref, stream_bytes)

    # 4. Zarejestruj Form XObject w zasobach strony
    form_name = f"Fm{layer_name.replace('-', '').replace(' ', '')}"
    import re as _re

    def _get_res_xref():
        """Znajdź xref obiektu Resources (może być pośredni)."""
        ri = doc.xref_get_key(page_xref, "Resources")
        if ri[0] == "xref":
            return int(_re.search(r'(\d+)', ri[1]).group(1))
        return page_xref  # inline

    def _get_xobj_target():
        """Znajdź xref i klucz do modyfikacji XObject dict."""
        res_x = _get_res_xref()
        xi = doc.xref_get_key(res_x, "XObject")
        if xi[0] == "xref":
            # XObject jest pośredni
            return int(_re.search(r'(\d+)', xi[1]).group(1)), None
        return res_x, "XObject"

    target_xref, target_key = _get_xobj_target()

    if target_key is None:
        # XObject jest osobnym obiektem — modyfikuj go bezpośrednio
        obj_str = doc.xref_object(target_xref)
        # Dodaj nowy klucz do słownika
        new_entry = f"/{form_name} {form_xref} 0 R"
        if new_entry not in obj_str:
            obj_str = obj_str.rstrip().removesuffix(">>").rstrip() + f" {new_entry}>>"
            doc.update_object(target_xref, obj_str)
    else:
        # XObject jest w Resources — użyj xref_set_key
        xi = doc.xref_get_key(target_xref, target_key)
        if xi[0] == "dict":
            existing = xi[1].removesuffix(">>").rstrip()
            doc.xref_set_key(target_xref, target_key,
                             f"{existing}/{form_name} {form_xref} 0 R>>")
        elif xi[0] == "null" or xi[0] not in ("dict", "xref"):
            doc.xref_set_key(target_xref, target_key,
                             f"<</{form_name} {form_xref} 0 R>>")

    # 5. Dodaj content stream wywołujący Form XObject
    invoke_stream = f"/{form_name} Do".encode('ascii')
    inject_content_stream(doc, page, invoke_stream)


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
    cutline_mode: str = "kiss-cut",
    white: bool = False,
) -> dict:
    """Eksportuje pojedynczą naklejkę z bleedem i opcjonalnym CutContour/FlexCut.

    Ścieżka eksportu zależy od typu źródła:
      RASTER (sticker.raster_path != None):
        - PNG/JPG/TIFF z raster_crop_box → crop do content area
        - RGBA: expanded canvas + dilation + bleed mask (kształt)
        - RGB: edge-clamping (prostokąt)

      RASTER-ONLY PDF (pdf_doc != None, outermost_drawing_idx == None):
        - Prosty clip → render 300dpi + dilation + mask
        - Curve clip → ścieżka wektorowa (expand_clip_paths)

      WEKTOR PDF (pdf_doc != None, outermost_drawing_idx != None):
        - Solid fill (bleed_segments) + show_pdf_page + CutContour

    Warstwy wyjściowe (1-4):
      1) Podkład bleed (RGB/CMYK fill z bleed_segments)
      2) Grafika oryginalna (wektor: show_pdf_page / raster: insert_image)
      3) CutContour spot color Separation (opcjonalnie)
      4) Biały poddruk (opcjonalnie, osobny plik *_white.pdf)
    """
    if sticker.bleed_segments is None:
        raise ValueError("Sticker nie ma bleed_segments — uruchom generate_bleed() najpierw")
    if sticker.edge_color_rgb is None:
        raise ValueError("Sticker nie ma edge_color_rgb — uruchom generate_bleed() najpierw")
    if sticker.pdf_doc is None and sticker.raster_path is None:
        raise ValueError("Sticker nie ma otwartego pdf_doc ani raster_path")
    # Walidacja spójności: is_cmyk wymaga edge_color_cmyk
    if getattr(sticker, 'is_cmyk', False) and sticker.edge_color_cmyk is None:
        log.warning("Sticker.is_cmyk=True ale edge_color_cmyk=None — fallback na RGB")
        sticker.is_cmyk = False

    bleed_pts = bleed_mm * MM_TO_PT
    page_w = sticker.page_width_pt
    page_h = sticker.page_height_pt

    out_w = page_w + 2 * bleed_pts
    out_h = page_h + 2 * bleed_pts

    log.info(
        f"Export: {sticker.source_path} → {output_path} "
        f"({out_w * PT_TO_MM:.1f}×{out_h * PT_TO_MM:.1f}mm z bleedem)"
    )

    # Tworzenie PDF wyjściowego
    doc_out = fitz.open()
    out_page = doc_out.new_page(width=out_w, height=out_h)

    # ========================================================================
    # ŚCIEŻKA from_source_cutpath — linia cięcia z pliku
    # ========================================================================
    # Aktywna TYLKO gdy GUI "użyj linii cięcia z pliku" jest zaznaczone.
    # Bleed = REAL source graphic w pasie bleed_mm poza cut line, reszta
    # odrzucona. Zero dilation / fake bleed.
    if getattr(sticker, 'from_source_cutpath', False) and sticker.pdf_doc is not None:
        _render_source_cutpath_layer(sticker, out_page, bleed_pts, out_w, out_h)
        # Warstwa 3: CutContour/FlexCut spot
        if cutcontour:
            if cutline_mode == "flexcut":
                _spot_name = SPOT_COLOR_FLEXCUT
                _spot_cmyk = SPOT_CMYK_FLEXCUT
                _label = "FlexCut"
            else:
                _spot_name = SPOT_COLOR_CUTCONTOUR
                _spot_cmyk = SPOT_CMYK_CUTCONTOUR
                _label = "CutContour"
            _cs_name = setup_separation_colorspace(
                doc_out, out_page, _spot_name, cmyk_alternate=_spot_cmyk
            )
            _cut_stream = build_cutcontour_stream(
                sticker.cut_segments, bleed_pts, out_h, _cs_name
            )
            inject_content_stream(doc_out, out_page, _cut_stream)
            log.info(f"Warstwa 3: {_label} — OK (source cutpath)")
        _fix_content_stream_newlines(doc_out, doc_out[0])
        from modules.pdf_metadata import apply_pdfx4
        try:
            apply_pdfx4(doc_out, bleed_mm=bleed_mm)
        except Exception as e:
            log.warning(f"PDF/X-4 metadata (source cutpath): {e}")
        doc_out.save(output_path, deflate=True, garbage=3)
        doc_out.close()
        log.info(f"Zapisano [source cutpath]: {output_path}")
        return {
            'source_path': sticker.source_path,
            'output_path': output_path,
            'white_path': None,
            'page_size_mm': (sticker.width_mm, sticker.height_mm),
            'output_size_mm': (out_w * PT_TO_MM, out_h * PT_TO_MM),
            'bleed_mm': bleed_mm,
            'num_cut_segments': len(sticker.cut_segments),
            'num_bleed_segments': len(sticker.bleed_segments),
            'edge_color_rgb': sticker.edge_color_rgb,
            'edge_color_cmyk': sticker.edge_color_cmyk,
        }

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
        # ====== ŚCIEŻKA RASTROWA: bleed via edge-clamping lub dilation ======
        if sticker.raster_path is not None:
            # Plik rastrowy (PNG/JPG/TIFF)
            src_pil = PILImage.open(sticker.raster_path)
            has_raster_alpha = src_pil.mode in ('RGBA', 'LA', 'PA')

            # Crop do content area (jeśli wykryto w contour.py)
            if sticker.raster_crop_box is not None:
                cx, cy, cx2, cy2 = sticker.raster_crop_box
                src_pil = src_pil.crop((cx, cy, cx2, cy2))
                log.info(f"Raster crop do content: ({cx},{cy})-({cx2},{cy2}) = {src_pil.size}")

            if has_raster_alpha:
                # Obraz z alpha — composite na BIAŁE tło, potem dilation + mask.
                # 1) Composite RGBA na białe tło → RGB (jak na białym winylu).
                #    Glow/cień naturalnie blenduje się z białym.
                # 2) Expanded canvas + dilation — rozszerza kolory krawędzi na bleed.
                #    Dilation na skomposytowanym RGB (nie na surowym RGBA) —
                #    krawędź to biała obwódka, nie ciemne wnętrze.
                # 3) Maska z bleed_segments ogranicza do gładkiego kształtu.
                rgba_img = src_pil.convert('RGBA')
                white_bg = PILImage.new('RGB', rgba_img.size, (255, 255, 255))
                white_bg.paste(rgba_img, mask=rgba_img.split()[3])
                composited = np.array(white_bg)
                white_bg.close()

                bleed_px = max(1, round(bleed_pts * composited.shape[1] / page_w))
                h_r, w_r = composited.shape[:2]
                new_h = h_r + 2 * bleed_px
                new_w = w_r + 2 * bleed_px

                _check_raster_memory(new_w, new_h, channels=4,
                                     context="raster RGBA z dilation")

                # Expanded canvas (biały = tło)
                expanded = np.full((new_h, new_w, 3), 255, dtype=np.uint8)
                expanded[bleed_px:bleed_px + h_r, bleed_px:bleed_px + w_r] = composited

                # Maska treści (nie-białe piksele = content) do dilation
                # Alpha z oryginalnego RGBA mówi gdzie jest content
                alpha_orig = np.array(rgba_img)[:, :, 3]
                alpha_expanded = np.zeros((new_h, new_w), dtype=np.uint8)
                alpha_expanded[bleed_px:bleed_px + h_r, bleed_px:bleed_px + w_r] = alpha_orig

                # Dilation — rozszerz kolory z krawędzi content na bleed zone
                expanded_rgba = np.dstack([expanded, alpha_expanded])
                fill_range = bleed_px * 3
                rgba_filled = _fill_transparent_pixels(expanded_rgba, fill_range)
                rgb_filled = rgba_filled[:, :, :3]

                # Maska z bleed_segments — gładki kształt
                bleed_mask = _render_bleed_mask(
                    sticker.bleed_segments, new_w, new_h,
                    page_w, page_h, bleed_pts,
                )
                result_rgba = np.dstack([rgb_filled, bleed_mask])
                ext_img = PILImage.fromarray(result_rgba, "RGBA")
                log.info(
                    f"Raster alpha: composite+dilation+mask "
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
                _check_raster_memory(new_w, new_h, channels=4,
                                     context="raster-only PDF z dilation")
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
        # (context manager gwarantuje usunięcie nawet przy wyjątku)
        with _SafeTempPng(".png") as tmp:
            try:
                ext_img.save(tmp.name)
            finally:
                ext_img.close()
            full_rect = fitz.Rect(0, 0, out_w, out_h)
            out_page.insert_image(full_rect, filename=tmp.name)
        log.info("Warstwa 1+2: bleed + grafika rastrowa — OK")
    else:
        # ====== ŚCIEŻKA WEKTOROWA: solid fill + show_pdf_page ======
        # Bleed fill w tym samym colorspace co grafika źródłowa
        if sticker.is_cmyk and sticker.edge_color_cmyk:
            bleed_stream = build_cmyk_fill_stream(
                sticker.bleed_segments, sticker.edge_color_cmyk, bleed_pts, out_h
            )
            log.info("Warstwa 1: podkład bleed (CMYK fill) — OK")
        else:
            bleed_stream = build_rgb_fill_stream(
                sticker.bleed_segments, sticker.edge_color_rgb, bleed_pts, out_h
            )
            log.info("Warstwa 1: podkład bleed (RGB fill) — OK")
        inject_content_stream(doc_out, out_page, bleed_stream)

        doc_src = sticker.pdf_doc
        src_page = doc_src[sticker.page_index]

        if getattr(sticker, 'is_artwork_on_artboard', False):
            # Artwork-on-artboard: CropBox ustawiony w detect_contour.
            # show_pdf_page mapuje CropBox na target rect — nie modyfikuj MediaBox
            # (MediaBox >> CropBox, a set_mediabox nie konwertuje y-coords).
            if black_100k:
                convert_black_to_100k(doc_src, src_page)
            graphic_rect = fitz.Rect(bleed_pts, bleed_pts,
                                     out_w - bleed_pts, out_h - bleed_pts)
            out_page.show_pdf_page(graphic_rect, doc_src, sticker.page_index)
            log.info("Warstwa 2: grafika wektorowa (artwork-on-artboard) — OK")
        else:
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

            # Rozszerz fill-e tła (re f) które pokrywają dokładnie stronę
            # (Canva: white + color fill nie wychodzą poza page).
            # Przekazujemy cropbox edges — content stream używa współrzędnych
            # MediaBox, więc dla offset CropBox musimy sprawdzać pozycję trim
            # (nie wymiar), inaczej fałszywie trafiamy wewnętrzne elementy.
            expand_page_fills(
                doc_src, src_page, bleed_pts,
                src_cropbox.width, src_cropbox.height,
                src_cropbox.x0, src_cropbox.y0,
                src_cropbox.x1, src_cropbox.y1,
            )

            # Rozszerz wektorowe ścieżki dotykające krawędzi trim
            # (strzałki, grafiki — dowolne kształty, nie tylko prostokąty)
            expand_edge_paths(
                doc_src, src_page, bleed_pts,
                src_cropbox.width, src_cropbox.height,
                src_cropbox.x0, src_cropbox.y0,
                src_cropbox.x1, src_cropbox.y1,
            )

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

    # --- WARSTWA 3: Linia cięcia (Kiss-Cut / FlexCut / Brak) ---
    if cutcontour:
        if cutline_mode == "flexcut":
            spot_name = SPOT_COLOR_FLEXCUT
            spot_cmyk = SPOT_CMYK_FLEXCUT
            label = "FlexCut"
        else:
            spot_name = SPOT_COLOR_CUTCONTOUR
            spot_cmyk = SPOT_CMYK_CUTCONTOUR
            label = "CutContour"
        cs_name = setup_separation_colorspace(doc_out, out_page,
                                              spot_name, cmyk_alternate=spot_cmyk)
        cut_stream = build_cutcontour_stream(
            sticker.cut_segments, bleed_pts, out_h, cs_name
        )
        inject_content_stream(doc_out, out_page, cut_stream)
        log.info(f"Warstwa 3: {label} — OK")
    else:
        log.info("Warstwa 3: linia cięcia — pominięta (sam spad)")

    _fix_content_stream_newlines(doc_out, doc_out[0])

    # PDF/X-4: OutputIntent FOGRA39 + TrimBox/BleedBox
    from modules.pdf_metadata import apply_pdfx4
    apply_pdfx4(doc_out, bleed_mm=bleed_mm)

    # Zapis
    doc_out.save(output_path, deflate=True, garbage=3)
    doc_out.close()

    log.info(f"Zapisano: {output_path}")

    # Osobny plik White (bialy poddruk)
    white_path = None
    if white and sticker.bleed_segments:
        base, ext = os.path.splitext(output_path)
        white_path = f"{base}_white{ext}"
        white_doc = fitz.open()
        white_page = white_doc.new_page(width=out_w, height=out_h)
        cs_white = setup_separation_colorspace(
            white_doc, white_page, SPOT_COLOR_WHITE, cmyk_alternate=SPOT_CMYK_WHITE,
        )
        white_segments = _get_white_segments(sticker.bleed_segments, sticker.cut_segments)
        white_stream = build_white_fill_stream(
            white_segments, bleed_pts, out_h, cs_white,
        )
        if white_stream:
            inject_content_stream(white_doc, white_page, white_stream)
        try:
            from modules.pdf_metadata import apply_pdfx4
            apply_pdfx4(white_doc, bleed_mm=bleed_mm)
        except Exception as e:
            log.warning(f"PDF/X-4 metadata (white layer) failed: {e}")
        white_doc.save(white_path, deflate=True, garbage=3)
        white_doc.close()
        log.info(f"White PDF zapisany: {white_path}")

    return {
        'source_path': sticker.source_path,
        'output_path': output_path,
        'white_path': white_path,
        'page_size_mm': (sticker.width_mm, sticker.height_mm),
        'output_size_mm': (out_w * PT_TO_MM, out_h * PT_TO_MM),
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
    """Usuwa strumienie zawierające linie cięcia (CutContour lub FlexCut) ze strony.

    Dla plików bleed_ output: linia cięcia jest ostatnim content streamem.
    Wykrywamy go po obecności 'CutContour' lub 'FlexCut' w bajtach strumienia.
    """
    def _is_cutline_stream(stream_bytes: bytes) -> bool:
        return b"CutContour" in stream_bytes or b"FlexCut" in stream_bytes

    contents_info = doc.xref_get_key(page.xref, "Contents")
    if contents_info[0] == "array":
        xrefs = [int(x) for x in re_module.findall(r'(\d+)\s+\d+\s+R', contents_info[1])]
        filtered = []
        for xref in xrefs:
            try:
                sd = doc.xref_stream(xref)
                if sd is None or not _is_cutline_stream(sd):
                    filtered.append(xref)
                else:
                    log.debug(f"_strip_cutcontour_streams: usunięto xref {xref} (cutline)")
            except Exception as e:
                log.debug(f"_strip_cutcontour_streams: xref {xref} not readable ({e}), zachowuje")
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
                if sd and _is_cutline_stream(sd):
                    doc.xref_set_key(page.xref, "Contents", "null")
                    log.debug(f"_strip_cutcontour_streams: usunięto xref {xref} (cutline)")
            except Exception as e:
                log.debug(f"_strip_cutcontour_streams: xref {xref} not readable ({e})")


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
        # Usuń CutContour (nie pokazuj w print PDF) + wyczyść nadmiarowe boxy
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
    # Walidacja spójności CMYK
    if getattr(sticker, 'is_cmyk', False) and sticker.edge_color_cmyk is None:
        sticker.is_cmyk = False

    bleed_pts = bleed_mm * MM_TO_PT
    px = placement.x_mm * MM_TO_PT
    py = placement.y_mm * MM_TO_PT

    # Rozmiar naklejki z bleedem w pt
    if int(placement.rotation_deg) % 360 in (90, 270):
        sticker_w_pt = sticker.page_height_pt
        sticker_h_pt = sticker.page_width_pt
    else:
        sticker_w_pt = sticker.page_width_pt
        sticker_h_pt = sticker.page_height_pt

    out_w = sticker_w_pt + 2 * bleed_pts
    out_h = sticker_h_pt + 2 * bleed_pts

    # Transformacja segmentów bleed do pozycji na arkuszu
    # Segmenty są w fitz coords (y-down), musimy:
    # 1. Przeliczyć do PDF coords naklejki (y-up, z bleed offset)
    # 2. Przeliczyć do pozycji na arkuszu (translation + optional rotation)
    # Fill w tym samym colorspace co grafika źródłowa
    # inject_content_stream opakowuje w q/Q — tu NIE dodajemy
    ops: list[str] = []
    if sticker.is_cmyk and sticker.edge_color_cmyk:
        c, m, y, k = sticker.edge_color_cmyk
        ops.append(f"{c:.6f} {m:.6f} {y:.6f} {k:.6f} k")
    else:
        r, g, b = sticker.edge_color_rgb
        ops.append(f"{r:.6f} {g:.6f} {b:.6f} rg")

    # Transformacja: translate do pozycji na arkuszu (PDF y-up)
    # px, py to lewy-dolny róg naklejki w pt (już w PDF coords)
    if int(placement.rotation_deg) % 360 in (90, 270):
        # Rotation 90°: translate + rotate
        # 90° CCW: [0 1 -1 0 tx ty]
        tx = px + out_h
        ty = py
        ops.append(f"0 1 -1 0 {tx:.4f} {ty:.4f} cm")
    else:
        ops.append(f"1 0 0 1 {px:.4f} {py:.4f} cm")

    path_ops = _segments_to_pdf_path_ops(sticker.bleed_segments, bleed_pts, out_h)
    ops.append(path_ops)
    ops.append("f")

    return "\n".join(ops).encode('ascii')


def _build_sheet_white_fill_stream(
    placement: Placement,
    sheet_h_pt: float,
    bleed_mm: float,
    cs_name: str,
) -> bytes:
    """Buduje content stream: White fill dla jednego placement na arkuszu.

    Ksztalt: bleed_segments (grafika+spad) — White pokrywa cala naklejke.
    Spot color White — bialy poddruk pod grafika.
    """
    sticker = placement.sticker
    if not sticker.bleed_segments:
        return b""

    bleed_pts = bleed_mm * MM_TO_PT
    px = placement.x_mm * MM_TO_PT
    py = placement.y_mm * MM_TO_PT

    if int(placement.rotation_deg) % 360 in (90, 270):
        sticker_w_pt = sticker.page_height_pt
        sticker_h_pt = sticker.page_width_pt
    else:
        sticker_w_pt = sticker.page_width_pt
        sticker_h_pt = sticker.page_height_pt

    out_w = sticker_w_pt + 2 * bleed_pts
    out_h = sticker_h_pt + 2 * bleed_pts

    white_segments = _get_white_segments(sticker.bleed_segments, sticker.cut_segments)

    # inject_content_stream opakowuje w q/Q — tu NIE dodajemy
    ops: list[str] = []
    ops.append(f"/{cs_name} cs")
    ops.append("1 scn")

    if int(placement.rotation_deg) % 360 in (90, 270):
        tx = px + out_h
        ty = py
        ops.append(f"0 1 -1 0 {tx:.4f} {ty:.4f} cm")
    else:
        ops.append(f"1 0 0 1 {px:.4f} {py:.4f} cm")

    path_ops = _segments_to_pdf_path_ops(white_segments, bleed_pts, out_h)
    ops.append(path_ops)
    ops.append("f")

    return "\n".join(ops).encode('ascii')


def _build_sheet_cutcontour_stream(
    placement: Placement,
    sheet_h_pt: float,
    bleed_mm: float,
    cs_name: str | None = None,
    segments_override: list | None = None,
    flexcut_h_pt: list[float] | None = None,
    flexcut_v_pt: list[float] | None = None,
    cut_ocg_name: str | None = None,
    cut_cmyk: tuple | None = None,
) -> bytes:
    """Buduje content stream: CutContour stroke dla jednego placement.

    Dwa tryby:
      - Separation (cs_name): używa spot color — dla single sticker export
      - OCG (cut_ocg_name + cut_cmyk): bezpośredni CMYK na OCG warstwie — dla cut PDF (GoSign)

    Args:
        placement: Placement z naklejką
        sheet_h_pt: wysokość arkusza w pt
        bleed_mm: bleed w mm
        cs_name: nazwa Separation colorspace (tryb spot)
        segments_override: przefiltrowane segmenty
        flexcut_h_pt: pozycje FlexCut poziomych w pt
        flexcut_v_pt: pozycje FlexCut pionowych w pt
        cut_ocg_name: nazwa OCG property (tryb OCG, np. "PrCut")
        cut_cmyk: kolor CMYK (c,m,y,k) dla trybu OCG
    """
    sticker = placement.sticker
    if not sticker.cut_segments:
        return b""

    bleed_pts = bleed_mm * MM_TO_PT
    px = placement.x_mm * MM_TO_PT
    py = placement.y_mm * MM_TO_PT

    if int(placement.rotation_deg) % 360 in (90, 270):
        sticker_w_pt = sticker.page_height_pt
        sticker_h_pt = sticker.page_width_pt
    else:
        sticker_w_pt = sticker.page_width_pt
        sticker_h_pt = sticker.page_height_pt

    out_w = sticker_w_pt + 2 * bleed_pts
    out_h = sticker_h_pt + 2 * bleed_pts

    segments = segments_override if segments_override is not None else sticker.cut_segments

    if not segments:
        return b""

    # FlexCut filtrowanie — tylko gdy segmenty NIE były pre-filtrowane
    # (segments_override z _deduplicate_cut_segments już jest przefiltrowane
    #  z poprawnym uwzględnieniem rotacji; drugi filtr jest zbędny i błędny
    #  dla obróconych placementów — porównuje lokalne coords z sheet-space FlexCut)
    skip_flexcut_filter = segments_override is not None

    if not skip_flexcut_filter:
        flex_tol_pt = 2.0 * MM_TO_PT
        flex_h_local = [fy - py for fy in (flexcut_h_pt or [])]
        flex_v_local = [fx - px for fx in (flexcut_v_pt or [])]
    else:
        flex_h_local = []
        flex_v_local = []

    ops: list[str] = []
    if cut_ocg_name and cut_cmyk:
        # Tryb OCG: bezpośredni CMYK na warstwie (format pluginu Summa)
        c, m, y, k = cut_cmyk
        ops.append(f"/OC /{cut_ocg_name} BDC")
        ops.append(f"{c:.4f} {m:.4f} {y:.4f} {k:.4f} K")
        ops.append("0 J")
        ops.append("0 j")
        ops.append("0.5669 w")  # 0.2mm jak plugin Summa
    else:
        # Tryb Separation (single sticker export)
        ops.append(f"/{cs_name} cs")
        ops.append(f"/{cs_name} CS")
        ops.append("1 SCN")
        ops.append(f"{CUTCONTOUR_STROKE_WIDTH_PT} w")
    ops.append("q")

    if int(placement.rotation_deg) % 360 in (90, 270):
        tx = px + out_h
        ty = py
        ops.append(f"0 1 -1 0 {tx:.4f} {ty:.4f} cm")
    else:
        ops.append(f"1 0 0 1 {px:.4f} {py:.4f} cm")

    # Rysuj każdy segment osobno (m→l/c→S) — bezpieczne po deduplikacji
    # (brak ryzyka niechcianych zamknięć/przekątnych)
    def tx_pt(x: float, y: float) -> tuple[float, float]:
        return x + bleed_pts, out_h - (y + bleed_pts)

    def _on_flexcut(sx, sy, ex, ey) -> bool:
        """Sprawdza czy segment (w cm-local pt) leży na linii FlexCut."""
        # Segment poziomy? (oba Y podobne)
        if abs(sy - ey) < 1.0:
            avg_y = (sy + ey) / 2
            for fy in flex_h_local:
                if abs(avg_y - fy) < flex_tol_pt:
                    return True
        # Segment pionowy? (oba X podobne)
        if abs(sx - ex) < 1.0:
            avg_x = (sx + ex) / 2
            for fx in flex_v_local:
                if abs(avg_x - fx) < flex_tol_pt:
                    return True
        return False

    skipped = 0
    for seg in segments:
        if seg[0] == 'l':
            sx, sy = tx_pt(seg[1][0], seg[1][1])
            ex, ey = tx_pt(seg[2][0], seg[2][1])
            if flex_h_local or flex_v_local:
                if _on_flexcut(sx, sy, ex, ey):
                    skipped += 1
                    continue
            ops.append(f"{sx:.4f} {sy:.4f} m")
            ops.append(f"{ex:.4f} {ey:.4f} l")
            ops.append("S")
        elif seg[0] == 'c':
            sx, sy = tx_pt(seg[1][0], seg[1][1])
            cx1, cy1 = tx_pt(seg[2][0], seg[2][1])
            cx2, cy2 = tx_pt(seg[3][0], seg[3][1])
            ex, ey = tx_pt(seg[4][0], seg[4][1])
            if flex_h_local or flex_v_local:
                if _on_flexcut(sx, sy, ex, ey):
                    skipped += 1
                    continue
            ops.append(f"{sx:.4f} {sy:.4f} m")
            ops.append(f"{cx1:.4f} {cy1:.4f} {cx2:.4f} {cy2:.4f} {ex:.4f} {ey:.4f} c")
            ops.append("S")

    if skipped:
        log.info(f"CutContour: pominięto {skipped} segmentów na liniach FlexCut")

    ops.append("Q")
    if cut_ocg_name:
        ops.append("EMC")

    return "\n".join(ops).encode('ascii')


def _seg_to_sheet_mm(
    seg, placement: Placement, sticker: Sticker,
) -> tuple[float, float, float, float] | None:
    """Konwertuje punkty segmentu z pt (fitz coords, y-down) na mm (sheet coords, y-up).

    Fitz Y rośnie w dół, sheet Y rośnie w górę. Odwracamy Y:
      y_sheet = py_mm + (sticker_h_mm - y_fitz_mm)
    gdzie sticker_h_mm = page_height_pt * pt_to_mm (content height).

    Obsługuje rotację 90° placement'u.
    Returns: (sx, sy, ex, ey) w mm sheet coords, lub None.
    """
    pt_to_mm_x = sticker.width_mm / sticker.page_width_pt if sticker.page_width_pt > 0 else 0
    pt_to_mm_y = sticker.height_mm / sticker.page_height_pt if sticker.page_height_pt > 0 else 0
    px_mm = placement.x_mm
    py_mm = placement.y_mm
    h_mm = sticker.height_mm  # content height (dla odwrócenia Y)

    if seg[0] == 'l':
        _, start, end = seg
        lx0, ly0 = start[0] * pt_to_mm_x, start[1] * pt_to_mm_y
        lx1, ly1 = end[0] * pt_to_mm_x, end[1] * pt_to_mm_y
    elif seg[0] == 'c':
        _, p0, _, _, p3 = seg
        lx0, ly0 = p0[0] * pt_to_mm_x, p0[1] * pt_to_mm_y
        lx1, ly1 = p3[0] * pt_to_mm_x, p3[1] * pt_to_mm_y
    else:
        return None

    # Rotacja 90° — cm: 0 1 -1 0 (px+out_h) py
    # page_x = raw_y + px, page_y = raw_x + py (Y-flip znosi się w cm)
    if int(placement.rotation_deg) % 360 in (90, 270):
        sx = px_mm + ly0
        sy = py_mm + lx0
        ex = px_mm + ly1
        ey = py_mm + lx1
    else:
        # Odwróć Y: fitz y-down → sheet y-up
        sx = px_mm + lx0
        sy = py_mm + (h_mm - ly0)
        ex = px_mm + lx1
        ey = py_mm + (h_mm - ly1)

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
    flexcut_h: list[tuple[float, float, float]],
    flexcut_v: list[tuple[float, float, float]],
    bleed_mm: float,
    gap_mm: float = 5.0,
    tolerance_mm: float = 0.3,
) -> list[tuple[Placement, list]]:
    """Deduplikuje segmenty CutContour nakładające się między placementami.

    Gdy gap=0, sąsiednie naklejki mają wspólne krawędzie — te same segmenty
    są generowane dwukrotnie. Ta funkcja zachowuje tylko pierwszą kopię.

    flexcut_h / flexcut_v: lista (position_mm, start_mm, end_mm) — pelny
    span kazdej linii FlexCut. Filtr uzywa span'u zeby usunac kiss-cut
    TYLKO gdy faktycznie lezy W ZAKRESIE FlexCut (nie tylko ma identyczna
    wspolrzedna w innym obszarze arkusza).

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

        # Filtr kiss-cut na liniach FlexCut.
        # Usuwamy kiss-cut TYLKO gdy DOKLADNIE pokrywa sie z linia FlexCut
        # (tolerancja ~0.3mm — szum pt<->mm) I lezy W ZAKRESIE spanu FlexCut.
        # Dwukrotne ciecie w tym samym miejscu uszkadza ploter/material.
        # Kiss-cut na tej samej wspolrzednej ale w innej czesci arkusza
        # (poza spanem FlexCut) zostaje zachowane.
        segments = sticker.cut_segments
        if flexcut_h or flexcut_v:
            segments = _filter_segments_on_flexcut(
                segments, placement, sticker, bleed_mm,
                flexcut_h, flexcut_v, gap_mm,
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
    flexcut_h: list[tuple[float, float, float]],
    flexcut_v: list[tuple[float, float, float]],
    gap_mm: float = 5.0,
) -> list:
    """Filtruje segmenty CutContour — usuwa te leżące DOKŁADNIE na liniach FlexCut.

    Segment jest usuwany gdy:
      1. Ma orientacje zgodna z FlexCut (horizontal/vertical)
      2. Jego wspolrzedna pokrywa sie z pozycja FlexCut (tol = 0.3mm —
         szum float-point pt<->mm)
      3. Jego zakres nakladkuje sie ze spanem FlexCut w drugiej osi
         (inaczej to kiss-cut w innym obszarze arkusza o przypadkowo
         zgodnej wspolrzednej — MUSI zostac zachowany)

    flexcut_h/flexcut_v: lista (position_mm, start_mm, end_mm).

    Stara wersja (tol = bleed + gap/2 + 1 bez span-check) dwukrotnie
    zawodzila:
      - tolerancja zbyt szeroka → usuwala kiss-cut odsuniete o gap/2
      - brak span-check → usuwala kiss-cut naklejek w innym obszarze
        arkusza ale o przypadkowo identycznej wspolrzednej.
    """
    if not flexcut_h and not flexcut_v:
        return segments

    # Tolerancja koincydencji — tylko szum float-point pt<->mm konwersji.
    tol = 0.3
    # Tolerancja na sprawdzenie "segment jest prostą linią" (mały epsilon).
    line_tol = 1.0
    # Margines overlap span — mikro-overlap sasiadujacych jednostek cie
    span_tol = 0.3

    pt_to_mm_x = sticker.width_mm / sticker.page_width_pt if sticker.page_width_pt > 0 else 0
    pt_to_mm_y = sticker.height_mm / sticker.page_height_pt if sticker.page_height_pt > 0 else 0
    px_mm = placement.x_mm
    py_mm = placement.y_mm
    h_mm = sticker.height_mm
    w_mm = sticker.width_mm

    def seg_to_sheet_mm(seg):
        """Konwertuje punkty segmentu z pt (fitz y-down) na mm (sheet y-up).

        Uwzględnia bleed offset — CutContour jest na krawędzi trim,
        czyli bleed_mm do wewnątrz footprintu.
        """
        if seg[0] == 'l':
            _, start, end = seg
            lx0, ly0 = start[0] * pt_to_mm_x, start[1] * pt_to_mm_y
            lx1, ly1 = end[0] * pt_to_mm_x, end[1] * pt_to_mm_y
        elif seg[0] == 'c':
            _, p0, _, _, p3 = seg
            lx0, ly0 = p0[0] * pt_to_mm_x, p0[1] * pt_to_mm_y
            lx1, ly1 = p3[0] * pt_to_mm_x, p3[1] * pt_to_mm_y
        else:
            return None

        if int(placement.rotation_deg) % 360 in (90, 270):
            # cm: 0 1 -1 0 (px+out_h) py → page_x = raw_y + px, page_y = raw_x + py
            sx = px_mm + bleed_mm + ly0
            sy = py_mm + bleed_mm + lx0
            ex = px_mm + bleed_mm + ly1
            ey = py_mm + bleed_mm + lx1
        else:
            sx = px_mm + bleed_mm + lx0
            sy = py_mm + bleed_mm + (h_mm - ly0)
            ex = px_mm + bleed_mm + lx1
            ey = py_mm + bleed_mm + (h_mm - ly1)
        return (sx, sy, ex, ey)

    filtered = []
    for seg in segments:
        coords = seg_to_sheet_mm(seg)
        if coords is None:
            filtered.append(seg)
            continue

        sx, sy, ex, ey = coords
        skip = False

        # Segment poziomy? (oba punkty mają podobne Y)
        if abs(sy - ey) < line_tol:
            avg_y = (sy + ey) / 2
            seg_x0 = min(sx, ex)
            seg_x1 = max(sx, ex)
            for fy, fstart, fend in flexcut_h:
                if abs(avg_y - fy) >= tol:
                    continue
                # Filtruj TYLKO gdy segment mieści się W CAŁOŚCI w span FlexCut.
                # Częściowy overlap (np. kiss-cut naklejki wystaje poza FlexCut):
                # zachowaj cały segment — dwukrotne cięcie w strefie overlap
                # jest akceptowalne (krótsze niż FlexCut), ale obcięcie całego
                # segmentu straciłoby kiss-cut w części wystającej.
                if seg_x0 < fstart - span_tol or seg_x1 > fend + span_tol:
                    continue  # segment wystaje poza FlexCut, zachowaj
                skip = True
                break

        # Segment pionowy? (oba punkty mają podobne X)
        if not skip and abs(sx - ex) < line_tol:
            avg_x = (sx + ex) / 2
            seg_y0 = min(sy, ey)
            seg_y1 = max(sy, ey)
            for fx, fstart, fend in flexcut_v:
                if abs(avg_x - fx) >= tol:
                    continue
                if seg_y0 < fstart - span_tol or seg_y1 > fend + span_tol:
                    continue  # segment wystaje poza FlexCut, zachowaj
                skip = True
                break

        if not skip:
            filtered.append(seg)
        else:
            log.debug(
                f"FlexCut filtr: usunięto segment ({sx:.1f},{sy:.1f})-({ex:.1f},{ey:.1f})mm"
            )

    return filtered


def _build_marks_stream(marks: list[Mark], sheet_h_pt: float,
                        cs_name: str | None = None,
                        ocg_name: str | None = None) -> bytes:
    """Buduje content stream: znaczniki rejestracji (czarne prostokąty/krzyżyki).

    Tryby:
      - ocg_name: BDC/EMC marked content z OCG (jak plugin Summa do CorelDraw)
        Kolor: bezpośredni CMYK 0 0 0 1 (100% K). GoSign czyta warstwę po nazwie OCG.
      - cs_name: Separation spot color (np. print PDF)
      - brak: czarny DeviceRGB (kompatybilność wsteczna)
    """
    if not marks:
        return b""

    ops: list[str] = []
    if ocg_name:
        # BDC/EMC OCG layer — GoSign rozpoznaje po nazwie warstwy
        # Kolor ustawiany per marker (jak plugin Summa do CorelDraw)
        ops.append(f"/OC /{ocg_name} BDC")
    elif cs_name:
        # Spot color
        ops.append(f"/{cs_name} cs")
        ops.append(f"/{cs_name} CS")
        ops.append("1 scn")
        ops.append("1 SCN")
    else:
        ops.append("0 0 0 rg")
        ops.append("0 0 0 RG")

    for mark in marks:
        x = mark.x_mm * MM_TO_PT
        y = mark.y_mm * MM_TO_PT
        w = mark.width_mm * MM_TO_PT
        h = mark.height_mm * MM_TO_PT

        if mark.mark_type == "opos_rectangle":
            if ocg_name:
                # Format identyczny z pluginem Summa do CorelDraw:
                # pełny stan graficzny + path m/l/h/b* (fill+stroke even-odd)
                ops.append("0 J")
                ops.append("0 j")
                ops.append("0.0003 w")
                ops.append("[] 0 d")
                ops.append("0.0000 0.0000 0.0000 1.0000 K")
                ops.append("0.0000 0.0000 0.0000 1.0000 k")
                ops.append(f"{x:.4f} {y:.4f} m")
                ops.append(f"{x + w:.4f} {y:.4f} l")
                ops.append(f"{x + w:.4f} {y + h:.4f} l")
                ops.append(f"{x:.4f} {y + h:.4f} l")
                ops.append("h")
                ops.append("b*")
            else:
                # Kompatybilność wsteczna (print PDF)
                ops.append(f"{x:.4f} {y:.4f} {w:.4f} {h:.4f} re")
                ops.append("f")

        elif mark.mark_type == "crosshair":
            cx = x + w / 2
            cy = y + h / 2
            ops.append("0.5 w")
            ops.append(f"{x:.4f} {cy:.4f} m")
            ops.append(f"{x + w:.4f} {cy:.4f} l")
            ops.append("S")
            ops.append(f"{cx:.4f} {y:.4f} m")
            ops.append(f"{cx:.4f} {y + h:.4f} l")
            ops.append("S")

        elif mark.mark_type == "crop_mark":
            ops.append("0.5 w")
            ops.append(f"{x:.4f} {y:.4f} m")
            ops.append(f"{x + w:.4f} {y:.4f} l")
            ops.append("S")
            ops.append(f"{x:.4f} {y:.4f} m")
            ops.append(f"{x:.4f} {y + h:.4f} l")
            ops.append("S")

    if ocg_name:
        ops.append("EMC")

    return "\n".join(ops).encode('ascii')


def _build_flexcut_stream(
    panel_lines: list[PanelLine],
    sheet_w_mm: float,
    sheet_h_mm: float,
    cs_name: str | None = None,
    ocg_name: str | None = None,
    ocg_cmyk: tuple | None = None,
) -> bytes:
    """Buduje content stream: FlexCut linie.

    Dwa tryby:
      - Separation (cs_name): spot color — single sticker / legacy
      - OCG (ocg_name + ocg_cmyk): bezpośredni CMYK na OCG warstwie — cut PDF (GoSign)
    """
    if not panel_lines:
        return b""

    # Deduplikacja linii
    seen: set[tuple] = set()
    unique_lines: list = []
    for line in panel_lines:
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
    if ocg_name and ocg_cmyk:
        # Tryb OCG: bezpośredni CMYK (format pluginu Summa)
        c, m, y, k = ocg_cmyk
        ops.append(f"/OC /{ocg_name} BDC")
        ops.append(f"{c:.4f} {m:.4f} {y:.4f} {k:.4f} K")
        ops.append("0 J")
        ops.append("0 j")
        ops.append("0.5669 w")  # 0.2mm
    else:
        # Tryb Separation
        ops.append(f"/{cs_name} cs")
        ops.append(f"/{cs_name} CS")
        ops.append("1 SCN")
        ops.append(f"{FLEXCUT_STROKE_WIDTH_PT} w")

    for line in unique_lines:
        if line.axis == "horizontal":
            y_pt = line.position_mm * MM_TO_PT
            x0_pt = line.start_mm * MM_TO_PT
            x1_pt = line.end_mm * MM_TO_PT
            ops.append(f"{x0_pt:.4f} {y_pt:.4f} m")
            ops.append(f"{x1_pt:.4f} {y_pt:.4f} l")
            ops.append("S")
        elif line.axis == "vertical":
            x_pt = line.position_mm * MM_TO_PT
            y0_pt = line.start_mm * MM_TO_PT
            y1_pt = line.end_mm * MM_TO_PT
            ops.append(f"{x_pt:.4f} {y0_pt:.4f} m")
            ops.append(f"{x_pt:.4f} {y1_pt:.4f} l")
            ops.append("S")

    if ocg_name:
        ops.append("EMC")

    stream = "\n".join(ops)
    return stream.encode('ascii') if ops else b""


# =============================================================================
# SHEET EXPORT — PRINT + CUT PDF
# =============================================================================

def export_sheet_print(
    sheet: Sheet,
    output_path: str,
    bleed_mm: float = DEFAULT_BLEED_MM,
    outer_bleed_dpi: int = 300,
) -> str:
    """Eksportuje print PDF arkusza (bleed fills + grafika + marks).

    Każda naklejka na arkuszu:
      1) Bleed fill (solid RGB) w pozycji placement
      2) Grafika wektorowa (show_pdf_page) w pozycji placement

    Args:
        sheet: Sheet z placements
        output_path: ścieżka do pliku wyjściowego
        bleed_mm: wielkość bleed w mm
        outer_bleed_dpi: DPI rastra dla generowania outer bleed (2mm dookoła
            grupy naklejek). 300 dla finalnego eksportu (jakość druku),
            150 dla szybkiego preview w FlexCut (4x mniej pikseli → EDT 4x
            szybsze, render PDF bez zmiany finalnego arkusza).

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

    # === PASS 0: Anti-gap fill dla bleed output PDFs ===
    # Rysujemy podkład w kolorze krawędzi PRZED naklejkami, pokrywa białe
    # hairline przerwy między sąsiadami gdy bleed=0 (lub rendering gaps).
    # Fill rozszerzony o (gap/2 + 0.5mm) w każdym kierunku — overlap z
    # sąsiadem eliminuje szpary. KLUCZOWE: pre-pass przed rysowaniem naklejek
    # (wszystkie fills trafiają na dół content stream). Inaczej fill sticker[i+1]
    # nadpisywałby sticker[i] (poprzednia implementacja miała ten bug, usunięta
    # w 70a0a48, przywrócona poprawnie tutaj jako two-pass).
    _gap_mm = getattr(sheet, 'gap_mm', 0.0)
    _gap_half_pt = (_gap_mm / 2.0 + 0.5) * MM_TO_PT
    _antigap_parts = []
    for placement in sheet.placements:
        sticker = placement.sticker
        if not getattr(sticker, 'is_bleed_output', False):
            continue
        if sticker.edge_color_rgb is None:
            continue
        sticker_w = sticker.page_width_pt + 2 * bleed_pts
        sticker_h = sticker.page_height_pt + 2 * bleed_pts
        px = placement.x_mm * MM_TO_PT
        py = placement.y_mm * MM_TO_PT
        rot = int(placement.rotation_deg) % 360
        if rot in (90, 270):
            tr = fitz.Rect(px, sheet_h_pt - py - sticker_w,
                           px + sticker_h, sheet_h_pt - py)
        else:
            tr = fitz.Rect(px, sheet_h_pt - py - sticker_h,
                           px + sticker_w, sheet_h_pt - py)
        fx0 = tr.x0 - _gap_half_pt
        fy0 = tr.y0 - _gap_half_pt
        fw = tr.width + 2 * _gap_half_pt
        fh = tr.height + 2 * _gap_half_pt
        r, g, b = sticker.edge_color_rgb
        _antigap_parts.append(
            f"q {r:.4f} {g:.4f} {b:.4f} rg "
            f"{fx0:.4f} {fy0:.4f} {fw:.4f} {fh:.4f} re f Q"
        )
    if _antigap_parts:
        inject_content_stream(
            doc_out, out_page, "\n".join(_antigap_parts).encode('ascii')
        )

    # Cache prepared documents per sticker source (ta sama naklejka × N kopii)
    _prepared_cache: dict[int, fitz.Document] = {}

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

            rot = int(placement.rotation_deg) % 360
            if rot in (90, 270):
                img_rect = fitz.Rect(
                    px + bleed_pts,
                    sheet_h_pt - py - sticker_w + bleed_pts,
                    px + bleed_pts + img_h,
                    sheet_h_pt - py - bleed_pts,
                )
            else:
                img_rect = fitz.Rect(
                    px + bleed_pts,
                    sheet_h_pt - py - sticker_h + bleed_pts,
                    px + bleed_pts + img_w,
                    sheet_h_pt - py - bleed_pts,
                )
            out_page.insert_image(img_rect, filename=sticker.raster_path, rotate=rot)

        elif sticker.pdf_doc is not None:
            # Wektor: show_pdf_page z cached prepared source
            cache_key = id(sticker.pdf_doc) * 1000 + sticker.page_index
            prepared_doc = _prepared_cache.get(cache_key)
            if prepared_doc is None:
                prepared_doc = _prepare_source_for_embedding(sticker, bleed_mm)
                _prepared_cache[cache_key] = prepared_doc

            sticker_w = sticker.page_width_pt + 2 * bleed_pts
            sticker_h = sticker.page_height_pt + 2 * bleed_pts

            px = placement.x_mm * MM_TO_PT
            py = placement.y_mm * MM_TO_PT

            rot = int(placement.rotation_deg) % 360
            if rot == 90:
                target_rect = fitz.Rect(
                    px, sheet_h_pt - py - sticker_w,
                    px + sticker_h, sheet_h_pt - py,
                )
            elif rot == 270:
                target_rect = fitz.Rect(
                    px, sheet_h_pt - py - sticker_w,
                    px + sticker_h, sheet_h_pt - py,
                )
            else:
                # 0° i 180° — ten sam rect (show_pdf_page obsługuje rotate)
                target_rect = fitz.Rect(
                    px, sheet_h_pt - py - sticker_h,
                    px + sticker_w, sheet_h_pt - py,
                )

            out_page.show_pdf_page(target_rect, prepared_doc, 0, rotate=rot)

    # === Outer bleed (spad wokół grupy naklejek) ===
    outer_bleed = getattr(sheet, 'outer_bleed_mm', 0.0)
    if outer_bleed > 0 and sheet.placements:
        _apply_outer_bleed(doc_out, out_page, sheet, bleed_mm, outer_bleed,
                           dpi=outer_bleed_dpi)

    # Marks — spot "Regmark"
    if sheet.marks:
        cs_regmark = setup_separation_colorspace(
            doc_out, out_page, SPOT_COLOR_REGMARK, cmyk_alternate=SPOT_CMYK_REGMARK
        )
        marks_stream = _build_marks_stream(sheet.marks, sheet_h_pt, cs_name=cs_regmark)
        if marks_stream:
            inject_content_stream(doc_out, out_page, marks_stream)

    # Nazwa folderu output — 5mm od dolnej krawędzi
    _insert_folder_label(out_page, output_path, sheet_w_pt, sheet_h_pt)

    # Napraw content streams — newline na końcu (zapobiega PS error na Xerox RIP)
    _fix_content_stream_newlines(doc_out, out_page)

    # PDF/X-4: OutputIntent FOGRA39
    from modules.pdf_metadata import apply_pdfx4
    apply_pdfx4(doc_out, bleed_mm=bleed_mm)

    doc_out.save(output_path, deflate=True, garbage=3)
    doc_out.close()

    # Cleanup: zamknij wszystkie przygotowane źródłowe dokumenty z cache
    # (inaczej fd leakuje — 100 naklejek = 100 otwartych fitz.Document)
    for prepared_doc in _prepared_cache.values():
        try:
            prepared_doc.close()
        except Exception:
            pass
    _prepared_cache.clear()

    log.info(f"Print PDF zapisany: {output_path}")
    return output_path


def _insert_folder_label(
    page: fitz.Page,
    output_path: str,
    sheet_w_pt: float,
    sheet_h_pt: float,
) -> None:
    """Wstawia nazwę folderu output 5mm od dolnej krawędzi arkusza.

    Tekst wycentrowany, szary, 7pt — informacja dla operatora.
    """
    import os
    folder_name = os.path.basename(os.path.dirname(os.path.abspath(output_path)))
    if not folder_name:
        return

    y_from_bottom_mm = 5.0
    y_pt = sheet_h_pt - y_from_bottom_mm * MM_TO_PT
    x_center_pt = sheet_w_pt / 2.0

    fontsize = 7
    color = (0.5, 0.5, 0.5)  # szary
    fontname = "helv"

    # Oblicz szerokość tekstu do wycentrowania
    text_width = fitz.get_text_length(folder_name, fontname=fontname, fontsize=fontsize)
    x_pt = x_center_pt - text_width / 2.0

    page.insert_text(
        fitz.Point(x_pt, y_pt),
        folder_name,
        fontsize=fontsize,
        fontname=fontname,
        color=color,
    )
    log.info(f"Folder label: '{folder_name}' @ 5mm od dołu")


def _apply_outer_bleed(
    doc: fitz.Document,
    page: fitz.Page,
    sheet: Sheet,
    bleed_mm: float,
    outer_bleed_mm: float,
    dpi: int = 300,
) -> None:
    """Generuje zewnetrzny spad po obwodzie GRUPY naklejek.

    Algorytm (Chebyshev EDT + nearest-mask-pixel lookup):
      1. Render strony jako raster 300 DPI.
      2. Zbuduj binarna maske `mask` — unia prostokatow footprintow
         placementow. Definiuje ksztalt grupy.
      3. `scipy.ndimage.distance_transform_cdt(~mask, metric='chessboard',
         return_indices=True)` — dla kazdego piksela poza maska zwraca
         Chebyshev-dystans do maski + indeksy najblizszego piksela maski.
      4. Piksele z dist <= bleed_px dostaja kolor z najblizszego piksela
         maski (INDEKSY podane przez EDT).
      5. Wstaw zmodyfikowany raster jako tlo (overlay=False).

    Wlasnosci:
      - Kolor spadu pobierany z NAJBLIZSZEGO piksela maski — dla liniowej
        krawedzi propagacja PROSTOPADLA (pixel nad krawedzia -> pixel
        bezposrednio pod nim). Dla naroznika — kolor piksela naroznikowego.
        Bez diagonalnych artefaktow "skosu".
      - Chebyshev metric -> SZKADRATOWE narozniki spadu (2mm × 2mm).
      - Jednoprzebiegowa dilacja przez C-zoptymalizowane scipy — znaczaco
         szybsze niz iteracyjny numpy (>10x na typowym arkuszu).
    """
    from PIL import Image as PILImage
    import numpy as np
    from scipy.ndimage import distance_transform_cdt
    import math

    sheet_w_pt = sheet.width_mm * MM_TO_PT
    sheet_h_pt = sheet.height_mm * MM_TO_PT

    bleed2 = 2 * bleed_mm
    def _pw(p):
        return p.sticker.height_mm + bleed2 if int(p.rotation_deg) % 360 in (90, 270) else p.sticker.width_mm + bleed2
    def _ph(p):
        return p.sticker.width_mm + bleed2 if int(p.rotation_deg) % 360 in (90, 270) else p.sticker.height_mm + bleed2

    ob_mm = outer_bleed_mm

    # Render strony jako raster (DPI konfigurowalne: 300 dla finalnego eksportu,
    # 150 dla preview w FlexCut — EDT scaling quadratycznie z pikselami)
    scale = dpi / 72.0
    # math.ceil gwarantuje pelne pokrycie 2mm. int() by dalo 23px=1.95mm
    # i w rogach Chebyshev dist=24 wypadlo poza bleed_px → rogi spadu bialo.
    bleed_px = max(1, int(math.ceil(ob_mm * dpi / 25.4)))
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    img = PILImage.frombytes("RGB", (pix.width, pix.height), pix.samples)
    arr = np.array(img)
    H, W = arr.shape[:2]

    # Memory guard — pelna strona + maska
    _check_raster_memory(W, H, channels=3, context="outer bleed union-mask (sheet)")

    # Zbuduj maske unii placementow (prostokaty footprintow)
    mask = np.zeros((H, W), dtype=bool)
    for p in sheet.placements:
        pw_mm = _pw(p)
        ph_mm = _ph(p)
        px0 = int(round(p.x_mm * MM_TO_PT * scale))
        py0 = int(round((sheet_h_pt - (p.y_mm + ph_mm) * MM_TO_PT) * scale))
        px1 = int(round((p.x_mm + pw_mm) * MM_TO_PT * scale))
        py1 = int(round((sheet_h_pt - p.y_mm * MM_TO_PT) * scale))
        px0 = max(0, px0); py0 = max(0, py0)
        px1 = min(W, px1); py1 = min(H, py1)
        if px1 > px0 and py1 > py0:
            mask[py0:py1, px0:px1] = True

    if not mask.any():
        log.warning("Outer bleed: brak placementow — pomijam")
        return

    # AXIS-ALIGNED propagation (decomposed):
    #   - Dla kazdej KOLUMNY rastra propaguje pionowo (UP/DOWN) kolor z
    #     najblizszego content pixela w TEJ SAMEJ KOLUMNIE — bleed_px rows.
    #   - Dla kazdego WIERSZA propaguje poziomo (LEFT/RIGHT) kolor z
    #     najblizszego content pixela w TYM SAMYM WIERSZU — bleed_px cols.
    #   - Laczenie: pixel filled przez WIELE propagacji -> najblizsza wygrywa.
    #   - Pixel filled przez tylko JEDNA -> uzywa jej.
    #   - Pixel filled przez ZADNA -> bialy.
    #
    # Motywacja (kształt litery L w grupie):
    #   Przy wklęsłym naroźniku (top sticker węższy niż bottom grid) EDT z
    #   Chebyshev/Manhattan metric daje DIAGONAL boundary miedzy propagacja
    #   z right edge top sticker i top edge bottom grid — wyglada "pod katem".
    #   User chce: wybierz JEDNO ramie L i wyprowadz prostopadle, drugie
    #   niech bedzie dopelnieniem.
    #   Axis-aligned: zawsze perpendicular po osi (nigdy diagonalnie). W
    #   narozniku konkawym najblizsze ramie wygrywa (czysta prostokatna
    #   transition zamiast diagonal).
    WHITE_THRESH = 245
    content_mask = mask & ~np.all(arr > WHITE_THRESH, axis=2)
    if not content_mask.any():
        log.warning("Outer bleed: brak solidnej tresci w naklejkach — pomijam")
        return

    # 1D-axis EDT via forward/backward cummax trick (vectorized, bez pętli).
    # Idee: dla każdej osobnej osi wykonujemy distance-to-nearest-content
    # uzywajac `np.maximum.accumulate` z indeksem (r lub c) gdzie jest content.

    rows_idx = np.arange(H)[:, None]  # (H, 1)
    cols_idx = np.arange(W)[None, :]  # (1, W)

    # --- PIONOWA propagacja (per kolumna) ---
    # forward: dla każdego (r, c) ostatni content row <= r w kolumnie c
    up_rows = np.where(content_mask, rows_idx, -1).astype(np.int32)
    up_cummax = np.maximum.accumulate(up_rows, axis=0)
    up_dist = rows_idx - up_cummax  # dist od content powyzej
    up_dist[up_cummax < 0] = H + 1  # brak content powyzej

    # backward: pierwszy content row >= r w kolumnie c
    # Flip, cummax, flip back
    down_rows = np.where(content_mask, rows_idx, H + 1).astype(np.int32)
    down_cummin = np.minimum.accumulate(down_rows[::-1], axis=0)[::-1]
    down_dist = down_cummin - rows_idx
    down_dist[down_cummin > H] = H + 1

    # pick closer (up or down) for vertical
    v_src_row = np.where(up_dist <= down_dist, up_cummax, down_cummin).astype(np.int32)
    v_dist = np.minimum(up_dist, down_dist)

    # --- POZIOMA propagacja (per wiersz) ---
    left_cols = np.where(content_mask, cols_idx, -1).astype(np.int32)
    left_cummax = np.maximum.accumulate(left_cols, axis=1)
    left_dist = cols_idx - left_cummax
    left_dist[left_cummax < 0] = W + 1

    right_cols = np.where(content_mask, cols_idx, W + 1).astype(np.int32)
    right_cummin = np.minimum.accumulate(right_cols[:, ::-1], axis=1)[:, ::-1]
    right_dist = right_cummin - cols_idx
    right_dist[right_cummin > W] = W + 1

    h_src_col = np.where(left_dist <= right_dist, left_cummax, right_cummin).astype(np.int32)
    h_dist = np.minimum(left_dist, right_dist)

    # --- Laczenie ---
    # Strefa spadu = Chebyshev dist od placement mask <= bleed_px (2mm kwadrat).
    # Zrodlo koloru:
    #   - axis-aligned: blizsza os (v lub h) jesli w zasiegu bleed_px
    #   - diagonal corner fallback: jesli axis-aligned nie siega (np. rog
    #     convex 2mm× 2mm gdzie oba axes > bleed_px), uzyj EDT do content_mask
    #     (Chebyshev -> wskazuje na najblizszy content pixel, tu = rog maski)
    dist_mask = distance_transform_cdt(~mask, metric='chessboard')
    fill_zone = (~mask) & (dist_mask <= bleed_px) & (dist_mask > 0)

    # Najblizsza os (dla kazdego pixela w fill_zone)
    axis_reachable = (v_dist <= bleed_px) | (h_dist <= bleed_px)
    use_vertical = v_dist <= h_dist

    # Dla pikseli gdzie axis-aligned nie dziala (convex corner z dist > bleed_px
    # po obu osiach) fallback na EDT content_mask — daje kolor z najblizszego
    # content pixela (naturalnie rog convex dostaje kolor z narożnika maski).
    _, idx_content = distance_transform_cdt(
        ~content_mask, metric='chessboard', return_indices=True
    )

    rr, cc = np.where(fill_zone)
    axis_ok = axis_reachable[rr, cc]
    use_v = use_vertical[rr, cc] & axis_ok
    use_h = (~use_vertical[rr, cc]) & axis_ok

    src_rows = np.where(
        use_v, v_src_row[rr, cc],
        np.where(use_h, rr, idx_content[0, rr, cc])
    )
    src_cols = np.where(
        use_v, cc,
        np.where(use_h, h_src_col[rr, cc], idx_content[1, rr, cc])
    )
    arr[rr, cc] = arr[src_rows, src_cols]

    # Wstaw rozszerzony raster jako tlo — pokrywa cala strone,
    # ale tylko obszar unia+bleed ma content, reszta jest biala (oryginalne tlo).
    bleed_img = PILImage.fromarray(arr)
    insert_rect = fitz.Rect(0, 0, sheet_w_pt, sheet_h_pt)
    with _SafeTempPng(".png") as tmp:
        bleed_img.save(tmp.name)
        page.insert_image(insert_rect, filename=tmp.name, overlay=False)

    log.info(f"Outer bleed: {ob_mm}mm union-mask dilation, {len(sheet.placements)} placementow")


def _str_to_utf16be_hex(s: str) -> str:
    """Konwertuje string na PDF hex string w UTF-16BE (z BOM FEFF)."""
    encoded = s.encode('utf-16-be')
    hex_str = 'FEFF' + encoded.hex().upper()
    return f"<{hex_str}>"


def _setup_cut_ocg_layers(
    doc: fitz.Document, page: fitz.Page,
    layer_config: dict,
) -> dict[str, str]:
    """Tworzy OCG warstwy dla cut PDF i rejestruje w Resources/Properties.

    Args:
        layer_config: dict z kluczami "CutContour", "FlexCut", "Regmark".
            Każdy ma {"ocg_name": str, "cmyk": tuple}.
            Summa: CUT_SUMMA_LAYERS, JWEI: CUT_JWEI_LAYERS

    Returns:
        {"CutContour": {"prop": "Pr0", "cmyk": (c,m,y,k)}, ...}
    """
    result = {}
    ocg_xrefs = {}

    # 1. Utwórz OCG obiekty z UTF-16BE names
    for i, (key, cfg) in enumerate(layer_config.items()):
        ocg_name = cfg["ocg_name"]
        prop_name = f"Pr{i}"
        xref = doc.get_new_xref()
        utf16_name = _str_to_utf16be_hex(ocg_name)
        doc.update_object(xref, f"<</Name {utf16_name} /Type /OCG>>")
        ocg_xrefs[key] = xref
        result[key] = {"prop": prop_name, "cmyk": cfg["cmyk"]}

    # 2. Zarejestruj w katalogu (/OCProperties)
    cat_xref = doc.pdf_catalog()
    ocg_refs = " ".join(f"{x} 0 R" for x in ocg_xrefs.values())
    doc.xref_set_key(cat_xref, "OCProperties",
        f"<</OCGs [{ocg_refs}] "
        f"/D <</OFF [] /Order [{ocg_refs}] /RBGroups []>>>>")

    # 3. Zarejestruj w /Resources/Properties strony
    props_entries = " ".join(
        f"/{result[key]['prop']} {ocg_xrefs[key]} 0 R"
        for key in layer_config
    )
    page_xref = page.xref
    res_info = doc.xref_get_key(page_xref, "Resources")
    if res_info[0] == "xref":
        import re as _re
        res_xref = int(_re.search(r'(\d+)', res_info[1]).group(1))
        doc.xref_set_key(res_xref, "Properties", f"<<{props_entries}>>")
    else:
        doc.xref_set_key(page_xref, "Resources/Properties", f"<<{props_entries}>>")

    ocg_names = [cfg["ocg_name"] for cfg in layer_config.values()]
    log.info(f"OCG layers: {', '.join(ocg_names)}")
    return result


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

    # === OCG layers — per ploter (z config) ===
    from config import PLOTTERS
    plotter_cfg = PLOTTERS.get(plotter, {})
    layer_config = plotter_cfg.get("cut_layers", CUT_SUMMA_LAYERS)
    ocg = _setup_cut_ocg_layers(doc_out, out_page, layer_config)

    # Pozycje FlexCut linii w pt — do filtrowania CutContour.
    # Zachowujemy pelne span'y (start..end) zeby filtr sprawdzal czy
    # segment kiss-cut RZECZYWISCIE lezy W ZAKRESIE FlexCut — nie tylko
    # ma te sama wspolrzedna. Bez tego kiss-cut naklejek poza obszarem
    # FlexCut (ale o przypadkowej wspolrzednej identycznej z FlexCut)
    # byly blednie usuwane.
    flexcut_h = []  # list[(pos_mm, start_mm, end_mm)]
    flexcut_v = []
    flexcut_h_pt = []
    flexcut_v_pt = []
    for pl in sheet.panel_lines:
        if pl.bridge_length_mm <= 0:
            continue
        if pl.axis == "horizontal":
            flexcut_h.append((pl.position_mm, pl.start_mm, pl.end_mm))
            flexcut_h_pt.append(pl.position_mm * MM_TO_PT)
        elif pl.axis == "vertical":
            flexcut_v.append((pl.position_mm, pl.start_mm, pl.end_mm))
            flexcut_v_pt.append(pl.position_mm * MM_TO_PT)

    # Deduplikacja CutContour
    deduped = _deduplicate_cut_segments(
        sheet.placements, flexcut_h, flexcut_v, bleed_mm,
        gap_mm=sheet.gap_mm,
    )

    # CutContour / FlexCut — per sticker cutline_mode
    for placement, segments in deduped:
        sticker_mode = getattr(placement.sticker, 'cutline_mode', 'kiss-cut')
        if sticker_mode == "flexcut":
            cut_cfg = ocg["FlexCut"]
        else:
            cut_cfg = ocg["CutContour"]
        cut_stream = _build_sheet_cutcontour_stream(
            placement, sheet_h_pt, bleed_mm,
            segments_override=segments,
            flexcut_h_pt=flexcut_h_pt,
            flexcut_v_pt=flexcut_v_pt,
            cut_ocg_name=cut_cfg["prop"],
            cut_cmyk=cut_cfg["cmyk"],
        )
        if cut_stream:
            inject_content_stream(doc_out, out_page, cut_stream)

    # Full-cut panel lines (bridge=0, np. spad) — na warstwie CutContour
    fullcut_lines = [pl for pl in sheet.panel_lines if pl.bridge_length_mm <= 0]
    if fullcut_lines:
        fullcut_stream = _build_flexcut_stream(
            fullcut_lines, sheet.width_mm, sheet.height_mm,
            ocg_name=cut_cfg["prop"],
            ocg_cmyk=cut_cfg["cmyk"],
        )
        if fullcut_stream:
            inject_content_stream(doc_out, out_page, fullcut_stream)

    # FlexCut (tylko linie z bridge > 0)
    flexcut_lines = [pl for pl in sheet.panel_lines if pl.bridge_length_mm > 0]
    if flexcut_lines:
        flex_cfg = ocg["FlexCut"]
        flexcut_stream = _build_flexcut_stream(
            flexcut_lines, sheet.width_mm, sheet.height_mm,
            ocg_name=flex_cfg["prop"],
            ocg_cmyk=flex_cfg["cmyk"],
        )
        if flexcut_stream:
            inject_content_stream(doc_out, out_page, flexcut_stream)

    # Marks
    if sheet.marks:
        reg_cfg = ocg["Regmark"]
        marks_stream = _build_marks_stream(
            sheet.marks, sheet_h_pt, ocg_name=reg_cfg["prop"])
        if marks_stream:
            inject_content_stream(doc_out, out_page, marks_stream)

    _fix_content_stream_newlines(doc_out, out_page)
    doc_out.save(output_path, deflate=True, garbage=3)
    doc_out.close()
    log.info(f"Cut PDF zapisany: {output_path}")
    return output_path


def export_sheet_white(
    sheet: Sheet,
    output_path: str,
    bleed_mm: float = DEFAULT_BLEED_MM,
) -> str:
    """Eksportuje osobny PDF z białym poddrukiem (spot color White).

    Zawartość:
      - White fill dla każdej naklejki (kontur cięcia z insetem 0.3mm)
      - Spot color Separation "White" — drukarka UV drukuje białym tuszem

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
        f"Export white PDF: {sheet.width_mm:.0f}×{sheet.height_mm:.0f}mm, "
        f"{len(sheet.placements)} naklejek"
    )

    doc_out = fitz.open()
    out_page = doc_out.new_page(width=sheet_w_pt, height=sheet_h_pt)

    # Setup White Separation
    cs_white = setup_separation_colorspace(
        doc_out, out_page, SPOT_COLOR_WHITE, cmyk_alternate=SPOT_CMYK_WHITE,
    )

    for i, placement in enumerate(sheet.placements):
        sticker = placement.sticker
        if not sticker.bleed_segments:
            continue

        white_stream = _build_sheet_white_fill_stream(
            placement, sheet_h_pt, bleed_mm, cs_white,
        )
        if white_stream:
            inject_content_stream(doc_out, out_page, white_stream)

    _fix_content_stream_newlines(doc_out, out_page)

    # PDF/X-4 metadata
    try:
        from modules.pdf_metadata import apply_pdfx4
        apply_pdfx4(doc_out, bleed_mm=bleed_mm)
    except Exception as e:
        log.warning(f"PDF/X-4 metadata (white): {e}")

    doc_out.save(output_path, deflate=True, garbage=3)
    doc_out.close()
    log.info(f"White PDF zapisany: {output_path}")
    return output_path


def export_sheet(
    sheet: Sheet,
    print_output_path: str,
    cut_output_path: str,
    bleed_mm: float = DEFAULT_BLEED_MM,
    plotter: str = "summa_s3",
    white: bool = False,
    white_output_path: str | None = None,
) -> tuple[str, str]:
    """Eksportuje PDF-y arkusza (print + cut + opcjonalnie white).

    Returns:
        (print_path, cut_path)
    """
    print_path = export_sheet_print(sheet, print_output_path, bleed_mm)
    cut_path = export_sheet_cut(sheet, cut_output_path, bleed_mm, plotter)
    if white and white_output_path:
        export_sheet_white(sheet, white_output_path, bleed_mm)
    return print_path, cut_path
