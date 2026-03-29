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
from config import DEFAULT_BLEED_MM, MM_TO_PT

log = logging.getLogger(__name__)


# =============================================================================
# ICC COLOR MANAGEMENT
# =============================================================================

# Ścieżki szukania profilu FOGRA39
_ICC_SEARCH_PATHS = [
    # Adobe Creative Cloud / Creative Suite
    "/Library/Application Support/Adobe/Color/Profiles/Recommended/CoatedFOGRA39.icc",
    # Systemowy ColorSync
    "/Library/ColorSync/Profiles/CoatedFOGRA39.icc",
    # User ColorSync
    os.path.expanduser("~/Library/ColorSync/Profiles/CoatedFOGRA39.icc"),
    # Lokalnie w projekcie
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "profiles", "CoatedFOGRA39.icc"),
]


@lru_cache(maxsize=1)
def _find_fogra39_path() -> str | None:
    """Szuka CoatedFOGRA39.icc w znanych lokalizacjach."""
    for path in _ICC_SEARCH_PATHS:
        if os.path.isfile(path):
            return path
    return None


@lru_cache(maxsize=1)
def _get_icc_transform():
    """Tworzy ImageCms transform sRGB→FOGRA39. Cache'owane."""
    fogra39_path = _find_fogra39_path()
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

    # 3. Konwersja RGB → CMYK (ICC FOGRA39)
    edge_cmyk = rgb_to_cmyk(edge_rgb)
    sticker.edge_color_cmyk = edge_cmyk
    log.info(
        f"Kolor krawędzi CMYK: ({edge_cmyk[0]:.3f}, {edge_cmyk[1]:.3f}, "
        f"{edge_cmyk[2]:.3f}, {edge_cmyk[3]:.3f})"
    )

    return sticker
