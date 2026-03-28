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

from config import DEFAULT_BLEED_MM

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

# Obsługiwane formaty
_SUPPORTED_EXT = ('.pdf', '.svg', '.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp', '.webp')


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
        self.geometry("900x650")
        self.minsize(700, 500)

        # Stan
        self._files: list[str] = []
        self._output_dir: str = os.path.join(APP_DIR, "output")
        self._processing = False
        self._preview_images: list = []  # keep references to avoid GC

        # Crop
        self._crop_offsets: dict[str, tuple[float, float]] = {}
        self._crop_preview_file_idx: int = 0
        self._crop_canvas_img = None       # ImageTk.PhotoImage ref
        self._crop_src_img = None           # PIL source image cache
        self._crop_src_path: str | None = None  # cached source path
        self._drag_start: tuple[int, int] | None = None

        self._build_ui()
        self._setup_dnd()

    # =========================================================================
    # UI
    # =========================================================================

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # --- Header ---
        header = customtkinter.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=20, pady=(15, 5))
        header.grid_columnconfigure(1, weight=1)

        customtkinter.CTkLabel(
            header, text="Bleed Tool",
            font=customtkinter.CTkFont(size=22, weight="bold"),
        ).grid(row=0, column=0, sticky="w")

        customtkinter.CTkLabel(
            header, text="Generowanie bleed dla naklejek",
            text_color=TEXT_SECONDARY,
        ).grid(row=0, column=1, sticky="w", padx=(12, 0))

        # Theme toggle
        self._theme_var = customtkinter.StringVar(value="light")
        theme_btn = customtkinter.CTkSegmentedButton(
            header, values=["Light", "Dark"],
            command=self._on_theme_change,
            width=120,
        )
        theme_btn.set("Light")
        theme_btn.grid(row=0, column=2, sticky="e")

        # --- Main content ---
        main = customtkinter.CTkFrame(self, fg_color="transparent")
        main.grid(row=1, column=0, sticky="nsew", padx=20, pady=5)
        main.grid_columnconfigure(0, weight=1)
        main.grid_columnconfigure(1, weight=1)
        main.grid_rowconfigure(1, weight=1)

        # Left panel: files + settings
        left = customtkinter.CTkFrame(main, fg_color=CARD_BG, corner_radius=10)
        left.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(0, 5), pady=0)
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(1, weight=1)

        self._build_file_section(left)
        self._build_settings_section(left)

        # Right panel: preview + log
        right = customtkinter.CTkFrame(main, fg_color=CARD_BG, corner_radius=10)
        right.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=(5, 0), pady=0)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=0)

        self._build_preview_section(right)
        self._build_log_section(right)

        # --- Bottom bar ---
        bottom = customtkinter.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=2, column=0, sticky="ew", padx=20, pady=(5, 15))
        bottom.grid_columnconfigure(0, weight=1)

        self._status_label = customtkinter.CTkLabel(
            bottom, text="Gotowy", text_color=TEXT_SECONDARY,
        )
        self._status_label.grid(row=0, column=0, sticky="w")

        self._run_btn = customtkinter.CTkButton(
            bottom,
            text="Generuj bleed",
            font=customtkinter.CTkFont(size=14, weight="bold"),
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            height=40,
            width=180,
            command=self._on_run,
        )
        self._run_btn.grid(row=0, column=1, sticky="e")

    def _build_file_section(self, parent):
        """Drop zone + file list."""
        # Drop zone
        self._drop_frame = customtkinter.CTkFrame(
            parent, fg_color=DROP_ZONE_BG, corner_radius=8,
            border_width=2, border_color=DROP_ZONE_BORDER,
            height=80,
        )
        self._drop_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))
        self._drop_frame.grid_columnconfigure(0, weight=1)
        self._drop_frame.grid_propagate(False)

        if HAS_DND:
            drop_text = "Przeciagnij pliki PDF / SVG / PNG / JPG\nlub kliknij aby wybrac"
        else:
            drop_text = "Kliknij aby wybrac pliki\nPDF / SVG / PNG / JPG"

        drop_label = customtkinter.CTkLabel(
            self._drop_frame,
            text=drop_text,
            text_color=TEXT_SECONDARY,
            justify="center",
            cursor="hand2",
        )
        drop_label.grid(row=0, column=0, pady=15)
        drop_label.bind("<Button-1>", lambda e: self._browse_files())
        self._drop_frame.configure(cursor="hand2")
        self._drop_frame.bind("<Button-1>", lambda e: self._browse_files())

        # File list
        self._file_list = customtkinter.CTkScrollableFrame(
            parent, fg_color="transparent",
        )
        self._file_list.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 5))
        self._file_list.grid_columnconfigure(0, weight=1)

        count_row = customtkinter.CTkFrame(parent, fg_color="transparent")
        count_row.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 5))
        count_row.grid_columnconfigure(0, weight=1)

        self._file_count_label = customtkinter.CTkLabel(
            count_row, text="0 plikow", text_color=TEXT_SECONDARY,
            font=customtkinter.CTkFont(size=11),
        )
        self._file_count_label.grid(row=0, column=0, sticky="w", padx=(5, 0))

        self._clear_btn = customtkinter.CTkButton(
            count_row, text="Wyczysc", width=70, height=24,
            fg_color="transparent", hover_color=("#fee2e2", "#3d1111"),
            text_color=ERROR, border_width=1, border_color=ERROR,
            font=customtkinter.CTkFont(size=11),
            command=self._clear_files,
        )
        self._clear_btn.grid(row=0, column=1, sticky="e")

    def _build_settings_section(self, parent):
        """Bleed settings + output dir."""
        settings = customtkinter.CTkFrame(parent, fg_color="transparent")
        settings.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 10))
        settings.grid_columnconfigure(1, weight=1)

        # Bleed mm
        customtkinter.CTkLabel(settings, text="Bleed (mm):").grid(
            row=0, column=0, sticky="w", pady=3,
        )
        self._bleed_var = customtkinter.DoubleVar(value=DEFAULT_BLEED_MM)
        bleed_entry = customtkinter.CTkEntry(
            settings, textvariable=self._bleed_var, width=70,
        )
        bleed_entry.grid(row=0, column=1, sticky="w", padx=(8, 0), pady=3)

        # Wysokość docelowa (cm) — opcjonalna, skaluje proporcjonalnie
        customtkinter.CTkLabel(settings, text="Wysokość (cm):").grid(
            row=1, column=0, sticky="w", pady=3,
        )
        self._height_var = customtkinter.StringVar(value="")
        height_entry = customtkinter.CTkEntry(
            settings, textvariable=self._height_var, width=70,
            placeholder_text="auto",
        )
        height_entry.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=3)

        # Crop — przycinanie do rozmiaru
        self._crop_var = customtkinter.BooleanVar(value=False)
        self._crop_cb = customtkinter.CTkCheckBox(
            settings, text="Przytnij do rozmiaru",
            variable=self._crop_var,
            font=customtkinter.CTkFont(size=12),
            checkbox_width=18, checkbox_height=18,
            command=self._on_crop_changed,
        )
        self._crop_cb.grid(
            row=2, column=0, columnspan=2, sticky="w", pady=3,
        )
        self._crop_cb.configure(state="disabled")

        # Kształt crop
        self._crop_shape_var = customtkinter.StringVar(value="Kwadrat")
        self._crop_shape_btn = customtkinter.CTkSegmentedButton(
            settings, values=["Kwadrat", "Okrag"],
            variable=self._crop_shape_var,
            command=self._on_crop_shape_changed,
            width=140,
            font=customtkinter.CTkFont(size=11),
        )
        self._crop_shape_btn.grid(
            row=3, column=0, columnspan=2, sticky="w", padx=(26, 0), pady=(0, 3),
        )
        self._crop_shape_btn.grid_remove()  # ukryty domyślnie

        # Trace na wysokość — aktywuje/deaktywuje crop
        self._height_var.trace_add("write", self._on_height_changed)

        # Czarny 100% K
        self._black_100k_var = customtkinter.BooleanVar(value=False)
        self._black_100k_cb = customtkinter.CTkCheckBox(
            settings, text="Czarny 100% K",
            variable=self._black_100k_var,
            font=customtkinter.CTkFont(size=12),
            checkbox_width=18, checkbox_height=18,
        )
        self._black_100k_cb.grid(
            row=4, column=0, columnspan=2, sticky="w", pady=3,
        )
        self._black_100k_cb.configure(state="disabled")

        # Linia cięcia CutContour
        self._cutcontour_var = customtkinter.BooleanVar(value=True)
        self._cutcontour_cb = customtkinter.CTkCheckBox(
            settings, text="Linia cięcia (CutContour)",
            variable=self._cutcontour_var,
            font=customtkinter.CTkFont(size=12),
            checkbox_width=18, checkbox_height=18,
        )
        self._cutcontour_cb.grid(
            row=5, column=0, columnspan=2, sticky="w", pady=3,
        )

        # Output dir
        customtkinter.CTkLabel(settings, text="Output:").grid(
            row=6, column=0, sticky="w", pady=3,
        )
        out_frame = customtkinter.CTkFrame(settings, fg_color="transparent")
        out_frame.grid(row=6, column=1, sticky="ew", pady=3)
        out_frame.grid_columnconfigure(0, weight=1)

        self._output_var = customtkinter.StringVar(value=self._output_dir)
        customtkinter.CTkEntry(
            out_frame, textvariable=self._output_var,
        ).grid(row=0, column=0, sticky="ew", padx=(8, 4))

        customtkinter.CTkButton(
            out_frame, text="...", width=30,
            command=self._browse_output,
        ).grid(row=0, column=1)

    def _build_preview_section(self, parent):
        """PDF preview area + crop canvas."""
        self._preview_parent = parent

        preview_label = customtkinter.CTkLabel(
            parent, text="Podglad", font=customtkinter.CTkFont(weight="bold"),
        )
        preview_label.grid(row=0, column=0, sticky="nw", padx=10, pady=(10, 0))

        # Normalny podgląd (output po przetworzeniu)
        self._preview_frame = customtkinter.CTkScrollableFrame(
            parent, fg_color="transparent",
        )
        self._preview_frame.grid(row=0, column=0, sticky="nsew", padx=10, pady=(30, 5))
        self._preview_frame.grid_columnconfigure(0, weight=1)

        # Crop canvas (ukryty domyślnie)
        self._crop_container = customtkinter.CTkFrame(parent, fg_color="transparent")
        self._crop_container.grid(row=0, column=0, sticky="nsew", padx=10, pady=(30, 5))
        self._crop_container.grid_columnconfigure(0, weight=1)
        self._crop_container.grid_rowconfigure(1, weight=1)
        self._crop_container.grid_remove()

        # Nawigacja między plikami
        nav = customtkinter.CTkFrame(self._crop_container, fg_color="transparent")
        nav.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        nav.grid_columnconfigure(1, weight=1)

        self._crop_prev_btn = customtkinter.CTkButton(
            nav, text="<", width=30, command=self._crop_prev_file,
        )
        self._crop_prev_btn.grid(row=0, column=0, padx=(0, 5))

        self._crop_file_label = customtkinter.CTkLabel(
            nav, text="", font=customtkinter.CTkFont(size=12),
        )
        self._crop_file_label.grid(row=0, column=1)

        self._crop_next_btn = customtkinter.CTkButton(
            nav, text=">", width=30, command=self._crop_next_file,
        )
        self._crop_next_btn.grid(row=0, column=2, padx=(5, 0))

        # Canvas
        self._crop_canvas = tk.Canvas(
            self._crop_container, bg="#e0e0e0",
            highlightthickness=0, cursor="fleur",
        )
        self._crop_canvas.grid(row=1, column=0, sticky="nsew")
        self._crop_canvas.bind("<ButtonPress-1>", self._crop_on_press)
        self._crop_canvas.bind("<B1-Motion>", self._crop_on_drag)
        self._crop_canvas.bind("<ButtonRelease-1>", self._crop_on_release)
        self._crop_canvas.bind("<Configure>", self._crop_on_resize)

        self._preview_placeholder = customtkinter.CTkLabel(
            self._preview_frame,
            text="Podglad pojawi sie po przetworzeniu",
            text_color=TEXT_SECONDARY,
        )
        self._preview_placeholder.grid(row=0, column=0, pady=40)

    def _build_log_section(self, parent):
        """Log output."""
        self._log_text = customtkinter.CTkTextbox(
            parent, height=120, fg_color=LOG_BG, text_color=LOG_FG,
            font=customtkinter.CTkFont(family="Consolas", size=11),
            state="disabled",
        )
        self._log_text.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 10))

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
            self._crop_shape_btn.grid()
            self._show_crop_preview()
        else:
            self._crop_shape_btn.grid_remove()
            self._hide_crop_preview()

    def _on_crop_shape_changed(self, _value=None):
        """Callback: zmiana kształtu crop — odśwież podgląd."""
        if self._crop_var.get():
            self._redraw_crop_canvas()

    def _show_crop_preview(self):
        """Pokaż crop canvas, ukryj normalny podgląd."""
        if not self._files:
            return
        self._preview_frame.grid_remove()
        self._crop_container.grid()
        self._crop_preview_file_idx = 0
        self._crop_src_path = None  # force reload
        self._update_crop_preview()

    def _hide_crop_preview(self):
        """Ukryj crop canvas, pokaż normalny podgląd."""
        self._crop_container.grid_remove()
        self._preview_frame.grid()
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
        crop_shape = "circle" if self._crop_shape_var.get() == "Okrag" else "square"

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
        if crop_shape == "circle":
            canvas.create_oval(
                crop_x0, crop_y0, crop_x1, crop_y1,
                outline=ACCENT, width=2,
            )
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
        if not HAS_DND:
            return
        try:
            # Rejestruj DnD na drop zone i na całym oknie
            for w in (self._drop_frame, self):
                w.drop_target_register(tkinterdnd2.DND_FILES)
                w.dnd_bind("<<Drop>>", self._on_drop)
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
        for p in paths:
            p = os.path.normpath(p)
            if p.lower().endswith(_SUPPORTED_EXT) and p not in existing:
                self._files.append(p)
                existing.add(p)
        self._refresh_file_list()

    def _remove_file(self, path: str):
        if path in self._files:
            self._files.remove(path)
            self._refresh_file_list()

    def _clear_files(self):
        self._files.clear()
        self._refresh_file_list()

    def _refresh_file_list(self):
        for widget in self._file_list.winfo_children():
            widget.destroy()

        for i, path in enumerate(self._files):
            row = customtkinter.CTkFrame(self._file_list, fg_color="transparent")
            row.grid(row=i, column=0, sticky="ew", pady=1)
            row.grid_columnconfigure(0, weight=1)

            name = os.path.basename(path)
            customtkinter.CTkLabel(
                row, text=name, anchor="w",
                font=customtkinter.CTkFont(size=12),
            ).grid(row=0, column=0, sticky="w")

            customtkinter.CTkButton(
                row, text="x", width=24, height=24,
                fg_color="transparent", hover_color=("#fee2e2", "#3d1111"),
                text_color=ERROR,
                command=lambda p=path: self._remove_file(p),
            ).grid(row=0, column=1, padx=(4, 0))

        count = len(self._files)
        self._file_count_label.configure(
            text=f"{count} plik(ow)" if count != 1 else "1 plik"
        )

        # Czarny 100% K: aktywny tylko gdy jest PDF wektorowy
        has_pdf = any(p.lower().endswith(('.pdf', '.svg')) for p in self._files)
        self._black_100k_cb.configure(state="normal" if has_pdf else "disabled")
        if not has_pdf:
            self._black_100k_var.set(False)

        # Odśwież crop preview jeśli aktywny
        if self._crop_var.get() and self._files:
            self._crop_preview_file_idx = min(
                self._crop_preview_file_idx, len(self._files) - 1
            )
            self._crop_src_path = None
            self._show_crop_preview()
        elif self._crop_var.get() and not self._files:
            self._hide_crop_preview()

    def _browse_output(self):
        d = filedialog.askdirectory(title="Wybierz folder wyjsciowy")
        if d:
            self._output_dir = d
            self._output_var.set(d)

    # =========================================================================
    # THEME
    # =========================================================================

    def _on_theme_change(self, value):
        mode = "dark" if value == "Dark" else "light"
        customtkinter.set_appearance_mode(mode)

    # =========================================================================
    # PROCESSING
    # =========================================================================

    def _on_run(self):
        if self._processing:
            return
        if not self._files:
            messagebox.showwarning("Bleed Tool", "Brak plikow do przetworzenia.\nPrzeciagnij lub wybierz pliki.")
            return

        self._output_dir = self._output_var.get()
        bleed_mm = self._bleed_var.get()
        black_100k = self._black_100k_var.get()
        cutcontour = self._cutcontour_var.get()

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
        crop_shape = "circle" if self._crop_shape_var.get() == "Okrag" else "square"
        crop_offsets = dict(self._crop_offsets) if crop_enabled else {}

        self._processing = True
        self._run_btn.configure(state="disabled", text="Przetwarzam...")
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
        self._log(f"Output: {self._output_dir}\n")

        thread = threading.Thread(
            target=self._worker,
            args=(list(self._files), self._output_dir, bleed_mm, black_100k,
                  cutcontour, target_height_mm, crop_enabled, crop_shape,
                  crop_offsets),
            daemon=True,
        )
        thread.start()

    def _worker(self, files: list[str], output_dir: str, bleed_mm: float,
                black_100k: bool = False, cutcontour: bool = True,
                target_height_mm: float | None = None,
                crop_enabled: bool = False, crop_shape: str = "square",
                crop_offsets: dict | None = None):
        from modules.contour import detect_contour, scale_sticker
        from modules.bleed import generate_bleed
        from modules.export import export_single_sticker

        os.makedirs(output_dir, exist_ok=True)
        crop_offsets = crop_offsets or {}
        temp_files: list[str] = []

        t0 = time.time()
        ok, err = 0, 0
        output_paths = []

        for filepath in files:
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

    def _on_worker_done(self, output_paths: list[str]):
        self._processing = False
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
        self._preview_images.clear()
        for widget in self._preview_frame.winfo_children():
            widget.destroy()

    def _show_previews(self, paths: list[str]):
        self._clear_preview()
        max_previews = 8

        for i, path in enumerate(paths[:max_previews]):
            try:
                doc = fitz_module.open(path)
                page = doc[0]
                # Renderuj na ~250px szerokości
                zoom = 250.0 / page.rect.width
                mat = fitz_module.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                doc.close()

                ctk_img = customtkinter.CTkImage(
                    light_image=img, dark_image=img,
                    size=(img.width, img.height),
                )
                self._preview_images.append(ctk_img)

                name = os.path.basename(path)
                frame = customtkinter.CTkFrame(self._preview_frame, fg_color="transparent")
                frame.grid(row=i * 2, column=0, sticky="ew", pady=(5, 0))

                customtkinter.CTkLabel(
                    frame, text=name,
                    font=customtkinter.CTkFont(size=11, weight="bold"),
                ).pack(anchor="w")

                customtkinter.CTkLabel(
                    self._preview_frame, image=ctk_img, text="",
                ).grid(row=i * 2 + 1, column=0, sticky="w", pady=(0, 5))

            except Exception as e:
                customtkinter.CTkLabel(
                    self._preview_frame,
                    text=f"Blad podgladu: {os.path.basename(path)}",
                    text_color=ERROR,
                ).grid(row=i * 2, column=0, sticky="w", pady=2)

        if len(paths) > max_previews:
            customtkinter.CTkLabel(
                self._preview_frame,
                text=f"...i {len(paths) - max_previews} wiecej",
                text_color=TEXT_SECONDARY,
            ).grid(row=max_previews * 2, column=0, sticky="w", pady=5)

    # =========================================================================
    # LOG
    # =========================================================================

    def _log(self, msg: str):
        def _append():
            self._log_text.configure(state="normal")
            self._log_text.insert("end", msg + "\n")
            self._log_text.see("end")
            self._log_text.configure(state="disabled")
        self.after(0, _append)

    def _clear_log(self):
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
