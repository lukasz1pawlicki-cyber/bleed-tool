"""
Bleed Tool — main_window.py
==============================
Layout: [Top TabBar: Bleed | Nest]
        [Files (resizable) | Settings (fixed) | Preview (stretch)]
        [LogPanel — pod całością]
"""

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFrame, QLabel,
    QSplitter, QStackedWidget, QTabBar, QSizePolicy,
)
from PyQt6.QtCore import Qt

from gui.log_panel import LogPanel
from gui.file_section import FileSection


class _FilePanel(QFrame):
    """Kolumna plików. DropZone (FileSection) dociągnięty do górnej krawędzi."""

    def __init__(self, title: str, aux: str, show_copies: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("FilePanel")
        lay = QVBoxLayout(self)
        # Mały top-margin — odstęp od zakładek (Bleed/Nest)
        lay.setContentsMargins(0, 12, 0, 0)
        lay.setSpacing(0)
        # FileSection (DropZone + lista + bar)
        self.file_section = FileSection(show_copies=show_copies)
        lay.addWidget(self.file_section, stretch=1)


class MainWindow(QMainWindow):
    """Glowne okno aplikacji."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Bleed Tool")
        self.resize(1440, 900)
        self.setMinimumSize(1200, 720)

        # Stan
        self._active_tab = "bleed"

        # Centralny widget
        central = QWidget()
        central.setObjectName("centralWidget")
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # === Top tab bar ===
        self._tab_bar = QTabBar()
        self._tab_bar.setObjectName("TopTabBar")
        self._tab_bar.addTab("Bleed")
        self._tab_bar.addTab("Nest")
        self._tab_bar.setExpanding(False)
        self._tab_bar.setDrawBase(True)
        # Większe zakładki — font + padding
        _tab_font = self._tab_bar.font()
        _tab_font.setPointSize(max(_tab_font.pointSize() + 3, 12))
        _tab_font.setBold(True)
        self._tab_bar.setFont(_tab_font)
        self._tab_bar.setStyleSheet(
            "QTabBar::tab{padding:10px 28px;min-width:120px;}"
            "QTabBar::tab:selected{background:#2563EB;color:#fff;}"
        )
        self._tab_bar.currentChanged.connect(self._on_tab_changed)
        root_layout.addWidget(self._tab_bar)

        # === Splitter: Files (resizable) | Settings (fixed) | Preview (stretch) ===
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setHandleWidth(1)

        # --- Lewa kolumna: Files stack ---
        self._file_stack = QStackedWidget()
        # Oba panele z copies — user może ustawić liczbę kopii na etapie Bleed
        # (propaguje się do Nest po auto-agregacji) lub poprawić na Nest.
        self._bleed_files_panel = _FilePanel(
            "Pliki wejściowe",
            "PDF · AI · SVG · EPS · PNG · JPG · TIFF",
            show_copies=True,
        )
        self._nest_files_panel = _FilePanel(
            "Pliki do arkusza",
            "po bleedzie · kopie per plik",
            show_copies=True,
        )
        self._file_stack.addWidget(self._bleed_files_panel)
        self._file_stack.addWidget(self._nest_files_panel)
        # Kolumna plików: min 360 (mieści się na 1200px oknie obok fixed 420+ settings)
        self._file_stack.setMinimumWidth(360)
        self._splitter.addWidget(self._file_stack)

        # --- Środkowa kolumna: Settings stack (fixed width) ---
        self._settings_container = QWidget()
        sc_layout = QVBoxLayout(self._settings_container)
        sc_layout.setContentsMargins(0, 0, 0, 0)
        sc_layout.setSpacing(0)
        self._stack = QStackedWidget()
        sc_layout.addWidget(self._stack, stretch=1)
        self._splitter.addWidget(self._settings_container)

        # --- Prawa kolumna: Preview stack ---
        self._preview_container = QWidget()
        self._preview_layout = QVBoxLayout(self._preview_container)
        self._preview_layout.setContentsMargins(0, 0, 0, 0)
        self._preview_stack = QStackedWidget()
        self._preview_layout.addWidget(self._preview_stack)
        # Preview min 360 (mieści się na 1200px oknie)
        self._preview_container.setMinimumWidth(360)
        self._splitter.addWidget(self._preview_container)

        # Splitter policy
        for i in range(3):
            self._splitter.setCollapsible(i, False)
        self._splitter.setStretchFactor(0, 1)  # files: expand
        self._splitter.setStretchFactor(1, 0)  # settings: fixed
        self._splitter.setStretchFactor(2, 2)  # preview: expand more
        # Sizes początkowe: files 630 (+50% vs 420), settings ~460 (fixed), preview reszta
        self._splitter.setSizes([630, 460, 560])

        root_layout.addWidget(self._splitter, stretch=1)

        # === Log panel (pod całością) ===
        self.log_panel = LogPanel()
        self.log_panel.setMinimumHeight(80)
        self.log_panel.setMaximumHeight(160)
        root_layout.addWidget(self.log_panel)

        # === Zakladki ===
        self._bleed_tab = None
        self._nest_tab = None
        self._bleed_preview = None
        self._nest_preview = None
        self._init_tabs()
        # Fixed width settings column — policzone z sizeHint obu tabs
        self._apply_fixed_settings_width()
        self._activate_tab("bleed")

    def _init_tabs(self):
        from gui.bleed_tab import BleedTab
        from gui.nest_tab import NestTab
        from gui.preview_panel import PreviewPanel

        # Tabs otrzymują file_section z panelu lewej kolumny
        self._bleed_tab = BleedTab(
            log_fn=self.log_panel.log,
            file_section=self._bleed_files_panel.file_section,
        )
        self._nest_tab = NestTab(
            log_fn=self.log_panel.log,
            main_window=self,
            file_section=self._nest_files_panel.file_section,
        )
        self._stack.addWidget(self._bleed_tab)
        self._stack.addWidget(self._nest_tab)

        self._bleed_preview = PreviewPanel(split_enabled=True)
        self._nest_preview = PreviewPanel(split_enabled=False)
        self._preview_stack.addWidget(self._bleed_preview)
        self._preview_stack.addWidget(self._nest_preview)

        self._bleed_tab.preview_ready.connect(self._on_bleed_preview)
        self._nest_tab.preview_ready.connect(self._on_nest_preview)
        self._bleed_tab.crop_preview_requested.connect(self._bleed_preview.show_crop_preview)
        self._bleed_preview.crop_offset_changed.connect(self._bleed_tab.update_crop_offset)

        self._bleed_tab._file_section.clear_requested.connect(self.clear_all)
        self._nest_tab._file_section.clear_requested.connect(self.clear_all)

    def _apply_fixed_settings_width(self):
        """Ustawia stałą szerokość środkowej kolumny (settings).

        Lock na CONTAINER (nie tylko inner stack) — inaczej QSplitter pozwala
        draggować uchwyt, container się rozciąga, a inner stack pozostaje
        fixed → "skakanie" kontrolek przy drag. Dodatkowo blokujemy uchwyty
        splittera (przycisków przesuwania granicy środkowej kolumny).
        """
        try:
            bleed_w = self._bleed_tab.sizeHint().width()
            nest_w = self._nest_tab.sizeHint().width()
            target = max(bleed_w, nest_w)
            target = max(target + 20, 420)
        except Exception:
            target = 460
        self._stack.setFixedWidth(target)
        # Container MUSI mieć fixed width — QSplitter wtedy nie pozwoli draggować.
        self._settings_container.setFixedWidth(target)
        # Jawnie wyłącz uchwyty splittera wokół środkowej kolumny
        # (handle[0] = między files i settings, handle[1] = między settings i preview)
        for i in (1, 2):
            h = self._splitter.handle(i)
            if h is not None:
                h.setEnabled(False)
                h.setCursor(Qt.CursorShape.ArrowCursor)

    def _on_tab_changed(self, index: int):
        key = "bleed" if index == 0 else "nest"
        self._activate_tab(key)

    def _activate_tab(self, key: str):
        self._active_tab = key
        idx = 0 if key == "bleed" else 1
        self._file_stack.setCurrentIndex(idx)
        self._stack.setCurrentIndex(idx)
        self._preview_stack.setCurrentIndex(idx)
        if self._tab_bar.currentIndex() != idx:
            self._tab_bar.blockSignals(True)
            self._tab_bar.setCurrentIndex(idx)
            self._tab_bar.blockSignals(False)

    # --- Preview slots ---

    def _on_bleed_preview(self, input_infos: list, output_paths: list):
        if self._bleed_preview:
            self._bleed_preview.show_bleed_results(output_paths, input_infos=input_infos)
        if self._nest_tab and output_paths:
            self._nest_tab.add_files(output_paths)
            # Propaguj liczbę kopii z Bleed input → Nest output
            bleed_fs = self._bleed_files_panel.file_section
            nest_fs = self._nest_files_panel.file_section
            for out_path, info in zip(output_paths, input_infos):
                src_path = info[0] if isinstance(info, (tuple, list)) else info
                copies = bleed_fs._file_copies.get(src_path)
                if copies and copies > 1:
                    nest_fs._file_copies[out_path] = copies
            nest_fs._rebuild_list()

    def _on_nest_preview(self, job, sheet_pdfs, bleed_mm):
        if self._nest_preview:
            self._nest_preview.show_nest_job(job, sheet_pdfs, bleed_mm)

    # --- Clear all ---

    def clear_all(self):
        if self._bleed_tab:
            self._bleed_tab.clear()
        if self._nest_tab:
            self._nest_tab.clear()
        if self._bleed_preview:
            self._bleed_preview.clear()
        if self._nest_preview:
            self._nest_preview.clear()
        self.log_panel.clear_log()

    def log(self, msg: str):
        self.log_panel.log(msg)

    def clear_log(self):
        self.log_panel.clear_log()

    def closeEvent(self, event):
        # Zwolnij fitz.Document-y źródeł trzymane przez ostatni job Nest
        # (FlexCut/re-export potrzebuje ich póki job jest aktywny, ale przy
        # zamykaniu aplikacji handle-e muszą zostać zamknięte — Windows
        # blokuje pliki dopóki proces trzyma fitz.Document).
        if self._nest_tab is not None:
            self._nest_tab._close_last_open_docs()
        super().closeEvent(event)
