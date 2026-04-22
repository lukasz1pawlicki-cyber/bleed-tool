"""
Bleed Tool — Konfiguracja
===========================
Stałe potrzebne do pipeline bleed + nest: contour → bleed → nest → panelize → marks → export.
"""

import os as _os

# =============================================================================
# DOMYŚLNE PARAMETRY
# =============================================================================

DEFAULT_BLEED_MM = 2.0
DEFAULT_GAP_MM = 0
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
    "675×600": (675, 600),
}

# Presety szerokości rolek (tylko szerokość, długość wynika z rozkładu)
ROLL_PRESETS = [1370]
DEFAULT_ROLL_MAX_LENGTH_MM = 1400

# Backward-compat alias (używany przez nesting fallback)
SHEET_SIZES = {
    "SRA3": (320, 450),
    "SRA3+": (330, 480),
    "675×600": (675, 600),
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
    "mark_offset_mm": 12,           # domyślny (używany jako fallback)
    "mark_offset_x_mm": 5,          # 5mm od lewej/prawej krawędzi papieru
    "mark_offset_y_mm": 50,         # 50mm od górnej/dolnej krawędzi papieru
    "mark_zone_mm": 5,              # strefa wykluczenia grafiki od krawędzi arkusza
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

# --- Cut PDF: OCG warstwy per ploter (bezpośredni DeviceCMYK) ---

# Summa S3 / GoSign: warstwy z pluginu Summa do CorelDraw
CUT_SUMMA_LAYERS = {
    "CutContour": {"ocg_name": "CutContour", "cmyk": (1, 0, 1, 0)},   # zielony — kiss-cut
    "FlexCut":    {"ocg_name": "FlexCut",    "cmyk": (0, 1, 1, 0)},   # czerwony — thru-cut
    "Regmark":    {"ocg_name": "Regmark",    "cmyk": (0, 0, 0, 1)},   # czarny
}

# JWEI / OptiScout: narzędzia SP1–SP8
# SP3 = nacinanie (kiss-cut), SP2 = rozcinanie (thru-cut)
CUT_JWEI_LAYERS = {
    "CutContour": {"ocg_name": "SP3",       "cmyk": (1, 0, 1, 0)},   # zielony — SP3 kiss-cut
    "FlexCut":    {"ocg_name": "SP2",        "cmyk": (0, 1, 1, 0)},   # czerwony — SP2 thru-cut
    "Regmark":    {"ocg_name": "regmarks",   "cmyk": (0, 0, 0, 1)},   # czarny
}

# Przypisanie cut_layers do ploterów (po definicji CUT_*_LAYERS)
PLOTTER_SUMMA_S3["cut_layers"] = CUT_SUMMA_LAYERS
PLOTTER_JWEI["cut_layers"] = CUT_JWEI_LAYERS

# Backward-compat aliases (używane przez stary kod)
CUT_CMYK_CUTCONTOUR = CUT_SUMMA_LAYERS["CutContour"]["cmyk"]
CUT_CMYK_FLEXCUT = CUT_SUMMA_LAYERS["FlexCut"]["cmyk"]
CUT_CMYK_REGMARK = CUT_SUMMA_LAYERS["Regmark"]["cmyk"]

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

# Tolerancja porównań zmiennoprzecinkowych (mm)
# Kompensuje błędy zaokrągleń PDF pt→mm (np. 100.00001mm zamiast 100mm)
FLOAT_TOLERANCE_MM = 0.01

# =============================================================================
# CONTOUR ENGINE — silnik detekcji konturu rastrowego
# =============================================================================
# "opencv" — cv2.findContours (C, 5–35× szybsze, wymaga opencv-python) — DEFAULT
# "moore"  — Moore boundary tracing (Python, zero extra deps, fallback)
# "auto"   — użyj opencv jeśli dostępne, fallback na moore
#
# Benchmark (2000×2000 px naklejka): moore 37ms vs opencv 1.1ms (34× speedup).
# Jakość identyczna — oba silniki zwracają ten sam kontur co do piksela.
# opencv jest w requirements.txt, więc domyślnie zawsze dostępny. Przy braku
# cv2 dispatcher (_boundary_trace) robi automatyczny fallback na moore.
# Zmienna środowiskowa BLEED_CONTOUR_ENGINE nadpisuje tę wartość.
CONTOUR_ENGINE = _os.environ.get("BLEED_CONTOUR_ENGINE", "opencv").lower()


# =============================================================================
# RASTER MODE — tryb detekcji konturu dla plików rastrowych (PNG/JPG/TIFF)
# =============================================================================
# "smooth" — DP + Chaikin + Catmull-Rom Bezier (DEFAULT)
#            Dla organicznych kształtów (logotypy, ilustracje Canva-style,
#            okrągłe naklejki) — wygładza krawędzie do przyjemnych krzywych.
# "sharp"  — DP + linie proste (bez Chaikin, bez Bezier)
#            Dla geometrycznych kształtów (gwiazdki, strzałki, zygzaki,
#            diamenty, logo z ostrymi kątami) — zachowuje ostre narożniki
#            1:1 z input.
#
# Operator wybiera tryb per-zadanie: smooth dla bunnyego, sharp dla gwiazdki.
# Zmienna srodowiskowa BLEED_RASTER_MODE nadpisuje tę wartosc.
# GUI i CLI maja przełączniki (CLI: --sharp-edges flag).
RASTER_MODE = _os.environ.get("BLEED_RASTER_MODE", "smooth").lower()


# =============================================================================
# RASTER CONTOUR MODE — tryb detekcji obrysu dla rastrowych naklejek
# =============================================================================
# "standard" — threshold alpha 50, bez morfologii (DEFAULT)
#              Dla czystych plików rastrowych z widoczną granicą naklejki
#              (domyślny dotychczasowy algorytm).
# "glow"     — threshold 30 + binary_closing (5 iter)
#              Dla plików z rozmytą poświatą/glow wokół naklejki. Niski
#              threshold łapie cały halo, closing łączy rozproszone
#              komponenty (np. gwiazdki dookoła postaci Anieli) w jedną
#              otoczkę do obrysu.
# "tight"    — threshold 150, bez morfologii
#              Dla plików gdzie linia cięcia ma iść BLISKO widocznej treści
#              (ignoruje faint shadow / pół-przezroczyste tło).
#
# Zmienna środowiskowa BLEED_RASTER_CONTOUR_MODE nadpisuje tę wartość.
# GUI ma przełącznik "Obrys kształtu".
RASTER_CONTOUR_MODE = _os.environ.get(
    "BLEED_RASTER_CONTOUR_MODE", "standard"
).lower()


# =============================================================================
# ALPHA CONTOUR METHOD — algorytm śledzenia konturu z alpha mask
# =============================================================================
# "moore"   — Moore boundary tracing (domyślne). Chodzi po krawędzi piksel-po-
#             pikselu, prawidłowo śledzi wklęsłości i nieregularne kształty
#             (postacie z kończynami, halo, ilustracje).
# "rowscan" — leftmost/rightmost per wiersz. Szybsze, dobre dla idealnie
#             okrągłych/owalnych naklejek bez wklęsłości (fit_circle).
#
# Zmienna środowiskowa BLEED_ALPHA_CONTOUR_METHOD nadpisuje tę wartość.
# GUI Bleed ma przełącznik "Obrys" (widoczny dla plików rastrowych/PDF).
ALPHA_CONTOUR_METHOD = _os.environ.get(
    "BLEED_ALPHA_CONTOUR_METHOD", "moore"
).lower()


# =============================================================================
# RASTER CONTOUR SHRINK — cofnięcie linii cięcia do wewnątrz
# =============================================================================
# Liczba mm o jaką cofnąć obrys konturu do wewnątrz alpha-mask. 0 = bez cofania.
# Użycie: pliki z halo/glow/shadow gdzie zewnętrzna granica alpha to halo,
# a prawdziwa linia cięcia ma iść po wewnętrznej krawędzi halo (na granicy
# halo/grafika). Np. stickery z Canva: halo 3mm → shrink 3.0 daje cięcie
# dokładnie po obrysie rysunku.
#
# Implementacja: scipy.ndimage.binary_erosion przed boundary trace.
# Dla plików bez halo: 0 (domyślne) — nie obcinać krawędzi grafiki.
#
# Zmienna środowiskowa BLEED_RASTER_CONTOUR_SHRINK_MM nadpisuje tę wartość.
RASTER_CONTOUR_SHRINK_MM = float(
    _os.environ.get("BLEED_RASTER_CONTOUR_SHRINK_MM", "0")
)


# =============================================================================
# PDF METADATA ENGINE — silnik zapisu PDF/X-4 OutputIntent
# =============================================================================
# "pymupdf" — PyMuPDF xref manipulation (default, zero extra deps)
# "pikepdf" — pikepdf.OutputIntent (czystsze API, wymaga pikepdf)
#
# Zmienna środowiskowa BLEED_PDF_METADATA_ENGINE nadpisuje tę wartość.
PDF_METADATA_ENGINE = _os.environ.get("BLEED_PDF_METADATA_ENGINE", "pymupdf").lower()


# =============================================================================
# RGB → CMYK KONWERSJA (opcjonalna, postprocess przez Ghostscript)
# =============================================================================
# Jeśli True, po wygenerowaniu PDF z bleedem zostanie uruchomiony Ghostscript
# który skonwertuje RGB → CMYK używając ICC FOGRA39. Spot colors są zachowane.
#
# Zalety:
#   - Wszystkie kolory wyjściowe w CMYK (drukarka UV nie musi konwertować)
#   - Konwersja wysokiej jakości (ICC rendering intent = RelativeColorimetric)
# Wady:
#   - Wymaga Ghostscript w PATH
#   - Dodaje 2-10s na plik
#
# Zmienna środowiskowa BLEED_RGB_TO_CMYK włącza konwersję.
RGB_TO_CMYK_POSTPROCESS = _os.environ.get("BLEED_RGB_TO_CMYK", "0").lower() in ("1", "true", "yes")
RGB_TO_CMYK_RENDERING_INTENT = _os.environ.get(
    "BLEED_CMYK_INTENT", "RelativeColorimetric"
)


# =============================================================================
# SNAP WYMIARÓW (bleed.py)
# =============================================================================
# Dociąganie wymiarów naklejek do pełnych rozmiarów (eliminuje białe gap-y)
SNAP_STEP_MM = 0.5        # Siatka zaokrąglenia
SNAP_TOLERANCE_MM = 0.05  # Max odchylenie żeby dociągnąć

# =============================================================================
# OPOS REGMARKS (marks.py, Summa S3)
# =============================================================================
# Parametry z pluginu Summa GoSign Tools (gosign_opos_regmarks_base.py)
REGMARK_SIZE_MM = 3
REGMARK_MARGIN_LR_MM = REGMARK_SIZE_MM * 4   # 12mm (4× size)
REGMARK_MARGIN_TB_MM = REGMARK_SIZE_MM        # 3mm
REGMARK_DIST_MM = 400                         # max odległość między markerami Y
OPOS_XY_MARGIN_MM = 10                        # gap bar ↔ narożnik
OPOS_XY_HEIGHT_MM = 3                         # wysokość bara

# =============================================================================
# ICC PROFILE
# =============================================================================

ICC_SEARCH_PATHS = [
    # macOS — Adobe Creative Cloud / Creative Suite
    "/Library/Application Support/Adobe/Color/Profiles/Recommended/CoatedFOGRA39.icc",
    # macOS — system ColorSync
    "/Library/ColorSync/Profiles/CoatedFOGRA39.icc",
    # macOS — user ColorSync
    _os.path.expanduser("~/Library/ColorSync/Profiles/CoatedFOGRA39.icc"),
    # Windows
    _os.path.expandvars(r"%WINDIR%\System32\spool\drivers\color\CoatedFOGRA39.icc"),
    # Linux
    "/usr/share/color/icc/ghostscript/CoatedFOGRA39.icc",
    "/usr/share/ghostscript/icc/CoatedFOGRA39.icc",
    # Lokalny katalog projektu
    _os.path.join(_os.path.dirname(__file__), "profiles", "CoatedFOGRA39.icc"),
]

# =============================================================================
# PROFILES LOADER — external profiles/output_profiles.json
# =============================================================================
# Po imporcie: ładuje JSON i nadpisuje PLOTTERS (in-place). Brak pliku = noop.
# Odłożone do końca pliku żeby PLOTTERS były już zdefiniowane.

try:
    from modules.profiles import apply_profiles_to_config as _apply_profiles
    import sys as _sys
    _apply_profiles(_sys.modules[__name__])
    # Po scaleniu: odśwież cache'owane aliasy CUT_CMYK_*
    if "summa_s3" in PLOTTERS and "cut_layers" in PLOTTERS["summa_s3"]:
        _summa_layers = PLOTTERS["summa_s3"]["cut_layers"]
        CUT_CMYK_CUTCONTOUR = _summa_layers.get("CutContour", {}).get("cmyk", CUT_CMYK_CUTCONTOUR)
        CUT_CMYK_FLEXCUT = _summa_layers.get("FlexCut", {}).get("cmyk", CUT_CMYK_FLEXCUT)
        CUT_CMYK_REGMARK = _summa_layers.get("Regmark", {}).get("cmyk", CUT_CMYK_REGMARK)
except Exception as _e:  # pragma: no cover — defensive, nie blokuj importu
    import logging as _logging
    _logging.getLogger(__name__).warning(
        f"Profiles loader failed: {_e} — używam hardcoded PLOTTERS"
    )
