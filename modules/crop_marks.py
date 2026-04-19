"""
Bleed Tool — crop_marks.py
==========================
Detekcja znaczników cięcia (crop marks / trim marks) w PDF.

Cel: gdy plik wyeksportowany z Illustratora/InDesign ma crop marks
w zewnętrznym obszarze strony (a TrimBox == MediaBox), wykrywamy
pozycje marks i ustawiamy CropBox na obszar trim. Dalszy pipeline
(detect_contour → generate_bleed → export) pracuje już na prawidłowym
obszarze użytku (bez marks w grafice).

Wzorzec Illustratora (standard):
  - 4 krótkie linie horyzontalne (na Y = trim_top, trim_bottom)
    rozciągnięte poziomo z trim corner w stronę krawędzi strony
  - 4 krótkie linie wertykalne (na X = trim_left, trim_right)
    rozciągnięte pionowo z trim corner w stronę krawędzi strony
  - Typowo zdublowane: biały underlay (1.25pt) + czarny on-top (0.25pt)
  - Kolor: registration black (Separation /All) lub DeviceCMYK 100/100/100/100

API:
  detect_crop_marks_trim(page) → fitz.Rect | None — trim rect w fitz-coords
  apply_crop_marks_cropping(doc, skip_pages) → set[int] — indeksy przyciętych stron
"""
from __future__ import annotations

import logging
import fitz

from config import PT_TO_MM

log = logging.getLogger(__name__)

# Max długość pojedynczej linii crop mark (pt). Illustrator używa 18pt + 6pt offset,
# więc typowa linia ma 18-27pt. 36pt = 12.7mm zapewnia margines bezpieczeństwa
# bez łapania krótkich linii z grafiki użytkowej.
_CROP_MARK_MAX_LENGTH_PT = 36.0

# Max grubość linii crop mark (pt). Standard: 0.25pt (czarny) + 1.25pt (biały underlay).
# Powyżej 2pt to raczej element designu, nie crop mark.
_CROP_MARK_MAX_STROKE_PT = 2.0

# Min bbox full-page drawing (% strony). Crop marks są w rogach → bbox obejmuje
# praktycznie całą stronę mimo że indywidualne linie są krótkie.
_FULL_PAGE_BBOX_RATIO = 0.9

# Tolerancja klasteryzacji Y/X (pt). Linie o Y różniącym się < tol są tego
# samego trim-edge (artefakty numeryczne w eksporcie Illustratora).
_CLUSTER_TOLERANCE_PT = 1.0

# Trim musi pokrywać co najmniej tyle % strony, żeby uznać detekcję za wiarygodną.
# Mniej niż 30% to prawdopodobnie fałszywe dopasowanie.
_MIN_TRIM_RATIO = 0.30

# Trim musi mieć co najmniej taki margines od krawędzi strony (pt). Crop marks
# Illustratora są standardowo >= 6pt od krawędzi strony (= 2.1mm offset).
_MIN_MARGIN_PT = 5.0


