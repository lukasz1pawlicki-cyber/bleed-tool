"""
Sticker Toolkit — panelize.py
===============================
Podział dużego arkusza na mniejsze sub-arkusze liniami FlexCut.

FlexCut = linia perforacji realizowana przez ploter (np. Summa S3 / FlexiSign).
W PDF rysujemy ciągłą linię w spot color "FlexCut" — ploter sam tworzy perforację.

Sub-arkusze to **zamknięte prostokąty** FlexCut. Sąsiednie prostokąty dzielą
wspólną krawędź (nie ma zdublowanych linii).

Podział 2D — zarówno poziomy (wiersze) jak i pionowy (kolumny) — pozwala
dzielić duże arkusze (np. 1320×1000) na sub-arkusze z max N naklejek.

Algorytm:
  1. Nesting rozmieszcza naklejki na dużym arkuszu (oryginalny nesting)
  2. Panelize wykrywa wiersze nestingowe (shelf boundaries)
  3. Wiersze grupowane w "pasy" wg wzoru (source_path)
  4. Pasy dzielone wg max_per_subsheet — ile naklejek na sub-arkusz
  5. FlexCut = zamknięte prostokąty wokół grup (tight bounding box + fc_gap)
  6. Naklejki centrowane XY w swoim sub-arkuszu FlexCut

fc_gap = odległość od naklejek do linii FlexCut.
Przy fc_gap=0 FlexCut przylega do naklejek — klient dostaje "blok" naklejek
z CutContour rozdzielającym je wewnątrz.
"""

from __future__ import annotations

import logging
import math
from models import Sheet, Placement, PanelLine
from config import FLEXCUT_GAP_MM

log = logging.getLogger(__name__)

# Presety sub-arkuszy FlexCut (label → max naklejek na sub-arkusz)
SUBSHEET_PRESETS: dict[str, int] = {
    "2": 2,
    "4": 4,
    "6": 6,
    "8": 8,
    "10": 10,
    "12": 12,
    "18": 18,
    "24": 24,
}

# Presety rozmiarów sub-arkuszy FlexCut (landscape, mm)
SUBSHEET_SIZE_PRESETS: dict[str, tuple[float, float]] = {
    "A4": (297, 210),
    "A3": (420, 297),
}


