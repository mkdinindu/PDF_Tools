#!/usr/bin/env python3
"""PDF Tools - desktop interface.

Run with ``python3 PDF_Tools.py --gui`` (or ``python3 gui.py``).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from functools import partial
from pathlib import Path

from PyQt6.QtCore import (
    QObject,
    QRunnable,
    QSettings,
    QSize,
    Qt,
    QThreadPool,
    pyqtSignal,
)
from PyQt6.QtGui import QKeySequence, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

import pdf_core as core
import theme as T
from widgets import (
    Badge,
    Button,
    Card,
    DropZone,
    FieldRow,
    PageEntry,
    PageGrid,
    ResultCard,
    SectionHeader,
    SegmentedControl,
    ToastHost,
    ToggleSwitch,
    divider,
    ghost_button,
    icon_button,
    label,
    primary_button,
    soft_button,
    FileList,
    ProgressStrip,
    _IconBadge,
)

APP_NAME = "PDF Tools"


# ---------------------------------------------------------------- workers
class JobSignals(QObject):
    progress = pyqtSignal(int, str)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)


class Job(QRunnable):
    """Runs a pdf_core call off the UI thread."""

    def __init__(self, func, *args, **kwargs):
        super().__init__()
        self.signals = JobSignals()
        self._func = func
        self._args = args
        self._kwargs = kwargs

    def run(self):
        try:
            result = self._func(*self._args, progress=self.signals.progress.emit, **self._kwargs)
        except core.PdfToolError as exc:
            self.signals.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            self.signals.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            self.signals.finished.emit(result)


class ScanSignals(QObject):
    done = pyqtSignal(list)
    failed = pyqtSignal(str)


class Scan(QRunnable):
    """Reads metadata for a batch of files off the UI thread."""

    def __init__(self, paths):
        super().__init__()
        self.signals = ScanSignals()
        self._paths = paths

    def run(self):
        try:
            self.signals.done.emit(core.inspect_files(self._paths))
        except Exception as exc:  # noqa: BLE001
            self.signals.failed.emit(str(exc))


class ThumbSignals(QObject):
    ready = pyqtSignal(int, object)   # page index, QPixmap


class Thumbnailer(QRunnable):
    """Renders one page to a pixmap with Ghostscript, cached on disk."""

    _cache: dict[tuple, str] = {}

    def __init__(self, path: str, index: int, width: int, signals: ThumbSignals,
                 generation: int):
        super().__init__()
        self.path = path
        self.index = index
        self.width = width
        self.signals = signals
        self.generation = generation

    @staticmethod
    def _key(path: str, index: int, width: int, mtime: float, size: int) -> tuple:
        return (path, index, width, mtime, size)

    def run(self):
        try:
            stat = os.stat(self.path)
        except OSError:
            return
        key = self._key(self.path, self.index, self.width, stat.st_mtime, stat.st_size)
        cache_dir = Path(tempfile.gettempdir()) / "pdf-tools-thumbs"
        cache_file = cache_dir / (f"{abs(hash(key))}.png")
        if cache_file.exists():
            try:
                pixmap = QPixmap(str(cache_file))
                if not pixmap.isNull():
                    self.signals.ready.emit(self.index, pixmap)
                    return
            except Exception:  # noqa: BLE001
                pass

        gs = core.ghostscript_path()
        if not gs:
            self.signals.ready.emit(self.index, None)
            return
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            args = [
                gs, "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-dQUIET",
                "-dTextAlphaBits=4", "-dGraphicsAlphaBits=4",
                "-dInterpolateControl=-1", "-dAutoRotatePages=/None",
                "-dFirstPage=%d" % (self.index + 1), "-dLastPage=%d" % (self.index + 1),
                "-sDEVICE=png16m", "-r96",
                "-g1000x1400", "-dPDFFitPage",
                f"-sOutputFile={cache_file}", self.path,
            ]
            subprocess.run(args, capture_output=True, timeout=40)
        except (subprocess.TimeoutExpired, OSError):
            pass
        pixmap = QPixmap(str(cache_file)) if cache_file.exists() else None
        if pixmap is not None and pixmap.isNull():
            pixmap = None
        self.signals.ready.emit(self.index, pixmap)


# ---------------------------------------------------------------- panels
class ToolPanel(QWidget):
    """Base class for each tool's workspace."""

    icon = "file"
    title = "Tool"
    subtitle = ""
    action_label = "Run"
    action_icon = "sparkle"
    accept_multiple = False
    accept_folders = False

    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.window = window
        self.paths: list[str] = []
        self._scan: Scan | None = None
        self._jobs: list[Job] = []
        self.setAcceptDrops(True)

    # -- helpers used by subclasses -------------------------------------
    def busy(self, value: bool) -> None:
        self.window.set_busy(value)
        for child in self.findChildren(Button):
            if child.objectName() in ("Primary", "Accent", "Danger", "IconButton"):
                continue
            child.setEnabled(not value)

    def run_job(self, func, *args, title: str = "", on_done=None, **kwargs) -> None:
        if self.window.busy_flag:
            return
        job = Job(func, *args, **kwargs)
        self._jobs.append(job)
        self.busy(True)
        self.window.progress.start(title or "Working")
        job.signals.progress.connect(self.window.progress.update_progress)

        def ok(result):
            self.busy(False)
            self.window.progress.finish("Done", True)
            self.window.toasts.notify(f"{title or 'Finished'}", result.detail or "", "success")
            if on_done:
                on_done(result)

        def bad(message):
            self.busy(False)
            self.window.progress.finish("Failed", False)
            self.window.toasts.notify("Could not finish", message, "error", 6500)

        job.signals.finished.connect(ok)
        job.signals.failed.connect(bad)
        QThreadPool.globalInstance().start(job)

    def scan_files(self, paths, on_done=None) -> None:
        unique: list[str] = []
        seen = set()
        for p in paths:
            text = str(p)
            if text in seen:
                continue
            seen.add(text)
            unique.append(text)
        if not unique:
            return
        self._scan = Scan(unique)
        self.window.progress.start(f"Reading {len(unique)} file(s)")

        def ok(infos):
            self.window.progress.finish("Ready", True)
            if on_done:
                on_done(infos)

        def bad(message):
            self.window.progress.finish("Failed", False)
            self.window.toasts.notify("Could not read files", message, "error")

        self._scan.signals.done.connect(ok)
        self._scan.signals.failed.connect(bad)
        QThreadPool.globalInstance().start(self._scan)

    def load_thumbs(self, grid: PageGrid, path: str, count: int) -> None:
        signals = ThumbSignals()
        signals.ready.connect(partial(self._apply_thumb, grid, path, count))
        pool = QThreadPool.globalInstance()
        for i in range(count):
            pool.start(Thumbnailer(path, i, grid.card_width, signals, id(grid)))

    def _apply_thumb(self, grid: PageGrid, path: str, count: int, index: int, pixmap) -> None:
        if self.paths and self.paths[0] != path:
            return
        entries = grid.entries()
        if index < len(entries) and pixmap is not None:
            entries[index].thumb = pixmap
            item = grid.item(index)
            if item is not None:
                grid.viewport().update(grid.visualItemRect(item))
        remaining = sum(1 for e in entries if e.thumb is None)
        if remaining == 0:
            self.window.progress.finish("Ready", True)

    # -- drag & drop ----------------------------------------------------
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.accept_paths(paths)

    def accept_paths(self, paths) -> None:  # pragma: no cover - overridden
        pass


