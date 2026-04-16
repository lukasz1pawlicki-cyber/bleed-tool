"""
Bleed Tool — main_window.py
==============================
Główne okno: sidebar, QSplitter, QStackedWidget, LogPanel, PreviewPanel.
"""

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QSplitter, QStackedWidget, QSizePolicy,
)
from PyQt6.QtCore import Qt

from gui.log_panel import LogPanel
from gui.theme import ACCENT


class MainWindow(QMainWindow):
    """Główne okno aplikacji."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Bleed Tool")
        self.resize(1440, 900)
        self.setMinimumSize(1024, 700)

        # Stan
        self._active_tab = "bleed"

        # Centralny widget
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # === Sidebar ===
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(150)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(8, 12, 8, 12)
        sidebar_layout.setSpacing(4)

        # Logo
        logo = QLabel("Bleed Tool")
        logo.setObjectName("logo")
        sidebar_layout.addWidget(logo)
        sidebar_layout.addSpacing(16)

        # Nav buttons
        self._nav_buttons: dict[str, QPushButton] = {}
        self._bleed_btn = self._add_nav_btn(sidebar_layout, "bleed", "  Bleed")
        self._nest_btn = self._add_nav_btn(sidebar_layout, "nest", "  Nest")
        sidebar_layout.addStretch()

        root_layout.addWidget(sidebar)

        # === Splitter: lewy (content+log) | prawy (preview) ===
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setHandleWidth(5)

        # Lewy panel
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(16, 10, 8, 10)
        left_layout.setSpacing(0)

        # Stacked: Bleed | Nest
        self._stack = QStackedWidget()
        left_layout.addWidget(self._stack, stretch=1)

        # Log
        self.log_panel = LogPanel()
        self.log_panel.setMinimumHeight(60)
        self.log_panel.setMaximumHeight(160)
        left_layout.addWidget(self.log_panel)

        self._splitter.addWidget(left)

        # Prawy panel — preview. Niezalezne panele dla bleed i nest
        # (kazda zakladka ma wlasny stan podgladu, nie nadpisuja sie nawzajem).
        self._preview_container = QWidget()
        self._preview_layout = QVBoxLayout(self._preview_container)
        self._preview_layout.setContentsMargins(0, 4, 4, 4)
        self._preview_stack = QStackedWidget()
        self._preview_layout.addWidget(self._preview_stack)
        self._splitter.addWidget(self._preview_container)

        # Proporcje splitera
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([580, 700])

        root_layout.addWidget(self._splitter)

        # === Inicjalizacja zakładek (lazy) ===
        self._bleed_tab = None
        self._nest_tab = None
        self._bleed_preview = None  # PreviewPanel dla zakladki Bleed (split-view on)
        self._nest_preview = None   # PreviewPanel dla zakladki Nest (split-view off)
        self._init_tabs()
        self._activate_tab("bleed")

    # --- Tab management ---

    def _init_tabs(self):
        """Tworzy zakładki i dwa niezalezne preview panels."""
        from gui.bleed_tab import BleedTab
        from gui.nest_tab import NestTab
        from gui.preview_panel import PreviewPanel

        self._bleed_tab = BleedTab(log_fn=self.log_panel.log)
        self._nest_tab = NestTab(log_fn=self.log_panel.log, main_window=self)
        self._stack.addWidget(self._bleed_tab)   # index 0
        self._stack.addWidget(self._nest_tab)     # index 1

        # Dwa niezalezne panele podgladu — kazdy z wlasnym stanem.
        # Bleed: split-view wlaczony (przed/po oryginalu vs wynik).
        # Nest:  split-view wylaczony (brak oryginalu dla arkusza) —
        #        przycisk "Przed/Po" w ogole nie powstaje.
        self._bleed_preview = PreviewPanel(split_enabled=True)
        self._nest_preview = PreviewPanel(split_enabled=False)
        self._preview_stack.addWidget(self._bleed_preview)  # index 0
        self._preview_stack.addWidget(self._nest_preview)   # index 1

        # Połączenia: po przetworzeniu → podgląd w swoim panelu
        self._bleed_tab.preview_ready.connect(self._on_bleed_preview)
        self._nest_tab.preview_ready.connect(self._on_nest_preview)

        # Crop preview: live update tylko w panelu bleed
        self._bleed_tab.crop_preview_requested.connect(self._bleed_preview.show_crop_preview)
        self._bleed_preview.crop_offset_changed.connect(self._bleed_tab.update_crop_offset)

        # Wyczyść z dowolnej zakładki → clear all
        self._bleed_tab._file_section.clear_requested.connect(self.clear_all)
        self._nest_tab._file_section.clear_requested.connect(self.clear_all)

    def _add_nav_btn(self, layout, key: str, text: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setProperty("class", "nav-btn")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(lambda: self._activate_tab(key))
        layout.addWidget(btn)
        self._nav_buttons[key] = btn
        return btn

    def _activate_tab(self, key: str):
        self._active_tab = key
        idx = 0 if key == "bleed" else 1
        self._stack.setCurrentIndex(idx)
        # Zsynchronizuj panel podgladu z aktywna zakladka
        self._preview_stack.setCurrentIndex(idx)

        # Style nav buttons
        for k, btn in self._nav_buttons.items():
            btn.setProperty("active", "true" if k == key else "false")
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    # --- Preview slots ---

    def _on_bleed_preview(self, input_infos: list, output_paths: list):
        """Slot: bleed zakończony → pokaż podgląd w panelu bleed + dodaj do nest.

        input_infos: [(src_path, page_idx), ...] parallel do output_paths.
        """
        if self._bleed_preview:
            self._bleed_preview.show_bleed_results(output_paths, input_infos=input_infos)
        # Auto-agregacja: dodaj outputy bleed do listy plików w nest
        if self._nest_tab and output_paths:
            self._nest_tab.add_files(output_paths)

    def _on_nest_preview(self, job, sheet_pdfs, bleed_mm):
        """Slot: nest zakończony → pokaż podgląd arkuszy w panelu nest."""
        if self._nest_preview:
            self._nest_preview.show_nest_job(job, sheet_pdfs, bleed_mm)

    # --- Clear all ---

    def clear_all(self):
        """Wyczyść pliki z obu zakładek + oba podgladu + log."""
        if self._bleed_tab:
            self._bleed_tab.clear()
        if self._nest_tab:
            self._nest_tab.clear()
        if self._bleed_preview:
            self._bleed_preview.clear()
        if self._nest_preview:
            self._nest_preview.clear()
        self.log_panel.clear_log()

    # --- Convenience ---

    def log(self, msg: str):
        self.log_panel.log(msg)

    def clear_log(self):
        self.log_panel.clear_log()
