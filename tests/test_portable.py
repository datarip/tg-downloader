"""Portable backup bundle: round-trips, the session never travels, safe merges."""

import json
import sys

import pytest

from tg_downloader.models import FileItem
from tg_downloader.portable import (
    export_bundle,
    import_bundle,
    merge_channels,
    merge_imported_scans,
    read_bundle,
    write_bundle,
)
from tg_downloader.store import ChannelScan


def f(msg_id, name="a.csv", size=100, *, chat=-100, title="Grp", selected=True, set_aside=""):
    return FileItem(
        chat, title, msg_id, name, size, ".csv", selected=selected, set_aside=set_aside
    )


def sample_cfg():
    return {
        "api_id": "12345",
        "api_hash": "deadbeef",
        "download_dir": "/Users/alice/Downloads/TG",  # machine-specific
        "extensions": ".zip, .csv",
        "scan_limit": "500",
        "download_concurrency": "2",
        "download_connections": "8",
        "flood_sleep_threshold": "5",
        "favourites": [{"id": -7, "title": "Fav"}],
        "filters": {"name": "log"},
        "view": {},
        "channels": [{"id": -100, "title": "Grp"}, {"id": "@x", "title": ""}],
    }


def sample_scans():
    files = [f(42, selected=False), f(43, set_aside="timeout")]
    return {"-100": ChannelScan(-100, "Grp", 42, files, {7})}


# -- the safety-critical guarantee -------------------------------------


def test_bundle_never_contains_the_session():
    """No serialised bundle may carry anything session-shaped."""
    bundle = export_bundle(sample_cfg(), sample_scans(), include_secrets=True)
    blob = json.dumps(bundle).lower()
    assert "session" not in blob
    assert ".session" not in json.dumps(bundle)


def test_download_dir_is_not_exported():
    bundle = export_bundle(sample_cfg(), sample_scans(), include_secrets=True)
    assert "download_dir" not in bundle["config"]
    assert "/Users/alice" not in json.dumps(bundle)


# -- secrets are opt-in ------------------------------------------------


def test_secrets_omitted_by_default():
    bundle = export_bundle(sample_cfg(), sample_scans(), include_secrets=False)
    assert "credentials" not in bundle
    assert "deadbeef" not in json.dumps(bundle)


def test_secrets_included_on_request():
    bundle = export_bundle(sample_cfg(), sample_scans(), include_secrets=True)
    assert bundle["credentials"] == {"api_id": "12345", "api_hash": "deadbeef"}


# -- round-trip --------------------------------------------------------


def test_round_trip_preserves_config_channels_and_scans(tmp_path):
    p = tmp_path / "bundle.json"
    written = write_bundle(export_bundle(sample_cfg(), sample_scans(), include_secrets=True), p)
    if sys.platform != "win32":
        assert oct(written.stat().st_mode)[-3:] == "600"

    imported = import_bundle(read_bundle(p))
    assert imported.config["extensions"] == ".zip, .csv"
    assert imported.config["download_connections"] == "8"
    assert imported.config["favourites"] == [{"id": -7, "title": "Fav"}]
    assert "download_dir" not in imported.config
    assert imported.credentials == {"api_id": "12345", "api_hash": "deadbeef"}
    assert imported.channels == [{"id": -100, "title": "Grp"}, {"id": "@x", "title": ""}]

    scan = imported.scans["-100"]
    assert scan.last_msg_id == 42
    assert scan.discarded == {7}
    assert [x.msg_id for x in scan.files] == [42, 43]
    assert scan.files[0].selected is False  # ticks survive
    assert scan.files[1].set_aside == "timeout"  # set-aside label survives


# -- validation --------------------------------------------------------


def test_rejects_foreign_file():
    with pytest.raises(ValueError):
        import_bundle({"app": "not-us", "version": 1})


def test_rejects_unknown_version():
    with pytest.raises(ValueError):
        import_bundle({"app": "tg-downloader", "version": 999})


def test_skips_a_bad_scan_entry_without_failing():
    data = export_bundle(sample_cfg(), sample_scans(), include_secrets=False)
    data["scans"]["channels"]["-100"] = "not a record"
    assert import_bundle(data).scans == {}  # the one bad entry is skipped


# -- merges are additive / non-destructive -----------------------------


def test_merge_channels_unions_by_id():
    existing = [{"id": -100, "title": "Local"}]
    incoming = [{"id": -100, "title": "Other"}, {"id": -200, "title": "New"}]
    assert merge_channels(existing, incoming) == [
        {"id": -100, "title": "Local"},  # local title kept
        {"id": -200, "title": "New"},
    ]


def test_merge_scans_adopts_new_channel_and_preserves_local_ticks():
    local = {"-100": ChannelScan(-100, "Grp", 42, [f(42, selected=True)], set())}
    incoming = {
        "-100": ChannelScan(-100, "Grp", 50, [f(42, selected=False), f(50)], set()),
        "-200": ChannelScan(-200, "New", 5, [f(5, chat=-200)], set()),
    }
    merged = merge_imported_scans(local, incoming)
    # new channel adopted wholesale
    assert merged["-200"].files[0].msg_id == 5
    # existing channel: new file appended, local tick wins on the overlap
    by_id = {x.msg_id: x for x in merged["-100"].files}
    assert set(by_id) == {42, 50}
    assert by_id[42].selected is True
