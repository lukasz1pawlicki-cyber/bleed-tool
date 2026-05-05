"""Regression: bleed extensions for non-uniform edge drawn at page-level.

Slowinski p1 (etykiety_6sztuk, 2026-05-05): grafika w głęboko zagnieżdżonym
Form XObjekcie (X2 → X4/X6/X8). expand_edge_paths/expand_page_fills nie
działają wewnątrz sub-XObjektów (utracony parent CTM tracking) → spad był
cały biały (solid-fill = outermost biały, brak rozszerzeń).

Fix: `overlay_edge_extensions` używa get_drawings() jako API (page-coords
po zaaplikowaniu wszystkich CTM, niezależnie od głębokości XObjektów)
i rysuje extension rectangles BEZPOŚREDNIO na page-level w obszarze
poza CropBox.
"""
import fitz
import numpy as np
from PIL import Image

from config import MM_TO_PT
from modules.bleed import generate_bleed
from modules.contour import detect_contour
from modules.export import export_single_sticker, overlay_edge_extensions


def _make_xobject_nonuniform_pdf(tmp_path):
    """PDF gdzie kolorowe pasy są wewnątrz Form XObject (jak Slowinski).

    Imituje strukturę Illustrator/InDesign export:
      - Page Contents: tylko `q /X1 Do Q`
      - X1 Form XObject: cała grafika (full-page bg, kolorowe pasy boczne, top bar)
    """
    doc = fitz.open()
    w = 80 * MM_TO_PT
    h = 50 * MM_TO_PT
    page = doc.new_page(width=w, height=h)

    # Stwórz Form XObject z pełną grafiką
    # Używamy fitz Document: nowa strona jako "form template"
    form_doc = fitz.open()
    form_page = form_doc.new_page(width=w, height=h)
    form_page.draw_rect(fitz.Rect(0, 0, w, h), fill=(1, 1, 1), color=None, width=0)
    # Boczny pas fioletowy (lewy)
    form_page.draw_rect(fitz.Rect(0, 0, 10 * MM_TO_PT, h),
                         fill=(0.5, 0.3, 0.7), color=None, width=0)
    # Boczny pas fioletowy (prawy)
    form_page.draw_rect(fitz.Rect(w - 10 * MM_TO_PT, 0, w, h),
                         fill=(0.5, 0.3, 0.7), color=None, width=0)
    # Górny pasek pomarańczowy
    form_page.draw_rect(fitz.Rect(0, 0, w, 5 * MM_TO_PT),
                         fill=(0.95, 0.5, 0.17), color=None, width=0)

    # Embed form_page jako XObject na main page
    page.show_pdf_page(fitz.Rect(0, 0, w, h), form_doc, 0)
    form_doc.close()

    src = tmp_path / "xobject_nonuniform.pdf"
    doc.save(str(src))
    doc.close()
    return str(src)


def test_overlay_extensions_added_for_nonuniform_xobject_input(tmp_path):
    """Sticker z grafiką w XObjekcie + niejednolitą krawędzią → output ma
    extension rect-y narysowane na bleed area (tj. liczba drawings rośnie)."""
    src = _make_xobject_nonuniform_pdf(tmp_path)
    stickers = detect_contour(src)
    s = stickers[0]
    generate_bleed(s, 2.0)

    output_path = str(tmp_path / "out.pdf")
    export_single_sticker(s, output_path, bleed_mm=2.0)

    # Output musi pozostać wektorowy
    out_doc = fitz.open(output_path)
    out_page = out_doc[0]
    assert len(out_page.get_images()) == 0, "Wektorowy input → wektorowy output"

    # Render output i sprawdź że bleed area ma kolorowe piksele (nie biały)
    pix = out_page.get_pixmap(dpi=150)
    out_doc.close()
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)

    # Bleed strefa to zewnętrzne ~3% pikseli (bleed=2mm na ~50mm strony)
    # Sample 1px obwódkę i sprawdź że NIE jest cały biały
    border = np.concatenate([
        arr[0, :, :],
        arr[-1, :, :],
        arr[1:-1, 0, :],
        arr[1:-1, -1, :],
    ], axis=0)
    # Liczba pikseli wyraźnie nie-białych (jakikolwiek kanał < 240)
    non_white = np.sum(np.any(border < 240, axis=1))
    total = len(border)
    nonwhite_ratio = non_white / total
    assert nonwhite_ratio > 0.30, (
        f"Bleed area cały biały (non-white ratio {nonwhite_ratio:.2%}) — "
        f"overlay_edge_extensions nie zadziałał dla XObject-zagnieżdżonej grafiki"
    )


