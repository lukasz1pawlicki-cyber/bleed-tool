"""Testy struktury Separation colorspace w output PDF.

Cel: zabezpieczenie przed regresjami, ktore cicho zepsuja odczyt CutContour /
FlexCut na ploterach (Summa S3, JWEI). Plotery czytaja spot name DOKLADNIE
wedlug nazwy i oczekuja poprawnej struktury FunctionType 2.

Weryfikacja (parsing surowych xref przez PyMuPDF):
  1. CutContour: /Separation /CutContour <alternate> <func>
     - spot name: literalnie "CutContour" (case-sensitive)
     - alternate: DeviceRGB (cut PDF) lub DeviceCMYK (print PDF)
     - FunctionType 2, Domain [0 1], N 1
     - C0 / C1 w zakresie [0,1]
  2. Colorspace zarejestrowany w Resources /ColorSpace strony
     (inaczej RIP nie znajdzie go przy operatorze "scn")
"""
from __future__ import annotations

import re
import pytest
import fitz

from modules.contour import detect_contour
from modules.bleed import generate_bleed
from modules.export import export_single_sticker
from tests.fixtures import make_rectangle_vector


# =============================================================================
# PARSER — wyciaga Separation colorspaces z surowego PDF
# =============================================================================

_SEP_ARRAY_RE = re.compile(
    r"\[\s*/Separation\s+/(\S+)\s+(/Device\w+|\[[^\]]+\])\s+(\d+)\s+0\s+R\s*\]"
)
_FUNC_KEYS_RE = re.compile(r"/(\w+)")


def _find_separations(doc: fitz.Document) -> list[dict]:
    """Znajduje wszystkie obiekty Separation w PDF.

    Zwraca liste slownikow:
      {
        'xref': int,
        'spot_name': str,     # 'CutContour'
        'alternate': str,     # '/DeviceRGB' lub '/DeviceCMYK'
        'func_xref': int,
        'func_body': str,     # surowy string dict-a funkcji
      }
    """
    out = []
    for xref in range(1, doc.xref_length()):
        try:
            body = doc.xref_object(xref)
        except Exception:
            continue
        if not body or "/Separation" not in body:
            continue
        m = _SEP_ARRAY_RE.search(body)
        if not m:
            continue
        spot_name, alternate, func_xref = m.group(1), m.group(2), int(m.group(3))
        try:
            func_body = doc.xref_object(func_xref)
        except Exception:
            func_body = ""
        out.append({
            "xref": xref,
            "spot_name": spot_name,
            "alternate": alternate,
            "func_xref": func_xref,
            "func_body": func_body or "",
        })
    return out


def _parse_function(func_body: str) -> dict:
    """Parser minimalny FunctionType 2 — wyciaga Domain/C0/C1/N/FunctionType."""
    result = {}
    m = re.search(r"/FunctionType\s+(\d+)", func_body)
    if m:
        result["FunctionType"] = int(m.group(1))
    m = re.search(r"/Domain\s*\[([^\]]+)\]", func_body)
    if m:
        result["Domain"] = [float(x) for x in m.group(1).split()]
    m = re.search(r"/C0\s*\[([^\]]+)\]", func_body)
    if m:
        result["C0"] = [float(x) for x in m.group(1).split()]
    m = re.search(r"/C1\s*\[([^\]]+)\]", func_body)
    if m:
        result["C1"] = [float(x) for x in m.group(1).split()]
    m = re.search(r"/N\s+([\d.]+)", func_body)
    if m:
        result["N"] = float(m.group(1))
    return result


def _page_colorspace_names(doc: fitz.Document, page_idx: int = 0) -> list[str]:
    """Zwraca nazwy kluczy w Page Resources /ColorSpace (bez leading slash)."""
    page = doc[page_idx]
    res = doc.xref_get_key(page.xref, "Resources")
    names: list[str] = []

    def _harvest(dict_str: str) -> list[str]:
        # Szukamy wpisow typu "/Foo 123 0 R" w dict-a
        return re.findall(r"/(\w+)\s+\d+\s+\d+\s+R", dict_str)

    if res[0] == "xref":
        m = re.match(r"(\d+)\s+\d+\s+R", res[1])
        if m:
            res_xref = int(m.group(1))
            cs = doc.xref_get_key(res_xref, "ColorSpace")
            if cs[0] == "dict":
                names.extend(_harvest(cs[1]))
    elif res[0] == "dict":
        cs = doc.xref_get_key(page.xref, "Resources/ColorSpace")
        if cs[0] == "dict":
            names.extend(_harvest(cs[1]))
    return names


# =============================================================================
# FIXTURE — prostokatny sticker z CutContour
# =============================================================================

@pytest.fixture
def rect_pdf_with_cut(tmp_path):
    inp = make_rectangle_vector(tmp_path, w_mm=80, h_mm=50)
    stickers = detect_contour(inp)
    s = stickers[0]
    generate_bleed(s, bleed_mm=2.0)
    out = str(tmp_path / "out.pdf")
    export_single_sticker(s, out, bleed_mm=2.0, cutcontour=True)
    if s.pdf_doc is not None:
        s.pdf_doc.close()
    return out


# =============================================================================
# TESTS
# =============================================================================

