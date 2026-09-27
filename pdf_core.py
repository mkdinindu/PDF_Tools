#!/usr/bin/env python3
"""PDF engine shared by the CLI and the GUI.

Deliberately free of any Qt import so it can be unit tested and reused.
Every operation reports progress through a ``progress(percent, message)``
callback and raises :class:`PdfToolError` for anything a user can fix.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Callable, Iterable, Sequence

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError

ProgressFn = Callable[[int, str], None]

#: Ghostscript / re-encode presets. ``dpi`` is what images are resampled to.
COMPRESSION_PRESETS: dict[str, dict] = {
    "low": {"dpi": 200, "quality": 80, "scale": 0.8, "label": "Low",
            "hint": "Best quality, smallest reduction"},
    "medium": {"dpi": 120, "quality": 60, "scale": 0.55, "label": "Medium",
               "hint": "Balanced - recommended"},
    "high": {"dpi": 72, "quality": 40, "scale": 0.32, "label": "High",
             "hint": "Smallest file, lower image detail"},
}

#: Image types the GUI accepts as drop-ins that become PDF pages.
IMAGE_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".jpe", ".jfif", ".bmp", ".gif", ".tif", ".tiff",
    ".webp", ".ppm", ".pgm", ".pnm", ".pbm",
})

#: Page size in points, used to fit dropped images onto a sheet.
A4_POINTS = (595.0, 842.0)

META_FIELDS = ("/Title", "/Author", "/Subject", "/Keywords", "/Creator")

METRIC_KEYS = META_FIELDS + ("/Producer", "/CreatorDate")


class PdfToolError(Exception):
    """An error the user can act on, already phrased for a human."""


@dataclass
class FileInfo:
    path: Path
    pages: int = 0
    size: int = 0
    title: str = ""
    encrypted: bool = False
    error: str = ""

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def size_text(self) -> str:
        return human_size(self.size)

    @property
    def ok(self) -> bool:
        return not self.error and self.pages > 0


@dataclass
class JobResult:
    output: Path
    pages: int
    input_size: int
    output_size: int
    detail: str = ""
    files: list[Path] = field(default_factory=list)

    @property
    def size_text(self) -> str:
        return f"{human_size(self.input_size)} -> {human_size(self.output_size)}"

    @property
    def saved(self) -> int:
        if not self.input_size or self.output_size >= self.input_size:
            return 0
        return round((1 - self.output_size / self.input_size) * 100)


def human_size(num: float) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def noop_progress(_percent: int, _message: str) -> None:
    return None


# --------------------------
# Reading
# --------------------------
def inspect_file(path: str | os.PathLike, progress: ProgressFn = noop_progress) -> FileInfo:
    p = Path(path).expanduser()
    try:
        p = p.resolve()
    except OSError:
        pass
    if not p.exists():
        return FileInfo(p, error="File not found")
    if p.suffix.lower() != ".pdf":
        return FileInfo(p, 0, p.stat().st_size, error="Not a .pdf file")
    if p.is_dir():
        return FileInfo(p, 0, 0, error="This is a folder")

    size = p.stat().st_size
    progress(30, f"Reading {p.name}")
    try:
        reader = PdfReader(str(p))
        encrypted = bool(reader.is_encrypted)
        if encrypted and reader.decrypt("") == 0:
            return FileInfo(p, 0, size, encrypted=True,
                            error="Password protected")
        pages = len(reader.pages)
        title = ""
        try:
            title = str((reader.metadata or {}).get("/Title") or "").strip()
        except Exception:  # noqa: BLE001
            title = ""
    except PdfReadError:
        return FileInfo(p, 0, size, error="Corrupt or not a valid PDF")
    except Exception as exc:  # noqa: BLE001
        return FileInfo(p, 0, size, error=type(exc).__name__)

    progress(100, f"{pages} pages")
    return FileInfo(p, pages, size, title=title, encrypted=encrypted)


def inspect_files(paths: Iterable[str | os.PathLike],
                  progress: ProgressFn = noop_progress) -> list[FileInfo]:
    items = list(paths)
    out: list[FileInfo] = []
    for i, path in enumerate(items):
        done = int(i / max(len(items), 1) * 100)
        span = 100 / max(len(items), 1)
        out.append(inspect_file(path, lambda pct, msg, _d=done, _s=span: progress(_d + int(pct * _s / 100), msg)))
    return out


def read_metadata(path: str | os.PathLike) -> dict[str, str]:
    reader = _read_any(path)
    info = reader.metadata or {}
    out = {}
    for key in META_FIELDS:
        try:
            out[key] = str(info.get(key) or "")
        except Exception:  # noqa: BLE001
            out[key] = ""
    return out


# --------------------------
# Internals
# --------------------------
def _read_any(path: str | os.PathLike) -> PdfReader:
    p = Path(path)
    try:
        reader = PdfReader(str(p))
    except PdfReadError as exc:
        raise PdfToolError(f"{p.name} is not a readable PDF") from exc
    if reader.is_encrypted:
        try:
            if reader.decrypt("") == 0:
                raise PdfToolError(f"{p.name} is password protected")
        except PdfToolError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PdfToolError(f"{p.name} is password protected") from exc
    return reader


def _resolve_output(output: str | os.PathLike, fallback_stem: str) -> Path:
    raw = str(output).strip() if output is not None else ""
    out = Path(raw).expanduser() if raw else Path(fallback_stem).with_suffix(".pdf")
    if not out.name or out.name in (".", ".."):
        raise PdfToolError("Enter an output file name")
    if not out.suffix:
        out = out.with_suffix(".pdf")
    parent = out.parent if str(out.parent) else Path(".")
    if not parent.exists():
        raise PdfToolError(f"Folder does not exist: {parent}")
    if not os.access(parent, os.W_OK):
        raise PdfToolError(f"No permission to write in {parent}")
    return out.absolute()


def _derived_output(requested: str | os.PathLike, src: Path | None, tag: str) -> Path:
    """Output path for derived output, refusing to silently overwrite the input."""
    raw = str(requested).strip() if requested is not None else ""
    if not raw:
        if src is None:
            return Path.cwd() / f"{tag}.pdf"
        return src.with_name(f"{src.stem}_{tag}.pdf")
    out = Path(raw).expanduser()
    if not out.suffix:
        out = out.with_suffix(".pdf")
    if src is not None and out.absolute() == src.absolute():
        return src.with_name(f"{src.stem}_{tag}.pdf")
    return _resolve_output(out, src.stem if src is not None else out.stem)


def _copy_metadata(reader: PdfReader, writer: PdfWriter) -> None:
    try:
        info = reader.metadata
    except Exception:  # noqa: BLE001
        return
    if not info:
        return
    clean = {}
    for key, value in info.items():
        if key in META_FIELDS and isinstance(value, str) and value.strip():
            clean[key] = value
    if clean:
        try:
            writer.add_metadata(clean)
        except Exception:  # noqa: BLE001
            pass


def split_images(paths: Iterable[str | os.PathLike]) -> tuple[list[Path], list[Path]]:
    """Split dropped paths into (images, everything else)."""
    images: list[Path] = []
    others: list[Path] = []
    for raw in paths:
        path = Path(str(raw))
        (images if path.suffix.lower() in IMAGE_SUFFIXES else others).append(path)
    return images, others


def image_page(path: str | os.PathLike):
    """Build a one-page PDF page from an image, fitted to A4 keeping aspect."""
    from PIL import Image, ImageOps

    p = Path(str(path))
    try:
        with Image.open(p) as handle:
            handle.seek(0)
            # honour the EXIF orientation flag so phone photos are not sideways
            image = ImageOps.exif_transpose(handle)
            if image.mode in ("RGBA", "LA", "P"):
                flat = Image.new("RGB", image.size, (255, 255, 255))
                rgba = image.convert("RGBA")
                flat.paste(rgba, mask=rgba.split()[-1])
                image = flat
            elif image.mode != "RGB":
                image = image.convert("RGB")
            width, height = image.size
            sheet_w, sheet_h = A4_POINTS
            # never magnify past 1 pixel per point (72 dpi) to keep it sharp
            scale = min(sheet_w / max(1, width), sheet_h / max(1, height), 1.0)
            # Pillow sets page points = pixels * 72 / resolution
            buffer = BytesIO()
            image.save(buffer, "PDF", resolution=72.0 / scale)
    except PdfToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PdfToolError(f"{p.name} could not be read as an image") from exc
    buffer.seek(0)
    try:
        return PdfReader(buffer).pages[0]
    except Exception as exc:  # noqa: BLE001
        raise PdfToolError(f"{p.name} could not be converted to a page") from exc


def _write(writer: PdfWriter, output: Path) -> None:
    tmp = output.with_name(f".{output.name}.part")
    try:
        with open(tmp, "wb") as fh:
            writer.write(fh)
        os.replace(tmp, output)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise PdfToolError(f"Could not write {output.name}: {exc.strerror or exc}") from exc


def _copied_writer(src: Path, progress: ProgressFn, label: str = "Copying") -> tuple[PdfWriter, int]:
    reader = _read_any(src)
    writer = PdfWriter()
    _copy_metadata(reader, writer)
    total = len(reader.pages)
    if total == 0:
        raise PdfToolError(f"{src.name} has no pages")
    for i, page in enumerate(reader.pages):
        writer.add_page(page)
        progress(5 + int((i + 1) / total * 85), f"{label} page {i + 1} of {total}")
    return writer, total


# --------------------------
# Page range parsing
# --------------------------
def parse_page_range(spec: str, total: int) -> list[int]:
    """'1-3, 7, 10-' -> sorted, de-duplicated 0-based page indexes."""
    spec = (spec or "").strip()
    if not spec:
        raise PdfToolError("Enter a page or range, e.g. 1-3, 7")
    if total < 1:
        raise PdfToolError("That PDF has no pages")

    pages: list[int] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if not re.fullmatch(r"\d+(\s*-\s*\d*)?|\s*-\s*\d+", chunk):
            raise PdfToolError(f"Cannot read '{chunk}' - use numbers and ranges")
        if "-" in chunk:
            lo, _, hi = chunk.partition("-")
            start = int(lo.strip()) if lo.strip() else 1
            end = int(hi.strip()) if hi.strip() else total
            if start < 1 or end < 1:
                raise PdfToolError("Page numbers start at 1")
            if start > end:
                raise PdfToolError(f"Range is backwards: '{chunk}'")
            if end > total:
                raise PdfToolError(f"'{chunk}' goes past the last page ({total})")
            pages.extend(range(start - 1, end))
        else:
            n = int(chunk)
            if n < 1:
                raise PdfToolError("Page numbers start at 1")
            if n > total:
                raise PdfToolError(f"Page {n} is past the last page ({total})")
            pages.append(n - 1)

    if not pages:
        raise PdfToolError("Enter a page or range, e.g. 1-3, 7")
    return sorted(dict.fromkeys(pages))


def page_label(indexes: Sequence[int]) -> str:
    """0-based indexes -> compact label like '1-3, 7'."""
    if not indexes:
        return ""
    parts: list[str] = []
    start = prev = indexes[0]
    for item in list(indexes[1:]) + [None]:
        if item is not None and item == prev + 1:
            prev = item
            continue
        parts.append(str(start + 1) if start == prev else f"{start + 1}-{prev + 1}")
        if item is not None:
            start = prev = item
    return ", ".join(parts)


# --------------------------
# Operations
# --------------------------
def merge_pdfs(pdf_list: Sequence[str | os.PathLike], output_filename: str | os.PathLike,
               progress: ProgressFn = noop_progress) -> JobResult:
    paths = [Path(str(p)).expanduser() for p in pdf_list]
    paths = [p for p in paths if str(p).strip()]
    if len(paths) < 2:
        raise PdfToolError("Add at least two PDFs to merge")
    for p in paths:
        if not p.exists():
            raise PdfToolError(f"Missing file: {p.name}")
    out = _resolve_output(output_filename, "merged")

    writer = PdfWriter()
    total_bytes = 0
    for i, path in enumerate(paths):
        progress(int(i / len(paths) * 92), f"Adding {path.name} ({i + 1}/{len(paths)})")
        reader = _read_any(path)
        if i == 0:
            _copy_metadata(reader, writer)
        total_bytes += path.stat().st_size
        for page in reader.pages:
            writer.add_page(page)
    progress(95, "Writing output")
    _write(writer, out)
    progress(100, "Done")
    return JobResult(out, len(writer.pages), total_bytes, out.stat().st_size,
                     detail=f"{len(paths)} files combined")


def split_pdf(input_path: str | os.PathLike, output_path: str | os.PathLike,
              mode: str = "range", spec: str = "", every: int = 1,
              progress: ProgressFn = noop_progress) -> JobResult:
    src = Path(str(input_path)).expanduser()
    if not src.exists():
        raise PdfToolError("Select a PDF first")
    reader = _read_any(src)
    total = len(reader.pages)
    in_size = src.stat().st_size
    if total == 0:
        raise PdfToolError("That PDF has no pages")

    if mode == "range":
        out = _derived_output(output_path, src, "split")
        indexes = parse_page_range(spec, total)
        writer = PdfWriter()
        _copy_metadata(reader, writer)
        for i in indexes:
            writer.add_page(reader.pages[i])
        progress(95, "Writing output")
        _write(writer, out)
        progress(100, "Done")
        return JobResult(out, len(indexes), in_size, out.stat().st_size,
                         detail=f"Pages {page_label(indexes)} extracted")

    if mode == "each":
        if every < 1:
            raise PdfToolError("Chunks must be 1 page or more")
        raw = str(output_path).strip() if output_path else ""
        folder = Path(raw).expanduser() if raw else src.parent / f"{src.stem}_split"
        if raw and folder.suffix.lower() == ".pdf":
            folder = folder.with_suffix("")
        if folder.exists() and not folder.is_dir():
            raise PdfToolError(f"{folder.name} is a file, not a folder")
        folder.mkdir(parents=True, exist_ok=True)

        chunks = [list(range(i, min(i + every, total))) for i in range(0, total, every)]
        written: list[Path] = []
        for i, group in enumerate(chunks):
            writer = PdfWriter()
            if i == 0:
                _copy_metadata(reader, writer)
            for idx in group:
                writer.add_page(reader.pages[idx])
            target = folder / f"{src.stem}_part{i + 1:02d}.pdf"
            _write(writer, target)
            written.append(target)
            progress(5 + int((i + 1) / len(chunks) * 92), f"Wrote {target.name} ({i + 1}/{len(chunks)})")
        size = sum(p.stat().st_size for p in written)
        return JobResult(written[0], total, in_size, size, files=written,
                         detail=f"{len(written)} files in {folder}")

    raise PdfToolError(f"Unknown split mode: {mode}")


def ghostscript_path() -> str | None:
    return shutil.which("gs") or shutil.which("ghostscript")


def compress_pdf(input_path: str | os.PathLike, output_path: str | os.PathLike,
                 level: str = "medium", progress: ProgressFn = noop_progress) -> JobResult:
    src = Path(str(input_path)).expanduser()
    if not src.exists():
        raise PdfToolError("Select a PDF first")
    preset = COMPRESSION_PRESETS.get(level)
    if preset is None:
        raise PdfToolError(f"Unknown compression level: {level}")
    out = _derived_output(output_path, src, "compressed")
    in_size = src.stat().st_size
    pages = len(_read_any(src).pages)
    progress(5, f"{pages} pages - {preset['hint']}")

    gs = ghostscript_path()
    if gs:
        try:
            result = _ghostscript_compress(gs, src, out, preset, pages, in_size, progress)
            if result.output_size < in_size:
                progress(100, "Done")
                return result
        except PdfToolError:
            raise
        except Exception:  # noqa: BLE001 - fall through to the pure-python path
            pass
        progress(70, "Ghostscript gained nothing - re-encoding images instead")

    result = _reencode_compress(src, out, preset, pages, in_size, progress)
    progress(100, "Done")
    return result


def _ghostscript_compress(gs: str, src: Path, out: Path, preset: dict, pages: int,
                          in_size: int, progress: ProgressFn) -> JobResult:
    progress(30, "Optimising with Ghostscript")
    args = [
        gs, "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER",
        "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.7",
        "-dPDFSETTINGS=/prepress",
        "-dDownsampleColorImages=true", f"-dColorImageResolution={preset['dpi']}",
        "-dDownsampleGrayImages=true", f"-dGrayImageResolution={preset['dpi']}",
        "-dDownsampleMonoImages=true", f"-dMonoImageResolution={preset['dpi']}",
        "-dColorImageDownsampleType=/Average",
        "-dGrayImageDownsampleType=/Average",
        "-dMonoImageDownsampleType=/Subsample",
        "-dAutoFilterColorImages=false", "-dAutoFilterGrayImages=false",
        "-dAutoFilterMonoImages=false",
        "-dColorImageFilter=/DCTEncode", "-dGrayImageFilter=/DCTEncode",
        "-dDetectDuplicateImages=true",
        "-dCompressFonts=true", "-dSubsetFonts=true", "-dEmbedAllFonts=true",
        f"-sOutputFile={out}", str(src),
    ]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired as exc:
        raise PdfToolError("Ghostscript timed out - try a lower compression level") from exc
    except OSError as exc:
        raise PdfToolError(f"Could not run Ghostscript: {exc}") from exc

    if proc.returncode != 0 or not out.exists():
        lines = [ln for ln in (proc.stderr or "").strip().splitlines() if ln.strip()]
        raise PdfToolError(f"Ghostscript failed: {lines[-1] if lines else 'unknown error'}")
    progress(95, "Finishing")
    return JobResult(out, pages, in_size, out.stat().st_size,
                     detail=f"Downsampled to {preset['dpi']} DPI")


def _reencode_compress(src: Path, out: Path, preset: dict, pages: int, in_size: int,
                       progress: ProgressFn) -> JobResult:
    """Ghostscript-free path: downsample and re-encode every embedded image."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise PdfToolError("Install Pillow, or Ghostscript, to compress PDFs") from exc
    from pypdf.generic import DecodedStreamObject, NameObject, NumberObject

    scale = float(preset["scale"])
    quality = int(preset["quality"])
    reader = _read_any(src)
    total = len(reader.pages)
    writer = PdfWriter()
    _copy_metadata(reader, writer)
    rebuilt = 0

    for index in range(total):
        page = reader.pages[index]
        try:
            by_id = {img.indirect_reference.idnum: img for img in page.images if img.indirect_reference}
        except Exception:  # noqa: BLE001
            by_id = {}
        if by_id:
            try:
                xobjects = page[NameObject("/Resources")][NameObject("/XObject")]
            except Exception:  # noqa: BLE001
                xobjects = None
            if xobjects is not None:
                for name, ref in list(xobjects.items()):
                    image = by_id.get(getattr(ref, "idnum", None))
                    if image is None:
                        continue
                    stream = _jpeg_stream(image, Image, scale, quality,
                                          DecodedStreamObject, NameObject, NumberObject)
                    if stream is None:
                        continue
                    try:
                        xobjects[NameObject(str(name))] = stream
                        rebuilt += 1
                    except Exception:  # noqa: BLE001
                        continue
        writer.add_page(page)
        progress(5 + int((index + 1) / total * 88), f"Page {index + 1} of {total}")

    if rebuilt == 0:
        writer, _ = _copied_writer(src, progress, "Copying")
    _write(writer, out)
    detail = (f"{rebuilt} image(s) re-encoded" if rebuilt
              else "No embedded images - structure only")
    return JobResult(out, pages, in_size, out.stat().st_size, detail=detail)


