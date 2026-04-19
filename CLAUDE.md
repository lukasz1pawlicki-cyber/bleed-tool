# CLAUDE.md — StickerPrep: Przygotowanie naklejek do druku

## Cel programu

> **Wejście:** plik graficzny (wektorowy lub rastrowy)
> **Wyjście:** Plik PDF z 2mm spadem i wektorową linią cięcia (spot color CutContour)

Program zastępuje ręczne przygotowanie pliku w Illustratorze. Operator wrzuca plik, klika „Przygotuj" — dostaje gotowy PDF do rozłożenia na arkuszu.

---

## Park maszynowy

| Maszyna | Rola |
|---|---|
| Mimaki UCJV 300-160 | Druk UV — naklejki, etykiety |
| Summa S3 T160 | Ploter tnący — czyta CutContour |
| JWEI CTE-1606H | Cyfrowy stół tnący — alternatywny eksport |

---

## Obecny stan — co działa (NIE PSUĆ)

Poniższe moduły są przetestowane i działają produkcyjnie. Każda zmiana musi zachować ich funkcjonalność.

### Pipeline (3 kroki):
```
detect_contour() → generate_bleed() → export_single_sticker() / export_sheet()
```

### `modules/contour.py` — Detekcja konturu
- **Wektor PDF:** `find_outermost_drawing()` + `extract_path_segments()` → linie + krzywe Bézier
- **TrimBox:** `_crop_to_trimbox()` → pliki ze spadami (MediaBox != TrimBox) → crop do TrimBox
- **Artwork-on-artboard:** `_get_images_bbox()` + `_is_artwork_on_artboard()` → grafika mniejsza niż strona
- **Okrąg:** `_fit_circle()` + `_circle_to_bezier_segments()` → 4 krzywe Bézier (k≈0.5523)
- **Alpha contour:** `_render_alpha_contour()` → render z alpha, wykrywanie granicy, circle fitting
- **Raster:** `_detect_raster()` → PNG/JPG/TIFF → prostokątny kontur + edge color
- **SVG:** cairosvg → PDF → standardowy pipeline

### `modules/bleed.py` — Generowanie spadu
- `offset_segments()` → flatten → offset per-vertex (normalne) → refit Bézier
- `_fit_cubic_bezier()` → least-squares refit po offsetcie
- `rgb_to_cmyk_icc()` → FOGRA39 ICC + fallback UCR
- `extract_edge_color()` → kolor z outermost drawing (fill → stroke → white)

### `modules/export.py` — Eksport PDF (3 warstwy)
1. **Warstwa bleed:** `build_rgb_fill_stream()` — RGB solid fill z bleed_segments
2. **Warstwa grafiki:** `show_pdf_page()` z rozszerzonym MediaBox (wektor) LUB raster z dilation (raster)
3. **Warstwa CutContour:** `build_cutcontour_stream()` — spot color Separation

Dodatkowo:
- `inject_page_boundary_clip()` — maskuje markery cięcia (pliki z TrimBox)
- `expand_clip_paths()` — rozszerza clip paths o bleed (prostokąty, polygony)
- Expanded canvas + nearest-neighbor dilation — bleed śledzi kształt (okrągłe naklejki)
- Sheet layout: placements, panel lines (FlexCut), registration marks

### `models.py` — Dataclasses
- `Sticker` — grafika + kontur + bleed + source PDF
- `Placement` — pozycja na arkuszu
- `Sheet` — arkusz z naklejkami, panelami, znacznikami
- `Mark`, `PanelLine` — znaczniki rejestracji, linie FlexCut

### GUI: `bleed_app.py` (PyQt6 + QSS)
- Sidebar nawigacyjny (`gui/main_window.py`) + QStackedWidget: **dwie zakładki Bleed i Nest**
- Każda zakładka ma **niezależny panel podglądu** (`gui/preview_panel.py`) — stan Bleed i stan Nest nie nadpisują się nawzajem
- Bleed preview: split-view on (przed/po); Nest preview: split-view off (brak oryginału dla arkusza)
- Zakładki: `gui/bleed_tab.py`, `gui/nest_tab.py`; wspólny pasek plików `gui/file_section.py`; log `gui/log_panel.py`; dialog FlexCut `gui/flexcut_dialog.py`
- Drag & drop (native QWidget) + kliknięcie do wyboru
- QThread workers: `BleedWorker`, `NestWorker` (`gui/workers.py`)
- Theming: `gui/theme.py` + `gui/resources/style.qss` (QSS)
- Auto-agregacja: po Bleed wyniki automatycznie trafiają na listę Nest

