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
import os
import sys
import time

# Dodaj katalog bleed-tool do path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import DEFAULT_BLEED_MM

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


def run_bleed(input_path: str, output_dir: str, bleed_mm: float,
              file_list: list[str] | None = None, white: bool = False):
    """Generuje bleed dla plików w input_path lub z podanej listy file_list."""
    from modules.contour import detect_contour
    from modules.bleed import generate_bleed
    from modules.export import export_single_sticker

    files = file_list if file_list is not None else find_files(input_path)
    if not files:
        print("Brak plikow do przetworzenia.")
        return

    os.makedirs(output_dir, exist_ok=True)

    total = len(files)
    print(f"[i] Przetwarzam {total} plik(ow), bleed={bleed_mm}mm")
    print(f"[i] Output: {output_dir}\n")

    t0 = time.time()
    ok, err = 0, 0

    for idx, filepath in enumerate(files, start=1):
        name = os.path.splitext(os.path.basename(filepath))[0]
        basename = os.path.basename(filepath)

        if total > 1:
            print(f"Processing file {idx}/{total}: {basename}...")

        try:
            stickers = detect_contour(filepath)
            multi = len(stickers) > 1

            for sticker in stickers:
                if multi:
                    out = os.path.join(output_dir, f"bleed_{name}_p{sticker.page_index + 1}.pdf")
                    label = f"{name} p{sticker.page_index + 1}"
                else:
                    out = os.path.join(output_dir, f"bleed_{name}.pdf")
                    label = name

                try:
                    sticker = generate_bleed(sticker, bleed_mm=bleed_mm)
                    info = export_single_sticker(sticker, out, bleed_mm=bleed_mm, white=white)

                    size_kb = os.path.getsize(out) / 1024
                    print(
                        f"  [OK] {label}: "
                        f"{info['output_size_mm'][0]:.1f}x{info['output_size_mm'][1]:.1f}mm "
                        f"({size_kb:.1f}KB)"
                    )
                    ok += 1
                except Exception as e:
                    print(f"  [ERR] {label}: {e}")
                    err += 1

            # Zamknij dokument po przetworzeniu wszystkich stron
            if stickers[0].pdf_doc is not None:
                stickers[0].pdf_doc.close()

        except Exception as e:
            print(f"  [ERR] {name}: {e}")
            err += 1

    elapsed = time.time() - t0
    print(f"\nDone: {ok} OK, {err} errors in {elapsed:.1f}s")


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
        "-v", "--verbose",
        action="store_true",
        help="Verbose logging",
    )

    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")

    if args.batch:
        # Tryb batch — przetworz caly katalog
        files = find_batch_files(args.batch, recursive=args.recursive)
        if not files:
            ext_list = ", ".join(_SUPPORTED_EXT)
            print(f"Brak obslugiwanych plikow ({ext_list}) w: {args.batch}")
            sys.exit(1)
        run_bleed(args.batch, args.output, args.bleed, file_list=files, white=args.white)
    elif args.input:
        # Tryb pojedynczy — oryginalny (backward compatible)
        if args.recursive:
            print("[!] Flaga --recursive dziala tylko z --batch")
            sys.exit(1)
        run_bleed(args.input, args.output, args.bleed, white=args.white)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
