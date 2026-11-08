#!/usr/bin/env python3
"""Turn legacy Sinhala font encodings back into Unicode.

Word-processor era Sinhala fonts (FM Abhaya, FM Malithi, DL-Manel, Kaputa,
Amalee, Thibus) store Sinhala glyphs at ASCII/Latin code points.  A PDF made
with one of those fonts extracts as strings such as ``o<od ud,s.dj`` instead
of ``දළදා මාලිගාව``.  The mappings in ``assets/legacy`` map those byte
sequences back to Unicode, using greedy longest-match, so the editor shows
real Sinhala in its text box while leaving already-Unicode text untouched.

Mapping licences:
* ``fm_abhaya.json`` - MIT, akuruAI/pandukabhaya
* ``amalee``, ``dl_manel``, ``kaputa``, ``thibus`` - MIT,
  nuwanperera-me/sinhala-script-kit
* ``malithi.json`` - derived from the two sources above
See ``assets/legacy/README.md`` for the notices.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "assets" / "legacy"

#: Font-name fragments (compared after stripping punctuation) -> mapping name.
#: Specific names come first; FM Malithi must be checked before any generic
#: FM rule.
_FONT_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("malithi", ("fmmalithi", "fmmalith", "malithi", "malith")),
    ("fm_abhaya", ("fmabhaya", "fmabaya", "abhaya", "achintha")),
    ("amalee", ("amalee",)),
    ("dl_manel", ("dlmanel",)),
    ("kaputa", ("kaputa",)),
    ("thibus", ("thibus",)),
)

_SINHALA_RE = re.compile(r"[\u0D80-\u0DFF]")
_CACHE: dict[str, "Ruleset | None"] = {}
_LOCK = threading.Lock()


def _contains_sinhala(text: str) -> bool:
    return bool(_SINHALA_RE.search(text or ""))


_SUBSET_TAG = re.compile(r"^[A-Z]{6}\+")


def _font_key(name: str) -> str:
    name = _SUBSET_TAG.sub("", str(name or ""))
    return "".join(ch for ch in name.lower() if ch.isalnum())


#: Ordinary Latin/system faces.  Text in these is never guessed to be legacy
#: Sinhala, so French or Spanish accents are not mistaken for FM bytes.
_LATIN_FONT_HINTS = (
    "arial", "times", "helvetica", "calibri", "cambria", "courier", "verdana",
    "tahoma", "georgia", "liberation", "dejavu", "noto", "roboto", "opensans",
    "segoe", "symbol", "garamond", "palatino", "consolas", "trebuchet",
    "lucida", "century", "cmr", "cmsy", "cmmi", "calisto", "minion",
    "myriad", "bookantiqua", "comicsans", "wingdings", "zapf",
)

#: Characters that legacy encodings use but ordinary English text does not.
_MARKER_CHARS = frozenset(
    [chr(c) for c in range(0x00A1, 0x0100)]
    + list("\u0152\u0153\u0160\u0161\u0178\u017d\u017e\u0192\u02c6\u02dc"
           "\u2013\u2014\u2018\u2019\u201a\u201c\u201d\u201e\u2020\u2021"
           "\u2022\u2026\u2030\u2039\u203a\u2122")
)


def _build_trie(rules) -> dict:
    """Build a longest-match trie from (pattern, replacement) pairs."""
    root: dict = {}
    for pattern, replacement in rules:
        if not pattern:
            continue
        node = root
        for char in str(pattern):
            node = node.setdefault(char, {})
        node[None] = str(replacement)
    return root


def _convert(text: str, trie: dict) -> str:
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        node = trie
        cursor = index
        best = None
        best_end = index
        while cursor < length and text[cursor] in node:
            node = node[text[cursor]]
            cursor += 1
            if None in node:
                best = node[None]
                best_end = cursor
        if best is None:
            # No rule matched; keep the character (spaces, digits, Latin...).
            out.append(text[index])
            index += 1
        else:
            out.append(best)
            index = best_end
    return "".join(out)


def _pairs_from(obj) -> list:
    """Accept a dict {pattern: replacement} or a list of [pattern, replacement]."""
    if not obj:
        return []
    if isinstance(obj, dict):
        return [(k, v) for k, v in obj.items()]
    return [(item[0], item[1]) for item in obj]


class Ruleset:
    """One legacy encoding, ready to decode strings."""

    def __init__(self, path: Path):
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        mappings = data.get("mappings") if isinstance(data, dict) else None
        if mappings:
            # pandukabhaya format: optional reordering rules, then
            # single-character and multi-character substitutions.
            self.pre_rules: dict[str, str] = dict(mappings.get("rules") or {})
            pairs = _pairs_from(mappings.get("singles"))
            pairs += _pairs_from(mappings.get("combos"))
        else:
            self.pre_rules = {}
            pairs = _pairs_from(data.get("rules"))
        # Regex alternation takes the FIRST match, not the longest, so the
        # keys must be tried longest-first or reordering rules misfire.
        keys = sorted(self.pre_rules, key=len, reverse=True)
        self.pre_pattern = (
            re.compile("|".join(re.escape(key) for key in keys)) if keys else None
        )
        self.trie = _build_trie(pairs)
        if not self.trie:
            raise ValueError("mapping file contains no rules")

    def decode(self, text: str) -> str:
        if self.pre_pattern is not None:
            text = self.pre_pattern.sub(
                lambda match: self.pre_rules[match.group(0)], text)
        return _convert(text, self.trie)


def _search_dirs() -> list[Path]:
    """Places the mapping files may live (source tree, cwd, frozen bundle)."""
    dirs: list[Path] = []
    env = os.environ.get("SINHALA_LEGACY_DIR")
    if env:
        dirs.append(Path(env))
    here = Path(__file__).resolve().parent
    dirs += [DATA_DIR, here / "legacy", here,
             Path.cwd() / "assets" / "legacy", Path.cwd() / "legacy"]
    for base in (sys.argv[0] if sys.argv and sys.argv[0] else None,
                 sys.executable):
        if base:
            root = Path(base).resolve().parent
            dirs += [root / "assets" / "legacy", root / "_internal" / "assets" / "legacy"]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        dirs.append(Path(bundle) / "assets" / "legacy")
    seen, out = set(), []
    for folder in dirs:
        if folder not in seen:
            seen.add(folder)
            out.append(folder)
    return out


def _mapping_path(name: str) -> Path:
    for folder in _search_dirs():
        candidate = folder / f"{name}.json"
        if candidate.exists():
            return candidate
    return DATA_DIR / f"{name}.json"


#: mapping name -> why it could not be loaded (file present but unreadable).
_LOAD_ERRORS: dict[str, str] = {}


def missing_mapping_report() -> str:
    """Non-empty when a legacy font was met but its mapping could not be used."""
    parts = []
    if _WARNED:
        parts.append(f"Sinhala mapping files not found ({', '.join(sorted(_WARNED))}). "
                     f"Put the assets/legacy folder next to the app: {DATA_DIR}")
    for name, why in sorted(_LOAD_ERRORS.items()):
        parts.append(f"Mapping '{name}.json' could not be read: {why}")
    return "  ".join(parts)


def _ruleset(name: str) -> Ruleset | None:
    with _LOCK:
        if _CACHE.get(name) is not None:
            return _CACHE[name]
        path = _mapping_path(name)
        if not path.exists():
            return None            # not cached: it may be installed later
        if name in _CACHE:         # present but broken: do not retry endlessly
            return None
        try:
            value: Ruleset | None = Ruleset(path)
            _LOAD_ERRORS.pop(name, None)
        except Exception as exc:  # noqa: BLE001
            _LOAD_ERRORS[name] = f"{type(exc).__name__}: {exc}"
            value = None
        _CACHE[name] = value
        return value


def encoding_for_font(font_name: str) -> str | None:
    """Return the legacy encoding name for a PDF font, if it is recognised."""
    key = _font_key(font_name)
    if not key:
        return None
    for name, aliases in _FONT_ALIASES:
        if any(alias in key for alias in aliases):
            return name
    # Every FM-series face (Bindumathi, Emanee, Derana, Basuru, Samanala,
    # Ganganee, ...) shares the FM Abhaya key layout.
    if key.startswith("fm") and not key.startswith("fmono"):
        return "fm_abhaya"
    return None


def _is_latin_font(font_name: str) -> bool:
    key = _font_key(font_name)
    return any(hint in key for hint in _LATIN_FONT_HINTS)


def _sinhala_ratio(text: str) -> float:
    """Share of Sinhala among letters (digits, spaces, ASCII punctuation ignored)."""
    chars = [ch for ch in text
             if not ch.isspace() and not ch.isdigit()
             and not (ord(ch) < 128 and not ch.isalpha())]
    if not chars:
        return 0.0
    return sum(1 for ch in chars if _SINHALA_RE.match(ch)) / len(chars)


def _guess(text: str) -> tuple[str | None, str]:
    """Best (encoding, decoded) for text of unknown origin, or (None, text).

    Needs at least two characters such as o-slash, O-circumflex, u-acute that
    plain English never uses, and an overwhelmingly Sinhala result.
    """
    text = _normalise(text)
    if sum(1 for ch in text if ch in _MARKER_CHARS) < 2:
        return None, text
    best_enc, best, best_score = None, text, 0.0
    for name, _aliases in _FONT_ALIASES:
        if _ruleset(name) is None:
            _WARNED.add(name)
            continue
        try:
            candidate = decode(text, name)
        except Exception:  # noqa: BLE001
            continue
        score = _sinhala_ratio(candidate)
        if score > best_score:
            best_enc, best, best_score = name, candidate, score
    if best_score >= 0.7:
        return best_enc, best
    return None, text


def guess_decode(text: str) -> str:
    return _guess(text)[1]


#: font key -> encoding, learned from other text set in the same font.
_LEARNED: dict[str, str] = {}


def learn_fonts(spans) -> None:
    """Work out which unrecognised fonts are legacy Sinhala.

    *spans* is an iterable of (text, font_name).  All text in one font is
    pooled, so a font whose short lines carry no tell-tale characters is still
    identified from its other lines, and then decodes everywhere.
    """
    pooled: dict[str, list[str]] = {}
    for text, font in spans:
        key = _font_key(font)
        if (not key or key in _LEARNED or encoding_for_font(font)
                or _is_latin_font(font) or _contains_sinhala(str(text))):
            continue
        pooled.setdefault(key, []).append(str(text))
    for key, parts in pooled.items():
        encoding, _decoded = _guess(" ".join(parts))
        if encoding:
            _LEARNED[key] = encoding


_WARNED: set[str] = set()


def _normalise(text: str) -> str:
    """Undo how PDF extractors disguise legacy bytes.

    Symbolic TrueType fonts often surface as U+F020..U+F0FF (private use,
    code + 0xF000); non-breaking spaces stand in for spaces.
    """
    return "".join(
        chr(ord(ch) - 0xF000) if 0xF020 <= ord(ch) <= 0xF0FF
        else (" " if ch == "\u00a0" else ch)
        for ch in text)


def decode(text: str, encoding: str) -> str:
    """Decode a legacy-encoded string; unknown characters pass through."""
    text = _normalise(text)
    ruleset = _ruleset(encoding)
    if ruleset is None and encoding not in _WARNED:
        _WARNED.add(encoding)
        print(f"legacy_sinhala: mapping '{encoding}.json' not found in "
              f"{DATA_DIR} - legacy Sinhala text cannot be decoded",
              file=sys.stderr)
    if ruleset is None or not text:
        return text
    return ruleset.decode(text)


_HAS_LETTER = re.compile(r"[A-Za-z]")


def decode_for_font(text: str, font_name: str) -> str:
    """Decode *text* when *font_name* is a legacy Sinhala face.

    The face is known by name, or learned from the document (learn_fonts), or
    guessed from the text itself.  Text that already contains Unicode Sinhala
    is never decoded twice.  In a legacy font, a span with no letters (such as
    ``01'`` or a dotted leader) is decoded too, because ``'`` is the full stop.
    """
    if not text or _contains_sinhala(text):
        return text
    encoding = encoding_for_font(font_name) or _LEARNED.get(_font_key(font_name))
    legacy_font = encoding is not None
    if encoding is None:
        if _is_latin_font(font_name):
            return text
        encoding, decoded = _guess(text)
        if encoding is None:
            return text
        return decoded
    try:
        decoded = decode(text, encoding)
    except Exception:  # noqa: BLE001 - never break page loading over a mapping
        return text
    if decoded != text and (_contains_sinhala(decoded)
                            or (legacy_font and not _HAS_LETTER.search(text))):
        return decoded
    return text


def _main(argv: list[str]) -> int:
    """``python legacy_sinhala.py file.pdf`` - show how each font is handled."""
    if len(argv) < 2:
        print("usage: python legacy_sinhala.py file.pdf [page-number]")
        return 2
    try:
        import fitz  # PyMuPDF
    except ImportError:
        print("PyMuPDF is required: pip install pymupdf")
        return 1
    doc = fitz.open(argv[1])
    page = doc[int(argv[2]) - 1 if len(argv) > 2 else 0]
    spans = [(sp.get("text", ""), sp.get("font", ""))
             for blk in page.get_text("dict")["blocks"] if blk.get("type") == 0
             for ln in blk["lines"] for sp in ln["spans"]]
    learn_fonts(spans)
    print("Mapping folders searched:")
    for folder in _search_dirs():
        print(f"  {'OK ' if folder.exists() else '-- '}{folder}")
    print()
    shown: dict[str, int] = {}
    for text, font in spans:
        if not text.strip() or shown.get(font, 0) >= 3:
            continue
        shown[font] = shown.get(font, 0) + 1
        enc = encoding_for_font(font) or _LEARNED.get(_font_key(font))
        print(f"font={font!r} encoding={enc}")
        print(f"   raw    : {text!r}")
        print(f"   decoded: {decode_for_font(text, font)!r}")
    report = missing_mapping_report()
    if report:
        print("\nPROBLEM:", report)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
