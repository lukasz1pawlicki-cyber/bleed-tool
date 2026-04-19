"""Generator 10 zroznicowanych stress-test plikow dla bleed-tool.

Pokrywa najtrudniejsze realne scenariusze:
  01 - Okragla naklejka SVG z clipPath (circle detection)
  02 - Skrajnie waska naklejka barcode (aspect ratio 10:1)
  03 - Canva-style PDF (bialy overlay + full-page colored bg)
  04 - PNG RGBA z glow (soft edges, Moore boundary trace)
  05 - Multipage PDF (3 strony rozne rozmiary)
  06 - Illustrator crop marks (brak TrimBox, L-marks w rogach)
  07 - PDF z bleedem (TrimBox != MediaBox, spady 3mm)
  08 - CMYK rich black z nadrukiem
  09 - SVG bez wymiarow w nazwie (default size)
  10 - Raster-only PDF (grafika = osadzony PNG)

Uruchomienie: `python tests/make_stress_tests.py`
Output: input/Stress Test/ (gitignorowane — folder wygenerowany lokalnie)
"""
from __future__ import annotations

from pathlib import Path

import fitz
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# Output: input/Stress Test/ relatywnie od roota repo.
REPO_ROOT = Path(__file__).resolve().parent.parent
OUT = REPO_ROOT / "input" / "Stress Test"
OUT.mkdir(parents=True, exist_ok=True)

MM = 72.0 / 25.4


# ---------------------------------------------------------------------------
# 01 — Okragla naklejka SVG z clipPath
# ---------------------------------------------------------------------------
def make_01_round_svg():
    svg = '''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="600" height="600" viewBox="0 0 600 600">
  <defs>
    <clipPath id="cut">
      <circle cx="300" cy="300" r="280"/>
    </clipPath>
    <radialGradient id="bg" cx="50%" cy="30%" r="70%">
      <stop offset="0%" stop-color="#ffcc00"/>
      <stop offset="100%" stop-color="#ff6600"/>
    </radialGradient>
  </defs>
  <g clip-path="url(#cut)">
    <circle cx="300" cy="300" r="280" fill="url(#bg)"/>
    <text x="300" y="200" font-family="sans-serif" font-size="80"
          font-weight="900" text-anchor="middle" fill="#8B0000">FRESH</text>
    <text x="300" y="290" font-family="sans-serif" font-size="100"
          font-weight="900" text-anchor="middle" fill="#fff">50%</text>
    <text x="300" y="380" font-family="sans-serif" font-size="60"
          font-weight="700" text-anchor="middle" fill="#8B0000">OFF</text>
    <path d="M100 400 Q300 500 500 400 L500 550 L100 550 Z" fill="#8B0000"/>
    <text x="300" y="520" font-family="sans-serif" font-size="40"
          text-anchor="middle" fill="#fff">LIMITED TIME</text>
  </g>
  <circle cx="300" cy="300" r="280" fill="none" stroke="#000" stroke-width="3"/>
</svg>'''
    (OUT / "01 Round badge 60x60.svg").write_text(svg, encoding="utf-8")


# ---------------------------------------------------------------------------
# 02 — Skrajnie waska naklejka (barcode)
# ---------------------------------------------------------------------------
def make_02_extreme_aspect():
    """100x10mm - aspect 10:1. Test skrajnych proporcji."""
    doc = fitz.open()
    w_mm, h_mm = 100, 10
    page = doc.new_page(width=w_mm * MM, height=h_mm * MM)
    # Czarny border + white bg
    page.draw_rect(
        fitz.Rect(0, 0, w_mm * MM, h_mm * MM),
        fill=(1, 1, 1), color=None, width=0,
    )
    # Barcode-like vertical stripes
    np.random.seed(42)
    x_mm = 5.0
    while x_mm < 70:
        w = np.random.uniform(0.3, 1.2)
        page.draw_rect(
            fitz.Rect(x_mm * MM, 2 * MM, (x_mm + w) * MM, 8 * MM),
            fill=(0, 0, 0), color=None, width=0,
        )
        x_mm += w + np.random.uniform(0.3, 0.8)
    # Text
    page.insert_text(
        fitz.Point(75 * MM, 6 * MM), "SKU-839201",
        fontsize=8, color=(0, 0, 0),
    )
    doc.save(OUT / "02 Extreme landscape barcode 100x10.pdf")
    doc.close()


