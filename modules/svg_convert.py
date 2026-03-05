"""
SVG → PDF conversion using cairosvg.

Converts SVG files to temporary PDF files with correct dimensions
based on the filename (e.g. "plik 50x50.svg" → 50mm × 50mm).
Also extracts the sticker contour (cut path) from SVG clipPaths.

Pipeline:
  1. parse_size_from_filename() → wymiary mm z nazwy pliku
  2. svg_to_pdf()              → konwersja SVG → PDF (cairosvg, wektory)
  3. extract_svg_contours()    → kontur cięcia z clipPath SVG
"""

from __future__ import annotations

import ctypes.util
import logging
import os
import re
import tempfile
import xml.etree.ElementTree as ET

import numpy as np

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Zapewnij dostęp do biblioteki cairo (Homebrew na macOS)
# ---------------------------------------------------------------------------
_HOMEBREW_LIB = "/opt/homebrew/lib"
if os.path.isdir(_HOMEBREW_LIB):
    _dyld = os.environ.get("DYLD_LIBRARY_PATH", "")
    if _HOMEBREW_LIB not in _dyld:
        os.environ["DYLD_LIBRARY_PATH"] = (
            f"{_HOMEBREW_LIB}:{_dyld}" if _dyld else _HOMEBREW_LIB
        )

# Monkey-patch ctypes.util.find_library żeby znalazło cairo z Homebrew
_orig_find_library = ctypes.util.find_library

def _patched_find_library(name: str) -> str | None:
    result = _orig_find_library(name)
    if result is None and name in ("cairo", "cairo-2"):
        # Szukaj w /opt/homebrew/lib
        for candidate in ("libcairo.2.dylib", "libcairo.dylib"):
            full = os.path.join(_HOMEBREW_LIB, candidate)
            if os.path.isfile(full):
                return full
    return result

ctypes.util.find_library = _patched_find_library

try:
    import cairosvg  # noqa: E402  — musi być po ustawieniu DYLD
    HAS_CAIROSVG = True
except ImportError:
    HAS_CAIROSVG = False

# ---------------------------------------------------------------------------
# SVG namespace
# ---------------------------------------------------------------------------
_SVG_NS = "http://www.w3.org/2000/svg"

# ---------------------------------------------------------------------------
# Parsowanie wymiarów z nazwy pliku
# ---------------------------------------------------------------------------
_SIZE_RE = re.compile(r'(\d+(?:[.,]\d+)?)\s*[xX×]\s*(\d+(?:[.,]\d+)?)')


def parse_size_from_filename(filepath: str) -> tuple[float, float] | None:
    """Wyciąga wymiary (mm) z nazwy pliku, np. '50x50' → (50, 50).

    Obsługuje formaty: 50x50, 50X50, 50×50, 50.5x30
    Zwraca None jeśli nie znaleziono wzorca.
    """
    name = os.path.splitext(os.path.basename(filepath))[0]
    m = _SIZE_RE.search(name)
    if m:
        w = float(m.group(1).replace(",", "."))
        h = float(m.group(2).replace(",", "."))
        if w > 0 and h > 0:
            return (w, h)
    return None


# ---------------------------------------------------------------------------
# Konwersja SVG → PDF
# ---------------------------------------------------------------------------
_MM_TO_PT = 72.0 / 25.4


