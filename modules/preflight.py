"""
Bleed Tool — preflight.py
===========================
Szybka analiza pliku pod katem produkcji naklejek (preflight check).

Sprawdza: rozdzielczosc, tryb kolorow, rozmiar, przezroczystosc,
tekst (fonty), spot colors, liczba stron, format pliku.

Uzycie:
    from modules.preflight import preflight_check
    result = preflight_check("naklejka.pdf")
    print(result['status'])   # 'ok' / 'warning' / 'error'
    print(result['issues'])   # lista problemow
"""

from __future__ import annotations

import logging
import os
import xml.etree.ElementTree as ET

import fitz  # PyMuPDF
from PIL import Image

from config import PT_TO_MM

log = logging.getLogger(__name__)

# Rozszerzenia plików rastrowych
_RASTER_EXT = ('.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp', '.webp')
_VECTOR_EXT = ('.pdf', '.ai')
_EPS_EXT = ('.eps', '.epsf')
_SVG_EXT = ('.svg',)


def _make_issue(code: str, message: str, severity: str = "warning") -> dict:
    """Tworzy slownik issue/warning."""
    return {"code": code, "message": message, "severity": severity}


def _preflight_raster(file_path: str) -> dict:
    """Preflight dla plikow rastrowych (PNG/JPG/TIFF/BMP/WEBP)."""
    img = Image.open(file_path)
    w_px, h_px = img.size
    mode = img.mode  # RGB, RGBA, CMYK, L, P...

    # DPI — Pillow zwraca (xdpi, ydpi) lub None
    dpi_info = img.info.get("dpi")
    dpi_assumed = False
    if dpi_info and isinstance(dpi_info, (tuple, list)) and dpi_info[0] > 0:
        dpi = float(dpi_info[0])
    else:
        # Brak DPI — zaloż 300 (fallback dla eksportu)
        dpi = 300.0
        dpi_assumed = True

    # Rozmiar w mm
    w_mm = w_px / dpi * 25.4
    h_mm = h_px / dpi * 25.4

    has_transparency = mode in ("RGBA", "LA", "PA") or "transparency" in img.info

    # Tryb koloru
    if mode in ("CMYK",):
        color_mode = "CMYK"
    elif mode in ("L", "LA"):
        color_mode = "Grayscale"
    elif mode in ("RGB", "RGBA", "P", "PA"):
        color_mode = "RGB"
    else:
        color_mode = mode

    img.close()

    issues: list[dict] = []
    warnings: list[dict] = []

    # Rozdzielczosc
    if dpi_assumed:
        warnings.append(_make_issue(
            "NO_DPI", "Brak metadanych DPI — zakladam 300 DPI", "info"))
    elif dpi < 72:
        issues.append(_make_issue(
            "DPI_CRITICAL", f"Rozdzielczosc {dpi:.0f} DPI — za niska do druku!", "error"))
    elif dpi < 150:
        warnings.append(_make_issue(
            "DPI_LOW", f"Rozdzielczosc {dpi:.0f} DPI — moze byc niewystarczajaca", "warning"))

    # Tryb kolorow
    if color_mode == "RGB":
        warnings.append(_make_issue(
            "COLOR_RGB", "Plik RGB — zostanie skonwertowany do druku", "info"))
    elif color_mode == "Grayscale":
        warnings.append(_make_issue(
            "COLOR_GRAY", "Plik w skali szarosci", "info"))

    # Przezroczystosc
    if has_transparency:
        warnings.append(_make_issue(
            "TRANSPARENCY", "Ma przezroczystosc — okragly/nieregularny ksztalt", "info"))

    # Rozmiar
    if w_mm < 10 or h_mm < 10:
        warnings.append(_make_issue(
            "SIZE_SMALL", f"Bardzo maly rozmiar: {w_mm:.1f}x{h_mm:.1f}mm", "warning"))
    if w_mm > 500 or h_mm > 500:
        warnings.append(_make_issue(
            "SIZE_LARGE", f"Bardzo duzy rozmiar: {w_mm:.1f}x{h_mm:.1f}mm", "warning"))

    # Format
    ext = os.path.splitext(file_path)[1].lower()
    warnings.append(_make_issue(
        "FORMAT_RASTER", f"Format rastrowy ({ext})", "info"))

    # Status
    status = "ok"
    if any(i["severity"] == "error" for i in issues):
        status = "error"
    elif any(i["severity"] == "warning" for i in issues + warnings):
        status = "warning"

    return {
        "path": file_path,
        "name": os.path.basename(file_path),
        "size_mm": (round(w_mm, 1), round(h_mm, 1)),
        "dpi": round(dpi, 0),
        "dpi_assumed": dpi_assumed,
        "color_mode": color_mode,
        "has_transparency": has_transparency,
        "has_spot_colors": False,
        "is_vector": False,
        "has_text": False,
        "page_count": 1,
        "issues": issues,
        "warnings": warnings,
        "status": status,
    }


