"""Scan cache: round-trips, and the merge rules that make resume/rescan correct."""

import sys

from tg_downloader.models import FileItem
from tg_downloader.store import ChannelScan, load_scans, merge_scan, save_scans


def f(msg_id, name="a.csv", size=100, *, chat=-100, title="Grp", selected=True):
    return FileItem(chat, title, msg_id, name, size, ".csv", selected=selected)


# -- persistence -------------------------------------------------------


def test_round_trip(tmp_path):
    p = tmp_path / "scans.json"
    scans = {
        "-100": ChannelScan(-100, "Grp", 42, [f(42, selected=False)], {7, 9}),
    }
    save_scans(scans, p)
    back = load_scans(p)
    assert back["-100"].last_msg_id == 42
    assert back["-100"].discarded == {7, 9}
    assert back["-100"].files[0].msg_id == 42
    assert back["-100"].files[0].selected is False


def test_saved_cache_is_owner_only(tmp_path):
    p = tmp_path / "scans.json"
    save_scans({"-1": ChannelScan(-1)}, p)
    if sys.platform != "win32":
        assert oct(p.stat().st_mode)[-3:] == "600"


def test_missing_file_loads_empty(tmp_path):
    assert load_scans(tmp_path / "nope.json") == {}


def test_corrupt_file_loads_empty(tmp_path):
    p = tmp_path / "scans.json"
    p.write_text("{ this is not json")
    assert load_scans(p) == {}


def test_one_bad_channel_entry_doesnt_sink_the_rest(tmp_path):
    p = tmp_path / "scans.json"
    p.write_text(
        '{"version": 1, "channels": {"-1": "garbage", "-2": {"chat_id": -2, "files": []}}}'
    )
    scans = load_scans(p)
    assert "-2" in scans and "-1" not in scans


# -- update scan -------------------------------------------------------


def test_update_appends_new_and_advances_watermark():
    prev = ChannelScan(-100, "Grp", 10, [f(10)], set())
    after = merge_scan(prev, [f(20), f(30)], watermark=30, full=False)
    assert [x.msg_id for x in after.files] == [10, 20, 30]
    assert after.last_msg_id == 30


def test_update_preserves_prior_tick_state():
    prev = ChannelScan(-100, "Grp", 10, [f(10, selected=False)], set())
    after = merge_scan(prev, [f(20)], watermark=20, full=False)
    kept = {x.msg_id: x.selected for x in after.files}
    assert kept == {10: False, 20: True}


def test_update_from_nothing_behaves_like_full():
    after = merge_scan(None, [f(5), f(6)], watermark=6, full=False)
    assert [x.msg_id for x in after.files] == [5, 6]
    assert after.last_msg_id == 6


def test_watermark_advances_even_when_batch_is_empty():
    """Newest message may be non-matching; the watermark must still move."""
    prev = ChannelScan(-100, "Grp", 10, [f(10)], set())
    after = merge_scan(prev, [], watermark=55, full=False)
    assert after.last_msg_id == 55
    assert [x.msg_id for x in after.files] == [10]


# -- full rescan -------------------------------------------------------


def test_full_rebuilds_and_drops_vanished_files():
    prev = ChannelScan(-100, "Grp", 30, [f(10), f(20), f(30)], set())
    after = merge_scan(prev, [f(20), f(30), f(40)], watermark=40, full=True)
    assert [x.msg_id for x in after.files] == [20, 30, 40]  # 10 deleted upstream


def test_full_carries_over_ticks_for_surviving_files():
    prev = ChannelScan(-100, "Grp", 20, [f(10, selected=False), f(20)], set())
    after = merge_scan(prev, [f(10), f(20)], watermark=20, full=True)
    assert {x.msg_id: x.selected for x in after.files} == {10: False, 20: True}


def test_full_carries_over_set_aside_marker():
    """A file set aside on a failure stays set aside through a full re-scan."""
    aside = f(10, selected=False)
    aside.set_aside = "not found"
    prev = ChannelScan(-100, "Grp", 20, [aside, f(20)], set())
    after = merge_scan(prev, [f(10), f(20)], watermark=20, full=True)
    assert {x.msg_id: x.set_aside for x in after.files} == {10: "not found", 20: ""}


# -- discards stick ----------------------------------------------------


def test_discarded_ids_are_not_resurrected_by_update():
    prev = ChannelScan(-100, "Grp", 10, [], {20})
    after = merge_scan(prev, [f(20), f(21)], watermark=21, full=False)
    assert [x.msg_id for x in after.files] == [21]  # 20 stays discarded
    assert after.discarded == {20}


def test_discarded_ids_are_not_resurrected_by_full():
    prev = ChannelScan(-100, "Grp", 30, [], {20})
    after = merge_scan(prev, [f(10), f(20), f(30)], watermark=30, full=True)
    assert [x.msg_id for x in after.files] == [10, 30]
    assert after.discarded == {20}