# ---------------------------------------------------------------------------
# 03 — Canva-style PDF
# ---------------------------------------------------------------------------
def make_03_canva_style():
    """Biały overlay + pełno-stronicowe kolorowe tło (jak eksport z Canva).

    Struktura:
      1. Biały prostokąt (outermost, full page)
      2. Kolorowy prostokąt (full page, POD bialym? nie — Canva kładzie white
         ABOVE the design with transparency? symulujemy: white rect first,
         then colored rect that covers page)
      3. Content: text + shapes
    """
    doc = fitz.open()
    w_mm, h_mm = 70, 100
    page = doc.new_page(width=w_mm * MM, height=h_mm * MM)
    W, H = w_mm * MM, h_mm * MM

    # Warstwa 1: biały overlay pełnej strony (Canva zawsze tak robi)
    page.draw_rect(fitz.Rect(0, 0, W, H), fill=(1, 1, 1), color=None, width=0)
    # Warstwa 2: "rzeczywiste" kolorowe tło pełnej strony (teal)
    page.draw_rect(fitz.Rect(0, 0, W, H), fill=(0.06, 0.45, 0.5), color=None, width=0)
    # Ozdoby
    page.draw_circle(fitz.Point(W * 0.2, H * 0.15), 20 * MM,
                     fill=(0.95, 0.85, 0.3), color=None, width=0)
    page.draw_circle(fitz.Point(W * 0.85, H * 0.85), 15 * MM,
                     fill=(0.95, 0.85, 0.3), color=None, width=0)
    # Tytul
    page.insert_text(fitz.Point(10 * MM, 30 * MM), "SUMMER",
                     fontsize=40, fontname="hebo", color=(1, 1, 1))
    page.insert_text(fitz.Point(10 * MM, 45 * MM), "SALE 40%",
                     fontsize=30, fontname="hebo", color=(0.95, 0.85, 0.3))
    # Kolorowa linia oddzielajaca
    page.draw_line(
        fitz.Point(10 * MM, 55 * MM), fitz.Point(60 * MM, 55 * MM),
        color=(1, 1, 1), width=2,
    )
    # Drobny tekst
    for i, line in enumerate([
        "Kolekcja lato 2026",
        "Do konca sierpnia",
        "www.example.com",
    ]):
        page.insert_text(
            fitz.Point(10 * MM, (65 + i * 6) * MM), line,
            fontsize=12, color=(1, 1, 1),
        )
    doc.save(OUT / "03 Canva style teal portrait.pdf")
    doc.close()


# ---------------------------------------------------------------------------
# 04 — PNG RGBA z glow
# ---------------------------------------------------------------------------
def make_04_rgba_glow():
    """Typowy pattern Canva/Figma: logo z glow/shadow wokol."""
    size = 900
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Glow warstwa — rozmyty kontur
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    # Star shape
    def star_pts(cx, cy, ro, ri, n=5):
        import math
        pts = []
        for i in range(2 * n):
            angle = math.pi / 2 + i * math.pi / n
            r = ro if i % 2 == 0 else ri
            pts.append((cx + r * math.cos(angle), cy - r * math.sin(angle)))
        return pts

    pts = star_pts(size / 2, size / 2, size * 0.35, size * 0.15)
    gdraw.polygon(pts, fill=(255, 100, 0, 200))
    glow = glow.filter(ImageFilter.GaussianBlur(40))
    img.paste(glow, (0, 0), glow)
    # Solid star on top
    draw.polygon(pts, fill=(255, 50, 0, 255))
    # White text
    try:
        font = ImageFont.truetype("arial.ttf", 60)
    except OSError:
        font = ImageFont.load_default()
    draw.text((size / 2, size / 2), "STAR", anchor="mm", fill=(255, 255, 255, 255), font=font)
    img.save(OUT / "04 Star logo with glow.png", "PNG", dpi=(300, 300))


