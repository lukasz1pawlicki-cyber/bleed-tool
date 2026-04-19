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
    QSizePolicy, QGridLayout,
)
from PyQt6.QtCore import Qt, QRectF, pyqtSignal, QObject, QThread, pyqtSlot
from PyQt6.QtGui import (
    QPixmap, QImage, QPen, QColor, QBrush, QWheelEvent, QMouseEvent,
    QPainterPath,
)


def apply_softproof_fogra39(pil_img):
    """Symuluje wygląd CMYK FOGRA39 softproof: sRGB → CMYK → sRGB.

    PERCEPTUAL rendering intent kompresuje kolory spoza gamutu CMYK.
    Zwraca nowy PIL.Image lub ten sam obiekt jeśli ICC niedostępny.
    """
    try:
        from PIL import ImageCms
        from modules.bleed import _find_fogra39_path
    except Exception:
        return pil_img
    fogra_path = _find_fogra39_path()
    if fogra_path is None:
        return pil_img
    try:
        srgb_profile = ImageCms.createProfile("sRGB")
        fogra_profile = ImageCms.getOpenProfile(fogra_path)
        rgb_in = pil_img if pil_img.mode == "RGB" else pil_img.convert("RGB")
        pil_cmyk = ImageCms.profileToProfile(
            rgb_in, srgb_profile, fogra_profile,
            outputMode="CMYK",
            renderingIntent=ImageCms.Intent.PERCEPTUAL,
        )
        return ImageCms.profileToProfile(
            pil_cmyk, fogra_profile, srgb_profile,
            outputMode="RGB",
            renderingIntent=ImageCms.Intent.PERCEPTUAL,
        )
    except Exception as e:
        log.warning(f"Softproof FOGRA39 failed: {e}")
        return pil_img


class _RenderWorker(QObject):
    """Worker renderujacy PDF do QPixmap w watku tla.

    Komunikacja: slot render_request wywolany z main thread przez BlockingQueued
    lub QueuedConnection wykonuje fitz.open + get_pixmap w worker thread.
    Wynik (QPixmap) emitowany przez sygnal pixmap_ready z request_id ktory pozwala
    main thread ignorowac outdated requesty (user mogl przekliknac dalej).

    Cache jest trzymany w PreviewPanel (main thread) — worker nie cache'uje sam,
    zeby uniknac race conditions na dict (QPixmap nie jest w pelni thread-safe
    po przekazaniu).
    """

    pixmap_ready = pyqtSignal(int, str, object)  # (request_id, path, QPixmap|None)

    @pyqtSlot(int, str, int, bool, bool)
    def render_request(self, request_id: int, path: str, dpi: int,
                       alpha: bool, softproof: bool = False):
        """Renderuje strone PDF. Emit pixmap_ready niezaleznie od sukcesu.

        softproof=True — round-trip sRGB→CMYK FOGRA39→sRGB (pokazuje kolory
        po konwersji drukowej). Wymaga alpha=False (sRGB round-trip).
        """
        import fitz
        from PIL import Image as PILImage
        doc = None
        qpix = None
        try:
            doc = fitz.open(path)
            page = doc[0]
            mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
            pix = page.get_pixmap(matrix=mat, alpha=alpha)
            if softproof and not alpha:
                pil = PILImage.frombytes(
                    "RGB", (pix.width, pix.height), pix.samples
                ).copy()
                pil = apply_softproof_fogra39(pil)
                data = pil.tobytes("raw", "RGB")
                qimg = QImage(data, pil.width, pil.height,
                              pil.width * 3, QImage.Format.Format_RGB888)
                qpix = QPixmap.fromImage(qimg.copy())
            else:
                if alpha:
                    qimg = QImage(pix.samples, pix.width, pix.height,
                                  pix.stride, QImage.Format.Format_RGBA8888)
                else:
                    qimg = QImage(pix.samples, pix.width, pix.height,
                                  pix.stride, QImage.Format.Format_RGB888)
                # copy() — odklej dane od pix.samples (zwolni sie gdy doc.close)
                qpix = QPixmap.fromImage(qimg.copy())
        except Exception as e:
            log.warning(f"Async render failed: {path}: {e}")
            qpix = None
        finally:
            if doc is not None:
                try:
                    doc.close()
                except Exception:
                    pass
        self.pixmap_ready.emit(request_id, path, qpix)