def _preflight_pdf(file_path: str) -> dict:
    """Preflight dla plikow PDF/AI."""
    doc = fitz.open(file_path)
    page = doc[0]

    # Rozmiar strony
    rect = page.rect
    w_pt, h_pt = rect.width, rect.height
    w_mm = w_pt * PT_TO_MM
    h_mm = h_pt * PT_TO_MM

    # Sprawdz CropBox/TrimBox
    cropbox = page.cropbox
    trimbox = page.trimbox
    if trimbox and trimbox != page.mediabox:
        # TrimBox definiuje rozmiar naklejki
        w_mm = trimbox.width * PT_TO_MM
        h_mm = trimbox.height * PT_TO_MM

    # Drawings (wektory)
    drawings = page.get_drawings()
    has_drawings = len(drawings) > 0

    # Obrazy rastrowe
    images = page.get_images()
    has_images = len(images) > 0

    # DPI (efektywne) — dla osadzonych obrazow
    dpi = None
    if has_images and not has_drawings:
        # Tylko raster w PDF — oblicz efektywne DPI
        try:
            img_info = images[0]
            xref = img_info[0]
            base_image = doc.extract_image(xref)
            img_w = base_image.get("width", 0)
            if img_w > 0 and w_pt > 0:
                dpi = img_w / (w_pt / 72.0)
        except Exception:
            pass

    # Tekst (fonty nie zamienione na krzywe)
    text_content = page.get_text("text").strip()
    has_text = len(text_content) > 0

    # Spot colors (Separation colorspaces)
    has_spot_colors = False
    try:
        page_text_raw = page.get_text("rawdict")
        # Szybkie sprawdzenie: szukaj Separation w xref
        for i in range(1, doc.xref_length()):
            try:
                xref_str = doc.xref_object(i)
                if "/Separation" in xref_str:
                    has_spot_colors = True
                    break
            except Exception:
                continue
    except Exception:
        pass

    # Tryb kolorow
    if has_drawings and not has_images:
        color_mode = "Vector"
    elif has_images and not has_drawings:
        # Sprawdz colorspace osadzonego obrazu
        try:
            img_info = images[0]
            xref = img_info[0]
            base_image = doc.extract_image(xref)
            cs = base_image.get("colorspace", 0)
            # PyMuPDF: colorspace int (1=gray, 3=rgb, 4=cmyk)
            if cs == 4:
                color_mode = "CMYK"
            elif cs == 1:
                color_mode = "Grayscale"
            else:
                color_mode = "RGB"
        except Exception:
            color_mode = "RGB"
    elif has_drawings and has_images:
        color_mode = "Mixed"
    else:
        color_mode = "Vector"  # pusty PDF lub tylko tekst

    is_vector = has_drawings

    # Przezroczystosc — sprawdz alpha w osadzonych obrazach
    has_transparency = False
    if has_images:
        try:
            for img_info in images[:3]:  # Sprawdz max 3 obrazy
                xref = img_info[0]
                smask = img_info[1]  # smask xref (0 = brak)
                if smask != 0:
                    has_transparency = True
                    break
        except Exception:
            pass

    page_count = len(doc)
    doc.close()

    issues: list[dict] = []
    warnings_list: list[dict] = []

    # Rozdzielczosc (tylko dla rasterow w PDF)
    if dpi is not None:
        if dpi < 72:
            issues.append(_make_issue(
                "DPI_CRITICAL", f"Rozdzielczosc {dpi:.0f} DPI — za niska!", "error"))
        elif dpi < 150:
            warnings_list.append(_make_issue(
                "DPI_LOW", f"Rozdzielczosc {dpi:.0f} DPI — moze byc niewystarczajaca", "warning"))

    # Tekst
    if has_text:
        warnings_list.append(_make_issue(
            "HAS_TEXT", "Plik zawiera tekst — sprawdz czy fonty sa zamienione na krzywe", "warning"))

    # Spot colors
    if has_spot_colors:
        warnings_list.append(_make_issue(
            "SPOT_COLORS", "Plik zawiera kolory spot (Separation)", "info"))

    # Rozmiar
    if w_mm < 10 or h_mm < 10:
        warnings_list.append(_make_issue(
            "SIZE_SMALL", f"Bardzo maly rozmiar: {w_mm:.1f}x{h_mm:.1f}mm", "warning"))
    if w_mm > 500 or h_mm > 500:
        warnings_list.append(_make_issue(
            "SIZE_LARGE", f"Bardzo duzy rozmiar: {w_mm:.1f}x{h_mm:.1f}mm", "warning"))

    # Przezroczystosc
    if has_transparency:
        warnings_list.append(_make_issue(
            "TRANSPARENCY", "Ma przezroczystosc — okragly/nieregularny ksztalt", "info"))

    # Tryb kolorow
    if color_mode == "RGB":
        warnings_list.append(_make_issue(
            "COLOR_RGB", "Raster RGB w PDF — zostanie skonwertowany", "info"))
    elif color_mode == "Vector":
        warnings_list.append(_make_issue(
            "FORMAT_VECTOR", "Plik wektorowy (PDF)", "info"))
    elif color_mode == "Mixed":
        warnings_list.append(_make_issue(
            "FORMAT_MIXED", "Plik mieszany (wektor + raster)", "info"))

    # Wielostronicowy
    if page_count > 1:
        warnings_list.append(_make_issue(
            "MULTIPAGE", f"PDF wielostronicowy ({page_count} stron)", "info"))

    # Status
    status = "ok"
    if any(i["severity"] == "error" for i in issues):
        status = "error"
    elif any(i["severity"] == "warning" for i in issues + warnings_list):
        status = "warning"

    return {
        "path": file_path,
        "name": os.path.basename(file_path),
        "size_mm": (round(w_mm, 1), round(h_mm, 1)),
        "dpi": round(dpi, 0) if dpi else None,
        "color_mode": color_mode,
        "has_transparency": has_transparency,
        "has_spot_colors": has_spot_colors,
        "is_vector": is_vector,
        "has_text": has_text,
        "page_count": page_count,
        "issues": issues,
        "warnings": warnings_list,
        "status": status,
    }


