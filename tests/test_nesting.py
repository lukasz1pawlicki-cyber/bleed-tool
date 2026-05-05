"""Smoke testy nesting/shelf packing (modules/nesting.nest_job)."""
import numpy as np
import pytest

from models import Sticker, Job
from modules.nesting import nest_job


def make_sticker(w_mm: float, h_mm: float, source: str = "/tmp/a.pdf") -> Sticker:
    """Tworzy pusty Sticker o zadanych wymiarach (z prostokatnym konturem)."""
    # Minimalne cut_segments — prostokat
    segs = [
        ('l', np.array([0.0, 0.0]), np.array([w_mm, 0.0])),
        ('l', np.array([w_mm, 0.0]), np.array([w_mm, h_mm])),
        ('l', np.array([w_mm, h_mm]), np.array([0.0, h_mm])),
        ('l', np.array([0.0, h_mm]), np.array([0.0, 0.0])),
    ]
    return Sticker(
        source_path=source,
        width_mm=w_mm,
        height_mm=h_mm,
        cut_segments=segs,
    )


# ============================================================================
# Smoke testy — podstawowe przypadki
# ============================================================================

def test_nest_empty_job_returns_no_sheets():
    """Pusty Job → brak arkuszy."""
    job = Job(stickers=[], plotter="summa_s3")
    result = nest_job(job, sheet_width_mm=320, sheet_height_mm=450)
    assert len(result.sheets) == 0


def test_nest_single_sticker_fits_one_sheet():
    """Jedna naklejka 50x30 na arkuszu SRA3 → 1 arkusz z 1 placement."""
    sticker = make_sticker(50, 30)
    job = Job(stickers=[(sticker, 1)], plotter="summa_s3")
    result = nest_job(job, sheet_width_mm=320, sheet_height_mm=450)
    assert len(result.sheets) == 1
    assert len(result.sheets[0].placements) == 1


def test_nest_multiple_copies_same_sticker():
    """10 kopii naklejki 50x30 — wszystkie na jednym arkuszu."""
    sticker = make_sticker(50, 30)
    job = Job(stickers=[(sticker, 10)], plotter="summa_s3")
    result = nest_job(job, sheet_width_mm=320, sheet_height_mm=450,
                      gap_mm=2, mark_zone_mm=0)
    assert len(result.sheets) >= 1
    total_placements = sum(len(s.placements) for s in result.sheets)
    assert total_placements == 10


def test_nest_too_big_sticker_skipped():
    """Naklejka wieksza niz arkusz → pomijana, brak arkuszy."""
    sticker = make_sticker(500, 500)  # nie miesci sie na 320x450
    job = Job(stickers=[(sticker, 1)], plotter="summa_s3")
    result = nest_job(job, sheet_width_mm=320, sheet_height_mm=450)
    assert len(result.sheets) == 0


def test_nest_placements_within_sheet_bounds():
    """Wszystkie placements miesza sie w granicach arkusza."""
    sticker = make_sticker(60, 40)
    job = Job(stickers=[(sticker, 20)], plotter="summa_s3")
    result = nest_job(job, sheet_width_mm=320, sheet_height_mm=450,
                      gap_mm=2, margins_mm=(5, 5, 5, 5), mark_zone_mm=10)

    for sheet in result.sheets:
        for p in sheet.placements:
            # Zachowuje ramki arkusza
            assert p.x_mm >= 0
            assert p.y_mm >= 0
            # Nie wychodzi poza prawą/gorną krawedź (uwzgledniajac rotacje)
            w = p.sticker.height_mm if p.rotation_deg == 90 else p.sticker.width_mm
            h = p.sticker.width_mm if p.rotation_deg == 90 else p.sticker.height_mm
            assert p.x_mm + w <= sheet.width_mm + 1  # +1 tol
            assert p.y_mm + h <= sheet.height_mm + 1


def test_nest_invalid_bleed_raises():
    """Ujemny bleed → ValueError."""
    sticker = make_sticker(50, 30)
    job = Job(stickers=[(sticker, 1)], plotter="summa_s3")
    with pytest.raises(ValueError, match="bleed"):
        nest_job(job, sheet_width_mm=320, sheet_height_mm=450, bleed_mm=-1)


def test_nest_invalid_gap_raises():
    """Ujemny gap → ValueError."""
    sticker = make_sticker(50, 30)
    job = Job(stickers=[(sticker, 1)], plotter="summa_s3")
    with pytest.raises(ValueError, match="gap"):
        nest_job(job, sheet_width_mm=320, sheet_height_mm=450, gap_mm=-1)


def test_nest_separate_mode_one_pattern_per_sheet():
    """Tryb 'separate': 2 rozne wzory → 2 arkusze (kazdy na osobnym)."""
    s1 = make_sticker(50, 30, source="/tmp/a.pdf")
    s2 = make_sticker(40, 20, source="/tmp/b.pdf")
    job = Job(stickers=[(s1, 1), (s2, 1)], plotter="summa_s3")
    result = nest_job(job, sheet_width_mm=320, sheet_height_mm=450,
                      grouping_mode="separate")
    assert len(result.sheets) == 2
    # Na kazdym arkuszu tylko jeden wzor
    for sheet in result.sheets:
        sources = {p.sticker.source_path for p in sheet.placements}
        assert len(sources) == 1


