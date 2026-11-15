"""Resumable downloads: an interrupted .part must continue, not restart.

Exercises download_item against a fake client — no network, no session — so the
resume/alignment logic is covered without touching Telegram.
"""

import asyncio
import os
from types import SimpleNamespace

import pytest

from tg_downloader import telegram
from tg_downloader.models import FileItem
from tg_downloader.telegram import download_item


class _FakeFile:
    def __init__(self, size):
        self.size = size


class _FakeMessage:
    def __init__(self, size):
        self.media = True
        self.file = _FakeFile(size)


class FakeClient:
    """Serves ``content`` in small chunks, recording the offsets it was asked for."""

    def __init__(self, content: bytes):
        self.content = content
        self.offsets: list[int] = []
        self.progress: list[int] = []

    async def get_messages(self, chat_id, ids):
        return _FakeMessage(len(self.content))

    def iter_download(self, message, offset=0):
        self.offsets.append(offset)
        content = self.content

        async def gen():
            pos = offset
            while pos < len(content):
                chunk = content[pos : pos + 7]  # deliberately odd chunk size
                pos += len(chunk)
                yield chunk

        return gen()


def item(size, tmp_path):
    return FileItem(-1, "Grp", 1, "blob.bin", size, ".bin")


def run(coro):
    return asyncio.run(coro)


def test_fresh_download_writes_whole_file(tmp_path):
    content = bytes(i % 256 for i in range(10_000))
    c = FakeClient(content)
    it = item(len(content), tmp_path)
    target = run(download_item(c, it, tmp_path, lambda recv, tot: c.progress.append(recv)))
    assert target.read_bytes() == content
    assert c.offsets == [0]  # started from the beginning
    assert not it.part_path(tmp_path).exists()  # .part renamed away on success


def test_resume_appends_from_aligned_part(tmp_path):
    content = bytes(i % 256 for i in range(10_000))
    it = item(len(content), tmp_path)
    part = it.part_path(tmp_path)
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(content[:8192])  # 8192 is 4 KiB-aligned

    c = FakeClient(content)
    target = run(download_item(c, it, tmp_path))
    assert c.offsets == [8192]  # resumed, didn't restart
    assert target.read_bytes() == content  # and the file is whole


def test_misaligned_part_is_truncated_to_a_valid_offset(tmp_path):
    content = bytes(i % 256 for i in range(10_000))
    it = item(len(content), tmp_path)
    part = it.part_path(tmp_path)
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(content[:8000])  # 8000 % 4096 == 3904, not aligned

    c = FakeClient(content)
    target = run(download_item(c, it, tmp_path))
    assert c.offsets == [4096]  # aligned down to the last 4 KiB
    assert target.read_bytes() == content


def test_progress_reports_running_total_including_resumed_bytes(tmp_path):
    content = bytes(i % 256 for i in range(9000))
    it = item(len(content), tmp_path)
    part = it.part_path(tmp_path)
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(content[:4096])

    c = FakeClient(content)
    run(download_item(c, it, tmp_path, lambda recv, tot: c.progress.append(recv)))
    assert c.progress[0] > 4096  # continues past what was on disk
    assert c.progress[-1] == len(content)  # ends at the full size
    assert c.progress == sorted(c.progress)  # monotonic


def test_missing_message_raises(tmp_path):
    class Gone(FakeClient):
        async def get_messages(self, chat_id, ids):
            return None

    it = item(100, tmp_path)
    with pytest.raises(RuntimeError, match="no longer available"):
        run(download_item(Gone(b""), it, tmp_path))


# -- retries: transient Telegram timeouts must not kill a download ------


class FlakyClient(FakeClient):
    """Fails ``fail_times`` download attempts (like a Telegram timeout) then works."""

    def __init__(self, content, fail_times):
        super().__init__(content)
        self.fail_times = fail_times
        self.attempts = 0

    def iter_download(self, message, offset=0):
        self.attempts += 1
        if self.attempts <= self.fail_times:

            async def boom():
                if self.attempts:  # always true — keeps this a generator
                    raise TimeoutError("Timeout while fetching data")
                yield b""

            return boom()
        return super().iter_download(message, offset=offset)


