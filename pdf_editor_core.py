#!/usr/bin/env python3
"""PDF page editor engine - text and image objects, Sinhala-aware.

This module is intentionally free of Qt so it can be tested and reused from
scripts.  It uses PyMuPDF for three things pypdf does not do well:

* text extraction with per-line bounding boxes,
* true redaction (the old text is removed from the content stream), and
* shaped text insertion, which is essential for Sinhala (and other complex
  scripts).  Insertion goes through MuPDF's HTML/Story layout engine and a
  bundled Noto Sans Sinhala face, so pre-base vowels and conjuncts are
  reordered correctly instead of being drawn as isolated glyphs.

Everything is non-destructive: edits live in :class:`PageState` objects and
are only written to a new file by :func:`save_edits`.
"""

from __future__ import annotations

import html
import math
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from pdf_core import JobResult, PdfToolError, human_size, noop_progress

import legacy_sinhala as legacy

# PyMuPDF renamed its import from ``fitz`` to ``pymupdf``.  Support both so the
# editor works with 1.24 (the first release with the pymupdf alias) and 2.x.
try:  # pragma: no cover - import setup
    import pymupdf as fitz  # type: ignore
    _HAS_FITZ = True
except Exception:  # noqa: BLE001
    try:  # pragma: no cover
        import fitz  # type: ignore
        _HAS_FITZ = True
    except Exception:  # noqa: BLE001
        fitz = None  # type: ignore
        _HAS_FITZ = False

ProgressFn = Callable[[int, str], None]

SINHALA_RE = re.compile(r"[\u0D80-\u0DFF]")
FONT_SUFFIXES = frozenset({".ttf", ".otf"})
BUNDLED_FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"
BUNDLED_SINHALA_REGULAR = BUNDLED_FONT_DIR / "NotoSansSinhala-Regular.ttf"
BUNDLED_SINHALA_BOLD = BUNDLED_FONT_DIR / "NotoSansSinhala-Bold.ttf"

#: Families that usually ship with a desktop and look good for Latin text.
PREFERRED_LATIN = (
    "noto sans", "dejavu sans", "liberation sans", "freesans", "arial",
    "segoe ui", "helvetica", "helvetica neue", "verdana", "tahoma",
    "noto serif", "dejavu serif", "liberation serif", "times new roman",
    "georgia",
)

#: Redaction flags, with numeric fallbacks for older PyMuPDF builds.
_REDACT_IMAGE_NONE = getattr(fitz, "PDF_REDACT_IMAGE_NONE", 0) if fitz else 0
_REDACT_IMAGE_REMOVE = getattr(fitz, "PDF_REDACT_IMAGE_REMOVE", 1) if fitz else 1
_REDACT_LINE_ART_NONE = getattr(fitz, "PDF_REDACT_LINE_ART_NONE", 0) if fitz else 0
_REDACT_TEXT_REMOVE = getattr(fitz, "PDF_REDACT_TEXT_REMOVE", 0) if fitz else 0
_REDACT_TEXT_NONE = getattr(fitz, "PDF_REDACT_TEXT_NONE", 1) if fitz else 1


class EditorError(PdfToolError):
    """An editor problem phrased for a human."""


# --------------------------------------------------------------------------
# Availability and helpers
# --------------------------------------------------------------------------
def available() -> bool:
    """True when PyMuPDF is importable and the editor can run."""
    return bool(_HAS_FITZ and fitz is not None)


def require() -> None:
    if not available():
        raise EditorError(
            "The page editor needs PyMuPDF. Install it with: "
            "python3 -m pip install PyMuPDF")


def contains_sinhala(text: str) -> bool:
    return bool(SINHALA_RE.search(text or ""))


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _rgb_from_int(value: int) -> tuple[int, int, int]:
    value = int(value)
    return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)


def _rgb_floats(color: Sequence[int]) -> tuple[float, float, float]:
    r, g, b = (int(c) for c in color[:3])
    return (r / 255.0, g / 255.0, b / 255.0)


def _hex(color: Sequence[int]) -> str:
    r, g, b = (int(c) for c in color[:3])
    return f"#{r:02x}{g:02x}{b:02x}"


def _rect_tuple(rect) -> tuple[float, float, float, float]:
    if hasattr(rect, "x0"):
        return (float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1))
    x0, y0, x1, y1 = (float(v) for v in rect)
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _rect_close(a: Sequence[float], b: Sequence[float], tol: float = 0.25) -> bool:
    return all(abs(float(x) - float(y)) <= tol for x, y in zip(a, b))


def _page_rect(page) -> "fitz.Rect":
    return page.rect


def _fitz_rect(rect) -> "fitz.Rect":
    x0, y0, x1, y1 = _rect_tuple(rect)
    return fitz.Rect(x0, y0, x1, y1)


def _clamp_rect(rect, page_rect) -> "fitz.Rect":
    x0, y0, x1, y1 = _rect_tuple(rect)
    box = fitz.Rect(
        _clamp(x0, page_rect.x0, page_rect.x1),
        _clamp(y0, page_rect.y0, page_rect.y1),
        _clamp(x1, page_rect.x0, page_rect.x1),
        _clamp(y1, page_rect.y0, page_rect.y1),
    )
    return box


