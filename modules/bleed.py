"""
Sticker Toolkit — bleed.py
============================
Generowanie bleed (offset konturu) i ekstrakcja koloru krawędzi.

Pipeline:
  Sticker (z contour.py) → generate_bleed() → Sticker wzbogacony o:
    - bleed_segments (offset konturu)
    - edge_color_rgb + edge_color_cmyk

Konwersja RGB→CMYK:
  - Preferuje ICC FOGRA39 (CoatedFOGRA39.icc) przez Pillow ImageCms
  - Fallback: prosta formuła matematyczna (UCR)
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache

import numpy as np

from models import Sticker
from config import DEFAULT_BLEED_MM, MM_TO_PT, PT_TO_MM

log = logging.getLogger(__name__)


# =============================================================================
# ICC COLOR MANAGEMENT
# =============================================================================

# Ścieżki szukania profilu FOGRA39
from config import ICC_SEARCH_PATHS as _ICC_SEARCH_PATHS


def _find_fogra39_path() -> str | None:
    """Szuka CoatedFOGRA39.icc w znanych lokalizacjach."""
    for path in _ICC_SEARCH_PATHS:
        if os.path.isfile(path):
            return path
    return None


_icc_cache: dict = {"transform": None, "mtime": 0.0, "path": None}


def _get_icc_transform():
    """Tworzy ImageCms transform sRGB→FOGRA39. Cache z invalidacją po mtime pliku."""
    fogra39_path = _find_fogra39_path()
    if fogra39_path is None:
        return _icc_cache.get("transform")

    try:
        mtime = os.path.getmtime(fogra39_path)
    except OSError:
        mtime = 0.0

    if (_icc_cache["transform"] is not None
            and _icc_cache["path"] == fogra39_path
            and _icc_cache["mtime"] == mtime):
        return _icc_cache["transform"]

    fogra39_path = fogra39_path
    if fogra39_path is None:
        return None

    try:
        from PIL import ImageCms

        srgb_profile = ImageCms.createProfile("sRGB")
        fogra39_profile = ImageCms.getOpenProfile(fogra39_path)

        transform = ImageCms.buildTransform(
            srgb_profile,
            fogra39_profile,
            "RGB",
            "CMYK",
            renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
        )
        _icc_cache["transform"] = transform
        _icc_cache["mtime"] = mtime
        _icc_cache["path"] = fogra39_path
        log.info(f"ICC transform sRGB→FOGRA39 załadowany z: {fogra39_path}")
        return transform
    except Exception as e:
        log.warning(f"Nie udało się załadować ICC transform: {e}")
        return None


def rgb_to_cmyk_icc(rgb: tuple[float, float, float]) -> tuple[float, float, float, float]:
    """Konwersja RGB (0-1) → CMYK (0-1) przez ICC FOGRA39.

    Używa Pillow ImageCms z profilem CoatedFOGRA39.icc.
    """
    transform = _get_icc_transform()
    if transform is None:
        return rgb_to_cmyk_simple(rgb)

    from PIL import Image, ImageCms

    # ImageCms operuje na pikselach (0-255)
    r8 = int(round(rgb[0] * 255))
    g8 = int(round(rgb[1] * 255))
    b8 = int(round(rgb[2] * 255))

    # 1-pikselowy obraz
    img_rgb = Image.new("RGB", (1, 1), (r8, g8, b8))
    img_cmyk = ImageCms.applyTransform(img_rgb, transform)
    c8, m8, y8, k8 = img_cmyk.getpixel((0, 0))

    return (c8 / 255.0, m8 / 255.0, y8 / 255.0, k8 / 255.0)


def rgb_to_cmyk_simple(rgb: tuple[float, float, float]) -> tuple[float, float, float, float]:
    """Konwersja RGB (0-1) → CMYK (0-1) — prosta formuła matematyczna (fallback)."""
    r, g, b = rgb
    k = 1.0 - max(r, g, b)
    if k < 1.0:
        c = (1.0 - r - k) / (1.0 - k)
        m = (1.0 - g - k) / (1.0 - k)
        y = (1.0 - b - k) / (1.0 - k)
    else:
        c = m = y = 0.0
    return (c, m, y, k)


def rgb_to_cmyk(rgb: tuple[float, float, float]) -> tuple[float, float, float, float]:
    """Konwersja RGB (0-1) → CMYK (0-1). Preferuje ICC FOGRA39, fallback na prostą formułę."""
    return rgb_to_cmyk_icc(rgb)


# =============================================================================
# KOLOR KRAWĘDZI
# =============================================================================

def extract_native_cmyk(doc, page) -> tuple[float, float, float, float] | None:
    """Wyciąga kolor CMYK fill krawędzi z content stream strony PDF.

    Szuka pierwszego nie-białego (0,0,0,0) koloru CMYK fill.
    Zwraca (c, m, y, k) w zakresie 0-1 lub None jeśli brak CMYK.
    Używane dla plików CMYK aby uniknąć podwójnej konwersji CMYK→RGB→CMYK.
    """
    import re
    try:
        contents = bytearray()
        for xref in page.get_contents():
            contents += doc.xref_stream(xref)
        cs = contents.decode('latin-1', errors='replace')
        # Szukaj operatorów k (CMYK fill) — weź pierwszy nie-biały
        for m in re.finditer(
            r'([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+k\b', cs
        ):
            c, mm, y, kk = float(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4))
            # Pomiń biały (0,0,0,0) — to zazwyczaj tło
            if c + mm + y + kk > 0.01:
                return (c, mm, y, kk)
    except Exception:
        pass
    return None


def extract_edge_color(drawing: dict) -> tuple[float, float, float]:
    """Zwraca kolor fill zewnętrznego drawingu jako (r, g, b) w zakresie 0-1.

    Fallback: kolor stroke jeśli brak fill, biały jeśli brak obu.
    """
    fill = drawing.get('fill')
    if fill is not None:
        if len(fill) == 3:
            return tuple(fill)
        return tuple(fill[:3])

    # Fallback: kolor stroke
    stroke = drawing.get('color')
    if stroke is not None:
        log.warning("Zewnętrzna ścieżka nie ma fill — używam koloru stroke")
        if len(stroke) == 3:
            return tuple(stroke)
        return tuple(stroke[:3])

    # Ostateczny fallback: biały
    log.warning("Zewnętrzna ścieżka nie ma fill ani stroke — używam białego")
    return (1.0, 1.0, 1.0)


# =============================================================================
# SPŁASZCZANIE SEGMENTÓW → POLILINIA
# =============================================================================

def flatten_segments_to_polyline(
    segments: list, segments_per_curve: int = 30
) -> tuple[np.ndarray, list[int]]:
    """Konwertuje segmenty na gęstą polilinię.

    Zachowuje informację o granicach segmentów (indeksy w polyline
    gdzie zaczyna się nowy segment).

    Returns:
        (polyline, segment_boundaries) — polyline to ndarray (N, 2),
        segment_boundaries to lista indeksów.
    """
    points: list = []
    segment_boundaries: list[int] = [0]

    for seg in segments:
        if seg[0] == 'l':
            start, end = seg[1], seg[2]
            if not points:
                points.append(start)
            points.append(end)
            segment_boundaries.append(len(points) - 1)

        elif seg[0] == 'c':
            p0, p1, p2, p3 = seg[1], seg[2], seg[3], seg[4]
            if not points:
                points.append(p0)
            t = np.linspace(0, 1, segments_per_curve + 1)[1:]
            t = t.reshape(-1, 1)
            pts = (
                ((1 - t) ** 3) * p0
                + 3 * ((1 - t) ** 2) * t * p1
                + 3 * (1 - t) * (t ** 2) * p2
                + (t ** 3) * p3
            )
            points.extend(pts.tolist())
            segment_boundaries.append(len(points) - 1)

    polyline = np.array(points)

    # Usuń duplikat zamknięcia
    if len(polyline) > 1 and np.allclose(polyline[0], polyline[-1], atol=0.5):
        polyline = polyline[:-1]
        if segment_boundaries[-1] >= len(polyline):
            segment_boundaries[-1] = 0

    return polyline, segment_boundaries


# =============================================================================
# OFFSET POLILINII
# =============================================================================

def offset_polyline(polyline: np.ndarray, distance: float) -> np.ndarray:
    """Offset polilinii na zewnątrz o distance (w pt). Normal-based per-vertex."""
    n = len(polyline)
    normals = np.zeros_like(polyline, dtype=np.float64)

    for i in range(n):
        prev_pt = polyline[(i - 1) % n]
        next_pt = polyline[(i + 1) % n]
        tangent = next_pt - prev_pt
        normal = np.array([-tangent[1], tangent[0]])
        length = np.linalg.norm(normal)
        if length > 1e-8:
            normal /= length
        normals[i] = normal

    # Upewnij się że offset jest na zewnątrz (od centroidu)
    # Shoelace formula: wyznacz kierunek nawinięcia (CW/CCW) polygonu
    # signed_area > 0 → CCW, signed_area < 0 → CW
    x = polyline[:, 0]
    y = polyline[:, 1]
    x_next = np.roll(x, -1)
    y_next = np.roll(y, -1)
    signed_area = np.sum(x * y_next - x_next * y) / 2.0

    # Dla CCW (signed_area > 0): normalne [-dy, dx] wskazują na zewnątrz
    # Dla CW (signed_area < 0): normalne [-dy, dx] wskazują do wewnątrz → trzeba odwrócić
    # Weryfikacja: dot product kilku normalnych z wektorem od centroidu
    centroid = polyline.mean(axis=0)
    test_count = min(10, n)
    test_indices = np.linspace(0, n - 1, test_count, dtype=int)
    dot_sum = 0.0
    for idx in test_indices:
        test_vec = polyline[idx] - centroid
        dot_sum += np.dot(test_vec, normals[idx])

    if dot_sum < 0:
        normals = -normals

    return polyline + normals * distance


# =============================================================================
# FIT CUBIC BÉZIER
# =============================================================================

def _fit_cubic_bezier(points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Fituje cubic Bézier (p1, p2 control points) do zestawu punktów.

    p0 i p3 to pierwszy i ostatni punkt. Parametryzacja chord-length.
    Rozwiązanie least-squares.
    """
    n = len(points)
    p0 = points[0]
    p3 = points[-1]

    # Parametryzacja chord-length
    dists = np.linalg.norm(np.diff(points, axis=0), axis=1)
    total = dists.sum()
    if total < 1e-10:
        return p0.copy(), p3.copy()

    t = np.zeros(n)
    t[1:] = np.cumsum(dists) / total

    # Bernstein basis B1(t) = 3(1-t)²t, B2(t) = 3(1-t)t²
    b1 = 3 * (1 - t) ** 2 * t
    b2 = 3 * (1 - t) * t ** 2
    b0 = (1 - t) ** 3
    b3 = t ** 3

    # Target: points - b0*p0 - b3*p3 = b1*p1 + b2*p2
    rhs = points - np.outer(b0, p0) - np.outer(b3, p3)

    # Least squares: [b1, b2] @ [p1; p2] = rhs
    A = np.column_stack([b1, b2])
    p1 = np.zeros(2)
    p2 = np.zeros(2)
    for d in range(2):
        result = np.linalg.lstsq(A, rhs[:, d], rcond=None)
        p1[d] = result[0][0]
        p2[d] = result[0][1]

    return p1, p2