def panelize_sheet(
    sheet: Sheet,
    flexcut: bool = True,
    max_per_subsheet: int | None = None,
    flexcut_gap_mm: float = FLEXCUT_GAP_MM,
    subsheet_size_mm: tuple[float, float] | None = None,
) -> Sheet:
    """Dzieli arkusz na sub-arkusze FlexCut.

    FlexCut prostokąty otaczają grupy naklejek (tight bounding box + fc_gap).
    Naklejki centrowane XY w sub-arkuszach.

    Args:
        sheet: Arkusz z placements do podzielenia.
        flexcut: Czy włączony FlexCut.
        max_per_subsheet: Maks. naklejek na sub-arkusz (None = bez podziału).
        flexcut_gap_mm: Odległość od naklejek do linii FlexCut.

    Algorytm:
      1. Wykryj wiersze (shelves) z nestingu wg Y-pozycji
      2. Pogrupuj wiersze w pasy wg wzoru (source_path)
      3. Podziel pasy wg max_per_subsheet
      4. FlexCut = tight bbox + fc_gap per sub-arkusz
      5. Centruj XY w każdym sub-arkuszu
    """
    sheet.panel_lines = []

    if not flexcut:
        log.info("FlexCut wyłączony — pomijam panelizację")
        return sheet

    if not sheet.placements:
        log.info("Brak placements — pomijam panelizację")
        return sheet

    # Oblicz content bounding box
    content_left = min(p.x_mm for p in sheet.placements)
    content_right = max(p.x_mm + _pw(p) for p in sheet.placements)
    content_bottom = min(p.y_mm for p in sheet.placements)
    content_top = max(p.y_mm + _ph(p) for p in sheet.placements)
    content_w = content_right - content_left
    content_h = content_top - content_bottom

    if subsheet_size_mm is not None:
        # =============================================================
        # TRYB SIZE-BASED — dziel arkusz na prostokąty ~target size
        # =============================================================
        target_w, target_h = subsheet_size_mm

        # Wypróbuj obie orientacje, wybierz lepszą (mniej sub-arkuszy)
        n_cols_a = max(1, math.ceil(content_w / target_w))
        n_rows_a = max(1, math.ceil(content_h / target_h))
        n_cols_b = max(1, math.ceil(content_w / target_h))
        n_rows_b = max(1, math.ceil(content_h / target_w))

        if n_cols_a * n_rows_a <= n_cols_b * n_rows_b:
            n_cols, n_rows = n_cols_a, n_rows_a
        else:
            n_cols, n_rows = n_cols_b, n_rows_b

        cell_w = content_w / n_cols
        cell_h = content_h / n_rows

        log.info(
            f"Panelizacja SIZE: target {target_w:.0f}×{target_h:.0f}mm → "
            f"siatka {n_cols}×{n_rows}, cell {cell_w:.0f}×{cell_h:.0f}mm, "
            f"fc_gap={flexcut_gap_mm:.1f}mm"
        )

        # H-boundaries: równomierny podział contentu
        h_boundaries: list[float] = []
        h_boundaries.append(content_bottom - flexcut_gap_mm)
        for i in range(1, n_rows):
            h_boundaries.append(content_bottom + i * cell_h)
        h_boundaries.append(content_top + flexcut_gap_mm)

        # Col edges do przypisania naklejek
        col_edges = [content_left + i * cell_w for i in range(n_cols + 1)]
        col_edges[-1] = content_right + 0.01

    else:
        # =============================================================
        # TRYB COUNT-BASED — istniejący algorytm (kroki 1-5)
        # =============================================================

        # === 1. Wykryj wiersze (shelves) z nestingu ===
        y_groups: dict[float, list[Placement]] = {}
        for p in sheet.placements:
            y_key = round(p.y_mm * 2) / 2
            y_groups.setdefault(y_key, []).append(p)

        sorted_y_keys = sorted(y_groups.keys())

        # === 2. Pogrupuj wiersze w pasy wg wzoru (source_path) ===
        bands: list[list[Placement]] = []
        current_band: list[Placement] = []
        current_source: str | None = None

        for y_key in sorted_y_keys:
            row_pls = y_groups[y_key]
            source_counts: dict[str, int] = {}
            for p in row_pls:
                sp = p.sticker.source_path
                source_counts[sp] = source_counts.get(sp, 0) + 1
            dominant = max(source_counts, key=source_counts.get)

            if current_source is None or dominant == current_source:
                current_band.extend(row_pls)
                current_source = dominant
            else:
                if current_band:
                    bands.append(current_band)
                current_band = list(row_pls)
                current_source = dominant

        if current_band:
            bands.append(current_band)

        log.info(f"Panelizacja: {len(bands)} pasów wzorów")

        # === 3. Oblicz granice H (między pasami) ===
        band_bounds: list[tuple[float, float, float, float]] = []
        for band in bands:
            bl = min(p.x_mm for p in band)
            br = max(p.x_mm + _pw(p) for p in band)
            bb = min(p.y_mm for p in band)
            bt = max(p.y_mm + _ph(p) for p in band)
            band_bounds.append((bl, br, bb, bt))

        log.info(
            f"  content {content_w:.0f}×{content_h:.0f}mm"
            + (f", max_per_sub={max_per_subsheet}" if max_per_subsheet else "")
            + f", fc_gap={flexcut_gap_mm:.1f}mm"
        )

        h_boundaries: list[float] = []
        h_boundaries.append(band_bounds[0][2] - flexcut_gap_mm)
        for i in range(len(bands) - 1):
            top_of_band_i = band_bounds[i][3]
            bottom_of_band_next = band_bounds[i + 1][2]
            mid = (top_of_band_i + bottom_of_band_next) / 2.0
            h_boundaries.append(mid)
        h_boundaries.append(band_bounds[-1][3] + flexcut_gap_mm)

        # === 4+5. Oblicz siatkę n_cols × rows_per_sub dążąc do prostokątów ===
        # Zamiast tylko poziomych pasów, wybierz podział na kolumny
        # który daje sub-arkusze najbliższe kwadratowi.
        n_cols = 1
        if max_per_subsheet is not None and max_per_subsheet > 0:
            # Zbierz wiersze z wszystkich pasów
            all_y_groups: dict[float, list[Placement]] = {}
            for p in sheet.placements:
                y_key = round(p.y_mm * 2) / 2
                all_y_groups.setdefault(y_key, []).append(p)
            all_y_keys = sorted(all_y_groups.keys())

            max_row_count = max(len(all_y_groups[yk]) for yk in all_y_keys)
            n_total_rows = len(all_y_keys)

            # Średni rozmiar naklejki (do oceny proporcji sub-arkusza)
            avg_w = sum(_pw(p) for p in sheet.placements) / len(sheet.placements)
            avg_h = sum(_ph(p) for p in sheet.placements) / len(sheet.placements)

            # Wypróbuj różne podziały na kolumny, wybierz najlepszy aspect ratio
            best_n_cols = 1
            best_rows_per_sub = n_total_rows
            best_aspect = float('inf')

            for try_cols in range(1, max_row_count + 1):
                stickers_per_col = math.ceil(max_row_count / try_cols)
                if stickers_per_col == 0:
                    continue
                rps = max_per_subsheet // stickers_per_col
                if rps < 1:
                    continue
                actual = stickers_per_col * rps
                if actual > max_per_subsheet:
                    rps -= 1
                    if rps < 1:
                        continue

                # Wymiary sub-arkusza
                sub_w = stickers_per_col * avg_w
                sub_h = rps * avg_h
                aspect = max(sub_w, sub_h) / max(min(sub_w, sub_h), 0.01)

                if aspect < best_aspect:
                    best_aspect = aspect
                    best_n_cols = try_cols
                    best_rows_per_sub = rps

            n_cols = best_n_cols
            rows_per_sub = best_rows_per_sub
            stickers_per_col = math.ceil(max_row_count / n_cols)

            log.info(
                f"  Grid: {n_cols} kol × {rows_per_sub} wierszy/sub "
                f"({stickers_per_col}×{rows_per_sub}={stickers_per_col * rows_per_sub} szt/sub), "
                f"aspect={best_aspect:.1f}"
            )

            # H-boundaries: dziel wiersze na grupy po rows_per_sub
            new_h_boundaries: list[float] = [h_boundaries[0]]

            for band_idx, band in enumerate(bands):
                band_top = h_boundaries[band_idx + 1]

                band_y_groups: dict[float, list[Placement]] = {}
                for p in band:
                    y_key = round(p.y_mm * 2) / 2
                    band_y_groups.setdefault(y_key, []).append(p)
                band_y_keys = sorted(band_y_groups.keys())

                accumulated_rows = 0
                for yi, y_key in enumerate(band_y_keys):
                    accumulated_rows += 1
                    is_last = (yi == len(band_y_keys) - 1)

                    if not is_last and accumulated_rows >= rows_per_sub:
                        row_pls = band_y_groups[y_key]
                        row_top = max(p.y_mm + _ph(p) for p in row_pls)
                        next_y = band_y_keys[yi + 1]
                        mid = (row_top + next_y) / 2.0
                        new_h_boundaries.append(mid)
                        accumulated_rows = 0

                new_h_boundaries.append(band_top)

            h_boundaries = new_h_boundaries

        n_rows = len(h_boundaries) - 1

        col_target = content_w / n_cols

        if n_cols > 1:
            log.info(f"  V: {n_cols} kolumn, target_w={col_target:.1f}mm")

        col_edges = [content_left + i * col_target for i in range(n_cols + 1)]
        col_edges[-1] = content_right + 0.01

    # === 6. Przypisz placements do sub-arkuszy ===
    grid: list[list[list[Placement]]] = [
        [[] for _ in range(n_cols)]
        for _ in range(n_rows)
    ]

    for p in sheet.placements:
        cx = p.x_mm + _pw(p) / 2.0
        cy = p.y_mm + _ph(p) / 2.0

        # Wiersz wg h_boundaries
        row_idx = n_rows - 1
        for ri in range(n_rows):
            if cy < h_boundaries[ri + 1]:
                row_idx = ri
                break

        # Kolumna wg col_edges
        col_idx = n_cols - 1
        for ci in range(n_cols):
            if cx < col_edges[ci + 1]:
                col_idx = ci
                break

        grid[row_idx][col_idx].append(p)

    # Jeśli tylko 1×1 i flexcut włączony — i tak pozycjonuj
    if n_cols <= 1 and n_rows <= 1:
        log.info("  Siatka 1×1 — pozycjonowanie XY bez FlexCut linii")
        # X: centruj, Y: wyrównaj do dołu printable area (nad paserami)
        pls = sheet.placements
        if pls:
            c_left = min(p.x_mm for p in pls)
            c_right = max(p.x_mm + _pw(p) for p in pls)
            c_bottom = min(p.y_mm for p in pls)

            pa_x0, pa_y0, pa_x1, pa_y1 = sheet.printable_rect_mm
            pa_cx = (pa_x0 + pa_x1) / 2.0
            content_cx = (c_left + c_right) / 2.0

            dx = pa_cx - content_cx
            dy = pa_y0 - c_bottom  # wyrównaj dół contentu do dołu printable area

            if abs(dx) > 0.01 or abs(dy) > 0.01:
                for p in pls:
                    p.x_mm += dx
                    p.y_mm += dy
        return sheet

    # === 7. Oblicz bounding-box per sub-arkusz ===
    def _calc_sub_bounds():
        bounds = [[None] * n_cols for _ in range(n_rows)]
        for ri in range(n_rows):
            for ci in range(n_cols):
                pls = grid[ri][ci]
                if not pls:
                    continue
                bl = min(p.x_mm for p in pls)
                br = max(p.x_mm + _pw(p) for p in pls)
                bb = min(p.y_mm for p in pls)
                bt = max(p.y_mm + _ph(p) for p in pls)
                bounds[ri][ci] = (bl, br, bb, bt)
        return bounds

    sub_bounds = _calc_sub_bounds()

    # === 8. Oblicz V-boundaries (midpoint gapów) ===
    v_boundaries = _compute_boundaries_v(sub_bounds, n_rows, n_cols, flexcut_gap_mm)

    # === 9. Centruj placements w sub-arkuszach FlexCut ===
    # Y-centering: per ROW BAND (ten sam dy dla wszystkich kolumn w danym wierszu).
    # Zapobiega rozjeżdżaniu się wierszy gdy komórki mają różną liczbę naklejek
    # (np. na częściowo wypełnionym arkuszu 2).
    # X-centering: per CELL (kolumny niezależne).
    for ri in range(n_rows):
        sub_y_lo = h_boundaries[ri]
        sub_y_hi = h_boundaries[ri + 1]
        sub_cy = (sub_y_lo + sub_y_hi) / 2.0

        # Zbierz WSZYSTKIE placements z tego wiersza (wszystkie kolumny)
        all_row_pls: list[Placement] = []
        for ci in range(n_cols):
            all_row_pls.extend(grid[ri][ci])

        # Oblicz dy z unii wszystkich placements w wierszu
        dy = 0.0
        if all_row_pls:
            row_bottom = min(p.y_mm for p in all_row_pls)
            row_top = max(p.y_mm + _ph(p) for p in all_row_pls)
            row_content_cy = (row_bottom + row_top) / 2.0
            dy = sub_cy - row_content_cy

        for ci in range(n_cols):
            sub_x_lo = v_boundaries[ci]
            sub_x_hi = v_boundaries[ci + 1]
            sub_cx = (sub_x_lo + sub_x_hi) / 2.0

            pls = grid[ri][ci]
            if not pls:
                continue

            # X-centering per cell (niezależne kolumny)
            c_left = min(p.x_mm for p in pls)
            c_right = max(p.x_mm + _pw(p) for p in pls)
            content_cx = (c_left + c_right) / 2.0
            dx = sub_cx - content_cx

            if abs(dx) > 0.01 or abs(dy) > 0.01:
                for p in pls:
                    p.x_mm += dx
                    p.y_mm += dy

    # === 10. Pozycjonuj cały blok FlexCut na arkuszu ===
    # X: centruj na printable area
    # Y: wyrównaj do dołu printable area (tuż nad paserami)
    pa_x0, pa_y0, pa_x1, pa_y1 = sheet.printable_rect_mm
    pa_cx = (pa_x0 + pa_x1) / 2.0

    block_cx = (v_boundaries[0] + v_boundaries[-1]) / 2.0

    shift_x = pa_cx - block_cx
    shift_y = pa_y0 - h_boundaries[0]  # dół bloku = dół printable area

    if abs(shift_x) > 0.01 or abs(shift_y) > 0.01:
        for p in sheet.placements:
            p.x_mm += shift_x
            p.y_mm += shift_y
        v_boundaries = [v + shift_x for v in v_boundaries]
        h_boundaries = [h + shift_y for h in h_boundaries]
        log.info(f"  Blok przesunięty: dx={shift_x:.1f}mm, dy={shift_y:.1f}mm")

    # === 10b. Przelicz boundaries z AKTUALNYCH pozycji (po centrowaniu + shift) ===
    # External boundaries = content_edge ± fc_gap (z aktualnych pozycji).
    # Internal boundaries = midpoint gapu (z aktualnych pozycji).
    # Przy fc_gap=0 external boundary = content edge → content przylega do FlexCut.
    # Edge sub-sheets mają asymetryczny gap (content flush do external, gap na internal)
    # — to jest PRAWIDŁOWE zachowanie.
    sub_bounds_post = _calc_sub_bounds()
    v_boundaries = _compute_boundaries_v(sub_bounds_post, n_rows, n_cols, flexcut_gap_mm)

    row_bounds: list[tuple[float, float]] = []
    for ri in range(n_rows):
        bottoms = []
        tops = []
        for ci in range(n_cols):
            b = sub_bounds_post[ri][ci]
            if b is not None:
                bottoms.append(b[2])
                tops.append(b[3])
        if bottoms:
            row_bounds.append((min(bottoms), max(tops)))
        else:
            row_bounds.append((h_boundaries[ri], h_boundaries[ri + 1]))

    h_boundaries_new: list[float] = []
    h_boundaries_new.append(row_bounds[0][0] - flexcut_gap_mm)
    for ri in range(n_rows - 1):
        top_curr = row_bounds[ri][1]
        bottom_next = row_bounds[ri + 1][0]
        h_boundaries_new.append((top_curr + bottom_next) / 2.0)
    h_boundaries_new.append(row_bounds[-1][1] + flexcut_gap_mm)
    h_boundaries = h_boundaries_new

    log.info(
        f"  Siatka: {n_cols}×{n_rows}, "
        f"V={[f'{v:.1f}' for v in v_boundaries]}, "
        f"H={[f'{h:.1f}' for h in h_boundaries]}"
    )

    # === 11. Generuj FlexCut linie ===
    h_lines = sorted(set(h_boundaries))
    v_lines = sorted(set(v_boundaries))

    rect_left = v_lines[0]
    rect_right = v_lines[-1]
    rect_bottom = h_lines[0]
    rect_top = h_lines[-1]

    for y in h_lines:
        sheet.panel_lines.append(PanelLine(
            axis="horizontal", position_mm=y,
            start_mm=rect_left, end_mm=rect_right,
            bridge_length_mm=1.0))

    for x in v_lines:
        sheet.panel_lines.append(PanelLine(
            axis="vertical", position_mm=x,
            start_mm=rect_bottom, end_mm=rect_top,
            bridge_length_mm=1.0))

    sheet._flexcut_h_lines_mm = h_lines
    sheet._flexcut_v_lines_mm = v_lines
    sheet._flexcut_gap_mm = flexcut_gap_mm

    n_sub = n_rows * n_cols
    log.info(
        f"Panelizacja: {len(sheet.panel_lines)} linii FlexCut, "
        f"siatka {n_cols}×{n_rows} = {n_sub} sub-arkusz(y)"
    )
    return sheet


