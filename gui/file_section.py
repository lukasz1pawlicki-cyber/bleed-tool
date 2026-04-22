"""
Bleed Tool — file_section.py
===============================
DropZone + lista plikow (reusable Bleed + Nest). Technikadruku QSS.

Sygnaly:
  files_changed()        — gdy lista plikow sie zmieni
  clear_requested()      — gdy kliknieto "Wyczyść"
"""

import os
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QScrollArea, QFileDialog, QSpinBox, QSizePolicy,
)
from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtGui import QDragEnterEvent, QDropEvent

from gui.atoms import StatusDot, set_prop, make_button


_SUPPORTED_EXT = (
    '.pdf', '.svg', '.eps', '.epsf',
    '.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp', '.webp',
)


class DropZone(QFrame):
    """Strefa drag-and-drop (#DropZone) z blueprint look.

    Klikniecie otwiera QFileDialog. Stan [active="true"] podczas dragover.
    """

    files_dropped = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Kompaktowa wysokość — ma zajmować ~20% kolumny plików
        self.setFixedHeight(56)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(10)

        # Ikona mała
        icon = QLabel("↑")
        icon.setFixedSize(30, 30)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet(
            "background:#FFFFFF;border:1px solid #E2E5ED;border-radius:7px;"
            "color:#2563EB;font-size:15px;font-weight:700;"
        )
        lay.addWidget(icon)

        # Tytuł (jeden wiersz, bez subtekstu)
        self._title = QLabel("Przeciągnij pliki lub kliknij")
        self._title.setObjectName("DropZoneTitle")
        lay.addWidget(self._title, stretch=1)

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
            set_prop(self, "active", "true")

    def dragLeaveEvent(self, event):
        set_prop(self, "active", "false")

    def dropEvent(self, event: QDropEvent):
        set_prop(self, "active", "false")
        paths = []
        for url in event.mimeData().urls():
            p = url.toLocalFile()
            if p and os.path.splitext(p)[1].lower() in _SUPPORTED_EXT:
                paths.append(p)
        if paths:
            self.files_dropped.emit(paths)


