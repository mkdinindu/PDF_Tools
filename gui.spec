# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

# Override the output name without editing this file:
#   APP_NAME="PDF Tools" pyinstaller --noconfirm --clean gui.spec
APP_NAME = os.environ.get("APP_NAME", "gui").strip() or "gui"

# Bundled data files are unpacked next to the code at runtime (sys._MEIPASS in
# one-file builds), so the editor and legacy_sinhala can resolve them.
ROOT = Path(SPECPATH)
datas = [
    (str(ROOT / 'assets' / 'fonts'), 'assets/fonts'),
    (str(ROOT / 'assets' / 'legacy'), 'assets/legacy'),
]

# Optional: drop a platform-specific Ghostscript tree here to ship it with the
# app.  See README.md - without it the app still works, using PyMuPDF/Pillow.
_ghostscript = ROOT / 'assets' / 'ghostscript'
if _ghostscript.is_dir():
    datas.append((str(_ghostscript), 'assets/ghostscript'))

a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
