"""
Bleed Tool — Modele danych
============================
Dataclasses dla pipeline bleed.
"""

from __future__ import annotations
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


_FORBIDDEN_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_stem(stem: str) -> str:
    """Usuwa znaki niedozwolone na Windows (<>:"/\\|?* + control chars).

    macOS/Linux pozwalaja np. na ':' w nazwie, ale Windows i niektore RIP-y
    odrzucaja. Bezpieczniej zamienic na '_' niz wybuchnac przy save().
    """
    sanitized = _FORBIDDEN_FILENAME_CHARS.sub("_", stem).strip().rstrip(".")
    return sanitized or "output"


def build_output_name(
    input_path: str,
    trim_w_mm: float,
    trim_h_mm: float,
    bleed_mm: float,
    page_index: int | None = None,
) -> str:
    """Zwraca nazwe pliku wyjsciowego wg konwencji:
        {stem}_PRINT_{W}x{H}mm_bleed{N}mm.pdf

    Dla wielostronicowych dodaje sufiks _p{N}:
        {stem}_p{N}_PRINT_{W}x{H}mm_bleed{N}mm.pdf
    """
    stem = _sanitize_stem(os.path.splitext(os.path.basename(input_path))[0])
    w = round(trim_w_mm)
    h = round(trim_h_mm)
    b = round(bleed_mm)
    if page_index is not None:
        return f"{stem}_p{page_index + 1}_PRINT_{w}x{h}mm_bleed{b}mm.pdf"
    return f"{stem}_PRINT_{w}x{h}mm_bleed{b}mm.pdf"


@dataclass
class Sticker:
    """Pojedyncza naklejka — grafika wektorowa + kontury + bleed.

    Pipeline: contour.py wypełnia pola konturu → bleed.py dodaje bleed.
    """
    source_path: str
    page_index: int = 0               # Indeks strony w PDF (0-based)
    width_mm: float = 0.0
    height_mm: float = 0.0

    # Kontur wektorowy (z contour.py):
    # Segmenty: [('l', start, end), ('c', p0, p1, p2, p3), ...]
    cut_segments: list = field(default_factory=list)

    # Bleed (z bleed.py, None dopóki nie przetworzony):
    bleed_segments: Optional[list] = None

    # Kolor krawędzi:
    edge_color_rgb: Optional[tuple] = None   # (r, g, b) 0-1
    edge_color_cmyk: Optional[tuple] = None  # (c, m, y, k) 0-1

    # Źródłowy PDF (potrzebny do show_pdf_page w exportie):
    pdf_doc: Optional[object] = None          # fitz.Document
    page_width_pt: float = 0.0
    page_height_pt: float = 0.0
    outermost_drawing_idx: Optional[int] = None

    # Ścieżka do tmp PDF wygenerowanego z EPS/SVG (do usunięcia przez konsumenta
    # po zamknięciu pdf_doc). None = oryginalny plik, nic do czyszczenia.
    # Ustawiane tylko na pierwszym stickerze z danego source — współdzielony doc.
    tmp_pdf_path: Optional[str] = None

    # Raster source (PNG/JPG/TIFF — alternatywa dla pdf_doc):
    raster_path: Optional[str] = None         # sciezka do oryginalnego pliku rastrowego
    raster_crop_box: Optional[tuple] = None   # (x, y, x2, y2) w px — crop do content area

    # Flaga: plik jest już gotowym outputem bleed (bleed_ prefix)
    # Eksport: nie rozszerzaj MediaBox, usuń CutContour ze źródłowego PDF
    is_bleed_output: bool = False

    # Typ linii cięcia: "kiss-cut" (CutContour) lub "flexcut" (FlexCut)
    cutline_mode: str = "kiss-cut"

    # Artwork-on-artboard: grafika mniejsza od strony, set_cropbox() zastosowany.
    # Export: nie modyfikuj MediaBox, użyj show_pdf_page z CropBox.
    is_artwork_on_artboard: bool = False

    # Colorspace źródłowego PDF (True = DeviceCMYK, False = DeviceRGB/inne)
    is_cmyk: bool = False

    # True = cut_segments pochodzi z linii cięcia w pliku źródłowym (stroke-only
    # drawing), nie z auto-detekcji. Export: render source (OCG stroke-only OFF)
    # + mask do bleed_segments (= cut_segments offset). Spad = real source content
    # w pasie bleed_mm poza cut line, reszta odrzucona (żadnej dilation/fake bleed).
    from_source_cutpath: bool = False

    # Bbox cutpath w coords oryginalnej strony (bb_x0, bb_y0, bb_x1, bb_y1) w pt.
    # Używane w export przy from_source_cutpath=True do rerenderu page.get_pixmap
    # z clip_rect odpowiednio rozszerzonym o bleed_mm.
    _src_cutpath_bbox: tuple[float, float, float, float] | None = None

    def __post_init__(self):
        if self.width_mm < 0 or self.height_mm < 0:
            log.warning(f"Sticker z ujemnymi wymiarami: {self.width_mm}×{self.height_mm}mm")
        if self.page_index < 0:
            log.warning(f"Sticker z ujemnym page_index: {self.page_index}")
        # Spójność is_cmyk
        if self.is_cmyk and self.edge_color_cmyk is None and self.edge_color_rgb is not None:
            log.debug("is_cmyk=True ale brak edge_color_cmyk — zostanie skorygowane w eksporcie")


@dataclass
class Placement:
    """Naklejka umieszczona na arkuszu."""
    sticker: Sticker
    x_mm: float                      # Pozycja lewego dolnego rogu
    y_mm: float
    rotation_deg: float = 0.0        # 0 lub 90

    def __post_init__(self):
        if self.rotation_deg not in (0, 0.0, 90, 90.0):
            log.warning(f"Placement z nietypową rotacją: {self.rotation_deg}° (oczekiwane: 0 lub 90)")