class MergePanel(ToolPanel):
    icon = "merge"
    title = "Merge PDFs"
    subtitle = "Combine lecture notes, chapters or scans into one document."
    action_label = "Merge documents"
    action_icon = "merge"
    accept_multiple = True

    def __init__(self, window):
        super().__init__(window)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        # drop zone
        self.drop = DropZone("Drop PDFs here", "or click to browse  ·  files merge top to bottom",
                             "merge")
        self.drop.clicked.connect(self._browse)
        self.drop.files_dropped.connect(self.accept_paths)
        layout.addWidget(self.drop)

        # queue card
        self.queue_card = Card(pad=18)
        header = SectionHeader("Merge order",
                               "Reorder with the arrows — the first file starts the document.")
        clear = ghost_button("Clear all", "close", self._clear)
        header.add_action(clear)
        self.queue_card.add(header)
        self.summary = label("", 12, 500, T.TEXT_MUTED)
        self.queue_card.add(self.summary)
        self.files = FileList(on_change=self._sync)
        self.queue_card.add(self.files, 1)
        layout.addWidget(self.queue_card, 1)

        # output
        output_card = Card(pad=18)
        output_card.add(SectionHeader("Output", "Where the combined document should be written."))
        self.output = FieldRow("File name", "merged.pdf", "merged.pdf")
        self.output.browse.clicked.connect(self._choose_output)
        output_card.add(self.output)
        layout.addWidget(output_card)

        # action bar
        self.action = primary_button(self.action_label, self.action_icon, self._run)
        self.progress_strip = ResultCard()
        self.progress = self.window.progress
        bar = QWidget()
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(0, 0, 0, 0)
        bar_layout.setSpacing(10)
        bar_layout.addWidget(self.action)
        bar_layout.addStretch(1)
        bar_layout.addWidget(self.progress_strip)
        layout.addWidget(self.progress_strip)
        layout.addWidget(bar)
        self._sync()

    def _browse(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select PDFs to merge", self.window.last_dir(), "PDF files (*.pdf)")
        if paths:
            self.accept_paths(paths)

    def _choose_output(self):
        suggested = self.output.text() or "merged.pdf"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save merged PDF", str(Path(self.window.last_dir()) / suggested),
            "PDF files (*.pdf)")
        if path:
            self.output.set_text(path)

    def accept_paths(self, paths):
        paths = [p for p in paths if str(p).lower().endswith(".pdf")]
        if not paths:
            self.window.toasts.notify("Only PDFs", "That file is not a .pdf", "warning")
            return
        self.scan_files(paths, self._on_scanned)

    def _on_scanned(self, infos):
        existing = {str(i.path) for i in self.files.infos()}
        fresh = [i for i in infos if str(i.path) not in existing]
        self.files.add_items(fresh)
        bad = [i for i in infos if i.error]
        if bad:
            self.window.toasts.notify(
                f"Skipped {len(bad)} file(s)", f"{bad[0].name}: {bad[0].error}", "warning", 6000)
        if fresh and not self.output.text().endswith(".pdf"):
            self.output.set_text("merged.pdf")

    def _clear(self):
        self.files.clear()
        self.progress_strip.hide_result()
        self._sync()

    def _sync(self):
        items = self.files.infos()
        usable = [i for i in items if i.ok]
        total_pages = sum(i.pages for i in usable)
        if not items:
            self.summary.setText("No files queued yet.")
        elif len(usable) < 2:
            self.summary.setText(f"{len(usable)} file queued — add one more to merge.")
        else:
            self.summary.setText(
                f"{len(usable)} files  ·  {total_pages} pages  ·  "
                f"{core.human_size(sum(i.size for i in usable))}")
        self.action.setEnabled(len(usable) >= 2)
        self.drop.set_active(not items)

    def _run(self):
        usable = [i for i in self.files.infos() if i.ok]
        if len(usable) < 2:
            self.window.toasts.notify("Need more files", "Queue at least two readable PDFs",
                                      "warning")
            return
        target = self.output.text() or "merged.pdf"
        if Path(target).name.lower() in {p.name.lower() for p in usable}:
            self.window.toasts.notify(
                "Pick a new name", "The output would overwrite one of the inputs", "warning")
            return
        self.progress_strip.hide_result()
        self.run_job(core.merge_pdfs, [str(i.path) for i in usable], target,
                     title="Merged", on_done=self._on_done)

    def _on_done(self, result):
        self.progress_strip.show_result(
            f"Created {result.output.name}", result.detail, result.size_text, result.saved,
            result.output)


