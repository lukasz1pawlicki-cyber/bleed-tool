"""Testy offset konturu (modules/bleed.offset_segments, offset_polyline)."""
import numpy as np

from modules.bleed import offset_polyline, offset_segments


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
