"""
Bleed Tool — main_window.py
==============================
Glowne okno: navy Sidebar (Technikadruku), QSplitter, QStackedWidget,
LogPanel, dwa niezalezne PreviewPanels.
"""

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFrame, QLabel,
    QPushButton, QSplitter, QStackedWidget, QButtonGroup, QSizePolicy,
)
from PyQt6.QtCore import Qt, pyqtSignal

from gui.log_panel import LogPanel
from gui.atoms import repolish


class Sidebar(QFrame):
    """Navy sidebar (196px): logo, nav, MachineCard, footer meta."""

    navigated = pyqtSignal(str)  # "bleed" | "nest" | "flexcut" | ...

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Sidebar")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 18, 14, 14)
        lay.setSpacing(10)

        # === Logo ===
        logo = QWidget()
        lh = QHBoxLayout(logo)
        lh.setContentsMargins(4, 0, 4, 14)
        lh.setSpacing(10)
        mark = QLabel("BT")
        mark.setFixedSize(30, 30)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setStyleSheet(
            "background:#2563EB;color:#fff;border-radius:7px;"
            "font-family:'JetBrains Mono',monospace;font-weight:700;font-size:13px;"
        )
        lh.addWidget(mark)
        txt = QWidget()
        tl = QVBoxLayout(txt)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(2)
        name = QLabel("Bleed Tool")
        name.setObjectName("SidebarLogoText")
        sub = QLabel("STICKERPREP")
        sub.setObjectName("SidebarLogoSub")
        tl.addWidget(name)
        tl.addWidget(sub)
        lh.addWidget(txt)
        lh.addStretch(1)
        lay.addWidget(logo)

        # === Workflow ===
        lay.addWidget(self._section_label("Workflow"))
        self.btn_bleed = self._nav("Bleed", key="bleed", checked=True)
        self.btn_nest = self._nav("Nest", key="nest")
        lay.addWidget(self.btn_bleed)
        lay.addWidget(self.btn_nest)

        # Grupa ekskluzywna
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        for b in (self.btn_bleed, self.btn_nest):
            self._nav_group.addButton(b)
            b.clicked.connect(
                lambda _=False, k=b.property("navKey"): self.navigated.emit(k)
            )

        lay.addStretch(1)
        lay.addWidget(self._machine_card())

        # === Footer meta ===
        foot = QHBoxLayout()
        v = QLabel("v3.2.0")
        v.setObjectName("SidebarFootMeta")
        p = QLabel("FOGRA39")
        p.setObjectName("SidebarFootMeta")
        foot.addWidget(v)
        foot.addStretch(1)
        foot.addWidget(p)
        lay.addLayout(foot)

    def _section_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("NavLabel")
        return lbl

    def _nav(self, text: str, *, key: str, checked: bool = False) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("NavItem")
        b.setCheckable(True)
        b.setChecked(checked)
        b.setProperty("navKey", key)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        return b

    def _machine_card(self) -> QFrame:
        c = QFrame()
        c.setObjectName("MachineCard")
        v = QVBoxLayout(c)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(4)

        header = QHBoxLayout()
        title = QLabel("PARK MASZYNOWY")
        title.setObjectName("MachineCardTitle")
        header.addWidget(title)
        header.addStretch(1)
        dot = QLabel()
        dot.setFixedSize(6, 6)
        dot.setStyleSheet("background:#34D399;border-radius:3px;")
        header.addWidget(dot)
        v.addLayout(header)

        for key, val in (("Mimaki", "UCJV"), ("Summa", "S3"), ("JWEI", "0806")):
            row = QHBoxLayout()
            k = QLabel(key)
            k.setObjectName("MachineRowKey")
            vl = QLabel(val)
            vl.setObjectName("MachineRowVal")
            row.addWidget(k)
            row.addStretch(1)
            row.addWidget(vl)
            v.addLayout(row)

        return c

    def set_active(self, key: str) -> None:
        mapping = {
            "bleed": self.btn_bleed,
            "nest": self.btn_nest,
        }
        btn = mapping.get(key)
        if btn:
            btn.setChecked(True)


class MainWindow(QMainWindow):
    """Glowne okno aplikacji."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Bleed Tool")
        self.resize(1440, 900)
        self.setMinimumSize(1100, 720)

        # Stan
        self._active_tab = "bleed"

        # Centralny widget
        central = QWidget()
        central.setObjectName("centralWidget")
        self.setCentralWidget(central)
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # === Sidebar ===
        self._sidebar = Sidebar()
        self._sidebar.navigated.connect(self._activate_tab)
        root_layout.addWidget(self._sidebar)

        # === Splitter: lewy (content+log) | prawy (preview) ===
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setHandleWidth(1)

        # Lewy panel
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        # Stacked: Bleed | Nest
        self._stack = QStackedWidget()
        left_layout.addWidget(self._stack, stretch=1)

        # Log (navy terminal)
        self.log_panel = LogPanel()
        self.log_panel.setMinimumHeight(80)
        self.log_panel.setMaximumHeight(160)
        left_layout.addWidget(self.log_panel)

        self._splitter.addWidget(left)

        # Prawy panel — preview (stack dla Bleed / Nest)
        self._preview_container = QWidget()
        self._preview_layout = QVBoxLayout(self._preview_container)
        self._preview_layout.setContentsMargins(0, 0, 0, 0)
        self._preview_stack = QStackedWidget()
        self._preview_layout.addWidget(self._preview_stack)
        self._splitter.addWidget(self._preview_container)

        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([520, 720])

        root_layout.addWidget(self._splitter)

        # === Zakladki ===
        self._bleed_tab = None
        self._nest_tab = None
        self._bleed_preview = None
        self._nest_preview = None
        self._init_tabs()
        self._activate_tab("bleed")

    def _init_tabs(self):
        from gui.bleed_tab import BleedTab
        from gui.nest_tab import NestTab
        from gui.preview_panel import PreviewPanel

        self._bleed_tab = BleedTab(log_fn=self.log_panel.log)
        self._nest_tab = NestTab(log_fn=self.log_panel.log, main_window=self)
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

    def _activate_tab(self, key: str):
        self._active_tab = key
        idx = 0 if key == "bleed" else 1
        self._stack.setCurrentIndex(idx)
        self._preview_stack.setCurrentIndex(idx)
        self._sidebar.set_active(key)

    # --- Preview slots ---

    def _on_bleed_preview(self, input_infos: list, output_paths: list):
        if self._bleed_preview:
            self._bleed_preview.show_bleed_results(output_paths, input_infos=input_infos)
        if self._nest_tab and output_paths:
            self._nest_tab.add_files(output_paths)

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
