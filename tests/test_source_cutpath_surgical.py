"""Regresja: surgical removal linii ciecia (S/s -> n) zachowuje strukture PDF.

Commit 1505df5: zamiast usuwac cale `q..Q` bloki (psulo BDC/EMC pairing),
zamieniamy KAZDE `S` na `n` w content stream. Struktura streamu (q/Q,
BDC/EMC, cm, gs, Do) nietknieta.

Test weryfikuje funkcje `_replace_strokes_with_nop(ops)` w modules/export.py:
1. Baseline (bez OCG): S zamieniane na n, pozostale operatory identyczne
2. OCG (BDC/EMC): liczba BDC/EMC par zachowana, q/Q balanced
3. Wiele stroke ops: wszystkie zamienione (not tylko pierwsze/ostatnie)
4. s (close+stroke) -> h + n (dwa operatory zamiast jednego)
"""
from __future__ import annotations

import pikepdf

from modules.export import _replace_strokes_with_nop


def _count_ops(ops, names: set[str]) -> int:
    return sum(1 for _, op in ops if str(op) in names)


def _make_op(operands, name: str):
    return (operands, pikepdf.Operator(name))


def test_replace_S_with_n_baseline():
    """Najprostszy przypadek: m l l S -> m l l n."""
    ops = [
        _make_op([pikepdf.Name("/G0"), 1], "gs"),
        _make_op([0, 0], "m"),
        _make_op([100, 0], "l"),
        _make_op([100, 100], "l"),
        _make_op([], "S"),
    ]
    cleaned = _replace_strokes_with_nop(ops)
    assert len(cleaned) == len(ops), "Nie moze dodawac/usuwac operatorow (poza s -> h n)"
    assert _count_ops(cleaned, {"S"}) == 0, "S musi zniknac"
    assert _count_ops(cleaned, {"n"}) == 1, "Dokladnie jedno n zamiast S"
    # Pozostale ops identyczne
    for (_, orig), (_, cleaned_op) in zip(ops[:-1], cleaned[:-1]):
        assert str(orig) == str(cleaned_op)


def test_replace_s_with_h_n():
    """Closepath+stroke (s) -> closepath (h) + endpath (n). Jedno IN, dwa OUT."""
    ops = [
        _make_op([0, 0], "m"),
        _make_op([100, 0], "l"),
        _make_op([], "s"),
    ]
    cleaned = _replace_strokes_with_nop(ops)
    # s -> h n: ops rosnie o 1
    assert len(cleaned) == len(ops) + 1
    assert _count_ops(cleaned, {"s"}) == 0
    assert _count_ops(cleaned, {"h"}) == 1
    assert _count_ops(cleaned, {"n"}) == 1
    # Kolejnosc: ..., h, n
    assert str(cleaned[-2][1]) == "h"
    assert str(cleaned[-1][1]) == "n"


def test_bdc_emc_pairing_preserved():
    """Regresja: OCG BDC/EMC pary musza byc zachowane (liczba + kolejnosc).

    Poprzednia impl usuwala cale q..Q bloki z OCG -> BDC bez EMC -> PDF broken.
    Nowa impl: tylko S->n, BDC/EMC nietkniete.
    """
    ops = [
        _make_op([pikepdf.Name("/Layer1")], "BDC"),
        _make_op([], "q"),
        _make_op([0, 0], "m"),
        _make_op([50, 50], "l"),
        _make_op([], "S"),
        _make_op([], "Q"),
        _make_op([], "EMC"),
        _make_op([pikepdf.Name("/Layer2")], "BDC"),
        _make_op([], "q"),
        _make_op([60, 60], "m"),
        _make_op([100, 100], "l"),
        _make_op([], "s"),
        _make_op([], "Q"),
        _make_op([], "EMC"),
    ]
    cleaned = _replace_strokes_with_nop(ops)
    assert _count_ops(cleaned, {"BDC"}) == 2, "BDC musi byc zachowane"
    assert _count_ops(cleaned, {"EMC"}) == 2, "EMC musi byc zachowane"
    assert _count_ops(cleaned, {"q"}) == _count_ops(cleaned, {"Q"}), "q/Q balanced"
    assert _count_ops(cleaned, {"BDC"}) == _count_ops(cleaned, {"EMC"}), "BDC/EMC balanced"
    # Stroke usunieta
    assert _count_ops(cleaned, {"S", "s"}) == 0


def test_multiple_strokes_all_replaced():
    """Jesli content stream ma N stroke ops, wszystkie musza byc zamienione."""
    ops = []
    for i in range(5):
        ops.append(_make_op([i * 10, 0], "m"))
        ops.append(_make_op([i * 10 + 5, 5], "l"))
        ops.append(_make_op([], "S"))
    cleaned = _replace_strokes_with_nop(ops)
    assert _count_ops(cleaned, {"S"}) == 0, "Zadne S nie moze zostac"
    assert _count_ops(cleaned, {"n"}) == 5, "Piec S -> piec n"


def test_non_stroke_ops_untouched():
    """m, l, c, cm, gs, Do, Tj, BI/ID/EI pozostaja nietkniete."""
    ops = [
        _make_op([1, 0, 0, 1, 50, 50], "cm"),
        _make_op([pikepdf.Name("/Img1")], "Do"),
        _make_op([], "q"),
        _make_op([0, 0], "m"),
        _make_op([10, 10, 20, 20, 30, 30], "c"),
        _make_op([], "f"),       # fill — NIE jest stroke
        _make_op([], "Q"),
    ]
    cleaned = _replace_strokes_with_nop(ops)
    assert len(cleaned) == len(ops)
    # Zadne z tych operatorow nie powinno sie zmienic
    for (orig_operands, orig_op), (cleaned_operands, cleaned_op) in zip(ops, cleaned):
        assert str(orig_op) == str(cleaned_op)


def test_B_variants_convert_to_fill():
    """B/B*/b/b* (stroke+fill) -> fill-only (f/f*, z opcjonalnym h)."""
    ops_B = [_make_op([0, 0], "m"), _make_op([], "B")]
    cleaned = _replace_strokes_with_nop(ops_B)
    assert _count_ops(cleaned, {"B"}) == 0
    assert _count_ops(cleaned, {"f"}) == 1

    ops_Bs = [_make_op([0, 0], "m"), _make_op([], "B*")]
    cleaned = _replace_strokes_with_nop(ops_Bs)
    assert _count_ops(cleaned, {"B*"}) == 0
    assert _count_ops(cleaned, {"f*"}) == 1

    ops_b = [_make_op([0, 0], "m"), _make_op([], "b")]
    cleaned = _replace_strokes_with_nop(ops_b)
    assert _count_ops(cleaned, {"b"}) == 0
    assert _count_ops(cleaned, {"h"}) == 1
    assert _count_ops(cleaned, {"f"}) == 1
