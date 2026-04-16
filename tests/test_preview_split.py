"""Testy trybu split-view w PreviewPanel (przed/po).

Weryfikuje że:
  - Przycisk split jest widoczny tylko gdy mamy input_paths
  - Toggle przełącza tryb renderowania
  - Kompatybilność wsteczna: show_bleed_results([paths]) bez input_paths działa
"""
import os
import tempfile
import pytest

# PyQt6 tests wymagają QApplication
pytest.importorskip("PyQt6", reason="PyQt6 not installed")

from PyQt6.QtWidgets import QApplication
import fitz

from config import MM_TO_PT


@pytest.fixture(scope="module")
def qapp():
    """Pojedyncza QApplication na moduł — Qt nie lubi wielu instancji."""
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def two_pdfs(tmp_path):
    """Dwa minimalne PDF do podglądu."""
    paths = []
    for name in ["input.pdf", "output.pdf"]:
        doc = fitz.open()
        doc.new_page(width=100 * MM_TO_PT, height=100 * MM_TO_PT)
        p = tmp_path / name
        doc.save(str(p))
        doc.close()
        paths.append(str(p))
    return paths


# ============================================================================
# PreviewPanel — inicjalizacja
# ============================================================================

def test_preview_panel_init(qapp):
    from gui.preview_panel import PreviewPanel
    panel = PreviewPanel()
    assert panel._split_view is False
    assert panel._split_btn is not None
    # Początkowo ukryty (brak wyników)
    assert not panel._split_btn.isVisibleTo(panel) or not panel._split_btn.isVisible()


def test_show_bleed_results_without_input_paths_backward_compat(qapp, two_pdfs):
    """Wywołanie show_bleed_results z jedną listą (stary API) nie crashuje."""
    from gui.preview_panel import PreviewPanel
    panel = PreviewPanel()
    panel.show_bleed_results([two_pdfs[1]])  # tylko output
    assert len(panel._results) == 1
    assert panel._results[0].get("input_path") is None
    # Split btn ukryty gdy brak inputs
    assert not panel._split_btn.isVisible()


def test_show_bleed_results_with_input_paths_enables_split(qapp, two_pdfs):
    from gui.preview_panel import PreviewPanel
    panel = PreviewPanel()
    panel.show_bleed_results([two_pdfs[1]], input_paths=[two_pdfs[0]])
    assert len(panel._results) == 1
    assert panel._results[0].get("input_path") == two_pdfs[0]


def test_show_bleed_results_input_padding(qapp, two_pdfs):
    """Gdy input_paths ma mniej elementów niż paths → pad None."""
    from gui.preview_panel import PreviewPanel
    panel = PreviewPanel()
    panel.show_bleed_results(
        [two_pdfs[1], two_pdfs[1]],  # 2 outputs
        input_paths=[two_pdfs[0]],   # 1 input
    )
    assert len(panel._results) == 2
    assert panel._results[0].get("input_path") == two_pdfs[0]
    assert panel._results[1].get("input_path") is None


def test_toggle_split_changes_mode(qapp, two_pdfs):
    from gui.preview_panel import PreviewPanel
    panel = PreviewPanel()
    panel.show_bleed_results([two_pdfs[1]], input_paths=[two_pdfs[0]])
    assert panel._split_view is False

    panel._on_toggle_split(True)
    assert panel._split_view is True

    panel._on_toggle_split(False)
    assert panel._split_view is False


def test_clear_resets_split_state(qapp, two_pdfs):
    from gui.preview_panel import PreviewPanel
    panel = PreviewPanel()
    panel.show_bleed_results([two_pdfs[1]], input_paths=[two_pdfs[0]])
    panel._on_toggle_split(True)
    assert panel._split_view is True

    panel.clear()
    assert panel._split_view is False
    assert panel._split_btn.isChecked() is False


def test_render_input_file_caches(qapp, two_pdfs):
    """_render_input_file powinien cache'ować wyniki."""
    from gui.preview_panel import PreviewPanel
    panel = PreviewPanel()
    panel.show_bleed_results([two_pdfs[1]], input_paths=[two_pdfs[0]])

    # Pierwsze wywołanie
    pix1 = panel._render_input_file(two_pdfs[0])
    cache_size_1 = len(panel._cache)

    # Drugie wywołanie — z cache (bez nowego entry)
    pix2 = panel._render_input_file(two_pdfs[0])
    cache_size_2 = len(panel._cache)

    assert pix1 is not None
    assert pix2 is not None
    assert cache_size_1 == cache_size_2  # nie dodano nowego entry


def test_split_render_creates_items_in_scene(qapp, two_pdfs):
    """W trybie split-view scena ma elementy (pixmapy + etykiety)."""
    from gui.preview_panel import PreviewPanel
    panel = PreviewPanel()
    panel.show_bleed_results([two_pdfs[1]], input_paths=[two_pdfs[0]])
    panel._on_toggle_split(True)

    # Co najmniej 2 elementy (input + output pixmapy)
    items = panel._scene.items()
    assert len(items) >= 2


# ============================================================================
# split_enabled=False — tryb dla zakladki Nest (brak przycisku "Przed/Po")
# ============================================================================

def test_panel_without_split_has_no_button(qapp):
    """PreviewPanel(split_enabled=False) nie tworzy przycisku 'Przed/Po'."""
    from gui.preview_panel import PreviewPanel
    panel = PreviewPanel(split_enabled=False)
    assert panel._split_btn is None


def test_panel_without_split_bleed_results_does_not_crash(qapp, two_pdfs):
    """show_bleed_results na panelu bez split_enabled nie crashuje (None guard)."""
    from gui.preview_panel import PreviewPanel
    panel = PreviewPanel(split_enabled=False)
    panel.show_bleed_results([two_pdfs[1]], input_paths=[two_pdfs[0]])
    assert len(panel._results) == 1
    assert panel._split_view is False


def test_panel_without_split_clear_does_not_crash(qapp, two_pdfs):
    from gui.preview_panel import PreviewPanel
    panel = PreviewPanel(split_enabled=False)
    panel.show_bleed_results([two_pdfs[1]], input_paths=[two_pdfs[0]])
    panel.clear()
    assert len(panel._results) == 0


def test_panel_without_split_custom_placeholder(qapp):
    from gui.preview_panel import PreviewPanel
    panel = PreviewPanel(split_enabled=False, placeholder_text="test placeholder")
    assert panel._placeholder.text() == "test placeholder"


def test_panel_without_split_default_placeholder_mentions_arkusze(qapp):
    """Domyslny placeholder dla nest mentions 'arkusze' zamiast 'bleed'."""
    from gui.preview_panel import PreviewPanel
    panel = PreviewPanel(split_enabled=False)
    assert "arkusze" in panel._placeholder.text().lower()