def _preflight_eps(file_path: str) -> dict:
    """Preflight dla plikow EPS — konwersja przez Ghostscript -> _preflight_pdf.

    Powod: PyMuPDF nie otwiera EPS natywnie. Bez tej sciezki preflight_gate
    blokowal EPS (status=error) mimo ze pipeline obsluguje EPS przez
    ghostscript_bridge.eps_to_pdf().
    """
    from modules.ghostscript_bridge import eps_to_pdf, is_ghostscript_available

    if not is_ghostscript_available():
        # Bez Ghostscript nie mozemy zwalidowac EPS, ale nie blokujemy —
        # operator dostanie blad dopiero w pipeline (jasniejszy komunikat).
        return {
            "path": file_path,
            "name": os.path.basename(file_path),
            "size_mm": (0, 0),
            "dpi": None,
            "color_mode": "Vector",
            "has_transparency": False,
            "has_spot_colors": False,
            "is_vector": True,
            "has_text": False,
            "page_count": 1,
            "issues": [],
            "warnings": [_make_issue(
                "EPS_NO_GS",
                "EPS — Ghostscript niedostepny, walidacja pominieta",
                "warning",
            )],
            "status": "warning",
        }

    tmp_pdf = None
    try:
        tmp_pdf = eps_to_pdf(file_path)
        result = _preflight_pdf(tmp_pdf)
        # Podmien sciezke i nazwe na oryginalny EPS — operator widzi swoj plik
        result["path"] = file_path
        result["name"] = os.path.basename(file_path)
        result["warnings"] = list(result.get("warnings", [])) + [
            _make_issue("FORMAT_EPS", "Format EPS — konwersja przez Ghostscript", "info")
        ]
        # Status moze podskoczyc do warning przez nowy info — przeliczmy
        if result["status"] == "ok":
            result["status"] = "warning"
        return result
    except (FileNotFoundError, RuntimeError) as e:
        return {
            "path": file_path,
            "name": os.path.basename(file_path),
            "size_mm": (0, 0),
            "dpi": None,
            "color_mode": "Vector",
            "has_transparency": False,
            "has_spot_colors": False,
            "is_vector": True,
            "has_text": False,
            "page_count": 1,
            "issues": [_make_issue(
                "EPS_CONVERT_FAILED",
                f"EPS — konwersja Ghostscript nie powiodla sie: {e}",
                "error",
            )],
            "warnings": [],
            "status": "error",
        }
    finally:
        if tmp_pdf and os.path.exists(tmp_pdf):
            try:
                os.unlink(tmp_pdf)
            except OSError:
                pass