def test_overlay_corner_extensions_fill_bleed_corners(tmp_path):
    """Corner extensions: drawing dotykające 2 przyległych krawędzi
    wypełniają narożnik bleed (bez tego narożniki zostają białe).

    Slowinski p1 (2026-05-05): TL/TR narożniki muszą być pomarańczowe
    (kolor górnego paska), BL fioletowy (kolor lewej kolumny). Bez corner
    extensions zostawały białe (4 białe kwadraty 2×2mm w narożnikach).
    """
    src = _make_xobject_nonuniform_pdf(tmp_path)
    stickers = detect_contour(src)
    s = stickers[0]
    generate_bleed(s, 2.0)
    output_path = str(tmp_path / "out_corners.pdf")
    export_single_sticker(s, output_path, bleed_mm=2.0)

    doc = fitz.open(output_path)
    pix = doc[0].get_pixmap(dpi=300)
    doc.close()
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)

    # Bleed=2mm at 300dpi ≈ 24px. Sample 10x10 z każdego narożnika
    # (centralnie w narożniku bleed area).
    def corner_avg(top: bool, left: bool) -> tuple[float, float, float]:
        # Środek narożnego kwadratu bleed (5-15px od kąta strony)
        y_slice = slice(5, 15) if top else slice(-15, -5)
        x_slice = slice(5, 15) if left else slice(-15, -5)
        avg = arr[y_slice, x_slice, :].mean(axis=(0, 1))
        return tuple(float(c) for c in avg)

    tl = corner_avg(top=True, left=True)
    tr = corner_avg(top=True, left=False)
    bl = corner_avg(top=False, left=True)
    br = corner_avg(top=False, left=False)

    # Fixture: górny pasek pomarańczowy (0.95, 0.5, 0.17), boczne pasy
    # fioletowe (0.5, 0.3, 0.7) zarówno L jak i R, brak dolnego paska
    # → BL i BR powinny być fioletowe (drawing prawego pasa touches R+B)
    def is_orange(rgb):
        return rgb[0] > 200 and rgb[1] < 180 and rgb[2] < 100

    def is_purple(rgb):
        return rgb[0] > 80 and rgb[2] > 130 and rgb[2] > rgb[1]

    # TL: pomarańczowy pasek na wierzchu fioletowego (drawing[75] po drawing[5])
    # Ale my rysujemy w kolejności drawings: białe full → fioletowy boczny →
    # pomarańczowy górny → wynikowy stack na TL = pomarańczowy.
    assert is_orange(tl), f"TL narożnik powinien być pomarańczowy, jest {tl}"
    assert is_orange(tr), f"TR narożnik powinien być pomarańczowy, jest {tr}"
    # BL/BR: fioletowy boczny pas (left/right) touches L/R + B → fioletowy
    assert is_purple(bl), f"BL narożnik powinien być fioletowy, jest {bl}"
    assert is_purple(br), f"BR narożnik powinien być fioletowy, jest {br}"


def test_overlay_extensions_function_directly(tmp_path):
    """Bezpośredni test funkcji: dodaje operatory do content stream."""
    src = _make_xobject_nonuniform_pdf(tmp_path)
    doc = fitz.open(src)
    page = doc[0]
    bleed_pts = 2.0 * MM_TO_PT
    edge_x0, edge_y0 = 0.0, 0.0
    edge_x1, edge_y1 = page.rect.width, page.rect.height

    # Przed wywołaniem — content stream nie ma extension operatorów
    # (brak gwarancji bo Contents może już mieć rg/re f, ale liczymy długość)
    page.wrap_contents()
    contents_xref = page.xref
    contents_info = doc.xref_get_key(contents_xref, "Contents")
    import re as _re
    xref_match = _re.search(r'(\d+)\s+\d+\s+R', contents_info[1])
    content_xref = int(xref_match.group(1))
    before_len = len(doc.xref_stream(content_xref) or b'')

    overlay_edge_extensions(doc, page, bleed_pts, edge_x0, edge_y0, edge_x1, edge_y1)

    after_len = len(doc.xref_stream(content_xref) or b'')
    # Powinny dojść jakieś bajty (extensions)
    assert after_len > before_len, "overlay_edge_extensions nie dodał operatorów"
    doc.close()
