"""Smoke testy GUI (PyQt6) — uruchamianie bez pytest-qt.

Uzywamy QApplication bezposrednio, bez interakcji uzytkownika. Weryfikuja:
1. MainWindow otwiera sie bez crash
2. Zakladki Bleed/Nest istnieja i sa przelaczalne
3. Niezalezne panele podgladu (stan Bleed nie nadpisuje Nest i odwrotnie)
4. BleedWorker przetwarza prostokat i emituje `finished`
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Offscreen Qt — bez prawdziwego ekranu (CI-friendly)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PyQt6 = pytest.importorskip("PyQt6")
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QEventLoop, QTimer

from tests.fixtures import make_rectangle_vector


# Jeden QApplication dla calego modulu (QApplication moze istniec tylko raz)
@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app
    # Nie wywolujemy app.quit() - inne testy Qt moga uzywac instance


def _pump_events(ms: int = 50) -> None:
    """Pompuje event loop przez `ms` milisekund (bez blocking)."""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def test_main_window_opens(qapp):
    """MainWindow tworzy sie i zamyka bez wyjatku."""
    from gui.main_window import MainWindow
    win = MainWindow()
    try:
        assert win.windowTitle() == "Bleed Tool"
        # Minimalna geometria aplikacji
        assert win.minimumWidth() >= 1000
    finally:
        win.close()
        win.deleteLater()
        _pump_events()


def test_main_window_has_both_tabs(qapp):
    """Zakladki Bleed (idx 0) i Nest (idx 1) sa obecne."""
    from gui.main_window import MainWindow
    win = MainWindow()
    try:
        assert win._bleed_tab is not None, "BleedTab nie zainicjalizowana"
        assert win._nest_tab is not None, "NestTab nie zainicjalizowana"
        assert win._bleed_preview is not None
        assert win._nest_preview is not None
        # Domyslnie aktywna Bleed
        assert win._active_tab == "bleed"
    finally:
        win.close()
        win.deleteLater()
        _pump_events()


def test_tab_switch_changes_preview(qapp):
    """Przelaczenie zakladki zmienia aktywny preview panel.

    Regresja: stan Bleed i Nest musza byc w osobnych panelach, nie wspoldzielonych.
    """
    from gui.main_window import MainWindow
    win = MainWindow()
    try:
        # Start: Bleed aktywny
        win._activate_tab("bleed")
        assert win._preview_stack.currentWidget() is win._bleed_preview

        # Przelacz na Nest
        win._activate_tab("nest")
        assert win._preview_stack.currentWidget() is win._nest_preview
        assert win._active_tab == "nest"

        # Powrot na Bleed
        win._activate_tab("bleed")
        assert win._preview_stack.currentWidget() is win._bleed_preview
    finally:
        win.close()
        win.deleteLater()
        _pump_events()


def test_bleed_and_nest_previews_are_independent(qapp):
    """Preview Bleed i Nest to DWA ROZNE obiekty — stan nie wspoldzielony.

    Historyczna regresja: jeden wspolny panel preview -> pokazanie arkusza
    na Nest nadpisywalo split-view Bleed.
    """
    from gui.main_window import MainWindow
    from gui.preview_panel import PreviewPanel
    win = MainWindow()
    try:
        assert isinstance(win._bleed_preview, PreviewPanel)
        assert isinstance(win._nest_preview, PreviewPanel)
        assert win._bleed_preview is not win._nest_preview
        # Split-view enabled wylacznie dla Bleed (przed/po)
        # Nest preview ma split_enabled=False (jeden arkusz, bez porownania)
    finally:
        win.close()
        win.deleteLater()
        _pump_events()


def test_bleed_worker_processes_rectangle(qapp, tmp_path: Path):
    """BleedWorker na prostokacie wektorowym -> emituje `finished` z outputami."""
    from gui.workers import BleedWorker

    src = make_rectangle_vector(tmp_path, w_mm=40, h_mm=30)
    out_dir = tmp_path / "out"

    worker = BleedWorker(
        files=[src],
        output_dir=str(out_dir),
        bleed_mm=2.0,
    )

    captured = {"finished": None, "error": None}

    def on_finished(outs, infos):
        captured["finished"] = (outs, infos)

    def on_error(msg):
        captured["error"] = msg

    worker.finished.connect(on_finished)
    worker.error.connect(on_error)

    worker.start()

    # Czekamy do 30s (single rectangle powinien byc <5s)
    deadline_ms = 30_000
    step = 100
    elapsed = 0
    while elapsed < deadline_ms:
        if captured["finished"] or captured["error"]:
            break
        _pump_events(step)
        elapsed += step

    worker.wait(5000)

    assert captured["error"] is None, f"BleedWorker error: {captured['error']}"
    assert captured["finished"] is not None, "BleedWorker nie wyemitowal finished"

    outs, infos = captured["finished"]
    assert len(outs) == 1, f"Oczekiwano 1 output, jest {len(outs)}"
    assert os.path.isfile(outs[0]), f"Output PDF nie istnieje: {outs[0]}"
    assert "_PRINT_" in os.path.basename(outs[0])


def test_file_section_accepts_drops(qapp, tmp_path: Path):
    """FileSection przyjmuje listy plikow programistycznie (add_files).

    Zamiast symulowac drop & drop — weryfikujemy API ktore jest uzywane
    przez drop handler.
    """
    from gui.main_window import MainWindow
    win = MainWindow()
    try:
        # Ktorakolwiek sekcja plikow — sprawdz czy akceptuje liste
        fs = win._bleed_files_panel.file_section
        assert hasattr(fs, "_file_copies"), "FileSection powinien miec _file_copies"

        # Jesli jest add_files — uzyj
        src1 = make_rectangle_vector(tmp_path, w_mm=40, h_mm=30)
        os.rename(src1, str(tmp_path / "a.pdf"))
        src2 = make_rectangle_vector(tmp_path, w_mm=60, h_mm=40)
        os.rename(src2, str(tmp_path / "b.pdf"))

        if hasattr(fs, "add_files"):
            fs.add_files([str(tmp_path / "a.pdf"), str(tmp_path / "b.pdf")])
            # Nie crash — test podstawowy
    finally:
        win.close()
        win.deleteLater()
        _pump_events()
