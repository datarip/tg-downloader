"""Persisted scan results — the file list, per channel, across restarts.

Lives in ``~/.tg_downloader/scans.json`` next to ``config.json``. Holds, for each
channel: the matching files found, each file's ticked state, the ids the user has
discarded (so a rescan doesn't resurrect them), and a watermark — the newest
message id seen — that drives *update* scans (fetch only what's newer).

Telethon-free by design, like config/models/filters: it moves plain ``FileItem``
data to and from disk and never talks to Telegram.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .config import APP_DIR
from .models import FileItem

SCANS_PATH = APP_DIR / "scans.json"
SCHEMA_VERSION = 1


@dataclass
class ChannelScan:
    """One channel's cached scan."""

    chat_id: int | str
    title: str = ""
    last_msg_id: int = 0  # watermark for update scans
    files: list[FileItem] = field(default_factory=list)
    discarded: set[int] = field(default_factory=set)  # msg_ids to never re-add

    def as_record(self) -> dict:
        return {
            "chat_id": self.chat_id,
            "title": self.title,
            "last_msg_id": self.last_msg_id,
            "files": [f.as_record() for f in self.files],
            "discarded": sorted(self.discarded),
        }

    @classmethod
    def from_record(cls, rec: dict) -> ChannelScan:
        return cls(
            chat_id=rec.get("chat_id", 0),
            title=rec.get("title", ""),
            last_msg_id=rec.get("last_msg_id", 0),
            files=[FileItem.from_record(f) for f in rec.get("files", [])],
            discarded=set(rec.get("discarded", [])),
        )


def merge_scan(
    existing: ChannelScan | None,
    incoming: Iterable[FileItem],
    *,
    watermark: int = 0,
    full: bool,
) -> ChannelScan:
    """Fold a fresh batch of scanned files into a channel's cached scan.

    Two invariants hold whichever mode is used:

    - **Discards stick.** A msg_id in ``existing.discarded`` is dropped from the
      incoming batch, so neither a full nor an update scan brings it back.
    - **Ticks survive.** A file's ``selected`` state is carried over by msg_id —
      as is its ``set_aside`` failure label, so a file the app set aside on a
      past error stays skipped (with its reason) through a full re-scan instead
      of being rebuilt as a fresh, ticked item.

    ``full=True`` — ``incoming`` is the *whole* history: the file list is rebuilt
    from it, so files deleted from the channel fall away. ``full=False`` (update)
    — ``incoming`` is only messages newer than the watermark: existing files are
    kept and the new ones appended.

    >>> a = FileItem(-1, "G", 10, "a.zip", 5, ".zip", selected=False)
    >>> prev = ChannelScan(-1, "G", 10, [a], {99})
    >>> b = FileItem(-1, "G", 20, "b.zip", 6, ".zip")   # new
    >>> after = merge_scan(prev, [b], watermark=20, full=False)
    >>> [f.msg_id for f in after.files], after.last_msg_id
    ([10, 20], 20)
    >>> merge_scan(prev, [FileItem(-1, "G", 99, "x", 1, "")], full=True).files
    []
    """
    discarded = set(existing.discarded) if existing else set()
    prior_selected = {f.msg_id: f.selected for f in existing.files} if existing else {}
    prior_set_aside = {f.msg_id: f.set_aside for f in existing.files} if existing else {}
    title = existing.title if existing else ""
    chat_id = existing.chat_id if existing else 0

    fresh = [f for f in incoming if f.msg_id not in discarded]
    for f in fresh:
        if f.msg_id in prior_selected:  # remember whether it was ticked
            f.selected = prior_selected[f.msg_id]
        if f.msg_id in prior_set_aside:  # ...and whether we set it aside on a failure
            f.set_aside = prior_set_aside[f.msg_id]
    if fresh:
        chat_id = fresh[0].chat_id
        title = fresh[0].chat_title or title

    if full:
        files = fresh
    else:
        seen = {f.msg_id for f in existing.files} if existing else set()
        files = (list(existing.files) if existing else []) + [
            f for f in fresh if f.msg_id not in seen
        ]

    last = max([existing.last_msg_id if existing else 0, watermark] + [f.msg_id for f in files])
    return ChannelScan(
        chat_id=chat_id, title=title, last_msg_id=last, files=files, discarded=discarded
    )


def load_scans(path: Path | None = None) -> dict[str, ChannelScan]:
    """Load the cache, keyed by ``str(chat_id)``. Missing or corrupt → empty."""
    path = path or SCANS_PATH
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}  # a corrupt cache should never block startup
    channels = data.get("channels", {}) if isinstance(data, dict) else {}
    out: dict[str, ChannelScan] = {}
    for cid, rec in channels.items():
        try:
            out[str(cid)] = ChannelScan.from_record(rec)
        except (TypeError, AttributeError, KeyError):
            continue  # skip a single bad entry rather than losing the whole file
    return out


def save_scans(scans: dict[str, ChannelScan], path: Path | None = None) -> None:
    """Write the cache with owner-only permissions (channel ids are private)."""
    path = path or SCANS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": SCHEMA_VERSION,
        "channels": {cid: scan.as_record() for cid, scan in scans.items()},
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    path.chmod(0o600)
