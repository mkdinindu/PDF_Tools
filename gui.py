#!/usr/bin/env python3
"""PDF Tools - desktop interface.

Run with ``python3 PDF_Tools.py --gui`` (or ``python3 gui.py``).
"""

from __future__ import annotations

import copy
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from PyQt6.QtCore import (
    QObject,
    QRunnable,
    QSettings,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QFontDatabase,
    QFontMetrics,
    QGuiApplication,
    QImage,
    QKeySequence,
    QPixmap,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

import pdf_core as core
import pdf_editor_core as editor
import theme as T
import win_text_input
from editor_widgets import (
    EditorCanvas,
    EditorInspector,
    PageRail,
    RenderWorker,
    ThumbnailWorker,
    tool_toggle,
)
from widgets import (
    Badge,
    Button,
    Card,
    DropZone,
    FieldRow,
    FileChip,
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

    def __init__(self, path: str, index: int, row: int, width: int,
                 signals: ThumbSignals, generation: int):
        super().__init__()
        self.path = path
        self.index = index
        self.row = row
        self.width = width
        self.signals = signals
        self.generation = generation

    @staticmethod
    def _key(path: str, index: int, width: int, mtime: float, size: int, generation: int) -> tuple:
        return (path, index, width, mtime, size, generation)

    def run(self):
        if QGuiApplication.instance() is None:
            return
        try:
            stat = os.stat(self.path)
        except OSError:
            return
        key = self._key(self.path, self.index, self.width, stat.st_mtime, stat.st_size, self.generation)
        cache_dir = Path(tempfile.gettempdir()) / "pdf-tools-thumbs"
        cache_file = cache_dir / (f"{abs(hash(key))}.png")
        if cache_file.exists():
            try:
                pixmap = QPixmap(str(cache_file))
                if not pixmap.isNull():
                    self.signals.ready.emit(self.row, pixmap)
                    return
            except Exception:  # noqa: BLE001
                pass

        gs = core.ghostscript_path()
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            if gs:
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
            else:
                # No Ghostscript: PyMuPDF is bundled with the app, so the
                # Organise grid still gets real page thumbnails.
                cache_file.write_bytes(editor.render_thumbnail(self.path, self.index, self.width))
        except (subprocess.TimeoutExpired, OSError):
            pass
        except Exception:  # noqa: BLE001 - a failed thumbnail must not break the grid
            pass
        if QGuiApplication.instance() is None:
            return
        pixmap = QPixmap(str(cache_file)) if cache_file.exists() else None
        if pixmap is not None and pixmap.isNull():
            pixmap = None
        self.signals.ready.emit(self.row, pixmap)


class ImageThumb(QRunnable):
    """Renders a dropped image file to a QImage for its grid card.

    Uses QImage rather than QPixmap because pixmaps may only be built on the
    GUI thread; the result is converted in :meth:`ToolPanel._apply_thumb`.
    """

    def __init__(self, path: str, row: int, width: int, signals: ThumbSignals,
                 generation: int):
        super().__init__()
        self.path = path
        self.row = row
        self.width = width
        self.signals = signals
        self.generation = generation

    def run(self):
        if QGuiApplication.instance() is None:
            return
        try:
            stat = os.stat(self.path)
        except OSError:
            return
        key = (self.path, self.width, stat.st_mtime, stat.st_size, self.generation)
        cache_dir = Path(tempfile.gettempdir()) / "pdf-tools-thumbs"
        cache_file = cache_dir / f"img-{abs(hash(key))}.png"
        if cache_file.exists():
            try:
                cached = QImage(str(cache_file))
                if not cached.isNull():
                    self.signals.ready.emit(self.row, cached)
                    return
            except Exception:  # noqa: BLE001
                pass
        try:
            image = QImage(self.path)
            if image.isNull():
                self.signals.ready.emit(self.row, None)
                return
            # honour EXIF orientation so photos are not shown sideways
            if not image.isNull() and hasattr(image, "setAutoTransform"):
                image.setAutoTransform(True)
            scaled = image.scaledToWidth(
                max(48, self.width * 3),
                Qt.TransformationMode.SmoothTransformation)
            cache_dir.mkdir(parents=True, exist_ok=True)
            scaled.save(str(cache_file), "PNG")
        except Exception:  # noqa: BLE001
            self.signals.ready.emit(self.row, None)
            return
        self.signals.ready.emit(self.row, scaled)

# ------------------------------------------------------------ source bar
class SourceBar(QWidget):
    """The single place files are loaded. Every tool reads from here.

    Files are added once - by dropping them anywhere in the window or via the
    browse button - and then reused by all seven tools. The tool that acts on a
    single document follows the selected chip; merge uses the whole list in
    order.
    """

    changed = pyqtSignal()                # the list itself changed
    current_changed = pyqtSignal(object)  # FileInfo | None

    def __init__(self, window: "MainWindow", parent: QWidget | None = None):
        super().__init__(parent)
        self.window = window
        self._items: list[core.FileInfo] = []
        self._current = -1
        self._scan: Scan | None = None

        row = QHBoxLayout(self)
        row.setContentsMargins(30, 0, 30, 0)
        row.setSpacing(12)

        self.drop = DropZone("Add PDFs", "drop anywhere", "file", compact=True)
        self.drop.setFixedWidth(232)
        self.drop.clicked.connect(self.browse)
        self.drop.files_dropped.connect(self.add_paths)
        row.addWidget(self.drop, 0, Qt.AlignmentFlag.AlignVCenter)

        self.clear = ghost_button("Clear", "close", self.clear_all,
                                  tooltip="Remove every loaded file")
        self.clear.setFixedHeight(40)
        row.addWidget(self.clear, 0, Qt.AlignmentFlag.AlignVCenter)

        self._scroll = QScrollArea()
        self._scroll.setObjectName("ChipScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFixedHeight(64)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._holder = QWidget()
        self._holder.setStyleSheet("background: transparent;")
        self._layout = QHBoxLayout(self._holder)
        self._layout.setContentsMargins(2, 0, 2, 0)
        self._layout.setSpacing(8)
        self._layout.addStretch(1)
        self._scroll.setWidget(self._holder)
        row.addWidget(self._scroll, 1)

        self._empty = label("No files loaded yet — drop PDFs here or use Add PDFs. "
                            "Every tool then reuses the file you select.",
                            12, 500, T.TEXT_MUTED)
        self._empty.setWordWrap(True)
        self._rebuild()

    # -- state ----------------------------------------------------------
    def infos(self) -> list[core.FileInfo]:
        return list(self._items)

    def usable(self) -> list[core.FileInfo]:
        return [i for i in self._items if i.ok]

    def current(self) -> core.FileInfo | None:
        if 0 <= self._current < len(self._items):
            return self._items[self._current]
        return None

    def total_pages(self) -> int:
        return sum(i.pages for i in self.usable())

    def total_size(self) -> str:
        return core.human_size(sum(i.size for i in self.usable()))

    def set_current(self, index: int) -> None:
        if index == self._current or not (-1 <= index < len(self._items)):
            return
        self._current = index
        for position, chip in enumerate(self._chips):
            chip.set_selected(position == index, position + 1)
        self.current_changed.emit(self.current())

    # -- mutations ------------------------------------------------------
    def browse(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select PDFs", self.window.last_dir(), "PDF files (*.pdf)")
        if paths:
            self.add_paths(paths)

    def add_paths(self, paths) -> None:
        picked = [p for p in paths if str(p).lower().endswith(".pdf")]
        if not picked:
            self.window.toasts.notify("Only PDFs", "That file is not a .pdf", "warning")
            return
        self.window.settings.setValue("last_dir", str(Path(str(picked[0])).parent))
        known = {str(i.path) for i in self._items}
        fresh: list[str] = []
        for path in picked:
            text = str(path)
            if text not in known:
                known.add(text)
                fresh.append(text)
        if not fresh:
            self.window.toasts.notify("Already loaded", "Those files are in the list", "info")
            return
        self._scan = Scan(fresh)
        self.window.progress.start(f"Reading {len(fresh)} file(s)")

        def ok(loaded):
            self.window.progress.finish("Ready", True)
            bad = [i for i in loaded if i.error]
            if bad:
                self.window.toasts.notify(
                    f"Skipped {len(bad)} file(s)", f"{bad[0].name}: {bad[0].error}",
                    "warning", 6000)
            good = [i for i in loaded if not i.error]
            if good:
                self._items.extend(good)
                if self._current < 0:
                    self._current = 0
                self._rebuild()
                self.changed.emit()
                self.current_changed.emit(self.current())

        def bad(message):
            self.window.progress.finish("Failed", False)
            self.window.toasts.notify("Could not read files", message, "error")

        self._scan.signals.done.connect(ok)
        self._scan.signals.failed.connect(bad)
        QThreadPool.globalInstance().start(self._scan)

    def remove_at(self, index: int) -> None:
        if not (0 <= index < len(self._items)):
            return
        self._items.pop(index)
        if not self._items:
            self._current = -1
        elif self._current >= len(self._items):
            self._current = len(self._items) - 1
        self._rebuild()
        self.changed.emit()
        self.current_changed.emit(self.current())

    def move(self, index: int, delta: int) -> None:
        target = index + delta
        if not (0 <= index < len(self._items) and 0 <= target < len(self._items)):
            return
        self._items.insert(target, self._items.pop(index))
        self._current = target
        self._rebuild()
        self.changed.emit()
        self.current_changed.emit(self.current())

    def clear_all(self) -> None:
        if not self._items:
            return
        self._items.clear()
        self._current = -1
        self._rebuild()
        self.changed.emit()
        self.current_changed.emit(None)

    # -- ui -------------------------------------------------------------
    @property
    def _chips(self) -> list[FileChip]:
        return [w for w in self._holder.findChildren(FileChip)]

    def _rebuild(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None and widget is not self._empty:
                widget.setParent(None)
                widget.deleteLater()
        total = len(self._items)
        self._empty.setVisible(total == 0)
        if not total:
            self._layout.addWidget(self._empty)
        for index, info in enumerate(self._items):
            chip = FileChip(
                info, index + 1, index == self._current,
                index > 0, index < total - 1,
                on_pick=partial(self.set_current, index),
                on_remove=partial(self.remove_at, index),
                on_move=partial(self.move, index),
                parent=self._holder,
            )
            self._layout.addWidget(chip)
        self._layout.addStretch(1)
        chips = self._chips
        needed = (sum(c.sizeHint().width() for c in chips)
                  + self._layout.spacing() * (len(chips) + 1) + 4) if chips else 0
        self._holder.setMinimumWidth(needed)
        self.clear.setEnabled(total > 0)


class UsingBar(QFrame):
    """Shows which file from the shared source a tool is about to work on."""

    def __init__(self, icon: str, caption: str, on_browse,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("CardSunken")
        self.setFixedHeight(66)
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 0, 12, 0)
        row.setSpacing(11)

        self.badge = _IconBadge(icon, 34)
        row.addWidget(self.badge)

        text = QVBoxLayout()
        text.setSpacing(2)
        text.setContentsMargins(0, 0, 0, 0)
        self.name = label("No file selected", 13, 600, T.TEXT)
        self.sub = label(caption, 12, 400, T.TEXT_MUTED)
        text.addWidget(self.name)
        text.addWidget(self.sub)
        row.addLayout(text)
        row.addStretch(1)

        self.browse = ghost_button("Add PDFs", "plus", on_browse,
                                   tooltip="Add more PDFs to the top bar")
        self.browse.setFixedHeight(36)
        row.addWidget(self.browse)

    def set_info(self, info: core.FileInfo | None) -> None:
        if info is None:
            self.name.setText("No file selected")
            self.name.setStyleSheet(f"color: {T.TEXT_MUTED};")
        else:
            metrics = QFontMetrics(self.name.font())
            self.name.setText(metrics.elidedText(info.name, Qt.TextElideMode.ElideMiddle, 430))
            self.name.setStyleSheet("")

    def set_detail(self, text: str) -> None:
        self.sub.setText(text)


# ---------------------------------------------------------------- panels
class ToolPanel(QWidget):
    """Base class for each tool's workspace."""

    icon = "file"
    title = "Tool"
    subtitle = ""
    action_label = "Run"
    action_icon = "sparkle"

    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.window = window
        self._jobs: list[Job] = []
        self._thumb_generation = 0
        self.setAcceptDrops(True)
        window.source.current_changed.connect(self._on_source)

    # -- shared source --------------------------------------------------
    @property
    def source(self) -> SourceBar:
        return self.window.source

    @property
    def info(self) -> core.FileInfo | None:
        """The file this tool works on - the one selected in the top bar."""
        return self.window.source.current()

    @property
    def paths(self) -> list[str]:
        info = self.info
        return [str(info.path)] if info is not None else []

    def _on_source(self, info) -> None:
        """Called whenever the shared source list or selection changes."""
        self._refresh_source()
        self._sync()

    def _refresh_source(self) -> None:  # pragma: no cover - overridden
        """Reset per-file state that no longer applies. Called on every change."""

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

    # -- helpers used by subclasses -------------------------------------
    def load_entry_thumbs(self, grid: PageGrid, path: str | None,
                          entries: list[PageEntry], generation: int = 0) -> None:
        """Request one thumbnail per card, PDF pages and dropped images alike."""
        if not entries:
            return
        signals = ThumbSignals()
        signals.ready.connect(partial(self._apply_thumb, grid, path, len(entries), generation))
        pool = QThreadPool.globalInstance()
        for row, entry in enumerate(entries):
            if entry.is_image:
                pool.start(ImageThumb(str(entry.image), row, grid.card_width,
                                      signals, generation))
            elif path:
                pool.start(Thumbnailer(path, entry.index, row, grid.card_width,
                                       signals, generation))

    def _apply_thumb(self, grid: PageGrid, path: str | None, count: int,
                     generation: int, index: int, pixmap) -> None:
        if generation != self._thumb_generation:
            return
        info = self.info
        if info is not None and path and str(info.path) != path:
            return
        if isinstance(pixmap, QImage):
            if pixmap.isNull():
                return
            pixmap = QPixmap.fromImage(pixmap)
        entries = grid.entries()
        if 0 <= index < len(entries) and pixmap is not None and not pixmap.isNull():
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

    def accept_paths(self, paths) -> None:
        """Drops never land in a single tool - they feed the shared source."""
        self.window.source.add_paths(paths)

    def _browse_source(self) -> None:
        self.window.source.browse()


class MergePanel(ToolPanel):
    icon = "merge"
    title = "Merge PDFs"
    subtitle = "Combine lecture notes, chapters or scans into one document."
    action_label = "Merge documents"
    action_icon = "merge"

    def __init__(self, window):
        super().__init__(window)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        # which files are being merged, straight from the shared source
        self.using = UsingBar("merge", "No files loaded yet.", self._browse_source)
        layout.addWidget(self.using)

        # order card - the chips in the top bar are the queue
        self.queue_card = Card(pad=18)
        self.queue_card.add(SectionHeader(
            "Merge order",
            "Drag PDFs into the app, then reorder them in the top bar — "
            "the first file starts the document."))
        self.summary = label("", 12, 500, T.TEXT_MUTED)
        self.queue_card.add(self.summary)
        self.order = label("", 12, 400, T.TEXT_MUTED)
        self.order.setWordWrap(True)
        self.queue_card.add(self.order)
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
        layout.addWidget(self.progress_strip)
        layout.addWidget(self.action)
        self._sync()

    def _choose_output(self):
        suggested = self.output.text() or "merged.pdf"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save merged PDF", str(Path(self.window.last_dir()) / suggested),
            "PDF files (*.pdf)")
        if path:
            self.output.set_text(path)

    def _refresh_source(self) -> None:
        self.progress_strip.hide_result()

    def _sync(self):
        items = self.source.infos()
        usable = self.source.usable()
        if not items:
            self.using.set_info(None)
            self.using.set_detail("Load two or more PDFs in the top bar.")
            self.summary.setText("No files loaded yet.")
            self.order.setText("")
        elif len(usable) < 2:
            self.using.set_info(items[0])
            self.using.set_detail("1 file loaded — add one more in the top bar.")
            self.summary.setText(f"{len(usable)} file loaded — add one more to merge.")
            self.order.setText("")
        else:
            self.using.set_info(None)
            self.using.set_detail(
                f"{len(usable)} files selected · {self.source.total_pages()} pages · "
                f"{self.source.total_size()}")
            self.summary.setText(
                f"Merging {len(usable)} files · {self.source.total_pages()} pages · "
                f"{self.source.total_size()}")
            numbered = "  →  ".join(f"{n}. {i.name}" for n, i in enumerate(usable, 1))
            self.order.setText(numbered)
        self.action.setEnabled(len(usable) >= 2)

    def _run(self):
        usable = self.source.usable()
        if len(usable) < 2:
            self.window.toasts.notify("Need more files", "Load at least two readable PDFs",
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

        self.using = UsingBar("split", "No file selected in the top bar.",
                              self._browse_source)
        layout.addWidget(self.using)

        self.options = Card(pad=18)
        self.options.add(SectionHeader("How should it be split?"))
        self.modes = SegmentedControl([("range", "Page range"), ("each", "Every N pages")])
        self.modes.changed.connect(self._on_mode)
        self.options.add(self.modes)

        self.range_row = QWidget()
        range_layout = QHBoxLayout(self.range_row)
        range_layout.setContentsMargins(0, 0, 0, 0)
        range_layout.setSpacing(10)
        self.range_field = FieldRow("Pages to keep", "e.g. 1-3, 7, 12-", "")
        range_layout.addWidget(self.range_field, 1)
        self.range_hint = label("", 12, 500, T.TEXT_MUTED)
        self.range_hint.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        range_layout.addWidget(self.range_hint)
        self.options.add(self.range_row)

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
        self.options.add(self.each_row)

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
        self.options.add(self.page_buttons)
        layout.addWidget(self.options)

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

    def _refresh_source(self) -> None:
        info = self.info
        self.result.hide_result()
        self.range_field.set_text("")
        self.using.set_info(info)
        if info is None:
            self.using.set_detail("Pick a file in the top bar to split it.")
            return
        self.output.set_text(f"{Path(info.path).stem}_split.pdf")
        self.using.set_detail(f"{info.pages} pages  ·  {info.size_text}")
    def _sync(self):
        has = self.info is not None
        self.action.setEnabled(has)
        self.page_buttons.setEnabled(has)
        self.modes.setEnabled(has)
        self.range_field.setEnabled(has)
        self.every.setEnabled(has)
        if has:
            total = self.info.pages
            if not self.range_field.text():
                self.range_field.set_text(f"1-{total}")
            self.range_hint.setText(f"of {total}")

    def _quick_pick(self, which):
        if not self.paths:
            return
        total = self.info.pages
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

        self.using = UsingBar("compress", "No file selected in the top bar.",
                              self._browse_source)
        layout.addWidget(self.using)

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

    def _choose_output(self):
        suggested = self.output.text() or "compressed.pdf"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save compressed PDF", str(Path(self.window.last_dir()) / suggested),
            "PDF files (*.pdf)")
        if path:
            self.output.set_text(path)

    def _refresh_source(self) -> None:
        info = self.info
        self.result.hide_result()
        self.using.set_info(info)
        if info is None:
            self.using.set_detail("Pick a file in the top bar to compress it.")
            self.original_badge.setText("-")
            return
        self.output.set_text(f"{Path(info.path).stem}_compressed.pdf")
        self.original_badge.setText(info.size_text)
        self.using.set_detail(f"{info.pages} pages  ·  {info.size_text} before compressing")
    def _sync(self):
        has = self.info is not None
        self.action.setEnabled(has)
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

        self.using = UsingBar("rotate", "No file selected in the top bar.",
                              self._browse_source)
        layout.addWidget(self.using)

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

    def _choose_output(self):
        suggested = self.output.text() or "rotated.pdf"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save rotated PDF", str(Path(self.window.last_dir()) / suggested),
            "PDF files (*.pdf)")
        if path:
            self.output.set_text(path)

    def _refresh_source(self) -> None:
        info = self.info
        self.result.hide_result()
        self.range_field.set_text("")
        self.using.set_info(info)
        if info is None:
            self.using.set_detail("Pick a file in the top bar to rotate it.")
            return
        self.output.set_text(f"{Path(info.path).stem}_rotated.pdf")
        self.range_field.set_text(f"1-{info.pages}")
        self.using.set_detail(f"{info.pages} pages  ·  {info.size_text}")
    def _sync(self):
        has = self.info is not None
        self.action.setEnabled(has)
        self.angles.setEnabled(has)
        self.scope.setEnabled(has)
        self.output.setEnabled(has)
        if not has:
            self.preview.setText("")
            return
        total = self.info.pages
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

        self.using = UsingBar("organize", "No file selected in the top bar.",
                              self._browse_source)
        layout.addWidget(self.using)

        self.card = Card(pad=18)
        header = SectionHeader("Pages", "Drag to reorder  ·  drag images in to add pages  "
                                        "·  click to select")
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

        self.grid = PageGrid(on_request_thumbs=self._thumbs_wanted,
                             image_suffixes=core.IMAGE_SUFFIXES)
        self.grid.selection_changed.connect(self._sync)
        self.grid.count_changed.connect(lambda _: self._sync())
        self.grid.reorder_moved.connect(self._on_reorder)
        self.grid.images_dropped.connect(self._add_images)
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

    def _choose_output(self):
        suggested = self.output.text() or "edited.pdf"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save edited PDF", str(Path(self.window.last_dir()) / suggested),
            "PDF files (*.pdf)")
        if path:
            self.output.set_text(path)

    def _refresh_source(self) -> None:
        info = self.info
        self.result.hide_result()
        self.using.set_info(info)
        if info is None:
            self.using.set_detail("Pick a file in the top bar to reorganise its pages.")
            self.total = 0
            self.entries = []
            self.grid.set_entries([])
            return
        self._thumb_generation += 1
        self.total = info.pages
        self.output.set_text(f"{Path(info.path).stem}_edited.pdf")
        self.entries = [PageEntry(index=i) for i in range(info.pages)]
        self.grid.set_entries(self.entries)
        self.window.progress.start("Rendering page previews")
        self.using.set_detail(f"{info.pages} pages  ·  {info.size_text}")

    def _thumbs_wanted(self):
        if not self.entries:
            return
        source = self.paths[0] if self.paths else None
        if source is None and not any(e.is_image for e in self.entries):
            return
        self.window.progress.start("Rendering page previews")
        self.load_entry_thumbs(self.grid, source, self.entries, self._thumb_generation)

    # -- dropping images -------------------------------------------------
    def accept_paths(self, paths) -> None:
        """Images become new pages here; PDFs still feed the shared source."""
        images, others = core.split_images(paths)
        if images:
            self._add_images(images, len(self.entries))
        if others:
            self.window.source.add_paths(others)

    def dropEvent(self, event):
        """Drops on the grid slot images in where the cursor is aiming."""
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if not paths:
            return
        images, others = core.split_images(paths)
        if images:
            viewport = self.grid.viewport()
            here = event.position().toPoint()
            inside = viewport.geometry().contains(here)
            at = self.grid.slot_for_point(viewport.mapFrom(self, here)) if inside \
                else len(self.entries)
            self._add_images(images, at)
        if others:
            self.window.settings.setValue("last_dir", str(Path(others[0]).parent))
            self.window.source.add_paths(others)
        if images or others:
            event.acceptProposedAction()

    def _add_images(self, images, at: int | None = None) -> None:
        existing = {e.image for e in self.entries if e.image is not None}
        fresh = [p for p in images if p not in existing]
        if not fresh:
            self.window.toasts.notify("Already added", "Those images are already in the grid",
                                      "warning")
            return
        entries = [PageEntry(index=0, image=path) for path in fresh]
        position = len(self.entries) if at is None else max(0, min(int(at), len(self.entries)))
        if not self.entries:
            self.total = 0
        self.grid.insert_entries(entries, position)
        self.entries = self.grid.entries()
        self._sync()
        self._thumbs_wanted()
        self.window.toasts.notify(
            f"Added {len(entries)} image{'s' if len(entries) > 1 else ''}",
            "Drag to place them, then save", "success")

    def _on_reorder(self, source: int, target: int) -> None:
        entries = self.entries
        if not (0 <= source < len(entries)) or not (0 <= target < len(entries)):
            return
        entry = entries.pop(source)
        entries.insert(target, entry)
        self.grid.relayout()
        self._sync()

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
            clone = PageEntry(index=entry.index, rotation=entry.rotation,
                              thumb=entry.thumb, image=entry.image)
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
            if self.info is not None:
                self.entries = [PageEntry(index=i) for i in range(max(1, self.total))]
            else:
                self.window.toasts.notify("Nothing left", "Drop in some images to continue",
                                          "warning")
                self.grid.set_entries([])
                self._sync()
                return
        self.grid.set_entries(self.entries)
        self._sync()

    def _sync(self):
        count = len(self._selected()) if self.entries else 0
        self.selection_badge.setText(f"{count} selected" if count else "Nothing selected")
        self.selection_badge.set_tone("accent" if count else "neutral")
        has = bool(self.entries)
        self.action.setEnabled(has)
        self.output.setEnabled(has)
        images = sum(1 for e in self.entries if e.is_image)
        if has and images:
            self.action.setText(f"Save {len(self.entries)} pages")
            self.using.set_detail(f"{len(self.entries)} pages  ·  {images} from images")
        elif has:
            self.action.setText(f"Save {len(self.entries)} pages")
        else:
            self.action.setText(self.action_label)

    def _run(self):
        if not self.entries:
            return
        order = [e.image if e.is_image else e.index for e in self.entries]
        turns = [e.rotation for e in self.entries]
        source = self.paths[0] if self.paths else None
        if source is None:
            self.output.set_text(self.output.text() or "images.pdf")
        self.result.hide_result()
        self.run_job(core.rebuild_pdf, source, self.output.text(), order,
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

        self.using = UsingBar("lock", "No file selected in the top bar.",
                              self._browse_source)
        layout.addWidget(self.using)

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

    def _choose_output(self):
        suggested = self.output.text() or "protected.pdf"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save protected PDF", str(Path(self.window.last_dir()) / suggested),
            "PDF files (*.pdf)")
        if path:
            self.output.set_text(path)

    def _refresh_source(self) -> None:
        info = self.info
        self.result.hide_result()
        self.using.set_info(info)
        if info is None:
            self.using.set_detail("Pick a file in the top bar to protect it.")
            return
        self.output.set_text(f"{Path(info.path).stem}_protected.pdf")
        if info.encrypted:
            self.using.set_detail(f"{info.pages} pages  ·  already password protected")
        else:
            self.using.set_detail(f"{info.pages} pages  ·  {info.size_text}")
    def _sync(self):
        has = self.info is not None
        password = self.password.text()
        match = password and password == self.confirm.text()
        self.action.setEnabled(has and bool(match))
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

        self.using = UsingBar("tag", "No file selected in the top bar.",
                              self._browse_source)
        layout.addWidget(self.using)

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

    def _choose_output(self):
        suggested = self.output.text() or "meta.pdf"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save PDF", str(Path(self.window.last_dir()) / suggested),
            "PDF files (*.pdf)")
        if path:
            self.output.set_text(path)

    def _refresh_source(self) -> None:
        info = self.info
        self.result.hide_result()
        self.using.set_info(info)
        if info is None:
            self.using.set_detail("Pick a file in the top bar to edit its details.")
            for edit in self.inputs.values():
                edit.clear()
            self.current.setText("No file selected")
            return
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
        self.using.set_detail(f"{info.pages} pages  ·  {info.size_text}")
    def _sync(self):
        has = self.info is not None
        self.action.setEnabled(has)
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


class EditorPanel(ToolPanel):
    """Visual page editor for text and images, with Sinhala-aware shaping."""

    icon = "edit"
    title = "Edit page"
    subtitle = ("Change text and pictures on a page - Sinhala is shaped correctly, "
                "not just swapped glyph by glyph.")
    action_label = "Save edited PDF"
    action_icon = "check"

    def __init__(self, window):
        super().__init__(window)
        self._sessions: dict[str, "EditorSession"] = {}
        self._session: EditorSession | None = None
        self._render_token = 0
        self._render_worker = None
        self._rail_worker = None
        self._change_tag = ""
        self._last_render_error = ""
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(110)
        self._render_timer.timeout.connect(self._render_now)
        self._change_timer = QTimer(self)
        self._change_timer.setSingleShot(True)
        self._change_timer.timeout.connect(self._clear_change_tag)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        self.using = UsingBar("edit", "No file selected in the top bar.",
                              self._browse_source)
        layout.addWidget(self.using)

        self.workspace = Card(pad=12)
        self.workspace.add_layout(self._build_toolbar())

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(8)

        self.rail = PageRail()
        self.rail.page_selected.connect(self._open_page)
        self.splitter.addWidget(self.rail)

        self.canvas = EditorCanvas()
        self.canvas.selection_changed.connect(self._on_selection)
        self.canvas.before_change.connect(self._begin_change)
        self.canvas.changed.connect(self._on_canvas_changed)
        self.canvas.committed.connect(self._on_canvas_commit)
        self.canvas.zoom_delta.connect(self._zoom_by)
        self.canvas.tool_finished.connect(lambda: self._set_tool("select"))
        self.canvas.set_placeholder(
            "Open a PDF in the top bar, then click a line of text or an image.\n"
            "Pick the Text tool to draw a new text box.")

        self.scroll = QScrollArea()
        self.scroll.setObjectName("EditorScroll")
        self.scroll.setWidgetResizable(False)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
        self.scroll.setWidget(self.canvas)
        self.splitter.addWidget(self.scroll)

        self.inspector = EditorInspector()
        self.inspector.begin_change.connect(self._begin_change)
        self.inspector.changed.connect(self._on_inspector_changed)
        self.inspector.delete_requested.connect(self._delete_selected)
        self.inspector.replace_image_requested.connect(self._replace_image)
        self.splitter.addWidget(self.inspector)

        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setSizes([136, 620, 286])
        self.splitter.setMinimumHeight(340)
        self.workspace.add(self.splitter, 1)
        layout.addWidget(self.workspace, 1)

        bottom = Card(pad=14)
        row = QHBoxLayout()
        row.setSpacing(12)
        self.output = FieldRow("Save as", "notes_edited.pdf", "")
        self.output.browse.clicked.connect(self._choose_output)
        row.addWidget(self.output, 1)
        self.save = primary_button(self.action_label, self.action_icon, self._save,
                                   "Write the changes to a new PDF")
        row.addWidget(self.save, 0, Qt.AlignmentFlag.AlignBottom)
        bottom.add_layout(row)
        layout.addWidget(bottom)

        self.result = ResultCard()
        layout.addWidget(self.result)

        self._install_canvas_shortcuts()
        if not editor.available():
            self.canvas.set_placeholder(
                "The page editor needs PyMuPDF.\n\n"
                "Install it with:  python3 -m pip install PyMuPDF\n"
                "then reopen this tab.")
        self._sync()

    # -- construction ---------------------------------------------------
    def _build_toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setContentsMargins(2, 0, 2, 0)
        bar.setSpacing(6)

        self.select_tool = tool_toggle(
            "Select", "cursor", lambda _=False: self._set_tool("select"), True,
            "Select, move, resize or rotate")
        self.text_tool = tool_toggle(
            "Text", "text", lambda _=False: self._set_tool("text"), False,
            "Drag on the page to add a text box — Sinhala typing works here")
        shape_tip = "Drag on the page to draw a shape (transparent or opaque)"
        try:
            self.shape_tool = tool_toggle(
                "Shape", "shape", lambda _=False: self._set_tool("shape"), False, shape_tip)
        except Exception:  # noqa: BLE001 - icon set has no "shape" glyph
            self.shape_tool = tool_toggle(
                "Shape", "", lambda _=False: self._set_tool("shape"), False, shape_tip)
        self.shape_kind = QComboBox()
        self.shape_kind.setMinimumHeight(34)
        self.shape_kind.setToolTip("Which shape to draw")
        for kind, title in editor.SHAPE_KINDS:
            self.shape_kind.addItem(title, kind)
        self.shape_kind.activated.connect(self._shape_kind_picked)
        self.image_button = soft_button("Add image", "image", self._add_image,
                                        "Place an image on the current page")
        bar.addWidget(self.select_tool)
        bar.addWidget(self.text_tool)
        bar.addWidget(self.shape_tool)
        bar.addWidget(self.shape_kind)
        bar.addWidget(self.image_button)

        bar.addSpacing(8)
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setFrameShadow(QFrame.Shadow.Plain)
        separator.setStyleSheet(f"color: {T.BORDER};")
        separator.setFixedHeight(24)
        bar.addWidget(separator)
        bar.addSpacing(2)

        self.undo_button = icon_button("undo", self.undo, "Undo (Ctrl+Z)")
        self.redo_button = icon_button("redo", self.redo, "Redo (Ctrl+Y)")
        bar.addWidget(self.undo_button)
        bar.addWidget(self.redo_button)
        bar.addStretch(1)

        self.prev_page = icon_button("arrow_left", lambda: self._step_page(-1),
                                     "Previous page")
        self.page_badge = Badge("0 / 0", "neutral")
        self.next_page = icon_button("arrow_right", lambda: self._step_page(1),
                                     "Next page")
        bar.addWidget(self.prev_page)
        bar.addWidget(self.page_badge)
        bar.addWidget(self.next_page)
        bar.addSpacing(8)

        bar.addWidget(icon_button("zoom_out", lambda: self._zoom_by(-1), "Zoom out"))
        self.zoom_label = label("100%", 12, 600, T.TEXT_MUTED)
        self.zoom_label.setFixedWidth(42)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bar.addWidget(self.zoom_label)
        bar.addWidget(icon_button("zoom_in", lambda: self._zoom_by(1), "Zoom in"))
        bar.addWidget(ghost_button("1:1", "", lambda: self._set_zoom(1.0),
                                   "Actual size"))
        bar.addWidget(icon_button("fit", self._fit_view, "Fit page to the window"))
        return bar

    def _install_canvas_shortcuts(self) -> None:
        QShortcut(QKeySequence.StandardKey.Undo, self.canvas, activated=self.undo)
        QShortcut(QKeySequence.StandardKey.Redo, self.canvas, activated=self.redo)
        QShortcut(QKeySequence("Ctrl+Y"), self.canvas, activated=self.redo)

    # -- source handling ------------------------------------------------
    def _refresh_source(self) -> None:
        info = self.info
        new_path = str(info.path) if info is not None else ""
        current = self._session.path if self._session is not None else ""

        if not editor.available():
            self._detach_session()
            self._clear_view()
            self.using.set_info(info)
            self.using.set_detail("Install PyMuPDF to use the page editor.")
            return

        if new_path and new_path == current:
            self.using.set_info(info)
            self._update_using()
            return

        self._detach_session()
        if not new_path:
            self._clear_view()
            self.using.set_info(None)
            self.using.set_detail("Pick a file in the top bar to edit a page.")
            return

        session = self._sessions.get(new_path)
        if session is None:
            session = EditorSession(path=new_path, pages=max(1, info.pages))
            session.output = f"{Path(new_path).stem}_edited.pdf"
            self._sessions[new_path] = session
        self._session = session
        self.using.set_info(info)
        self.rail.set_pages(session.pages)
        if session.thumbs:
            self.rail.set_thumbs(new_path, session.thumbs)
        else:
            self._load_thumbs()
        self.output.set_text(session.output)
        self._open_page(session.page, initial=True)
        self._update_using()

    def _detach_session(self) -> None:
        self.canvas.commit_inline()
        self._render_timer.stop()
        self._render_token += 1
        self._session = None

    def _clear_view(self) -> None:
        self.rail.clear()
        self.canvas.clear_render()
        self.canvas.set_placeholder(
            "Open a PDF in the top bar, then click a line of text or an image.")
        self.inspector.show_object(None, "")
        self.page_badge.setText("0 / 0")
        self.save.setEnabled(False)
        self._set_tool_buttons("select")
        self._sync_tools()

    def _load_thumbs(self) -> None:
        session = self._session
        if session is None:
            return
        worker = ThumbnailWorker(session.path)
        self._rail_worker = worker
        worker.signals.ready.connect(self._on_thumbs)
        worker.signals.failed.connect(lambda _msg: None)
        QThreadPool.globalInstance().start(worker)

    def _on_thumbs(self, path: str, images) -> None:
        session = self._sessions.get(path)
        if session is None:
            return
        session.thumbs = list(images or [])
        if self._session is session:
            self.rail.set_thumbs(path, session.thumbs)

    # -- pages ----------------------------------------------------------
    def _open_page(self, index: int, initial: bool = False) -> None:
        session = self._session
        if session is None or not (0 <= index < session.pages):
            return
        self.canvas.commit_inline()
        if index not in session.states:
            try:
                data = editor.page_data(session.path, index)
            except Exception as exc:  # noqa: BLE001
                self.window.toasts.notify("Could not open page", str(exc), "error", 6000)
                return
            session.states[index] = editor.PageState.from_page_data(data)
            session.sizes[index] = (data.width, data.height)
            try:
                problem = editor.legacy.missing_mapping_report()
            except Exception:  # noqa: BLE001
                problem = ""
            if problem:
                self.window.toasts.notify(
                    "Sinhala text can't be decoded", problem, "error", 12000)
        session.page = index
        self.canvas.set_state(session.states[index])
        size = session.sizes.get(index)
        if size:
            self.canvas.set_page_size(*size)
        self.canvas.select("")
        self.rail.set_current(index)
        self.inspector.show_object(None, session.path)
        self.page_badge.setText(f"{index + 1} / {session.pages}")
        self._sync_tools()
        if initial:
            QTimer.singleShot(0, self._fit_view)
        self._request_render(immediate=True)

    def _step_page(self, delta: int) -> None:
        if self._session is None:
            return
        self._open_page(self._session.page + int(delta))

    # -- rendering ------------------------------------------------------
    def _request_render(self, immediate: bool = False) -> None:
        if self._session is None:
            return
        if immediate:
            self._render_timer.stop()
            self._render_now()
        else:
            self._render_timer.start()

    def _render_now(self) -> None:
        session = self._session
        if session is None:
            return
        state = session.states.get(session.page)
        snapshot = copy.deepcopy(state) if state is not None else None
        self._render_token += 1
        token = self._render_token
        worker = RenderWorker(token, session.path, session.page, session.zoom, snapshot)
        self._render_worker = worker
        worker.signals.ready.connect(self._render_ready)
        worker.signals.failed.connect(self._render_failed)
        QThreadPool.globalInstance().start(worker)

    def _render_ready(self, token: int, result) -> None:
        session = self._session
        if session is None or token != self._render_token:
            return
        self._last_render_error = ""
        self.canvas.set_render(result.png, result.width, result.height,
                               result.page_width, result.page_height, session.zoom)

    def _render_failed(self, token: int, message: str) -> None:
        if token != self._render_token or message == self._last_render_error:
            return
        self._last_render_error = message
        self.window.toasts.notify("Could not render the page", message, "error", 5000)

    # -- zoom -----------------------------------------------------------
    def _set_zoom(self, value: float) -> None:
        session = self._session
        if session is None:
            return
        session.zoom = max(0.15, min(5.0, float(value)))
        self.canvas.set_zoom(session.zoom)
        self.zoom_label.setText(f"{round(session.zoom * 100)}%")
        self._request_render(immediate=True)

    def _zoom_by(self, direction: int) -> None:
        session = self._session
        if session is None:
            return
        factor = 1.18 if direction > 0 else 1 / 1.18
        self._set_zoom(session.zoom * factor)

    def _fit_view(self) -> None:
        session = self._session
        if session is None:
            return
        size = session.sizes.get(session.page)
        if not size:
            return
        viewport = self.scroll.viewport().size()
        avail_w = viewport.width() - 48
        avail_h = viewport.height() - 48
        if avail_w < 80 or avail_h < 80:
            QTimer.singleShot(60, self._fit_view)
            return
        zoom = min(avail_w / max(1.0, size[0]), avail_h / max(1.0, size[1]))
        self._set_zoom(max(0.15, min(3.0, zoom)))

    # -- tools and selection --------------------------------------------
    def _set_tool(self, tool: str) -> None:
        self._set_tool_buttons(tool)
        self.canvas.set_tool(tool)
        if tool == "shape":
            self.canvas.set_shape_kind(self.shape_kind.currentData() or "rect")
            self.canvas.setFocus()
            self.window.hint_bar("Drag on the page to draw the shape - or just click "
                                 "for a default size", "info")
        elif tool == "text":
            self.canvas.setFocus()
            self.window.hint_bar("Drag on the page to size a new text box", "info")
        else:
            self.window.hint_bar("")

    def _shape_kind_picked(self, _index: int) -> None:
        self._set_tool("shape")

    def _set_tool_buttons(self, tool: str) -> None:
        self.select_tool.setChecked(tool == "select")
        self.text_tool.setChecked(tool == "text")
        self.shape_tool.setChecked(tool == "shape")

    def _on_selection(self, key: str) -> None:
        session = self._session
        if session is None:
            self.inspector.show_object(None, "")
            return
        state = session.states.get(session.page)
        obj = state.find(key) if state is not None and key else None
        self.inspector.show_object(obj, session.path)

    def _refresh_inspector(self) -> None:
        session = self._session
        if session is None:
            return
        obj = self.canvas.selected_object()
        self.inspector.show_object(obj, session.path)

    def _on_canvas_changed(self) -> None:
        self.canvas.update()
        self._request_render()
        self._sync_tools()

    def _on_canvas_commit(self, _reason: str) -> None:
        self._refresh_inspector()
        self._refresh_marks()
        self._update_using()
        self._request_render(immediate=True)

    def _on_inspector_changed(self) -> None:
        self.canvas.remember_shape_style(self.canvas.selected_object())
        self.canvas.update()
        self._request_render()
        self._refresh_marks()
        self._update_using()
        self._sync_tools()

    def _delete_selected(self) -> None:
        self.canvas.delete_selected()

    # -- adding content -------------------------------------------------
    def _add_image(self) -> None:
        session = self._session
        if session is None:
            self.window.toasts.notify("Open a PDF first",
                                      "Pick a file in the top bar", "warning")
            return
        self.canvas.commit_inline()
        suffixes = " ".join(f"*{ext}" for ext in sorted(core.IMAGE_SUFFIXES))
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose an image", self.window.last_dir(),
            f"Images ({suffixes});;All files (*)")
        if not path:
            return
        width, height = editor.image_dimensions(path)
        page_w, page_h = session.sizes.get(session.page, (595.0, 842.0))
        if width > 0 and height > 0:
            scale = min((page_w * 0.55) / width, (page_h * 0.45) / height, 1.0)
            draw_w = max(24.0, width * scale)
            draw_h = max(24.0, height * scale)
        else:
            draw_w, draw_h = page_w * 0.45, page_h * 0.35
        left = (page_w - draw_w) / 2
        top = (page_h - draw_h) / 2
        self._begin_change("add-image")
        state = session.states.get(session.page)
        if state is None:
            return
        obj = state.add_image((left, top, left + draw_w, top + draw_h),
                              path, width, height, keep_proportion=True)
        self.canvas.select(obj.key)
        self.inspector.show_object(obj, session.path)
        self._refresh_marks()
        self._update_using()
        self._request_render(immediate=True)
        self._sync_tools()

    def _replace_image(self) -> None:
        obj = self.canvas.selected_object()
        if not isinstance(obj, editor.ImageObject):
            return
        suffixes = " ".join(f"*{ext}" for ext in sorted(core.IMAGE_SUFFIXES))
        path, _ = QFileDialog.getOpenFileName(
            self, "Replace image", self.window.last_dir(),
            f"Images ({suffixes});;All files (*)")
        if not path:
            return
        self._begin_change("replace-image")
        width, height = editor.image_dimensions(path)
        obj.replacement_path = path
        obj.width = width or obj.width
        obj.height = height or obj.height
        self.inspector.show_object(obj, self._session.path if self._session else "")
        self._refresh_marks()
        self._update_using()
        self._request_render(immediate=True)

    # -- history --------------------------------------------------------
    def _snapshot(self):
        if self._session is None:
            return {}
        return {n: copy.deepcopy(state) for n, state in self._session.states.items()}

    def _begin_change(self, tag: str) -> None:
        if self._session is None:
            return
        if tag == self._change_tag and self._change_timer.isActive():
            return
        self._session.history.append(self._snapshot())
        if len(self._session.history) > 80:
            self._session.history.pop(0)
        self._session.future.clear()
        self._change_tag = tag
        self._change_timer.start(1200)
        self._sync_tools()

    def _clear_change_tag(self) -> None:
        self._change_tag = ""

    def undo(self) -> None:
        session = self._session
        if session is None or not session.history:
            return
        self.canvas.commit_inline()
        session.future.append(self._snapshot())
        session.states = session.history.pop()
        self._after_history()

    def redo(self) -> None:
        session = self._session
        if session is None or not session.future:
            return
        self.canvas.commit_inline()
        session.history.append(self._snapshot())
        session.states = session.future.pop()
        self._after_history()

    def _after_history(self) -> None:
        session = self._session
        if session is None:
            return
        self._change_tag = ""
        self._change_timer.stop()
        self._render_token += 1
        state = session.states.get(session.page)
        if state is None:
            self._open_page(session.page)
        else:
            self.canvas.set_state(state)
            self.canvas.select("")
            self.inspector.show_object(None, session.path)
            self._request_render(immediate=True)
        self._refresh_marks()
        self._update_using()
        self._sync_tools()

    # -- saving ---------------------------------------------------------
    def _choose_output(self) -> None:
        suggested = self.output.text() or "edited.pdf"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save edited PDF", str(Path(self.window.last_dir()) / suggested),
            "PDF files (*.pdf)")
        if path:
            self.output.set_text(path)
            if self._session is not None:
                self._session.output = path

    def _save(self) -> None:
        session = self._session
        if session is None:
            return
        if not session.modified():
            self.window.toasts.notify("Nothing to save", "Make a change first", "info")
            return
        self.canvas.commit_inline()
        target = self.output.text().strip()
        if not target:
            self._choose_output()
            target = self.output.text().strip()
            if not target:
                return
        session.output = target
        states = {n: copy.deepcopy(state) for n, state in session.states.items()
                  if state.has_changes}
        self.result.hide_result()
        self.run_job(editor.save_edits, session.path, target, states,
                     title="Saved", on_done=self._on_saved)

    def _on_saved(self, result) -> None:
        if self._session is not None:
            self._session.output = str(result.output)
        self.result.show_result(f"Created {result.output.name}", result.detail,
                                result.size_text, result.saved, result.output)

    # -- status ---------------------------------------------------------
    def _refresh_marks(self) -> None:
        session = self._session
        if session is None:
            return
        for index in range(session.pages):
            state = session.states.get(index)
            self.rail.set_modified(index, bool(state and state.has_changes))

    def _update_using(self) -> None:
        session = self._session
        if session is None:
            return
        edited = sum(1 for state in session.states.values() if state.has_changes)
        if edited:
            detail = (f"{session.pages} pages  ·  {edited} page"
                      f"{'s' if edited != 1 else ''} with unsaved changes")
        else:
            detail = f"{session.pages} pages  ·  select text, an image or a shape to edit it"
        self.using.set_detail(detail)

    def _sync_tools(self) -> None:
        session = self._session
        has = session is not None
        self.undo_button.setEnabled(has and bool(session.history))
        self.redo_button.setEnabled(has and bool(session.future))
        self.prev_page.setEnabled(has and session.page > 0)
        self.next_page.setEnabled(has and session.page < session.pages - 1)
        self.select_tool.setEnabled(has)
        self.text_tool.setEnabled(has)
        self.shape_tool.setEnabled(has)
        self.shape_kind.setEnabled(has)
        self.image_button.setEnabled(has)
        self.save.setEnabled(has and session.modified())

    def _sync(self) -> None:
        session = self._session
        has = session is not None
        self.output.setEnabled(has)
        self._sync_tools()
        if session is not None:
            zoom = round(session.zoom * 100)
            self.zoom_label.setText(f"{zoom}%")
            self.page_badge.setText(f"{session.page + 1} / {session.pages}")
        else:
            self.zoom_label.setText("100%")
            self.page_badge.setText("0 / 0")