### CLI: `bleed_cli.py`
- `python bleed_cli.py input.pdf -o ./out --bleed 2`
- Batch: `python bleed_cli.py --batch ./folder -o ./out [--recursive]`
- Równoległy batch: `-j N` (0 = auto = liczba CPU) — ProcessPoolExecutor, 4-8x speedup na multi-core
- Projekty: `--project plik.bleedproj` (ładuj sesję) / `--save-project plik.bleedproj` (zapisz)
- Preflight gate: `--preflight off|lenient|strict` (domyślnie `lenient` — blokuje błędy)
- Cache: `--no-cache` (wyłącz dla pojedynczego uruchomienia) / `--clear-cache` (wyczyść i zakończ)
- `--fail-fast` (przerwij przy pierwszym błędzie) / `--overwrite` (nadpisz zamiast suffix `_v2`)
- Output naming: `{stem}_PRINT_{W}x{H}mm_bleed{N}mm.pdf` (`build_output_name` w `models.py`)

---

## Stack technologiczny

### Obecne zależności (requirements.txt):
```
PyMuPDF>=1.24.0      # import fitz — PDF: render, rysowanie, ekstrakcja
numpy>=1.20.0        # operacje geometryczne, piksele
Pillow>=8.0.0        # raster I/O, ICC color transform
PyQt6>=6.5.0         # GUI (drag&drop, QThread workers)
cairosvg>=2.5.0      # SVG → PDF
opencv-python>=4.5.0 # detekcja krawędzi (CONTOUR_ENGINE=auto/opencv)
```

### Stack:

| Narzędzie | Rola | Status |
|---|---|---|
| **PyMuPDF** | PDF: render, rysowanie, ekstrakcja, xref manipulation | JEST |
| **PyQt6** | GUI, drag&drop, QThread workers | JEST |
| **Ghostscript** | EPS→PDF (`modules/ghostscript_bridge.py`) + opcjonalny RGB→CMYK postprocess | JEST (zewn. binarny) |
| **Pillow + NumPy** | Obróbka rastrów, ICC FOGRA39 transform | JEST |
| **OpenCV** | Detekcja krawędzi raster (domyślnie przez `CONTOUR_ENGINE=auto`) | JEST |
| **pikepdf** | Opcjonalny backend zapisu PDF/X-4 (`BLEED_PDF_METADATA_ENGINE=pikepdf`) | JEST (opcjonalne) |

### Praca na rastrach wewnątrz PDF:
```
PyMuPDF page.get_pixmap(dpi=300) → Pillow obróbka → NumPy operacje pikselowe
→ OpenCV detekcja krawędzi → wynik z powrotem: PyMuPDF osadza bitmapę w nowym PDF
```

---

## Obsługiwane formaty wejściowe

| Format | Typ | Biblioteka | Status |
|---|---|---|---|
| `.pdf` | Wektor | PyMuPDF | DZIAŁA |
| `.ai` | Wektor (Adobe Illustrator) | PyMuPDF (jeśli zapisany jako PDF) | DZIAŁA |
| `.svg` | Wektor | CairoSVG → PDF → PyMuPDF | DZIAŁA |
| `.eps`, `.epsf` | Wektor | Ghostscript → PDF (modules/ghostscript_bridge.py) | DZIAŁA |
| `.png`, `.jpg`, `.tiff`, `.webp`, `.bmp` | Raster | Pillow → PyMuPDF | DZIAŁA |

---

## Architektura — struktura projektu