def _open_doc(path: str | os.PathLike):
    require()
    p = Path(str(path)).expanduser()
    if not p.exists():
        raise EditorError(f"File not found: {p.name}")
    if p.suffix.lower() != ".pdf":
        raise EditorError(f"{p.name} is not a PDF")
    try:
        doc = fitz.open(str(p))
    except Exception as exc:  # noqa: BLE001
        raise EditorError(f"{p.name} could not be opened: {exc}") from exc
    try:
        if doc.needs_pass:
            if doc.authenticate("") == 0:
                doc.close()
                raise EditorError(f"{p.name} is password protected")
        if len(doc) == 0:
            doc.close()
            raise EditorError(f"{p.name} has no pages")
    except EditorError:
        raise
    except Exception as exc:  # noqa: BLE001
        doc.close()
        raise EditorError(f"{p.name} could not be read: {exc}") from exc
    return doc


# --------------------------------------------------------------------------
# Font discovery
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class FontChoice:
    path: str
    family: str
    style: str = "Regular"
    sinhala: bool = False
    bundled: bool = False

    @property
    def label(self) -> str:
        name = self.family
        if self.style and self.style.lower() not in ("regular", "book", "normal"):
            name = f"{name} · {self.style}"
        if self.sinhala:
            return f"{name}  (Sinhala)"
        return name

    @property
    def tooltip(self) -> str:
        return self.path


_FONT_CACHE: list[FontChoice] | None = None
_FONT_CODEPOINTS: dict[str, set[int]] = {}
_FONT_META: dict[str, "fitz.Font"] = {}


def _font_face(path: Path) -> "fitz.Font | None":
    key = str(path)
    if key in _FONT_META:
        return _FONT_META[key]
    try:
        face = fitz.Font(fontfile=str(path))
    except Exception:  # noqa: BLE001
        return None
    _FONT_META[key] = face
    return face


def _font_supports_sinhala(path: Path) -> bool:
    face = _font_face(path)
    if face is None:
        return False
    try:
        return 0x0D85 in face.valid_codepoints()
    except Exception:  # noqa: BLE001
        return False


def _font_codepoints(path: str) -> set[int]:
    if path in _FONT_CODEPOINTS:
        return _FONT_CODEPOINTS[path]
    try:
        face = fitz.Font(fontfile=path)
        points = set(face.valid_codepoints())
    except Exception:  # noqa: BLE001
        points = set()
    _FONT_CODEPOINTS[path] = points
    return points


