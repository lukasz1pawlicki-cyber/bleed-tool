#!/usr/bin/env python3
"""
Bleed Tool — CLI
==================
Generuje bleed (offset konturu + kolor krawędzi) dla naklejek wektorowych.

Przykłady:
  python bleed_cli.py plik.pdf
  python bleed_cli.py input/ -o output/ --bleed 3.0
  python bleed_cli.py plik.svg --bleed 2.0
  python bleed_cli.py --batch ./input_folder -o ./out --bleed 2
  python bleed_cli.py --batch ./input_folder --recursive -o ./out
"""

from __future__ import annotations

import argparse
import glob
import logging
import multiprocessing as mp
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

# Dodaj katalog bleed-tool do path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import DEFAULT_BLEED_MM
from models import build_output_name

log = logging.getLogger("bleed-tool")

_SUPPORTED_EXT = (
    '.pdf', '.svg', '.png', '.jpg', '.jpeg', '.tiff', '.tif',
    '.bmp', '.webp', '.eps', '.epsf', '.ai',
)
_GLOB_PATTERNS = (
    "*.pdf", "*.svg", "*.png", "*.jpg", "*.jpeg", "*.tiff", "*.tif",
    "*.bmp", "*.webp", "*.eps", "*.epsf", "*.ai",
)


def find_files(path: str) -> list[str]:
    """Znajduje pliki PDF, SVG i rastrowe w podanej ścieżce."""
    if os.path.isfile(path):
        if path.lower().endswith(_SUPPORTED_EXT):
            return [path]
        else:
            print(f"[!] Nieobslugiwany format: {path}")
            return []
    elif os.path.isdir(path):
        files = []
        for pat in _GLOB_PATTERNS:
            files.extend(glob.glob(os.path.join(path, pat)))
        files = sorted(files)
        if not files:
            for pat in _GLOB_PATTERNS:
                files.extend(glob.glob(os.path.join(path, "**", pat), recursive=True))
            files = sorted(files)
        return files
    else:
        print(f"[!] Nie znaleziono: {path}")
        return []


def find_batch_files(folder: str, recursive: bool = False) -> list[str]:
    """Znajduje wszystkie obsługiwane pliki w katalogu (batch mode)."""
    if not os.path.isdir(folder):
        print(f"[!] Katalog nie istnieje: {folder}")
        return []

    files: list[str] = []
    if recursive:
        for pat in _GLOB_PATTERNS:
            files.extend(glob.glob(os.path.join(folder, "**", pat), recursive=True))
    else:
        for pat in _GLOB_PATTERNS:
            files.extend(glob.glob(os.path.join(folder, pat)))

    return sorted(set(files))


