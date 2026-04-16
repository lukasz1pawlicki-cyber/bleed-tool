"""
Bleed Tool — nest_tab.py
===========================
Zakładka Nest: rozmieszczanie naklejek na arkuszu.
"""

import os
import math
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QCheckBox, QComboBox, QProgressBar, QFileDialog,
    QSizePolicy, QMessageBox,
)
from PyQt6.QtCore import pyqtSignal, Qt

from config import (
    DEFAULT_BLEED_MM, DEFAULT_GAP_MM, DEFAULT_MARK_ZONE_MM,
    DEFAULT_ROLL_MAX_LENGTH_MM,
    SHEET_PRESETS, ROLL_PRESETS, PLOTTERS, FLOAT_TOLERANCE_MM,
    PT_TO_MM,
)
from gui.file_section import FileSection


class SegmentedButton(QWidget):
    """Grupa przycisków segmentowanych (emulacja CTkSegmentedButton)."""

    value_changed = pyqtSignal(str)

    def __init__(self, values: list[str], default: str = "", parent=None):
        super().__init__(parent)
        self._buttons: dict[str, QPushButton] = {}
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        for i, val in enumerate(values):
            btn = QPushButton(val)
            btn.setCheckable(True)
            if i == 0:
                btn.setProperty("class", "segment-left")
            elif i == len(values) - 1:
                btn.setProperty("class", "segment-right")
            else:
                btn.setProperty("class", "segment")
            btn.clicked.connect(lambda checked, v=val: self._on_click(v))
            layout.addWidget(btn)
            self._buttons[val] = btn
        if default and default in self._buttons:
            self._buttons[default].setChecked(True)

    def value(self) -> str:
        for val, btn in self._buttons.items():
            if btn.isChecked():
                return val
        return ""

    def _on_click(self, val: str):
        for v, btn in self._buttons.items():
            btn.setChecked(v == val)
        self.value_changed.emit(val)


