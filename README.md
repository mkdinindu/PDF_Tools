# PDF Tools

A desktop PDF toolkit with a command line interface. Everything runs locally —
no file ever leaves your machine.

It started as a small script for joining lecture notes into one document
(`part1.pdf part2.pdf → notes.pdf`) and grew into a full toolkit with a PyQt6
interface on top of the same engine.

## Install

```bash
python3 -m pip install -r requirements.txt
```

Requires Python 3.10+. [Ghostscript](https://www.ghostscript.com/) is optional
but recommended — with it installed, compression is noticeably better and the
app reports "Ghostscript ready" in the sidebar. Without it, compression falls
back to re-encoding embedded images.

The page editor uses [PyMuPDF](https://pymupdf.readthedocs.io/). It is installed
by `requirements.txt`; if you install packages by hand, add `PyMuPDF`.

```bash
python3 -m pip install PyMuPDF
```

## Desktop app

```bash
python3 PDF_Tools.py            # or: python3 PDF_Tools.py --gui
```

### Load your files once

There is a single source bar at the top of the window. Drop PDFs anywhere in the
app — the bar, the sidebar, a tool screen — or press **Add PDFs**. That is the
only upload step; the files stay loaded for the whole session.

Each chip is one document:

- **click** it to point the current tool at that file
- **▲ ▼** to change the order (this is the merge order)
- **✕** to remove it, and **Clear** to empty the bar

Merge uses every loaded file in bar order. The other six tools work on the chip
you have selected, and they re-target themselves the moment you select a
different one — no re-uploading, no re-picking.

### Tools

| Tool | What it does |
| --- | --- |
| **Merge PDFs** | Combine every loaded file, in the order shown in the bar |
| **Edit page** | Change or delete existing text, add new text, and move, resize, rotate or replace images — including shaped Sinhala |
| **Split** | Extract a page range, or cut into chunks of N pages |
| **Compress** | Three levels, each reported with the size actually saved |
| **Rotate** | All pages or a chosen range, 90/180/270° |
| **Organise** | Visual page grid — reorder by dragging, delete, rotate, duplicate, and add image files as new pages |
| **Protect** | Add an open password, optionally blocking print and copy |
| **Metadata** | Title, author, subject and keywords |

Output names are filled in for you (`notes.pdf` → `notes_compressed.pdf`), so
switching files updates the target too. `Ctrl+1` … `Ctrl+8` switch tools,
`Ctrl+Q` quits. Long jobs run on a background thread with a progress bar, so the
window stays responsive.

### Edit page: text and images

The editor is visual and non-destructive: every change is stored in memory and
written to a new PDF only when you press **Save edited PDF**.

- **Edit text** — click a line, or double-click it to type directly on the page.
  The inspector also lets you change the content, font, size, colour and
  alignment. Empty a line to remove it.
- **Add text** — choose the **Text** tool, then drag a box on the page. New text
  uses the bundled Sinhala font by default and can be switched to any installed
  face in the inspector.
- **Delete** — select the object and press `Delete`, or use the inspector button.
- **Images** — click an image to select it. Drag it to move, use the corner or
  edge handles to resize, and the round handle above it to rotate in 15° steps.
  The inspector can replace the image, flip the aspect-ratio lock, or delete it.
- **Undo / redo** — `Ctrl+Z` and `Ctrl+Y`, or the toolbar arrows. Each document
  keeps its own edits while it stays loaded, so switching chips does not lose
  work.
- **Zoom** — `Ctrl`+wheel, the toolbar buttons, `1:1`, or **Fit**.

Redaction is used to remove the original text or image cleanly, so backgrounds,
rules and other page graphics are preserved. New text is laid out through
MuPDF's HTML/Story engine with `NotoSansSinhala-Regular.ttf` (and the bold face),
which means complex scripts are shaped correctly — pre-base vowels are
reordered, conjuncts and ligatures form, and Latin falls back gracefully.

PDFs made with legacy Sinhala fonts (FM Abhaya, FM Malithi, DL-Manel, Kaputa,
Amalee, Thibus) store Sinhala glyphs as ASCII look-alike bytes, so the raw text
extracts as strings like `o<od ud,s.dj` instead of `දළදා මාලිගාව`. The editor
recognises those fonts and converts the text back to Unicode while reading the
page, so the inspector and the in-place text box show real Sinhala. Text that is
already Unicode is never converted twice, and Latin text in ordinary fonts is
left as it is. Only the conversion tables are bundled (see `assets/legacy/`),
not the legacy fonts themselves.

If PyMuPDF is missing, the rest of the app still works; the editor tab explains
how to install it.

### Organise: build the page order by hand

Organise shows every page of whichever file is selected in the top bar as a
thumbnail, and the order on screen is the order you get in the saved file.

- **Select** — click a card, `Ctrl+click` or `Shift+click` to add more, `Ctrl+A`
  for all, `Esc` to clear.
- **Delete** — select the pages you do not want, then the bin button.
- **Reorder** — press a card and drag it. A dashed marker shows where it will
  land; the grid scrolls if you drag towards the edge. The arrow buttons do the
  same thing a page at a time.
- **Rotate / duplicate** — act on everything currently selected.
- **Add images** — drop `.png`, `.jpg`, `.webp`, `.tif` and similar files
  anywhere on the grid and they become new pages, inserted where you dropped
  them. They are marked `IMG` and captioned with the file name, and you can
  reorder, rotate and delete them like any other page.
- Images are fitted onto an A4 page keeping their aspect ratio, never blown up
  beyond their real resolution, and transparent areas are flattened onto white.
  Photo EXIF orientation is respected. You can build a PDF from images alone,
  with no source file selected.

Dropping PDFs still adds them to the shared bar in the top left, so mixing files
in and dropping images on the grid can both happen in one session.

## Command line

```bash
python3 PDF_Tools.py -m part1.pdf part2.pdf -o notes.pdf
```

| Command | Example |
| --- | --- |
| Merge | `-m a.pdf b.pdf -o out.pdf` |
| Split a range | `-s in.pdf -r 1-3,7 -o out.pdf` |
| Split into chunks | `-s in.pdf -n 2 -o chunks/` |
| Compress | `-c in.pdf -l high -o out.pdf` |
| Rotate | `-t in.pdf -a 90 -o out.pdf` |
| Rotate a range | `-t in.pdf -a 180 -r 2-5 -o out.pdf` |
| Edit metadata | `-i in.pdf -T "Title" --author "You" -o out.pdf` |
| Encrypt | `-e in.pdf -w secret -o out.pdf` (add `--no-print` to also block copying) |
| Read text | `-x in.pdf` |

Levels are `low`, `medium` (default) and `high`. Angles are `90`, `180`, `270`.

Page ranges accept `1-3`, `7`, `1-3,7,12-` and open-ended `12-` forms.

```bash
python3 PDF_Tools.py --help
```

## Notes

- Your originals are never modified. Every tool writes a new file, and the app
  refuses to overwrite a file you loaded.
- The engine is [pypdf](https://pypdf.readthedocs.io/); the interface is PyQt6.
- The command line is unchanged, so any scripts you already have keep working.
