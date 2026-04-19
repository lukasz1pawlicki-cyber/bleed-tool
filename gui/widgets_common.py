"""
Bleed Tool — widgets_common.py
================================
Reusable section widgets: PageTitleBar, CardSection, ActionBar.
Zgodne z QSS Technikadruku (objectName-driven).
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QWidget, QSizePolicy,
)
from PyQt6.QtCore import Qt

from gui.atoms import IconButton, hline_dashed


class PageTitleBar(QFrame):
    """Bialy header strony: crumb (mono caps) + H1 + opcjonalny IconButton.

    Uzywany na gorze BleedTab / NestTab.
    """

    def __init__(self, crumb: str, title: str, help_tip: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("PageTitleBar")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 14, 22, 16)
        lay.setSpacing(4)

        # Crumb row
        crumb_row = QHBoxLayout()
        crumb_row.setContentsMargins(0, 0, 0, 0)
        crumb_row.setSpacing(8)
        dash = QLabel()
        dash.setFixedSize(16, 2)
        dash.setStyleSheet("background:#2563EB;")
        crumb_row.addWidget(dash, alignment=Qt.AlignmentFlag.AlignVCenter)
        crumb_label = QLabel(crumb)
        crumb_label.setObjectName("PageCrumb")
        crumb_row.addWidget(crumb_label)
        crumb_row.addStretch(1)
        if help_tip:
            self.help_btn = IconButton("?", tip=help_tip)
            crumb_row.addWidget(self.help_btn)
        lay.addLayout(crumb_row)

        # Title
        title_lbl = QLabel(title)
        title_lbl.setObjectName("PageTitle")
        lay.addWidget(title_lbl)


class CardSection(QFrame):
    """Standardowa karta z section label + aux text + dashed divider.

    Uzycie:
        card = CardSection("PARAMETRY BLEEDA", aux="pipeline: ...")
        card.body.addWidget(...)
        card.body.addLayout(...)
    """

    def __init__(self, title: str, aux: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("Card")

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 14)
        root.setSpacing(8)

        # Header row
        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 0, 0)
        hdr.setSpacing(8)
        dash = QLabel()
        dash.setFixedSize(16, 2)
        dash.setStyleSheet("background:#2563EB;")
        hdr.addWidget(dash, alignment=Qt.AlignmentFlag.AlignVCenter)
        lbl = QLabel(title.upper())
        lbl.setObjectName("CardSectionLabel")
        hdr.addWidget(lbl)
        hdr.addStretch(1)
        if aux:
            aux_lbl = QLabel(aux)
            aux_lbl.setObjectName("CardSectionAux")
            hdr.addWidget(aux_lbl)
        root.addLayout(hdr)

        # Dashed divider
        root.addWidget(hline_dashed())

        # Body container (public)
        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(10)
        root.addLayout(self.body)


class FieldRow(QHBoxLayout):
    """Poziomy wiersz: [FieldLabel 110px] [control...] [stretch].

    Uzycie:
        row = FieldRow("Spad")
        row.addWidget(spinbox)
        row.addWidget(unit_label)
        card.body.addLayout(row)
    """

    def __init__(self, label_text: str = "", label_width: int = 110, parent=None):
        super().__init__(parent)
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(8)
        if label_text:
            lbl = QLabel(label_text)
            lbl.setObjectName("FieldLabel")
            lbl.setFixedWidth(label_width)
            self.addWidget(lbl)


class ActionBar(QFrame):
    """Pasek akcji na dole work column (#ActionBar).

    Zawiera primary button + progress bar + mono progress label.
    Uzycie: bar.body.addWidget(btn) etc.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ActionBar")
        self.body = QHBoxLayout(self)
        self.body.setContentsMargins(22, 12, 22, 12)
        self.body.setSpacing(10)
