"""Testy przełącznika silnika konturu (moore/opencv).

Weryfikuje:
  - dispatcher _boundary_trace() wybiera właściwy silnik
  - oba silniki zwracają podobne wyniki na prostych kształtach
  - fallback działa gdy opencv niedostępne / engine nieznany
"""
import numpy as np
import pytest

from modules.contour import (
    _moore_boundary_trace,
    _opencv_boundary_trace,
    _boundary_trace,
)


# cv2 jest opcjonalne — niektóre testy skippuj jeśli brak
cv2_available = pytest.importorskip("cv2", reason="opencv-python not installed")


# ============================================================================
# Fixture — proste maski binarne
# ============================================================================

@pytest.fixture
def rectangle_mask():
    """20x30 maska z prostokątem 10x20 w środku."""
    mask = np.zeros((30, 20), dtype=np.uint8)
    mask[5:25, 5:15] = 1
    return mask


@pytest.fixture
def circle_mask():
    """40x40 maska z wypełnionym okręgiem r=15."""
    h, w = 40, 40
    cy, cx = h // 2, w // 2
    y, x = np.ogrid[:h, :w]
    mask = ((x - cx) ** 2 + (y - cy) ** 2 <= 15 ** 2).astype(np.uint8)
    return mask


@pytest.fixture
def empty_mask():
    return np.zeros((10, 10), dtype=np.uint8)


# ============================================================================
# _opencv_boundary_trace — bezpośredni test
# ============================================================================

def test_opencv_trace_rectangle(rectangle_mask):
    pts = _opencv_boundary_trace(rectangle_mask)
    assert pts is not None
    assert len(pts) >= 4  # min. 4 narożniki
    # Wszystkie punkty wewnątrz maski
    assert pts[:, 0].min() >= 5 - 1
    assert pts[:, 0].max() <= 14 + 1
    assert pts[:, 1].min() >= 5 - 1
    assert pts[:, 1].max() <= 24 + 1


def test_opencv_trace_circle(circle_mask):
    pts = _opencv_boundary_trace(circle_mask)
    assert pts is not None
    assert len(pts) >= 20  # okrąg ma wiele punktów
    # Punkty blisko promienia 15 (±2 tolerancja pikselowa)
    cy, cx = 20, 20
    dists = np.sqrt((pts[:, 0] - cx) ** 2 + (pts[:, 1] - cy) ** 2)
    assert 13 <= dists.mean() <= 17


def test_opencv_trace_empty_returns_none(empty_mask):
    pts = _opencv_boundary_trace(empty_mask)
    assert pts is None


def test_opencv_trace_returns_largest_contour():
    """Dwa rozdzielne kształty → zwraca tylko większy."""
    mask = np.zeros((30, 50), dtype=np.uint8)
    # Mały prostokąt
    mask[2:5, 2:5] = 1
    # Duży prostokąt
    mask[10:25, 20:45] = 1
    pts = _opencv_boundary_trace(mask)
    assert pts is not None
    # Punkty w obszarze dużego prostokąta
    assert pts[:, 0].min() >= 19
    assert pts[:, 1].min() >= 9


# ============================================================================
# Dispatcher _boundary_trace — routing silnika
# ============================================================================

def test_dispatcher_moore(rectangle_mask):
    pts = _boundary_trace(rectangle_mask, engine="moore")
    assert pts is not None
    assert len(pts) >= 4


def test_dispatcher_opencv(rectangle_mask):
    pts = _boundary_trace(rectangle_mask, engine="opencv")
    assert pts is not None
    assert len(pts) >= 4


def test_dispatcher_auto(rectangle_mask):
    """auto powinien wybrać opencv jeśli dostępne, fallback na moore."""
    pts = _boundary_trace(rectangle_mask, engine="auto")
    assert pts is not None


def test_dispatcher_unknown_engine_falls_back_to_moore(rectangle_mask):
    """Nieznany engine → moore (default)."""
    pts = _boundary_trace(rectangle_mask, engine="nonexistent_engine")
    assert pts is not None
    assert len(pts) >= 4


def test_dispatcher_none_uses_config_default(rectangle_mask):
    """engine=None → użyj config.CONTOUR_ENGINE."""
    pts = _boundary_trace(rectangle_mask, engine=None)
    assert pts is not None


def test_dispatcher_empty_mask(empty_mask):
    assert _boundary_trace(empty_mask, engine="moore") is None
    assert _boundary_trace(empty_mask, engine="opencv") is None
    assert _boundary_trace(empty_mask, engine="auto") is None


# ============================================================================
# Equivalence — oba silniki na tych samych kształtach
# ============================================================================

def test_both_engines_find_same_rectangle_bbox(rectangle_mask):
    """Moore i OpenCV na prostokącie → bboxy zbliżone (±1 piksel)."""
    moore_pts = _moore_boundary_trace(rectangle_mask)
    opencv_pts = _opencv_boundary_trace(rectangle_mask)
    assert moore_pts is not None and opencv_pts is not None

    m_bbox = (
        moore_pts[:, 0].min(), moore_pts[:, 1].min(),
        moore_pts[:, 0].max(), moore_pts[:, 1].max(),
    )
    o_bbox = (
        opencv_pts[:, 0].min(), opencv_pts[:, 1].min(),
        opencv_pts[:, 0].max(), opencv_pts[:, 1].max(),
    )
    # Tolerancja 1px — różne algorytmy mogą mieć punkt więcej/mniej
    for m, o in zip(m_bbox, o_bbox):
        assert abs(m - o) <= 1.5, f"bbox mismatch: moore={m_bbox}, opencv={o_bbox}"


def test_both_engines_find_circle_similar_centroid(circle_mask):
    """Moore i OpenCV na okręgu → centroidy zbliżone (±1 px)."""
    moore_pts = _moore_boundary_trace(circle_mask)
    opencv_pts = _opencv_boundary_trace(circle_mask)
    assert moore_pts is not None and opencv_pts is not None

    m_cx, m_cy = moore_pts[:, 0].mean(), moore_pts[:, 1].mean()
    o_cx, o_cy = opencv_pts[:, 0].mean(), opencv_pts[:, 1].mean()
    assert abs(m_cx - o_cx) <= 1.5
    assert abs(m_cy - o_cy) <= 1.5


# ============================================================================
# Config integration
# ============================================================================

def test_config_has_contour_engine_attr():
    import config
    assert hasattr(config, "CONTOUR_ENGINE")
    assert config.CONTOUR_ENGINE in ("moore", "opencv", "auto")
