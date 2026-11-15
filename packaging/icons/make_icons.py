"""Generate the packaged icon files from the 1024 px artwork master.

Dev-only: run it once after the logo changes, then commit the three outputs.

    uvx --with pillow python packaging/icons/make_icons.py

CI never needs Pillow, because `tdm.png`, `tdm.ico` and `tdm.icns` are committed.
The `.icns` step needs macOS (`iconutil`); on another OS it is skipped with a
warning, so a Windows or Linux contributor can still refresh the PNG and ICO.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
MASTER = ROOT / "logo" / "arrowstem-t-icon-flat-1024.png"
OUT = Path(__file__).resolve().parent

#: Windows icon sizes, per the plan: 16 to 256.
ICO_SIZES = (16, 32, 48, 64, 128, 256)

#: The names `iconutil` expects inside an `.iconset`, with their pixel sizes.
ICONSET = {
    "icon_16x16": 16,
    "icon_16x16@2x": 32,
    "icon_32x32": 32,
    "icon_32x32@2x": 64,
    "icon_128x128": 128,
    "icon_128x128@2x": 256,
    "icon_256x256": 256,
    "icon_256x256@2x": 512,
    "icon_512x512": 512,
    "icon_512x512@2x": 1024,
}


def _load_master() -> Image.Image:
    image = Image.open(MASTER).convert("RGBA")
    if image.size != (1024, 1024):
        image = image.resize((1024, 1024), Image.LANCZOS)
    return image


def _resized(image: Image.Image, size: int) -> Image.Image:
    return image.resize((size, size), Image.LANCZOS)


def build_png(master: Image.Image) -> None:
    _resized(master, 256).save(OUT / "tdm.png", format="PNG")


def build_ico(master: Image.Image) -> None:
    _resized(master, 256).save(
        OUT / "tdm.ico", format="ICO", sizes=[(size, size) for size in ICO_SIZES]
    )


def build_icns(master: Image.Image) -> bool:
    if sys.platform != "darwin" or shutil.which("iconutil") is None:
        print("skipping tdm.icns: iconutil is macOS-only")
        return False
    iconset = OUT / "tdm.iconset"
    shutil.rmtree(iconset, ignore_errors=True)
    iconset.mkdir()
    try:
        for name, size in ICONSET.items():
            _resized(master, size).save(iconset / f"{name}.png", format="PNG")
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(OUT / "tdm.icns")],
            check=True,
        )
    finally:
        shutil.rmtree(iconset, ignore_errors=True)
    return True


def main() -> int:
    master = _load_master()
    build_png(master)
    build_ico(master)
    built_icns = build_icns(master)
    for name in ("tdm.png", "tdm.ico", "tdm.icns"):
        path = OUT / name
        if path.is_file():
            print(f"{path.relative_to(ROOT)}: {path.stat().st_size} bytes")
        elif name != "tdm.icns" or built_icns:
            print(f"{path.relative_to(ROOT)}: missing", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
