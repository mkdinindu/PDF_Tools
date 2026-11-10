#!/usr/bin/env python3
"""Widgets for the visual PDF page editor.

The engine lives in :mod:`pdf_editor_core`; this module is the Qt side:
a page rail, an editing canvas with selection handles, and an inspector.
Keeping them here leaves ``gui.py`` as the shell that wires tools together.
"""

from __future__ import annotations

import copy
import math
from pathlib import Path

from PyQt6.QtCore import (
    QEvent,
    QObject,
    QPointF,
    QRectF,
    QRunnable,
    QSize,
    QSizeF,
    Qt,
    QThreadPool,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
from PyQt6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QColorDialog,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QSizePolicy,
    QSlider,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import pdf_editor_core as ec
import theme as T
from widgets import (
    Badge,
    Button,
    Card,
    SegmentedControl,
    ToggleSwitch,
    danger_button,
    divider,
    icon_button,
    label,
    soft_button,
)

CANVAS_MARGIN = 30
HANDLE_SIZE = 8.0
ROTATE_OFFSET = 26.0

#: Cache of font-file path -> Qt family name (registering a file is expensive).
_FONT_FAMILIES: dict[str, str] = {}


def font_family_for(path: str) -> str:
    """Register a font file with Qt and return its family name, or ''.

    The family is used to give the on-canvas editor the same face as the
    text object instead of relying on an opaque platform fallback.
    """
    path = str(path or "")
    if not path:
        return ""
    if path in _FONT_FAMILIES:
        return _FONT_FAMILIES[path]
    family = ""
    if Path(path).is_file():
        try:
            font_id = QFontDatabase.addApplicationFont(path)
            names = (QFontDatabase.applicationFontFamilies(font_id)
                     if font_id >= 0 else [])
            family = names[0] if names else ""
        except Exception:  # noqa: BLE001
            family = ""
    _FONT_FAMILIES[path] = family
    return family


def editor_font_families(font_path: str) -> list[str]:
    """Preferred families for a text editor: object face, Sinhala, then UI."""
    families: list[str] = []
    for family in (
        font_family_for(font_path),
        font_family_for(ec.default_sinhala_font()),
        T.ui_font_family(),
    ):
        family = (family or "").strip()
        if family and family not in families:
            families.append(family)
    return families


def font_family_css(font_path: str) -> str:
    """A QSS ``font-family`` value with per-glyph fallbacks."""
    families = editor_font_families(font_path)
    return ", ".join(f'"{family}"' for family in families)


def fit_font_to_text(obj) -> None:
    """Use a face that can actually draw the object's text.

    New text boxes start with the bundled Sinhala face, which has no Latin
    glyphs.  MuPDF then substitutes whatever the OS offers for Latin (which
    is why English could look serif/cursive).  Switching to the matching
    bundled/system face keeps the preview predictable.
    """
    text = str(getattr(obj, "text", "") or "")
    if not text.strip() or ec.font_covers(getattr(obj, "font_path", ""), text):
        return
    candidate = ec.default_font_for(text)
    if (candidate and candidate != obj.font_path
            and ec.font_covers(candidate, text)):
        obj.font_path = candidate


# ------------------------------------------------------------------ workers
class RenderSignals(QObject):
    ready = pyqtSignal(int, object)
    failed = pyqtSignal(int, str)


class RenderWorker(QRunnable):
    """Renders one page (with pending edits) off the UI thread."""

    def __init__(self, token: int, path: str, index: int, zoom: float,
                 state: "ec.PageState | None"):
        super().__init__()
        self.signals = RenderSignals()
        self.token = token
        self.path = path
        self.index = index
        self.zoom = zoom
        self.state = state

    def run(self):
        try:
            result = ec.render_page(self.path, self.index, self.zoom, self.state)
        except Exception as exc:  # noqa: BLE001
            self.signals.failed.emit(self.token, str(exc))
        else:
            self.signals.ready.emit(self.token, result)


class RailSignals(QObject):
    ready = pyqtSignal(str, object)
    failed = pyqtSignal(str)


class ThumbnailWorker(QRunnable):
    """Builds the small page previews for the rail in one document open."""

    def __init__(self, path: str, max_width: int = 92):
        super().__init__()
        self.signals = RailSignals()
        self.path = path
        self.max_width = max_width

    def run(self):
        try:
            images = ec.render_thumbnails(self.path, self.max_width)
        except Exception as exc:  # noqa: BLE001
            self.signals.failed.emit(str(exc))
        else:
            self.signals.ready.emit(self.path, images)


# ------------------------------------------------------------------ helpers
def tool_toggle(text: str, icon: str, on_click=None, checked: bool = False,
                tooltip: str = "") -> Button:
    """Checkable toolbar button with the editor's accent when active."""
    button = Button(text, "soft", icon, tooltip=tooltip)
    button.setObjectName("ToolToggle")
    button.setCheckable(True)
    button.setChecked(checked)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if on_click is not None:
        button.clicked.connect(on_click)
    return button


def _rgb(color) -> tuple[int, int, int]:
    if isinstance(color, QColor):
        return (color.red(), color.green(), color.blue())
    return tuple(int(c) for c in color[:3])


class ColorButton(QWidget):
    """Small colour swatch with its hex value written inside."""

    clicked = pyqtSignal()

    def __init__(self, color=(17, 24, 39), parent: QWidget | None = None):
        super().__init__(parent)
        self._color = QColor(*_rgb(color))
        self.setFixedHeight(34)
        self.setMinimumWidth(96)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def color(self) -> tuple[int, int, int]:
        return (self._color.red(), self._color.green(), self._color.blue())

    def set_color(self, color) -> None:
        self._color = QColor(*_rgb(color))
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setBrush(self._color)
        painter.setPen(QPen(QColor(T.BORDER_STRONG), 1))
        painter.drawRoundedRect(rect, 9, 9)
        luminance = (0.299 * self._color.red() + 0.587 * self._color.green()
                     + 0.114 * self._color.blue())
        painter.setPen(QColor(T.TEXT if luminance > 160 else "#FFFFFF"))
        font = QFont(T.ui_font_family() or "Sans Serif")
        font.setPixelSize(11)
        font.setWeight(QFont.Weight(600))
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._color.name().upper())
        painter.end()


