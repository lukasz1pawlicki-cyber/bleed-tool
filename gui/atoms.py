"""
Bleed Tool — atoms.py
========================
Male, reusable widgety + helpery zgodne z QSS (Technikadruku design).

Kontrakt stylowania:
  - objectName  = "CSS class"  (np. Card, NavItem, FieldLabel)
  - setProperty = modifier     (np. [variant="ghost"], [state="ok"])
Po zmianie dynamic property MUSISZ wywolac repolish(widget).
"""

from __future__ import annotations

from typing import Iterable, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
    QSpacerItem, QWidget,
)


# =============================================================================
# HELPERS
# =============================================================================

def repolish(w: QWidget) -> None:
    """Wymus reewaluacje stylu po zmianie dynamic property."""
    s = w.style()
    s.unpolish(w)
    s.polish(w)
    w.update()


def set_prop(w: QWidget, key: str, value) -> None:
    """Ustaw dynamic property + repolish."""
    w.setProperty(key, value)
    repolish(w)


def hline_dashed() -> QFrame:
    """Pozioma linia przerywana (#CardDivider)."""
    f = QFrame()
    f.setObjectName("CardDivider")
    f.setFrameShape(QFrame.Shape.HLine)
    return f


def spacer_h() -> QSpacerItem:
    """Poziomy spacer (expanding)."""
    return QSpacerItem(0, 0, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)


# =============================================================================
# ATOMS
# =============================================================================

class StatusDot(QFrame):
    """Kolorowa kropka statusu (10x10).

    Stany: wait / proc / ok / warn / err (CSS-driven).
    """

    def __init__(self, state: str = "wait", parent=None):
        super().__init__(parent)
        self.setObjectName("StatusDot")
        self.setFixedSize(10, 10)
        self.set_state(state)

    def set_state(self, state: str) -> None:
        if state not in ("wait", "proc", "ok", "warn", "err"):
            state = "wait"
        set_prop(self, "state", state)


class Segmented(QWidget):
    """Segmentowy przelacznik (QSS: #Segmented + role=segment).

    Emituje currentChanged(int) oraz currentTextChanged(str).
    Opcja accent=True daje solid-blue checked state (Nest grouping).
    """
    currentChanged = pyqtSignal(int)
    currentTextChanged = pyqtSignal(str)

    def __init__(self, options: Iterable[str], accent: bool = False,
                 default: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.setObjectName("Segmented")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._options = list(options)
        for i, text in enumerate(self._options):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setProperty("role", "segment")
            if accent:
                b.setProperty("accent", "true")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            self._group.addButton(b, i)
            lay.addWidget(b)
        # Default selection
        idx_default = 0
        if default is not None and default in self._options:
            idx_default = self._options.index(default)
        if self._group.button(idx_default):
            self._group.button(idx_default).setChecked(True)
        self._group.idToggled.connect(self._on_toggled)

    def _on_toggled(self, idx: int, on: bool):
        if on:
            self.currentChanged.emit(idx)
            if 0 <= idx < len(self._options):
                self.currentTextChanged.emit(self._options[idx])

    def current(self) -> int:
        return self._group.checkedId()

    def value(self) -> str:
        idx = self._group.checkedId()
        if 0 <= idx < len(self._options):
            return self._options[idx]
        return ""

    def set_current(self, idx: int) -> None:
        btn = self._group.button(idx)
        if btn:
            btn.setChecked(True)

    def set_value(self, text: str) -> None:
        if text in self._options:
            self.set_current(self._options.index(text))


class IconButton(QPushButton):
    """Kwadratowy icon-only button 30x30 (QSS: #IconButton)."""

    def __init__(self, text: str = "", tip: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("IconButton")
        if text:
            self.setText(text)
        if tip:
            self.setToolTip(tip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)


def make_button(
    text: str,
    *,
    variant: Optional[str] = None,
    size: Optional[str] = None,
) -> QPushButton:
    """Helper do tworzenia przyciskow z variant/size properties.

    variant: secondary | ghost | danger | success | None (primary)
    size: lg | sm | None (md default)
    """
    b = QPushButton(text)
    if variant:
        b.setProperty("variant", variant)
    if size:
        b.setProperty("size", size)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


class CardSectionLabel(QLabel):
    """Mono caps label dla sekcji karty (#CardSectionLabel)."""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setObjectName("CardSectionLabel")


class FieldLabel(QLabel):
    """Etykieta pola (#FieldLabel)."""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setObjectName("FieldLabel")


class UnitLabel(QLabel):
    """Jednostka obok spinboxa (#UnitLabel)."""

    def __init__(self, text: str = "mm", parent=None):
        super().__init__(text, parent)
        self.setObjectName("UnitLabel")


class Card(QFrame):
    """Standardowa karta (#Card) — bialy bg, border, radius.

    Uzywaj z QVBoxLayout(card) i addWidget/addLayout.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