```
bleed-tool/
├── CLAUDE.md                      ← ten plik
├── INSTALACJA.txt                 ← instrukcja instalacji (PL)
├── bleed_app.py                   ← GUI entry point (PyQt6)
├── bleed_cli.py                   ← CLI (single + --batch + -j N + --project + --preflight)
├── config.py                      ← stałe (DEFAULT_BLEED_MM, SPOT_COLORS, PLOTTERS, CONTOUR_ENGINE, PDF_METADATA_ENGINE)
├── models.py                      ← dataclasses (Sticker, Placement, Sheet, Mark, PanelLine) + build_output_name
├── gui/
│   ├── main_window.py             ← QMainWindow — sidebar + Bleed/Nest tabs + 2x PreviewPanel
│   ├── bleed_tab.py               ← zakładka Bleed (plik → bleed → preview)
│   ├── nest_tab.py                ← zakładka Nest (pliki → arkusz → preview)
│   ├── file_section.py            ← wspólny pasek plików (drag & drop)
│   ├── preview_panel.py           ← podgląd PDF (split-view przed/po, crop offset)
│   ├── log_panel.py               ← panel logów
│   ├── flexcut_dialog.py          ← dialog konfiguracji FlexCut
│   ├── workers.py                 ← QThread: BleedWorker, NestWorker (async preview)
│   ├── theme.py                   ← loader QSS
│   └── resources/
│       └── style.qss              ← theming
├── modules/
│   ├── contour.py                 ← detekcja konturu (wektor, raster, circle, alpha; silniki moore/opencv/auto)
│   ├── bleed.py                   ← offset konturu + kolor krawędzi (ICC FOGRA39)
│   ├── export.py                  ← eksport PDF (sticker + sheet, OCG layers per ploter)
│   ├── svg_convert.py             ← SVG → PDF (cairosvg)
│   ├── file_loader.py             ← abstrakcja loadera (routing formatów: EPS, SVG, PDF, raster)
│   ├── crop.py                    ← crop do wysokości (sticker + maski circle/rounded/oval)
│   ├── preflight.py               ← gate przed eksportem (DPI, tryb koloru, rozmiar)
│   ├── nesting.py                 ← shelf-based nesting, 3 grouping modes + utylizacja
│   ├── panelize.py                ← linie paneli (FlexCut) w arkuszu
│   ├── marks.py                   ← znaczniki OPOS (Summa) / 4 narożne (JWEI)
│   ├── pdf_metadata.py            ← PDF/X-4 OutputIntent FOGRA39 + TrimBox/BleedBox (dual backend pymupdf/pikepdf)
│   ├── profiles.py                ← loader profili per maszyna (profiles/output_profiles.json)
│   ├── project.py                 ← format .bleedproj (zapis/odczyt sesji operatora)
│   ├── cache.py                   ← disk cache dla detect_contour() (sha1 + pickle)
│   └── ghostscript_bridge.py      ← EPS → PDF + opcjonalny RGB→CMYK postprocess
├── profiles/
│   ├── CoatedFOGRA39.icc          ← ICC profile (opcjonalnie)
│   └── output_profiles.json       ← profile per ploter (Summa S3 / JWEI / Mimaki)
├── tests/                         ← 140+ testów: contour, bleed, export, cache, profiles, projekty, preview, pipeline
├── requirements.txt
├── uruchom.bat                    ← launcher Windows
├── uruchom.command                ← launcher macOS/Linux
└── pakuj.bat                      ← pakowanie releasu (Windows)
```

---

## Pipeline przetwarzania (szczegółowy)

```
INPUT FILE (PDF/SVG/EPS/PNG/JPG)
  │
  ▼
contour.detect_contour()        → list[Sticker]
                                  (dispatch wewnętrzny: svg_convert dla SVG,
                                   ghostscript_bridge dla EPS, bezpośredni PDF,
                                   _detect_raster dla rastrów)
  │                               • _crop_to_trimbox() — pliki ze spadami
  │                               • find_outermost_drawing() — wektor
  │                               • _render_alpha_contour() — raster-only PDF
  │                               • _detect_raster() — pliki rastrowe
  │
  ▼
bleed.generate_bleed()          → Sticker.bleed_segments
  │                               • offset_segments() — normalne + refit
  │                               • extract_edge_color() → RGB + CMYK (ICC)
  │
  ▼
export.export_single_sticker()  → PDF z 3 warstwami:
  │                               1. RGB fill (bleed_segments)
  │                               2. Grafika oryginalna (wektor/raster)
  │                               3. CutContour (spot color Separation)
  │
  ▼
pdf_metadata.apply_pdfx4()      → OutputIntent FOGRA39 (ICC) + Box'y:
  │                               • TrimBox = MediaBox − bleed (rozmiar naklejki)
  │                               • BleedBox = MediaBox (pełny obszar ze spadem)
  │                               • CropBox = MediaBox (dla Xerox RIP)
  │                               • XMP metadata z deklaracją PDF/X-4
  │
  ▼
OUTPUT: {nazwa}_PRINT_{W}x{H}mm_bleed{N}mm.pdf
```

---

## Definicja koloru spot CutContour

