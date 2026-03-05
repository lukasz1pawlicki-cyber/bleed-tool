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

        self.title("Bleed Tool")
        self.geometry("900x650")
        self.minsize(700, 500)

        # Stan
        self._files: list[str] = []
        self._output_dir: str = os.path.join(APP_DIR, "output")
        self._processing = False
        self._preview_images: list = []  # keep references to avoid GC

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

        drop_label = customtkinter.CTkLabel(
            self._drop_frame,
            text="Przeciagnij pliki PDF / SVG / PNG / JPG\nlub kliknij aby wybrac",
            text_color=TEXT_SECONDARY,
            justify="center",
        )
        drop_label.grid(row=0, column=0, pady=15)
        drop_label.bind("<Button-1>", lambda e: self._browse_files())
        self._drop_frame.bind("<Button-1>", lambda e: self._browse_files())

        # File list
        self._file_list = customtkinter.CTkScrollableFrame(
            parent, fg_color="transparent",
        )
        self._file_list.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 5))
        self._file_list.grid_columnconfigure(0, weight=1)

        self._file_count_label = customtkinter.CTkLabel(
            parent, text="0 plikow", text_color=TEXT_SECONDARY,
            font=customtkinter.CTkFont(size=11),
        )
        self._file_count_label.grid(row=2, column=0, sticky="w", padx=15, pady=(0, 5))

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

        # Output dir
        customtkinter.CTkLabel(settings, text="Output:").grid(
            row=1, column=0, sticky="w", pady=3,
        )
        out_frame = customtkinter.CTkFrame(settings, fg_color="transparent")
        out_frame.grid(row=1, column=1, sticky="ew", pady=3)
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
        """PDF preview area."""
        preview_label = customtkinter.CTkLabel(
            parent, text="Podglad", font=customtkinter.CTkFont(weight="bold"),
        )
        preview_label.grid(row=0, column=0, sticky="nw", padx=10, pady=(10, 0))

        self._preview_frame = customtkinter.CTkScrollableFrame(
            parent, fg_color="transparent",
        )
        self._preview_frame.grid(row=0, column=0, sticky="nsew", padx=10, pady=(30, 5))
        self._preview_frame.grid_columnconfigure(0, weight=1)

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
    # DRAG & DROP
    # =========================================================================

    def _setup_dnd(self):
        if not HAS_DND:
            return
        try:
            self.drop_target_register(tkinterdnd2.DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop)
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

        self._processing = True
        self._run_btn.configure(state="disabled", text="Przetwarzam...")
        self._clear_log()
        self._clear_preview()
        self._log(f"Start: {len(self._files)} plik(ow), bleed={bleed_mm}mm")
        self._log(f"Output: {self._output_dir}\n")

        thread = threading.Thread(
            target=self._worker, args=(list(self._files), self._output_dir, bleed_mm),
            daemon=True,
        )
        thread.start()

    def _worker(self, files: list[str], output_dir: str, bleed_mm: float):
        from modules.contour import detect_contour
        from modules.bleed import generate_bleed
        from modules.export import export_single_sticker

        os.makedirs(output_dir, exist_ok=True)

        t0 = time.time()
        ok, err = 0, 0
        output_paths = []

        for filepath in files:
            name = os.path.splitext(os.path.basename(filepath))[0]

            try:
                stickers = detect_contour(filepath)
                multi = len(stickers) > 1

                for sticker in stickers:
                    if multi:
                        out = os.path.join(output_dir, f"bleed_{name}_p{sticker.page_index + 1}.pdf")
                        label = f"{name} p{sticker.page_index + 1}"
                    else:
                        out = os.path.join(output_dir, f"bleed_{name}.pdf")
                        label = name

                    try:
                        sticker = generate_bleed(sticker, bleed_mm=bleed_mm)
                        info = export_single_sticker(sticker, out, bleed_mm=bleed_mm)

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
    """Minimalizuje okno konsoli (Windows)."""
    if platform.system() != "Windows":
        return
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
    except Exception:
        pass


def main():
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    _minimize_console()

    if HAS_DND:
        try:
            app = BleedApp()
            # Patch: re-create with tkinterdnd2 support
            app.destroy()
            root = tkinterdnd2.Tk()
            root.withdraw()
            root.destroy()
        except Exception:
            pass

    app = BleedApp()
    app.mainloop()


if __name__ == "__main__":
    main()