# =============================================================================
# OFFSET SEGMENTÓW ŚCIEŻKI
# =============================================================================

def offset_segments(
    segments: list, distance: float, segments_per_curve: int = 30
) -> list:
    """Offsetuje segmenty ścieżki na zewnątrz o distance (w pt).

    Zachowuje typ operacji (linia/krzywa):
      1. Spłaszcza do polilinii
      2. Offsetuje polilinię (normal-based per-vertex)
      3. Odtwarza segmenty z offset polilinii:
         - Linie → linie (start/end z offset polilinii)
         - Krzywe → least-squares Bézier refit
    """
    if not segments:
        return []
    if distance <= 0:
        return list(segments)  # no offset needed

    polyline, boundaries = flatten_segments_to_polyline(segments, segments_per_curve)
    offset_poly = offset_polyline(polyline, distance)

    result_segments = []
    for seg_idx, seg in enumerate(segments):
        start_boundary = boundaries[seg_idx]
        end_boundary = (
            boundaries[seg_idx + 1] if seg_idx + 1 < len(boundaries) else boundaries[0]
        )

        if seg[0] == 'l':
            result_segments.append((
                'l',
                offset_poly[start_boundary].copy(),
                offset_poly[end_boundary].copy(),
            ))

        elif seg[0] == 'c':
            if end_boundary > start_boundary:
                seg_pts = offset_poly[start_boundary:end_boundary + 1]
            else:
                # Segment przechodzi przez koniec tablicy
                seg_pts = np.vstack([
                    offset_poly[start_boundary:],
                    offset_poly[:end_boundary + 1],
                ])

            if len(seg_pts) >= 4:
                p0 = seg_pts[0]
                p3 = seg_pts[-1]
                p1, p2 = _fit_cubic_bezier(seg_pts)
                result_segments.append(('c', p0, p1, p2, p3))
            else:
                result_segments.append((
                    'l', seg_pts[0].copy(), seg_pts[-1].copy()
                ))

    return result_segments