def _jpeg_stream(image, Image, scale: float, quality: int,
                 DecodedStreamObject, NameObject, NumberObject):
    try:
        pil = image.image
        width, height = pil.size
        if min(width, height) < 64 or pil.mode not in ("RGB", "L"):
            return None
        if max(width, height) < 400:
            return None
        smaller = pil.resize((max(1, round(width * scale)), max(1, round(height * scale))),
                             Image.LANCZOS).convert("RGB")
        buffer = BytesIO()
        smaller.save(buffer, format="JPEG", quality=quality, optimize=True)
        stream = DecodedStreamObject()
        stream.set_data(buffer.getvalue())
        stream[NameObject("/Type")] = NameObject("/XObject")
        stream[NameObject("/Subtype")] = NameObject("/Image")
        stream[NameObject("/Width")] = NumberObject(smaller.width)
        stream[NameObject("/Height")] = NumberObject(smaller.height)
        stream[NameObject("/ColorSpace")] = NameObject("/DeviceRGB")
        stream[NameObject("/BitsPerComponent")] = NumberObject(8)
        stream[NameObject("/Filter")] = NameObject("/DCTDecode")
        return stream
    except Exception:  # noqa: BLE001 - keep the original image
        return None


def rotate_pdf(input_path: str | os.PathLike, output_path: str | os.PathLike,
               angle: int = 90, pages: str = "all",
               progress: ProgressFn = noop_progress) -> JobResult:
    src = Path(str(input_path)).expanduser()
    if not src.exists():
        raise PdfToolError("Select a PDF first")
    if int(angle) % 90:
        raise PdfToolError("Rotation must be 90, 180 or 270 degrees")
    angle = int(angle)
    out = _derived_output(output_path, src, "rotated")
    reader = _read_any(src)
    total = len(reader.pages)
    if total == 0:
        raise PdfToolError("That PDF has no pages")
    targets = set(range(total)) if pages == "all" else set(parse_page_range(pages, total))

    writer = PdfWriter()
    _copy_metadata(reader, writer)
    for i, page in enumerate(reader.pages):
        if i in targets:
            page.rotate(angle)
        writer.add_page(page)
        progress(5 + int((i + 1) / total * 90), f"Page {i + 1} of {total}")
    _write(writer, out)
    scope = "every page" if pages == "all" else f"pages {page_label(sorted(targets))}"
    return JobResult(out, total, src.stat().st_size, out.stat().st_size,
                     detail=f"{len(targets)} page(s) turned {angle}deg ({scope})")


