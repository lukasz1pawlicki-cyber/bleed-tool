#!/usr/bin/env python3
"""
Bleed Tool — GUI (CustomTkinter)
==================================
Generowanie bleed dla naklejek wektorowych.
Drag & drop lub file dialog, podgląd wynikowego PDF.
"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter

# Dodaj katalog bleed-tool do path
APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

import fitz as fitz_module
from PIL import Image, ImageTk

# Drag & drop — opcjonalne
try:
    import tkinterdnd2
    HAS_DND = True
except ImportError:
    HAS_DND = False

from config import (
    DEFAULT_BLEED_MM, DEFAULT_GAP_MM, DEFAULT_MARK_ZONE_MM, SHEET_SIZES,
    SHEET_PRESETS, ROLL_PRESETS, DEFAULT_ROLL_MAX_LENGTH_MM,
    PLOTTERS, FLEXCUT_GAP_MM,
)
from modules.preflight import preflight_check, format_preflight_result

log = logging.getLogger("bleed-tool")

# =============================================================================
# SETUP
# =============================================================================

customtkinter.set_appearance_mode("light")
customtkinter.set_default_color_theme("blue")

# Kolory
ACCENT        = "#4f6ef7"
ACCENT_HOVER  = "#3b5bdb"
SUCCESS       = "#37b24d"
ERROR         = "#e03131"
CARD_BG       = ("#ffffff", "#25262b")
MAIN_BG       = ("#f1f3f5", "#1a1b1e")
TEXT          = ("#212529", "#e9ecef")
TEXT_SECONDARY = ("#868e96", "#909296")
DROP_ZONE_BG  = ("#f8f9fa", "#2c2e33")
DROP_ZONE_BORDER = ("#ced4da", "#373a40")
LOG_BG        = ("#f8f9fa", "#141517")
LOG_FG        = ("#212529", "#c1c2c5")
SIDEBAR_BG    = ("#f8f9fa", "#1a1b1e")
SIDEBAR_HOVER = ("#e9ecef", "#2c2e33")
SIDEBAR_ACTIVE_BG = ("#e7f0ff", "#25334a")
SIDEBAR_ACTIVE_FG = ("#4f6ef7", "#7ba1f7")

# Podglad
_PREVIEW_CUTCONTOUR = "#ff1744"
_PREVIEW_FLEXCUT = "#00e676"
_PREVIEW_MARK = "#000000"

# Obsługiwane formaty
_SUPPORTED_EXT = ('.pdf', '.svg', '.eps', '.epsf', '.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp', '.webp')


def _draw_rounded_rect(canvas, x0, y0, x1, y1, r, **kw):
    """Rysuje zaokrąglony prostokąt na Canvas (łuki + linie)."""
    # Arc używa outline=, Line używa fill= — rozdziel kwargs
    line_kw = {k: v for k, v in kw.items() if k != 'outline'}
    line_kw['fill'] = kw.get('outline', 'black')
    canvas.create_arc(x0, y0, x0 + 2 * r, y0 + 2 * r, start=90, extent=90, style="arc", **kw)
    canvas.create_arc(x1 - 2 * r, y0, x1, y0 + 2 * r, start=0, extent=90, style="arc", **kw)
    canvas.create_arc(x1 - 2 * r, y1 - 2 * r, x1, y1, start=270, extent=90, style="arc", **kw)
    canvas.create_arc(x0, y1 - 2 * r, x0 + 2 * r, y1, start=180, extent=90, style="arc", **kw)
    canvas.create_line(x0 + r, y0, x1 - r, y0, **line_kw)
    canvas.create_line(x0 + r, y1, x1 - r, y1, **line_kw)
    canvas.create_line(x0, y0 + r, x0, y1 - r, **line_kw)
    canvas.create_line(x1, y0 + r, x1, y1 - r, **line_kw)


# =============================================================================
# SHEET PREVIEW PANEL (wzorowany na sticker-toolkit)
# =============================================================================

def _preview_bg():
    return "#1a1b1e" if customtkinter.get_appearance_mode() == "Dark" else "#f1f3f5"


class SheetPreviewPanel:
    """Panel podgladu arkuszy — Canvas z renderem PDF, zoom, pan, nawigacja."""

    PADDING = 30

    def __init__(self, parent):
        self.parent = parent
        self.job = None
        self.bleed_mm: float = 2.0
        self.bleed_results: list[dict] = []
        self._bleed_images: list = []
        self.sheet_pdfs: list[tuple[str, str]] = []
        self._current_rendered = None
        self.current_sheet_idx = 0
        self._render_cache: dict = {}

        # Transformacja (zapisywana przy rysowaniu)
        self._tx_ox = 0.0
        self._tx_oy = 0.0
        self._tx_scale = 1.0
        self._tx_sh_mm = 0.0

        # Główna ramka
        self.frame = customtkinter.CTkFrame(parent, fg_color=CARD_BG, corner_radius=10)

        # Toolbar
        toolbar = customtkinter.CTkFrame(self.frame, fg_color="transparent", height=36)
        toolbar.pack(fill="x", side="top", padx=8, pady=(8, 0))
        toolbar.pack_propagate(False)

        self.prev_btn = customtkinter.CTkButton(
            toolbar, text="‹", command=self._prev_sheet, width=28, height=26,
            fg_color=("gray92", "gray22"), hover_color=SIDEBAR_HOVER,
            text_color=TEXT, font=customtkinter.CTkFont(size=14), corner_radius=6,
        )
        self.prev_btn.pack(side="left", padx=(0, 4))

        self.sheet_label = customtkinter.CTkLabel(
            toolbar, text="Podglad", font=customtkinter.CTkFont(size=11, weight="bold"),
            text_color=TEXT,
        )
        self.sheet_label.pack(side="left", padx=2)

        self.next_btn = customtkinter.CTkButton(
            toolbar, text="›", command=self._next_sheet, width=28, height=26,
            fg_color=("gray92", "gray22"), hover_color=SIDEBAR_HOVER,
            text_color=TEXT, font=customtkinter.CTkFont(size=14), corner_radius=6,
        )
        self.next_btn.pack(side="left", padx=(4, 0))

        self.info_label = customtkinter.CTkLabel(
            toolbar, text="", font=customtkinter.CTkFont(size=9),
            text_color=TEXT_SECONDARY,
        )
        self.info_label.pack(side="right", padx=4)

        # Legenda
        legend_bar = customtkinter.CTkFrame(self.frame, fg_color="transparent", height=20)
        legend_bar.pack(fill="x", padx=12)
        legend_bar.pack_propagate(False)
        for color, label in [
            (_PREVIEW_CUTCONTOUR, "Cut"),
            (_PREVIEW_FLEXCUT, "Flex"),
            (_PREVIEW_MARK, "OPOS"),
        ]:
            dot = tk.Frame(legend_bar, bg=color, width=8, height=8)
            dot.pack(side="left", padx=(4, 2), pady=2)
            customtkinter.CTkLabel(
                legend_bar, text=label, font=customtkinter.CTkFont(size=8),
                text_color=TEXT_SECONDARY,
            ).pack(side="left", padx=(0, 6))

        # Canvas
        self.canvas = tk.Canvas(self.frame, bg=_preview_bg(), highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self._resize_after_id = None
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        # Zoom & pan
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._drag_start = None
        self._drag_moved = False
        self.canvas.bind("<MouseWheel>", self._on_scroll_zoom)
        self.canvas.bind("<Button-4>", self._on_scroll_zoom)
        self.canvas.bind("<Button-5>", self._on_scroll_zoom)
        self.canvas.bind("<ButtonPress-1>", self._on_pan_start)
        self.canvas.bind("<B1-Motion>", self._on_pan_move)
        self.canvas.bind("<ButtonRelease-1>", self._on_click_release)
        self.canvas.bind("<Double-Button-1>", self._on_zoom_reset)

        self._update_nav()

    # ----- Public API -----

    def set_job(self, job, bleed_mm=2.0, sheet_pdfs=None):
        """Ustaw podgląd arkuszy z nestingu."""
        self._render_cache.clear()
        self.job = job
        self.bleed_mm = bleed_mm
        self.sheet_pdfs = sheet_pdfs or []
        self.bleed_results = []
        self.current_sheet_idx = 0
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._update_nav()
        self._draw_current_sheet()

    def set_bleed_results(self, results: list[dict]):
        """Ustaw podgląd przetworzonych plików bleed."""
        self._render_cache.clear()
        self.job = None
        self.bleed_results = results
        self._bleed_images = []
        self.current_sheet_idx = 0
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._update_nav()
        self._draw_current_sheet()

    def clear(self):
        """Wyczyść podgląd."""
        self._render_cache.clear()
        self.job = None
        self.bleed_results = []
        self.sheet_pdfs = []
        self._bleed_images = []
        self.current_sheet_idx = 0
        self._update_nav()
        self.canvas.delete("all")

    # ----- Navigation -----

    def _prev_sheet(self):
        if self.current_sheet_idx > 0:
            self.current_sheet_idx -= 1
            self._zoom = 1.0
            self._pan_x = self._pan_y = 0.0
            self._update_nav()
            self._draw_current_sheet()

    def _next_sheet(self):
        max_idx = len(self.bleed_results) if self.bleed_results else (
            len(self.job.sheets) if self.job else 0)
        if self.current_sheet_idx < max_idx - 1:
            self.current_sheet_idx += 1
            self._zoom = 1.0
            self._pan_x = self._pan_y = 0.0
            self._update_nav()
            self._draw_current_sheet()

    def _update_nav(self):
        # Bleed preview mode
        if self.bleed_results:
            n = len(self.bleed_results)
            idx = self.current_sheet_idx
            self.sheet_label.configure(text=f"Plik {idx + 1}/{n}")
            self.prev_btn.configure(state="normal" if idx > 0 else "disabled")
            self.next_btn.configure(state="normal" if idx < n - 1 else "disabled")
            r = self.bleed_results[idx]
            w, h = r["size_mm"]
            self.info_label.configure(text=f"{r['label']}  |  {w:.1f}×{h:.1f}mm")
            return

        if not self.job or not self.job.sheets:
            self.sheet_label.configure(text="Podglad")
            self.prev_btn.configure(state="disabled")
            self.next_btn.configure(state="disabled")
            self.info_label.configure(text="Uruchom przetwarzanie")
            return

        n = len(self.job.sheets)
        idx = self.current_sheet_idx
        self.sheet_label.configure(text=f"Arkusz {idx + 1}/{n}")
        self.prev_btn.configure(state="normal" if idx > 0 else "disabled")
        self.next_btn.configure(state="normal" if idx < n - 1 else "disabled")

        sheet = self.job.sheets[idx]
        placed = len(sheet.placements)
        flexcut = len(sheet.panel_lines)
        marks = len(sheet.marks)
        info = f"{sheet.width_mm:.0f}×{sheet.height_mm:.0f}mm | {placed} szt"
        if flexcut:
            info += f" | {flexcut} FlexCut"
        info += f" | {marks} markerow"
        self.info_label.configure(text=info)

    # ----- Drawing -----

    def _draw_current_sheet(self):
        self.canvas.delete("all")
        self.canvas.configure(bg=_preview_bg())

        if self.bleed_results:
            self._draw_bleed_preview()
            return

        if not self.job or not self.job.sheets:
            cw = self.canvas.winfo_width()
            ch = self.canvas.winfo_height()
            if cw > 10 and ch > 10:
                self.canvas.create_text(
                    cw / 2, ch / 2,
                    text="Brak podgladu\n\nUruchom przetwarzanie",
                    font=customtkinter.CTkFont(size=12),
                    fill="#9e9e9e", anchor="center", justify="center",
                )
            return

        sheet = self.job.sheets[self.current_sheet_idx]
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw < 10 or ch < 10:
            return

        pad = self.PADDING
        avail_w = cw - 2 * pad
        avail_h = ch - 2 * pad
        if avail_w <= 0 or avail_h <= 0:
            return

        sw_mm, sh_mm = sheet.width_mm, sheet.height_mm
        if sw_mm <= 0 or sh_mm <= 0:
            return

        base_scale = min(avail_w / sw_mm, avail_h / sh_mm)
        scale = base_scale * self._zoom

        cx_canvas = cw / 2 + self._pan_x
        cy_canvas = ch / 2 + self._pan_y
        ox = cx_canvas - (sw_mm * scale) / 2
        oy = cy_canvas - (sh_mm * scale) / 2

        def tx(xmm): return ox + xmm * scale
        def ty(ymm): return oy + (sh_mm - ymm) * scale

        # Zapisz transformację (do hit-test FlexCut)
        self._tx_ox = ox
        self._tx_oy = oy
        self._tx_scale = scale
        self._tx_sh_mm = sh_mm

        # Cień
        is_dark = customtkinter.get_appearance_mode() == "Dark"
        shadow = "#1a1a1a" if is_dark else "#c0c0c0"
        self.canvas.create_rectangle(
            tx(0) + 3, ty(sh_mm) + 3, tx(sw_mm) + 3, ty(0) + 3,
            fill=shadow, outline="",
        )

        # Renderuj PDF (print+cut)
        rendered = self._render_sheet_pdf(self.current_sheet_idx, sw_mm, sh_mm, scale)
        if rendered:
            self.canvas.create_image(tx(0), ty(sh_mm), anchor="nw", image=rendered)
            self._current_rendered = rendered
        else:
            self.canvas.create_rectangle(
                tx(0), ty(sh_mm), tx(sw_mm), ty(0),
                fill="#ffffff", outline="#999999", width=1,
            )

        # Wymiary
        dim_color = "#9e9e9e" if is_dark else "#5f6368"
        self.canvas.create_text(
            tx(sw_mm / 2), ty(0) + 14,
            text=f"{sw_mm:.0f}mm", font=customtkinter.CTkFont(size=9), fill=dim_color,
        )
        self.canvas.create_text(
            tx(sw_mm) + 16, ty(sh_mm / 2),
            text=f"{sh_mm:.0f}mm", font=customtkinter.CTkFont(size=9), fill=dim_color, angle=90,
        )

        # Zoom indicator
        if self._zoom > 1.05 or self._zoom < 0.95:
            self.canvas.create_text(
                cw - 8, ch - 8,
                text=f"{self._zoom:.1f}×", font=customtkinter.CTkFont(size=9, weight="bold"),
                fill=dim_color, anchor="se",
            )

    # ----- PDF render -----

    def _render_sheet_pdf(self, sheet_idx, sw_mm, sh_mm, scale):
        """Renderuje print+cut PDF jako bitmapę do podglądu."""
        if not self.sheet_pdfs or sheet_idx >= len(self.sheet_pdfs):
            return None

        print_path, cut_path = self.sheet_pdfs[sheet_idx]
        if not os.path.exists(print_path) or not os.path.exists(cut_path):
            return None

        target_w = max(1, int(sw_mm * scale))
        target_h = max(1, int(sh_mm * scale))

        # LRU-style cache — unikaj ponownego renderowania przy zoom/pan/resize
        cache_key = (sheet_idx, target_w, target_h)
        if cache_key in self._render_cache:
            return self._render_cache[cache_key]

        try:
            import numpy as np

            # Renderuj print PDF
            doc_print = fitz_module.open(print_path)
            page_print = doc_print[0]
            zoom_x = target_w / page_print.rect.width
            zoom_y = target_h / page_print.rect.height
            mat = fitz_module.Matrix(zoom_x, zoom_y)
            pix_print = page_print.get_pixmap(matrix=mat, alpha=False)
            img_print = Image.frombytes("RGB", [pix_print.width, pix_print.height], pix_print.samples)
            doc_print.close()

            # Renderuj cut PDF — pogrub linie
            import re as _re
            doc_cut = fitz_module.open(cut_path)
            page_cut = doc_cut[0]
            for cont_xref in page_cut.get_contents():
                stream = doc_cut.xref_stream(cont_xref)
                if stream:
                    text = stream.decode("latin-1", errors="replace")
                    text = _re.sub(r'\b0\.25\s+w\b', '4.0 w', text)
                    doc_cut.update_stream(cont_xref, text.encode("latin-1"))

            pix_cut = page_cut.get_pixmap(matrix=mat, alpha=True)
            img_cut = Image.frombytes("RGBA", [pix_cut.width, pix_cut.height], pix_cut.samples)
            doc_cut.close()

            # Nałóż cut na print (białe → przezroczyste)
            arr = np.array(img_cut)
            white_mask = (arr[:, :, 0] > 240) & (arr[:, :, 1] > 240) & (arr[:, :, 2] > 240)
            arr[white_mask, 3] = 0
            img_cut = Image.fromarray(arr, "RGBA")

            img_print = img_print.convert("RGBA")
            img_final = Image.alpha_composite(img_print, img_cut)
            result = ImageTk.PhotoImage(img_final)

            # Zapisz w cache (limit 5 wpisów)
            if len(self._render_cache) >= 5:
                oldest_key = next(iter(self._render_cache))
                del self._render_cache[oldest_key]
            self._render_cache[cache_key] = result
            return result

        except Exception:
            return None

    # ----- Bleed preview -----

    def _draw_bleed_preview(self):
        """Renderuje podgląd pliku bleed z CutContour overlay."""
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw < 10 or ch < 10 or not self.bleed_results:
            return

        idx = self.current_sheet_idx
        if idx >= len(self.bleed_results):
            return

        result = self.bleed_results[idx]
        pdf_path = result["path"]
        w_mm, h_mm = result["size_mm"]

        pad = self.PADDING
        avail_w = cw - 2 * pad
        avail_h = ch - 2 * pad
        if avail_w <= 0 or avail_h <= 0:
            return

        base_scale = min(avail_w / w_mm, avail_h / h_mm)
        scale = base_scale * self._zoom

        img_w = max(1, int(w_mm * scale))
        img_h = max(1, int(h_mm * scale))

        try:
            doc = fitz_module.open(pdf_path)
            page = doc[0]
            render_zoom = max(scale * 1.3, 2.0)
            mat = fitz_module.Matrix(render_zoom, render_zoom)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            doc.close()
            img = img.resize((img_w, img_h), Image.LANCZOS)

            cx = cw / 2 + self._pan_x
            cy = ch / 2 + self._pan_y

            # Cień
            is_dark = customtkinter.get_appearance_mode() == "Dark"
            shadow = "#1a1a1a" if is_dark else "#c0c0c0"
            self.canvas.create_rectangle(
                cx - img_w / 2 + 3, cy - img_h / 2 + 3,
                cx + img_w / 2 + 3, cy + img_h / 2 + 3,
                fill=shadow, outline="",
            )

            photo = ImageTk.PhotoImage(img)
            self._bleed_images = [photo]
            self.canvas.create_image(cx, cy, anchor="center", image=photo)

        except Exception:
            self.canvas.create_text(
                cw / 2, ch / 2, text="Blad renderowania",
                fill="#e03131", font=customtkinter.CTkFont(size=11),
            )

    # ----- Zoom & Pan -----

    def _has_preview(self):
        return bool(self.bleed_results) or bool(self.job and self.job.sheets)

    def _on_scroll_zoom(self, event):
        if not self._has_preview():
            return
        if event.num == 4 or (hasattr(event, 'delta') and event.delta > 0):
            factor = 1.15
        elif event.num == 5 or (hasattr(event, 'delta') and event.delta < 0):
            factor = 1 / 1.15
        else:
            return
        old_zoom = self._zoom
        self._zoom = max(0.5, min(8.0, self._zoom * factor))
        if old_zoom != self._zoom:
            cx = self.canvas.winfo_width() / 2
            cy = self.canvas.winfo_height() / 2
            ratio = self._zoom / old_zoom
            self._pan_x = event.x - ratio * (event.x - cx - self._pan_x) - cx
            self._pan_y = event.y - ratio * (event.y - cy - self._pan_y) - cy
            self._draw_current_sheet()

    def _on_pan_start(self, event):
        self._drag_start = (event.x, event.y, self._pan_x, self._pan_y)
        self._drag_moved = False

    def _on_pan_move(self, event):
        if not self._has_preview() or not self._drag_start:
            return
        sx, sy, px, py = self._drag_start
        dx = event.x - sx
        dy = event.y - sy
        if abs(dx) > 3 or abs(dy) > 3:
            self._drag_moved = True
        self._pan_x = px + dx
        self._pan_y = py + dy
        self._draw_current_sheet()

    def _on_canvas_configure(self, event):
        """Debounce resize — przerysuj po 150ms bezczynności."""
        if self._resize_after_id:
            self.canvas.after_cancel(self._resize_after_id)
        self._resize_after_id = self.canvas.after(150, self._draw_current_sheet)

    def _on_click_release(self, event):
        pass  # pasywny podgląd — brak interakcji

    def _on_zoom_reset(self, event):
        if not self._has_preview():
            return
        self._zoom = 1.0
        self._pan_x = self._pan_y = 0.0
        self._draw_current_sheet()


# =============================================================================
# FLEXCUT WINDOW — osobne okno do zaznaczania naklejek FlexCut
# =============================================================================

class FlexCutWindow(customtkinter.CTkToplevel):
    """Osobne okno do interaktywnego zaznaczania naklejek FlexCut.

    Obsługuje:
      - Klik na naklejkę → toggle zaznaczenia
      - Przeciągnięcie → prostokąt zaznaczenia (rubber band) → zaznacz naklejki w obszarze
      - Scroll → zoom, Shift+drag → pan, Double-click → reset zoom
      - "Dodaj FlexCut" → tworzy prostokąt FlexCut wokół zaznaczonych, można powtarzać
      - "Wyczysc" → usuwa wszystkie FlexCut z bieżącego arkusza
    """

    PADDING = 40

    def __init__(self, parent, job, sheet_pdfs, bleed_mm, on_reexport):
        super().__init__(parent)
        self.title("FlexCut")
        self.geometry("1100x750")
        self.minsize(800, 500)
        self.transient(parent)
        self._parent_app = parent

        self.job = job
        self.sheet_pdfs = sheet_pdfs
        self.bleed_mm = bleed_mm
        self._on_reexport = on_reexport
        self.current_sheet_idx = 0

        # Stan selekcji
        self._selected_placements: set[int] = set()
        self._flexcut_sets_by_sheet: dict[int, list[set[int]]] = {}

        # Transformacja
        self._tx_ox = 0.0
        self._tx_oy = 0.0
        self._tx_scale = 1.0
        self._tx_sh_mm = 0.0
        self._current_rendered = None
        self._render_cache: dict = {}

        # Zoom & pan (Right-click drag lub Shift+drag)
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0

        # Rubber band selection (Left-click drag)
        self._sel_start = None   # (canvas_x, canvas_y) start of selection rectangle
        self._sel_rect_id = None  # canvas item id for rubber band

        # Debounce resize
        self._resize_after_id = None

        # --- Toolbar ---
        toolbar = customtkinter.CTkFrame(self, fg_color="transparent", height=42)
        toolbar.pack(fill="x", padx=10, pady=(8, 0))
        toolbar.pack_propagate(False)

        self._prev_btn = customtkinter.CTkButton(
            toolbar, text="‹", width=28, height=28, corner_radius=6,
            fg_color=("gray90", "gray25"), hover_color=("gray82", "gray38"),
            text_color=TEXT, font=customtkinter.CTkFont(size=14),
            command=self._prev_sheet,
        )
        self._prev_btn.pack(side="left", padx=(0, 4))

        self._sheet_label = customtkinter.CTkLabel(
            toolbar, text="", font=customtkinter.CTkFont(size=12, weight="bold"),
            text_color=TEXT,
        )
        self._sheet_label.pack(side="left", padx=4)

        self._next_btn = customtkinter.CTkButton(
            toolbar, text="›", width=28, height=28, corner_radius=6,
            fg_color=("gray90", "gray25"), hover_color=("gray82", "gray38"),
            text_color=TEXT, font=customtkinter.CTkFont(size=14),
            command=self._next_sheet,
        )
        self._next_btn.pack(side="left", padx=(4, 12))

        self._info_label = customtkinter.CTkLabel(
            toolbar, text="", font=customtkinter.CTkFont(size=10),
            text_color=TEXT_SECONDARY,
        )
        self._info_label.pack(side="left", padx=4)

        # Przyciski po prawej
        customtkinter.CTkButton(
            toolbar, text="Zamknij", width=80, height=28, corner_radius=6,
            fg_color=("gray90", "gray25"), hover_color=("gray82", "gray38"),
            text_color=TEXT, font=customtkinter.CTkFont(size=11),
            command=self._on_close,
        ).pack(side="right", padx=(4, 0))

        customtkinter.CTkButton(
            toolbar, text="Wyczysc", width=80, height=28, corner_radius=6,
            fg_color="transparent", hover_color=("#fee2e2", "#3d1111"),
            text_color=ERROR, font=customtkinter.CTkFont(size=11),
            command=self._on_clear,
        ).pack(side="right", padx=4)

        customtkinter.CTkButton(
            toolbar, text="Dodaj FlexCut", width=120, height=28, corner_radius=6,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            text_color="white", font=customtkinter.CTkFont(size=11, weight="bold"),
            command=self._on_apply,
        ).pack(side="right", padx=4)

        # Instrukcja
        customtkinter.CTkLabel(
            toolbar, text="Zaznacz naklejki (klik / przeciągnij) → Dodaj FlexCut",
            font=customtkinter.CTkFont(size=10), text_color=TEXT_SECONDARY,
        ).pack(side="right", padx=8)

        # --- Canvas ---
        self.canvas = tk.Canvas(self, bg=_preview_bg(), highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True, padx=6, pady=(4, 6))

        # Canvas bindings
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<MouseWheel>", self._on_scroll_zoom)
        self.canvas.bind("<Button-4>", self._on_scroll_zoom)
        self.canvas.bind("<Button-5>", self._on_scroll_zoom)
        # Left click: selekcja (klik/rubber band)
        self.canvas.bind("<ButtonPress-1>", self._on_lmb_press)
        self.canvas.bind("<B1-Motion>", self._on_lmb_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_lmb_release)
        # Right click / middle: pan
        self.canvas.bind("<ButtonPress-2>", self._on_pan_start)
        self.canvas.bind("<B2-Motion>", self._on_pan_move)
        self.canvas.bind("<ButtonPress-3>", self._on_pan_start)
        self.canvas.bind("<B3-Motion>", self._on_pan_move)
        self.canvas.bind("<Double-Button-1>", self._on_zoom_reset)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._update_nav()

    # ----- Navigation -----

    def _prev_sheet(self):
        if self.current_sheet_idx > 0:
            self.current_sheet_idx -= 1
            self._selected_placements.clear()
            self._zoom = 1.0
            self._pan_x = self._pan_y = 0.0
            self._update_nav()
            self._draw_current_sheet()

    def _next_sheet(self):
        n = len(self.job.sheets) if self.job else 0
        if self.current_sheet_idx < n - 1:
            self.current_sheet_idx += 1
            self._selected_placements.clear()
            self._zoom = 1.0
            self._pan_x = self._pan_y = 0.0
            self._update_nav()
            self._draw_current_sheet()

    def _update_nav(self):
        if not self.job or not self.job.sheets:
            self._sheet_label.configure(text="Brak arkuszy")
            self._prev_btn.configure(state="disabled")
            self._next_btn.configure(state="disabled")
            self._info_label.configure(text="")
            return
        n = len(self.job.sheets)
        idx = self.current_sheet_idx
        self._sheet_label.configure(text=f"Arkusz {idx + 1}/{n}")
        self._prev_btn.configure(state="normal" if idx > 0 else "disabled")
        self._next_btn.configure(state="normal" if idx < n - 1 else "disabled")
        sheet = self.job.sheets[idx]
        placed = len(sheet.placements)
        fc = len(sheet.panel_lines)
        info = f"{sheet.width_mm:.0f}×{sheet.height_mm:.0f}mm | {placed} naklejek"
        if fc:
            info += f" | {fc // 4} sub-arkuszy FlexCut"
        self._info_label.configure(text=info)

    # ----- Drawing -----

    def _draw_current_sheet(self):
        self.canvas.delete("all")
        self.canvas.configure(bg=_preview_bg())

        if not self.job or not self.job.sheets:
            return

        sheet = self.job.sheets[self.current_sheet_idx]
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw < 10 or ch < 10:
            return

        pad = self.PADDING
        avail_w = cw - 2 * pad
        avail_h = ch - 2 * pad
        if avail_w <= 0 or avail_h <= 0:
            return

        sw_mm, sh_mm = sheet.width_mm, sheet.height_mm
        if sw_mm <= 0 or sh_mm <= 0:
            return

        base_scale = min(avail_w / sw_mm, avail_h / sh_mm)
        scale = base_scale * self._zoom

        cx_canvas = cw / 2 + self._pan_x
        cy_canvas = ch / 2 + self._pan_y
        ox = cx_canvas - (sw_mm * scale) / 2
        oy = cy_canvas - (sh_mm * scale) / 2

        def tx(xmm): return ox + xmm * scale
        def ty(ymm): return oy + (sh_mm - ymm) * scale

        # Zapisz transformację (do hit-test)
        self._tx_ox = ox
        self._tx_oy = oy
        self._tx_scale = scale
        self._tx_sh_mm = sh_mm

        # Cień
        is_dark = customtkinter.get_appearance_mode() == "Dark"
        shadow = "#1a1a1a" if is_dark else "#c0c0c0"
        self.canvas.create_rectangle(
            tx(0) + 3, ty(sh_mm) + 3, tx(sw_mm) + 3, ty(0) + 3,
            fill=shadow, outline="",
        )

        # Renderuj PDF (print+cut)
        rendered = self._render_sheet_pdf(self.current_sheet_idx, sw_mm, sh_mm, scale)
        if rendered:
            self.canvas.create_image(tx(0), ty(sh_mm), anchor="nw", image=rendered)
            self._current_rendered = rendered
        else:
            self.canvas.create_rectangle(
                tx(0), ty(sh_mm), tx(sw_mm), ty(0),
                fill="#ffffff", outline="#999999", width=1,
            )

        # Wymiary
        dim_color = "#9e9e9e" if is_dark else "#5f6368"
        self.canvas.create_text(
            tx(sw_mm / 2), ty(0) + 14,
            text=f"{sw_mm:.0f}mm", font=customtkinter.CTkFont(size=9), fill=dim_color,
        )
        self.canvas.create_text(
            tx(sw_mm) + 16, ty(sh_mm / 2),
            text=f"{sh_mm:.0f}mm", font=customtkinter.CTkFont(size=9), fill=dim_color, angle=90,
        )

        # Zoom indicator
        if self._zoom > 1.05 or self._zoom < 0.95:
            self.canvas.create_text(
                cw - 8, ch - 8,
                text=f"{self._zoom:.1f}×", font=customtkinter.CTkFont(size=9, weight="bold"),
                fill=dim_color, anchor="se",
            )

        # Zaznaczenie naklejek
        if sheet.placements:
            self._draw_flexcut_selection(sheet, tx, ty)

    def _draw_flexcut_selection(self, sheet, tx, ty):
        """Rysuje ramki wokół zaznaczonych naklejek i bbox setu."""
        applied_sets = self._flexcut_sets_by_sheet.get(self.current_sheet_idx, [])
        all_applied = set()
        for s in applied_sets:
            all_applied |= s

        for idx, p in enumerate(sheet.placements):
            pw = p.sticker.height_mm if abs(p.rotation_deg) in (90, 270) else p.sticker.width_mm
            ph = p.sticker.width_mm if abs(p.rotation_deg) in (90, 270) else p.sticker.height_mm

            if idx in self._selected_placements:
                self.canvas.create_rectangle(
                    tx(p.x_mm), ty(p.y_mm + ph), tx(p.x_mm + pw), ty(p.y_mm),
                    outline="#4f6ef7", width=2.5,
                )
            elif idx in all_applied:
                self.canvas.create_rectangle(
                    tx(p.x_mm), ty(p.y_mm + ph), tx(p.x_mm + pw), ty(p.y_mm),
                    outline="#00e676", width=2, dash=(4, 2),
                )

        # Bbox aktualnego setu
        if self._selected_placements:
            sel = [sheet.placements[i] for i in self._selected_placements
                   if i < len(sheet.placements)]
            if sel:
                def _pw(p):
                    return p.sticker.height_mm if abs(p.rotation_deg) in (90, 270) else p.sticker.width_mm
                def _ph(p):
                    return p.sticker.width_mm if abs(p.rotation_deg) in (90, 270) else p.sticker.height_mm
                bx0 = min(p.x_mm for p in sel)
                by0 = min(p.y_mm for p in sel)
                bx1 = max(p.x_mm + _pw(p) for p in sel)
                by1 = max(p.y_mm + _ph(p) for p in sel)
                self.canvas.create_rectangle(
                    tx(bx0 - 2), ty(by1 + 2), tx(bx1 + 2), ty(by0 - 2),
                    outline="#ff6b00", width=2, dash=(6, 3),
                )

    def _render_sheet_pdf(self, sheet_idx, sw_mm, sh_mm, scale):
        """Renderuje print+cut PDF jako bitmapę do podglądu."""
        if not self.sheet_pdfs or sheet_idx >= len(self.sheet_pdfs):
            return None
        print_path, cut_path = self.sheet_pdfs[sheet_idx]
        if not os.path.exists(print_path) or not os.path.exists(cut_path):
            return None
        target_w = max(1, int(sw_mm * scale))
        target_h = max(1, int(sh_mm * scale))

        # LRU-style cache — unikaj ponownego renderowania przy zoom/pan/resize
        cache_key = (sheet_idx, target_w, target_h)
        if cache_key in self._render_cache:
            return self._render_cache[cache_key]

        try:
            import numpy as np
            import re as _re

            doc_print = fitz_module.open(print_path)
            page_print = doc_print[0]
            zoom_x = target_w / page_print.rect.width
            zoom_y = target_h / page_print.rect.height
            mat = fitz_module.Matrix(zoom_x, zoom_y)
            pix_print = page_print.get_pixmap(matrix=mat, alpha=False)
            img_print = Image.frombytes("RGB", [pix_print.width, pix_print.height], pix_print.samples)
            doc_print.close()

            doc_cut = fitz_module.open(cut_path)
            page_cut = doc_cut[0]
            for cont_xref in page_cut.get_contents():
                stream = doc_cut.xref_stream(cont_xref)
                if stream:
                    text = stream.decode("latin-1", errors="replace")
                    text = _re.sub(r'\b0\.25\s+w\b', '4.0 w', text)
                    doc_cut.update_stream(cont_xref, text.encode("latin-1"))
            pix_cut = page_cut.get_pixmap(matrix=mat, alpha=True)
            img_cut = Image.frombytes("RGBA", [pix_cut.width, pix_cut.height], pix_cut.samples)
            doc_cut.close()

            arr = np.array(img_cut)
            white_mask = (arr[:, :, 0] > 240) & (arr[:, :, 1] > 240) & (arr[:, :, 2] > 240)
            arr[white_mask, 3] = 0
            img_cut = Image.fromarray(arr, "RGBA")
            img_print = img_print.convert("RGBA")
            img_final = Image.alpha_composite(img_print, img_cut)
            result = ImageTk.PhotoImage(img_final)

            # Zapisz w cache (limit 5 wpisów)
            if len(self._render_cache) >= 5:
                oldest_key = next(iter(self._render_cache))
                del self._render_cache[oldest_key]
            self._render_cache[cache_key] = result
            return result
        except Exception:
            return None

    # ----- Debounced resize -----

    def _on_canvas_configure(self, event):
        if self._resize_after_id:
            self.canvas.after_cancel(self._resize_after_id)
        self._resize_after_id = self.canvas.after(150, self._draw_current_sheet)

    # ----- Zoom (scroll) & Pan (right-click / middle-click drag) -----

    def _on_scroll_zoom(self, event):
        if not self.job or not self.job.sheets:
            return
        if event.num == 4 or (hasattr(event, 'delta') and event.delta > 0):
            factor = 1.15
        elif event.num == 5 or (hasattr(event, 'delta') and event.delta < 0):
            factor = 1 / 1.15
        else:
            return
        old_zoom = self._zoom
        self._zoom = max(0.5, min(8.0, self._zoom * factor))
        if old_zoom != self._zoom:
            cx = self.canvas.winfo_width() / 2
            cy = self.canvas.winfo_height() / 2
            ratio = self._zoom / old_zoom
            self._pan_x = event.x - ratio * (event.x - cx - self._pan_x) - cx
            self._pan_y = event.y - ratio * (event.y - cy - self._pan_y) - cy
            self._draw_current_sheet()

    def _on_pan_start(self, event):
        self._pan_drag_start = (event.x, event.y, self._pan_x, self._pan_y)

    def _on_pan_move(self, event):
        if not hasattr(self, '_pan_drag_start') or not self._pan_drag_start:
            return
        sx, sy, px, py = self._pan_drag_start
        self._pan_x = px + (event.x - sx)
        self._pan_y = py + (event.y - sy)
        self._draw_current_sheet()

    def _on_zoom_reset(self, event):
        self._zoom = 1.0
        self._pan_x = self._pan_y = 0.0
        self._draw_current_sheet()

    # ----- Left mouse button: selection (click or rubber band) -----

    def _on_lmb_press(self, event):
        """Początek zaznaczenia: zapisz punkt startowy."""
        self._sel_start = (event.x, event.y)
        self._sel_rect_id = None

    def _on_lmb_motion(self, event):
        """Przeciągnięcie: rysuj prostokąt zaznaczenia (rubber band)."""
        if not self._sel_start:
            return
        sx, sy = self._sel_start
        dx, dy = abs(event.x - sx), abs(event.y - sy)
        if dx < 5 and dy < 5:
            return  # za mały ruch — ignoruj
        # Usuń stary rubber band
        if self._sel_rect_id:
            self.canvas.delete(self._sel_rect_id)
        # Rysuj nowy (tkinter nie obsługuje alpha — użyj stipple)
        self._sel_rect_id = self.canvas.create_rectangle(
            sx, sy, event.x, event.y,
            outline="#4f6ef7", width=2, dash=(4, 4),
        )

    def _on_lmb_release(self, event):
        """Koniec: klik (toggle 1 naklejkę) lub rubber band (zaznacz naklejki w prostokącie)."""
        if not self._sel_start:
            return
        sx, sy = self._sel_start
        ex, ey = event.x, event.y
        self._sel_start = None

        # Usuń rubber band
        if self._sel_rect_id:
            self.canvas.delete(self._sel_rect_id)
            self._sel_rect_id = None

        if not self.job or not self.job.sheets:
            return
        sheet = self.job.sheets[self.current_sheet_idx]
        scale = self._tx_scale
        if scale <= 0:
            return

        dx, dy = abs(ex - sx), abs(ey - sy)

        if dx < 5 and dy < 5:
            # KLIK — toggle jednej naklejki
            x_mm = (ex - self._tx_ox) / scale
            y_mm = self._tx_sh_mm - (ey - self._tx_oy) / scale
            for idx, p in enumerate(sheet.placements):
                pw = p.sticker.height_mm if abs(p.rotation_deg) in (90, 270) else p.sticker.width_mm
                ph = p.sticker.width_mm if abs(p.rotation_deg) in (90, 270) else p.sticker.height_mm
                if p.x_mm <= x_mm <= p.x_mm + pw and p.y_mm <= y_mm <= p.y_mm + ph:
                    if idx in self._selected_placements:
                        self._selected_placements.discard(idx)
                    else:
                        self._selected_placements.add(idx)
                    self._draw_current_sheet()
                    return
        else:
            # RUBBER BAND — zaznacz naklejki w prostokącie
            # Konwertuj oba narożniki na sheet mm
            x0_mm = (min(sx, ex) - self._tx_ox) / scale
            y0_mm = self._tx_sh_mm - (max(sy, ey) - self._tx_oy) / scale
            x1_mm = (max(sx, ex) - self._tx_ox) / scale
            y1_mm = self._tx_sh_mm - (min(sy, ey) - self._tx_oy) / scale

            # Zaznacz naklejki których środek jest w prostokącie
            changed = False
            for idx, p in enumerate(sheet.placements):
                pw = p.sticker.height_mm if abs(p.rotation_deg) in (90, 270) else p.sticker.width_mm
                ph = p.sticker.width_mm if abs(p.rotation_deg) in (90, 270) else p.sticker.height_mm
                cx = p.x_mm + pw / 2
                cy = p.y_mm + ph / 2
                if x0_mm <= cx <= x1_mm and y0_mm <= cy <= y1_mm:
                    self._selected_placements.add(idx)
                    changed = True

            if changed:
                self._draw_current_sheet()

    # ----- FlexCut actions -----

    def _app_log(self, msg):
        """Log do GUI głównej aplikacji (widoczne dla operatora)."""
        try:
            self._parent_app._log(msg)
        except Exception:
            print(msg)

    def _on_apply(self):
        """Dodaj FlexCut — bbox zaznaczonych + 3mm → PanelLine → re-export."""
        try:
            self._on_apply_inner()
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            self._app_log(f"  [ERR] FlexCut crash: {e}\n{tb}")
            try:
                from tkinter import messagebox
                messagebox.showerror("FlexCut Error", f"{e}\n\n{tb}", parent=self)
            except Exception:
                pass

    def _on_apply_inner(self):
        from models import PanelLine
        if not self._selected_placements:
            self._app_log("FlexCut: brak zaznaczonych naklejek")
            return
        if not self.job or not self.job.sheets:
            self._app_log("FlexCut: brak arkuszy")
            return
        sheet = self.job.sheets[self.current_sheet_idx]
        sel = [sheet.placements[i] for i in self._selected_placements
               if i < len(sheet.placements)]
        if not sel:
            self._app_log("FlexCut: zaznaczone indeksy poza zakresem")
            return

        def _pw(p):
            return p.sticker.height_mm if abs(p.rotation_deg) in (90, 270) else p.sticker.width_mm
        def _ph(p):
            return p.sticker.width_mm if abs(p.rotation_deg) in (90, 270) else p.sticker.height_mm

        # Bbox = pełny footprint (content + 2*bleed) + pół gapu na każdą stronę
        # Deduplikacja w export usuwa duplikaty → maszyna tnie 1× w każdym miejscu
        bleed2 = 2 * self.bleed_mm
        gap = getattr(sheet, 'gap_mm', 3.0)
        half_gap = gap / 2

        bx0 = min(p.x_mm for p in sel) - half_gap
        by0 = min(p.y_mm for p in sel) - half_gap
        bx1 = max(p.x_mm + _pw(p) + bleed2 for p in sel) + half_gap
        by1 = max(p.y_mm + _ph(p) + bleed2 for p in sel) + half_gap

        lines = [
            PanelLine("horizontal", by0, bx0, bx1, bridge_length_mm=1.0),
            PanelLine("horizontal", by1, bx0, bx1, bridge_length_mm=1.0),
            PanelLine("vertical", bx0, by0, by1, bridge_length_mm=1.0),
            PanelLine("vertical", bx1, by0, by1, bridge_length_mm=1.0),
        ]
        sheet.panel_lines.extend(lines)
        self._app_log(f"FlexCut: dodano prostokat ({bx0:.1f},{by0:.1f})-({bx1:.1f},{by1:.1f})mm")

        # Zapisz set
        self._flexcut_sets_by_sheet.setdefault(self.current_sheet_idx, []).append(
            set(self._selected_placements))
        self._selected_placements.clear()

        # Re-export
        self._on_reexport(self.current_sheet_idx)
        self._render_cache.clear()

        # Odśwież okno FlexCut (nowy PDF na dysku)
        self._update_nav()
        self._draw_current_sheet()

    def _on_clear(self):
        """Wyczyść wszystkie sety FlexCut na bieżącym arkuszu."""
        if not self.job or not self.job.sheets:
            return
        idx = self.current_sheet_idx
        sheet = self.job.sheets[idx]
        sheet.panel_lines.clear()
        self._flexcut_sets_by_sheet.pop(idx, None)
        self._selected_placements.clear()
        self._app_log("FlexCut: wyczyszczono")

        try:
            self._on_reexport(idx)
        except Exception as e:
            self._app_log(f"  [ERR] FlexCut clear: {e}")
        self._render_cache.clear()
        self._update_nav()
        self._draw_current_sheet()

    def _on_close(self):
        self._selected_placements.clear()
        self.destroy()


class BleedApp(customtkinter.CTk):
    """Główne okno aplikacji Bleed Tool."""

    def __init__(self):
        super().__init__()

        # Inicjalizacja tkdnd (drag & drop)
        if HAS_DND:
            try:
                self.TkdndVersion = tkinterdnd2.TkinterDnD._require(self)
            except Exception:
                pass

        self.title("Bleed Tool")
        self.geometry("1280x780")
        self.minsize(900, 600)

        # Stan — oddzielne listy plików per zakładka
        self._bleed_files: list[str] = []
        self._nest_files: list[str] = []
        self._file_copies: dict[str, int] = {}  # filepath -> copies (nest)
        self._output_dir: str = os.path.join(APP_DIR, "output")
        self._processing = False
        self._preview_images: list = []  # keep references to avoid GC
        self._active_tab: str = "bleed"

        # Crop
        self._crop_offsets: dict[str, tuple[float, float]] = {}
        self._crop_preview_file_idx: int = 0
        self._crop_canvas_img = None       # ImageTk.PhotoImage ref
        self._crop_src_img = None           # PIL source image cache
        self._crop_src_path: str | None = None  # cached source path
        self._drag_start: tuple[int, int] | None = None

        # Batched logging
        self._log_buffer: list[str] = []
        self._log_flush_scheduled = False

        self._build_ui()
        self._setup_dnd()
        self._activate_nav("bleed")

    @property
    def _files(self) -> list[str]:
        """Zwraca listę plików aktywnej zakładki."""
        return self._bleed_files if self._active_tab == "bleed" else self._nest_files

    @_files.setter
    def _files(self, value: list[str]):
        if self._active_tab == "bleed":
            self._bleed_files = value
        else:
            self._nest_files = value

    # =========================================================================
    # UI
    # =========================================================================

    def _build_ui(self):
        # --- Sidebar (nawigacja Bleed / Nest) ---
        self._sidebar = customtkinter.CTkFrame(self, fg_color=SIDEBAR_BG, width=150, corner_radius=0)
        self._sidebar.pack(side="left", fill="y")
        self._sidebar.pack_propagate(False)

        # Logo
        hdr = customtkinter.CTkFrame(self._sidebar, fg_color="transparent")
        hdr.pack(fill="x", padx=14, pady=(18, 14))
        customtkinter.CTkLabel(
            hdr, text="Bleed Tool",
            font=customtkinter.CTkFont(size=15, weight="bold"),
            text_color=TEXT,
        ).pack(anchor="w")

        # Nav buttons
        self._nav_buttons: dict[str, dict] = {}
        self._add_nav("bleed", "Bleed", self._show_bleed_tab)
        self._add_nav("nest", "Nest", self._show_nest_tab)

        # Spacer
        customtkinter.CTkFrame(self._sidebar, fg_color="transparent").pack(fill="both", expand=True)

        # Theme toggle
        customtkinter.CTkButton(
            self._sidebar, text="Light / Dark",
            command=self._on_theme_change,
            width=110, height=28,
            fg_color="transparent", hover_color=SIDEBAR_HOVER,
            text_color=TEXT_SECONDARY,
            font=customtkinter.CTkFont(size=11),
            corner_radius=14,
        ).pack(padx=14, pady=(0, 12))

        # --- Main area z PanedWindow (rozciągalny podział content / preview) ---
        main_area = customtkinter.CTkFrame(self, fg_color=MAIN_BG)
        main_area.pack(side="left", fill="both", expand=True)

        is_dark = customtkinter.get_appearance_mode() == "Dark"
        sash_bg = "#2c2e33" if is_dark else "#ced4da"
        self._paned = tk.PanedWindow(
            main_area, orient="horizontal", sashwidth=5, sashrelief="flat",
            bg=sash_bg, borderwidth=0,
        )
        self._paned.pack(fill="both", expand=True)

        # Lewy panel: content (ustawienia, pliki)
        self._content_outer = customtkinter.CTkFrame(self._paned, fg_color="transparent")
        self._content = customtkinter.CTkScrollableFrame(
            self._content_outer, fg_color="transparent",
        )
        self._content.pack(fill="both", expand=True, padx=16, pady=10)
        self._paned.add(self._content_outer, minsize=300, stretch="always")

        # Prawy panel: preview + log
        self._preview_wrapper = customtkinter.CTkFrame(self._paned, fg_color=MAIN_BG)
        self.preview_panel = SheetPreviewPanel(self._preview_wrapper)
        self.preview_panel.frame.pack(fill="both", expand=True, padx=(0, 4), pady=4)

        # Log pod podglądem
        self._log_text = customtkinter.CTkTextbox(
            self._preview_wrapper, height=100, fg_color=LOG_BG, text_color=LOG_FG,
            font=customtkinter.CTkFont(family="Consolas", size=11),
            state="disabled",
        )
        self._log_text.pack(fill="x", padx=8, pady=(0, 8))
        self._paned.add(self._preview_wrapper, minsize=250, width=420, stretch="always")

    # =========================================================================
    # SIDEBAR NAVIGATION
    # =========================================================================

    def _add_nav(self, key: str, text: str, cmd):
        btn = customtkinter.CTkButton(
            self._sidebar, text=f"  {text}", anchor="w",
            font=customtkinter.CTkFont(size=12, weight="bold"),
            height=40,
            fg_color="transparent", hover_color=SIDEBAR_HOVER,
            text_color=TEXT, corner_radius=8,
            command=lambda k=key: self._activate_nav(k),
        )
        btn.pack(fill="x", padx=8, pady=2)
        self._nav_buttons[key] = {"btn": btn, "cmd": cmd}

    def _activate_nav(self, key: str):
        if self._processing:
            return
        # Deaktywuj poprzedni
        for k, v in self._nav_buttons.items():
            if k.startswith("_"):
                continue
            v["btn"].configure(fg_color="transparent", text_color=TEXT)
        # Aktywuj wybrany
        self._nav_buttons[key]["btn"].configure(
            fg_color=SIDEBAR_ACTIVE_BG, text_color=SIDEBAR_ACTIVE_FG,
        )
        self._active_tab = key
        self._nav_buttons[key]["cmd"]()

    def _clear_content(self):
        # Ukryj crop preview (jeśli aktywny) i przywróć normalny canvas
        if hasattr(self, '_crop_container') and self._crop_container is not None:
            try:
                self._crop_container.pack_forget()
            except Exception:
                pass
        try:
            self.preview_panel.canvas.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        except Exception:
            pass
        for w in self._content.winfo_children():
            w.destroy()

    # =========================================================================
    # TAB: BLEED
    # =========================================================================

    def _show_bleed_tab(self):
        self._clear_content()
        parent = self._content

        # Header
        hdr = customtkinter.CTkFrame(parent, fg_color="transparent")
        hdr.pack(fill="x", pady=(0, 10))
        customtkinter.CTkLabel(
            hdr, text="Bleed",
            font=customtkinter.CTkFont(size=18, weight="bold"),
            text_color=TEXT,
        ).pack(side="left")
        customtkinter.CTkLabel(
            hdr, text="  Generuj bleed i CutContour",
            text_color=TEXT_SECONDARY,
        ).pack(side="left", pady=(3, 0))

        # File section
        self._build_file_section(parent)

        # Settings
        self._build_bleed_settings(parent)

        # Run button
        bar = customtkinter.CTkFrame(parent, fg_color="transparent")
        bar.pack(fill="x", pady=(8, 4))
        self._run_btn = customtkinter.CTkButton(
            bar, text="Generuj bleed",
            font=customtkinter.CTkFont(size=13, weight="bold"),
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            height=38, width=180,
            command=self._on_run,
        )
        self._run_btn.pack(side="left")

        # Preflight button
        self._preflight_btn = customtkinter.CTkButton(
            bar, text="Preflight",
            font=customtkinter.CTkFont(size=12),
            fg_color="transparent", hover_color=("#e7f0ff", "#25334a"),
            text_color=ACCENT, border_width=1, border_color=ACCENT,
            height=38, width=100,
            command=self._on_preflight,
        )
        self._preflight_btn.pack(side="left", padx=(8, 0))

        self._progress_bar = customtkinter.CTkProgressBar(
            bar, width=150, height=8, progress_color=ACCENT,
        )
        self._progress_bar.set(0)
        # Hidden by default — shown during processing
        self._status_label = customtkinter.CTkLabel(
            bar, text="", text_color=TEXT_SECONDARY,
        )
        self._status_label.pack(side="left", padx=(12, 0))

    # =========================================================================
    # TAB: NEST
    # =========================================================================

    def _show_nest_tab(self):
        self._clear_content()
        parent = self._content
        LW = 70   # label width
        FW = 160  # field/control width (jednolita szerokosc dla wszystkich pól)
        _font = customtkinter.CTkFont(size=11)
        _font_b = customtkinter.CTkFont(size=11, weight="bold")

        # Header
        hdr = customtkinter.CTkFrame(parent, fg_color="transparent")
        hdr.pack(fill="x", pady=(0, 8))
        customtkinter.CTkLabel(
            hdr, text="Nest",
            font=customtkinter.CTkFont(size=18, weight="bold"), text_color=TEXT,
        ).pack(side="left")
        customtkinter.CTkLabel(
            hdr, text="  Rozmieszczanie naklejek na arkuszu",
            text_color=TEXT_SECONDARY,
        ).pack(side="left", pady=(3, 0))

        # File section (z kopie per plik)
        self._build_file_section(parent, show_copies=True)

        # === Karta: Arkusz ===
        sheet_card = customtkinter.CTkFrame(parent, fg_color=CARD_BG, corner_radius=10)
        sheet_card.pack(fill="x", pady=(0, 6))
        sb = customtkinter.CTkFrame(sheet_card, fg_color="transparent")
        sb.pack(fill="x", padx=14, pady=(10, 10))

        # Tryb (Arkusze / Rola)
        r = customtkinter.CTkFrame(sb, fg_color="transparent")
        r.pack(fill="x", pady=2)
        customtkinter.CTkLabel(r, text="Tryb", width=LW, anchor="w",
            font=_font, text_color=TEXT_SECONDARY).pack(side="left")
        self._nest_mode_var = customtkinter.StringVar(value="Arkusze")
        customtkinter.CTkSegmentedButton(
            r, values=["Arkusze", "Rola"],
            variable=self._nest_mode_var,
            command=self._on_nest_mode_change,
            font=_font, height=28, width=FW,
        ).pack(side="left")

        # Format — kontener na dwa tryby (sheet / roll)
        r = customtkinter.CTkFrame(sb, fg_color="transparent")
        r.pack(fill="x", pady=2)
        customtkinter.CTkLabel(r, text="Format", width=LW, anchor="w",
            font=_font, text_color=TEXT_SECONDARY).pack(side="left")
        self._nest_format_container = customtkinter.CTkFrame(r, fg_color="transparent")
        self._nest_format_container.pack(side="left", fill="x", expand=True)

        # -- Sheet frame (presety arkuszy)
        self._nest_sheet_frame = customtkinter.CTkFrame(self._nest_format_container, fg_color="transparent")
        sheet_names = list(SHEET_PRESETS.keys()) + list(SHEET_SIZES.keys())
        sheet_names = list(dict.fromkeys(sheet_names))  # unique, ordered
        self._sheet_var = customtkinter.StringVar(value="SRA3")
        customtkinter.CTkComboBox(
            self._nest_sheet_frame, variable=self._sheet_var,
            values=sheet_names, width=FW,
        ).pack(side="left")

        # -- Roll frame (szerokość rolki + max długość)
        self._nest_roll_frame = customtkinter.CTkFrame(self._nest_format_container, fg_color="transparent")
        roll_values = [str(w) for w in ROLL_PRESETS]
        self._roll_width_var = customtkinter.StringVar(value=roll_values[0] if roll_values else "1320")
        customtkinter.CTkComboBox(
            self._nest_roll_frame, variable=self._roll_width_var,
            values=roll_values, width=85,
        ).pack(side="left", padx=(0, 6))
        customtkinter.CTkLabel(self._nest_roll_frame, text="Max",
            font=_font, text_color=TEXT_SECONDARY).pack(side="left", padx=(0, 3))
        self._roll_max_len_var = customtkinter.StringVar(value=str(DEFAULT_ROLL_MAX_LENGTH_MM))
        customtkinter.CTkEntry(
            self._nest_roll_frame, textvariable=self._roll_max_len_var, width=60,
        ).pack(side="left")

        # Auto-ploter: SRA3/SRA3+ → jwei
        self._sheet_var.trace_add("write", self._on_sheet_format_changed)

        # Domyślnie: tryb Arkusze
        self._on_nest_mode_change("Arkusze")

        # Ploter
        r = customtkinter.CTkFrame(sb, fg_color="transparent")
        r.pack(fill="x", pady=2)
        customtkinter.CTkLabel(r, text="Ploter", width=LW, anchor="w",
            font=_font, text_color=TEXT_SECONDARY).pack(side="left")
        self._plotter_var = customtkinter.StringVar(value="jwei")
        customtkinter.CTkOptionMenu(
            r, variable=self._plotter_var,
            values=list(PLOTTERS.keys()), width=FW,
        ).pack(side="left")

        # === Karta: Parametry ===
        params_card = customtkinter.CTkFrame(parent, fg_color=CARD_BG, corner_radius=10)
        params_card.pack(fill="x", pady=(0, 6))
        pb = customtkinter.CTkFrame(params_card, fg_color="transparent")
        pb.pack(fill="x", padx=14, pady=(10, 10))

        # Kopie + Gap w jednym wierszu
        r = customtkinter.CTkFrame(pb, fg_color="transparent")
        r.pack(fill="x", pady=2)
        customtkinter.CTkLabel(r, text="Kopie", width=LW, anchor="w",
            font=_font, text_color=TEXT_SECONDARY).pack(side="left")
        self._copies_var = customtkinter.StringVar(value="1")
        customtkinter.CTkEntry(r, textvariable=self._copies_var, width=55).pack(side="left")
        customtkinter.CTkLabel(r, text="Gap", anchor="w",
            font=_font, text_color=TEXT_SECONDARY).pack(side="left", padx=(16, 4))
        self._gap_var = customtkinter.StringVar(value=str(DEFAULT_GAP_MM))
        customtkinter.CTkEntry(r, textvariable=self._gap_var, width=55).pack(side="left")
        customtkinter.CTkLabel(r, text="mm", font=_font,
            text_color=TEXT_SECONDARY).pack(side="left", padx=(3, 0))

        # Wzory (grupowanie)
        r = customtkinter.CTkFrame(pb, fg_color="transparent")
        r.pack(fill="x", pady=2)
        customtkinter.CTkLabel(r, text="Wzory", width=LW, anchor="w",
            font=_font, text_color=TEXT_SECONDARY).pack(side="left")
        self._grouping_var = customtkinter.StringVar(value="Grupuj")
        customtkinter.CTkSegmentedButton(
            r, values=["Grupuj", "Osobne", "Mieszaj"],
            variable=self._grouping_var,
            font=_font, height=28, width=FW,
        ).pack(side="left")

        # FlexCut
        r = customtkinter.CTkFrame(pb, fg_color="transparent")
        r.pack(fill="x", pady=2)
        customtkinter.CTkLabel(r, text="FlexCut", width=LW, anchor="w",
            font=_font, text_color=TEXT_SECONDARY).pack(side="left")
        customtkinter.CTkButton(
            r, text="FlexCut...", height=28, width=FW,
            font=_font,
            fg_color=("gray90", "gray30"), hover_color=("gray82", "gray38"),
            text_color=TEXT, corner_radius=6,
            command=self._open_flexcut_window,
        ).pack(side="left")

        # Bialy poddruk (White) — nest
        self._nest_white_var = customtkinter.BooleanVar(value=False)
        customtkinter.CTkCheckBox(
            pb, text="Bialy poddruk (White)",
            variable=self._nest_white_var,
            font=customtkinter.CTkFont(size=12),
            checkbox_width=18, checkbox_height=18,
        ).pack(anchor="w", pady=2)

        # Output
        r = customtkinter.CTkFrame(pb, fg_color="transparent")
        r.pack(fill="x", pady=(4, 0))
        customtkinter.CTkLabel(r, text="Output", width=LW, anchor="w",
            font=_font, text_color=TEXT_SECONDARY).pack(side="left")
        self._output_var = customtkinter.StringVar(value=self._output_dir)
        customtkinter.CTkEntry(
            r, textvariable=self._output_var,
        ).pack(side="left", fill="x", expand=True, padx=(0, 4))
        customtkinter.CTkButton(
            r, text="...", width=30, command=self._browse_output,
        ).pack(side="left")

        # === Action bar ===
        bar = customtkinter.CTkFrame(parent, fg_color="transparent")
        bar.pack(fill="x", pady=(8, 4))
        self._nest_btn = customtkinter.CTkButton(
            bar, text="Generuj arkusze",
            font=customtkinter.CTkFont(size=13, weight="bold"),
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            height=38, width=180,
            command=self._on_run_nest,
        )
        self._nest_btn.pack(side="left")
        self._nest_progress_bar = customtkinter.CTkProgressBar(
            bar, width=150, height=8, progress_color=ACCENT,
        )
        self._nest_progress_bar.set(0)
        # Hidden by default — shown during processing
        self._status_label = customtkinter.CTkLabel(
            bar, text="", text_color=TEXT_SECONDARY,
        )
        self._status_label.pack(side="left", padx=(12, 0))

    def _on_nest_mode_change(self, mode: str):
        """Przełącza widoczność sheet_frame / roll_frame + auto-ploter."""
        self._nest_sheet_frame.pack_forget()
        self._nest_roll_frame.pack_forget()
        if mode == "Arkusze":
            self._nest_sheet_frame.pack(fill="x")
            # Auto-ploter: przywróć na podstawie formatu arkusza
            self._on_sheet_format_changed()
        else:
            self._nest_roll_frame.pack(fill="x")
            # Rola → summa_s3
            self._plotter_var.set("summa_s3")

    def _on_sheet_format_changed(self, *_args):
        """Auto-zmiana plotera: SRA3/SRA3+ → jwei, reszta → summa_s3."""
        if not hasattr(self, '_plotter_var'):
            return
        fmt = self._sheet_var.get().upper()
        if fmt in ("SRA3", "SRA3+"):
            self._plotter_var.set("jwei")
        else:
            self._plotter_var.set("summa_s3")

    def _build_file_section(self, parent, show_copies: bool = False):
        """Drop zone + file list. show_copies=True dodaje spinbox kopii per plik (nest)."""
        self._show_copies = show_copies
        card = customtkinter.CTkFrame(parent, fg_color=CARD_BG, corner_radius=10)
        card.pack(fill="x", pady=(0, 8))

        # Drop zone
        self._drop_frame = customtkinter.CTkFrame(
            card, fg_color=DROP_ZONE_BG, corner_radius=8,
            border_width=2, border_color=DROP_ZONE_BORDER,
            height=64,
        )
        self._drop_frame.pack(fill="x", padx=10, pady=(10, 5))
        self._drop_frame.pack_propagate(False)

        if HAS_DND:
            drop_text = "Przeciagnij pliki PDF / SVG / EPS / PNG / JPG\nlub kliknij aby wybrac"
        else:
            drop_text = "Kliknij aby wybrac pliki\nPDF / SVG / EPS / PNG / JPG"

        drop_label = customtkinter.CTkLabel(
            self._drop_frame, text=drop_text,
            text_color=TEXT_SECONDARY, justify="center", cursor="hand2",
        )
        drop_label.place(relx=0.5, rely=0.5, anchor="center")
        drop_label.bind("<Button-1>", lambda e: self._browse_files())
        self._drop_frame.configure(cursor="hand2")
        self._drop_frame.bind("<Button-1>", lambda e: self._browse_files())

        # File list (stała wysokość — max 5 wierszy)
        _fl_wrap = customtkinter.CTkFrame(card, fg_color="transparent", height=75)
        _fl_wrap.pack(fill="x", padx=10, pady=(0, 3))
        _fl_wrap.pack_propagate(False)
        self._file_list = customtkinter.CTkScrollableFrame(
            _fl_wrap, fg_color="transparent",
        )
        self._file_list.pack(fill="both", expand=True)

        # Count + clear
        count_row = customtkinter.CTkFrame(card, fg_color="transparent")
        count_row.pack(fill="x", padx=10, pady=(0, 8))

        self._file_count_label = customtkinter.CTkLabel(
            count_row, text="0 plikow", text_color=TEXT_SECONDARY,
            font=customtkinter.CTkFont(size=11),
        )
        self._file_count_label.pack(side="left")

        self._clear_btn = customtkinter.CTkButton(
            count_row, text="Wyczysc", width=70, height=24,
            fg_color="transparent", hover_color=("#fee2e2", "#3d1111"),
            text_color=ERROR, border_width=1, border_color=ERROR,
            font=customtkinter.CTkFont(size=11),
            command=self._clear_files,
        )
        self._clear_btn.pack(side="right")

        # Rejestruj DnD na nowym drop_frame
        self._register_drop_target(self._drop_frame)
        self._register_drop_target(drop_label)

        # Odśwież listę (pokaże count)
        self._refresh_file_list()

    def _build_bleed_settings(self, parent):
        """Ustawienia bleed — pack-based card."""
        card = customtkinter.CTkFrame(parent, fg_color=CARD_BG, corner_radius=10)
        card.pack(fill="x", pady=(0, 8))

        customtkinter.CTkLabel(
            card, text="Parametry",
            font=customtkinter.CTkFont(size=11, weight="bold"),
            text_color=TEXT,
        ).pack(anchor="w", padx=14, pady=(10, 4))

        body = customtkinter.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=14, pady=(0, 10))

        def _row(label_text, widget_factory):
            r = customtkinter.CTkFrame(body, fg_color="transparent")
            r.pack(fill="x", pady=2)
            customtkinter.CTkLabel(
                r, text=label_text, width=100, anchor="w",
                font=customtkinter.CTkFont(size=11), text_color=TEXT_SECONDARY,
            ).pack(side="left")
            widget_factory(r)

        # Bleed
        self._bleed_var = customtkinter.StringVar(value=str(DEFAULT_BLEED_MM))
        _row("Bleed (mm)", lambda r: customtkinter.CTkEntry(
            r, textvariable=self._bleed_var, width=70,
        ).pack(side="left"))

        # Wysokość
        self._height_var = customtkinter.StringVar(value="")
        _row("Wysokosc (cm)", lambda r: customtkinter.CTkEntry(
            r, textvariable=self._height_var, width=70, placeholder_text="auto",
        ).pack(side="left"))

        # Crop
        self._crop_var = customtkinter.BooleanVar(value=False)
        crop_row = customtkinter.CTkFrame(body, fg_color="transparent")
        crop_row.pack(fill="x", pady=2)
        self._crop_cb = customtkinter.CTkCheckBox(
            crop_row, text="Przytnij do rozmiaru",
            variable=self._crop_var,
            font=customtkinter.CTkFont(size=12),
            checkbox_width=18, checkbox_height=18,
            command=self._on_crop_changed,
        )
        self._crop_cb.pack(side="left")
        self._crop_cb.configure(state="disabled")

        self._crop_shape_var = customtkinter.StringVar(value="Kwadrat")
        self._crop_shape_btn = customtkinter.CTkSegmentedButton(
            crop_row, values=["Kwadrat", "Zaokraglony", "Okrag", "Owal"],
            variable=self._crop_shape_var,
            command=self._on_crop_shape_changed,
            width=290, font=customtkinter.CTkFont(size=11),
        )
        # ukryty domyślnie (pack gdy crop włączony)

        self._height_var.trace_add("write", self._on_height_changed)

        # Czarny 100% K
        self._black_100k_var = customtkinter.BooleanVar(value=False)
        self._black_100k_cb = customtkinter.CTkCheckBox(
            body, text="Czarny → 100% K",
            variable=self._black_100k_var,
            font=customtkinter.CTkFont(size=12),
            checkbox_width=18, checkbox_height=18,
        )
        self._black_100k_cb.pack(anchor="w", pady=2)
        self._black_100k_cb.configure(state="disabled")

        # CutContour
        self._cutcontour_var = customtkinter.BooleanVar(value=True)
        customtkinter.CTkCheckBox(
            body, text="Linia ciecia (CutContour)",
            variable=self._cutcontour_var,
            font=customtkinter.CTkFont(size=12),
            checkbox_width=18, checkbox_height=18,
        ).pack(anchor="w", pady=2)

        # Bialy poddruk (White)
        self._white_var = customtkinter.BooleanVar(value=False)
        customtkinter.CTkCheckBox(
            body, text="Bialy poddruk (White)",
            variable=self._white_var,
            font=customtkinter.CTkFont(size=12),
            checkbox_width=18, checkbox_height=18,
        ).pack(anchor="w", pady=2)

        # Output
        out_row = customtkinter.CTkFrame(body, fg_color="transparent")
        out_row.pack(fill="x", pady=(4, 0))
        customtkinter.CTkLabel(
            out_row, text="Output", width=100, anchor="w",
            font=customtkinter.CTkFont(size=11), text_color=TEXT_SECONDARY,
        ).pack(side="left")
        self._output_var = customtkinter.StringVar(value=self._output_dir)
        customtkinter.CTkEntry(
            out_row, textvariable=self._output_var,
        ).pack(side="left", fill="x", expand=True, padx=(0, 4))
        customtkinter.CTkButton(
            out_row, text="...", width=30, command=self._browse_output,
        ).pack(side="left")

    # (_build_nest_settings removed — nest settings built inline in _show_nest_tab)

    # (_build_preview_section and _build_log_section removed —
    #  replaced by SheetPreviewPanel + inline log in _build_ui)

    # =========================================================================
    # CROP — callbacks i podgląd
    # =========================================================================

    def _on_height_changed(self, *_args):
        """Trace callback: aktywuj/deaktywuj checkbox crop."""
        val_str = self._height_var.get().strip().replace(",", ".")
        try:
            val = float(val_str)
            valid = val > 0
        except (ValueError, TypeError):
            valid = False

        if valid:
            self._crop_cb.configure(state="normal")
        else:
            self._crop_cb.configure(state="disabled")
            if self._crop_var.get():
                self._crop_var.set(False)
                self._on_crop_changed()

    def _on_crop_changed(self):
        """Callback: włącz/wyłącz tryb crop."""
        if self._crop_var.get():
            self._crop_shape_btn.pack(side="left", padx=(10, 0))
            self._show_crop_preview()
        else:
            self._crop_shape_btn.pack_forget()
            self._hide_crop_preview()

    def _on_crop_shape_changed(self, _value=None):
        """Callback: zmiana kształtu crop — odśwież podgląd."""
        if self._crop_var.get():
            self._redraw_crop_canvas()

    def _show_crop_preview(self):
        """Pokaż crop canvas, ukryj normalny podgląd."""
        if not self._files:
            return
        # Lazy init crop container w preview panel
        if not hasattr(self, '_crop_container') or self._crop_container is None:
            self._crop_container = customtkinter.CTkFrame(
                self.preview_panel.frame, fg_color="transparent",
            )
            nav = customtkinter.CTkFrame(self._crop_container, fg_color="transparent")
            nav.pack(fill="x", pady=(0, 5))
            self._crop_prev_btn = customtkinter.CTkButton(
                nav, text="‹", width=30, command=self._crop_prev_file,
            )
            self._crop_prev_btn.pack(side="left", padx=(0, 4))
            # Prawy przycisk PRZED label — pack(side="right") rezerwuje miejsce
            self._crop_next_btn = customtkinter.CTkButton(
                nav, text="›", width=30, command=self._crop_next_file,
            )
            self._crop_next_btn.pack(side="right", padx=(4, 0))
            # Label wypełnia resztę — długa nazwa obcinana (anchor="center")
            self._crop_file_label = customtkinter.CTkLabel(
                nav, text="", font=customtkinter.CTkFont(size=11),
                anchor="center",
            )
            self._crop_file_label.pack(side="left", fill="x", expand=True)
            self._crop_canvas = tk.Canvas(
                self._crop_container, bg="#e0e0e0", highlightthickness=0, cursor="fleur",
            )
            self._crop_canvas.pack(fill="both", expand=True)
            self._crop_canvas.bind("<ButtonPress-1>", self._crop_on_press)
            self._crop_canvas.bind("<B1-Motion>", self._crop_on_drag)
            self._crop_canvas.bind("<ButtonRelease-1>", self._crop_on_release)
            self._crop_canvas.bind("<Configure>", self._crop_on_resize)

        self.preview_panel.canvas.pack_forget()
        self._crop_container.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self._crop_preview_file_idx = 0
        self._crop_src_path = None
        self._update_crop_preview()

    def _hide_crop_preview(self):
        """Ukryj crop canvas, pokaż normalny podgląd."""
        if hasattr(self, '_crop_container') and self._crop_container is not None:
            self._crop_container.pack_forget()
        self.preview_panel.canvas.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self._crop_canvas.delete("all")
        self._crop_canvas_img = None
        self._crop_src_img = None
        self._crop_src_path = None

    def _crop_prev_file(self):
        if not self._files:
            return
        self._crop_preview_file_idx = (self._crop_preview_file_idx - 1) % len(self._files)
        self._crop_src_path = None
        self._update_crop_preview()

    def _crop_next_file(self):
        if not self._files:
            return
        self._crop_preview_file_idx = (self._crop_preview_file_idx + 1) % len(self._files)
        self._crop_src_path = None
        self._update_crop_preview()

    def _update_crop_preview(self):
        """Załaduj obraz i narysuj crop overlay."""
        if not self._files:
            return
        idx = self._crop_preview_file_idx % len(self._files)
        filepath = self._files[idx]

        # Nawigacja label
        name = os.path.basename(filepath)
        self._crop_file_label.configure(text=f"{idx + 1}/{len(self._files)}: {name}")
        self._crop_prev_btn.configure(state="normal" if len(self._files) > 1 else "disabled")
        self._crop_next_btn.configure(state="normal" if len(self._files) > 1 else "disabled")

        # Załaduj source image (cache)
        if self._crop_src_path != filepath:
            self._crop_src_path = filepath
            try:
                from modules.crop import load_preview_image
                self._crop_src_img = load_preview_image(filepath, max_size=600)
            except Exception:
                self._crop_src_img = Image.new("RGB", (200, 200), (200, 200, 200))

        # Domyślny offset jeśli brak
        if filepath not in self._crop_offsets:
            self._crop_offsets[filepath] = (0.5, 0.5)

        self._redraw_crop_canvas()

    def _redraw_crop_canvas(self):
        """Przerysuj canvas z obrazem i overlayem crop."""
        canvas = self._crop_canvas
        canvas.delete("all")

        if self._crop_src_img is None:
            return

        cw = canvas.winfo_width()
        ch = canvas.winfo_height()
        if cw < 10 or ch < 10:
            return

        src_w, src_h = self._crop_src_img.size
        crop_shape = {"Okrag": "circle", "Zaokraglony": "rounded", "Owal": "oval"}.get(self._crop_shape_var.get(), "square")

        # Crop area jest kwadratowy — dopasuj do mniejszego wymiaru canvas
        canvas_crop = int(min(cw, ch) * 0.85)

        # Skaluj obraz aby pokrył crop area (cover)
        scale = max(canvas_crop / src_w, canvas_crop / src_h)
        disp_w = int(src_w * scale)
        disp_h = int(src_h * scale)

        # Pozycja obrazu na canvas (z offsetu)
        filepath = self._files[self._crop_preview_file_idx % len(self._files)]
        ox, oy = self._crop_offsets.get(filepath, (0.5, 0.5))

        # Obszar przesunięcia
        pan_x = max(0, disp_w - canvas_crop)
        pan_y = max(0, disp_h - canvas_crop)

        # Pozycja lewego górnego rogu obrazu
        img_x = (cw - canvas_crop) // 2 - int(ox * pan_x)
        img_y = (ch - canvas_crop) // 2 - int(oy * pan_y)

        # Resize obrazu
        resized = self._crop_src_img.resize((disp_w, disp_h), Image.LANCZOS)
        self._crop_canvas_img = ImageTk.PhotoImage(resized)

        # Rysuj obraz
        canvas.create_image(img_x, img_y, anchor="nw", image=self._crop_canvas_img)

        # Crop overlay — przyciemnij poza crop area
        crop_x0 = (cw - canvas_crop) // 2
        crop_y0 = (ch - canvas_crop) // 2
        crop_x1 = crop_x0 + canvas_crop
        crop_y1 = crop_y0 + canvas_crop

        # Zapamiętaj wymiary crop do drag
        self._crop_rect = (crop_x0, crop_y0, crop_x1, crop_y1)
        self._crop_disp_size = (disp_w, disp_h)
        self._crop_canvas_crop = canvas_crop

        # Semi-transparent overlay (4 prostokąty wokół)
        overlay_color = "#00000060"
        canvas.create_rectangle(0, 0, cw, crop_y0, fill="#888888", stipple="gray50", outline="")
        canvas.create_rectangle(0, crop_y1, cw, ch, fill="#888888", stipple="gray50", outline="")
        canvas.create_rectangle(0, crop_y0, crop_x0, crop_y1, fill="#888888", stipple="gray50", outline="")
        canvas.create_rectangle(crop_x1, crop_y0, cw, crop_y1, fill="#888888", stipple="gray50", outline="")

        # Ramka crop
        if crop_shape == "circle" or crop_shape == "oval":
            canvas.create_oval(
                crop_x0, crop_y0, crop_x1, crop_y1,
                outline=ACCENT, width=2,
            )
        elif crop_shape == "rounded":
            # Zaokrąglony kwadrat — promień = 15% boku
            r = int(canvas_crop * 0.15)
            _draw_rounded_rect(canvas, crop_x0, crop_y0, crop_x1, crop_y1, r,
                               outline=ACCENT, width=2)
        else:
            canvas.create_rectangle(
                crop_x0, crop_y0, crop_x1, crop_y1,
                outline=ACCENT, width=2,
            )

    def _crop_on_press(self, event):
        self._drag_start = (event.x, event.y)

    def _crop_on_drag(self, event):
        if self._drag_start is None or not self._files:
            return

        dx = event.x - self._drag_start[0]
        dy = event.y - self._drag_start[1]
        self._drag_start = (event.x, event.y)

        filepath = self._files[self._crop_preview_file_idx % len(self._files)]
        ox, oy = self._crop_offsets.get(filepath, (0.5, 0.5))

        # Przelicz dx/dy na zmianę offsetu
        canvas_crop = getattr(self, '_crop_canvas_crop', 100)
        disp_w, disp_h = getattr(self, '_crop_disp_size', (100, 100))
        pan_x = max(1, disp_w - canvas_crop)
        pan_y = max(1, disp_h - canvas_crop)

        # Przeciągamy obraz (odwrotny kierunek do offsetu)
        new_ox = max(0.0, min(1.0, ox - dx / pan_x))
        new_oy = max(0.0, min(1.0, oy - dy / pan_y))

        self._crop_offsets[filepath] = (new_ox, new_oy)
        self._redraw_crop_canvas()

    def _crop_on_release(self, event):
        self._drag_start = None

    def _crop_on_resize(self, event):
        """Canvas się zmienił — przerysuj."""
        if self._crop_var.get() and self._crop_src_img is not None:
            self.after(50, self._redraw_crop_canvas)

    # =========================================================================
    # DRAG & DROP
    # =========================================================================

    def _setup_dnd(self):
        """Rejestruj DnD na głównym oknie — wywoływane RAZ w __init__."""
        if not HAS_DND:
            return
        try:
            self.drop_target_register(tkinterdnd2.DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:
            pass

    def _register_drop_target(self, widget):
        """Rejestruj DnD na pojedynczym widgecie (drop zone, label)."""
        if not HAS_DND:
            return
        try:
            widget.drop_target_register(tkinterdnd2.DND_FILES)
            widget.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:
            pass

    def _on_drop(self, event):
        raw = event.data
        # Parsuj ścieżki (mogą być w {} dla spacji)
        paths = []
        i = 0
        while i < len(raw):
            if raw[i] == '{':
                j = raw.index('}', i)
                paths.append(raw[i + 1:j])
                i = j + 1
            elif raw[i] == ' ':
                i += 1
            else:
                j = raw.find(' ', i)
                if j == -1:
                    j = len(raw)
                paths.append(raw[i:j])
                i = j
        self._add_files(paths)

    # =========================================================================
    # FILE MANAGEMENT
    # =========================================================================

    def _browse_files(self):
        paths = filedialog.askopenfilenames(
            title="Wybierz pliki",
            filetypes=[
                ("Grafika", "*.pdf *.svg *.png *.jpg *.jpeg *.tiff *.tif *.bmp *.webp"),
                ("PDF & SVG", "*.pdf *.svg"),
                ("Obrazy rastrowe", "*.png *.jpg *.jpeg *.tiff *.tif *.bmp *.webp"),
                ("Wszystkie pliki", "*.*"),
            ],
        )
        if paths:
            self._add_files(list(paths))

    def _add_files(self, paths: list[str]):
        existing = set(self._files)
        added = []
        for p in paths:
            p = os.path.normpath(p)
            if p.lower().endswith(_SUPPORTED_EXT) and p not in existing:
                self._files.append(p)
                existing.add(p)
                added.append(p)
        self._refresh_file_list()
        # Auto-preflight nowo dodanych plikow (tylko bleed tab)
        if added and self._active_tab == "bleed":
            self._clear_log()
            self._log("Preflight:\n")
            for fpath in added:
                try:
                    result = preflight_check(fpath)
                    self._log(format_preflight_result(result))
                except Exception:
                    pass

    def _remove_file(self, path: str):
        if path in self._files:
            self._files.remove(path)
            self._refresh_file_list()

    def _clear_files(self):
        self._files.clear()
        self._refresh_file_list()

    def _refresh_file_list(self):
        # Sprawdź czy widget istnieje (mógł zostać zniszczony przy przełączaniu tabów)
        try:
            self._file_list.winfo_exists()
        except Exception:
            return
        if not self._file_list.winfo_exists():
            return

        for widget in self._file_list.winfo_children():
            widget.destroy()

        show_copies = getattr(self, '_show_copies', False)

        for path in self._files:
            row_h = 22 if show_copies else 18
            row = customtkinter.CTkFrame(self._file_list, fg_color="transparent", height=row_h)
            row.pack(fill="x", pady=0)
            row.pack_propagate(False)

            customtkinter.CTkButton(
                row, text="x", width=18, height=16,
                fg_color="transparent", hover_color=("#fee2e2", "#3d1111"),
                text_color=ERROR, font=customtkinter.CTkFont(size=9),
                command=lambda p=path: self._remove_file(p),
            ).pack(side="right")

            if show_copies:
                # Spinbox kopii
                if path not in self._file_copies:
                    self._file_copies[path] = 1
                copies_var = tk.StringVar(value=str(self._file_copies[path]))
                filepath = path
                def _on_copies_change(var, filepath=filepath, sv=copies_var):
                    try:
                        val = int(sv.get())
                        if val >= 1:
                            self._file_copies[filepath] = val
                    except ValueError:
                        pass
                copies_var.trace_add("write", lambda *_, cb=_on_copies_change: cb(None))
                customtkinter.CTkEntry(
                    row, textvariable=copies_var, width=35, height=18,
                    font=customtkinter.CTkFont(size=9), justify="center",
                ).pack(side="right", padx=(2, 4))

            name = os.path.basename(path)
            customtkinter.CTkLabel(
                row, text=name, anchor="w",
                font=customtkinter.CTkFont(size=10),
            ).pack(side="left", fill="x", expand=True)

        count = len(self._files)
        try:
            self._file_count_label.configure(
                text=f"{count} plik(ow)" if count != 1 else "1 plik"
            )
        except Exception:
            pass

        # Bleed-only: aktualizuj stan widgetów (tylko gdy istnieją na aktywnej zakładce)
        if hasattr(self, '_black_100k_cb') and self._active_tab == "bleed":
            has_pdf = any(p.lower().endswith(('.pdf', '.svg')) for p in self._files)
            try:
                self._black_100k_cb.configure(state="normal" if has_pdf else "disabled")
                if not has_pdf:
                    self._black_100k_var.set(False)
            except Exception:
                pass

        if hasattr(self, '_crop_var') and self._active_tab == "bleed":
            try:
                if self._crop_var.get() and self._files:
                    self._crop_preview_file_idx = min(
                        self._crop_preview_file_idx, len(self._files) - 1
                    )
                    self._crop_src_path = None
                    self._show_crop_preview()
                elif self._crop_var.get() and not self._files:
                    self._hide_crop_preview()
            except Exception:
                pass

    def _browse_output(self):
        d = filedialog.askdirectory(title="Wybierz folder wyjsciowy")
        if d:
            self._output_dir = d
            self._output_var.set(d)

    # =========================================================================
    # THEME
    # =========================================================================

    def _on_theme_change(self):
        if customtkinter.get_appearance_mode() == "Dark":
            customtkinter.set_appearance_mode("light")
        else:
            customtkinter.set_appearance_mode("dark")

    # =========================================================================
    # PROCESSING
    # =========================================================================

    # =========================================================================
    # PREFLIGHT
    # =========================================================================

    def _on_preflight(self):
        """Uruchamia preflight check na wszystkich zaladowanych plikach."""
        if not self._files:
            messagebox.showwarning("Bleed Tool", "Brak plikow do sprawdzenia.\nPrzeciagnij lub wybierz pliki.")
            return
        self._clear_log()
        self._log("Preflight check:\n")
        ok_count = 0
        warn_count = 0
        err_count = 0
        for fpath in self._files:
            try:
                result = preflight_check(fpath)
                line = format_preflight_result(result)
                self._log(line)
                # Dodatkowe szczegoly dla issues/warnings
                for issue in result["issues"]:
                    self._log(f"      {issue['message']}")
                for warn in result["warnings"]:
                    if warn["severity"] == "warning":
                        self._log(f"      {warn['message']}")
                if result["status"] == "ok":
                    ok_count += 1
                elif result["status"] == "warning":
                    warn_count += 1
                else:
                    err_count += 1
            except Exception as e:
                self._log(f"[XX] {os.path.basename(fpath)}: blad analizy — {e}")
                err_count += 1
        self._log(f"\nPreflight: {ok_count} OK, {warn_count} ostrzezen, {err_count} bledow")

    def _run_auto_preflight(self):
        """Szybki auto-preflight przy dodawaniu plikow — loguje podsumowanie."""
        if not self._files or self._active_tab != "bleed":
            return
        # Zbierz krotkie info o nowo dodanych plikach
        for fpath in self._files:
            try:
                result = preflight_check(fpath)
                line = format_preflight_result(result)
                self._log(line)
            except Exception:
                pass

    def _on_run(self):
        if self._processing:
            return
        if not self._files:
            messagebox.showwarning("Bleed Tool", "Brak plikow do przetworzenia.\nPrzeciagnij lub wybierz pliki.")
            return

        self._output_dir = self._output_var.get()
        try:
            bleed_mm = float(self._bleed_var.get())
            if bleed_mm < 0:
                raise ValueError
        except (ValueError, TypeError):
            messagebox.showwarning("Bleed Tool", "Nieprawidlowa wartosc bleed.")
            return
        black_100k = self._black_100k_var.get()
        cutcontour = self._cutcontour_var.get()
        white = self._white_var.get()

        # Wysokość docelowa (cm → mm), puste = brak skalowania
        height_cm_str = self._height_var.get().strip()
        target_height_mm = None
        if height_cm_str:
            try:
                val = float(height_cm_str.replace(",", "."))
                if val > 0:
                    target_height_mm = val * 10.0  # cm → mm
                else:
                    messagebox.showwarning("Bleed Tool", "Wysokosc musi byc wieksza od 0.")
                    return
            except ValueError:
                messagebox.showwarning("Bleed Tool", "Nieprawidlowa wartosc wysokosci.")
                return

        # Crop
        crop_enabled = self._crop_var.get() and target_height_mm is not None
        crop_shape = {"Okrag": "circle", "Zaokraglony": "rounded", "Owal": "oval"}.get(self._crop_shape_var.get(), "square")
        crop_offsets = dict(self._crop_offsets) if crop_enabled else {}

        self._processing = True
        self._run_btn.configure(state="disabled", text="Przetwarzam...")
        if hasattr(self, '_progress_bar'):
            self._progress_bar.set(0)
            self._progress_bar.pack(side="left", padx=(8, 0))
        self._clear_log()
        self._clear_preview()
        self._log(f"Start: {len(self._files)} plik(ow), bleed={bleed_mm}mm")
        if target_height_mm is not None:
            self._log(f"  Wysokosc docelowa: {target_height_mm:.1f}mm ({height_cm_str}cm)")
        if crop_enabled:
            self._log(f"  Crop: {crop_shape}")
        if black_100k:
            self._log("  Czarny 100% K: wlaczony")
        if not cutcontour:
            self._log("  Linia ciecia: wylaczona (sam spad)")
        if white:
            self._log("  Bialy poddruk (White): wlaczony")
        self._log(f"Output: {self._output_dir}\n")

        thread = threading.Thread(
            target=self._worker,
            args=(list(self._files), self._output_dir, bleed_mm, black_100k,
                  cutcontour, target_height_mm, crop_enabled, crop_shape,
                  crop_offsets, white),
            daemon=True,
        )
        thread.start()

    def _worker(self, files: list[str], output_dir: str, bleed_mm: float,
                black_100k: bool = False, cutcontour: bool = True,
                target_height_mm: float | None = None,
                crop_enabled: bool = False, crop_shape: str = "square",
                crop_offsets: dict | None = None, white: bool = False):
        from modules.contour import detect_contour, scale_sticker
        from modules.bleed import generate_bleed
        from modules.export import export_single_sticker

        os.makedirs(output_dir, exist_ok=True)
        crop_offsets = crop_offsets or {}
        temp_files: list[str] = []

        t0 = time.time()
        ok, err = 0, 0
        output_paths = []

        total = len(files)
        for file_idx, filepath in enumerate(files):
            # Progress update (thread-safe)
            self.after(0, lambda v=file_idx/total, idx=file_idx, tot=total:
                       (self._progress_bar.set(v) if hasattr(self, '_progress_bar') else None,
                        self._status_label.configure(text=f"Plik {idx+1}/{tot}...")))
            name = os.path.splitext(os.path.basename(filepath))[0]
            actual_path = filepath

            try:
                # Crop: przycięcie przed pipeline
                if crop_enabled and target_height_mm is not None:
                    from modules.crop import apply_crop
                    offset = crop_offsets.get(filepath, (0.5, 0.5))
                    actual_path = apply_crop(
                        filepath,
                        target_size_mm=target_height_mm,
                        shape=crop_shape,
                        offset=offset,
                    )
                    temp_files.append(actual_path)

                stickers = detect_contour(actual_path)
                multi = len(stickers) > 1

                # Skalowanie do docelowej wysokości (pomijane gdy crop)
                if target_height_mm is not None and not crop_enabled:
                    stickers = [
                        scale_sticker(s, target_height_mm)
                        for s in stickers
                    ]

                for sticker in stickers:
                    if multi:
                        out = os.path.join(output_dir, f"bleed_{name}_p{sticker.page_index + 1}.pdf")
                        label = f"{name} p{sticker.page_index + 1}"
                    else:
                        out = os.path.join(output_dir, f"bleed_{name}.pdf")
                        label = name

                    try:
                        sticker = generate_bleed(sticker, bleed_mm=bleed_mm)
                        info = export_single_sticker(
                            sticker, out, bleed_mm=bleed_mm,
                            black_100k=black_100k, cutcontour=cutcontour,
                            white=white,
                        )

                        size_kb = os.path.getsize(out) / 1024
                        self._log(
                            f"  [OK] {label}: "
                            f"{info['output_size_mm'][0]:.1f}x{info['output_size_mm'][1]:.1f}mm "
                            f"({size_kb:.1f}KB)"
                        )
                        output_paths.append(out)
                        ok += 1
                    except Exception as e:
                        self._log(f"  [ERR] {label}: {e}")
                        err += 1

                if stickers[0].pdf_doc is not None:
                    stickers[0].pdf_doc.close()

            except Exception as e:
                self._log(f"  [ERR] {name}: {e}")
                err += 1

        # Cleanup temp plików z crop
        for tmp in temp_files:
            try:
                os.unlink(tmp)
            except OSError:
                pass

        elapsed = time.time() - t0
        summary = f"\nGotowe: {ok} naklejek"
        if err:
            summary += f", {err} bledow"
        summary += f" ({elapsed:.1f}s)"
        self._log(summary)

        # Aktualizacja UI z głównego wątku
        self.after(0, lambda: self._on_worker_done(output_paths))

    # =========================================================================
    # NEST — rozkład na arkuszu
    # =========================================================================

    def _on_run_nest(self):
        if self._processing:
            return
        if not self._files:
            messagebox.showwarning("Bleed Tool", "Brak plikow do przetworzenia.\nPrzeciagnij lub wybierz pliki.")
            return

        self._output_dir = self._output_var.get()

        # Kopie
        try:
            copies = int(self._copies_var.get())
            if copies < 1:
                raise ValueError
        except (ValueError, TypeError):
            messagebox.showwarning("Bleed Tool", "Nieprawidlowa liczba kopii.")
            return

        # Parsuj wymiary arkusza wg trybu
        mode = self._nest_mode_var.get()
        if mode == "Rola":
            try:
                sheet_w = float(self._roll_width_var.get())
            except ValueError:
                messagebox.showwarning("Bleed Tool", "Niepoprawna szerokosc rolki.")
                return
            try:
                max_len = float(self._roll_max_len_var.get())
            except ValueError:
                max_len = DEFAULT_ROLL_MAX_LENGTH_MM
            sheet_h = None
            max_sheet_length = max_len
        else:
            sheet_name = self._sheet_var.get()
            # Parsuj "WxH" lub preset
            if "x" in sheet_name.lower():
                try:
                    parts = sheet_name.lower().split("x")
                    sheet_w, sheet_h = float(parts[0]), float(parts[1])
                except (ValueError, IndexError):
                    messagebox.showwarning("Bleed Tool", f"Nieznany format: {sheet_name}")
                    return
            elif sheet_name in SHEET_SIZES:
                sheet_w, sheet_h = SHEET_SIZES[sheet_name]
            else:
                messagebox.showwarning("Bleed Tool", f"Nieznany format: {sheet_name}")
                return
            max_sheet_length = None

        # Gap
        try:
            gap_mm = float(self._gap_var.get())
        except (ValueError, TypeError):
            messagebox.showwarning("Bleed Tool", "Nieprawidlowa wartosc gap.")
            return

        params = {
            "out_dir": self._output_dir,
            "sheet_w": sheet_w,
            "sheet_h": sheet_h,
            "max_sheet_length": max_sheet_length,
            "bleed": DEFAULT_BLEED_MM,
            "copies": copies,
            "plotter": self._plotter_var.get(),
            "gap": gap_mm,
            # FlexCut jest dodawany ręcznie po nestingu (zaznacz naklejki)
            "grouping_mode": self._grouping_var.get(),
            "file_copies": dict(self._file_copies),
            "input_files": list(self._files),
            "white": self._nest_white_var.get() if hasattr(self, '_nest_white_var') else False,
        }

        self._processing = True
        self._nest_btn.configure(state="disabled", text="Rozmieszczam...")
        if hasattr(self, '_nest_progress_bar'):
            self._nest_progress_bar.set(0)
            self._nest_progress_bar.pack(side="left", padx=(8, 0))
        self._clear_log()
        self._clear_preview()

        h_desc = f"{sheet_h}mm" if sheet_h else f"rola (max {max_sheet_length}mm)" if max_sheet_length else "rola"
        self._log(f"Nest: {len(self._files)} plik(ow)")
        self._log(f"  Arkusz: {sheet_w}x{h_desc}, ploter: {params['plotter']}")
        self._log(f"  Kopie: {copies}, gap: {gap_mm}mm")
        self._log(f"Output: {self._output_dir}\n")

        threading.Thread(target=self._worker_nest, args=(params,), daemon=True).start()

    def _worker_nest(self, params: dict):
        """Pełny pipeline: contour → bleed → nest → panelize → marks → export."""
        from modules.contour import detect_contour
        from modules.bleed import generate_bleed
        from modules.nesting import nest_job
        from modules.panelize import panelize_sheet
        from modules.marks import generate_marks
        from modules.export import export_sheet
        from models import Job

        out_dir = params["out_dir"]
        os.makedirs(out_dir, exist_ok=True)

        # Usuń stare pliki z poprzednich uruchomień
        import glob as _glob
        for old in _glob.glob(os.path.join(out_dir, "sheet_*_print.pdf")):
            try: os.remove(old)
            except OSError: pass
        for old in _glob.glob(os.path.join(out_dir, "sheet_*_cut.pdf")):
            try: os.remove(old)
            except OSError: pass

        bleed = params["bleed"]
        copies_override = params["copies"]
        plotter = params["plotter"]
        gap = params["gap"]
        white = params.get("white", False)
        # FlexCut dodawany ręcznie po nestingu — nie auto-panelize
        file_copies_dict = params.get("file_copies", {})

        t0 = time.time()

        # 1. Contour + Bleed
        sticker_copies_list: list[tuple] = []
        open_docs = []
        input_files = params["input_files"]

        total_files = len(input_files)
        for i, pdf in enumerate(input_files):
            # Progress update (thread-safe)
            self.after(0, lambda v=i/total_files, idx=i, tot=total_files:
                       (self._nest_progress_bar.set(v) if hasattr(self, '_nest_progress_bar') else None,
                        self._status_label.configure(text=f"Plik {idx+1}/{tot}...")))
            name = os.path.basename(pdf)
            file_copies = file_copies_dict.get(pdf, 1)
            copies = copies_override if copies_override > 1 else file_copies
            # Sprawdź czy plik to już gotowy output bleeda (prefiks "bleed_")
            is_bleed_output = name.startswith("bleed_")
            try:
                if is_bleed_output:
                    # Plik już ma bleed — odczytaj CutContour z PDF, pomiń detect_contour/generate_bleed
                    from models import Sticker
                    import re as _re
                    mm_to_pt = 72.0 / 25.4
                    pt_to_mm = 25.4 / 72.0
                    doc = fitz_module.open(pdf)
                    open_docs.append(doc)
                    page = doc[0]
                    pw_mm = page.rect.width * pt_to_mm   # pełna strona z bleedem (mm)
                    ph_mm = page.rect.height * pt_to_mm
                    b = bleed                              # bleed w mm
                    b_pt = b * mm_to_pt                   # bleed w pt
                    page_h_pt = page.rect.height           # out_h użyte przy tworzeniu bleed_ PDF
                    cw_pt = (pw_mm - 2 * b) * mm_to_pt   # sticker content width (pt)
                    ch_pt = (ph_mm - 2 * b) * mm_to_pt   # sticker content height (pt)

                    # --- Ekstrakcja CutContour z content streamów ---
                    cut_segs = None
                    contents_info = doc.xref_get_key(page.xref, "Contents")
                    xref_list = []
                    if contents_info[0] == "array":
                        xref_list = [int(x) for x in _re.findall(r'(\d+)\s+\d+\s+R', contents_info[1])]
                    elif contents_info[0] == "xref":
                        m = _re.search(r'(\d+)\s+\d+\s+R', contents_info[1])
                        if m:
                            xref_list = [int(m.group(1))]

                    for xref in xref_list:
                        try:
                            sd = doc.xref_stream(xref)
                            if sd and b"CutContour" in sd:
                                # Parsuj path operatory i odwróć transformację koordynatów
                                # Oryginalna transformacja: x_pdf = x_fitz + bleed_pts
                                #                           y_pdf = out_h - (y_fitz + bleed_pts)
                                # Odwrotna:   x_fitz = x_pdf - bleed_pts
                                #             y_fitz = out_h - y_pdf - bleed_pts
                                cut_segs = []
                                last_x, last_y = None, None
                                for line in sd.decode('latin-1', errors='replace').split('\n'):
                                    line = line.strip()
                                    if not line:
                                        continue
                                    parts = line.split()
                                    if len(parts) < 2:
                                        continue
                                    op = parts[-1]
                                    try:
                                        if op == 'm' and len(parts) >= 3:
                                            last_x = float(parts[-3])
                                            last_y = float(parts[-2])
                                        elif op == 'l' and len(parts) >= 3:
                                            ex, ey = float(parts[-3]), float(parts[-2])
                                            if last_x is not None:
                                                cut_segs.append(('l',
                                                    (last_x - b_pt, page_h_pt - last_y - b_pt),
                                                    (ex - b_pt, page_h_pt - ey - b_pt)))
                                            last_x, last_y = ex, ey
                                        elif op == 'c' and len(parts) >= 7:
                                            cx1, cy1 = float(parts[-7]), float(parts[-6])
                                            cx2, cy2 = float(parts[-5]), float(parts[-4])
                                            ex, ey = float(parts[-3]), float(parts[-2])
                                            if last_x is not None:
                                                cut_segs.append(('c',
                                                    (last_x - b_pt, page_h_pt - last_y - b_pt),
                                                    (cx1 - b_pt, page_h_pt - cy1 - b_pt),
                                                    (cx2 - b_pt, page_h_pt - cy2 - b_pt),
                                                    (ex - b_pt, page_h_pt - ey - b_pt)))
                                            last_x, last_y = ex, ey
                                    except (ValueError, IndexError):
                                        continue
                                if not cut_segs:
                                    cut_segs = None
                                else:
                                    self._log(f"    CutContour: {len(cut_segs)} segmentów odczytanych z PDF")
                                break
                        except Exception:
                            pass

                    # Fallback: prostokąt przy granicy naklejki
                    if cut_segs is None:
                        self._log(f"    CutContour: brak w PDF — używam prostokąta")
                        cut_segs = [
                            ('l', (0.0, 0.0), (cw_pt, 0.0)),
                            ('l', (cw_pt, 0.0), (cw_pt, ch_pt)),
                            ('l', (cw_pt, ch_pt), (0.0, ch_pt)),
                            ('l', (0.0, ch_pt), (0.0, 0.0)),
                        ]

                    # bleed_segments: pełny obszar naklejki+bleed (w pt, fitz y-down)
                    bleed_segs = [
                        ('l', (-b_pt, -b_pt), (cw_pt + b_pt, -b_pt)),
                        ('l', (cw_pt + b_pt, -b_pt), (cw_pt + b_pt, ch_pt + b_pt)),
                        ('l', (cw_pt + b_pt, ch_pt + b_pt), (-b_pt, ch_pt + b_pt)),
                        ('l', (-b_pt, ch_pt + b_pt), (-b_pt, -b_pt)),
                    ]
                    s = Sticker(
                        source_path=pdf,
                        page_index=0,
                        width_mm=pw_mm - 2 * b,   # content naklejki bez bleeda (do nestingu)
                        height_mm=ph_mm - 2 * b,
                        cut_segments=cut_segs,
                        bleed_segments=bleed_segs,
                        edge_color_rgb=(1.0, 1.0, 1.0),    # biały — bleed już w grafice
                        edge_color_cmyk=(0.0, 0.0, 0.0, 0.0),
                        pdf_doc=doc,
                        page_width_pt=cw_pt,               # content (bez bleeda) — dla math CutContour
                        page_height_pt=ch_pt,
                        is_bleed_output=True,               # nie rozszerzaj MediaBox, usuń CutContour
                    )
                    sticker_copies_list.append((s, copies))
                    self._log(f"  {name} (bleed PDF): {pw_mm - 2*b:.1f}x{ph_mm - 2*b:.1f}mm x{copies}")
                else:
                    page_stickers = detect_contour(pdf)
                    if page_stickers[0].pdf_doc is not None:
                        open_docs.append(page_stickers[0].pdf_doc)
                    multi = len(page_stickers) > 1
                    for s in page_stickers:
                        label = f"{name} p{s.page_index + 1}" if multi else name
                        try:
                            s = generate_bleed(s, bleed_mm=bleed)
                            sticker_copies_list.append((s, copies))
                            self._log(f"  {label}: {s.width_mm:.1f}x{s.height_mm:.1f}mm x{copies}")
                        except Exception as e:
                            self._log(f"  [ERR] {label}: {e}")
            except Exception as e:
                self._log(f"  [ERR] {name}: {e}")

        if not sticker_copies_list:
            self._log("\nBrak naklejek do nestowania.")
            for doc in open_docs:
                try: doc.close()
                except Exception: pass
            self.after(0, lambda: self._on_nest_done([]))
            return

        # 2. Nesting
        grouping_map = {"Grupuj": "group", "Osobne": "separate", "Mieszaj": "mix"}
        grouping_mode = grouping_map.get(params.get("grouping_mode", "Grupuj"), "group")
        self._log(f"\nNestowanie...")

        # mark_zone z konfiguracji plotera (JWEI=5mm, Summa=15mm)
        plotter_cfg = PLOTTERS.get(plotter, {})
        mark_zone = plotter_cfg.get("mark_zone_mm", DEFAULT_MARK_ZONE_MM)

        job = Job(stickers=sticker_copies_list, plotter=plotter)
        job = nest_job(
            job,
            sheet_width_mm=params["sheet_w"],
            sheet_height_mm=params["sheet_h"],
            gap_mm=gap,
            max_sheet_length_mm=params.get("max_sheet_length"),
            grouping_mode=grouping_mode,
            bleed_mm=bleed,
            mark_zone_mm=mark_zone,
        )

        # 3. Panelize + Marks + Export
        ns = len(job.sheets)
        sheet_pdf_paths: list[tuple[str, str]] = []

        for i, sheet in enumerate(job.sheets):
            sheet = panelize_sheet(sheet, flexcut=False)
            sheet = generate_marks(sheet, plotter=plotter)
            job.sheets[i] = sheet

            pp = os.path.join(out_dir, f"sheet_{i + 1}_print.pdf")
            cp = os.path.join(out_dir, f"sheet_{i + 1}_cut.pdf")
            export_sheet(sheet, pp, cp, bleed_mm=bleed, plotter=plotter, white=white)
            sheet_pdf_paths.append((pp, cp))

            pk = os.path.getsize(pp) / 1024
            ck = os.path.getsize(cp) / 1024
            fl = f", {len(sheet.panel_lines)} FlexCut" if sheet.panel_lines else ""
            self._log(f"  Arkusz {i + 1}: {len(sheet.placements)} naklejek{fl}, print={pk:.1f}KB, cut={ck:.1f}KB")

        # NIE zamykaj docs — potrzebne do FlexCut re-export (show_pdf_page)
        # Zamknięcie starych docs z poprzedniego uruchomienia
        old_docs = getattr(self, '_nest_open_docs', [])
        for doc in old_docs:
            try: doc.close()
            except Exception: pass
        self._nest_open_docs = open_docs

        elapsed = time.time() - t0
        total = sum(len(s.placements) for s in job.sheets)
        self._log(f"\nGotowe: {total} naklejek na {ns} arkusz(ach) ({elapsed:.1f}s)")

        # Zapisz stan do podglądu
        self._last_nest_job = job
        self._last_nest_bleed = bleed
        self._last_nest_pdfs = sheet_pdf_paths

        output_paths = [pp for pp, _ in sheet_pdf_paths]
        self.after(0, lambda: self._on_nest_done(output_paths))

    def _on_nest_done(self, output_paths: list[str]):
        self._processing = False
        if hasattr(self, '_nest_progress_bar'):
            self._nest_progress_bar.set(1.0)
            self._nest_progress_bar.pack_forget()
        if hasattr(self, '_nest_btn'):
            self._nest_btn.configure(state="normal", text="Generuj arkusze")
        self._status_label.configure(
            text=f"Gotowe — {len(output_paths)} arkusz(y)",
            text_color=SUCCESS if output_paths else ERROR,
        )
        # Podgląd arkuszy z renderem print+cut PDF
        job = getattr(self, '_last_nest_job', None)
        pdfs = getattr(self, '_last_nest_pdfs', [])
        bleed = getattr(self, '_last_nest_bleed', 2.0)
        if job and job.sheets:
            self.preview_panel.set_job(job, bleed, pdfs)

    # =========================================================================
    # FLEXCUT — zaznaczanie naklejek
    # =========================================================================

    def _open_flexcut_window(self):
        """Otwórz osobne okno FlexCut do zaznaczania naklejek."""
        job = getattr(self, '_last_nest_job', None)
        pdfs = getattr(self, '_last_nest_pdfs', [])
        bleed = getattr(self, '_last_nest_bleed', 2.0)
        if not job or not job.sheets:
            self._log("FlexCut: brak arkuszy — najpierw uruchom nest")
            return
        # Singleton — nie otwieraj wielu okien
        if hasattr(self, '_flexcut_window') and self._flexcut_window.winfo_exists():
            self._flexcut_window.focus()
            return
        self._flexcut_window = FlexCutWindow(
            parent=self,
            job=job,
            sheet_pdfs=pdfs,
            bleed_mm=bleed,
            on_reexport=self._reexport_sheet,
        )

    def _reexport_sheet(self, idx: int):
        """Re-export jednego arkusza po zmianach FlexCut."""
        job = getattr(self, '_last_nest_job', None)
        pdfs = getattr(self, '_last_nest_pdfs', [])
        bleed = getattr(self, '_last_nest_bleed', 2.0)
        if not job:
            self._log("  [ERR] FlexCut re-export: brak _last_nest_job")
            return
        if idx >= len(job.sheets):
            self._log(f"  [ERR] FlexCut re-export: idx={idx} >= sheets={len(job.sheets)}")
            return
        if idx >= len(pdfs):
            self._log(f"  [ERR] FlexCut re-export: idx={idx} >= pdfs={len(pdfs)}")
            return
        sheet = job.sheets[idx]
        self._log(f"FlexCut: re-export arkusz {idx + 1}, panel_lines={len(sheet.panel_lines)}, plotter={job.plotter}")
        try:
            from modules.marks import generate_marks
            from modules.export import export_sheet
            pp, cp = pdfs[idx]
            sheet = generate_marks(sheet, plotter=job.plotter)
            job.sheets[idx] = sheet
            white = self._nest_white_var.get() if hasattr(self, '_nest_white_var') else False
            export_sheet(sheet, pp, cp, bleed_mm=bleed, plotter=job.plotter, white=white)
            self._log(f"  OK: {os.path.basename(cp)} ({os.path.getsize(cp) / 1024:.1f}KB)")
            # Odśwież podgląd główny
            self.preview_panel.set_job(job, bleed, pdfs)
            self.preview_panel.current_sheet_idx = idx
            self.preview_panel._draw_current_sheet()
        except Exception as e:
            self._log(f"  [ERR] FlexCut re-export: {e}")
            import traceback
            traceback.print_exc()

    def _on_worker_done(self, output_paths: list[str]):
        self._processing = False
        if hasattr(self, '_progress_bar'):
            self._progress_bar.set(1.0)
            self._progress_bar.pack_forget()
        if hasattr(self, '_run_btn'):
            self._run_btn.configure(state="normal", text="Generuj bleed")
        self._status_label.configure(
            text=f"Gotowe — {len(output_paths)} plik(ow)",
            text_color=SUCCESS if output_paths else ERROR,
        )
        if output_paths:
            self._show_previews(output_paths)

    # =========================================================================
    # PREVIEW
    # =========================================================================

    def _clear_preview(self):
        self.preview_panel.clear()

    def _show_previews(self, paths: list[str]):
        """Pokaz podglad plikow bleed w panelu."""
        results = []
        for path in paths:
            try:
                doc = fitz_module.open(path)
                page = doc[0]
                w_mm = page.rect.width * 25.4 / 72.0
                h_mm = page.rect.height * 25.4 / 72.0
                doc.close()
                results.append({
                    "path": path,
                    "label": os.path.basename(path),
                    "size_mm": (w_mm, h_mm),
                })
            except Exception:
                pass
        if results:
            self.preview_panel.set_bleed_results(results)

    # =========================================================================
    # LOG
    # =========================================================================

    def _log(self, msg: str):
        self._log_buffer.append(msg)
        if not self._log_flush_scheduled:
            self._log_flush_scheduled = True
            self.after(50, self._flush_log)

    def _flush_log(self):
        self._log_flush_scheduled = False
        if not self._log_buffer:
            return
        text = "\n".join(self._log_buffer) + "\n"
        self._log_buffer.clear()
        self._log_text.configure(state="normal")
        self._log_text.insert("end", text)
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

    def _clear_log(self):
        self._log_buffer.clear()
        self._log_flush_scheduled = False
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.configure(state="disabled")


# =============================================================================
# MAIN
# =============================================================================

def _minimize_console():
    """Ukrywa okno konsoli (Windows)."""
    if platform.system() != "Windows":
        return
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


def main():
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    _minimize_console()

    app = BleedApp()
    app.mainloop()


if __name__ == "__main__":
    main()
