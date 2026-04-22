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
        # path -> target height/width in mm (mutualnie wykluczajace sie —
        # worker liczy drugi wymiar z aspect ratio sticker'a)
        self._file_height_mm: dict[str, float | None] = {}
        self._file_width_mm: dict[str, float | None] = {}
        # Widgety per wiersz (dla mutex: zmiana width zeruje UI heightu i odwrotnie)
        self._row_widgets: dict[str, dict] = {}
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

    @property
    def file_heights(self) -> dict[str, float | None]:
        return dict(self._file_height_mm)

    @property
    def file_widths(self) -> dict[str, float | None]:
        return dict(self._file_width_mm)

    def clear_files(self):
        self._files.clear()
        self._file_copies.clear()
        self._file_height_mm.clear()
        self._file_width_mm.clear()
        self._row_widgets.clear()
        self._file_status.clear()
        self._rebuild_list()
        self.files_changed.emit()

    def set_status(self, path: str, status: str, issue: str | None = None):
        if path not in self._files:
            return
        prev = self._file_status.get(path, ("wait", None))
        self._file_status[path] = (status, issue)
        # Rebuild tylko gdy zmieniaja sie rzeczy WIDOCZNE w wierszu:
        # - tlo (err <-> inne) lub
        # - obecnosc/tekst issue label
        # Inaczej (proc/ok/wait bez issue) tylko update licznika — bez
        # przerysowywania spinerow (zerowaly wartosci, skakal scroll).
        visual_change = (
            (prev[0] == "err") != (status == "err")
            or prev[1] != issue
        )
        if visual_change:
            self._rebuild_list()
        else:
            self._update_count()

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
            self._file_height_mm.pop(path, None)
            self._file_width_mm.pop(path, None)
            self._row_widgets.pop(path, None)
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
        hl.setContentsMargins(10, 8, 8, 8)
        hl.setSpacing(5)

        # Status (kropka) usunieta — stan bledu oznaczany tłem wiersza (#FEF4F4)
        # + opis bledu w issue label ponizej.

        # Ext tag
        ext = os.path.splitext(filepath)[1].lstrip('.').upper() or "FILE"
        ext_lbl = QLabel(ext)
        ext_lbl.setObjectName("FileExtTag")
        hl.addWidget(ext_lbl, alignment=Qt.AlignmentFlag.AlignVCenter)

        # Kopie + wysokość + szerokość — PRZED nazwa (po lewej).
        # Wysokość/Szerokość mutualnie wykluczajace sie — worker doliczy
        # drugi wymiar z aspect ratio po detect_contour.
        if self._show_copies:
            from PyQt6.QtWidgets import QDoubleSpinBox, QAbstractSpinBox
            spin = QSpinBox()
            spin.setObjectName("CopiesSpin")
            spin.setMinimum(1)
            spin.setMaximum(999)  # 999 kopii az nadto; max wplywa na sizeHint
            spin.setValue(self._file_copies.get(filepath, 1))
            spin.setToolTip("Liczba kopii (mouse wheel lub wpisz)")
            spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
            # NoButtons = brak strzalek up/down → Qt sizeHint ~53px zamiast
            # ~150px. User nadal moze zmieniac wartosc kolem myszy lub wpisac.
            spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
            spin.setFixedSize(54, 28)
            spin.valueChanged.connect(
                lambda v, p=filepath: self._on_copies_change(p, v)
            )
            hl.addWidget(spin, alignment=Qt.AlignmentFlag.AlignVCenter)

            def _make_dim_spin(object_name: str, tooltip: str):
                s = QDoubleSpinBox()
                s.setObjectName(object_name)
                s.setDecimals(1)
                s.setMinimum(0.0)
                s.setMaximum(99.9)   # 99.9 cm = prawie 1m, wystarczy
                s.setSingleStep(0.5)
                s.setSpecialValueText("auto")
                s.setToolTip(tooltip)
                s.setAlignment(Qt.AlignmentFlag.AlignCenter)
                s.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
                s.setFixedSize(60, 28)
                return s

            hspin = _make_dim_spin(
                "HeightSpin",
                "Wysokość naklejki w cm (auto = oryginalna / globalna).\n"
                "Wpisanie wartości zeruje Szerokość — proporcje zachowane.")
            h_mm = self._file_height_mm.get(filepath)
            hspin.setValue(0.0 if h_mm is None else float(h_mm) / 10.0)
            hl.addWidget(hspin, alignment=Qt.AlignmentFlag.AlignVCenter)

            wspin = _make_dim_spin(
                "WidthSpin",
                "Szerokość naklejki w cm (auto = oryginalna / globalna).\n"
                "Wpisanie wartości zeruje Wysokość — proporcje zachowane.")
            w_mm = self._file_width_mm.get(filepath)
            wspin.setValue(0.0 if w_mm is None else float(w_mm) / 10.0)
            hl.addWidget(wspin, alignment=Qt.AlignmentFlag.AlignVCenter)

            self._row_widgets[filepath] = {"h": hspin, "w": wspin}

            hspin.valueChanged.connect(
                lambda v_cm, p=filepath: self._on_height_change(p, v_cm)
            )
            wspin.valueChanged.connect(
                lambda v_cm, p=filepath: self._on_width_change(p, v_cm)
            )

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

        self._update_count()

    def _update_count(self):
        """Licznik ze statusami (np. '5 plików · 3 OK · 1 błąd').

        Wolane osobno przy set_status bez visual_change — update tylko
        licznika, bez przerysowywania wierszy (spinery nie skacza,
        scroll nie ucieka, focus/wartosci nie gina).
        """
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

    def _on_copies_change(self, filepath: str, value: int):
        self._file_copies[filepath] = max(1, int(value))

    def _on_height_change(self, filepath: str, value_cm: float):
        # 0.0 = special "auto" → brak override. UI w cm, storage w mm.
        if value_cm <= 0.0:
            self._file_height_mm.pop(filepath, None)
        else:
            self._file_height_mm[filepath] = float(value_cm) * 10.0
            # Mutex: zmiana height zeruje width (UI + storage)
            self._file_width_mm.pop(filepath, None)
            w = self._row_widgets.get(filepath, {}).get("w")
            if w is not None and w.value() != 0.0:
                w.blockSignals(True)
                w.setValue(0.0)
                w.blockSignals(False)

    def _on_width_change(self, filepath: str, value_cm: float):
        if value_cm <= 0.0:
            self._file_width_mm.pop(filepath, None)
        else:
            self._file_width_mm[filepath] = float(value_cm) * 10.0
            # Mutex: zmiana width zeruje height (UI + storage)
            self._file_height_mm.pop(filepath, None)
            h = self._row_widgets.get(filepath, {}).get("h")
            if h is not None and h.value() != 0.0:
                h.blockSignals(True)
                h.setValue(0.0)
                h.blockSignals(False)

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
