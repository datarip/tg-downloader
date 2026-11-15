"""The .env layer: it must feed the config, and never leak back into config.json."""

import json
import os
import sys

from tg_downloader.config import (
    DEFAULT_CONFIG,
    ENV_FILE_VAR,
    ENV_STORED,
    apply_to_environ,
    env_file_path,
    env_overrides,
    env_target_path,
    load_config,
    save_config,
    to_env_values,
    write_env_file,
)

# Isolation from the real ~/.tg_downloader/.env comes from conftest.py's
# `isolate_env`, which clears TG_* *and* redirects TG_ENV_FILE to a path that
# doesn't exist. Do NOT re-add a local fixture that deletes TG_ENV_FILE — that
# undoes the redirect and lets these tests read (and leak) real credentials.


def write_env(tmp_path, text, monkeypatch):
    p = tmp_path / ".env"
    p.write_text(text)
    monkeypatch.setenv(ENV_FILE_VAR, str(p))
    return p


def test_env_file_supplies_credentials(tmp_path, monkeypatch):
    write_env(tmp_path, "TG_API_ID=42\nTG_API_HASH=deadbeef\n", monkeypatch)
    cfg = load_config(tmp_path / "config.json")
    assert cfg["api_id"] == "42"
    assert cfg["api_hash"] == "deadbeef"


def test_env_file_supplies_channels(tmp_path, monkeypatch):
    write_env(tmp_path, "TG_CHANNELS=-100123=Dumps, @other\n", monkeypatch)
    cfg = load_config(tmp_path / "config.json")
    assert cfg["channels"] == [
        {"id": -100123, "title": "Dumps"},
        {"id": "@other", "title": ""},
    ]


def test_env_beats_config_file(tmp_path, monkeypatch):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"api_id": "from-json", "scan_limit": "10"}))
    write_env(tmp_path, "TG_API_ID=from-env\n", monkeypatch)
    cfg = load_config(p)
    assert cfg["api_id"] == "from-env"
    assert cfg["scan_limit"] == "10"  # untouched keys still come from json


def test_process_env_beats_env_file(tmp_path, monkeypatch):
    write_env(tmp_path, "TG_SCAN_LIMIT=100\n", monkeypatch)
    monkeypatch.setenv("TG_SCAN_LIMIT", "5")
    assert env_overrides()["scan_limit"] == "5"


def test_secrets_are_never_written_into_config_json(tmp_path, monkeypatch):
    """The whole point: a hash kept in .env must not get copied to disk elsewhere."""
    write_env(tmp_path, "TG_API_HASH=deadbeef\nTG_CHANNELS=-100123\n", monkeypatch)
    p = tmp_path / "config.json"
    cfg = load_config(p)
    cfg["download_dir"] = "/tmp/out"
    save_config(cfg, p)

    written = json.loads(p.read_text())
    assert "api_hash" not in written
    assert "channels" not in written
    assert written["download_dir"] == "/tmp/out"
    assert "deadbeef" not in p.read_text()


def test_config_json_still_works_without_any_env(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"api_id": "1", "api_hash": "abc"}))
    cfg = load_config(p)
    assert (cfg["api_id"], cfg["api_hash"]) == ("1", "abc")
    save_config(cfg, p)
    assert json.loads(p.read_text())["api_hash"] == "abc"  # nothing stripped


def test_empty_env_value_means_unset(tmp_path, monkeypatch):
    write_env(tmp_path, "TG_EXTENSIONS=\n", monkeypatch)
    cfg = load_config(tmp_path / "config.json")
    assert cfg["extensions"] == DEFAULT_CONFIG["extensions"]


def test_download_dir_tilde_is_expanded(tmp_path, monkeypatch):
    write_env(tmp_path, "TG_DOWNLOAD_DIR=~/Downloads/TG\n", monkeypatch)
    assert not env_overrides()["download_dir"].startswith("~")


def test_missing_env_file_pointer_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_FILE_VAR, str(tmp_path / "nope.env"))
    assert env_file_path() is None
    assert env_overrides() == {}


# -- the write path: what the GUI does on Save -------------------------


def test_target_is_the_pointer_even_before_the_file_exists(tmp_path, monkeypatch):
    """Regression: the first save must not land in ~/.tg_downloader/.env instead.

    env_file_path() returns None for a file that doesn't exist yet — which is
    precisely the first-save case — so env_target_path() can't be built on it.
    """
    wanted = tmp_path / "brand-new.env"
    monkeypatch.setenv(ENV_FILE_VAR, str(wanted))
    assert env_file_path() is None
    assert env_target_path() == wanted

    write_env_file({"TG_API_ID": "1"}, env_target_path())
    assert wanted.exists()