# ---------------------------------------------------------------------------
# 05 — Multipage PDF rozne rozmiary
# ---------------------------------------------------------------------------
def make_05_multipage():
    doc = fitz.open()
    sizes = [(50, 80), (30, 30), (90, 45)]  # (w_mm, h_mm)
    labels = ["FRONT", "BADGE", "BACK"]
    colors = [(0.8, 0.2, 0.2), (0.2, 0.6, 0.3), (0.2, 0.3, 0.7)]
    for (w_mm, h_mm), label, color in zip(sizes, labels, colors):
        page = doc.new_page(width=w_mm * MM, height=h_mm * MM)
        W, H = w_mm * MM, h_mm * MM
        page.draw_rect(fitz.Rect(0, 0, W, H), fill=color, color=None, width=0)
        page.insert_text(
            fitz.Point(W / 2 - 15 * MM, H / 2 + 2 * MM), label,
            fontsize=24, fontname="hebo", color=(1, 1, 1),
        )
        # Logo-like circle
        page.draw_circle(fitz.Point(W * 0.85, H * 0.15), min(W, H) * 0.08,
                         fill=(1, 1, 1), color=None, width=0)
    doc.save(OUT / "05 Multipage mixed sizes.pdf")
    doc.close()


# ---------------------------------------------------------------------------
# 06 — Illustrator crop marks
# ---------------------------------------------------------------------------
def make_06_crop_marks():
    """Naklejka z crop marks Illustratora (brak TrimBox).

    Design: 60x80mm w centrum strony 80x100mm. 10mm margines.
    Crop marks w rogach trim area (60x80mm @ offset 10mm).
    """
    doc = fitz.open()
    page_w_mm, page_h_mm = 80, 100
    trim_w_mm, trim_h_mm = 60, 80
    margin_mm = 10
    page = doc.new_page(width=page_w_mm * MM, height=page_h_mm * MM)
    W, H = page_w_mm * MM, page_h_mm * MM
    M = margin_mm * MM

    # Design inside trim
    trim_rect = fitz.Rect(M, M, W - M, H - M)
    page.draw_rect(trim_rect, fill=(0.95, 0.95, 0.95), color=None, width=0)
    page.insert_text(
        fitz.Point(M + 10 * MM, M + 20 * MM), "Premium",
        fontsize=24, fontname="hebo", color=(0.1, 0.1, 0.1),
    )
    page.insert_text(
        fitz.Point(M + 10 * MM, M + 30 * MM), "Coffee Beans",
        fontsize=16, color=(0.3, 0.3, 0.3),
    )
    page.draw_rect(
        fitz.Rect(M + 5 * MM, M + 40 * MM, M + 55 * MM, M + 42 * MM),
        fill=(0.6, 0.3, 0.1), color=None, width=0,
    )
    page.insert_text(
        fitz.Point(M + 10 * MM, M + 60 * MM), "Dark Roast",
        fontsize=12, color=(0.1, 0.1, 0.1),
    )

    # Crop marks — standard Illustrator style (L-shapes in 4 corners)
    # Horizontal arms at Y = trim top/bottom
    # Vertical arms at X = trim left/right
    # Offset from corner: 6pt (standard Illustrator)
    offset = 6.0
    mark_len = 18.0
    # Stroke registration black via DeviceCMYK (100/100/100/100)
    shape = page.new_shape()
    # Draw 8 line segments
    tl_x, tl_y = trim_rect.x0, trim_rect.y0
    br_x, br_y = trim_rect.x1, trim_rect.y1
    # Top-left corner
    shape.draw_line(fitz.Point(tl_x - offset, tl_y),
                    fitz.Point(tl_x - offset - mark_len, tl_y))
    shape.draw_line(fitz.Point(tl_x, tl_y - offset),
                    fitz.Point(tl_x, tl_y - offset - mark_len))
    # Top-right
    shape.draw_line(fitz.Point(br_x + offset, tl_y),
                    fitz.Point(br_x + offset + mark_len, tl_y))
    shape.draw_line(fitz.Point(br_x, tl_y - offset),
                    fitz.Point(br_x, tl_y - offset - mark_len))
    # Bottom-left
    shape.draw_line(fitz.Point(tl_x - offset, br_y),
                    fitz.Point(tl_x - offset - mark_len, br_y))
    shape.draw_line(fitz.Point(tl_x, br_y + offset),
                    fitz.Point(tl_x, br_y + offset + mark_len))
    # Bottom-right
    shape.draw_line(fitz.Point(br_x + offset, br_y),
                    fitz.Point(br_x + offset + mark_len, br_y))
    shape.draw_line(fitz.Point(br_x, br_y + offset),
                    fitz.Point(br_x, br_y + offset + mark_len))
    shape.finish(color=(0, 0, 0), width=0.25)
    shape.commit()

    doc.save(OUT / "06 Illustrator crop marks 60x80.pdf")
    doc.close()