def _preflight_svg(file_path: str) -> dict:
    """Preflight dla plikow SVG — szybkie parsowanie XML."""
    w_mm, h_mm = 100.0, 100.0  # domyslne

    try:
        tree = ET.parse(file_path)
        root = tree.getroot()
        # Usun namespace
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"

        # viewBox
        vb = root.get("viewBox")
        width_attr = root.get("width", "")
        height_attr = root.get("height", "")

        if width_attr and height_attr:
            # Konwersja z roznych jednostek
            w_val = _parse_svg_dimension(width_attr)
            h_val = _parse_svg_dimension(height_attr)
            if w_val and h_val:
                w_mm, h_mm = w_val, h_val
        elif vb:
            parts = vb.replace(",", " ").split()
            if len(parts) == 4:
                # viewBox w px — przelicz zakladajac 96 DPI (standard SVG)
                vb_w = float(parts[2])
                vb_h = float(parts[3])
                w_mm = vb_w / 96.0 * 25.4
                h_mm = vb_h / 96.0 * 25.4
    except Exception as e:
        log.debug(f"SVG parse error: {e}")

    issues: list[dict] = []
    warnings: list[dict] = []

    warnings.append(_make_issue(
        "FORMAT_SVG", "Format SVG — zostanie skonwertowany do PDF", "info"))

    # Rozmiar
    if w_mm < 10 or h_mm < 10:
        warnings.append(_make_issue(
            "SIZE_SMALL", f"Bardzo maly rozmiar: {w_mm:.1f}x{h_mm:.1f}mm", "warning"))
    if w_mm > 500 or h_mm > 500:
        warnings.append(_make_issue(
            "SIZE_LARGE", f"Bardzo duzy rozmiar: {w_mm:.1f}x{h_mm:.1f}mm", "warning"))

    status = "ok"
    if any(i["severity"] == "warning" for i in warnings):
        status = "warning"

    return {
        "path": file_path,
        "name": os.path.basename(file_path),
        "size_mm": (round(w_mm, 1), round(h_mm, 1)),
        "dpi": None,
        "color_mode": "Vector",
        "has_transparency": False,
        "has_spot_colors": False,
        "is_vector": True,
        "has_text": False,  # SVG moze miec <text> ale nie sprawdzamy szczegolowo
        "page_count": 1,
        "issues": issues,
        "warnings": warnings,
        "status": status,
    }


def _parse_svg_dimension(value: str) -> float | None:
    """Parsuje wymiar SVG (np. '50mm', '100px', '2in') na mm."""
    value = value.strip()
    if not value:
        return None

    units = {
        "mm": 1.0,
        "cm": 10.0,
        "in": 25.4,
        "pt": 25.4 / 72.0,
        "pc": 25.4 / 6.0,
        "px": 25.4 / 96.0,
    }

    for unit, factor in units.items():
        if value.endswith(unit):
            try:
                return float(value[:-len(unit)]) * factor
            except ValueError:
                return None

    # Brak jednostki — zakladam px (96 DPI)
    try:
        return float(value) * 25.4 / 96.0
    except ValueError:
        return None


def preflight_gate(file_path: str, strict: bool = False) -> tuple[bool, dict]:
    """Sprawdza plik i decyduje czy mozna go eksportowac.

    Ten helper dodaje "gate" przed eksportem — uruchamia preflight_check
    i zwraca boolean blokujacy lub pozwalajacy na przejscie do pipeline
    detect_contour -> generate_bleed -> export.

    Args:
        file_path: sciezka do pliku wejsciowego
        strict: jesli True, ostrzezenia tez blokuja (dla produkcji);
                jesli False (default), tylko errors blokuja.

    Returns:
        (can_export, preflight_result):
            can_export — True gdy plik mozna bezpiecznie eksportowac
            preflight_result — pelne wyniki z preflight_check() dla logowania
    """
    result = preflight_check(file_path)
    status = result.get("status", "error")
    if status == "error":
        return (False, result)
    if strict and status == "warning":
        return (False, result)
    return (True, result)


def preflight_summary(result: dict) -> str:
    """Formatuje wyniki preflight do jednej linii (PL) dla log/stdout.

    Przyklad: "ok · 80×50mm · CMYK · 300dpi"
    """
    parts = [result.get("status", "?")]
    w, h = result.get("size_mm", (0, 0))
    if w and h:
        parts.append(f"{w:.0f}×{h:.0f}mm")
    mode = result.get("color_mode")
    if mode and mode not in ("Unknown", "Vector"):
        parts.append(mode)
    elif result.get("is_vector"):
        parts.append("wektor")
    dpi = result.get("dpi")
    if dpi:
        parts.append(f"{dpi:.0f}dpi")
    if result.get("has_transparency"):
        parts.append("alpha")
    issues = result.get("issues", []) + result.get("warnings", [])
    if issues:
        parts.append(f"{len(issues)} uwagi")
    return " · ".join(parts)