def _unique_output_path(path: str) -> str:
    """Zwraca ścieżkę, która nie istnieje: dopisuje suffix _v2, _v3, ..."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 2
    while True:
        candidate = f"{stem}_v{i}{ext}"
        if not os.path.exists(candidate):
            return candidate
        i += 1


def _resolve_output(path: str, overwrite: bool) -> str | None:
    """Rozstrzyga kolizję nazw. Zwraca None jeśli pomijamy plik.

    overwrite=True  → nadpisz istniejący plik
    overwrite=False → dopisz suffix _v2, _v3, ... (nie trać pracy operatora)
    """
    if not os.path.exists(path) or overwrite:
        return path
    new_path = _unique_output_path(path)
    print(f"  [i] Plik istnieje, zapis jako: {os.path.basename(new_path)}")
    return new_path


def _process_one_file(args: tuple) -> dict:
    """Worker funkcji dla ProcessPoolExecutor.

    Musi byc funkcja top-level (nie nested) — inaczej multiprocessing.Pool
    nie umie pickle'owac. Argumenty przekazujemy jako jedna krotka
    dla zgodnosci z executor.map(), a zwracamy slownik z wynikami.

    Kazdy worker otwiera plik samodzielnie (fitz.Document nie jest picklable).
    """
    (filepath, output_dir, bleed_mm, white, overwrite, preflight_mode,
     black_100k, cutline_mode, engine) = args

    # Import wewnatrz workera: lazy load ciezkich zaleznosci (fitz, cv2).
    import config
    from modules.contour import detect_contour
    from modules.bleed import generate_bleed
    from modules.export import export_single_sticker

    # Ustaw silnik konturu per-worker (subprocess ma swoj config module).
    if engine:
        config.CONTOUR_ENGINE = engine

    name = os.path.splitext(os.path.basename(filepath))[0]
    results: list[dict] = []

    # === PREFLIGHT GATE (opcjonalnie) ===
    # preflight_mode: "off" | "lenient" (errors blokuja) | "strict" (+warnings)
    if preflight_mode != "off":
        try:
            from modules.preflight import preflight_gate, preflight_summary
            can_export, pf = preflight_gate(filepath, strict=(preflight_mode == "strict"))
            if not can_export:
                reason = preflight_summary(pf)
                issue_texts = [
                    f"[{i['severity']}] {i['code']}: {i['message']}"
                    for i in (pf.get("issues", []) + pf.get("warnings", []))
                ]
                err_msg = f"preflight BLOCK: {reason} — " + "; ".join(issue_texts[:3])
                results.append({"ok": False, "label": name, "error": err_msg})
                return {"filepath": filepath, "results": results}
        except Exception as e:
            # Preflight sam nie moze zablokowac eksportu — log i kontynuuj
            results.append({
                "ok": False, "label": name,
                "error": f"preflight crashed: {e} (kontynuacja)"
            })
            # Nie return — probujemy dalej

    stickers = []
    try:
        stickers = detect_contour(filepath)
        multi = len(stickers) > 1

        for sticker in stickers:
            if multi:
                label = f"{name} p{sticker.page_index + 1}"
                page_idx = sticker.page_index
            else:
                label = name
                page_idx = None

            try:
                sticker = generate_bleed(sticker, bleed_mm=bleed_mm)
                out_name = build_output_name(
                    filepath, sticker.width_mm, sticker.height_mm,
                    bleed_mm, page_index=page_idx,
                )
                out = os.path.join(output_dir, out_name)
                out = _resolve_output(out, overwrite=overwrite)
                info = export_single_sticker(
                    sticker, out, bleed_mm=bleed_mm,
                    black_100k=black_100k,
                    cutcontour=(cutline_mode != "none"),
                    cutline_mode=cutline_mode,
                    white=white,
                )
                size_kb = os.path.getsize(out) / 1024
                results.append({
                    "ok": True,
                    "label": label,
                    "size_mm": info['output_size_mm'],
                    "size_kb": size_kb,
                    "out": out,
                })
            except Exception as e:
                results.append({"ok": False, "label": label, "error": str(e)})

    except Exception as e:
        results.append({"ok": False, "label": name, "error": str(e)})
    finally:
        # Cleanup zasobów źródła — wspóldzielony pdf_doc + tmp PDF z EPS/SVG.
        # Wykonuje się niezależnie od sukcesu/porażki eksportu pojedynczych stron.
        if stickers and stickers[0].pdf_doc is not None:
            try:
                stickers[0].pdf_doc.close()
            except Exception:
                pass
        if stickers and stickers[0].tmp_pdf_path:
            try:
                os.unlink(stickers[0].tmp_pdf_path)
            except OSError:
                pass

    return {"filepath": filepath, "results": results}


def _print_file_result(result: dict) -> tuple[int, int]:
    """Wypisuje wyniki pojedynczego pliku. Zwraca (ok_count, err_count)."""
    ok = err = 0
    for r in result["results"]:
        if r["ok"]:
            size_mm = r["size_mm"]
            print(
                f"  [OK] {r['label']}: "
                f"{size_mm[0]:.1f}x{size_mm[1]:.1f}mm "
                f"({r['size_kb']:.1f}KB)"
            )
            ok += 1
        else:
            print(f"  [ERR] {r['label']}: {r['error']}")
            err += 1
    return (ok, err)


def run_bleed(input_path: str, output_dir: str, bleed_mm: float,
              file_list: list[str] | None = None, white: bool = False,
              overwrite: bool = False, fail_fast: bool = False,
              jobs: int = 1, preflight_mode: str = "lenient",
              black_100k: bool = False, cutline_mode: str = "kiss-cut",
              engine: str | None = None) -> tuple[int, int]:
    """Generuje bleed dla plików w input_path lub z podanej listy file_list.

    Args:
        jobs: liczba rownoleglych procesow (1 = sekwencyjnie, typowo speedup
              rowny ~min(jobs, n_plików, n_cpu) dla niezaleznych plikow).
              Dla multi-page PDF pojedynczy plik wciaz jest przetwarzany
              sekwencyjnie po stronach w jednym workerze.
        preflight_mode: "off" | "lenient" (domyslnie — errors blokuja) |
                        "strict" (warningi tez blokuja).

    Zwraca (ok, err) — liczbę udanych i nieudanych eksportów.
    """
    files = file_list if file_list is not None else find_files(input_path)
    if not files:
        print("Brak plikow do przetworzenia.")
        return (0, 0)

    os.makedirs(output_dir, exist_ok=True)

    total = len(files)
    jobs = max(1, min(jobs, total))
    mode = f", jobs={jobs}" if jobs > 1 else ""
    pf_info = "" if preflight_mode == "off" else f", preflight={preflight_mode}"
    print(f"[i] Przetwarzam {total} plik(ow), bleed={bleed_mm}mm{mode}{pf_info}")
    print(f"[i] Output: {output_dir}\n")

    t0 = time.time()
    ok, err = 0, 0

    # fail_fast w trybie rownoleglym jest problematyczny (running workers
    # nie sa zabijane natychmiast) — dla spojnosci wymuszamy sekwencje.
    if jobs > 1 and fail_fast:
        print("[i] --fail-fast wymusza --jobs 1 (sekwencyjnie)")
        jobs = 1

    if jobs == 1:
        # Tryb sekwencyjny (backward compatible)
        for idx, filepath in enumerate(files, start=1):
            basename = os.path.basename(filepath)
            if total > 1:
                print(f"Processing file {idx}/{total}: {basename}...")
            result = _process_one_file(
                (filepath, output_dir, bleed_mm, white, overwrite,
                 preflight_mode, black_100k, cutline_mode, engine)
            )
            file_ok, file_err = _print_file_result(result)
            ok += file_ok
            err += file_err
            if fail_fast and file_err > 0:
                print(f"\n[FATAL] Przerwanie (--fail-fast)")
                break
    else:
        # Tryb rownolegly — ProcessPoolExecutor
        tasks = [(f, output_dir, bleed_mm, white, overwrite, preflight_mode,
                  black_100k, cutline_mode, engine)
                 for f in files]
        completed = 0
        with ProcessPoolExecutor(max_workers=jobs) as executor:
            # Zachowaj mapowanie future → filepath dla lepszego reportingu
            future_to_file = {
                executor.submit(_process_one_file, t): t[0] for t in tasks
            }
            for future in as_completed(future_to_file):
                completed += 1
                filepath = future_to_file[future]
                basename = os.path.basename(filepath)
                print(f"Processing file {completed}/{total}: {basename}...")
                try:
                    result = future.result()
                except Exception as e:
                    print(f"  [ERR] {basename}: worker crash: {e}")
                    err += 1
                    continue
                file_ok, file_err = _print_file_result(result)
                ok += file_ok
                err += file_err

    elapsed = time.time() - t0
    print(f"\nDone: {ok} OK, {err} errors in {elapsed:.1f}s")
    return (ok, err)


def main():
    parser = argparse.ArgumentParser(
        prog="bleed-tool",
        description="Bleed Tool — Generuje bleed dla naklejek wektorowych (PDF/SVG/raster)",
    )
    parser.add_argument(
        "input",
        nargs="?",
        default=None,
        help="Plik lub katalog z plikami (tryb pojedynczy)",
    )
    parser.add_argument(
        "--batch",
        metavar="FOLDER",
        default=None,
        help="Tryb batch: przetworz wszystkie obslugiwane pliki w katalogu",
    )
    parser.add_argument(
        "--recursive", "-r",
        action="store_true",
        help="Szukaj plikow rekurencyjnie w podkatalogach (tylko z --batch)",
    )
    parser.add_argument(
        "-o", "--output",
        default="output",
        help="Katalog wyjsciowy (domyslnie: output/)",
    )
    parser.add_argument(
        "--bleed",
        type=float,
        default=DEFAULT_BLEED_MM,
        help=f"Wielkosc bleed w mm (domyslnie: {DEFAULT_BLEED_MM})",
    )
    parser.add_argument(
        "--white",
        action="store_true",
        help="Dodaj bialy poddruk (White ink) pod grafika",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Nadpisuj istniejace pliki wyjsciowe (domyslnie: dopisuje suffix _v2)",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Przerwij batch przy pierwszym bledzie (domyslnie: kontynuuj)",
    )
    parser.add_argument(
        "-j", "--jobs",
        type=int,
        default=1,
        metavar="N",
        help="Liczba rownoleglych procesow dla batch (domyslnie: 1 = sekwencyjnie). "
             "Uzyj 0 dla auto = liczba rdzeni CPU. Typowy speedup 4-8x na multi-core.",
    )
    parser.add_argument(
        "--project",
        metavar="FILE",
        help="Laduj projekt .bleedproj (zastepuje input/--batch/--bleed/--white)",
    )
    parser.add_argument(
        "--save-project",
        metavar="FILE",
        help="Zapisz biezaca konfiguracje jako .bleedproj (razem z lista plikow)",
    )
    parser.add_argument(
        "--preflight",
        choices=["off", "lenient", "strict"],
        default="lenient",
        help="Preflight gate przed eksportem: off=brak, lenient=blokuj bledy (default), "
             "strict=blokuj bledy+ostrzezenia",
    )
    parser.add_argument(
        "--sharp-edges",
        action="store_true",
        help="Sharp-edge mode dla rastrow: zachowuje ostre narozniki (gwiazdki, "
             "strzalki, diamenty). Domyslnie: smooth (wygladzone krzywe Bezier). "
             "Uzyj dla geometrycznych ksztaltow z ostrymi kątami.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Wylacz cache detect_contour (zawsze od nowa)",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Wyczysc cache detect_contour i zakoncz",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose logging",
    )

    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")

    # --clear-cache: wyczysc i zakoncz
    if args.clear_cache:
        from modules import cache as _cache
        n = _cache.clear_all()
        print(f"Usunieto {n} plik(ow) cache ({_cache.size_bytes()} B pozostale)")
        sys.exit(0)

    # --no-cache: ustaw zmienna srodowiskowa dla wszystkich workerow
    if args.no_cache:
        os.environ["BLEED_NO_CACHE"] = "1"

    # --sharp-edges: przelacz tryb rastrow na sharp (ostre narozniki)
    if args.sharp_edges:
        os.environ["BLEED_RASTER_MODE"] = "sharp"
        # Przeladuj config zeby workery subprocess widzialy zmiane
        import config as _config
        _config.RASTER_MODE = "sharp"

    # --project: zaladuj plik projektu i zastap flagi CLI
    project_files: list[str] | None = None
    project_black_100k = False
    project_cutline_mode = "kiss-cut"
    project_engine: str | None = None
    if args.project:
        from modules.project import Project
        proj = Project.load(args.project)
        print(f"[i] Projekt: {proj.name} ({len(proj.files)} plik(ow))")
        missing = proj.missing_files()
        if missing:
            print(f"[!] Brakujace pliki ({len(missing)}):")
            for m in missing:
                print(f"    {m}")
        project_files = [f.path for f in proj.valid_files()]
        if not project_files:
            print("[!] Brak prawidlowych plikow w projekcie")
            sys.exit(1)
        # Parametry z projektu (o ile user nie podal explicit na CLI)
        if args.bleed == DEFAULT_BLEED_MM:
            args.bleed = proj.bleed.bleed_mm
        if not args.white:
            args.white = proj.bleed.white
        # Pelna konfiguracja BleedParams — bez tego --project ignorowal
        # engine/black_100k/cutline_mode mimo zapisu w .bleedproj.
        project_black_100k = bool(proj.bleed.black_100k)
        project_cutline_mode = str(proj.bleed.cutline_mode)
        project_engine = str(proj.bleed.engine) if proj.bleed.engine else None

    # Rozwiazanie liczby jobs: 0 = auto (cpu count)
    jobs = args.jobs
    if jobs == 0:
        jobs = mp.cpu_count()

    if project_files is not None:
        # Tryb projektu — pliki z .bleedproj
        try:
            _ok, err = run_bleed(
                os.path.dirname(args.project) or ".",
                args.output, args.bleed,
                file_list=project_files, white=args.white,
                overwrite=args.overwrite, fail_fast=args.fail_fast,
                jobs=jobs, preflight_mode=args.preflight,
                black_100k=project_black_100k,
                cutline_mode=project_cutline_mode,
                engine=project_engine,
            )
        except Exception:
            sys.exit(2)
        sys.exit(0 if err == 0 else 1)

    if args.batch:
        # Tryb batch — przetworz caly katalog
        files = find_batch_files(args.batch, recursive=args.recursive)
        if not files:
            ext_list = ", ".join(_SUPPORTED_EXT)
            print(f"Brak obslugiwanych plikow ({ext_list}) w: {args.batch}")
            sys.exit(1)
        # --save-project: zapisz aktualna konfiguracje
        if args.save_project:
            from modules.project import Project, ProjectFile, BleedParams
            proj = Project(
                files=[ProjectFile(path=os.path.abspath(f)) for f in files],
                bleed=BleedParams(bleed_mm=args.bleed, white=args.white),
                name=os.path.splitext(os.path.basename(args.save_project))[0],
            )
            proj.save(args.save_project)
            print(f"[i] Projekt zapisany: {args.save_project}")
        try:
            _ok, err = run_bleed(
                args.batch, args.output, args.bleed,
                file_list=files, white=args.white,
                overwrite=args.overwrite, fail_fast=args.fail_fast,
                jobs=jobs, preflight_mode=args.preflight,
            )
        except Exception:
            sys.exit(2)
        sys.exit(0 if err == 0 else 1)
    elif args.input:
        # Tryb pojedynczy — oryginalny (backward compatible)
        if args.recursive:
            print("[!] Flaga --recursive dziala tylko z --batch")
            sys.exit(1)
        try:
            _ok, err = run_bleed(
                args.input, args.output, args.bleed,
                white=args.white, overwrite=args.overwrite,
                fail_fast=args.fail_fast, jobs=jobs,
                preflight_mode=args.preflight,
            )
        except Exception:
            sys.exit(2)
        sys.exit(0 if err == 0 else 1)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