def rebuild_pdf(input_path: str | os.PathLike | None, output_path: str | os.PathLike,
                order: Sequence[int | str | os.PathLike], progress: ProgressFn = noop_progress,
                rotations: Sequence[int] | None = None) -> JobResult:
    """Write a new PDF from a custom page order.

    ``order`` drives the output: each entry is either a 0-based page index into
    the source PDF, or a path to an image file which becomes a new page.
    Repeated indexes duplicate a page, and ``rotations`` gives a clockwise turn
    in degrees per entry. ``input_path`` may be ``None`` when every page comes
    from an image.
    """
    src = Path(str(input_path)).expanduser() if input_path else None
    if src is not None and not src.exists():
        raise PdfToolError("Select a PDF first")
    if src is not None and src.absolute() == _as_path(output_path).absolute():
        raise PdfToolError("Pick a different name - that would overwrite the original")
    out = _derived_output(output_path, src, "edited")
    reader = _read_any(src) if src is not None else None
    total = len(reader.pages) if reader is not None else 0

    turns = list(rotations) if rotations is not None else []
    turns += [0] * (len(order) - len(turns))
    # normalise to (kind, value, turn) so a bad entry is dropped, not fatal
    sequence: list[tuple[str, object, int]] = []
    for n, entry in enumerate(order):
        turn = int(turns[n]) % 360
        if isinstance(entry, (str, os.PathLike)) and not isinstance(entry, int):
            image = Path(str(entry))
            if image.exists() and image.suffix.lower() in IMAGE_SUFFIXES:
                sequence.append(("image", image, turn))
        else:
            idx = int(entry)
            if reader is not None and 0 <= idx < total:
                sequence.append(("page", idx, turn))
    if not sequence:
        raise PdfToolError("Keep at least one page")

    writer = PdfWriter()
    if reader is not None:
        _copy_metadata(reader, writer)
    for n, (kind, value, turn) in enumerate(sequence):
        if kind == "page":
            writer.add_page(reader.pages[int(value)])
            where = f"page {int(value) + 1}"
        else:
            writer.add_page(image_page(value))
            where = f"image {Path(str(value)).name}"
        if turn:
            writer.pages[-1].rotate(turn)
        progress(5 + int((n + 1) / len(sequence) * 90), f"Writing {where}")
    _write(writer, out)

    indexes = [int(v) for k, v, _ in sequence if k == "page"]
    images = sum(1 for k, _, _ in sequence if k == "image")
    parts = [f"{len(sequence)} pages written"]
    if images:
        parts.append(f"{images} from image{'s' if images > 1 else ''}")
    if indexes and len(indexes) > len(set(indexes)):
        parts.append(f"{len(indexes) - len(set(indexes))} duplicated")
    if reader is not None:
        kept = len(set(indexes))
        if total - kept:
            parts.append(f"{total - kept} removed")
        if indexes != sorted(set(indexes)):
            parts.append("reordered")
    before = src.stat().st_size if src is not None else 0
    return JobResult(out, len(sequence), before, out.stat().st_size,
                     detail=", ".join(parts))