def preflight_check(file_path: str) -> dict:
    """Sprawdza plik pod katem produkcji naklejek.

    Args:
        file_path: sciezka do pliku (PDF/AI/SVG/PNG/JPG/TIFF/BMP/WEBP)

    Returns:
        dict z polami:
            'path': str - sciezka do pliku
            'name': str - nazwa pliku
            'size_mm': tuple[float, float] - (szerokosc, wysokosc) w mm
            'dpi': float | None - efektywna rozdzielczosc (dla rasterow)
            'color_mode': str - 'RGB', 'CMYK', 'Grayscale', 'Mixed', 'Vector'
            'has_transparency': bool - czy plik ma przezroczystosc
            'has_spot_colors': bool - czy plik ma kolory spot (Separation)
            'is_vector': bool - True jesli wektorowy PDF z rysunkami
            'has_text': bool - True jesli plik ma nieskonwertowany tekst
            'page_count': int
            'issues': list[dict] - lista problemow
            'warnings': list[dict] - lista ostrzezen
            'status': str - 'ok', 'warning', 'error'
    """
    if not os.path.isfile(file_path):
        return {
            "path": file_path,
            "name": os.path.basename(file_path),
            "size_mm": (0, 0),
            "dpi": None,
            "color_mode": "Unknown",
            "has_transparency": False,
            "has_spot_colors": False,
            "is_vector": False,
            "has_text": False,
            "page_count": 0,
            "issues": [_make_issue("FILE_NOT_FOUND", "Plik nie istnieje", "error")],
            "warnings": [],
            "status": "error",
        }

    ext = os.path.splitext(file_path)[1].lower()

    try:
        if ext in _RASTER_EXT:
            return _preflight_raster(file_path)
        elif ext in _SVG_EXT:
            return _preflight_svg(file_path)
        elif ext in _EPS_EXT:
            return _preflight_eps(file_path)
        elif ext in _VECTOR_EXT:
            return _preflight_pdf(file_path)
        else:
            return {
                "path": file_path,
                "name": os.path.basename(file_path),
                "size_mm": (0, 0),
                "dpi": None,
                "color_mode": "Unknown",
                "has_transparency": False,
                "has_spot_colors": False,
                "is_vector": False,
                "has_text": False,
                "page_count": 0,
                "issues": [_make_issue(
                    "UNSUPPORTED_FORMAT", f"Nieobslugiwany format: {ext}", "error")],
                "warnings": [],
                "status": "error",
            }
    except Exception as e:
        log.error(f"Preflight error for {file_path}: {e}")
        return {
            "path": file_path,
            "name": os.path.basename(file_path),
            "size_mm": (0, 0),
            "dpi": None,
            "color_mode": "Unknown",
            "has_transparency": False,
            "has_spot_colors": False,
            "is_vector": False,
            "has_text": False,
            "page_count": 0,
            "issues": [_make_issue("PREFLIGHT_ERROR", f"Blad analizy: {e}", "error")],
            "warnings": [],
            "status": "error",
        }


def format_preflight_result(result: dict) -> str:
    """Formatuje wynik preflight do czytelnego stringa (dla logu GUI).

    Zwraca string w formacie:
        [OK] nazwa.pdf: 67.7x79.2mm, Wektor, CMYK
        [!!] photo.jpg: 100x100mm, 150 DPI (niskie), RGB
        [XX] tiny.png: 5x5mm, 72 DPI (za niskie!)
    """
    name = result["name"]
    w, h = result["size_mm"]
    status = result["status"]

    # Ikona statusu
    if status == "ok":
        icon = "[OK]"
    elif status == "warning":
        icon = "[!!]"
    else:
        icon = "[XX]"

    # Opis
    parts = [f"{w}x{h}mm"]

    if result["dpi"] is not None:
        dpi = result["dpi"]
        if dpi < 72:
            parts.append(f"{dpi:.0f} DPI (za niskie!)")
        elif dpi < 150:
            parts.append(f"{dpi:.0f} DPI (niskie)")
        else:
            parts.append(f"{dpi:.0f} DPI")

    if result["is_vector"]:
        parts.append("Wektor")

    parts.append(result["color_mode"])

    if result["has_transparency"]:
        parts.append("Alpha")

    if result["has_text"]:
        parts.append("Tekst!")

    if result["has_spot_colors"]:
        parts.append("Spot")

    if result["page_count"] > 1:
        parts.append(f"{result['page_count']} stron")

    return f"{icon} {name}: {', '.join(parts)}"
