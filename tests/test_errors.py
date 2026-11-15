"""Error classification: turn a Telethon/network exception into a cause.

No Telethon here — classify() works off type name, message and duck-typed
attributes, so the fakes below stand in for the real exception classes.
"""

import errno
import logging
from dataclasses import FrozenInstanceError

import pytest

from tg_downloader.errors import (
    ErrorCause,
    RateLimited,
    classify,
    configure_logging,
    log,
)


class FakeFloodWait(Exception):
    """Stands in for telethon.errors.FloodWaitError."""

    def __init__(self, seconds):
        self.seconds = seconds
        self.code = 420
        super().__init__(f"A wait of {seconds} seconds is required (caused by GetFileRequest)")


class FakeRPCError(Exception):
    """Stands in for a telethon RPCError with an HTTP-like .code."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def test_flood_wait_is_a_rate_limit_with_retry_after():
    cause = classify(FakeFloodWait(42))
    assert cause.kind == "rate_limit"
    assert cause.retry_after == 42.0
    assert "rate" in cause.label


def test_rate_limit_parsed_from_message_when_no_seconds_attr():
    cause = classify(RuntimeError("A wait of 30 seconds is required"))
    assert cause.kind == "rate_limit"
    assert cause.retry_after == 30.0


class FakePremiumWait(Exception):
    """Stands in for telethon.errors.FloodPremiumWaitError (download-speed cap)."""

    def __init__(self, seconds):
        self.seconds = seconds
        self.code = 420
        super().__init__(f"FLOOD_PREMIUM_WAIT_{seconds}")


def test_premium_wait_is_a_rate_limit_with_distinct_label():
    cause = classify(FakePremiumWait(37))
    assert cause.kind == "rate_limit"  # handled by the same coordinator
    assert cause.retry_after == 37.0  # the exact wait the proto returned
    assert cause.label == "speed capped"  # but reads differently from a flood
    assert "premium" in cause.detail.lower()


def test_timeout_is_classified_transient():
    cause = classify(TimeoutError("Timeout while fetching data (caused by GetFileRequest)"))
    assert cause.kind == "timeout"


def test_connection_errors_are_no_connection():
    assert classify(ConnectionError("Cannot connect to Telegram")).kind == "no_connection"
    assert classify(OSError("Name or service not known")).kind == "no_connection"


def test_404_and_gone_are_not_found():
    assert classify(FakeRPCError(404, "Not found")).kind == "not_found"
    assert classify(RuntimeError("message no longer available")).kind == "not_found"


def test_forbidden_is_protected():
    assert classify(FakeRPCError(403, "CHAT_FORBIDDEN")).kind == "protected"


def test_unknown_falls_through_with_message():
    cause = classify(ValueError("something odd"))
    assert cause.kind == "unknown"
    assert "something odd" in cause.detail


def test_rate_limited_exception_carries_retry_after_and_label():
    e = RateLimited(12.5, "Telegram rate limit — wait 12s")
    assert e.retry_after == 12.5
    assert e.label == "rate limited"  # default
    assert "12" in str(e)
    assert RateLimited(60, "cap", label="speed capped").label == "speed capped"


def test_connection_lost_exception_defaults():
    from tg_downloader.errors import ConnectionLost

    e = ConnectionLost()
    assert e.label == "no connection"  # default label the row shows
    assert str(e) == "no connection"
    assert ConnectionLost("link down", label="offline").label == "offline"


def test_connection_state_distinguishes_internet_from_telegram():
    from tg_downloader.errors import connection_state

    assert connection_state(internet=True, telegram=True) == (True, "")
    online, detail = connection_state(internet=True, telegram=False)
    assert online is False and "Telegram" in detail  # internet up, TG unreachable
    online, detail = connection_state(internet=False, telegram=False)
    assert online is False and "internet" in detail.lower()


def test_error_cause_is_frozen():
    c = ErrorCause("timeout", "timeout", "…")
    with pytest.raises(FrozenInstanceError):
        c.kind = "x"  # frozen dataclass


def test_configure_logging_writes_a_file(tmp_path):
    path = tmp_path / "sub" / "run.log"
    configure_logging(path)
    log.warning("hello from the test")
    # Flush handlers so the assertion sees the line.
    for h in log.handlers:
        h.flush()
    assert path.exists()
    assert "hello from the test" in path.read_text()

    # Idempotent: a second call for the same path must not stack handlers.
    before = len(log.handlers)
    configure_logging(path)
    assert len(log.handlers) == before

    # Don't leave the file handler attached for the rest of the suite.
    for h in [h for h in log.handlers if getattr(h, "_tg_path", None) == str(path)]:
        log.removeHandler(h)
        h.close()
    log.setLevel(logging.WARNING)


# -- local disk faults ----------------------------------------------------
#
# A download folder that's gone (an unmounted drive) fails every queued file
# identically. Classifying it as a per-file error is what let one ejected disk
# untick a whole 3,000-file queue, so these pin the errno-driven branch.


@pytest.mark.parametrize(
    "exc",
    [
        PermissionError(errno.EACCES, "Permission denied", "/Volumes/Disk"),
        FileNotFoundError(errno.ENOENT, "No such file or directory", "/Volumes/Disk/x"),
        OSError(errno.EROFS, "Read-only file system", "/Volumes/Disk"),
        OSError(errno.ENOSPC, "No space left on device", "/Volumes/Disk"),
        OSError(errno.ENODEV, "No such device", "/Volumes/Disk"),
    ],
)
def test_local_disk_faults_are_storage_not_telegram_errors(exc):
    cause = classify(exc)
    assert cause.kind == "no_storage"
    assert cause.label == "storage"


def test_permission_denied_is_not_read_as_content_protection():
    # "Permission denied" contains none of the 403 wording, but a future edit
    # could easily make it match — this is the regression that matters.
    assert classify(PermissionError(errno.EACCES, "Permission denied")).kind != "protected"


def test_file_not_found_is_not_read_as_a_missing_message():
    # FileNotFoundError's *type name* contains "notfound", which rule 2 matches.
    # A local ENOENT is the folder, not an expired file reference.
    exc = FileNotFoundError(errno.ENOENT, "No such file or directory", "/Volumes/Disk")
    assert classify(exc).kind == "no_storage"


def test_network_oserrors_still_classify_as_network():
    # ConnectionError and TimeoutError are OSError subclasses; the storage rule
    # runs first, so it must not swallow them.
    assert classify(ConnectionResetError(errno.ECONNRESET, "Connection reset")).kind == (
        "no_connection"
    )
    assert classify(TimeoutError(errno.ETIMEDOUT, "Operation timed out")).kind == "timeout"


def test_an_oserror_with_an_unrelated_errno_is_not_storage():
    # EBADF isn't one of ours and Python doesn't auto-map it to a subclass
    # (EPIPE, for instance, becomes BrokenPipeError → a connection error).
    assert classify(OSError(errno.EBADF, "Bad file descriptor")).kind == "unknown"
