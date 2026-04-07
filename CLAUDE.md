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

### GUI: `bleed_app.py` (CustomTkinter)
- Drag & drop (tkinterdnd2) + kliknięcie do wyboru
- Podgląd PDF, ustawienia, log
- Multi-threaded processing

### CLI: `bleed_cli.py`
- `python bleed_cli.py input.pdf --output-dir ./out --bleed 2`

---

## Stack technologiczny

### Obecne zależności (requirements.txt):
```
PyMuPDF>=1.24.0      # import fitz — PDF: render, rysowanie, ekstrakcja
numpy>=1.20.0        # operacje geometryczne, piksele
Pillow>=8.0.0        # raster I/O, ICC color transform
customtkinter>=5.0.0 # GUI
tkinterdnd2>=0.3.0   # drag & drop
cairosvg>=2.5.0      # SVG → PDF
```

### Rekomendowany stack docelowy:

| Narzędzie | Rola | Status |
|---|---|---|
| **pikepdf** | Analiza struktury PDF, modyfikacja Box-ów (BleedBox, TrimBox, MediaBox) | DO DODANIA |
| **PyMuPDF** | Rysowanie linii cięcia, rasteryzacja, ekstrakcja obrazów | JEST |
| **Ghostscript** | Konwersja EPS→PDF, RGB→CMYK, finalna rasteryzacja do druku | DO DODANIA |
| **Pillow + NumPy** | Obróbka rastrów | JEST |
| **OpenCV** | Detekcja krawędzi, analiza treści (alternatywa dla alpha contour) | DO DODANIA |

### Praca na rastrach wewnątrz PDF:
```
PyMuPDF page.get_pixmap(dpi=300) → Pillow obróbka → NumPy operacje pikselowe
→ OpenCV detekcja krawędzi → wynik z powrotem: PyMuPDF osadza bitmapę w nowym PDF
```

---

## Obsługiwane formaty wejściowe

| Format | Typ | Biblioteka | Status |
|---|---|---|---|
| `.pdf` | Wektor | PyMuPDF (+ pikepdf docelowo) | DZIAŁA |
| `.ai` | Wektor (Adobe Illustrator) | PyMuPDF (jeśli zapisany jako PDF) | DZIAŁA |
| `.svg` | Wektor | CairoSVG → PDF → PyMuPDF | DZIAŁA |
| `.eps` | Wektor | Ghostscript → PDF | DO DODANIA |
| `.png`, `.jpg`, `.tiff`, `.webp`, `.bmp` | Raster | Pillow → PyMuPDF | DZIAŁA |

---

## Architektura — struktura docelowa

```
sticker_prep/
├── CLAUDE.md                      ← ten plik
├── bleed_app.py                   ← GUI (CustomTkinter) — DZIAŁA
├── bleed_cli.py                   ← CLI — DZIAŁA
├── config.py                      ← stałe (DEFAULT_BLEED_MM, SPOT_COLORS, MM_TO_PT)
├── models.py                      ← dataclasses (Sticker, Placement, Sheet, Mark, PanelLine)
├── modules/
│   ├── contour.py                 ← detekcja konturu (wektor, raster, circle) — DZIAŁA
│   ├── bleed.py                   ← offset konturu + kolor krawędzi — DZIAŁA
│   ├── export.py                  ← eksport PDF (sticker + sheet) — DZIAŁA
│   ├── svg_convert.py             ← SVG → PDF — DZIAŁA
│   ├── file_loader.py             ← DO DODANIA: abstrakcja wczytywania plików
│   ├── pdf_metadata.py            ← DO DODANIA: pikepdf — ustawienie Box-ów w output
│   └── ghostscript_bridge.py      ← DO DODANIA: EPS→PDF, rasteryzacja CMYK
├── profiles/
│   └── output_profiles.json       ← DO DODANIA: ustawienia per maszyna
├── tests/
│   ├── test_contour.py
│   ├── test_bleed.py
│   ├── test_export.py
│   └── fixtures/                  ← pliki testowe
├── requirements.txt
├── uruchom.bat                    ← launcher Windows
└── uruchom.command                ← launcher macOS
```

---

## Pipeline przetwarzania (szczegółowy)

```
INPUT FILE (PDF/SVG/EPS/PNG/JPG)
  │
  ▼
file_loader.load()              → fitz.Document (zawsze PDF wewnętrznie)
  │                               • PDF/AI: otwarcie bezpośrednie
  │                               • SVG: CairoSVG → tmp PDF
  │                               • EPS: Ghostscript → tmp PDF
  │                               • Raster: Pillow → osadź w PDF
  │
  ▼
contour.detect_contour()        → list[Sticker]
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
pdf_metadata.set_boxes()        → DO DODANIA (pikepdf):
  │                               • TrimBox = oryginalny rozmiar
  │                               • BleedBox = TrimBox + 2mm
  │                               • MediaBox = BleedBox
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

## Nazewnictwo pliku wyjściowego (DO ZAIMPLEMENTOWANIA)

```python
def build_output_name(input_path: Path, trim_w_mm: float, trim_h_mm: float, bleed_mm: float) -> str:
    w = round(trim_w_mm)
    h = round(trim_h_mm)
    b = round(bleed_mm)
    return f"{input_path.stem}_PRINT_{w}x{h}mm_bleed{b}mm.pdf"
```

---

## DO ZROBIENIA (priorytety)

### Priorytet 1 — Stabilizacja obecnego kodu
- [ ] BleedBox/TrimBox w output PDF (pikepdf lub PyMuPDF xref)
- [ ] Output naming convention (`_PRINT_{W}x{H}mm_bleed{N}mm.pdf`)
- [ ] Testy jednostkowe (contour, bleed, export)

### Priorytet 2 — Nowe formaty
- [ ] `ghostscript_bridge.py` — EPS → PDF
- [ ] `file_loader.py` — abstrakcja wczytywania (detect format → load → fitz.Document)

### Priorytet 3 — Zaawansowane
- [ ] OpenCV contour detection (alternatywa/uzupełnienie alpha rendering)
- [ ] pikepdf do czystej modyfikacji metadanych PDF
- [ ] Profile eksportu per maszyna (output_profiles.json)
- [ ] GUI: podgląd przed/po side-by-side

### Priorytet 4 — Opcjonalne
- [ ] Migracja GUI na PyQt6 (jeśli CTk nie wystarczy)
- [ ] Ghostscript RGB→CMYK finalna rasteryzacja
- [ ] Batch processing z CLI (wiele plików naraz)

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

## Testy (do stworzenia)

```
tests/fixtures/
├── rectangle_vector.pdf         ← prostokątna naklejka wektorowa
├── circle_on_artboard.pdf       ← okrągła naklejka na dużej stronie (Problem 1)
├── with_trimbox.pdf             ← plik ze spadami i markerami cięcia (Cyclonic)
├── irregular_alpha.png          ← nieregularny kształt na przezroczystym tle
├── simple_raster.jpg            ← zdjęcie prostokątne
└── multipage.pdf                ← wielostronicowy PDF
```

Każdy test weryfikuje:
1. Poprawność cut_segments (typ, liczba, wymiary)
2. bleed_segments = offset cut_segments o 2mm
3. Output PDF ma 3 warstwy (fill + grafika + CutContour spot)
4. TrimBox/BleedBox poprawnie ustawione
