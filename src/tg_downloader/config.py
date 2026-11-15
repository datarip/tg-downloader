"""Persisted configuration, with an environment-file layer on top.

Settings resolve in this order (first one wins):

1. the process environment  (``TG_API_HASH=… tg-download``)
2. an env file             (``./.env``, else ``~/.tg_downloader/.env``)
3. ``~/.tg_downloader/config.json``
4. the defaults below

Anything supplied by layers 1–2 is treated as *externally managed*: it is never
written back to ``config.json`` by :func:`save_config`. That's the whole point —
you can keep ``api_id``/``api_hash``/channel ids in a gitignored ``.env`` and
have the app read them without ever copying them into a second file on disk.

Telethon-free by design — see filters.py for the same rationale.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def _xdg_download_dir() -> str:
    """The user's Downloads folder on XDG systems, honouring the XDG user-dirs spec.

    Applies to every Unix-like desktop that follows freedesktop.org — Linux
    (Ubuntu, Fedora, Arch, Debian, …) *and* the BSDs (FreeBSD, …), all of which
    ship ``xdg-user-dirs``. macOS and Windows do not use this (their Downloads
    folder is always physically ``~/Downloads``, only the display name is
    localised), so they never reach here.

    Resolution order (first that resolves wins):

    1. ``$XDG_DOWNLOAD_DIR`` in the process environment (rarely exported, but
       authoritative when it is);
    2. the ``XDG_DOWNLOAD_DIR=...`` line in ``$XDG_CONFIG_HOME/user-dirs.dirs``
       (default ``~/.config/user-dirs.dirs``) — this is where the desktop
       actually records it, and where a *localised* name (e.g.
       ``~/Téléchargements``) or a relocated folder shows up;
    3. ``~/Downloads`` as the universal fallback.

    Values in ``user-dirs.dirs`` are shell-quoted and written relative to
    ``$HOME`` (``XDG_DOWNLOAD_DIR="$HOME/Downloads"``), so we strip the quotes
    and expand ``$HOME``/``~`` ourselves.
    """
    env = os.environ.get("XDG_DOWNLOAD_DIR", "").strip()
    if env:
        return os.path.expanduser(os.path.expandvars(env))

    config_home = os.environ.get("XDG_CONFIG_HOME", "").strip() or os.path.expanduser(
        "~/.config"
    )
    user_dirs = Path(config_home) / "user-dirs.dirs"
    try:
        for line in user_dirs.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() != "XDG_DOWNLOAD_DIR":
                continue
            value = value.strip().strip('"').strip("'")
            if value:
                return os.path.expanduser(os.path.expandvars(value))
    except OSError:
        pass  # no user-dirs.dirs (minimal/headless install) → fall through

    return os.path.expanduser("~/Downloads")


def default_download_dir() -> str:
    """The OS-appropriate default download folder, with this app's ``TG`` subfolder.

    macOS and Windows both keep downloads in ``~/Downloads`` (``~`` expands to the
    user profile on Windows too). Every other Unix-like — Linux *and* the BSDs —
    resolves the real, possibly localised, Downloads folder via the XDG
    user-dirs spec — see :func:`_xdg_download_dir`.

    >>> default_download_dir().endswith("TG")
    True
    """
    if sys.platform == "darwin" or sys.platform.startswith("win"):
        base = os.path.join(os.path.expanduser("~"), "Downloads")
    else:
        base = _xdg_download_dir()
    return os.path.join(base, "TG")


APP_DIR = Path(os.path.expanduser("~/.tg_downloader"))
CONFIG_PATH = APP_DIR / "config.json"
SESSION_PATH = APP_DIR / "tg_session"  # Telethon appends .session
ENV_PATH = APP_DIR / ".env"  # where the GUI writes, if no ./.env exists
LOG_PATH = APP_DIR / "tg_downloader.log"  # run log: download errors, causes, retries

#: What the GUI keeps in the env file rather than config.json: the secrets, plus
#: the channel ids (which say what you're subscribed to — also nobody's business).
ENV_STORED: tuple[str, ...] = ("api_id", "api_hash", "channels")

#: config key -> environment variable name.
ENV_VARS: dict[str, str] = {
    "api_id": "TG_API_ID",
    "api_hash": "TG_API_HASH",
    "download_dir": "TG_DOWNLOAD_DIR",
    "extensions": "TG_EXTENSIONS",
    "scan_limit": "TG_SCAN_LIMIT",
    "channels": "TG_CHANNELS",
}

#: Point this at an env file to override the search below.
ENV_FILE_VAR = "TG_ENV_FILE"

DEFAULT_CONFIG: dict[str, Any] = {
    "api_id": "",
    "api_hash": "",
    "download_dir": default_download_dir(),
    "extensions": (
        ".zip, .rar, .7z, .tar, .tar.gz, .tgz, .tar.bz2, .tar.xz, .csv, .json, .jsonl, .txt"
    ),
    # list of {"id": int | str, "title": str}
    "channels": [],
    # starred groups/channels — a browsing aid for the fetch dialog, not the
    # scan list. Same shape as channels; kept in config.json, not the .env.
    "favourites": [],
    # Files-tab column filters, remembered across launches. Plain app state
    # (config.json, not the .env); shape is set by App._snapshot_filters.
    "filters": {},
    # Files-tab column layout — drag-reordered column order and resized widths,
    # remembered across launches. Plain app state (config.json, not the .env);
    # shape is set by App._snapshot_view. Empty → the code-defined defaults.
    "view": {},
    # messages scanned per channel; "0" means the whole history
    "scan_limit": "0",
    # how many files to download at once (1 = the old sequential behaviour)
    "download_concurrency": "1",
    # connections per file: split one file across N simultaneous senders to the
    # media DC for faster single-file downloads (1 = the old one-connection path).
    "download_connections": "4",
    # seconds: flood/premium waits up to this are slept by Telethon itself
    # (silent, per-request); longer ones surface to our coordinator, which
    # pauses the whole batch, shows the cause, and retries. See errors.py.
    "flood_sleep_threshold": "20",
}


# -- env file ----------------------------------------------------------


def parse_env_file(text: str) -> dict[str, str]:
    """Parse ``KEY=value`` lines. Comments, blanks and ``export`` are tolerated.

    Quoted values keep whatever is inside the quotes; unquoted ones are stripped
    and lose any trailing ``# comment``.

    >>> parse_env_file('# creds\\nexport TG_API_ID=12345\\n')
    {'TG_API_ID': '12345'}
    >>> parse_env_file('TG_EXTENSIONS=".csv, .zip"   # what we want')
    {'TG_EXTENSIONS': '.csv, .zip'}
    >>> parse_env_file('BAD LINE\\nTG_API_HASH=abc')
    {'TG_API_HASH': 'abc'}
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        if not key:
            continue
        value = value.strip()
        if value[:1] in ('"', "'") and value.count(value[0]) >= 2:
            quote = value[0]
            value = value[1 : value.index(quote, 1)]  # trailing comment, if any, is dropped
        else:
            value = value.split(" #", 1)[0].strip()
        out[key] = value
    return out


