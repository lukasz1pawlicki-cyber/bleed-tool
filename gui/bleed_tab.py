"""
Bleed Tool — bleed_tab.py
============================
Zakladka Bleed: DropZone, Card(Parametry), ActionBar. Technikadruku QSS.
"""

import os
import logging
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QCheckBox, QRadioButton, QButtonGroup, QComboBox,
    QProgressBar, QFileDialog, QSizePolicy, QMessageBox, QSpinBox,
    QDoubleSpinBox, QScrollArea,
)
from PyQt6.QtCore import pyqtSignal, Qt

from config import DEFAULT_BLEED_MM
from gui.file_section import FileSection
from gui.atoms import (
    Segmented, IconButton, make_button, FieldLabel, UnitLabel,
)
from gui.widgets_common import PageTitleBar, CardSection, ActionBar
from gui import settings as _settings

log = logging.getLogger(__name__)


class BleedTab(QWidget):
    """Zakladka Bleed — pelny formularz z generowaniem."""

    preview_ready = pyqtSignal(list, list)
    crop_preview_requested = pyqtSignal(dict)

    def __init__(self, log_fn=None, parent=None):
        super().__init__(parent)
        self._log = log_fn or (lambda msg: None)
        self._processing = False
        _saved = _settings.load().get("bleed", {})

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # === Page title bar ===
        self._title_bar = PageTitleBar(
            crumb="Workflow · Krok 01",
            title="Bleed",
            help_tip="Generuj bleed i CutContour",
            help_text=(
                "Krok 01 — Bleed\n\n"
                "Wejście: PDF / AI / SVG / EPS / PNG / JPG / TIFF.\n"
                "Wyjście: PDF z 2 mm spadem + wektorowa linia cięcia CutContour (spot color).\n\n"
                "Parametry:\n"
                "  • Spad — szerokość spadu w mm (domyślnie 2 mm).\n"
                "  • Wysokość — opcjonalny crop do wysokości; wymagane dla crop shape.\n"
                "  • Linia cięcia — Kiss-Cut / FlexCut / Brak.\n"
                "  • Silnik konturu — Auto (Moore+OpenCV) / Moore / OpenCV.\n"
                "  • Biały poddruk — dodaje osobną warstwę white.\n\n"
                "Preflight gate — Off / Lenient / Strict — blokuje eksport gdy plik\n"
                "nie spełnia wymagań (tryb RGB/CMYK, rozmiar, DPI).\n\n"
                "Output idzie do Nest (Krok 02) automatycznie."
            ),
        )
        root.addWidget(self._title_bar)

        # === Scroll area dla cards ===
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(22, 10, 22, 10)
        layout.setSpacing(8)
        scroll.setWidget(inner)
        root.addWidget(scroll, stretch=1)

        # === Files card ===
        files_card = CardSection(
            "Pliki wejściowe",
            aux="PDF · AI · SVG · EPS · PNG · JPG · TIFF",
        )
        self._file_section = FileSection(show_copies=False)
        files_card.body.addWidget(self._file_section)
        layout.addWidget(files_card)

        # === Parametry card ===
        params_card = CardSection(
            "Parametry bleeda",
            aux="pipeline: detect → offset → refit",
        )

        # Row: Spad
        row_spad = QHBoxLayout()
        row_spad.setSpacing(8)
        row_spad.addWidget(self._field_label("Spad"))
        self._bleed_spin = QSpinBox()
        self._bleed_spin.setRange(0, 50)
        self._bleed_spin.setSingleStep(1)
        self._bleed_spin.setValue(int(round(float(_saved.get("bleed_mm", DEFAULT_BLEED_MM)))))
        self._bleed_spin.setFixedWidth(80)
        self._bleed_spin.setProperty("variant", "mono")
        row_spad.addWidget(self._bleed_spin)
        row_spad.addWidget(UnitLabel("mm"))
        row_spad.addStretch(1)
        self._black_100k_cb = QCheckBox("Czarny → 100% K")
        self._black_100k_cb.setEnabled(False)
        row_spad.addWidget(self._black_100k_cb)
        # Sharp edges (ostre narozy) — widoczny tylko gdy sa pliki rastrowe
        self._sharp_edges_cb = QCheckBox("Ostre krawędzie (raster)")
        self._sharp_edges_cb.setToolTip(
            "Dla geometrycznych ksztaltow z ostrymi kątami (gwiazdki, strzalki, "
            "diamenty). Zachowuje kontur 1:1 z input bez wygladzania krzywymi.\n\n"
            "Domyslnie wylaczone — uzywamy wygladzonych krzywych Bezier (lepsze "
            "dla logotypow, ilustracji, okraglych naklejek)."
        )
        self._sharp_edges_cb.setVisible(False)
        self._sharp_edges_cb.setChecked(bool(_saved.get("sharp_edges", False)))
        row_spad.addWidget(self._sharp_edges_cb)
        params_card.body.addLayout(row_spad)

        # Row: Wysokosc
        row_h = QHBoxLayout()
        row_h.setSpacing(8)
        row_h.addWidget(self._field_label("Wysokość"))
        self._height_edit = QLineEdit()
        self._height_edit.setFixedWidth(80)
        self._height_edit.setPlaceholderText("auto")
        self._height_edit.setProperty("variant", "mono")
        row_h.addWidget(self._height_edit)
        row_h.addWidget(UnitLabel("cm"))
        row_h.addStretch(1)
        params_card.body.addLayout(row_h)

        # Row: Linia ciecia (Segmented)
        row_cut = QHBoxLayout()
        row_cut.setSpacing(8)
        row_cut.addWidget(self._field_label("Linia cięcia"))
        _cl_map = {"kiss-cut": "Kiss-Cut", "flexcut": "FlexCut", "none": "Brak"}
        _cl_saved = _saved.get("cutline_mode", "kiss-cut")
        self._cutline_seg = Segmented(
            ["Kiss-Cut", "FlexCut", "Brak"],
            accent=True,
            default=_cl_map.get(_cl_saved, "Kiss-Cut"),
        )
        row_cut.addWidget(self._cutline_seg)
        row_cut.addStretch(1)
        params_card.body.addLayout(row_cut)

        # Silnik konturu = config.CONTOUR_ENGINE (domyslnie opencv).
        # Bialy poddruk usuniety z GUI Bleed — naklejki generalnie bez bialego.

        # Row: Obrys kształtu (raster) — widoczny gdy sa pliki rastrowe lub PDF
        row_contour = QHBoxLayout()
        row_contour.setSpacing(8)
        row_contour.addWidget(self._field_label("Obrys"))
        _rc_map = {
            "standard": "Standardowy",
            "glow": "Z poświatą",
            "tight": "Ciasny",
        }
        _rc_saved = _saved.get("raster_contour_mode", "standard")
        self._raster_contour_seg = Segmented(
            ["Standardowy", "Z poświatą", "Ciasny"],
            default=_rc_map.get(_rc_saved, "Standardowy"),
        )
        self._raster_contour_seg.setToolTip(
            "Standardowy — domyślny algorytm (threshold 50).\n"
            "Z poświatą — dla plików z halo/glow/rozproszonymi elementami\n"
            "  (np. gwiazdki wokół postaci). Niski threshold + closing 5px.\n"
            "Ciasny — linia cięcia blisko widocznej treści (threshold 150,\n"
            "  ignoruje faint shadow/tło)."
        )
        row_contour.addWidget(self._raster_contour_seg)
        row_contour.addSpacing(16)

        # Zmniejsz obrys — cofnięcie linii do wewnątrz alpha-mask (dla plików
        # Canva z halo: shrink ~3mm daje cięcie na granicy halo/grafika)
        row_contour.addWidget(self._field_label("Zmniejsz"))
        self._shrink_spin = QDoubleSpinBox()
        self._shrink_spin.setRange(0.0, 20.0)
        self._shrink_spin.setSingleStep(0.5)
        self._shrink_spin.setDecimals(1)
        self._shrink_spin.setValue(float(_saved.get("contour_shrink_mm", 0.0)))
        self._shrink_spin.setFixedWidth(80)
        self._shrink_spin.setProperty("variant", "mono")
        self._shrink_spin.setToolTip(
            "Cofnięcie linii cięcia o X mm do wewnątrz alpha-mask.\n"
            "0 = bez cofania (domyślne).\n"
            "~3mm dla plików Canva z halo — linia trafia na granicę\n"
            "  halo/grafika zamiast zewnętrznej krawędzi halo."
        )
        row_contour.addWidget(self._shrink_spin)
        row_contour.addWidget(UnitLabel("mm"))
        row_contour.addStretch(1)
        params_card.body.addLayout(row_contour)

        # Row: Crop (advanced — ukryty przy braku wysokosci)
        row_crop = QHBoxLayout()
        row_crop.setSpacing(8)
        row_crop.addWidget(self._field_label("Crop"))
        self._crop_cb = QCheckBox("Crop")
        self._crop_cb.setEnabled(False)
        self._crop_cb.toggled.connect(self._on_crop_toggled)
        row_crop.addWidget(self._crop_cb)
        row_crop.addStretch(1)

        self._crop_shape_group = QButtonGroup(self)
        self._rb_square = QRadioButton("Kwadrat")
        self._rb_rounded = QRadioButton("Zaokrąglony")
        self._rb_circle = QRadioButton("Okrąg")
        self._rb_square.setChecked(True)
        self._crop_shape_group.addButton(self._rb_square, 0)
        self._crop_shape_group.addButton(self._rb_rounded, 1)
        self._crop_shape_group.addButton(self._rb_circle, 2)
        for rb in (self._rb_square, self._rb_rounded, self._rb_circle):
            row_crop.addWidget(rb)
            rb.setVisible(False)

        self._radius_label = QLabel("R 9%")
        self._radius_label.setObjectName("FieldSubLabel")
        self._radius_label.setVisible(False)
        row_crop.addWidget(self._radius_label)
        self._radius_dec_btn = IconButton("−")
        self._radius_dec_btn.setVisible(False)
        self._radius_dec_btn.clicked.connect(self._crop_radius_dec)
        row_crop.addWidget(self._radius_dec_btn)
        self._radius_inc_btn = IconButton("+")
        self._radius_inc_btn.setVisible(False)
        self._radius_inc_btn.clicked.connect(self._crop_radius_inc)
        row_crop.addWidget(self._radius_inc_btn)
        self._radius_pct = 9
        self._crop_shape_group.idToggled.connect(self._on_crop_shape_changed)

        params_card.body.addLayout(row_crop)

        # Row: Output
        row_out = QHBoxLayout()
        row_out.setSpacing(8)
        row_out.addWidget(self._field_label("Output"))
        self._output_edit = QLineEdit()
        self._output_edit.setPlaceholderText("Katalog pliku wejściowego")
        self._output_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row_out.addWidget(self._output_edit, stretch=1)
        browse_btn = IconButton("…", tip="Wybierz folder")
        browse_btn.clicked.connect(self._browse_output)
        row_out.addWidget(browse_btn)
        params_card.body.addLayout(row_out)

        layout.addWidget(params_card)

        # === Preflight gate card ===
        pg_card = CardSection("Preflight gate")
        row_pg = QHBoxLayout()
        row_pg.setSpacing(10)
        _gate_map = {"off": "Off", "lenient": "Lenient", "strict": "Strict"}
        _gate_default = _saved.get("preflight_gate", "off")
        self._preflight_gate_seg = Segmented(
            ["Off", "Lenient", "Strict"],
            default=_gate_map.get(_gate_default, "Off"),
        )
        row_pg.addWidget(self._preflight_gate_seg)
        row_pg.addStretch(1)
        self._preflight_btn = make_button("Preflight", variant="secondary", size="sm")
        self._preflight_btn.clicked.connect(self._on_preflight)
        row_pg.addWidget(self._preflight_btn)
        pg_card.body.addLayout(row_pg)
        layout.addWidget(pg_card)

        layout.addStretch(1)

        # === Action bar ===
        self._action_bar = ActionBar()
        self._run_btn = make_button("▶ Generuj bleed", size="lg")
        self._run_btn.clicked.connect(self._on_run)
        self._action_bar.body.addWidget(self._run_btn)

        self._progress = QProgressBar()
        self._progress.setFixedWidth(160)
        self._progress.setValue(0)
        self._progress.setVisible(False)
        self._action_bar.body.addWidget(self._progress)

        self._status_label = QLabel("")
        self._status_label.setObjectName("ProgressText")
        self._action_bar.body.addWidget(self._status_label)
        self._action_bar.body.addStretch(1)
        root.addWidget(self._action_bar)

        # Sygnaly
        self._file_section.files_changed.connect(self._on_files_changed)
        self._height_edit.textChanged.connect(self._on_height_changed)
        self._crop_offsets: dict[str, tuple[float, float]] = {}

    def _field_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("FieldLabel")
        lbl.setFixedWidth(110)
        return lbl

    # --- Public API ---

    def clear(self):
        self._file_section.clear_files()
        self._output_edit.setText("")
        self._status_label.setText("")

    @property
    def files(self) -> list[str]:
        return self._file_section.files

    @property
    def bleed_mm(self) -> float:
        return float(self._bleed_spin.value())

    @property
    def cutline_mode(self) -> str:
        m = {"Kiss-Cut": "kiss-cut", "FlexCut": "flexcut", "Brak": "none"}
        return m.get(self._cutline_seg.value(), "kiss-cut")

    @property
    def crop_enabled(self) -> bool:
        return self._crop_cb.isChecked() and self._parse_height() is not None

    @property
    def raster_contour_mode(self) -> str:
        m = {
            "Standardowy": "standard",
            "Z poświatą": "glow",
            "Ciasny": "tight",
        }
        return m.get(self._raster_contour_seg.value(), "standard")

    @property
    def raster_contour_shrink_mm(self) -> float:
        return float(self._shrink_spin.value())

    @property
    def crop_shape(self) -> str:
        if self._rb_circle.isChecked():
            return "circle"
        if self._rb_rounded.isChecked():
            return "rounded"
        return "square"

    @property
    def output_dir(self) -> str:
        txt = self._output_edit.text().strip()
        if txt:
            return txt
        if self.files:
            return os.path.dirname(self.files[0])
        return os.path.join(os.path.dirname(os.path.dirname(__file__)), "output")

    # --- Callbacks ---

    def _on_files_changed(self):
        has_pdf = any(p.lower().endswith(('.pdf', '.svg')) for p in self.files)
        self._black_100k_cb.setEnabled(has_pdf)
        if not has_pdf:
            self._black_100k_cb.setChecked(False)
        # Sharp edges — widoczny tylko gdy dodano plik rastrowy
        _RASTER_EXT = ('.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp', '.webp')
        has_raster = any(p.lower().endswith(_RASTER_EXT) for p in self.files)
        self._sharp_edges_cb.setVisible(has_raster)
        if not has_raster:
            self._sharp_edges_cb.setChecked(False)
        has_height = bool(self._height_edit.text().strip())
        self._crop_cb.setEnabled(bool(self.files) and has_height)
        if not self._crop_cb.isEnabled():
            self._crop_cb.setChecked(False)
        if self.files:
            self._output_edit.setText(os.path.dirname(self.files[-1]))

    def _on_height_changed(self, _text: str = ""):
        has_height = bool(self._height_edit.text().strip())
        self._crop_cb.setEnabled(bool(self.files) and has_height)
        if not self._crop_cb.isEnabled():
            self._crop_cb.setChecked(False)

    def _on_crop_toggled(self, checked: bool):
        for rb in (self._rb_square, self._rb_rounded, self._rb_circle):
            rb.setVisible(checked)
        self._on_crop_shape_changed()
        self._emit_crop_preview()

    def _on_crop_shape_changed(self, *_args):
        is_rounded = self._rb_rounded.isChecked() and self._crop_cb.isChecked()
        self._radius_label.setVisible(is_rounded)
        self._radius_dec_btn.setVisible(is_rounded)
        self._radius_inc_btn.setVisible(is_rounded)
        self._emit_crop_preview()

    def _crop_radius_dec(self):
        self._radius_pct = max(1, self._radius_pct - 2)
        self._radius_label.setText(f"R {self._radius_pct}%")
        self._emit_crop_preview()

    def _crop_radius_inc(self):
        self._radius_pct = min(50, self._radius_pct + 2)
        self._radius_label.setText(f"R {self._radius_pct}%")
        self._emit_crop_preview()

    def _emit_crop_preview(self):
        if not self._crop_cb.isChecked() or not self.files:
            self.crop_preview_requested.emit({})
            return
        filepath = self.files[0]
        offset = self._crop_offsets.get(filepath, (0.5, 0.5))
        self.crop_preview_requested.emit({
            "file": filepath,
            "shape": self.crop_shape,
            "offset": offset,
            "radius_pct": self._radius_pct,
        })

    def update_crop_offset(self, filepath: str, offset: tuple):
        self._crop_offsets[filepath] = offset

    def _browse_output(self):
        d = QFileDialog.getExistingDirectory(self, "Folder wyjściowy", self._output_edit.text())
        if d:
            self._output_edit.setText(d)

    def _on_preflight(self):
        from modules.preflight import preflight_check, format_preflight_result
        self._log("\n--- Preflight ---")
        for path in self.files:
            result = preflight_check(path)
            text = format_preflight_result(result)
            self._log(f"  {os.path.basename(path)}:")
            for line in text.strip().split('\n'):
                self._log(f"    {line}")

    def _preflight_gate_value(self) -> str:
        m = {"Off": "off", "Lenient": "lenient", "Strict": "strict"}
        return m.get(self._preflight_gate_seg.value(), "off")

    def _preflight_gate_passes(self, gate: str) -> bool:
        try:
            from modules.preflight import preflight_gate, preflight_summary
        except Exception as e:
            self._log(f"[preflight] import failed: {e}")
            return True
        blockers: list[tuple[str, str]] = []
        for path in self.files:
            try:
                can_export, pf = preflight_gate(path, strict=(gate == "strict"))
            except Exception as e:
                self._log(f"[preflight] {os.path.basename(path)}: crash ({e}) — kontynuuje")
                continue
            if not can_export:
                reason = preflight_summary(pf)
                blockers.append((path, reason))
                self._file_section.set_status(path, "err", reason)
                self._log(f"[preflight BLOCK] {os.path.basename(path)}: {reason}")
        if not blockers:
            return True
        msg = "\n".join(f"• {os.path.basename(p)}: {r}" for p, r in blockers[:5])
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(f"Preflight {gate} — zablokowano")
        box.setText(f"Preflight gate '{gate}' zablokował {len(blockers)} z {len(self.files)} plików.")
        box.setInformativeText(msg)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()
        return False

    def _on_run(self):
        if self._processing or not self.files:
            return

        gate = self._preflight_gate_value()
        if gate != "off":
            if not self._preflight_gate_passes(gate):
                return

        _settings.update({"bleed": {
            "bleed_mm": self.bleed_mm,
            "cutline_mode": self.cutline_mode,
            "sharp_edges": self._sharp_edges_cb.isChecked(),
            "raster_contour_mode": self.raster_contour_mode,
            "contour_shrink_mm": self.raster_contour_shrink_mm,
            "preflight_gate": gate,
        }})

        self._processing = True
        self._run_btn.setEnabled(False)
        self._run_btn.setText("Przetwarzam…")
        self._progress.setVisible(True)
        self._progress.setValue(0)
        self._status_label.setText("")
        self._file_section.reset_statuses()

        from gui.workers import BleedWorker
        self._worker = BleedWorker(
            files=self.files,
            output_dir=self.output_dir,
            bleed_mm=self.bleed_mm,
            black_100k=self._black_100k_cb.isChecked(),
            cutline_mode=self.cutline_mode,
            target_height_mm=self._parse_height(),
            white=False,
            crop_enabled=self.crop_enabled,
            crop_shape=self.crop_shape,
            crop_offsets=dict(self._crop_offsets),
            radius_pct=self._radius_pct,
            raster_mode=("sharp" if self._sharp_edges_cb.isChecked() else "smooth"),
            raster_contour_mode=self.raster_contour_mode,
            raster_contour_shrink_mm=self.raster_contour_shrink_mm,
        )
        self._worker.log_message.connect(self._log)
        self._worker.progress.connect(self._on_progress)
        self._worker.file_status.connect(self._file_section.set_status)
        self._worker.finished.connect(self._on_done)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _parse_height(self) -> float | None:
        txt = self._height_edit.text().strip()
        if not txt:
            return None
        try:
            return float(txt.replace(",", ".")) * 10.0
        except ValueError:
            return None

    def _on_progress(self, current: int, total: int):
        if total > 0:
            self._progress.setValue(int(100 * current / total))
        self._status_label.setText(f"Plik {current} / {total} · {int(100 * current / max(1, total))}%")

    def _on_done(self, output_paths: list, input_infos: list):
        self._processing = False
        self._run_btn.setEnabled(True)
        self._run_btn.setText("▶ Generuj bleed")
        self._progress.setVisible(False)
        n = len(output_paths)
        self._status_label.setText(f"Gotowe — {n} plik(ów)")
        if output_paths:
            self.preview_ready.emit(input_infos, output_paths)

    def _on_error(self, msg: str):
        self._processing = False
        self._run_btn.setEnabled(True)
        self._run_btn.setText("▶ Generuj bleed")
        self._progress.setVisible(False)
        self._status_label.setText("BŁĄD")
        self._log(f"[BŁĄD KRYTYCZNY] {msg}")
        short = msg if len(msg) <= 600 else msg[:600] + "\n\n[...] pełny log w panelu na dole."
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("Błąd przetwarzania")
        box.setText("Nie udało się wygenerować bleedu dla jednego lub więcej plików.")
        box.setInformativeText(short)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()
