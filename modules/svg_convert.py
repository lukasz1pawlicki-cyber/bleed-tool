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
import math
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
from config import MM_TO_PT as _MM_TO_PT


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

def _arc_endpoint_to_center(
    x1: float, y1: float, rx: float, ry: float,
    phi_deg: float, fa: int, fs: int, x2: float, y2: float,
) -> tuple[float, float, float, float, float, float, float, float]:
    """SVG arc endpoint parameterization → center parameterization.

    Returns: (cx, cy, rx, ry, start_angle, sweep_angle, cos_phi, sin_phi)
    Implements the algorithm from SVG spec F.6.5-F.6.6.
    """
    phi = math.radians(phi_deg)
    cos_phi = math.cos(phi)
    sin_phi = math.sin(phi)

    # F.6.5.1 — compute (x1', y1')
    dx2 = (x1 - x2) / 2.0
    dy2 = (y1 - y2) / 2.0
    x1p = cos_phi * dx2 + sin_phi * dy2
    y1p = -sin_phi * dx2 + cos_phi * dy2

    # F.6.6.2 — ensure radii are large enough
    rx = abs(rx)
    ry = abs(ry)
    x1p2 = x1p * x1p
    y1p2 = y1p * y1p
    rx2 = rx * rx
    ry2 = ry * ry

    lam = x1p2 / rx2 + y1p2 / ry2
    if lam > 1.0:
        lam_sqrt = math.sqrt(lam)
        rx *= lam_sqrt
        ry *= lam_sqrt
        rx2 = rx * rx
        ry2 = ry * ry

    # F.6.5.2 — compute (cx', cy')
    num = max(rx2 * ry2 - rx2 * y1p2 - ry2 * x1p2, 0.0)
    den = rx2 * y1p2 + ry2 * x1p2
    sq = math.sqrt(num / den) if den > 0 else 0.0
    if fa == fs:
        sq = -sq
    cxp = sq * rx * y1p / ry
    cyp = -sq * ry * x1p / rx

    # F.6.5.3 — compute (cx, cy)
    cx = cos_phi * cxp - sin_phi * cyp + (x1 + x2) / 2.0
    cy = sin_phi * cxp + cos_phi * cyp + (y1 + y2) / 2.0

    # F.6.5.5-6 — compute start_angle and sweep_angle
    def _angle(ux: float, uy: float, vx: float, vy: float) -> float:
        dot = ux * vx + uy * vy
        length = math.sqrt(ux * ux + uy * uy) * math.sqrt(vx * vx + vy * vy)
        cos_val = max(-1.0, min(1.0, dot / length)) if length > 0 else 1.0
        a = math.acos(cos_val)
        if ux * vy - uy * vx < 0:
            a = -a
        return a

    start_angle = _angle(1.0, 0.0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    sweep_angle = _angle(
        (x1p - cxp) / rx, (y1p - cyp) / ry,
        (-x1p - cxp) / rx, (-y1p - cyp) / ry,
    )

    # F.6.5.6 — adjust sweep
    if fs == 0 and sweep_angle > 0:
        sweep_angle -= 2 * math.pi
    elif fs == 1 and sweep_angle < 0:
        sweep_angle += 2 * math.pi

    return cx, cy, rx, ry, start_angle, sweep_angle, cos_phi, sin_phi


def _arc_to_beziers(
    cx: float, cy: float, rx: float, ry: float,
    start_angle: float, sweep_angle: float,
    cos_phi: float, sin_phi: float,
) -> list[tuple[float, float, float, float, float, float]]:
    """Convert an arc segment to cubic bezier curves.

    Returns list of (cp1x, cp1y, cp2x, cp2y, x, y) tuples.
    """
    if abs(sweep_angle) < 1e-10:
        return []

    n_segs = max(1, int(abs(sweep_angle) / (math.pi / 2) + 0.999))
    delta = sweep_angle / n_segs
    segments = []

    for seg_i in range(n_segs):
        t1 = start_angle + seg_i * delta
        t2 = t1 + delta
        half = delta / 2.0
        tan_half = math.tan(half)
        alpha = math.sin(delta) * (math.sqrt(4 + 3 * tan_half * tan_half) - 1) / 3

        cos_t1, sin_t1 = math.cos(t1), math.sin(t1)
        cos_t2, sin_t2 = math.cos(t2), math.sin(t2)

        # Points on the ellipse (pre-rotation)
        p1x = rx * cos_t1
        p1y = ry * sin_t1
        p2x = rx * cos_t2
        p2y = ry * sin_t2

        # Control points (pre-rotation)
        cp1x = p1x - alpha * rx * sin_t1
        cp1y = p1y + alpha * ry * cos_t1
        cp2x = p2x + alpha * rx * sin_t2
        cp2y = p2y - alpha * ry * cos_t2

        # Rotate and translate
        segments.append((
            cx + cp1x * cos_phi - cp1y * sin_phi,
            cy + cp1x * sin_phi + cp1y * cos_phi,
            cx + cp2x * cos_phi - cp2y * sin_phi,
            cy + cp2x * sin_phi + cp2y * cos_phi,
            cx + p2x * cos_phi - p2y * sin_phi,
            cy + p2x * sin_phi + p2y * cos_phi,
        ))

    return segments


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
    # Track last control point for S/s and T/t reflection
    last_cp2x, last_cp2y = 0.0, 0.0  # last cubic cp2
    last_qx, last_qy = 0.0, 0.0      # last quadratic control point
    prev_cmd: str | None = None

    while i < len(tokens):
        t = tokens[i]
        if t.isalpha():
            cmd = t
            i += 1
            if cmd in ("Z", "z"):
                commands.append(("Z",))
                cx, cy = sx, sy
                prev_cmd = cmd
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
                prev_cmd = cmd
                cmd = "L"  # subsequent are lineTo
            elif cmd == "m":
                x, y = float(tokens[i]), float(tokens[i + 1])
                cx, cy = cx + x, cy + y
                commands.append(("M", cx, cy))
                sx, sy = cx, cy
                i += 2
                prev_cmd = cmd
                cmd = "l"
            elif cmd == "L":
                x, y = float(tokens[i]), float(tokens[i + 1])
                commands.append(("L", x, y))
                cx, cy = x, y
                i += 2
                prev_cmd = cmd
            elif cmd == "l":
                x, y = float(tokens[i]), float(tokens[i + 1])
                cx, cy = cx + x, cy + y
                commands.append(("L", cx, cy))
                i += 2
                prev_cmd = cmd
            elif cmd == "H":
                cx = float(tokens[i])
                commands.append(("L", cx, cy))
                i += 1
                prev_cmd = cmd
            elif cmd == "h":
                cx += float(tokens[i])
                commands.append(("L", cx, cy))
                i += 1
                prev_cmd = cmd
            elif cmd == "V":
                cy = float(tokens[i])
                commands.append(("L", cx, cy))
                i += 1
                prev_cmd = cmd
            elif cmd == "v":
                cy += float(tokens[i])
                commands.append(("L", cx, cy))
                i += 1
                prev_cmd = cmd
            elif cmd == "C":
                x1 = float(tokens[i]); y1 = float(tokens[i + 1])
                x2 = float(tokens[i + 2]); y2 = float(tokens[i + 3])
                x = float(tokens[i + 4]); y = float(tokens[i + 5])
                commands.append(("C", x1, y1, x2, y2, x, y))
                last_cp2x, last_cp2y = x2, y2
                cx, cy = x, y
                i += 6
                prev_cmd = cmd
            elif cmd == "c":
                x1 = cx + float(tokens[i]); y1 = cy + float(tokens[i + 1])
                x2 = cx + float(tokens[i + 2]); y2 = cy + float(tokens[i + 3])
                x = cx + float(tokens[i + 4]); y = cy + float(tokens[i + 5])
                commands.append(("C", x1, y1, x2, y2, x, y))
                last_cp2x, last_cp2y = x2, y2
                cx, cy = x, y
                i += 6
                prev_cmd = cmd
            elif cmd == "S":
                x2 = float(tokens[i]); y2 = float(tokens[i + 1])
                x = float(tokens[i + 2]); y = float(tokens[i + 3])
                # Reflect previous cp2 through current point for cp1
                if prev_cmd in ("C", "c", "S", "s"):
                    x1 = 2 * cx - last_cp2x
                    y1 = 2 * cy - last_cp2y
                else:
                    x1, y1 = cx, cy
                commands.append(("C", x1, y1, x2, y2, x, y))
                last_cp2x, last_cp2y = x2, y2
                cx, cy = x, y
                i += 4
                prev_cmd = cmd
            elif cmd == "s":
                x2 = cx + float(tokens[i]); y2 = cy + float(tokens[i + 1])
                x = cx + float(tokens[i + 2]); y = cy + float(tokens[i + 3])
                # Reflect previous cp2 through current point for cp1
                if prev_cmd in ("C", "c", "S", "s"):
                    x1 = 2 * cx - last_cp2x
                    y1 = 2 * cy - last_cp2y
                else:
                    x1, y1 = cx, cy
                commands.append(("C", x1, y1, x2, y2, x, y))
                last_cp2x, last_cp2y = x2, y2
                cx, cy = x, y
                i += 4
                prev_cmd = cmd
            elif cmd == "Q":
                x1 = float(tokens[i]); y1 = float(tokens[i + 1])
                x = float(tokens[i + 2]); y = float(tokens[i + 3])
                last_qx, last_qy = x1, y1
                # Convert quadratic to cubic
                c1x = cx + 2 / 3 * (x1 - cx); c1y = cy + 2 / 3 * (y1 - cy)
                c2x = x + 2 / 3 * (x1 - x); c2y = y + 2 / 3 * (y1 - y)
                commands.append(("C", c1x, c1y, c2x, c2y, x, y))
                cx, cy = x, y
                i += 4
                prev_cmd = cmd
            elif cmd == "q":
                x1 = cx + float(tokens[i]); y1 = cy + float(tokens[i + 1])
                x = cx + float(tokens[i + 2]); y = cy + float(tokens[i + 3])
                last_qx, last_qy = x1, y1
                c1x = cx + 2 / 3 * (x1 - cx); c1y = cy + 2 / 3 * (y1 - cy)
                c2x = x + 2 / 3 * (x1 - x); c2y = y + 2 / 3 * (y1 - y)
                commands.append(("C", c1x, c1y, c2x, c2y, x, y))
                cx, cy = x, y
                i += 4
                prev_cmd = cmd
            elif cmd == "T":
                x = float(tokens[i]); y = float(tokens[i + 1])
                # Reflect previous quadratic control point
                if prev_cmd in ("Q", "q", "T", "t"):
                    x1 = 2 * cx - last_qx
                    y1 = 2 * cy - last_qy
                else:
                    x1, y1 = cx, cy
                last_qx, last_qy = x1, y1
                c1x = cx + 2 / 3 * (x1 - cx); c1y = cy + 2 / 3 * (y1 - cy)
                c2x = x + 2 / 3 * (x1 - x); c2y = y + 2 / 3 * (y1 - y)
                commands.append(("C", c1x, c1y, c2x, c2y, x, y))
                cx, cy = x, y
                i += 2
                prev_cmd = cmd
            elif cmd == "t":
                dx = float(tokens[i]); dy = float(tokens[i + 1])
                x, y = cx + dx, cy + dy
                if prev_cmd in ("Q", "q", "T", "t"):
                    x1 = 2 * cx - last_qx
                    y1 = 2 * cy - last_qy
                else:
                    x1, y1 = cx, cy
                last_qx, last_qy = x1, y1
                c1x = cx + 2 / 3 * (x1 - cx); c1y = cy + 2 / 3 * (y1 - cy)
                c2x = x + 2 / 3 * (x1 - x); c2y = y + 2 / 3 * (y1 - y)
                commands.append(("C", c1x, c1y, c2x, c2y, x, y))
                cx, cy = x, y
                i += 2
                prev_cmd = cmd
            elif cmd == "A":
                rx_a = float(tokens[i]); ry_a = float(tokens[i + 1])
                x_rot = float(tokens[i + 2])
                fa = int(float(tokens[i + 3]))
                fs = int(float(tokens[i + 4]))
                x = float(tokens[i + 5]); y = float(tokens[i + 6])
                _emit_arc_commands(commands, cx, cy, rx_a, ry_a,
                                   x_rot, fa, fs, x, y)
                cx, cy = x, y
                i += 7
                prev_cmd = cmd
            elif cmd == "a":
                rx_a = float(tokens[i]); ry_a = float(tokens[i + 1])
                x_rot = float(tokens[i + 2])
                fa = int(float(tokens[i + 3]))
                fs = int(float(tokens[i + 4]))
                x = cx + float(tokens[i + 5]); y = cy + float(tokens[i + 6])
                _emit_arc_commands(commands, cx, cy, rx_a, ry_a,
                                   x_rot, fa, fs, x, y)
                cx, cy = x, y
                i += 7
                prev_cmd = cmd
            else:
                i += 1  # skip unknown
                prev_cmd = cmd
        except (IndexError, ValueError):
            break

    return commands


def _emit_arc_commands(
    commands: list[tuple],
    x1: float, y1: float,
    rx: float, ry: float,
    x_rot: float, fa: int, fs: int,
    x2: float, y2: float,
) -> None:
    """Konwertuje łuk SVG na komendy C (cubic bezier) i dopisuje do commands."""
    # Degenerate cases
    if abs(x1 - x2) < 1e-10 and abs(y1 - y2) < 1e-10:
        return
    if rx < 1e-10 or ry < 1e-10:
        commands.append(("L", x2, y2))
        return

    center = _arc_endpoint_to_center(x1, y1, rx, ry, x_rot, fa, fs, x2, y2)
    cx_a, cy_a, rx_a, ry_a, start_angle, sweep_angle, cos_phi, sin_phi = center

    beziers = _arc_to_beziers(
        cx_a, cy_a, rx_a, ry_a, start_angle, sweep_angle, cos_phi, sin_phi
    )
    for cp1x, cp1y, cp2x, cp2y, ex, ey in beziers:
        commands.append(("C", cp1x, cp1y, cp2x, cp2y, ex, ey))


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