def env_file_path() -> Path | None:
    """The env file we'd read: ``$TG_ENV_FILE``, else ``./.env``, else the one in APP_DIR."""
    override = os.environ.get(ENV_FILE_VAR)
    if override:
        p = Path(os.path.expanduser(override))
        return p if p.exists() else None
    for p in (Path.cwd() / ".env", APP_DIR / ".env"):
        if p.exists():
            return p
    return None


def env_target_path() -> Path:
    """Where a write goes: ``$TG_ENV_FILE``, else an existing ``./.env``, else ``ENV_PATH``.

    Not ``env_file_path() or ENV_PATH`` — that returns None for a ``$TG_ENV_FILE``
    pointing at a file that doesn't exist *yet*, which is exactly the first-save
    case, and the write would silently land in the home directory instead.
    """
    override = os.environ.get(ENV_FILE_VAR)
    if override:
        return Path(os.path.expanduser(override))
    return env_file_path() or ENV_PATH


def parse_channels(raw: str) -> list[dict[str, Any]]:
    """Parse ``TG_CHANNELS``: a JSON list, or comma-separated ``id`` / ``id=Title``.

    Ids that look numeric become ints, so they match what the GUI stores.

    >>> parse_channels("-1001234567890, @somegroup")
    [{'id': -1001234567890, 'title': ''}, {'id': '@somegroup', 'title': ''}]
    >>> parse_channels("-100123=Research Dumps")
    [{'id': -100123, 'title': 'Research Dumps'}]
    >>> parse_channels('[{"id": -100123, "title": "X"}]')
    [{'id': -100123, 'title': 'X'}]
    >>> parse_channels("  ")
    []
    """
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return [
            {"id": c.get("id"), "title": c.get("title", "")}
            for c in loaded
            if isinstance(c, dict) and c.get("id") not in (None, "")
        ]

    channels: list[dict[str, Any]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        ident, _, title = chunk.partition("=")
        ident = ident.strip()
        if not ident:
            continue
        try:
            value: int | str = int(ident)
        except ValueError:
            value = ident
        channels.append({"id": value, "title": title.strip()})
    return channels


def format_channels(channels: list[dict[str, Any]]) -> str:
    """Serialise channels for ``TG_CHANNELS`` — the inverse of :func:`parse_channels`.

    Falls back to JSON when a title contains a ``,`` or ``=``, which the compact
    form can't represent unambiguously.

    >>> format_channels([{"id": -100123, "title": "Dumps"}, {"id": "@x", "title": ""}])
    '-100123=Dumps, @x'
    >>> format_channels([{"id": 1, "title": "A, B"}])
    '[{"id": 1, "title": "A, B"}]'
    >>> format_channels([])
    ''
    """
    if any({",", "="} & set(str(c.get("title") or "")) for c in channels):
        return json.dumps(channels)
    parts = []
    for c in channels:
        title = str(c.get("title") or "").strip()
        parts.append(f"{c['id']}={title}" if title else str(c["id"]))
    return ", ".join(parts)


def to_env_values(cfg: dict[str, Any], keys: Iterable[str] | None = None) -> dict[str, str]:
    """Render config keys as ``TG_*`` strings, ready for an env file or ``os.environ``.

    >>> to_env_values({"api_id": "42", "channels": []}, ["api_id"])
    {'TG_API_ID': '42'}
    """
    out: dict[str, str] = {}
    for key in keys if keys is not None else ENV_VARS:
        if key not in ENV_VARS or key not in cfg:
            continue
        value = cfg[key]
        out[ENV_VARS[key]] = format_channels(value) if key == "channels" else str(value)
    return out


def write_env_file(values: dict[str, str], path: Path | None = None) -> Path:
    """Merge ``TG_*`` values into the env file, in place, 0600.

    Lines we don't manage — comments, blanks, other people's variables — are kept
    as they are, so hand-editing the file and editing it from the GUI don't fight.
    An empty value removes the variable rather than writing ``KEY=``, since
    :func:`env_overrides` reads a blank as "not set".
    """
    path = path or env_target_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)
    out: list[str] = []
    for line in lines:
        key = line.strip().removeprefix("export ").partition("=")[0].strip()
        if key not in remaining:
            out.append(line)
            continue
        value = remaining.pop(key)
        if value:
            out.append(f"{key}={_quote(value)}")
        # else: drop the line — the variable is being unset

    new = [f"{k}={_quote(v)}" for k, v in remaining.items() if v]
    if new and out and out[-1].strip():
        out.append("")
    out.extend(new)

    path.write_text("\n".join(out).strip("\n") + "\n", encoding="utf-8")
    path.chmod(0o600)  # holds the api_hash
    return path


