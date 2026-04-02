"""
Bleed Tool — theme.py
=======================
Stale kolorow i loader QSS.
Ikony (strzalka dropdown, checkmark) generowane w pamieci jako temp PNG
— zero zewnetrznych plikow graficznych.
"""

import os
import tempfile

# =============================================================================
# KOLORY (uzywane w kodzie Python, nie w QSS)
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

# Katalog tymczasowy na ikony (tworzony raz, zyje do konca procesu)
_icon_dir: str | None = None


def _ensure_icons() -> str:
    """Generuje ikony PNG w katalogu tymczasowym (raz na sesje).

    Returns:
        sciezka do katalogu z ikonami (forward slashes dla Qt url())
    """
    global _icon_dir
    if _icon_dir and os.path.isdir(_icon_dir):
        return _icon_dir.replace("\\", "/")

    _icon_dir = tempfile.mkdtemp(prefix="bleedtool_icons_")

    from PIL import Image, ImageDraw

    # Strzalka dropdown (chevron down) — 12x12
    img = Image.new("RGBA", (12, 12), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.polygon([(2, 4), (10, 4), (6, 9)], fill=(110, 110, 115, 255))
    img.save(os.path.join(_icon_dir, "chevron-down.png"))

    # Checkmark — 14x14
    img2 = Image.new("RGBA", (14, 14), (0, 0, 0, 0))
    d2 = ImageDraw.Draw(img2)
    d2.line([(3, 7), (6, 10), (11, 3)], fill=(255, 255, 255, 255), width=2)
    img2.save(os.path.join(_icon_dir, "check.png"))

    return _icon_dir.replace("\\", "/")


def load_theme(app):
    """Laduje QSS z pliku style.qss.

    Placeholder {{ICONS}} w QSS zamieniany na sciezke do wygenerowanych ikon
    (forward slashes — wymagane przez Qt url()).
    """
    qss_path = os.path.join(os.path.dirname(__file__), "resources", "style.qss")
    if not os.path.isfile(qss_path):
        return

    icons_dir = _ensure_icons()

    with open(qss_path, encoding="utf-8") as f:
        qss = f.read()

    qss = qss.replace("{{ICONS}}", icons_dir)
    app.setStyleSheet(qss)
