"""
Sticker Toolkit — contour.py
=============================
Detekcja konturu cięcia z pliku wejściowego.

Obsługiwane formaty i ścieżki przetwarzania:
  ┌─────────────────────────────────────────────────────────────────────────┐
  │ Format                  │ Ścieżka                                     │
  │─────────────────────────│─────────────────────────────────────────────│
  │ PNG/JPG/TIFF/BMP/WEBP   │ _detect_raster() → alpha/bg contour         │
  │   - RGBA (przezroczystość) │  → _detect_raster_alpha_contour()        │
  │   - RGB (bez alpha)        │  → _detect_raster_bg_contour()           │
  │ SVG                     │ svg_to_pdf() → PDF pipeline z svg_contour   │
  │ EPS                     │ eps_to_pdf() → PDF pipeline                 │
  │ PDF wektor              │ find_outermost_drawing() → extract_path()   │
  │ PDF raster-only         │ _render_alpha_contour() lub prostokąt       │
  └─────────────────────────────────────────────────────────────────────────┘

Główna funkcja: detect_contour(path) → list[Sticker]

UWAGA: Każdy typ wejścia ma ODRĘBNĄ ścieżkę przetwarzania.
Nie mieszaj ustawień między typami — zmiany w jednej ścieżce
NIE powinny wpływać na inne.
"""

from __future__ import annotations

import logging
import os
import re as _re
import numpy as np
import fitz  # PyMuPDF
from PIL import Image as PILImage, ImageFilter

from models import Sticker
import config
from config import PT_TO_MM, MM_TO_PT
from modules.file_loader import FileType, detect_type, to_pdf, svg_dimensions_from_name
from modules.svg_convert import extract_svg_contour, parse_size_from_filename
from modules.crop_marks import apply_crop_marks_cropping

log = logging.getLogger(__name__)

# Rozszerzenia plików rastrowych
_RASTER_EXT = ('.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp', '.webp')
_DEFAULT_DPI = 300  # DPI fallback gdy brak metadanych


# =============================================================================
# WALIDACJA WEJŚCIA
# =============================================================================

def validate_input(pdf_path: str) -> tuple[fitz.Document, fitz.Page, list]:
    """Otwiera PDF i waliduje pierwszą stronę (kompatybilność wsteczna).

    Returns:
        (doc, page, drawings) — otwarty dokument, strona, lista drawings
    """
    doc = fitz.open(pdf_path)
    page, drawings = validate_page(doc, 0)
    return doc, page, drawings


def validate_page(doc: fitz.Document, page_index: int,
                   skip_raster_check: bool = False) -> tuple[fitz.Page, list]:
    """Waliduje pojedynczą stronę PDF.

    Wymaga ścieżek wektorowych (drawings). Obrazy rastrowe są dozwolone
    (ostrzeżenie w logu), ale strona musi mieć przynajmniej 1 drawing.

    Args:
        doc: otwarty dokument PDF
        page_index: indeks strony (0-based)
        skip_raster_check: pomiń ostrzeżenie o rastrach (dla SVG-konwertowanych)

    Returns:
        (page, drawings)

    Raises:
        ValueError: jeśli strona jest nieprawidłowa
    """
    if page_index < 0 or page_index >= len(doc):
        raise ValueError(f"Strona {page_index + 1} nie istnieje (PDF ma {len(doc)} stron)")

    page = doc[page_index]

    if not skip_raster_check:
        images = page.get_images()
        if len(images) > 0:
            log.info(
                f"Strona {page_index + 1}: zawiera {len(images)} obraz(ów) rastrowych "
                f"(przetwarzanie kontynuowane)"
            )

    drawings = page.get_drawings()
    if len(drawings) == 0:
        raise ValueError(f"Strona {page_index + 1} nie zawiera sciezek wektorowych")

    log.info(
        f"Walidacja OK: strona {page_index + 1}, {len(drawings)} drawings, "
        f"{page.rect.width:.1f}x{page.rect.height:.1f}pt"
    )
    return page, drawings


# =============================================================================
# WYKRYWANIE ZEWNĘTRZNEGO KONTURU
# =============================================================================

def find_outermost_drawing(
    drawings: list, page_rect: fitz.Rect, tolerance: float = 0.5
) -> tuple[int, dict]:
    """Znajduje drawing, którego bbox zawiera cały page_rect (najciaśniejsze dopasowanie).

    Preferuje drawings z fill (tło), ignoruje stroke-only (crop marks, die lines).
    Fallback: drawing o największym bbox z fill, potem bez fill.
    """
    candidates = []
    for i, d in enumerate(drawings):
        r = d['rect']
        if (r.x0 <= page_rect.x0 + tolerance and
                r.y0 <= page_rect.y0 + tolerance and
                r.x1 >= page_rect.x1 - tolerance and
                r.y1 >= page_rect.y1 - tolerance):
            area = abs(r.width * r.height)
            has_fill = d.get('fill') is not None
            candidates.append((i, d, area, has_fill))

    if candidates:
        # Preferuj kandydatów z fill (tło/background)
        filled = [c for c in candidates if c[3]]
        if filled:
            idx, drawing, area, _ = min(filled, key=lambda x: x[2])
            log.info(f"Zewnętrzny kontur: drawing[{idx}], area={area:.1f}pt² (filled)")
            return idx, drawing
        # Brak filled — weź najciaśniejszy stroke-only
        idx, drawing, area, _ = min(candidates, key=lambda x: x[2])
        log.info(f"Zewnętrzny kontur: drawing[{idx}], area={area:.1f}pt² (stroke-only)")
        return idx, drawing

    # Fallback: największy bbox z fill
    filled_drawings = [(i, d) for i, d in enumerate(drawings) if d.get('fill') is not None]
    if filled_drawings:
        idx, drawing = max(
            filled_drawings,
            key=lambda x: abs(x[1]['rect'].width * x[1]['rect'].height),
        )
        log.info(f"Zewnętrzny kontur (fallback filled): drawing[{idx}]")
        return idx, drawing

    # Ostateczny fallback: największy bbox (nawet bez fill)
    idx, drawing = max(
        enumerate(drawings),
        key=lambda x: abs(x[1]['rect'].width * x[1]['rect'].height),
    )
    log.info(f"Zewnętrzny kontur (fallback): drawing[{idx}]")
    return idx, drawing


# =============================================================================
# EKSTRAKCJA SEGMENTÓW ŚCIEŻKI
# =============================================================================

def _subpath_bbox_area(segs: list) -> float:
    """Oblicza area bboxu subpath."""
    pts = []
    for s in segs:
        if s[0] == 'l':
            pts.extend([s[1], s[2]])
        elif s[0] == 'c':
            pts.extend([s[1], s[4]])
    if not pts:
        return 0.0
    arr = np.array(pts)
    return float(
        (arr[:, 0].max() - arr[:, 0].min()) * (arr[:, 1].max() - arr[:, 1].min())
    )


def extract_path_segments(items: list, gap_threshold: float = 2.0) -> list:
    """Wyodrębnia segmenty ścieżki z items[] drawingu.

    Zwraca listę segmentów:
      ('l', np.array(start), np.array(end))
      ('c', np.array(p0), np.array(p1), np.array(p2), np.array(p3))

    Obsługuje compound paths — wykrywa subpaths po moveTo lub gapach.
    Zwraca zewnętrzną subpath (największy bbox).
    """
    all_subpaths: list[list] = []
    current_segments: list = []
    last_point: np.ndarray | None = None

    def _start_new_subpath():
        nonlocal current_segments
        if current_segments:
            all_subpaths.append(current_segments)
        current_segments = []

    for item in items:
        op = item[0]

        if op == 'm':  # moveTo
            _start_new_subpath()
            last_point = np.array([item[1].x, item[1].y])

        elif op == 'l':  # lineTo
            start = np.array([item[1].x, item[1].y])
            end = np.array([item[2].x, item[2].y])
            if last_point is not None and np.linalg.norm(start - last_point) > gap_threshold:
                _start_new_subpath()
            if last_point is None:
                last_point = start
            current_segments.append(('l', start, end))
            last_point = end

        elif op == 'c':  # curveTo (cubic Bézier)
            p0 = np.array([item[1].x, item[1].y])
            p1 = np.array([item[2].x, item[2].y])
            p2 = np.array([item[3].x, item[3].y])
            p3 = np.array([item[4].x, item[4].y])
            if last_point is not None and np.linalg.norm(p0 - last_point) > gap_threshold:
                _start_new_subpath()
            if last_point is None:
                last_point = p0
            current_segments.append(('c', p0, p1, p2, p3))
            last_point = p3

        elif op == 're':  # rectangle
            rect = item[1]
            tl = np.array([rect.x0, rect.y0])
            tr = np.array([rect.x1, rect.y0])
            br = np.array([rect.x1, rect.y1])
            bl = np.array([rect.x0, rect.y1])
            _start_new_subpath()
            current_segments.extend([
                ('l', tl, tr), ('l', tr, br),
                ('l', br, bl), ('l', bl, tl),
            ])
            last_point = tl

    if current_segments:
        all_subpaths.append(current_segments)

    if not all_subpaths:
        raise ValueError("Brak segmentów ścieżki")

    # Dla compound paths wybierz zewnętrzną subpath (największy bbox)
    outermost = max(all_subpaths, key=_subpath_bbox_area)
    n_lines = sum(1 for s in outermost if s[0] == 'l')
    n_curves = sum(1 for s in outermost if s[0] == 'c')
    log.info(
        f"Segmenty: {len(outermost)} ({n_lines} linii, {n_curves} krzywych) "
        f"z {len(all_subpaths)} subpath(s)"
    )
    return outermost


# =============================================================================
# TRIMBOX — PRZYCINANIE STRON ZE SPADAMI
# =============================================================================