class NestTab(QWidget):
    """Zakładka Nest — rozmieszczanie naklejek na arkuszu."""

    preview_ready = pyqtSignal(object, list, float)  # (job, sheet_pdfs, bleed_mm)

    def __init__(self, log_fn=None, main_window=None, parent=None):
        super().__init__(parent)
        self._log = log_fn or (lambda msg: None)
        self._main_window = main_window
        self._processing = False
        self._last_job = None
        self._last_pdfs = []
        self._last_bleed = 0.0
        self._roll_widths = list(ROLL_PRESETS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # === Header ===
        hdr = QHBoxLayout()
        title = QLabel("Nest")
        title.setProperty("class", "page-title")
        hdr.addWidget(title)
        subtitle = QLabel("  Rozmieszczanie naklejek na arkuszu")
        subtitle.setProperty("class", "page-subtitle")
        hdr.addWidget(subtitle)
        hdr.addStretch()
        layout.addLayout(hdr)

        # === File section (z kopiami) ===
        self._file_section = FileSection(show_copies=True)
        layout.addWidget(self._file_section)

        # === Ustawienia arkusza (card) ===
        sheet_card = QWidget()
        sheet_card.setProperty("class", "card")
        sc_layout = QVBoxLayout(sheet_card)
        sc_layout.setContentsMargins(16, 12, 16, 12)
        sc_layout.setSpacing(8)

        # Row: Tryb (Arkusze / Rola)
        row_mode = QHBoxLayout()
        row_mode.setSpacing(8)
        lbl_mode = QLabel("Tryb")
        lbl_mode.setProperty("class", "field-label")
        row_mode.addWidget(lbl_mode)
        self._mode_seg = SegmentedButton(["Arkusze", "Rola"], default="Arkusze")
        self._mode_seg.value_changed.connect(self._on_mode_change)
        row_mode.addWidget(self._mode_seg)
        row_mode.addStretch()
        sc_layout.addLayout(row_mode)

        # Row: Format — container (sheet / roll frames)
        row_format = QHBoxLayout()
        row_format.setSpacing(8)
        lbl_fmt = QLabel("Format")
        lbl_fmt.setProperty("class", "field-label")
        row_format.addWidget(lbl_fmt)

        # Sheet frame
        self._sheet_frame = QWidget()
        sf_layout = QHBoxLayout(self._sheet_frame)
        sf_layout.setContentsMargins(0, 0, 0, 0)
        sf_layout.setSpacing(4)
        sheet_names = list(SHEET_PRESETS.keys())
        self._sheet_combo = QComboBox()
        self._sheet_combo.addItems(sheet_names)
        self._sheet_combo.setFixedWidth(100)
        self._sheet_combo.currentTextChanged.connect(self._on_sheet_changed)
        sf_layout.addWidget(self._sheet_combo)
        row_format.addWidget(self._sheet_frame)

        # Roll frame
        self._roll_frame = QWidget()
        rf_layout = QHBoxLayout(self._roll_frame)
        rf_layout.setContentsMargins(0, 0, 0, 0)
        rf_layout.setSpacing(4)
        self._roll_combo = QComboBox()
        self._roll_combo.setEditable(True)
        self._roll_combo.addItems([str(w) for w in self._roll_widths])
        self._roll_combo.setFixedWidth(100)
        rf_layout.addWidget(self._roll_combo)
        add_btn = QPushButton("+")
        add_btn.setProperty("class", "ghost")
        add_btn.setFixedSize(24, 24)
        add_btn.clicked.connect(self._roll_add)
        rf_layout.addWidget(add_btn)
        rm_btn = QPushButton("-")
        rm_btn.setProperty("class", "ghost")
        rm_btn.setFixedSize(24, 24)
        rm_btn.clicked.connect(self._roll_remove)
        rf_layout.addWidget(rm_btn)
        rf_layout.addWidget(QLabel("Max"))
        self._roll_max_edit = QLineEdit(str(DEFAULT_ROLL_MAX_LENGTH_MM))
        self._roll_max_edit.setFixedWidth(85)
        rf_layout.addWidget(self._roll_max_edit)
        row_format.addWidget(self._roll_frame)
        self._roll_frame.setVisible(False)

        row_format.addStretch()
        sc_layout.addLayout(row_format)

        # Row: Ploter
        row_plotter = QHBoxLayout()
        row_plotter.setSpacing(8)
        lbl_plotter = QLabel("Ploter")
        lbl_plotter.setProperty("class", "field-label")
        row_plotter.addWidget(lbl_plotter)
        self._plotter_combo = QComboBox()
        self._plotter_combo.addItems(list(PLOTTERS.keys()))
        self._plotter_combo.setCurrentText("jwei")
        self._plotter_combo.setFixedWidth(120)
        row_plotter.addWidget(self._plotter_combo)
        row_plotter.addStretch()
        sc_layout.addLayout(row_plotter)

        layout.addWidget(sheet_card)

        # === Parametry (card) ===
        params_card = QWidget()
        params_card.setProperty("class", "card")
        pc_layout = QVBoxLayout(params_card)
        pc_layout.setContentsMargins(16, 12, 16, 12)
        pc_layout.setSpacing(8)

        # Row: Kopie + Max + Gap
        row_cg = QHBoxLayout()
        row_cg.setSpacing(8)
        lbl_copies = QLabel("Kopie")
        lbl_copies.setProperty("class", "field-label")
        row_cg.addWidget(lbl_copies)
        self._copies_edit = QLineEdit("1")
        self._copies_edit.setFixedWidth(55)
        row_cg.addWidget(self._copies_edit)
        max_btn = QPushButton("Max")
        max_btn.setProperty("class", "toolbar-btn")
        max_btn.clicked.connect(self._calc_max_copies)
        row_cg.addWidget(max_btn)
        row_cg.addSpacing(4)
        lbl_gap = QLabel("Gap")
        lbl_gap.setStyleSheet("min-width: 0; font-size: 13px; font-weight: 500; color: #6e6e73;")
        lbl_gap.setFixedWidth(28)
        row_cg.addWidget(lbl_gap)
        self._gap_edit = QLineEdit(str(DEFAULT_GAP_MM))
        self._gap_edit.setFixedWidth(55)
        row_cg.addWidget(self._gap_edit)
        row_cg.addWidget(QLabel("mm"))
        row_cg.addStretch()
        pc_layout.addLayout(row_cg)

        # Row: Wzory
        row_group = QHBoxLayout()
        row_group.setSpacing(8)
        lbl_group = QLabel("Wzory")
        lbl_group.setProperty("class", "field-label")
        row_group.addWidget(lbl_group)
        self._grouping_seg = SegmentedButton(["Grupuj", "Osobne", "Mieszaj"], default="Grupuj")
        row_group.addWidget(self._grouping_seg)
        row_group.addStretch()
        pc_layout.addLayout(row_group)

        # Row: FlexCut button
        row_flex = QHBoxLayout()
        row_flex.setSpacing(8)
        lbl_flex = QLabel("FlexCut")
        lbl_flex.setProperty("class", "field-label")
        row_flex.addWidget(lbl_flex)
        self._flexcut_btn = QPushButton("FlexCut...")
        self._flexcut_btn.setObjectName("outline")
        self._flexcut_btn.clicked.connect(self._open_flexcut)
        row_flex.addWidget(self._flexcut_btn)
        row_flex.addStretch()
        pc_layout.addLayout(row_flex)

        # Checkbox: Biały poddruk
        self._white_cb = QCheckBox("Biały poddruk (White)")
        pc_layout.addWidget(self._white_cb)

        # Row: Output
        out_row = QHBoxLayout()
        out_row.setSpacing(8)
        out_lbl = QLabel("Output")
        out_lbl.setProperty("class", "field-label")
        out_row.addWidget(out_lbl)
        self._output_edit = QLineEdit("")
        self._output_edit.setPlaceholderText("Katalog pliku wejściowego")
        self._output_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        out_row.addWidget(self._output_edit)
        browse_btn = QPushButton("...")
        browse_btn.setFixedWidth(30)
        browse_btn.clicked.connect(self._browse_output)
        out_row.addWidget(browse_btn)
        pc_layout.addLayout(out_row)

        layout.addWidget(params_card)

        # === Action bar ===
        bar = QHBoxLayout()
        bar.setSpacing(8)

        self._nest_btn = QPushButton("Generuj arkusze")
        self._nest_btn.setObjectName("primary")
        self._nest_btn.clicked.connect(self._on_run)
        bar.addWidget(self._nest_btn)

        self._progress = QProgressBar()
        self._progress.setFixedWidth(150)
        self._progress.setFixedHeight(8)
        self._progress.setValue(0)
        self._progress.setVisible(False)
        bar.addWidget(self._progress)

        self._status_label = QLabel("")
        self._status_label.setProperty("class", "subheader")
        bar.addWidget(self._status_label)
        bar.addStretch()

        layout.addLayout(bar)
        layout.addStretch()

        # Aktualizuj output po dodaniu plików
        self._file_section.files_changed.connect(self._on_files_changed)

    # --- Public API ---

    def clear(self):
        """Wyczyść pliki i zresetuj output."""
        self._file_section.clear_files()
        self._output_edit.setText("")
        self._status_label.setText("")
        self._last_job = None
        self._last_pdfs = []

    @property
    def files(self) -> list[str]:
        return self._file_section.files

    @property
    def plotter(self) -> str:
        return self._plotter_combo.currentText()

    @property
    def gap_mm(self) -> float:
        try:
            return float(self._gap_edit.text())
        except ValueError:
            return DEFAULT_GAP_MM

    @property
    def copies(self) -> int:
        try:
            return max(1, int(self._copies_edit.text()))
        except ValueError:
            return 1

    @property
    def output_dir(self) -> str:
        txt = self._output_edit.text().strip()
        if txt:
            return txt
        if self.files:
            return os.path.dirname(self.files[0])
        return os.path.join(os.path.dirname(os.path.dirname(__file__)), "output")

    def _on_files_changed(self):
        """Ustaw output na katalog ostatnio dodanego pliku."""
        if self.files:
            self._output_edit.setText(os.path.dirname(self.files[-1]))

    def add_files(self, paths: list[str]):
        """Dodaj pliki programowo (np. z bleed tab)."""
        self._file_section._add_files(paths)

    # --- Mode switching ---

    def _on_mode_change(self, mode: str):
        is_sheet = mode == "Arkusze"
        self._sheet_frame.setVisible(is_sheet)
        self._roll_frame.setVisible(not is_sheet)
        if is_sheet:
            self._on_sheet_changed(self._sheet_combo.currentText())
        else:
            # Rola → automatycznie Summa S3
            self._plotter_combo.setCurrentText("summa_s3")

    def _on_sheet_changed(self, name: str):
        if name in ("SRA3", "SRA3+"):
            self._plotter_combo.setCurrentText("jwei")
        else:
            self._plotter_combo.setCurrentText("summa_s3")

    # --- Roll width management ---

    def _roll_add(self):
        try:
            val = int(float(self._roll_combo.currentText()))
        except ValueError:
            return
        if 50 <= val <= 5000 and val not in self._roll_widths:
            self._roll_widths.append(val)
            self._roll_widths.sort()
            self._roll_combo.clear()
            self._roll_combo.addItems([str(w) for w in self._roll_widths])
            self._roll_combo.setCurrentText(str(val))
            self._log(f"Rolka: dodano szerokość {val}mm")

    def _roll_remove(self):
        try:
            val = int(float(self._roll_combo.currentText()))
        except ValueError:
            return
        if val in self._roll_widths and len(self._roll_widths) > 1:
            self._roll_widths.remove(val)
            self._roll_combo.clear()
            self._roll_combo.addItems([str(w) for w in self._roll_widths])
            self._log(f"Rolka: usunięto szerokość {val}mm")

    # --- Max copies ---

    def _calc_max_copies(self):
        """Oblicza maks. liczbę kopii mieszczących się na JEDNYM arkuszu.

        Używa prawdziwego nest_job (binary search) zamiast formuły grid —
        zapewnia zgodność z faktycznym wynikiem nestowania.
        """
        if not self.files:
            self._log("Max: brak plików")
            return
        sheet_w, sheet_h = self._get_sheet_size()
        if sheet_w <= 0:
            return
        plotter_cfg = PLOTTERS.get(self.plotter, {})
        mark_zone = plotter_cfg.get("mark_zone_mm", DEFAULT_MARK_ZONE_MM)
        leading_offset = plotter_cfg.get("leading_offset_mm", 0)
        side_offset = plotter_cfg.get("side_offset_mm", 0)
        # Te same transformacje co w NestWorker._run_inner
        nest_w = sheet_w - 2 * side_offset
        nest_h = sheet_h
        gap = self.gap_mm

        import fitz
        from models import Sticker, Job
        from modules.nesting import nest_job

        try:
            doc = fitz.open(self.files[0])
            pg = doc[0]
            fw = pg.rect.width * PT_TO_MM
            fh = pg.rect.height * PT_TO_MM
            cw_pt = pg.rect.width
            ch_pt = pg.rect.height
            doc.close()
        except Exception as e:
            self._log(f"Max: błąd — {e}")
            return

        # Minimalny stub cut_segments (nesting potrzebuje tylko wymiarów)
        cut_segs = [
            ('l', (0, 0), (cw_pt, 0)),
            ('l', (cw_pt, 0), (cw_pt, ch_pt)),
            ('l', (cw_pt, ch_pt), (0, ch_pt)),
            ('l', (0, ch_pt), (0, 0)),
        ]
        st = Sticker(
            source_path=self.files[0], page_index=0,
            width_mm=fw, height_mm=fh,
            cut_segments=cut_segs, bleed_segments=[],
            edge_color_rgb=(1, 1, 1), edge_color_cmyk=(0, 0, 0, 0),
        )

        def fits_on_one_sheet(n: int) -> bool:
            try:
                job = Job(stickers=[(st, n)], plotter=self.plotter)
                job = nest_job(
                    job,
                    sheet_width_mm=nest_w,
                    sheet_height_mm=nest_h,
                    gap_mm=gap,
                    mark_zone_mm=mark_zone,
                    bleed_mm=0,
                    grouping_mode={"Grupuj": "group", "Osobne": "separate", "Mieszaj": "mix"}.get(
                        self._grouping_seg.value(), "group"),
                )
                placed = sum(len(s.placements) for s in job.sheets)
                return len(job.sheets) <= 1 and placed >= n
            except Exception:
                return False

        # Binary search: górne oszacowanie z gridu, potem zawężamy
        upper = max(
            1,
            int(math.floor((nest_w - 2 * mark_zone - 10 + gap) / max(0.1, min(fw, fh) + gap)))
            * int(math.floor((nest_h - 2 * mark_zone - 10 + gap) / max(0.1, min(fw, fh) + gap))),
        )
        upper = max(upper, 1)

        lo, hi = 1, upper
        best = 1 if fits_on_one_sheet(1) else 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if fits_on_one_sheet(mid):
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1

        n_max = max(best, 1)
        self._copies_edit.setText(str(n_max))
        self._log(f"Max: {n_max} kopii ({fw:.0f}x{fh:.0f}mm na arkuszu {sheet_w:.0f}x{sheet_h:.0f}mm)")

    def _get_sheet_size(self) -> tuple[float, float]:
        mode = self._mode_seg.value()
        if mode == "Arkusze":
            preset = self._sheet_combo.currentText()
            if preset in SHEET_PRESETS:
                return SHEET_PRESETS[preset]
            return (320, 450)
        else:
            try:
                w = float(self._roll_combo.currentText())
                h = float(self._roll_max_edit.text())
                return (w, h)
            except ValueError:
                return (0, 0)

    # --- FlexCut ---

    def _open_flexcut(self):
        if not self._last_job or not self._last_pdfs:
            self._log("FlexCut: najpierw wygeneruj arkusze")
            return
        from gui.flexcut_dialog import FlexCutDialog
        dlg = FlexCutDialog(
            self._last_job, self._last_pdfs, self._last_bleed,
            reexport_fn=self._reexport_sheet,
            reexport_cut_fn=self._reexport_cut_only,
            reexport_fast_fn=self._reexport_fast,
            log_fn=self._log, parent=self,
        )
        dlg.exec()

    def _reexport_sheet(self, idx: int):
        """Re-export jednego arkusza (print + cut + white)."""
        if not self._last_job or idx >= len(self._last_job.sheets):
            return
        from modules.marks import generate_marks
        from modules.export import export_sheet
        sheet = self._last_job.sheets[idx]
        sheet = generate_marks(sheet, plotter=self.plotter)
        self._last_job.sheets[idx] = sheet
        pp, cp = self._last_pdfs[idx]
        white = self._white_cb.isChecked()
        wp = pp.replace("_print.pdf", "_white.pdf") if white else None
        export_sheet(sheet, pp, cp, bleed_mm=0, plotter=self.plotter,
                     white=white, white_output_path=wp)
        self._log(f"  Re-export arkusz {idx + 1}: OK")

    def _reexport_cut_only(self, idx: int):
        """Re-export TYLKO cut PDF jednego arkusza (szybki — bez print/white)."""
        if not self._last_job or idx >= len(self._last_job.sheets):
            return
        from modules.export import export_sheet_cut
        sheet = self._last_job.sheets[idx]
        _, cp = self._last_pdfs[idx]
        export_sheet_cut(sheet, cp, bleed_mm=0, plotter=self.plotter)

    def _reexport_fast(self, idx: int):
        """Re-export print + cut (bez white, bez regeneracji markerów).

        Szybszy od pełnego reexport — używany przy rotate/bleed w FlexCut.
        """
        if not self._last_job or idx >= len(self._last_job.sheets):
            return
        from modules.export import export_sheet_print, export_sheet_cut
        sheet = self._last_job.sheets[idx]
        pp, cp = self._last_pdfs[idx]
        export_sheet_print(sheet, pp, bleed_mm=0)
        export_sheet_cut(sheet, cp, bleed_mm=0, plotter=self.plotter)

    # --- Browse ---

    def _browse_output(self):
        d = QFileDialog.getExistingDirectory(self, "Folder wyjściowy", self._output_edit.text())
        if d:
            self._output_edit.setText(d)

    # --- Run ---

    def _on_run(self):
        if self._processing or not self.files:
            return
        self._processing = True
        self._nest_btn.setEnabled(False)
        self._nest_btn.setText("Rozmieszczam...")
        self._progress.setVisible(True)
        self._progress.setValue(0)

        sheet_w, sheet_h = self._get_sheet_size()
        mode = self._mode_seg.value()

        from gui.workers import NestWorker
        self._worker = NestWorker(
            files=self.files,
            file_copies=self._file_section.file_copies,
            output_dir=self.output_dir,
            sheet_w=sheet_w,
            sheet_h=sheet_h if mode == "Arkusze" else None,
            max_sheet_length=sheet_h if mode == "Rola" else None,
            copies=self.copies,
            gap=self.gap_mm,
            plotter=self.plotter,
            grouping_mode={"Grupuj": "group", "Osobne": "separate", "Mieszaj": "mix"}.get(
                self._grouping_seg.value(), "group"),
            white=self._white_cb.isChecked(),
        )
        self._worker.log_message.connect(self._log)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_done)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_progress(self, current: int, total: int):
        if total > 0:
            self._progress.setValue(int(100 * current / total))
        self._status_label.setText(f"Plik {current}/{total}...")

    def _on_done(self, job, sheet_pdfs):
        self._processing = False
        self._nest_btn.setEnabled(True)
        self._nest_btn.setText("Generuj arkusze")
        self._progress.setVisible(False)
        total = sum(len(s.placements) for s in job.sheets)
        self._status_label.setText(f"Gotowe — {total} naklejek na {len(job.sheets)} arkusz(ach)")
        self._last_job = job
        self._last_pdfs = sheet_pdfs
        self._last_bleed = 0.0
        self.preview_ready.emit(job, sheet_pdfs, 0.0)

    def _on_error(self, msg: str):
        self._processing = False
        self._nest_btn.setEnabled(True)
        self._nest_btn.setText("Generuj arkusze")
        self._progress.setVisible(False)
        self._status_label.setText("BŁĄD")
        self._log(f"[BŁĄD KRYTYCZNY] {msg}")
        short = msg if len(msg) <= 600 else msg[:600] + "\n\n[...] pełny log w panelu na dole."
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("Błąd rozkładania arkuszy")
        box.setText("Nie udało się wygenerować arkusza.")
        box.setInformativeText(short)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()
