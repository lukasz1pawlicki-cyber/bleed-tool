"""
Bleed Tool — Modele danych
============================
Dataclasses dla pipeline bleed.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


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

    # Raster source (PNG/JPG/TIFF — alternatywa dla pdf_doc):
    raster_path: Optional[str] = None         # sciezka do oryginalnego pliku rastrowego

    # Flaga: plik jest już gotowym outputem bleed (bleed_ prefix)
    # Eksport: nie rozszerzaj MediaBox, usuń CutContour ze źródłowego PDF
    is_bleed_output: bool = False

    # Colorspace źródłowego PDF (True = DeviceCMYK, False = DeviceRGB/inne)
    is_cmyk: bool = False

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
