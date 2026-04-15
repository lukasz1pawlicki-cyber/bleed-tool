"""Testy spojnosci config.py — stale muszą trzymac sensowne wartosci."""
import config


def test_unit_conversion():
    assert abs(config.MM_TO_PT * config.PT_TO_MM - 1.0) < 1e-9
    # 1 inch = 72pt = 25.4mm
    assert abs(25.4 * config.MM_TO_PT - 72.0) < 1e-9


def test_sheet_presets_have_dimensions():
    for name, (w, h) in config.SHEET_PRESETS.items():
        assert w > 0 and h > 0, f"Invalid sheet {name}: {w}x{h}"


def test_plotters_have_required_keys():
    required_keys = {"mark_type", "mark_size_mm", "min_marks", "mark_offset_mm",
                     "mark_zone_mm"}
    for name, cfg in config.PLOTTERS.items():
        missing = required_keys - set(cfg.keys())
        assert not missing, f"Plotter {name} missing: {missing}"


def test_plotters_have_cut_layers():
    for name in config.PLOTTERS:
        assert "cut_layers" in config.PLOTTERS[name], f"{name}: missing cut_layers"
        layers = config.PLOTTERS[name]["cut_layers"]
        # Kazdy ploter obsluguje te 3 warstwy
        assert "CutContour" in layers
        assert "FlexCut" in layers
        assert "Regmark" in layers


def test_spot_colors_are_strings():
    assert isinstance(config.SPOT_COLOR_CUTCONTOUR, str)
    assert isinstance(config.SPOT_COLOR_FLEXCUT, str)
    assert isinstance(config.SPOT_COLOR_WHITE, str)
    assert isinstance(config.SPOT_COLOR_REGMARK, str)


def test_cmyk_tuples_are_4_floats_in_range():
    # (C, M, Y, K) w [0, 1]
    for cmyk in [config.SPOT_CMYK_CUTCONTOUR, config.SPOT_CMYK_FLEXCUT,
                 config.SPOT_CMYK_WHITE, config.SPOT_CMYK_REGMARK,
                 config.CUT_CMYK_CUTCONTOUR, config.CUT_CMYK_FLEXCUT,
                 config.CUT_CMYK_REGMARK]:
        assert len(cmyk) == 4
        for v in cmyk:
            assert 0 <= v <= 1, f"CMYK out of range: {cmyk}"


def test_snap_constants():
    assert config.SNAP_STEP_MM > 0
    assert 0 < config.SNAP_TOLERANCE_MM < config.SNAP_STEP_MM


def test_default_bleed_positive():
    assert config.DEFAULT_BLEED_MM > 0
    assert config.DEFAULT_DPI >= 150


def test_float_tolerance_small():
    assert 0 < config.FLOAT_TOLERANCE_MM < 0.1