class SplitPanel(ToolPanel):
    icon = "split"
    title = "Split PDF"
    subtitle = "Pull out a page range, or break a document into chunks."
    action_label = "Split document"
    action_icon = "split"

    def __init__(self, window):
        super().__init__(window)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        self.drop = DropZone("Drop a PDF to split", "or click to choose one", "split")
        self.drop.clicked.connect(self._browse)
        self.drop.files_dropped.connect(self.accept_paths)
        layout.addWidget(self.drop)

        self.info = Card(pad=18)
        self.info_header = SectionHeader("How should it be split?")
        self.info.add(self.info_header)

        self.modes = SegmentedControl([("range", "Page range"), ("each", "Every N pages")])
        self.modes.changed.connect(self._on_mode)
        self.info.add(self.modes)

        self.range_row = QWidget()
        range_layout = QHBoxLayout(self.range_row)
        range_layout.setContentsMargins(0, 0, 0, 0)
        range_layout.setSpacing(10)
        self.range_field = FieldRow("Pages to keep", "e.g. 1-3, 7, 12-", "")
        range_layout.addWidget(self.range_field, 1)
        self.range_hint = label("", 12, 500, T.TEXT_MUTED)
        self.range_hint.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        range_layout.addWidget(self.range_hint)
        self.info.add(self.range_row)

        self.each_row = QWidget()
        each_layout = QHBoxLayout(self.each_row)
        each_layout.setContentsMargins(0, 0, 0, 0)
        each_layout.setSpacing(10)
        self.every = QLineEdit("1")
        self.every.setFixedWidth(90)
        self.every.setAlignment(Qt.AlignmentFlag.AlignCenter)
        every_caption = label("Pages per file", 12, 600, T.TEXT_MUTED, "FieldLabel")
        each_layout.addWidget(every_caption)
        each_layout.addWidget(self.every)
        each_layout.addWidget(label("1 = one file per page", 12, 400, T.TEXT_SUBTLE))
        each_layout.addStretch(1)
        self.each_row.setVisible(False)
        self.info.add(self.each_row)

        self.page_buttons = QWidget()
        pb_layout = QHBoxLayout(self.page_buttons)
        pb_layout.setContentsMargins(0, 0, 0, 0)
        pb_layout.setSpacing(7)
        pb_layout.addWidget(label("Quick pick:", 12, 600, T.TEXT_MUTED, "FieldLabel"))
        self._quick: list[Button] = []
        for text in ("All", "First half", "Odd pages", "Even pages"):
            btn = soft_button(text, on_click=lambda _=False, t=text: self._quick_pick(t))
            self._quick.append(btn)
            pb_layout.addWidget(btn)
        pb_layout.addStretch(1)
        self.info.add(self.page_buttons)
        layout.addWidget(self.info)

        output_card = Card(pad=18)
        output_card.add(SectionHeader("Output", "A file for ranges, a folder for chunks."))
        self.output = FieldRow("File name or folder", "notes_split.pdf", "notes_split.pdf")
        self.output.browse.clicked.connect(self._choose_output)
        output_card.add(self.output)
        layout.addWidget(output_card)

        self.result = ResultCard()
        self.action = primary_button(self.action_label, self.action_icon, self._run)
        layout.addWidget(self.result)
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.addWidget(self.action)
        bar.addStretch(1)
        layout.addLayout(bar)
        self._sync()

    def _on_mode(self, key):
        is_range = key == "range"
        self.range_row.setVisible(is_range)
        self.page_buttons.setVisible(is_range)
        self.each_row.setVisible(not is_range)
        self._sync()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a PDF", self.window.last_dir(), "PDF files (*.pdf)")
        if path:
            self.accept_paths([path])

    def _choose_output(self):
        if self.modes.value() == "each":
            path = QFileDialog.getExistingDirectory(
                self, "Choose a folder for the split files", self.window.last_dir())
            if path:
                self.output.set_text(path)
        else:
            suggested = self.output.text() or "split.pdf"
            path, _ = QFileDialog.getSaveFileName(
                self, "Save extracted pages", str(Path(self.window.last_dir()) / suggested),
                "PDF files (*.pdf)")
            if path:
                self.output.set_text(path)

    def accept_paths(self, paths):
        pdfs = [p for p in paths if str(p).lower().endswith(".pdf")]
        if not pdfs:
            self.window.toasts.notify("Only PDFs", "That file is not a .pdf", "warning")
            return
        self.scan_files(pdfs[:1], self._on_scanned)

    def _on_scanned(self, infos):
        info = infos[0]
        if info.error:
            self.window.toasts.notify("Cannot open it", info.error, "error")
            self.paths = []
            self._sync()
            return
        self.paths = [str(info.path)]
        self.source = info
        self.drop.set_state(info.name, f"{info.pages} pages  ·  {info.size_text}", "split")
        self._sync()

    def _sync(self):
        has = bool(self.paths)
        self.action.setEnabled(has)
        self.drop.set_active(not has)
        self.page_buttons.setEnabled(has)
        self.modes.setEnabled(has)
        self.range_field.setEnabled(has)
        self.every.setEnabled(has)
        if has:
            total = self.source.pages
            if not self.range_field.text():
                self.range_field.set_text(f"1-{total}")
            self.range_hint.setText(f"of {total}")

    def _quick_pick(self, which):
        if not self.paths:
            return
        total = self.source.pages
        if which == "All":
            spec = f"1-{total}"
        elif which == "First half":
            spec = f"1-{max(1, total // 2)}"
        elif which == "Odd pages":
            spec = ",".join(str(n) for n in range(1, total + 1, 2))
        else:
            spec = ",".join(str(n) for n in range(2, total + 1, 2))
        self.range_field.set_text(spec)

    def _run(self):
        if not self.paths:
            return
        mode = self.modes.value()
        every = 1
        if mode == "range":
            spec = self.range_field.text()
            if not spec:
                self.window.toasts.notify("Enter a range", "e.g. 1-3, 7", "warning")
                return
        else:
            try:
                every = int(self.every.text() or "1")
            except ValueError:
                self.window.toasts.notify("Pages per file", "Enter a whole number", "warning")
                return
            if every < 1:
                self.window.toasts.notify("Pages per file", "Must be 1 or more", "warning")
                return
            spec = ""
        self.result.hide_result()
        self.run_job(core.split_pdf, self.paths[0], self.output.text(),
                     mode=mode, spec=spec, every=every,
                     title="Split", on_done=self._on_done)

    def _on_done(self, result):
        if result.files:
            self.result.show_result(
                f"{len(result.files)} files written", result.detail, result.size_text, 0,
                result.files[0])
        else:
            self.result.show_result(f"Created {result.output.name}", result.detail,
                                    result.size_text, result.saved, result.output)


