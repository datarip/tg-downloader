"""File-name filtering and formatting helpers.

Deliberately free of any Telethon import so the logic here stays unit-testable
without network access or a Telegram session.
"""

from __future__ import annotations

import re

#: Extensions routed to the ``archives/`` subfolder. Everything else that
#: matches the allow-list goes to ``data/``.
ARCHIVE_EXTS: frozenset[str] = frozenset(
    {
        ".zip",
        ".rar",
        ".7z",
        ".tar",
        ".tar.gz",
        ".tgz",
        ".tar.bz2",
        ".tar.xz",
    }
)


def parse_extensions(text: str) -> set[str]:
    """Parse a user-entered extension list into a normalised set.

    Accepts commas and/or whitespace, tolerates a missing leading dot, and
    lowercases everything.

    >>> sorted(parse_extensions("zip, .CSV  json"))
    ['.csv', '.json', '.zip']
    """
    out: set[str] = set()
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        if not part.startswith("."):
            part = "." + part
        out.add(part.lower())
    return out


def matched_extension(filename: str, allowed: set[str]) -> str | None:
    """Return the allowed extension ``filename`` ends with, else ``None``.

    Matching is done against the *end* of the name rather than by splitting on
    the last dot, so compound extensions work correctly:

    >>> allowed = {".tar.gz", ".gz", ".json", ".jsonl"}
    >>> matched_extension("logs.tar.gz", allowed)
    '.tar.gz'
    >>> matched_extension("events.jsonl", allowed)
    '.jsonl'
    """
    lowered = filename.lower()
    # Longest first so '.tar.gz' wins over a bare '.gz'.
    for ext in sorted(allowed, key=len, reverse=True):
        if lowered.endswith(ext):
            return ext
    return None


def subfolder_for(ext: str) -> str:
    """Which subfolder a given extension belongs in."""
    return "archives" if ext in ARCHIVE_EXTS else "data"


def human_size(n: float | None) -> str:
    """Format a byte count for display.

    Two decimals above bytes, so a 1.8 TB total and a 1.84 TB one don't
    read as the same number.

    >>> human_size(2048)
    '2.00 KB'
    """
    n = n or 0
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def human_duration(seconds: float) -> str:
    """Format a rough duration, coarsest two units.

    >>> human_duration(45)
    '45s'
    >>> human_duration(90)
    '1m 30s'
    >>> human_duration(3661)
    '1h 1m'
    >>> human_duration(90000)
    '1d 1h'
    """
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m"
    d, h = divmod(h, 24)
    return f"{d}d {h}h"


#: Names Windows refuses as a file or folder stem, even with an extension.
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def safe_name(name: str) -> str:
    """Make an arbitrary chat title or filename safe to use on every OS.

    Strips path separators and other troublesome characters, which also
    neutralises traversal attempts coming from a chat title, and neutralises
    the two Windows-specific traps: reserved device names and names ending in
    a dot or space.

    >>> safe_name("../../etc")
    '.._.._etc'
    >>> safe_name("")
    'chat'
    >>> safe_name("CON")
    '_CON'
    >>> safe_name("aux.csv")
    '_aux.csv'
    >>> safe_name("report. ")
    'report'
    """
    cleaned = re.sub(r"[^\w\-. ]+", "_", name).strip().rstrip(". ") or "chat"
    if cleaned.split(".")[0].upper() in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned
