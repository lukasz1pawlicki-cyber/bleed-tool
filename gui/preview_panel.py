"""
Bleed Tool — preview_panel.py
================================
Podgląd PDF na QGraphicsView z zoom/pan.
Tryb crop: podgląd obrazu z overlayem kształtu crop + drag offset.
"""

import os
import logging
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGraphicsView, QGraphicsScene, QGraphicsPixmapItem,
    QGraphicsEllipseItem, QGraphicsRectItem,
    QSizePolicy,
)
from PyQt6.QtCore import Qt, QRectF, pyqtSignal
from PyQt6.QtGui import (
    QPixmap, QImage, QPen, QColor, QBrush, QWheelEvent, QMouseEvent,
    QPainterPath,
)

from gui.theme import PREVIEW_CUTCONTOUR, PREVIEW_FLEXCUT, PREVIEW_MARK

log = logging.getLogger(__name__)


class _PDFGraphicsView(QGraphicsView):
    """QGraphicsView z zoom (scroll) i pan (prawy przycisk / środkowy).
    Tryb crop: lewy przycisk drag → przesuwanie offsetu crop.
    """

    crop_drag = pyqtSignal(float, float)  # (delta_x_ratio, delta_y_ratio)

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
        self._crop_mode = False      # aktywny crop preview
        self._crop_dragging = False
        self._crop_drag_start = None
        self._crop_dim = 0           # rozmiar crop area w px (do przeliczenia delta)

    def set_crop_mode(self, enabled: bool, crop_dim: int = 0):
        self._crop_mode = enabled
        self._crop_dim = crop_dim

    def wheelEvent(self, event: QWheelEvent):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() in (Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton):
            self._panning = True
            self._pan_start = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif event.button() == Qt.MouseButton.LeftButton and self._crop_mode:
            self._crop_dragging = True
            self._crop_drag_start = event.position()
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
        elif self._crop_dragging and self._crop_drag_start is not None:
            delta = event.position() - self._crop_drag_start
            self._crop_drag_start = event.position()
            # Przelicz piksele ekranu na ratio (0..1)
            if self._crop_dim > 0:
                # Uwzglednij zoom (transform scale)
                scale = self.transform().m11()
                dx_ratio = delta.x() / (self._crop_dim * scale)
                dy_ratio = delta.y() / (self._crop_dim * scale)
                self.crop_drag.emit(dx_ratio, dy_ratio)
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() in (Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton):
            self._panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
        elif event.button() == Qt.MouseButton.LeftButton and self._crop_dragging:
            self._crop_dragging = False
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
    """Panel podglądu PDF z nawigacją, legendą, zoom/pan.
    Tryb crop: wyświetla obraz z overlayem kształtu crop.
    """

    crop_offset_changed = pyqtSignal(str, tuple)  # (filepath, (ox, oy))

    def __init__(self, parent=None):
        super().__init__(parent)
        self._results: list[dict] = []
        self._job = None
        self._sheet_pdfs: list[tuple] = []
        self._bleed_mm: float = 0.0
        self._current_idx: int = 0
        self._cache: dict = {}
        self._crop_data: dict = {}  # aktywny crop preview
        self._split_view: bool = False  # True = podgląd przed/po side-by-side

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Toolbar: nawigacja
        toolbar = QHBoxLayout()
        toolbar.setSpacing(4)

        self._prev_btn = QPushButton("<")
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

        self._next_btn = QPushButton(">")
        self._next_btn.setFixedSize(28, 26)
        self._next_btn.clicked.connect(self._next)
        toolbar.addWidget(self._next_btn)

        # Split view toggle — podgląd przed/po side-by-side (tylko bleed mode)
        self._split_btn = QPushButton("Przed/Po")
        self._split_btn.setCheckable(True)
        self._split_btn.setFixedHeight(26)
        self._split_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._split_btn.setToolTip(
            "Podgląd side-by-side: oryginał (lewo) vs wynik z bleedem (prawo)"
        )
        self._split_btn.clicked.connect(self._on_toggle_split)
        self._split_btn.setVisible(False)  # widoczny tylko gdy _results
        toolbar.addWidget(self._split_btn)

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
        self._view.crop_drag.connect(self._on_crop_drag)
        layout.addWidget(self._view, stretch=1)

        # Placeholder (overlay na view — center)
        self._placeholder = QLabel("Przeciągnij pliki i kliknij\n\"Generuj bleed\" aby zobaczyć podgląd")
        self._placeholder.setObjectName("preview-placeholder")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self._placeholder, stretch=1)
        self._view.setVisible(False)

    # --- Public API ---

    def show_bleed_results(self, paths: list[str],
                           input_paths: list[str] | None = None,
                           input_infos: list[tuple[str, int]] | None = None):
        """Pokaż podgląd plików bleed.

        Args:
            paths: output PDF paths (wyniki)
            input_paths: (legacy) oryginalne pliki wejściowe — padding None
                gdy mniej niż paths (mylące przy wielostronicowych)
            input_infos: [(src_path, page_idx), ...] — parallel do paths.
                Preferowane: każdy output ma dokładnie jeden odpowiadający
                source input + page_idx (dla wielostronicowych PDF).
                Ma pierwszeństwo przed input_paths.
        """
        self._job = None
        self._sheet_pdfs = []
        self._results = []
        self._current_idx = 0
        self._cache.clear()

        import fitz

        # Normalizuj do listy (src_path, page_idx) | (None, 0)
        normalized: list[tuple[str | None, int]] = []
        if input_infos is not None:
            # Nowy API — parallel do paths
            for i in range(len(paths)):
                if i < len(input_infos) and input_infos[i] is not None:
                    src, pidx = input_infos[i]
                    normalized.append((src, int(pidx)))
                else:
                    normalized.append((None, 0))
        elif input_paths is not None:
            # Legacy API — pojedyncza strona (page_idx=0)
            padded = list(input_paths) + [None] * max(0, len(paths) - len(input_paths))
            for inp in padded[:len(paths)]:
                normalized.append((inp, 0))
        else:
            normalized = [(None, 0)] * len(paths)

        for p, (inp, page_idx) in zip(paths, normalized):
            try:
                doc = fitz.open(p)
                page = doc[0]
                w_mm = page.rect.width * 25.4 / 72.0
                h_mm = page.rect.height * 25.4 / 72.0
                doc.close()
                self._results.append({
                    "path": p,
                    "input_path": inp,
                    "input_page_idx": page_idx,
                    "label": os.path.basename(p),
                    "size_mm": (w_mm, h_mm),
                })
            except Exception:
                pass

        # Split-view dostępny tylko gdy mamy input paths
        has_inputs = any(r.get("input_path") for r in self._results)
        self._split_btn.setVisible(has_inputs)
        if not has_inputs:
            self._split_btn.setChecked(False)
            self._split_view = False

        self._update_nav()
        self._render_current()

    def _on_toggle_split(self, checked: bool):
        """Przełącznik trybu split-view (przed/po)."""
        self._split_view = checked
        self._render_current()

    def show_nest_job(self, job, sheet_pdfs, bleed_mm):
        """Pokaż podgląd arkuszy nestingu."""
        self._results = []
        self._job = job
        self._sheet_pdfs = sheet_pdfs
        self._bleed_mm = bleed_mm
        self._current_idx = 0
        self._cache.clear()
        # Split-view ma sens tylko w trybie bleed (przed/po oryginału)
        self._split_btn.setVisible(False)
        self._split_btn.setChecked(False)
        self._split_view = False
        self._update_nav()
        self._render_current()

    def clear(self):
        self._results = []
        self._job = None
        self._sheet_pdfs = []
        self._cache.clear()
        self._scene.clear()
        self._split_btn.setVisible(False)
        self._split_btn.setChecked(False)
        self._split_view = False
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
                self._info_label.setText(f"{w:.0f}x{h:.0f}mm")
            elif self._job:
                sheet = self._job.sheets[idx]
                placed = len(sheet.placements)
                self._title_label.setText(f"Arkusz {idx + 1}/{n}")
                self._info_label.setText(
                    f"{sheet.width_mm:.0f}x{sheet.height_mm:.0f}mm | {placed} szt"
                )
        else:
            self._title_label.setText("Podgląd")
            self._info_label.setText("")

    # --- Rendering ---

    def _render_current(self):
        """Renderuje aktualny PDF do sceny."""
        self._scene.clear()
        self._view.set_crop_mode(False)
        has_content = bool(self._results) or bool(self._job and self._job.sheets)
        self._view.setVisible(has_content)
        self._placeholder.setVisible(not has_content)
        if not has_content:
            return
        idx = self._current_idx

        if self._results and idx < len(self._results):
            self._render_bleed(idx)
        elif self._job and self._sheet_pdfs and idx < len(self._sheet_pdfs):
            self._render_sheet(idx)

        self._view.fit_content()

    def _render_bleed(self, idx: int):
        result = self._results[idx]
        path = result["path"]
        input_path = result.get("input_path")
        input_page_idx = result.get("input_page_idx", 0)

        # Split view: pokaż input (lewo) + output (prawo)
        if self._split_view and input_path:
            self._render_split(input_path, path, input_page_idx)
            return

        qpix = self._render_pdf_page(path, dpi=150)
        if qpix:
            self._scene.addPixmap(qpix)

    def _render_split(self, input_path: str, output_path: str,
                      input_page_idx: int = 0):
        """Renderuje podgląd side-by-side: oryginał vs wynik z bleedem."""
        # Output (PDF z bleedem)
        out_pix = self._render_pdf_page(output_path, dpi=150)
        # Input — obsługuje PDF/SVG/EPS/raster (page_idx dla wielostronicowych)
        in_pix = self._render_input_file(input_path, page_idx=input_page_idx)

        if not out_pix and not in_pix:
            return

        # Wspólny rozmiar — skaluj obrazki do tej samej wysokości (max)
        # dla lepszego porównania
        if in_pix and out_pix:
            max_h = max(in_pix.height(), out_pix.height())
            if in_pix.height() != max_h:
                in_pix = in_pix.scaledToHeight(max_h, Qt.TransformationMode.SmoothTransformation)
            if out_pix.height() != max_h:
                out_pix = out_pix.scaledToHeight(max_h, Qt.TransformationMode.SmoothTransformation)

        gap_px = 20  # odstęp między panelami
        x_offset = 0

        # Input (lewo) + etykieta
        if in_pix:
            item_in = self._scene.addPixmap(in_pix)
            item_in.setPos(0, 30)  # +30 na etykietę
            label_in = self._scene.addText("PRZED (oryginał)")
            label_in.setDefaultTextColor(QColor("#333"))
            label_in.setPos(0, 0)
            x_offset = in_pix.width() + gap_px

        # Output (prawo) + etykieta
        if out_pix:
            item_out = self._scene.addPixmap(out_pix)
            item_out.setPos(x_offset, 30)
            label_out = self._scene.addText("PO (z bleedem)")
            label_out.setDefaultTextColor(QColor("#333"))
            label_out.setPos(x_offset, 0)

    def _render_input_file(self, path: str, max_size: int = 600,
                           page_idx: int = 0) -> QPixmap | None:
        """Renderuje plik wejściowy (PDF/SVG/EPS/raster) jako QPixmap.

        Samodzielna implementacja (bez importu crop.py), żeby nie wymagać
        libcairo (cairosvg crashuje import crop.py gdy brak systemowej cairo).

        page_idx: dla wielostronicowych PDF — którą stronę renderować.
        """
        mtime = os.path.getmtime(path) if os.path.isfile(path) else 0
        cache_key = ("input", path, page_idx, mtime)
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            pil = self._load_input_as_pil(path, max_size, page_idx=page_idx)
            if pil is None:
                return None
            rgb_data = pil.convert("RGB").tobytes()
            w, h = pil.size
            qimg = QImage(rgb_data, w, h, w * 3, QImage.Format.Format_RGB888)
            qpix = QPixmap.fromImage(qimg.copy())

            if len(self._cache) > 10:
                self._cache.pop(next(iter(self._cache)))
            self._cache[cache_key] = qpix
            return qpix
        except Exception as e:
            log.warning(f"Render input failed: {path}: {e}")
            return None

    def _load_input_as_pil(self, path: str, max_size: int, page_idx: int = 0):
        """Ładuje plik wejściowy jako PIL.Image (RGB).

        Obsługuje PDF/AI (fitz), raster (PIL.Image.open), SVG (cairosvg — fallback),
        EPS (ghostscript_bridge.eps_to_pdf → fitz).

        page_idx: dla PDF — którą stronę (dla rastrów/SVG ignorowane).
        """
        from PIL import Image
        ext = os.path.splitext(path)[1].lower()

        RASTER = ('.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp', '.webp')
        if ext in RASTER:
            img = Image.open(path).convert("RGB")
            w, h = img.size
            if max(w, h) > max_size:
                scale = max_size / max(w, h)
                img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            return img

        if ext == ".svg":
            try:
                import cairosvg
                import io as _io
                png_data = cairosvg.svg2png(url=path, output_width=max_size)
                return Image.open(_io.BytesIO(png_data)).convert("RGB")
            except Exception as e:
                log.warning(f"SVG preview render failed ({e}) — placeholder")
                return Image.new("RGB", (max_size, max_size), (230, 230, 230))

        if ext in (".eps", ".epsf"):
            try:
                from modules.ghostscript_bridge import eps_to_pdf
                tmp_pdf = eps_to_pdf(path)
                img = self._render_pdf_as_pil(tmp_pdf, max_size, page_idx=page_idx)
                try:
                    os.unlink(tmp_pdf)
                except OSError:
                    pass
                return img
            except Exception as e:
                log.warning(f"EPS preview render failed ({e}) — placeholder")
                return Image.new("RGB", (max_size, max_size), (230, 230, 230))

        # Default: PDF / AI / unknown — próba fitz
        return self._render_pdf_as_pil(path, max_size, page_idx=page_idx)

    def _render_pdf_as_pil(self, path: str, max_size: int, page_idx: int = 0):
        """Renderuje wybraną stronę PDF przez fitz i zwraca PIL.Image."""
        import fitz
        from PIL import Image
        doc = fitz.open(path)
        try:
            # Bezpieczny clamp — brak strony → fallback na 0
            pidx = page_idx if 0 <= page_idx < len(doc) else 0
            page = doc[pidx]
            zoom = max_size / max(page.rect.width, page.rect.height)
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        finally:
            doc.close()

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

    # --- Crop preview ---

    def show_crop_preview(self, data: dict):
        """Pokaż podgląd crop z overlayem kształtu.

        data: {"file", "shape", "offset", "radius_pct"} lub {} aby wyłączyć.
        """
        self._crop_data = data
        if not data:
            # Wyłącz crop preview — przywróć normalny podgląd
            if self._results:
                self._render_current()
            return

        filepath = data["file"]
        shape = data["shape"]
        ox, oy = data.get("offset", (0.5, 0.5))
        radius_pct = data.get("radius_pct", 9)

        # Pokaż view
        self._view.setVisible(True)
        self._placeholder.setVisible(False)
        self._scene.clear()

        # Załaduj obraz
        try:
            from modules.crop import load_preview_image
            from PIL import Image as PILImage
            pil_img = load_preview_image(filepath, max_size=600)
            src_w, src_h = pil_img.size

            # Crop area — kwadrat (bok = min wymiar * 0.85)
            crop_dim = int(min(src_w, src_h) * 0.85)

            # Skaluj obraz aby pokrył crop area (cover)
            scale = max(crop_dim / src_w, crop_dim / src_h)
            disp_w = int(src_w * scale)
            disp_h = int(src_h * scale)
            pil_img = pil_img.resize((disp_w, disp_h), PILImage.LANCZOS)

            # Pozycja obrazu (offset pan)
            pan_x = max(0, disp_w - crop_dim)
            pan_y = max(0, disp_h - crop_dim)
            img_x = -int(ox * pan_x)
            img_y = -int(oy * pan_y)

            # Konwertuj PIL -> QPixmap
            rgb_data = pil_img.convert("RGB").tobytes()
            qimg = QImage(rgb_data, disp_w, disp_h, disp_w * 3,
                          QImage.Format.Format_RGB888)
            qpix = QPixmap.fromImage(qimg)

            # Dodaj obraz do sceny
            img_item = self._scene.addPixmap(qpix)
            img_item.setPos(img_x, img_y)

            # Overlay — przyciemnij poza crop
            dim_brush = QBrush(QColor(0, 0, 0, 120))
            no_pen = QPen(Qt.PenStyle.NoPen)

            # 4 prostokąty wokół crop area
            self._scene.addRect(QRectF(img_x, img_y, disp_w, -img_y),
                                no_pen, dim_brush)  # góra
            self._scene.addRect(QRectF(img_x, crop_dim, disp_w, disp_h - crop_dim + img_y),
                                no_pen, dim_brush)  # dół
            self._scene.addRect(QRectF(img_x, 0, -img_x, crop_dim),
                                no_pen, dim_brush)  # lewo
            self._scene.addRect(QRectF(crop_dim, 0, disp_w - crop_dim + img_x, crop_dim),
                                no_pen, dim_brush)  # prawo

            # Ramka crop
            frame_pen = QPen(QColor(255, 255, 255, 200), 2)
            if shape == "circle":
                self._scene.addEllipse(QRectF(0, 0, crop_dim, crop_dim), frame_pen)
            elif shape == "rounded":
                r = crop_dim * radius_pct / 100
                path = QPainterPath()
                path.addRoundedRect(QRectF(0, 0, crop_dim, crop_dim), r, r)
                self._scene.addPath(path, frame_pen)
            else:
                self._scene.addRect(QRectF(0, 0, crop_dim, crop_dim), frame_pen)

            # Ustaw crop mode na view (drag do przesuwania)
            self._view.set_crop_mode(True, crop_dim)

            # Info
            self._title_label.setText("Crop")
            self._info_label.setText(f"{shape} | offset ({ox:.2f}, {oy:.2f})")

            self._view.fit_content()
        except Exception as e:
            log.warning(f"Crop preview failed: {e}")

    def _on_crop_drag(self, dx_ratio: float, dy_ratio: float):
        """Drag w crop preview → aktualizuj offset i odśwież."""
        if not self._crop_data:
            return
        filepath = self._crop_data.get("file")
        if not filepath:
            return
        ox, oy = self._crop_data.get("offset", (0.5, 0.5))
        # Drag w lewo/górę → mniejszy offset (obraz przesuwa się w prawo/dół)
        ox = max(0.0, min(1.0, ox + dx_ratio))
        oy = max(0.0, min(1.0, oy + dy_ratio))
        self._crop_data["offset"] = (ox, oy)
        # Emituj zmianę do bleed_tab
        self.crop_offset_changed.emit(filepath, (ox, oy))
        # Odśwież podgląd
        self.show_crop_preview(self._crop_data)
