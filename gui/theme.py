"""
Bleed Tool — theme.py
=======================
Stałe kolorów i loader QSS.
"""

import os

# =============================================================================
# KOLORY (używane w kodzie Python, nie w QSS)
# =============================================================================

ACCENT = "#4f6ef7"
ACCENT_HOVER = "#3b5bdb"
SUCCESS = "#37b24d"
SUCCESS_HOVER = "#2e7d32"
ERROR = "#e53935"
ERROR_HOVER = "#c62828"

CARD_BG = "#ffffff"
MAIN_BG = "#f1f3f5"
SIDEBAR_BG = "#f8f9fa"
TEXT = "#212529"
TEXT_SECONDARY = "#868e96"
DROP_ZONE_BG = "#f8f9fa"
DROP_ZONE_BORDER = "#ced4da"
LOG_BG = "#f8f9fa"
LOG_FG = "#212529"

# Preview
PREVIEW_CUTCONTOUR = "#00e676"
PREVIEW_FLEXCUT = "#ff1744"
PREVIEW_MARK = "#000000"


def load_theme(app):
    """Ładuje QSS z pliku style.qss."""
    qss_path = os.path.join(os.path.dirname(__file__), "resources", "style.qss")
    if os.path.isfile(qss_path):
        with open(qss_path, encoding="utf-8") as f:
            app.setStyleSheet(f.read())