from gui.theme import PREVIEW_CUTCONTOUR, PREVIEW_FLEXCUT, PREVIEW_MARK


class MetadataGrid(QWidget):
    """MetaPanel: KV-grid metadanych PDF (Technikadruku style).

    5-kolumnowy layout: MediaBox | TrimBox | BleedBox | Spad | OutputIntent.
    QSS: QFrame#MetaPanel + QLabel#MetaKey + QLabel#MetaVal (+status).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        from PyQt6.QtWidgets import QFrame
        self.setObjectName("MetaPanel")
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(18, 10, 18, 10)
        self._grid.setHorizontalSpacing(24)
        self._grid.setVerticalSpacing(4)
        self._rows: list[tuple[QLabel, QLabel]] = []

    def clear(self):
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._rows.clear()

    def set_data(self, items: list[tuple[str, str]]):
        """items = [(key, value), ...]. Ukladany poziomo (max 5 kolumn).

        Dla 'OutputIntent' z 'FOGRA39' — automatyczne status="ok" (zielony).
        """
        self.clear()
        for col, (k, v) in enumerate(items):
            key = QLabel(k.upper())
            key.setObjectName("MetaKey")
            val = QLabel(v)
            val.setObjectName("MetaVal")
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            # Auto-status: FOGRA39 → ok (zielony)
            if "FOGRA" in v.upper() or v.upper() in ("OK", "PDF/X-4"):
                val.setProperty("status", "ok")
            self._grid.addWidget(key, 0, col)
            self._grid.addWidget(val, 1, col)
            self._rows.append((key, val))

log = logging.getLogger(__name__)


class _PDFGraphicsView(QGraphicsView):
    """QGraphicsView z zoom (scroll) i pan (prawy przycisk / środkowy).
    Tryb crop: lewy przycisk drag → przesuwanie offsetu crop.
    """

    crop_drag = pyqtSignal(float, float)  # (delta_x_ratio, delta_y_ratio)

    def __init__(self, parent=None):
        super().__init__(parent)
        from PyQt6.QtGui import QPainter
        self.setObjectName("PreviewCanvas")
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setBackgroundBrush(QColor("#E9ECEF"))
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
        # Lagodny zoom — 1.05 per tick (starsze 1.15 bylo za agresywne).
        factor = 1.05 if event.angleDelta().y() > 0 else 1 / 1.05
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
        elif event.button() == Qt.MouseButton.LeftButton:
            # Lewy przycisk (bez crop_mode) = pan (click & hold)
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
        elif event.button() == Qt.MouseButton.LeftButton and self._panning:
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
    """Panel podglądu PDF z nawigacją, legendą, zoom/pan.
    Tryb crop: wyświetla obraz z overlayem kształtu crop.
    """

    crop_offset_changed = pyqtSignal(str, tuple)  # (filepath, (ox, oy))

    def __init__(self, parent=None, split_enabled: bool = True,
                 placeholder_text: str | None = None):
        """
        Args:
            split_enabled: czy tworzyć przycisk "Przed/Po" (split-view).
                True dla zakładki Bleed (porównanie oryginału z wynikiem),
                False dla zakładki Nest (podgląd arkusza — brak "przed").
            placeholder_text: tekst placeholder gdy brak podglądu.
        """
        super().__init__(parent)
        self.setObjectName("PreviewPane")
        self._split_enabled = split_enabled
        self._results: list[dict] = []
        self._job = None
        self._sheet_pdfs: list[tuple] = []
        self._bleed_mm: float = 0.0
        self._current_idx: int = 0
        self._cache: dict = {}
        self._crop_data: dict = {}
        self._split_view: bool = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # === PreviewHead (navy style) ===
        from PyQt6.QtWidgets import QFrame
        head = QFrame()
        head.setObjectName("PreviewHead")
        head_lay = QHBoxLayout(head)
        head_lay.setContentsMargins(18, 10, 18, 10)
        head_lay.setSpacing(12)

        # PreviewNav pill (prev | idx | next)
        nav = QWidget()
        nav.setObjectName("PreviewNav")
        nav_lay = QHBoxLayout(nav)
        nav_lay.setContentsMargins(3, 3, 3, 3)
        nav_lay.setSpacing(0)
        self._prev_btn = QPushButton("‹")
        self._prev_btn.setProperty("role", "prev-next")
        self._prev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._prev_btn.clicked.connect(self._prev)
        nav_lay.addWidget(self._prev_btn)
        self._idx_label = QLabel("—")
        self._idx_label.setObjectName("PreviewIdx")
        nav_lay.addWidget(self._idx_label)
        self._next_btn = QPushButton("›")
        self._next_btn.setProperty("role", "prev-next")
        self._next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_btn.clicked.connect(self._next)
        nav_lay.addWidget(self._next_btn)
        head_lay.addWidget(nav)

        # Title + size
        self._title_label = QLabel("Podgląd")
        self._title_label.setObjectName("PreviewTitle")
        head_lay.addWidget(self._title_label)
        sep = QLabel("·")
        sep.setObjectName("PreviewTitleSep")
        head_lay.addWidget(sep)
        self._info_label = QLabel("")
        self._info_label.setObjectName("PreviewTitleSz")
        head_lay.addWidget(self._info_label)

        head_lay.addStretch(1)

        # Split view toggle (tylko Bleed mode)
        if self._split_enabled:
            self._split_btn = QPushButton("▮▮ Przed / Po")
            self._split_btn.setObjectName("SplitBtn")
            self._split_btn.setCheckable(True)
            self._split_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._split_btn.setToolTip(
                "Podgląd side-by-side: oryginał (lewo) vs wynik z bleedem (prawo)"
            )
            self._split_btn.clicked.connect(self._on_toggle_split)
            self._split_btn.setVisible(False)
            head_lay.addWidget(self._split_btn)
        else:
            self._split_btn = None

        layout.addWidget(head)

        # Graphics View
        self._scene = QGraphicsScene()
        self._view = _PDFGraphicsView()
        self._view.setScene(self._scene)
        self._view.crop_drag.connect(self._on_crop_drag)
        layout.addWidget(self._view, stretch=1)

        # Metadata grid (TrimBox/BleedBox/OutputIntent) — wypelniana po renderze
        self._meta_grid = MetadataGrid()
        self._meta_grid.setVisible(False)
        layout.addWidget(self._meta_grid)

        # Placeholder (overlay na view — center)
        default_placeholder = (
            "Przeciągnij pliki i kliknij\n\"Generuj bleed\" aby zobaczyć podgląd"
            if self._split_enabled else
            "Dodaj pliki i kliknij \"Generuj arkusze\"\naby zobaczyć podgląd"
        )
        self._placeholder = QLabel(placeholder_text or default_placeholder)

        # Async render worker — tworzony LAZY dopiero przy pierwszym cache miss.
        # Eksplicitny opt-in: testy nie uruchamiaja threada (brak render_sheet),
        # a realne show_nest_job dostaje nieblokujacy render dla duzych arkuszy.
        self._render_thread: QThread | None = None
        self._render_worker: _RenderWorker | None = None
        self._async_request_counter = 0
        self._active_async_requests: dict[int, dict] = {}  # req_id -> {role, cache_key}
        self._async_ticket: int = 0  # increment per _render_current, invalidate prev

    def _ensure_render_thread(self) -> None:
        """Lazy init workera. Wolane przed pierwszym async submit."""
        if self._render_thread is not None:
            return
        self._render_thread = QThread()
        self._render_worker = _RenderWorker()
        self._render_worker.moveToThread(self._render_thread)
        self._render_worker.pixmap_ready.connect(
            self._on_async_pixmap_ready, type=Qt.ConnectionType.QueuedConnection
        )
        self._render_thread.start()

    def _shutdown_render_thread(self) -> None:
        """Zatrzymuje worker thread. Idempotentne (safe dla multiple calls)."""
        try:
            if self._render_thread is not None and self._render_thread.isRunning():
                self._render_thread.quit()
                self._render_thread.wait(2000)
        except RuntimeError:
            pass

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

        # Split-view dostępny tylko gdy mamy input paths i panel go obsługuje
        has_inputs = any(r.get("input_path") for r in self._results)
        if self._split_btn is not None:
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
        if self._split_btn is not None:
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
        if self._split_btn is not None:
            self._split_btn.setVisible(False)
            self._split_btn.setChecked(False)
        self._split_view = False
        self._meta_grid.setVisible(False)
        self._active_async_requests.clear()
        self._async_ticket += 1  # invalidate pending async
        self._update_nav()

    def closeEvent(self, event):
        """Czysty shutdown worker threada przy zamknieciu panelu."""
        self._shutdown_render_thread()
        super().closeEvent(event)

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
            self._idx_label.setText(f"{idx + 1:02d} / {n:02d}")
            if self._results:
                r = self._results[idx]
                path = r.get("output") or r.get("path") or ""
                name = os.path.basename(path) if path else f"Podgląd {idx + 1}"
                self._title_label.setText(name)
                w, h = r["size_mm"]
                self._info_label.setText(f"{w:.0f} × {h:.0f} mm · bleed {self._bleed_mm:.0f} mm")
            elif self._job:
                sheet = self._job.sheets[idx]
                placed = len(sheet.placements)
                self._title_label.setText(f"Arkusz {idx + 1}")
                self._info_label.setText(
                    f"{sheet.width_mm:.0f} × {sheet.height_mm:.0f} mm · {placed} szt"
                )
        else:
            self._idx_label.setText("—")
            self._title_label.setText("Podgląd")
            self._info_label.setText("")

    # --- Rendering ---

    def _render_current(self):
        """Renderuje aktualny PDF do sceny."""
        self._scene.clear()
        self._view.set_crop_mode(False)
        self._meta_grid.setVisible(False)
        has_content = bool(self._results) or bool(self._job and self._job.sheets)
        self._view.setVisible(has_content)
        self._placeholder.setVisible(not has_content)
        if not has_content:
            return
        idx = self._current_idx

        if self._results and idx < len(self._results):
            self._render_bleed(idx)
            self._update_metadata(self._results[idx]["path"])
        elif self._job and self._sheet_pdfs and idx < len(self._sheet_pdfs):
            self._render_sheet(idx)

        # Zsynchronizuj sceneRect z items przed fit — żeby wyśrodkować
        # canvas na rzeczywistej zawartości, a nie na stale cached rect'u.
        self._scene.setSceneRect(self._scene.itemsBoundingRect())
        self._view.fit_content()

    def _update_metadata(self, pdf_path: str):
        """Wczytaj TrimBox/BleedBox/CropBox + OutputIntent z PDF i pokaz w KV-grid."""
        try:
            import fitz
            doc = fitz.open(pdf_path)
            page = doc[0]

            def _box_str(rect) -> str:
                if rect is None:
                    return "—"
                w_mm = (rect.x1 - rect.x0) * 25.4 / 72.0
                h_mm = (rect.y1 - rect.y0) * 25.4 / 72.0
                return f"{w_mm:.1f} × {h_mm:.1f} mm"

            media = page.mediabox
            trim = page.trimbox if page.trimbox else None

            # Wylicz spad z roznicy MediaBox - TrimBox (wszystkie cztery boki)
            bleed_mm_str = "—"
            if trim is not None and media is not None:
                dx = (trim.x0 - media.x0) * 25.4 / 72.0
                bleed_mm_str = f"{abs(dx):.1f} mm"

            doc.close()

            items = [
                ("Rozmiar",  _box_str(media)),
                ("TrimBox",  _box_str(trim)),
                ("Spad",     bleed_mm_str),
            ]
            self._meta_grid.set_data(items)
            self._meta_grid.setVisible(True)
        except Exception as e:
            log.warning(f"Metadata read failed: {pdf_path}: {e}")
            self._meta_grid.setVisible(False)

    def _render_bleed(self, idx: int):
        result = self._results[idx]
        path = result["path"]
        input_path = result.get("input_path")
        input_page_idx = result.get("input_page_idx", 0)

        # Split view: pokaż input (lewo) + output (prawo)
        if self._split_view and input_path:
            self._render_split(input_path, path, input_page_idx)
            return

        # Domyślny widok = output PO konwersji CMYK (softproof FOGRA39).
        # Split view pokazuje oba (input sRGB vs output CMYK).
        qpix = self._render_pdf_page_softproof(path, dpi=150)
        if qpix:
            self._scene.addPixmap(qpix)

    def _render_split(self, input_path: str, output_path: str,
                      input_page_idx: int = 0):
        """Renderuje podgląd side-by-side: oryginał vs wynik z bleedem.

        Output jest renderowany z softproofem CMYK FOGRA39 (round-trip
        sRGB→CMYK→sRGB) — żeby operator widział kolory takie, jak wyjdą
        po konwersji w RIP-ie drukarki, a nie źródłowy sRGB.
        """
        # Output (PDF z bleedem) — softproof CMYK
        out_pix = self._render_pdf_page_softproof(output_path, dpi=150)
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
        """Renderuje arkusz. Cache hit = synchronic, miss = async (nieblokujacy).

        Dla arkusza A4+ @ 150dpi synchroniczny render trwa ~300-800ms
        co zamraza GUI. Delegacja do worker thread pozwala interfejsowi
        pozostac responsywnym podczas renderowania.

        Print pixmap idzie przez softproof CMYK FOGRA39 — operator widzi
        kolory takie, jak wyjdą po konwersji drukowej.
        """
        pp, cp = self._sheet_pdfs[idx]

        # Invalidate wszystkie pending async z poprzednich _render_sheet
        self._async_ticket += 1
        current_ticket = self._async_ticket

        # Print PDF — z softproofem
        pp_key = self._cache_key(pp, 150, False, softproof=True)
        if pp_key in self._cache:
            self._scene.addPixmap(self._cache[pp_key])
        else:
            self._placeholder_in_scene("Renderowanie arkusza...")
            self._submit_async_render(pp, dpi=150, alpha=False,
                                      role="print", ticket=current_ticket,
                                      softproof=True)

        # Cut overlay (jesli istnieje) — bez softproof (spot color)
        if os.path.isfile(cp):
            cp_key = self._cache_key(cp, 150, True)
            if cp_key in self._cache:
                item = self._scene.addPixmap(self._cache[cp_key])
                item.setOpacity(0.8)
            else:
                self._submit_async_render(cp, dpi=150, alpha=True,
                                          role="cut", ticket=current_ticket)

    def _cache_key(self, path: str, dpi: int, alpha: bool,
                   softproof: bool = False) -> tuple:
        return (path, dpi, alpha, softproof,
                os.path.getmtime(path) if os.path.isfile(path) else 0)

    def _placeholder_in_scene(self, text: str) -> None:
        """Tymczasowy tekst "Renderowanie..." w scenie."""
        item = self._scene.addText(text)
        item.setDefaultTextColor(QColor("#666"))
        item.setPos(10, 10)

    def _submit_async_render(self, path: str, dpi: int, alpha: bool,
                             role: str, ticket: int,
                             softproof: bool = False) -> None:
        """Wysyla request renderowania do _RenderWorker w watku tla."""
        self._ensure_render_thread()
        self._async_request_counter += 1
        rid = self._async_request_counter
        self._active_async_requests[rid] = {
            "role": role,
            "path": path,
            "dpi": dpi,
            "alpha": alpha,
            "softproof": softproof,
            "ticket": ticket,
        }
        # QueuedConnection + invokeMethod approach
        from PyQt6.QtCore import QMetaObject, Q_ARG, Qt as _Qt
        QMetaObject.invokeMethod(
            self._render_worker, "render_request",
            _Qt.ConnectionType.QueuedConnection,
            Q_ARG(int, rid),
            Q_ARG(str, path),
            Q_ARG(int, dpi),
            Q_ARG(bool, alpha),
            Q_ARG(bool, softproof),
        )

    @pyqtSlot(int, str, object)
    def _on_async_pixmap_ready(self, request_id: int, path: str,
                               qpix: object) -> None:
        """Slot wywolany po zakonczeniu renderu w watku tla."""
        req = self._active_async_requests.pop(request_id, None)
        if req is None:
            return
        # Sprawdz czy request nie jest outdated (user przelkiknal do innego arkusza)
        if req["ticket"] != self._async_ticket:
            return
        if qpix is None:
            return
        # Cache + wstaw do sceny (pozbadz sie wszystkich placeholder textow)
        key = self._cache_key(req["path"], req["dpi"], req["alpha"],
                              softproof=req.get("softproof", False))
        if len(self._cache) > 10:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = qpix

        # Usun placeholder text (pierwszy tekstowy element bez grafiki)
        from PyQt6.QtWidgets import QGraphicsTextItem
        for item in list(self._scene.items()):
            if isinstance(item, QGraphicsTextItem):
                self._scene.removeItem(item)
                break

        item = self._scene.addPixmap(qpix)
        if req["role"] == "cut":
            item.setOpacity(0.8)
        # Scene rect musi być ustawiony po dodaniu pixmap, żeby fit obejmował
        # rzeczywisty content (nie placeholder). Bez tego canvas trzyma się
        # pierwotnego rect'u placeholdera i naklejka ląduje w lewym górnym rogu.
        self._scene.setSceneRect(self._scene.itemsBoundingRect())
        self._view.fit_content()

    def _render_pdf_page_softproof(self, path: str, dpi: int = 150) -> QPixmap | None:
        """Renderuje PDF z symulacją softproof CMYK FOGRA39.

        Round-trip sRGB → CMYK (FOGRA39) → sRGB. Kolory spoza gamutu CMYK
        są kompresowane zgodnie z rendering intent PERCEPTUAL — podgląd
        pokazuje jak output będzie wyglądał po druku UV na Mimaki UCJV.
        Fallback: zwykły render RGB gdy ICC niedostępny.
        """
        cache_key = (path, dpi, "softproof",
                     os.path.getmtime(path) if os.path.isfile(path) else 0)
        if cache_key in self._cache:
            return self._cache[cache_key]
        try:
            import fitz
            from PIL import Image
            doc = fitz.open(path)
            page = doc[0]
            mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            pil = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).copy()
            doc.close()

            pil = apply_softproof_fogra39(pil)

            data = pil.tobytes("raw", "RGB")
            qimg = QImage(data, pil.width, pil.height,
                          pil.width * 3, QImage.Format.Format_RGB888)
            qpixmap = QPixmap.fromImage(qimg.copy())
            if len(self._cache) > 10:
                self._cache.pop(next(iter(self._cache)))
            self._cache[cache_key] = qpixmap
            return qpixmap
        except Exception as e:
            log.warning(f"Softproof render failed: {path}: {e}")
            return self._render_pdf_page(path, dpi=dpi, alpha=False)

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
            # Wyłącz crop preview — reset trybu + transform, delegacja do
            # _render_current żeby widocznosc widget'ów była spójna z reszta
            # UI (brak duplikowanych placeholderów/okien).
            self._view.set_crop_mode(False)
            self._view.resetTransform()
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
            self._idx_label.setText("CROP")
            self._title_label.setText("Crop")
            self._info_label.setText(f"{shape} · offset ({ox:.2f}, {oy:.2f})")

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