# =========================================================================
# HELPERS — rozmiary z uwzgl. rotacji
# =========================================================================

def _pw(p: Placement) -> float:
    if abs(p.rotation_deg) in (90.0, 270.0):
        return p.sticker.height_mm
    return p.sticker.width_mm


def _ph(p: Placement) -> float:
    if abs(p.rotation_deg) in (90.0, 270.0):
        return p.sticker.width_mm
    return p.sticker.height_mm


# =========================================================================
# GRID BOUNDARIES
# =========================================================================

def _compute_boundaries_v(
    sub_bounds: list[list[tuple[float, float, float, float] | None]],
    n_rows: int,
    n_cols: int,
    fc_gap: float,
) -> list[float]:
    """Oblicza V-boundaries (pionowe linie FlexCut).

    Zewnętrzne: content_edge ± fc_gap.
    Wewnętrzne: midpoint gapu między sąsiednimi kolumnami.
    """
    col_rights: list[float] = []
    col_lefts: list[float] = []
    for ci in range(n_cols):
        rights = []
        lefts = []
        for ri in range(n_rows):
            b = sub_bounds[ri][ci]
            if b is not None:
                lefts.append(b[0])
                rights.append(b[1])
        col_lefts.append(min(lefts) if lefts else 0)
        col_rights.append(max(rights) if rights else 0)

    boundaries: list[float] = []
    # Lewa krawędź: content - fc_gap
    boundaries.append(col_lefts[0] - fc_gap)
    # Wewnętrzne granice: midpoint
    for ci in range(n_cols - 1):
        mid = (col_rights[ci] + col_lefts[ci + 1]) / 2.0
        boundaries.append(mid)
    # Prawa krawędź: content + fc_gap
    boundaries.append(col_rights[-1] + fc_gap)

    return boundaries