class CompressPanel(ToolPanel):
    icon = "compress"
    title = "Compress PDF"
    subtitle = "Shrink file size for email, uploads or LMS submission limits."
    action_label = "Compress"
    action_icon = "compress"

    def __init__(self, window):
        super().__init__(window)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        self.drop = DropZone("Drop a PDF to compress", "or click to choose one", "compress")
        self.drop.clicked.connect(self._browse)
        self.drop.files_dropped.connect(self.accept_paths)
        layout.addWidget(self.drop)

        self.card = Card(pad=18)
        self.card.add(SectionHeader(
            "Compression level",
            core.ghostscript_path()
            and "Images are downsampled and fonts subset with Ghostscript."
            or "Ghostscript not found - embedded images will be re-encoded instead."))
        self.levels = SegmentedControl([("low", "Low"), ("medium", "Medium"), ("high", "High")])
        self.levels.changed.connect(self._on_level)
        self.card.add(self.levels)
        self.level_hint = label("", 13, 500, T.TEXT)
        self.card.add(self.level_hint)
        self.card.add(divider())

        self.before_row = QWidget()
        before_layout = QHBoxLayout(self.before_row)
        before_layout.setContentsMargins(0, 0, 0, 0)
        before_layout.setSpacing(14)
        self.original_badge = Badge("-", "neutral")
        before_layout.addWidget(label("Original", 12, 600, T.TEXT_MUTED, "FieldLabel"))
        before_layout.addWidget(self.original_badge)
        before_layout.addStretch(1)
        self.card.add(self.before_row)
        layout.addWidget(self.card)

        output_card = Card(pad=18)
        output_card.add(SectionHeader("Output", "A new file is written — the original is untouched."))
        self.output = FieldRow("File name", "notes_compressed.pdf", "")
        self.output.browse.clicked.connect(self._choose_output)
        output_card.add(self.output)
        layout.addWidget(output_card)

        self.result = ResultCard()
        self.action = primary_button(self.action_label, self.action_icon, self._run)
        layout.addWidget(self.result)
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.addWidget(self.action)
        bar.addStretch(1)
        layout.addLayout(bar)
        self._on_level("medium")
        self._sync()

    def _on_level(self, key):
        preset = core.COMPRESSION_PRESETS[key]
        self.level_hint.setText(f"{preset['label']} — {preset['hint']}")

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a PDF", self.window.last_dir(), "PDF files (*.pdf)")
        if path:
            self.accept_paths([path])

    def _choose_output(self):
        suggested = self.output.text() or "compressed.pdf"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save compressed PDF", str(Path(self.window.last_dir()) / suggested),
            "PDF files (*.pdf)")
        if path:
            self.output.set_text(path)

    def accept_paths(self, paths):
        pdfs = [p for p in paths if str(p).lower().endswith(".pdf")]
        if not pdfs:
            self.window.toasts.notify("Only PDFs", "That file is not a .pdf", "warning")
            return
        self.scan_files(pdfs[:1], self._on_scanned)

    def _on_scanned(self, infos):
        info = infos[0]
        if info.error:
            self.window.toasts.notify("Cannot open it", info.error, "error")
            self.paths = []
            self._sync()
            return
        self.paths = [str(info.path)]
        self.source = info
        self.drop.set_state(info.name, f"{info.pages} pages  ·  {info.size_text}", "compress")
        stem = Path(info.path).stem
        self.output.set_text(f"{stem}_compressed.pdf")
        self.original_badge.setText(info.size_text)
        self.result.hide_result()
        self._sync()

    def _sync(self):
        has = bool(self.paths)
        self.action.setEnabled(has)
        self.drop.set_active(not has)
        self.levels.setEnabled(has)
        self.output.setEnabled(has)

    def _run(self):
        if not self.paths:
            return
        self.result.hide_result()
        self.run_job(core.compress_pdf, self.paths[0], self.output.text(),
                     level=self.levels.value(), title="Compressed", on_done=self._on_done)

    def _on_done(self, result):
        grew = result.output_size >= result.input_size
        if grew:
            self.result.show_result(
                f"Created {result.output.name}",
                f"{result.detail}. This file was already optimised, so it came out the same size.",
                result.size_text, 0, result.output)
            self.window.toasts.notify(
                "Nothing to gain", "That PDF was already well compressed", "info", 6000)
        else:
            self.result.show_result(f"Created {result.output.name}", result.detail,
                                    result.size_text, result.saved, result.output)


class RotatePanel(ToolPanel):
    icon = "rotate"
    title = "Rotate PDF"
    subtitle = "Fix sideways scans and upside-down pages."
    action_label = "Rotate"
    action_icon = "rotate"

    def __init__(self, window):
        super().__init__(window)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        self.drop = DropZone("Drop a PDF to rotate", "or click to choose one", "rotate")
        self.drop.clicked.connect(self._browse)
        self.drop.files_dropped.connect(self.accept_paths)
        layout.addWidget(self.drop)

        self.card = Card(pad=18)
        self.card.add(SectionHeader("Rotation", "Applied as a new copy — the original stays as it is."))
        angle_row = QHBoxLayout()
        angle_row.setSpacing(10)
        self.angles = SegmentedControl([("90", "90° right"), ("180", "180°"), ("270", "90° left")])
        self.angles.changed.connect(self._on_angle)
        angle_row.addWidget(self.angles)
        angle_row.addStretch(1)
        self.card.add_layout(angle_row)
        self.card.add(divider())
        self.scope_row = QWidget()
        scope_layout = QHBoxLayout(self.scope_row)
        scope_layout.setContentsMargins(0, 0, 0, 0)
        scope_layout.setSpacing(10)
        self.scope = SegmentedControl([("all", "All pages"), ("range", "Choose pages")])
        self.scope.changed.connect(self._on_scope)
        scope_layout.addWidget(self.scope)
        self.range_field = FieldRow("", "e.g. 1-3, 8", "")
        self.range_field.setVisible(False)
        scope_layout.addWidget(self.range_field, 1)
        self.card.add(self.scope_row)
        self.preview = label("", 12, 500, T.TEXT_MUTED)
        self.card.add(self.preview)
        layout.addWidget(self.card)

        output_card = Card(pad=18)
        output_card.add(SectionHeader("Output"))
        self.output = FieldRow("File name", "notes_rotated.pdf", "")
        self.output.browse.clicked.connect(self._choose_output)
        output_card.add(self.output)
        layout.addWidget(output_card)

        self.result = ResultCard()
        self.action = primary_button(self.action_label, self.action_icon, self._run)
        layout.addWidget(self.result)
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.addWidget(self.action)
        bar.addStretch(1)
        layout.addLayout(bar)
        self._on_angle("90")
        self._sync()

    def _on_angle(self, key):
        self._sync()

    def _on_scope(self, key):
        self.range_field.setVisible(key == "range")
        self._sync()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a PDF", self.window.last_dir(), "PDF files (*.pdf)")
        if path:
            self.accept_paths([path])

    def _choose_output(self):
        suggested = self.output.text() or "rotated.pdf"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save rotated PDF", str(Path(self.window.last_dir()) / suggested),
            "PDF files (*.pdf)")
        if path:
            self.output.set_text(path)

    def accept_paths(self, paths):
        pdfs = [p for p in paths if str(p).lower().endswith(".pdf")]
        if not pdfs:
            self.window.toasts.notify("Only PDFs", "That file is not a .pdf", "warning")
            return
        self.scan_files(pdfs[:1], self._on_scanned)

    def _on_scanned(self, infos):
        info = infos[0]
        if info.error:
            self.window.toasts.notify("Cannot open it", info.error, "error")
            self.paths = []
            self._sync()
            return
        self.paths = [str(info.path)]
        self.source = info
        self.drop.set_state(info.name, f"{info.pages} pages  ·  {info.size_text}", "rotate")
        self.output.set_text(f"{Path(info.path).stem}_rotated.pdf")
        self.range_field.set_text(f"1-{info.pages}")
        self.result.hide_result()
        self._sync()

    def _sync(self):
        has = bool(self.paths)
        self.action.setEnabled(has)
        self.drop.set_active(not has)
        self.angles.setEnabled(has)
        self.scope.setEnabled(has)
        self.output.setEnabled(has)
        if not has:
            self.preview.setText("")
            return
        total = self.source.pages
        angle = int(self.angles.value())
        if self.scope.value() == "all":
            self.preview.setText(f"All {total} pages will be turned {angle}° clockwise.")
        else:
            spec = self.range_field.text() or f"1-{total}"
            try:
                count = len(core.parse_page_range(spec, total))
                self.preview.setText(f"{count} page(s) matching “{spec}” will be turned {angle}°.")
            except core.PdfToolError as exc:
                self.preview.setText(str(exc))

    def _run(self):
        if not self.paths:
            return
        scope = self.scope.value()
        pages = "all" if scope == "all" else self.range_field.text()
        if scope == "range" and not pages.strip():
            self.window.toasts.notify("Choose pages", "e.g. 1-3, 8", "warning")
            return
        self.result.hide_result()
        self.run_job(core.rotate_pdf, self.paths[0], self.output.text(),
                     angle=int(self.angles.value()), pages=pages,
                     title="Rotated", on_done=self._on_done)

    def _on_done(self, result):
        self.result.show_result(f"Created {result.output.name}", result.detail,
                                result.size_text, result.saved, result.output)


