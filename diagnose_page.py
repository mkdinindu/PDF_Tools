#!/usr/bin/env python3
"""Usage: python diagnose_page.py file.pdf [page_number]   (run from the app folder)

Prints, for every text span on the page: font name, raw text, decoded text.
"""
import sys
import fitz
import legacy_sinhala as L

path = sys.argv[1]
page_no = int(sys.argv[2]) - 1 if len(sys.argv) > 2 else 0
page = fitz.open(path)[page_no]
for block in page.get_text("dict")["blocks"]:
    for line in block.get("lines", []):
        for s in line["spans"]:
            raw, font = s["text"], s["font"]
            if not raw.strip():
                continue
            out = L.decode_for_font(raw, font)
            status = "OK  " if out != raw else "SAME"
            print(f"{status} font={font!r} enc={L.encoding_for_font(font)}")
            print(f"     raw    : {raw!r}")
            print(f"     decoded: {out!r}")
