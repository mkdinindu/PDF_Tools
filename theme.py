#!/usr/bin/env python3
"""Light theme: palette, typography, global stylesheet and vector icons.

Everything visual is defined here so the look stays consistent and the whole
app could be re-skinned by editing one file.
"""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, QSizeF, Qt
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

# ---------------------------------------------------------------- palette
BG = "#F5F6FA"
SURFACE = "#FFFFFF"
SURFACE_SUNKEN = "#FAFBFD"
SURFACE_ALT = "#F1F3F9"
BORDER = "#E6E9F2"
BORDER_STRONG = "#D5DAE8"
DIVIDER = "#EFF1F7"

TEXT = "#111827"
TEXT_MUTED = "#667085"
TEXT_SUBTLE = "#98A2B3"
TEXT_ON_ACCENT = "#FFFFFF"

ACCENT = "#5B5BD6"
ACCENT_HOVER = "#4B4BC4"
ACCENT_PRESSED = "#3F3FAE"
ACCENT_SOFT = "#EDEDFB"
ACCENT_SOFT_HOVER = "#E1E1F8"

SUCCESS = "#0E9F6E"
SUCCESS_SOFT = "#E6F6F0"
DANGER = "#D92D20"
DANGER_HOVER = "#B42318"
DANGER_SOFT = "#FEECEB"
WARNING = "#DC6803"
WARNING_SOFT = "#FEF3E6"

RADIUS_SM = 8
RADIUS = 12
RADIUS_LG = 16
RADIUS_XL = 20

# ---------------------------------------------------------------- typography
_FONT_STACK = ("Inter", "Segoe UI", "SF Pro Text", "Noto Sans", "Ubuntu",
               "Cantarell", "DejaVu Sans", "Helvetica Neue", "Arial")
_resolved_font: str | None = None


def ui_font_family() -> str:
    """Pick the first installed family from the stack."""
    global _resolved_font
    if _resolved_font is not None:
        return _resolved_font
    try:
        available = set(QFontDatabase.families())
    except Exception:  # noqa: BLE001
        available = set()
    _resolved_font = next((f for f in _FONT_STACK if f in available), "")
    return _resolved_font


def font(size: int = 13, weight: int = 400, letter_spacing: float = 0.0) -> QFont:
    f = QFont(ui_font_family() or "Sans Serif")
    f.setPixelSize(size)
    f.setWeight(QFont.Weight(weight))
    if letter_spacing:
        f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 100 + letter_spacing)
    return f


def alpha(hex_color: str, value: float) -> QColor:
    color = QColor(hex_color)
    color.setAlphaF(max(0.0, min(1.0, value)))
    return color


def mix(hex_a: str, hex_b: str, ratio: float) -> QColor:
    a, b = QColor(hex_a), QColor(hex_b)
    return QColor(
        round(a.red() + (b.red() - a.red()) * ratio),
        round(a.green() + (b.green() - a.green()) * ratio),
        round(a.blue() + (b.blue() - a.blue()) * ratio),
    )


def css(color, opacity: float | None = None) -> str:
    """Colour literal for QSS. QSS cannot read a QColor repr."""
    value = QColor(color)
    if opacity is None:
        return value.name(QColor.NameFormat.HexRgb)
    value.setAlphaF(max(0.0, min(1.0, opacity)))
    return value.name(QColor.NameFormat.HexArgb)


