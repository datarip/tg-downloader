"""Prove that a build can actually run: Tk initialises, cryptg is native.

Called by ``tg-downloader --self-check`` (and ``--self-check-json PATH``), both
from a source checkout and from the frozen executable. It checks the three
things that a packaging mistake breaks silently:

1. **Tk** — a real ``Tk()`` root is created and destroyed, which proves the
   Tcl/Tk data files made it into the bundle. Without this the app would exit
   with a traceback the user never sees in a console-less Windows build.
2. **cryptg** — the native AES backend is importable, so downloads are not
   silently CPU-bound.
3. **Telethon** — the library imports, including its generated ``tl`` package.

Telethon-free at import time: everything heavy is imported inside the function,
so this module stays importable (and testable) without the Telegram stack.
"""

from __future__ import annotations

import json
import platform
import sys
from pathlib import Path

from . import __version__


def run_self_check(json_path: str | None = None) -> int:
    """Run the checks, optionally write them as JSON, and return 0 or 1.

    The JSON file matters for the Windows build: its console is detached, so the
    exit code and the file are the only signals CI can read.
    """
    info: dict[str, object] = {
        "version": __version__,
        "python": platform.python_version(),
        "tk": None,
        "cryptg": False,
        "frozen": bool(getattr(sys, "frozen", False)),
    }
    errors: list[str] = []

    try:
        import tkinter

        root = tkinter.Tk()
        info["tk"] = str(tkinter.TkVersion)
        root.destroy()
    except Exception as exc:  # any failure here means the GUI cannot start
        errors.append(f"tk: {type(exc).__name__}: {exc}")

    try:
        import telethon

        info["telethon"] = getattr(telethon, "__version__", "unknown")
    except Exception as exc:
        errors.append(f"telethon: {type(exc).__name__}: {exc}")

    try:
        from .telegram import crypto_backend

        info["cryptg"] = crypto_backend() == "cryptg"
        if not info["cryptg"]:
            errors.append("cryptg: the native AES backend is not active")
    except Exception as exc:
        errors.append(f"cryptg: {type(exc).__name__}: {exc}")

    if errors:
        info["errors"] = errors

    print(f"TDM {info['version']} self-check")
    print(f"  python : {info['python']}")
    print(f"  tk     : {info['tk'] or 'unavailable'}")
    print(f"  cryptg : {'yes' if info['cryptg'] else 'no'}")
    print(f"  frozen : {'yes' if info['frozen'] else 'no'}")
    for error in errors:
        print(f"  FAIL   : {error}")

    if json_path:
        try:
            Path(json_path).write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"  FAIL   : cannot write {json_path}: {exc}")
            return 1

    print("self-check: ok" if not errors else "self-check: FAILED")
    return 0 if not errors else 1
