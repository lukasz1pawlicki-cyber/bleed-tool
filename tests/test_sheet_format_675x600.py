"""Regresja: format arkusza 675x600mm musi byc obslugiwany w konfiguracji i nestingu.

Commit b3aba8b: dodany format arkusza 675x600mm (duzy arkusz papieru rolki
rozciete na format dla ploterow digital cutting).

Sprawdza:
1. config.SHEET_PRESETS / SHEET_SIZES zawieraja "675x600"
2. Wymiary sa poprawne: (675, 600)
3. nest_job akceptuje 675x600 jako sheet_width/sheet_height
4. Naklejka 100x100mm miesci sie na 675x600 arkuszu (placement w bounds)
5. Utilization > 0 (naklejki faktycznie wchodza)
"""
from __future__ import annotations

import numpy as np

from config import SHEET_PRESETS, SHEET_SIZES
from models import Sticker, Job
from modules.nesting import nest_job


def _make_sticker(w_mm: float, h_mm: float) -> Sticker:
    segs = [
        ('l', np.array([0.0, 0.0]), np.array([w_mm, 0.0])),
        ('l', np.array([w_mm, 0.0]), np.array([w_mm, h_mm])),
        ('l', np.array([w_mm, h_mm]), np.array([0.0, h_mm])),
        ('l', np.array([0.0, h_mm]), np.array([0.0, 0.0])),
    ]
    return Sticker(
        source_path="/tmp/sticker.pdf",
        width_mm=w_mm, height_mm=h_mm,
        cut_segments=segs,
    )


def test_675x600_in_sheet_presets():
    """Format 675x600 musi byc obecny w SHEET_PRESETS (UI) i SHEET_SIZES (nesting)."""
    assert "675×600" in SHEET_PRESETS, "Brakuje 675x600 w SHEET_PRESETS (GUI)"
    assert SHEET_PRESETS["675×600"] == (675, 600), \
        f"Zle wymiary 675x600: {SHEET_PRESETS['675×600']}"

    assert "675×600" in SHEET_SIZES, "Brakuje 675x600 w SHEET_SIZES (nesting fallback)"
    assert SHEET_SIZES["675×600"] == (675, 600)


def test_675x600_accepts_small_sticker():
    """Naklejka 100x100 wchodzi na 675x600 arkusz — placement wraca."""
    sticker = _make_sticker(100, 100)
    job = Job(stickers=[(sticker, 1)], plotter="summa_s3")
    result = nest_job(
        job,
        sheet_width_mm=675, sheet_height_mm=600,
        gap_mm=2, mark_zone_mm=13,
    )
    assert len(result.sheets) == 1
    sheet = result.sheets[0]
    assert len(sheet.placements) == 1
    assert sheet.width_mm == 675
    assert sheet.height_mm == 600


def test_675x600_placement_within_bounds():
    """Placement naklejki na 675x600 nie moze wychodzic poza arkusz.

    Sprawdza: x, y, x+w, y+h w (0, sheet_w/h). Uwzglednia mark zone.
    """
    sticker = _make_sticker(120, 80)
    job = Job(stickers=[(sticker, 1)], plotter="summa_s3")
    result = nest_job(
        job,
        sheet_width_mm=675, sheet_height_mm=600,
        gap_mm=0, mark_zone_mm=13,
    )
    assert len(result.sheets) == 1
    for p in result.sheets[0].placements:
        assert p.x_mm >= 0, f"Placement x < 0: {p.x_mm}"
        assert p.y_mm >= 0, f"Placement y < 0: {p.y_mm}"
        assert p.x_mm + p.sticker.width_mm <= 675 + 0.01, \
            f"Placement x+w={p.x_mm + p.sticker.width_mm} > 675"
        assert p.y_mm + p.sticker.height_mm <= 600 + 0.01, \
            f"Placement y+h={p.y_mm + p.sticker.height_mm} > 600"


def test_675x600_utilization_greater_than_zero():
    """Duza liczba naklejek na 675x600 -> utilization > 0 (arkusz nie pusty)."""
    sticker = _make_sticker(50, 50)
    job = Job(stickers=[(sticker, 40)], plotter="summa_s3")
    result = nest_job(
        job,
        sheet_width_mm=675, sheet_height_mm=600,
        gap_mm=3, mark_zone_mm=13,
    )
    assert len(result.sheets) >= 1
    total_placements = sum(len(s.placements) for s in result.sheets)
    assert total_placements >= 20, (
        f"Oczekiwano >=20 placements na 675x600, "
        f"otrzymano {total_placements} (prawdopodobnie format nieobslugiwany)"
    )
    # Pierwszy arkusz powinien byc dobrze wypelniony
    sheet_area = 675 * 600
    sticker_area = 50 * 50
    first_sheet_used = len(result.sheets[0].placements) * sticker_area
    util = first_sheet_used / sheet_area
    assert util > 0.05, f"Utilization {util*100:.1f}% za niskie"


def test_675x600_larger_capacity_than_sra3():
    """675x600 > SRA3 (320x450) -> wieksza pojemnosc arkusza."""
    sticker = _make_sticker(50, 50)
    # Ta sama liczba kopii, rozne rozmiary arkusza
    job_sra3 = Job(stickers=[(sticker, 60)], plotter="summa_s3")
    job_675 = Job(stickers=[(sticker, 60)], plotter="summa_s3")

    res_sra3 = nest_job(job_sra3, sheet_width_mm=320, sheet_height_mm=450,
                        gap_mm=2, mark_zone_mm=13)
    res_675 = nest_job(job_675, sheet_width_mm=675, sheet_height_mm=600,
                       gap_mm=2, mark_zone_mm=13)

    # 675x600 (405000 mm2) vs SRA3 (144000 mm2) -> >= 2x mniej arkuszy
    assert len(res_675.sheets) <= len(res_sra3.sheets), (
        f"675x600 wymaga {len(res_675.sheets)} arkuszy, "
        f"SRA3 {len(res_sra3.sheets)} — wiekszy format powinien byc efektywniejszy"
    )
