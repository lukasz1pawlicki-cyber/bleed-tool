"""
Sticker Toolkit — contour.py
=============================
Detekcja konturu zewnętrznego z wektorowego PDF.

Pipeline:
  1. validate_input()  → walidacja PDF (single-page, wektorowy)
  2. find_outermost_drawing() → znalezienie zewnętrznej ścieżki
  3. extract_path_segments() → ekstrakcja segmentów (linie + krzywe Bézier)
  4. detect_contour()  → główna funkcja: PDF → Sticker z wypełnionymi polami konturu

Przyjmuje: ścieżka do PDF
Zwraca: Sticker z wypełnionymi polami: source_path, width_mm, height_mm,
        cut_segments, pdf_doc, page_width_pt, page_height_pt, outermost_drawing_idx
"""

from __future__ import annotations

import logging
import os
import numpy as np
import fitz  # PyMuPDF

from models import Sticker
from config import PT_TO_MM, MM_TO_PT

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

def _crop_to_trimbox(doc: fitz.Document) -> set[int]:
    """Jeśli strona ma TrimBox mniejszy od MediaBox, przycina do TrimBox.

    Pliki eksportowane z Illustratora/InDesign ze spadami mają:
      - MediaBox = cała strona ze spadami
      - TrimBox  = właściwy rozmiar naklejki (bez spadów)
    Ustawiamy CropBox = TrimBox, co powoduje, że PyMuPDF
    traktuje TrimBox jako widoczny obszar strony.

    Returns:
        set indeksów stron, które zostały przycięte (mają spady)
    """
    cropped_pages: set[int] = set()
    for page in doc:
        trimbox = page.trimbox
        mediabox = page.mediabox

        # Sprawdź czy TrimBox różni się od MediaBox (= plik ze spadami)
        if (abs(trimbox.x0 - mediabox.x0) > 0.5 or
                abs(trimbox.y0 - mediabox.y0) > 0.5 or
                abs(trimbox.x1 - mediabox.x1) > 0.5 or
                abs(trimbox.y1 - mediabox.y1) > 0.5):
            log.info(
                f"Strona {page.number + 1}: TrimBox "
                f"{trimbox.width:.1f}x{trimbox.height:.1f}pt "
                f"({trimbox.width * PT_TO_MM:.1f}x{trimbox.height * PT_TO_MM:.1f}mm) "
                f"— przycinanie ze spadów"
            )
            page.set_cropbox(trimbox)
            cropped_pages.add(page.number)
    return cropped_pages