def detect_crop_marks_trim(page: fitz.Page) -> fitz.Rect | None:
    """Wykrywa crop marks na stronie i zwraca obszar trim.

    Algorytm:
      1. Szuka stroke drawings o bbox ≈ całej strony (marks sięgają rogów)
      2. Z każdego takiego drawing wyciąga items typu 'l' (linie proste)
      3. Filtruje linie krótkie (< 36pt), poziome/pionowe
      4. Horyzontalne → zbiera Y; wertykalne → zbiera X
      5. Klasteryzuje Y i X (tol 1pt)
      6. Wymaga >= 2 unique Y i >= 2 unique X (top/bottom + left/right)
      7. Trim = bbox [min X, min Y, max X, max Y]
      8. Waliduje: trim ma rozsądny rozmiar (>= 30% strony) i margines (>= 5pt)

    Args:
        page: strona PDF

    Returns:
        fitz.Rect z obszarem trim (fitz y-down coords) lub None gdy brak marks
    """
    drawings = page.get_drawings()
    W = page.rect.width
    H = page.rect.height

    h_y_values: list[float] = []
    v_x_values: list[float] = []

    for d in drawings:
        if d.get('type') != 's':
            continue

        bbox = d.get('rect')
        if not bbox:
            continue

        # Crop marks są w rogach → ich bbox sięga (prawie) całej strony
        if bbox.width < W * _FULL_PAGE_BBOX_RATIO:
            continue
        if bbox.height < H * _FULL_PAGE_BBOX_RATIO:
            continue

        stroke_w = d.get('width') or 0.0
        if stroke_w > _CROP_MARK_MAX_STROKE_PT:
            continue

        for item in d.get('items', []):
            if not item or item[0] != 'l':
                continue

            p1, p2 = item[1], item[2]
            dx = abs(p2.x - p1.x)
            dy = abs(p2.y - p1.y)
            length = max(dx, dy)

            if length < 1.0 or length > _CROP_MARK_MAX_LENGTH_PT:
                continue

            if dy < 0.5 and dx > 1.0:
                h_y_values.append((p1.y + p2.y) / 2.0)
            elif dx < 0.5 and dy > 1.0:
                v_x_values.append((p1.x + p2.x) / 2.0)

    y_clusters = _cluster_values(h_y_values, _CLUSTER_TOLERANCE_PT)
    x_clusters = _cluster_values(v_x_values, _CLUSTER_TOLERANCE_PT)

    if len(y_clusters) < 2 or len(x_clusters) < 2:
        return None

    x0 = min(x_clusters)
    x1 = max(x_clusters)
    y0 = min(y_clusters)
    y1 = max(y_clusters)

    trim_w = x1 - x0
    trim_h = y1 - y0

    if trim_w < W * _MIN_TRIM_RATIO or trim_h < H * _MIN_TRIM_RATIO:
        return None

    if x0 < _MIN_MARGIN_PT or y0 < _MIN_MARGIN_PT:
        return None
    if x1 > W - _MIN_MARGIN_PT or y1 > H - _MIN_MARGIN_PT:
        return None

    return fitz.Rect(x0, y0, x1, y1)


def apply_crop_marks_cropping(
    doc: fitz.Document, skip_pages: set[int] | None = None
) -> set[int]:
    """Dla każdej strony bez wyraźnego TrimBox: wykrywa crop marks
    i ustawia CropBox na detected trim rect.

    Crop marks zostają w content stream — są po prostu poza CropBox i zostają
    zmaskowane przez inject_page_boundary_clip() w export.py (który dodaje
    clip do cropbox + bleed_pts). Dla standardowego bleed 2mm crop marks
    Illustratora (offset 6pt ≈ 2.1mm od trim) pozostają poza clipem.

    Nie dotyka stron z indeksami w `skip_pages` (te zostały już przycięte
    przez _crop_to_trimbox na podstawie wyraźnego TrimBox).

    Args:
        doc: otwarty dokument PDF
        skip_pages: strony do pominięcia (np. te z wyraźnym TrimBox)

    Returns:
        set indeksów stron, na których ustawiono CropBox z crop marks
    """
    skip_pages = skip_pages or set()
    cropped: set[int] = set()

    for page in doc:
        if page.number in skip_pages:
            continue

        trim = detect_crop_marks_trim(page)
        if trim is None:
            continue

        log.info(
            f"Strona {page.number + 1}: wykryto crop marks → przycinanie "
            f"{trim.width:.1f}x{trim.height:.1f}pt "
            f"({trim.width * PT_TO_MM:.1f}x{trim.height * PT_TO_MM:.1f}mm)"
        )
        page.set_cropbox(trim)
        cropped.add(page.number)

    return cropped


def _cluster_values(values: list[float], tolerance: float) -> list[float]:
    """Klasteryzuje listę wartości 1D: sąsiednie wartości < tolerance
    traktowane jako jeden klaster (zwraca średnią każdego klastra).
    """
    if not values:
        return []
    sorted_vals = sorted(values)
    clusters: list[list[float]] = [[sorted_vals[0]]]
    for v in sorted_vals[1:]:
        if v - clusters[-1][-1] <= tolerance:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    return [sum(c) / len(c) for c in clusters]
