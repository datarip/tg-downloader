"""The pure parts of `packaging/build.py`.

The build itself needs PyInstaller and a display, so it is exercised by
`packaging/build.py` and CI. What is worth testing here is the logic that
decides whether a build is acceptable: the glibc floor and the Windows version
resource.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "packaging" / "build.py"
_spec = importlib.util.spec_from_file_location("packaging_build", SCRIPT)
assert _spec is not None and _spec.loader is not None
build = importlib.util.module_from_spec(_spec)
sys.modules["packaging_build"] = build
_spec.loader.exec_module(build)


def test_highest_glibc_takes_the_largest_version() -> None:
    output = "GLIBC_2.2.5 GLIBC_2.14 GLIBC_2.28 GLIBC_2.4"
    assert build.highest_glibc(output) == (2, 28)


def test_highest_glibc_ignores_other_symbols() -> None:
    assert build.highest_glibc("GLIBCXX_3.4.21") is None
    assert build.highest_glibc("") is None


def test_needed_libraries_reads_dt_needed() -> None:
    output = """
Dynamic Section:
  NEEDED               libc.so.6
  NEEDED               libcrypto.so.3
  RUNPATH              /none
"""
    assert build.needed_libraries(output) == ["libc.so.6", "libcrypto.so.3"]
    assert build.needed_libraries("no dynamic section") == []


def test_version_quad_pads_to_four_numbers() -> None:
    assert build.version_quad("0.1.0") == "0, 1, 0, 0"
    assert build.version_quad("2.0") == "2, 0, 0, 0"
    assert build.version_quad("1.2.3.4") == "1, 2, 3, 4"


def test_version_quad_drops_a_prerelease_suffix() -> None:
    assert build.version_quad("1.2.3-rc1") == "1, 2, 3, 0"
    assert build.version_quad("0.1.0-dev") == "0, 1, 0, 0"


def test_the_documented_glibc_floor_matches_the_readme() -> None:
    readme = (SCRIPT.parents[1] / "README.md").read_text(encoding="utf-8")
    assert f"glibc {build.GLIBC_FLOOR[0]}.{build.GLIBC_FLOOR[1]:02d} or later" in readme
