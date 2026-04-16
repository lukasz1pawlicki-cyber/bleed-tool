"""
Bleed Tool — project.py
==========================
Format projektu (.bleedproj) — zapisuje sesje operatora:
lista plikow wejsciowych + parametry bleed + parametry arkusza.

Zapis: JSON (czytelny dla czlowieka, editable recznie w edytorze).

Minimalna struktura:
{
  "version": 1,
  "created_at": "2026-04-15T22:00:00",
  "files": [
    {"path": "abs/path/to/file.pdf", "count": 1},
    ...
  ],
  "bleed": {
    "bleed_mm": 2.0,
    "white": false,
    "engine": "auto"
  },
  "sheet": {
    "format": "A4",
    "width_mm": 210,
    "height_mm": 297,
    "gap_mm": 3.0,
    "margins_mm": [10, 10, 10, 10],
    "marks": "opos",
    "plotter": "summa_s3"
  }
}

Sciezki sa zachowywane jako abs, opcjonalnie moga byc rozwiazywane relatywnie
do lokalizacji pliku .bleedproj (wygodne gdy operator przenosi projekt).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)

PROJECT_VERSION = 1
PROJECT_EXT = ".bleedproj"


@dataclass
class BleedParams:
    """Parametry bleed (dla kazdego pliku wspolne)."""
    bleed_mm: float = 2.0
    white: bool = False
    engine: str = "auto"           # CONTOUR_ENGINE: auto/moore/opencv
    black_100k: bool = True
    cutline_mode: str = "kiss-cut"


@dataclass
class SheetParams:
    """Parametry arkusza (nesting)."""
    format: str = "A4"
    width_mm: float = 210.0
    height_mm: float = 297.0
    gap_mm: float = 3.0
    margins_mm: tuple = (10.0, 10.0, 10.0, 10.0)
    marks: str = "opos"            # opos/jwei/none
    plotter: str = "summa_s3"


@dataclass
class ProjectFile:
    """Pojedynczy plik w projekcie."""
    path: str                       # abs path
    count: int = 1                  # liczba powtorzen dla nestingu
    rotation_deg: int = 0           # 0 | 90 (dla nestingu)

    def exists(self) -> bool:
        return os.path.isfile(self.path)


@dataclass
class Project:
    """Kompletny projekt — stan sesji operatora."""
    files: list[ProjectFile] = field(default_factory=list)
    bleed: BleedParams = field(default_factory=BleedParams)
    sheet: SheetParams = field(default_factory=SheetParams)
    name: str = "Untitled"

    # ====== SERIALIZATION ======

    def to_dict(self) -> dict:
        return {
            "version": PROJECT_VERSION,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "name": self.name,
            "files": [
                {"path": f.path, "count": f.count, "rotation_deg": f.rotation_deg}
                for f in self.files
            ],
            "bleed": asdict(self.bleed),
            "sheet": {
                **asdict(self.sheet),
                "margins_mm": list(self.sheet.margins_mm),  # JSON: list zamiast tuple
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Project":
        ver = data.get("version", 0)
        if ver > PROJECT_VERSION:
            log.warning(
                f"Projekt ma nowsza wersje ({ver}) niz obslugiwana ({PROJECT_VERSION}) — "
                "moze nie dzialac poprawnie."
            )

        files = [
            ProjectFile(
                path=f["path"],
                count=int(f.get("count", 1)),
                rotation_deg=int(f.get("rotation_deg", 0)),
            )
            for f in data.get("files", [])
        ]

        bleed_raw = data.get("bleed", {})
        bleed = BleedParams(
            bleed_mm=float(bleed_raw.get("bleed_mm", 2.0)),
            white=bool(bleed_raw.get("white", False)),
            engine=str(bleed_raw.get("engine", "auto")),
            black_100k=bool(bleed_raw.get("black_100k", True)),
            cutline_mode=str(bleed_raw.get("cutline_mode", "kiss-cut")),
        )

        sheet_raw = data.get("sheet", {})
        margins = sheet_raw.get("margins_mm", [10.0, 10.0, 10.0, 10.0])
        sheet = SheetParams(
            format=str(sheet_raw.get("format", "A4")),
            width_mm=float(sheet_raw.get("width_mm", 210.0)),
            height_mm=float(sheet_raw.get("height_mm", 297.0)),
            gap_mm=float(sheet_raw.get("gap_mm", 3.0)),
            margins_mm=tuple(float(x) for x in margins),
            marks=str(sheet_raw.get("marks", "opos")),
            plotter=str(sheet_raw.get("plotter", "summa_s3")),
        )

        return cls(
            files=files,
            bleed=bleed,
            sheet=sheet,
            name=str(data.get("name", "Untitled")),
        )

    # ====== FILE IO ======

    def save(self, path: str) -> None:
        """Zapisuje projekt do pliku .bleedproj (JSON)."""
        if not path.endswith(PROJECT_EXT):
            path = path + PROJECT_EXT
        data = self.to_dict()
        # Atomic: tmp + rename
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, path)
            log.info(f"Projekt zapisany: {path}")
        except OSError:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            raise

    @classmethod
    def load(cls, path: str) -> "Project":
        """Laduje projekt z pliku .bleedproj."""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        project = cls.from_dict(data)
        # Nazwa z filename (jesli nie byla ustawiona)
        if project.name == "Untitled":
            project.name = os.path.splitext(os.path.basename(path))[0]
        return project

    # ====== VALIDATION ======

    def missing_files(self) -> list[str]:
        """Zwraca liste plikow ktore wymieniono w projekcie ale nie istnieja."""
        return [f.path for f in self.files if not f.exists()]

    def valid_files(self) -> list[ProjectFile]:
        """Zwraca liste istniejacych plikow."""
        return [f for f in self.files if f.exists()]
