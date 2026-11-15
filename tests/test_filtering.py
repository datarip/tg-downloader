"""The per-column Files-tab filter is pure logic, so it's unit-tested here
without any Tk widgets or a Telegram session — the GUI just feeds it primitives."""

from tg_downloader.filtering import MB, FileFilter, status_category, status_note


class TestStatusCategory:
    def test_downloading_matches_by_prefix(self):
        assert status_category("downloading…", True) == "downloading"

    def test_throttles_read_as_paused_when_ticked(self):
        # A throttle is a wait: the download is parked on the rate-limit cooldown,
        # so it reads "paused" (and returns to "downloading" when the wait lapses).
        assert status_category("speed capped", True) == "paused"
        assert status_category("rate limited", True) == "paused"

    def test_ticked_error_and_retry_read_as_queued(self):
        # A still-selected error re-queues, so it's queued; the reason is the note.
        assert status_category("error: not found", True) == "queued"
        assert status_category("retry: timeout", True) == "queued"

    def test_completed_covers_completed_and_on_disk(self):
        assert status_category("completed", False) == "completed"
        assert status_category("on disk", False) == "completed"

    def test_ticked_and_blank_is_queued(self):
        assert status_category("", True) == "queued"

    def test_unticked_and_blank_is_skipped_not_queued(self):
        # A file the user set aside reads "skipped", never "queued".
        assert status_category("", False) == "skipped"

    def test_unticked_throttle_or_error_is_skipped(self):
        # Skipped (unticked) rows never count as queued, whatever their status.
        assert status_category("speed capped", False) == "skipped"
        assert status_category("error: not found", False) == "skipped"

    def test_lifecycle_states_get_their_own_category(self):
        assert status_category("moved", False) == "moved"
        assert status_category("incomplete", True) == "incomplete"
        assert status_category("paused", True) == "paused"
        assert status_category("stopped", True) == "stopped"

    def test_unknown_status_falls_through_to_other(self):
        assert status_category("something weird", True) == "other"


class TestStatusNote:
    def test_error_strips_prefix(self):
        assert status_note("error: not found") == "not found"
        assert status_note("error") == "error"

    def test_retry_is_kept_verbatim(self):
        assert status_note("retry: timeout") == "retry: timeout"

    def test_throttles_and_healthy_rows_have_no_note(self):
        assert status_note("speed capped") == ""
        assert status_note("rate limited") == ""
        assert status_note("downloading… 40%") == ""
        assert status_note("") == ""


class TestFileFilterEmpty:
    def test_default_filter_is_empty_and_matches_anything(self):
        f = FileFilter()
        assert f.is_empty()
        assert f.matches(
            name="whatever.zip", channel="Grp", size=1, progress=0, category="other"
        )

    def test_any_field_makes_it_non_empty(self):
        assert not FileFilter(name_contains="a").is_empty()
        assert not FileFilter(size_max_mb=10).is_empty()
        assert not FileFilter(status_exclude=frozenset({"queued"})).is_empty()


def _match(f, **over):
    base = dict(
        name="Q3-report.zip", channel="Data Group", size=5 * MB, progress=42, category="queued"
    )
    base.update(over)
    return f.matches(**base)


class TestNameAndChannel:
    def test_name_contains_is_case_insensitive(self):
        assert _match(FileFilter(name_contains="REPORT"))
        assert not _match(FileFilter(name_contains="invoice"))

    def test_name_excludes(self):
        assert not _match(FileFilter(name_excludes="report"))
        assert _match(FileFilter(name_excludes="invoice"))

    def test_channel_contains_and_excludes(self):
        assert _match(FileFilter(channel_contains="data"))
        assert not _match(FileFilter(channel_contains="photos"))
        assert not _match(FileFilter(channel_excludes="group"))


class TestKeywordLists:
    """Multiple pill keywords combine with OR (contains → any, excludes → any)."""

    def test_contains_matches_any_keyword(self):
        f = FileFilter(name_contains=["invoice", "report"])
        assert _match(f)  # "Q3-report.zip" hits "report"
        assert _match(f, name="2024-invoice.pdf")  # hits "invoice"
        assert not _match(f, name="holiday-photo.jpg")  # hits neither

    def test_excludes_hides_on_any_keyword(self):
        f = FileFilter(name_excludes=["tmp", "report"])
        assert not _match(f)  # "report" present → hidden
        assert not _match(f, name="scratch.tmp")  # "tmp" present → hidden
        assert _match(f, name="final.zip")  # neither → shown

    def test_channel_keyword_lists(self):
        assert _match(FileFilter(channel_contains=["photos", "data"]))
        assert not _match(FileFilter(channel_contains=["photos", "music"]))
        assert not _match(FileFilter(channel_excludes=["music", "group"]))

    def test_keyword_lists_are_case_insensitive(self):
        assert _match(FileFilter(name_contains=["REPORT"]))
        assert not _match(FileFilter(name_excludes=["REPORT"]))

    def test_blank_and_whitespace_terms_are_dropped(self):
        # An all-blank list constrains nothing (mirrors an empty pill field).
        f = FileFilter(name_contains=["", "  "])
        assert f.is_empty()
        assert _match(f)


class TestSizeBounds:
    def test_size_above(self):
        assert _match(FileFilter(size_min_mb=1), size=5 * MB)
        assert not _match(FileFilter(size_min_mb=10), size=5 * MB)

    def test_size_below(self):
        assert _match(FileFilter(size_max_mb=10), size=5 * MB)
        assert not _match(FileFilter(size_max_mb=1), size=5 * MB)

    def test_size_window(self):
        f = FileFilter(size_min_mb=2, size_max_mb=8)
        assert _match(f, size=5 * MB)
        assert not _match(f, size=1 * MB)
        assert not _match(f, size=9 * MB)


class TestProgressBounds:
    def test_progress_above_and_below(self):
        assert _match(FileFilter(progress_min=40), progress=42)
        assert not _match(FileFilter(progress_min=50), progress=42)
        assert _match(FileFilter(progress_max=50), progress=42)
        assert not _match(FileFilter(progress_max=40), progress=42)


class TestStatusExclude:
    def test_empty_exclude_matches_any_category(self):
        # All boxes ticked (the default) → hide nothing.
        assert _match(FileFilter(), category="downloading")
        assert _match(FileFilter(), category="other")

    def test_excluded_categories_are_hidden_rest_shown(self):
        # Unticking "completed" and "downloading" hides only those.
        f = FileFilter(status_exclude=frozenset({"completed", "downloading"}))
        assert not _match(f, category="completed")
        assert not _match(f, category="downloading")
        assert _match(f, category="queued")
        # "other" (unknown) has no checkbox, so it's never excluded.
        assert _match(f, category="other")


class TestCombined:
    def test_all_predicates_must_pass(self):
        f = FileFilter(
            name_contains="report",
            channel_excludes="photos",
            size_min_mb=1,
            progress_max=90,
            status_exclude=frozenset({"downloading"}),
        )
        assert _match(f)
        # Fails the size floor.
        assert not _match(f, size=10)
        # Fails the status gate (category is excluded).
        assert not _match(f, category="downloading")
