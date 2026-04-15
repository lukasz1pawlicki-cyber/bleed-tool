"""Testy geometrii konturu: circle fit, Moore boundary tracing, circle→Bezier."""
import numpy as np
import pytest

from modules.contour import (
    _fit_circle,
    _is_circular,
    _circle_to_bezier_segments,
    _moore_boundary_trace,
)


# ============================================================================
# _fit_circle — least-squares circle fitting
# ============================================================================

def test_fit_circle_perfect():
    """Punkty na dokladnym okregu → zwracany srodek i promien ok."""
    cx_true, cy_true, r_true = 50.0, 30.0, 20.0
    angles = np.linspace(0, 2 * np.pi, 32, endpoint=False)
    points = np.column_stack([
        cx_true + r_true * np.cos(angles),
        cy_true + r_true * np.sin(angles),
    ])
    result = _fit_circle(points)
    assert result is not None
    cx, cy, r = result
    assert abs(cx - cx_true) < 1e-6
    assert abs(cy - cy_true) < 1e-6
    assert abs(r - r_true) < 1e-6


def test_fit_circle_with_noise():
    """Punkty z szumem → dopasowanie blisko prawdziwych wartosci."""
    np.random.seed(42)
    cx_true, cy_true, r_true = 10.0, 10.0, 5.0
    angles = np.linspace(0, 2 * np.pi, 100, endpoint=False)
    noise = np.random.normal(0, 0.01, size=(100, 2))
    points = np.column_stack([
        cx_true + r_true * np.cos(angles),
        cy_true + r_true * np.sin(angles),
    ]) + noise
    result = _fit_circle(points)
    assert result is not None
    cx, cy, r = result
    assert abs(cx - cx_true) < 0.1
    assert abs(cy - cy_true) < 0.1
    assert abs(r - r_true) < 0.1


def test_fit_circle_too_few_points():
    """<3 punkty → None."""
    assert _fit_circle(np.array([[0, 0]])) is None
    assert _fit_circle(np.array([[0, 0], [1, 1]])) is None


def test_fit_circle_collinear_points():
    """Punkty wspolliniowe — fit nie powinien sie wywalic (None lub wynik)."""
    points = np.array([[0, 0], [1, 0], [2, 0], [3, 0]], dtype=float)
    # Tylko: bez exception. Wynik moze byc None lub degenerowany.
    result = _fit_circle(points)
    assert result is None or len(result) == 3


# ============================================================================
# _is_circular — klasyfikacja ksztaltu
# ============================================================================

def test_is_circular_perfect_circle():
    angles = np.linspace(0, 2 * np.pi, 40, endpoint=False)
    points = np.column_stack([50 + 10 * np.cos(angles),
                              50 + 10 * np.sin(angles)])
    assert _is_circular(points, 50.0, 50.0, 10.0, tolerance=0.05)


def test_is_circular_square_is_not():
    """Kwadrat z punktami na krawedziach NIE jest okragly.

    4 rogi same w sobie leza na okregu opisanym — dlatego dodajemy tez
    punkty srodkowe krawedzi (blizsze srodka).
    """
    points = np.array([
        [0, 0], [50, 0], [100, 0],       # dolna krawedz
        [100, 50], [100, 100],            # prawa krawedz
        [50, 100], [0, 100],              # gorna krawedz
        [0, 50],                          # lewa krawedz
    ], dtype=float)
    fit = _fit_circle(points)
    assert fit is not None
    cx, cy, r = fit
    # Naroznik: odl ~70.7, punkt srodkowy: odl ~50 — duze odchylenie
    assert not _is_circular(points, cx, cy, r, tolerance=0.05)


# ============================================================================
# _circle_to_bezier_segments — aproksymacja okregu 4 krzywymi Bezier
# ============================================================================

def test_circle_to_bezier_count():
    """Okrag zawsze 4 segmenty Bezier."""
    segs = _circle_to_bezier_segments(50.0, 50.0, 20.0)
    assert len(segs) == 4
    for seg in segs:
        assert seg[0] == 'c'  # cubic Bezier