def _quote(value: str) -> str:
    """Quote if the value would otherwise be misread on the way back in.

    >>> _quote(".csv, .zip"), _quote("secret # not a comment"), _quote("42")
    ('.csv, .zip', '"secret # not a comment"', '42')
    """
    if "#" in value or value != value.strip():
        return f'"{value}"'
    return value


def apply_to_environ(values: dict[str, str]) -> None:
    """Push values into ``os.environ`` so this process — and anything it spawns — sees them.

    Without this, a GUI save would only take effect on the next launch: the env
    layer is read from ``os.environ`` first, and a stale value there would keep
    shadowing the file we just wrote.
    """
    for key, value in values.items():
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)


def env_overrides() -> dict[str, Any]:
    """Settings coming from the process environment or the env file.

    The process environment wins over the file, so a one-off
    ``TG_SCAN_LIMIT=100 tg-download`` beats what's in ``.env``.
    """
    layer: dict[str, str] = {}
    path = env_file_path()
    if path:
        try:
            layer.update(parse_env_file(path.read_text(encoding="utf-8")))
        except OSError:
            pass  # unreadable .env shouldn't stop the app from starting
    layer.update({k: v for k, v in os.environ.items() if k in ENV_VARS.values()})

    out: dict[str, Any] = {}
    for key, var in ENV_VARS.items():
        raw = layer.get(var, "").strip()
        if not raw:
            continue  # an empty value means "not set", not "set to empty"
        if key == "channels":
            out[key] = parse_channels(raw)
        elif key == "download_dir":
            out[key] = os.path.expanduser(raw)  # "~/Downloads/TG" is a natural thing to write
        else:
            out[key] = raw
    return out


# -- config file -------------------------------------------------------


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load defaults, then config.json, then the env layer on top."""
    path = path or CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = dict(DEFAULT_CONFIG)
    cfg["channels"] = []  # don't share the default list objects
    cfg["favourites"] = []
    cfg["filters"] = {}
    cfg["view"] = {}
    if path.exists():
        try:
            cfg.update(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            # A corrupt config shouldn't stop the app from starting.
            pass
    cfg.update(env_overrides())
    return cfg


def save_config(cfg: dict[str, Any], path: Path | None = None) -> None:
    """Write config to disk with owner-only permissions.

    Keys supplied by the environment are dropped before writing: a secret kept
    in ``.env`` must not get copied into ``config.json`` behind the user's back.
    """
    path = path or CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    managed = env_overrides()
    persisted = {k: v for k, v in cfg.items() if k not in managed}
    path.write_text(json.dumps(persisted, indent=2), encoding="utf-8")
    path.chmod(0o600)  # may still contain an api_hash typed into the GUI