```
CutContour: spot color → Separation → DeviceCMYK alternate
  • stroke: 0.25 pt (cienka linia)
  • 100% tint → CMYK (1, 0, 1, 0) — zielony alternate (widoczny na ekranie)
  • Cutter (Summa S3) czyta spot name "CutContour" i ignoruje kolor

FlexCut: spot color → Separation
  • CMYK alternate: (0, 1, 1, 0) — czerwony (magenta+yellow)
  • Dla linii paneli z mostkami (bridge_length_mm > 0)
```

Implementacja w `export.py`: `setup_separation_colorspace()` + `_create_separation()`.

---

## Kluczowe algorytmy — NIE ZMIENIAJ bez dobrego powodu

### 1. Offset konturu (bleed.py)
```
Segments → flatten_to_polyline(30 pts/curve) → offset_polyline(normals) → refit_cubic_bezier
```
- Normalne obliczone z tangent sąsiadów
- Kierunek: centroid test (dot product)
- Refit: chord-length parametryzacja + least-squares

### 2. Circle detection (contour.py)
```
Alpha render → boundary points → fit_circle (least squares) → is_circular (5% tol) → 4 Bézier
```

### 3. Pipeline raster RGBA (contour.py + export.py) — NIE ZMIENIAJ

#### 3a. Detekcja konturu — Moore boundary tracing
```
RGBA → Gaussian blur → threshold alpha>50 → Moore neighborhood trace → DP → Chaikin → Bézier
```
**KRYTYCZNE**: Jedyna poprawna metoda detekcji konturu z PNG z poświatą/glow.
- `_moore_boundary_trace()` — chodzi po krawędzi piksel po pikselu (8-connected clockwise)
- Prawidłowo śledzi wklęsłości (między nogami, nad głową) — row-scan ich NIE WIDZI
- Threshold alpha > 50 = widoczna treść + biała obwódka (NIE cały glow)
- Gaussian blur przed skalowaniem wygładza krawędzie
- Douglas-Peucker (epsilon ~1% rozmiaru) → uproszczony polygon
- Chaikin's corner cutting (2 iteracje) → wygładza narożniki polygonu
- Min-dist filter (2× min_dist_pt ≈ 36pt) → redukuje nadmiar punktów po Chaikinie
- Catmull-Rom → cubic Bézier → gładka linia cięcia (26-38 segmentów)
- Nie używaj: morfologii (kurczy kształt), row-scan left/right (traci wklęsłości),
  threshold < 50 (łapie niewidoczny glow), threshold > 128 (obcina dolne krawędzie),
  usuwania Chaikina (wrócą ostre narożniki/cusps z Catmull-Rom)

#### 3b. Crop do cut bbox — wymiary stickera
```
cut_segments bbox (ON-CURVE points only) → sticker dimensions → PDF = sticker + 2×bleed
```
**KRYTYCZNE**: Wymiary stickera = DOKŁADNIE bounding box linii cięcia (p0, p3).
- Bbox liczymy TYLKO z on-curve points (p0, p3), NIE z control points (cp1, cp2)
  — control points leżą poza krzywą i zawyżają bbox
- raster_crop_box = cut bbox w pikselach (bez marginesu — eksport sam rozszerza canvas)
- Segmenty przesunięte do origin (0,0) po cropie
- Bleed line = krawędź PDF (zweryfikowane matematycznie: bleed_bbox = page_size)
- Nie używaj: alpha>0 do crop (glow sięga krawędzi obrazu → brak cropa),
  marginesu wokół cut bbox (tworzy białe pole), control points do bbox

#### 3c. Export raster — bleed via dilation
```
RGBA → composite na białe tło → expanded canvas → nearest-neighbor dilation → mask
```
Composite na białe tło PRZED dilation — glow blenduje się z białym (jak na winylu).
Dilation rozszerza kolory krawędzi (biała obwódka) na bleed zone.
Maska z bleed_segments ogranicza do gładkiego kształtu.

### 4. Boundary clip injection (export.py)
```
inject_page_boundary_clip() → clip path w CropBox coords → maskuje markery cięcia
```
Dla plików z TrimBox != MediaBox. Współrzędne w przestrzeni MediaBox (surowy content stream).

### 5. Expanded MediaBox (export.py, vector path)
```
expand_clip_paths() → rozszerz clipping paths
set_mediabox(cropbox ± bleed) → show_pdf_page()
```
MediaBox musi być wokół CropBox, nie wokół (0,0).

---

## Konwersja jednostek

