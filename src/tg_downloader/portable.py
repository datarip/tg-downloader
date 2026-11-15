"""Portable backup bundle — move your setup to another machine.

Serialises the app's *portable* state into one JSON file so a second install can
import it and resume: the app settings, the scan-source channels, and the scan
cache (the download list, with ticks and set-aside markers). API credentials are
included only when the caller opts in.

**The login session is never part of a bundle.** ``tg_session.session`` is a live
bearer credential for the user's Telegram account; a backup file is the kind of
thing that ends up in Dropbox or an email, so it must not carry account access.
On the new machine you import the bundle and log in again — Telethon writes a
fresh session there. This module never reads or writes the session file; the
``test_bundle_never_contains_the_session`` guard keeps it that way.

The machine-specific ``download_dir`` is deliberately left out too: an absolute
path from one computer rarely exists on another, so the target install keeps its
own folder. Everything on disk is re-derived from that folder on import, so the
status of each file sorts itself out — nothing status-shaped needs to travel.

Telethon-free by the same rule as config/models/store: it only moves plain data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .store import SCHEMA_VERSION, ChannelScan, merge_scan

#: Tag every bundle carries, so import can reject an unrelated JSON file.
APP_TAG = "tg-downloader"
BUNDLE_VERSION = 1

#: Plain app-state config keys that travel. Note what's *absent*: ``download_dir``
#: (machine-specific absolute path) and the secrets (opt-in, handled separately).
EXPORTABLE_CONFIG: tuple[str, ...] = (
    "extensions",
    "scan_limit",
    "download_concurrency",
    "download_connections",
    "flood_sleep_threshold",
    "favourites",
    "filters",
    "view",
)

#: Credential keys, carried only when the caller passes ``include_secrets=True``.
SECRET_CONFIG: tuple[str, ...] = ("api_id", "api_hash")


@dataclass
class Imported:
    """The result of reading a bundle — plain data, ready to merge into a live app."""

    config: dict[str, Any] = field(default_factory=dict)  # exportable app-state keys
    channels: list[dict[str, Any]] = field(default_factory=list)
    credentials: dict[str, str] = field(default_factory=dict)  # {} unless the bundle had them
    scans: dict[str, ChannelScan] = field(default_factory=dict)

    def file_count(self) -> int:
        return sum(len(s.files) for s in self.scans.values())


def export_bundle(
    cfg: dict[str, Any],
    scans: dict[str, ChannelScan],
    *,
    include_secrets: bool,
    exported_at: str = "",
) -> dict[str, Any]:
    """Build a JSON-safe bundle dict from a live config and scan cache.

    ``exported_at`` is passed in (an ISO timestamp) rather than read here, so this
    stays pure and testable. The session file is never touched.

    >>> b = export_bundle({"extensions": ".zip", "channels": []}, {}, include_secrets=False)
    >>> b["app"], b["version"], "credentials" in b
    ('tg-downloader', 1, False)
    """
    config = {k: cfg[k] for k in EXPORTABLE_CONFIG if k in cfg}
    bundle: dict[str, Any] = {
        "app": APP_TAG,
        "version": BUNDLE_VERSION,
        "exported_at": exported_at,
        "include_secrets": bool(include_secrets),
        "config": config,
        "channels": [dict(c) for c in cfg.get("channels", [])],
        "scans": {
            "version": SCHEMA_VERSION,
            "channels": {cid: s.as_record() for cid, s in scans.items()},
        },
    }
    if include_secrets:
        bundle["credentials"] = {k: str(cfg.get(k, "")) for k in SECRET_CONFIG}
    return bundle


def import_bundle(data: Any) -> Imported:
    """Validate and unpack a bundle dict into plain, mergeable data.

    Raises :class:`ValueError` on anything that isn't a bundle this app can read.

    >>> import_bundle({"app": "something-else"})
    Traceback (most recent call last):
    ...
    ValueError: Not a tg-downloader export file.
    """
    if not isinstance(data, dict) or data.get("app") != APP_TAG:
        raise ValueError("Not a tg-downloader export file.")
    version = data.get("version")
    if version != BUNDLE_VERSION:
        raise ValueError(
            f"Unsupported export version {version!r}; this app reads version {BUNDLE_VERSION}."
        )

    raw_cfg = data.get("config") or {}
    config = {k: raw_cfg[k] for k in EXPORTABLE_CONFIG if k in raw_cfg}

    channels = [
        {"id": c.get("id"), "title": c.get("title", "")}
        for c in (data.get("channels") or [])
        if isinstance(c, dict) and c.get("id") not in (None, "")
    ]

    creds_raw = data.get("credentials") or {}
    credentials = {}
    for key in SECRET_CONFIG:
        value = str(creds_raw.get(key, "")).strip()
        if value:
            credentials[key] = value

    scans: dict[str, ChannelScan] = {}
    for cid, rec in ((data.get("scans") or {}).get("channels") or {}).items():
        try:
            scans[str(cid)] = ChannelScan.from_record(rec)
        except (TypeError, AttributeError, KeyError):
            continue  # skip one bad entry rather than failing the whole import

    return Imported(config=config, channels=channels, credentials=credentials, scans=scans)


def merge_channels(
    existing: list[dict[str, Any]], imported: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Union channels by id — additive, so an import never drops what's already set.

    >>> merge_channels([{"id": 1, "title": "A"}], [{"id": 1}, {"id": 2, "title": "B"}])
    [{'id': 1, 'title': 'A'}, {'id': 2, 'title': 'B'}]
    """
    out = list(existing)
    have = {str(c.get("id")) for c in out}
    for c in imported:
        if str(c.get("id")) not in have:
            out.append(c)
            have.add(str(c.get("id")))
    return out


def merge_imported_scans(
    existing: dict[str, ChannelScan], imported: dict[str, ChannelScan]
) -> dict[str, ChannelScan]:
    """Fold imported scans into the local cache, never clobbering local state.

    A channel new to this machine is adopted wholesale. One already present is
    merged with :func:`store.merge_scan` in update mode — bundle files new to the
    channel are appended, while local ticks and discard tombstones win (an import
    shouldn't undo choices made on this machine).
    """
    out = dict(existing)
    for cid, inc in imported.items():
        if cid not in out:
            out[cid] = inc
        else:
            out[cid] = merge_scan(out[cid], inc.files, watermark=inc.last_msg_id, full=False)
    return out


def write_bundle(bundle: dict[str, Any], path: str | Path) -> Path:
    """Write a bundle to disk, owner-only — it may carry channel ids and secrets."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
    path.chmod(0o600)
    return path


def read_bundle(path: str | Path) -> Any:
    """Load a bundle's JSON. Raises ``OSError``/``json.JSONDecodeError`` on bad input."""
    return json.loads(Path(path).read_text(encoding="utf-8"))
