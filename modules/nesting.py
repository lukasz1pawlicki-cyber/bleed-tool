"""
Bleed Tool — nesting.py
=========================
Rozmieszczanie naklejek na arkuszach (bin packing).

Algorytm: Group-Ordered Shelf z opcjonalnym cross-group backfill
  1. Naklejki sa grupowane wg wzoru (source_path). Grupy sa sortowane
     malejaco wg rozmiaru najwyzszej naklejki w grupie.
  2. Grupy sa przetwarzane po kolei. W ramach grupy — malejaco wg rozmiaru.
  3. Tryb "group": kazdy wzor trzymany razem na arkuszu (bez mieszania).
     Jesli grupa nie miesci sie — cala przenoszona na nowy arkusz.
  4. Tryb "mix": cross-group backfill po kazdej grupie — wolne miejsce
     w shelfach wypelniane naklejkami z nastepnych grup.
     Konsolidacja na koncu.
  5. Tryb "separate": kazdy wzor na osobnym arkuszu.

Wiersze (shelves) to czyste poziome pasma — kluczowe dla FlexCut.
Kazdy wiersz ma wysokosc najwyzszej naklejki w nim umieszczonej.

Obsluguje:
  - Wiele kopii tej samej naklejki (Job.stickers = [(sticker, count)])
  - Bounding box nesting (prostokatny)
  - Two-pass rotation: najpierw 0°, potem 90° w wolne miejsca
  - Multi-sheet overflow
  - Roll mode (height=None -> zmienna dlugosc)
  - Grupowanie wg wzoru z cross-group backfill

Naprawione wzgledem sticker-toolkit:
  - _rebuild_shelves() uwzglednia bleed (parametr bleed2)
  - _finalize_sheet() i konsolidacja: roll height z bleed2
  - Usuniete dead code (nieosiagalny elif, cross-group w group mode)
  - Spojne sortowanie w trybie mix (max_dim first)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from models import Sticker, Placement, Sheet, Job
from config import (
    SHEET_SIZES,
    DEFAULT_GAP_MM,
    DEFAULT_MARGINS_MM,
    DEFAULT_MARK_ZONE_MM,
    FLOAT_TOLERANCE_MM,
)

log = logging.getLogger(__name__)

# Tolerancja dopasowania shelf — kompensuje kumulację błędów pt→mm.
# 5 naklejek × 0.03mm błędu = 0.15mm. 0.5mm pokrywa z zapasem.
FIT_TOLERANCE_MM = 0.5


# =============================================================================
# SHELF (ROW) DATA STRUCTURE
# =============================================================================

@dataclass
class _Shelf:
    """Pojedynczy wiersz (shelf) — poziomy pas na arkuszu."""
    y: float            # pozycja dolnej krawedzi (relative to printable area origin)
    height: float       # wysokosc wiersza (= najwyzsza naklejka + gap)
    cursor_x: float = 0.0   # nastepna wolna pozycja X
    area_w: float = 0.0     # szerokosc printable area

    def remaining_width(self) -> float:
        return self.area_w - self.cursor_x


# =============================================================================
# NESTING ITEM
# =============================================================================

@dataclass
class _NestingItem:
    """Naklejka do umieszczenia z informacja o rozmiarze i rotacji."""
    sticker: Sticker
    width_mm: float     # Po uwzglednieniu rotacji (zawiera bleed)
    height_mm: float
    rotation_deg: float


# =============================================================================
# FIT HELPERS
# =============================================================================

def _best_fit_for_shelf(
    sticker: Sticker,
    remaining_w: float,
    shelf_height: float,
    gap_mm: float,
    allow_rotation: bool = True,
    bleed2: float = 0.0,
) -> _NestingItem | None:
    """Najlepsza orientacja (0°/90°) dla istniejacego wiersza.

    Preferuje orientacje ktora lepiej pasuje do shelf_height (mniej straty).
    Jesli allow_rotation=False, probuje tylko 0°.
    bleed2 = 2 * bleed_mm — doliczany do wymiarow naklejki.
    """
    w, h = sticker.width_mm + bleed2, sticker.height_mm + bleed2
    candidates: list[_NestingItem] = []

    if w <= remaining_w + FIT_TOLERANCE_MM:
        candidates.append(_NestingItem(sticker, w, h, 0.0))
    if allow_rotation and abs(w - h) > 0.1 and h <= remaining_w + FIT_TOLERANCE_MM:
        candidates.append(_NestingItem(sticker, h, w, 90.0))

    if not candidates:
        return None
    return min(candidates, key=lambda c: abs(shelf_height - (c.height_mm + gap_mm)))


def _best_fit_for_new_shelf(
    sticker: Sticker,
    area_w: float,
    allow_rotation: bool = True,
    bleed2: float = 0.0,
) -> _NestingItem | None:
    """Najlepsza orientacja (0°/90°) dla nowego wiersza.

    Preferuje orientacje z mniejsza wysokoscia.
    Jesli allow_rotation=False, probuje tylko 0°.
    bleed2 = 2 * bleed_mm — doliczany do wymiarow naklejki.
    """
    w, h = sticker.width_mm + bleed2, sticker.height_mm + bleed2
    candidates: list[_NestingItem] = []

    if w <= area_w + FIT_TOLERANCE_MM:
        candidates.append(_NestingItem(sticker, w, h, 0.0))
    if allow_rotation and abs(w - h) > 0.1 and h <= area_w + FIT_TOLERANCE_MM:
        candidates.append(_NestingItem(sticker, h, w, 90.0))

    if not candidates:
        return None
    return min(candidates, key=lambda c: c.height_mm)


# =============================================================================
# BACKFILL INTO SPECIFIC SHELF LIST
# =============================================================================

def _find_backfill_item(
    sticker: Sticker,
    shelves: list[_Shelf],
    gap_mm: float,
    allow_rotation: bool = True,
    bleed2: float = 0.0,
) -> tuple[int, _NestingItem] | None:
    """Szuka najlepszego shelfa do backfill (best-fit wg wysokosci).

    Nie rozszerza shelfow — naklejka musi miescic sie w istniejacym.
    Jesli allow_rotation=False, probuje tylko 0°.
    bleed2 = 2 * bleed_mm — doliczany do wymiarow naklejki.
    """
    best_idx: int | None = None
    best_item: _NestingItem | None = None
    best_waste: float = float("inf")

    w, h = sticker.width_mm + bleed2, sticker.height_mm + bleed2

    for idx, shelf in enumerate(shelves):
        remaining = shelf.remaining_width()
        candidates: list[_NestingItem] = []

        if w <= remaining + FIT_TOLERANCE_MM and h + gap_mm <= shelf.height + FIT_TOLERANCE_MM:
            candidates.append(_NestingItem(sticker, w, h, 0.0))
        if allow_rotation and abs(w - h) > 0.1 and h <= remaining + FIT_TOLERANCE_MM and w + gap_mm <= shelf.height + FIT_TOLERANCE_MM:
            candidates.append(_NestingItem(sticker, h, w, 90.0))

        for c in candidates:
            waste = shelf.height - (c.height_mm + gap_mm)
            if waste < best_waste:
                best_waste = waste
                best_idx = idx
                best_item = c

    if best_idx is not None and best_item is not None:
        return (best_idx, best_item)
    return None


# =============================================================================
# NESTING ENGINE
# =============================================================================

def nest_job(
    job: Job,
    sheet_width_mm: float | None = None,
    sheet_height_mm: float | None = None,
    gap_mm: float = DEFAULT_GAP_MM,
    margins_mm: tuple = DEFAULT_MARGINS_MM,
    mark_zone_mm: float = DEFAULT_MARK_ZONE_MM,
    max_sheet_length_mm: float | None = None,
    grouping_mode: str = "group",
    bleed_mm: float = 0.0,
    _center_mode: str = "xy",
) -> Job:
    """Rozmieszcza naklejki na arkuszach algorytmem shelf (row-by-row).

    Args:
        job: Job z lista stickers [(Sticker, count)]
        sheet_width_mm: szerokosc arkusza (None -> z config SRA3)
        sheet_height_mm: wysokosc arkusza (None -> roll mode)
        gap_mm: odstep miedzy naklejkami
        margins_mm: marginesy (top, right, bottom, left)
        mark_zone_mm: strefa na znaczniki rejestracji
        max_sheet_length_mm: max dlugosc arkusza w roll mode
        grouping_mode: "group" = grupuj wg wzoru (bez mieszania miedzy arkuszami),
                       "separate" = kazdy wzor na osobnym arkuszu,
                       "mix" = mieszaj wszystkie wzory (cross-group backfill)
        bleed_mm: wielkosc bleed w mm (naklejki powiekszone o 2*bleed)

    Returns:
        Job z wypelniona lista sheets (z placements).
    """
    if sheet_width_mm is None:
        sheet_width_mm = SHEET_SIZES["SRA3"][0]

    # Roll mode
    is_roll = sheet_height_mm is None or sheet_height_mm <= 0
    if is_roll:
        sheet_height_mm = max_sheet_length_mm if max_sheet_length_mm else 0
    # FIX: usunieto nieosiagalny elif (sheet_height_mm is None -> is_roll=True)

    # Printable area
    top, right, bottom, left = margins_mm
    area_w = sheet_width_mm - left - right - 2 * mark_zone_mm
    area_h: float | None
    if is_roll and not max_sheet_length_mm:
        area_h = None
    else:
        area_h = sheet_height_mm - top - bottom - 2 * mark_zone_mm

    if area_w <= 0:
        raise ValueError(f"Printable area width <= 0 (sheet_width={sheet_width_mm}, margins={margins_mm})")
    if bleed_mm < 0:
        raise ValueError(f"bleed_mm musi byc >= 0, podano {bleed_mm}")
    if gap_mm < 0:
        raise ValueError(f"gap_mm musi byc >= 0, podano {gap_mm}")

    # Bleed: kazda naklejka jest wieksza o 2*bleed w obu kierunkach
    bleed2 = 2 * bleed_mm

    log.info(
        f"Nesting: arkusz {sheet_width_mm}\u00d7{sheet_height_mm}mm, "
        f"printable {area_w:.1f}\u00d7{area_h if area_h else '\u221e'}mm, "
        f"gap={gap_mm}mm, bleed={bleed_mm}mm"
    )

    # -------------------------------------------------------------------
    # Rozwin stickers x count
    # -------------------------------------------------------------------
    sticker_list: list[Sticker] = []
    for sticker, count in job.stickers:
        for _ in range(count):
            sticker_list.append(sticker)

    if not sticker_list:
        log.warning("Brak naklejek do rozmieszczenia")
        return job

    # -------------------------------------------------------------------
    # Grupowanie wg wzoru + sortowanie
    # -------------------------------------------------------------------
    # Odfiltruj niemieszczace sie
    valid: list[Sticker] = []
    for sticker in sticker_list:
        min_dim = min(sticker.width_mm + bleed2, sticker.height_mm + bleed2)
        if min_dim <= area_w + FIT_TOLERANCE_MM:
            valid.append(sticker)
        else:
            log.warning(
                f"Naklejka {sticker.source_path} "
                f"({sticker.width_mm:.1f}\u00d7{sticker.height_mm:.1f}mm) "
                f"nie miesci sie — pomijam"
            )

    if not valid:
        log.warning("Zadna naklejka nie miesci sie na arkuszu")
        return job

    # -------------------------------------------------------------------
    # Smart pre-rotation: jeśli obrót 90° zmieści więcej na jednym arkuszu
    # -------------------------------------------------------------------
    _pre_rotate_90 = False
    # Zaokrąglenie do 1mm — kompensuje drobne różnice pt→mm (170.01 vs 169.97)
    unique_sizes = set((round(s.width_mm + bleed2, 0), round(s.height_mm + bleed2, 0)) for s in valid)
    if len(unique_sizes) == 1:
        # Jeden rozmiar — sprawdź czy obrót 90° daje lepsze wypełnienie
        fw, fh = next(iter(unique_sizes))
        total_count = len(valid)
        def _grid_count(w, h):
            """Ile naklejek (w×h) zmieści się na arkuszu w siatce."""
            g = gap_mm
            # Bez tolerancji — czysta estymacja pojemności siatki.
            # (FLOAT_TOLERANCE_MM służy do fit-check przy placement, nie do estymacji)
            nx = int(math.floor((area_w + g) / (w + g))) if w > 0 else 0
            ny = int(math.floor(((area_h or 99999) + g) / (h + g))) if h > 0 else 0
            return nx * ny
        n_normal = _grid_count(fw, fh)
        n_rotated = _grid_count(fh, fw) if abs(fw - fh) > 0.1 else n_normal
        _pre_rotate_90 = False
        if n_rotated > n_normal:
            # Obrót 90° daje więcej naklejek na arkuszu → obracamy
            # Swap width_mm/height_mm (nesting), ale NIE page_width_pt/page_height_pt (export)
            # Placement dostanie rotation_deg=90 → export użyje show_pdf_page(rotate=90)
            log.info(f"Pre-rotation 90°: {n_normal} → {n_rotated} na arkuszu "
                     f"({fw:.0f}×{fh:.0f}mm → {fh:.0f}×{fw:.0f}mm)")
            _pre_rotate_90 = True
            swapped_ids: set[int] = set()
            for s in valid:
                sid = id(s)
                if sid not in swapped_ids:
                    s.width_mm, s.height_mm = s.height_mm, s.width_mm
                    # NIE swapuj page_width_pt / page_height_pt!
                    swapped_ids.add(sid)

    if grouping_mode in ("group", "separate"):
        # Zbuduj grupy wg source_path
        source_order: list[str] = []
        groups: dict[str, list[Sticker]] = {}
        for s in valid:
            sp = s.source_path
            if sp not in groups:
                source_order.append(sp)
                groups[sp] = []
            groups[sp].append(s)

        # Sortuj grupy malejaco wg max wymiaru
        def _group_max_dim(sp: str) -> float:
            return max(max(s.height_mm, s.width_mm) for s in groups[sp])

        source_order.sort(key=lambda sp: -_group_max_dim(sp))

        # W ramach grupy: malejaco wg rozmiaru
        for sp in source_order:
            groups[sp].sort(key=lambda s: (
                -max(s.height_mm, s.width_mm),
                -min(s.height_mm, s.width_mm),
            ))

        ordered_groups: list[list[Sticker]] = [groups[sp] for sp in source_order]
    else:
        # FIX: "mix" — spojne sortowanie (-max_dim, -min_dim) jak w group mode
        valid.sort(key=lambda s: (
            -max(s.height_mm, s.width_mm),
            -min(s.height_mm, s.width_mm),
        ))
        ordered_groups = [valid]

    total_stickers = sum(len(g) for g in ordered_groups)
    log.info(f"Do rozmieszczenia: {total_stickers} naklejek w {len(ordered_groups)} grupach")

    # -------------------------------------------------------------------
    # Shelf-based nesting z cross-group backfill
    # -------------------------------------------------------------------
    job.sheets = []
    origin_x = left + mark_zone_mm
    origin_y = bottom + mark_zone_mm

    # Sledzenie wolnych naklejek per grupa — zbior indeksow juz umieszczonych
    placed_flags: list[list[bool]] = [
        [False] * len(g) for g in ordered_groups
    ]

    # Stan biezacego arkusza
    sheet: Sheet | None = None
    shelves: list[_Shelf] = []
    current_shelf_top: float = 0.0

    def _new_sheet() -> Sheet:
        """Tworzy nowy arkusz i resetuje shelves."""
        nonlocal shelves, current_shelf_top
        s = Sheet(
            width_mm=sheet_width_mm,
            height_mm=sheet_height_mm if not is_roll else 0,
            margins_mm=margins_mm,
            mark_zone_mm=mark_zone_mm,
            gap_mm=gap_mm,
        )
        shelves = []
        current_shelf_top = 0.0
        return s

    def _finalize_sheet(s: Sheet):
        """Zamyka arkusz i dodaje do job.sheets."""
        if not s.placements:
            return
        if is_roll:
            # FIX: uwzgledniaj bleed2 w obliczaniu wysokosci arkusza
            max_y = max(
                p.y_mm + (
                    p.sticker.height_mm + bleed2 if p.rotation_deg == 0
                    else p.sticker.width_mm + bleed2
                )
                for p in s.placements
            )
            s.height_mm = max_y + top + mark_zone_mm + gap_mm

        job.sheets.append(s)
        log.info(
            f"Arkusz {len(job.sheets)}: {len(s.placements)} naklejek "
            f"({s.width_mm:.1f}\u00d7{s.height_mm:.1f}mm)"
        )

    def _place_in_current_shelf(item: _NestingItem) -> bool:
        """Umieszcza naklejke w biezacym (ostatnim) wierszu."""
        nonlocal current_shelf_top
        if not shelves:
            return False
        cur_shelf = shelves[-1]
        item_h_gap = item.height_mm + gap_mm

        # Rozszerz shelf jesli item jest wyzszy
        new_shelf_height = max(cur_shelf.height, item_h_gap)
        if new_shelf_height > cur_shelf.height + FLOAT_TOLERANCE_MM:
            height_increase = new_shelf_height - cur_shelf.height
            new_total_top = current_shelf_top + height_increase
            if area_h is not None and new_total_top > area_h + FLOAT_TOLERANCE_MM:
                return False
            cur_shelf.height = new_shelf_height
            current_shelf_top = new_total_top

        placement = Placement(
            sticker=item.sticker,
            x_mm=origin_x + cur_shelf.cursor_x,
            y_mm=origin_y + cur_shelf.y,
            rotation_deg=item.rotation_deg,
        )
        sheet.placements.append(placement)
        cur_shelf.cursor_x = round(cur_shelf.cursor_x + item.width_mm + gap_mm, 2)
        return True

    def _place_in_new_shelf(item: _NestingItem) -> bool:
        """Tworzy nowy wiersz i umieszcza naklejke."""
        nonlocal current_shelf_top
        item_h_gap = item.height_mm + gap_mm
        new_shelf_y = current_shelf_top
        new_shelf_top = new_shelf_y + item_h_gap

        if area_h is not None and new_shelf_top > area_h + FLOAT_TOLERANCE_MM:
            return False

        new_shelf = _Shelf(
            y=new_shelf_y,
            height=item_h_gap,
            cursor_x=0.0,
            area_w=area_w,
        )
        shelves.append(new_shelf)
        current_shelf_top = new_shelf_top

        placement = Placement(
            sticker=item.sticker,
            x_mm=origin_x,
            y_mm=origin_y + new_shelf.y,
            rotation_deg=item.rotation_deg,
        )
        sheet.placements.append(placement)
        new_shelf.cursor_x = item.width_mm + gap_mm
        return True

    def _backfill_into_shelves(sticker: Sticker, allow_rotation: bool = True) -> bool:
        """Backfill: umieszcza naklejke w dowolnym wierszu biezacego arkusza.

        Nie rozszerza wiersza — naklejka musi miescic sie w istniejacym.
        Szukamy shelfa z NAJMNIEJSZA strata wysokosci (best-fit).
        Jesli allow_rotation=False, probuje tylko 0°.
        Wymiary naklejki uwzgledniaja bleed (bleed2).
        """
        best_shelf_idx: int | None = None
        best_item: _NestingItem | None = None
        best_waste: float = float("inf")

        for idx, shelf_candidate in enumerate(shelves):
            remaining = shelf_candidate.remaining_width()
            w, h = sticker.width_mm + bleed2, sticker.height_mm + bleed2
            candidates: list[_NestingItem] = []

            if w <= remaining + FIT_TOLERANCE_MM and h + gap_mm <= shelf_candidate.height + FIT_TOLERANCE_MM:
                candidates.append(_NestingItem(sticker, w, h, 0.0))
            if allow_rotation and abs(w - h) > 0.1 and h <= remaining + FIT_TOLERANCE_MM and w + gap_mm <= shelf_candidate.height + FIT_TOLERANCE_MM:
                candidates.append(_NestingItem(sticker, h, w, 90.0))

            for c in candidates:
                waste = shelf_candidate.height - (c.height_mm + gap_mm)
                if waste < best_waste:
                    best_waste = waste
                    best_shelf_idx = idx
                    best_item = c

        if best_shelf_idx is None or best_item is None:
            return False

        bf_shelf = shelves[best_shelf_idx]
        placement = Placement(
            sticker=best_item.sticker,
            x_mm=origin_x + bf_shelf.cursor_x,
            y_mm=origin_y + bf_shelf.y,
            rotation_deg=best_item.rotation_deg,
        )
        sheet.placements.append(placement)
        bf_shelf.cursor_x += best_item.width_mm + gap_mm
        return True

    def _cross_group_backfill(current_group_idx: int, allow_rotation: bool = True):
        """Eagerly backfill: po zakonczeniu grupy, wypelnij wolne miejsca
        w shelfach naklejkami z NASTEPNYCH grup.

        Iterujemy po nastepnych grupach (grupa po grupie) i dla kazdej
        nieumieszczonej naklejki sprawdzamy czy zmiesci sie w jakims shelfie.
        NIE tworzymy nowych shelfow — tylko backfill do istniejacych.
        Zatrzymujemy sie kiedy zaden shelf nie ma juz miejsca.
        """
        any_placed = True
        while any_placed:
            any_placed = False
            for g_idx in range(current_group_idx + 1, len(ordered_groups)):
                group = ordered_groups[g_idx]
                for s_idx, sticker in enumerate(group):
                    if placed_flags[g_idx][s_idx]:
                        continue

                    if _backfill_into_shelves(sticker, allow_rotation=allow_rotation):
                        placed_flags[g_idx][s_idx] = True
                        any_placed = True

    # --- Glowna petla: grupa po grupie ---
    # Strategia dwuprzebiegowa:
    #   PASS 1: umieszczaj tylko w 0° (bez rotacji)
    #   PASS 2: dopelnij puste miejsca uzytkami obroconymi o 90°
    sheet = _new_sheet()
    is_separate = (grouping_mode == "separate")

    # ===== PASS 1: tylko 0° =====
    is_group_mode = (grouping_mode == "group")

    for g_idx, group in enumerate(ordered_groups):
        # Tryb "separate": kazda grupa zaczyna na nowym arkuszu
        if is_separate and g_idx > 0:
            _finalize_sheet(sheet)
            sheet = _new_sheet()

        # Zapamietaj stan arkusza PRZED ta grupa (do rollback w trybie group)
        if is_group_mode:
            _snapshot_placements_len = len(sheet.placements)
            _snapshot_shelves_len = len(shelves)
            _snapshot_shelf_top = current_shelf_top
            # Snapshot cursor_x i height kazdego shelfa
            _snapshot_shelf_state = [
                (s.cursor_x, s.height) for s in shelves
            ]

        group_needs_new_sheet = False

        for s_idx, sticker in enumerate(group):
            if placed_flags[g_idx][s_idx]:
                continue  # Juz umieszczona przez cross-group backfill

            placed = False

            # 1. Sprobuj w biezacym (ostatnim) wierszu — tylko 0°
            # W trybie "group": tylko jeśli ostatni shelf należy do bieżącej grupy
            can_use_current = True
            if is_group_mode and len(shelves) <= _snapshot_shelves_len:
                can_use_current = False  # Brak wierszy tej grupy — nie wstawiaj do cudzego

            if can_use_current and shelves:
                item = _best_fit_for_shelf(
                    sticker, shelves[-1].remaining_width(), shelves[-1].height, gap_mm,
                    allow_rotation=False, bleed2=bleed2,
                )
                if item is not None:
                    placed = _place_in_current_shelf(item)

            # 2. Nowy wiersz na biezacym arkuszu — tylko 0°
            if not placed:
                item = _best_fit_for_new_shelf(sticker, area_w, allow_rotation=False, bleed2=bleed2)
                if item is not None:
                    placed = _place_in_new_shelf(item)

            # 3. Backfill — sprobuj w wierszu — tylko 0°
            # W trybie "group": szukaj TYLKO w wierszach bieżącej grupy
            # (od _snapshot_shelves_len), żeby nie mieszać wzorów.
            if not placed:
                if is_group_mode:
                    # Backfill ograniczony do wierszy bieżącej grupy
                    group_shelves = shelves[_snapshot_shelves_len:]
                    if group_shelves:
                        result = _find_backfill_item(sticker, group_shelves, gap_mm,
                                                     allow_rotation=False, bleed2=bleed2)
                        if result is not None:
                            shelf_local_idx, item = result
                            bf_shelf = group_shelves[shelf_local_idx]
                            placement = Placement(
                                sticker=item.sticker,
                                x_mm=origin_x + bf_shelf.cursor_x,
                                y_mm=origin_y + bf_shelf.y,
                                rotation_deg=item.rotation_deg,
                            )
                            sheet.placements.append(placement)
                            bf_shelf.cursor_x += item.width_mm + gap_mm
                            placed = True
                else:
                    placed = _backfill_into_shelves(sticker, allow_rotation=False)

            # 4. Nowy arkusz
            if not placed:
                if is_group_mode:
                    # Tryb grupuj: cala grupa musi byc razem.
                    # Cofnij naklejki tej grupy z biezacego arkusza
                    # i przenies cala grupe na nowy arkusz.
                    group_needs_new_sheet = True
                    break
                else:
                    # Tryb mix/separate: normalny overflow
                    if grouping_mode == "mix":
                        _cross_group_backfill(g_idx, allow_rotation=False)
                    _finalize_sheet(sheet)
                    sheet = _new_sheet()

                    item = _best_fit_for_new_shelf(sticker, area_w, allow_rotation=False, bleed2=bleed2)
                    if item is not None:
                        placed = _place_in_new_shelf(item)

            if placed:
                placed_flags[g_idx][s_idx] = True

        if group_needs_new_sheet and is_group_mode:
            # Cofnij wszystkie naklejki tej grupy z biezacego arkusza
            for s_idx2 in range(len(group)):
                placed_flags[g_idx][s_idx2] = False

            # Przywroc stan arkusza sprzed tej grupy
            sheet.placements = sheet.placements[:_snapshot_placements_len]
            while len(shelves) > _snapshot_shelves_len:
                shelves.pop()
            for i, (cx, ch) in enumerate(_snapshot_shelf_state):
                shelves[i].cursor_x = cx
                shelves[i].height = ch
            current_shelf_top = _snapshot_shelf_top

            # FIX: usunieto dead code — cross-group backfill w trybie group
            # (grouping_mode == "mix" zawsze False tutaj)
            _finalize_sheet(sheet)
            sheet = _new_sheet()

            # Umiesz cala grupe na nowym arkuszu
            for s_idx, sticker in enumerate(group):
                if placed_flags[g_idx][s_idx]:
                    continue

                placed = False

                if shelves:
                    item = _best_fit_for_shelf(
                        sticker, shelves[-1].remaining_width(), shelves[-1].height, gap_mm,
                        allow_rotation=False, bleed2=bleed2,
                    )
                    if item is not None:
                        placed = _place_in_current_shelf(item)

                if not placed:
                    item = _best_fit_for_new_shelf(sticker, area_w, allow_rotation=False, bleed2=bleed2)
                    if item is not None:
                        placed = _place_in_new_shelf(item)

                if not placed:
                    placed = _backfill_into_shelves(sticker, allow_rotation=False)

                if not placed:
                    # Nawet na pustym arkuszu nie miesci sie — nowy arkusz
                    _finalize_sheet(sheet)
                    sheet = _new_sheet()
                    item = _best_fit_for_new_shelf(sticker, area_w, allow_rotation=False, bleed2=bleed2)
                    if item is not None:
                        placed = _place_in_new_shelf(item)

                if placed:
                    placed_flags[g_idx][s_idx] = True

        # Po zakonczeniu grupy: cross-group backfill (tylko w trybie "mix", tylko 0°)
        if grouping_mode == "mix":
            _cross_group_backfill(g_idx, allow_rotation=False)

    # Zamknij ostatni arkusz po PASS 1
    _finalize_sheet(sheet)

    # ===== PASS 2: dopelnij puste miejsca uzytkami obroconymi o 90° =====
    # Zbierz nieumieszczone naklejki
    unplaced: list[tuple[int, int, Sticker]] = []
    for g_idx, group in enumerate(ordered_groups):
        for s_idx, sticker in enumerate(group):
            if not placed_flags[g_idx][s_idx]:
                unplaced.append((g_idx, s_idx, sticker))

    if unplaced:
        log.info(f"Pass 2 (rotacja 90\u00b0): {len(unplaced)} nieumieszczonych naklejek")

        # Odbuduj shelves dla kazdego istniejacego arkusza i probuj backfill z rotacja
        for sheet_idx, existing_sheet in enumerate(job.sheets):
            if not unplaced:
                break

            # FIX: odbuduj shelves z uwzglednieniem bleed2
            shelves = _rebuild_shelves(existing_sheet, area_w, gap_mm, origin_x, origin_y, bleed2)
            sheet = existing_sheet  # _backfill_into_shelves uzywa nonlocal sheet

            still_unplaced: list[tuple[int, int, Sticker]] = []
            for g_idx, s_idx, sticker in unplaced:
                # Probuj backfill z rotacja 90° (dopelnienie pustych miejsc)
                if _backfill_into_shelves(sticker, allow_rotation=True):
                    placed_flags[g_idx][s_idx] = True
                else:
                    still_unplaced.append((g_idx, s_idx, sticker))

            unplaced = still_unplaced

        # Jesli nadal zostaly nieumieszczone — nowe arkusze z rotacja dozwolona
        if unplaced:
            sheet = _new_sheet()
            for g_idx, s_idx, sticker in unplaced:
                placed = False

                # Sprobuj w biezacym wierszu z rotacja
                if shelves:
                    item = _best_fit_for_shelf(
                        sticker, shelves[-1].remaining_width(), shelves[-1].height, gap_mm,
                        allow_rotation=False, bleed2=bleed2,
                    )
                    if item is not None:
                        placed = _place_in_current_shelf(item)

                # Nowy wiersz z rotacja
                if not placed:
                    item = _best_fit_for_new_shelf(sticker, area_w, allow_rotation=False, bleed2=bleed2)
                    if item is not None:
                        placed = _place_in_new_shelf(item)

                # Backfill z rotacja
                if not placed:
                    placed = _backfill_into_shelves(sticker, allow_rotation=False)

                # Nowy arkusz z rotacja
                if not placed:
                    _finalize_sheet(sheet)
                    sheet = _new_sheet()
                    item = _best_fit_for_new_shelf(sticker, area_w, allow_rotation=False, bleed2=bleed2)
                    if item is not None:
                        placed = _place_in_new_shelf(item)

                if placed:
                    placed_flags[g_idx][s_idx] = True
                else:
                    log.error(
                        f"Nie udalo sie umiescic naklejki "
                        f"{sticker.source_path} ({sticker.width_mm:.1f}\u00d7{sticker.height_mm:.1f}mm)"
                    )

            _finalize_sheet(sheet)

    # -------------------------------------------------------------------
    # Konsolidacja: przenies naklejki z ostatniego arkusza do wczesniejszych
    # (nie dla trybu "separate" — tam kazdy wzor ma swoj arkusz)
    # -------------------------------------------------------------------
    # Tryb "mix": pelna konsolidacja (dowolny wzor -> dowolny arkusz).
    # Tryb "group": konsolidacja z ograniczeniem do tego samego source_path —
    #   naklejki z ostatniego arkusza wracaja tylko na arkusze ktore juz
    #   zawieraja ten sam wzor. Nie mieszamy grup, ale wykorzystujemy wolne
    #   miejsce po rotacji 90° (np. 35 kopii 170×40mm: 3. arkusz z niepelna
    #   grupa moze wciagnac rotacje do 1./2. arkusza tego samego wzoru).
    if len(job.sheets) >= 2 and grouping_mode in ("mix", "group"):
        same_source_only = (grouping_mode == "group")
        _consolidate_last_sheet(
            job, area_w, area_h, gap_mm, origin_x, origin_y,
            is_roll, top, mark_zone_mm, bleed2,
            same_source_only=same_source_only,
        )

    # -------------------------------------------------------------------
    # Centruj zawartosc na kazdym arkuszu
    # -------------------------------------------------------------------
    if _center_mode == "xy":
        for s in job.sheets:
            _center_placements(s, center_y=True, bleed2=bleed2)
    elif _center_mode == "x":
        for s in job.sheets:
            _center_placements(s, center_y=False, bleed2=bleed2)
    # _center_mode == "none" → bez centrowania

    # Post-processing: pre-rotation → dodaj 90° do placements + przywróć wymiary stickera
    if _pre_rotate_90:
        swapped_back: set[int] = set()
        for s in job.sheets:
            for p in s.placements:
                p.rotation_deg = (p.rotation_deg + 90) % 360
                sid = id(p.sticker)
                if sid not in swapped_back:
                    # Przywróć oryginalne wymiary stickera (swap back)
                    p.sticker.width_mm, p.sticker.height_mm = p.sticker.height_mm, p.sticker.width_mm
                    swapped_back.add(sid)

    log.info(
        f"Nesting zakonczony: {sum(len(s.placements) for s in job.sheets)} naklejek "
        f"na {len(job.sheets)} arkusz(ach)"
    )
    return job


# =============================================================================
# CONSOLIDATION
# =============================================================================

def _consolidate_last_sheet(
    job: Job,
    area_w: float,
    area_h: float | None,
    gap_mm: float,
    origin_x: float,
    origin_y: float,
    is_roll: bool,
    top: float,
    mark_zone_mm: float,
    bleed2: float = 0.0,
    same_source_only: bool = False,
):
    """Przenosi naklejki z ostatniego arkusza do wczesniejszych.

    Jesli ostatni arkusz jest slabo zapelniony (< 60% powierzchni),
    probuje przeniesc jego naklejki do wczesniejszych arkuszy
    metoda backfill + tworzenie nowych shelfow.

    Kluczowe: shelfy sa budowane RAZ per arkusz i mutowane w trakcie
    przenoszenia (aktualizacja cursor_x), wiec kolejne naklejki widza
    aktualny stan.

    same_source_only: gdy True (tryb "group"), naklejka jest przenoszona
    TYLKO na arkusze ktore juz zawieraja ten sam source_path — chroni
    grupowanie wzorow przed wymieszaniem.
    """
    last_sheet = job.sheets[-1]
    prev_sheets = job.sheets[:-1]

    if not last_sheet.placements:
        job.sheets.pop()
        return

    # Oblicz zapelnienie ostatniego arkusza
    if area_h is None or area_h <= 0:
        return  # Roll mode — nie konsoliduj

    total_area = area_w * area_h
    used_area = sum(
        (p.sticker.width_mm + bleed2) * (p.sticker.height_mm + bleed2)
        for p in last_sheet.placements
    )
    fill_ratio = used_area / total_area if total_area > 0 else 1.0

    if fill_ratio > 0.60:
        return  # Arkusz wystarczajaco pelny

    log.info(
        f"Konsolidacja: ostatni arkusz ({len(last_sheet.placements)} nak, "
        f"{fill_ratio:.0%} zapelnienia) — probuje przeniesc do wczesniejszych"
        + (" [same-source]" if same_source_only else "")
    )

    # Indeks source_path na arkusz (tryb group) — naklejke przenosimy
    # tylko na arkusz ktory juz ma ten wzor.
    prev_sheet_sources: list[set[str]] = [
        {p.sticker.source_path for p in ps.placements}
        for ps in prev_sheets
    ]

    # FIX: odbuduj shelvy z uwzglednieniem bleed2
    prev_shelves_map: list[list[_Shelf]] = [
        _rebuild_shelves(ps, area_w, gap_mm, origin_x, origin_y, bleed2)
        for ps in prev_sheets
    ]

    moved_count = 0
    remaining_placements: list[Placement] = []

    # Sortuj naklejki z ostatniego arkusza: duze najpierw (latwiej znalezc miejsce)
    sorted_last = sorted(
        last_sheet.placements,
        key=lambda p: -(p.sticker.width_mm * p.sticker.height_mm),
    )

    # Zlozonosc petli: O(P * S * K) gdzie P = naklejki ostatniego arkusza,
    # S = poprzednie arkusze, K = shelfy per arkusz. W praktyce P jest male
    # (arkusz < 60% zapelnienia), S i K tez niewielkie. Budowanie indeksu
    # (np. bisect na remaining_width) nie oplaca sie, bo _find_backfill_item
    # szuka best-fit wg *height waste* wsrod shelfow spelniajacych oba warunki
    # (szerokosc + wysokosc), a po kazdym umieszczeniu mutuje cursor_x jednego
    # shelfa — indeks wymagalby re-insert po kazdej zmianie.
    for placement in sorted_last:
        moved = False
        sticker = placement.sticker

        for sheet_idx, prev_sheet in enumerate(prev_sheets):
            # Tryb group: przenosimy tylko na arkusz ktory juz ma ten wzor.
            if same_source_only and sticker.source_path not in prev_sheet_sources[sheet_idx]:
                continue
            prev_shelves = prev_shelves_map[sheet_idx]

            # 1. Backfill w istniejacych shelfach
            result = _find_backfill_item(sticker, prev_shelves, gap_mm, bleed2=bleed2)
            if result is not None:
                shelf_idx_found, item = result
                shelf = prev_shelves[shelf_idx_found]
                new_placement = Placement(
                    sticker=sticker,
                    x_mm=origin_x + shelf.cursor_x,
                    y_mm=origin_y + shelf.y,
                    rotation_deg=item.rotation_deg,
                )
                prev_sheet.placements.append(new_placement)
                shelf.cursor_x += item.width_mm + gap_mm
                moved = True
                moved_count += 1
                break

            # 2. Nowy shelf
            shelves_top = _shelves_top(prev_shelves)
            remaining_h = (area_h or 0) - shelves_top
            if remaining_h > 0:
                new_item = _best_fit_for_new_shelf(sticker, area_w, bleed2=bleed2)
                if new_item is not None and new_item.height_mm + gap_mm <= remaining_h + FIT_TOLERANCE_MM:
                    new_shelf_y = shelves_top
                    new_placement = Placement(
                        sticker=sticker,
                        x_mm=origin_x,
                        y_mm=origin_y + new_shelf_y,
                        rotation_deg=new_item.rotation_deg,
                    )
                    prev_sheet.placements.append(new_placement)
                    # Dodaj nowy shelf do mapy zeby kolejne naklejki mogly go uzywac
                    new_shelf = _Shelf(
                        y=new_shelf_y,
                        height=new_item.height_mm + gap_mm,
                        cursor_x=new_item.width_mm + gap_mm,
                        area_w=area_w,
                    )
                    prev_shelves.append(new_shelf)
                    moved = True
                    moved_count += 1
                    break

        if not moved:
            remaining_placements.append(placement)

    if moved_count > 0:
        log.info(f"  Przeniesiono {moved_count} naklejek z ostatniego arkusza")

    if remaining_placements:
        last_sheet.placements = remaining_placements
        # Przelicz height dla roll mode
        if is_roll and last_sheet.placements:
            # FIX: uwzgledniaj bleed2
            max_y = max(
                p.y_mm + (
                    p.sticker.height_mm + bleed2 if p.rotation_deg == 0
                    else p.sticker.width_mm + bleed2
                )
                for p in last_sheet.placements
            )
            last_sheet.height_mm = max_y + top + mark_zone_mm + gap_mm
    else:
        # Caly arkusz przeniesiony — usun go
        job.sheets.pop()
        log.info("  Ostatni arkusz calkowicie wchlaniety — usuniety")


def _rebuild_shelves(
    sheet: Sheet,
    area_w: float,
    gap_mm: float,
    origin_x: float,
    origin_y: float,
    bleed2: float = 0.0,
) -> list[_Shelf]:
    """Odbudowuje shelvy z placements (do konsolidacji i pass 2).

    FIX: uwzglednia bleed2 w obliczeniach wymiarow — bez tego
    shelfy myslaly ze maja wiecej wolnego miejsca niz w rzeczywistosci.
    """
    if not sheet.placements:
        return []

    # Zbierz Y-pozycje i pogrupuj w shelvy
    y_groups: dict[float, list[Placement]] = {}
    for p in sheet.placements:
        # Zaokraglij Y do 0.1mm zeby pogrupowac
        y_key = round((p.y_mm - origin_y) * 10) / 10
        y_groups.setdefault(y_key, []).append(p)

    shelves: list[_Shelf] = []
    for y_key in sorted(y_groups.keys()):
        placements = y_groups[y_key]
        # FIX: wysokosc shelfa uwzglednia bleed
        shelf_h = max(
            (p.sticker.height_mm + bleed2 if p.rotation_deg == 0
             else p.sticker.width_mm + bleed2)
            for p in placements
        ) + gap_mm
        # FIX: cursor_x uwzglednia bleed w wymiarach naklejek
        cursor_x = max(
            (p.x_mm - origin_x) +
            (p.sticker.width_mm + bleed2 if p.rotation_deg == 0
             else p.sticker.height_mm + bleed2) +
            gap_mm
            for p in placements
        )
        shelves.append(_Shelf(
            y=y_key,
            height=shelf_h,
            cursor_x=cursor_x,
            area_w=area_w,
        ))

    return shelves


def _center_placements(sheet: Sheet, center_y: bool = False, bleed2: float = 0.0):
    """Centruje zawartosc (placements) na arkuszu.

    Zawsze centruje w poziomie (X).
    Opcjonalnie centruje w pionie (Y) — center_y=True.
    bleed2 = 2 * bleed_mm — do obliczenia pełnego footprintu.
    """
    if not sheet.placements:
        return

    def _fw(p):
        """Pełna szerokość footprintu (content + bleed) z rotacją."""
        if abs(p.rotation_deg) in (90.0, 270.0):
            return p.sticker.height_mm + bleed2
        return p.sticker.width_mm + bleed2

    def _fh(p):
        """Pełna wysokość footprintu (content + bleed) z rotacją."""
        if abs(p.rotation_deg) in (90.0, 270.0):
            return p.sticker.width_mm + bleed2
        return p.sticker.height_mm + bleed2

    # Bounding box footprintów (z bleedem)
    content_left = min(p.x_mm for p in sheet.placements)
    content_right = max(p.x_mm + _fw(p) for p in sheet.placements)

    # Srodek printable area
    pa_x0, pa_y0, pa_x1, pa_y1 = sheet.printable_rect_mm
    pa_cx = (pa_x0 + pa_x1) / 2
    cc_x = (content_left + content_right) / 2
    dx = pa_cx - cc_x

    dy = 0.0
    if center_y:
        content_bottom = min(p.y_mm for p in sheet.placements)
        content_top = max(p.y_mm + _fh(p) for p in sheet.placements)
        pa_cy = (pa_y0 + pa_y1) / 2
        cc_y = (content_bottom + content_top) / 2
        dy = pa_cy - cc_y

    if abs(dx) < 0.01 and abs(dy) < 0.01:
        return

    for p in sheet.placements:
        p.x_mm += dx
        p.y_mm += dy

    log.debug(f"Centrowanie: dx={dx:.1f}mm, dy={dy:.1f}mm")


def _shelves_top(shelves: list[_Shelf]) -> float:
    """Zwraca gorny brzeg najwyzszego shelfa."""
    if not shelves:
        return 0.0
    return max(s.y + s.height for s in shelves)
