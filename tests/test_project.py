"""Testy zapisu/odczytu projektu .bleedproj."""
from __future__ import annotations

import json
import os

import pytest

from modules.project import (
    Project, ProjectFile, BleedParams, SheetParams, PROJECT_EXT, PROJECT_VERSION
)


def test_project_save_load_roundtrip(tmp_path):
    proj = Project(
        files=[
            ProjectFile(path="/abs/logo.pdf", count=3, rotation_deg=0),
            ProjectFile(path="/abs/round.svg", count=1, rotation_deg=90),
        ],
        bleed=BleedParams(bleed_mm=3.0, white=True, engine="opencv"),
        sheet=SheetParams(format="A3", width_mm=297, height_mm=420, gap_mm=5.0),
        name="Zlecenie_XYZ",
    )
    path = str(tmp_path / "test.bleedproj")
    proj.save(path)
    assert os.path.exists(path)

    loaded = Project.load(path)
    assert loaded.name == "Zlecenie_XYZ"
    assert len(loaded.files) == 2
    assert loaded.files[0].path == "/abs/logo.pdf"
    assert loaded.files[0].count == 3
    assert loaded.files[1].rotation_deg == 90
    assert loaded.bleed.bleed_mm == 3.0
    assert loaded.bleed.white is True
    assert loaded.bleed.engine == "opencv"
    assert loaded.sheet.format == "A3"
    assert loaded.sheet.gap_mm == 5.0


def test_project_save_auto_appends_extension(tmp_path):
    """Bez .bleedproj — powinno zostac dodane."""
    proj = Project(name="foo")
    path_no_ext = str(tmp_path / "myproj")
    proj.save(path_no_ext)
    assert os.path.exists(path_no_ext + PROJECT_EXT)


def test_project_version_in_saved_file(tmp_path):
    proj = Project()
    path = str(tmp_path / "v.bleedproj")
    proj.save(path)
    with open(path) as f:
        data = json.load(f)
    assert data["version"] == PROJECT_VERSION


def test_missing_files_detection(tmp_path):
    """missing_files() zwraca pliki ktore nie istnieja na dysku."""
    real = tmp_path / "exists.pdf"
    real.write_bytes(b"%PDF-1.4\n%%EOF\n")
    proj = Project(files=[
        ProjectFile(path=str(real)),
        ProjectFile(path="/nonexistent/absent.pdf"),
    ])
    missing = proj.missing_files()
    assert len(missing) == 1
    assert "absent.pdf" in missing[0]
    valid = proj.valid_files()
    assert len(valid) == 1
    assert valid[0].path == str(real)


def test_load_defaults_fills_missing_fields(tmp_path):
    """Minimalny JSON z {} laduje z domyslnymi wartosciami, nie crashuje."""
    path = str(tmp_path / "minimal.bleedproj")
    with open(path, "w") as f:
        json.dump({"version": 1, "files": []}, f)
    proj = Project.load(path)
    assert proj.bleed.bleed_mm == 2.0  # default
    assert proj.sheet.format == "A4"


def test_load_from_future_version_warns(tmp_path, caplog):
    """Wyzsza wersja formatu — nie crashuje, tylko warning."""
    import logging
    caplog.set_level(logging.WARNING)
    path = str(tmp_path / "future.bleedproj")
    with open(path, "w") as f:
        json.dump({"version": 999, "files": []}, f)
    proj = Project.load(path)
    assert proj is not None
    assert any("wersje" in rec.message.lower() or "nowsz" in rec.message.lower()
               for rec in caplog.records)


def test_name_from_filename_when_untitled(tmp_path):
    """Gdy name=Untitled w pliku, brany jest stem z filename."""
    path = str(tmp_path / "moja_nazwa.bleedproj")
    with open(path, "w") as f:
        json.dump({"version": 1, "files": [], "name": "Untitled"}, f)
    proj = Project.load(path)
    assert proj.name == "moja_nazwa"


def test_margins_serialized_as_list(tmp_path):
    """JSON nie ma tuple — margins musza byc list po serializacji."""
    proj = Project(sheet=SheetParams(margins_mm=(5.0, 10.0, 15.0, 20.0)))
    path = str(tmp_path / "m.bleedproj")
    proj.save(path)
    with open(path) as f:
        data = json.load(f)
    assert isinstance(data["sheet"]["margins_mm"], list)
    assert data["sheet"]["margins_mm"] == [5.0, 10.0, 15.0, 20.0]
    # Round-trip odtwarza tuple
    loaded = Project.load(path)
    assert loaded.sheet.margins_mm == (5.0, 10.0, 15.0, 20.0)
