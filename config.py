"""
Bleed Tool — Konfiguracja
===========================
Stałe potrzebne do pipeline bleed + nest: contour → bleed → nest → panelize → marks → export.
"""

# =============================================================================
# DOMYŚLNE PARAMETRY
# =============================================================================

DEFAULT_BLEED_MM = 2.0
DEFAULT_GAP_MM = 5.0
DEFAULT_DPI = 300
DEFAULT_CROP_DPI = 300
DEFAULT_MARGINS_MM = (5, 5, 5, 5)  # top, right, bottom, left
DEFAULT_MARK_ZONE_MM = 13          # mark_offset(10) + mark_size(3) = 13mm (Summa S3)

# =============================================================================
# ROZMIARY ARKUSZY (mm)
# =============================================================================

# Presety arkuszy (stałe WxH) — wyświetlane w GUI
SHEET_PRESETS = {
    "SRA3": (320, 450),
    "SRA3+": (330, 480),
}

# Presety szerokości rolek (tylko szerokość, długość wynika z rozkładu)
ROLL_PRESETS = [1320]
DEFAULT_ROLL_MAX_LENGTH_MM = 3000

# Backward-compat alias (używany przez nesting fallback)
SHEET_SIZES = {
    "SRA3": (320, 450),
    "SRA3+": (330, 480),
    "A4": (210, 297),
    "A3": (297, 420),
    "A2": (420, 594),
    "A1": (594, 841),
}

# =============================================================================
# FLEXCUT
# =============================================================================

FLEXCUT_CUT_MM = 10.0       # Długość cięcia w perforacji
FLEXCUT_BRIDGE_MM = 1.0     # Długość mostka między cięciami
FLEXCUT_GAP_MM = 5.0        # Szerszy gap w miejscu linii FlexCut

# =============================================================================
# PLOTERY — PARAMETRY ZNACZNIKÓW
# =============================================================================

PLOTTER_SUMMA_S3 = {
    "mark_type": "opos_rectangle",
    "mark_size_mm": (3, 3),
    "min_marks": 4,
    "mark_offset_mm": 10,
    "mark_zone_mm": 13,   # offset(10) + size(3) — pełna strefa wykluczenia
    "leading_offset_mm": 20,  # odsunięcie grafiki od dolnych markerów (Y bottom)
    "side_offset_mm": 10,     # odsunięcie grafiki od lewych/prawych/górnych markerów
}

PLOTTER_JWEI = {
    "mark_type": "opos_rectangle",
    "mark_size_mm": (3, 3),
    "min_marks": 4,
    "mark_offset_mm": 12,
    "mark_zone_mm": 5,    # markery tylko w rogach — naklejki mogą sięgać pod nie
}

PLOTTERS = {
    "summa_s3": PLOTTER_SUMMA_S3,
    "jwei": PLOTTER_JWEI,
}

# =============================================================================
# SPOT COLORS
# =============================================================================

SPOT_COLOR_CUTCONTOUR = "CutContour"
SPOT_COLOR_FLEXCUT = "FlexCut"
SPOT_COLOR_WHITE = "White"
SPOT_COLOR_REGMARK = "Regmark"

# --- Cut PDF: bezpośredni DeviceCMYK na OCG warstwach (format pluginu Summa) ---
# GoSign czyta OCG layer names i mapuje na metody
CUT_CMYK_CUTCONTOUR = (1, 0, 1, 0)    # zielony (C+Y) — kiss-cut
CUT_CMYK_FLEXCUT = (0, 1, 1, 0)       # czerwony (M+Y) — thru-cut
CUT_CMYK_REGMARK = (0, 0, 0, 1)       # czarny (K) — regmark

# --- Print/White PDF: Separation spot colors (prepress, drukarka UV) ---
SPOT_CMYK_CUTCONTOUR = (1, 0, 1, 0)   # CutContour alternate
SPOT_CMYK_FLEXCUT = (0, 1, 1, 0)      # FlexCut alternate
SPOT_CMYK_WHITE = (0, 0.5, 0, 0)      # White alternate (różowy podgląd)
SPOT_CMYK_REGMARK = (0, 0, 0, 1)      # Regmark alternate

# White underprint inset — cofnięcie białego poddruku od linii cięcia (mm)
# Zapobiega wystaniu białego tuszu na krawędziach naklejki
WHITE_INSET_MM = 0.3

# =============================================================================
# PDF
# =============================================================================

CUTCONTOUR_STROKE_WIDTH_PT = 0.25   # Grubość linii CutContour
FLEXCUT_STROKE_WIDTH_PT = 0.25      # Grubość linii FlexCut

# =============================================================================
# KONWERSJA JEDNOSTEK
# =============================================================================

MM_TO_PT = 72.0 / 25.4   # 1mm = 2.8346pt
PT_TO_MM = 25.4 / 72.0   # 1pt = 0.3528mm