def _crop_to_trimbox(doc: fitz.Document) -> tuple[set[int], set[int]]:
    """Jeśli strona ma TrimBox mniejszy od MediaBox, przycina do TrimBox.

    Pliki eksportowane z Illustratora/InDesign ze spadami mają:
      - MediaBox = cała strona ze spadami
      - TrimBox  = właściwy rozmiar naklejki (bez spadów)
    Ustawiamy CropBox = TrimBox, co powoduje, że PyMuPDF
    traktuje TrimBox jako widoczny obszar strony.

    UWAGA: Porównujemy surowe wartości PDF (xref), nie fitz-owe,
    bo fitz normalizuje y-coords (y-down) inaczej dla mediabox vs trimbox,
    co powoduje fałszywe różnice gdy boxy są identyczne w PDF.

    Dodatkowo wykrywa malformed TrimBox (Canva): TB ma te same wymiary
    co MB, tylko przesunięcie offset. Content streamu rysuje w MB coords,
    nie w TB — pipeline wektorowy daje biały pasek / beżowy bleed.
    Takie strony NIE są cropowane i trafiają do raster fallback.

    Returns:
        (cropped_pages, malformed_pages) — zbiory indeksów
    """
    cropped_pages: set[int] = set()
    malformed_pages: set[int] = set()
    for page in doc:
        xref = page.xref
        raw_media = doc.xref_get_key(xref, "MediaBox")
        raw_trim = doc.xref_get_key(xref, "TrimBox")

        # Brak TrimBox → nie ma spadów
        if raw_trim[0] == "null":
            continue

        # Specjalny przypadek Canva malformed: raw MB == raw TB (identyczne),
        # ALE fitz normalizuje y-axis inaczej i page.mediabox != page.trimbox.
        # Content streamu rysuje w MB coords, fitz page.rect = TB po normalizacji.
        # Pipeline wektorowy daje przesunięcie → raster fallback.
        if raw_media[1] == raw_trim[1]:
            fmb, ftb = page.mediabox, page.trimbox
            if (abs(fmb.width - ftb.width) < 0.5
                    and abs(fmb.height - ftb.height) < 0.5
                    and (abs(fmb.x0 - ftb.x0) > 0.5
                         or abs(fmb.y0 - ftb.y0) > 0.5)):
                log.info(
                    f"Strona {page.number + 1}: Canva malformed "
                    f"(raw MB=raw TB, fitz normalized różne) — raster fallback"
                )
                malformed_pages.add(page.number)
            continue

        # Parsuj surowe wartości do porównania numerycznego
        def _parse_box(raw: str) -> list[float]:
            return [float(x) for x in raw.strip("[] ").split()]

        try:
            media_vals = _parse_box(raw_media[1])
            trim_vals = _parse_box(raw_trim[1])
        except (ValueError, IndexError):
            continue

        if len(media_vals) != 4 or len(trim_vals) != 4:
            continue

        # Sprawdź czy wartości się różnią (= prawdziwe spady)
        if all(abs(m - t) <= 0.5 for m, t in zip(media_vals, trim_vals)):
            continue

        # Malformed TrimBox: TB ma te same WYMIARY co MB, tylko offset się różni.
        # To nie są prawdziwe spady — Canva eksportuje tak błędnie. Content
        # rysowany w MB coords, pipeline wektorowy z TB daje przesunięcie.
        mb_w = media_vals[2] - media_vals[0]
        mb_h = media_vals[3] - media_vals[1]
        tb_w = trim_vals[2] - trim_vals[0]
        tb_h = trim_vals[3] - trim_vals[1]
        if abs(mb_w - tb_w) < 0.5 and abs(mb_h - tb_h) < 0.5:
            log.info(
                f"Strona {page.number + 1}: TrimBox ma te same wymiary co "
                f"MediaBox (offset różny) — malformed, raster fallback"
            )
            malformed_pages.add(page.number)
            continue

        trimbox = page.trimbox
        log.info(
            f"Strona {page.number + 1}: TrimBox "
            f"{trimbox.width:.1f}x{trimbox.height:.1f}pt "
            f"({trimbox.width * PT_TO_MM:.1f}x{trimbox.height * PT_TO_MM:.1f}mm) "
            f"— przycinanie ze spadów"
        )
        page.set_cropbox(trimbox)
        cropped_pages.add(page.number)
    return cropped_pages, malformed_pages


def _detect_raster(image_path: str) -> Sticker:
    """Tworzy Sticker z pliku rastrowego (PNG/JPG/TIFF/BMP/WEBP).

    Ścieżka przetwarzania zależy od trybu obrazu:
      - RGBA/LA/PA (z alpha) → _detect_raster_alpha_contour()
        Wynik: polygon lub okrąg (Bézier), przycinanie do content bbox
      - RGB/L (bez alpha) → _detect_raster_bg_contour()
        Wynik: polygon z flood-fill tła lub prostokąt

    Wymiary mm (priorytet):
      1. Z nazwy pliku (np. '50x50' → 50×50mm)
      2. Z metadanych DPI obrazu
      3. Fallback: 300 DPI

    Output Sticker ma:
      - raster_path = ścieżka do oryginalnego pliku
      - raster_crop_box = (x, y, x2, y2) px jeśli przycięty do content
      - cut_segments przesunięte do origin (0,0) relative do crop box
      - width_mm/height_mm = wymiary content area (nie pełnego artboardu)
    """
    img = PILImage.open(image_path)
    px_w, px_h = img.size

    # 1. Wymiary z nazwy pliku (priorytet — jak w SVG)
    size_from_name = parse_size_from_filename(image_path)
    if size_from_name is not None:
        w_mm, h_mm = size_from_name
        log.info(f"Raster wymiary z nazwy pliku: {w_mm:.1f}×{h_mm:.1f}mm")
    else:
        # 2. Z metadanych DPI
        dpi_info = img.info.get('dpi')
        if dpi_info and dpi_info[0] > 0 and dpi_info[1] > 0:
            dpi_x, dpi_y = dpi_info
            # Niektóre obrazy mają DPI jako float
            dpi_x = float(dpi_x)
            dpi_y = float(dpi_y)
            # Sanity check — DPI poniżej 10 lub powyżej 10000 to błąd
            # 72 i 96 DPI to domyślne wartości ekranowe (JFIF), nie rzeczywiste DPI druku
            if dpi_x < 10 or dpi_x > 10000:
                dpi_x = _DEFAULT_DPI
            if dpi_y < 10 or dpi_y > 10000:
                dpi_y = _DEFAULT_DPI
            # Tolerancja na DPI typu 96.012 (Canva), 71.999 itp. — screen DPI bywa float
            _is_screen_dpi = lambda d: abs(d - 72) < 1.0 or abs(d - 96) < 1.0
            if _is_screen_dpi(dpi_x) and _is_screen_dpi(dpi_y):
                log.info(
                    f"Raster DPI={dpi_x:.1f} (domyślne ekranowe), "
                    f"fallback na {_DEFAULT_DPI} DPI"
                )
                dpi_x = dpi_y = _DEFAULT_DPI
            else:
                log.info(f"Raster DPI z metadanych: {dpi_x:.0f}×{dpi_y:.0f}")
        else:
            dpi_x = dpi_y = _DEFAULT_DPI
            log.info(f"Raster: brak DPI w metadanych, fallback {_DEFAULT_DPI} DPI")

        w_mm = px_w / dpi_x * 25.4
        h_mm = px_h / dpi_y * 25.4

    # Wymiary strony w pt (PDF points)
    w_pt = w_mm * MM_TO_PT
    h_pt = h_mm * MM_TO_PT

    # Sprawdź alpha channel — jeśli obraz ma przezroczystość, wykryj kształt
    has_alpha = img.mode == 'RGBA' or (img.mode == 'PA') or (img.mode == 'LA')
    cut_segments = None
    edge_rgb = None
    raster_crop_box = None  # (x, y, x2, y2) w px — crop do content area

    if has_alpha:
        cut_segments, edge_rgb = _detect_raster_alpha_contour(
            img, w_pt, h_pt
        )

    # RGB bez alpha: domyslnie PROSTOKATNY cut (rectangular sticker, typowy
    # Canva PNG export). BG contour detection (die-cut po jednolitym tle)
    # jest opt-in przez env var — uzywane np. dla zeskanowanych logo z bialym
    # tlem gdzie chcemy sticker w ksztalcie logo.
    _bg_detect = os.environ.get("BLEED_RASTER_BG_DETECT", "0").strip() in ("1", "true", "yes")
    if cut_segments is None and not has_alpha and _bg_detect:
        try:
            cut_segments, edge_rgb = _detect_raster_bg_contour(
                img, w_pt, h_pt
            )
        except ImportError:
            log.warning("scipy niedostępne — pomijam detekcję tła (fallback: prostokąt)")
            cut_segments, edge_rgb = None, None

    if cut_segments is None:
        # Fallback: kontur cięcia = prostokąt (fitz coords: y-down)
        page_rect = fitz.Rect(0, 0, w_pt, h_pt)
        cut_segments = _make_page_rect_contour(page_rect)

    if edge_rgb is None:
        # Kolor krawędzi — próbkowanie z krawędzi obrazu
        edge_rgb = _sample_raster_edge_color(img)

    # --- Przytnij do bounding box linii cięcia ---
    # Wymiary stickera = DOKŁADNIE bbox cut_segments.
    # Eksport dodaje bleed wokół tego. Crop box rastra rozszerzony o zapas
    # na dilation (bleed zone potrzebuje pikseli źródłowych).
    if has_alpha and cut_segments is not None:
        # Bbox z ON-CURVE points (p0, p3) — control points (cp1, cp2)
        # leżą poza krzywą i zawyżają bbox.
        oncurve_pts = []
        for seg in cut_segments:
            if seg[0] == 'l':
                oncurve_pts.extend([seg[1], seg[2]])
            elif seg[0] == 'c':
                oncurve_pts.extend([seg[1], seg[4]])  # p0, p3 only
        if oncurve_pts:
            oncurve_arr = np.array(oncurve_pts)
            seg_min_x_pt = float(oncurve_arr[:, 0].min())
            seg_min_y_pt = float(oncurve_arr[:, 1].min())
            seg_max_x_pt = float(oncurve_arr[:, 0].max())
            seg_max_y_pt = float(oncurve_arr[:, 1].max())

            cut_w_pt = seg_max_x_pt - seg_min_x_pt
            cut_h_pt = seg_max_y_pt - seg_min_y_pt

            # Przytnij jeśli cut bbox jest mniejszy niż strona (> 2pt różnicy)
            if (w_pt - cut_w_pt) > 2 or (h_pt - cut_h_pt) > 2:
                px_to_pt_x = w_pt / px_w
                px_to_pt_y = h_pt / px_h

                # Crop box w px — DOKŁADNIE cut bbox (eksport sam rozszerza canvas)
                crop_x = max(0, int(round(seg_min_x_pt / px_to_pt_x)))
                crop_y = max(0, int(round(seg_min_y_pt / px_to_pt_y)))
                crop_x2 = min(px_w, int(round(seg_max_x_pt / px_to_pt_x)))
                crop_y2 = min(px_h, int(round(seg_max_y_pt / px_to_pt_y)))
                raster_crop_box = (crop_x, crop_y, crop_x2, crop_y2)

                # Przesuń segmenty do origin (0,0) — offset = cut bbox origin
                offset = np.array([seg_min_x_pt, seg_min_y_pt])
                shifted_segments = []
                for seg in cut_segments:
                    if seg[0] == 'l':
                        shifted_segments.append((
                            'l', seg[1] - offset, seg[2] - offset,
                        ))
                    elif seg[0] == 'c':
                        shifted_segments.append((
                            'c',
                            seg[1] - offset, seg[2] - offset,
                            seg[3] - offset, seg[4] - offset,
                        ))
                cut_segments = shifted_segments

                # Wymiary stickera = DOKŁADNIE bbox linii cięcia
                w_pt = cut_w_pt
                h_pt = cut_h_pt
                w_mm = w_pt * PT_TO_MM
                h_mm = h_pt * PT_TO_MM
                log.info(
                    f"Raster alpha: crop do content bbox "
                    f"{w_mm:.1f}x{h_mm:.1f}mm (crop_px={raster_crop_box})"
                )

    img.close()

    sticker = Sticker(
        source_path=image_path,
        page_index=0,
        width_mm=w_mm,
        height_mm=h_mm,
        cut_segments=cut_segments,
        pdf_doc=None,          # raster — nie ma pdf_doc
        raster_path=image_path,
        raster_crop_box=raster_crop_box,
        page_width_pt=w_pt,
        page_height_pt=h_pt,
        outermost_drawing_idx=None,
        edge_color_rgb=edge_rgb,
    )

    log.info(
        f"Raster sticker: {w_mm:.1f}×{h_mm:.1f}mm ({px_w}×{px_h}px), "
        f"edge RGB=({edge_rgb[0]:.2f}, {edge_rgb[1]:.2f}, {edge_rgb[2]:.2f})"
    )
    return sticker


