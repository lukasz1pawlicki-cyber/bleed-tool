"""Testy offset konturu (modules/bleed.offset_segments, offset_polyline).

Obejmują:
  - Offset polyline/segments podstawowy (kwadrat, zero, empty)
  - Miter limit dla ostrych kątów (zamiast spike → cap)
  - _fit_cubic_bezier stability dla degenerowanych danych
"""
import numpy as np

from modules.bleed import (
    DEFAULT_MITER_LIMIT,
    offset_polyline,
    offset_segments,
    _fit_cubic_bezier,
    _linear_bezier_controls,
)


def test_offset_square_polyline():
    """Kwadrat 100x100 w (0,0)-(100,100), offset o 10 → 120x120."""
    square = np.array([
        [0.0, 0.0],
        [100.0, 0.0],
        [100.0, 100.0],
        [0.0, 100.0],
    ])
    offset = offset_polyline(square, 10.0)

    x_min, y_min = offset.min(axis=0)
    x_max, y_max = offset.max(axis=0)

    # Powinien byc offset o 10 na zewnatrz
    assert abs(x_min - (-10)) < 1.0, f"x_min={x_min}"
    assert abs(y_min - (-10)) < 1.0, f"y_min={y_min}"
    assert abs(x_max - 110) < 1.0, f"x_max={x_max}"
    assert abs(y_max - 110) < 1.0, f"y_max={y_max}"


def test_offset_zero_distance():
    """Offset 0 → bez zmian."""
    segments = [
        ('l', (0.0, 0.0), (100.0, 0.0)),
        ('l', (100.0, 0.0), (100.0, 100.0)),
        ('l', (100.0, 100.0), (0.0, 100.0)),
        ('l', (0.0, 100.0), (0.0, 0.0)),
    ]
    result = offset_segments(segments, 0.0)
    assert result == segments


def test_offset_empty():
    """Empty segments → empty list."""
    assert offset_segments([], 10.0) == []


def test_offset_preserves_segment_count():
    """Liczba segmentow po offsecie musi byc ta sama."""
    segments = [
        ('l', (0.0, 0.0), (100.0, 0.0)),
        ('l', (100.0, 0.0), (100.0, 50.0)),
        ('l', (100.0, 50.0), (0.0, 50.0)),
        ('l', (0.0, 50.0), (0.0, 0.0)),
    ]
    result = offset_segments(segments, 5.0)
    assert len(result) == len(segments)
    # Typy segmentow zachowane
    for orig, new in zip(segments, result):
        assert orig[0] == new[0]


# =============================================================================
# MITER LIMIT — zabezpieczenie przed spike'ami na ostrych narożnikach
# =============================================================================

def test_offset_sharp_corner_is_bounded():
    """Bardzo ostry kat (spike, ~10°) NIE moze dac spike'a w offset.

    Bez miter limitu wierzcholek uciekalby na 1/sin(5°) ≈ 11.5× distance.
    Z miter_limit=4.0 wartosc wierzcholka musi miescic sie w
    miter_limit × distance od oryginalnego.
    """
    # Trójkat rownoramienny z bardzo ostrym wierzcholkiem u gory
    polyline = np.array([
        [-100.0, 0.0],
        [100.0, 0.0],
        [0.0, 10.0],   # ostry wierzcholek (~11° rozwarcia)
    ])
    distance = 5.0
    offset = offset_polyline(polyline, distance, miter_limit=4.0)

    # Oryginalny top-vertex → (0, 10). Offset przesuwa go w gore (outward).
    # Sprawdzamy ze nie odbiegl od oryginalu o wiecej niz miter_limit × distance.
    top_orig = polyline[2]
    top_offset = offset[2]
    displacement = np.linalg.norm(top_offset - top_orig)

    max_allowed = 4.0 * distance + 1e-6
    assert displacement <= max_allowed, \
        f"Spike: |offset-orig|={displacement:.2f} > miter_cap={max_allowed:.2f}"


def test_offset_gentle_corner_not_affected():
    """Lagodny kat (90°) ma miter_length = distance × √2 < miter_cap,
    wiec dokladny miter powinien zostac uzyty (bez cappingu)."""
    # Prostokat 100x100 — wszystkie kąty to 90°.
    square = np.array([
        [0.0, 0.0],
        [100.0, 0.0],
        [100.0, 100.0],
        [0.0, 100.0],
    ])
    distance = 5.0
    offset = offset_polyline(square, distance)

    # Kazdy wierzcholek powinien byc przesuniety o distance × √2 po przekątnej
    # (tj. do narożników większego kwadratu).
    expected_corner = np.array([-distance, -distance])
    assert np.allclose(offset[0], expected_corner, atol=1e-3)


def test_offset_default_miter_limit_is_svg_standard():
    """Domyslny miter limit = 4.0 (zgodnie ze standardem SVG stroke-miterlimit)."""
    assert DEFAULT_MITER_LIMIT == 4.0


# =============================================================================
# _fit_cubic_bezier — stabilnosc numeryczna
# =============================================================================

def test_fit_bezier_too_few_points_uses_linear():
    """< 4 próbek → linear fallback (1/3, 2/3 chord)."""
    pts = np.array([[0.0, 0.0], [1.0, 0.0], [2.0, 0.0]])
    p1, p2 = _fit_cubic_bezier(pts)
    lin_p1, lin_p2 = _linear_bezier_controls(pts[0], pts[-1])
    assert np.allclose(p1, lin_p1)
    assert np.allclose(p2, lin_p2)


def test_fit_bezier_degenerate_points_uses_linear():
    """Wszystkie punkty w jednym miejscu → linear fallback (bez NaN)."""
    pts = np.zeros((8, 2))
    p1, p2 = _fit_cubic_bezier(pts)
    assert np.all(np.isfinite(p1))
    assert np.all(np.isfinite(p2))
    # Oczekiwany linear: p1 = p0, p2 = p0 (bo p3 == p0 == 0)
    assert np.allclose(p1, 0.0)
    assert np.allclose(p2, 0.0)


def test_fit_bezier_collinear_points_produces_linear_result():
    """Punkty na linii prostej → control points na tej samej linii
    (nie spike'i odskakujące w bok)."""
    pts = np.array([[float(i), 0.0] for i in range(12)])
    p1, p2 = _fit_cubic_bezier(pts)
    # Punkty kontrolne powinny byc na y ≈ 0 (chord y=0)
    assert abs(p1[1]) < 1e-6
    assert abs(p2[1]) < 1e-6
    # I mniej-wiecej na 1/3 i 2/3 chord
    assert 2.0 < p1[0] < 5.0
    assert 6.0 < p2[0] < 9.0


def test_fit_bezier_smooth_curve_preserves_shape():
    """Dla prawdziwej krzywej (semicircle) fit nie powinien degenerować
    do linear — controlki znacząco oddalone od chord."""
    theta = np.linspace(0, np.pi, 30)
    r = 10.0
    pts = np.column_stack([r * np.cos(theta), r * np.sin(theta)])
    p1, p2 = _fit_cubic_bezier(pts)
    # Punkty kontrolne cubic Bezier semicircle powinny byc w pobliżu
    # (±r, r*k) gdzie k ≈ 4(√2 − 1)/3 ≈ 0.5523 (klasyczna aproksymacja).
    assert p1[1] > r * 0.3, f"p1 zbyt blisko chord: {p1}"
    assert p2[1] > r * 0.3, f"p2 zbyt blisko chord: {p2}"
