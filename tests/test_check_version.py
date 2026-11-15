"""Checks for the release version guard, which gates every tag.

The script is stdlib-only and is not a package, so it is loaded from its path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_version.py"
_spec = importlib.util.spec_from_file_location("check_version", SCRIPT)
assert _spec is not None and _spec.loader is not None
check = importlib.util.module_from_spec(_spec)
sys.modules["check_version"] = check
_spec.loader.exec_module(check)


def test_normalise_accepts_every_spelling(capsys) -> None:
    assert check.normalise("v0.1.0") == "0.1.0"
    assert check.normalise("refs/tags/v0.1.0") == "0.1.0"
    assert check.normalise("0.1.0") == "0.1.0"
    assert check.normalise(" v0.1.0\n") == "0.1.0"


def test_package_version_is_read_from_the_source() -> None:
    version = check.package_version()
    assert version.count(".") >= 2
    assert " " not in version


def test_the_changelog_documents_the_current_version() -> None:
    assert check.changelog_has(check.package_version())


def test_main_prints_the_version_for_github_output(capsys) -> None:
    assert check.main([]) == 0
    assert capsys.readouterr().out == f"version={check.package_version()}\n"


def test_a_matching_tag_is_accepted() -> None:
    version = check.package_version()
    assert check.main([f"v{version}"]) == 0


def test_a_mismatched_tag_is_refused(capsys) -> None:
    assert check.main(["v999.0.0"]) == 1
    assert "does not match" in capsys.readouterr().err


def test_a_missing_changelog_heading_is_refused(tmp_path, monkeypatch, capsys) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n\n## [Unreleased]\n", encoding="utf-8")
    monkeypatch.setattr(check, "CHANGELOG", changelog)
    assert check.main([]) == 1
    assert "no '## [" in capsys.readouterr().err
