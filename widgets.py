#!/usr/bin/env python3
"""Reusable UI building blocks: cards, buttons, drop zones, file lists,
page grids, toasts and progress. Everything is themed through ``theme``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import (
    QEasingCurve,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QKeyEvent,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

import theme as T
from pdf_core import FileInfo

CARD_SHADOW = (16, 24, 40, 12)


# ---------------------------------------------------------------- helpers
def enable_drag(widget: QWidget) -> None:
    widget.setAcceptDrops(True)


def soft_shadow(widget: QWidget, blur: int = 28, dy: int = 6, alpha: float = 0.055) -> None:
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, dy)
    color = QColor(16, 24, 40)
    color.setAlphaF(alpha)
    effect.setColor(color)
    widget.setGraphicsEffect(effect)


def label(text: str, size: int = 13, weight: int = 400, color: str = T.TEXT,
          role: str = "") -> QLabel:
    lab = QLabel(text)
    f = QFont(T.ui_font_family() or "Sans Serif")
    f.setPixelSize(size)
    f.setWeight(QFont.Weight(weight))
    lab.setFont(f)
    if color:
        lab.setStyleSheet(f"color: {color};")
    if role:
        lab.setObjectName(role)
    return lab


def set_role(widget: QWidget, role: str) -> QWidget:
    widget.setObjectName(role)
    return widget


def divider() -> QFrame:
    line = QFrame()
    line.setObjectName("Divider")
    line.setFrameShape(QFrame.Shape.NoFrame)
    line.setFixedHeight(1)
    return line


# ---------------------------------------------------------------- surfaces
class Card(QFrame):
    """White rounded surface with a whisper of depth."""

    def __init__(self, parent: QWidget | None = None, sunken: bool = False, pad: int = 20,
                 shadow: bool = True):
        super().__init__(parent)
        self.setObjectName("CardSunken" if sunken else "Card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(pad, pad, pad, pad)
        outer.setSpacing(14)
        if shadow and not sunken:
            soft_shadow(self)
        self.body = outer

    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        self.body.addWidget(widget, stretch)
        return widget

    def add_layout(self, layout) -> None:
        self.body.addLayout(layout)


class SectionHeader(QWidget):
    """Title + optional description + optional right-hand slot."""

    def __init__(self, title: str, description: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        column = QVBoxLayout()
        column.setSpacing(3)
        self.title = label(title, 14, 700, T.TEXT, "SectionTitle")
        column.addWidget(self.title)
        self.description: QLabel | None = None
        if description:
            self.description = label(description, 12, 400, T.TEXT_MUTED)
            self.description.setWordWrap(True)
            column.addWidget(self.description)
        row.addLayout(column, 1)
        self.slot = QHBoxLayout()
        self.slot.setSpacing(8)
        row.addLayout(self.slot)

    def set_description(self, text: str) -> None:
        if self.description is None:
            self.description = label(text, 12, 400, T.TEXT_MUTED)
            self.description.setWordWrap(True)
            self.slot.parent().layout().insertWidget(0, self.description)
        else:
            self.description.setText(text)

    def add_action(self, widget: QWidget) -> QWidget:
        self.slot.addWidget(widget)
        return widget


# ---------------------------------------------------------------- buttons
class Button(QPushButton):
    def __init__(self, text: str = "", variant: str = "default", icon: str = "",
                 parent: QWidget | None = None, tooltip: str = ""):
        super().__init__(text, parent)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.variant = variant
        self._icon_key = icon
        roles = {"primary": "Primary", "accent": "Accent", "soft": "Soft",
                 "danger": "Danger", "ghost": "Ghost", "icon": "IconButton"}
        self.setObjectName(roles.get(variant, ""))
        if icon:
            self.setIcon(T.make_icon(icon, 17, self._icon_color()))
            self.setIconSize(QSize(17, 17))
        if tooltip:
            self.setToolTip(tooltip)
        self._restyle_on_hover = variant in ("primary", "accent", "danger", "soft", "default")

    def _icon_color(self) -> str:
        return T.TEXT_ON_ACCENT if self.variant in ("primary", "accent") else T.TEXT_MUTED

    def enterEvent(self, event):
        if self._restyle_on_hover and self.isEnabled():
            color = {"primary": T.ACCENT_HOVER, "accent": T.ACCENT_HOVER,
                     "danger": T.DANGER, "soft": T.ACCENT_SOFT_HOVER}.get(self.variant)
            if color:
                self.setIcon(T.make_icon(self._icon_name(), 17, color))
        super().enterEvent(event)

    def _icon_name(self) -> str:
        return getattr(self, "_icon_key", "")

    def leaveEvent(self, event):
        if self._restyle_on_hover and self.isEnabled():
            self.setIcon(T.make_icon(self._icon_key, 17, self._icon_color()))
        super().leaveEvent(event)


def primary_button(text: str, icon: str = "", on_click=None, tooltip: str = "") -> Button:
    btn = Button(text, "primary", icon, tooltip=tooltip)
    btn._icon_key = icon
    if on_click:
        btn.clicked.connect(on_click)
    return btn


def secondary_button(text: str, icon: str = "", on_click=None, tooltip: str = "") -> Button:
    btn = Button(text, "default", icon, tooltip=tooltip)
    btn._icon_key = icon
    if on_click:
        btn.clicked.connect(on_click)
    return btn


def soft_button(text: str, icon: str = "", on_click=None, tooltip: str = "") -> Button:
    btn = Button(text, "soft", icon, tooltip=tooltip)
    btn._icon_key = icon
    if on_click:
        btn.clicked.connect(on_click)
    return btn


def danger_button(text: str, icon: str = "", on_click=None, tooltip: str = "") -> Button:
    btn = Button(text, "danger", icon, tooltip=tooltip)
    btn._icon_key = icon
    if on_click:
        btn.clicked.connect(on_click)
    return btn


def ghost_button(text: str, icon: str = "", on_click=None, tooltip: str = "") -> Button:
    btn = Button(text, "ghost", icon, tooltip=tooltip)
    btn._icon_key = icon
    if on_click:
        btn.clicked.connect(on_click)
    return btn


def icon_button(icon: str, on_click=None, tooltip: str = "", size: int = 30,
                color: str = T.TEXT_MUTED, danger: bool = False) -> Button:
    btn = Button("", "icon", tooltip=tooltip)
    btn._icon_key = icon
    if danger:
        btn.setObjectName("IconButtonDanger")
    btn.setIcon(T.make_icon(icon, int(size * 0.58), T.DANGER if danger else color))
    btn.setIconSize(QSize(int(size * 0.58), int(size * 0.58)))
    btn.setFixedSize(size, size)
    if on_click:
        btn.clicked.connect(on_click)
    return btn


class SegmentedControl(QWidget):
    """Pill-style exclusive choice."""

    changed = pyqtSignal(str)

    def __init__(self, options: list[tuple[str, str]], parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Segment")
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 4, 4, 4)
        row.setSpacing(2)
        self._buttons: dict[str, Button] = {}
        for key, text in options:
            btn = Button(text, "default")
            btn.setObjectName("SegmentItem")
            btn.setCheckable(True)
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn.clicked.connect(lambda _checked, k=key: self._pick(k))
            row.addWidget(btn)
            self._buttons[key] = btn
        if options:
            self._buttons[options[0][0]].setChecked(True)
        self._value = options[0][0] if options else ""

    def _pick(self, key: str) -> None:
        if key == self._value and self._buttons[key].isChecked():
            return
        self._value = key
        for k, btn in self._buttons.items():
            btn.setChecked(k == key)
        self.changed.emit(key)

    def value(self) -> str:
        return self._value

    def set_value(self, key: str) -> None:
        if key in self._buttons and key != self._value:
            self._pick(key)


# ---------------------------------------------------------------- inputs
class SearchField(QLineEdit):
    def __init__(self, placeholder: str = "Search", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Search")
        self.setPlaceholderText(placeholder)
        self.setFixedHeight(36)
        self.addAction(T.make_icon("search", 16, T.TEXT_SUBTLE),
                       QLineEdit.ActionPosition.LeadingPosition)


class ToggleSwitch(QWidget):
    """Small animated on/off switch."""

    toggled = pyqtSignal(bool)

    def __init__(self, checked: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self._checked = checked
        self._knob = 1.0 if checked else 0.0
        self.setFixedSize(40, 23)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._anim.finished.connect(lambda: self.update())

    def get_knob(self) -> float:
        return self._knob

    def set_knob(self, value: float) -> None:
        self._knob = value
        self.update()

    knob = pyqtProperty(float, get_knob, set_knob)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, value: bool) -> None:
        value = bool(value)
        if value == self._checked:
            return
        self._checked = value
        self._anim.stop()
        self._anim.setStartValue(self._knob)
        self._anim.setEndValue(1.0 if value else 0.0)
        self._anim.start()
        self.toggled.emit(value)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track_on, track_off = QColor(T.ACCENT), QColor(T.BORDER_STRONG)
        pos = max(0.0, min(1.0, self._knob))
        track = QColor(
            round(track_off.red() + (track_on.red() - track_off.red()) * pos),
            round(track_off.green() + (track_on.green() - track_off.green()) * pos),
            round(track_off.blue() + (track_on.blue() - track_off.blue()) * pos),
        )
        radius = self.height() / 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(QRectF(0, 0, self.width(), self.height()), radius, radius)
        knob_x = 3 + 17 * pos
        painter.setPen(QPen(QColor(0, 0, 0, 18), 1))
        painter.setBrush(QColor(T.SURFACE))
        painter.drawEllipse(QPointF(knob_x + 8.5, self.height() / 2), 8.5, 8.5)
        painter.end()

    def mousePressEvent(self, event):
        self.setChecked(not self._checked)


class FieldRow(QWidget):
    """Labelled input with a monospace path readout underneath."""

    def __init__(self, caption: str, placeholder: str = "", value: str = "",
                 parent: QWidget | None = None):
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        self.caption = label(caption, 12, 600, T.TEXT_MUTED, "FieldLabel")
        column.addWidget(self.caption)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.input = QLineEdit(value)
        self.input.setPlaceholderText(placeholder)
        row.addWidget(self.input, 1)
        self.browse = secondary_button("Browse", "folder", self._browse)
        row.addWidget(self.browse)
        column.addLayout(row)
        self._browsed = False

    def _browse(self) -> None:
        self._browsed = True

    def text(self) -> str:
        return self.input.text().strip()

    def set_text(self, value: str) -> None:
        self.input.setText(value)


# ---------------------------------------------------------------- drop zone
class DropZone(QFrame):
    """Dashed drop target that doubles as the primary call to action."""

    files_dropped = pyqtSignal(list)
    clicked = pyqtSignal()

    def __init__(self, title: str, hint: str, icon: str = "file",
                 parent: QWidget | None = None, compact: bool = False):
        super().__init__(parent)
        self.setObjectName("CardSunken")
        self._hover = False
        self._icon = icon
        self._active = True
        self.compact = compact
        self.setAcceptDrops(True)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self.badge = _IconBadge(icon, 30 if compact else 44)
        self.title = label(title, 13 if compact else 14, 600, T.TEXT)
        self.hint = label(hint, 12, 400, T.TEXT_MUTED)

        if compact:
            self.setFixedHeight(50)
            layout = QHBoxLayout(self)
            layout.setContentsMargins(16, 0, 16, 0)
            layout.setSpacing(9)
            self.title.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self.hint.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            layout.addWidget(self.badge)
            layout.addWidget(self.title)
            layout.addWidget(self.hint)
            layout.addStretch(1)
        else:
            self.setMinimumHeight(132)
            layout = QVBoxLayout(self)
            layout.setContentsMargins(18, 22, 18, 22)
            layout.setSpacing(5)
            layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row = QHBoxLayout()
            row.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row.addWidget(self.badge)
            layout.addLayout(row)
            self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(self.title)
            layout.addWidget(self.hint)

    def set_state(self, title: str, hint: str, icon: str | None = None) -> None:
        self.title.setText(title)
        self.hint.setText(hint)
        if icon:
            self._icon = icon
            self.badge.set_icon(icon)

    def set_active(self, active: bool) -> None:
        self._active = active
        self.setEnabled(active)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor) if active
                       else QCursor(Qt.CursorShape.ArrowCursor))

    def enterEvent(self, event):
        if self._active:
            self._hover = True
            self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._active and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def dragEnterEvent(self, event):
        if not self._active:
            event.ignore()
            return
        if event.mimeData().hasUrls():
            self._hover = True
            self.update()
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self._hover = False
        self.update()

    def dropEvent(self, event):
        self._hover = False
        self.update()
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        active = self._active
        hot = self._hover and active
        border = T.ACCENT if hot else (T.BORDER_STRONG if active else T.BORDER)
        width = 2.0 if hot else 1.4
        dash = active and not self.compact
        painter.setPen(QPen(QColor(border), width,
                            Qt.PenStyle.DashLine if dash else Qt.PenStyle.SolidLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        radius = T.RADIUS
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), radius, radius)
        if hot:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(T.alpha(T.ACCENT, 0.05))
            path = QPainterPath()
            path.addRoundedRect(QRectF(self.rect().adjusted(1, 1, -1, -1)), radius, radius)
            painter.drawPath(path)
        painter.end()


class _IconBadge(QWidget):
    """Rounded gradient (or flat) tile holding a single line icon."""

    def __init__(self, icon: str, size: int = 44, tone: tuple[str, str] | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._icon = icon
        self._tone = tone
        self.setFixedSize(size, size)

    def set_icon(self, icon: str) -> None:
        self._icon = icon
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0, 0, self.width(), self.height())
        painter.setPen(Qt.PenStyle.NoPen)
        if self._tone:
            painter.setBrush(QColor(self._tone[0]))
            icon_color = self._tone[1]
        else:
            gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
            gradient.setColorAt(0.0, QColor(T.ACCENT))
            gradient.setColorAt(1.0, QColor("#8B5CF6"))
            painter.setBrush(QBrush(gradient))
            icon_color = T.TEXT_ON_ACCENT
        radius = rect.height() * 0.32
        painter.drawRoundedRect(rect, radius, radius)
        inset = QRectF(rect.x() + rect.width() * 0.26, rect.y() + rect.height() * 0.26,
                      rect.width() * 0.48, rect.height() * 0.48)
        T.draw_icon(painter, self._icon, inset, icon_color, 1.9)
        painter.end()


class Badge(QLabel):
    """Small pill, e.g. page counts or file sizes."""

    def __init__(self, text: str = "", tone: str = "neutral", parent: QWidget | None = None):
        super().__init__(text, parent)
        self.set_tone(tone)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def set_tone(self, tone: str) -> None:
        palette = {
            "neutral": (T.SURFACE_ALT, T.TEXT_MUTED),
            "accent": (T.ACCENT_SOFT, T.ACCENT),
            "success": (T.SUCCESS_SOFT, T.SUCCESS),
            "danger": (T.DANGER_SOFT, T.DANGER),
            "warning": (T.WARNING_SOFT, T.WARNING),
        }
        bg, fg = palette.get(tone, palette["neutral"])
        f = QFont(T.ui_font_family() or "Sans Serif")
        f.setPixelSize(11)
        f.setWeight(QFont.Weight(600))
        self.setFont(f)
        self.setStyleSheet(
            f"background: {bg}; color: {fg}; border-radius: 6px; padding: 3px 7px;")


# ---------------------------------------------------------------- source chip
class FileChip(QFrame):
    """One loaded document in the shared source bar."""

    def __init__(self, info: FileInfo, position: int, selected: bool,
                 can_up: bool, can_down: bool, on_pick=None, on_remove=None,
                 on_move=None, parent: QWidget | None = None):
        super().__init__(parent)
        self.info = info
        self.position = position
        self._on_pick = on_pick
        self._on_remove = on_remove
        self._on_move = on_move
        self.setObjectName("SourceChipOn" if selected else "SourceChip")
        self.setFixedHeight(52)
        self.setFixedWidth(248)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 0, 6, 0)
        row.setSpacing(7)

        self.glyph = _FileGlyph()
        row.addWidget(self.glyph)

        text = QVBoxLayout()
        text.setSpacing(1)
        text.setContentsMargins(0, 0, 0, 0)
        metrics = QFontMetrics(self.font())
        self.name = label(metrics.elidedText(info.name, Qt.TextElideMode.ElideMiddle, 138),
                           12, 600, T.TEXT)
        detail = f"{info.pages} pages  ·  {info.size_text}" if info.ok else "unreadable"
        self.sub = label(metrics.elidedText(detail, Qt.TextElideMode.ElideRight, 138),
                         11, 400, T.TEXT_MUTED)
        text.addWidget(self.name)
        text.addWidget(self.sub)
        row.addLayout(text)
        row.addStretch(1)

        self.order = label(str(position), 11, 700, T.ACCENT if selected else T.TEXT_MUTED,
                          "ChipOrder")
        self.order.setFixedWidth(14)
        self.order.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.order)

        self.up = icon_button("up", lambda: self._move(-1), "Move earlier", 22)
        self.up.setEnabled(can_up)
        self.down = icon_button("down", lambda: self._move(1), "Move later", 22)
        self.down.setEnabled(can_down)
        self.remove = icon_button("close", self._remove, "Remove", 22, danger=True)
        for button in (self.up, self.down, self.remove):
            button.setFixedSize(22, 22)
            row.addWidget(button)

    def set_selected(self, selected: bool, position: int) -> None:
        self.setObjectName("SourceChipOn" if selected else "SourceChip")
        self.position = position
        self.order.setText(str(position))
        self.order.setStyleSheet(
            f"color: {T.ACCENT if selected else T.TEXT_MUTED};")
        self._restyle()

    def _move(self, delta: int) -> None:
        if self._on_move:
            self._on_move(delta)

    def _remove(self) -> None:
        if self._on_remove:
            self._on_remove()

    def _restyle(self) -> None:
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._on_pick:
            self._on_pick()
        super().mouseReleaseEvent(event)


class _FileGlyph(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(34, 34)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0, 0, self.width(), self.height())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(T.ACCENT_SOFT))
        painter.drawRoundedRect(rect, 9, 9)
        T.draw_icon(painter, "file", rect.adjusted(8, 7, -8, -7), T.ACCENT, 1.6)
        painter.end()


class _ScrollArea(QScrollArea):
    """Scrollable host without a visible frame or background."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet("QScrollArea{background:transparent;border:none;}")


