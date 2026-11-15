"""Target-path construction — this decides where bytes land on disk."""

import os
from pathlib import Path

import pytest

from tg_downloader.models import (
    FileItem,
    dedupe,
    download_dir_problem,
    duplicate_key,
    split_resume_first,
)


def item(**kw) -> FileItem:
    base = dict(
        chat_id=-100123,
        chat_title="My Group",
        msg_id=42,
        name="data.csv",
        size=1024,
        ext=".csv",
    )
    base.update(kw)
    return FileItem(**base)


def test_data_file_layout(tmp_path):
    p = item().target_path(tmp_path)
    assert p == tmp_path / "My Group" / "data" / "42_data.csv"


def test_archive_file_layout(tmp_path):
    p = item(name="dump.zip", ext=".zip").target_path(tmp_path)
    assert p == tmp_path / "My Group" / "archives" / "42_dump.zip"


def test_msg_id_prefix_keeps_duplicates_apart(tmp_path):
    a = item(msg_id=1).target_path(tmp_path)
    b = item(msg_id=2).target_path(tmp_path)
    assert a != b
    assert a.parent == b.parent


def test_part_path_preserves_compound_extension(tmp_path):
    """`with_suffix` would mangle .tar.gz — make sure we don't use it."""
    it = item(name="logs.tar.gz", ext=".tar.gz")
    assert it.part_path(tmp_path).name == "42_logs.tar.gz.part"
    assert it.part_path(tmp_path) == Path(str(it.target_path(tmp_path)) + ".part")


def test_part_path_for_extensionless_name(tmp_path):
    it = item(name="README", ext=".txt")
    assert it.part_path(tmp_path).name == "42_README.part"


# -- existing_duplicate: the "already on disk" safety net --------------


def place(it, tmp_path, filename, size):
    """Drop a file of ``size`` bytes into the item's target folder."""
    folder = it.target_path(tmp_path).parent
    folder.mkdir(parents=True, exist_ok=True)
    (folder / filename).write_bytes(b"x" * size)


def test_no_folder_means_no_duplicate(tmp_path):
    assert item(size=10).existing_duplicate(tmp_path) is None


def test_same_name_and_size_under_another_msg_id_is_a_duplicate(tmp_path):
    it = item(name="data.csv", size=500, msg_id=1)
    place(it, tmp_path, "99_data.csv", 500)  # a repost, different id
    assert it.existing_duplicate(tmp_path).name == "99_data.csv"


def test_bare_hand_placed_name_matches(tmp_path):
    it = item(name="data.csv", size=500)
    place(it, tmp_path, "data.csv", 500)  # dropped in by hand
    assert it.existing_duplicate(tmp_path) is not None


def test_same_name_different_size_is_not_a_duplicate(tmp_path):
    it = item(name="data.csv", size=500)
    place(it, tmp_path, "99_data.csv", 501)
    assert it.existing_duplicate(tmp_path) is None


def test_exact_target_is_not_its_own_duplicate(tmp_path):
    """The item's own file is the 'completed' case, handled separately — not here."""
    it = item(name="data.csv", size=500, msg_id=1)
    place(it, tmp_path, "1_data.csv", 500)  # this IS the exact target
    assert it.existing_duplicate(tmp_path) is None


def test_underscored_filename_still_matches(tmp_path):
    it = item(name="my_data_v2.csv", size=7, msg_id=1)
    place(it, tmp_path, "42_my_data_v2.csv", 7)  # only the id prefix is stripped
    assert it.existing_duplicate(tmp_path) is not None


@pytest.mark.parametrize("hostile_title", ["../../../etc", "/absolute/evil"])
def test_hostile_chat_title_stays_inside_download_dir(tmp_path, hostile_title):
    p = item(chat_title=hostile_title).target_path(tmp_path)
    assert tmp_path.resolve() in p.resolve().parents


@pytest.mark.parametrize("hostile_name", ["../../evil.csv", "/etc/evil.csv"])
def test_hostile_filename_stays_inside_download_dir(tmp_path, hostile_name):
    """Filenames come from other people's uploads — treat them as untrusted."""
    p = item(name=hostile_name).target_path(tmp_path)
    assert tmp_path.resolve() in p.resolve().parents
    assert p.parent == tmp_path / "My Group" / "data"


def test_selected_by_default():
    assert item().selected is True
    assert item().status == ""


def test_downloaded_flag_round_trips():
    """The 'we finished this' flag must survive a save/load — it drives 'moved'."""
    rec = item(downloaded=True).as_record()
    assert rec["downloaded"] is True
    assert FileItem.from_record(rec).downloaded is True
    assert FileItem.from_record({"msg_id": 1}).downloaded is False  # default


def test_status_and_progress_are_not_persisted():
    rec = item(status="downloading…", progress=42).as_record()
    assert "status" not in rec and "progress" not in rec


def test_set_aside_label_round_trips():
    """A file the app set aside on a failure must reload as skipped-with-reason."""
    rec = item(set_aside="not found").as_record()
    assert rec["set_aside"] == "not found"
    assert FileItem.from_record(rec).set_aside == "not found"
    assert FileItem.from_record({"msg_id": 1}).set_aside == ""  # default


# -- de-duplication ----------------------------------------------------