# ---------------------------------------------------------------------------
# 07 — PDF ze spadami (TrimBox != MediaBox)
# ---------------------------------------------------------------------------
def make_07_bleed_included():
    """PDF z wbudowanymi spadami 3mm (TrimBox).

    MediaBox: 56x96mm (z bleedem)
    TrimBox:  50x90mm (właściwy rozmiar)
    Grafika wychodzi na bleed (kolorowy prostokąt pokrywa cala MediaBox).
    """
    doc = fitz.open()
    trim_w_mm, trim_h_mm = 50, 90
    bleed_mm = 3
    media_w_mm = trim_w_mm + 2 * bleed_mm
    media_h_mm = trim_h_mm + 2 * bleed_mm
    page = doc.new_page(width=media_w_mm * MM, height=media_h_mm * MM)
    W, H = media_w_mm * MM, media_h_mm * MM
    B = bleed_mm * MM

    # Full-bleed purple background (extends to MediaBox)
    page.draw_rect(fitz.Rect(0, 0, W, H), fill=(0.4, 0.2, 0.6), color=None, width=0)
    # Inner content (inside trim area)
    page.insert_text(
        fitz.Point(B + 5 * MM, B + 25 * MM), "VINYL",
        fontsize=36, fontname="hebo", color=(1, 1, 1),
    )
    page.insert_text(
        fitz.Point(B + 5 * MM, B + 38 * MM), "Collection 2026",
        fontsize=14, color=(1, 0.9, 0.3),
    )
    # Decorative circles — one touches trim edge
    page.draw_circle(fitz.Point(B + trim_w_mm / 2 * MM, B + trim_h_mm * 0.7 * MM),
                     12 * MM, fill=(1, 0.9, 0.3), color=None, width=0)

    # Set TrimBox
    xref = page.xref
    trim = [B, B, B + trim_w_mm * MM, B + trim_h_mm * MM]
    doc.xref_set_key(xref, "TrimBox", f"[{trim[0]} {trim[1]} {trim[2]} {trim[3]}]")
    doc.save(OUT / "07 Bleed included trimbox 50x90.pdf")
    doc.close()


