"""Testy konwersji kolorow (modules/bleed.py)."""
from modules.bleed import rgb_to_cmyk_simple


def test_white_is_no_ink():
    c, m, y, k = rgb_to_cmyk_simple((1.0, 1.0, 1.0))
    assert (c, m, y, k) == (0.0, 0.0, 0.0, 0.0)


def test_black_is_pure_k():
    c, m, y, k = rgb_to_cmyk_simple((0.0, 0.0, 0.0))
    assert k == 1.0
    assert c == m == y == 0.0


def test_pure_red():
    # R=1, G=0, B=0 → k=0, c=0, m=1, y=1
    c, m, y, k = rgb_to_cmyk_simple((1.0, 0.0, 0.0))
    assert k == 0.0
    assert abs(c - 0.0) < 1e-9
    assert abs(m - 1.0) < 1e-9
    assert abs(y - 1.0) < 1e-9


def test_pure_green():
    c, m, y, k = rgb_to_cmyk_simple((0.0, 1.0, 0.0))
    assert k == 0.0
    assert abs(c - 1.0) < 1e-9
    assert abs(m - 0.0) < 1e-9
    assert abs(y - 1.0) < 1e-9


def test_pure_blue():
    c, m, y, k = rgb_to_cmyk_simple((0.0, 0.0, 1.0))
    assert k == 0.0
    assert abs(c - 1.0) < 1e-9
    assert abs(m - 1.0) < 1e-9
    assert abs(y - 0.0) < 1e-9


def test_gray_uses_k_only():
    # 50% gray → k=0.5, c=m=y=0
    c, m, y, k = rgb_to_cmyk_simple((0.5, 0.5, 0.5))
    assert abs(k - 0.5) < 1e-9
    assert c == m == y == 0.0
