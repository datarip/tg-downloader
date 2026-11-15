"""PyInstaller entry point for the GUI.

A script, not a module: PyInstaller executes the entry file as ``__main__``, so
``src/tg_downloader/__main__.py``'s relative import would fail there. This file
is the reason the packaging folder exists at all.
"""

from tg_downloader.app import main

if __name__ == "__main__":
    raise SystemExit(main())