def _detect_raster(image_path: str) -> Sticker:
    """Tworzy Sticker z pliku rastrowego (PNG/JPG/TIFF/BMP/WEBP).

    Wymiary mm obliczane wg priorytetu:
      1. Z nazwy pliku (np. '50x50' → 50mm × 50mm) — jak SVG
      2. Z metadanych DPI obrazu (pixels / DPI * 25.4)
      3. Fallback: 300 DPI

    Kontur cięcia = prostokąt (bounding box).
    Kolor krawędzi = próbkowanie pikseli z krawędzi obrazu.
    """
    from PIL import Image
    from modules.svg_convert import parse_size_from_filename

    img = Image.open(image_path)
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
            if dpi_x in (72, 96) and dpi_y in (72, 96):
                log.info(
                    f"Raster DPI={dpi_x:.0f} (domyślne ekranowe), "
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

    if has_alpha:
        cut_segments, edge_rgb = _detect_raster_alpha_contour(
            img, w_pt, h_pt
        )

    if cut_segments is None and not has_alpha:
        # Obraz bez alpha — spróbuj wykryć kształt z jednolitego tła
        cut_segments, edge_rgb = _detect_raster_bg_contour(
            img, w_pt, h_pt
        )

    if cut_segments is None:
        # Fallback: kontur cięcia = prostokąt (fitz coords: y-down)
        page_rect = fitz.Rect(0, 0, w_pt, h_pt)
        cut_segments = _make_page_rect_contour(page_rect)

    if edge_rgb is None:
        # Kolor krawędzi — próbkowanie z krawędzi obrazu
        edge_rgb = _sample_raster_edge_color(img)

    img.close()

    sticker = Sticker(
        source_path=image_path,
        page_index=0,
        width_mm=w_mm,
        height_mm=h_mm,
        cut_segments=cut_segments,
        pdf_doc=None,          # raster — nie ma pdf_doc
        raster_path=image_path,
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


def _detect_raster_alpha_contour(
    img, w_pt: float, h_pt: float
) -> tuple[list | None, tuple[float, float, float] | None]:
    """Wykrywa kontur z kanału alpha obrazu rastrowego.

    Jeśli obraz ma nietrywialną przezroczystość (nie jest w pełni opaque),
    analizuje kształt treści:
      - okrąg → 4 krzywe Bézier
      - inny kształt → polygon z linii (Douglas-Peucker)

    Zwraca (cut_segments, edge_rgb) lub (None, None) gdy alpha jest trywialna.
    Segmenty w koordynatach pt, origin (0,0).
    """
    rgba = img.convert('RGBA')
    arr = np.array(rgba)
    alpha = arr[:, :, 3]
    rgb = arr[:, :, :3]
    h, w = alpha.shape

    # Sprawdź czy alpha jest trywialna (cały obraz opaque)
    transparent_count = np.sum(alpha < 128)
    total = h * w
    if transparent_count < total * 0.01:
        # Mniej niż 1% przezroczystych — traktuj jako prostokąt
        log.info("Raster alpha: obraz prawie w pełni opaque, prostokątny kontur")
        return None, None

    # Skaluj do rozdzielczości roboczej (500px maks)
    target_px = 500
    scale_factor = target_px / max(h, w)
    if scale_factor < 1.0:
        from PIL import Image as PILImage
        new_w = max(1, int(w * scale_factor))
        new_h = max(1, int(h * scale_factor))
        rgba_small = rgba.resize((new_w, new_h), PILImage.NEAREST)
        arr_s = np.array(rgba_small)
        alpha_s = arr_s[:, :, 3]
        rgb_s = arr_s[:, :, :3]
        hs, ws = alpha_s.shape
    else:
        scale_factor = 1.0
        alpha_s = alpha
        rgb_s = rgb
        hs, ws = h, w

    threshold = 128

    # Zbierz punkty graniczne i kolory krawędzi
    boundary_pts = []
    edge_colors = []

    for y in range(hs):
        opaque_cols = np.where(alpha_s[y] > threshold)[0]
        if len(opaque_cols) > 0:
            lx = int(opaque_cols[0])
            rx = int(opaque_cols[-1])
            boundary_pts.append([float(lx), float(y)])
            if rx != lx:
                boundary_pts.append([float(rx), float(y)])
            edge_colors.append(rgb_s[y, lx].astype(float))
            edge_colors.append(rgb_s[y, rx].astype(float))

    if len(boundary_pts) < 6:
        return None, None

    boundary_arr = np.array(boundary_pts)

    # Kolor krawędzi z pikseli granicznych
    if edge_colors:
        avg = np.mean(edge_colors, axis=0) / 255.0
        edge_rgb = (float(avg[0]), float(avg[1]), float(avg[2]))
    else:
        edge_rgb = (1.0, 1.0, 1.0)

    # Przelicznik px (skalowany) → pt
    px_to_pt_x = w_pt / ws
    px_to_pt_y = h_pt / hs

    # Próba dopasowania okręgu
    circle = _fit_circle(boundary_arr)
    if circle is not None:
        cx_px, cy_px, r_px = circle
        if _is_circular(boundary_arr, cx_px, cy_px, r_px, tolerance=0.05):
            # Okrąg! Generuj Bézier w pt
            cx_pt = cx_px * px_to_pt_x
            cy_pt = cy_px * px_to_pt_y
            r_pt = r_px * (px_to_pt_x + px_to_pt_y) / 2  # średni skalowanie
            segments = _circle_to_bezier_segments(cx_pt, cy_pt, r_pt)
            log.info(
                f"Raster alpha kontur: okrąg Bézier, r={r_pt * PT_TO_MM:.1f}mm, "
                f"edge RGB=({edge_rgb[0]:.2f}, {edge_rgb[1]:.2f}, {edge_rgb[2]:.2f})"
            )
            return segments, edge_rgb

    # Fallback: polygon z linii (Douglas-Peucker)
    left_pts = []
    right_pts = []
    for y in range(hs):
        opaque_cols = np.where(alpha_s[y] > threshold)[0]
        if len(opaque_cols) > 0:
            left_pts.append([float(opaque_cols[0]), float(y)])
            right_pts.append([float(opaque_cols[-1]), float(y)])

    polygon_px = np.array(left_pts + right_pts[::-1])
    polygon_px = _douglas_peucker(polygon_px, epsilon=1.0)

    segments = []
    n = len(polygon_px)
    for i in range(n):
        p0 = polygon_px[i]
        p1 = polygon_px[(i + 1) % n]
        segments.append(('l',
                         np.array([p0[0] * px_to_pt_x, p0[1] * px_to_pt_y]),
                         np.array([p1[0] * px_to_pt_x, p1[1] * px_to_pt_y])))

    log.info(
        f"Raster alpha kontur: polygon {len(segments)} segmentów, "
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
        from PIL import Image as PILImage
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

    segments = []
    n = len(polygon_px)
    for i in range(n):
        p0 = polygon_px[i]
        p1 = polygon_px[(i + 1) % n]
        segments.append(('l',
                         np.array([p0[0] * px_to_pt_x, p0[1] * px_to_pt_y]),
                         np.array([p1[0] * px_to_pt_x, p1[1] * px_to_pt_y])))

    log.info(
        f"Raster BG kontur: polygon {len(segments)} segmentów, "
        f"bg=({bg_avg[0]:.0f},{bg_avg[1]:.0f},{bg_avg[2]:.0f}), "
        f"edge RGB=({edge_rgb[0]:.2f}, {edge_rgb[1]:.2f}, {edge_rgb[2]:.2f})"
    )
    return segments, edge_rgb


def _sample_raster_edge_color(img) -> tuple[float, float, float]:
    """Próbkuje średni kolor z krawędzi obrazu (1px obwódka).

    Zwraca (r, g, b) w zakresie 0-1.
    """
    from PIL import Image
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

    segments = []
    n = len(polygon_px)
    for i in range(n):
        p0 = polygon_px[i]
        p1 = polygon_px[(i + 1) % n]
        segments.append(('l',
                         np.array([p0[0] / scale, p0[1] / scale]),
                         np.array([p1[0] / scale, p1[1] / scale])))

    log.info(
        f"Kontur: polygon {len(segments)} segmentów, "
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


def _sample_pdf_page_edge_color(
    doc: fitz.Document, page_index: int
) -> tuple[float, float, float]:
    """Próbkuje kolor krawędzi ze zrenderowanej strony PDF.

    Renderuje stronę na niskiej rozdzielczości i próbkuje piksele z krawędzi.
    Zwraca (r, g, b) w zakresie 0-1.
    """
    from PIL import Image

    page = doc[page_index]
    # Niska rozdzielczość — wystarczy do próbkowania koloru krawędzi
    zoom = 72.0 / 72.0  # 72 DPI
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    edge_rgb = _sample_raster_edge_color(img)
    img.close()
    return edge_rgb


def _sample_page_edge_color(page: fitz.Page) -> tuple[float, float, float]:
    """Sampluje dominujący kolor krawędzi renderowanej strony.

    Renderuje stronę na 72 DPI i uśrednia piksele z 2px obramowania.
    Zwraca (r, g, b) w zakresie 0-1.
    """
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


# =============================================================================
# GŁÓWNA FUNKCJA: detect_contour
# =============================================================================

def detect_contour(pdf_path: str) -> list[Sticker]:
    """Wykrywa kontury ze wszystkich stron PDF/SVG.

    Zwraca listę Sticker — jeden per prawidłowa strona.
    Strony z obrazami rastrowymi lub bez drawings są pomijane.

    Dla SVG:
      - Wymiary (mm) pobierane z nazwy pliku (np. '50x50')
      - Konwersja SVG→PDF przez cairosvg (wektory)
      - Kontur cięcia wyciągany z clipPath SVG (nie z PDF)

    UWAGA: pdf_doc pozostaje otwarty! Zamknięcie po stronie konsumenta.
    """
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"Plik nie istnieje: {pdf_path}")

    _tmp_pdf = None
    original_path = pdf_path
    svg_contour = None  # kontur wyciągnięty z SVG clipPath
    svg_w_mm = None
    svg_h_mm = None

    if pdf_path.lower().endswith(_RASTER_EXT):
        # Plik rastrowy — osobna ścieżka przetwarzania
        sticker = _detect_raster(pdf_path)
        return [sticker]

    if pdf_path.lower().endswith('.svg'):
        from modules.svg_convert import (
            svg_to_pdf, parse_size_from_filename, extract_svg_contour,
        )

        # 1. Wymiary z nazwy pliku
        size = parse_size_from_filename(pdf_path)
        if size is None:
            raise ValueError(
                f"Brak wymiarów w nazwie pliku SVG (wymagany format np. '50x50'): "
                f"{pdf_path}"
            )
        svg_w_mm, svg_h_mm = size

        # 2. Konwersja SVG → PDF
        _tmp_pdf = svg_to_pdf(pdf_path,
                              target_w_mm=svg_w_mm,
                              target_h_mm=svg_h_mm)
        pdf_path = _tmp_pdf

        # 3. Wyciągnij kontur z SVG clipPath
        svg_contour = extract_svg_contour(original_path, svg_w_mm, svg_h_mm)
        if svg_contour:
            log.info(f"SVG contour: {len(svg_contour)} segmentów z clipPath")

    elif not pdf_path.lower().endswith('.pdf'):
        raise ValueError(f"Nieobslugiwany format pliku. Wymagany PDF, SVG lub obraz rastrowy: {pdf_path}")

    doc = fitz.open(pdf_path)

    # Jeśli TrimBox ≠ MediaBox → plik ze spadami, przycinamy do TrimBox
    cropped_pages = _crop_to_trimbox(doc)

    stickers: list[Sticker] = []

    for page_idx in range(len(doc)):
        try:
            page, drawings = validate_page(
                doc, page_idx, skip_raster_check=bool(_tmp_pdf)
            )
        except ValueError:
            # Strona bez drawings — sprawdź czy ma obrazy rastrowe
            page = doc[page_idx]
            images = page.get_images()
            if images:
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
                )
                stickers.append(sticker)
                log.info(
                    f"Sticker p{page_idx + 1}: {w_mm:.1f}x{h_mm:.1f}mm, "
                    f"raster-only PDF, {len(cut_segments)} segmentów konturu, "
                    f"edge RGB=({edge_rgb[0]:.2f}, {edge_rgb[1]:.2f}, {edge_rgb[2]:.2f})"
                )
                continue
            else:
                log.warning(f"Strona {page_idx + 1} pominieta: brak wektorów i obrazów")
                continue

        try:
            page_w_pt = page.rect.width
            page_h_pt = page.rect.height

            extends_beyond = False
            if svg_contour:
                # Użyj konturu z SVG clipPath + wymiary z nazwy pliku
                cut_segments = svg_contour
                w_mm = svg_w_mm
                h_mm = svg_h_mm
                # Znajdź outermost drawing dla ekstrakcji koloru krawędzi (bleed)
                idx, _ = find_outermost_drawing(drawings, page.rect)
                outermost_idx = idx
            elif page_idx in cropped_pages:
                # Plik ze spadami — kontur = prostokąt TrimBox (page.rect)
                # Drawings mają koordynaty relative do CropBox, ale ich rects
                # mogą wykraczać poza page.rect (bo były w MediaBox)
                cut_segments = _make_page_rect_contour(page.rect)
                w_mm = page_w_pt * PT_TO_MM
                h_mm = page_h_pt * PT_TO_MM
                # Szukaj outermost drawing dla koloru krawędzi
                idx, _ = find_outermost_drawing(drawings, page.rect)
                outermost_idx = idx
                log.info(
                    f"Strona {page_idx + 1}: kontur z TrimBox "
                    f"({w_mm:.1f}x{h_mm:.1f}mm)"
                )
            else:
                # Standardowa ścieżka: szukaj konturu w PDF
                idx, outermost_drawing = find_outermost_drawing(
                    drawings, page.rect
                )

                # Sprawdź czy outermost drawing wykracza poza stronę
                # (np. elementy dekoracyjne na wizytówce) — wtedy kontur = strona
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
                    # Kolor krawędzi: renderuj stronę i sampluj krawędzie
                    edge_rgb = _sample_page_edge_color(page)
                    log.info(
                        f"Strona {page_idx + 1}: outermost drawing wykracza "
                        f"poza stronę → kontur = prostokąt strony, "
                        f"edge RGB=({edge_rgb[0]:.3f}, {edge_rgb[1]:.3f}, {edge_rgb[2]:.3f})"
                    )
                else:
                    # Adaptacyjny gap_threshold — skalowany do rozmiaru rysunku
                    # Dla małych rysunków (< 50pt) mniejszy próg; dla dużych standardowy
                    _draw_diag = max(outermost_drawing['rect'].width,
                                     outermost_drawing['rect'].height)
                    _gap_thr = max(0.5, min(2.0, _draw_diag * 0.01))
                    cut_segments = extract_path_segments(
                        outermost_drawing['items'], gap_threshold=_gap_thr
                    )

                w_mm = page_w_pt * PT_TO_MM
                h_mm = page_h_pt * PT_TO_MM
                outermost_idx = idx

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
            )
            # Ustaw kolor krawędzi gdy wykryty z renderowanej strony
            if extends_beyond:
                sticker.edge_color_rgb = edge_rgb
            stickers.append(sticker)

            log.info(
                f"Sticker p{page_idx + 1}: {sticker.width_mm:.1f}x{sticker.height_mm:.1f}mm, "
                f"{len(cut_segments)} segmentow konturu"
            )
        except ValueError as e:
            log.warning(f"Strona {page_idx + 1} pominieta: {e}")

    if not stickers:
        doc.close()
        raise ValueError(f"Brak prawidlowych stron w {pdf_path}")

    return stickers