class PageGridDelegate(QStyledItemDelegate):
    """Paints a thumbnail card with a page badge and selection state."""

    def __init__(self, owner: "PageGrid"):
        super().__init__(owner)
        self.owner = owner

    def sizeHint(self, option, index):
        return QSize(self.owner.card_width, self.owner.card_height)

    def paint(self, painter: QPainter, option, index):
        entry: PageEntry = index.data(Qt.ItemDataRole.UserRole)
        painter.save()
        # option.rect is where Qt actually wants this card drawn in the
        # viewport; every local coordinate below is relative to (0, 0), so
        # without this translate every card paints on top of the first one.
        painter.translate(option.rect.topLeft())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        width, height = self.owner.card_width, self.owner.card_height
        rect = QRectF(0, 0, width, height)
        radius = 11.0
        selected = entry.selected
        hovered = index.row() == self.owner.hover_row

        if selected:
            path = QPainterPath()
            path.addRoundedRect(rect, radius, radius)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(T.alpha(T.ACCENT, 0.07))
            painter.drawPath(path)

        border = T.ACCENT if selected else (T.BORDER_STRONG if hovered else T.BORDER)
        width = 2.0 if selected else 1.2
        style = Qt.PenStyle.SolidLine
        if index.row() == self.owner.drag_row:
            # the card being dragged right now
            border, width, style = T.ACCENT, 2.0, Qt.PenStyle.DashLine
        painter.setBrush(QColor(T.SURFACE))
        pen = QPen(QColor(border), width)
        pen.setStyle(style)
        painter.setPen(pen)
        painter.drawRoundedRect(rect.adjusted(0.75, 0.75, -0.75, -0.75), radius, radius)

        inner = rect.adjusted(8, 8, -8, -8)
        frame = QRectF(inner.x(), inner.y(), inner.width(), inner.height() - 22)
        painter.save()
        clip = QPainterPath()
        clip.addRoundedRect(frame, 6, 6)
        painter.setClipPath(clip)
        if entry.thumb is not None and not entry.thumb.isNull():
            scaled = entry.thumb.scaled(
                QSize(int(frame.width()), int(frame.height())),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            painter.fillRect(frame, QColor("#FAFBFD"))
            painter.drawPixmap(
                QRectF(frame.x() + (frame.width() - scaled.width()) / 2,
                       frame.y() + (frame.height() - scaled.height()) / 2,
                       scaled.width(), scaled.height()),
                scaled, QRectF(scaled.rect()))
        else:
            painter.fillRect(frame, QColor("#F7F8FC"))
            ratio = QRectF(frame.x(), frame.y(), frame.width() * 0.72, frame.height() * 0.72)
            ratio.moveCenter(frame.center())
            painter.setPen(QPen(QColor(T.BORDER), 1.4))
            painter.setBrush(QColor(T.SURFACE))
            painter.drawRoundedRect(ratio, 4, 4)
            for i in range(4):
                y = ratio.y() + 14 + i * 9
                if y < ratio.bottom() - 8:
                    painter.setPen(QPen(QColor(T.BORDER), 2))
                    painter.drawLine(QPointF(ratio.x() + 8, y), QPointF(ratio.right() - 8, y))
        painter.restore()

        if entry.is_image:
            text = "IMG"
            f = QFont(T.ui_font_family() or "Sans Serif")
            f.setPixelSize(10)
            f.setWeight(QFont.Weight(700))
            painter.setFont(f)
            metrics = painter.fontMetrics()
            pill_w = metrics.horizontalAdvance(text) + 12
            pill = QRectF(frame.left() + 3, frame.top() + 3, pill_w, 16)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(T.TEXT_MUTED))
            painter.drawRoundedRect(pill, 8, 8)
            painter.setPen(QColor(T.SURFACE))
            painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)

        if entry.rotation % 360:
            text = f"{entry.rotation:g}°"
            f = QFont(T.ui_font_family() or "Sans Serif")
            f.setPixelSize(10)
            f.setWeight(QFont.Weight(700))
            painter.setFont(f)
            metrics = painter.fontMetrics()
            pill_w = metrics.horizontalAdvance(text) + 12
            pill = QRectF(frame.right() - pill_w - 3, frame.top() + 3, pill_w, 16)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(T.ACCENT))
            painter.drawRoundedRect(pill, 8, 8)
            painter.setPen(QColor(T.TEXT_ON_ACCENT))
            painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)

        if selected:
            check = QRectF(inner.right() - 17, inner.top() - 1, 17, 17)
            painter.setPen(QPen(QColor(T.ACCENT), 1.4))
            painter.setBrush(QColor(T.ACCENT))
            painter.drawEllipse(check)
            T.draw_icon(painter, "check", check.adjusted(3.4, 3.4, -3.4, -3.4),
                        T.TEXT_ON_ACCENT, 2.4)

        painter.setPen(QColor(T.TEXT_MUTED))
        footer = QFont(T.ui_font_family() or "Sans Serif")
        footer.setPixelSize(11)
        footer.setWeight(QFont.Weight(600))
        painter.setFont(footer)
        text_rect = QRectF(inner.x(), frame.bottom() + 3, inner.width(), 18)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         entry.caption())
        painter.restore()