def test_dedupe_drops_reposts_across_channels():
    """Same name and size, different chat/msg_id — the repost pattern."""
    a = item(chat_title="A", msg_id=1, name="dump.zip", size=500, ext=".zip")
    b = item(chat_title="B", msg_id=99, name="dump.zip", size=500, ext=".zip")
    kept, dropped = dedupe([a, b])
    assert kept == [a]  # first-scanned copy wins
    assert dropped == [b]


def test_dedupe_keeps_same_name_different_size():
    a = item(name="data.csv", size=500)
    b = item(name="data.csv", size=501)
    kept, dropped = dedupe([a, b])
    assert dropped == []
    assert kept == [a, b]


def test_dedupe_is_case_insensitive_on_name():
    a = item(name="Report.CSV", size=10)
    b = item(name="report.csv", size=10)
    _, dropped = dedupe([a, b])
    assert dropped == [b]


def test_dedupe_collapses_more_than_two_copies():
    items = [
        item(chat_title=str(i), msg_id=i, name="x.zip", size=7, ext=".zip") for i in range(4)
    ]
    kept, dropped = dedupe(items)
    assert len(kept) == 1 and len(dropped) == 3


def test_dedupe_preserves_order_of_kept_items():
    a = item(name="a.csv", size=1)
    b = item(name="b.csv", size=1)
    a2 = item(name="a.csv", size=1, msg_id=2)
    kept, _ = dedupe([a, b, a2])
    assert kept == [a, b]


def test_dedupe_empty():
    assert dedupe([]) == ([], [])


def test_duplicate_key_ignores_msg_id_and_chat():
    a = item(chat_id=1, chat_title="A", msg_id=1, name="F.txt", size=3, ext=".txt")
    b = item(chat_id=2, chat_title="B", msg_id=2, name="f.txt", size=3, ext=".txt")
    assert duplicate_key(a) == duplicate_key(b)


# --- split_resume_first: resume oldest partial downloads first --------------


def _make_part(it, tmp_path, size, mtime):
    """Write a `.part` of `size` bytes for `it` and stamp its mtime."""
    part = it.part_path(tmp_path)
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"x" * size)
    os.utime(part, (mtime, mtime))
    return part


def test_split_resume_first_orders_oldest_part_first(tmp_path):
    a = item(msg_id=1, name="a.csv")
    b = item(msg_id=2, name="b.csv")
    c = item(msg_id=3, name="c.csv")  # no .part → fresh
    _make_part(b, tmp_path, 10, mtime=100)  # newer .part, enqueued first
    _make_part(a, tmp_path, 10, mtime=50)  # longest-stalled — must come first
    resume, fresh = split_resume_first([("i1", a), ("i2", b), ("i3", c)], tmp_path)
    assert resume == ["i1", "i2"]  # a (mtime 50) before b (mtime 100)
    assert fresh == ["i3"]


def test_split_resume_first_treats_empty_part_as_resume(tmp_path):
    # A zero-byte `.part` is an interrupted download (shown as "incomplete" at
    # 0%), not a fresh file — it must resume so it stays ahead of fresh work.
    a = item(msg_id=1, name="a.csv")
    _make_part(a, tmp_path, 0, mtime=50)
    resume, fresh = split_resume_first([("i1", a)], tmp_path)
    assert resume == ["i1"]
    assert fresh == []


def test_split_resume_first_keeps_fresh_input_order(tmp_path):
    xs = [item(msg_id=i, name=f"{i}.csv") for i in range(3)]
    resume, fresh = split_resume_first([(f"i{i}", x) for i, x in enumerate(xs)], tmp_path)
    assert resume == []
    assert fresh == ["i0", "i1", "i2"]


# -- download folder pre-flight -------------------------------------------
#
# The check that stops an unmounted drive from failing (and unticking) an
# entire queue one file at a time.


def test_download_dir_problem_accepts_a_writable_folder(tmp_path):
    assert download_dir_problem(tmp_path) == ""


def test_download_dir_problem_creates_a_missing_folder(tmp_path):
    target = tmp_path / "new" / "downloads"
    assert download_dir_problem(target) == ""
    assert target.is_dir()  # a first run into a fresh folder still works


def test_download_dir_problem_reports_an_unwritable_folder(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores the permission bits")
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        problem = download_dir_problem(locked)
        assert "isn't writable" in problem
        assert str(locked) in problem
    finally:
        locked.chmod(0o700)  # so tmp_path cleanup can remove it


def test_download_dir_problem_reports_an_uncreatable_folder(tmp_path):
    # Stand-in for the real case: /Volumes/<drive> after the drive is ejected,
    # where mkdir fails because /Volumes is root-owned.
    if os.geteuid() == 0:
        pytest.skip("root ignores the permission bits")
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o500)
    try:
        problem = download_dir_problem(parent / "gone")
        assert "Can't create the download folder" in problem
    finally:
        parent.chmod(0o700)


def test_download_dir_problem_rejects_a_file(tmp_path):
    f = tmp_path / "not-a-folder"
    f.write_text("x")
    assert "not a folder" in download_dir_problem(f)


def test_download_dir_problem_rejects_an_empty_setting():
    assert "No download folder" in download_dir_problem("   ")


def test_download_dir_problem_leaves_no_probe_file_behind(tmp_path):
    download_dir_problem(tmp_path)
    assert list(tmp_path.iterdir()) == []
