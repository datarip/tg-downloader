"""Allow `python -m tg_downloader` to launch the GUI."""

from .app import main

if __name__ == "__main__":
    raise SystemExit(main())
