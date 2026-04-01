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

        # Prawy panel — preview (placeholder)
        self._preview_container = QWidget()
        self._preview_layout = QVBoxLayout(self._preview_container)
        self._preview_layout.setContentsMargins(0, 4, 4, 4)
        self._splitter.addWidget(self._preview_container)

        # Proporcje splitera
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([580, 700])

        root_layout.addWidget(self._splitter)

        # === Inicjalizacja zakładek (lazy) ===
        self._bleed_tab = None
        self._nest_tab = None
        self._preview_panel = None
        self._init_tabs()
        self._activate_tab("bleed")

    # --- Tab management ---

    def _init_tabs(self):
        """Tworzy zakładki i preview panel."""
        from gui.bleed_tab import BleedTab
        from gui.nest_tab import NestTab
        from gui.preview_panel import PreviewPanel

        self._bleed_tab = BleedTab(log_fn=self.log_panel.log)
        self._nest_tab = NestTab(log_fn=self.log_panel.log, main_window=self)
        self._stack.addWidget(self._bleed_tab)   # index 0
        self._stack.addWidget(self._nest_tab)     # index 1

        self._preview_panel = PreviewPanel()
        self._preview_layout.addWidget(self._preview_panel)

        # Połączenia: po przetworzeniu → podgląd
        self._bleed_tab.preview_ready.connect(self._on_bleed_preview)
        self._nest_tab.preview_ready.connect(self._on_nest_preview)

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

        # Style nav buttons
        for k, btn in self._nav_buttons.items():
            btn.setProperty("active", "true" if k == key else "false")
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    # --- Preview slots ---

    def _on_bleed_preview(self, paths: list):
        """Slot: bleed zakończony → pokaż podgląd."""
        if self._preview_panel:
            self._preview_panel.show_bleed_results(paths)

    def _on_nest_preview(self, job, sheet_pdfs, bleed_mm):
        """Slot: nest zakończony → pokaż podgląd arkuszy."""
        if self._preview_panel:
            self._preview_panel.show_nest_job(job, sheet_pdfs, bleed_mm)

    # --- Convenience ---

    def log(self, msg: str):
        self.log_panel.log(msg)

    def clear_log(self):
        self.log_panel.clear_log()
