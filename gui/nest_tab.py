"""
Bleed Tool — nest_tab.py
===========================
Zakladka Nest: rozmieszczanie naklejek na arkuszu. Technikadruku QSS.
"""

import os
import math
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QLineEdit, QCheckBox, QComboBox, QProgressBar, QFileDialog,
    QSizePolicy, QMessageBox, QSpinBox, QDoubleSpinBox, QScrollArea,
)
from PyQt6.QtCore import pyqtSignal, Qt

from config import (
    DEFAULT_BLEED_MM, DEFAULT_GAP_MM, DEFAULT_MARK_ZONE_MM,
    DEFAULT_ROLL_MAX_LENGTH_MM,
    SHEET_PRESETS, ROLL_PRESETS, PLOTTERS, FLOAT_TOLERANCE_MM,
    PT_TO_MM,
)
from gui.file_section import FileSection
from gui.util_card import UtilCard
from gui.atoms import (
    Segmented, IconButton, make_button, FieldLabel, UnitLabel,
)
from gui.widgets_common import PageTitleBar, CardSection, ActionBar
from gui import settings as _settings


class NestTab(QWidget):
    """Zakladka Nest."""

    preview_ready = pyqtSignal(object, list, float)

    def __init__(self, log_fn=None, main_window=None, parent=None):
        super().__init__(parent)
        self._log = log_fn or (lambda msg: None)
        self._main_window = main_window
        self._processing = False
        self._last_job = None
        self._last_pdfs = []
        self._last_bleed = 0.0
        self._roll_widths = list(ROLL_PRESETS)
        _saved = _settings.load().get("nest", {})

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # === Page title bar ===
        self._title_bar = PageTitleBar(
            crumb="Workflow · Krok 02",
            title="Nest",
            help_tip="Rozmieszczanie naklejek na arkuszu",
            help_text=(
                "Krok 02 — Nest\n\n"
                "Wejście: PDF-y z bleedem (z Kroku 01).\n"
                "Wyjście: PDF-y arkuszy z print + cut + (opc.) white + OPOS markery.\n\n"
                "Tryb:\n"
                "  • Arkusze — preset (SRA3, SRA3+, ...). Plotter JWEI 0806.\n"
                "  • Rola — szerokość + max długość. Plotter Summa S3.\n\n"
                "Rozkład:\n"
                "  • Kopie — liczba na plik; Max = binary search dla jednego arkusza.\n"
                "  • Gap — odstęp między naklejkami (mm).\n"
                "  • Wzory — Grupuj / Osobne / Mieszaj.\n"
                "    - Grupuj: każdy wzór trzymany razem\n"
                "    - Osobne: każdy wzór na osobnym arkuszu\n"
                "    - Mieszaj: cross-group backfill\n\n"
                "Utylizacja arkusza ≥ 65% = dobrze, 45-65% ok, < 45% niska.\n"
                "FlexCut — interaktywne zaznaczanie (mostki, rotacja 180°, spad)."
            ),
        )
        root.addWidget(self._title_bar)

        # === Scroll ===
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
            "Pliki do arkusza",
            aux="po bleedzie · kopie per plik",
        )
        self._file_section = FileSection(show_copies=True)
        files_card.body.addWidget(self._file_section)
        layout.addWidget(files_card)

        # === Sheet / Roll card ===
        sheet_card = CardSection("Arkusz / Rola")

        # Tryb (Segmented)
        row_mode = QHBoxLayout()
        row_mode.setSpacing(8)
        row_mode.addWidget(self._field_label("Tryb"))
        self._mode_seg = Segmented(
            ["Arkusze", "Rola"],
            default=_saved.get("mode", "Arkusze"),
        )
        self._mode_seg.currentTextChanged.connect(self._on_mode_change)
        row_mode.addWidget(self._mode_seg)
        row_mode.addStretch(1)
        sheet_card.body.addLayout(row_mode)

        # Format
        row_fmt = QHBoxLayout()
        row_fmt.setSpacing(8)
        row_fmt.addWidget(self._field_label("Format"))

        # Sheet frame
        self._sheet_frame = QWidget()
        sf = QHBoxLayout(self._sheet_frame)
        sf.setContentsMargins(0, 0, 0, 0)
        sf.setSpacing(6)
        sheet_names = list(SHEET_PRESETS.keys())
        self._sheet_combo = QComboBox()
        self._sheet_combo.setProperty("variant", "mono")
        self._sheet_combo.addItems(sheet_names)
        self._sheet_combo.setFixedWidth(160)
        _sp = _saved.get("sheet_preset")
        if _sp and _sp in sheet_names:
            self._sheet_combo.setCurrentText(_sp)
        self._sheet_combo.currentTextChanged.connect(self._on_sheet_changed)
        sf.addWidget(self._sheet_combo)
        row_fmt.addWidget(self._sheet_frame)

        # Roll frame
        self._roll_frame = QWidget()
        rf = QHBoxLayout(self._roll_frame)
        rf.setContentsMargins(0, 0, 0, 0)
        rf.setSpacing(6)
        self._roll_combo = QComboBox()
        self._roll_combo.setProperty("variant", "mono")
        self._roll_combo.setEditable(True)
        self._roll_combo.addItems([str(w) for w in self._roll_widths])
        self._roll_combo.setFixedWidth(110)
        rf.addWidget(self._roll_combo)
        add_btn = IconButton("+", tip="Dodaj szerokość")
        add_btn.clicked.connect(self._roll_add)
        rf.addWidget(add_btn)
        rm_btn = IconButton("−", tip="Usuń szerokość")
        rm_btn.clicked.connect(self._roll_remove)
        rf.addWidget(rm_btn)
        rf.addWidget(UnitLabel("max"))
        self._roll_max_edit = QLineEdit(str(DEFAULT_ROLL_MAX_LENGTH_MM))
        self._roll_max_edit.setFixedWidth(90)
        self._roll_max_edit.setProperty("variant", "mono")
        rf.addWidget(self._roll_max_edit)
        row_fmt.addWidget(self._roll_frame)
        self._roll_frame.setVisible(False)
        row_fmt.addStretch(1)
        sheet_card.body.addLayout(row_fmt)

        # Ploter
        row_plot = QHBoxLayout()
        row_plot.setSpacing(8)
        row_plot.addWidget(self._field_label("Ploter"))
        self._plotter_combo = QComboBox()
        self._plotter_combo.setProperty("variant", "mono")
        self._plotter_combo.addItems(list(PLOTTERS.keys()))
        _pl = _saved.get("plotter", "jwei")
        if _pl in PLOTTERS:
            self._plotter_combo.setCurrentText(_pl)
        else:
            self._plotter_combo.setCurrentText("jwei")
        self._plotter_combo.setFixedWidth(160)
        row_plot.addWidget(self._plotter_combo)
        row_plot.addStretch(1)
        sheet_card.body.addLayout(row_plot)

        layout.addWidget(sheet_card)

        # === Rozklad card ===
        params_card = CardSection(
            "Rozkład",
            aux="shelf nesting + backfill",
        )

        # Kopie + Max + Gap — QGridLayout dla idealnego wyrownania wierszy
        grid_cg = QGridLayout()
        grid_cg.setContentsMargins(0, 0, 0, 0)
        grid_cg.setHorizontalSpacing(8)
        grid_cg.setVerticalSpacing(0)

        # Wiersz: [Kopie | spin | Max] [gap 12px] [Gap | spin | mm] [stretch]
        lbl_copies = self._field_label("Kopie")
        lbl_copies.setFixedHeight(26)
        grid_cg.addWidget(lbl_copies, 0, 0, Qt.AlignmentFlag.AlignVCenter)

        self._copies_spin = QSpinBox()
        self._copies_spin.setMinimum(1)
        self._copies_spin.setMaximum(9999)
        self._copies_spin.setValue(1)
        self._copies_spin.setFixedSize(80, 26)
        grid_cg.addWidget(self._copies_spin, 0, 1, Qt.AlignmentFlag.AlignVCenter)

        max_btn = make_button("Max", variant="ghost", size="sm")
        max_btn.setFixedSize(56, 26)
        max_btn.setStyleSheet(
            "QPushButton{min-height:26px;max-height:26px;padding:0 10px;"
            "font-size:11px;}"
        )
        max_btn.clicked.connect(self._calc_max_copies)
        grid_cg.addWidget(max_btn, 0, 2, Qt.AlignmentFlag.AlignVCenter)

        # 12px gap miedzy Kopie-sekcja a Gap-sekcja
        grid_cg.setColumnMinimumWidth(3, 12)

        gap_lbl = QLabel("Gap")
        gap_lbl.setObjectName("FieldLabel")
        gap_lbl.setFixedHeight(26)
        grid_cg.addWidget(gap_lbl, 0, 4, Qt.AlignmentFlag.AlignVCenter)

        self._gap_spin = QSpinBox()
        self._gap_spin.setRange(0, 100)
        self._gap_spin.setSingleStep(1)
        self._gap_spin.setValue(int(round(float(_saved.get("gap_mm", DEFAULT_GAP_MM)))))
        self._gap_spin.setFixedSize(80, 26)
        grid_cg.addWidget(self._gap_spin, 0, 5, Qt.AlignmentFlag.AlignVCenter)

        mm_lbl = UnitLabel("mm")
        mm_lbl.setFixedHeight(26)
        grid_cg.addWidget(mm_lbl, 0, 6, Qt.AlignmentFlag.AlignVCenter)

        # stretch
        grid_cg.setColumnStretch(7, 1)
        params_card.body.addLayout(grid_cg)

        # Grupowanie (accent Segmented)
        row_group = QHBoxLayout()
        row_group.setSpacing(8)
        row_group.addWidget(self._field_label("Wzory"))
        self._grouping_seg = Segmented(
            ["Grupuj", "Osobne", "Mieszaj"],
            accent=True,
            default=_saved.get("grouping", "Grupuj"),
        )
        row_group.addWidget(self._grouping_seg)
        row_group.addStretch(1)
        self._white_cb = QCheckBox("Biały poddruk")
        self._white_cb.setChecked(bool(_saved.get("white", False)))
        row_group.addWidget(self._white_cb)
        params_card.body.addLayout(row_group)

        # FlexCut + Output
        row_tools = QHBoxLayout()
        row_tools.setSpacing(8)
        row_tools.addWidget(self._field_label("Narzędzia"))
        self._flexcut_btn = make_button("FlexCut…", variant="secondary", size="sm")
        self._flexcut_btn.clicked.connect(self._open_flexcut)
        row_tools.addWidget(self._flexcut_btn)
        row_tools.addStretch(1)
        params_card.body.addLayout(row_tools)

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

        # === UtilCard (widoczny po gotowym job) ===
        self._util_card = UtilCard()
        self._util_card.setVisible(False)
        layout.addWidget(self._util_card)

        layout.addStretch(1)

        # === Action bar ===
        self._action_bar = ActionBar()
        self._nest_btn = make_button("▶ Generuj arkusze", size="lg")
        self._nest_btn.clicked.connect(self._on_run)
        self._action_bar.body.addWidget(self._nest_btn)

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

        # Przywroc widocznosc sheet/roll po trybie
        if self._mode_seg.value() == "Rola":
            self._sheet_frame.setVisible(False)
            self._roll_frame.setVisible(True)

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
        self._last_job = None
        self._last_pdfs = []
        self._util_card.clear()
        self._util_card.setVisible(False)

    @property
    def files(self) -> list[str]:
        return self._file_section.files

    @property
    def plotter(self) -> str:
        return self._plotter_combo.currentText()

    @property
    def gap_mm(self) -> float:
        return float(self._gap_spin.value())

    @property
    def copies(self) -> int:
        return max(1, int(self._copies_spin.value()))

    @property
    def output_dir(self) -> str:
        txt = self._output_edit.text().strip()
        if txt:
            return txt
        if self.files:
            return os.path.dirname(self.files[0])
        return os.path.join(os.path.dirname(os.path.dirname(__file__)), "output")

    def _on_files_changed(self):
        if self.files:
            self._output_edit.setText(os.path.dirname(self.files[-1]))

    def add_files(self, paths: list[str]):
        self._file_section._add_files(paths)

    # --- Mode switching ---

    def _on_mode_change(self, mode: str):
        is_sheet = mode == "Arkusze"
        self._sheet_frame.setVisible(is_sheet)
        self._roll_frame.setVisible(not is_sheet)
        if is_sheet:
            self._on_sheet_changed(self._sheet_combo.currentText())
        else:
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
        self._copies_spin.setValue(n_max)
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
        if not self._last_job or idx >= len(self._last_job.sheets):
            return
        from modules.export import export_sheet_cut
        sheet = self._last_job.sheets[idx]
        _, cp = self._last_pdfs[idx]
        export_sheet_cut(sheet, cp, bleed_mm=0, plotter=self.plotter)

    def _reexport_fast(self, idx: int):
        if not self._last_job or idx >= len(self._last_job.sheets):
            return
        from modules.export import export_sheet_print, export_sheet_cut
        sheet = self._last_job.sheets[idx]
        pp, cp = self._last_pdfs[idx]
        # Fast preview w FlexCut: niższe DPI outer bleed (150 zamiast 300) —
        # EDT 4x szybsza przy A3. Finalny eksport ("Zastosuj") używa pełnych
        # 300 DPI przez _reexport_sheet/export_sheet.
        export_sheet_print(sheet, pp, bleed_mm=0, outer_bleed_dpi=150)
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
        _settings.update({"nest": {
            "plotter": self.plotter,
            "gap_mm": self.gap_mm,
            "grouping": self._grouping_seg.value(),
            "white": self._white_cb.isChecked(),
            "sheet_preset": self._sheet_combo.currentText(),
            "mode": self._mode_seg.value(),
        }})
        self._processing = True
        self._nest_btn.setEnabled(False)
        self._nest_btn.setText("Rozmieszczam…")
        self._progress.setVisible(True)
        self._progress.setValue(0)
        self._file_section.reset_statuses()

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
        self._worker.file_status.connect(self._file_section.set_status)
        self._worker.finished.connect(self._on_done)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_progress(self, current: int, total: int):
        if total > 0:
            self._progress.setValue(int(100 * current / total))
        pct = int(100 * current / max(1, total))
        self._status_label.setText(f"Plik {current} / {total} · {pct}%")

    def _on_done(self, job, sheet_pdfs):
        self._processing = False
        self._nest_btn.setEnabled(True)
        self._nest_btn.setText("▶ Generuj arkusze")
        self._progress.setVisible(False)
        total = sum(len(s.placements) for s in job.sheets)

        if job.sheets:
            used = sum(s.used_area_mm2 for s in job.sheets)
            printable = sum(s.printable_area_mm2 for s in job.sheets)
            sheet_total = sum(s.sheet_area_mm2 for s in job.sheets)
            util_print = 100.0 * used / printable if printable > 0 else 0.0
            util_sheet = 100.0 * used / sheet_total if sheet_total > 0 else 0.0
            emoji = "✓" if util_sheet >= 65 else ("·" if util_sheet >= 45 else "⚠")
            self._status_label.setText(
                f"Gotowe — {total} naklejek · utylizacja {util_sheet:.0f}%"
            )
            self._log(
                f"{emoji} Utylizacja materialu: {util_sheet:.1f}% arkusza "
                f"({util_print:.1f}% obszaru drukowania, {used:.0f}/{sheet_total:.0f} mm²)"
            )
            if util_sheet < 45:
                self._log(
                    "  ⚠ Niska utylizacja — rozwaz zwiekszenie liczby powtorzen "
                    "lub mniejszy format arkusza."
                )
            self._util_card.set_data(
                util_sheet_pct=util_sheet,
                util_print_pct=util_print,
                used_mm2=used,
                sheet_total_mm2=sheet_total,
                sheets_count=len(job.sheets),
                placements_count=total,
            )
            self._util_card.setVisible(True)
        else:
            self._status_label.setText(
                f"Gotowe — {total} naklejek na {len(job.sheets)} arkusz(ach)"
            )
            self._util_card.setVisible(False)

        self._last_job = job
        self._last_pdfs = sheet_pdfs
        self._last_bleed = 0.0
        self.preview_ready.emit(job, sheet_pdfs, 0.0)

    def _on_error(self, msg: str):
        self._processing = False
        self._nest_btn.setEnabled(True)
        self._nest_btn.setText("▶ Generuj arkusze")
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