def test_circle_to_bezier_closed():
    """Pierwszy p0 = ostatni p3 (domkniety kontur)."""
    segs = _circle_to_bezier_segments(0.0, 0.0, 10.0)
    first_p0 = segs[0][1]
    last_p3 = segs[-1][4]
    assert np.allclose(first_p0, last_p3)


def test_circle_to_bezier_kappa():
    """Kontrola: odleglosc miedzy kolejnymi on-curve = r, k ≈ 0.5523."""
    cx, cy, r = 0.0, 0.0, 10.0
    segs = _circle_to_bezier_segments(cx, cy, r)
    # Pierwszy segment: (r,0) → (0,r), control points (r, kr) i (kr, r)
    p0 = segs[0][1]
    cp1 = segs[0][2]
    # Odleglosc od (r,0) do (r, kr) = kr ≈ 5.5228
    dist = np.linalg.norm(cp1 - p0)
    assert abs(dist - 5.5228) < 0.01


def test_circle_to_bezier_points_on_circle():
    """Wszystkie on-curve punkty leza na okregu o promieniu r."""
    cx, cy, r = 5.0, 7.0, 3.0
    segs = _circle_to_bezier_segments(cx, cy, r)
    for seg in segs:
        p0, p3 = seg[1], seg[4]
        for p in (p0, p3):
            d = np.sqrt((p[0] - cx) ** 2 + (p[1] - cy) ** 2)
            assert abs(d - r) < 1e-6, f"Punkt {p} nie na okregu r={r}"


# ============================================================================
# _moore_boundary_trace — sledzenie konturu maski binarnej
# ============================================================================

def test_moore_empty_mask_returns_none():
    """Pusta maska → None."""
    mask = np.zeros((10, 10), dtype=bool)
    assert _moore_boundary_trace(mask) is None


def test_moore_single_pixel_too_small():
    """Pojedynczy piksel — brak konturu (< 3 punkty)."""
    mask = np.zeros((10, 10), dtype=bool)
    mask[5, 5] = True
    result = _moore_boundary_trace(mask)
    assert result is None


def test_moore_rectangle_closed_contour():
    """Prostokat 5x5 → kontur z 4 rogami i krawedziami."""
    mask = np.zeros((20, 20), dtype=bool)
    mask[5:10, 5:10] = True  # prostokat 5x5 od (5,5) do (9,9)
    result = _moore_boundary_trace(mask)
    assert result is not None
    # Kontur powinien zawierac wszystkie 4 rogi
    xs = result[:, 0]
    ys = result[:, 1]
    assert xs.min() == 5
    assert xs.max() == 9
    assert ys.min() == 5
    assert ys.max() == 9


def test_moore_filled_circle_roughly_round():
    """Wypelnione kolo → kontur obwiedni, punkty blisko okregu."""
    mask = np.zeros((50, 50), dtype=bool)
    # Rysuj kolo r=15 wokol (25, 25)
    yy, xx = np.ogrid[:50, :50]
    mask[(xx - 25) ** 2 + (yy - 25) ** 2 <= 15 ** 2] = True
    result = _moore_boundary_trace(mask)
    assert result is not None
    # Punkty graniczne ~ na promieniu 15 od srodka
    dists = np.sqrt((result[:, 0] - 25) ** 2 + (result[:, 1] - 25) ** 2)
    # Moore chodzi po pikselach granicznych (wewnetrznych foreground),
    # wiec promien bedzie ~14-15
    assert 12 <= dists.mean() <= 16


def test_moore_returns_ordered_points():
    """Kolejne punkty konturu sa sasiadami (8-connected)."""
    mask = np.zeros((20, 20), dtype=bool)
    mask[5:15, 5:15] = True
    result = _moore_boundary_trace(mask)
    assert result is not None
    # Sprawdz kilka par sasiednich punktow
    for i in range(min(10, len(result) - 1)):
        dx = abs(result[i + 1, 0] - result[i, 0])
        dy = abs(result[i + 1, 1] - result[i, 1])
        assert dx <= 1 and dy <= 1, f"Punkty {i},{i+1} nie sa sasiadami"
