"""The leak net's own rules, so a later edit cannot quietly widen them.

`scripts/public_audit.py` is stdlib-only and is not a package, so it is loaded
from its path. Nothing here touches git or the network.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "public_audit.py"
_spec = importlib.util.spec_from_file_location("public_audit", SCRIPT)
assert _spec is not None and _spec.loader is not None
audit = importlib.util.module_from_spec(_spec)
sys.modules["public_audit"] = audit  # a dataclass reads its module during exec
_spec.loader.exec_module(audit)


def rules_for(path: str, line: str) -> list[str]:
    return [finding.rule for finding in audit.scan_content(path, line.encode())]


def _phone(*parts: str) -> str:
    """Assemble a test number so this file is clean under its own audit."""
    return "".join(parts)


#: 32 hex characters, assembled for the same reason as _phone.
HASH = "a1b2c3d4" * 4


def test_a_phone_number_in_a_doc_is_flagged() -> None:
    line = _phone("call +33 ", "6 12 34 56 78 today")
    assert "phone-number" in rules_for("README.md", line)


def test_a_space_grouped_national_number_is_flagged() -> None:
    line = _phone("# ring 020 ", "7123 4567")
    assert "phone-number" in rules_for("src/tg_downloader/app.py", line)


def test_a_number_with_no_separators_is_not_a_phone_shape() -> None:
    assert rules_for("src/tg_downloader/app.py", "id = 1001234567890") == []


def test_the_reserved_example_ranges_are_allowed() -> None:
    for line in ("+1 555 0100", "+44 20 7946 0000", "020 7946 0000", "07700 900123"):
        assert "phone-number" not in rules_for("README.md", line), line


def test_coordinates_hashes_and_hosts_are_not_phones() -> None:
    assert "phone-number" not in rules_for("logo/icon.svg", "0 0 1024 1024")
    assert "phone-number" not in rules_for("logo/icon.svg", "translate(210.29 842.00)")
    assert "phone-number" not in rules_for("uv.lock", 'sha256 = "6385d2b1c1a4e9f0a1b2c3d4"')
    assert "phone-number" not in rules_for("tg.py", 'HOST = ("149.154.160.0", 443)')
    assert "phone-number" not in rules_for("cli.py", "--chat -1001234567890")


def test_a_row_of_space_aligned_columns_is_not_a_phone() -> None:
    row = "    0.9.0 thru 1.2              1991-1995   CWI         yes"
    assert "phone-number" not in rules_for("THIRD_PARTY_NOTICES.md", row)


def test_a_hex_uuid_is_not_a_phone() -> None:
    row = "AppId={{DBA49416-9124-4D6B-9B63-A7BD3244FF33}"
    assert "phone-number" not in rules_for("packaging/windows/tdm.iss", row)


def test_the_placeholder_api_hash_is_allowed_and_a_real_one_is_not() -> None:
    placeholder = "TG_API_HASH=0123456789abcdef0123456789abcdef"
    real = f"TG_API_HASH={HASH}"
    assert "api-hash" not in rules_for(".env.example", placeholder)
    assert "api-hash" in rules_for(".env.example", real)


def test_a_quoted_api_hash_is_flagged() -> None:
    line = f'api_hash = "{HASH}"'
    assert "api-hash-quoted" in rules_for("config.json", line)


def test_denied_paths_cover_the_local_only_files() -> None:
    assert audit.denied_path("AGENTS.md") is not None
    assert audit.denied_path(".env") is not None
    assert audit.denied_path("config.json") is not None
    assert audit.denied_path("tg_session.session") is not None
    assert audit.denied_path("docs/implementation-plan/plan.md") is not None
    assert audit.denied_path(".claude/settings.local.json") is not None


def test_ordinary_project_files_are_allowed() -> None:
    assert audit.denied_path(".env.example") is None
    assert audit.denied_path("src/tg_downloader/app.py") is None
    assert audit.denied_path("screenshots/1-download-list.png") is None