def svg_to_pdf(svg_path: str, target_w_mm: float | None = None,
               target_h_mm: float | None = None) -> str:
    """Convert an SVG file to a temporary PDF file using cairosvg.

    Dimensions are set from target_w_mm/target_h_mm (from filename).
    Falls back to SVG intrinsic size if no target given.

    Returns the path to the generated temp PDF.
    """
    if not HAS_CAIROSVG:
        raise ImportError("cairosvg is required for SVG conversion. Install with: pip install cairosvg")
    if not os.path.isfile(svg_path):
        raise FileNotFoundError(f"SVG file not found: {svg_path}")

    base = os.path.splitext(os.path.basename(svg_path))[0]
    tmp = tempfile.NamedTemporaryFile(
        prefix=f"stk_{base}_", suffix=".pdf", delete=False,
    )
    tmp.close()

    kwargs: dict = {"url": svg_path, "write_to": tmp.name}

    if target_w_mm and target_h_mm:
        # Oblicz skalę na podstawie viewBox SVG
        vb_w, vb_h = _get_viewbox_size(svg_path)
        if vb_w and vb_h:
            target_w_pt = target_w_mm * _MM_TO_PT
            target_h_pt = target_h_mm * _MM_TO_PT
            # cairosvg domyślnie renderuje viewBox 1:1 (viewBox units = pt)
            # scale = target / viewbox daje nam poprawny rozmiar
            scale_x = target_w_pt / vb_w
            scale_y = target_h_pt / vb_h
            scale = min(scale_x, scale_y)  # zachowaj proporcje
            kwargs["scale"] = scale
            log.info(
                f"SVG→PDF: viewBox={vb_w:.1f}×{vb_h:.1f}, "
                f"target={target_w_mm:.1f}×{target_h_mm:.1f}mm, scale={scale:.4f}"
            )
        else:
            # Brak viewBox — użyj output_width/height
            kwargs["output_width"] = str(target_w_mm * _MM_TO_PT)
            kwargs["output_height"] = str(target_h_mm * _MM_TO_PT)

    cairosvg.svg2pdf(**kwargs)

    log.info(f"SVG→PDF: {svg_path} → {tmp.name} ({os.path.getsize(tmp.name)} bytes)")
    return tmp.name


def _get_viewbox_size(svg_path: str) -> tuple[float | None, float | None]:
    """Wyciąga rozmiar viewBox z SVG."""
    try:
        tree = ET.parse(svg_path)
        root = tree.getroot()
        vb = root.get("viewBox")
        if vb:
            parts = vb.split()
            if len(parts) == 4:
                return float(parts[2]), float(parts[3])
    except Exception:
        pass
    return None, None


# ---------------------------------------------------------------------------
# Parsowanie SVG path d-attribute
# ---------------------------------------------------------------------------