# ---------------------------------------------------------------------------
# 08 — CMYK rich black
# ---------------------------------------------------------------------------
def make_08_cmyk_rich_black():
    """Design w CMYK z rich black (C=60 M=40 Y=40 K=100).

    Test: czy tool poprawnie wykrywa CMYK colorspace i edge_color_cmyk
    nie koliduje z rich black interior content. Injectujemy CMYK operatory
    bezposrednio do content stream bo PyMuPDF draw_rect tylko RGB.
    """
    doc = fitz.open()
    w_mm, h_mm = 70, 50
    W, H = w_mm * MM, h_mm * MM
    page = doc.new_page(width=W, height=H)
    # Dodatkowo wstawiamy wektor text jako RGB (tlem jest white CMYK via stream)
    page.insert_text(
        fitz.Point(5 * MM, 15 * MM), "RICH BLACK",
        fontsize=24, fontname="hebo", color=(0.1, 0.1, 0.1),
    )
    page.insert_text(
        fitz.Point(5 * MM, 35 * MM), "Premium Edition",
        fontsize=14, color=(0.1, 0.1, 0.1),
    )
    page.insert_text(
        fitz.Point(5 * MM, 42 * MM), "Numbered 001/100",
        fontsize=10, color=(0.3, 0.3, 0.3),
    )
    # Wstaw CMYK content (white bg + red accent bar) bezposrednio do streamu
    cmyk_stream = f"""q
0 0 0 0 k
0 0 {W} {H} re
f
0 1 1 0 k
{5 * MM} {20 * MM} {60 * MM} {2 * MM} re
f
Q
""".encode("ascii")
    # Wstaw PRZED istniejacym content (zeby bialy byl na dole)
    xref = page.xref
    contents = doc.xref_get_key(xref, "Contents")
    if contents[0] == "array":
        # Lista — prepend new stream xref
        new_xref = doc.get_new_xref()
        doc.update_object(new_xref, "<<>>")
        doc.update_stream(new_xref, cmyk_stream, new=True)
        # Prepend do contents array
        existing = contents[1]
        new_contents = f"[ {new_xref} 0 R " + existing.lstrip("[ ")
        doc.xref_set_key(xref, "Contents", new_contents)
    else:
        # Single ref — wrap w array
        existing_xref = contents[1]
        new_xref = doc.get_new_xref()
        doc.update_object(new_xref, "<<>>")
        doc.update_stream(new_xref, cmyk_stream, new=True)
        doc.xref_set_key(xref, "Contents", f"[ {new_xref} 0 R {existing_xref} ]")
    doc.save(OUT / "08 CMYK rich black 70x50.pdf")
    doc.close()


# ---------------------------------------------------------------------------
# 09 — SVG bez wymiarow + complex curves
# ---------------------------------------------------------------------------
def make_09_svg_complex_curves():
    """SVG z dziesiątkami krzywych Bezier, bez wymiarow w nazwie.

    Test: (a) SVG→PDF conversion z wielo-segment paths
          (b) Auto-size z viewBox (default 80mm longest side)
    """
    # Rysujemy stylizowany liść/kwiat
    paths = [
        # Main leaf shape
        '<path d="M300 50 C 450 100 500 250 450 400 C 400 550 200 550 150 400 '
        'C 100 250 150 100 300 50 Z" fill="#2d6a3e" stroke="#143319" stroke-width="4"/>',
        # Vein
        '<path d="M300 80 Q 310 250 300 520" fill="none" stroke="#143319" stroke-width="3"/>',
        # Side veins
        '<path d="M300 150 Q 380 180 430 250" fill="none" stroke="#143319" stroke-width="2"/>',
        '<path d="M300 250 Q 400 280 450 350" fill="none" stroke="#143319" stroke-width="2"/>',
        '<path d="M300 150 Q 220 180 170 250" fill="none" stroke="#143319" stroke-width="2"/>',
        '<path d="M300 250 Q 200 280 150 350" fill="none" stroke="#143319" stroke-width="2"/>',
        # Highlight
        '<path d="M250 150 C 280 130 330 130 360 155 C 340 200 280 210 240 180 Z" '
        'fill="#5fa876" stroke="none" opacity="0.6"/>',
        # Small decorative curves
    ]
    for i in range(10):
        cx = 300 + (i - 5) * 30
        cy = 450
        paths.append(
            f'<circle cx="{cx}" cy="{cy}" r="5" fill="#8fd5a3"/>'
        )
    svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="600" height="600" viewBox="0 0 600 600">
  <rect width="600" height="600" fill="#faf6e8"/>
  {chr(10).join(paths)}
  <text x="300" y="580" font-family="Georgia, serif" font-size="24"
        text-anchor="middle" fill="#143319">NATURA</text>