class OrganizePanel(ToolPanel):
    icon = "organize"
    title = "Organise pages"
    subtitle = "Delete, rotate, duplicate and reorder pages visually."
    action_label = "Save new PDF"
    action_icon = "check"

    def __init__(self, window):
        super().__init__(window)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        self.drop = DropZone("Drop a PDF to organise", "or click to choose one", "organize")
        self.drop.clicked.connect(self._browse)
        self.drop.files_dropped.connect(self.accept_paths)
        layout.addWidget(self.drop)

        self.card = Card(pad=18)
        header = SectionHeader("Pages", "Click to select, Ctrl+click for more. "
                                        "Then delete, rotate or duplicate.")
        select_all = ghost_button("Select all", "check", lambda: self.grid.select_all())
        clear_sel = ghost_button("Clear", "close", lambda: self.grid.clear_selection())
        header.add_action(clear_sel)
        header.add_action(select_all)
        self.card.add(header)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.selection_badge = Badge("0 selected", "accent")
        toolbar.addWidget(self.selection_badge)
        toolbar.addStretch(1)
        toolbar.addWidget(icon_button("rotate_ccw", lambda: self._rotate_selected(-90),
                                      "Rotate left"))
        toolbar.addWidget(icon_button("rotate_cw", lambda: self._rotate_selected(90),
                                      "Rotate right"))
        toolbar.addWidget(icon_button("copy", self._duplicate_selected, "Duplicate"))
        toolbar.addWidget(icon_button("arrow_left", lambda: self._move_selected(-1),
                                      "Move earlier"))
        toolbar.addWidget(icon_button("arrow_right", lambda: self._move_selected(1),
                                      "Move later"))
        toolbar.addWidget(icon_button("trash", self._delete_selected, "Delete", danger=True))
        self.card.add_layout(toolbar)

        self.grid = PageGrid(on_request_thumbs=self._thumbs_wanted)
        self.grid.selection_changed.connect(self._sync)
        self.grid.count_changed.connect(lambda _: self._sync())
        self.card.add(self.grid, 1)
        self.grid.setMinimumHeight(300)
        layout.addWidget(self.card, 1)

        output_card = Card(pad=18)
        output_card.add(SectionHeader("Output"))
        self.output = FieldRow("File name", "notes_edited.pdf", "")
        self.output.browse.clicked.connect(self._choose_output)
        output_card.add(self.output)
        layout.addWidget(output_card)

        self.result = ResultCard()
        self.action = primary_button(self.action_label, self.action_icon, self._run)
        layout.addWidget(self.result)
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.addWidget(self.action)
        bar.addStretch(1)
        layout.addLayout(bar)
        self.entries: list[PageEntry] = []
        self.total = 0
        self._sync()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a PDF", self.window.last_dir(), "PDF files (*.pdf)")
        if path:
            self.accept_paths([path])

    def _choose_output(self):
        suggested = self.output.text() or "edited.pdf"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save edited PDF", str(Path(self.window.last_dir()) / suggested),
            "PDF files (*.pdf)")
        if path:
            self.output.set_text(path)

    def accept_paths(self, paths):
        pdfs = [p for p in paths if str(p).lower().endswith(".pdf")]
        if not pdfs:
            self.window.toasts.notify("Only PDFs", "That file is not a .pdf", "warning")
            return
        self.scan_files(pdfs[:1], self._on_scanned)

    def _on_scanned(self, infos):
        info = infos[0]
        if info.error:
            self.window.toasts.notify("Cannot open it", info.error, "error")
            self.paths = []
            self._sync()
            return
        self.paths = [str(info.path)]
        self.source = info
        self.total = info.pages
        self.drop.set_state(info.name, f"{info.pages} pages  ·  {info.size_text}", "organize")
        self.output.set_text(f"{Path(info.path).stem}_edited.pdf")
        self.entries = [PageEntry(index=i) for i in range(info.pages)]
        self.grid.set_entries(self.entries)
        self.window.progress.start("Rendering page previews")
        self.result.hide_result()
        self._sync()

    def _thumbs_wanted(self):
        if self.paths and self.entries:
            self.window.progress.start("Rendering page previews")
            self.load_thumbs(self.grid, self.paths[0], len(self.entries))

    def _selected(self) -> list[PageEntry]:
        return [e for e in self.entries if e.selected]

    def _delete_selected(self):
        chosen = self._selected()
        if not chosen:
            self.window.toasts.notify("Nothing selected", "Click the pages to remove", "warning")
            return
        doomed = {id(e) for e in chosen}
        self.entries = [e for e in self.entries if id(e) not in doomed]
        self._refresh()

    def _duplicate_selected(self):
        chosen = self._selected()
        if not chosen:
            self.window.toasts.notify("Nothing selected", "Click pages to duplicate", "warning")
            return
        for entry in chosen:
            for other in chosen:
                entry.selected = False
        for entry in reversed(chosen):
            clone = PageEntry(index=entry.index, rotation=entry.rotation, thumb=entry.thumb)
            self.entries.insert(self.entries.index(entry) + 1, clone)
        self._refresh()

    def _rotate_selected(self, delta: int):
        chosen = self._selected()
        if not chosen:
            self.window.toasts.notify("Nothing selected", "Click pages to rotate", "warning")
            return
        for entry in chosen:
            entry.rotation = (entry.rotation + delta) % 360
            entry.selected = False
        self._refresh()

    def _move_selected(self, delta: int):
        chosen = self._selected()
        if not chosen:
            self.window.toasts.notify("Nothing selected", "Click pages to move", "warning")
            return
        if delta < 0:
            for entry in reversed(chosen):
                i = self.entries.index(entry)
                if i > 0:
                    self.entries.insert(i - 1, self.entries.pop(i))
        else:
            for entry in chosen:
                i = self.entries.index(entry)
                if i < len(self.entries) - 1:
                    self.entries.insert(i + 1, self.entries.pop(i))
        self._refresh()

    def _refresh(self):
        if not self.entries:
            self.window.toasts.notify("Nothing left", "Keep at least one page", "error")
            self.entries = [PageEntry(index=i) for i in range(max(1, self.total))]
        self.grid.set_entries(self.entries)
        self._sync()

    def _sync(self):
        count = len(self._selected()) if self.entries else 0
        self.selection_badge.setText(f"{count} selected" if count else "Nothing selected")
        self.selection_badge.set_tone("accent" if count else "neutral")
        has = bool(self.paths) and bool(self.entries)
        self.action.setEnabled(has)
        self.drop.set_active(not self.paths)
        self.output.setEnabled(has)
        if has:
            self.action.setText(f"Save {len(self.entries)} pages")

    def _run(self):
        if not self.paths or not self.entries:
            return
        order = [e.index for e in self.entries]
        turns = [e.rotation for e in self.entries]
        self.result.hide_result()
        self.run_job(core.rebuild_pdf, self.paths[0], self.output.text(), order,
                     title="Saved", on_done=self._on_done, rotations=turns)

    def _on_done(self, result):
        self.result.show_result(f"Created {result.output.name}", result.detail,
                                result.size_text, result.saved, result.output)


