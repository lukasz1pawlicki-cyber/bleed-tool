"""Testy preflight_gate — blokada eksportu na podstawie severity."""
from __future__ import annotations

import os
import pytest

from modules.preflight import preflight_gate, preflight_summary
from tests.fixtures import make_rectangle_vector


def test_gate_accepts_ok_file(tmp_path):
    """Plik bez issues = can_export=True."""
    pdf = make_rectangle_vector(tmp_path, w_mm=80, h_mm=50)
    can_export, result = preflight_gate(pdf)
    assert can_export is True
    assert result["status"] in ("ok", "warning")


def test_gate_blocks_missing_file(tmp_path):
    """Nieistniejacy plik = can_export=False (severity=error)."""
    can_export, result = preflight_gate(str(tmp_path / "nope.pdf"))
    assert can_export is False
    assert result["status"] == "error"


def test_gate_lenient_allows_warnings(tmp_path):
    """lenient (default): status=warning nie blokuje."""
    pdf = make_rectangle_vector(tmp_path, w_mm=80, h_mm=50)
    can_export, result = preflight_gate(pdf, strict=False)
    # Nawet jesli sa warningi, lenient pozwala
    assert can_export is True


def test_gate_strict_blocks_warnings(tmp_path):
    """strict: status=warning blokuje (nie tylko error)."""
    # Robimy plik ktory bedzie mial co najmniej jeden warning.
    # Bardzo maly plik (<10mm) powinien wygenerowac warning.
    pdf = make_rectangle_vector(tmp_path, w_mm=5, h_mm=5)
    can_export_lenient, _ = preflight_gate(pdf, strict=False)
    can_export_strict, result = preflight_gate(pdf, strict=True)
    # Jesli sa warningi, strict powinien blokowac
    if result["status"] == "warning":
        assert can_export_lenient is True
        assert can_export_strict is False


def test_summary_format(tmp_path):
    pdf = make_rectangle_vector(tmp_path, w_mm=80, h_mm=50)
    _, result = preflight_gate(pdf)
    s = preflight_summary(result)
    assert isinstance(s, str)
    assert len(s) > 0
    # Powinien zawierac wymiary
    assert "80" in s or "50" in s or result["status"] in s