@pytest.fixture
def no_backoff(monkeypatch):
    async def instant(*_a, **_k):
        return None

    monkeypatch.setattr("tg_downloader.telegram.asyncio.sleep", instant)


def test_transient_error_is_retried_then_succeeds(tmp_path, no_backoff):
    content = bytes(i % 256 for i in range(3000))
    c = FlakyClient(content, fail_times=2)  # fail twice, then work
    it = item(len(content), tmp_path)
    target = run(download_item(c, it, tmp_path, retries=4))
    assert target.read_bytes() == content
    assert c.attempts == 3  # 2 failures + 1 success


def test_retries_are_bounded_then_the_error_surfaces(tmp_path, no_backoff):
    c = FlakyClient(b"x" * 100, fail_times=99)  # never recovers
    it = item(100, tmp_path)
    with pytest.raises(TimeoutError):
        run(download_item(c, it, tmp_path, retries=3))
    assert c.attempts == 4  # initial + 3 retries


def test_part_is_kept_after_exhausted_retries(tmp_path, no_backoff):
    c = FlakyClient(b"x" * 100, fail_times=99)
    it = item(100, tmp_path)
    with pytest.raises(TimeoutError):
        run(download_item(c, it, tmp_path, retries=2))
    assert it.part_path(tmp_path).exists()  # so the next run can resume


def test_cancel_during_retry_raises_cancelled(tmp_path, no_backoff):
    c = FlakyClient(b"x" * 100, fail_times=99)
    it = item(100, tmp_path)
    with pytest.raises(asyncio.CancelledError):
        run(download_item(c, it, tmp_path, retries=5, cancelled=lambda: True))
    assert c.attempts == 1  # stopped after the first failure


def test_on_error_reports_transient_cause(tmp_path, no_backoff):
    """The UI callback fires for each retried failure, with the classified cause."""
    content = bytes(i % 256 for i in range(1000))
    c = FlakyClient(content, fail_times=2)
    it = item(len(content), tmp_path)
    seen = []
    run(
        download_item(
            c, it, tmp_path, on_error=lambda cause, attempt: seen.append((cause.kind, attempt))
        )
    )
    assert [s[0] for s in seen] == ["timeout", "timeout"]  # two transient failures
    assert [s[1] for s in seen] == [1, 2]  # attempt numbers


# -- rate limits: surfaced immediately, not retried locally ------------


class _FloodError(Exception):
    def __init__(self, seconds):
        self.seconds = seconds
        self.code = 420
        super().__init__(f"A wait of {seconds} seconds is required")


class FloodClient(FakeClient):
    """Every download attempt trips a flood wait (like Telegram throttling)."""

    def __init__(self, content, seconds=42):
        super().__init__(content)
        self.seconds = seconds
        self.attempts = 0

    def iter_download(self, message, offset=0):
        self.attempts += 1
        secs = self.seconds

        async def boom():
            if secs:  # always true — keeps this a generator
                raise _FloodError(secs)
            yield b""

        return boom()


def test_rate_limit_raises_RateLimited_without_local_retry(tmp_path, no_backoff):
    from tg_downloader.errors import RateLimited

    c = FloodClient(b"x" * 100, seconds=42)
    it = item(100, tmp_path)
    with pytest.raises(RateLimited) as ei:
        run(download_item(c, it, tmp_path, retries=4))
    assert ei.value.retry_after == 42.0
    assert c.attempts == 1  # surfaced at once, not spun on the retry loop


class _PremiumWait(Exception):
    def __init__(self, seconds):
        self.seconds = seconds
        self.code = 420
        super().__init__(f"FLOOD_PREMIUM_WAIT_{seconds}")