# ---------------------------------------------------------------- icons
# Hand-drawn 24x24 stroke icons, painted at runtime so there are no assets.
# Icon command formats:
#   ("line",  [x1,y1, x2,y2, ...])        open polyline
#   ("lines", [[x1,y1,...], [x1,y1,...]]) several separate polylines
#   ("poly",  [x1,y1, x2,y2, ...])        closed polyline
#   ("rect",  (x, y, w, h, radius))
#   ("dot",   (x, y, radius))
#   ("arc",   (cx, cy, r, start_deg, end_deg[, 1]))  1 = leave the path open
_PATHS: dict[str, list] = {
    "merge": [
        ("lines", [[(4, 7), (11, 7)], [(4, 17), (11, 17)], [(13, 12), (20, 12)]]),
        ("lines", [[(15.5, 7.5), (19, 7), (19, 12), (15.5, 12)],
                   [(15.5, 12), (19, 12), (19, 17), (15.5, 17)]]),
    ],
    "split": [
        ("lines", [[(20, 7), (13, 7)], [(20, 17), (13, 17)], [(4, 12), (11, 12)]]),
        ("lines", [[(8.5, 7.5), (5, 7), (5, 12), (8.5, 12)],
                   [(8.5, 12), (5, 12), (5, 17), (8.5, 17)]]),
    ],
    "compress": [
        ("lines", [[(4, 9), (13, 9)], [(4, 15), (13, 15)], [(17, 12), (20.5, 12)]]),
        ("poly", [(10.5, 5.5), (14, 9), (10.5, 12.5)]),
        ("poly", [(10.5, 11.5), (14, 15), (10.5, 18.5)]),
    ],
    "rotate": [
        ("arc", (12, 12, 7.5, 40, 320)),
        ("poly", [(16.6, 5.4), (17.6, 10.1), (13, 9.3)]),
    ],
    "organize": [
        ("rect", (3.5, 4.5, 7, 6.5, 1.6)),
        ("rect", (13.5, 4.5, 7, 6.5, 1.6)),
        ("rect", (3.5, 13, 7, 6.5, 1.6)),
        ("rect", (13.5, 13, 7, 6.5, 1.6)),
    ],
    "lock": [
        ("rect", (4.5, 10.5, 15, 9.5, 2.4)),
        ("arc", (12, 10.5, 4.2, 180, 360, 1)),
        ("dot", (12, 15.3, 1.1)),
    ],
    "tag": [
        ("poly", [(4, 4), (4, 12), (12.5, 20.5), (20.5, 20.5), (20.5, 12.5), (12.5, 4)]),
        ("dot", (16.3, 7.7, 1.4)),
    ],
    "plus": [("lines", [[(12, 5), (12, 19)], [(5, 12), (19, 12)]])],
    "minus": [("line", [5, 12, 19, 12])],
    "check": [("poly", [5, 12.5, 10, 17.5, 19, 7])],
    "close": [("lines", [[(6, 6), (18, 18)], [(18, 6), (6, 18)]])],
    "chevron_right": [("poly", [9.5, 5, 16.5, 12, 9.5, 19])],
    "chevron_down": [("poly", [5, 9.5, 12, 16.5, 19, 9.5])],
    "chevron_up": [("poly", [5, 15, 12, 8, 19, 15])],
    "arrow_up": [("line", [12, 19, 12, 6]), ("poly", [6.5, 11.5, 12, 6, 17.5, 11.5])],
    "arrow_down": [("line", [12, 5, 12, 18]), ("poly", [6.5, 12.5, 12, 18, 17.5, 12.5])],
    "arrow_left": [("line", [19, 12, 6, 12]), ("poly", [11.5, 6.5, 6, 12, 11.5, 17.5])],
    "arrow_right": [("line", [5, 12, 18, 12]), ("poly", [12.5, 6.5, 18, 12, 12.5, 17.5])],
    "rotate_cw": [
        ("arc", (12, 12, 7.5, 35, 300)),
        ("poly", [(17.3, 3.3), (18.7, 8.1), (14, 8.3)]),
    ],
    "rotate_ccw": [
        ("arc", (12, 12, 7.5, 240, 145)),
        ("poly", [(6.7, 3.3), (5.3, 8.1), (10, 8.3)]),
    ],
    "trash": [
        ("line", [4.5, 6.5, 19.5, 6.5]),
        ("line", [9.5, 6.5, 9.5, 4.8, 14.5, 4.8, 14.5, 6.5]),
        ("line", [6.5, 6.5, 7.4, 19.5, 16.6, 19.5, 17.5, 6.5]),
        ("line", [10.3, 10, 10.6, 16.2]),
        ("line", [13.7, 10, 13.4, 16.2]),
    ],
    "folder": [("poly", [3.5, 19, 3.5, 5.5, 9.5, 5.5, 11.5, 8.5, 20.5, 8.5, 20.5, 19])],
    "file": [
        ("poly", [6, 3.5, 14, 3.5, 18.5, 8, 18.5, 20.5, 6, 20.5]),
        ("poly", [13.8, 3.6, 13.8, 8.2, 18.4, 8.2]),
    ],
    "sparkle": [
        ("poly", [12, 3.5, 13.9, 9.4, 19.8, 11.3, 13.9, 13.2, 12, 19.1,
                  10.1, 13.2, 4.2, 11.3, 10.1, 9.4]),
    ],
    "info": [
        ("arc", (12, 12, 8, 0, 360)),
        ("dot", (12, 7.6, 1.15)),
        ("line", [12, 11.5, 12, 16.5]),
    ],
    "alert": [
        ("poly", [12, 4, 20.5, 19.5, 3.5, 19.5]),
        ("line", [12, 10, 12, 14]),
        ("dot", (12, 16.6, 1.1)),
    ],
    "copy": [
        ("rect", (8.5, 3.5, 12, 12, 2.2)),
        ("poly", [15.5, 20.5, 3.5, 20.5, 3.5, 8.5]),
    ],
    "search": [
        ("arc", (10.5, 10.5, 6.2, 0, 360)),
        ("line", [15, 15, 20, 20]),
    ],
    "grid": [
        ("rect", (4, 4, 7, 7, 1.6)),
        ("rect", (13, 4, 7, 7, 1.6)),
        ("rect", (4, 13, 7, 7, 1.6)),
        ("rect", (13, 13, 7, 7, 1.6)),
    ],
    "eye": [
        ("poly", [2.5, 12, 7, 6.5, 17, 6.5, 21.5, 12, 17, 17.5, 7, 17.5]),
        ("arc", (12, 12, 3.1, 0, 360)),
    ],
    "download": [
        ("line", [12, 4, 12, 15]),
        ("poly", [7, 10.5, 12, 15.5, 17, 10.5]),
        ("poly", [4.5, 16.5, 4.5, 19.5, 19.5, 19.5, 19.5, 16.5]),
    ],
    "grip": [
        ("dot", (9.5, 7, 1.35)),
        ("dot", (14.5, 7, 1.35)),
        ("dot", (9.5, 12, 1.35)),
        ("dot", (14.5, 12, 1.35)),
        ("dot", (9.5, 17, 1.35)),
        ("dot", (14.5, 17, 1.35)),
    ],
    "shuffle": [
        ("lines", [[(4, 7), (8, 7), (16, 17), (19, 17)],
                   [(4, 17), (8, 17), (16, 7), (19, 7)]]),
        ("poly", [(16.5, 4.5), (19.5, 7), (16.5, 9.5)]),
        ("poly", [(16.5, 14.5), (19.5, 17), (16.5, 19.5)]),
    ],
    "settings": [
        ("arc", (12, 12, 3.1, 0, 360)),
        ("arc", (12, 12, 7.6, 0, 360, 1)),
    ],
}


