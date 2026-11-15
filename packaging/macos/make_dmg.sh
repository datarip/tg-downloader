#!/usr/bin/env bash
# Build the macOS disk image from the PyInstaller app bundle.
#
#   bash packaging/macos/make_dmg.sh 0.1.0
#
# Unsigned on purpose: signing and notarisation are a later phase. The README
# documents the macOS 15 "Open Anyway" path for the first launch.
set -euo pipefail

VERSION="${1:?usage: make_dmg.sh VERSION}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

APP="dist/TDM.app"
if [ ! -d "$APP" ]; then
    echo "make_dmg: $APP is missing; run packaging/build.py first" >&2
    exit 1
fi

rm -rf dist/dmg
mkdir -p dist/dmg dist/release
cp -R "$APP" dist/dmg/
ln -s /Applications dist/dmg/Applications
cp THIRD_PARTY_NOTICES.md LICENSE dist/dmg/

OUT="dist/release/TDM-$VERSION-macos-arm64.dmg"
rm -f "$OUT"
hdiutil create \
    -volname "TDM $VERSION" \
    -srcfolder dist/dmg \
    -ov -format UDZO \
    "$OUT"

shasum -a 256 "$OUT"