def _parse_svg_path_d(d: str) -> list[tuple]:
    """Parsuje SVG path 'd' attribute na listę komend.

    Zwraca: [('M', x, y), ('L', x, y), ('C', x1,y1,x2,y2,x,y), ('Z',), ...]
    Wszystkie koordynaty absolutne.
    """
    tokens = re.findall(
        r'[MmZzLlHhVvCcSsQqTtAa]|[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?', d
    )

    commands: list[tuple] = []
    i = 0
    cmd = None
    cx, cy = 0.0, 0.0  # current point
    sx, sy = 0.0, 0.0  # subpath start

    while i < len(tokens):
        t = tokens[i]
        if t.isalpha():
            cmd = t
            i += 1
            if cmd in ("Z", "z"):
                commands.append(("Z",))
                cx, cy = sx, sy
                continue
        if cmd is None:
            i += 1
            continue

        try:
            if cmd == "M":
                x, y = float(tokens[i]), float(tokens[i + 1])
                commands.append(("M", x, y))
                cx, cy = x, y
                sx, sy = x, y
                i += 2
                cmd = "L"  # subsequent are lineTo
            elif cmd == "m":
                x, y = float(tokens[i]), float(tokens[i + 1])
                cx, cy = cx + x, cy + y
                commands.append(("M", cx, cy))
                sx, sy = cx, cy
                i += 2
                cmd = "l"
            elif cmd == "L":
                x, y = float(tokens[i]), float(tokens[i + 1])
                commands.append(("L", x, y))
                cx, cy = x, y
                i += 2
            elif cmd == "l":
                x, y = float(tokens[i]), float(tokens[i + 1])
                cx, cy = cx + x, cy + y
                commands.append(("L", cx, cy))
                i += 2
            elif cmd == "H":
                cx = float(tokens[i])
                commands.append(("L", cx, cy))
                i += 1
            elif cmd == "h":
                cx += float(tokens[i])
                commands.append(("L", cx, cy))
                i += 1
            elif cmd == "V":
                cy = float(tokens[i])
                commands.append(("L", cx, cy))
                i += 1
            elif cmd == "v":
                cy += float(tokens[i])
                commands.append(("L", cx, cy))
                i += 1
            elif cmd == "C":
                x1 = float(tokens[i]); y1 = float(tokens[i + 1])
                x2 = float(tokens[i + 2]); y2 = float(tokens[i + 3])
                x = float(tokens[i + 4]); y = float(tokens[i + 5])
                commands.append(("C", x1, y1, x2, y2, x, y))
                cx, cy = x, y
                i += 6
            elif cmd == "c":
                x1 = cx + float(tokens[i]); y1 = cy + float(tokens[i + 1])
                x2 = cx + float(tokens[i + 2]); y2 = cy + float(tokens[i + 3])
                x = cx + float(tokens[i + 4]); y = cy + float(tokens[i + 5])
                commands.append(("C", x1, y1, x2, y2, x, y))
                cx, cy = x, y
                i += 6
            elif cmd == "S":
                x2 = float(tokens[i]); y2 = float(tokens[i + 1])
                x = float(tokens[i + 2]); y = float(tokens[i + 3])
                commands.append(("C", cx, cy, x2, y2, x, y))  # simplified
                cx, cy = x, y
                i += 4
            elif cmd == "s":
                x2 = cx + float(tokens[i]); y2 = cy + float(tokens[i + 1])
                x = cx + float(tokens[i + 2]); y = cy + float(tokens[i + 3])
                commands.append(("C", cx, cy, x2, y2, x, y))
                cx, cy = x, y
                i += 4
            elif cmd == "Q":
                x1 = float(tokens[i]); y1 = float(tokens[i + 1])
                x = float(tokens[i + 2]); y = float(tokens[i + 3])
                # Convert quadratic to cubic
                c1x = cx + 2 / 3 * (x1 - cx); c1y = cy + 2 / 3 * (y1 - cy)
                c2x = x + 2 / 3 * (x1 - x); c2y = y + 2 / 3 * (y1 - y)
                commands.append(("C", c1x, c1y, c2x, c2y, x, y))
                cx, cy = x, y
                i += 4
            elif cmd == "q":
                x1 = cx + float(tokens[i]); y1 = cy + float(tokens[i + 1])
                x = cx + float(tokens[i + 2]); y = cy + float(tokens[i + 3])
                c1x = cx + 2 / 3 * (x1 - cx); c1y = cy + 2 / 3 * (y1 - cy)
                c2x = x + 2 / 3 * (x1 - x); c2y = y + 2 / 3 * (y1 - y)
                commands.append(("C", c1x, c1y, c2x, c2y, x, y))
                cx, cy = x, y
                i += 4
            else:
                i += 1  # skip unknown
        except (IndexError, ValueError):
            break

    return commands


def _is_simple_rect(commands: list[tuple]) -> bool:
    """Sprawdza czy ścieżka to prosty prostokąt (M + 3-4 L + Z)."""
    moves = [c for c in commands if c[0] == "M"]
    lines = [c for c in commands if c[0] == "L"]
    curves = [c for c in commands if c[0] == "C"]
    if curves:
        return False
    if len(moves) <= 2 and len(lines) <= 5 and not curves:
        return True
    return False


def _commands_to_segments(commands: list[tuple], scale: float) -> list[tuple]:
    """Konwertuje SVG commands na segmenty konturu.

    Zwraca: [('l', np.array(start), np.array(end)),
             ('c', np.array(p0), np.array(p1), np.array(p2), np.array(p3)), ...]

    Segmenty skalowane przez `scale` (viewBox → pt → mm).
    """
    segments = []
    cx, cy = 0.0, 0.0
    sx, sy = 0.0, 0.0  # subpath start

    for cmd in commands:
        if cmd[0] == "M":
            cx, cy = cmd[1] * scale, cmd[2] * scale
            sx, sy = cx, cy
        elif cmd[0] == "L":
            nx, ny = cmd[1] * scale, cmd[2] * scale
            segments.append(("l", np.array([cx, cy]), np.array([nx, ny])))
            cx, cy = nx, ny
        elif cmd[0] == "C":
            x1, y1 = cmd[1] * scale, cmd[2] * scale
            x2, y2 = cmd[3] * scale, cmd[4] * scale
            x, y = cmd[5] * scale, cmd[6] * scale
            segments.append((
                "c",
                np.array([cx, cy]),
                np.array([x1, y1]),
                np.array([x2, y2]),
                np.array([x, y]),
            ))
            cx, cy = x, y
        elif cmd[0] == "Z":
            # Zamknij ścieżkę linią do startu
            if abs(cx - sx) > 0.01 or abs(cy - sy) > 0.01:
                segments.append(("l", np.array([cx, cy]), np.array([sx, sy])))
            cx, cy = sx, sy

    return segments


