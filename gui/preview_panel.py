"""
Bleed Tool — preview_panel.py
================================
Podgląd PDF na QGraphicsView z zoom/pan.
"""

import os
import logging
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGraphicsView, QGraphicsScene, QGraphicsPixmapItem,
    QSizePolicy,
)
from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QPixmap, QImage, QPen, QColor, QWheelEvent, QMouseEvent

from gui.theme import PREVIEW_CUTCONTOUR, PREVIEW_FLEXCUT, PREVIEW_MARK

log = logging.getLogger(__name__)


class _PDFGraphicsView(QGraphicsView):
    """QGraphicsView z zoom (scroll) i pan (prawy przycisk / środkowy)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        from PyQt6.QtGui import QPainter
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setBackgroundBrush(QColor("#e9ecef"))
        self._panning = False
        self._pan_start = None

    def wheelEvent(self, event: QWheelEvent):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() in (Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton):
            self._panning = True
            self._pan_start = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._panning and self._pan_start is not None:
            delta = event.position() - self._pan_start
            self._pan_start = event.position()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - int(delta.x())
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - int(delta.y())
            )
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() in (Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton):
            self._panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
        else:
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        """Double-click → fit in view."""
        if self.scene() and self.scene().sceneRect():
            self.fitInView(self.scene().sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def fit_content(self):
        if self.scene():
            self.fitInView(self.scene().sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)


class PreviewPanel(QWidget):
    """Panel podglądu PDF z nawigacją, legendą, zoom/pan."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._results: list[dict] = []
        self._job = None
        self._sheet_pdfs: list[tuple] = []
        self._bleed_mm: float = 0.0
        self._current_idx: int = 0
        self._cache: dict = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Toolbar: nawigacja
        toolbar = QHBoxLayout()
        toolbar.setSpacing(4)

        self._prev_btn = QPushButton("‹")
        self._prev_btn.setFixedSize(28, 26)
        self._prev_btn.clicked.connect(self._prev)
        toolbar.addWidget(self._prev_btn)

        self._title_label = QLabel("Podgląd")
        self._title_label.setProperty("class", "header")
        font = self._title_label.font()
        font.setPointSize(12)
        font.setBold(True)
        self._title_label.setFont(font)
        toolbar.addWidget(self._title_label)

        self._next_btn = QPushButton("›")
        self._next_btn.setFixedSize(28, 26)
        self._next_btn.clicked.connect(self._next)
        toolbar.addWidget(self._next_btn)

        toolbar.addStretch()

        self._info_label = QLabel("")
        self._info_label.setProperty("class", "subheader")
        font2 = self._info_label.font()
        font2.setPointSize(9)
        self._info_label.setFont(font2)
        toolbar.addWidget(self._info_label)

        layout.addLayout(toolbar)

        # Legenda
        legend = QHBoxLayout()
        legend.setSpacing(4)
        legend.setContentsMargins(4, 0, 0, 0)
        for color, label in [
            (PREVIEW_CUTCONTOUR, "Cut"),
            (PREVIEW_FLEXCUT, "Flex"),
            (PREVIEW_MARK, "OPOS"),
        ]:
            dot = QLabel()
            dot.setFixedSize(8, 8)
            dot.setStyleSheet(f"background: {color}; border-radius: 4px;")
            legend.addWidget(dot)
            lbl = QLabel(label)
            lbl.setProperty("class", "legend-label")
            font3 = lbl.font()
            font3.setPointSize(8)
            lbl.setFont(font3)
            legend.addWidget(lbl)
            legend.addSpacing(4)
        legend.addStretch()
        layout.addLayout(legend)

        # Graphics View
        self._scene = QGraphicsScene()
        self._view = _PDFGraphicsView()
        self._view.setScene(self._scene)
        layout.addWidget(self._view, stretch=1)

    # --- Public API ---

    def show_bleed_results(self, paths: list[str]):
        """Pokaż podgląd plików bleed."""
        self._job = None
        self._sheet_pdfs = []
        self._results = []
        self._current_idx = 0
        self._cache.clear()

        import fitz
        for p in paths:
            try:
                doc = fitz.open(p)
                page = doc[0]
                w_mm = page.rect.width * 25.4 / 72.0
                h_mm = page.rect.height * 25.4 / 72.0
                doc.close()
                self._results.append({
                    "path": p,
                    "label": os.path.basename(p),
                    "size_mm": (w_mm, h_mm),
                })
            except Exception:
                pass

        self._update_nav()
        self._render_current()

    def show_nest_job(self, job, sheet_pdfs, bleed_mm):
        """Pokaż podgląd arkuszy nestingu."""
        self._results = []
        self._job = job
        self._sheet_pdfs = sheet_pdfs
        self._bleed_mm = bleed_mm
        self._current_idx = 0
        self._cache.clear()
        self._update_nav()
        self._render_current()

    def clear(self):
        self._results = []
        self._job = None
        self._sheet_pdfs = []
        self._cache.clear()
        self._scene.clear()
        self._update_nav()

    # --- Navigation ---

    def _prev(self):
        if self._current_idx > 0:
            self._current_idx -= 1
            self._render_current()
            self._update_nav()

    def _next(self):
        max_idx = self._max_idx()
        if self._current_idx < max_idx - 1:
            self._current_idx += 1
            self._render_current()
            self._update_nav()

    def _max_idx(self) -> int:
        if self._results:
            return len(self._results)
        if self._job and self._job.sheets:
            return len(self._job.sheets)
        return 0

    def _update_nav(self):
        n = self._max_idx()
        has = n > 0
        self._prev_btn.setEnabled(has and self._current_idx > 0)
        self._next_btn.setEnabled(has and self._current_idx < n - 1)

        if has:
            idx = self._current_idx
            if self._results:
                r = self._results[idx]
                self._title_label.setText(f"Podgląd {idx + 1}/{n}")
                w, h = r["size_mm"]
                self._info_label.setText(f"{w:.0f}×{h:.0f}mm")
            elif self._job:
                sheet = self._job.sheets[idx]
                placed = len(sheet.placements)
                self._title_label.setText(f"Arkusz {idx + 1}/{n}")
                self._info_label.setText(
                    f"{sheet.width_mm:.0f}×{sheet.height_mm:.0f}mm | {placed} szt"
                )
        else:
            self._title_label.setText("Podgląd")
            self._info_label.setText("")

    # --- Rendering ---

    def _render_current(self):
        """Renderuje aktualny PDF do sceny."""
        self._scene.clear()
        idx = self._current_idx

        if self._results and idx < len(self._results):
            self._render_bleed(idx)
        elif self._job and self._sheet_pdfs and idx < len(self._sheet_pdfs):
            self._render_sheet(idx)

        self._view.fit_content()

    def _render_bleed(self, idx: int):
        path = self._results[idx]["path"]
        qpix = self._render_pdf_page(path, dpi=150)
        if qpix:
            self._scene.addPixmap(qpix)

    def _render_sheet(self, idx: int):
        pp, cp = self._sheet_pdfs[idx]

        # Print PDF
        print_pix = self._render_pdf_page(pp, dpi=150)
        if print_pix:
            self._scene.addPixmap(print_pix)

        # Cut overlay (jeśli istnieje)
        if os.path.isfile(cp):
            cut_pix = self._render_pdf_page(cp, dpi=150, alpha=True)
            if cut_pix:
                item = self._scene.addPixmap(cut_pix)
                item.setOpacity(0.8)

    def _render_pdf_page(self, path: str, dpi: int = 150, alpha: bool = False) -> QPixmap | None:
        """Renderuje pierwszą stronę PDF jako QPixmap."""
        cache_key = (path, dpi, alpha, os.path.getmtime(path) if os.path.isfile(path) else 0)
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            import fitz
            doc = fitz.open(path)
            page = doc[0]
            mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
            pix = page.get_pixmap(matrix=mat, alpha=alpha)

            if alpha:
                fmt = QImage.Format.Format_RGBA8888
                qimg = QImage(pix.samples, pix.width, pix.height, pix.stride, fmt)
            else:
                fmt = QImage.Format.Format_RGB888
                qimg = QImage(pix.samples, pix.width, pix.height, pix.stride, fmt)

            qpixmap = QPixmap.fromImage(qimg.copy())  # .copy() bo fitz samples jest tymczasowe
            doc.close()

            # Cache (max 10)
            if len(self._cache) > 10:
                self._cache.pop(next(iter(self._cache)))
            self._cache[cache_key] = qpixmap
            return qpixmap
        except Exception as e:
            log.warning(f"Render PDF failed: {path}: {e}")
            return None