# =========================================================================
# PARSE FLEXCUT COUNT
# =========================================================================

def parse_subsheet_size(value: str | None) -> tuple[float, float] | None:
    """Parsuje rozmiar sub-arkusza FlexCut.

    Akceptuje:
      - "A4", "A3" → preset z SUBSHEET_SIZE_PRESETS
      - "300x200" → custom (w, h) tuple w mm
      - None, "", "0" → None (brak size-based podziału)

    Returns:
        (width, height) w mm lub None.
    """
    if value is None:
        return None

    value = value.strip()
    if not value or value == "0":
        return None

    # Preset
    upper = value.upper()
    if upper in SUBSHEET_SIZE_PRESETS:
        return SUBSHEET_SIZE_PRESETS[upper]

    # Custom WxH
    parts = value.lower().replace("×", "x").split("x")
    if len(parts) == 2:
        try:
            w = float(parts[0])
            h = float(parts[1])
            if w > 0 and h > 0:
                return (w, h)
        except ValueError:
            pass

    raise ValueError(
        f"Niepoprawny rozmiar sub-arkusza: '{value}'. "
        f"Użyj: A4, A3, lub WxH (np. 300x200)."
    )


def parse_flexcut_count(value: str | int | None) -> int | None:
    """Parsuje wartość FlexCut — maks. naklejek na sub-arkusz.

    Returns:
        int > 0 jeśli FlexCut aktywny, None jeśli wyłączony.
        0, puste, None → None (FlexCut off).
    """
    if value is None:
        return None

    if isinstance(value, (int, float)):
        val = int(value)
        return val if val > 0 else None

    if isinstance(value, str):
        value = value.strip()
        if not value or value == "0":
            return None
        try:
            val = int(value)
        except ValueError:
            raise ValueError(
                f"Niepoprawna wartość FlexCut: '{value}'. "
                "Podaj liczbę całkowitą > 0 (maks. naklejek na sub-arkusz) lub 0 = brak FlexCut."
            )
        if val <= 0:
            return None
        return val

    return None