def _opencv_boundary_trace(mask: np.ndarray) -> np.ndarray | None:
    """Śledzi kontur binarnej maski przez cv2.findContours (RETR_EXTERNAL).

    Alternatywa dla _moore_boundary_trace — zaimplementowana w C, szybsza.
    Wymaga zainstalowanego opencv-python. Jeśli brak biblioteki,
    dispatcher (_boundary_trace) robi fallback na Moore.

    Args:
        mask: binarna maska (np.uint8, 0/1) — foreground = 1

    Returns:
        np.ndarray (N, 2) [x, y] — uporządkowane punkty graniczne (clockwise)
        lub None jeśli brak konturu / import cv2 się nie udał.
    """
    try:
        import cv2
    except ImportError:
        log.warning("opencv-python nie jest zainstalowany — fallback na Moore trace")
        return None

    # cv2 wymaga uint8 i wartości 0/255 (albo 0/1 zadziała, ale dokumentacja preferuje 255)
    bin_mask = (mask > 0).astype(np.uint8) * 255

    # RETR_EXTERNAL = tylko kontury zewnętrzne (ignoruje dziury)
    # CHAIN_APPROX_NONE = wszystkie punkty (bez uproszczenia — DP robimy sami)
    contours, _ = cv2.findContours(
        bin_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE
    )

    if not contours:
        return None

    # Wybierz największy kontur (największe pole) — najbardziej prawdopodobny
    # kształt naklejki
    largest = max(contours, key=cv2.contourArea)
    if len(largest) < 3:
        return None

    # OpenCV zwraca shape (N, 1, 2) — spłaszczamy do (N, 2)
    pts = largest.reshape(-1, 2).astype(float)
    return pts


def _boundary_trace(mask: np.ndarray, engine: str | None = None) -> np.ndarray | None:
    """Dispatcher: wybiera silnik detekcji konturu (moore/opencv).

    Args:
        mask: binarna maska
        engine: "moore" | "opencv" | "auto" | None (None = z config.CONTOUR_ENGINE)

    Returns:
        np.ndarray (N, 2) uporządkowanych punktów granicznych lub None.
    """
    if engine is None:
        engine = config.CONTOUR_ENGINE

    engine = engine.lower()
    if engine == "opencv":
        result = _opencv_boundary_trace(mask)
        if result is not None:
            return result
        log.warning("OpenCV engine nie zwrócił konturu — fallback na Moore")
        return _moore_boundary_trace(mask)

    if engine == "auto":
        # Spróbuj opencv, fallback na moore
        try:
            import cv2  # noqa: F401
            result = _opencv_boundary_trace(mask)
            if result is not None:
                return result
        except ImportError:
            pass
        return _moore_boundary_trace(mask)

    # Default: moore
    return _moore_boundary_trace(mask)


def _moore_boundary_trace(mask: np.ndarray) -> np.ndarray | None:
    """Śledzi kontur binarnej maski algorytmem Moore neighborhood tracing.

    Chodzi po krawędzi kształtu piksel po pikselu (8-connected clockwise).
    Zwraca uporządkowane punkty graniczne jako np.ndarray (N, 2) [x, y]
    lub None jeśli brak kształtu.
    """
    h, w = mask.shape
    # Padujemy maską 1px aby uniknąć sprawdzania granic
    padded = np.pad(mask, 1, mode='constant', constant_values=0)

    # Znajdź pierwszy piksel foreground (skanuj top→bottom, left→right)
    start = None
    for y in range(1, h + 1):
        for x in range(1, w + 1):
            if padded[y, x]:
                start = (x, y)
                break
        if start:
            break
    if start is None:
        return None

    # 8 kierunków (clockwise): E, SE, S, SW, W, NW, N, NE
    #                          0   1   2   3   4   5   6   7
    dx = [1, 1, 0, -1, -1, -1, 0, 1]
    dy = [0, 1, 1,  1,  0, -1, -1, -1]

    boundary = [start]
    cx, cy = start
    # Startowy kierunek: przyszliśmy z lewej (W), więc zaczynamy od NW (dir=5)
    # bo skanowaliśmy od lewej i znaleźliśmy piksel — tło jest po lewej
    direction = 6  # zaczynamy szukać od N (bo przyszliśmy z góry — skan top→bottom)

    max_iter = h * w * 2  # safety limit
    for _ in range(max_iter):
        # Szukaj następnego piksela foreground — clockwise od direction
        # Zaczynamy od (direction + 5) % 8 — cofnij się 3 (= sprawdź od strony tła)
        search_start = (direction + 6) % 8  # start from opposite + 1 back

        found = False
        for i in range(8):
            d = (search_start + i) % 8
            nx = cx + dx[d]
            ny = cy + dy[d]
            if padded[ny, nx]:
                cx, cy = nx, ny
                direction = d
                break
        else:
            # Izolowany piksel
            break

        if (cx, cy) == start:
            break
        boundary.append((cx, cy))

    if len(boundary) < 3:
        return None

    # Konwertuj z padded coords (1-based) do original coords (0-based)
    pts = np.array(boundary, dtype=float)
    pts[:, 0] -= 1
    pts[:, 1] -= 1
    return pts


