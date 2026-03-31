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
                 parent=None):
        super().__init__(parent)
        self._files = files
        self._output_dir = output_dir
        self._bleed_mm = bleed_mm
        self._black_100k = black_100k
        self._cutline_mode = cutline_mode
        self._target_height_mm = target_height_mm
        self._white = white

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

        for i, pdf in enumerate(self._files):
            self.progress.emit(i, total)
            name = os.path.basename(pdf)
            try:
                stickers = detect_contour(pdf)
                for sticker in stickers:
                    if self._target_height_mm:
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
                        f"  {name}: {sticker.width_mm:.1f}×{sticker.height_mm:.1f}mm → {sz:.0f}KB"
                    )
                    ok += 1
                    if sticker.pdf_doc is not None:
                        sticker.pdf_doc.close()
            except Exception as e:
                self.log_message.emit(f"  [ERR] {name}: {e}")
                err += 1

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
                cw_pt = (pw_mm - 2 * b) * MM_TO_PT
                ch_pt = (ph_mm - 2 * b) * MM_TO_PT

                # Prostokątny kontur (brak CutContour → brak linii cięcia)
                cut_segs = []
                bleed_segs = []

                s = Sticker(
                    source_path=pdf, page_index=0,
                    width_mm=pw_mm - 2 * b,
                    height_mm=ph_mm - 2 * b,
                    cut_segments=cut_segs,
                    bleed_segments=bleed_segs,
                    edge_color_rgb=(1.0, 1.0, 1.0),
                    edge_color_cmyk=(0.0, 0.0, 0.0, 0.0),
                    pdf_doc=doc,
                    page_width_pt=cw_pt,
                    page_height_pt=ch_pt,
                    is_bleed_output=is_bleed_output,
                )
                sticker_copies_list.append((s, copies))
                tag = "bleed" if is_bleed_output else "surowy"
                self.log_message.emit(f"  {name} ({tag}): {pw_mm:.1f}×{ph_mm:.1f}mm ×{copies}")
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
