"""
Bleed Tool — flexcut_dialog.py
=================================
Dialog FlexCut: interaktywne zaznaczanie naklejek, rubber band, skróty Z/R/S.
"""

import os
import traceback
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QGraphicsView, QGraphicsScene, QGraphicsRectItem, QGraphicsPixmapItem,
    QGraphicsLineItem, QRubberBand,
)
from PyQt6.QtCore import Qt, QRectF, QPointF, QRect, QSize, pyqtSignal
from PyQt6.QtGui import (
    QPixmap, QImage, QPen, QColor, QBrush, QKeyEvent, QMouseEvent,
    QWheelEvent, QTransform,
)

from gui.theme import PREVIEW_CUTCONTOUR, PREVIEW_FLEXCUT, ACCENT


class _FlexCutView(QGraphicsView):
    """QGraphicsView z kliknieciem, rubber-band i scroll-zoom."""
    clicked = pyqtSignal(QPointF)
    area_selected = pyqtSignal(QRectF)

    _DRAG_THRESHOLD = 6
    _ZOOM_FACTOR = 1.05

    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self._origin = None
        self._band = None

    # --- Zoom scroll ---

    def wheelEvent(self, event: QWheelEvent):
        delta = event.angleDelta().y()
        if delta > 0:
            factor = self._ZOOM_FACTOR
        elif delta < 0:
            factor = 1.0 / self._ZOOM_FACTOR
        else:
            return
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.scale(factor, factor)

    # --- Rubber band + click ---

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.position().toPoint()
            if self._band is None:
                self._band = QRubberBand(QRubberBand.Shape.Rectangle, self)
            self._band.setGeometry(QRect(self._origin, QSize()))
            self._band.show()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._origin is not None and self._band is not None:
            self._band.setGeometry(QRect(self._origin, event.position().toPoint()).normalized())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton and self._origin is not None:
            end = event.position().toPoint()
            if self._band:
                self._band.hide()

            dx = abs(end.x() - self._origin.x())
            dy = abs(end.y() - self._origin.y())

            if dx > self._DRAG_THRESHOLD or dy > self._DRAG_THRESHOLD:
                r = QRectF(self.mapToScene(self._origin),
                           self.mapToScene(end)).normalized()
                self.area_selected.emit(r)
            else:
                self.clicked.emit(self.mapToScene(end))

            self._origin = None
        super().mouseReleaseEvent(event)


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
        self._initial_fit_done = False
        self._dpi = 150
        self._scale = self._dpi / 72.0
        self._sheet_h_pt = 0.0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        # Toolbar
        toolbar = QHBoxLayout()
        toolbar.setSpacing(4)

        self._prev_btn = QPushButton("\u2039")
        self._prev_btn.setFixedSize(28, 26)
        self._prev_btn.clicked.connect(self._prev_sheet)
        toolbar.addWidget(self._prev_btn)

        self._title = QLabel("Arkusz 1/1")
        font = self._title.font()
        font.setPointSize(12)
        font.setBold(True)
        self._title.setFont(font)
        toolbar.addWidget(self._title)

        self._next_btn = QPushButton("\u203a")
        self._next_btn.setFixedSize(28, 26)
        self._next_btn.clicked.connect(self._next_sheet)
        toolbar.addWidget(self._next_btn)

        toolbar.addStretch()

        # Buttons (right)
        for text, slot, style in [
            ("Dodaj spad (S)", self._on_add_bleed, "toolbar-purple"),
            ("Obr\u00f3\u0107 180\u00b0 (R)", self._on_rotate_180, "toolbar-btn"),
            ("Dodaj FlexCut (Z)", self._on_add_flexcut, "toolbar-btn"),
            ("Zastosuj", self._on_apply, "success"),
            ("Wyczy\u015b\u0107", self._on_clear, "danger"),
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
        self._view = _FlexCutView(self._scene)
        self._view.setRenderHints(self._view.renderHints())
        self._view.setDragMode(QGraphicsView.DragMode.NoDrag)
        self._view.setBackgroundBrush(QColor("#e9ecef"))
        self._view.clicked.connect(self._on_view_click)
        self._view.area_selected.connect(self._on_area_select)
        self._placement_rects: list[QGraphicsRectItem] = []
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
            self._initial_fit_done = False
            self._render_current()
            self._update_nav()

    def _next_sheet(self):
        n = len(self.job.sheets) if self.job else 0
        if self.current_sheet_idx < n - 1:
            self.current_sheet_idx += 1
            self._selected.clear()
            self._initial_fit_done = False
            self._render_current()
            self._update_nav()

    def _update_nav(self):
        n = len(self.job.sheets) if self.job else 0
        self._prev_btn.setEnabled(self.current_sheet_idx > 0)
        self._next_btn.setEnabled(self.current_sheet_idx < n - 1)
        self._title.setText(f"Arkusz {self.current_sheet_idx + 1}/{n}")

    # --- Coordinate helpers ---

    def _mm_to_px(self, mm_val: float) -> float:
        from config import MM_TO_PT
        return mm_val * MM_TO_PT * self._scale

    def _mm_y_to_px(self, y_mm: float) -> float:
        """Konwersja mm (y-up) na piksele (y-down)."""
        from config import MM_TO_PT
        return (self._sheet_h_pt - y_mm * MM_TO_PT) * self._scale

    # --- Reexport wrapper ---

    def _safe_reexport(self, idx: int):
        """Wywoluje reexport z obsluga bledow."""
        if not self._reexport:
            return
        try:
            self._reexport(idx)
        except Exception:
            self._log(f"[BLAD] Re-export arkusz {idx + 1}:\n{traceback.format_exc()}")

    # --- Rendering ---

    def _render_current(self):
        saved_transform = self._view.transform() if self._initial_fit_done else None

        self._scene.clear()
        self._placement_rects = []
        idx = self.current_sheet_idx
        if not self.job or idx >= len(self.sheet_pdfs):
            return
        pp, cp = self.sheet_pdfs[idx]
        if not os.path.isfile(pp):
            return

        import fitz
        doc = fitz.open(pp)
        page = doc[0]
        mat = fitz.Matrix(self._scale, self._scale)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        qimg = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888)
        qpixmap = QPixmap.fromImage(qimg.copy())
        self._sheet_h_pt = page.rect.height
        doc.close()

        self._scene.addPixmap(qpixmap)

        # Overlay: cut PDF (jesli istnieje — pokazuje CutContour/FlexCut z eksportu)
        if os.path.isfile(cp):
            try:
                doc_cut = fitz.open(cp)
                page_cut = doc_cut[0]
                pix_cut = page_cut.get_pixmap(matrix=mat, alpha=True)
                qimg_cut = QImage(pix_cut.samples, pix_cut.width, pix_cut.height,
                                  pix_cut.stride, QImage.Format.Format_RGBA8888)
                qpixmap_cut = QPixmap.fromImage(qimg_cut.copy())
                doc_cut.close()
                overlay = self._scene.addPixmap(qpixmap_cut)
                overlay.setOpacity(0.7)
            except Exception:
                pass

        # Klikalne prostokaty naklejek (niewidoczne — do hit-test)
        sheet = self.job.sheets[idx]
        bleed2 = 2 * self.bleed_mm
        for i, p in enumerate(sheet.placements):
            rot = int(p.rotation_deg) % 360
            if rot in (90, 270):
                pw_mm = p.sticker.height_mm + bleed2
                ph_mm = p.sticker.width_mm + bleed2
            else:
                pw_mm = p.sticker.width_mm + bleed2
                ph_mm = p.sticker.height_mm + bleed2

            x_px = self._mm_to_px(p.x_mm)
            y_px = self._mm_y_to_px(p.y_mm + ph_mm)
            w_px = self._mm_to_px(pw_mm)
            h_px = self._mm_to_px(ph_mm)

            rect_item = QGraphicsRectItem(x_px, y_px, w_px, h_px)
            rect_item.setPen(QPen(Qt.PenStyle.NoPen))
            rect_item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            rect_item.setData(0, i)
            self._scene.addItem(rect_item)
            self._placement_rects.append(rect_item)

        # Overlay: panel_lines (linie FlexCut i full-cut)
        self._draw_panel_lines()

        # Overlay: selekcja
        self._draw_selection()

        if saved_transform is not None:
            self._view.setTransform(saved_transform)
        else:
            self._view.fitInView(self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
            self._initial_fit_done = True

    # --- Panel lines overlay ---

    def _draw_panel_lines(self):
        """Rysuje linie FlexCut (czerwone przerywane) i full-cut (zielone) na podgladzie."""
        if not self.job:
            return
        sheet = self.job.sheets[self.current_sheet_idx]
        if not sheet.panel_lines:
            return

        pen_flex = QPen(QColor(PREVIEW_FLEXCUT), 2.5, Qt.PenStyle.DashLine)
        pen_cut = QPen(QColor(PREVIEW_CUTCONTOUR), 2.0, Qt.PenStyle.SolidLine)

        for pl in sheet.panel_lines:
            is_flex = pl.bridge_length_mm > 0
            pen = pen_flex if is_flex else pen_cut

            if pl.axis == "horizontal":
                x1_px = self._mm_to_px(pl.start_mm)
                x2_px = self._mm_to_px(pl.end_mm)
                y_px = self._mm_y_to_px(pl.position_mm)
                line = QGraphicsLineItem(x1_px, y_px, x2_px, y_px)
            else:  # vertical
                y1_px = self._mm_y_to_px(pl.start_mm)
                y2_px = self._mm_y_to_px(pl.end_mm)
                x_px = self._mm_to_px(pl.position_mm)
                line = QGraphicsLineItem(x_px, y1_px, x_px, y2_px)

            line.setPen(pen)
            line.setData(1, "panel_line")
            self._scene.addItem(line)

    # --- Selection ---

    def _on_view_click(self, scene_pos: QPointF):
        """Klikniecie w view — zaznacz/odznacz naklejke."""
        items = self._scene.items(scene_pos)
        for item in items:
            if isinstance(item, QGraphicsRectItem) and item.data(0) is not None:
                idx = item.data(0)
                if idx in self._selected:
                    self._selected.discard(idx)
                else:
                    self._selected.add(idx)
                self._draw_selection()
                return
        # Klikniecie poza naklejka — wyczysc selekcje
        self._selected.clear()
        self._draw_selection()

    def _on_area_select(self, scene_rect: QRectF):
        """Rubber-band — zaznacz naklejki w prostokacie."""
        self._selected.clear()
        for i, rect_item in enumerate(self._placement_rects):
            if scene_rect.intersects(rect_item.rect()):
                self._selected.add(i)
        self._draw_selection()

    def _draw_selection(self):
        """Rysuje ramki wokol zaznaczonych naklejek."""
        for item in list(self._scene.items()):
            if isinstance(item, QGraphicsRectItem) and item.data(1) == "selection":
                self._scene.removeItem(item)

        for idx in self._selected:
            if idx < len(self._placement_rects):
                src = self._placement_rects[idx]
                r = src.rect()
                sel_item = QGraphicsRectItem(r)
                sel_item.setPen(QPen(QColor("#4f6ef7"), 3))
                sel_item.setBrush(QBrush(QColor(79, 110, 247, 40)))
                sel_item.setData(1, "selection")
                self._scene.addItem(sel_item)

        n = len(self._selected)
        self._log(f"Zaznaczono: {n} naklejek" if n else "")

    # --- Actions ---

    def _on_add_flexcut(self):
        """Dodaj FlexCut wokol zaznaczonych naklejek."""
        if not self._selected:
            self._log("FlexCut: zaznacz naklejki najpierw")
            return
        from models import PanelLine
        sheet = self.job.sheets[self.current_sheet_idx]
        bleed2 = 2 * self.bleed_mm
        sel = [sheet.placements[i] for i in self._selected if i < len(sheet.placements)]
        if not sel:
            return

        def _pw(p):
            return p.sticker.height_mm + bleed2 if int(p.rotation_deg) % 360 in (90, 270) else p.sticker.width_mm + bleed2
        def _ph(p):
            return p.sticker.width_mm + bleed2 if int(p.rotation_deg) % 360 in (90, 270) else p.sticker.height_mm + bleed2

        gap = getattr(sheet, 'gap_mm', 0)
        half_gap = gap / 2
        foot_x0 = min(p.x_mm for p in sel)
        foot_y0 = min(p.y_mm for p in sel)
        foot_x1 = max(p.x_mm + _pw(p) for p in sel)
        foot_y1 = max(p.y_mm + _ph(p) for p in sel)

        cx = (foot_x0 + foot_x1) / 2
        cy = (foot_y0 + foot_y1) / 2
        hw = (foot_x1 - foot_x0) / 2 + half_gap
        hh = (foot_y1 - foot_y0) / 2 + half_gap
        bx0, by0, bx1, by1 = cx - hw, cy - hh, cx + hw, cy + hh

        sheet.panel_lines.extend([
            PanelLine("horizontal", by0, bx0, bx1, bridge_length_mm=1.0),
            PanelLine("horizontal", by1, bx0, bx1, bridge_length_mm=1.0),
            PanelLine("vertical", bx0, by0, by1, bridge_length_mm=1.0),
            PanelLine("vertical", bx1, by0, by1, bridge_length_mm=1.0),
        ])
        self._selected.clear()
        self._safe_reexport(self.current_sheet_idx)
        self._render_cache.clear()
        self._render_current()
        self._log(f"FlexCut: dodano ({bx0:.1f},{by0:.1f})-({bx1:.1f},{by1:.1f})mm")

    def _on_rotate_180(self):
        if not self._selected or not self.job:
            self._log("Obr\u00f3\u0107 180\u00b0: brak zaznaczonych")
            return
        sheet = self.job.sheets[self.current_sheet_idx]
        for idx in self._selected:
            if idx < len(sheet.placements):
                p = sheet.placements[idx]
                p.rotation_deg = (p.rotation_deg + 180) % 360
        self._selected.clear()
        self._safe_reexport(self.current_sheet_idx)
        self._render_cache.clear()
        self._render_current()
        self._log("Obr\u00f3\u0107 180\u00b0: OK")

    def _on_add_bleed(self):
        if not self.job or not self.job.sheets:
            return
        sheet = self.job.sheets[self.current_sheet_idx]
        if not sheet.placements:
            return
        sheet.outer_bleed_mm = 2.0
        self._safe_reexport(self.current_sheet_idx)
        self._render_cache.clear()
        self._render_current()
        self._log("Spad: 2mm")

    def _on_apply(self):
        if not self.job:
            return
        for idx in range(len(self.job.sheets)):
            self._safe_reexport(idx)
        self._render_cache.clear()
        self._render_current()
        self._log("Zastosuj: wyeksportowano")

    def _on_clear(self):
        if not self.job:
            return
        sheet = self.job.sheets[self.current_sheet_idx]
        sheet.panel_lines = []
        sheet.outer_bleed_mm = 0.0
        self._selected.clear()
        self._safe_reexport(self.current_sheet_idx)
        self._render_cache.clear()
        self._render_current()
        self._log("Wyczyszczono")
