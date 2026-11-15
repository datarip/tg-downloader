"""Config persistence. The file holds an api_hash, so permissions matter."""

import json
import os
import sys

import pytest

import tg_downloader.config as config
from tg_downloader.config import (
    DEFAULT_CONFIG,
    ENV_VARS,
    _xdg_download_dir,
    default_download_dir,
    load_config,
    save_config,
)


def test_load_returns_defaults_when_absent(tmp_path):
    cfg = load_config(tmp_path / "nope.json")
    assert cfg["extensions"] == DEFAULT_CONFIG["extensions"]
    assert cfg["channels"] == []


def test_round_trip(tmp_path):
    p = tmp_path / "config.json"
    cfg = load_config(p)
    cfg["api_id"] = "999"
    cfg["channels"] = [{"id": -1001234567890, "title": "My Group"}]
    save_config(cfg, p)

    again = load_config(p)
    assert again["api_id"] == "999"
    assert again["channels"][0]["id"] == -1001234567890
    assert again["channels"][0]["title"] == "My Group"


def test_saved_file_is_owner_only(tmp_path):
    p = tmp_path / "config.json"
    save_config(load_config(p), p)
    if sys.platform != "win32":
        assert oct(p.stat().st_mode)[-3:] == "600"


def test_partial_config_is_filled_from_defaults(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"api_id": "1"}))
    cfg = load_config(p)
    assert cfg["api_id"] == "1"
    assert cfg["download_dir"] == DEFAULT_CONFIG["download_dir"]


def test_corrupt_config_falls_back_instead_of_crashing(tmp_path):
    """A truncated write shouldn't brick the app on next launch."""
    p = tmp_path / "config.json"
    p.write_text("{not valid json")
    cfg = load_config(p)
    assert cfg["extensions"] == DEFAULT_CONFIG["extensions"]


def test_default_channels_list_is_not_shared(tmp_path):
    """Guards against the classic mutable-default aliasing bug."""
    a = load_config(tmp_path / "a.json")
    b = load_config(tmp_path / "b.json")
    a["channels"].append({"id": 1, "title": "x"})
    assert b["channels"] == []
    assert DEFAULT_CONFIG["channels"] == []


def test_favourites_round_trip_and_default(tmp_path):
    p = tmp_path / "config.json"
    assert load_config(p)["favourites"] == []
    cfg = load_config(p)
    cfg["favourites"] = [{"id": -100, "title": "Starred"}]
    save_config(cfg, p)
    assert load_config(p)["favourites"] == [{"id": -100, "title": "Starred"}]


def test_default_favourites_list_is_not_shared(tmp_path):
    a = load_config(tmp_path / "a.json")
    b = load_config(tmp_path / "b.json")
    a["favourites"].append({"id": 1, "title": "x"})
    assert b["favourites"] == []
    assert DEFAULT_CONFIG["favourites"] == []


def test_favourites_persist_in_config_json_not_env(tmp_path):
    """Favourites are app state, not a credential — they stay in config.json."""
    p = tmp_path / "config.json"
    cfg = load_config(p)
    cfg["favourites"] = [{"id": -5, "title": "F"}]
    save_config(cfg, p)
    assert "favourites" in json.loads(p.read_text())


def test_filters_round_trip_and_default(tmp_path):
    p = tmp_path / "config.json"
    assert load_config(p)["filters"] == {}
    cfg = load_config(p)
    cfg["filters"] = {"name_text": "report", "status_exclude": ["completed"]}
    save_config(cfg, p)
    assert load_config(p)["filters"] == {
        "name_text": "report",
        "status_exclude": ["completed"],
    }


def test_default_filters_dict_is_not_shared(tmp_path):
    a = load_config(tmp_path / "a.json")
    b = load_config(tmp_path / "b.json")
    a["filters"]["name_text"] = "x"
    assert b["filters"] == {}
    assert DEFAULT_CONFIG["filters"] == {}


