"""
Bleed Tool — flexcut_dialog.py
=================================
Dialog FlexCut: interaktywne zaznaczanie naklejek, rubber band, skróty Z/R/S.

Optymalizacja wydajności:
  - Cache print pixmap (renderowany raz, reużywany)
  - FlexCut add/clear: ZERO I/O — przerysowanie overlayów w pamięci
  - Swap marks: tylko cut PDF reexport (bez print)
  - Rotate/Bleed: pełny reexport (unavoidable — print się zmienia)
  - Zastosuj: pełny reexport wszystkich arkuszy
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

    def __init__(self, job, sheet_pdfs, bleed_mm,
                 reexport_fn=None, reexport_cut_fn=None,
                 reexport_fast_fn=None,
                 log_fn=None, parent=None):
        super().__init__(parent)
        self.setObjectName("FlexCutDialog")
        self.setWindowTitle("FlexCut")
        self.resize(1280, 780)
        self.setMinimumSize(1000, 600)

        self.job = job
        self.sheet_pdfs = sheet_pdfs
        self.bleed_mm = bleed_mm
        self._reexport = reexport_fn              # pełny (print+cut+white+marks)
        self._reexport_cut = reexport_cut_fn      # tylko cut PDF
        self._reexport_fast = reexport_fast_fn    # print+cut (bez white/marks)
        self._log = log_fn or (lambda msg: None)
        self.current_sheet_idx = 0
        self._selected: set[int] = set()
        self._initial_fit_done = False
        self._dpi = 150
        self._scale = self._dpi / 72.0
        self._sheet_h_pt = 0.0

        # Cache print pixmap — renderowany raz, reużywany dopóki print się nie zmieni
        self._cached_print_pixmap: QPixmap | None = None
        self._cached_print_idx: int = -1

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        # Toolbar
        toolbar = QHBoxLayout()
        toolbar.setSpacing(4)

        self._prev_btn = QPushButton("\u25C0")  # ◀ black left-pointing triangle
        self._prev_btn.setFixedSize(32, 28)
        self._prev_btn.setToolTip("Poprzedni arkusz")
        self._prev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._prev_btn.clicked.connect(self._prev_sheet)
        toolbar.addWidget(self._prev_btn)

        self._title = QLabel("Arkusz 1/1")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title.setMinimumWidth(90)
        font = self._title.font()
        font.setPointSize(12)
        font.setBold(True)
        self._title.setFont(font)
        toolbar.addWidget(self._title)

        self._next_btn = QPushButton("\u25B6")  # ▶ black right-pointing triangle
        self._next_btn.setFixedSize(32, 28)
        self._next_btn.setToolTip("Nastepny arkusz")
        self._next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_btn.clicked.connect(self._next_sheet)
        toolbar.addWidget(self._next_btn)

        # "Odwróć markery" — tylko JWEI
        self._marks_swapped = False
        self._swap_marks_btn = QPushButton("Odwróć markery")
        self._swap_marks_btn.setProperty("class", "toolbar-btn")
        self._swap_marks_btn.setCheckable(True)
        self._swap_marks_btn.clicked.connect(self._on_swap_marks)
        toolbar.addWidget(self._swap_marks_btn)
        if not (self.job and getattr(self.job, 'plotter', '') == 'jwei'):
            self._swap_marks_btn.setVisible(False)

        toolbar.addStretch()

        # Buttons (right) — mapowane na QSS variants
        for text, slot, variant in [
            ("Dodaj spad (S)", self._on_add_bleed, "secondary"),
            ("Obr\u00f3\u0107 180\u00b0 (R)", self._on_rotate_180, "ghost"),
            ("Dodaj FlexCut (Z)", self._on_add_flexcut, "ghost"),
            ("Zastosuj", self._on_apply, "success"),
            ("Wyczy\u015b\u0107", self._on_clear, "danger"),
            ("Zamknij", self.close, "ghost"),
        ]:
            btn = QPushButton(text)
            btn.setProperty("variant", variant)
            btn.setProperty("size", "sm")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
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

    # --- Reexport wrappers ---

    def _safe_reexport(self, idx: int):
        """Pełny reexport (print + cut + white) z obsługą błędów."""
        if not self._reexport:
            return
        try:
            self._reexport(idx)
        except Exception:
            self._log(f"[BŁĄD] Re-export arkusz {idx + 1}:\n{traceback.format_exc()}")

    def _safe_reexport_cut(self, idx: int):
        """Reexport TYLKO cut PDF z obsługą błędów."""
        fn = self._reexport_cut or self._reexport  # fallback na pełny
        if not fn:
            return
        try:
            fn(idx)
        except Exception:
            self._log(f"[BŁĄD] Re-export CUT arkusz {idx + 1}:\n{traceback.format_exc()}")

    def _safe_reexport_fast(self, idx: int):
        """Reexport print + cut (bez white/marks) z obsługą błędów."""
        fn = self._reexport_fast or self._reexport  # fallback na pełny
        if not fn:
            return
        try:
            fn(idx)
        except Exception:
            self._log(f"[BŁĄD] Re-export FAST arkusz {idx + 1}:\n{traceback.format_exc()}")

    # --- Rendering ---

    def _render_current(self, rerender_print: bool = True):
        """Renderuje podgląd arkusza.

        Args:
            rerender_print: True = renderuj print PDF od nowa (po rotate/bleed).
                            False = użyj cached pixmap (po FlexCut/marks).
        """
        saved_transform = self._view.transform() if self._initial_fit_done else None

        self._scene.clear()
        self._placement_rects = []
        idx = self.current_sheet_idx
        if not self.job or idx >= len(self.sheet_pdfs):
            return
        pp, cp = self.sheet_pdfs[idx]

        import fitz

        # --- Print pixmap (tło) — z cache jeśli możliwe ---
        need_print_render = (
            rerender_print
            or self._cached_print_pixmap is None
            or self._cached_print_idx != idx
        )
        if need_print_render:
            if not os.path.isfile(pp):
                return
            doc = fitz.open(pp)
            page = doc[0]
            mat = fitz.Matrix(self._scale, self._scale)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            qimg = QImage(pix.samples, pix.width, pix.height, pix.stride,
                          QImage.Format.Format_RGB888)
            self._cached_print_pixmap = QPixmap.fromImage(qimg.copy())
            self._sheet_h_pt = page.rect.height
            self._cached_print_idx = idx
            doc.close()

        self._scene.addPixmap(self._cached_print_pixmap)

        # --- Cut overlay (zawsze renderowany — może się zmienić) ---
        if os.path.isfile(cp):
            try:
                doc_cut = fitz.open(cp)
                page_cut = doc_cut[0]
                mat = fitz.Matrix(self._scale, self._scale)
                pix_cut = page_cut.get_pixmap(matrix=mat, alpha=True)
                qimg_cut = QImage(pix_cut.samples, pix_cut.width, pix_cut.height,
                                  pix_cut.stride, QImage.Format.Format_RGBA8888)
                qpixmap_cut = QPixmap.fromImage(qimg_cut.copy())
                doc_cut.close()
                overlay = self._scene.addPixmap(qpixmap_cut)
                overlay.setOpacity(0.7)
            except Exception:
                pass

        # Klikalne prostokąty naklejek (niewidoczne — do hit-test)
        self._build_hit_rects()

        # Overlay: panel_lines (linie FlexCut i full-cut)
        self._draw_panel_lines()

        # Overlay: selekcja
        self._draw_selection()

        if saved_transform is not None:
            self._view.setTransform(saved_transform)
        else:
            self._view.fitInView(self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
            self._initial_fit_done = True

    def _refresh_overlays(self):
        """Szybkie przerysowanie overlayów (panel lines + selekcja) BEZ renderowania PDF.

        ~0ms — zero I/O, zero PDF. Używane przy dodawaniu/usuwaniu FlexCut linii.
        """
        # Usuń stare panel lines i selection z sceny
        for item in list(self._scene.items()):
            d1 = item.data(1)
            if d1 in ("selection", "panel_line"):
                self._scene.removeItem(item)
        self._draw_panel_lines()
        self._draw_selection()

    def _build_hit_rects(self):
        """Buduje niewidoczne prostokąty do klikania naklejek."""
        sheet = self.job.sheets[self.current_sheet_idx]
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
        """Rubber-band — zaznacz naklejki w prostokącie (agregacja z poprzednim zaznaczeniem)."""
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

    def _on_swap_marks(self):
        """Odwróć markery JWEI: zamień mark_offset_x_mm i mark_offset_y_mm.

        TYLKO dla JWEI — nigdy nie modyfikuje konfiguracji Summa S3.
        Summa S3 ma osobny algorytm markerów (OPOS) z precyzyjnymi parametrami
        wymaganymi przez GoSign — nie podlega żadnym modyfikacjom.
        """
        if not self.job or self.job.plotter != 'jwei':
            self._log("Odwróć markery: dostępne tylko dla JWEI")
            return
        from config import PLOTTERS
        jwei = PLOTTERS.get('jwei')
        if not jwei:
            return
        self._marks_swapped = not self._marks_swapped
        x, y = jwei['mark_offset_x_mm'], jwei['mark_offset_y_mm']
        jwei['mark_offset_x_mm'], jwei['mark_offset_y_mm'] = y, x
        self._swap_marks_btn.setChecked(self._marks_swapped)
        label = f"Markery: {jwei['mark_offset_x_mm']}/{jwei['mark_offset_y_mm']}"
        self._swap_marks_btn.setText(label)
        # Re-generuj markery JWEI i reexport (marks są na print I cut PDF)
        from modules.marks import generate_marks
        for i, sh in enumerate(self.job.sheets):
            generate_marks(sh, 'jwei')  # jawnie JWEI — nigdy summa
            self._safe_reexport_fast(i)
        self._invalidate_print_cache()
        self._render_current(rerender_print=True)
        self._log(f"Markery JWEI: X={jwei['mark_offset_x_mm']}mm, Y={jwei['mark_offset_y_mm']}mm")

    def _on_add_flexcut(self):
        """Dodaj FlexCut wokol zaznaczonych naklejek.

        SZYBKA ŚCIEŻKA: zero I/O — tylko modyfikacja danych + przerysowanie overlayów.
        Cut PDF zostanie wygenerowany przy 'Zastosuj'.
        """
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
        # Szybka ścieżka: tylko przerysuj overlaye (zero I/O)
        self._refresh_overlays()
        self._log(f"FlexCut: dodano ({bx0:.1f},{by0:.1f})-({bx1:.1f},{by1:.1f})mm")

    def _on_rotate_180(self):
        """Obróć zaznaczone naklejki o 180°.

        FAST REEXPORT: print+cut (bez white, bez marks regen).
        """
        if not self._selected or not self.job:
            self._log("Obr\u00f3\u0107 180\u00b0: brak zaznaczonych")
            return
        sheet = self.job.sheets[self.current_sheet_idx]
        for idx in self._selected:
            if idx < len(sheet.placements):
                p = sheet.placements[idx]
                p.rotation_deg = (p.rotation_deg + 180) % 360
        self._selected.clear()
        self._safe_reexport_fast(self.current_sheet_idx)
        self._invalidate_print_cache()
        self._render_current(rerender_print=True)
        self._log("Obr\u00f3\u0107 180\u00b0: OK")

    def _on_add_bleed(self):
        """Dodaj zewnętrzny spad wokół grupy naklejek.

        PEŁNY REEXPORT: print PDF się zmienia (dilation).
        """
        if not self.job or not self.job.sheets:
            return
        sheet = self.job.sheets[self.current_sheet_idx]
        if not sheet.placements:
            return
        sheet.outer_bleed_mm = 2.0
        self._safe_reexport_fast(self.current_sheet_idx)
        self._invalidate_print_cache()
        self._render_current(rerender_print=True)
        self._log("Spad: 2mm")

    def _on_apply(self):
        """Zastosuj: pełny reexport wszystkich arkuszy (finalna wersja PDF)."""
        if not self.job:
            return
        for idx in range(len(self.job.sheets)):
            self._safe_reexport(idx)
        self._log("Zastosuj: wyeksportowano")
        self.accept()

    def _on_clear(self):
        """Wyczyść FlexCut linie i spad."""
        if not self.job:
            return
        sheet = self.job.sheets[self.current_sheet_idx]
        had_bleed = sheet.outer_bleed_mm > 0
        sheet.panel_lines = []
        sheet.outer_bleed_mm = 0.0
        self._selected.clear()
        if had_bleed:
            # Print się zmienił (usunięcie bleed) → fast reexport
            self._safe_reexport_fast(self.current_sheet_idx)
            self._invalidate_print_cache()
            self._render_current(rerender_print=True)
        else:
            # Tylko panel lines → szybka ścieżka
            self._refresh_overlays()
        self._log("Wyczyszczono")

    # --- Cache management ---

    def _invalidate_print_cache(self):
        """Unieważnia cache print pixmap — wymusza ponowne renderowanie."""
        self._cached_print_pixmap = None
        self._cached_print_idx = -1