class ProtectPanel(ToolPanel):
    icon = "lock"
    title = "Protect PDF"
    subtitle = "Add a password so only people with it can open the file."
    action_label = "Protect"
    action_icon = "lock"

    def __init__(self, window):
        super().__init__(window)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        self.drop = DropZone("Drop a PDF to protect", "or click to choose one", "lock")
        self.drop.clicked.connect(self._browse)
        self.drop.files_dropped.connect(self.accept_paths)
        layout.addWidget(self.drop)

        self.card = Card(pad=18)
        self.card.add(SectionHeader("Password", "Viewers ask for this before showing the document."))
        self.password = FieldRow("Password", "required", "")
        self.password.input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.caption.setText("Password")
        self.card.add(self.password)
        self.confirm = FieldRow("Confirm password", "repeat it", "")
        self.confirm.input.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm.caption.setText("Confirm password")
        self.confirm.input.textChanged.connect(self._sync)
        self.card.add(self.confirm)
        self.password.input.textChanged.connect(self._sync)
        self.card.add(divider())

        toggle_row = QHBoxLayout()
        toggle_row.setSpacing(12)
        self.allow_print = ToggleSwitch(True)
        toggle_row.addWidget(self.allow_print)
        toggle_row.addWidget(label("Allow printing and copying", 13, 500, T.TEXT))
        toggle_row.addStretch(1)
        self.card.add_layout(toggle_row)
        layout.addWidget(self.card)

        output_card = Card(pad=18)
        output_card.add(SectionHeader("Output"))
        self.output = FieldRow("File name", "notes_protected.pdf", "")
        self.output.browse.clicked.connect(self._choose_output)
        output_card.add(self.output)
        layout.addWidget(output_card)

        self.result = ResultCard()
        self.action = primary_button(self.action_label, self.action_icon, self._run)
        layout.addWidget(self.result)
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.addWidget(self.action)
        bar.addStretch(1)
        layout.addLayout(bar)
        self._sync()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a PDF", self.window.last_dir(), "PDF files (*.pdf)")
        if path:
            self.accept_paths([path])

    def _choose_output(self):
        suggested = self.output.text() or "protected.pdf"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save protected PDF", str(Path(self.window.last_dir()) / suggested),
            "PDF files (*.pdf)")
        if path:
            self.output.set_text(path)

    def accept_paths(self, paths):
        pdfs = [p for p in paths if str(p).lower().endswith(".pdf")]
        if not pdfs:
            self.window.toasts.notify("Only PDFs", "That file is not a .pdf", "warning")
            return
        self.scan_files(pdfs[:1], self._on_scanned)

    def _on_scanned(self, infos):
        info = infos[0]
        if info.error:
            self.window.toasts.notify("Cannot open it", info.error, "error")
            self.paths = []
            self._sync()
            return
        self.paths = [str(info.path)]
        self.source = info
        self.drop.set_state(info.name, f"{info.pages} pages  ·  {info.size_text}", "lock")
        self.output.set_text(f"{Path(info.path).stem}_protected.pdf")
        self.result.hide_result()
        self._sync()

    def _sync(self):
        has = bool(self.paths)
        password = self.password.text()
        match = password and password == self.confirm.text()
        self.action.setEnabled(has and bool(match))
        self.drop.set_active(not has)
        if not has:
            return
        if password and not match:
            self.window.hint_bar("Passwords do not match yet", "warning")
        else:
            self.window.hint_bar("")

    def _run(self):
        if not self.paths:
            return
        password = self.password.text()
        if password != self.confirm.text():
            self.window.toasts.notify("Check the password", "The two entries differ", "warning")
            return
        self.result.hide_result()
        self.run_job(core.encrypt_pdf, self.paths[0], self.output.text(), password,
                     allow_print=self.allow_print.isChecked(),
                     title="Protected", on_done=self._on_done)

    def _on_done(self, result):
        self.result.show_result(f"Created {result.output.name}",
                                f"{result.detail} — keep this password safe.",
                                result.size_text, 0, result.output)


