"""Locate files the app ships with, frozen or from a source checkout.

PyInstaller sets ``sys._MEIPASS`` to the folder it unpacked the bundle into (in
a one-dir build, the ``_internal`` directory beside the executable). From a
checkout the same artwork lives in ``packaging/icons``.

Nothing here imports Tk or Telethon: the caller decides whether a missing asset
is fatal. The window icon treats it as optional, because an app that refuses to
start over an icon is worse than an app with the default icon.
"""

from __future__ import annotations

import sys
from pathlib import Path


def asset_roots() -> list[Path]:
    """Every directory an asset may live in, most specific first."""
    roots: list[Path] = []
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        roots.append(Path(bundled) / "assets")
    package = Path(__file__).resolve().parent
    roots.append(package / "assets")  # if an asset is ever shipped as package data
    roots.append(package.parents[1] / "packaging" / "icons")  # repo root, source run
    return roots


def asset_path(name: str) -> Path:
    """The first existing ``name`` under :func:`asset_roots`, or ``FileNotFoundError``."""
    for root in asset_roots():
        candidate = root / name
        if candidate.is_file():
            return candidate
    looked = ", ".join(str(root) for root in asset_roots())
    raise FileNotFoundError(f"asset {name!r} not found (looked in: {looked})")
