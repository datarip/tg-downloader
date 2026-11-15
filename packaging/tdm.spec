# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller definition for the TDM GUI.

One-dir on every platform: faster startup than one-file, and the wrappers
(Inno Setup, DMG, AppImage) all consume a folder anyway. The CLI is not bundled;
the installers ship the GUI only.

Paths are absolute, derived from ``SPECPATH``, so the build works from any
working directory. Version and icon inputs come from the environment that
``packaging/build.py`` sets up.
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).resolve().parent
VERSION = os.environ.get("TDM_VERSION", "0.0.0")
ICONS = ROOT / "packaging" / "icons"

# Telethon's generated `tl` package and its data files are easy to miss with
# static analysis alone, so collect the whole package.
datas, binaries, hiddenimports = collect_all("telethon")

# Telethon imports cryptg inside a `try:`, which static analysis skips. Without
# this the build succeeds and silently loses the native AES backend, which is
# exactly what the frozen self-check exists to catch.
hiddenimports += ["cryptg"]
datas += [(str(ICONS / "tdm.png"), "assets")]

windows_version = ROOT / "packaging" / "windows" / "version_info.txt"

a = Analysis(
    [str(ROOT / "packaging" / "entry_gui.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "_pytest", "ruff"],
    noarchive=False,
)
pyz = PYZ(a.pure)

# Host libraries to leave to the target system. PyInstaller copies these from
# the Ubuntu 22.04 runner, and they are built against glibc 2.34/2.35, which
# would raise the bundle's floor above the 2.28 that the cryptg wheel sets.
# Every glibc desktop ships them, and the dependency graph that
# packaging/build.py prints when the floor is exceeded shows nothing inside the
# bundle needing libssl.
SYSTEM_LIBS = {"libgcc_s.so.1", "libssl.so.3", "libcrypto.so.3"}
a.binaries = [entry for entry in a.binaries if Path(entry[0]).name not in SYSTEM_LIBS]

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TDM",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ICONS / "tdm.ico") if sys.platform == "win32" else None,
    version=(
        str(windows_version)
        if sys.platform == "win32" and windows_version.is_file()
        else None
    ),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="TDM",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="TDM.app",
        icon=str(ICONS / "tdm.icns"),
        bundle_identifier="com.datarip.tdm",
        info_plist={
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
