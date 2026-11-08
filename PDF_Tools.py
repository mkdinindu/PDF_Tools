#!/usr/bin/env python3
"""PDF Tools - command line interface and graphical launcher.

  python3 PDF_Tools.py --gui                       open the desktop app
  python3 PDF_Tools.py -m a.pdf b.pdf -o all.pdf   merge
  python3 PDF_Tools.py -s in.pdf -o out.pdf -r 1-3  split a page range
  python3 PDF_Tools.py -c in.pdf -o small.pdf      compress
  python3 PDF_Tools.py -t in.pdf -o fixed.pdf -a 90 rotate
  python3 PDF_Tools.py -i in.pdf -o meta.pdf -k "notes, exam"  edit metadata
  python3 PDF_Tools.py -e in.pdf -o lock.pdf -w secret           encrypt
"""

from __future__ import annotations

import sys
from argparse import ArgumentParser, RawDescriptionHelpFormatter

import pdf_core as core


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(
        prog="PDF_Tools.py",
        description="Merge, split, compress, rotate, protect and tag PDFs.",
        epilog="Run without arguments, or with --gui, to open the desktop app.",
        formatter_class=RawDescriptionHelpFormatter,
    )
    parser.add_argument("-g", "--gui", action="store_true", help="open the desktop app")
    parser.add_argument("-m", "--merge", nargs="+", metavar="PDF", help="merge PDFs in order")
    parser.add_argument("-s", "--split", metavar="PDF", help="split a PDF")
    parser.add_argument("-c", "--compress", metavar="PDF", help="compress a PDF")
    parser.add_argument("-t", "--rotate", metavar="PDF", help="rotate a PDF")
    parser.add_argument("-i", "--info", metavar="PDF", help="edit metadata")
    parser.add_argument("-e", "--encrypt", metavar="PDF", help="password protect a PDF")
    parser.add_argument("-x", "--extract-text", metavar="PDF", help="print the text content")
    parser.add_argument("-o", "--output", metavar="FILE", help="output file or folder")
    parser.add_argument("-r", "--range", metavar="PAGES", help="page range, e.g. 1-3,7")
    parser.add_argument("-n", "--every", type=int, default=1, metavar="N",
                        help="with --split, pages per output file")
    parser.add_argument("-a", "--angle", type=int, default=90, choices=(90, 180, 270),
                        help="rotation in degrees (default 90)")
    parser.add_argument("-l", "--level", default="medium", choices=tuple(core.COMPRESSION_PRESETS),
                        help="compression level (default medium)")
    parser.add_argument("-T", "--title", metavar="TEXT", help="metadata title")
    parser.add_argument("--author", metavar="TEXT", help="metadata author")
    parser.add_argument("--subject", metavar="TEXT", help="metadata subject")
    parser.add_argument("-k", "--keywords", metavar="TEXT", help="metadata keywords")
    parser.add_argument("-w", "--password", metavar="TEXT", help="password for --encrypt")
    parser.add_argument("--no-print", action="store_true",
                        help="with --encrypt, block printing and copying")
    return parser


def show_progress(percent: int, message: str) -> None:
    width = 24
    filled = int(percent / 100 * width)
    bar = "#" * filled + "-" * (width - filled)
    sys.stdout.write(f"\r  [{bar}] {percent:3d}%  {message[:44]:<44}")
    sys.stdout.flush()
    if percent >= 100:
        sys.stdout.write("\n")


def report(result: core.JobResult) -> None:
    print()
    if result.saved:
        print(f"  Saved {result.saved}%  ({result.size_text})")
    else:
        print(f"  Size  {result.size_text}")
    if result.detail:
        print(f"  {result.detail}")
    if len(result.files) > 1:
        print(f"  {len(result.files)} files written to {result.output.parent}")
    else:
        print(f"  Wrote {result.output}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    modes = [args.merge, args.split, args.compress, args.rotate, args.info,
             args.encrypt, args.extract_text]
    chosen = [m for m in modes if m]

    if args.gui or not chosen:
        try:
            from gui import main as gui_main
        except ImportError as exc:
            print(f"Cannot start the desktop app: {exc}", file=sys.stderr)
            print("Install the requirements first:  pip install -r requirements.txt",
                  file=sys.stderr)
            return 1
        return gui_main()

    if len(chosen) > 1:
        print("Pick one operation at a time (see --help).", file=sys.stderr)
        return 2

    try:
        if args.merge:
            if not args.output:
                print("Missing --output for merging", file=sys.stderr)
                return 2
            result = core.merge_pdfs(args.merge, args.output, show_progress)
        elif args.split:
            if not args.output:
                print("Missing --output for splitting", file=sys.stderr)
                return 2
            mode = "each" if args.range is None and args.every > 1 else "range"
            result = core.split_pdf(args.split, args.output, mode=mode,
                                    spec=args.range or "", every=args.every, progress=show_progress)
        elif args.compress:
            result = core.compress_pdf(args.compress, args.output or "", args.level, show_progress)
        elif args.rotate:
            result = core.rotate_pdf(args.rotate, args.output or "", args.angle,
                                     args.range or "all", show_progress)
        elif args.info:
            changes = {}
            if args.title is not None:
                changes["/Title"] = args.title
            if args.author is not None:
                changes["/Author"] = args.author
            if args.subject is not None:
                changes["/Subject"] = args.subject
            if args.keywords is not None:
                changes["/Keywords"] = args.keywords
            if not changes:
                info = core.inspect_file(args.info)
                if info.error:
                    print(f"  {info.name}: {info.error}", file=sys.stderr)
                    return 1
                print(f"\n  {info.name}  ·  {info.pages} pages  ·  {info.size_text}\n")
                for key, value in core.read_metadata(args.info).items():
                    print(f"  {key.lstrip('/'):<10} {value or '-'}")
                return 0
            result = core.edit_metadata(args.info, args.output or "", changes, show_progress)
        elif args.encrypt:
            result = core.encrypt_pdf(args.encrypt, args.output or "", args.password or "",
                                      allow_print=not args.no_print, progress=show_progress)
        else:
            text, pages = core.extract_text(args.extract_text, show_progress)
            print()
            print(text if text.strip() else "  (no embedded text found)")
            return 0
    except core.PdfToolError as exc:
        show_progress(100, "done")
        print(f"\n  {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n  Cancelled", file=sys.stderr)
        return 130

    report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