class ImagePreview(QWidget):
    """Centres and scales a preview pixmap without distorting it."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._pixmap = QPixmap()
        self.setMinimumHeight(128)
        self.setMaximumHeight(170)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def set_data(self, data: bytes) -> None:
        pixmap = QPixmap()
        if data:
            pixmap.loadFromData(data)
        self._pixmap = pixmap
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        painter.setPen(QPen(QColor(T.BORDER), 1))
        painter.setBrush(QColor(T.SURFACE_SUNKEN))
        painter.drawRoundedRect(rect, 9, 9)
        if self._pixmap.isNull():
            painter.setPen(QColor(T.TEXT_SUBTLE))
            font = QFont(T.ui_font_family() or "Sans Serif")
            font.setPixelSize(12)
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "No preview")
        else:
            inner = rect.adjusted(7, 7, -7, -7)
            scaled = self._pixmap.scaled(
                inner.size().toSize(), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            target = QRectF(0, 0, scaled.width(), scaled.height())
            target.moveCenter(inner.center())
            painter.drawPixmap(target.topLeft(), scaled)
        painter.end()


class InlineEditor(QPlainTextEdit):
    """A text box drawn on top of the page canvas."""

    finished = pyqtSignal(bool, str)

    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._done = False
        self._closing = False
        self._app_active = True
        self._filtering = False
        self._forwarding = False
        self._focus_timer = QTimer(self)
        self._focus_timer.setSingleShot(True)
        self._focus_timer.setInterval(0)
        self._focus_timer.timeout.connect(self._reclaim_focus)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
            self._filtering = True
        self.setObjectName("InlineEditor")
        self.setPlainText(text)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTabChangesFocus(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)

    def event(self, event):
        # QWidget::event turns Tab into a focus move before keyPressEvent is
        # called.  Catch it here so Tab finishes editing instead of bouncing
        # focus around on Windows.
        if (event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab)):
            self.keyPressEvent(event)
            return True
        return super().event(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            self._finish(False, self.toPlainText())
            return
        if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            event.accept()
            self._finish(True, self.toPlainText())
            return
        if event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            # Tab used to move focus, which closed the editor on Windows.
            # Commit explicitly instead.
            event.accept()
            self._finish(True, self.toPlainText())
            return
        super().keyPressEvent(event)

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress:
            if not (self._closing or self._done) and self.isVisible():
                app = QApplication.instance()
                if app is not None and app.activePopupWidget() is not None:
                    return False
                target = obj if isinstance(obj, QWidget) else None
                if target is None:
                    return False
                inside = target is self or self.isAncestorOf(target)
                same_window = target.window() is self.window()
                if not inside and same_window:
                    # A click in this window is the real signal that editing
                    # is done.  Clicks in menus/dialogs/other windows are not.
                    self._finish(True, self.toPlainText())
        elif kind == QEvent.Type.KeyPress:
            return self._forward_escaped_key(obj, event)
        elif kind == QEvent.Type.ApplicationDeactivate:
            self._app_active = False
        elif kind == QEvent.Type.ApplicationActivate:
            self._app_active = True
            if not (self._closing or self._done) and self.isVisible():
                self._focus_timer.start()
        return False

    def _forward_escaped_key(self, obj, event) -> bool:
        """Send a key to the editor when Windows moved focus somewhere else.

        Focus reclaim (``_reclaim_focus``) usually runs before the next key,
        but on Windows the focus can land on a plain container widget and the
        key would be dropped there.  Forward printable keys and the editing
        keys instead of losing them.  Shortcuts (Ctrl/Alt/Meta) are left alone
        so Save/Open/Undo still reach the application.
        """
        if (self._forwarding or self._closing or self._done
                or not self.isVisible()):
            return False
        target = obj if isinstance(obj, QWidget) else None
        if target is None or target.window() is not self.window():
            return False
        if target is self or self.isAncestorOf(target):
            return False
        # The user may legitimately be typing in another text field (on a
        # dialog, for example).  Never take those keys away from it.
        if isinstance(target, (QLineEdit, QAbstractSpinBox,
                               QTextEdit, QPlainTextEdit)):
            return False
        app = QApplication.instance()
        if app is None:
            return False
        if app.activePopupWidget() is not None or app.activeModalWidget() is not None:
            return False
        ctrl_enter = (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                      and bool(event.modifiers()
                               & Qt.KeyboardModifier.ControlModifier))
        if not ctrl_enter and event.modifiers() & (
                Qt.KeyboardModifier.ControlModifier
                | Qt.KeyboardModifier.AltModifier
                | Qt.KeyboardModifier.MetaModifier):
            return False
        key = event.key()
        if key in (Qt.Key.Key_Shift, Qt.Key.Key_Control,
                   Qt.Key.Key_Alt, Qt.Key.Key_Meta):
            return False
        editing_keys = (
            Qt.Key.Key_Backspace, Qt.Key.Key_Delete,
            Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down,
            Qt.Key.Key_Home, Qt.Key.Key_End, Qt.Key.Key_PageUp,
            Qt.Key.Key_PageDown, Qt.Key.Key_Return, Qt.Key.Key_Enter,
            Qt.Key.Key_Escape, Qt.Key.Key_Tab, Qt.Key.Key_Backtab,
        )
        if not event.text() and key not in editing_keys:
            return False
        self._forwarding = True
        try:
            self.setFocus(Qt.FocusReason.OtherFocusReason)
            self.keyPressEvent(event)
        finally:
            self._forwarding = False
        return True

    def focusInEvent(self, event):
        self._focus_timer.stop()
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        if self._closing or self._done:
            return
        # Windows can move focus away for a moment (IME, window activation,
        # style polish) or even permanently after the first keystroke.  Do not
        # commit here: keep the editor alive and take focus back.  Real exits
        # are handled by clicks outside (eventFilter), Tab, Escape and
        # Ctrl+Enter.
        self._focus_timer.start()

    def _reclaim_focus(self) -> None:
        if self._closing or self._done or not self.isVisible():
            return
        if not self._app_active or not self.window().isActiveWindow():
            return
        app = QApplication.instance()
        if app is None:
            return
        if app.activePopupWidget() is not None or app.activeModalWidget() is not None:
            # A menu/dialog/IME candidate is open; do not pull focus from it.
            return
        focus = app.focusWidget()
        if focus is self:
            return
        if focus is not None and focus.window() is not self.window():
            # Focus is in another window (dialog or popup).
            return
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def close_editor(self) -> None:
        """Mark this editor as intentionally closing (skips focus handling)."""
        self._closing = True
        self._focus_timer.stop()
        self._remove_filter()
        self.hide()

    def _remove_filter(self) -> None:
        if not self._filtering:
            return
        self._filtering = False
        app = QApplication.instance()
        if app is not None:
            try:
                app.removeEventFilter(self)
            except Exception:  # noqa: BLE001
                pass

    def _finish(self, ok: bool, text: str) -> None:
        if self._done:
            return
        self._done = True
        self._closing = True
        self._focus_timer.stop()
        self._remove_filter()
        self.finished.emit(ok, text)


# ------------------------------------------------------------------ canvas
def obj_reverse(drag: dict, obj) -> bool:
    """Whether the line's tail is its right-hand end when the drag began."""
    ends = drag.get("ends")
    if not ends:
        return False
    (tx, ty), (hx, hy) = ends
    return (tx, ty) > (hx, hy)