def test_cutcontour_spot_present(rect_pdf_with_cut):
    """W PDF jest przynajmniej jeden Separation /CutContour."""
    doc = fitz.open(rect_pdf_with_cut)
    try:
        seps = _find_separations(doc)
    finally:
        doc.close()
    cut = [s for s in seps if s["spot_name"] == "CutContour"]
    assert len(cut) >= 1, f"Brak Separation /CutContour. Znaleziono: {seps}"


def test_cutcontour_spot_name_exact_case(rect_pdf_with_cut):
    """Spot name musi byc DOKLADNIE 'CutContour' (case sensitive) —
    plotery Summa S3 / JWEI nie tolerują cutcontour / CUTCONTOUR."""
    doc = fitz.open(rect_pdf_with_cut)
    try:
        seps = _find_separations(doc)
    finally:
        doc.close()
    names = {s["spot_name"] for s in seps}
    assert "CutContour" in names, f"Spot names: {names}"
    # Nie ma wariantow ze zla wielkoscia liter
    bad = {n for n in names if n.lower() == "cutcontour" and n != "CutContour"}
    assert not bad, f"Niepoprawna kapitalizacja CutContour: {bad}"


def test_cutcontour_alternate_is_device_color(rect_pdf_with_cut):
    """Alternate colorspace to DeviceRGB lub DeviceCMYK (nie ICCBased / inny)."""
    doc = fitz.open(rect_pdf_with_cut)
    try:
        seps = _find_separations(doc)
    finally:
        doc.close()
    cut_seps = [s for s in seps if s["spot_name"] == "CutContour"]
    for sep in cut_seps:
        assert sep["alternate"] in ("/DeviceRGB", "/DeviceCMYK"), \
            f"Niepoprawny alternate dla CutContour: {sep['alternate']}"


def test_cutcontour_function_structure(rect_pdf_with_cut):
    """FunctionType 2, Domain [0 1], N 1, C0 / C1 dopasowane do alternate."""
    doc = fitz.open(rect_pdf_with_cut)
    try:
        seps = _find_separations(doc)
    finally:
        doc.close()
    cut_seps = [s for s in seps if s["spot_name"] == "CutContour"]
    assert cut_seps, "brak Separation /CutContour"

    for sep in cut_seps:
        func = _parse_function(sep["func_body"])
        assert func.get("FunctionType") == 2, \
            f"FunctionType != 2 dla CutContour: {func}"
        assert func.get("Domain") == [0.0, 1.0], \
            f"Domain powinno byc [0 1], jest: {func.get('Domain')}"
        assert func.get("N") == 1.0, \
            f"N powinno byc 1, jest: {func.get('N')}"

        c0 = func.get("C0", [])
        c1 = func.get("C1", [])
        # Arity C0/C1 musi zgadzac sie z alternate
        expected_arity = 3 if sep["alternate"] == "/DeviceRGB" else 4
        assert len(c0) == expected_arity, \
            f"C0 ma zla dlugosc ({len(c0)}) dla {sep['alternate']}"
        assert len(c1) == expected_arity, \
            f"C1 ma zla dlugosc ({len(c1)}) dla {sep['alternate']}"
        # Wszystkie wartosci C0/C1 w [0,1]
        for v in c0 + c1:
            assert 0.0 <= v <= 1.0, \
                f"Wartosc kanalu poza [0,1]: {v} (C0={c0}, C1={c1})"


def test_cutcontour_registered_in_page_resources(rect_pdf_with_cut):
    """Colorspace musi byc zarejestrowany w Page /Resources /ColorSpace
    pod nazwa "CS_CutContour", inaczej 'scn' operator nie zadziala."""
    doc = fitz.open(rect_pdf_with_cut)
    try:
        names = _page_colorspace_names(doc, page_idx=0)
    finally:
        doc.close()
    assert "CS_CutContour" in names, \
        f"Brak CS_CutContour w Page Resources /ColorSpace. Zarejestrowane: {names}"


def test_cutcontour_used_in_content_stream(rect_pdf_with_cut):
    """Content stream musi wywolywac /CS_CutContour operatorami cs/CS + scn/SCN."""
    doc = fitz.open(rect_pdf_with_cut)
    try:
        page = doc[0]
        # Wszystkie streamy strony
        contents = doc.xref_get_key(page.xref, "Contents")
        xrefs: list[int] = []
        if contents[0] == "array":
            xrefs = [int(x) for x in re.findall(r"(\d+)\s+\d+\s+R", contents[1])]
        elif contents[0] == "xref":
            m = re.match(r"(\d+)\s+\d+\s+R", contents[1])
            if m:
                xrefs = [int(m.group(1))]

        combined = b""
        for xr in xrefs:
            s = doc.xref_stream(xr)
            if s:
                combined += s
        text = combined.decode("latin-1", errors="replace")
    finally:
        doc.close()

    assert "/CS_CutContour" in text, \
        "Content stream nie odwoluje sie do /CS_CutContour — RIP nie uzyje spot color"
    # Musi byc przynajmniej jeden stroke z tym colorspace
    assert ("CS" in text and "SCN" in text) or ("cs" in text and "scn" in text), \
        "Brak operatorow ustawienia colorspace (cs/CS) + koloru (scn/SCN)"
