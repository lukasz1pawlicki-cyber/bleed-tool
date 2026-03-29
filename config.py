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
DEFAULT_MARK_ZONE_MM = 15          # mark_offset(12) + mark_size(3) = 15mm (JWEI/Summa)

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
    "mark_size_mm": (5, 5),
    "min_marks": 4,
    "mark_offset_mm": 10,
    "mark_zone_mm": 15,   # offset(10) + size(5) — pełna strefa wykluczenia
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

# Separation colorspace: DeviceCMYK alternate, Type 2 function
# CutContour → 100% magenta w alternate CMYK
SPOT_CMYK_CUTCONTOUR = (0, 1, 0, 0)
# FlexCut → cyan + yellow = zielony w alternate CMYK
SPOT_CMYK_FLEXCUT = (1, 0, 1, 0)

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