class MetadataPanel(ToolPanel):
    icon = "tag"
    title = "Metadata"
    subtitle = "Set the title, author and subject readers see in their viewer."
    action_label = "Save metadata"
    action_icon = "check"

    FIELDS = [("/Title", "Title"), ("/Author", "Author"),
              ("/Subject", "Subject"), ("/Keywords", "Keywords")]

    def __init__(self, window):
        super().__init__(window)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        self.drop = DropZone("Drop a PDF to edit", "or click to choose one", "tag")
        self.drop.clicked.connect(self._browse)
        self.drop.files_dropped.connect(self.accept_paths)
        layout.addWidget(self.drop)

        self.card = Card(pad=18)
        self.card.add(SectionHeader("Document information",
                                    "Shown by Acrobat, Preview and most other readers."))
        self.inputs: dict[str, QLineEdit] = {}
        for key, caption in self.FIELDS:
            field_row = QWidget()
            row = QHBoxLayout(field_row)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(12)
            caption_label = label(caption, 12, 600, T.TEXT_MUTED, "FieldLabel")
            caption_label.setFixedWidth(78)
            row.addWidget(caption_label)
            edit = QLineEdit()
            edit.setPlaceholderText(f"{caption}…")
            row.addWidget(edit, 1)
            self.inputs[key] = edit
            self.card.add(field_row)
        self.card.add(divider())
        self.current = label("", 12, 500, T.TEXT_SUBTLE)
        self.card.add(self.current)
        layout.addWidget(self.card)

        output_card = Card(pad=18)
        output_card.add(SectionHeader("Output"))
        self.output = FieldRow("File name", "notes_meta.pdf", "")
        self.output.browse.clicked.connect(self._choose_output)
        output_card.add(self.output)
        layout.addWidget(output_card)

        self.result = ResultCard()
        self.action = primary_button(self.action_label, self.action_icon, self._run)
        layout.addWidget(self.result)
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.addWidget(self.action)
        bar.addStretch(1)
        layout.addLayout(bar)
        self._sync()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a PDF", self.window.last_dir(), "PDF files (*.pdf)")
        if path:
            self.accept_paths([path])

    def _choose_output(self):
        suggested = self.output.text() or "meta.pdf"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save PDF", str(Path(self.window.last_dir()) / suggested),
            "PDF files (*.pdf)")
        if path:
            self.output.set_text(path)

    def accept_paths(self, paths):
        pdfs = [p for p in paths if str(p).lower().endswith(".pdf")]
        if not pdfs:
            self.window.toasts.notify("Only PDFs", "That file is not a .pdf", "warning")
            return
        self.scan_files(pdfs[:1], self._on_scanned)

    def _on_scanned(self, infos):
        info = infos[0]
        if info.error:
            self.window.toasts.notify("Cannot open it", info.error, "error")
            self.paths = []
            self._sync()
            return
        self.paths = [str(info.path)]
        self.source = info
        self.drop.set_state(info.name, f"{info.pages} pages  ·  {info.size_text}", "tag")
        self.output.set_text(f"{Path(info.path).stem}_meta.pdf")
        try:
            values = core.read_metadata(info.path)
        except core.PdfToolError as exc:
            values = {}
            self.window.toasts.notify("Could not read details", str(exc), "warning")
        for key, edit in self.inputs.items():
            edit.setText(values.get(key, ""))
        filled = [v for v in values.values() if v.strip()]
        self.current.setText(
            f"{len(filled)} of {len(self.FIELDS)} fields already set" if filled
            else "No metadata set yet")
        self.result.hide_result()
        self._sync()

    def _sync(self):
        has = bool(self.paths)
        self.action.setEnabled(has)
        self.drop.set_active(not has)
        self.output.setEnabled(has)
        for edit in self.inputs.values():
            edit.setEnabled(has)

    def _run(self):
        if not self.paths:
            return
        changes = {key: edit.text().strip() for key, edit in self.inputs.items()}
        self.result.hide_result()
        self.run_job(core.edit_metadata, self.paths[0], self.output.text(), changes,
                     title="Updated", on_done=self._on_done)

    def _on_done(self, result):
        self.result.show_result(f"Created {result.output.name}", result.detail,
                                result.size_text, 0, result.output)


