"""Testy utylizacji materialu w modelu Sheet."""
from models import Sheet, Placement, Sticker


def _make_sticker(w_mm: float, h_mm: float) -> Sticker:
    return Sticker(source_path="dummy.pdf", width_mm=w_mm, height_mm=h_mm)


def test_empty_sheet_has_zero_utilization():
    sheet = Sheet(width_mm=210, height_mm=297)
    assert sheet.used_area_mm2 == 0.0
    assert sheet.utilization_percent == 0.0
    assert sheet.utilization_of_sheet_percent == 0.0


def test_single_sticker_utilization():
    """1 naklejka 100x100 na arkuszu 200x200: 25% arkusza."""
    sheet = Sheet(width_mm=200, height_mm=200, margins_mm=(0, 0, 0, 0), mark_zone_mm=0)
    sheet.placements = [
        Placement(sticker=_make_sticker(100, 100), x_mm=0, y_mm=0)
    ]
    assert sheet.used_area_mm2 == 10000.0
    assert sheet.sheet_area_mm2 == 40000.0
    assert sheet.utilization_of_sheet_percent == 25.0


def test_rotation_90_swaps_dimensions():
    """Rotacja 90° zmienia wymiary bbox — nie powinno zmieniac area (w×h = h×w)."""
    sheet = Sheet(width_mm=200, height_mm=200, margins_mm=(0, 0, 0, 0), mark_zone_mm=0)
    sheet.placements = [
        Placement(sticker=_make_sticker(80, 50), x_mm=0, y_mm=0, rotation_deg=0),
        Placement(sticker=_make_sticker(80, 50), x_mm=0, y_mm=0, rotation_deg=90),
    ]
    # 80*50 + 80*50 = 8000, niezaleznie od rotacji
    assert sheet.used_area_mm2 == 8000.0


def test_utilization_printable_vs_sheet():
    """utilization_percent > utilization_of_sheet_percent (marginesy wliczone)."""
    sheet = Sheet(width_mm=210, height_mm=297, margins_mm=(10, 10, 10, 10), mark_zone_mm=15)
    sheet.placements = [
        Placement(sticker=_make_sticker(80, 50), x_mm=0, y_mm=0)
    ]
    # Printable area < sheet area
    assert sheet.printable_area_mm2 < sheet.sheet_area_mm2
    assert sheet.utilization_percent > sheet.utilization_of_sheet_percent


def test_utilization_clamped_at_100():
    """Jesli naklejki laczna powierzchnia > printable_area → clamp 100%."""
    sheet = Sheet(width_mm=100, height_mm=100, margins_mm=(0, 0, 0, 0), mark_zone_mm=0)
    sheet.placements = [
        Placement(sticker=_make_sticker(90, 90), x_mm=0, y_mm=0),
        Placement(sticker=_make_sticker(90, 90), x_mm=0, y_mm=0),  # overlap (teoretyczny)
    ]
    # 2 * 90*90 = 16200 > sheet 10000 → clamp
    assert sheet.utilization_of_sheet_percent == 100.0


def test_typical_sticker_sheet_utilization():
    """Realny scenariusz: 8 naklejek 80x50 na A4."""
    sheet = Sheet(width_mm=210, height_mm=297)
    sheet.placements = [
        Placement(sticker=_make_sticker(80, 50), x_mm=0, y_mm=0)
        for _ in range(8)
    ]
    # 8 * 80*50 = 32000 mm². Sheet A4 = 62370 mm² → 51.3%
    assert 50 < sheet.utilization_of_sheet_percent < 55


def test_zero_size_sheet_returns_zero():
    """Edge case: zerowy arkusz nie crashuje (brak division by zero)."""
    sheet = Sheet(width_mm=1, height_mm=0)  # role, dynamic height
    assert sheet.utilization_percent == 0.0
    assert sheet.utilization_of_sheet_percent == 0.0
