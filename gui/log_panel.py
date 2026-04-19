"""
Bleed Tool — log_panel.py
===========================
Navy terminal log (#0F172A bg, colored levels). Technikadruku style.

Levels detection (keywords):
  ERR  — #F87171  ([ERR], [BŁĄD, BLAD, KRYTYCZNY, Traceback)
  WARN — #FBBF24  (WARN, OSTRZEŻENIE, WARNING)
  OK   — #34D399  ([OK], gotowe, zapisano, Zakończono)
  INFO — #60A5FA  (default)
"""

from PyQt6.QtWidgets import QPlainTextEdit
from PyQt6.QtCore import QTimer


_ERR_KW = ("[ERR]", "[BŁĄD", "BLAD", "KRYTYCZNY", "Traceback", "ERROR")
_WARN_KW = ("WARN", "OSTRZEŻENIE", "WARNING")
_OK_KW = ("[OK]", "Gotowe", "gotowe", "Zapisano", "zapisano", "Zakończono", "zakończono")


def _classify(msg: str) -> str:
    for kw in _ERR_KW:
        if kw in msg:
            return "err"
    for kw in _WARN_KW:
        if kw in msg:
            return "warn"
    for kw in _OK_KW:
        if kw in msg:
            return "ok"
    return "info"


_LEVEL_COLORS = {
    "err": "#F87171",
    "warn": "#FBBF24",
    "ok": "#34D399",
    "info": "#60A5FA",
}


class LogPanel(QPlainTextEdit):
    """Navy terminal log z batched flush (50ms) + colored levels."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setObjectName("LogView")
        self.setPlaceholderText("Log…")

        self._buffer: list[str] = []
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._flush)

    def log(self, msg: str):
        self._buffer.append(msg)
        if not self._timer.isActive():
            self._timer.start()

    def _flush(self):
        if not self._buffer:
            return
        cursor = self.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.setTextCursor(cursor)
        for msg in self._buffer:
            level = _classify(msg)
            color = _LEVEL_COLORS[level]
            level_tag = level.upper().ljust(4)
            safe = msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            html = (
                f'<span style="color:{color};font-weight:600;">{level_tag}</span>'
                f' <span style="color:#E5E7EB;">{safe}</span><br>'
            )
            self.appendHtml(html)
        self._buffer.clear()
        self.ensureCursorVisible()

    def clear_log(self):
        self._buffer.clear()
        self._timer.stop()
        self.clear()
