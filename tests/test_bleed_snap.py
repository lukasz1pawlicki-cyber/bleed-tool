"""Testy snap wymiarow (modules/bleed._snap_value_mm)."""
from modules.bleed import _snap_value_mm
from config import SNAP_STEP_MM, SNAP_TOLERANCE_MM


def test_snap_step_and_tolerance_from_config():
    # Sanity check — kod uzywa wartosci z config.py
    assert SNAP_STEP_MM == 0.5
    assert SNAP_TOLERANCE_MM == 0.05


def test_snap_exact_step_unchanged():
    # Wartosc dokladnie na siatce
    assert _snap_value_mm(100.0) == 100.0
    assert _snap_value_mm(50.5) == 50.5


def test_snap_within_tolerance():
    # W tolerancji → dociaganie
    assert _snap_value_mm(169.97) == 170.0  # diff=0.03 <= 0.05
    assert _snap_value_mm(40.01) == 40.0    # diff=0.01 <= 0.05
    assert _snap_value_mm(39.96) == 40.0    # diff=0.04 <= 0.05


def test_snap_outside_tolerance_unchanged():
    # Poza tolerancja → bez zmian
    assert _snap_value_mm(35.30) == 35.30   # diff=0.20 > 0.05
    assert _snap_value_mm(100.1) == 100.1   # diff=0.1 > 0.05


def test_snap_negative_not_expected_but_safe():
    # Wartosci ujemne — nie wystepuja w praktyce, ale nie powinny sie wywalic
    result = _snap_value_mm(-0.02)
    assert abs(result - 0.0) < 1e-9
