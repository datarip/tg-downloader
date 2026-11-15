"""Telegram error classification and run logging.

Telethon-free by design (see filters.py for the rationale): this module turns an
arbitrary exception — a Telethon ``FloodWaitError``, a dropped socket, a plain
``TimeoutError`` — into a small, testable :class:`ErrorCause` describing *why* a
download failed, without importing Telethon. Both the GUI and the CLI use it to

* show the cause in the Files-tab status column and the bottom status bar, and
* write a log file of the run (:func:`configure_logging`) so failures can be
  diagnosed after the fact.

Classification works off the exception's type name, its message, and duck-typed
attributes (``.code`` on Telethon's ``RPCError``, ``.seconds`` on
``FloodWaitError``) rather than ``isinstance`` against Telethon classes — that's
what keeps it importable, and unit-testable, without a session.
"""

from __future__ import annotations

import errno as _errno
import logging
import re
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import LOG_PATH

#: One logger for the whole app. A NullHandler keeps it silent until
#: configure_logging() attaches the file handler, and ``propagate = False`` stops
#: stray warnings (e.g. from download retries under test) reaching the root
#: logger's default stderr handler.
log = logging.getLogger("tg_downloader")
log.addHandler(logging.NullHandler())
log.propagate = False


@dataclass(frozen=True)
class ErrorCause:
    """Why a download failed, in a form both the UI and the log can use.

    ``kind`` is a stable machine tag; ``label`` is the short text for the Files
    status column; ``detail`` is the fuller sentence for the status bar / log.
    ``retry_after`` is the wait Telegram asked for, when it's a rate limit.
    """

    # kind ∈ rate_limit | not_found | no_storage | no_connection | timeout
    #      | protected | unknown
    kind: str
    label: str  # short, for the status column
    detail: str  # fuller, for the status bar and log
    retry_after: float | None = None


class RateLimited(Exception):
    """Raised out of :func:`tg_downloader.telegram.download_item` on a flood wait.

    Rate limits are handled *globally* by the caller (pause every worker, wait,
    then re-queue), not with a per-file backoff — so ``download_item`` surfaces
    them straight away instead of spinning on its own retry loop.
    """

    def __init__(
        self, retry_after: float | None, detail: str = "", label: str = "rate limited"
    ) -> None:
        self.retry_after = retry_after
        self.label = label  # short text for the status column (distinguishes premium caps)
        super().__init__(detail or f"rate limited (retry after {retry_after or '?'}s)")


class ConnectionLost(Exception):
    """Raised out of :func:`tg_downloader.telegram.download_item` when the link is down.

    Like :class:`RateLimited`, a lost connection is handled *globally* by the
    caller — pause every worker until connectivity returns, then resume the
    *same* file (its ``.part`` is kept) — not with a per-file backoff. Surfacing
    it immediately (instead of spinning the local retry loop offline) is what lets
    a whole-batch outage stall in place rather than churning every queued file
    into a "no connection" error and advancing.
    """

    def __init__(self, detail: str = "", label: str = "no connection") -> None:
        self.label = label  # short text for the status column
        super().__init__(detail or "no connection")


def connection_state(internet: bool, telegram: bool) -> tuple[bool, str]:
    """Turn two reachability probes into (online, detail) for the connection light.

    ``online`` is True only when Telegram itself is reachable. ``detail`` is a
    short reason shown while offline — distinguishing a dead internet link from
    Telegram *specifically* being unreachable (internet up, DC not) — and ``""``
    when online. Pure so it's unit-testable; the actual TCP probes live in
    ``telegram.py``.

    >>> connection_state(internet=True, telegram=True)
    (True, '')
    >>> connection_state(internet=True, telegram=False)
    (False, 'Internet OK, but Telegram is unreachable — retrying')
    >>> connection_state(internet=False, telegram=False)
    (False, 'No internet connection — retrying')
    >>> connection_state(internet=False, telegram=True)
    (True, '')
    """
    if telegram:
        return True, ""
    if internet:
        return False, "Internet OK, but Telegram is unreachable — retrying"
    return False, "No internet connection — retrying"


_WAIT_SECONDS = re.compile(r"(\d+)\s*second", re.IGNORECASE)

#: Local-filesystem errnos that mean "the download folder is unusable" — an
#: unmounted or read-only volume, a revoked permission, a full disk. Every one
#: of them fails *every* file in the batch identically, so :func:`classify`
#: tags them ``no_storage`` and the caller stops the run instead of blaming the
#: file it happened to be on.
_STORAGE_ERRNOS = frozenset(
    getattr(_errno, n)
    for n in (
        "EACCES",  # permission denied (macOS: mkdir under an unmounted /Volumes)
        "EPERM",
        "EROFS",  # read-only filesystem
        "ENOSPC",  # disk full
        "EDQUOT",  # over quota
        "ENOENT",  # the folder (or its volume) is gone
        "ENOTDIR",
        "ENODEV",  # device removed mid-write
        "ENXIO",
        "EIO",
        "ESTALE",  # stale NFS handle
    )
    if hasattr(_errno, n)
)


def _seconds_from_message(text: str) -> float | None:
    """Pull "A wait of 42 seconds is required" → 42.0, else None."""
    m = _WAIT_SECONDS.search(text)
    return float(m.group(1)) if m else None