def _fc_fonts() -> list[tuple[Path, str, str, str]]:
    """Return (path, family, style, languages) from fontconfig, if present."""
    command = shutil.which("fc-list")
    if not command:
        return []
    try:
        proc = subprocess.run(
            [command, "--format", "%{file}\t%{family}\t%{style}\t%{lang}\n"],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return []
    rows: list[tuple[Path, str, str, str]] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        raw, family, style, langs = parts[0], parts[1], parts[2], parts[3]
        path = Path(raw)
        if path.suffix.lower() not in FONT_SUFFIXES:
            continue
        rows.append((path, family.split(",")[0].strip(), style.split(",")[0].strip(), langs))
    return rows


def _append_font(store: list[FontChoice], seen: set[str], choice: FontChoice) -> None:
    key = choice.path
    if key in seen:
        return
    if not Path(key).exists():
        return
    seen.add(key)
    store.append(choice)


def font_catalog(force: bool = False) -> list[FontChoice]:
    """Bundled Sinhala faces first, then Sinhala and common system fonts."""
    global _FONT_CACHE
    if _FONT_CACHE is not None and not force:
        return list(_FONT_CACHE)

    store: list[FontChoice] = []
    seen: set[str] = set()

    if BUNDLED_SINHALA_REGULAR.exists():
        _append_font(store, seen, FontChoice(
            str(BUNDLED_SINHALA_REGULAR), "Noto Sans Sinhala", "Regular", True, True))
    if BUNDLED_SINHALA_BOLD.exists():
        _append_font(store, seen, FontChoice(
            str(BUNDLED_SINHALA_BOLD), "Noto Sans Sinhala", "Bold", True, True))

    preferred = set(PREFERRED_LATIN)
    fc_rows = _fc_fonts()
    for path, family, style, langs in fc_rows:
        is_sinhala = "si" in langs.split("|") or "sinhala" in family.lower()
        if is_sinhala and not _font_supports_sinhala(path):
            is_sinhala = False
        preferred_latin = family.lower().strip() in preferred
        if not (is_sinhala or preferred_latin):
            continue
        if not is_sinhala and not _font_supports_sinhala(path) and not preferred_latin:
            continue
        _append_font(store, seen, FontChoice(
            str(path), family or path.stem, style or "Regular", is_sinhala, False))

    # Windows and macOS have no fontconfig.  Scan their font folders even
    # though the bundled Sinhala faces are already in the store - otherwise
    # the catalog would contain only Sinhala and Latin text would fall back
    # to the Sinhala-only face.
    if not fc_rows:
        for folder in _font_folders():
            for path in _walk_fonts(folder):
                sinhala = _font_supports_sinhala(path)
                face = _font_face(path)
                raw_name = getattr(face, "name", "") if face else ""
                family, style = _split_face_name(raw_name, path.stem)
                if not sinhala and not _preferred_family(family, path.stem):
                    continue
                _append_font(store, seen, FontChoice(
                    str(path), family, style, sinhala, False))

    _FONT_CACHE = store
    return list(store)


def _font_folders() -> list[Path]:
    home = Path.home()
    return [
        Path("/usr/share/fonts"), Path("/usr/local/share/fonts"),
        home / ".fonts", home / ".local/share/fonts",
        Path("/Library/Fonts"), home / "Library/Fonts",
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
    ]


def _walk_fonts(folder: Path) -> Iterable[Path]:
    if not folder.exists():
        return
    try:
        for path in folder.rglob("*"):
            if path.is_file() and path.suffix.lower() in FONT_SUFFIXES:
                yield path
    except OSError:
        return


def default_sinhala_font() -> str:
    if BUNDLED_SINHALA_REGULAR.exists():
        return str(BUNDLED_SINHALA_REGULAR)
    for choice in font_catalog():
        if choice.sinhala:
            return choice.path
    return ""


def default_latin_font() -> str:
    wanted = ("noto sans", "dejavu sans", "liberation sans", "freesans",
              "arial", "helvetica", "segoe ui")
    catalog = font_catalog()
    for name in wanted:
        for choice in catalog:
            if (choice.family.lower().strip() == name and not choice.sinhala
                    and _is_regular(choice.style)):
                return choice.path
    for name in wanted:
        for choice in catalog:
            if choice.family.lower().strip() == name and not choice.sinhala:
                return choice.path
    for choice in catalog:
        if not choice.sinhala and _is_regular(choice.style):
            return choice.path
    for choice in catalog:
        if not choice.sinhala:
            return choice.path
    return default_sinhala_font()


def _is_regular(style: str) -> bool:
    return str(style or "").lower().strip() in ("", "regular", "book", "normal")


def _font_key(name: str) -> str:
    return "".join(ch for ch in str(name or "").lower() if ch.isalnum())


_STYLE_SUFFIXES = (
    "extra bold", "extrabold", "semi bold", "semibold", "extra light",
    "extralight", "ultra light", "ultralight", "bold italic", "bold oblique",
    "light italic", "light oblique", "medium italic", "medium oblique",
    "black", "heavy", "bold", "italic", "oblique", "regular", "book",
    "normal", "medium", "light", "thin",
)


def _split_face_name(name: str, fallback: str) -> tuple[str, str]:
    """Split a PostScript-style name into (family, style)."""
    text = str(name or "").strip() or str(fallback or "Font")
    low = text.lower()
    for style in _STYLE_SUFFIXES:
        if low.endswith(" " + style) or low.endswith("-" + style):
            family = text[:-len(style)].rstrip(" -")
            if family:
                return family, style.title()
    return text, "Regular"


def _preferred_family(family: str, stem: str = "") -> bool:
    """True when a family (or its file stem) is one of the preferred faces."""
    candidates = (_font_key(family), _font_key(stem))
    for name in PREFERRED_LATIN:
        key = _font_key(name)
        if key and any(c == key or c.startswith(key) for c in candidates if c):
            return True
    return False


#: Common PDF core fonts mapped to families that a desktop usually has.
_FONT_ALIASES = {
    "helvetica": ("liberationsans", "arial", "notosans", "freesans", "dejavusans"),
    "arial": ("liberationsans", "arial", "notosans", "freesans", "dejavusans"),
    "arialmt": ("liberationsans", "arial", "notosans", "freesans", "dejavusans"),
    "times": ("liberationserif", "timesnewroman", "notoserif", "dejavuserif"),
    "timesnewroman": ("liberationserif", "timesnewroman", "notoserif", "dejavuserif"),
    "timesnewromanpsmt": ("liberationserif", "timesnewroman", "notoserif", "dejavuserif"),
    "courier": ("liberationmono", "dejavusansmono", "notosansmono", "couriernew"),
    "couriernew": ("liberationmono", "dejavusansmono", "notosansmono", "couriernew"),
}


def font_for_item(item: "TextItem") -> str:
    """A good editable face for a line extracted from an existing PDF.

    PDFs often use a font that is not installed as a file (Helvetica, Times,
    embedded subsets).  Match the nearest family/style so replacing a line does
    not suddenly turn regular text bold, and always use the bundled Sinhala
    face for Sinhala content.
    """
    if contains_sinhala(item.text):
        return default_sinhala_font() or default_latin_font()
    catalog = [choice for choice in font_catalog() if not choice.sinhala]
    if not catalog:
        return default_sinhala_font()
    name = _font_key(item.font)
    bold = bool(int(item.flags or 0) & 16) or "bold" in item.font.lower()
    italic = (bool(int(item.flags or 0) & 2) or "italic" in item.font.lower()
              or "oblique" in item.font.lower())

    def style_ok(choice: FontChoice) -> bool:
        style = _font_key(choice.style)
        has_bold = "bold" in style or "black" in style or "heavy" in style
        has_italic = "italic" in style or "oblique" in style
        return has_bold == bold and has_italic == italic

    def family_ok(choice: FontChoice) -> bool:
        family = _font_key(choice.family)
        if not family:
            return False
        return family == name or family in name or name in family

    for choice in catalog:
        if family_ok(choice) and style_ok(choice):
            return choice.path
    for choice in catalog:
        if family_ok(choice):
            return choice.path

    wanted = list(_FONT_ALIASES.get(name, ()))
    if not wanted:
        for alias, families in _FONT_ALIASES.items():
            if alias in name or name in alias:
                wanted = list(families)
                break
    for family in wanted:
        for choice in catalog:
            if _font_key(choice.family) == family and style_ok(choice):
                return choice.path
    for family in wanted:
        for choice in catalog:
            if _font_key(choice.family) == family:
                return choice.path

    for choice in catalog:
        if style_ok(choice):
            return choice.path
    return default_latin_font()


def default_font_for(text: str) -> str:
    if contains_sinhala(text):
        return default_sinhala_font() or default_latin_font()
    return default_latin_font() or default_sinhala_font()


def font_has_sinhala(path: str | os.PathLike) -> bool:
    try:
        return 0x0D85 in _font_codepoints(str(path))
    except Exception:  # noqa: BLE001
        return False


def font_covers(path: str | os.PathLike, text: str) -> bool:
    """Best effort check that ``path`` can draw every non-space character."""
    points = _font_codepoints(str(path))
    if not points:
        return False
    return all((ord(ch) in points) or ch.isspace() for ch in text)


# --------------------------------------------------------------------------
# Page model
# --------------------------------------------------------------------------
@dataclass
class TextItem:
    id: int
    text: str
    rect: tuple[float, float, float, float]
    font: str
    size: float
    color: tuple[int, int, int]
    origin: tuple[float, float]
    flags: int = 0

    @property
    def baseline_offset(self) -> float:
        return float(self.origin[1] - self.rect[1])


@dataclass
class ImageItem:
    id: int
    rect: tuple[float, float, float, float]
    xref: int
    width: int
    height: int
    rotate: int = 0
    has_mask: bool = False

    @property
    def can_move(self) -> bool:
        return self.xref > 0


@dataclass
class PageData:
    number: int
    width: float
    height: float
    texts: list[TextItem] = field(default_factory=list)
    images: list[ImageItem] = field(default_factory=list)


@dataclass
class TextEdit:
    rect: tuple[float, float, float, float]
    text: str
    font_path: str
    font_size: float
    color: tuple[int, int, int]
    align: str = "left"
    source_rect: tuple[float, float, float, float] | None = None
    baseline_offset: float | None = None
    delete: bool = False
    is_new: bool = False


@dataclass
class ImageEdit:
    rect: tuple[float, float, float, float]
    source_rect: tuple[float, float, float, float] | None = None
    xref: int = 0
    source_path: str | None = None
    rotate: int = 0
    keep_proportion: bool = False
    delete: bool = False
    is_new: bool = False


@dataclass
class TextObject:
    """One editable line of text on a page."""

    key: str
    text: str
    rect: tuple[float, float, float, float]
    font_path: str
    font_size: float
    color: tuple[int, int, int]
    align: str = "left"
    item: TextItem | None = None
    deleted: bool = False
    orig_text: str = ""
    orig_rect: tuple[float, float, float, float] | None = None
    orig_font_path: str = ""
    orig_size: float = 0.0
    orig_color: tuple[int, int, int] = (0, 0, 0)
    orig_align: str = "left"
    label: str = ""

    @classmethod
    def from_item(cls, item: TextItem, font_path: str) -> "TextObject":
        return cls(
            key=f"t{item.id}", text=item.text, rect=item.rect,
            font_path=font_path, font_size=item.size, color=item.color,
            item=item, orig_text=item.text, orig_rect=item.rect,
            orig_font_path=font_path, orig_size=item.size,
            orig_color=item.color, orig_align="left",
            label=item.text.strip().replace("\n", " "),
        )

    @property
    def is_new(self) -> bool:
        return self.item is None

    @property
    def is_changed(self) -> bool:
        if self.deleted or self.item is None:
            return True
        if self.text != self.orig_text:
            return True
        if self.orig_rect is not None and not _rect_close(self.rect, self.orig_rect):
            return True
        if abs(self.font_size - self.orig_size) > 0.05:
            return True
        if tuple(self.color) != tuple(self.orig_color):
            return True
        if self.font_path != self.orig_font_path:
            return True
        if self.align != self.orig_align:
            return True
        return False

    def to_edit(self) -> TextEdit | None:
        if self.deleted and self.item is not None:
            return TextEdit(rect=self.item.rect, text="", font_path=self.font_path,
                            font_size=self.font_size, color=self.color,
                            source_rect=self.item.rect, delete=True)
        if self.item is None:
            if not self.text.strip():
                return None
            return TextEdit(rect=self.rect, text=self.text, font_path=self.font_path,
                            font_size=self.font_size, color=self.color,
                            align=self.align, is_new=True)
        if not self.is_changed:
            return None
        if not self.text.strip():
            return TextEdit(rect=self.item.rect, text="", font_path=self.font_path,
                            font_size=self.font_size, color=self.color,
                            source_rect=self.item.rect, delete=True)
        return TextEdit(
            rect=self.rect, text=self.text, font_path=self.font_path,
            font_size=self.font_size, color=self.color, align=self.align,
            source_rect=self.item.rect,
            baseline_offset=self.item.baseline_offset,
        )


@dataclass
class ImageObject:
    """One placed raster image on a page."""

    key: str
    rect: tuple[float, float, float, float]
    xref: int = 0
    width: int = 0
    height: int = 0
    rotate: int = 0
    keep_proportion: bool = False
    item: ImageItem | None = None
    deleted: bool = False
    replacement_path: str | None = None
    new_path: str | None = None
    orig_rect: tuple[float, float, float, float] | None = None
    orig_rotate: int = 0
    label: str = ""

    @classmethod
    def from_item(cls, item: ImageItem) -> "ImageObject":
        return cls(
            key=f"i{item.id}", rect=item.rect, xref=item.xref,
            width=item.width, height=item.height, rotate=item.rotate,
            keep_proportion=False, item=item, orig_rect=item.rect,
            orig_rotate=item.rotate,
            label=f"Image {item.width}×{item.height}",
        )

    @classmethod
    def new(cls, key: str, rect, path: str, width: int = 0, height: int = 0,
            keep_proportion: bool = True) -> "ImageObject":
        return cls(
            key=key, rect=_rect_tuple(rect), new_path=str(path),
            width=width, height=height, keep_proportion=keep_proportion,
            label=Path(str(path)).name,
        )

    @property
    def is_new(self) -> bool:
        return self.item is None and self.new_path is not None

    @property
    def can_move(self) -> bool:
        return self.item is None or self.item.can_move or bool(self.replacement_path)

    @property
    def is_changed(self) -> bool:
        if self.deleted or self.item is None or self.replacement_path:
            return True
        if self.orig_rect is not None and not _rect_close(self.rect, self.orig_rect):
            return True
        if int(self.rotate) % 360 != int(self.orig_rotate) % 360:
            return True
        return False

    def to_edit(self) -> ImageEdit | None:
        if self.deleted and self.item is not None:
            return ImageEdit(rect=self.item.rect, source_rect=self.item.rect,
                             xref=self.item.xref, delete=True)
        if self.item is None:
            if not self.new_path:
                return None
            return ImageEdit(rect=self.rect, source_path=self.new_path,
                             rotate=int(self.rotate) % 360,
                             keep_proportion=self.keep_proportion, is_new=True)
        if self.replacement_path:
            return ImageEdit(rect=self.rect, source_rect=self.item.rect,
                             source_path=self.replacement_path,
                             rotate=int(self.rotate) % 360,
                             keep_proportion=self.keep_proportion)
        if not self.is_changed:
            return None
        if not self.item.can_move:
            return None
        return ImageEdit(rect=self.rect, source_rect=self.item.rect,
                         xref=self.item.xref, rotate=int(self.rotate) % 360,
                         keep_proportion=self.keep_proportion)


@dataclass
class PageState:
    """All editable objects on one page plus their original counterparts."""

    number: int
    texts: list[TextObject] = field(default_factory=list)
    images: list[ImageObject] = field(default_factory=list)
    _new_counter: int = 0

    @classmethod
    def from_page_data(cls, data: PageData) -> "PageState":
        state = cls(number=data.number)
        for item in data.texts:
            state.texts.append(TextObject.from_item(item, font_for_item(item)))
        for item in data.images:
            state.images.append(ImageObject.from_item(item))
        return state

    def objects(self) -> list[TextObject | ImageObject]:
        """Images first, then text - matching the usual paint order."""
        return [*self.images, *self.texts]

    def find(self, key: str) -> TextObject | ImageObject | None:
        for obj in self.objects():
            if obj.key == key:
                return obj
        return None

    def next_key(self, prefix: str) -> str:
        self._new_counter += 1
        return f"{prefix}{self._new_counter}"

    def add_text(self, rect, font_path: str, font_size: float = 14.0,
                 color: tuple[int, int, int] = (17, 24, 39),
                 text: str = "") -> TextObject:
        obj = TextObject(
            key=self.next_key("nt"), text=text, rect=_rect_tuple(rect),
            font_path=font_path, font_size=font_size, color=color,
            align="left", label="New text",
        )
        self.texts.append(obj)
        return obj

    def add_image(self, rect, path: str, width: int = 0, height: int = 0,
                  keep_proportion: bool = True) -> ImageObject:
        obj = ImageObject.new(self.next_key("ni"), rect, path, width, height,
                              keep_proportion)
        self.images.append(obj)
        return obj

    def remove(self, key: str) -> None:
        for collection in (self.texts, self.images):
            for obj in collection:
                if obj.key == key:
                    if isinstance(obj, TextObject) and obj.item is not None:
                        obj.deleted = True
                    elif isinstance(obj, ImageObject) and obj.item is not None:
                        obj.deleted = True
                    else:
                        collection.remove(obj)
                    return

    def to_edits(self) -> tuple[list[TextEdit], list[ImageEdit]]:
        texts: list[TextEdit] = []
        images: list[ImageEdit] = []
        for obj in self.texts:
            edit = obj.to_edit()
            if edit is not None:
                texts.append(edit)
        for obj in self.images:
            edit = obj.to_edit()
            if edit is not None:
                images.append(edit)
        return texts, images

    @property
    def has_changes(self) -> bool:
        if any(obj.is_changed for obj in self.texts):
            return True
        return any(obj.is_changed for obj in self.images)

    def counts(self) -> tuple[int, int]:
        """(changed-or-new text count, changed-or-new image count)."""
        t = sum(1 for obj in self.texts if obj.is_changed)
        i = sum(1 for obj in self.images if obj.is_changed)
        return t, i


# --------------------------------------------------------------------------
# Reading documents
# --------------------------------------------------------------------------
def page_data(path: str | os.PathLike, index: int) -> PageData:
    doc = _open_doc(path)
    try:
        if index < 0 or index >= len(doc):
            raise EditorError(f"Page {index + 1} does not exist")
        page = doc[index]
        rect = page.rect
        texts = _extract_text(page)
        images = _extract_images(page)
        return PageData(index, float(rect.width), float(rect.height), texts, images)
    finally:
        doc.close()


def _extract_text(page) -> list[TextItem]:
    try:
        data = page.get_text("dict", sort=False)
    except Exception:  # noqa: BLE001
        return []
    items: list[TextItem] = []
    seen: set[tuple] = set()
    # Identify legacy Sinhala fonts from everything on the page first, so
    # short lines in the same font decode even without tell-tale characters.
    legacy.learn_fonts(
        (str(sp.get("text", "")), str(sp.get("font", "")))
        for blk in data.get("blocks", []) if blk.get("type") == 0
        for ln in blk.get("lines", []) for sp in ln.get("spans", []))
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            all_spans = list(line.get("spans", []))
            spans = [s for s in all_spans if str(s.get("text", "")).strip()]
            if not spans:
                continue
            # Legacy Sinhala fonts (FM Abhaya, Bindumathi, ...) extract as Latin
            # look-alike bytes.  Decode each span with its own font so mixed
            # lines (Sinhala + digits/Latin) come out right, and keep the
            # whitespace-only spans so words are not glued together.
            text = "".join(
                legacy.decode_for_font(str(s.get("text", "")), str(s.get("font", "")))
                for s in all_spans)
            if not text.strip():
                continue
            bbox = _rect_tuple(line.get("bbox") or spans[0].get("bbox"))
            if bbox[2] - bbox[0] < 0.5 or bbox[3] - bbox[1] < 0.5:
                continue
            first = spans[0]
            origin = first.get("origin") or (bbox[0], bbox[3])
            size = max(float(s.get("size", 0) or 0) for s in spans)
            key = (round(bbox[0], 1), round(bbox[1], 1), round(bbox[2], 1),
                   round(bbox[3], 1), text)
            if key in seen:
                continue
            seen.add(key)
            items.append(TextItem(
                id=len(items), text=text, rect=bbox,
                font=str(first.get("font", "")), size=size or 10.0,
                color=_rgb_from_int(first.get("color", 0)),
                origin=(float(origin[0]), float(origin[1])),
                flags=int(first.get("flags", 0)),
            ))
    items.sort(key=lambda t: (round(t.rect[1], 1), round(t.rect[0], 1)))
    for n, item in enumerate(items):
        item.id = n
    return items


def _image_rotation(transform: Sequence[float] | None) -> int:
    if not transform or len(transform) < 4:
        return 0
    a, b = float(transform[0]), float(transform[1])
    if not a and not b:
        return 0
    angle = math.degrees(math.atan2(b, a)) % 360
    return min((0, 90, 180, 270), key=lambda value: abs(value - angle))


def _extract_images(page) -> list[ImageItem]:
    try:
        infos = page.get_image_info(xrefs=True)
    except TypeError:  # pragma: no cover - very old PyMuPDF
        infos = page.get_image_info()
    except Exception:  # noqa: BLE001
        return []
    items: list[ImageItem] = []
    seen: set[tuple] = set()
    for info in infos:
        bbox = info.get("bbox")
        if not bbox:
            continue
        rect = _rect_tuple(bbox)
        if rect[2] - rect[0] < 2 or rect[3] - rect[1] < 2:
            continue
        xref = int(info.get("xref") or 0)
        key = (round(rect[0], 1), round(rect[1], 1), round(rect[2], 1),
               round(rect[3], 1), xref)
        if key in seen:
            continue
        seen.add(key)
        items.append(ImageItem(
            id=len(items), rect=rect, xref=xref,
            width=int(info.get("width") or 0),
            height=int(info.get("height") or 0),
            rotate=_image_rotation(info.get("transform")),
            has_mask=bool(info.get("has-mask", False)),
        ))
    # Large background images first, so foreground images sit above them.
    items.sort(key=lambda i: -((i.rect[2] - i.rect[0]) * (i.rect[3] - i.rect[1])))
    for n, item in enumerate(items):
        item.id = n
    return items


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
@dataclass
class RenderResult:
    png: bytes
    width: int
    height: int
    page_width: float
    page_height: float


def render_page(path: str | os.PathLike, index: int, zoom: float = 1.5,
                state: PageState | None = None) -> RenderResult:
    """Render one page to PNG, optionally applying a page's pending edits."""
    require()
    doc = _open_doc(path)
    try:
        if index < 0 or index >= len(doc):
            raise EditorError(f"Page {index + 1} does not exist")
        page = doc[index]
        if state is not None and state.has_changes:
            _apply_page_state(page, state)
        return _render_page_object(page, zoom)
    finally:
        doc.close()


def render_thumbnails(path: str | os.PathLike, max_width: int = 110,
                      limit: int | None = None) -> list[bytes]:
    """Small PNGs for the page rail.  One document open for all pages."""
    require()
    doc = _open_doc(path)
    out: list[bytes] = []
    try:
        total = len(doc) if limit is None else min(len(doc), max(0, limit))
        for index in range(total):
            page = doc[index]
            width = max(1.0, page.rect.width)
            zoom = max(0.05, min(1.0, float(max_width) / width))
            try:
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                out.append(pix.tobytes("png"))
            except Exception:  # noqa: BLE001
                out.append(b"")
    finally:
        doc.close()
    return out


def _render_page_object(page, zoom: float) -> RenderResult:
    zoom = _clamp(float(zoom), 0.1, 8.0)
    try:
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    except Exception as exc:  # noqa: BLE001
        raise EditorError(f"Could not render the page: {exc}") from exc
    return RenderResult(
        png=pix.tobytes("png"), width=pix.width, height=pix.height,
        page_width=float(page.rect.width), page_height=float(page.rect.height),
    )


def image_preview(path: str | os.PathLike, xref: int, max_size: int = 260) -> bytes:
    """A small PNG of an embedded image, for the inspector."""
    if not xref:
        return b""
    require()
    doc = _open_doc(path)
    try:
        pix = fitz.Pixmap(doc, int(xref))
        if pix.alpha:
            pix = fitz.Pixmap(pix, 0)
        if pix.colorspace and pix.colorspace.n not in (1, 3):
            pix = fitz.Pixmap(fitz.csRGB, pix)
        longest = max(pix.width, pix.height) or 1
        if longest > max_size:
            scale = max_size / longest
            pix = fitz.Pixmap(pix, 0) if pix.alpha else pix
            pix = fitz.Pixmap(pix, int(pix.width * scale), int(pix.height * scale), None)
        return pix.tobytes("png")
    except Exception:  # noqa: BLE001
        return b""
    finally:
        doc.close()


def file_preview(path: str | os.PathLike, max_size: int = 260) -> bytes:
    """A small PNG of an image file, for the inspector."""
    try:
        from io import BytesIO

        from PIL import Image, ImageOps
    except ImportError:  # pragma: no cover
        return b""
    try:
        with Image.open(path) as handle:
            handle.seek(0)
            image = ImageOps.exif_transpose(handle)
            if image.mode in ("RGBA", "LA", "P"):
                flat = Image.new("RGB", image.size, (255, 255, 255))
                rgba = image.convert("RGBA")
                flat.paste(rgba, mask=rgba.split()[-1])
                image = flat
            elif image.mode != "RGB":
                image = image.convert("RGB")
            image.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
            buffer = BytesIO()
            image.save(buffer, "PNG")
            return buffer.getvalue()
    except Exception:  # noqa: BLE001
        return b""


def image_dimensions(path: str | os.PathLike) -> tuple[int, int]:
    try:
        from PIL import Image
        with Image.open(path) as handle:
            return handle.size
    except Exception:  # noqa: BLE001
        return (0, 0)


# --------------------------------------------------------------------------
# Applying edits
# --------------------------------------------------------------------------
def _apply_page_state(page, state: PageState) -> list[str]:
    warnings: list[str] = []
    text_edits, image_edits = state.to_edits()
    page_rect = page.rect

    text_rects = []
    for edit in text_edits:
        if edit.source_rect:
            box = _clamp_rect(edit.source_rect, page_rect)
            if box.width > 0 and box.height > 0:
                text_rects.append(box)
    if text_rects:
        for box in text_rects:
            try:
                page.add_redact_annot(box, fill=False)
            except TypeError:  # older builds want a colour
                page.add_redact_annot(box)
        try:
            page.apply_redactions(images=_REDACT_IMAGE_NONE,
                                  graphics=_REDACT_LINE_ART_NONE,
                                  text=_REDACT_TEXT_REMOVE)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Could not clear text: {exc}")

    image_rects = []
    for edit in image_edits:
        if edit.source_rect:
            box = _clamp_rect(edit.source_rect, page_rect)
            if box.width > 0 and box.height > 0:
                image_rects.append(box)
    if image_rects:
        for box in image_rects:
            try:
                page.add_redact_annot(box, fill=False)
            except TypeError:  # pragma: no cover
                page.add_redact_annot(box)
        try:
            page.apply_redactions(images=_REDACT_IMAGE_REMOVE,
                                  graphics=_REDACT_LINE_ART_NONE,
                                  text=_REDACT_TEXT_NONE)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Could not clear image: {exc}")

    for edit in image_edits:
        if edit.delete:
            continue
        target = _clamp_rect(edit.rect, page_rect)
        if target.width < 1 or target.height < 1:
            continue
        try:
            if edit.source_path:
                page.insert_image(target, filename=str(edit.source_path),
                                  keep_proportion=bool(edit.keep_proportion),
                                  rotate=int(edit.rotate) % 360, overlay=True)
            elif edit.xref:
                page.insert_image(target, xref=int(edit.xref),
                                  keep_proportion=bool(edit.keep_proportion),
                                  rotate=int(edit.rotate) % 360, overlay=True)
            else:
                warnings.append("An inline image could not be moved")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Could not place image: {exc}")

    for edit in text_edits:
        if edit.delete or not edit.text.strip():
            continue
        warnings.extend(_insert_text(page, edit, page_rect))
    return warnings


def _insert_text(page, edit: TextEdit, page_rect) -> list[str]:
    text = edit.text.replace("\t", "    ")
    if not text.strip():
        return []
    font_path = edit.font_path
    if not font_path or not Path(font_path).exists():
        font_path = default_font_for(text)
    if not font_path or not Path(font_path).exists():
        return ["No font available for the new text"]

    try:
        face = fitz.Font(fontfile=font_path)
        ascender = float(face.ascender)
        descender = float(face.descender)
    except Exception:  # noqa: BLE001
        ascender, descender = 1.0, -0.25

    size = _clamp(float(edit.font_size or 12.0), 4.0, 300.0)
    rect = _clamp_rect(edit.rect, page_rect)
    width = max(rect.width, size * 1.5, 8.0)
    if edit.baseline_offset is not None:
        # Match the original baseline, not the top of the original bbox.
        top = rect.y0 + float(edit.baseline_offset) - ascender * size - 0.5
    else:
        top = rect.y0
    height = max(float(rect.height), (ascender - descender) * size + 2.0)
    height = max(height, size * 1.35)

    css = (
        "@font-face {font-family: edfont; src: url(%s);}\n"
        "* {font-family: edfont, sans-serif; margin: 0; padding: 0;}\n"
    )
    body = html.escape(text).replace("\n", "<br/>")
    markup = (
        f'<div style="font-size:{size:.2f}px; color:{_hex(edit.color)}; '
        f'text-align:{edit.align}; line-height:1.3; white-space:pre-wrap;">'
        f"{body}</div>"
    )

    try:
        arc = fitz.Archive()
        arc_name = f"edfont{abs(hash(font_path)) % 100000}{Path(font_path).suffix}"
        arc.add(str(font_path), arc_name)
    except Exception:  # noqa: BLE001
        arc = None

    if arc is not None:
        css = css % arc_name
        for _attempt in range(6):
            target = fitz.Rect(rect.x0, top, rect.x0 + width, top + height)
            try:
                spare, scale = page.insert_htmlbox(
                    target, markup, css=css, archive=arc,
                    scale_low=0.75, overlay=True)
            except Exception:  # noqa: BLE001
                break
            if spare >= 0:
                # A positive spare means the text was drawn, even if MuPDF
                # scaled it down a little to fit.  Retrying here would draw
                # the same text a second time.
                return []
            # MuPDF did not insert anything when it cannot fit the text, so
            # giving the box more room and trying again is safe.  Widen it
            # (up to the page edge) before growing it taller, so a narrow
            # box never makes the text vanish from the preview.
            room = max(page_rect.x1 - rect.x0, width)
            if width < room and _attempt < 2:
                width = min(room, width * 1.7)
            else:
                height *= 1.8
            if height > page_rect.height * 2:
                break

    # Last resort: a plain textbox.  This does not shape complex scripts, but
    # it keeps the editor usable on unusual fonts / malformed pages.
    try:
        fallback = fitz.Rect(rect.x0, top, rect.x0 + width, top + height)
        remaining = page.insert_textbox(
            fallback, text, fontsize=size, fontfile=font_path,
            fontname=f"ed{abs(hash(font_path)) % 10000}",
            color=_rgb_floats(edit.color), align=_align_code(edit.align),
            overlay=True)
        if remaining >= 0:
            return ["Text was placed without complex-script shaping"]
    except Exception:  # noqa: BLE001
        pass
    return ["The new text did not fit in its box - try making it wider"]


def _align_code(align: str) -> int:
    return {"left": 0, "center": 1, "right": 2, "justify": 3}.get(align, 0)


# --------------------------------------------------------------------------
# Saving
# --------------------------------------------------------------------------
def save_edits(input_path: str | os.PathLike, output_path: str | os.PathLike,
               states: dict[int, PageState],
               progress: ProgressFn = noop_progress) -> JobResult:
    require()
    src = Path(str(input_path)).expanduser()
    if not src.exists():
        raise EditorError("Select a PDF first")
    raw = str(output_path).strip() if output_path is not None else ""
    if not raw:
        out = src.with_name(f"{src.stem}_edited.pdf")
    else:
        out = Path(raw).expanduser()
        if not out.suffix:
            out = out.with_suffix(".pdf")
    if out.absolute() == src.absolute():
        raise EditorError("Pick a different name - that would overwrite the original")
    parent = out.parent if str(out.parent) else Path(".")
    if not parent.exists():
        raise EditorError(f"Folder does not exist: {parent}")
    if not os.access(parent, os.W_OK):
        raise EditorError(f"No permission to write in {parent}")

    edited_pages = sorted(n for n, state in states.items() if state.has_changes)
    if not edited_pages:
        raise EditorError("There are no changes to save")

    doc = _open_doc(src)
    warnings: list[str] = []
    total = len(doc)
    try:
        for step, index in enumerate(edited_pages):
            if index < 0 or index >= total:
                continue
            progress(5 + int((step + 1) / max(1, len(edited_pages)) * 80),
                     f"Applying page {index + 1} of {total}")
            warnings.extend(_apply_page_state(doc[index], states[index]))
        tmp = out.with_name(f".{out.name}.part")
        progress(90, "Writing the new PDF")
        try:
            doc.save(str(tmp), garbage=3, deflate=True)
        except Exception as exc:  # noqa: BLE001
            tmp.unlink(missing_ok=True)
            raise EditorError(f"Could not write {out.name}: {exc}") from exc
    finally:
        doc.close()
    try:
        os.replace(tmp, out)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise EditorError(f"Could not save {out.name}: {exc.strerror or exc}") from exc

    text_changes = sum(states[n].counts()[0] for n in edited_pages)
    image_changes = sum(states[n].counts()[1] for n in edited_pages)
    parts = [f"{len(edited_pages)} page{'s' if len(edited_pages) != 1 else ''} edited"]
    if text_changes:
        parts.append(f"{text_changes} text change{'s' if text_changes != 1 else ''}")
    if image_changes:
        parts.append(f"{image_changes} image change{'s' if image_changes != 1 else ''}")
    if warnings:
        parts.append(warnings[0])
    progress(100, "Done")
    return JobResult(out, total, src.stat().st_size, out.stat().st_size,
                     detail=", ".join(parts))


def edited_summary(states: dict[int, PageState]) -> tuple[int, int, int]:
    """(edited pages, text changes, image changes) - for the UI status line."""
    pages = [n for n, state in states.items() if state.has_changes]
    texts = sum(states[n].counts()[0] for n in pages)
    images = sum(states[n].counts()[1] for n in pages)
    return len(pages), texts, images