def gui_save(cfg, env_path, config_path):
    """What App.persist() does, minus Tk."""
    values = to_env_values(cfg, ENV_STORED)
    write_env_file(values, env_path)
    apply_to_environ(values)
    save_config(cfg, config_path)


def test_gui_save_writes_env_and_reloads(tmp_path, monkeypatch):
    env, conf = tmp_path / ".env", tmp_path / "config.json"
    monkeypatch.setenv(ENV_FILE_VAR, str(env))

    cfg = load_config(conf)
    cfg["api_id"] = "42"
    cfg["api_hash"] = "deadbeef"
    cfg["channels"] = [{"id": -100123, "title": "Dumps"}]
    cfg["download_dir"] = "/tmp/out"
    gui_save(cfg, env, conf)

    assert "TG_API_HASH=deadbeef" in env.read_text()
    if sys.platform != "win32":
        assert oct(env.stat().st_mode)[-3:] == "600"
    assert "deadbeef" not in conf.read_text()  # never lands in config.json
    assert json.loads(conf.read_text())["download_dir"] == "/tmp/out"

    again = load_config(conf)
    assert again["api_hash"] == "deadbeef"
    assert again["channels"] == [{"id": -100123, "title": "Dumps"}]
    assert again["download_dir"] == "/tmp/out"


def test_gui_save_migrates_secrets_out_of_config_json(tmp_path, monkeypatch):
    """An existing install has the hash in config.json. First Save should move it."""
    env, conf = tmp_path / ".env", tmp_path / "config.json"
    conf.write_text(json.dumps({"api_id": "42", "api_hash": "legacy", "scan_limit": "9"}))
    monkeypatch.setenv(ENV_FILE_VAR, str(env))

    cfg = load_config(conf)
    assert cfg["api_hash"] == "legacy"  # still readable from the old place
    gui_save(cfg, env, conf)

    assert "legacy" not in conf.read_text()  # gone from config.json
    assert "TG_API_HASH=legacy" in env.read_text()  # now in .env
    assert load_config(conf)["scan_limit"] == "9"  # non-secrets stay put


def test_save_preserves_hand_written_lines(tmp_path, monkeypatch):
    env, conf = tmp_path / ".env", tmp_path / "config.json"
    env.write_text("# my creds\nTG_API_ID=1\nOTHER_APP_KEY=keepme\n")
    monkeypatch.setenv(ENV_FILE_VAR, str(env))

    cfg = load_config(conf)
    cfg["api_id"] = "2"
    gui_save(cfg, env, conf)

    text = env.read_text()
    assert "# my creds" in text
    assert "OTHER_APP_KEY=keepme" in text
    assert "TG_API_ID=2" in text
    assert "TG_API_ID=1" not in text


def test_clearing_a_field_removes_the_variable(tmp_path, monkeypatch):
    env, conf = tmp_path / ".env", tmp_path / "config.json"
    monkeypatch.setenv(ENV_FILE_VAR, str(env))
    gui_save({"api_id": "1", "api_hash": "x", "channels": []}, env, conf)
    gui_save({"api_id": "", "api_hash": "x", "channels": []}, env, conf)

    assert "TG_API_ID" not in env.read_text()
    assert "TG_API_ID" not in os.environ  # and unshadowed in-process
    assert load_config(conf)["api_id"] == ""


def test_saved_values_propagate_to_the_environment(tmp_path, monkeypatch):
    """Child processes (and our own next read) must see the new value, not the old."""
    env, conf = tmp_path / ".env", tmp_path / "config.json"
    monkeypatch.setenv(ENV_FILE_VAR, str(env))
    gui_save({"api_id": "1", "api_hash": "old", "channels": []}, env, conf)
    assert os.environ["TG_API_HASH"] == "old"

    gui_save({"api_id": "1", "api_hash": "new", "channels": []}, env, conf)
    assert os.environ["TG_API_HASH"] == "new"  # not shadowed by the stale export
    assert load_config(conf)["api_hash"] == "new"


def test_round_trip_of_awkward_channel_titles(tmp_path, monkeypatch):
    env, conf = tmp_path / ".env", tmp_path / "config.json"
    monkeypatch.setenv(ENV_FILE_VAR, str(env))
    channels = [{"id": -1, "title": "Papers, Drafts = 2024"}, {"id": "@x", "title": ""}]
    gui_save({"channels": channels}, env, conf)
    assert load_config(conf)["channels"] == channels