def classify(exc: BaseException) -> ErrorCause:
    """Map an exception to an :class:`ErrorCause`.

    Ordered most-specific first: a flood wait is a rate limit even though its
    message also mentions a timeout, a 404 is "not found" before the generic
    catch-all, and so on.

    >>> classify(TimeoutError("Timeout while fetching data")).kind
    'timeout'
    >>> classify(ConnectionError("Cannot connect to Telegram")).kind
    'no_connection'
    >>> classify(RuntimeError("message no longer available")).kind
    'not_found'
    >>> c = classify(RuntimeError("A wait of 30 seconds is required"))
    >>> c.kind, c.retry_after
    ('rate_limit', 30.0)
    >>> classify(RuntimeError("FLOOD_PREMIUM_WAIT_20")).label
    'speed capped'
    >>> classify(PermissionError(13, "Permission denied", "/Volumes/Disk")).kind
    'no_storage'
    """
    name = type(exc).__name__.lower()
    msg = str(exc)
    low = msg.lower()
    code = getattr(exc, "code", None)  # Telethon RPCError HTTP-like code
    seconds = getattr(exc, "seconds", None)  # Telethon FloodWaitError

    # 0. A local disk fault — the download folder, not Telegram. Checked first
    # because it's decided by errno, not by wording: PermissionError says
    # "Permission denied" and FileNotFoundError's *type name* contains
    # "notfound", so either would otherwise be read as a Telegram 403/404 and
    # blamed on the file. ConnectionError and TimeoutError are OSError
    # subclasses too, hence the explicit exclusion — those are network errors
    # and belong to rules 3 and 4.
    if isinstance(exc, OSError) and not isinstance(exc, (ConnectionError, TimeoutError)):
        if getattr(exc, "errno", None) in _STORAGE_ERRNOS:
            where = getattr(exc, "filename", None)
            detail = f"Can't write to the download folder ({exc.strerror or msg})"
            if where:
                detail = f"{detail}: {where}"
            return ErrorCause("no_storage", "storage", detail)

    # 1. Rate limit / flood wait — Telegram is throttling us.
    is_flood_seconds = isinstance(seconds, (int, float)) and not isinstance(seconds, bool)
    if (
        (is_flood_seconds and ("flood" in name or "wait" in low))
        or code == 420
        or "flood" in low
        or "too many requests" in low
        or ("wait of" in low and "second" in low)
    ):
        wait = float(seconds) if is_flood_seconds else _seconds_from_message(low)
        note = f" — wait {int(wait)}s" if wait else ""
        # FLOOD_PREMIUM_WAIT is a *download-speed* cap on non-premium accounts,
        # not a request-rate flood; label it so the two read differently.
        if "premium" in name or "premium" in low:
            return ErrorCause(
                "rate_limit",
                "speed capped",
                f"Telegram download speed cap (premium limit){note}",
                retry_after=wait,
            )
        return ErrorCause(
            "rate_limit",
            "rate limited",
            f"Telegram rate limit (flood wait){note}",
            retry_after=wait,
        )

    # 2. Gone / not found / stale file reference — nothing to resume.
    if (
        code == 404
        or "notfound" in name
        or "filereferenceexpired" in name
        or "fileidinvalid" in name
        or "no longer available" in low
        or "not found" in low
        or ("reference" in low and "expired" in low)
    ):
        return ErrorCause(
            "not_found", "not found", "File no longer available (404 / expired reference)"
        )

    # 3. No connection — the network is down or Telegram is unreachable.
    if (
        isinstance(exc, ConnectionError)
        or "disconnected" in name
        or "cannotconnect" in name
        or "connection" in low
        or "cannot connect" in low
        or "network is unreachable" in low
        or "name or service not known" in low
        or "temporary failure in name resolution" in low
        or "no route to host" in low
    ):
        return ErrorCause(
            "no_connection", "no connection", "No internet connection / cannot reach Telegram"
        )

    # 4. Timeout — usually transient; a retry from the larger .part often works.
    if isinstance(exc, TimeoutError) or "timeout" in low or "timed out" in low:
        return ErrorCause("timeout", "timeout", "Timed out talking to Telegram (transient)")

    # 5. Content protection / forbidden — the chat blocks saving.
    if code == 403 or "forbidden" in low or "noforward" in low or "protected" in low:
        return ErrorCause(
            "protected", "protected", "Download blocked by the chat's content protection"
        )

    return ErrorCause("unknown", "error", msg or type(exc).__name__)


def configure_logging(path: str | Path | None = None, *, level: int = logging.INFO):
    """Attach a rotating file handler so a whole run is logged to disk.

    Idempotent: called again for the same path it does nothing, so the GUI and a
    CLI sharing a process don't stack handlers. The file lives at
    ``~/.tg_downloader/tg_downloader.log`` by default and holds no secrets — only
    filenames, chat titles and error text.
    """
    path = Path(path) if path else LOG_PATH
    for h in log.handlers:
        if getattr(h, "_tg_path", None) == str(path):
            return log
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    except OSError:
        return log  # a read-only home shouldn't stop the app from running
    handler._tg_path = str(path)  # type: ignore[attr-defined]
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
    )
    log.addHandler(handler)
    log.setLevel(level)
    return log
