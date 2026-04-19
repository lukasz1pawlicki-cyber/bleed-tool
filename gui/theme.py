"""
Bleed Tool — theme.py
=======================
Stale kolorow (Technikadruku palette) i loader QSS.

Paleta zgodna z handoff designu: navy #0F172A + blue #2563EB + warm neutrals.
"""

import os

# =============================================================================
# KOLORY — Technikadruku (uzywane w kodzie Python, nie w QSS)
# =============================================================================

# Akcenty
ACCENT = "#2563EB"          # blue
ACCENT_HOVER = "#1D4ED8"    # blue-hover
ACCENT_LIGHT = "#EFF6FF"    # blue-light
FUCHSIA = "#6D28D9"         # FlexCut accent
SUCCESS = "#059669"
SUCCESS_HOVER = "#047857"
ERROR = "#DC2626"
ERROR_HOVER = "#B91C1C"
WARN = "#D97706"

# Neutralne
NAVY = "#0F172A"
WHITE = "#FFFFFF"
BG = "#FAFBFC"
BG_WARM = "#F6F7F9"
BG_TINTED = "#F0F2F7"
BORDER = "#E2E5ED"
BORDER_STRONG = "#CBD0DC"
TEXT = "#111827"
TEXT_2 = "#4B5563"
TEXT_3 = "#6B7280"

# Compat (stary kod)
CARD_BG = WHITE
MAIN_BG = BG
SIDEBAR_BG = NAVY
TEXT_SECONDARY = TEXT_3
DROP_ZONE_BG = BG_WARM
DROP_ZONE_BORDER = BORDER_STRONG
LOG_BG = NAVY
LOG_FG = "#E5E7EB"

# Preview
PREVIEW_CUTCONTOUR = "#059669"
PREVIEW_FLEXCUT = "#DC2626"
PREVIEW_MARK = "#000000"


def load_theme(app):
    """Laduje QSS z pliku style.qss (Technikadruku).

    Probuje zaladowac fonty Instrument Sans + JetBrains Mono z
    gui/resources/fonts/ (jesli dostepne); w przeciwnym razie
    system fallback (Segoe UI / system mono).
    """
    from PyQt6.QtGui import QFontDatabase

    resources_dir = os.path.join(os.path.dirname(__file__), "resources")
    fonts_dir = os.path.join(resources_dir, "fonts")
    if os.path.isdir(fonts_dir):
        for fn in os.listdir(fonts_dir):
            if fn.lower().endswith((".ttf", ".otf")):
                QFontDatabase.addApplicationFont(os.path.join(fonts_dir, fn))

    qss_path = os.path.join(resources_dir, "style.qss")
    if not os.path.isfile(qss_path):
        return
    with open(qss_path, encoding="utf-8") as f:
        qss = f.read()
    app.setStyleSheet(qss)