class PremiumFloodClient(FloodClient):
    def iter_download(self, message, offset=0):
        self.attempts += 1
        secs = self.seconds

        async def boom():
            if secs:
                raise _PremiumWait(secs)
            yield b""

        return boom()


def test_premium_wait_surfaces_with_speed_capped_label(tmp_path, no_backoff):
    from tg_downloader.errors import RateLimited

    c = PremiumFloodClient(b"x" * 100, seconds=25)
    it = item(100, tmp_path)
    with pytest.raises(RateLimited) as ei:
        run(download_item(c, it, tmp_path))
    assert ei.value.retry_after == 25.0
    assert ei.value.label == "speed capped"  # distinct from a request-rate flood
    assert c.attempts == 1


def test_part_is_kept_after_rate_limit(tmp_path, no_backoff):
    from tg_downloader.errors import RateLimited

    it = item(100_000, tmp_path)  # bigger than what's on disk, so it streams
    part = it.part_path(tmp_path)
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"x" * 8192)  # some progress already on disk
    with pytest.raises(RateLimited):
        run(download_item(FloodClient(b"x" * 100), it, tmp_path))
    assert part.exists()  # kept so the re-queue resumes it


# -- lost connection: surfaced immediately, not retried locally --------


class OfflineClient(FakeClient):
    """Every download attempt fails as if the link is down."""

    def __init__(self, content):
        super().__init__(content)
        self.attempts = 0

    def iter_download(self, message, offset=0):
        self.attempts += 1

        async def boom():
            if self.attempts:  # always true — keeps this a generator
                raise ConnectionError("Cannot send requests while disconnected")
            yield b""

        return boom()


def test_connection_lost_is_retried_then_raised_when_persistent(tmp_path, no_backoff):
    from tg_downloader.errors import ConnectionLost

    c = OfflineClient(b"x" * 100)  # never recovers
    it = item(100, tmp_path)
    with pytest.raises(ConnectionLost) as ei:
        run(download_item(c, it, tmp_path, retries=4))
    assert ei.value.label == "no connection"
    # Retried locally first (a transient server-side drop self-heals); only after
    # the retries are exhausted is it escalated to the caller's offline gate.
    assert c.attempts == 5  # initial + 4 retries


def test_transient_connection_drop_recovers_without_escalating(tmp_path, no_backoff):
    # A single dropped connection (Telegram recycling a download connection) heals
    # on the next attempt — no ConnectionLost, no offline gate.
    content = bytes(i % 256 for i in range(3000))

    class _DropOnce(FakeClient):
        def __init__(self, content):
            super().__init__(content)
            self.attempts = 0

        def iter_download(self, message, offset=0):
            self.attempts += 1
            if self.attempts == 1:

                async def boom():
                    if self.attempts:
                        raise ConnectionError("Server closed the connection")
                    yield b""

                return boom()
            return super().iter_download(message, offset=offset)

    c = _DropOnce(content)
    it = item(len(content), tmp_path)
    target = run(download_item(c, it, tmp_path, retries=4))
    assert target.read_bytes() == content  # completed
    assert c.attempts == 2  # dropped once, resumed and finished


def test_part_is_kept_after_connection_lost(tmp_path, no_backoff):
    from tg_downloader.errors import ConnectionLost

    it = item(100_000, tmp_path)
    part = it.part_path(tmp_path)
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"x" * 8192)  # progress already on disk
    with pytest.raises(ConnectionLost):
        run(download_item(OfflineClient(b"x" * 100), it, tmp_path))
    assert part.exists()  # kept so this exact file resumes when back online


# -- parallel download: one file split across several connections ------


class _FakeSender:
    """Serves byte ranges of ``content`` for a ``GetFileRequest``, logging offsets."""

    def __init__(self, content, offsets):
        self.content = content
        self.offsets = offsets

    async def send(self, request):
        from types import SimpleNamespace

        self.offsets.append(request.offset)
        data = self.content[request.offset : request.offset + request.limit]
        return SimpleNamespace(bytes=data)


