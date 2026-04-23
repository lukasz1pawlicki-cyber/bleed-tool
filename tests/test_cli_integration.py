"""Testy integracyjne CLI: batch parallel, project roundtrip, preflight, cache.

Uzywamy subprocess zeby testowac prawdziwe wywolanie CLI (jak operator).
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import fitz
import pytest

from modules.project import Project, ProjectFile, BleedParams
from tests.fixtures import make_rectangle_vector


ROOT = Path(__file__).resolve().parent.parent
CLI = str(ROOT / "bleed_cli.py")


def _run_cli(*args: str, env_extra: dict | None = None, cwd: str | None = None) -> subprocess.CompletedProcess:
    """Uruchamia bleed_cli.py z podanymi argumentami. Zwraca CompletedProcess."""
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, CLI, *args],
        capture_output=True, text=True, env=env, cwd=cwd or str(ROOT),
        timeout=180,
    )


def _file_sha1(path: Path) -> str:
    h = hashlib.sha1()
    h.update(path.read_bytes())
    return h.hexdigest()


def _boxes_hash(pdf_path: Path) -> tuple:
    """Porownywalny fingerprint: MediaBox/TrimBox/BleedBox + liczba obiektow.

    Nie uzywamy byte-hash bo PDF ma timestamp/ID rozne w kazdym zapisie.
    """
    doc = fitz.open(str(pdf_path))
    try:
        page = doc[0]
        return (
            tuple(round(x, 3) for x in page.mediabox),
            tuple(round(x, 3) for x in page.trimbox),
            tuple(round(x, 3) for x in page.bleedbox),
            len(doc),
        )
    finally:
        doc.close()


# ============================================================================
# Batch parallel
# ============================================================================

@pytest.fixture
def batch_folder(tmp_path: Path) -> Path:
    """Folder z kilkoma plikami wektorowymi (batch source)."""
    folder = tmp_path / "batch_in"
    folder.mkdir()
    # Roznej wielkosci zeby wykryc regresje pomieszania wynikow w parallel
    make_rectangle_vector(folder, w_mm=40, h_mm=30)
    (folder / "rectangle_vector.pdf").rename(folder / "a.pdf")
    make_rectangle_vector(folder, w_mm=60, h_mm=50)
    (folder / "rectangle_vector.pdf").rename(folder / "b.pdf")
    make_rectangle_vector(folder, w_mm=80, h_mm=40)
    (folder / "rectangle_vector.pdf").rename(folder / "c.pdf")
    return folder


def test_cli_batch_sequential(tmp_path: Path, batch_folder: Path):
    """Batch sekwencyjny (-j 1) przetwarza wszystkie 3 pliki."""
    out = tmp_path / "out_seq"
    r = _run_cli("--batch", str(batch_folder), "-o", str(out), "-j", "1")
    assert r.returncode == 0, f"CLI non-zero exit: {r.stderr}"

    pdfs = sorted(out.glob("*.pdf"))
    assert len(pdfs) == 3, f"Oczekiwano 3 plikow w out, jest {len(pdfs)}"

    # Kazdy musi miec nazwe wg konwencji _PRINT_{W}x{H}mm_bleed{N}mm
    for p in pdfs:
        assert "_PRINT_" in p.name
        assert "mm_bleed" in p.name


def test_cli_batch_parallel_deterministic(tmp_path: Path, batch_folder: Path):
    """Batch parallel (-j 2) daje ten sam wynik co sekwencyjny.

    Regresja: ProcessPoolExecutor nie moze zgubic/pomieszac plikow.
    """
    out_seq = tmp_path / "out_seq"
    out_par = tmp_path / "out_par"

    r1 = _run_cli("--batch", str(batch_folder), "-o", str(out_seq), "-j", "1")
    assert r1.returncode == 0, r1.stderr

    r2 = _run_cli("--batch", str(batch_folder), "-o", str(out_par), "-j", "2")
    assert r2.returncode == 0, r2.stderr

    names_seq = sorted(p.name for p in out_seq.glob("*.pdf"))
    names_par = sorted(p.name for p in out_par.glob("*.pdf"))
    assert names_seq == names_par, (
        f"Parallel wyprodukowal inne nazwy plikow:\n"
        f"  seq: {names_seq}\n  par: {names_par}"
    )

    # Struktura boxow musi byc identyczna (CPU parallelism nie zmienia geometrii)
    for n in names_seq:
        h_seq = _boxes_hash(out_seq / n)
        h_par = _boxes_hash(out_par / n)
        assert h_seq == h_par, (
            f"{n}: boxy rozne seq vs par\n  seq: {h_seq}\n  par: {h_par}"
        )


# ============================================================================
# Project roundtrip
# ============================================================================

def test_cli_project_save_and_load(tmp_path: Path):
    """--save-project zapisuje, --project ladowany daje ten sam output."""
    # Przygotuj folder z plikami
    src = tmp_path / "src"
    src.mkdir()
    make_rectangle_vector(src, w_mm=50, h_mm=40)
    (src / "rectangle_vector.pdf").rename(src / "sticker.pdf")

    proj_path = tmp_path / "session.bleedproj"
    out_direct = tmp_path / "out_direct"

    # Zapisz projekt + przetworz w jednym wywolaniu (batch)
    r1 = _run_cli(
        "--batch", str(src), "-o", str(out_direct),
        "--bleed", "3",
        "--save-project", str(proj_path),
    )
    assert r1.returncode == 0, r1.stderr
    assert proj_path.exists(), "Plik projektu nie zostal zapisany"

    # Zaladuj projekt — musi wyprodukowac ten sam plik (ta sama nazwa + boxy)
    out_from_proj = tmp_path / "out_from_proj"
    r2 = _run_cli("--project", str(proj_path), "-o", str(out_from_proj))
    assert r2.returncode == 0, r2.stderr

    pdfs_direct = sorted(p.name for p in out_direct.glob("*.pdf"))
    pdfs_proj = sorted(p.name for p in out_from_proj.glob("*.pdf"))
    assert pdfs_direct == pdfs_proj, \
        f"Inne pliki: direct={pdfs_direct} vs proj={pdfs_proj}"

    for n in pdfs_direct:
        h_direct = _boxes_hash(out_direct / n)
        h_proj = _boxes_hash(out_from_proj / n)
        assert h_direct == h_proj, f"{n}: inna struktura boxow"


def test_cli_project_preserves_bleed_value(tmp_path: Path):
    """Projekt zapisany z --bleed 5 -> ladowany tez uzywa 5mm (nie default 2mm)."""
    src = tmp_path / "src"
    src.mkdir()
    make_rectangle_vector(src, w_mm=50, h_mm=40)
    (src / "rectangle_vector.pdf").rename(src / "a.pdf")

    proj_path = tmp_path / "p.bleedproj"
    _ = _run_cli(
        "--batch", str(src), "-o", str(tmp_path / "dummy"),
        "--bleed", "5",
        "--save-project", str(proj_path),
    )

    # Naload projekt i sprawdz parametry
    p = Project.load(str(proj_path))
    assert abs(p.bleed.bleed_mm - 5.0) < 0.01, \
        f"Projekt nie zachowal bleed=5: {p.bleed.bleed_mm}"

    # Output z projektu musi miec bleed5mm w nazwie
    out = tmp_path / "out"
    r = _run_cli("--project", str(proj_path), "-o", str(out))
    assert r.returncode == 0, r.stderr
    pdfs = list(out.glob("*.pdf"))
    assert len(pdfs) == 1
    assert "bleed5mm" in pdfs[0].name, f"Brak bleed5mm w nazwie: {pdfs[0].name}"


# ============================================================================
# Preflight gate
# ============================================================================

def test_cli_preflight_off_exports_unchecked(tmp_path: Path):
    """--preflight off nie blokuje nawet pliku z issue."""
    src_file = make_rectangle_vector(tmp_path, w_mm=30, h_mm=20)
    out = tmp_path / "out_off"
    r = _run_cli(str(src_file), "-o", str(out), "--preflight", "off")
    assert r.returncode == 0
    assert len(list(out.glob("*.pdf"))) == 1


def test_cli_preflight_lenient_passes_good_file(tmp_path: Path):
    """--preflight lenient (default) przepuszcza poprawny plik wektorowy."""
    src_file = make_rectangle_vector(tmp_path, w_mm=40, h_mm=30)
    out = tmp_path / "out_len"
    r = _run_cli(str(src_file), "-o", str(out), "--preflight", "lenient")
    assert r.returncode == 0
    assert len(list(out.glob("*.pdf"))) == 1


# ============================================================================
# Cache (CLI-level behavior)
# ============================================================================

def test_cli_no_cache_flag_sets_env(tmp_path: Path):
    """--no-cache ustawia BLEED_NO_CACHE=1 dla workerow (weryfikujemy ze CLI dziala)."""
    src_file = make_rectangle_vector(tmp_path, w_mm=40, h_mm=30)
    out = tmp_path / "out_nc"
    r = _run_cli(str(src_file), "-o", str(out), "--no-cache")
    assert r.returncode == 0
    assert len(list(out.glob("*.pdf"))) == 1


def test_cli_clear_cache_exits_zero(tmp_path: Path):
    """--clear-cache wypisuje liczbe usunietych i konczy z exit 0."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    r = _run_cli("--clear-cache", env_extra={"BLEED_CACHE_DIR": str(cache_dir)})
    assert r.returncode == 0
    # Output: "Usunieto N plik(ow) cache..."
    assert "cache" in r.stdout.lower() or "cache" in r.stderr.lower()


def test_cli_overwrite_replaces_existing(tmp_path: Path):
    """--overwrite nadpisuje istniejace pliki (zamiast _v2 suffix)."""
    src_file = make_rectangle_vector(tmp_path, w_mm=40, h_mm=30)
    out = tmp_path / "out_ow"

    # Pierwsze wywolanie
    r1 = _run_cli(str(src_file), "-o", str(out))
    assert r1.returncode == 0
    files_first = sorted(p.name for p in out.glob("*.pdf"))
    assert len(files_first) == 1

    # Drugie wywolanie z --overwrite: liczba plikow nie rosnie
    r2 = _run_cli(str(src_file), "-o", str(out), "--overwrite")
    assert r2.returncode == 0
    files_second = sorted(p.name for p in out.glob("*.pdf"))
    assert files_second == files_first, (
        f"--overwrite powinno zachowac jeden plik, jest {files_second}"
    )
