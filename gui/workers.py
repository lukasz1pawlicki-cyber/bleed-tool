"""
Bleed Tool — workers.py
==========================
QThread workers z sygnałami: BleedWorker, NestWorker.
"""

import os
import time
import traceback
from PyQt6.QtCore import QThread, pyqtSignal

from config import DEFAULT_BLEED_MM, DEFAULT_MARK_ZONE_MM, PLOTTERS, MM_TO_PT, PT_TO_MM


class BleedWorker(QThread):
    """Wątek: contour → bleed → export dla listy plików."""

    progress = pyqtSignal(int, int)       # (current, total)
    log_message = pyqtSignal(str)         # linia logu
    finished = pyqtSignal(list)           # output_paths
    error = pyqtSignal(str)              # błąd krytyczny

    def __init__(self, files, output_dir, bleed_mm=2.0, black_100k=False,
                 cutline_mode="kiss-cut", target_height_mm=None, white=False,
                 crop_enabled=False, crop_shape="square", crop_offsets=None,
                 radius_pct=9, parent=None):
        super().__init__(parent)
        self._files = files
        self._output_dir = output_dir
        self._bleed_mm = bleed_mm
        self._black_100k = black_100k
        self._cutline_mode = cutline_mode
        self._target_height_mm = target_height_mm
        self._white = white
        self._crop_enabled = crop_enabled
        self._crop_shape = crop_shape
        self._crop_offsets = crop_offsets or {}
        self._radius_pct = radius_pct

    def run(self):
        try:
            self._run_inner()
        except Exception as e:
            self.error.emit(f"{e}\n{traceback.format_exc()}")

    def _run_inner(self):
        from modules.contour import detect_contour, scale_sticker
        from modules.bleed import generate_bleed
        from modules.export import export_single_sticker

        os.makedirs(self._output_dir, exist_ok=True)
        t0 = time.time()
        output_paths = []
        ok, err = 0, 0
        total = len(self._files)

        cutcontour = self._cutline_mode != "none"
        temp_files = []

        for i, pdf in enumerate(self._files):
            self.progress.emit(i, total)
            name = os.path.basename(pdf)
            try:
                actual_path = pdf
                # Crop: przyciej przed pipeline
                if self._crop_enabled and self._target_height_mm:
                    from modules.crop import apply_crop
                    offset = self._crop_offsets.get(pdf, (0.5, 0.5))
                    actual_path = apply_crop(
                        pdf, self._target_height_mm,
                        shape=self._crop_shape,
                        offset=offset,
                        radius_pct=self._radius_pct,
                    )
                    temp_files.append(actual_path)
                    self.log_message.emit(f"  Crop: {self._crop_shape}")

                stickers = detect_contour(actual_path)
                for sticker in stickers:
                    # Skalowanie do docelowej wysokosci (pomijane gdy crop)
                    if self._target_height_mm and not self._crop_enabled:
                        sticker = scale_sticker(sticker, self._target_height_mm)
                    sticker = generate_bleed(sticker, bleed_mm=self._bleed_mm)
                    stem = os.path.splitext(name)[0]
                    out = os.path.join(self._output_dir, f"bleed_{stem}.pdf")
                    export_single_sticker(
                        sticker, out, bleed_mm=self._bleed_mm,
                        black_100k=self._black_100k, cutcontour=cutcontour,
                        cutline_mode=self._cutline_mode, white=self._white,
                    )
                    output_paths.append(out)
                    sz = os.path.getsize(out) / 1024
                    self.log_message.emit(
                        f"  {name}: {sticker.width_mm:.1f}x{sticker.height_mm:.1f}mm -> {sz:.0f}KB"
                    )
                    ok += 1
                    if sticker.pdf_doc is not None:
                        sticker.pdf_doc.close()
            except Exception as e:
                self.log_message.emit(f"  [ERR] {name}: {e}")
                err += 1

        # Cleanup temp z crop
        for tmp in temp_files:
            try:
                os.unlink(tmp)
            except OSError:
                pass

        elapsed = time.time() - t0
        summary = f"\nGotowe: {ok} naklejek"
        if err:
            summary += f", {err} błędów"
        summary += f" ({elapsed:.1f}s)"
        self.log_message.emit(summary)
        self.progress.emit(total, total)
        self.finished.emit(output_paths)


