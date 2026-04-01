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


class FileSection(QWidget):
    """Drop zone + lista plików z opcjonalnym polem kopii per plik."""

    files_changed = pyqtSignal()  # emitowany gdy lista się zmieni

    def __init__(self, show_copies: bool = False, parent=None):
        super().__init__(parent)
        self._show_copies = show_copies
        self._files: list[str] = []
        self._file_copies: dict[str, int] = {}

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
        clear_btn.clicked.connect(self.clear_files)
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
        self._rebuild_list()
        self.files_changed.emit()

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
            row = QWidget()
            row.setProperty("class", "file-item")
            row.setFixedHeight(24 if self._show_copies else 20)
            hl = QHBoxLayout(row)
            hl.setContentsMargins(4, 0, 4, 0)
            hl.setSpacing(4)

            # Przycisk usuwania
            rm = QPushButton("×")
            rm.setProperty("class", "ghost")
            rm.setFixedSize(18, 18)
            rm.setToolTip("Usuń")
            rm.clicked.connect(lambda checked, p=filepath: self._remove_file(p))
            hl.addWidget(rm)

            # Nazwa pliku
            name = os.path.basename(filepath)
            lbl = QLabel(name)
            lbl.setToolTip(filepath)
            lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            font = lbl.font()
            font.setPointSize(9)
            lbl.setFont(font)
            hl.addWidget(lbl)

            # Kopie (opcjonalnie, nest)
            if self._show_copies:
                copies_edit = QLineEdit(str(self._file_copies.get(filepath, 1)))
                copies_edit.setFixedWidth(36)
                copies_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
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

    def _on_copies_change(self, filepath: str, text: str):
        try:
            val = max(1, int(text))
            self._file_copies[filepath] = val
        except (ValueError, TypeError):
            pass
