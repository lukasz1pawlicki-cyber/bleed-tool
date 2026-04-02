"""
Sticker Toolkit — marks.py
=============================
Znaczniki rejestracji dla ploterów tnących.

Algorytm Summa S3 OPOS oparty na źródłach pluginu Summa GoSign Tools
(gosign_opos_regmarks_base.py). Parametry:

  REGMARK_OPOS_SIZE_MM = 3
  REGMARK_OPOS_MARGIN_LEFT_MM = 12  (4 × size)
  REGMARK_OPOS_MARGIN_RIGHT_MM = 12
  REGMARK_OPOS_MARGIN_TOP_MM = 3
  REGMARK_OPOS_MARGIN_BOTTOM_MM = 3
  REGMARK_DIST_MM = 400
  OPOSXYMargin_MM = 10
  OPOSXYHeight_MM = 3
"""

from __future__ import annotations

import logging
from models import Sheet, Mark
from config import PLOTTERS

log = logging.getLogger(__name__)

# --- Parametry z pluginu Summa GoSign Tools (gosign_opos_regmarks_base.py) ---
_REGMARK_SIZE_MM = 3
_REGMARK_MARGIN_LR_MM = _REGMARK_SIZE_MM * 4   # 12mm
_REGMARK_MARGIN_TB_MM = _REGMARK_SIZE_MM        # 3mm
_REGMARK_DIST_MM = 400                          # max odległość między markerami Y
_OPOS_XY_MARGIN_MM = 10                         # gap bar ↔ narożnik
_OPOS_XY_HEIGHT_MM = 3                          # wysokość bara