@dataclass
class PanelLine:
    """Linia podziału panelu (FlexCut). Zamknięte prostokąty."""
    axis: str                        # "horizontal" | "vertical"
    position_mm: float               # Pozycja Y (horizontal) lub X (vertical)
    start_mm: float = 0.0            # Początek linii (X dla horizontal, Y dla vertical)
    end_mm: float = 0.0              # Koniec linii
    bridge_length_mm: float = 1.0    # >0 = FlexCut

    def __post_init__(self):
        if self.axis not in ("horizontal", "vertical"):
            raise ValueError(f"PanelLine.axis musi być 'horizontal' lub 'vertical', jest: {self.axis!r}")


@dataclass
class Mark:
    """Znacznik rejestracji dla plotera."""
    x_mm: float
    y_mm: float
    width_mm: float = 3.0            # domyślne 3mm (standard OPOS)
    height_mm: float = 3.0
    mark_type: str = "opos_rectangle"  # "opos_rectangle" | "crosshair"
    is_bar: bool = False               # True = OPOS XY correction line

    def __post_init__(self):
        if self.width_mm <= 0 or self.height_mm <= 0:
            raise ValueError(f"Mark z nieprawidłowymi wymiarami: {self.width_mm}×{self.height_mm}mm")


@dataclass
class Sheet:
    """Arkusz z naklejkami, panelami i znacznikami."""
    width_mm: float
    height_mm: float
    placements: list[Placement] = field(default_factory=list)
    panel_lines: list[PanelLine] = field(default_factory=list)
    marks: list[Mark] = field(default_factory=list)
    margins_mm: tuple = (5, 5, 5, 5)   # top, right, bottom, left
    mark_zone_mm: float = 25            # Zarezerwowane na znaczniki (offset+mark+10mm gap)
    gap_mm: float = 3.0                 # Odstęp między naklejkami
    outer_bleed_mm: float = 0.0         # Zewnętrzny spad wokół grupy naklejek (0 = brak)

    def __post_init__(self):
        # height_mm=0 dozwolone dla roli — dynamicznie ustawiane w _finalize_sheet
        if self.width_mm <= 0 or self.height_mm < 0:
            raise ValueError(f"Sheet z nieprawidłowymi wymiarami: {self.width_mm}×{self.height_mm}mm")

    @property
    def printable_rect_mm(self) -> tuple:
        """Zwraca (x0, y0, x1, y1) obszaru drukowania po odjęciu marginesów i mark_zone."""
        top, right, bottom, left = self.margins_mm
        mz = self.mark_zone_mm
        x0 = left + mz
        y0 = bottom + mz
        x1 = self.width_mm - right - mz
        y1 = self.height_mm - top - mz
        return (x0, y0, x1, y1)

    @property
    def printable_width_mm(self) -> float:
        x0, y0, x1, y1 = self.printable_rect_mm
        return x1 - x0

    @property
    def printable_height_mm(self) -> float:
        x0, y0, x1, y1 = self.printable_rect_mm
        return y1 - y0

    @property
    def used_area_mm2(self) -> float:
        """Sumaryczna powierzchnia naklejek na arkuszu (z uwzglednieniem rotacji).

        Liczymy trim bbox (width_mm × height_mm) kazdego placement — bez bleedu.
        Dla rotacji 90° wymiary sa wymieniane.
        """
        total = 0.0
        for pl in self.placements:
            w = pl.sticker.width_mm
            h = pl.sticker.height_mm
            if pl.rotation_deg in (90, 90.0):
                w, h = h, w
            total += w * h
        return total

    @property
    def printable_area_mm2(self) -> float:
        """Powierzchnia obszaru drukowania (po odjeciu marginesow i mark_zone)."""
        return max(0.0, self.printable_width_mm * self.printable_height_mm)

    @property
    def utilization_percent(self) -> float:
        """Procent utylizacji materialu w obszarze drukowania (0-100).

        100% = caly printable area zajety naklejkami (idealne dopasowanie).
        Typowe wartosci: 70-90% dla dobrze ulozonych zestawow.
        Zwraca 0.0 gdy brak placements lub printable_area=0.
        """
        area = self.printable_area_mm2
        if area <= 0:
            return 0.0
        return min(100.0, 100.0 * self.used_area_mm2 / area)

    @property
    def sheet_area_mm2(self) -> float:
        """Pelna powierzchnia arkusza (wraz z marginesami)."""
        return max(0.0, self.width_mm * self.height_mm)

    @property
    def utilization_of_sheet_percent(self) -> float:
        """Procent utylizacji liczonej wobec pelnej powierzchni arkusza.

        Nizszy niz utilization_percent (bo wlicza marginesy + mark zone).
        Bardziej przydatny do realnej oceny marnowania materialu.
        """
        area = self.sheet_area_mm2
        if area <= 0:
            return 0.0
        return min(100.0, 100.0 * self.used_area_mm2 / area)


@dataclass
class Job:
    """Całe zlecenie — naklejki + arkusze."""
    stickers: list[tuple[Sticker, int]] = field(default_factory=list)  # (naklejka, ilość)
    sheets: list[Sheet] = field(default_factory=list)
    plotter: str = "summa_s3"         # "summa_s3" | "jwei"

    def __post_init__(self):
        from config import PLOTTERS
        if self.plotter not in PLOTTERS:
            log.warning(f"Nieznany ploter: {self.plotter!r}. Dostępne: {list(PLOTTERS.keys())}")