@dataclass(eq=False)
class PageEntry:
    """One card in the organise grid.

    Either a page of the source PDF (``index``) or a dropped image file
    (``image``), which becomes a brand new page when the tool saves.
    """

    index: int
    rotation: int = 0
    thumb: QPixmap | None = None
    selected: bool = False
    image: Path | None = None

    @property
    def is_image(self) -> bool:
        return self.image is not None

    def caption(self) -> str:
        if self.image is not None:
            name = self.image.name
            return name if len(name) <= 20 else name[:18] + "…"
        return f"Page {self.index + 1}"


class PageGrid(QListWidget):
    """Grid of page cards: click to select, drag to reorder, drop images in."""

    selection_changed = pyqtSignal()
    count_changed = pyqtSignal(int)
    reorder_moved = pyqtSignal(int, int)
    images_dropped = pyqtSignal(list, int)
    drag_state = pyqtSignal(str)

    def __init__(self, on_request_thumbs=None, image_suffixes=frozenset(),
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Strip")
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setSpacing(12)
        self.setWordWrap(False)
        self.setMouseTracking(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setIconSize(QSize(10, 10))
        self.card_width = 132
        self.card_height = 178
        self.hover_row = -1
        self.drag_row = -1
        self._entries: list[PageEntry] = []
        self._on_request_thumbs = on_request_thumbs
        self._image_suffixes = {s.lower() for s in image_suffixes}
        self._press_point: QPoint | None = None
        self._press_screen: QPoint | None = None
        self._press_row = -1
        self._drop_slot = -1
        self._after_flag = False
        self._delegate = PageGridDelegate(self)
        self.setItemDelegate(self._delegate)
        self.setGridSize(self.cell_size())
        self._empty_state: QWidget | None = None
        self.setAcceptDrops(bool(self._image_suffixes))
        if self._image_suffixes:
            self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)

    def set_entries(self, entries: list[PageEntry]) -> None:
        self.clear()
        self._entries = entries
        for entry in entries:
            self._append_item(entry)
        self.count_changed.emit(len(entries))
        if self._on_request_thumbs and entries:
            self._on_request_thumbs()
        self._sync_selection()

    def _append_item(self, entry: PageEntry) -> None:
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, entry)
        item.setSizeHint(QSize(self.card_width + 8, self.card_height + 8))
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        self.addItem(item)

    def relayout(self) -> None:
        """Re-bind cards to the current entry order without reloading thumbs."""
        for row, entry in enumerate(self._entries):
            item = self.item(row)
            if item is not None:
                item.setData(Qt.ItemDataRole.UserRole, entry)
        self.viewport().update()

    def insert_entries(self, entries: list[PageEntry], at: int) -> None:
        """Splice new cards in at ``at`` and ask for their thumbnails."""
        at = max(0, min(int(at), len(self._entries)))
        for offset, entry in enumerate(entries):
            self._entries.insert(at + offset, entry)
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, entry)
            item.setSizeHint(QSize(self.card_width + 8, self.card_height + 8))
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.insertItem(at + offset, item)
        self.count_changed.emit(len(self._entries))
        if self._on_request_thumbs:
            self._on_request_thumbs()
        self._sync_selection()

    def entries(self) -> list[PageEntry]:
        return self._entries

    def selected(self) -> list[PageEntry]:
        return [e for e in self._entries if e.selected]

    def clear_selection(self) -> None:
        for entry in self._entries:
            entry.selected = False
        self._sync_selection()

    def select_all(self) -> None:
        for entry in self._entries:
            entry.selected = True
        self._sync_selection()

    def _sync_selection(self) -> None:
        self.viewport().update()
        self.selection_changed.emit()

    def _row_at(self, point: QPoint) -> int:
        item = self.itemAt(point)
        return self.row(item) if item is not None else -1

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        point = event.position().toPoint()
        row = self._row_at(point)
        self._press_point = point
        self._press_screen = event.globalPosition().toPoint()
        self._press_row = row
        if row < 0:
            return
        entry = self._entries[row]
        modifiers = event.modifiers()
        additive = bool(modifiers & (Qt.KeyboardModifier.ShiftModifier
                                     | Qt.KeyboardModifier.ControlModifier))
        if not additive:
            for other in self._entries:
                other.selected = False
        if entry.selected and additive:
            entry.selected = False
        else:
            entry.selected = True
        self._sync_selection()

    def mouseDoubleClickEvent(self, event):
        row = self._row_at(event.position().toPoint())
        if row >= 0:
            entry = self._entries[row]
            entry.selected = not entry.selected
            self._sync_selection()

    def mouseMoveEvent(self, event):
        point = event.position().toPoint()
        row = self._row_at(point)
        if row != self.hover_row:
            self.hover_row = row
            self.viewport().update()
        if self.drag_row < 0 and self._press_row >= 0 and self._press_screen is not None:
            moved = event.globalPosition().toPoint() - self._press_screen
            if moved.manhattanLength() >= QApplication.startDragDistance():
                self.drag_row = self._press_row
                self._drop_slot = self._press_row
        if self.drag_row >= 0:
            self._drop_slot = self._slot_at(point)
            self._autoscroll(point)
            self._announce_drag()
            self.viewport().update()
            return
        super().mouseMoveEvent(event)

    def _announce_drag(self) -> None:
        """Tell the panel exactly where the dragged card would land."""
        total = len(self._entries)
        target = self._target_for_slot(self._drop_slot, self.drag_row) + 1
        self.drag_state.emit(
            f"Moving {self._entries[self.drag_row].caption()} "
            f"to position {target} of {total}")

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mouseReleaseEvent(event)
        if self.drag_row >= 0:
            source = self.drag_row
            target = self._target_for_slot(self._drop_slot, source)
            entry = self._entries[source].caption()
            self._end_drag()
            if target >= 0 and target != source:
                self.reorder_moved.emit(source, target)
            else:
                self.drag_state.emit(f"{entry} stayed at position {source + 1}")
            return
        self._press_row = -1
        self._press_point = None
        self._press_screen = None
        super().mouseReleaseEvent(event)

    def _end_drag(self) -> None:
        self.drag_row = -1
        self._drop_slot = -1
        self._press_row = -1
        self._press_point = None
        self._press_screen = None
        self.drag_state.emit("")
        self.viewport().update()

    def _autoscroll(self, point: QPoint) -> None:
        """Scroll when dragging past the top or bottom edge of the viewport."""
        bar = self.verticalScrollBar()
        margin = 34
        if point.y() < margin:
            bar.setValue(bar.value() - 2)
        elif point.y() > self.viewport().height() - margin:
            bar.setValue(bar.value() + 2)

    def _slot_at(self, point: QPoint) -> int:
        """Insertion slot (0..count) the cursor is aiming at.

        Also records ``_after_flag`` so the marker knows which side of the
        hovered card the card will land on.
        """
        count = self.count()
        self._after_flag = False
        if not count:
            return 0
        row = self._row_at(point)
        if row < 0:
            # in the gap between cards - use the closest card
            best, best_d = 0, None
            for candidate in range(count):
                centre = self.visualItemRect(self.item(candidate)).center()
                distance = ((centre.x() - point.x()) ** 2 + (centre.y() - point.y()) ** 2)
                if best_d is None or distance < best_d:
                    best, best_d = candidate, distance
            row = best
        rect = self.visualItemRect(self.item(row))
        self._after_flag = point.x() > rect.center().x()
        return row + 1 if self._after_flag else row

    def slot_for_point(self, point: QPoint) -> int:
        """Public slot lookup, clamped to a valid insertion position."""
        return max(0, min(self._slot_at(point), len(self._entries)))

    def _target_for_slot(self, slot: int, source: int) -> int:
        """Turn an insertion slot into a final list position for the drag."""
        target = slot - 1 if slot > source else slot
        return max(0, min(len(self._entries) - 1, target))

    def _marker_geometry(self) -> tuple[QRectF, QRectF] | None:
        """Where the dragged card lands: (insertion bar, tinted target cell)."""
        count = self.count()
        if not count or self._drop_slot < 0:
            return None
        slot = self._drop_slot
        if slot >= count:
            after = self.visualItemRect(self.item(count - 1))
            x = float(after.right())
            cell = QRectF(after.center().x(), after.y(), after.width() / 2, after.height())
        else:
            before = self.visualItemRect(self.item(slot))
            if self._after_flag:
                previous = self.visualItemRect(self.item(max(0, slot - 1)))
                x = float(previous.right())
                cell = QRectF(before.x(), before.y(), before.width() / 2, before.height())
            else:
                x = float(before.left())
                cell = before
        bar = QRectF(x - 2.0, cell.y(), 4.0, cell.height())
        return bar, QRectF(cell)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.drag_row < 0:
            return
        geometry = self._marker_geometry()
        if geometry is None:
            return
        bar, cell = geometry
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # tint the cell the card will occupy
        tint = QPainterPath()
        tint.addRoundedRect(cell.adjusted(3, 3, -3, -3), 10, 10)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(T.alpha(T.ACCENT, 0.16)))
        painter.drawPath(tint)
        # a solid bar at the exact insertion boundary
        marker = QPainterPath()
        marker.addRoundedRect(bar, 2.0, 2.0)
        painter.setBrush(QColor(T.ACCENT))
        painter.setPen(QPen(QColor("#FFFFFF"), 1.0))
        painter.drawPath(marker)
        painter.end()

    # -- dropping image files -------------------------------------------
    def _urls(self, event) -> list[Path]:
        if not event.mimeData().hasUrls():
            return []
        return [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]

    def _images_in(self, event) -> list[Path]:
        return [p for p in self._urls(event)
                if p.suffix.lower() in self._image_suffixes and p.is_file()]

    def dragEnterEvent(self, event):
        if self._images_in(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._images_in(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        images = self._images_in(event)
        if not images:
            event.ignore()
            return
        slot = self._slot_at(event.position().toPoint())
        self.images_dropped.emit(images, max(0, min(slot, len(self._entries))))
        event.acceptProposedAction()

    def leaveEvent(self, event):
        self.hover_row = -1
        self.viewport().update()
        super().leaveEvent(event)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Space:
            row = self.currentRow()
            if 0 <= row < len(self._entries):
                self._entries[row].selected = not self._entries[row].selected
                self._sync_selection()
            return
        if event.key() in (Qt.Key.Key_A,) and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.select_all()
            return
        if event.key() == Qt.Key.Key_Escape:
            self.clear_selection()
            return
        super().keyPressEvent(event)

    def cell_size(self) -> QSize:
        return QSize(self.card_width + 12, self.card_height + 12)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setGridSize(self.cell_size())


# ---------------------------------------------------------------- feedback
class ProgressStrip(QWidget):
    """Slim progress bar with a status line, used under the action bar."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        from PyQt6.QtWidgets import QProgressBar

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(7)
        row = QHBoxLayout()
        row.setSpacing(9)
        self.message = label("Ready", 12, 500, T.TEXT_MUTED)
        row.addWidget(self.message, 1)
        self.percent = label("0%", 12, 600, T.ACCENT)
        row.addWidget(self.percent)
        column.addLayout(row)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        column.addWidget(self.bar)
        self.hide()

        self._effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effect)
        self._effect.setOpacity(1.0)
        self._anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._anim.setDuration(200)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)

    def start(self, message: str = "Working") -> None:
        self.message.setText(message)
        self.percent.setText("0%")
        self.bar.setValue(0)
        self.show()
        self._anim.stop()
        self._effect.setOpacity(1.0)

    def update_progress(self, percent: int, message: str = "") -> None:
        self.bar.setValue(max(0, min(100, int(percent))))
        self.percent.setText(f"{int(percent)}%")
        if message:
            self.message.setText(message)

    def finish(self, message: str = "Done", success: bool = True) -> None:
        self.bar.setValue(100)
        self.percent.setText("100%")
        self.message.setText(message)
        self.message.setStyleSheet(f"color: {T.SUCCESS if success else T.DANGER};")


class Toast(QFrame):
    """Floating notification card."""

    closed = pyqtSignal()

    KINDS = {
        "success": (T.SUCCESS, T.SUCCESS_SOFT, "check"),
        "error": (T.DANGER, T.DANGER_SOFT, "alert"),
        "info": (T.ACCENT, T.ACCENT_SOFT, "info"),
        "warning": (T.WARNING, T.WARNING_SOFT, "alert"),
    }

    def __init__(self, title: str, message: str = "", kind: str = "info",
                 duration: int = 4200, parent: QWidget | None = None):
        super().__init__(parent)
        accent, soft, icon = self.KINDS.get(kind, self.KINDS["info"])
        self.setObjectName("Toast")
        self.setFixedWidth(392)
        soft_shadow(self, blur=34, dy=10, alpha=0.14)

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 13, 12, 13)
        row.setSpacing(11)

        badge = _IconBadge(icon, 30, tone=(soft, accent))
        row.addWidget(badge)

        text_column = QVBoxLayout()
        text_column.setSpacing(2)
        title_label = label(title, 13, 600, T.TEXT)
        text_column.addWidget(title_label)
        if message:
            body = label(message, 12, 400, T.TEXT_MUTED)
            body.setWordWrap(True)
            text_column.addWidget(body)
        row.addLayout(text_column, 1)
        row.addWidget(icon_button("close", self.dismiss, "Dismiss", 26))

        self.setStyleSheet(
            f"QFrame#Toast{{background: {T.SURFACE}; border: 1px solid {T.BORDER};"
            f" border-radius: {T.RADIUS}px;}}")
        self._accent = accent
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)
        if duration:
            self._timer.start(duration)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self._accent))
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, 4, self.height()), 2, 2)
        painter.drawPath(path)
        painter.end()

    def dismiss(self) -> None:
        self.closed.emit()
        self.deleteLater()


class ToastHost(QWidget):
    """Stacks toasts in the bottom-right corner of its parent window."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self._column = QVBoxLayout(self)
        self._column.setContentsMargins(0, 0, 0, 0)
        self._column.setSpacing(10)
        self._column.addStretch(1)
        self.setFixedWidth(392)
        self._live: list[Toast] = []

    def notify(self, title: str, message: str = "", kind: str = "info",
               duration: int = 4200) -> Toast:
        while len(self._live) >= 3:
            self._live.pop(0).dismiss()
        toast = Toast(title, message, kind, duration, self)
        self._live.append(toast)
        self._column.addWidget(toast)
        toast.closed.connect(lambda t=toast: self._forget(t))
        self._reposition()
        QTimer.singleShot(0, self._reposition)
        return toast

    def _forget(self, toast: Toast) -> None:
        if toast in self._live:
            self._live.remove(toast)
        self._reposition()

    def _reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        height = self.sizeHint().height()
        self.setGeometry(parent.width() - self.width() - 26,
                         parent.height() - height - 24,
                         self.width(), height)
        self.raise_()


class ResultCard(Card):
    """Success panel shown after a job finishes."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, pad=18)
        self.setVisible(False)
        row = QHBoxLayout()
        row.setSpacing(13)
        self.glyph = _IconBadge("check", 38)
        row.addWidget(self.glyph)
        column = QVBoxLayout()
        column.setSpacing(3)
        self.title = label("Done", 14, 700, T.TEXT)
        column.addWidget(self.title)
        self.detail = label("", 12, 400, T.TEXT_MUTED)
        self.detail.setWordWrap(True)
        column.addWidget(self.detail)
        row.addLayout(column, 1)
        self.stats = QVBoxLayout()
        self.stats.setSpacing(5)
        self.stats.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.size_badge = Badge("", "success")
        self.stats.addWidget(self.size_badge)
        self.open_button = soft_button("Open", "arrow_right", self._open)
        self.stats.addWidget(self.open_button)
        row.addLayout(self.stats)
        self.add_layout(row)
        self._path: Path | None = None

    def show_result(self, title: str, detail: str, size_text: str, saved: int,
                    path: Path) -> None:
        self.title.setText(title)
        self.detail.setText(detail)
        tone = "success" if saved > 0 else "neutral"
        self.size_badge.set_tone(tone)
        self.size_badge.setText(f"{size_text}  ·  -{saved}%" if saved > 0 else size_text)
        self._path = path
        self.setVisible(True)
        self.adjustSize()

    def hide_result(self) -> None:
        self.setVisible(False)
        self._path = None

    def _open(self) -> None:
        if self._path is None:
            return
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices

        if not self._path.exists() and self._path.parent.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._path.parent)))
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._path)))
