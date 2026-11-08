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
    QIcon,
    QPainter,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QComboBox,
    QColorDialog,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QSizePolicy,
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
#: Smallest width/height (PDF points) a box can be resized to.  Anything
#: tighter wraps text one letter per line and it stops rendering.
MIN_BOX = 14.0


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
        self.setObjectName("InlineEditor")
        self.setPlainText(text)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTabChangesFocus(True)

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
        super().keyPressEvent(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self._finish(True, self.toPlainText())

    def _finish(self, ok: bool, text: str) -> None:
        if self._done:
            return
        self._done = True
        self.finished.emit(ok, text)


# ------------------------------------------------------------------ canvas
class EditorCanvas(QWidget):
    """Rendered page with draggable / resizable / rotatable objects on top."""

    selection_changed = pyqtSignal(str)
    before_change = pyqtSignal(str)
    changed = pyqtSignal()
    committed = pyqtSignal(str)
    zoom_delta = pyqtSignal(int)
    status = pyqtSignal(str)

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
        self._applied_size()

    # -- state ---------------------------------------------------------
    def set_placeholder(self, text: str) -> None:
        self._placeholder = text
        self.update()

    def set_state(self, state: ec.PageState | None) -> None:
        self._finish_inline(True, None)
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

    def _clamp_page_rect(self, rect: QRectF, minimum: float = 4.0) -> QRectF:
        left = max(0.0, min(self._page_w - minimum, rect.left()))
        top = max(0.0, min(self._page_h - minimum, rect.top()))
        right = max(left + minimum, min(self._page_w, rect.right()))
        bottom = max(top + minimum, min(self._page_h, rect.bottom()))
        return QRectF(left, top, right - left, bottom - top)

    def _move_page_rect(self, rect: QRectF) -> QRectF:
        """Slide a box so it stays on the page *without changing its size*."""
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

    def _rotate_point(self, rect: QRectF) -> QPointF:
        return QPointF(rect.center().x(), rect.top() - ROTATE_OFFSET)

    def _handle_at(self, point: QPointF) -> str:
        obj = self.selected_object()
        if obj is None:
            return ""
        rect = self._widget_rect(obj.rect)
        # On a small box the grab zones would overlap and the first one in
        # the list used to win.  Shrink the zone with the box and pick the
        # nearest handle, so clicks inside a small box move it instead.
        radius = min(HANDLE_SIZE + 4, max(4.0, min(rect.width(), rect.height()) / 2.5))
        best, best_dist = "", radius + 1
        for name, handle in self._handles(rect).items():
            delta = point - handle
            dist = math.hypot(delta.x(), delta.y())
            if dist <= radius and dist < best_dist:
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
            for point in self._handles(rect).values():
                painter.drawRect(QRectF(point.x() - HANDLE_SIZE / 2,
                                        point.y() - HANDLE_SIZE / 2,
                                        HANDLE_SIZE, HANDLE_SIZE))

            if isinstance(obj, ec.ImageObject):
                rotate = self._rotate_point(rect)
                painter.setPen(QPen(QColor(T.ACCENT), 1.2))
                painter.drawLine(QPointF(rect.center().x(), rect.top()), rotate)
                painter.setBrush(QColor(T.SURFACE))
                painter.drawEllipse(rotate, 5.2, 5.2)

        if self._new_rect is not None:
            painter.setBrush(T.alpha(T.ACCENT, 0.06))
            painter.setPen(QPen(QColor(T.ACCENT), 1.2, Qt.PenStyle.DashLine))
            painter.drawRect(self._new_rect)

        painter.end()

    # -- mouse ---------------------------------------------------------
    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus()
        point = event.position()

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
                self._drag = {"kind": "resize", "handle": handle, "start": point,
                              "moved": False, "orig": tuple(obj.rect)}
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
            elif handle == "rotate":
                self.setCursor(Qt.CursorShape.CrossCursor)
            elif self._tool == "text":
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

        kind = self._drag["kind"]
        if kind == "new_text":
            self._new_rect = QRectF(self._drag["start"], point).normalized()
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
            rect = self._move_page_rect(rect)
            obj.rect = (rect.left(), rect.top(), rect.right(), rect.bottom())
        elif kind == "resize":
            obj.rect = self._resized_rect(obj, point)
        elif kind == "rotate" and isinstance(obj, ec.ImageObject):
            center = self._drag["center"]
            angle = math.degrees(math.atan2(point.y() - center.y(),
                                            point.x() - center.x())) + 90.0
            snapped = int(round((angle % 360) / 15.0) * 15) % 360
            obj.rotate = snapped
        self.changed.emit()
        self.update()

    def _resized_rect(self, obj, point: QPointF):
        handle = self._drag["handle"]
        start_page = self._page_point(self._drag["start"])
        now_page = self._page_point(point)
        dx = now_page.x() - start_page.x()
        dy = now_page.y() - start_page.y()
        x0, y0, x1, y1 = self._drag["orig"]
        left, top, right, bottom = x0, y0, x1, y1

        if "l" in handle:
            left = min(x0 + dx, right - MIN_BOX)
        if "r" in handle:
            right = max(x1 + dx, left + MIN_BOX)
        if "t" in handle:
            top = min(y0 + dy, bottom - MIN_BOX)
        if "b" in handle:
            bottom = max(y1 + dy, top + MIN_BOX)

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

        rect = QRectF(left, top, max(MIN_BOX, right - left), max(MIN_BOX, bottom - top))
        return tuple(self._clamp_page_rect(rect, MIN_BOX).getRect())

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self._drag is None:
            return
        kind = self._drag["kind"]
        moved = bool(self._drag.get("moved"))
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

    def begin_inline_edit(self, key: str) -> None:
        obj = self._object_at(key)
        if not isinstance(obj, ec.TextObject):
            return
        self._finish_inline(True, None)
        self.before_change.emit("text")
        rect = self._widget_rect(obj.rect)
        target = QRectF(rect.x() - 3, rect.y() - 3,
                        max(90.0, rect.width() + 6), max(30.0, rect.height() + 6))
        editor = InlineEditor(obj.text, self)
        editor.setGeometry(target.toRect())
        font = QFont(T.ui_font_family() or "Sans Serif")
        font.setPixelSize(max(10, int(round(obj.font_size * self._zoom))))
        editor.setFont(font)
        editor.finished.connect(lambda ok, text, k=key: self._finish_inline(ok, text, k))
        editor.show()
        editor.setFocus()
        editor.selectAll()
        editor.setStyleSheet(
            f"QPlainTextEdit#InlineEditor{{background: {T.SURFACE};"
            f" border: 1.4px solid {T.ACCENT}; border-radius: 4px;"
            f" color: {T.TEXT}; padding: 2px;}}")
        self._inline = (key, editor)

    def _finish_inline(self, ok: bool, text: str | None, key: str | None = None) -> None:
        if self._inline is None:
            return
        open_key, editor = self._inline
        if key is not None and key != open_key:
            return
        self._inline = None
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
            self.changed.emit()
            self.committed.emit("Edited text")
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
        hint = label("Select text or an image on the page, or drag a new text box "
                     "with the Text tool.", 12, 400, T.TEXT_MUTED)
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
        self._syncing = True
        is_text = isinstance(obj, ec.TextObject)
        is_image = isinstance(obj, ec.ImageObject)
        self.empty.setVisible(obj is None)
        self.text_panel.setVisible(is_text)
        self.image_panel.setVisible(is_image)

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
        self.changed.emit()

    def _font_changed(self, _index: int) -> None:
        if self._syncing or not isinstance(self._obj, ec.TextObject):
            return
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