class ParallelClient(FakeClient):
    """A fake media DC: hands out exported senders that serve file ranges."""

    flood_sleep_threshold = 5  # short flood waits (<= this) are absorbed per-worker

    def __init__(self, content):
        super().__init__(content)
        self.borrowed = 0
        self.returned = 0
        # Home DC is 1; stub_location serves files on DC 2, so the default tests
        # take the *exported* (borrow) path. The main sender covers the home-DC case.
        self.session = SimpleNamespace(dc_id=1)
        self._sender = _FakeSender(self.content, self.offsets)

    async def _borrow_exported_sender(self, dc_id):
        self.borrowed += 1
        return _FakeSender(self.content, self.offsets)

    async def _return_exported_sender(self, sender):
        self.returned += 1


@pytest.fixture
def stub_location(monkeypatch):
    """Make ``get_input_location`` succeed for the fake message (dc 2, dummy loc)."""
    monkeypatch.setattr(
        "tg_downloader.telegram.utils.get_input_location",
        lambda message: (2, object()),
    )


def _multipart_content():
    from tg_downloader.telegram import _PART_SIZE

    total = 2 * _PART_SIZE + 1234  # three parts, a short last one
    return bytes(i % 256 for i in range(total))


def test_parallel_download_writes_whole_file(tmp_path, stub_location):
    content = _multipart_content()
    c = ParallelClient(content)
    it = item(len(content), tmp_path)
    target = run(
        download_item(c, it, tmp_path, lambda recv, tot: c.progress.append(recv), connections=4)
    )
    assert target.read_bytes() == content  # every part landed at the right offset
    assert c.borrowed == c.returned  # every borrowed sender was returned
    assert not it.part_path(tmp_path).exists()  # .part renamed away on success
    from tg_downloader.telegram import part_index_path

    assert not part_index_path(it.part_path(tmp_path)).exists()  # sidecar dropped
    assert c.progress[-1] == len(content)  # progress reaches the full size


def test_pwrite_writes_at_offsets(tmp_path, monkeypatch):
    """Positioned writes land at the right offset, with or without os.pwrite."""
    if hasattr(os, "pwrite"):
        monkeypatch.delattr(telegram.os, "pwrite")
    path = tmp_path / "parts.bin"
    fd = telegram._open_rw(path)
    try:
        telegram._pwrite(fd, b"0123456789", 0)
        telegram._pwrite(fd, b"XY", 4)
    finally:
        os.close(fd)
    assert path.read_bytes() == b"0123XY6789"


def test_parallel_download_preserves_newlines(tmp_path, stub_location, monkeypatch):
    """Downloaded bytes must be verbatim; Windows text mode would corrupt 0x0A."""
    if hasattr(os, "pwrite"):
        monkeypatch.delattr(telegram.os, "pwrite")
    content = b"line one\nline two\n\r\n" * 5000
    c = ParallelClient(content)
    it = item(len(content), tmp_path)
    target = run(download_item(c, it, tmp_path, connections=4))
    assert target.read_bytes() == content


def test_parallel_resume_only_fetches_missing_parts(tmp_path, stub_location):
    from tg_downloader.telegram import _PART_SIZE, part_index_path

    content = _multipart_content()
    it = item(len(content), tmp_path)
    part = it.part_path(tmp_path)
    part.parent.mkdir(parents=True, exist_ok=True)
    # Preallocate the sparse .part with only part 0 written, and mark it done.
    buf = bytearray(len(content))
    buf[0:_PART_SIZE] = content[0:_PART_SIZE]
    part.write_bytes(buf)
    part_index_path(part).write_bytes(bytes([1, 0, 0]))  # part 0 done, 1 and 2 missing

    c = ParallelClient(content)
    target = run(download_item(c, it, tmp_path, connections=4))
    assert target.read_bytes() == content
    assert 0 not in c.offsets  # part 0 already on disk — not re-fetched
    assert set(c.offsets) == {_PART_SIZE, 2 * _PART_SIZE}  # only the missing two