class EditorCanvas(QWidget):
    """Rendered page with draggable / resizable / rotatable objects on top."""

    selection_changed = pyqtSignal(str)
    before_change = pyqtSignal(str)
    changed = pyqtSignal()
    committed = pyqtSignal(str)
    zoom_delta = pyqtSignal(int)
    status = pyqtSignal(str)
    tool_finished = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setMinimumSize(280, 280)
        self._pixmap = QPixmap()
        self._page_w = 595.0
        self._page_h = 842.0
        self._zoom = 1.0
        self._state: ec.PageState | None = None
        self._selected = ""
        self._tool = "select"
        self._hover = ""
        self._drag: dict | None = None
        self._new_rect: QRectF | None = None
        self._inline: tuple[str, InlineEditor] | None = None
        self._placeholder = "Open a PDF to start editing"
        self._default_font = ""
        self._default_size = 14.0
        self._default_color = (17, 24, 39)
        self._shape_kind = "rect"
        self._shape_style = {
            "fill": (59, 130, 246), "fill_opacity": 0.35,
            "stroke": (30, 64, 175), "stroke_opacity": 1.0, "stroke_width": 2.0,
        }
        self._applied_size()

    # -- state ---------------------------------------------------------
    def set_placeholder(self, text: str) -> None:
        self._placeholder = text
        self.update()

    def set_state(self, state: ec.PageState | None) -> None:
        self._finish_inline(True, None)
        self._drag = None
        self._new_rect = None
        self._state = state
        if state is not None and self._selected:
            if state.find(self._selected) is None:
                self._selected = ""
                self.selection_changed.emit("")

    def state(self) -> ec.PageState | None:
        return self._state

    def set_page_size(self, width: float, height: float) -> None:
        self._page_w = max(1.0, float(width))
        self._page_h = max(1.0, float(height))
        self._applied_size()

    def set_render(self, png: bytes, width: int, height: int,
                   page_width: float, page_height: float, zoom: float) -> None:
        pixmap = QPixmap()
        if png:
            pixmap.loadFromData(png)
        self._pixmap = pixmap
        self._zoom = float(zoom)
        self._page_w = float(page_width)
        self._page_h = float(page_height)
        self._applied_size()
        self.update()

    def clear_render(self) -> None:
        self._pixmap = QPixmap()
        self._state = None
        self._selected = ""
        self._hover = ""
        self._drag = None
        self._new_rect = None
        self._finish_inline(False, None)
        self._applied_size()
        self.update()

    def set_zoom(self, zoom: float) -> None:
        self._zoom = max(0.1, min(6.0, float(zoom)))
        self._applied_size()

    def zoom(self) -> float:
        return self._zoom

    def set_tool(self, tool: str) -> None:
        self._tool = tool
        self._drag = None
        self._new_rect = None
        self.setCursor(Qt.CursorShape.CrossCursor if tool != "select"
                       else Qt.CursorShape.ArrowCursor)
        self.update()

    def set_defaults(self, font_path: str, size: float,
                     color: tuple[int, int, int]) -> None:
        self._default_font = font_path
        self._default_size = float(size)
        self._default_color = tuple(color)

    def set_shape_kind(self, kind: str) -> None:
        self._shape_kind = kind

    def remember_shape_style(self, obj) -> None:
        """New shapes reuse the look of the last shape the user styled."""
        if isinstance(obj, ec.ShapeObject):
            self._shape_style = {
                "fill": tuple(obj.fill), "fill_opacity": float(obj.fill_opacity),
                "stroke": tuple(obj.stroke),
                "stroke_opacity": float(obj.stroke_opacity),
                "stroke_width": float(obj.stroke_width),
            }

    def selected_object(self):
        if self._state is None or not self._selected:
            return None
        obj = self._state.find(self._selected)
        if obj is None or getattr(obj, "deleted", False):
            return None
        return obj

    def select(self, key: str) -> None:
        if key == self._selected:
            self.update()
            return
        self._selected = key
        self.selection_changed.emit(key)
        self.update()

    def _applied_size(self) -> None:
        width = self._page_w * self._zoom + CANVAS_MARGIN * 2
        height = self._page_h * self._zoom + CANVAS_MARGIN * 2
        self.setFixedSize(int(max(280, round(width))), int(max(280, round(height))))

    # -- geometry ------------------------------------------------------
    def _page_rect_widget(self) -> QRectF:
        return QRectF(CANVAS_MARGIN, CANVAS_MARGIN,
                      self._page_w * self._zoom, self._page_h * self._zoom)

    def _widget_rect(self, rect) -> QRectF:
        x0, y0, x1, y1 = rect
        left = CANVAS_MARGIN + min(x0, x1) * self._zoom
        top = CANVAS_MARGIN + min(y0, y1) * self._zoom
        right = CANVAS_MARGIN + max(x0, x1) * self._zoom
        bottom = CANVAS_MARGIN + max(y0, y1) * self._zoom
        return QRectF(left, top, max(1.0, right - left), max(1.0, bottom - top))

    def _page_point(self, point: QPointF) -> QPointF:
        return QPointF((point.x() - CANVAS_MARGIN) / self._zoom,
                       (point.y() - CANVAS_MARGIN) / self._zoom)

    def _clamp_page_rect(self, rect: QRectF) -> QRectF:
        left = max(0.0, min(self._page_w - 4.0, rect.left()))
        top = max(0.0, min(self._page_h - 4.0, rect.top()))
        right = max(left + 4.0, min(self._page_w, rect.right()))
        bottom = max(top + 4.0, min(self._page_h, rect.bottom()))
        return QRectF(left, top, right - left, bottom - top)

    def _natural_text_size(self, obj) -> tuple[float, float]:
        """Width/height (page units) the text needs on one line per paragraph.

        Measured with Qt's shaper (HarfBuzz), so Sinhala conjuncts and
        pre-base vowels are counted the way they are drawn.
        """
        size = max(1.0, float(obj.font_size))
        family = font_family_for(str(obj.font_path or ""))
        scale = 8  # measure large, then divide: pixel sizes are integers
        font = QFont(family) if family else QFont()
        font.setPixelSize(max(1, int(round(size * scale))))
        metrics = QFontMetricsF(font)
        lines = str(obj.text or "").replace("\t", "    ").split("\n") or [""]
        width = max(metrics.horizontalAdvance(line) for line in lines) / scale
        width = width * 1.04 + 1.5          # small safety margin: no re-wrapping
        height = len(lines) * size * 1.35
        return width, height

    def _shift_into_page(self, rect: QRectF) -> QRectF:
        """Move *rect* inside the page without ever changing its size."""
        width = min(rect.width(), self._page_w)
        height = min(rect.height(), self._page_h)
        left = max(0.0, min(self._page_w - width, rect.left()))
        top = max(0.0, min(self._page_h - height, rect.top()))
        return QRectF(left, top, width, height)

    def _handles(self, rect: QRectF) -> dict[str, QPointF]:
        cx = rect.center().x()
        cy = rect.center().y()
        return {
            "tl": rect.topLeft(), "t": QPointF(cx, rect.top()), "tr": rect.topRight(),
            "l": QPointF(rect.left(), cy), "r": QPointF(rect.right(), cy),
            "bl": rect.bottomLeft(), "b": QPointF(cx, rect.bottom()),
            "br": rect.bottomRight(),
        }

    def _handle_points(self, obj, rect: QRectF) -> dict[str, QPointF]:
        """Lines/arrows get one handle per END; everything else gets eight."""
        if isinstance(obj, ec.ShapeObject) and obj.is_line:
            z = self._zoom
            (ax, ay), (bx, by) = ec.line_points(obj.rect, obj.flip)
            return {"p1": QPointF(CANVAS_MARGIN + ax * z, CANVAS_MARGIN + ay * z),
                    "p2": QPointF(CANVAS_MARGIN + bx * z, CANVAS_MARGIN + by * z)}
        return self._handles(rect)

    def _line_screen_points(self, obj):
        z = self._zoom
        (ax, ay), (bx, by) = ec.line_points(obj.rect, obj.flip, obj.reverse)
        return (QPointF(CANVAS_MARGIN + ax * z, CANVAS_MARGIN + ay * z),
                QPointF(CANVAS_MARGIN + bx * z, CANVAS_MARGIN + by * z))

    @staticmethod
    def _near_segment(point: QPointF, a: QPointF, b: QPointF, reach: float) -> bool:
        abx, aby = b.x() - a.x(), b.y() - a.y()
        length2 = abx * abx + aby * aby
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, (
            (point.x() - a.x()) * abx + (point.y() - a.y()) * aby) / length2))
        cx, cy = a.x() + t * abx, a.y() + t * aby
        return math.hypot(point.x() - cx, point.y() - cy) <= reach

    def _rotate_point(self, rect: QRectF) -> QPointF:
        return QPointF(rect.center().x(), rect.top() - ROTATE_OFFSET)

    def _handle_at(self, point: QPointF) -> str:
        obj = self.selected_object()
        if obj is None:
            return ""
        rect = self._widget_rect(obj.rect)
        # On a small box the grab zones would overlap and swallow the whole
        # box, so shrink them; and always pick the NEAREST handle, not the
        # first one in the list.
        reach = max(5.0, min(HANDLE_SIZE + 4.0,
                             min(rect.width(), rect.height()) / 2.5))
        if isinstance(obj, ec.ShapeObject):
            reach = HANDLE_SIZE + 4.0     # shapes: always an easy grab
        best, best_dist = "", reach + 1.0
        for name, handle in self._handle_points(obj, rect).items():
            dist = math.hypot(point.x() - handle.x(), point.y() - handle.y())
            if dist <= reach and dist < best_dist:
                best, best_dist = name, dist
        if best:
            return best
        if isinstance(obj, ec.ImageObject):
            if (point - self._rotate_point(rect)).manhattanLength() <= HANDLE_SIZE + 5:
                return "rotate"
        return ""

    def _hit_test(self, point: QPointF):
        if self._state is None:
            return None
        for obj in reversed(self._state.objects()):
            if getattr(obj, "deleted", False):
                continue
            if isinstance(obj, ec.ShapeObject) and obj.is_line:
                a, b = self._line_screen_points(obj)
                reach = max(6.0, float(obj.stroke_width) * self._zoom / 2 + 4.0)
                if self._near_segment(point, a, b, reach):
                    return obj
                continue
            if self._widget_rect(obj.rect).contains(point):
                return obj
        return None

    def _object_at(self, key: str):
        if self._state is None:
            return None
        obj = self._state.find(key)
        if obj is None or getattr(obj, "deleted", False):
            return None
        return obj

    # -- paint ---------------------------------------------------------
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.fillRect(self.rect(), QColor(T.SURFACE_ALT))

        if self._pixmap.isNull():
            painter.setPen(QColor(T.TEXT_SUBTLE))
            font = QFont(T.ui_font_family() or "Sans Serif")
            font.setPixelSize(13)
            painter.setFont(font)
            area = self.rect().adjusted(30, 30, -30, -30)
            painter.drawText(area, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                             self._placeholder)
            painter.end()
            return

        page = self._page_rect_widget()

        painter.setPen(Qt.PenStyle.NoPen)
        shadow = QColor(16, 24, 40, 26)
        painter.setBrush(shadow)
        painter.drawRoundedRect(page.translated(0, 4), 2, 2)

        painter.setBrush(QColor(T.SURFACE))
        painter.setPen(QPen(QColor(T.BORDER_STRONG), 1))
        painter.drawRect(page)
        painter.drawPixmap(page, self._pixmap, QRectF(self._pixmap.rect()))

        if self._hover and self._hover != self._selected:
            obj = self._object_at(self._hover)
            if obj is not None:
                rect = self._widget_rect(obj.rect)
                painter.setBrush(T.alpha(T.ACCENT, 0.04))
                painter.setPen(QPen(QColor(T.ACCENT), 1.1, Qt.PenStyle.DashLine))
                painter.drawRect(rect)

        obj = self.selected_object()
        if obj is not None:
            rect = self._widget_rect(obj.rect)
            painter.setBrush(T.alpha(T.ACCENT, 0.05))
            painter.setPen(QPen(QColor(T.ACCENT), 1.4))
            painter.drawRect(rect)

            painter.setPen(QPen(QColor(T.ACCENT), 1.2))
            painter.setBrush(QColor(T.SURFACE))
            for point in self._handle_points(obj, rect).values():
                painter.drawRect(QRectF(point.x() - HANDLE_SIZE / 2,
                                        point.y() - HANDLE_SIZE / 2,
                                        HANDLE_SIZE, HANDLE_SIZE))

            if isinstance(obj, ec.ImageObject):
                rotate = self._rotate_point(rect)
                painter.setPen(QPen(QColor(T.ACCENT), 1.2))
                painter.drawLine(QPointF(rect.center().x(), rect.top()), rotate)
                painter.setBrush(QColor(T.SURFACE))
                painter.drawEllipse(rotate, 5.2, 5.2)

        if (self._new_rect is not None and self._drag is not None
                and self._drag.get("kind") == "new_shape"):
            self._paint_shape_preview(painter, self._new_rect)
        elif self._new_rect is not None:
            painter.setBrush(T.alpha(T.ACCENT, 0.06))
            painter.setPen(QPen(QColor(T.ACCENT), 1.2, Qt.PenStyle.DashLine))
            painter.drawRect(self._new_rect)

        painter.end()

    # -- mouse ---------------------------------------------------------
    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        # Clicking the page while an inline editor is open finishes editing,
        # even if the platform did not deliver a focus-out event.
        self._commit_open_inline()
        self.setFocus()
        point = event.position()

        if self._tool == "shape":
            self._drag = {"kind": "new_shape", "start": point, "moved": False,
                          "flip": False}
            self._new_rect = QRectF(point, QSizeF(1, 1))
            self.update()
            return

        if self._tool == "text":
            self._drag = {"kind": "new_text", "start": point, "moved": False}
            self._new_rect = QRectF(point, QSizeF(1, 1))
            self.update()
            return

        obj = self.selected_object()
        if obj is not None:
            handle = self._handle_at(point)
            if handle == "rotate":
                rect = self._widget_rect(obj.rect)
                self._drag = {"kind": "rotate", "start": point, "moved": False,
                              "orig": tuple(obj.rect),
                              "center": rect.center(),
                              "orig_rotate": int(getattr(obj, "rotate", 0))}
                return
            if handle:
                x0, y0, x1, y1 = obj.rect
                min_w, min_h = 4.0, 4.0
                if isinstance(obj, ec.TextObject):
                    nat_w, nat_h = self._natural_text_size(obj)
                    # Never smaller than the text, but also never bigger than
                    # the box already is - so grabbing a handle cannot jolt.
                    min_w = max(4.0, min(nat_w, x1 - x0))
                    min_h = max(4.0, min(nat_h, y1 - y0))
                self._drag = {"kind": "resize", "handle": handle, "start": point,
                              "moved": False, "orig": tuple(obj.rect),
                              "min": (min_w, min_h)}
                if isinstance(obj, ec.ShapeObject) and obj.is_line:
                    self._drag["ends"] = ec.line_points(obj.rect, obj.flip, obj.reverse)
                return

        hit = self._hit_test(point)
        if hit is not None:
            if hit.key != self._selected:
                self.select(hit.key)
            self._drag = {"kind": "move", "start": point, "moved": False,
                          "orig": tuple(hit.rect)}
        else:
            self.select("")
            self._drag = None
        self.update()

    def mouseMoveEvent(self, event):
        point = event.position()

        if self._drag is None:
            handle = self._handle_at(point)
            if handle in ("tl", "br"):
                self.setCursor(Qt.CursorShape.SizeFDiagCursor)
            elif handle in ("tr", "bl"):
                self.setCursor(Qt.CursorShape.SizeBDiagCursor)
            elif handle in ("l", "r"):
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            elif handle in ("t", "b"):
                self.setCursor(Qt.CursorShape.SizeVerCursor)
            elif handle in ("p1", "p2"):
                self.setCursor(Qt.CursorShape.CrossCursor)
            elif handle == "rotate":
                self.setCursor(Qt.CursorShape.CrossCursor)
            elif self._tool in ("text", "shape"):
                self.setCursor(Qt.CursorShape.CrossCursor)
            else:
                hit = self._hit_test(point)
                self.setCursor(Qt.CursorShape.SizeAllCursor if hit is not None
                               else Qt.CursorShape.ArrowCursor)
                hover = hit.key if hit is not None else ""
                if hover != self._hover:
                    self._hover = hover
                    self.update()
            return

        if not (event.buttons() & Qt.MouseButton.LeftButton):
            # The release went to another widget (toast, inline editor...):
            # end the drag instead of letting the box follow the mouse.
            self._drag = None
            self._new_rect = None
            self.update()
            return

        kind = self._drag["kind"]
        if kind in ("new_text", "new_shape"):
            start = self._drag["start"]
            self._new_rect = QRectF(start, point).normalized()
            self._drag["cur"] = point
            self.update()
            return

        if not self._drag["moved"]:
            if (point - self._drag["start"]).manhattanLength() < 3:
                return
            self._drag["moved"] = True
            self.before_change.emit(kind)

        obj = self.selected_object()
        if obj is None:
            return

        if kind == "move":
            dx = (point.x() - self._drag["start"].x()) / self._zoom
            dy = (point.y() - self._drag["start"].y()) / self._zoom
            x0, y0, x1, y1 = self._drag["orig"]
            rect = QRectF(x0 + dx, y0 + dy, x1 - x0, y1 - y0)
            if not isinstance(obj, ec.ShapeObject):
                rect = self._shift_into_page(rect)
            obj.rect = (rect.left(), rect.top(), rect.right(), rect.bottom())
        elif kind == "resize":
            self._drag["shift"] = bool(
                event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            if isinstance(obj, ec.ShapeObject):
                self._resize_shape(obj, point)
            else:
                obj.rect = self._resized_rect(obj, point)
        elif kind == "rotate" and isinstance(obj, ec.ImageObject):
            center = self._drag["center"]
            angle = math.degrees(math.atan2(point.y() - center.y(),
                                            point.x() - center.x())) + 90.0
            snapped = int(round((angle % 360) / 15.0) * 15) % 360
            obj.rotate = snapped
        self.changed.emit()
        self.update()

    def _resize_shape(self, obj, point: QPointF) -> None:
        """Free resize for shapes: no minimum, no page limit, edges may cross.

        Hold Shift: keep the aspect ratio (boxes) or snap to 15° (lines).
        """
        handle = self._drag["handle"]
        shift = bool(self._drag.get("shift"))
        start = self._page_point(self._drag["start"])
        now = self._page_point(point)
        dx, dy = now.x() - start.x(), now.y() - start.y()

        if handle in ("p1", "p2"):
            tail, head = self._drag["ends"]
            moving_tail = (handle == "p1") != obj_reverse(self._drag, obj)
            fixed = head if moving_tail else tail
            moved = tail if moving_tail else head
            moved = (moved[0] + dx, moved[1] + dy)
            if shift:
                vx, vy = moved[0] - fixed[0], moved[1] - fixed[1]
                length = math.hypot(vx, vy)
                angle = round(math.atan2(vy, vx) / (math.pi / 12)) * (math.pi / 12)
                moved = (fixed[0] + length * math.cos(angle),
                         fixed[1] + length * math.sin(angle))
            new_tail, new_head = (moved, fixed) if moving_tail else (fixed, moved)
            obj.rect, obj.flip, obj.reverse = ec.line_from_points(new_tail, new_head)
            return

        x0, y0, x1, y1 = self._drag["orig"]
        left, top, right, bottom = x0, y0, x1, y1
        if "l" in handle:
            left = x0 + dx
        if "r" in handle:
            right = x1 + dx
        if "t" in handle:
            top = y0 + dy
        if "b" in handle:
            bottom = y1 + dy
        if shift and len(handle) == 2:
            ratio = max(x1 - x0, 0.01) / max(y1 - y0, 0.01)
            fx = x1 if "l" in handle else x0
            fy = y1 if "t" in handle else y0
            mx = left if "l" in handle else right
            my = top if "t" in handle else bottom
            width, height = abs(mx - fx), abs(my - fy)
            if width / max(height, 0.01) > ratio:
                height = width / ratio
            else:
                width = height * ratio
            mx = fx + (width if mx >= fx else -width)
            my = fy + (height if my >= fy else -height)
            left, right = sorted((fx, mx))
            top, bottom = sorted((fy, my))
        else:
            left, right = sorted((left, right))
            top, bottom = sorted((top, bottom))
        if right - left < 0.5:
            right = left + 0.5
        if bottom - top < 0.5:
            bottom = top + 0.5
        obj.rect = (left, top, right, bottom)

    def _resized_rect(self, obj, point: QPointF):
        handle = self._drag["handle"]
        start_page = self._page_point(self._drag["start"])
        now_page = self._page_point(point)
        dx = now_page.x() - start_page.x()
        dy = now_page.y() - start_page.y()
        x0, y0, x1, y1 = self._drag["orig"]
        left, top, right, bottom = x0, y0, x1, y1

        if "l" in handle:
            left = min(x0 + dx, right - 4.0)
        if "r" in handle:
            right = max(x1 + dx, left + 4.0)
        if "t" in handle:
            top = min(y0 + dy, bottom - 4.0)
        if "b" in handle:
            bottom = max(y1 + dy, top + 4.0)

        if isinstance(obj, ec.ImageObject) and obj.keep_proportion and handle in (
                "tl", "tr", "bl", "br"):
            ratio = max(0.05, (x1 - x0) / max(0.05, y1 - y0))
            anchor_x = right if "l" in handle else left
            anchor_y = bottom if "t" in handle else top
            width = abs(now_page.x() - anchor_x)
            height = abs(now_page.y() - anchor_y)
            if width / max(height, 0.05) > ratio:
                width = height * ratio
            else:
                height = width / ratio
            left = anchor_x - width if "l" in handle else anchor_x
            right = anchor_x + width if "r" in handle else anchor_x
            top = anchor_y - height if "t" in handle else anchor_y
            bottom = anchor_y + height if "b" in handle else anchor_y

        min_w, min_h = self._drag.get("min", (4.0, 4.0))
        # Only the edges being dragged move; the opposite edge is the anchor.
        if "l" in handle:
            left = min(left, x1 - min_w)
        if "r" in handle:
            right = max(right, x0 + min_w)
        if "t" in handle:
            top = min(top, y1 - min_h)
        if "b" in handle:
            bottom = max(bottom, y0 + min_h)
        rect = QRectF(left, top, right - left, bottom - top)
        return tuple(self._clamp_page_rect(rect).getRect())

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self._drag is None:
            return
        kind = self._drag["kind"]
        moved = bool(self._drag.get("moved"))
        if kind == "new_shape":
            start = self._drag["start"]
            end = event.position()
            rect = self._new_rect
            line = self._shape_kind in ec.LINE_KINDS
            tiny = (rect is None or (rect.width() < 8 and rect.height() < 8))
            self._new_rect = None
            self._drag = None
            if line:
                a, b = self._line_drag_points(start, end)
                if tiny:   # a plain click: a default-length horizontal line
                    b = QPointF(a.x() + 150 * self._zoom, a.y())
                rect_p, flip, reverse = ec.line_from_points(
                    (self._page_point(a).x(), self._page_point(a).y()),
                    (self._page_point(b).x(), self._page_point(b).y()))
                self._add_shape(rect_p, flip, reverse)
                return
            if tiny:
                rect = QRectF(start.x(), start.y(), 150 * self._zoom, 90 * self._zoom)
            page_rect = QRectF(
                (rect.x() - CANVAS_MARGIN) / self._zoom,
                (rect.y() - CANVAS_MARGIN) / self._zoom,
                max(1.0, rect.width() / self._zoom),
                max(1.0, rect.height() / self._zoom))
            self._add_shape(tuple(page_rect.getRect()), False, False)
            return

        if kind == "new_text":
            rect = self._new_rect
            if rect is None or rect.width() < 16 or rect.height() < 12:
                font_size = max(8.0, self._default_size)
                rect = QRectF(self._drag["start"].x(), self._drag["start"].y() - 3,
                              230, max(32, font_size * 1.7 * self._zoom))
            page_rect = QRectF(
                (rect.x() - CANVAS_MARGIN) / self._zoom,
                (rect.y() - CANVAS_MARGIN) / self._zoom,
                max(40.0, rect.width() / self._zoom),
                max(16.0, rect.height() / self._zoom))
            page_rect = self._clamp_page_rect(page_rect)
            self._new_rect = None
            self._drag = None
            self._add_text(tuple(page_rect.getRect()))
            return

        self._drag = None
        if moved:
            self.committed.emit(kind.title())
        self.update()

    def mouseDoubleClickEvent(self, event):
        if self._tool != "select":
            return
        hit = self._hit_test(event.position())
        if isinstance(hit, ec.TextObject):
            self.select(hit.key)
            self.begin_inline_edit(hit.key)
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom_delta.emit(1 if event.angleDelta().y() > 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event):
        # Last-resort safety net: if the platform moved focus off the inline
        # editor (Windows does this in some IME/window states), send the key
        # there instead of dropping it on the canvas.  Shortcuts are left to
        # the application so Save/Open/Undo still work.
        if self._inline is not None:
            _key, editor = self._inline
            shortcuts = event.modifiers() & (Qt.KeyboardModifier.ControlModifier
                                             | Qt.KeyboardModifier.AltModifier
                                             | Qt.KeyboardModifier.MetaModifier)
            if (not editor._done and editor.isVisible() and not shortcuts):
                editor.setFocus(Qt.FocusReason.OtherFocusReason)
                QApplication.sendEvent(editor, event)
                event.accept()
                return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_selected()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape:
            self.select("")
            event.accept()
            return
        step = 10.0 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1.0
        moves = {
            Qt.Key.Key_Left: (-step, 0.0), Qt.Key.Key_Right: (step, 0.0),
            Qt.Key.Key_Up: (0.0, -step), Qt.Key.Key_Down: (0.0, step),
        }
        if event.key() in moves and self.selected_object() is not None:
            obj = self.selected_object()
            dx, dy = moves[event.key()]
            x0, y0, x1, y1 = obj.rect
            rect = self._clamp_page_rect(QRectF(x0 + dx, y0 + dy, x1 - x0, y1 - y0))
            obj.rect = tuple(rect.getRect())
            self.before_change.emit("nudge")
            self.changed.emit()
            self.update()
            event.accept()
            return
        super().keyPressEvent(event)

    # -- text editing --------------------------------------------------
    def _add_text(self, rect) -> None:
        if self._state is None:
            return
        self.before_change.emit("add")
        font_path = self._default_font or ec.default_sinhala_font()
        obj = self._state.add_text(rect, font_path, self._default_size, self._default_color)
        self.select(obj.key)
        self.changed.emit()
        self.committed.emit("Added text")
        QTimer.singleShot(30, lambda key=obj.key: self.begin_inline_edit(key))

    def _line_drag_points(self, start: QPointF, cur: QPointF):
        """Start/end of a line being drawn; near-horizontal/vertical snaps."""
        dx, dy = cur.x() - start.x(), cur.y() - start.y()
        if abs(dy) < 6 and abs(dx) > abs(dy):
            dy = 0.0
        elif abs(dx) < 6 and abs(dy) > abs(dx):
            dx = 0.0
        return start, QPointF(start.x() + dx, start.y() + dy)

    def _add_shape(self, rect, flip: bool, reverse: bool = False) -> None:
        if self._state is None:
            return
        self.before_change.emit("add")
        obj = self._state.add_shape(self._shape_kind, rect, flip, reverse,
                                    **self._shape_style)
        self.select(obj.key)
        self.changed.emit()
        self.committed.emit("Added shape")
        self.tool_finished.emit()

    def _paint_shape_preview(self, painter: QPainter, rect: QRectF) -> None:
        """Live outline while dragging; mirrors how the PDF draws the shape."""
        style = self._shape_style
        kind = self._shape_kind
        fill = QColor(*style["fill"])
        fill.setAlphaF(float(style["fill_opacity"]))
        stroke = QColor(*style["stroke"])
        stroke.setAlphaF(float(style["stroke_opacity"]))
        width = float(style["stroke_width"]) * self._zoom
        pen = (QPen(stroke, max(1.0, width)) if style["stroke_width"] > 0
               else Qt.PenStyle.NoPen)
        if kind in ec.LINE_KINDS:
            start = self._drag["start"]
            a, b = self._line_drag_points(start, self._drag.get("cur", start))
            p1, p2 = (a.x(), a.y()), (b.x(), b.y())
            painter.setPen(QPen(stroke, max(1.0, width)))
            if kind == "arrow":
                end, head = ec.arrow_head(p1, p2, max(1.0, width))
                painter.drawLine(QPointF(*p1), QPointF(*end))
                painter.setBrush(stroke)
                painter.drawPolygon(QPolygonF([QPointF(*pt) for pt in head]))
            else:
                painter.drawLine(QPointF(*p1), QPointF(*p2))
            return
        painter.setPen(pen)
        painter.setBrush(fill)
        if kind == "ellipse":
            painter.drawEllipse(rect)
        elif kind == "triangle":
            painter.drawPolygon(QPolygonF([
                QPointF(rect.center().x(), rect.top()),
                rect.bottomRight(), rect.bottomLeft()]))
        elif kind == "rounded":
            radius = min(rect.width(), rect.height()) * 0.2
            painter.drawRoundedRect(rect, radius, radius)
        else:
            painter.drawRect(rect)

    def _commit_open_inline(self) -> None:
        if self._inline is None:
            return
        open_key, open_editor = self._inline
        self._finish_inline(True, open_editor.toPlainText(), open_key)

    def begin_inline_edit(self, key: str) -> None:
        obj = self._object_at(key)
        if not isinstance(obj, ec.TextObject):
            return
        self._commit_open_inline()
        self.before_change.emit("text")
        rect = self._widget_rect(obj.rect)
        target = QRectF(rect.x() - 3, rect.y() - 3,
                        max(90.0, rect.width() + 6), max(30.0, rect.height() + 6))
        editor = InlineEditor(obj.text, self)
        editor.setGeometry(target.toRect())
        pixel_size = max(10, int(round(obj.font_size * self._zoom)))
        family_css = font_family_css(obj.font_path)
        font = QFont()
        families = editor_font_families(obj.font_path)
        if families:
            font.setFamilies(families)
        font.setPixelSize(pixel_size)
        editor.setFont(font)
        # Apply the style before showing/focusing.  Re-polishing a focused
        # editor after it opens is what let Windows bounce the focus away.
        editor.setStyleSheet(
            f"QPlainTextEdit#InlineEditor{{background: {T.SURFACE};"
            f" border: 1.4px solid {T.ACCENT}; border-radius: 4px;"
            f" color: {T.TEXT}; padding: 2px;"
            f"{f' font-family: {family_css};' if family_css else ''}"
            f" font-size: {pixel_size}px;}}")
        editor.finished.connect(lambda ok, text, k=key: self._finish_inline(ok, text, k))
        self._inline = (key, editor)
        editor.show()
        editor.raise_()
        editor.setFocus(Qt.FocusReason.OtherFocusReason)
        editor.selectAll()

    def _finish_inline(self, ok: bool, text: str | None, key: str | None = None) -> None:
        if self._inline is None:
            return
        open_key, editor = self._inline
        if key is not None and key != open_key:
            return
        app = QApplication.instance()
        focus_before = app.focusWidget() if app is not None else None
        self._inline = None
        editor.close_editor()
        try:
            editor.finished.disconnect()
        except Exception:  # noqa: BLE001
            pass
        editor.blockSignals(True)
        editor.deleteLater()
        if not ok:
            return
        obj = self._object_at(open_key)
        if not isinstance(obj, ec.TextObject):
            return
        if text is not None and text != obj.text:
            obj.text = text
            obj.label = text.strip().replace("\n", " ")[:60] or "Text"
            fit_font_to_text(obj)
            self.changed.emit()
            self.committed.emit("Edited text")
        # Return focus to the page only when the editor itself had it.  If the
        # user clicked another widget, Qt already focused that widget and
        # stealing focus back would make it look like only the first click
        # worked.
        if (focus_before is None or focus_before is editor
                or (focus_before is not None
                    and editor.isAncestorOf(focus_before))):
            self.setFocus()

    def commit_inline(self) -> None:
        if self._inline is None:
            return
        _key, editor = self._inline
        self._finish_inline(True, editor.toPlainText())

    # -- mutations -----------------------------------------------------
    def delete_selected(self) -> None:
        obj = self.selected_object()
        if obj is None or self._state is None:
            return
        self.before_change.emit("delete")
        self._state.remove(obj.key)
        self.select("")
        self.changed.emit()
        self.committed.emit("Deleted")


# ------------------------------------------------------------------ rail
class PageRail(QWidget):
    """Vertical strip of page thumbnails."""

    page_selected = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMinimumWidth(120)
        self.setMaximumWidth(268)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        header = QHBoxLayout()
        header.setContentsMargins(2, 0, 2, 0)
        header.addWidget(label("Pages", 12, 700, T.TEXT_MUTED, "FieldLabel"))
        header.addStretch(1)
        self.count = Badge("0", "neutral")
        header.addWidget(self.count)
        column.addLayout(header)

        self.list = QListWidget()
        self.list.setObjectName("PageRail")
        self.list.setViewMode(QListView.ViewMode.IconMode)
        self.list.setFlow(QListView.Flow.TopToBottom)
        self.list.setWrapping(False)
        self.list.setResizeMode(QListView.ResizeMode.Adjust)
        self.list.setMovement(QListView.Movement.Static)
        self.list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.list.setIconSize(QSize(92, 124))
        self.list.setGridSize(QSize(112, 156))
        self.list.setSpacing(2)
        self.list.setUniformItemSizes(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.currentRowChanged.connect(self._on_row)
        column.addWidget(self.list, 1)
        self._path = ""
        self._syncing = False

    def clear(self) -> None:
        self._path = ""
        self.list.clear()
        self.count.setText("0")

    def set_pages(self, count: int) -> None:
        # A new page list belongs to a new document.  Without resetting this,
        # set_thumbs() would reject the first thumbnail batch for every
        # document after the first one.
        self._path = ""
        self.list.clear()
        blank = QPixmap(1, 1)
        blank.fill(Qt.GlobalColor.transparent)
        icon = QIcon(blank)
        for index in range(int(count)):
            item = QListWidgetItem(icon, str(index + 1))
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            item.setData(Qt.ItemDataRole.UserRole, str(index + 1))
            item.setSizeHint(QSize(108, 150))
            item.setToolTip(f"Page {index + 1}")
            self.list.addItem(item)
        self.count.setText(str(int(count)))

    def set_thumbs(self, path: str, images) -> None:
        if path != self._path and self._path:
            return
        self._path = path
        for index, data in enumerate(images or []):
            item = self.list.item(index)
            if item is None or not data:
                continue
            pixmap = QPixmap()
            pixmap.loadFromData(data)
            if pixmap.isNull():
                continue
            item.setIcon(QIcon(pixmap))
        self.list.setIconSize(QSize(92, 124))

    def set_current(self, index: int) -> None:
        self._syncing = True
        self.list.setCurrentRow(int(index))
        self._syncing = False

    def set_modified(self, index: int, modified: bool) -> None:
        item = self.list.item(index)
        if item is None:
            return
        base = item.data(Qt.ItemDataRole.UserRole) or str(index + 1)
        item.setText(f"{base} •" if modified else str(base))

    def current(self) -> int:
        return self.list.currentRow()

    def _on_row(self, row: int) -> None:
        if not self._syncing and row >= 0:
            self.page_selected.emit(row)


# ------------------------------------------------------------------ inspector
class EditorInspector(Card):
    """Properties for the selected text or image object."""

    begin_change = pyqtSignal(str)
    changed = pyqtSignal()
    delete_requested = pyqtSignal()
    replace_image_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, pad=16, shadow=False)
        self.setObjectName("Inspector")
        self.setMinimumWidth(254)
        self.setMaximumWidth(390)
        self._obj = None
        self._path = ""
        self._syncing = False
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.setInterval(220)
        self._fit_timer.timeout.connect(self._apply_font_fit)

        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(label("Inspector", 14, 700, T.TEXT, "SectionTitle"))
        header.addStretch(1)
        self.kind = Badge("Nothing", "neutral")
        header.addWidget(self.kind)
        self.add_layout(header)

        self.empty = QWidget()
        empty_col = QVBoxLayout(self.empty)
        empty_col.setContentsMargins(0, 12, 0, 12)
        empty_col.setSpacing(8)
        hint = label("Select text, an image or a shape on the page, or draw a new "
                     "text box or shape with the toolbar tools.", 12, 400, T.TEXT_MUTED)
        hint.setWordWrap(True)
        empty_col.addWidget(hint)
        empty_col.addWidget(divider())
        shortcut = label("Double-click text to edit it in place.", 12, 500, T.TEXT_SUBTLE)
        shortcut.setWordWrap(True)
        empty_col.addWidget(shortcut)
        self.add(self.empty)

        self.text_panel = QWidget()
        text_col = QVBoxLayout(self.text_panel)
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(9)

        text_col.addWidget(label("Content", 12, 600, T.TEXT_MUTED, "FieldLabel"))
        self.text_edit = QPlainTextEdit()
        self.text_edit.setPlaceholderText("Type text here — Sinhala shaping is automatic…")
        self.text_edit.setFixedHeight(96)
        # An explicit widget-level family also overrides the global ``*`` QSS
        # rule, which otherwise drops the fallback chain and can leave Qt to
        # guess a face for Sinhala on Windows.
        families = font_family_css("")
        if families:
            self.text_edit.setStyleSheet(
                f"QPlainTextEdit {{ font-family: {families}; }}")
        self.text_edit.textChanged.connect(self._text_changed)
        text_col.addWidget(self.text_edit)

        font_caption = label("Font", 12, 600, T.TEXT_MUTED, "FieldLabel")
        text_col.addWidget(font_caption)
        self.font_combo = QComboBox()
        self.font_combo.setMinimumHeight(36)
        self.font_combo.currentIndexChanged.connect(self._font_changed)
        text_col.addWidget(self.font_combo)
        self._font_items: dict[str, int] = {}

        style_row = QHBoxLayout()
        style_row.setSpacing(8)
        size_col = QVBoxLayout()
        size_col.setSpacing(5)
        size_col.addWidget(label("Size", 12, 600, T.TEXT_MUTED, "FieldLabel"))
        self.size_spin = QDoubleSpinBox()
        self.size_spin.setRange(4.0, 300.0)
        self.size_spin.setSingleStep(0.5)
        self.size_spin.setDecimals(1)
        self.size_spin.setValue(14.0)
        self.size_spin.setFixedWidth(92)
        self.size_spin.valueChanged.connect(self._size_changed)
        size_col.addWidget(self.size_spin)
        style_row.addLayout(size_col)
        colour_col = QVBoxLayout()
        colour_col.setSpacing(5)
        colour_col.addWidget(label("Colour", 12, 600, T.TEXT_MUTED, "FieldLabel"))
        self.colour = ColorButton((17, 24, 39))
        self.colour.clicked.connect(self._pick_colour)
        colour_col.addWidget(self.colour)
        style_row.addLayout(colour_col, 1)
        text_col.addLayout(style_row)

        align_row = QHBoxLayout()
        align_row.setSpacing(8)
        align_row.addWidget(label("Align", 12, 600, T.TEXT_MUTED, "FieldLabel"))
        self.align = SegmentedControl([("left", "Left"), ("center", "Center"),
                                       ("right", "Right")])
        self.align.changed.connect(self._align_changed)
        align_row.addWidget(self.align, 1)
        text_col.addLayout(align_row)

        text_col.addWidget(divider())
        self.delete_text = danger_button("Delete text", "trash", self.delete_requested.emit)
        text_col.addWidget(self.delete_text)
        self.add(self.text_panel)

        self.image_panel = QWidget()
        image_col = QVBoxLayout(self.image_panel)
        image_col.setContentsMargins(0, 0, 0, 0)
        image_col.setSpacing(9)
        image_col.addWidget(label("Preview", 12, 600, T.TEXT_MUTED, "FieldLabel"))
        self.preview = ImagePreview()
        image_col.addWidget(self.preview)
        self.image_info = label("", 11, 500, T.TEXT_SUBTLE)
        self.image_info.setWordWrap(True)
        image_col.addWidget(self.image_info)

        rotate_row = QHBoxLayout()
        rotate_row.setSpacing(6)
        rotate_row.addWidget(label("Rotate", 12, 600, T.TEXT_MUTED, "FieldLabel"))
        rotate_row.addWidget(icon_button("rotate_ccw", lambda: self._rotate(-15),
                                         "Rotate left 15°"))
        rotate_row.addWidget(icon_button("rotate_cw", lambda: self._rotate(15),
                                         "Rotate right 15°"))
        self.angle = Badge("0°", "neutral")
        rotate_row.addWidget(self.angle)
        rotate_row.addStretch(1)
        image_col.addLayout(rotate_row)

        keep_row = QHBoxLayout()
        keep_row.setSpacing(9)
        self.keep = ToggleSwitch(True)
        self.keep.toggled.connect(self._keep_changed)
        keep_row.addWidget(self.keep)
        keep_label = label("Keep aspect ratio when placing", 12, 500, T.TEXT)
        keep_label.setWordWrap(True)
        keep_row.addWidget(keep_label, 1)
        image_col.addLayout(keep_row)

        image_col.addWidget(soft_button("Replace image…", "image",
                                        self.replace_image_requested.emit))
        image_col.addWidget(divider())
        self.delete_image = danger_button("Delete image", "trash", self.delete_requested.emit)
        image_col.addWidget(self.delete_image)
        image_col.addStretch(1)
        self.add(self.image_panel)

        self.shape_panel = QWidget()
        shape_col = QVBoxLayout(self.shape_panel)
        shape_col.setContentsMargins(0, 0, 0, 0)
        shape_col.setSpacing(9)

        shape_col.addWidget(label("Shape", 12, 600, T.TEXT_MUTED, "FieldLabel"))
        self.shape_kind = QComboBox()
        self.shape_kind.setMinimumHeight(36)
        for kind, title in ec.SHAPE_KINDS:
            self.shape_kind.addItem(title, kind)
        self.shape_kind.currentIndexChanged.connect(self._shape_kind_changed)
        shape_col.addWidget(self.shape_kind)

        # Everything about the fill is hidden for lines and arrows.
        self.fill_box = QWidget()
        fill_col = QVBoxLayout(self.fill_box)
        fill_col.setContentsMargins(0, 0, 0, 0)
        fill_col.setSpacing(9)
        fill_col.addWidget(label("Fill", 12, 600, T.TEXT_MUTED, "FieldLabel"))
        self.fill_mode = SegmentedControl([("none", "None"), ("glass", "Transparent"),
                                           ("solid", "Opaque")])
        self.fill_mode.changed.connect(self._fill_mode_changed)
        fill_col.addWidget(self.fill_mode)
        fill_row = QHBoxLayout()
        fill_row.setSpacing(8)
        self.fill_colour = ColorButton((59, 130, 246))
        self.fill_colour.clicked.connect(self._pick_fill)
        fill_row.addWidget(self.fill_colour, 1)
        self.fill_opacity, self.fill_opacity_label = self._opacity_slider(
            self._fill_opacity_changed)
        fill_row.addWidget(self.fill_opacity, 2)
        fill_row.addWidget(self.fill_opacity_label)
        fill_col.addLayout(fill_row)
        shape_col.addWidget(self.fill_box)

        shape_col.addWidget(label("Outline", 12, 600, T.TEXT_MUTED, "FieldLabel"))
        line_row = QHBoxLayout()
        line_row.setSpacing(8)
        self.stroke_colour = ColorButton((30, 64, 175))
        self.stroke_colour.clicked.connect(self._pick_stroke)
        line_row.addWidget(self.stroke_colour, 1)
        self.stroke_width = QDoubleSpinBox()
        self.stroke_width.setRange(0.0, 60.0)
        self.stroke_width.setSingleStep(0.5)
        self.stroke_width.setDecimals(1)
        self.stroke_width.setSuffix(" pt")
        self.stroke_width.setFixedWidth(92)
        self.stroke_width.valueChanged.connect(self._stroke_width_changed)
        line_row.addWidget(self.stroke_width)
        shape_col.addLayout(line_row)
        stroke_row = QHBoxLayout()
        stroke_row.setSpacing(8)
        stroke_row.addWidget(label("Opacity", 12, 500, T.TEXT_MUTED))
        self.stroke_opacity, self.stroke_opacity_label = self._opacity_slider(
            self._stroke_opacity_changed)
        stroke_row.addWidget(self.stroke_opacity, 1)
        stroke_row.addWidget(self.stroke_opacity_label)
        shape_col.addLayout(stroke_row)

        shape_col.addWidget(divider())
        self.delete_shape = danger_button("Delete shape", "trash", self.delete_requested.emit)
        shape_col.addWidget(self.delete_shape)
        shape_col.addStretch(1)
        self.add(self.shape_panel)

        self._populate_fonts()
        self.show_object(None, "")

    # -- font list -----------------------------------------------------
    def _populate_fonts(self) -> None:
        self._syncing = True
        for choice in ec.font_catalog():
            self._font_items[choice.path] = self.font_combo.count()
            self.font_combo.addItem(choice.label, choice.path)
            self.font_combo.setItemData(self.font_combo.count() - 1, choice.tooltip,
                                        Qt.ItemDataRole.ToolTipRole)
        self._syncing = False

    def _ensure_font(self, path: str) -> None:
        if not path or path in self._font_items:
            return
        name = Path(path).stem
        self._font_items[path] = 0
        self.font_combo.insertItem(0, name, path)

    def set_font_choice(self, path: str | None) -> None:
        if not path:
            return
        was = self._syncing
        self._syncing = True
        self._ensure_font(path)
        index = self.font_combo.findData(path)
        if index >= 0:
            self.font_combo.setCurrentIndex(index)
        self._syncing = was

    def current_font(self) -> str:
        data = self.font_combo.currentData()
        return str(data or "")

    # -- population ----------------------------------------------------
    def show_object(self, obj, path: str) -> None:
        self._obj = obj
        self._path = path
        self._fit_timer.stop()
        self._syncing = True
        is_text = isinstance(obj, ec.TextObject)
        is_image = isinstance(obj, ec.ImageObject)
        is_shape = isinstance(obj, ec.ShapeObject)
        self.empty.setVisible(obj is None)
        self.text_panel.setVisible(is_text)
        self.image_panel.setVisible(is_image)
        self.shape_panel.setVisible(is_shape)

        if obj is None:
            self.kind.setText("Nothing")
            self.kind.set_tone("neutral")
        elif is_text:
            self.kind.setText("New text" if obj.is_new else "Text")
            self.kind.set_tone("accent" if obj.is_new else "neutral")
            self.text_edit.setPlainText(obj.text)
            self.set_font_choice(obj.font_path)
            self.size_spin.setValue(float(obj.font_size or 12.0))
            self.colour.set_color(obj.color)
            self.align.set_value(obj.align if obj.align in ("left", "center", "right")
                                 else "left")
            self.delete_text.setText("Delete text")
        elif is_shape:
            self.kind.setText("Shape")
            self.kind.set_tone("accent")
            index = self.shape_kind.findData(obj.kind)
            self.shape_kind.setCurrentIndex(max(0, index))
            self.fill_box.setVisible(not obj.is_line)
            self.fill_colour.set_color(obj.fill)
            self.fill_opacity.setValue(int(round(obj.fill_opacity * 100)))
            self.fill_opacity_label.setText(f"{int(round(obj.fill_opacity * 100))}%")
            self.fill_mode.set_value(self._fill_mode_for(obj.fill_opacity))
            self.stroke_colour.set_color(obj.stroke)
            self.stroke_width.setValue(float(obj.stroke_width))
            self.stroke_opacity.setValue(int(round(obj.stroke_opacity * 100)))
            self.stroke_opacity_label.setText(f"{int(round(obj.stroke_opacity * 100))}%")
        elif is_image:
            self.kind.setText("New image" if obj.is_new else "Image")
            self.kind.set_tone("accent" if obj.is_new else "neutral")
            self.angle.setText(f"{int(obj.rotate) % 360}°")
            self.keep.setChecked(bool(obj.keep_proportion))
            self.image_info.setText(self._image_description(obj))
            self._load_preview(obj)
            self.delete_image.setText("Delete image")
        self._syncing = False

    def _image_description(self, obj: ec.ImageObject) -> str:
        bits = []
        if obj.width and obj.height:
            bits.append(f"{obj.width} × {obj.height} px")
        if obj.is_new:
            bits.append("from file")
        elif obj.replacement_path:
            bits.append("replacement")
        elif not obj.item or not obj.item.can_move:
            bits.append("inline image")
        if obj.deleted:
            bits.append("deleted")
        return "  ·  ".join(bits)

    def _load_preview(self, obj: ec.ImageObject) -> None:
        data = b""
        if obj.replacement_path:
            data = ec.file_preview(obj.replacement_path)
        elif obj.new_path:
            data = ec.file_preview(obj.new_path)
        elif self._path and obj.xref:
            data = ec.image_preview(self._path, obj.xref)
        self.preview.set_data(data)

    def refresh_preview(self) -> None:
        if isinstance(self._obj, ec.ImageObject):
            self._syncing = True
            self.image_info.setText(self._image_description(self._obj))
            self._load_preview(self._obj)
            self._syncing = False

    # -- text fields ---------------------------------------------------
    def _text_changed(self) -> None:
        if self._syncing or not isinstance(self._obj, ec.TextObject):
            return
        self.begin_change.emit("text")
        self._obj.text = self.text_edit.toPlainText()
        self._obj.label = self._obj.text.strip().replace("\n", " ")[:60] or "Text"
        # Do the font switch after typing pauses.  Changing the combo and the
        # preview synchronously on the first keystroke is what made Windows
        # drop focus before the second character.
        self._fit_timer.start()
        self.changed.emit()

    def _apply_font_fit(self) -> None:
        obj = self._obj
        if self._syncing or not isinstance(obj, ec.TextObject):
            return
        before = obj.font_path
        fit_font_to_text(obj)
        if obj.font_path != before:
            self.set_font_choice(obj.font_path)
            self.changed.emit()

    def _font_changed(self, _index: int) -> None:
        if self._syncing or not isinstance(self._obj, ec.TextObject):
            return
        self._fit_timer.stop()
        path = self.current_font()
        if not path or path == self._obj.font_path:
            return
        self.begin_change.emit("font")
        self._obj.font_path = path
        self.changed.emit()

    def _size_changed(self, value: float) -> None:
        if self._syncing or not isinstance(self._obj, ec.TextObject):
            return
        self.begin_change.emit("size")
        self._obj.font_size = float(value)
        self.changed.emit()

    def _pick_colour(self) -> None:
        if not isinstance(self._obj, ec.TextObject):
            return
        colour = QColorDialog.getColor(QColor(*self._obj.color), self, "Text colour")
        if not colour.isValid():
            return
        self.begin_change.emit("colour")
        self._obj.color = (colour.red(), colour.green(), colour.blue())
        self._syncing = True
        self.colour.set_color(self._obj.color)
        self._syncing = False
        self.changed.emit()

    def _align_changed(self, value: str) -> None:
        if self._syncing or not isinstance(self._obj, ec.TextObject):
            return
        self.begin_change.emit("align")
        self._obj.align = value
        self.changed.emit()

    # -- shape fields --------------------------------------------------
    @staticmethod
    def _fill_mode_for(opacity: float) -> str:
        if opacity <= 0.01:
            return "none"
        return "solid" if opacity >= 0.99 else "glass"

    def _opacity_slider(self, handler):
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 100)
        slider.setValue(100)
        percent = label("100%", 12, 600, T.TEXT_MUTED)
        percent.setMinimumWidth(38)
        slider.valueChanged.connect(handler)
        return slider, percent

    def _shape(self):
        return None if self._syncing or not isinstance(self._obj, ec.ShapeObject) \
            else self._obj

    def _shape_kind_changed(self, _index: int) -> None:
        obj = self._shape()
        kind = self.shape_kind.currentData()
        if obj is None or not kind or kind == obj.kind:
            return
        self.begin_change.emit("shape")
        obj.kind = kind
        obj.label = dict(ec.SHAPE_KINDS).get(kind, "Shape")
        self.fill_box.setVisible(not obj.is_line)
        self.changed.emit()

    def _set_fill_opacity(self, value: float) -> None:
        obj = self._shape()
        if obj is None:
            return
        self.begin_change.emit("shape")
        obj.fill_opacity = max(0.0, min(1.0, float(value)))
        self._syncing = True
        self.fill_opacity.setValue(int(round(obj.fill_opacity * 100)))
        self.fill_opacity_label.setText(f"{int(round(obj.fill_opacity * 100))}%")
        self.fill_mode.set_value(self._fill_mode_for(obj.fill_opacity))
        self._syncing = False
        self.changed.emit()

    def _fill_mode_changed(self, mode: str) -> None:
        self._set_fill_opacity({"none": 0.0, "glass": 0.35, "solid": 1.0}.get(mode, 1.0))

    def _fill_opacity_changed(self, value: int) -> None:
        self._set_fill_opacity(value / 100.0)

    def _stroke_opacity_changed(self, value: int) -> None:
        obj = self._shape()
        if obj is None:
            return
        self.begin_change.emit("shape")
        obj.stroke_opacity = value / 100.0
        self.stroke_opacity_label.setText(f"{value}%")
        self.changed.emit()

    def _stroke_width_changed(self, value: float) -> None:
        obj = self._shape()
        if obj is None:
            return
        self.begin_change.emit("shape")
        obj.stroke_width = float(value)
        self.changed.emit()

    def _pick_shape_colour(self, attr: str, button, title: str) -> None:
        obj = self._obj
        if not isinstance(obj, ec.ShapeObject):
            return
        colour = QColorDialog.getColor(QColor(*getattr(obj, attr)), self, title)
        if not colour.isValid():
            return
        self.begin_change.emit("shape")
        setattr(obj, attr, (colour.red(), colour.green(), colour.blue()))
        if attr == "fill" and obj.fill_opacity <= 0.01:
            obj.fill_opacity = 0.35     # picking a colour should show it
            self._syncing = True
            self.fill_opacity.setValue(35)
            self.fill_opacity_label.setText("35%")
            self.fill_mode.set_value("glass")
            self._syncing = False
        self._syncing = True
        button.set_color(getattr(obj, attr))
        self._syncing = False
        self.changed.emit()

    def _pick_fill(self) -> None:
        self._pick_shape_colour("fill", self.fill_colour, "Fill colour")

    def _pick_stroke(self) -> None:
        self._pick_shape_colour("stroke", self.stroke_colour, "Outline colour")

    # -- image fields --------------------------------------------------
    def _rotate(self, delta: int) -> None:
        if not isinstance(self._obj, ec.ImageObject):
            return
        self.begin_change.emit("rotate")
        self._obj.rotate = (int(self._obj.rotate) + int(delta)) % 360
        self._syncing = True
        self.angle.setText(f"{self._obj.rotate}°")
        self._syncing = False
        self.changed.emit()

    def _keep_changed(self, value: bool) -> None:
        if self._syncing or not isinstance(self._obj, ec.ImageObject):
            return
        self.begin_change.emit("aspect")
        self._obj.keep_proportion = bool(value)
        self.changed.emit()