def test_filters_persist_in_config_json_not_env(tmp_path):
    """Column filters are app state, not a credential — they stay in config.json."""
    p = tmp_path / "config.json"
    cfg = load_config(p)
    cfg["filters"] = {"chan_text": "grp"}
    save_config(cfg, p)
    assert "filters" in json.loads(p.read_text())


def test_download_concurrency_is_plain_config_not_env():
    """It's plain app state, so App._locked() must tolerate a key absent from
    ENV_VARS — otherwise on_save (run before every scan) raises KeyError."""
    assert "download_concurrency" in DEFAULT_CONFIG
    assert "download_concurrency" not in ENV_VARS


def test_concurrency_round_trips(tmp_path):
    p = tmp_path / "config.json"
    cfg = load_config(p)
    cfg["download_concurrency"] = "6"
    save_config(cfg, p)
    assert load_config(p)["download_concurrency"] == "6"


# --- default download folder: cross-platform ---------------------------------


def _clear_xdg_env(monkeypatch):
    monkeypatch.delenv("XDG_DOWNLOAD_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


def test_default_download_dir_has_TG_subfolder():
    assert default_download_dir().endswith(
        os.path.join("Downloads", "TG")
    ) or default_download_dir().endswith("TG")


def test_mac_windows_use_home_downloads(monkeypatch):
    monkeypatch.setattr(config.sys, "platform", "darwin")
    assert default_download_dir() == os.path.join(os.path.expanduser("~"), "Downloads", "TG")
    monkeypatch.setattr(config.sys, "platform", "win32")
    assert default_download_dir() == os.path.join(os.path.expanduser("~"), "Downloads", "TG")


@pytest.mark.skipif(
    sys.platform == "win32", reason="XDG paths are Unix-only; Windows uses ~/Downloads"
)
def test_bsd_uses_xdg_not_bare_downloads(monkeypatch, tmp_path):
    """FreeBSD (sys.platform 'freebsd14') is Unix-like, not Windows/macOS, so it
    must take the XDG path and honour a localised user-dirs.dirs, same as Linux."""
    _clear_xdg_env(monkeypatch)
    monkeypatch.setattr(config.sys, "platform", "freebsd14")
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg_dir = tmp_path / ".config"
    cfg_dir.mkdir()
    (cfg_dir / "user-dirs.dirs").write_text('XDG_DOWNLOAD_DIR="$HOME/Téléchargements"\n')
    assert default_download_dir() == str(tmp_path / "Téléchargements" / "TG")


def test_xdg_honours_env_var_first(monkeypatch, tmp_path):
    _clear_xdg_env(monkeypatch)
    target = tmp_path / "MyDownloads"
    monkeypatch.setenv("XDG_DOWNLOAD_DIR", str(target))
    assert _xdg_download_dir() == str(target)


@pytest.mark.skipif(
    sys.platform == "win32", reason="XDG paths are Unix-only; Windows uses ~/Downloads"
)
def test_xdg_reads_localised_folder_from_user_dirs(monkeypatch, tmp_path):
    """A French desktop records ~/Téléchargements in user-dirs.dirs; we honour it."""
    _clear_xdg_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg_dir = tmp_path / ".config"
    cfg_dir.mkdir()
    (cfg_dir / "user-dirs.dirs").write_text(
        '# generated\nXDG_DESKTOP_DIR="$HOME/Bureau"\n'
        'XDG_DOWNLOAD_DIR="$HOME/Téléchargements"\n'
    )
    assert _xdg_download_dir() == str(tmp_path / "Téléchargements")


@pytest.mark.skipif(
    sys.platform == "win32", reason="XDG paths are Unix-only; Windows uses ~/Downloads"
)
def test_xdg_falls_back_to_home_downloads(monkeypatch, tmp_path):
    """No env var and no user-dirs.dirs → the universal ~/Downloads."""
    _clear_xdg_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-config"))
    assert _xdg_download_dir() == str(tmp_path / "Downloads")