def _as_path(value: str | os.PathLike) -> Path:
    return Path(str(value)).expanduser()


def edit_metadata(input_path: str | os.PathLike, output_path: str | os.PathLike,
                  changes: dict[str, str], progress: ProgressFn = noop_progress) -> JobResult:
    src = Path(str(input_path)).expanduser()
    if not src.exists():
        raise PdfToolError("Select a PDF first")
    out = _derived_output(output_path, src, "meta")
    writer, total = _copied_writer(src, progress)
    payload = {k: v for k, v in changes.items() if k in META_FIELDS}
    if payload:
        writer.add_metadata(payload)
    _write(writer, out)
    touched = sum(1 for v in payload.values() if str(v).strip())
    return JobResult(out, total, src.stat().st_size, out.stat().st_size,
                     detail=f"{touched} field(s) updated" if touched else "No changes")


def encrypt_pdf(input_path: str | os.PathLike, output_path: str | os.PathLike,
                user_password: str, owner_password: str = "", allow_print: bool = True,
                progress: ProgressFn = noop_progress) -> JobResult:
    src = Path(str(input_path)).expanduser()
    if not src.exists():
        raise PdfToolError("Select a PDF first")
    if not str(user_password):
        raise PdfToolError("Set a password for the document")
    out = _derived_output(output_path, src, "protected")
    writer, total = _copied_writer(src, progress)
    try:
        from pypdf.constants import UserAccessPermissions

        flag = (int(UserAccessPermissions.PRINT) | int(UserAccessPermissions.EXTRACT))
        if allow_print:
            flag |= int(UserAccessPermissions.PRINT)
        else:
            flag &= ~int(UserAccessPermissions.PRINT)
        writer.encrypt(str(user_password), str(owner_password) or str(user_password),
                       permissions_flag=flag)
    except Exception as exc:  # noqa: BLE001
        raise PdfToolError(f"Could not encrypt: {exc}") from exc
    _write(writer, out)
    return JobResult(out, total, src.stat().st_size, out.stat().st_size,
                     detail="Password protection enabled")


def extract_text(input_path: str | os.PathLike,
                 progress: ProgressFn = noop_progress) -> tuple[str, int]:
    reader = _read_any(input_path)
    total = max(len(reader.pages), 1)
    chunks = []
    for i, page in enumerate(reader.pages):
        progress(int((i + 1) / total * 100), f"Extracting page {i + 1}")
        try:
            chunks.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001
            chunks.append("")
    return "\n\n".join(chunks), len(chunks)
