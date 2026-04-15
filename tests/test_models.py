"""Testy dataclass Sticker/Placement/Sheet (models.py)."""
from models import Sticker, Placement, Sheet, Mark, PanelLine


def test_sticker_defaults():
    s = Sticker(source_path="/tmp/a.pdf")
    assert s.source_path == "/tmp/a.pdf"
    assert s.page_index == 0
    assert s.width_mm == 0.0
    assert s.height_mm == 0.0
    assert s.cut_segments == []
    # Dataclass default_factory — kazda instancja wlasna lista
    s2 = Sticker(source_path="/tmp/b.pdf")
    s.cut_segments.append("x")
    assert s2.cut_segments == []


def test_placement_basic():
    s = Sticker(source_path="/tmp/a.pdf", width_mm=50, height_mm=30)
    p = Placement(sticker=s, x_mm=10, y_mm=20)
    assert p.sticker is s
    assert p.x_mm == 10
    assert p.y_mm == 20
    assert p.rotation_deg == 0.0


def test_sheet_basic():
    sheet = Sheet(width_mm=320, height_mm=450)
    assert sheet.width_mm == 320
    assert sheet.height_mm == 450
    assert sheet.placements == []
    assert sheet.marks == []


def test_mark_dataclass():
    m = Mark(x_mm=10, y_mm=20, width_mm=3, height_mm=3, mark_type="opos_rectangle")
    assert m.x_mm == 10
    assert m.is_bar is False  # default


def test_panel_line_defaults():
    pl = PanelLine(axis='horizontal', position_mm=100.0)
    assert pl.axis == 'horizontal'
    assert pl.position_mm == 100.0


def test_panel_line_invalid_axis():
    import pytest
    with pytest.raises(ValueError):
        PanelLine(axis='h', position_mm=100.0)