class NestWorker(QThread):
    """Wątek: load → nest → marks → export sheet."""

    progress = pyqtSignal(int, int)
    log_message = pyqtSignal(str)
    finished = pyqtSignal(object, list)   # (job, sheet_pdf_paths)
    error = pyqtSignal(str)

    def __init__(self, files, file_copies, output_dir,
                 sheet_w, sheet_h=None, max_sheet_length=None,
                 copies=1, gap=0.0, plotter="jwei",
                 grouping_mode="group", white=False, parent=None):
        super().__init__(parent)
        self._files = files
        self._file_copies = file_copies
        self._output_dir = output_dir
        self._sheet_w = sheet_w
        self._sheet_h = sheet_h
        self._max_sheet_length = max_sheet_length
        self._copies = copies
        self._gap = gap
        self._plotter = plotter
        self._grouping_mode = grouping_mode
        self._white = white

    def run(self):
        try:
            self._run_inner()
        except Exception as e:
            self.error.emit(f"{e}\n{traceback.format_exc()}")

    def _run_inner(self):
        import fitz
        from models import Sticker, Job
        from modules.nesting import nest_job
        from modules.panelize import panelize_sheet
        from modules.marks import generate_marks
        from modules.export import export_sheet

        os.makedirs(self._output_dir, exist_ok=True)
        t0 = time.time()
        bleed = DEFAULT_BLEED_MM

        # 1. Load stickers
        sticker_copies_list = []
        open_docs = []
        total = len(self._files)

        for i, pdf in enumerate(self._files):
            self.progress.emit(i, total)
            name = os.path.basename(pdf)
            file_copies = self._file_copies.get(pdf, 1)
            copies = self._copies if self._copies > 1 else file_copies
            is_bleed_output = name.startswith("bleed_")
            try:
                doc = fitz.open(pdf)
                open_docs.append(doc)
                page = doc[0]
                pw_mm = page.rect.width * PT_TO_MM
                ph_mm = page.rect.height * PT_TO_MM
                b = 0  # Nest nie dodaje bleeda
                b_pt = b * MM_TO_PT
                page_h_pt = page.rect.height
                cw_pt = (pw_mm - 2 * b) * MM_TO_PT
                ch_pt = (ph_mm - 2 * b) * MM_TO_PT

                # Ekstrakcja CutContour z content streamów PDF
                cut_segs = None
                import re as _re
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
                        if sd and (b"CutContour" in sd or b"FlexCut" in sd):
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
                                self.log_message.emit(f"    Kontur cięcia: {len(cut_segs)} segmentów z PDF")
                            break
                    except Exception:
                        pass

                # Fallback: prostokątny kontur cięcia
                if cut_segs is None:
                    cut_segs = [
                        ('l', (0, 0), (cw_pt, 0)),
                        ('l', (cw_pt, 0), (cw_pt, ch_pt)),
                        ('l', (cw_pt, ch_pt), (0, ch_pt)),
                        ('l', (0, ch_pt), (0, 0)),
                    ]

                bleed_segs = []

                # Próbkuj kolor krawędzi z bleed output PDF (do anti-gap fill)
                from modules.contour import _sample_pdf_page_edge_color
                try:
                    edge_rgb = _sample_pdf_page_edge_color(doc, 0)
                except Exception:
                    edge_rgb = (1.0, 1.0, 1.0)

                s = Sticker(
                    source_path=pdf, page_index=0,
                    width_mm=pw_mm - 2 * b,
                    height_mm=ph_mm - 2 * b,
                    cut_segments=cut_segs,
                    bleed_segments=bleed_segs,
                    edge_color_rgb=edge_rgb,
                    edge_color_cmyk=(0.0, 0.0, 0.0, 0.0),
                    pdf_doc=doc,
                    page_width_pt=cw_pt,
                    page_height_pt=ch_pt,
                    is_bleed_output=is_bleed_output,
                )
                sticker_copies_list.append((s, copies))
                tag = "bleed" if is_bleed_output else "surowy"
                self.log_message.emit(f"  {name} ({tag}): {pw_mm:.1f}x{ph_mm:.1f}mm x{copies}")
            except Exception as e:
                self.log_message.emit(f"  [ERR] {name}: {e}")

        if not sticker_copies_list:
            self.log_message.emit("\nBrak naklejek do nestowania.")
            for doc in open_docs:
                try:
                    doc.close()
                except Exception:
                    pass
            self.finished.emit(Job(), [])
            return

        # 2. Nesting
        self.log_message.emit("\nNestowanie...")
        plotter_cfg = PLOTTERS.get(self._plotter, {})
        mark_zone = plotter_cfg.get("mark_zone_mm", DEFAULT_MARK_ZONE_MM)

        leading_offset = plotter_cfg.get("leading_offset_mm", 0)
        side_offset = plotter_cfg.get("side_offset_mm", 0)
        nest_w = self._sheet_w - 2 * side_offset
        nest_h = self._sheet_h

        nest_max_len = self._max_sheet_length
        if nest_max_len:
            nest_max_len = nest_max_len - leading_offset - side_offset

        job = Job(stickers=sticker_copies_list, plotter=self._plotter)
        job = nest_job(
            job,
            sheet_width_mm=nest_w,
            sheet_height_mm=nest_h,
            gap_mm=self._gap,
            max_sheet_length_mm=nest_max_len,
            grouping_mode=self._grouping_mode,
            bleed_mm=0,
            mark_zone_mm=mark_zone,
        )

        # Post-nesting offset
        if leading_offset > 0 or side_offset > 0:
            for sheet in job.sheets:
                for p in sheet.placements:
                    p.y_mm += leading_offset
                    p.x_mm += side_offset
                sheet.width_mm += 2 * side_offset
                sheet.height_mm += leading_offset + side_offset

        # 3. Export sheets
        ns = len(job.sheets)
        sheet_pdf_paths = []
        out_dir = self._output_dir

        for i, sheet in enumerate(job.sheets):
            self.progress.emit(total + i, total + ns)
            sheet = panelize_sheet(sheet, flexcut=False)
            sheet = generate_marks(sheet, plotter=self._plotter)
            job.sheets[i] = sheet

            pp = os.path.join(out_dir, f"sheet_{i + 1}_print.pdf")
            cp = os.path.join(out_dir, f"sheet_{i + 1}_cut.pdf")
            wp = os.path.join(out_dir, f"sheet_{i + 1}_white.pdf") if self._white else None
            export_sheet(sheet, pp, cp, bleed_mm=0, plotter=self._plotter,
                         white=self._white, white_output_path=wp)
            sheet_pdf_paths.append((pp, cp))

            pk = os.path.getsize(pp) / 1024
            ck = os.path.getsize(cp) / 1024
            fl = f", {len(sheet.panel_lines)} FlexCut" if sheet.panel_lines else ""
            self.log_message.emit(
                f"  Arkusz {i + 1}: {len(sheet.placements)} naklejek{fl}, "
                f"print={pk:.1f}KB, cut={ck:.1f}KB"
            )

        elapsed = time.time() - t0
        total_placed = sum(len(s.placements) for s in job.sheets)
        self.log_message.emit(
            f"\nGotowe: {total_placed} naklejek na {ns} arkusz(ach) ({elapsed:.1f}s)"
        )
        self.progress.emit(total + ns, total + ns)
        self.finished.emit(job, sheet_pdf_paths)