def _paint_icon(painter: QPainter, name: str, rect: QRectF, color: QColor,
                width: float = 1.7) -> None:
    commands = _PATHS.get(name)
    if not commands:
        return
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(color)
    pen.setWidthF(width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    scale = min(rect.width(), rect.height()) / 24.0

    def pt(x: float, y: float) -> QPointF:
        return QPointF(rect.x() + x * scale, rect.y() + y * scale)

    for kind, data in commands:
        if kind == "line":
            _polyline(painter, data, pt, close=False)
        elif kind == "lines":
            for segment in data:
                _polyline(painter, segment, pt, close=False)
        elif kind == "poly":
            _polyline(painter, data, pt, close=True)
        elif kind == "rect":
            x, y, w, h, r = data
            painter.drawRoundedRect(
                QRectF(pt(x, y), QSizeF(w * scale, h * scale)), r * scale, r * scale)
        elif kind == "dot":
            painter.setBrush(color)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(pt(data[0], data[1]), data[2] * scale, data[2] * scale)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(pen)
        elif kind == "arc":
            cx, cy, r, start, end = data[:5]
            bounds = QRectF(rect.x() + (cx - r) * scale, rect.y() + (cy - r) * scale,
                            2 * r * scale, 2 * r * scale)
            if len(data) > 5:
                path = QPainterPath()
                path.arcMoveTo(bounds, start)
                path.arcTo(bounds, start, end - start)
                painter.drawPath(path)
            else:
                painter.drawArc(bounds, int(start * 16), int((end - start) * 16))
    painter.restore()


def _polyline(painter: QPainter, points, pt, close: bool) -> None:
    """Accepts either [(x, y), ...] or a flat [x1, y1, x2, y2, ...] list."""
    flat = [c for point in points for c in (point if isinstance(point, (tuple, list)) else (point,))]
    if len(flat) < 4:
        return
    path = QPainterPath(pt(flat[0], flat[1]))
    for i in range(2, len(flat) - 1, 2):
        path.lineTo(pt(flat[i], flat[i + 1]))
    if close:
        path.closeSubpath()
    painter.drawPath(path)


def make_icon(name: str, size: int = 20, color: str = TEXT_MUTED,
              width: float = 1.7) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    _paint_icon(painter, name, QRectF(0, 0, size, size), QColor(color), width)
    painter.end()
    return QIcon(pixmap)


def draw_icon(painter: QPainter, name: str, rect: QRectF, color: str = TEXT_MUTED,
              width: float = 1.7) -> None:
    _paint_icon(painter, name, rect, QColor(color), width)


# ---------------------------------------------------------------- stylesheet
def stylesheet() -> str:
    return f"""
* {{
    font-family: "{ui_font_family() or 'Sans Serif'}";
    outline: none;
}}

QWidget#Root {{
    background: {BG};
}}

/* ---------- sidebar ---------- */
QWidget#Sidebar {{
    background: {SURFACE};
    border-right: 1px solid {BORDER};
}}
QLabel#BrandMark {{
    background: transparent;
}}
QLabel#BrandName {{
    color: {TEXT};
    font-size: 15px;
    font-weight: 700;
}}
QLabel#BrandTag {{
    color: {TEXT_SUBTLE};
    font-size: 11px;
    font-weight: 500;
}}
QLabel#NavSection {{
    color: {TEXT_SUBTLE};
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1.1px;
    padding: 0 4px;
}}
QPushButton#NavItem {{
    background: transparent;
    border: none;
    border-radius: 10px;
    color: {TEXT_MUTED};
    font-size: 13px;
    font-weight: 500;
    text-align: left;
    padding: 9px 12px;
}}
QPushButton#NavItem:hover {{
    background: {SURFACE_ALT};
    color: {TEXT};
}}
QPushButton#NavItem:checked {{
    background: {ACCENT_SOFT};
    color: {ACCENT};
    font-weight: 600;
}}

/* ---------- header ---------- */
QWidget#Header {{
    background: transparent;
}}
QLabel#PageTitle {{
    color: {TEXT};
    font-size: 21px;
    font-weight: 700;
}}
QLabel#PageSubtitle {{
    color: {TEXT_MUTED};
    font-size: 13px;
}}
QLabel#SectionTitle {{
    color: {TEXT};
    font-size: 13px;
    font-weight: 700;
}}
QLabel#FieldLabel {{
    color: {TEXT_MUTED};
    font-size: 12px;
    font-weight: 600;
}}
QLabel#Hint {{
    color: {TEXT_SUBTLE};
    font-size: 12px;
}}
QLabel#Mono {{
    color: {TEXT_MUTED};
    font-size: 12px;
}}

/* ---------- cards ---------- */
QFrame#Card {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_LG}px;
}}
QFrame#CardSunken {{
    background: {SURFACE_SUNKEN};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}

/* ---------- buttons ---------- */
QPushButton {{
    background: {SURFACE};
    border: 1px solid {BORDER_STRONG};
    border-radius: 10px;
    color: {TEXT};
    font-size: 13px;
    font-weight: 600;
    padding: 9px 16px;
    min-height: 18px;
}}
QPushButton:hover {{
    background: {SURFACE_ALT};
    border-color: {css(mix(BORDER_STRONG, TEXT_SUBTLE, 0.35))};
}}
QPushButton:pressed {{
    background: {css(mix(BORDER, TEXT_SUBTLE, 0.10))};
}}
QPushButton:disabled {{
    background: {SURFACE_SUNKEN};
    border-color: {BORDER};
    color: {TEXT_SUBTLE};
}}

QPushButton#Primary {{
    background: {ACCENT};
    border: 1px solid {ACCENT};
    color: {TEXT_ON_ACCENT};
    font-weight: 600;
    padding: 11px 20px;
}}
QPushButton#Primary:hover {{
    background: {ACCENT_HOVER};
    border-color: {ACCENT_HOVER};
}}
QPushButton#Primary:pressed {{
    background: {ACCENT_PRESSED};
    border-color: {ACCENT_PRESSED};
}}
QPushButton#Primary:disabled {{
    background: {css(mix(ACCENT, SURFACE, 0.62))};
    border-color: {css(mix(ACCENT, SURFACE, 0.62))};
    color: {css(TEXT_ON_ACCENT, 0.75)};
}}

QPushButton#Accent {{
    background: {ACCENT};
    border: 1px solid {ACCENT};
    color: {TEXT_ON_ACCENT};
    font-weight: 600;
    padding: 11px 20px;
}}
QPushButton#Accent:hover {{
    background: {ACCENT_HOVER};
    border-color: {ACCENT_HOVER};
}}
QPushButton#Accent:pressed {{
    background: {ACCENT_PRESSED};
    border-color: {ACCENT_PRESSED};
}}
QPushButton#Accent:disabled {{
    background: {css(mix(ACCENT, SURFACE, 0.62))};
    border-color: {css(mix(ACCENT, SURFACE, 0.62))};
}}

QPushButton#Soft {{
    background: {ACCENT_SOFT};
    border: 1px solid transparent;
    color: {ACCENT};
    font-weight: 600;
}}
QPushButton#Soft:hover {{
    background: {ACCENT_SOFT_HOVER};
}}
QPushButton#Soft:disabled {{
    background: {SURFACE_ALT};
    color: {TEXT_SUBTLE};
}}

QPushButton#Danger {{
    background: {SURFACE};
    border: 1px solid {css(DANGER, 0.30)};
    color: {DANGER};
    font-weight: 600;
}}
QPushButton#Danger:hover {{
    background: {DANGER_SOFT};
    border-color: {css(DANGER, 0.45)};
}}
QPushButton#Danger:disabled {{
    background: {SURFACE_SUNKEN};
    border-color: {BORDER};
    color: {TEXT_SUBTLE};
}}

QPushButton#Ghost {{
    background: transparent;
    border: 1px solid transparent;
    color: {TEXT_MUTED};
    font-weight: 500;
    padding: 7px 10px;
}}
QPushButton#Ghost:hover {{
    background: {SURFACE_ALT};
    color: {TEXT};
}}
QPushButton#Ghost:disabled {{
    background: transparent;
    color: {TEXT_SUBTLE};
}}

QPushButton#IconButton {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 0;
}}
QPushButton#IconButton:hover {{
    background: {SURFACE_ALT};
}}
QPushButton#IconButton:pressed {{
    background: {css(mix(BORDER, TEXT_SUBTLE, 0.12))};
}}
QPushButton#IconButton:disabled {{
    background: transparent;
}}

QPushButton#IconButtonDanger:hover {{
    background: {DANGER_SOFT};
}}

/* ---------- segmented control ---------- */
QWidget#Segment {{
    background: {SURFACE_ALT};
    border: 1px solid {BORDER};
    border-radius: 11px;
}}
QPushButton#SegmentItem {{
    background: transparent;
    border: none;
    border-radius: 8px;
    color: {TEXT_MUTED};
    font-size: 12px;
    font-weight: 600;
    padding: 7px 14px;
}}
QPushButton#SegmentItem:hover {{
    color: {TEXT};
}}
QPushButton#SegmentItem:checked {{
    background: {SURFACE};
    color: {ACCENT};
}}

/* ---------- inputs ---------- */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox {{
    background: {SURFACE};
    border: 1px solid {BORDER_STRONG};
    border-radius: 10px;
    color: {TEXT};
    font-size: 13px;
    padding: 9px 12px;
    selection-background-color: {ACCENT_SOFT};
    selection-color: {ACCENT};
}}
QLineEdit:hover, QPlainTextEdit:hover, QSpinBox:hover {{
    border-color: {css(mix(BORDER_STRONG, TEXT_SUBTLE, 0.4))};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus {{
    border-color: {ACCENT};
    background: {SURFACE};
}}
QLineEdit:disabled, QPlainTextEdit:disabled, QSpinBox:disabled {{
    background: {SURFACE_SUNKEN};
    color: {TEXT_SUBTLE};
}}
QLineEdit#Search {{
    padding-left: 34px;
    background: {SURFACE_SUNKEN};
    border-color: transparent;
}}
QLineEdit#Search:hover {{
    background: {SURFACE_ALT};
    border-color: {BORDER};
}}
QLineEdit#Search:focus {{
    background: {SURFACE};
    border-color: {ACCENT};
}}
QSpinBox::up-button, QSpinBox::down-button {{
    width: 0;
    border: none;
}}

/* ---------- lists ---------- */
QListWidget {{
    background: transparent;
    border: none;
    font-size: 13px;
}}
QListWidget::item {{
    border: none;
}}
QListWidget::item:selected {{
    background: transparent;
}}

QListWidget#Strip {{
    background: {SURFACE_SUNKEN};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}

/* ---------- scrollbars ---------- */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px 2px 2px 0;
}}
QScrollBar::handle:vertical {{
    background: {css(mix(BORDER_STRONG, TEXT_SUBTLE, 0.30))};
    border-radius: 4px;
    min-height: 32px;
}}
QScrollBar::handle:vertical:hover {{
    background: {css(mix(BORDER_STRONG, TEXT_SUBTLE, 0.55))};
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 0 2px 2px 2px;
}}
QScrollBar::handle:horizontal {{
    background: {css(mix(BORDER_STRONG, TEXT_SUBTLE, 0.30))};
    border-radius: 4px;
    min-width: 32px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {css(mix(BORDER_STRONG, TEXT_SUBTLE, 0.55))};
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0;
    width: 0;
    border: none;
    background: none;
}}
QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent;
}}

/* ---------- progress ---------- */
QProgressBar {{
    background: {SURFACE_ALT};
    border: none;
    border-radius: 3px;
    height: 6px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    background: {ACCENT};
    border-radius: 3px;
}}

/* ---------- misc ---------- */
QToolTip {{
    background: {TEXT};
    color: #FFFFFF;
    border: none;
    border-radius: 6px;
    padding: 5px 9px;
    font-size: 12px;
}}
QCheckBox {{
    color: {TEXT_MUTED};
    font-size: 12px;
    font-weight: 500;
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 17px;
    height: 17px;
    border-radius: 5px;
    border: 1.5px solid {BORDER_STRONG};
    background: {SURFACE};
}}
QCheckBox::indicator:hover {{
    border-color: {ACCENT};
}}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}
QRadioButton {{
    color: {TEXT_MUTED};
    font-size: 13px;
    spacing: 8px;
}}
QRadioButton::indicator {{
    width: 16px;
    height: 16px;
    border-radius: 8px;
    border: 1.5px solid {BORDER_STRONG};
    background: {SURFACE};
}}
QRadioButton::indicator:checked {{
    border: 5px solid {ACCENT};
}}
QFrame#Divider {{
    background: {DIVIDER};
    max-height: 1px;
    min-height: 1px;
    border: none;
}}
"""