class FileSection(QWidget):
    """DropZone + file list + filebar (count + preflight + clear)."""

    files_changed = pyqtSignal()
    clear_requested = pyqtSignal()

    def __init__(self, show_copies: bool = False, parent=None):
        super().__init__(parent)
        self._show_copies = show_copies
        self._files: list[str] = []
        self._file_copies: dict[str, int] = {}
        # path -> (status, issue_msg | None)
        self._file_status: dict[str, tuple[str, str | None]] = {}

        # Drag-and-drop obsłużony przez cały FileSection — user może upuścić
        # plik w DOWOLNE miejsce kolumny, nie tylko w DropZone
        self.setAcceptDrops(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # === DropZone ===
        self._drop = DropZone()
        self._drop.files_dropped.connect(self._add_files)
        layout.addWidget(self._drop)

        # === Lista plikow (scroll) ===
        self._scroll = QScrollArea()
        self._scroll.setObjectName("FileListScroll")
        self._scroll.setWidgetResizable(True)
        # Bez maksymalnej wysokości — lista wypełnia pozostałe ~80% kolumny
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(
            "QScrollArea{background:#FFFFFF;border:1px solid #E2E5ED;border-radius:8px;}"
        )
        self._list_widget = QWidget()
        self._list_widget.setStyleSheet("background:#FFFFFF;")
        self._list_layout = QVBoxLayout(self._list_widget)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(0)
        self._list_layout.addStretch()
        self._scroll.setWidget(self._list_widget)
        # Scroll zawsze visible (zarezerwowane miejsce) — bez tego bar "skacze"
        # gdy lista rośnie z 0 plików.
        layout.addWidget(self._scroll, stretch=1)

        # === Filebar: [Wyczyść] [licznik centered] [spacer] ===
        # Wyśrodkowany licznik przez 3-kolumnowy bar: przycisk po lewej, licznik
        # w środku (stretch), spacer po prawej o tej samej szerokości co przycisk.
        bar = QHBoxLayout()
        bar.setContentsMargins(8, 6, 8, 6)
        bar.setSpacing(8)
        clear_btn = make_button("Wyczyść", variant="danger", size="sm")
        clear_btn.clicked.connect(self._on_clear_clicked)
        clear_btn.setFixedWidth(86)
        bar.addWidget(clear_btn)
        self._count_label = QLabel("0 plików")
        self._count_label.setObjectName("FileBarCount")
        self._count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bar.addWidget(self._count_label, stretch=1)
        # Spacer symetryczny do przycisku dla wyśrodkowania licznika
        from PyQt6.QtWidgets import QSpacerItem
        bar.addSpacerItem(QSpacerItem(86, 0, QSizePolicy.Policy.Fixed,
                                      QSizePolicy.Policy.Minimum))
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
        if path not in self._files:
            return
        self._file_status[path] = (status, issue)
        self._rebuild_list()

    def reset_statuses(self):
        self._file_status.clear()
        self._rebuild_list()

    def _on_clear_clicked(self):
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

    def _build_row(self, filepath: str) -> QWidget:
        status, issue = self._file_status.get(filepath, ("wait", None))
        has_issue = bool(issue)

        row = QFrame()
        row.setObjectName("FileRow")
        row.setStyleSheet(
            "QFrame#FileRow{background:" + ("#FEF4F4" if status == "err" else "#FFFFFF") + ";"
            "border-bottom:1px solid #E2E5ED;}"
        )
        hl = QHBoxLayout(row)
        hl.setContentsMargins(12, 9, 12, 9)
        hl.setSpacing(10)

        # Status dot
        dot = StatusDot(state=status)
        if issue:
            dot.setToolTip(issue)
        hl.addWidget(dot, alignment=Qt.AlignmentFlag.AlignVCenter)

        # Ext tag
        ext = os.path.splitext(filepath)[1].lstrip('.').upper() or "FILE"
        ext_lbl = QLabel(ext)
        ext_lbl.setObjectName("FileExtTag")
        hl.addWidget(ext_lbl, alignment=Qt.AlignmentFlag.AlignVCenter)

        # Kopie (Nest) — PRZED nazwa (po lewej)
        if self._show_copies:
            spin = QSpinBox()
            spin.setObjectName("CopiesSpin")
            spin.setMinimum(1)
            spin.setMaximum(9999)
            spin.setValue(self._file_copies.get(filepath, 1))
            spin.setToolTip("Liczba kopii")
            spin.valueChanged.connect(
                lambda v, p=filepath: self._on_copies_change(p, v)
            )
            hl.addWidget(spin, alignment=Qt.AlignmentFlag.AlignVCenter)

        # Nazwa + meta/issue stack
        name_stack = QWidget()
        ns = QVBoxLayout(name_stack)
        ns.setContentsMargins(0, 0, 0, 0)
        ns.setSpacing(1)

        name = os.path.basename(filepath)
        name_lbl = QLabel(name)
        name_lbl.setObjectName("FileNameStrong")
        name_lbl.setToolTip(filepath)
        ns.addWidget(name_lbl)

        if has_issue:
            issue_lbl = QLabel(issue)
            issue_lbl.setObjectName("FileIssueErr" if status == "err" else "FileIssueWarn")
            issue_lbl.setToolTip(issue)
            ns.addWidget(issue_lbl)
        # Bez dirname/path — tylko nazwa pliku (user-request)

        hl.addWidget(name_stack, stretch=1)

        # Remove button
        rm = QPushButton("×")
        rm.setObjectName("RemoveRowBtn")
        rm.setToolTip("Usuń")
        rm.clicked.connect(lambda _=False, p=filepath: self._remove_file(p))
        rm.setCursor(Qt.CursorShape.PointingHandCursor)
        hl.addWidget(rm, alignment=Qt.AlignmentFlag.AlignVCenter)

        return row

    def _rebuild_list(self):
        # Usun stare rzedy (poza stretch na koncu)
        while self._list_layout.count() > 1:
            item = self._list_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        for filepath in self._files:
            row = self._build_row(filepath)
            self._list_layout.insertWidget(self._list_layout.count() - 1, row)

        # Licznik ze statusami (np. "5 plików · 3 OK · 1 błąd")
        n = len(self._files)
        ok = sum(1 for p in self._files if self._file_status.get(p, ("wait", None))[0] == "ok")
        err = sum(1 for p in self._files if self._file_status.get(p, ("wait", None))[0] == "err")
        proc = sum(1 for p in self._files if self._file_status.get(p, ("wait", None))[0] == "proc")
        parts = [f"{n} plików"]
        if ok:
            parts.append(f"{ok} OK")
        if proc:
            parts.append(f"{proc} przetwarzanie")
        if err:
            parts.append(f"{err} błąd")
        self._count_label.setText(" · ".join(parts))
        # Scroll zawsze visible — bez przeskakiwania layoutu

    def _on_copies_change(self, filepath: str, value: int):
        self._file_copies[filepath] = max(1, int(value))

    # --- Drag-and-drop na całą kolumnę ---

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = []
        for url in event.mimeData().urls():
            p = url.toLocalFile()
            if p and os.path.splitext(p)[1].lower() in _SUPPORTED_EXT:
                paths.append(p)
        if paths:
            self._add_files(paths)
            event.acceptProposedAction()
