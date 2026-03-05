"""
Bleed Tool — Konfiguracja
===========================
Stałe potrzebne do pipeline bleed: contour → bleed → export.
"""

# =============================================================================
# DOMYŚLNE PARAMETRY
# =============================================================================

DEFAULT_BLEED_MM = 2.0
DEFAULT_DPI = 300

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
