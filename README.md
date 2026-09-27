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
| **Split** | Extract a page range, or cut into chunks of N pages |
| **Compress** | Three levels, each reported with the size actually saved |
| **Rotate** | All pages or a chosen range, 90/180/270° |
| **Organise** | Visual page grid — delete, rotate, duplicate, reorder |
| **Protect** | Add an open password, optionally blocking print and copy |
| **Metadata** | Title, author, subject and keywords |

Output names are filled in for you (`notes.pdf` → `notes_compressed.pdf`), so
switching files updates the target too. `Ctrl+1` … `Ctrl+7` switch tools,
`Ctrl+Q` quits. Long jobs run on a background thread with a progress bar, so the
window stays responsive.

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
