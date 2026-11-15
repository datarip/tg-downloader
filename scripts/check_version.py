#!/usr/bin/env python3
"""Check that a release tag matches the package version and the changelog.

    python3 scripts/check_version.py            # the current __version__
    python3 scripts/check_version.py v0.1.0     # a tag, or a bare version

Reads ``__version__`` with a regex instead of importing the package, so this
runs on a bare Python with no dependencies. Prints ``version=X.Y.Z`` for the
calling workflow, and exits 1 with a message on any mismatch.

The tag check is the point: a tag that does not equal ``__version__`` would
publish installers labelled with the wrong number, which cannot be undone after
people download them.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "src" / "tg_downloader" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"

VERSION_PATTERN = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def normalise(ref: str) -> str:
    """Strip a ``refs/tags/`` prefix and a leading ``v``.

    >>> normalise("refs/tags/v0.1.0")
    '0.1.0'
    >>> normalise("v0.1.0")
    '0.1.0'
    >>> normalise("0.1.0")
    '0.1.0'
    """
    ref = ref.strip()
    if ref.startswith("refs/tags/"):
        ref = ref[len("refs/tags/") :]
    if ref.startswith("v"):
        ref = ref[1:]
    return ref


def package_version() -> str:
    """The version declared in ``src/tg_downloader/__init__.py``."""
    match = VERSION_PATTERN.search(INIT.read_text(encoding="utf-8"))
    if match is None:
        raise SystemExit(f"check_version: no __version__ assignment in {INIT}")
    return match.group(1)


def changelog_has(version: str) -> bool:
    """True when CHANGELOG.md has a ``## [version]`` heading."""
    heading = re.compile(rf"^## \[{re.escape(version)}\](?:\s|$)", re.MULTILINE)
    return heading.search(CHANGELOG.read_text(encoding="utf-8")) is not None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_version",
        description="Verify a tag against __version__ and CHANGELOG.md.",
    )
    parser.add_argument(
        "tag",
        nargs="?",
        help="tag or version to verify (defaults to the package version)",
    )
    args = parser.parse_args(argv)

    version = package_version()

    if args.tag is not None:
        tag_version = normalise(args.tag)
        if tag_version != version:
            print(
                f"check_version: tag '{args.tag}' does not match __version__ "
                f"'{version}' in {INIT.relative_to(ROOT)}",
                file=sys.stderr,
            )
            return 1

    if not changelog_has(version):
        print(
            f"check_version: CHANGELOG.md has no '## [{version}]' heading",
            file=sys.stderr,
        )
        return 1

    print(f"version={version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
