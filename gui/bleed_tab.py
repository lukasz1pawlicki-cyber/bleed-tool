"""
Bleed Tool — bleed_tab.py
============================
Zakładka Bleed: drop zone, parametry, generowanie bleed + CutContour.
"""

import os
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QCheckBox, QRadioButton, QButtonGroup,
    QProgressBar, QFileDialog, QSizePolicy,
)
from PyQt6.QtCore import pyqtSignal, Qt

from config import DEFAULT_BLEED_MM
from gui.file_section import FileSection


class BleedTab(QWidget):
    """Zakładka Bleed — pełny formularz z generowaniem."""

    preview_ready = pyqtSignal(list)  # output_paths po zakończeniu

    def __init__(self, log_fn=None, parent=None):
        super().__init__(parent)
        self._log = log_fn or (lambda msg: None)
        self._processing = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # === Header ===
        hdr = QHBoxLayout()
        title = QLabel("Bleed")
        title.setProperty("class", "header")
        hdr.addWidget(title)
        subtitle = QLabel("  Generuj bleed i CutContour")
        subtitle.setProperty("class", "subheader")
        hdr.addWidget(subtitle)
        hdr.addStretch()
        layout.addLayout(hdr)

        # === File section ===
        self._file_section = FileSection(show_copies=False)
        layout.addWidget(self._file_section)

        # === Parametry (card) ===
        card = QWidget()
        card.setProperty("class", "card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 12, 16, 12)
        card_layout.setSpacing(8)

        params_title = QLabel("Parametry")
        params_title.setProperty("class", "subheader")
        font = params_title.font()
        font.setBold(True)
        params_title.setFont(font)
        card_layout.addWidget(params_title)

        # Row: Bleed (mm)
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        lbl1 = QLabel("Bleed (mm)")
        lbl1.setProperty("class", "field-label")
        row1.addWidget(lbl1)
        self._bleed_edit = QLineEdit(str(DEFAULT_BLEED_MM))
        self._bleed_edit.setFixedWidth(70)
        row1.addWidget(self._bleed_edit)
        row1.addStretch()
        card_layout.addLayout(row1)

        # Row: Wysokość (cm)
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        lbl2 = QLabel("Wysokość (cm)")
        lbl2.setProperty("class", "field-label")
        row2.addWidget(lbl2)
        self._height_edit = QLineEdit()
        self._height_edit.setFixedWidth(70)
        self._height_edit.setPlaceholderText("auto")
        row2.addWidget(self._height_edit)
        row2.addStretch()
        card_layout.addLayout(row2)

        # Checkbox: Czarny -> 100% K
        self._black_100k_cb = QCheckBox("Czarny -> 100% K")
        self._black_100k_cb.setEnabled(False)
        card_layout.addWidget(self._black_100k_cb)

        # Radio: Linia cięcia
        cut_row = QHBoxLayout()
        cut_row.setSpacing(8)
        cut_lbl = QLabel("Linia cięcia:")
        cut_lbl.setProperty("class", "field-label")
        cut_row.addWidget(cut_lbl)
        self._cutline_group = QButtonGroup(self)
        self._rb_kisscut = QRadioButton("Kiss-Cut")
        self._rb_flexcut = QRadioButton("FlexCut")
        self._rb_nocut = QRadioButton("Brak")
        self._rb_kisscut.setChecked(True)
        self._cutline_group.addButton(self._rb_kisscut, 0)
        self._cutline_group.addButton(self._rb_flexcut, 1)
        self._cutline_group.addButton(self._rb_nocut, 2)
        cut_row.addWidget(self._rb_kisscut)
        cut_row.addWidget(self._rb_flexcut)
        cut_row.addWidget(self._rb_nocut)
        cut_row.addStretch()
        card_layout.addLayout(cut_row)

        # Checkbox: Biały poddruk
        self._white_cb = QCheckBox("Biały poddruk (White)")
        card_layout.addWidget(self._white_cb)

        # Row: Output
        out_row = QHBoxLayout()
        out_row.setSpacing(8)
        out_lbl = QLabel("Output")
        out_lbl.setProperty("class", "field-label")
        out_row.addWidget(out_lbl)
        self._output_edit = QLineEdit(os.path.join(os.path.dirname(os.path.dirname(__file__)), "output"))
        self._output_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        out_row.addWidget(self._output_edit)
        browse_btn = QPushButton("...")
        browse_btn.setFixedWidth(30)
        browse_btn.clicked.connect(self._browse_output)
        out_row.addWidget(browse_btn)
        card_layout.addLayout(out_row)

        layout.addWidget(card)

        # === Action bar ===
        bar = QHBoxLayout()
        bar.setSpacing(8)

        self._run_btn = QPushButton("Generuj bleed")
        self._run_btn.setObjectName("primary")
        self._run_btn.clicked.connect(self._on_run)
        bar.addWidget(self._run_btn)

        self._preflight_btn = QPushButton("Preflight")
        self._preflight_btn.setObjectName("outline")
        self._preflight_btn.clicked.connect(self._on_preflight)
        bar.addWidget(self._preflight_btn)

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

        # Enable black_100k when PDF/SVG loaded
        self._file_section.files_changed.connect(self._on_files_changed)

    # --- Properties ---

    @property
    def files(self) -> list[str]:
        return self._file_section.files

    @property
    def bleed_mm(self) -> float:
        try:
            return max(0.0, float(self._bleed_edit.text()))
        except ValueError:
            return DEFAULT_BLEED_MM

    @property
    def cutline_mode(self) -> str:
        checked = self._cutline_group.checkedId()
        return {0: "kiss-cut", 1: "flexcut", 2: "none"}.get(checked, "kiss-cut")

    @property
    def output_dir(self) -> str:
        return self._output_edit.text()

    # --- Callbacks ---

    def _on_files_changed(self):
        has_pdf = any(p.lower().endswith(('.pdf', '.svg')) for p in self.files)
        self._black_100k_cb.setEnabled(has_pdf)
        if not has_pdf:
            self._black_100k_cb.setChecked(False)

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

    def _on_run(self):
        if self._processing or not self.files:
            return
        self._processing = True
        self._run_btn.setEnabled(False)
        self._run_btn.setText("Przetwarzam...")
        self._progress.setVisible(True)
        self._progress.setValue(0)
        self._status_label.setText("")

        from gui.workers import BleedWorker
        self._worker = BleedWorker(
            files=self.files,
            output_dir=self.output_dir,
            bleed_mm=self.bleed_mm,
            black_100k=self._black_100k_cb.isChecked(),
            cutline_mode=self.cutline_mode,
            target_height_mm=self._parse_height(),
            white=self._white_cb.isChecked(),
        )
        self._worker.log_message.connect(self._log)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_done)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _parse_height(self) -> float | None:
        txt = self._height_edit.text().strip()
        if not txt:
            return None
        try:
            return float(txt.replace(",", ".")) * 10.0  # cm → mm
        except ValueError:
            return None

    def _on_progress(self, current: int, total: int):
        if total > 0:
            self._progress.setValue(int(100 * current / total))
        self._status_label.setText(f"Plik {current}/{total}...")

    def _on_done(self, output_paths: list):
        self._processing = False
        self._run_btn.setEnabled(True)
        self._run_btn.setText("Generuj bleed")
        self._progress.setVisible(False)
        n = len(output_paths)
        self._status_label.setText(f"Gotowe — {n} plik(ów)")
        if output_paths:
            self.preview_ready.emit(output_paths)

    def _on_error(self, msg: str):
        self._processing = False
        self._run_btn.setEnabled(True)
        self._run_btn.setText("Generuj bleed")
        self._progress.setVisible(False)
        self._status_label.setText("BŁĄD")
        self._log(f"[BŁĄD KRYTYCZNY] {msg}")