```python
MM_TO_PT = 72.0 / 25.4  # = 2.834645669
PT_TO_MM = 25.4 / 72.0  # = 0.352777...
```

Publiczne API: **milimetry**. Wewnętrznie: **punkty PDF (pt)**.

---

## Nazewnictwo pliku wyjściowego

Zaimplementowane w `models.build_output_name()`:

```python
def build_output_name(input_path: Path, trim_w_mm: float, trim_h_mm: float, bleed_mm: float) -> str:
    w = round(trim_w_mm)
    h = round(trim_h_mm)
    b = round(bleed_mm)
    return f"{input_path.stem}_PRINT_{w}x{h}mm_bleed{b}mm.pdf"
```

---

## Zrealizowane funkcjonalności

Pipeline:
- BleedBox/TrimBox/CropBox w output PDF (`modules/pdf_metadata.py`, dual backend PyMuPDF/pikepdf)
- PDF/X-4 OutputIntent FOGRA39 (idempotentny)
- Output naming convention `_PRINT_{W}x{H}mm_bleed{N}mm.pdf`
- `file_loader.py` — eksplicytna abstrakcja loadera (routing EPS/SVG/PDF/raster)
- `ghostscript_bridge.py` — EPS → PDF + opcjonalny RGB→CMYK postprocess
- Profile eksportu per maszyna (`profiles/output_profiles.json` + `modules/profiles.py`)
- Silnik detekcji konturu: `moore` / `opencv` / `auto` (domyślnie `auto`)

GUI:
- PyQt6 + QSS, podział na zakładki Bleed i Nest (sidebar nawigacyjny)
- Niezależne panele podglądu dla Bleed i Nest (stan się nie nadpisuje)
- Split-view (przed/po) w zakładce Bleed
- Async preview (QThread workers) — UI nie zamraża się podczas przetwarzania
- Auto-agregacja outputów Bleed → lista Nest

CLI / workflow:
- Batch processing + równoległe procesy (`-j N`, `ProcessPoolExecutor`)
- Projekty `.bleedproj` — zapis/odczyt sesji (`--project` / `--save-project`)
- Preflight gate (`--preflight off|lenient|strict`)
- Disk cache dla `detect_contour()` (sha1 + pickle, invalidacja po mtime/engine)

Testy: 140+ testów (contour, bleed, export, cache, profiles, projekty, preview, pipeline) — `tests/fixtures.py` generuje pliki w `tmp_path` (brak binariów w repo).

---

## Zasady kodowania

- Python 3.10+, type hints
- Dataclasses dla konfiguracji i wyników
- **Nigdy nie nadpisuj pliku wejściowego**
- Jednostki publiczne: **mm**, wewnętrzne: **pt**
- Logi przez `logging` (moduł `log`)
- Nazwy zmiennych i komentarze: polski (kontekst produkcyjny PL)
- Wyjątki: jasne komunikaty po polsku dla operatora

---

## Testy

Fixtury generowane są w pamięci w `tmp_path` (nie wprowadzamy binariów do repo):
- `tests/fixtures.py` — generatory: `make_rectangle_vector`, `make_circle_on_artboard`,
  `make_pdf_with_trimbox`, `make_irregular_alpha_png`, `make_simple_raster`, `make_multipage_pdf`
- `tests/test_integration_pipeline.py` — end-to-end pipeline dla każdego typu wejścia

Każdy test weryfikuje:
1. Poprawność cut_segments (typ, liczba, wymiary)
2. bleed_segments = offset cut_segments o 2mm
3. Output PDF ma 3 warstwy (fill + grafika + CutContour spot)
4. TrimBox/BleedBox/CropBox poprawnie ustawione

Uruchomienie: `python3 -m pytest tests/ -q`

## Zmienne środowiskowe (opcjonalne przełączniki)

- `BLEED_CONTOUR_ENGINE` — `auto` (domyślnie) | `moore` | `opencv` — silnik detekcji konturu raster
- `BLEED_PDF_METADATA_ENGINE` — `pymupdf` (domyślnie) | `pikepdf` — backend zapisu PDF/X-4
- `BLEED_RGB_TO_CMYK` — `1` włącza postprocess Ghostscript RGB→CMYK
- `BLEED_CACHE_DIR` — katalog cache detect_contour (domyślnie `~/.cache/bleed-tool/contour/` lub `%LOCALAPPDATA%/bleed-tool/contour/`)
- `BLEED_NO_CACHE` — `1` wyłącza cache (zawsze miss)