# ---------------------------------------------------------------- window
TOOLS = [
    ("Workspace", "merge", "Merge PDFs", MergePanel),
    ("Documents", "split", "Split", SplitPanel),
    ("Documents", "compress", "Compress", CompressPanel),
    ("Documents", "rotate", "Rotate", RotatePanel),
    ("Documents", "organize", "Organise", OrganizePanel),
    ("Security", "lock", "Protect", ProtectPanel),
    ("Details", "tag", "Metadata", MetadataPanel),
]


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("Root")
        self.setWindowTitle(APP_NAME)
        self.resize(1180, 780)
        self.setMinimumSize(940, 640)
        self.setAcceptDrops(True)
        self.settings = QSettings("pdf-tools", "pdf-tools")
        self.busy_flag = False
        self.panels: list[ToolPanel] = []
        self.nav_buttons: list[Button] = []

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_sidebar())

        body = QWidget()
        body.setObjectName("Root")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        body_layout.addWidget(self._build_header())
        self.stack = QStackedWidget()
        self.stack.setObjectName("Root")
        body_layout.addWidget(self.stack, 1)

        self.progress = ProgressStrip()
        progress_holder = QWidget()
        progress_holder.setObjectName("Root")
        progress_layout = QHBoxLayout(progress_holder)
        progress_layout.setContentsMargins(30, 10, 30, 4)
        progress_layout.addWidget(self.progress)
        body_layout.addWidget(progress_holder)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        self.stack_host = QWidget()
        self.stack_host.setObjectName("Root")
        host_layout = QVBoxLayout(self.stack_host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.addWidget(self.stack)
        scroll.setWidget(self.stack_host)
        body_layout.addWidget(scroll, 1)
        root.addWidget(body, 1)

        self._build_panels()
        self._install_shortcuts()
        self.toasts = ToastHost(self)

    # -- construction ---------------------------------------------------
    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(232)
        column = QVBoxLayout(sidebar)
        column.setContentsMargins(16, 22, 16, 18)
        column.setSpacing(0)

        brand = QHBoxLayout()
        brand.setSpacing(11)
        mark = _IconBadge("file", 38)
        brand.addWidget(mark)
        names = QVBoxLayout()
        names.setSpacing(1)
        brand_name = label(APP_NAME, 15, 700, T.TEXT, "BrandName")
        names.addWidget(brand_name)
        names.addWidget(label("Desktop toolkit", 11, 500, T.TEXT_SUBTLE, "BrandTag"))
        brand.addLayout(names)
        brand.addStretch(1)
        column.addLayout(brand)
        column.addSpacing(24)

        current_section = None
        for section, icon, title, factory in TOOLS:
            if section != current_section:
                column.addSpacing(14)
                column.addWidget(label(section.upper(), 10, 700, T.TEXT_SUBTLE, "NavSection"))
                column.addSpacing(7)
                current_section = section
            button = Button("", "default")
            button.setObjectName("NavItem")
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setIcon(T.make_icon(icon, 18, T.TEXT_MUTED))
            button.setIconSize(QSize(18, 18))
            button.setText(f"  {title}")
            button.setToolTip(f"{title} — {_describe(factory)}")
            column.addWidget(button)
            self.nav_buttons.append(button)
        column.addStretch(1)

        footer = QWidget()
        footer.setObjectName("CardSunken")
        foot_layout = QVBoxLayout(footer)
        foot_layout.setContentsMargins(12, 11, 12, 11)
        foot_layout.setSpacing(6)
        engine = ("Ghostscript ready" if core.ghostscript_path()
                  else "Ghostscript not found")
        row = QHBoxLayout()
        row.setSpacing(7)
        row.addWidget(Badge("●", "success" if core.ghostscript_path() else "warning"))
        row.addWidget(label(engine, 11, 600, T.TEXT_MUTED))
        row.addStretch(1)
        foot_layout.addLayout(row)
        foot_layout.addWidget(label("pypdf engine  ·  local only", 11, 400, T.TEXT_SUBTLE))
        column.addWidget(footer)
        return sidebar

    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setObjectName("Header")
        header.setFixedHeight(84)
        row = QHBoxLayout(header)
        row.setContentsMargins(30, 0, 30, 0)
        row.setSpacing(14)
        text = QVBoxLayout()
        text.setSpacing(3)
        self.page_title = label("", 21, 700, T.TEXT, "PageTitle")
        self.page_subtitle = label("", 13, 400, T.TEXT_MUTED, "PageSubtitle")
        text.addWidget(self.page_title)
        text.addWidget(self.page_subtitle)
        row.addLayout(text)
        row.addStretch(1)
        self.hint = label("", 12, 600, T.WARNING)
        row.addWidget(self.hint)
        row.addWidget(ghost_button("Open folder", "folder", self._open_folder))
        return header

    def _build_panels(self) -> None:
        for index, (section, icon, title, factory) in enumerate(TOOLS):
            panel = factory(self)
            column = panel.layout()
            column.setContentsMargins(30, 4, 30, 30)
            column.setSpacing(18)
            self.stack.addWidget(panel)
            self.panels.append(panel)
            self.nav_buttons[index].clicked.connect(
                lambda _checked=False, i=index: self.select(i))
        self.select(0)

    def _install_shortcuts(self) -> None:
        for index in range(len(TOOLS)):
            QShortcut(QKeySequence(f"Ctrl+{index + 1}"), self,
                      activated=partial(self.select, index))
        QShortcut(QKeySequence("Ctrl+Q"), self, activated=self.close)

    # -- state ----------------------------------------------------------
    def select(self, index: int) -> None:
        if not (0 <= index < len(self.panels)):
            return
        if self.busy_flag:
            self.toasts.notify("Still working", "Wait for the current job to finish", "info")
            return
        for i, button in enumerate(self.nav_buttons):
            button.setChecked(i == index)
            button.setIcon(T.make_icon(TOOLS[i][1], 18,
                                       T.ACCENT if i == index else T.TEXT_MUTED))
        panel = self.panels[index]
        self.stack.setCurrentWidget(panel)
        self.page_title.setText(TOOLS[index][2])
        self.page_subtitle.setText(TOOLS[index][3].subtitle)
        self.progress.hide()
        self.hint.setText("")

    def set_busy(self, value: bool) -> None:
        self.busy_flag = value
        if not value:
            self.progress.hide()
        for button in self.nav_buttons:
            button.setEnabled(not value)

    def hint_bar(self, text: str, tone: str = "warning") -> None:
        self.hint.setText(text)
        self.hint.setStyleSheet(f"color: {T.WARNING if tone == 'warning' else T.DANGER};")

    def last_dir(self) -> str:
        return self.settings.value("last_dir", str(Path.home()))

    def _open_folder(self) -> None:
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(self.last_dir()))

    # -- window level drag & drop --------------------------------------
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if not paths:
            return
        if paths:
            self.settings.setValue("last_dir", str(Path(paths[0]).parent))
        panel = self.stack.currentWidget()
        if isinstance(panel, ToolPanel):
            panel.accept_paths(paths)

    def closeEvent(self, event):
        if self.busy_flag:
            self.toasts.notify("Still working", "Wait for the job to finish", "warning")
            event.ignore()
            return
        if self.stack.currentIndex() >= 0:
            panel = self.stack.currentWidget()
            if getattr(panel, "paths", None):
                self.settings.setValue("last_dir", str(Path(panel.paths[0]).parent))
        event.accept()


def _describe(factory) -> str:
    return (factory.subtitle.split(".")[0] if factory.subtitle else "").strip()


def main() -> int:
    QApplication.setApplicationName(APP_NAME)
    QApplication.setOrganizationName("pdf-tools")
    QApplication.setStyle("Fusion")

    app = QApplication(sys.argv)
    family = T.ui_font_family()
    if family:
        app.setFont(T.font(13))
    app.setStyleSheet(T.stylesheet())

    window = MainWindow()
    window.show()

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        panel = window.panels[0]
        panel.accept_paths(args)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
