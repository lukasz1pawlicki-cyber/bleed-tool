"""
Bleed Tool — log_panel.py
===========================
Panel logu z batched flush i HTML kolorowaniem błędów.
"""

from PyQt6.QtWidgets import QTextEdit
from PyQt6.QtCore import QTimer


_ERROR_KEYWORDS = ("[ERR]", "[BŁĄD", "ERROR", "Traceback", "BLAD", "KRYTYCZNY")


class LogPanel(QTextEdit):
    """Read-only log panel z batched flush (50ms) i czerwonymi błędami."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setObjectName("log")
        self.setPlaceholderText("Log...")

        self._buffer: list[str] = []
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._flush)

    def log(self, msg: str):
        """Dodaje wiadomość do bufora i planuje flush."""
        self._buffer.append(msg)
        if not self._timer.isActive():
            self._timer.start()

    def _flush(self):
        """Zapisuje bufor do widgetu z HTML formatowaniem."""
        if not self._buffer:
            return
        cursor = self.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        for msg in self._buffer:
            is_error = any(kw in msg for kw in _ERROR_KEYWORDS)
            # Escape HTML
            safe = msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            if is_error:
                cursor.insertHtml(
                    f'<span style="color:#e53935;">{safe}</span><br>'
                )
            else:
                cursor.insertHtml(f'{safe}<br>')
        self._buffer.clear()
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    def clear_log(self):
        """Czyści cały log."""
        self._buffer.clear()
        self._timer.stop()
        self.clear()