</svg>'''
    (OUT / "09 Natural leaf organic curves.svg").write_text(svg, encoding="utf-8")


# ---------------------------------------------------------------------------
# 10 — Raster-only PDF (embedded PNG)
# ---------------------------------------------------------------------------
def make_10_raster_only_pdf():
    """PDF zawierajacy tylko osadzony PNG (brak wektora).

    Typowy przypadek: skan etykiety, lub export z narzędzia rastrowego
    zapisany jako PDF.
    """
    # Najpierw generujemy PNG
    size_px = 1200
    img = Image.new("RGB", (size_px, int(size_px * 0.7)), (255, 240, 220))
    draw = ImageDraw.Draw(img)
    # Frame
    draw.rectangle([20, 20, size_px - 20, int(size_px * 0.7) - 20],
                   outline=(139, 69, 19), width=8)
    # Decorative corners
    for cx, cy in [(50, 50), (size_px - 50, 50),
                   (50, int(size_px * 0.7) - 50),
                   (size_px - 50, int(size_px * 0.7) - 50)]:
        draw.ellipse([cx - 15, cy - 15, cx + 15, cy + 15],
                     fill=(139, 69, 19))
    try:
        font_big = ImageFont.truetype("georgia.ttf", 80)
        font_med = ImageFont.truetype("georgia.ttf", 40)
        font_small = ImageFont.truetype("arial.ttf", 24)
    except OSError:
        font_big = font_med = font_small = ImageFont.load_default()
    draw.text((size_px / 2, 150), "VINTAGE", anchor="mm",
              fill=(139, 69, 19), font=font_big)
    draw.text((size_px / 2, 250), "~ est. 1975 ~", anchor="mm",
              fill=(139, 69, 19), font=font_med)
    draw.line([(size_px * 0.2, 300), (size_px * 0.8, 300)],
              fill=(139, 69, 19), width=3)
    draw.text((size_px / 2, 400), "Handcrafted Artisan Goods", anchor="mm",
              fill=(80, 40, 10), font=font_med)
    draw.text((size_px / 2, 500), "Batch No. 0042 | Made in Poland",
              anchor="mm", fill=(80, 40, 10), font=font_small)
    png_tmp = OUT / ".tmp_raster_inner.png"
    img.save(png_tmp, "PNG", dpi=(300, 300))

    # Osadzamy PNG w PDF
    w_mm, h_mm = 90, 63  # odpowiada proporcjom obrazu
    doc = fitz.open()
    page = doc.new_page(width=w_mm * MM, height=h_mm * MM)
    page.insert_image(
        fitz.Rect(0, 0, w_mm * MM, h_mm * MM),
        filename=str(png_tmp),
    )
    doc.save(OUT / "10 Raster only vintage label 90x63.pdf")
    doc.close()
    png_tmp.unlink()


# ---------------------------------------------------------------------------
# Run all
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    generators = [
        ("01", make_01_round_svg),
        ("02", make_02_extreme_aspect),
        ("03", make_03_canva_style),
        ("04", make_04_rgba_glow),
        ("05", make_05_multipage),
        ("06", make_06_crop_marks),
        ("07", make_07_bleed_included),
        ("08", make_08_cmyk_rich_black),
        ("09", make_09_svg_complex_curves),
        ("10", make_10_raster_only_pdf),
    ]
    for tag, fn in generators:
        try:
            fn()
            print(f"[OK]  {tag} {fn.__name__}")
        except Exception as e:
            print(f"[ERR] {tag} {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\nOutput folder: {OUT}")
    for p in sorted(OUT.iterdir()):
        print(f"  {p.name}  ({p.stat().st_size:,} bytes)")