def test_parallel_falls_back_to_single_stream_without_a_location(tmp_path):
    # No stub_location: get_input_location on the fake message raises, so even with
    # connections=4 the one-connection iter_download path is used (offsets == [0]).
    content = bytes(i % 256 for i in range(3000))
    c = FakeClient(content)
    it = item(len(content), tmp_path)
    target = run(download_item(c, it, tmp_path, connections=4))
    assert target.read_bytes() == content
    assert c.offsets == [0]  # single stream from the start


def test_parallel_uses_main_sender_on_the_home_dc(tmp_path, monkeypatch):
    # A file on the DC we're already connected to must use the main sender, not a
    # borrowed one — exporting an auth for the current DC raises DC_ID_INVALID
    # ("same data center"). ParallelClient's home DC is 1, so serve the file on 1.
    monkeypatch.setattr(
        "tg_downloader.telegram.utils.get_input_location", lambda message: (1, object())
    )
    content = _multipart_content()
    c = ParallelClient(content)  # session.dc_id == 1 → same DC as the file
    it = item(len(content), tmp_path)
    target = run(download_item(c, it, tmp_path, connections=4))
    assert target.read_bytes() == content  # served over the main sender
    assert c.borrowed == 0  # never tried to export an auth for the home DC


def test_parallel_part_is_kept_on_failure(tmp_path, stub_location, no_backoff):
    class _BrokenSender:
        async def send(self, request):
            raise TimeoutError("Timeout while fetching data")

    class _BrokenClient(ParallelClient):
        async def _borrow_exported_sender(self, dc_id):
            self.borrowed += 1
            return _BrokenSender()

    content = _multipart_content()
    c = _BrokenClient(content)
    it = item(len(content), tmp_path)
    with pytest.raises(TimeoutError):
        run(download_item(c, it, tmp_path, retries=1, connections=4))
    from tg_downloader.telegram import part_index_path

    part = it.part_path(tmp_path)
    assert part.exists()  # kept so the next run resumes
    assert part_index_path(part).exists()  # sidecar kept too
    assert c.borrowed == c.returned  # senders released even on failure


def test_parallel_return_sender_error_does_not_mask_success(tmp_path, stub_location):
    # A reconnect can clear Telethon's borrowed-sender pool mid-download, so
    # _return_exported_sender then raises KeyError(dc_id). It must be swallowed,
    # not fail an otherwise-complete download.
    content = _multipart_content()

    class _BadReturn(ParallelClient):
        async def _return_exported_sender(self, sender):
            raise KeyError(5)

    c = _BadReturn(content)
    it = item(len(content), tmp_path)
    target = run(download_item(c, it, tmp_path, connections=4))
    assert target.read_bytes() == content  # the KeyError from return was swallowed


def test_parallel_return_error_does_not_mask_connection_lost(tmp_path, stub_location):
    # The real bug: a dropped connection raises ConnectionError from send *and*
    # KeyError from the return (pool cleared). The return's KeyError must not
    # replace the ConnectionError — the file must surface as ConnectionLost (→
    # offline gate), not an unknown "error" that churns the retry loop.
    from tg_downloader.errors import ConnectionLost

    class _DropSender:
        async def send(self, request):
            raise ConnectionError("Cannot send requests while disconnected")

    class _DroppingClient(ParallelClient):
        async def _borrow_exported_sender(self, dc_id):
            self.borrowed += 1
            return _DropSender()

        async def _return_exported_sender(self, sender):
            raise KeyError(5)  # reconnect wiped the pool

    c = _DroppingClient(_multipart_content())
    it = item(2 * 524288 + 1234, tmp_path)
    with pytest.raises(ConnectionLost):
        run(download_item(c, it, tmp_path, connections=4))


