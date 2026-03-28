"""
Sticker Toolkit — marks.py
=============================
Znaczniki rejestracji dla ploterów tnących.

Obsługiwane typy:
  - OPOS Rectangle (Summa S3): czarne wypełnione prostokąty
  - Crosshair (JWEI): krzyżyki
  - Crop Mark (Mimaki): znaczniki przycięcia

Układ znaczników (Summa S3 OPOS):
  - 3 znaczniki minimum: lewy-dolny, prawy-dolny, prawy-górny
  - 4 znaczniki: + lewy-górny (dla lepszej korekcji)
  - Umieszczone w mark_zone (25mm domyślnie) wewnątrz marginesu

Znaczniki muszą być:
  - Czarny fill (#000000) na białym tle
  - Minimalny margines 5mm wokół (kontrast dla kamery)
  - Dokładne pozycje (rejestracja plotera)
"""

from __future__ import annotations

import logging
from models import Sheet, Mark
from config import PLOTTERS

log = logging.getLogger(__name__)


def generate_marks(
    sheet: Sheet,
    plotter: str = "summa_s3",
) -> Sheet:
    """Generuje znaczniki rejestracji na arkuszu.

    Umieszcza markery w mark_zone wewnątrz marginesu arkusza.

    Args:
        sheet: Sheet z placements i panel_lines
        plotter: nazwa plotera (klucz z config.PLOTTERS)

    Returns:
        Sheet z wypełnioną listą marks.
    """
    plotter_config = PLOTTERS.get(plotter)
    if plotter_config is None:
        raise ValueError(f"Nieznany ploter: {plotter}. Dostępne: {list(PLOTTERS.keys())}")

    mark_type = plotter_config["mark_type"]
    mark_w, mark_h = plotter_config["mark_size_mm"]
    mark_offset = plotter_config["mark_offset_mm"]
    min_marks = plotter_config["min_marks"]

    top, right, bottom, left = sheet.margins_mm
    mz = sheet.mark_zone_mm
    sw = sheet.width_mm
    sh = sheet.height_mm

    # Pozycje znaczników — w mark_zone, offset od krawędzi panelu
    # Mark zone jest między marginesem a printable area

    # Obszar panelu (printable area)
    px0, py0, px1, py1 = sheet.printable_rect_mm

    # Pozycje narożne — w mark_zone, z offsetem
    positions = {
        "bottom_left": (
            left + mark_offset,
            bottom + mark_offset,
        ),
        "bottom_right": (
            sw - right - mark_offset - mark_w,
            bottom + mark_offset,
        ),
        "top_right": (
            sw - right - mark_offset - mark_w,
            sh - top - mark_offset - mark_h,
        ),
        "top_left": (
            left + mark_offset,
            sh - top - mark_offset - mark_h,
        ),
    }

    # Wybierz liczbę znaczników
    if min_marks >= 4:
        selected = ["bottom_left", "bottom_right", "top_right", "top_left"]
    else:
        # Minimum 3: LB, RB, RT (standardowy OPOS Summa)
        selected = ["bottom_left", "bottom_right", "top_right"]

    sheet.marks = []
    for corner in selected:
        x, y = positions[corner]
        mark = Mark(
            x_mm=x,
            y_mm=y,
            width_mm=mark_w,
            height_mm=mark_h,
            mark_type=mark_type,
        )
        sheet.marks.append(mark)
        log.info(f"Mark [{corner}]: ({x:.1f}, {y:.1f})mm, {mark_w}×{mark_h}mm, {mark_type}")

    # Dodatkowe markery / pasek na dolnej i gornej krawedzi
    # Summa S3: dolna krawedz = dlugi pasek (bar) + extra markery na gornej
    # JWEI: tylko 4 narożne kwadraty — bez paska, bez extra markerow

    if plotter == "summa_s3":
        # DOLNA KRAWEDZ: dlugi pasek (bar) MIEDZY naroznymi markerami
        # Gap 3mm miedzy paskiem a naroznymi kwadracikami
        bar_gap = 30.0
        bl_x = positions["bottom_left"][0]
        br_x = positions["bottom_right"][0]
        bar_x = bl_x + mark_w + bar_gap   # zaczyna sie za lewym markerem + gap
        bar_end = br_x - bar_gap           # konczy sie przed prawym markerem
        bar_w = bar_end - bar_x
        if bar_w > 1.0:
            bar = Mark(
                x_mm=bar_x,
                y_mm=positions["bottom_left"][1],
                width_mm=bar_w,
                height_mm=mark_h,
                mark_type=mark_type,
            )
            sheet.marks.append(bar)
            log.info(f"Mark [bottom bar]: ({bar_x:.1f}, {positions['bottom_left'][1]:.1f})mm, {bar_w:.1f}×{mark_h}mm")

        # GORNA KRAWEDZ: extra markery co ~300mm (jak dotychczas)
        if sw > 400:
            n_extra_h = max(0, int((px1 - px0) / 300) - 1)
            if n_extra_h > 0:
                spacing = (br_x - bl_x) / (n_extra_h + 1)
                for i in range(1, n_extra_h + 1):
                    x = bl_x + spacing * i
                    mark_top = Mark(
                        x_mm=x,
                        y_mm=positions["top_right"][1],
                        width_mm=mark_w,
                        height_mm=mark_h,
                        mark_type=mark_type,
                    )
                    sheet.marks.append(mark_top)
                    log.info(f"Mark [extra top H{i}]: ({x:.1f}mm)")

    elif plotter != "jwei" and sw > 400:
        # Inne plotery (nie JWEI): standardowe extra markery na dolnej i gornej krawedzi
        n_extra_h = max(0, int((px1 - px0) / 300) - 1)
        if n_extra_h > 0:
            spacing = (positions["bottom_right"][0] - positions["bottom_left"][0]) / (n_extra_h + 1)
            for i in range(1, n_extra_h + 1):
                x = positions["bottom_left"][0] + spacing * i
                # Dolna krawedz
                mark = Mark(
                    x_mm=x,
                    y_mm=positions["bottom_left"][1],
                    width_mm=mark_w,
                    height_mm=mark_h,
                    mark_type=mark_type,
                )
                sheet.marks.append(mark)
                # Gorna krawedz
                mark_top = Mark(
                    x_mm=x,
                    y_mm=positions["top_right"][1],
                    width_mm=mark_w,
                    height_mm=mark_h,
                    mark_type=mark_type,
                )
                sheet.marks.append(mark_top)
                log.info(f"Mark [extra H{i}]: bottom ({x:.1f}mm), top ({x:.1f}mm)")

    if plotter != "jwei" and sh > 400:
        # Vertical marks na lewej/prawej krawędzi (nie dla JWEI — tylko 4 narożniki)
        n_extra_v = max(0, int((py1 - py0) / 300) - 1)
        if n_extra_v > 0:
            spacing = (positions["top_left"][1] - positions["bottom_left"][1]) / (n_extra_v + 1)
            for i in range(1, n_extra_v + 1):
                y = positions["bottom_left"][1] + spacing * i
                # Lewa krawędź
                mark = Mark(
                    x_mm=positions["bottom_left"][0],
                    y_mm=y,
                    width_mm=mark_w,
                    height_mm=mark_h,
                    mark_type=mark_type,
                )
                sheet.marks.append(mark)
                # Prawa krawędź
                mark_right = Mark(
                    x_mm=positions["bottom_right"][0],
                    y_mm=y,
                    width_mm=mark_w,
                    height_mm=mark_h,
                    mark_type=mark_type,
                )
                sheet.marks.append(mark_right)
                log.info(f"Mark [extra V{i}]: left ({y:.1f}mm), right ({y:.1f}mm)")

    log.info(f"Marks: {len(sheet.marks)} znaczników ({plotter}, {mark_type})")
    return sheet