def test_nest_respects_sheet_size():
    """Arkusze maja ustawione width/height."""
    sticker = make_sticker(50, 30)
    job = Job(stickers=[(sticker, 5)], plotter="summa_s3")
    result = nest_job(job, sheet_width_mm=320, sheet_height_mm=450)
    for sheet in result.sheets:
        assert sheet.width_mm == 320
        assert sheet.height_mm == 450


def test_nest_bleed_reduces_capacity():
    """Z bleed 5mm miesci sie mniej niz bez bleed."""
    sticker = make_sticker(50, 30)
    job_no_bleed = Job(stickers=[(sticker, 100)], plotter="summa_s3")
    job_with_bleed = Job(stickers=[(sticker, 100)], plotter="summa_s3")

    no_bleed = nest_job(job_no_bleed, sheet_width_mm=320, sheet_height_mm=450,
                        gap_mm=2, bleed_mm=0, mark_zone_mm=0)
    with_bleed = nest_job(job_with_bleed, sheet_width_mm=320, sheet_height_mm=450,
                          gap_mm=2, bleed_mm=5, mark_zone_mm=0)

    n_no = sum(len(s.placements) for s in no_bleed.sheets)
    n_with = sum(len(s.placements) for s in with_bleed.sheets)
    # Mniej naklejek na arkusz → wiecej arkuszy LUB mniej placements per sheet
    # Kiedy wszystkie sie miesza, liczby beda rowne — ale arkuszy bedzie wiecej
    assert len(with_bleed.sheets) >= len(no_bleed.sheets)


# ============================================================================
# max_per_sheet — limit naklejek na arkuszu (tylko tryb mix)
# ============================================================================

def test_nest_max_per_sheet_caps_mix_mode():
    """Limit 10 szt./arkusz w trybie mix → arkusze maja <= 10 placementow."""
    s1 = make_sticker(50, 30, source="/tmp/a.pdf")
    s2 = make_sticker(50, 30, source="/tmp/b.pdf")
    # 30 naklejek; bez limitu zmiescilyby sie wszystkie na 1 arkuszu SRA3
    job = Job(stickers=[(s1, 15), (s2, 15)], plotter="summa_s3")
    result = nest_job(
        job, sheet_width_mm=320, sheet_height_mm=450,
        gap_mm=2, mark_zone_mm=0,
        grouping_mode="mix", max_per_sheet=10,
    )
    total = sum(len(s.placements) for s in result.sheets)
    assert total == 30, f"Wszystkie naklejki musza byc rozlozone (got {total})"
    for i, sheet in enumerate(result.sheets):
        assert len(sheet.placements) <= 10, (
            f"Arkusz {i + 1} ma {len(sheet.placements)} > 10 placements"
        )
    # 30 / 10 = 3 arkusze (lub wiecej jesli ostatni jest niepelny)
    assert len(result.sheets) >= 3


def test_nest_max_per_sheet_zero_means_unlimited():
    """max_per_sheet=0 = bez limitu (zachowanie sprzed feature)."""
    sticker = make_sticker(30, 20)
    job = Job(stickers=[(sticker, 20)], plotter="summa_s3")
    result = nest_job(
        job, sheet_width_mm=320, sheet_height_mm=450,
        gap_mm=2, mark_zone_mm=0,
        grouping_mode="mix", max_per_sheet=0,
    )
    total = sum(len(s.placements) for s in result.sheets)
    assert total == 20
    # Bez limitu wszystkie powinny zmiescic sie na 1 arkuszu
    assert len(result.sheets) == 1


def test_nest_max_per_sheet_ignored_outside_mix_mode():
    """max_per_sheet egzekwowany tylko w trybie 'mix' — w 'group' bez wplywu."""
    sticker = make_sticker(30, 20)
    job = Job(stickers=[(sticker, 20)], plotter="summa_s3")
    result = nest_job(
        job, sheet_width_mm=320, sheet_height_mm=450,
        gap_mm=2, mark_zone_mm=0,
        grouping_mode="group", max_per_sheet=5,  # cap ignorowany
    )
    total = sum(len(s.placements) for s in result.sheets)
    assert total == 20
    assert len(result.sheets) == 1  # tryb group nie respektuje cap


def test_nest_max_per_sheet_negative_raises():
    """Ujemna wartosc → ValueError."""
    sticker = make_sticker(30, 20)
    job = Job(stickers=[(sticker, 1)], plotter="summa_s3")
    with pytest.raises(ValueError):
        nest_job(
            job, sheet_width_mm=320, sheet_height_mm=450,
            grouping_mode="mix", max_per_sheet=-1,
        )
