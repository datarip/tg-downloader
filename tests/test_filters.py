"""Extension matching is the heart of the app — if it's wrong you either miss
files you wanted or fill your disk with ones you didn't."""

from pathlib import Path

import pytest

from tg_downloader.config import DEFAULT_CONFIG
from tg_downloader.filters import (
    human_size,
    matched_extension,
    parse_extensions,
    safe_name,
    subfolder_for,
)

DEFAULT_ALLOWED = parse_extensions(DEFAULT_CONFIG["extensions"])


class TestParseExtensions:
    def test_defaults_parse_to_expected_set(self):
        assert DEFAULT_ALLOWED == {
            ".zip",
            ".rar",
            ".7z",
            ".tar",
            ".tar.gz",
            ".tgz",
            ".tar.bz2",
            ".tar.xz",
            ".csv",
            ".json",
            ".jsonl",
            ".txt",
        }

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("zip, csv", {".zip", ".csv"}),  # missing leading dots
            (".ZIP  .Csv", {".zip", ".csv"}),  # case + whitespace only
            (".zip,,, .csv,", {".zip", ".csv"}),  # stray separators
            ("  ", set()),  # empty
            (".zip .zip", {".zip"}),  # deduped
        ],
    )
    def test_tolerates_messy_input(self, raw, expected):
        assert parse_extensions(raw) == expected


class TestMatchedExtension:
    @pytest.mark.parametrize(
        "filename, expected",
        [
            ("data.zip", ".zip"),
            ("Archive.RAR", ".rar"),  # case-insensitive
            ("x.7z", ".7z"),
            ("backup.tar", ".tar"),
            ("export.csv", ".csv"),
            ("a.json", ".json"),
            ("events.jsonl", ".jsonl"),  # must not be read as .json
            ("notes.txt", ".txt"),
            ("my.notes.v2.txt", ".txt"),  # multiple dots
            ("UPPER.CSV", ".csv"),
        ],
    )
    def test_accepts_allowed(self, filename, expected):
        assert matched_extension(filename, DEFAULT_ALLOWED) == expected

    @pytest.mark.parametrize(
        "filename",
        [
            "photo.jpg",
            "clip.mp4",
            "doc.pdf",
            "song.mp3",
            "sheet.xlsx",
            "script.js",
            "noext",
            "archive.gz",
        ],
    )
    def test_rejects_everything_else(self, filename):
        assert matched_extension(filename, DEFAULT_ALLOWED) is None

    def test_compound_extension_beats_shorter_one(self):
        """'.tar.gz' must win over '.gz' when both are allowed."""
        allowed = {".gz", ".tar.gz"}
        assert matched_extension("logs.tar.gz", allowed) == ".tar.gz"

    def test_empty_allowlist_matches_nothing(self):
        assert matched_extension("data.zip", set()) is None


class TestSubfolderFor:
    @pytest.mark.parametrize("ext", [".zip", ".rar", ".7z", ".tar", ".tar.gz"])
    def test_archives(self, ext):
        assert subfolder_for(ext) == "archives"

    @pytest.mark.parametrize("ext", [".csv", ".json", ".jsonl", ".txt"])
    def test_data(self, ext):
        assert subfolder_for(ext) == "data"


class TestHumanSize:
    @pytest.mark.parametrize(
        "n, expected",
        [
            (None, "0 B"),
            (0, "0 B"),
            (512, "512 B"),
            (2048, "2.00 KB"),
            (5 * 1024**2, "5.00 MB"),
            (3 * 1024**3, "3.00 GB"),
            (2 * 1024**4, "2.00 TB"),
            (1_800_000_000_000, "1.64 TB"),
            (1_850_000_000_000, "1.68 TB"),
        ],
    )
    def test_formats(self, n, expected):
        assert human_size(n) == expected


class TestSafeName:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("My Group", "My Group"),
            ("a/b:c*d", "a_b_c_d"),
            ("", "chat"),
            ("   ", "chat"),
            ("report.tar.gz", "report.tar.gz"),  # dots preserved
            ("café", "café"),  # unicode letters kept
            ("CON", "_CON"),  # Windows reserved device name
            ("aux.csv", "_aux.csv"),  # reserved stem, extension kept
            ("com7", "_com7"),
            ("report. ", "report"),  # Windows strips trailing dot/space
            ("...", "chat"),
        ],
    )
    def test_sanitises(self, raw, expected):
        assert safe_name(raw) == expected

    @pytest.mark.parametrize("hostile", ["../../etc/passwd", "/etc/passwd", "..\\..\\win"])
    def test_neutralises_path_traversal(self, hostile):
        """A hostile chat title must not escape the download dir.

        The guarantee is that no separator survives, so the result can only
        ever be a single path component.
        """
        out = safe_name(hostile)
        assert "/" not in out
        assert "\\" not in out
        assert Path(out).name == out


class TestHumanDuration:
    @pytest.mark.parametrize(
        "secs, expected",
        [
            (0, "0s"),
            (5, "5s"),
            (59, "59s"),
            (60, "1m 0s"),
            (90, "1m 30s"),
            (3599, "59m 59s"),
            (3600, "1h 0m"),
            (3661, "1h 1m"),
            (86400, "1d 0h"),
            (90000, "1d 1h"),
            (-10, "0s"),  # negative clamps to zero
        ],
    )
    def test_formats(self, secs, expected):
        from tg_downloader.filters import human_duration

        assert human_duration(secs) == expected