# =============================================================================
# GŁÓWNA FUNKCJA: generate_bleed
# =============================================================================

def generate_bleed(sticker: Sticker, bleed_mm: float = DEFAULT_BLEED_MM) -> Sticker:
    """Generuje bleed dla Stickera — offset konturu + kolor krawędzi.

    Wzbogaca Sticker o:
      - bleed_segments: offset segmenty konturu
      - edge_color_rgb: kolor krawędzi (r, g, b) 0-1
      - edge_color_cmyk: kolor krawędzi (c, m, y, k) 0-1 (ICC FOGRA39)

    Args:
        sticker: Sticker z wypełnionymi polami konturu (z contour.py)
        bleed_mm: wielkość bleed w mm

    Returns:
        Ten sam Sticker z wypełnionymi polami bleed.
    """
    if bleed_mm < 0:
        raise ValueError(f"bleed_mm musi byc >= 0, podano {bleed_mm}")
    if not sticker.cut_segments:
        raise ValueError("Sticker nie ma cut_segments — uruchom detect_contour() najpierw")

    bleed_pts = bleed_mm * MM_TO_PT
    log.info(f"Bleed: {bleed_mm}mm = {bleed_pts:.2f}pt")

    # 1. Offset segmentów konturu na bleed
    sticker.bleed_segments = offset_segments(sticker.cut_segments, bleed_pts)
    log.info(f"Offset segmentów: {len(sticker.bleed_segments)} segmentów bleed")

    # 2. Kolor krawędzi
    if sticker.edge_color_rgb is not None:
        # Kolor krawędzi już wykryty w detect_contour (raster lub raster-only PDF)
        edge_rgb = sticker.edge_color_rgb
        log.info(f"Kolor krawędzi RGB (pre-set): ({edge_rgb[0]:.3f}, {edge_rgb[1]:.3f}, {edge_rgb[2]:.3f})")
    elif sticker.pdf_doc is not None and sticker.outermost_drawing_idx is not None:
        page = sticker.pdf_doc[sticker.page_index]
        drawings = page.get_drawings()
        outermost_drawing = drawings[sticker.outermost_drawing_idx]

        edge_rgb = extract_edge_color(outermost_drawing)
        sticker.edge_color_rgb = edge_rgb
        log.info(f"Kolor krawędzi RGB: ({edge_rgb[0]:.3f}, {edge_rgb[1]:.3f}, {edge_rgb[2]:.3f})")
    else:
        # Fallback — biały
        edge_rgb = (1.0, 1.0, 1.0)
        sticker.edge_color_rgb = edge_rgb
        log.warning(f"Brak źródła koloru krawędzi, fallback biały: {sticker.source_path}")

    # 3. Kolor CMYK — natywny z content stream (CMYK PDF) lub konwersja RGB→CMYK
    if sticker.is_cmyk and sticker.pdf_doc is not None:
        native_cmyk = extract_native_cmyk(sticker.pdf_doc, sticker.pdf_doc[sticker.page_index])
        if native_cmyk is not None:
            edge_cmyk = native_cmyk
            log.info(
                f"Kolor krawędzi CMYK (natywny): ({edge_cmyk[0]:.3f}, {edge_cmyk[1]:.3f}, "
                f"{edge_cmyk[2]:.3f}, {edge_cmyk[3]:.3f})"
            )
        else:
            edge_cmyk = rgb_to_cmyk(edge_rgb)
            log.info(
                f"Kolor krawędzi CMYK (konwersja): ({edge_cmyk[0]:.3f}, {edge_cmyk[1]:.3f}, "
                f"{edge_cmyk[2]:.3f}, {edge_cmyk[3]:.3f})"
            )
    else:
        edge_cmyk = rgb_to_cmyk(edge_rgb)
        log.info(
            f"Kolor krawędzi CMYK (konwersja): ({edge_cmyk[0]:.3f}, {edge_cmyk[1]:.3f}, "
            f"{edge_cmyk[2]:.3f}, {edge_cmyk[3]:.3f})"
        )
    sticker.edge_color_cmyk = edge_cmyk

    # 4. Snap wymiarów do okrągłych wartości (eliminuje białe gap-y na arkuszu)
    _snap_sticker_dimensions(sticker, bleed_mm)

    return sticker


