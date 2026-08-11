# PyInstaller spec — one build description for all three platforms.
#
# What is bundled: the code, the schema, the UI, the seed list. What is not:
# Chromium (~150MB, fetched on first sign-in by collect.ensure_browser) and the
# embedding model (~30MB, fetched on first dedup). Both need to land somewhere
# writable anyway, which a signed .app bundle is not.
#
#   pyinstaller packaging/aisignal.spec --noconfirm

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent

datas = [
    (str(ROOT / "schema.sql"), "."),
    (str(ROOT / "seeds.txt"), "."),
    (str(ROOT / "ui"), "ui"),
]
# Playwright ships a node driver as package data; without it the bundle imports
# but cannot launch anything.
datas += collect_data_files("playwright")

hiddenimports = (
    collect_submodules("tracker")
    + collect_submodules("anthropic")
    + ["model2vec", "numpy", "sqlite3", "webview"]
)
if sys.platform == "win32":
    hiddenimports += ["clr", "webview.platforms.winforms"]
elif sys.platform == "darwin":
    hiddenimports += ["webview.platforms.cocoa"]
else:
    hiddenimports += ["gi", "webview.platforms.gtk"]

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hiddenimports,
    # torch is never imported (that is the point of model2vec), and tkinter
    # would double the bundle for a GUI we do not use.
    excludes=["torch", "tkinter", "matplotlib", "pytest"],
    noarchive=False,
)
# PyInstaller's GTK hook copies the build machine's entire icon and theme set —
# 307MB of it here, over half the bundle — to draw widgets this app does not
# use. Any Linux desktop running it already has its own, and the WebKit view
# reads those, not ours.
_BALLAST = ("share/icons/", "share/themes/", "share/doc/")
a.datas = [entry for entry in a.datas
           if not any(part in entry[0].replace("\\", "/") for part in _BALLAST)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="AI Signal",
    console=False,          # a GUI app; the CLI path still writes to stdout
    icon=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="AI Signal")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="AI Signal.app",
        bundle_identifier="com.josephs-ai.aisignal",
        info_plist={
            "NSHighResolutionCapable": True,
            # It never listens; it only reads timelines and calls one API.
            "LSApplicationCategoryType": "public.app-category.news",
        },
    )
