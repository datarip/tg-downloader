#!/usr/bin/env bash
# Build the Linux tarball and AppImage from the PyInstaller onedir tree.
#
#   bash packaging/linux/make_appimage.sh 0.1.0
#
# The AppImage is assembled from dist/appdir and packed with a pinned
# appimagetool, whose SHA-256 is checked before it runs. The finished AppImage
# is smoke-tested with the frozen self-check, under xvfb when there is no
# display (CI).
set -euo pipefail

VERSION="${1:?usage: make_appimage.sh VERSION}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

APPIMAGE_TOOL_VERSION="1.9.1"
APPIMAGE_TOOL_SHA256="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
APPIMAGE_TOOL_URL="https://github.com/AppImage/appimagetool/releases/download/${APPIMAGE_TOOL_VERSION}/appimagetool-x86_64.AppImage"

TREE="dist/TDM"
if [ ! -d "$TREE" ]; then
    echo "make_appimage: $TREE is missing; run packaging/build.py first" >&2
    exit 1
fi

mkdir -p dist/release

# 1. Plain tarball, for distributions without FUSE or with an old glibc.
TARBALL="dist/release/TDM-$VERSION-linux-x64.tar.gz"
tar -czf "$TARBALL" -C dist TDM

# 2. AppDir: AppRun, desktop file and icon at the root, the tree under usr/bin.
rm -rf dist/appdir
mkdir -p dist/appdir/usr/bin
cp -R "$TREE" dist/appdir/usr/bin/TDM
install -m 0755 packaging/linux/AppRun dist/appdir/AppRun
install -m 0644 packaging/linux/tdm.desktop dist/appdir/tdm.desktop
install -m 0644 packaging/icons/tdm.png dist/appdir/tdm.png

TOOL="dist/appimagetool-$APPIMAGE_TOOL_VERSION.AppImage"
if [ ! -f "$TOOL" ]; then
    curl -fsSL -o "$TOOL" "$APPIMAGE_TOOL_URL"
fi
echo "$APPIMAGE_TOOL_SHA256  $TOOL" | sha256sum -c -
chmod +x "$TOOL"

OUT="dist/release/TDM-$VERSION-linux-x64.AppImage"
rm -f "$OUT"
# --appimage-extract-and-run avoids needing FUSE 2 both here and on the runner.
ARCH=x86_64 "$TOOL" --appimage-extract-and-run dist/appdir "$OUT"

# 3. Smoke-test the packed image, not just the tree it was built from.
CHECK="$ROOT/dist/appimage-selfcheck.json"
rm -f "$CHECK"
SELF_CHECK=(--appimage-extract-and-run --self-check --self-check-json "$CHECK")
if [ -z "${DISPLAY:-}" ] && command -v xvfb-run >/dev/null 2>&1; then
    xvfb-run -a "$OUT" "${SELF_CHECK[@]}"
else
    "$OUT" "${SELF_CHECK[@]}"
fi

python3 - "$CHECK" <<'PY'
import json
import sys

report = json.load(open(sys.argv[1], encoding="utf-8"))
if report.get("cryptg") is not True:
    sys.exit("make_appimage: the AppImage lost the native cryptg backend")
print(f"make_appimage: self-check ok (tk {report.get('tk')}, cryptg yes)")
PY

sha256sum "$TARBALL" "$OUT"