# =============================================================================
# SNAP WYMIARÓW — dociąganie do pełnych rozmiarów
# =============================================================================

_SNAP_STEP_MM = 0.5       # Siatka zaokrąglenia: 0.5mm
_SNAP_TOLERANCE_MM = 0.05  # Max odchylenie żeby dociągnąć


def _snap_value_mm(value_mm: float) -> float:
    """Dociąga wymiar do najbliższej wielokrotności _SNAP_STEP_MM.

    Jeśli różnica <= _SNAP_TOLERANCE_MM, zwraca zaokrągloną wartość.
    W przeciwnym razie zwraca oryginalną.

    Przykłady (step=0.5, tol=0.05):
      169.97 → 170.0  (diff=0.03 ≤ 0.05)
      40.01  → 40.0   (diff=0.01 ≤ 0.05)
      39.96  → 40.0   (diff=0.04 ≤ 0.05)
      35.30  → 35.30  (diff=0.20 > 0.05, bez zmiany)
    """
    rounded = round(value_mm / _SNAP_STEP_MM) * _SNAP_STEP_MM
    if abs(value_mm - rounded) <= _SNAP_TOLERANCE_MM:
        return rounded
    return value_mm


def _scale_segments(segments: list, sx: float, sy: float) -> list:
    """Skaluje współrzędne segmentów (pt) przez (sx, sy)."""
    if sx == 1.0 and sy == 1.0:
        return segments
    out = []
    for seg in segments:
        kind = seg[0]
        if kind == 'l':
            _, p0, p1 = seg
            out.append(('l',
                        (p0[0] * sx, p0[1] * sy),
                        (p1[0] * sx, p1[1] * sy)))
        elif kind == 'c':
            _, p0, cp1, cp2, p3 = seg
            out.append(('c',
                        (p0[0] * sx, p0[1] * sy),
                        (cp1[0] * sx, cp1[1] * sy),
                        (cp2[0] * sx, cp2[1] * sy),
                        (p3[0] * sx, p3[1] * sy)))
        else:
            out.append(seg)
    return out


