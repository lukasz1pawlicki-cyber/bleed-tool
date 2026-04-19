"""
Bleed Tool — util_card.py
===========================
Karta utylizacji materialu (duze %, pasek, podsumowanie).
"""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar,
)
from PyQt6.QtCore import Qt


class UtilCard(QWidget):
    """Karta z dużym procentem utylizacji + paskiem + krótkim opisem."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("class", "util-card")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(6)

        # Header (caption)
        self._caption = QLabel("Utylizacja materiału")
        self._caption.setProperty("class", "util-caption")
        layout.addWidget(self._caption)

        # Wielki procent + sub
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        self._pct = QLabel("—")
        self._pct.setProperty("class", "util-pct")
        self._pct.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self._pct)

        self._sub = QLabel("")
        self._sub.setProperty("class", "util-sub")
        self._sub.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._sub.setWordWrap(True)
        row.addWidget(self._sub, stretch=1)
        layout.addLayout(row)

        # Pasek
        self._bar = QProgressBar()
        self._bar.setProperty("class", "util-bar")
        self._bar.setTextVisible(False)
        self._bar.setMinimum(0)
        self._bar.setMaximum(100)
        self._bar.setValue(0)
        self._bar.setFixedHeight(8)
        layout.addWidget(self._bar)

        # Dodatkowa linia (np. area)
        self._detail = QLabel("")
        self._detail.setProperty("class", "util-sub")
        layout.addWidget(self._detail)

        self.clear()

    def clear(self):
        self._pct.setText("—")
        self._sub.setText("")
        self._detail.setText("")
        self._bar.setValue(0)
        self._set_grade("ok")

    def set_data(
        self,
        util_sheet_pct: float,
        util_print_pct: float,
        used_mm2: float,
        sheet_total_mm2: float,
        sheets_count: int,
        placements_count: int,
    ):
        """Ustaw wszystkie wartosci na karcie."""
        pct = max(0.0, min(100.0, util_sheet_pct))
        self._pct.setText(f"{pct:.0f}%")
        self._sub.setText(
            f"{placements_count} naklejek · {sheets_count} arkusz(y)\n"
            f"{util_print_pct:.0f}% obszaru drukowania"
        )
        used_m2 = used_mm2 / 1_000_000.0
        total_m2 = sheet_total_mm2 / 1_000_000.0
        self._detail.setText(f"Powierzchnia: {used_m2:.3f} / {total_m2:.3f} m²")
        self._bar.setValue(int(round(pct)))
        if pct >= 65:
            self._set_grade("good")
        elif pct >= 45:
            self._set_grade("ok")
        else:
            self._set_grade("low")

    def _set_grade(self, grade: str):
        for w in (self._pct, self._bar):
            w.setProperty("grade", grade)
            w.style().unpolish(w)
            w.style().polish(w)