# ---------------------------------------------------------------------------
# Ekstrakcja konturów z SVG clipPaths
# ---------------------------------------------------------------------------

def extract_svg_contour(
    svg_path: str,
    target_w_mm: float,
    target_h_mm: float,
) -> list[tuple] | None:
    """Wyciąga kontur naklejki z clipPath w SVG.

    Strategia: szuka największego nie-prostokątnego clipPath w SVG.
    Jeden SVG = jedna naklejka, nawet jeśli ma wiele warstw/grup.
    Największy kształtowy clipPath = kontur cięcia naklejki.

    Returns:
        Lista segmentów konturu w przestrzeni pt, lub None.
        Segmenty: [('l', start, end), ('c', p0, p1, p2, p3), ...]
    """
    tree = ET.parse(svg_path)
    root = tree.getroot()

    # viewBox → do obliczenia skali
    vb_w, vb_h = _get_viewbox_size(svg_path)
    if not vb_w or not vb_h:
        log.warning("SVG bez viewBox, nie można wyciągnąć konturu")
        return None

    # Skala: viewBox coords → pt (docelowy rozmiar)
    target_w_pt = target_w_mm * _MM_TO_PT
    target_h_pt = target_h_mm * _MM_TO_PT
    scale_x = target_w_pt / vb_w
    scale_y = target_h_pt / vb_h
    scale = min(scale_x, scale_y)

    # Zbierz wszystkie clipPath definicje
    clip_defs: dict[str, list[tuple]] = {}  # id → parsed commands
    for clip_elem in root.iter(f"{{{_SVG_NS}}}clipPath"):
        cid = clip_elem.get("id", "")
        for path_elem in clip_elem.findall(f"{{{_SVG_NS}}}path"):
            d = path_elem.get("d", "")
            if d:
                cmds = _parse_svg_path_d(d)
                clip_defs[cid] = cmds

    # Szukaj największego nie-prostokątnego clipPath
    best_contour = _find_best_global_contour(clip_defs, scale)
    if best_contour:
        n_l = sum(1 for s in best_contour if s[0] == "l")
        n_c = sum(1 for s in best_contour if s[0] == "c")
        log.info(
            f"SVG contour: {len(best_contour)} segmentów "
            f"({n_l} linii, {n_c} krzywych)"
        )
        return best_contour

    log.warning("SVG contour: nie znaleziono konturu w clipPath")
    return None


def _find_best_global_contour(
    clip_defs: dict, scale: float
) -> list[tuple] | None:
    """Znajduje największy nie-prostokątny clipPath jako fallback."""
    best_area = 0.0
    best_segments = None

    for cid, cmds in clip_defs.items():
        if _is_simple_rect(cmds):
            continue
        segments = _commands_to_segments(cmds, scale)
        if not segments:
            continue
        # Oblicz bbox area
        all_pts = []
        for seg in segments:
            if seg[0] == "l":
                all_pts.extend([seg[1], seg[2]])
            elif seg[0] == "c":
                all_pts.extend([seg[1], seg[4]])
        if all_pts:
            arr = np.array(all_pts)
            area = (arr[:, 0].max() - arr[:, 0].min()) * (
                arr[:, 1].max() - arr[:, 1].min()
            )
            if area > best_area:
                best_area = area
                best_segments = segments

    return best_segments


def _extract_clip_id(clip_ref: str) -> str | None:
    """Wyciąga ID z 'url(#abc123)' → 'abc123'."""
    m = re.match(r'url\(#([^)]+)\)', clip_ref)
    return m.group(1) if m else None