def _snap_sticker_dimensions(sticker: Sticker, bleed_mm: float) -> None:
    """Dociąga wymiary naklejki do okrągłych wartości (w miejscu).

    Skaluje minimalnie (< 0.05mm) wymiary i segmenty konturu,
    żeby naklejki na arkuszu miały identyczne rozmiary — bez
    białych gap-ów z powodu niedokładności pt↔mm.

    Operuje na PEŁNYM rozmiarze (grafika + bleed):
      total_mm = width_mm + 2×bleed_mm → snap → nowa width_mm
    """
    bleed2 = 2 * bleed_mm
    total_w = sticker.width_mm + bleed2
    total_h = sticker.height_mm + bleed2

    snapped_w = _snap_value_mm(total_w)
    snapped_h = _snap_value_mm(total_h)

    if snapped_w == total_w and snapped_h == total_h:
        return  # Nic do zmiany

    # Współczynniki skalowania
    sx = snapped_w / total_w if total_w > 0 else 1.0
    sy = snapped_h / total_h if total_h > 0 else 1.0

    old_w = sticker.width_mm
    old_h = sticker.height_mm
    new_w = snapped_w - bleed2
    new_h = snapped_h - bleed2

    # Aktualizacja wymiarów
    sticker.width_mm = new_w
    sticker.height_mm = new_h
    sticker.page_width_pt = new_w * MM_TO_PT
    sticker.page_height_pt = new_h * MM_TO_PT

    # Skaluj segmenty konturu
    sticker.cut_segments = _scale_segments(sticker.cut_segments, sx, sy)
    if sticker.bleed_segments:
        sticker.bleed_segments = _scale_segments(sticker.bleed_segments, sx, sy)

    log.info(
        f"Snap wymiarów: {old_w:.4f}×{old_h:.4f}mm → {new_w:.4f}×{new_h:.4f}mm "
        f"(scale {sx:.6f}×{sy:.6f})"
    )
