"""Core data types.

Telethon-free, so target-path logic can be tested without a session.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .filters import safe_name, subfolder_for

#: The "<msg_id>_" prefix our stored files carry, stripped to compare bare names.
_MSGID_PREFIX = re.compile(r"^\d+_")


@dataclass
class FileItem:
    """One downloadable file discovered in a chat."""

    chat_id: int | str
    chat_title: str
    msg_id: int
    name: str
    size: int
    ext: str
    selected: bool = True
    status: str = ""
    progress: int = -1  # 0–100 while downloading; -1 = no bar. Not persisted.
    speed: float = 0.0  # bytes/s while downloading; ephemeral, not persisted.
    downloaded: bool = False  # we completed this once — persisted, drives "moved"
    set_aside: str = ""  # failure label if auto-skipped on error; persisted, drives reload
    _extra: dict = field(default_factory=dict, repr=False)

    def target_path(self, download_dir: str | Path) -> Path:
        """Where this file lands on disk.

        Layout: ``<download_dir>/<Chat Title>/<archives|data>/<msg_id>_<name>``

        The message id prefix keeps identically-named files apart and makes
        re-runs idempotent: if the path exists, it's already downloaded.
        """
        return (
            Path(download_dir)
            / safe_name(self.chat_title)
            / subfolder_for(self.ext)
            / f"{self.msg_id}_{safe_name(self.name)}"
        )

    def part_path(self, download_dir: str | Path) -> Path:
        """Temp path used while downloading; renamed onto target on success.

        Plain string concat rather than ``with_suffix`` so compound extensions
        like ``.tar.gz`` survive intact.
        """
        return Path(str(self.target_path(download_dir)) + ".part")

    def existing_duplicate(self, download_dir: str | Path) -> Path | None:
        """A file already in this item's folder with the same name and size.

        Safety net against re-fetching (and overwriting) a file that's already on
        disk under a different message-id prefix, or one dropped in by hand. The
        exact target path is ignored (that's the "already completed" case). Only
        the item's own target subfolder is scanned, and a size ``stat`` is taken
        only for entries whose (prefix-stripped) name matches — so it's cheap.
        """
        target = self.target_path(download_dir)
        folder = target.parent
        if not folder.is_dir():
            return None
        wanted = safe_name(self.name)
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return None
        for entry in entries:
            if entry.path == str(target):
                continue
            if _MSGID_PREFIX.sub("", entry.name) != wanted:
                continue
            try:
                if entry.is_file() and entry.stat().st_size == self.size:
                    return Path(entry.path)
            except OSError:
                continue
        return None

    #: Fields carried across a save/load. ``status``/``progress`` are re-derived
    #: from disk on load and ``_extra`` is ephemeral, so neither is persisted;
    #: ``selected`` (tick state), ``downloaded`` (did we ever finish it — the
    #: signal for a later "moved" once the file leaves the folder) and
    #: ``set_aside`` (the failure label if it was auto-skipped on error, so it
    #: reloads as "skipped" with the reason rather than re-deriving to
    #: "incomplete") are.
    _RECORD_FIELDS = (
        "chat_id",
        "chat_title",
        "msg_id",
        "name",
        "size",
        "ext",
        "selected",
        "downloaded",
        "set_aside",
    )

    def as_record(self) -> dict:
        """A JSON-safe dict of the fields worth persisting.

        >>> FileItem(-100, "Grp", 7, "a.csv", 12, ".csv").as_record()["msg_id"]
        7
        """
        return {k: getattr(self, k) for k in self._RECORD_FIELDS}

    @classmethod
    def from_record(cls, rec: dict) -> FileItem:
        """Rebuild from :meth:`as_record`, tolerating missing/extra keys.

        >>> FileItem.from_record({"msg_id": 7, "name": "a.csv"}).name
        'a.csv'
        """
        known = {k: rec[k] for k in cls._RECORD_FIELDS if k in rec}
        return cls(
            chat_id=known.get("chat_id", 0),
            chat_title=known.get("chat_title", ""),
            msg_id=known.get("msg_id", 0),
            name=known.get("name", ""),
            size=known.get("size", 0),
            ext=known.get("ext", ""),
            selected=known.get("selected", True),
            downloaded=known.get("downloaded", False),
            set_aside=known.get("set_aside", ""),
        )


def download_dir_problem(download_dir: str | Path) -> str:
    """Why the download folder can't be written to right now, or ``""`` if it can.

    A pre-flight for the whole run, because a folder that has gone away is a
    **systemic** fault, not one file's fault. The motivating case is an external
    drive: unmount ``/Volumes/Disk`` and every target path under it fails at
    once, one file after another — without this check the batch burns through
    the entire queue turning each file into an error and setting it aside, which
    quietly unticks work the user spent a long time choosing.

    A missing folder is *created* (so a first run into ``~/Downloads/tg`` still
    works), and only a failure to create it is reported — on macOS that's
    exactly the unmounted-volume case, since ``/Volumes`` is root-owned and
    ``mkdir`` there raises ``PermissionError``. Creating it is also the outcome
    we want to avoid silently: a writable ``/Volumes/Disk`` would put the
    download on the boot disk, to vanish when the real drive mounts over it.

    Writability is probed by actually creating and removing a file rather than
    with ``os.access``, which reports the permission bits and not what the
    filesystem (a read-only mount, a full disk) will really allow.
    """
    path = Path(download_dir)
    if not str(download_dir).strip():
        return "No download folder is set — pick one in Settings."
    try:
        path.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        return f"The download folder is not a folder: {path}"
    except OSError as exc:
        return f"Can't create the download folder {path} — {exc.strerror or exc}."
    if not path.is_dir():
        return f"The download folder is not a folder: {path}"
    probe = path / ".tg_downloader_write_test"
    try:
        probe.touch()
        probe.unlink()
    except OSError as exc:
        return f"The download folder {path} isn't writable — {exc.strerror or exc}."
    return ""


def split_resume_first(
    items: Iterable[tuple[str, FileItem]], download_dir: str | Path
) -> tuple[list[str], list[str]]:
    """Partition ``(iid, item)`` pairs into ``(resume, fresh)`` iid lists.

    A file with a ``.part`` on disk is a resume; everything else is fresh. This
    includes a **zero-byte** ``.part`` — a download that was interrupted before
    its first chunk landed. Such a file is what ``_load_items_from_scans`` shows
    as ``incomplete`` at 0%, so it must resume like any other partial: routing it
    to ``fresh`` instead would drop it behind every untouched file in the
    RESUME-before-FRESH ordering and, under sustained rate-limiting, starve it.
    The resume list is ordered **oldest-first** — by the ``.part``'s mtime, i.e.
    how long it's been since anything was last written to it — so the
    longest-stalled downloads finish before newer ones and a backlog of partial
    files drains instead of growing. Fresh files keep their input order.

    Only a single ``stat`` per file is taken; an unreadable/absent ``.part``
    (``OSError``) counts as fresh.
    """
    resume: list[tuple[float, str]] = []
    fresh: list[str] = []
    for iid, item in items:
        try:
            st = item.part_path(download_dir).stat()
        except OSError:
            st = None
        if st is not None:
            resume.append((st.st_mtime, iid))
        else:
            fresh.append(iid)
    resume.sort(key=lambda pair: pair[0])  # oldest mtime first
    return [iid for _, iid in resume], fresh


def duplicate_key(item: FileItem) -> tuple[str, int]:
    """What makes two uploads "the same file" for de-duplication.

    Cross-posting a file between channels keeps its name and size but gives it a
    new message id in each chat, so name (case-folded) plus size is the signal —
    not ``msg_id``, which is exactly what differs between the copies.
    """
    return (item.name.strip().lower(), item.size)


def dedupe(items: Iterable[FileItem]) -> tuple[list[FileItem], list[FileItem]]:
    """Split ``items`` into ``(kept, discarded)``, keeping the first of each key.

    Iteration order is preserved and the first occurrence of each
    :func:`duplicate_key` wins, so passing items in scan order keeps the copy
    from the channel you listed first.

    >>> a = FileItem(1, "Alpha", 10, "data.csv", 500, ".csv")
    >>> b = FileItem(2, "Beta", 20, "DATA.CSV", 500, ".csv")   # repost, other chat
    >>> c = FileItem(1, "Alpha", 11, "data.csv", 501, ".csv")  # same name, 1 byte bigger
    >>> kept, dropped = dedupe([a, b, c])
    >>> [i.msg_id for i in kept], [i.msg_id for i in dropped]
    ([10, 11], [20])
    """
    seen: set[tuple[str, int]] = set()
    kept: list[FileItem] = []
    discarded: list[FileItem] = []
    for it in items:
        key = duplicate_key(it)
        if key in seen:
            discarded.append(it)
        else:
            seen.add(key)
            kept.append(it)
    return kept, discarded
