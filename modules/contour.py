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
            if dpi_x < 10 or dpi_x > 10000:
                dpi_x = _DEFAULT_DPI
            if dpi_y < 10 or dpi_y > 10000:
                dpi_y = _DEFAULT_DPI
            log.info(f"Raster DPI z metadanych: {dpi_x:.0f}×{dpi_y:.0f}")
        else:
            dpi_x = dpi_y = _DEFAULT_DPI
            log.info(f"Raster: brak DPI w metadanych, fallback {_DEFAULT_DPI} DPI")

        w_mm = px_w / dpi_x * 25.4
        h_mm = px_h / dpi_y * 25.4

    # Wymiary strony w pt (PDF points)
    w_pt = w_mm * MM_TO_PT
    h_pt = h_mm * MM_TO_PT

    # Kontur cięcia = prostokąt (fitz coords: y-down)
    page_rect = fitz.Rect(0, 0, w_pt, h_pt)
    cut_segments = _make_page_rect_contour(page_rect)

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

            page_w_pt = page.rect.width
            page_h_pt = page.rect.height

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
                cut_segments = extract_path_segments(
                    outermost_drawing['items']
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