@dataclass
class EditorSession:
    """Edits and view state kept while the app is open, one per document."""

    path: str
    pages: int
    states: dict[int, editor.PageState] = field(default_factory=dict)
    sizes: dict[int, tuple[float, float]] = field(default_factory=dict)
    history: list[dict[int, editor.PageState]] = field(default_factory=list)
    future: list[dict[int, editor.PageState]] = field(default_factory=list)
    page: int = 0
    zoom: float = 1.0
    thumbs: list | None = None
    output: str = ""

    def modified(self) -> bool:
        return any(state.has_changes for state in self.states.values())


# ---------------------------------------------------------------- window
TOOLS = [
    ("Workspace", "merge", "Merge PDFs", MergePanel),
    ("Workspace", "edit", "Edit page", EditorPanel),
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
        self.source = SourceBar(self)

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
        host_layout.addWidget(self.stack, 1)
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
        header.setFixedHeight(152)

        column = QVBoxLayout(header)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        row = QHBoxLayout()
        row.setContentsMargins(30, 14, 30, 6)
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
        column.addLayout(row)

        column.addWidget(self.source)
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
        self.source.add_paths(paths)

    def closeEvent(self, event):
        if self.busy_flag:
            self.toasts.notify("Still working", "Wait for the job to finish", "warning")
            event.ignore()
            return
        info = self.source.current()
        if info is not None:
            self.settings.setValue("last_dir", str(info.path.parent))
        QThreadPool.globalInstance().waitForDone(2500)
        event.accept()


def _describe(factory) -> str:
    return (factory.subtitle.split(".")[0] if factory.subtitle else "").strip()


def main() -> int:
    QApplication.setApplicationName(APP_NAME)
    QApplication.setOrganizationName("pdf-tools")
    QApplication.setStyle("Fusion")

    app = QApplication(sys.argv)
    # Qt on Windows repeats the first letter typed with Sinhala keyboards
    # (injected VK_PACKET text); this routes that text around the bug.
    win_text_input.install(app)
    for bundled in (editor.BUNDLED_SINHALA_REGULAR, editor.BUNDLED_SINHALA_BOLD):
        if bundled.exists():
            QFontDatabase.addApplicationFont(str(bundled))
    # Always set a concrete family.  Leaving the family empty lets Qt/GDI pick
    # a platform default, which on Windows can be a script face.
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