def test_parallel_short_flood_is_absorbed_locally(tmp_path, stub_location, no_backoff):
    # A sub-threshold flood wait (2s <= 5s) must be slept per-worker and the part
    # retried — NOT escalated to the batch coordinator. Mirrors what Telethon's
    # _call does for the single-stream path (which the raw sender.send bypasses).
    from tg_downloader.errors import RateLimited

    content = _multipart_content()

    class _FloodOnceSender:
        def __init__(self, content, offsets):
            self.content = content
            self.offsets = offsets
            self.sent = 0

        async def send(self, request):
            self.sent += 1
            if self.sent == 1:  # first request on this sender floods briefly
                raise _FloodError(2)  # 2s <= threshold(5) → absorbed locally
            from types import SimpleNamespace

            self.offsets.append(request.offset)
            end = request.offset + request.limit
            return SimpleNamespace(bytes=self.content[request.offset : end])

    class _C(ParallelClient):
        async def _borrow_exported_sender(self, dc_id):
            self.borrowed += 1
            return _FloodOnceSender(self.content, self.offsets)

    c = _C(content)
    it = item(len(content), tmp_path)
    try:
        target = run(download_item(c, it, tmp_path, connections=4))
    except RateLimited:
        raise AssertionError("short flood must not escalate to the coordinator") from None
    assert target.read_bytes() == content  # completed after absorbing the short waits


def test_parallel_long_flood_escalates_to_rate_limited(tmp_path, stub_location, no_backoff):
    # A flood longer than the threshold must still surface as RateLimited so the
    # coordinator pauses the whole batch.
    from tg_downloader.errors import RateLimited

    class _LongFloodSender:
        async def send(self, request):
            raise _FloodError(42)  # 42s > threshold(5) → escalate

    class _C(ParallelClient):
        async def _borrow_exported_sender(self, dc_id):
            self.borrowed += 1
            return _LongFloodSender()

    c = _C(_multipart_content())
    it = item(2 * 524288 + 1234, tmp_path)
    with pytest.raises(RateLimited):
        run(download_item(c, it, tmp_path, connections=4))


# -- reachability probe: honest about a down socket --------------------


def test_probe_endpoint_false_for_a_closed_port(tmp_path):
    from tg_downloader.telegram import probe_endpoint

    # Port 1 on localhost isn't listening — the connect should fail fast.
    assert run(probe_endpoint("127.0.0.1", 1, timeout=1.0)) is False


def test_probe_endpoint_true_for_a_listening_socket(tmp_path):
    import socket as _socket

    from tg_downloader.telegram import probe_endpoint

    srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))  # OS picks a free port
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert run(probe_endpoint("127.0.0.1", port, timeout=1.0)) is True
    finally:
        srv.close()


# -- ensure_connected: gate reopens only on a real round-trip ----------


class _ReconnectClient:
    """Records disconnect/connect/get_me calls; ``get_me`` may be made to fail."""

    def __init__(self, get_me_ok=True):
        self.calls = []
        self.get_me_ok = get_me_ok

    async def disconnect(self):
        self.calls.append("disconnect")

    async def connect(self):
        self.calls.append("connect")

    async def get_me(self):
        self.calls.append("get_me")
        if not self.get_me_ok:
            raise ConnectionError("Cannot send requests while disconnected")
        return object()  # a truthy "user"


def test_ensure_connected_does_a_full_reconnect_then_confirms(tmp_path):
    from tg_downloader.telegram import ensure_connected

    c = _ReconnectClient(get_me_ok=True)
    assert run(ensure_connected(c)) is True
    # A full teardown resets the main *and* media-DC senders (a bare get_me on the
    # main DC would pass while downloads still fail), then one real request confirms.
    assert c.calls == ["disconnect", "connect", "get_me"]


def test_ensure_connected_false_when_the_request_still_fails(tmp_path):
    from tg_downloader.telegram import ensure_connected

    c = _ReconnectClient(get_me_ok=False)  # reconnected but the session's still dead
    assert run(ensure_connected(c)) is False  # so the caller keeps the batch parked
