"""
Bleed Tool — file_section.py
===============================
Drop zone + lista plików (reusable w Bleed i Nest tab).
"""

import os
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFileDialog, QLineEdit, QSizePolicy,
)
from PyQt6.QtCore import pyqtSignal, Qt, QMimeData
from PyQt6.QtGui import QDragEnterEvent, QDropEvent


_SUPPORTED_EXT = (
    '.pdf', '.svg', '.eps', '.epsf',
    '.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp', '.webp',
)


class DropZone(QLabel):
    """Strefa drag-and-drop + kliknięcie do wyboru plików."""

    files_dropped = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("drop-zone")
        self.setText("Przeciągnij pliki lub kliknij aby wybrać")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(54)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._browse()

    def _browse(self):
        exts = " ".join(f"*{e}" for e in _SUPPORTED_EXT)
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Wybierz pliki", "",
            f"Obsługiwane ({exts});;Wszystkie (*)",
        )
        if paths:
            self.files_dropped.emit(paths)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setProperty("dragOver", True)
            self.style().unpolish(self)
            self.style().polish(self)

    def dragLeaveEvent(self, event):
        self.setProperty("dragOver", False)
        self.style().unpolish(self)
        self.style().polish(self)

    def dropEvent(self, event: QDropEvent):
        self.setProperty("dragOver", False)
        self.style().unpolish(self)
        self.style().polish(self)
        paths = []
        for url in event.mimeData().urls():
            p = url.toLocalFile()
            if p and os.path.splitext(p)[1].lower() in _SUPPORTED_EXT:
                paths.append(p)
        if paths:
            self.files_dropped.emit(paths)


_STATUS_VALUES = ("wait", "ok", "warn", "err", "proc")


