"""
Bleed Tool — flexcut_dialog.py
=================================
Dialog FlexCut: interaktywne zaznaczanie naklejek, rubber band, skróty Z/R/S.
"""

import os
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QGraphicsView, QGraphicsScene, QGraphicsRectItem, QGraphicsPixmapItem,
)
from PyQt6.QtCore import Qt, QRectF, pyqtSignal
from PyQt6.QtGui import QPixmap, QImage, QPen, QColor, QBrush, QKeyEvent

from gui.theme import PREVIEW_CUTCONTOUR, PREVIEW_FLEXCUT, ACCENT


class FlexCutDialog(QDialog):
    """Dialog do interaktywnego zaznaczania FlexCut."""

    def __init__(self, job, sheet_pdfs, bleed_mm, reexport_fn=None, log_fn=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("FlexCut")
        self.resize(1280, 780)
        self.setMinimumSize(1000, 600)

        self.job = job
        self.sheet_pdfs = sheet_pdfs
        self.bleed_mm = bleed_mm
        self._reexport = reexport_fn
        self._log = log_fn or (lambda msg: None)
        self.current_sheet_idx = 0
        self._selected: set[int] = set()
        self._render_cache: dict = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        # Toolbar
        toolbar = QHBoxLayout()
        toolbar.setSpacing(4)

        self._prev_btn = QPushButton("‹")
        self._prev_btn.setFixedSize(28, 26)
        self._prev_btn.clicked.connect(self._prev_sheet)
        toolbar.addWidget(self._prev_btn)

        self._title = QLabel("Arkusz 1/1")
        font = self._title.font()
        font.setPointSize(12)
        font.setBold(True)
        self._title.setFont(font)
        toolbar.addWidget(self._title)

        self._next_btn = QPushButton("›")
        self._next_btn.setFixedSize(28, 26)
        self._next_btn.clicked.connect(self._next_sheet)
        toolbar.addWidget(self._next_btn)

        toolbar.addStretch()

        # Buttons (right)
        for text, slot, style in [
            ("Dodaj spad (S)", self._on_add_bleed, "toolbar-purple"),
            ("Obróć 180° (R)", self._on_rotate_180, "toolbar-btn"),
            ("Dodaj FlexCut (Z)", self._on_add_flexcut, "toolbar-btn"),
            ("Zastosuj", self._on_apply, "success"),
            ("Wyczyść", self._on_clear, "danger"),
            ("Zamknij", self.close, "ghost"),
        ]:
            btn = QPushButton(text)
            if style in ("success", "danger", "ghost"):
                btn.setObjectName(style)
            else:
                btn.setProperty("class", style)
            btn.clicked.connect(slot)
            toolbar.addWidget(btn)

        layout.addLayout(toolbar)

        # Scene + View
        self._scene = QGraphicsScene()
        self._view = QGraphicsView(self._scene)
        self._view.setRenderHints(self._view.renderHints())
        self._view.setDragMode(QGraphicsView.DragMode.NoDrag)
        self._view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._view.setBackgroundBrush(QColor("#e9ecef"))
        layout.addWidget(self._view, stretch=1)

        self._update_nav()
        self._render_current()

    # --- Keyboard shortcuts ---

    def keyPressEvent(self, event: QKeyEvent):
        key = event.text().lower()
        if key == 'z':
            self._on_add_flexcut()
        elif key == 'r':
            self._on_rotate_180()
        elif key == 's':
            self._on_add_bleed()
        else:
            super().keyPressEvent(event)

    # --- Navigation ---

    def _prev_sheet(self):
        if self.current_sheet_idx > 0:
            self.current_sheet_idx -= 1
            self._selected.clear()
            self._render_current()
            self._update_nav()

    def _next_sheet(self):
        n = len(self.job.sheets) if self.job else 0
        if self.current_sheet_idx < n - 1:
            self.current_sheet_idx += 1
            self._selected.clear()
            self._render_current()
            self._update_nav()

    def _update_nav(self):
        n = len(self.job.sheets) if self.job else 0
        self._prev_btn.setEnabled(self.current_sheet_idx > 0)
        self._next_btn.setEnabled(self.current_sheet_idx < n - 1)
        self._title.setText(f"Arkusz {self.current_sheet_idx + 1}/{n}")

    # --- Rendering ---

    def _render_current(self):
        self._scene.clear()
        idx = self.current_sheet_idx
        if not self.job or idx >= len(self.sheet_pdfs):
            return
        pp, cp = self.sheet_pdfs[idx]
        if not os.path.isfile(pp):
            return

        import fitz
        doc = fitz.open(pp)
        page = doc[0]
        mat = fitz.Matrix(150 / 72.0, 150 / 72.0)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        qimg = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888)
        qpixmap = QPixmap.fromImage(qimg.copy())
        doc.close()

        self._scene.addPixmap(qpixmap)
        self._view.fitInView(self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    # --- Actions (placeholder implementations) ---

    def _on_add_flexcut(self):
        self._log("FlexCut: dodaj (TODO: pełna implementacja rubber band)")

    def _on_rotate_180(self):
        if not self._selected or not self.job:
            self._log("Obróć 180°: brak zaznaczonych")
            return
        sheet = self.job.sheets[self.current_sheet_idx]
        for idx in self._selected:
            if idx < len(sheet.placements):
                p = sheet.placements[idx]
                p.rotation_deg = (p.rotation_deg + 180) % 360
        self._selected.clear()
        if self._reexport:
            self._reexport(self.current_sheet_idx)
            self._render_cache.clear()
            self._render_current()
        self._log(f"Obróć 180°: OK")

    def _on_add_bleed(self):
        if not self.job or not self.job.sheets:
            return
        from models import PanelLine
        sheet = self.job.sheets[self.current_sheet_idx]
        if not sheet.placements:
            return
        bleed2 = 2 * self.bleed_mm

        def _pw(p):
            return p.sticker.height_mm + bleed2 if int(p.rotation_deg) % 360 in (90, 270) else p.sticker.width_mm + bleed2
        def _ph(p):
            return p.sticker.width_mm + bleed2 if int(p.rotation_deg) % 360 in (90, 270) else p.sticker.height_mm + bleed2

        x0 = min(p.x_mm for p in sheet.placements)
        y0 = min(p.y_mm for p in sheet.placements)
        x1 = max(p.x_mm + _pw(p) for p in sheet.placements)
        y1 = max(p.y_mm + _ph(p) for p in sheet.placements)

        sheet.outer_bleed_mm = 2.0
        sheet.panel_lines = [pl for pl in sheet.panel_lines if pl.bridge_length_mm > 0]
        sheet.panel_lines.extend([
            PanelLine("horizontal", y0, x0, x1, bridge_length_mm=0.0),
            PanelLine("horizontal", y1, x0, x1, bridge_length_mm=0.0),
            PanelLine("vertical", x0, y0, y1, bridge_length_mm=0.0),
            PanelLine("vertical", x1, y0, y1, bridge_length_mm=0.0),
        ])
        if self._reexport:
            self._reexport(self.current_sheet_idx)
            self._render_cache.clear()
            self._render_current()
        self._log(f"Spad: 2mm + linia cięcia ({x0:.1f},{y0:.1f})-({x1:.1f},{y1:.1f})mm")

    def _on_apply(self):
        if not self.job:
            return
        for idx in range(len(self.job.sheets)):
            if self._reexport:
                self._reexport(idx)
        self._render_cache.clear()
        self._render_current()
        self._log("Zastosuj: wyeksportowano")

    def _on_clear(self):
        if not self.job:
            return
        sheet = self.job.sheets[self.current_sheet_idx]
        sheet.panel_lines.clear()
        sheet.outer_bleed_mm = 0.0
        self._selected.clear()
        if self._reexport:
            self._reexport(self.current_sheet_idx)
            self._render_cache.clear()
            self._render_current()
        self._log("Wyczyszczono")