def _detect_raster_alpha_contour(
    img, w_pt: float, h_pt: float
) -> tuple[list | None, tuple[float, float, float] | None]:
    """Wykrywa kontur z kanału alpha obrazu rastrowego.

    Algorytm:
      1. Threshold alpha > 50 → binarna maska (widoczna treść naklejki)
      2. Moore boundary tracing — chodzi po krawędzi piksel po pikselu
      3. Skalowanie do roboczej rozdzielczości (po śledzeniu)
      4. Douglas-Peucker simplification → Catmull-Rom → cubic Bézier

    Prawidłowo śledzi kształt z wklęsłościami (między nogami, nad głową itp.)
    w przeciwieństwie do row-scan (leftmost/rightmost), który je traci.

    Zwraca (cut_segments, edge_rgb) lub (None, None) gdy alpha jest trywialna.
    Segmenty w koordynatach pt, origin (0,0).
    """
    rgba = img.convert('RGBA')
    arr = np.array(rgba)
    alpha = arr[:, :, 3]
    rgb = arr[:, :, :3]
    h, w = alpha.shape

    # Sprawdź czy alpha jest trywialna (cały obraz opaque).
    # Threshold 0.1% — dla rounded-rect z radius 9% bok, odcięte rogi to
    # ~0.69% pikseli, więc 1% threshold fałszywie detektował to jako pełny
    # opaque i zwracał prostokąt zamiast zaokrąglonego konturu.
    transparent_count = np.sum(alpha < 128)
    total = h * w
    if transparent_count < total * 0.001:
        log.info("Raster alpha: obraz prawie w pełni opaque, prostokątny kontur")
        return None, None

    # Kolor krawędzi z pikseli o wysokim alpha (>100)
    edge_colors = []
    step = max(1, h // 200)
    for y in range(0, h, step):
        orig_cols = np.where(alpha[y] > 100)[0]
        if len(orig_cols) > 0:
            edge_colors.append(rgb[y, int(orig_cols[0])].astype(float))
            edge_colors.append(rgb[y, int(orig_cols[-1])].astype(float))

    if edge_colors:
        avg = np.mean(edge_colors, axis=0) / 255.0
        edge_rgb = (float(avg[0]), float(avg[1]), float(avg[2]))
    else:
        edge_rgb = (1.0, 1.0, 1.0)

    # --- Przygotowanie maski ---
    # Skaluj do roboczej rozdzielczości (maks 800px) — Moore tracing
    # na pełnych 1080px byłoby za wolne i za dużo punktów
    target_px = 800
    scale_factor = target_px / max(h, w)
    if scale_factor < 1.0:
        new_w = max(1, int(w * scale_factor))
        new_h = max(1, int(h * scale_factor))
        # Gaussian blur PRZED skalowaniem — wygładza krawędzie
        alpha_pil = PILImage.fromarray(alpha)
        blur_sigma = max(1.5, min(h, w) * 0.003)
        alpha_blurred = alpha_pil.filter(ImageFilter.GaussianBlur(radius=blur_sigma))
        alpha_small = np.array(alpha_blurred.resize((new_w, new_h), PILImage.BILINEAR))
        hs, ws = new_h, new_w
    else:
        scale_factor = 1.0
        alpha_small = alpha
        hs, ws = h, w

    # Threshold per RASTER_CONTOUR_MODE (patrz config.py):
    #   standard — alpha > 50 (domyślne, widoczna treść + biała obwódka)
    #   glow     — alpha > 30 + binary_closing (łączy rozproszoną poświatę)
    #   tight    — alpha > 150 (linia cięcia blisko widocznej treści)
    _contour_mode = getattr(config, "RASTER_CONTOUR_MODE", "standard")
    if _contour_mode == "glow":
        _alpha_thresh = 30
    elif _contour_mode == "tight":
        _alpha_thresh = 150
    else:
        _alpha_thresh = 50
    mask = (alpha_small > _alpha_thresh).astype(np.uint8)

    if _contour_mode == "glow":
        # Binary closing z kernelem proporcjonalnym do rozdzielczości —
        # zamyka przerwy w halo, łączy rozproszone komponenty (gwiazdki,
        # kropki dookoła głównej postaci) w jedną otoczkę.
        from scipy.ndimage import binary_closing
        _closing_iters = max(3, int(min(hs, ws) * 0.01))
        mask = binary_closing(
            mask.astype(bool), iterations=_closing_iters
        ).astype(np.uint8)
        log.debug(f"Raster alpha [glow]: closing iter={_closing_iters}")

    # Przelicznik px (skalowany) → pt
    px_to_pt_x = w_pt / ws
    px_to_pt_y = h_pt / hs

    # Boundary tracing — próba dopasowania okręgu
    boundary_pts = []
    for y in range(hs):
        opaque_cols = np.where(mask[y] > 0)[0]
        if len(opaque_cols) > 0:
            boundary_pts.append([float(opaque_cols[0]), float(y)])
            if opaque_cols[-1] != opaque_cols[0]:
                boundary_pts.append([float(opaque_cols[-1]), float(y)])

    if len(boundary_pts) < 6:
        return None, None

    boundary_arr = np.array(boundary_pts)

    # Próba dopasowania okręgu
    circle = _fit_circle(boundary_arr)
    if circle is not None:
        cx_px, cy_px, r_px = circle
        if _is_circular(boundary_arr, cx_px, cy_px, r_px, tolerance=0.05):
            cx_pt = cx_px * px_to_pt_x
            cy_pt = cy_px * px_to_pt_y
            r_pt = r_px * (px_to_pt_x + px_to_pt_y) / 2
            segments = _circle_to_bezier_segments(cx_pt, cy_pt, r_pt)
            log.info(
                f"Raster alpha kontur: okrąg Bézier, r={r_pt * PT_TO_MM:.1f}mm, "
                f"edge RGB=({edge_rgb[0]:.2f}, {edge_rgb[1]:.2f}, {edge_rgb[2]:.2f})"
            )
            return segments, edge_rgb

    # --- Boundary tracing (Moore / OpenCV — wg config.CONTOUR_ENGINE) ---
    contour_px = _boundary_trace(mask)
    if contour_px is None or len(contour_px) < 6:
        log.warning("Boundary trace: brak konturu, fallback na prostokąt")
        return None, None

    log.debug(f"Boundary trace [{config.CONTOUR_ENGINE}]: {len(contour_px)} raw boundary points")

    # Douglas-Peucker — epsilon proporcjonalny do rozdzielczości
    dp_epsilon = max(4.0, min(hs, ws) * 0.01)
    contour_simplified = _douglas_peucker(contour_px, epsilon=dp_epsilon)

    # Konwertuj px → pt
    pts_pt = np.column_stack([
        contour_simplified[:, 0] * px_to_pt_x,
        contour_simplified[:, 1] * px_to_pt_y,
    ])

    # Tryb konturu: smooth (Bezier) vs sharp (linie proste).
    # smooth: Catmull-Rom + Chaikin → wygladzone krzywe dla logotypow/ilustracji
    # sharp: DP polygon → proste linie dla gwiazdek/strzalek z ostrymi kątami
    if config.RASTER_MODE == "sharp":
        segments = _polygon_to_line_segments(pts_pt)
        seg_label = "linii prostych (sharp)"
    else:
        segments = _polygon_to_smooth_bezier(pts_pt)
        seg_label = "Bézier (smooth)"

    log.info(
        f"Raster alpha kontur: Moore trace → {len(segments)} {seg_label}, "
        f"dp_eps={dp_epsilon:.1f}px, raw={len(contour_px)}pts, "
        f"edge RGB=({edge_rgb[0]:.2f}, {edge_rgb[1]:.2f}, {edge_rgb[2]:.2f})"
    )
    return segments, edge_rgb


def _detect_raster_bg_contour(
    img, w_pt: float, h_pt: float
) -> tuple[list | None, tuple[float, float, float] | None]:
    """Wykrywa kontur z obrazu bez alpha na podstawie jednolitego tła w rogach.

    Metoda: flood-fill z rogów → binary_fill_holes → binary_closing → kontur.
    Niski threshold (10) zapobiega "wyciekaniu" do jasnych obszarów treści.

    Zwraca (cut_segments, edge_rgb) lub (None, None) gdy brak jednolitego tła.
    """
    from scipy.ndimage import label, binary_fill_holes, binary_closing, binary_erosion

    rgb = img.convert('RGB')
    arr = np.array(rgb)
    h, w = arr.shape[:2]

    # Sprawdź rogi (20x20 px) — czy są jednolite
    corner_size = min(20, h // 10, w // 10)
    if corner_size < 3:
        return None, None

    corners = [
        arr[:corner_size, :corner_size],
        arr[:corner_size, -corner_size:],
        arr[-corner_size:, :corner_size],
        arr[-corner_size:, -corner_size:],
    ]

    bg_colors = []
    for c in corners:
        std = c.std(axis=(0, 1))
        if np.max(std) > 10:
            return None, None
        bg_colors.append(c.mean(axis=(0, 1)))

    bg_avg = np.mean(bg_colors, axis=0)
    for bc in bg_colors:
        if np.max(np.abs(bc - bg_avg)) > 15:
            return None, None

    # Skaluj do rozdzielczości roboczej
    target_px = 500
    scale_factor = target_px / max(h, w)
    if scale_factor < 1.0:
        new_w = max(1, int(w * scale_factor))
        new_h = max(1, int(h * scale_factor))
        rgb_small = rgb.resize((new_w, new_h), PILImage.NEAREST)
        arr_s = np.array(rgb_small)
        hs, ws = new_h, new_w
    else:
        scale_factor = 1.0
        arr_s = arr
        hs, ws = h, w

    # Flood-fill z rogów: niski threshold (10) — strict BG matching
    bg_threshold = 10
    diff_s = np.max(np.abs(arr_s.astype(float) - bg_avg[None, None, :]), axis=2)
    is_bg_candidate = diff_s < bg_threshold

    # Znajdź regiony tła połączone z rogami
    labeled, _ = label(is_bg_candidate)
    corner_labels = set()
    for (cy, cx) in [(0, 0), (0, ws - 1), (hs - 1, 0), (hs - 1, ws - 1)]:
        lbl = labeled[cy, cx]
        if lbl > 0:
            corner_labels.add(lbl)

    if not corner_labels:
        return None, None

    is_bg = np.isin(labeled, list(corner_labels))
    is_content = ~is_bg

    # Zamknij dziury wewnętrzne + wygładź krawędzie
    is_content = binary_fill_holes(is_content)
    is_content = binary_closing(is_content, iterations=3)

    # Sprawdź proporcje treść/tło
    content_ratio = is_content.sum() / (hs * ws)
    if content_ratio < 0.1 or content_ratio > 0.99:
        return None, None

    # Granica: erozja → XOR
    edges = is_content.astype(np.uint8) - binary_erosion(is_content).astype(np.uint8)
    ys_b, xs_b = np.where(edges > 0)
    if len(xs_b) < 6:
        return None, None

    boundary_arr = np.column_stack([xs_b.astype(float), ys_b.astype(float)])

    # Kolor krawędzi — sampluj z pikseli na granicy
    edge_colors = arr_s[ys_b, xs_b].astype(float)
    avg_ec = np.mean(edge_colors, axis=0) / 255.0
    edge_rgb = (float(avg_ec[0]), float(avg_ec[1]), float(avg_ec[2]))

    # Przelicznik px → pt
    px_to_pt_x = w_pt / ws
    px_to_pt_y = h_pt / hs

    # Próba dopasowania okręgu
    circle = _fit_circle(boundary_arr)
    if circle is not None:
        cx_px, cy_px, r_px = circle
        if _is_circular(boundary_arr, cx_px, cy_px, r_px, tolerance=0.05):
            cx_pt = cx_px * px_to_pt_x
            cy_pt = cy_px * px_to_pt_y
            r_pt = r_px * (px_to_pt_x + px_to_pt_y) / 2
            segments = _circle_to_bezier_segments(cx_pt, cy_pt, r_pt)
            log.info(
                f"Raster BG kontur: okrąg Bézier, r={r_pt * PT_TO_MM:.1f}mm, "
                f"bg=({bg_avg[0]:.0f},{bg_avg[1]:.0f},{bg_avg[2]:.0f}), "
                f"edge RGB=({edge_rgb[0]:.2f}, {edge_rgb[1]:.2f}, {edge_rgb[2]:.2f})"
            )
            return segments, edge_rgb

    # Fallback: polygon z per-row leftmost/rightmost
    left_pts = []
    right_pts = []
    for y in range(hs):
        content_cols = np.where(is_content[y])[0]
        if len(content_cols) > 0:
            left_pts.append([float(content_cols[0]), float(y)])
            right_pts.append([float(content_cols[-1]), float(y)])

    if not left_pts:
        return None, None

    polygon_px = np.array(left_pts + right_pts[::-1])
    polygon_px = _douglas_peucker(polygon_px, epsilon=1.0)

    # Konwertuj polygon → gładkie krzywe Bézier
    pts_pt = np.column_stack([
        polygon_px[:, 0] * px_to_pt_x,
        polygon_px[:, 1] * px_to_pt_y,
    ])
    segments = _polygon_to_smooth_bezier(pts_pt)

    log.info(
        f"Raster BG kontur: smooth Bézier {len(segments)} segmentów, "
        f"bg=({bg_avg[0]:.0f},{bg_avg[1]:.0f},{bg_avg[2]:.0f}), "
        f"edge RGB=({edge_rgb[0]:.2f}, {edge_rgb[1]:.2f}, {edge_rgb[2]:.2f})"
    )
    return segments, edge_rgb


def _sample_raster_edge_color(img) -> tuple[float, float, float]:
    """Próbkuje średni kolor z krawędzi obrazu (1px obwódka).

    Zwraca (r, g, b) w zakresie 0-1.
    """
    # Konwertuj do RGB (ignoruj alpha)
    rgb = img.convert("RGB")
    px_w, px_h = rgb.size

    if px_w < 2 or px_h < 2:
        # Za mały obraz — zwróć biały
        return (1.0, 1.0, 1.0)

    # Próbkuj piksele z 4 krawędzi (1px border)
    pixels = []
    for x in range(px_w):
        pixels.append(rgb.getpixel((x, 0)))           # góra
        pixels.append(rgb.getpixel((x, px_h - 1)))    # dół
    for y in range(1, px_h - 1):
        pixels.append(rgb.getpixel((0, y)))            # lewo
        pixels.append(rgb.getpixel((px_w - 1, y)))     # prawo

    if not pixels:
        return (1.0, 1.0, 1.0)

    # Średni kolor
    avg_r = sum(p[0] for p in pixels) / len(pixels) / 255.0
    avg_g = sum(p[1] for p in pixels) / len(pixels) / 255.0
    avg_b = sum(p[2] for p in pixels) / len(pixels) / 255.0

    return (avg_r, avg_g, avg_b)


def _get_images_bbox(page: fitz.Page):
    """Zwraca union bounding box wszystkich obrazów na stronie.

    Returns None jeśli brak obrazów.
    """
    img_info = page.get_image_info()
    if not img_info:
        return None

    x0 = min(info['bbox'][0] for info in img_info)
    y0 = min(info['bbox'][1] for info in img_info)
    x1 = max(info['bbox'][2] for info in img_info)
    y1 = max(info['bbox'][3] for info in img_info)

    rect = fitz.Rect(x0, y0, x1, y1)
    if rect.is_empty or rect.width < 1 or rect.height < 1:
        return None
    return rect


def _get_text_bbox(page: fitz.Page):
    """Zwraca union bounding box bloków tekstowych (fonty) na stronie.

    Tekst renderowany czcionką NIE pojawia się w get_drawings() — musimy
    doliczyć go osobno, inaczej grafika z dużym tekstem (np. Affinity/Word)
    zostanie przycięta do bboxu samych drawings. Returns None jeśli brak tekstu.
    """
    try:
        blocks = page.get_text("blocks")
    except Exception:
        return None
    rects = []
    for b in blocks:
        if len(b) < 4:
            continue
        r = fitz.Rect(b[0], b[1], b[2], b[3])
        if r.is_empty or r.width < 0.5 or r.height < 0.5:
            continue
        rects.append(r)
    if not rects:
        return None
    return fitz.Rect(
        min(r.x0 for r in rects), min(r.y0 for r in rects),
        max(r.x1 for r in rects), max(r.y1 for r in rects),
    )


def _compute_content_rect(page: fitz.Page, drawings: list) -> fitz.Rect:
    """Oblicza bbox zawartości strony (drawings + images + text) ∩ page.rect.

    Clamp do page.rect jest KRYTYCZNY: np. Canva eksportuje pliki z tekstem
    klipowanym przez stronę, ale bbox tekstu wychodzi daleko poza MediaBox.
    Bez clampu content_rect byłby > page i psuł heurystyki artwork-on-artboard
    oraz outermost-is-fragment.
    """
    rects = [d['rect'] for d in drawings]
    ib = _get_images_bbox(page)
    if ib is not None:
        rects.append(ib)
    tb = _get_text_bbox(page)
    if tb is not None:
        rects.append(tb)
    if not rects:
        return fitz.Rect(page.rect)
    union = fitz.Rect(
        min(r.x0 for r in rects), min(r.y0 for r in rects),
        max(r.x1 for r in rects), max(r.y1 for r in rects),
    )
    union &= page.rect
    return union


def _is_artwork_on_artboard(page: fitz.Page, artwork_rect: fitz.Rect,
                             max_ratio: float = 0.6) -> bool:
    """Sprawdza czy grafika jest znacznie mniejsza od strony (= artwork na artboardzie)."""
    page_area = page.rect.width * page.rect.height
    art_area = artwork_rect.width * artwork_rect.height
    if page_area < 1:
        return False
    return art_area / page_area < max_ratio


def _fit_circle(points: np.ndarray) -> tuple[float, float, float] | None:
    """Dopasowuje okrąg do zbioru punktów 2D metodą najmniejszych kwadratów.

    Returns (cx, cy, radius) lub None jeśli dopasowanie się nie udało.
    """
    if len(points) < 3:
        return None

    x = points[:, 0]
    y = points[:, 1]

    # Linearyzacja: 2*cx*x + 2*cy*y + (r² - cx² - cy²) = x² + y²
    A = np.column_stack([2 * x, 2 * y, np.ones(len(x))])
    b = x ** 2 + y ** 2

    try:
        result, _, _, _ = np.linalg.lstsq(A, b, rcond=None)
    except np.linalg.LinAlgError:
        return None

    cx, cy, c = result
    val = c + cx ** 2 + cy ** 2
    if val <= 0:
        return None
    r = np.sqrt(val)
    return float(cx), float(cy), float(r)


def _is_circular(points: np.ndarray, cx: float, cy: float, r: float,
                  tolerance: float = 0.05) -> bool:
    """Sprawdza czy punkty leżą na okręgu (w granicy tolerance * r)."""
    distances = np.sqrt((points[:, 0] - cx) ** 2 + (points[:, 1] - cy) ** 2)
    max_deviation = np.max(np.abs(distances - r))
    return max_deviation / r < tolerance


def make_crop_shape_contour(w_pt: float, h_pt: float, shape: str,
                             radius_pct: int = 9) -> list:
    """Zwraca dokładne cut_segments dla kształtu crop: square/rounded/circle/oval.

    Używane gdy user świadomie wybiera kształt w GUI Crop — pomijamy raster
    boundary tracing (który po DP + smooth Bezier psuje zaokrąglone rogi,
    interpretując rounded-rect jako okrąg).

    Segmenty w y-down coords, origin (0,0), wymiary w punktach PDF.
    """
    if shape == "circle":
        r = min(w_pt, h_pt) / 2.0
        return _circle_to_bezier_segments(r, r, r)
    if shape == "oval":
        rx = w_pt / 2.0
        ry = h_pt / 2.0
        k = 0.5522847498
        kx = k * rx
        ky = k * ry
        cx, cy = rx, ry
        return [
            ('c',
             np.array([cx + rx, cy]), np.array([cx + rx, cy + ky]),
             np.array([cx + kx, cy + ry]), np.array([cx, cy + ry])),
            ('c',
             np.array([cx, cy + ry]), np.array([cx - kx, cy + ry]),
             np.array([cx - rx, cy + ky]), np.array([cx - rx, cy])),
            ('c',
             np.array([cx - rx, cy]), np.array([cx - rx, cy - ky]),
             np.array([cx - kx, cy - ry]), np.array([cx, cy - ry])),
            ('c',
             np.array([cx, cy - ry]), np.array([cx + kx, cy - ry]),
             np.array([cx + rx, cy - ky]), np.array([cx + rx, cy])),
        ]
    if shape == "rounded":
        r = min(w_pt, h_pt) * radius_pct / 100.0
        r = max(0.1, min(r, min(w_pt, h_pt) / 2.0))
        k = 0.5522847498
        kr = k * r
        # Rounded rectangle: 4 linie proste + 4 łuki Bézier (clockwise, y-down)
        # Punkty narożników (on-curve):
        #   TL: (r, 0) ← (0, r);  TR: (w-r, 0) → (w, r)
        #   BR: (w, h-r) → (w-r, h);  BL: (r, h) ← (0, h-r)
        return [
            # Górny bok
            ('l', np.array([r, 0.0]), np.array([w_pt - r, 0.0])),
            # Róg TR
            ('c',
             np.array([w_pt - r, 0.0]),
             np.array([w_pt - r + kr, 0.0]),
             np.array([w_pt, r - kr]),
             np.array([w_pt, r])),
            # Prawy bok
            ('l', np.array([w_pt, r]), np.array([w_pt, h_pt - r])),
            # Róg BR
            ('c',
             np.array([w_pt, h_pt - r]),
             np.array([w_pt, h_pt - r + kr]),
             np.array([w_pt - r + kr, h_pt]),
             np.array([w_pt - r, h_pt])),
            # Dolny bok
            ('l', np.array([w_pt - r, h_pt]), np.array([r, h_pt])),
            # Róg BL
            ('c',
             np.array([r, h_pt]),
             np.array([r - kr, h_pt]),
             np.array([0.0, h_pt - r + kr]),
             np.array([0.0, h_pt - r])),
            # Lewy bok
            ('l', np.array([0.0, h_pt - r]), np.array([0.0, r])),
            # Róg TL
            ('c',
             np.array([0.0, r]),
             np.array([0.0, r - kr]),
             np.array([r - kr, 0.0]),
             np.array([r, 0.0])),
        ]
    # square / default
    return _make_page_rect_contour(fitz.Rect(0, 0, w_pt, h_pt))


def _circle_to_bezier_segments(cx: float, cy: float, r: float) -> list:
    """Generuje 4 krzywe Bézier aproksymujące okrąg.

    Standardowa aproksymacja: k ≈ 0.5523 (4*(√2-1)/3).
    Koordynaty w y-down (fitz).
    """
    k = 0.5522847498  # 4*(sqrt(2)-1)/3
    kr = k * r

    # 4 ćwiartki: prawo → dół → lewo → góra → prawo (y-down, clockwise)
    return [
        ('c',
         np.array([cx + r, cy]),
         np.array([cx + r, cy + kr]),
         np.array([cx + kr, cy + r]),
         np.array([cx, cy + r])),
        ('c',
         np.array([cx, cy + r]),
         np.array([cx - kr, cy + r]),
         np.array([cx - r, cy + kr]),
         np.array([cx - r, cy])),
        ('c',
         np.array([cx - r, cy]),
         np.array([cx - r, cy - kr]),
         np.array([cx - kr, cy - r]),
         np.array([cx, cy - r])),
        ('c',
         np.array([cx, cy - r]),
         np.array([cx + kr, cy - r]),
         np.array([cx + r, cy - kr]),
         np.array([cx + r, cy])),
    ]


def _render_alpha_contour(doc: fitz.Document, page_index: int,
                           clip_rect: fitz.Rect) -> tuple[list, tuple]:
    """Renderuje obszar grafiki z alpha i wyodrębnia kontur + kolor krawędzi.

    Jeśli kształt jest okrągły → zwraca 4 krzywe Bézier (idealny okrąg).
    W przeciwnym razie → polygon z linii.
    Segmenty w koordynatach relatywnych do (0,0) clip_rect.
    """
    page = doc[page_index]

    # Render na umiarkowanej rozdzielczości
    target_px = 500
    scale = target_px / max(clip_rect.width, clip_rect.height)
    mat = fitz.Matrix(scale, scale)

    pix = page.get_pixmap(matrix=mat, clip=clip_rect, alpha=True)
    data = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 4)
    alpha = data[:, :, 3]
    rgb = data[:, :, :3]

    h, w = alpha.shape

    # Tryby inne niż "standard" przekierowujemy na pipeline Moore trace
    # z `_detect_raster_alpha_contour` (opakowujemy pixmap jako PIL).
    # Standardowy tryb zachowuje legacy row-scan (nic nie psujemy).
    _contour_mode = getattr(config, "RASTER_CONTOUR_MODE", "standard")
    if _contour_mode in ("glow", "tight"):
        img_pil = PILImage.frombytes("RGBA", (pix.width, pix.height), pix.samples)
        segs, rgb_edge = _detect_raster_alpha_contour(
            img_pil, clip_rect.width, clip_rect.height
        )
        if segs is not None:
            return segs, rgb_edge
        # fallback — w standardowym kodzie zwraca prostokąt gdy nie ma konturu

    threshold = 128

    # Dla każdego wiersza: lewy i prawy piksel opaque
    boundary_pts = []
    edge_colors = []

    for y in range(h):
        opaque_cols = np.where(alpha[y] > threshold)[0]
        if len(opaque_cols) > 0:
            lx = int(opaque_cols[0])
            rx = int(opaque_cols[-1])
            boundary_pts.append([float(lx), float(y)])
            if rx != lx:
                boundary_pts.append([float(rx), float(y)])
            # Próbkuj kolory z pikseli granicznych
            edge_colors.append(rgb[y, lx].astype(float))
            edge_colors.append(rgb[y, rx].astype(float))

    if len(boundary_pts) < 6:
        edge_rgb = _sample_pdf_page_edge_color(doc, page_index)
        rect_local = fitz.Rect(0, 0, clip_rect.width, clip_rect.height)
        return _make_page_rect_contour(rect_local), edge_rgb

    boundary_arr = np.array(boundary_pts)

    # Kolor krawędzi z pikseli granicznych
    if edge_colors:
        avg = np.mean(edge_colors, axis=0) / 255.0
        edge_rgb = (float(avg[0]), float(avg[1]), float(avg[2]))
    else:
        edge_rgb = (1.0, 1.0, 1.0)

    # Próba dopasowania okręgu
    circle = _fit_circle(boundary_arr)
    if circle is not None:
        cx_px, cy_px, r_px = circle
        if _is_circular(boundary_arr, cx_px, cy_px, r_px, tolerance=0.05):
            # Okrąg! Generuj Bézier w pt (relatywnie do 0,0)
            cx_pt = cx_px / scale
            cy_pt = cy_px / scale
            r_pt = r_px / scale
            segments = _circle_to_bezier_segments(cx_pt, cy_pt, r_pt)
            log.info(
                f"Kontur: okrąg Bézier, r={r_pt * PT_TO_MM:.1f}mm, "
                f"edge RGB=({edge_rgb[0]:.2f}, {edge_rgb[1]:.2f}, {edge_rgb[2]:.2f})"
            )
            return segments, edge_rgb

    # Fallback: polygon z linii (Douglas-Peucker)
    left_pts = []
    right_pts = []
    for y in range(h):
        opaque_cols = np.where(alpha[y] > threshold)[0]
        if len(opaque_cols) > 0:
            left_pts.append([float(opaque_cols[0]), float(y)])
            right_pts.append([float(opaque_cols[-1]), float(y)])

    polygon_px = np.array(left_pts + right_pts[::-1])
    polygon_px = _douglas_peucker(polygon_px, epsilon=1.0)

    # Konwertuj polygon → gładkie krzywe Bézier
    pts_pt = np.column_stack([
        polygon_px[:, 0] / scale,
        polygon_px[:, 1] / scale,
    ])
    segments = _polygon_to_smooth_bezier(pts_pt)

    log.info(
        f"Kontur: smooth Bézier {len(segments)} segmentów, "
        f"edge RGB=({edge_rgb[0]:.2f}, {edge_rgb[1]:.2f}, {edge_rgb[2]:.2f})"
    )
    return segments, edge_rgb


def _douglas_peucker(points: np.ndarray, epsilon: float) -> np.ndarray:
    """Upraszczanie wielokąta algorytmem Douglas-Peucker."""
    if len(points) <= 2:
        return points
    return _dp_recursive(points, epsilon)


def _dp_recursive(pts: np.ndarray, epsilon: float) -> np.ndarray:
    """Rekurencyjne upraszczanie DP."""
    if len(pts) <= 2:
        return pts

    start = pts[0]
    end = pts[-1]
    line_vec = end - start
    line_len = np.linalg.norm(line_vec)

    if line_len < 1e-10:
        return pts[[0, -1]]

    line_unit = line_vec / line_len

    # Odległość prostopadła każdego pośredniego punktu od linii
    diffs = pts[1:-1] - start
    cross = np.abs(diffs[:, 0] * line_unit[1] - diffs[:, 1] * line_unit[0])

    max_idx = np.argmax(cross) + 1  # +1 bo pominęliśmy pierwszy punkt
    max_dist = cross[max_idx - 1]

    if max_dist > epsilon:
        left = _dp_recursive(pts[:max_idx + 1], epsilon)
        right = _dp_recursive(pts[max_idx:], epsilon)
        return np.vstack([left[:-1], right])
    else:
        return pts[[0, -1]]


def _polygon_to_line_segments(pts: np.ndarray, min_dist_pt: float = 4.0) -> list:
    """Konwertuje zamkniety polygon na proste linie (bez wygladzania).

    Uzywane w RASTER_MODE=sharp dla ksztaltow z ostrymi narozami (gwiazdki,
    strzalki, diamenty). Zachowuje geometrie 1:1 z Douglas-Peucker wynikiem —
    bez Chaikin corner cutting, bez Catmull-Rom Bezier.

    Args:
        pts: punkty polygonu w pt (N x 2) — wynik Douglas-Peucker
        min_dist_pt: minimalna odleglosc miedzy punktami (pt) — filtruje
            mikro-segmenty z antialiasingu. Domyslnie 4pt (≈1.4mm) — mniejsze
            niz smooth (18pt) bo nie zaokraglamy i chcemy zachowac detale.

    Returns:
        lista segmentow ('l', p0, p1) tworzących zamknięty polygon.
    """
    if len(pts) > 3:
        filtered = [pts[0]]
        for i in range(1, len(pts)):
            dist = np.linalg.norm(pts[i] - filtered[-1])
            if dist >= min_dist_pt:
                filtered.append(pts[i])
        if len(filtered) > 2 and np.linalg.norm(filtered[-1] - filtered[0]) < min_dist_pt:
            filtered.pop()
        pts = np.array(filtered)

    n = len(pts)
    segments = []
    for i in range(n):
        p0 = pts[i]
        p1 = pts[(i + 1) % n]
        segments.append(('l', np.array(p0), np.array(p1)))
    return segments


def _polygon_to_smooth_bezier(pts: np.ndarray, min_dist_pt: float = 18.0) -> list:
    """Konwertuje zamknięty polygon na gładkie krzywe Bézier (Catmull-Rom → cubic).

    1. Filtruje punkty zbyt blisko siebie (< min_dist_pt)
    2. Generuje N segmentów ('c', p0, cp1, cp2, p1) — zamknięta krzywa C1-ciągła

    Catmull-Rom → cubic Bézier:
      cp1 = P[i]   + (P[i+1] - P[i-1]) / 6
      cp2 = P[i+1] - (P[i+2] - P[i])   / 6

    Args:
        pts: punkty polygonu w pt (N x 2)
        min_dist_pt: minimalna odległość między punktami (pt) — ~3.5mm
    """
    # Filtruj punkty zbyt blisko siebie — eliminuje ostre mikro-załamania
    if len(pts) > 4:
        filtered = [pts[0]]
        for i in range(1, len(pts)):
            dist = np.linalg.norm(pts[i] - filtered[-1])
            if dist >= min_dist_pt:
                filtered.append(pts[i])
        # Sprawdź odległość ostatni → pierwszy
        if len(filtered) > 2 and np.linalg.norm(filtered[-1] - filtered[0]) < min_dist_pt:
            filtered.pop()
        pts = np.array(filtered)

    # Chaikin's corner cutting — wygładza narożniki polygonu.
    # 2 iteracje: każdy punkt zastąpiony parą 75%/25% z sąsiadem.
    # Potem min_dist filter redukuje nadmiar punktów.
    if len(pts) > 4:
        for _ in range(2):
            n_ch = len(pts)
            new_pts = []
            for i in range(n_ch):
                p0 = pts[i]
                p1 = pts[(i + 1) % n_ch]
                new_pts.append(0.75 * p0 + 0.25 * p1)
                new_pts.append(0.25 * p0 + 0.75 * p1)
            pts = np.array(new_pts)
        # Re-filter: większy min_dist po Chaikinie (2x) → ~20-25 segmentów
        chaikin_min_dist = min_dist_pt * 2
        filtered = [pts[0]]
        for i in range(1, len(pts)):
            if np.linalg.norm(pts[i] - filtered[-1]) >= chaikin_min_dist:
                filtered.append(pts[i])
        if len(filtered) > 2 and np.linalg.norm(filtered[-1] - filtered[0]) < chaikin_min_dist:
            filtered.pop()
        pts = np.array(filtered)

    n = len(pts)
    if n < 3:
        segments = []
        for i in range(n):
            p0 = pts[i]
            p1 = pts[(i + 1) % n]
            segments.append(('l', np.array(p0), np.array(p1)))
        return segments

    segments = []
    for i in range(n):
        p_prev = pts[(i - 1) % n]
        p_curr = pts[i]
        p_next = pts[(i + 1) % n]
        p_next2 = pts[(i + 2) % n]

        # Catmull-Rom tangent → Bézier control points
        cp1 = p_curr + (p_next - p_prev) / 6.0
        cp2 = p_next - (p_next2 - p_curr) / 6.0

        segments.append((
            'c',
            np.array(p_curr),
            np.array(cp1),
            np.array(cp2),
            np.array(p_next),
        ))

    return segments


def _sample_pdf_page_edge_color(
    doc: fitz.Document, page_index: int
) -> tuple[float, float, float]:
    """Próbkuje kolor krawędzi ze zrenderowanej strony PDF.

    Renderuje stronę na niskiej rozdzielczości i próbkuje piksele z krawędzi.
    Zwraca (r, g, b) w zakresie 0-1.
    """
    page = doc[page_index]
    # Niska rozdzielczość — wystarczy do próbkowania koloru krawędzi
    zoom = 72.0 / 72.0  # 72 DPI
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    img = PILImage.frombytes("RGB", (pix.width, pix.height), pix.samples)
    edge_rgb = _sample_raster_edge_color(img)
    img.close()
    return edge_rgb


def _sample_page_edge_color(page: fitz.Page,
                             clip: fitz.Rect | None = None
                             ) -> tuple[float, float, float]:
    """Sampluje dominujący kolor krawędzi renderowanej strony (lub clip).

    Renderuje obszar na 72 DPI i uśrednia piksele z 2px obramowania.
    Gdy `clip` podany — sampluje krawędzie tego podobszaru (np. artwork_rect
    dla artwork-on-artboard), nie całej strony. Zwraca (r, g, b) w zakresie 0-1.
    """
    if clip is not None:
        pix = page.get_pixmap(dpi=72, alpha=False, clip=clip)
    else:
        pix = page.get_pixmap(dpi=72, alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    h, w = arr.shape[:2]
    b = 2  # 2px border

    # Zbierz piksele z krawędzi (górna, dolna, lewa, prawa)
    edges = np.concatenate([
        arr[:b, :].reshape(-1, 3),      # góra
        arr[-b:, :].reshape(-1, 3),     # dół
        arr[b:-b, :b].reshape(-1, 3),   # lewa
        arr[b:-b, -b:].reshape(-1, 3),  # prawa
    ], axis=0)

    # Mediana (odporna na outliery z dekoracji)
    median = np.median(edges, axis=0)
    return (median[0] / 255.0, median[1] / 255.0, median[2] / 255.0)


def _make_page_rect_contour(page_rect: fitz.Rect) -> list:
    """Tworzy prostokątny kontur cięcia z page.rect (dla plików ze spadami).

    Pliki ze spadami nie mają wewnętrznego konturu — kontur = granica TrimBox.
    """
    tl = np.array([page_rect.x0, page_rect.y0])
    tr = np.array([page_rect.x1, page_rect.y0])
    br = np.array([page_rect.x1, page_rect.y1])
    bl = np.array([page_rect.x0, page_rect.y1])
    return [
        ('l', tl, tr), ('l', tr, br),
        ('l', br, bl), ('l', bl, tl),
    ]


# =============================================================================
# SKALOWANIE STICKERA
# =============================================================================

def scale_sticker(sticker: Sticker, target_height_mm: float) -> Sticker:
    """Skaluje naklejkę proporcjonalnie do docelowej wysokości (mm).

    Skaluje: width_mm, height_mm, page_width_pt, page_height_pt,
    cut_segments (wszystkie punkty w pt).

    Args:
        sticker: Sticker z wypełnionymi polami konturu
        target_height_mm: docelowa wysokość w mm (bez spadu)

    Returns:
        Sticker ze zaktualizowanymi wymiarami i konturem
    """
    if sticker.height_mm <= 0:
        log.warning("scale_sticker: height_mm <= 0, pomijam skalowanie")
        return sticker

    scale = target_height_mm / sticker.height_mm
    if abs(scale - 1.0) < 0.001:
        # Brak zmiany
        return sticker

    old_w = sticker.width_mm
    old_h = sticker.height_mm
    sticker.width_mm = old_w * scale
    sticker.height_mm = target_height_mm
    sticker.page_width_pt = sticker.page_width_pt * scale
    sticker.page_height_pt = sticker.page_height_pt * scale

    # Skaluj cut_segments — wszystkie punkty (numpy arrays) w pt
    sticker.cut_segments = _scale_segments(sticker.cut_segments, scale)

    log.info(
        f"Skalowanie: {old_w:.1f}x{old_h:.1f}mm → "
        f"{sticker.width_mm:.1f}x{sticker.height_mm:.1f}mm "
        f"(skala {scale:.3f})"
    )
    return sticker


def _scale_segments(segments: list, scale: float) -> list:
    """Skaluje segmenty konturu (linie i krzywe Bézier) o współczynnik scale."""
    scaled = []
    for seg in segments:
        seg_type = seg[0]
        if seg_type == 'l':
            # Linia: ('l', start, end)
            scaled.append(('l', seg[1] * scale, seg[2] * scale))
        elif seg_type == 'c':
            # Bézier: ('c', p0, p1, p2, p3)
            scaled.append(('c',
                           seg[1] * scale, seg[2] * scale,
                           seg[3] * scale, seg[4] * scale))
        else:
            # Nieznany typ — przepuść bez zmian
            scaled.append(seg)
    return scaled


def _page_is_cmyk(doc: fitz.Document, page: fitz.Page) -> bool:
    """Sprawdza czy strona PDF używa DeviceCMYK (operator k/K w content stream)."""
    try:
        contents = bytearray()
        for xref in page.get_contents():
            contents += doc.xref_stream(xref)
        cs = contents.decode('latin-1', errors='replace')
        # Szukaj operatora k (CMYK fill) — 4 liczby + k
        return bool(_re.search(r'[\d.]+\s+[\d.]+\s+[\d.]+\s+[\d.]+\s+k\b', cs))
    except Exception:
        return False


# =============================================================================
# DETEKCJA CutContour Z PLIKU BLEED OUTPUT
# =============================================================================

def _extract_cutcontour_segments(doc: fitz.Document, page_idx: int) -> list | None:
    """Wyciąga segmenty CutContour z content stream strony PDF.

    Pliki wygenerowane przez bleed pipeline mają CutContour jako osobny
    content stream z komendami: m (moveto), l (lineto), c (curveto), S (stroke).

    Returns:
        list of segments [('c', p0, cp1, cp2, p3), ('l', start, end), ...]
        lub None jeśli brak CutContour.
    """
    page = doc[page_idx]
    contents = page.get_contents()

    for xref in contents:
        data = doc.xref_stream(xref)
        if data is None or b'CutContour' not in data:
            continue

        text = data.decode('latin-1', errors='replace')
        segments = []
        current_x, current_y = 0.0, 0.0

        for line in text.split('\n'):
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            op = parts[-1]

            if op == 'm' and len(parts) >= 3:
                # moveto: x y m
                current_x = float(parts[0])
                current_y = float(parts[1])
            elif op == 'l' and len(parts) >= 3:
                # lineto: x y l
                x, y = float(parts[0]), float(parts[1])
                segments.append((
                    'l',
                    np.array([current_x, current_y]),
                    np.array([x, y]),
                ))
                current_x, current_y = x, y
            elif op == 'c' and len(parts) >= 7:
                # curveto: x1 y1 x2 y2 x3 y3 c
                x1, y1 = float(parts[0]), float(parts[1])
                x2, y2 = float(parts[2]), float(parts[3])
                x3, y3 = float(parts[4]), float(parts[5])
                segments.append((
                    'c',
                    np.array([current_x, current_y]),
                    np.array([x1, y1]),
                    np.array([x2, y2]),
                    np.array([x3, y3]),
                ))
                current_x, current_y = x3, y3

        if segments:
            log.info(f"CutContour wykryty: {len(segments)} segmentów z content stream")
            return segments

    return None


# =============================================================================
# GŁÓWNA FUNKCJA: detect_contour
# =============================================================================

def _prepare_pdf_path(pdf_path: str) -> tuple[str, str | None, list | None, float | None, float | None]:
    """Konwersja EPS/SVG → tmp PDF + opcjonalny kontur z SVG clipPath.

    Używa file_loader.to_pdf() do konwersji formatu i file_loader.svg_dimensions_from_name
    do wymiarów SVG. Ekstrakcja SVG clipPath pozostaje tutaj (specyficzna dla contour).

    Returns:
        (pdf_path, tmp_pdf, svg_contour, svg_w_mm, svg_h_mm)
    """
    # Dla SVG zachowaj oryginalną ścieżkę do ekstrakcji clipPath i wymiarów
    svg_contour = None
    svg_w_mm = None
    svg_h_mm = None
    original_svg_path = pdf_path if detect_type(pdf_path) == FileType.SVG else None

    # Konwersja formatu (PDF → PDF, EPS/SVG → tmp PDF)
    pdf_path, tmp_pdf = to_pdf(pdf_path)

    # SVG: dodatkowo wymiary z nazwy + kontur z clipPath
    if original_svg_path is not None:
        size = svg_dimensions_from_name(original_svg_path)
        if size is not None:
            svg_w_mm, svg_h_mm = size
            svg_contour = extract_svg_contour(original_svg_path, svg_w_mm, svg_h_mm)
            if svg_contour:
                log.info(f"SVG contour: {len(svg_contour)} segmentów z clipPath")

    return pdf_path, tmp_pdf, svg_contour, svg_w_mm, svg_h_mm


def _build_sticker_from_cutcontour(
    doc: fitz.Document,
    page_idx: int,
    cutcontour_segs: list,
    original_path: str,
) -> Sticker:
    """Buduje Sticker z PDF bleed-output (re-ingest).

    PDF zawiera już spot color CutContour — używamy go jako konturu cięcia.
    Segmenty są w koordynatach strony z offsetem bleed, przesuwamy do (0,0).
    """
    page = doc[page_idx]

    # Wymiary stickera = bbox CutContour (on-curve only)
    oncurve = []
    for seg in cutcontour_segs:
        if seg[0] == 'l':
            oncurve.extend([seg[1], seg[2]])
        elif seg[0] == 'c':
            oncurve.extend([seg[1], seg[4]])
    oncurve_arr = np.array(oncurve)
    cut_min = oncurve_arr.min(axis=0)
    cut_max = oncurve_arr.max(axis=0)
    cut_w = cut_max[0] - cut_min[0]
    cut_h = cut_max[1] - cut_min[1]

    # Przesuń segmenty do origin (0,0)
    offset = cut_min
    shifted = []
    for seg in cutcontour_segs:
        if seg[0] == 'l':
            shifted.append(('l', seg[1] - offset, seg[2] - offset))
        elif seg[0] == 'c':
            shifted.append(('c',
                seg[1] - offset, seg[2] - offset,
                seg[3] - offset, seg[4] - offset))

    w_mm = cut_w * PT_TO_MM
    h_mm = cut_h * PT_TO_MM

    sticker = Sticker(
        source_path=original_path,
        page_index=page_idx,
        width_mm=w_mm,
        height_mm=h_mm,
        cut_segments=shifted,
        pdf_doc=doc,
        page_width_pt=cut_w,
        page_height_pt=cut_h,
        outermost_drawing_idx=None,
        edge_color_rgb=_sample_pdf_page_edge_color(doc, page_idx),
        is_bleed_output=True,
        is_cmyk=_page_is_cmyk(doc, page),
    )
    log.info(
        f"Sticker p{page_idx + 1}: {w_mm:.1f}x{h_mm:.1f}mm, "
        f"CutContour z bleed output, {len(shifted)} segmentów"
    )
    return sticker


def _render_page_to_tmp_raster(doc: fitz.Document, page_idx: int,
                                dpi: int = 300) -> str:
    """Renderuje stronę (page.rect) na tmp PNG dla malformed Canva TrimBox.

    Fitz normalizuje content stream z MediaBox (z offsetem) do page.rect
    (0,0,w,h) — renderowanie clip=page.rect daje pełny content prawidłowo
    wyśrodkowany. UŻYCIE clip=mediabox jest pułapką: fitz klampuje clip do
    page.rect (bo mediabox ma offset poza page.rect), co obcina content.
    """
    import tempfile
    page = doc[page_idx]
    xref = page.xref
    # Fitz klampuje clip do page.rect (intersection CropBox ∩ MediaBox).
    # Canva malformed ma TB wychodzący poza MB, więc page.rect to intersection
    # — co obcina content. Rozwiązanie: skopiuj stronę do nowego dokumentu
    # bez CropBox, wtedy page.rect == MB i render obejmuje pełen content.
    tmp_doc = fitz.open()
    tmp_doc.insert_pdf(doc, from_page=page_idx, to_page=page_idx)
    tmp_page = tmp_doc[0]
    # Usuń wszystkie boxy poza MediaBox w kopii — żeby page.rect = MB
    for box in ("CropBox", "TrimBox", "ArtBox", "BleedBox"):
        tmp_doc.xref_set_key(tmp_page.xref, box, "null")
    tmp_page = tmp_doc.reload_page(tmp_page)
    pix = tmp_page.get_pixmap(dpi=dpi, alpha=False)
    tmp_doc.close()

    tmp = tempfile.NamedTemporaryFile(
        suffix='.png', delete=False, prefix='bleed_malformed_'
    )
    tmp_name = tmp.name
    tmp.close()  # zamknij handle przed save (Windows: permission denied)
    pix.save(tmp_name)
    return tmp_name


def _build_sticker_from_malformed_trimbox(
    doc: fitz.Document,
    page_idx: int,
    original_path: str,
) -> Sticker:
    """Buduje Sticker dla malformed Canva (TB z tymi samymi wymiarami co MB).

    Renderuje page.rect jako raster i przekierowuje na ścieżkę raster
    w eksport.py (dilation + insert_image). Dzięki temu:
      - treść wyśrodkowana (fitz normalizuje content stream)
      - bleed rozciąga kolory krawędzi niezależnie od warstw wektora
    """
    raster_path = _render_page_to_tmp_raster(doc, page_idx, dpi=300)
    page = doc[page_idx]
    # Wymiary stickera = wymiary RAW MediaBox (zawartość streamu Canvy).
    # page.rect może być znormalizowany do CropBox (TrimBox w malformed).
    raw_mb = doc.xref_get_key(page.xref, "MediaBox")
    mb_values = [float(x) for x in raw_mb[1].strip("[] ").split()]
    aw = mb_values[2] - mb_values[0]
    ah = mb_values[3] - mb_values[1]
    w_mm = aw * PT_TO_MM
    h_mm = ah * PT_TO_MM
    cut_segments = [
        ('l', np.array([0.0, 0.0]), np.array([aw, 0.0])),
        ('l', np.array([aw, 0.0]), np.array([aw, ah])),
        ('l', np.array([aw, ah]), np.array([0.0, ah])),
        ('l', np.array([0.0, ah]), np.array([0.0, 0.0])),
    ]
    sticker = Sticker(
        source_path=original_path,
        page_index=page_idx,
        width_mm=w_mm,
        height_mm=h_mm,
        cut_segments=cut_segments,
        raster_path=raster_path,
        pdf_doc=None,
        page_width_pt=aw,
        page_height_pt=ah,
        outermost_drawing_idx=None,
    )
    log.info(
        f"Sticker p{page_idx + 1}: {w_mm:.1f}x{h_mm:.1f}mm, "
        f"malformed TrimBox → raster fallback ({raster_path})"
    )
    return sticker


def _build_sticker_from_raster_only_pdf(
    doc: fitz.Document,
    page_idx: int,
    original_path: str,
) -> Sticker | None:
    """Buduje Sticker ze strony PDF zawierającej tylko obrazy rastrowe.

    Wywoływane gdy validate_page() rzucił ValueError (brak wektorów).
    Jeśli strona ma obrazy — wykrywa kontur z renderingu z alpha lub używa
    prostokąta strony. Zwraca None gdy strona nie ma ani wektorów, ani obrazów.
    """
    page = doc[page_idx]
    images = page.get_images()
    if not images:
        log.warning(f"Strona {page_idx + 1} pominieta: brak wektorów i obrazów")
        return None

    log.info(
        f"Strona {page_idx + 1}: brak wektorów, ale {len(images)} obraz(ów) "
        f"rastrowych — traktowanie jako raster-only PDF"
    )

    # Sprawdź czy grafika jest mniejsza od strony (artwork na artboardzie)
    images_bbox = _get_images_bbox(page)
    artwork_on_artboard = (
        images_bbox is not None
        and _is_artwork_on_artboard(page, images_bbox)
    )

    if artwork_on_artboard:
        # Grafika mniejsza od strony — przytnij do obszaru grafiki
        artwork_rect = fitz.Rect(
            images_bbox.x0 - 1, images_bbox.y0 - 1,
            images_bbox.x1 + 1, images_bbox.y1 + 1,
        )
        artwork_rect &= page.rect  # nie wychodź poza stronę

        log.info(
            f"Strona {page_idx + 1}: grafika "
            f"{artwork_rect.width * PT_TO_MM:.1f}x"
            f"{artwork_rect.height * PT_TO_MM:.1f}mm na stronie "
            f"{page.rect.width * PT_TO_MM:.1f}x"
            f"{page.rect.height * PT_TO_MM:.1f}mm"
        )

        # Wykryj kontur z renderingu z alpha
        cut_segments, edge_rgb = _render_alpha_contour(
            doc, page_idx, artwork_rect
        )

        # Przytnij stronę do obszaru grafiki (dla eksportu)
        page.set_cropbox(artwork_rect)

        page_w_pt = artwork_rect.width
        page_h_pt = artwork_rect.height
    else:
        # Grafika wypełnia stronę — prostokąt z page.rect
        page_w_pt = page.rect.width
        page_h_pt = page.rect.height
        cut_segments = _make_page_rect_contour(
            fitz.Rect(0, 0, page_w_pt, page_h_pt)
        )
        edge_rgb = _sample_pdf_page_edge_color(doc, page_idx)

    w_mm = page_w_pt * PT_TO_MM
    h_mm = page_h_pt * PT_TO_MM

    sticker = Sticker(
        source_path=original_path,
        page_index=page_idx,
        width_mm=w_mm,
        height_mm=h_mm,
        cut_segments=cut_segments,
        pdf_doc=doc,
        page_width_pt=page_w_pt,
        page_height_pt=page_h_pt,
        outermost_drawing_idx=None,
        edge_color_rgb=edge_rgb,
        is_cmyk=_page_is_cmyk(doc, page),
    )
    log.info(
        f"Sticker p{page_idx + 1}: {w_mm:.1f}x{h_mm:.1f}mm, "
        f"raster-only PDF, {len(cut_segments)} segmentów konturu, "
        f"edge RGB=({edge_rgb[0]:.2f}, {edge_rgb[1]:.2f}, {edge_rgb[2]:.2f})"
    )
    return sticker


def _build_sticker_from_vector(
    doc: fitz.Document,
    page_idx: int,
    page: fitz.Page,
    drawings: list,
    cropped_pages: set,
    svg_contour: list | None,
    svg_w_mm: float | None,
    svg_h_mm: float | None,
    original_path: str,
) -> Sticker | None:
    """Standardowa ścieżka wektorowa — outermost drawing / TrimBox / SVG / artwork-on-artboard.

    Zwraca None gdy wystąpił ValueError w ekstrakcji (strona pominięta).
    """
    try:
        page_w_pt = page.rect.width
        page_h_pt = page.rect.height

        extends_beyond = False
        artwork_on_artboard_flag = False
        edge_rgb = None

        if svg_contour:
            # Użyj konturu z SVG clipPath + wymiary z nazwy pliku
            cut_segments = svg_contour
            w_mm = svg_w_mm
            h_mm = svg_h_mm
            idx, _ = find_outermost_drawing(drawings, page.rect)
            outermost_idx = idx

        elif page_idx in cropped_pages:
            # Plik ze spadami — kontur = prostokąt TrimBox (page.rect)
            cut_segments = _make_page_rect_contour(page.rect)
            w_mm = page_w_pt * PT_TO_MM
            h_mm = page_h_pt * PT_TO_MM
            idx, _ = find_outermost_drawing(drawings, page.rect)
            outermost_idx = idx
            edge_rgb = _sample_page_edge_color(page)
            log.info(
                f"Strona {page_idx + 1}: kontur z TrimBox "
                f"({w_mm:.1f}x{h_mm:.1f}mm), "
                f"edge RGB=({edge_rgb[0]:.3f}, {edge_rgb[1]:.3f}, {edge_rgb[2]:.3f})"
            )

        else:
            # Standardowa ścieżka: szukaj konturu w PDF
            idx, outermost_drawing = find_outermost_drawing(drawings, page.rect)
            outermost_idx = idx

            # Sprawdź czy outermost drawing wykracza poza stronę
            od_rect = outermost_drawing['rect']
            extends_beyond = (
                od_rect.x0 < page.rect.x0 - 1 or
                od_rect.y0 < page.rect.y0 - 1 or
                od_rect.x1 > page.rect.x1 + 1 or
                od_rect.y1 > page.rect.y1 + 1
            )

            if extends_beyond:
                # Elementy wychodzą poza stronę → strona = kontur cięcia
                cut_segments = _make_page_rect_contour(page.rect)
                edge_rgb = _sample_page_edge_color(page)
                log.info(
                    f"Strona {page_idx + 1}: outermost drawing wykracza "
                    f"poza stronę → kontur = prostokąt strony, "
                    f"edge RGB=({edge_rgb[0]:.3f}, {edge_rgb[1]:.3f}, {edge_rgb[2]:.3f})"
                )
            else:
                # content_rect = union(drawings, images, text) ∩ page
                content_rect = _compute_content_rect(page, drawings)

                if _is_artwork_on_artboard(page, content_rect):
                    artwork_rect = fitz.Rect(
                        content_rect.x0 - 1, content_rect.y0 - 1,
                        content_rect.x1 + 1, content_rect.y1 + 1,
                    )
                    artwork_rect &= page.rect

                    # Kolor bleed = kolor tła wokół grafiki (np. białe A4
                    # wokół "I ♥ SOCJOLOGIA"). Bez tego bleed używa koloru
                    # outermost drawing (np. czerwone serce) i zalewa tło.
                    edge_rgb = _sample_page_edge_color(page, clip=artwork_rect)

                    log.info(
                        f"Strona {page_idx + 1}: artwork-on-artboard (wektor), "
                        f"grafika {artwork_rect.width * PT_TO_MM:.1f}x"
                        f"{artwork_rect.height * PT_TO_MM:.1f}mm na stronie "
                        f"{page.rect.width * PT_TO_MM:.1f}x"
                        f"{page.rect.height * PT_TO_MM:.1f}mm, "
                        f"edge RGB=({edge_rgb[0]:.3f}, "
                        f"{edge_rgb[1]:.3f}, {edge_rgb[2]:.3f})"
                    )

                    # Kontur = prostokąt artwork
                    aw = artwork_rect.width
                    ah = artwork_rect.height
                    cut_segments = [
                        ('l', np.array([0.0, 0.0]), np.array([aw, 0.0])),
                        ('l', np.array([aw, 0.0]), np.array([aw, ah])),
                        ('l', np.array([aw, ah]), np.array([0.0, ah])),
                        ('l', np.array([0.0, ah]), np.array([0.0, 0.0])),
                    ]

                    page.set_cropbox(artwork_rect)
                    page_w_pt = artwork_rect.width
                    page_h_pt = artwork_rect.height
                    artwork_on_artboard_flag = True
                else:
                    # Grafika wypełnia stronę. Sprawdź czy outermost drawing
                    # reprezentuje outline: jego bbox powinien być zbliżony
                    # do union całej zawartości. Jeśli jest znacznie mniejszy,
                    # to tylko pojedynczy element kompozycji (np. ilustracja
                    # wielo-pathowa bez jawnego konturu) — bierzemy prostokąt
                    # strony zamiast ekstraktu jego ścieżek.
                    od_rect = outermost_drawing['rect']
                    od_area = abs(od_rect.width * od_rect.height)
                    content_area = abs(content_rect.width * content_rect.height)
                    is_fragment = (
                        content_area > 1.0 and od_area / content_area < 0.5
                    )

                    if is_fragment:
                        cut_segments = _make_page_rect_contour(page.rect)
                        edge_rgb = _sample_page_edge_color(page)
                        log.info(
                            f"Strona {page_idx + 1}: outermost drawing to "
                            f"fragment kompozycji ({od_area/content_area:.2f} "
                            f"powierzchni content) → kontur = prostokąt strony, "
                            f"edge RGB=({edge_rgb[0]:.3f}, "
                            f"{edge_rgb[1]:.3f}, {edge_rgb[2]:.3f})"
                        )
                    else:
                        draw_diag = max(od_rect.width, od_rect.height)
                        gap_thr = max(0.5, min(2.0, draw_diag * 0.01))
                        cut_segments = extract_path_segments(
                            outermost_drawing['items'], gap_threshold=gap_thr
                        )

            w_mm = page_w_pt * PT_TO_MM
            h_mm = page_h_pt * PT_TO_MM

        sticker = Sticker(
            source_path=original_path,
            page_index=page_idx,
            width_mm=w_mm,
            height_mm=h_mm,
            cut_segments=cut_segments,
            pdf_doc=doc,
            page_width_pt=page_w_pt,
            page_height_pt=page_h_pt,
            outermost_drawing_idx=outermost_idx,
            is_cmyk=_page_is_cmyk(doc, page),
            is_artwork_on_artboard=artwork_on_artboard_flag,
        )
        # Ustaw kolor krawędzi gdy wykryty z renderowanej strony
        if edge_rgb is not None:
            sticker.edge_color_rgb = edge_rgb

        log.info(
            f"Sticker p{page_idx + 1}: {sticker.width_mm:.1f}x{sticker.height_mm:.1f}mm, "
            f"{len(cut_segments)} segmentow konturu"
        )
        return sticker

    except ValueError as e:
        log.warning(f"Strona {page_idx + 1} pominieta: {e}")
        return None


def detect_contour(pdf_path: str) -> list[Sticker]:
    """Główna funkcja: wykrywa kontur cięcia z dowolnego pliku wejściowego.

    Routing per typ pliku:
      1. Raster (PNG/JPG/TIFF/BMP/WEBP) → _detect_raster()
      2. EPS → Ghostscript → PDF pipeline
      3. SVG → CairoSVG → PDF pipeline z clipPath contour
      4. PDF wektor → find_outermost_drawing() + extract_path_segments()
      5. PDF raster-only → _render_alpha_contour() lub prostokąt

    UWAGA: pdf_doc pozostaje otwarty! Zamknięcie po stronie konsumenta.

    Cache: dla PDF i plikow rastrowych cut_segments sa cache'owane
    na podstawie sha1(path + mtime + size + engine). Cache hit pomija
    najdrozszy krok (tracing konturu) i typowo konczy sie w <10ms.
    EPS/SVG NIE sa cache'owane (wymagaja konwersji tmp_pdf).

    Returns:
        list[Sticker] — jeden per prawidłowa strona
    """
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"Plik nie istnieje: {pdf_path}")

    original_path = pdf_path
    ext = os.path.splitext(pdf_path)[1].lower()
    _cacheable = ext in (".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp")

    # Cache check (tylko PDF + raster, nie EPS/SVG).
    if _cacheable:
        from modules import cache as _cache
        cached = _cache.load(pdf_path, config.CONTOUR_ENGINE)
        if cached is not None:
            # Re-open pdf_doc dla PDF (raster uzywa raster_path, nie pdf_doc).
            # Dla raster_only PDF wszystkie stickers wspoldziela jeden pdf_doc.
            reopened_doc = None
            for s in cached:
                if s.raster_path is None and reopened_doc is None:
                    reopened_doc = fitz.open(pdf_path)
                s.pdf_doc = reopened_doc
            return cached

    # === ŚCIEŻKA 1: Pliki rastrowe (PNG/JPG/TIFF/BMP/WEBP) ===
    if detect_type(pdf_path) == FileType.RASTER:
        result = [_detect_raster(pdf_path)]
        if _cacheable:
            from modules import cache as _cache
            _cache.save(pdf_path, config.CONTOUR_ENGINE, result)
        return result

    # === ŚCIEŻKI 2-3: EPS/SVG → tmp PDF ===
    pdf_path, tmp_pdf, svg_contour, svg_w_mm, svg_h_mm = _prepare_pdf_path(pdf_path)

    # === ŚCIEŻKA 4+5: PDF (wektor lub raster-only) ===
    doc = fitz.open(pdf_path)

    # TrimBox ≠ MediaBox → plik ze spadami, przycinamy do TrimBox
    cropped_pages, malformed_pages = _crop_to_trimbox(doc)

    # Crop marks w zewnętrznym obszarze strony (gdy TrimBox == MediaBox):
    # wykrywamy L-kształtne znaczniki Illustratora i przycinamy do trim.
    cropped_pages |= apply_crop_marks_cropping(
        doc, skip_pages=cropped_pages | malformed_pages
    )

    stickers: list[Sticker] = []

    for page_idx in range(len(doc)):
        # 1. PDF zawiera CutContour (bleed output) → re-ingest
        cutcontour_segs = _extract_cutcontour_segments(doc, page_idx)
        if cutcontour_segs is not None:
            stickers.append(
                _build_sticker_from_cutcontour(
                    doc, page_idx, cutcontour_segs, original_path
                )
            )
            continue

        # 1b. Malformed TrimBox (Canva): render MediaBox jako raster
        if page_idx in malformed_pages:
            stickers.append(
                _build_sticker_from_malformed_trimbox(
                    doc, page_idx, original_path
                )
            )
            continue

        # 2. Standardowa walidacja strony
        try:
            page, drawings = validate_page(
                doc, page_idx, skip_raster_check=bool(tmp_pdf)
            )
        except ValueError:
            # Brak wektorów — sprawdź raster-only PDF
            sticker = _build_sticker_from_raster_only_pdf(
                doc, page_idx, original_path
            )
            if sticker is not None:
                stickers.append(sticker)
            continue

        # 3. Standardowa ścieżka wektorowa
        sticker = _build_sticker_from_vector(
            doc, page_idx, page, drawings, cropped_pages,
            svg_contour, svg_w_mm, svg_h_mm, original_path,
        )
        if sticker is not None:
            stickers.append(sticker)

    if not stickers:
        doc.close()
        raise ValueError(f"Brak prawidlowych stron w {pdf_path}")

    # Zapis do cache (tylko PDF; EPS/SVG maja tmp_pdf inny od original_path).
    # Sprawdzamy rozszerzenie ORYGINALNEGO pliku, nie tmp_pdf.
    if _cacheable and not tmp_pdf:
        from modules import cache as _cache
        _cache.save(original_path, config.CONTOUR_ENGINE, stickers)

    return stickers
