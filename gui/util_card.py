"""
Bleed Tool — util_card.py
===========================
Karta utylizacji materialu (Technikadruku style).

Layout: giant mono % po lewej + 6px progress bar + 3-col stat grid po prawej.
Gradient top-border (green→blue) aplikowany przez QSS.
"""

from PyQt6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar, QGridLayout, QWidget,
)
from PyQt6.QtCore import Qt

from gui.atoms import set_prop


class UtilCard(QFrame):
    """Karta utylizacji: giant %, progress bar, 3-col stat grid."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("UtilCard")

        root = QHBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(22)

        # === Lewa kolumna: giant % + label ===
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(2)

        pct_row = QHBoxLayout()
        pct_row.setContentsMargins(0, 0, 0, 0)
        pct_row.setSpacing(4)
        self._pct = QLabel("—")
        self._pct.setObjectName("UtilValue")
        self._pct.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
        pct_row.addWidget(self._pct)
        self._suffix = QLabel("%")
        self._suffix.setObjectName("UtilSuffix")
        self._suffix.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
        pct_row.addWidget(self._suffix)
        pct_row.addStretch(1)
        left.addLayout(pct_row)

        self._caption = QLabel("UTYLIZACJA ARKUSZA")
        self._caption.setObjectName("UtilLabel")
        left.addWidget(self._caption)

        root.addLayout(left)

        # === Prawa kolumna: progress bar + stat grid ===
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(12)

        self._bar = QProgressBar()
        self._bar.setObjectName("UtilBar")
        self._bar.setTextVisible(False)
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        right.addWidget(self._bar)

        # Stat grid 3 cols
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(2)

        def _stat(col: int, key_text: str) -> QLabel:
            k = QLabel(key_text)
            k.setObjectName("MetaKey")
            grid.addWidget(k, 0, col)
            v = QLabel("—")
            v.setObjectName("MetaVal")
            grid.addWidget(v, 1, col)
            return v

        self._v_stickers = _stat(0, "NAKLEJEK")
        self._v_sheets = _stat(1, "ARKUSZY")
        self._v_area = _stat(2, "POWIERZCHNIA")

        right.addLayout(grid)
        root.addLayout(right, stretch=1)

    def clear(self):
        self._pct.setText("—")
        self._v_stickers.setText("—")
        self._v_sheets.setText("—")
        self._v_area.setText("—")
        self._bar.setValue(0)

    def set_data(
        self,
        util_sheet_pct: float,
        util_print_pct: float,
        used_mm2: float,
        sheet_total_mm2: float,
        sheets_count: int,
        placements_count: int,
    ):
        pct = max(0.0, min(100.0, util_sheet_pct))
        self._pct.setText(f"{pct:.0f}")
        self._bar.setValue(int(round(pct)))
        self._v_stickers.setText(f"{placements_count} szt.")
        self._v_sheets.setText(f"{sheets_count}")
        used_m2 = used_mm2 / 1_000_000.0
        total_m2 = sheet_total_mm2 / 1_000_000.0
        self._v_area.setText(f"{used_m2:.3f} / {total_m2:.3f} m²")

        # Auto-status na % value (kolor)
        if pct >= 65:
            set_prop(self._v_area, "status", "ok")
        elif pct < 45:
            set_prop(self._v_area, "status", "warn")
        else:
            set_prop(self._v_area, "status", "")