def generate_marks(
    sheet: Sheet,
    plotter: str = "summa_s3",
) -> Sheet:
    """Generuje znaczniki rejestracji na arkuszu.

    WAŻNE: Summa S3 i JWEI mają CAŁKOWICIE osobne algorytmy markerów.
    Summa S3 OPOS: precyzyjne parametry wymagane przez GoSign (mark_offset=10mm,
    margin_lr=12mm, OPOS XY bar). Nie modyfikować bez testów na ploterze!
    JWEI: 4 narożne kwadraty z mark_offset_x/y_mm.

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
    sw = sheet.width_mm
    sh = sheet.height_mm

    if sh <= 0:
        log.warning(f"Marks: arkusz ma height={sh}mm — pomijam generację markerów (rola nie sfinalizowana?)")
        sheet.marks = []
        return sheet

    sheet.marks = []

    if plotter == "summa_s3":
        # Summa S3 OPOS — osobny algorytm, NIE dzielony z JWEI
        _generate_summa_marks(sheet, mark_type, mark_w, mark_h, mark_offset,
                              top, right, bottom, left, sw, sh)
    elif plotter == "jwei":
        # JWEI — osobny algorytm, NIE dzielony z Summa
        _generate_jwei_marks(sheet, mark_type, mark_w, mark_h, mark_offset,
                             top, right, bottom, left, sw, sh)
    else:
        _generate_generic_marks(sheet, mark_type, mark_w, mark_h, mark_offset,
                                min_marks, top, right, bottom, left, sw, sh)

    log.info(f"Marks: {len(sheet.marks)} znaczników ({plotter}, {mark_type})")
    return sheet


def _generate_summa_marks(
    sheet: Sheet,
    mark_type: str, mark_w: float, mark_h: float, mark_offset: float,
    top: float, right: float, bottom: float, left: float,
    sw: float, sh: float,
) -> None:
    """Generuje markery Summa S3 OPOS — algorytm 1:1 z pluginu GoSign Tools.

    ╔══════════════════════════════════════════════════════════════════════╗
    ║ KRYTYCZNE: Parametry tego algorytmu są precyzyjnie dopasowane      ║
    ║ do wymagań GoSign / Summa S3 OPOS. NIE MODYFIKOWAĆ bez testów     ║
    ║ na fizycznym ploterze! Zmiana pozycji/rozmiaru markerów powoduje   ║
    ║ błędy detekcji OPOS na Summa S3.                                  ║
    ║                                                                    ║
    ║ Ten algorytm jest CAŁKOWICIE niezależny od JWEI.                   ║
    ╚══════════════════════════════════════════════════════════════════════╝

    Plugin oblicza bbox grafiki i rozkłada markery wokół niej.
    My używamy arkusza (sheet) jako bbox — markery na krawędziach arkusza.

    Układ pluginu:
      - leftBorder = bbox.left - margin_left(12mm) - mark_size(3mm)
      - rightBorder = bbox.left + margin_right(12mm) + bbox.width
      - bottomBorder = bbox.top + bbox.height + margin_bottom(3mm)
      - topBorder = bbox.top - 2 * margin_top(3mm)

    Nasze uproszczenie: bbox = printable area (po odjęciu marginesów arkusza).
    Pozycje narożników = mark_offset od krawędzi arkusza (jak dotychczas).

    Parametry z config (NIE ZMIENIAĆ):
      mark_size = 3×3mm, mark_offset = 10mm, OPOS XY bar height = 3mm
    """
    # Pozycje narożne (kompatybilne z resztą programu)
    bl = (left + mark_offset, bottom + mark_offset)
    br = (sw - right - mark_offset - mark_w, bottom + mark_offset)
    tl = (left + mark_offset, sh - top - mark_offset - mark_h)
    tr = (sw - right - mark_offset - mark_w, sh - top - mark_offset - mark_h)

    # --- Algorytm Y z pluginu: dziel na pół dopóki > REGMARK_DIST ---
    dy_total = tl[1] - bl[1]
    dy_step = dy_total
    n_y = 0
    while dy_step > _REGMARK_DIST_MM:
        dy_step /= 2
        n_y = n_y * 2 + 1
    n_y += 2   # +2 = dolny i górny narożnik

    # Markery lewe + prawe na każdej pozycji Y
    for i in range(n_y):
        y = bl[1] + i * dy_step
        # Lewa
        sheet.marks.append(Mark(
            x_mm=bl[0], y_mm=y,
            width_mm=mark_w, height_mm=mark_h,
            mark_type=mark_type,
        ))
        # Prawa
        sheet.marks.append(Mark(
            x_mm=br[0], y_mm=y,
            width_mm=mark_w, height_mm=mark_h,
            mark_type=mark_type,
        ))
        if i == 0:
            log.info(f"Mark [bottom L+R]: y={y:.1f}mm")
        elif i == n_y - 1:
            log.info(f"Mark [top L+R]: y={y:.1f}mm")
        else:
            log.info(f"Mark [extra Y{i} L+R]: y={y:.1f}mm")

    # --- OPOS XY correction line (dolna krawędź) ---
    # Plugin: x = leftBorder + OPOSXYMargin + mark_size
    #         width = rightBorder - mark_size - leftBorder - 2 * OPOSXYMargin
    #         y = bottomBorder (= dolna krawędź markerów)
    bar_x = bl[0] + mark_w + _OPOS_XY_MARGIN_MM
    bar_end_x = br[0] - _OPOS_XY_MARGIN_MM
    bar_w = bar_end_x - bar_x
    bar_y = bl[1]  # ten sam Y co dolne narożniki

    if bar_w > mark_w:
        bar = Mark(
            x_mm=bar_x,
            y_mm=bar_y,
            width_mm=bar_w,
            height_mm=_OPOS_XY_HEIGHT_MM,
            mark_type=mark_type,
            is_bar=True,
        )
        sheet.marks.append(bar)
        log.info(f"Mark [OPOS XY bar]: ({bar_x:.1f}, {bar_y:.1f})mm, {bar_w:.1f}×{_OPOS_XY_HEIGHT_MM}mm")


def _generate_jwei_marks(
    sheet: Sheet,
    mark_type: str, mark_w: float, mark_h: float, mark_offset: float,
    top: float, right: float, bottom: float, left: float,
    sw: float, sh: float,
) -> None:
    """JWEI: 4 narożne kwadraty, bez bara, bez extra markerów.

    Algorytm CAŁKOWICIE niezależny od Summa S3 OPOS.

    Pozycje markerów od krawędzi papieru (nie od marginesu):
      - X: mark_offset_x_mm (5mm) od lewej/prawej
      - Y: mark_offset_y_mm (50mm) od górnej/dolnej
    Wartości X/Y mogą być zamienione przez 'Odwróć markery' w FlexCut dialog.
    """
    from config import PLOTTERS
    jwei_cfg = PLOTTERS.get("jwei", {})
    off_x = jwei_cfg.get("mark_offset_x_mm", mark_offset)
    off_y = jwei_cfg.get("mark_offset_y_mm", mark_offset)
    corners = [
        (off_x, off_y),                                    # bottom-left
        (sw - off_x - mark_w, off_y),                      # bottom-right
        (sw - off_x - mark_w, sh - off_y - mark_h),        # top-right
        (off_x, sh - off_y - mark_h),                      # top-left
    ]
    for x, y in corners:
        sheet.marks.append(Mark(
            x_mm=x, y_mm=y,
            width_mm=mark_w, height_mm=mark_h,
            mark_type=mark_type,
        ))
        log.info(f"Mark [corner]: ({x:.1f}, {y:.1f})mm")


def _generate_generic_marks(
    sheet: Sheet,
    mark_type: str, mark_w: float, mark_h: float, mark_offset: float,
    min_marks: int,
    top: float, right: float, bottom: float, left: float,
    sw: float, sh: float,
) -> None:
    """Generyczne markery dla innych ploterów."""
    bl = (left + mark_offset, bottom + mark_offset)
    br = (sw - right - mark_offset - mark_w, bottom + mark_offset)
    tr = (sw - right - mark_offset - mark_w, sh - top - mark_offset - mark_h)
    tl = (left + mark_offset, sh - top - mark_offset - mark_h)

    corners = [bl, br, tr, tl] if min_marks >= 4 else [bl, br, tr]
    for x, y in corners:
        sheet.marks.append(Mark(
            x_mm=x, y_mm=y,
            width_mm=mark_w, height_mm=mark_h,
            mark_type=mark_type,
        ))

    # Extra markery co 300mm na dłuższych osiach
    px0, py0, px1, py1 = sheet.printable_rect_mm
    if sw > 400:
        n_h = max(0, int((px1 - px0) / 300) - 1)
        if n_h > 0:
            spacing = (br[0] - bl[0]) / (n_h + 1)
            for i in range(1, n_h + 1):
                x = bl[0] + spacing * i
                sheet.marks.append(Mark(x_mm=x, y_mm=bl[1],
                    width_mm=mark_w, height_mm=mark_h, mark_type=mark_type))
                sheet.marks.append(Mark(x_mm=x, y_mm=tr[1],
                    width_mm=mark_w, height_mm=mark_h, mark_type=mark_type))

    if sh > 400:
        n_v = max(0, int((py1 - py0) / 300) - 1)
        if n_v > 0:
            spacing = (tl[1] - bl[1]) / (n_v + 1)
            for i in range(1, n_v + 1):
                y = bl[1] + spacing * i
                sheet.marks.append(Mark(x_mm=bl[0], y_mm=y,
                    width_mm=mark_w, height_mm=mark_h, mark_type=mark_type))
                sheet.marks.append(Mark(x_mm=br[0], y_mm=y,
                    width_mm=mark_w, height_mm=mark_h, mark_type=mark_type))