class StatusDot(QLabel):
    """Mala kropka stanu pliku: wait/ok/warn/err/proc (CSS-driven)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(12, 12)
        self.setProperty("class", "status-dot")
        self.set_status("wait")

    def set_status(self, status: str):
        if status not in _STATUS_VALUES:
            status = "wait"
        self.setProperty("status", status)
        self.style().unpolish(self)
        self.style().polish(self)


class FileSection(QWidget):
    """Drop zone + lista plików z opcjonalnym polem kopii per plik."""

    files_changed = pyqtSignal()  # emitowany gdy lista się zmieni
    clear_requested = pyqtSignal()  # emitowany gdy kliknięto "Wyczyść"

    def __init__(self, show_copies: bool = False, parent=None):
        super().__init__(parent)
        self._show_copies = show_copies
        self._files: list[str] = []
        self._file_copies: dict[str, int] = {}
        # path -> (status, issue_msg | None)
        self._file_status: dict[str, tuple[str, str | None]] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # Drop zone
        self._drop = DropZone()
        self._drop.files_dropped.connect(self._add_files)
        layout.addWidget(self._drop)

        # Lista plików (scroll)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setMaximumHeight(100)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list_widget = QWidget()
        self._list_layout = QVBoxLayout(self._list_widget)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(1)
        self._list_layout.addStretch()
        self._scroll.setWidget(self._list_widget)
        self._scroll.setVisible(False)  # ukryta gdy brak plików
        layout.addWidget(self._scroll)

        # Pasek: licznik + wyczyść
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        self._count_label = QLabel("0 plików")
        self._count_label.setProperty("class", "count")
        bar.addWidget(self._count_label)
        bar.addStretch()
        clear_btn = QPushButton("Wyczyść")
        clear_btn.setObjectName("danger")
        clear_btn.clicked.connect(self._on_clear_clicked)
        bar.addWidget(clear_btn)
        layout.addLayout(bar)

    # --- Public API ---

    @property
    def files(self) -> list[str]:
        return list(self._files)

    @property
    def file_copies(self) -> dict[str, int]:
        return dict(self._file_copies)

    def clear_files(self):
        self._files.clear()
        self._file_copies.clear()
        self._file_status.clear()
        self._rebuild_list()
        self.files_changed.emit()

    def set_status(self, path: str, status: str, issue: str | None = None):
        """Ustaw status (wait/ok/warn/err/proc) + opcjonalnie komunikat issue."""
        if path not in self._files:
            return
        self._file_status[path] = (status, issue)
        self._rebuild_list()

    def reset_statuses(self):
        """Zresetuj statusy wszystkich plikow do wait (przed nowym runem)."""
        self._file_status.clear()
        self._rebuild_list()

    def _on_clear_clicked(self):
        """Kliknięcie Wyczyść → emituje clear_requested (globalny clear)."""
        self.clear_requested.emit()

    # --- Internal ---

    def _add_files(self, paths: list[str]):
        for p in paths:
            if p not in self._files:
                self._files.append(p)
                if self._show_copies:
                    self._file_copies[p] = 1
        self._rebuild_list()
        self.files_changed.emit()

    def _remove_file(self, path: str):
        if path in self._files:
            self._files.remove(path)
            self._file_copies.pop(path, None)
            self._file_status.pop(path, None)
        self._rebuild_list()
        self.files_changed.emit()

    def _rebuild_list(self):
        # Usuń stare widgety (poza stretch)
        while self._list_layout.count() > 1:
            item = self._list_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        for filepath in self._files:
            status, issue = self._file_status.get(filepath, ("wait", None))
            has_issue = bool(issue)

            row = QWidget()
            row.setProperty("class", "file-item")
            # Wyzszy wiersz gdy jest issue (dwie linie)
            base_h = 24 if self._show_copies else 20
            row.setFixedHeight(base_h + (18 if has_issue else 0))
            hl = QHBoxLayout(row)
            hl.setContentsMargins(4, 0, 4, 0)
            hl.setSpacing(4)

            # Status dot
            dot = StatusDot()
            dot.set_status(status)
            if issue:
                dot.setToolTip(issue)
            hl.addWidget(dot)

            # Przycisk usuwania
            rm = QPushButton("x")
            rm.setProperty("class", "ghost")
            rm.setFixedSize(18, 18)
            rm.setToolTip("Usuń")
            rm.clicked.connect(lambda checked, p=filepath: self._remove_file(p))
            hl.addWidget(rm)

            # Nazwa + opcjonalnie linia issue (pionowy stack)
            name_stack = QWidget()
            ns_layout = QVBoxLayout(name_stack)
            ns_layout.setContentsMargins(0, 0, 0, 0)
            ns_layout.setSpacing(0)

            name = os.path.basename(filepath)
            lbl = QLabel(name)
            lbl.setToolTip(filepath)
            lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            font = lbl.font()
            font.setPointSize(9)
            lbl.setFont(font)
            ns_layout.addWidget(lbl)

            if has_issue:
                issue_lbl = QLabel(issue)
                issue_lbl.setProperty("class", "file-issue")
                issue_lbl.setProperty("severity", status)  # warn | err
                issue_lbl.setToolTip(issue)
                font_i = issue_lbl.font()
                font_i.setPointSize(8)
                issue_lbl.setFont(font_i)
                ns_layout.addWidget(issue_lbl)

            hl.addWidget(name_stack, stretch=1)

            # Kopie (opcjonalnie, nest)
            if self._show_copies:
                copies_edit = QLineEdit(str(self._file_copies.get(filepath, 1)))
                copies_edit.setFixedWidth(48)
                copies_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
                copies_edit.setStyleSheet("padding: 0 4px; min-height: 24px; max-height: 24px;")
                font2 = copies_edit.font()
                font2.setPointSize(9)
                copies_edit.setFont(font2)
                copies_edit.textChanged.connect(
                    lambda text, p=filepath: self._on_copies_change(p, text)
                )
                hl.addWidget(copies_edit)

            # Wstaw PRZED stretch
            self._list_layout.insertWidget(self._list_layout.count() - 1, row)

        self._count_label.setText(f"{len(self._files)} plików")
        self._scroll.setVisible(len(self._files) > 0)
        # Rozszerz scroll area gdy sa issue (wiecej miejsca na liscie)
        has_any_issue = any(
            bool(issue) for _, issue in self._file_status.values()
        )
        self._scroll.setMaximumHeight(160 if has_any_issue else 100)

    def _on_copies_change(self, filepath: str, text: str):
        try:
            val = max(1, int(text))
            self._file_copies[filepath] = val
        except (ValueError, TypeError):
            pass
