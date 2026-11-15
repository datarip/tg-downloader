"""Thin Telethon layer shared by the GUI and the CLI.

This is the only place (besides the entry points) that imports Telethon.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from telethon import TelegramClient, utils
from telethon.tl.functions.upload import GetFileRequest

from .config import SESSION_PATH
from .errors import ConnectionLost, RateLimited, classify, log
from .filters import matched_extension
from .models import FileItem

#: Public hosts probed to tell "the internet is down" from "Telegram is
#: unreachable" (internet up, Telegram's DC not). Any one connecting = online.
_INTERNET_PROBES = (("1.1.1.1", 443), ("8.8.8.8", 53))
#: Telegram DC endpoint probed when the session hasn't recorded its own yet
#: (149.154.160.0/20 is Telegram's range). Only a fallback — normally we probe
#: the exact DC the session is using.
_TG_PROBE_FALLBACK = ("149.154.167.50", 443)


class CredentialsMissing(RuntimeError):
    """Raised when api_id / api_hash haven't been configured yet."""


#: Default ``flood_sleep_threshold`` (seconds). Telethon sleeps flood / premium
#: waits up to this itself (silent, per-request); longer ones are *raised* and so
#: reach our coordinator, which pauses the whole batch and shows the cause.
#: Lower than Telethon's own 60s default so real download-speed caps surface.
FLOOD_SLEEP_THRESHOLD = 20

#: Reconnection settings applied to every client we build. Telethon's default
#: ``connection_retries=5`` gives up after five reconnect attempts and leaves the
#: client disconnected — a brief network/DC blip during an overnight run then
#: cascades into "Cannot send requests while disconnected" for the whole batch
#: and ends the run. Retrying forever (``None``) with a short delay lets a
#: transient outage heal itself and the downloads resume, instead of dropping the
#: client. ``auto_reconnect`` is Telethon's default but pinned here for clarity.
RECONNECT_KWARGS = {
    "connection_retries": None,  # keep retrying the reconnect, don't give up at 5
    "retry_delay": 5,  # seconds between reconnect attempts
    "auto_reconnect": True,
}

#: ``O_BINARY`` exists only on Windows. Without it, ``os.open`` defaults to text
#: mode there and every ``0x0A`` written into a downloaded part becomes ``0x0D 0x0A``.
_O_BINARY = getattr(os, "O_BINARY", 0)


def _open_rw(path: str | Path) -> int:
    """Open (creating) ``path`` for positioned read/write, binary on every OS."""
    return os.open(path, os.O_RDWR | os.O_CREAT | _O_BINARY, 0o644)


def _pwrite(fd: int, data: bytes, offset: int) -> None:
    """Write ``data`` at ``offset`` without disturbing a shared file position.

    ``os.pwrite`` is Unix-only. The Windows fallback is a plain seek + write,
    which is safe here: every caller runs on the single asyncio event-loop
    thread and never awaits between the two calls.
    """
    view = memoryview(data)
    if hasattr(os, "pwrite"):
        while view:
            n = os.pwrite(fd, view, offset)
            if n <= 0:
                raise OSError("short write")
            view = view[n:]
            offset += n
        return
    os.lseek(fd, offset, os.SEEK_SET)
    while view:
        n = os.write(fd, view)
        if n <= 0:
            raise OSError("short write")
        view = view[n:]


_crypto_logged = False


def crypto_backend() -> str:
    """Which AES implementation Telethon will use: ``"cryptg"`` or ``"python"``.

    ``cryptg`` needs no wiring — Telethon routes AES through it automatically
    *iff* ``import cryptg`` succeeded, which it records by setting the
    ``telethon.crypto.aes.cryptg`` attribute. The fallback to pure-Python AES is
    otherwise silent and several times slower, so the self-check and the startup
    log both ask this function rather than each testing the attribute themselves.
    """
    from telethon.crypto import aes

    return "cryptg" if getattr(aes, "cryptg", None) is not None else "python"


def _log_crypto_backend() -> None:
    """Log once whether the native ``cryptg`` AES backend is active.

    ``cryptg`` is a hard dependency and needs no wiring — Telethon routes AES
    through it automatically *iff* ``import cryptg`` succeeded, which it checks by
    the ``telethon.crypto.aes.cryptg`` attribute being non-None. Without it,
    decryption falls back to pure-Python AES and downloads become CPU-bound and
    much slower. That fallback is otherwise silent, so we surface it: a broken
    wheel on some future machine should show up in the log, not just as mystery
    slowness. Logged once (first client build) to avoid noise.
    """
    global _crypto_logged
    if _crypto_logged:
        return
    _crypto_logged = True

    if crypto_backend() == "cryptg":
        log.info("Native cryptg AES backend active (fast downloads).")
    else:
        log.warning(
            "cryptg unavailable — falling back to pure-Python AES; downloads will be "
            "CPU-bound and slow. Reinstall deps (`uv sync`) to restore the native backend."
        )


async def probe_endpoint(host: str, port: int, timeout: float = 2.5) -> bool:
    """True if a TCP connection to ``host:port`` opens within ``timeout`` seconds.

    A cheap reachability check that spends no Telegram API call. We need it
    because Telethon's ``is_connected()`` reports *intent* — it stays True right
    through an outage while auto-reconnect spins in the background — so it can't
    tell the connection light whether the socket is actually up. An honest TCP
    probe can.
    """
    try:
        fut = asyncio.open_connection(host, port)
        _, writer = await asyncio.wait_for(fut, timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    try:
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), timeout)
    except (OSError, asyncio.TimeoutError):
        pass  # the connect already proved reachability; a messy close doesn't matter
    return True


async def check_internet(timeout: float = 2.5) -> bool:
    """True if any well-known public host is reachable — i.e. the internet is up."""
    for host, port in _INTERNET_PROBES:
        if await probe_endpoint(host, port, timeout):
            return True
    return False


def telegram_endpoint(client: TelegramClient) -> tuple[str, int]:
    """The Telegram DC ``(host, port)`` to probe: the session's own, or a default.

    Probing the exact DC the session is using tells "Telegram is unreachable"
    apart from "the whole internet is down" without an API call.
    """
    sess = getattr(client, "session", None)
    addr = getattr(sess, "server_address", None)
    port = getattr(sess, "port", None)
    if addr and port:
        return str(addr), int(port)
    return _TG_PROBE_FALLBACK


async def ensure_connected(client: TelegramClient, timeout: float = 8.0) -> bool:
    """Force a genuine, *full* reconnect and confirm the client can round-trip.

    Reopening the offline gate on a raw TCP probe — or on ``is_connected()``,
    which reports *intent* — isn't safe: the socket can be reachable again while
    Telethon's MTProto session is still dead, so the next download fails instantly
    with "Cannot send requests while disconnected" (the paused↔downloading
    flicker).

    A one-off ``get_me`` isn't enough either: it round-trips on the **main** DC,
    but file downloads run over a **separate media-DC** sender. After a drop the
    main DC can recover while the media sender is still dead, so ``get_me`` passes
    yet downloads keep failing — flicker again. So we tear the whole connection
    down and back up: ``disconnect()`` drops the main *and* exported media senders,
    ``connect()`` re-establishes (it won't do a real reconnect while the sender
    still thinks it's up, hence the disconnect first), and ``get_me`` confirms.
    The media senders are then re-created lazily on the next download, against the
    now-live network.

    ``disconnect()`` cancels any in-flight download; the caller's offline gate is
    still closed at that moment, so the cancelled file re-queues itself and resumes
    rather than being stranded (see ``App._run_download``'s CancelledError branch).
    Every step is bounded by ``timeout`` so a dead socket can't hang the monitor.
    """
    try:
        try:
            await asyncio.wait_for(client.disconnect(), timeout)
        except Exception:
            pass  # already down / mid-reconnect — the connect() below is what matters
        await asyncio.wait_for(client.connect(), timeout)
        await asyncio.wait_for(client.get_me(), timeout)  # a real round-trip, not intent
        return True
    except Exception:
        return False


async def build_client(
    api_id: str | int,
    api_hash: str,
    *,
    phone: Callable | None = None,
    code_callback: Callable | None = None,
    password: Callable | None = None,
    session: str | Path = SESSION_PATH,
    flood_sleep_threshold: float = FLOOD_SLEEP_THRESHOLD,
) -> TelegramClient:
    """Create and log in a Telethon client.

    The callbacks may be sync or async; Telethon awaits them if awaitable.
    Passing none of them makes ``start()`` prompt on stdin (CLI behaviour).

    ``flood_sleep_threshold`` sets the boundary described on
    :data:`FLOOD_SLEEP_THRESHOLD`: shorter waits Telethon absorbs transparently,
    longer ones surface to the caller as ``FloodWaitError`` / ``FloodPremiumWaitError``.
    """
    if not api_id or not api_hash:
        raise CredentialsMissing(
            "api_id and api_hash are not set — add them in Settings "
            "(GUI) or ~/.tg_downloader/config.json"
        )
    _log_crypto_backend()
    client = TelegramClient(
        str(session),
        int(api_id),
        api_hash,
        flood_sleep_threshold=flood_sleep_threshold,
        **RECONNECT_KWARGS,
    )
    kwargs = {}
    if phone is not None:
        kwargs["phone"] = phone
    if code_callback is not None:
        kwargs["code_callback"] = code_callback
    if password is not None:
        kwargs["password"] = password
    await client.start(**kwargs)
    return client


async def connect_if_authorized(
    api_id: str | int,
    api_hash: str,
    *,
    session: str | Path = SESSION_PATH,
    flood_sleep_threshold: float = FLOOD_SLEEP_THRESHOLD,
) -> TelegramClient | None:
    """Reconnect from a saved session **without** prompting to log in.

    Returns a connected, authorised client, or ``None`` when credentials are
    missing, the session isn't authorised yet, or the link can't be
    established. Never raises for the not-logged-in case — that's the normal
    first-run state, and this is meant to be called silently at launch.
    """
    if not api_id or not api_hash:
        return None
    _log_crypto_backend()
    client = TelegramClient(
        str(session),
        int(api_id),
        api_hash,
        flood_sleep_threshold=flood_sleep_threshold,
        **RECONNECT_KWARGS,
    )
    try:
        await client.connect()
        if await client.is_user_authorized():
            return client
    except Exception:
        pass
    try:
        await client.disconnect()
    except Exception:
        pass
    return None


def is_content_protected(entity) -> bool:
    """True if admins enabled 'restrict saving content' on this chat.

    Telegram enforces this server-side; downloads from such chats may fail.
    """
    return bool(getattr(entity, "noforwards", False))


async def latest_message_id(client: TelegramClient, entity) -> int:
    """The newest message id in a chat, or 0 if it has none.

    Used as the watermark for update scans: message ids are monotonic per chat,
    so anything above this at the next scan is genuinely new.
    """
    async for message in client.iter_messages(entity, limit=1):
        return message.id
    return 0


async def iter_matching_files(
    client: TelegramClient,
    entity,
    allowed: set[str],
    limit: int | None = None,
    min_id: int = 0,
) -> AsyncIterator[FileItem]:
    """Yield a FileItem for every message whose file matches ``allowed``.

    Messages without media, without a filename (photos, voice notes), or whose
    extension isn't allowed are skipped. ``min_id`` bounds the scan to messages
    newer than that id (exclusive) — pass a channel's watermark for an update
    scan; leave it 0 to walk the whole history.
    """
    title = getattr(entity, "title", str(entity))
    chat_id = getattr(entity, "id", entity)

    async for message in client.iter_messages(entity, limit=limit, min_id=min_id):
        if not message.media or not message.file or not message.file.name:
            continue
        ext = matched_extension(message.file.name, allowed)
        if not ext:
            continue
        yield FileItem(
            chat_id=chat_id,
            chat_title=title,
            msg_id=message.id,
            name=message.file.name,
            size=message.file.size or 0,
            ext=ext,
        )


#: MTProto file parts are 4 KiB aligned; resume offsets must be too.
_CHUNK_ALIGN = 4096


async def _stream_to_part(client, message, part, total, progress_callback) -> None:
    """One download attempt: resume the ``.part`` and append until complete."""
    # Resume from the largest 4 KiB-aligned prefix already on disk; drop any
    # partial trailing chunk from a hard kill so the offset stays valid.
    have = part.stat().st_size if part.exists() else 0
    offset = have - (have % _CHUNK_ALIGN)
    if offset and offset != have:
        with open(part, "r+b") as fh:
            fh.truncate(offset)

    if total and offset >= total:  # already fully fetched last time
        return

    received = offset
    with open(part, "ab" if offset else "wb") as fh:
        if offset == 0:
            fh.truncate(0)
        async for chunk in client.iter_download(message, offset=offset):
            fh.write(chunk)
            received += len(chunk)
            if progress_callback is not None:
                res = progress_callback(received, total)
                if inspect.isawaitable(res):
                    await res


#: One ``upload.getFile`` pulls this many bytes. 512 KiB is Telethon's own max
#: request size and stays 4 KiB aligned inside a single 1 MiB part boundary
#: (offset & limit never straddle a 1 MiB block, which Telegram rejects). Parallel
#: downloads split a file into parts of this size — one positioned write per part.
_PART_SIZE = 512 * 1024


def part_index_path(part: Path) -> Path:
    """Sidecar bitmap (one flag byte per part) for a parallel ``.part``.

    Records which parts are already on disk, since a parallel ``.part`` is filled
    out of order and its size no longer equals the bytes downloaded.
    """
    return part.with_name(part.name + ".idx")


def _part_len(index: int, nparts: int, total: int) -> int:
    """Byte length of part ``index`` — a full ``_PART_SIZE`` except a short last one."""
    if index == nparts - 1:
        return total - index * _PART_SIZE
    return _PART_SIZE


def _done_bitmap(part: Path, total: int) -> bytearray:
    """Which parts of ``part`` are already downloaded (one flag byte per part).

    Trusts the ``.idx`` sidecar only when ``part`` is the preallocated parallel
    file (size == ``total``); a *smaller* ``.part`` is a single-stream leftover — a
    contiguous prefix — whose whole parts count as done and whose partial tail is
    re-fetched. Because this reads state *before* the caller (re)creates the files,
    a stale ``.idx`` left behind by a deleted ``.part`` is harmless: the part won't
    exist, so we start empty and overwrite the sidecar with fresh zeros.
    """
    nparts = (total + _PART_SIZE - 1) // _PART_SIZE
    bm = bytearray(nparts)
    if not part.exists():
        return bm
    size = part.stat().st_size
    idx = part_index_path(part)
    if size == total and idx.exists():
        data = idx.read_bytes()
        for i in range(min(len(data), nparts)):
            bm[i] = 1 if data[i] else 0
    else:
        for i in range(min(size // _PART_SIZE, nparts)):  # contiguous prefix
            bm[i] = 1
    return bm


def downloaded_bytes(part: Path, total: int) -> int:
    """Bytes of ``part`` genuinely on disk, honouring the parallel sparse format.

    A parallel ``.part`` is preallocated to ``total`` and filled out of order, so
    its file size isn't the byte count — the ``.idx`` bitmap is. A single-stream
    ``.part`` is a contiguous prefix, so its size *is* the count. Used by the UI to
    show honest resume progress instead of a preallocated file's full length.
    """
    if not part.exists() or not total:
        return 0
    size = part.stat().st_size
    idx = part_index_path(part)
    if size == total and idx.exists():
        nparts = (total + _PART_SIZE - 1) // _PART_SIZE
        data = idx.read_bytes()
        return sum(
            _part_len(i, nparts, total) for i in range(min(len(data), nparts)) if data[i]
        )
    return size


async def _send_part_request(client, sender, request, cancelled):
    """``sender.send(request)`` with the same flood handling ``client(...)`` gives.

    A raw ``MTProtoSender.send`` bypasses ``TelegramClient._call``, which is where
    Telethon absorbs short flood waits (``e.seconds <= flood_sleep_threshold``,
    slept silently per-request) and only lets *longer* ones propagate. Without
    this, the parallel path escalates **every** flood — including the 2s waits the
    single-stream path swallows — to the batch-wide rate-limit coordinator, so a
    busy account flickers between pause and resume once per short wait. Here we
    replicate ``_call``: absorb a sub-threshold flood locally and retry the part;
    re-raise a longer one so the coordinator pauses the whole batch. ``cancelled``
    is polled during the sleep so Stop stays responsive.
    """
    while True:
        try:
            return await sender.send(request)
        except Exception as exc:
            cause = classify(exc)
            if cause.kind != "rate_limit":
                raise
            wait = cause.retry_after or 1.0
            if wait > client.flood_sleep_threshold:
                raise  # long throttle — let the coordinator pause the batch
            log.info("Absorbing short flood wait (%.0fs) on a download part", wait)
            slept = 0.0
            while slept < wait:
                if cancelled is not None and cancelled():
                    raise asyncio.CancelledError() from None
                step = min(0.5, wait - slept)
                await asyncio.sleep(step)
                slept += step


async def _stream_to_part_parallel(
    client, message, part, total, progress_callback, connections, cancelled=None
) -> None:
    """One download attempt spread over ``connections`` simultaneous senders.

    Splits the file into ``_PART_SIZE`` parts and pulls the missing ones with
    ``connections`` requests in flight at once — what makes a *single* file
    download several times faster on a link the one-connection path can't
    saturate (this is what the Telegram desktop app does). The requests go over a
    borrowed exported sender to the media DC, or the **main sender** when the file
    is on the DC we're already connected to (exporting an auth for the current DC
    is an error — see ``exported`` below). Each completed part is recorded in the
    ``.idx`` sidecar, so a resumed run only fetches what's still missing. Any sender
    error cancels the rest and propagates, so
    :func:`download_item`'s retry / rate-limit / offline handling treats it exactly
    as it does the single-stream path. Short flood waits are absorbed per-worker by
    :func:`_send_part_request` (mirroring ``TelegramClient._call``), so only a
    *long* throttle reaches the batch coordinator. ``progress_callback`` is called
    with the running aggregate byte total and, as in the single path, may await
    (pause) or raise (stop) to control the whole file — every worker parks or aborts
    on it.
    """
    dc_id, location = utils.get_input_location(message)
    # A file whose media DC is the one we're *already* connected to must use the
    # main sender: exporting an auth for the current DC raises DC_ID_INVALID
    # ("authorization exported for the same data center one is currently connected
    # to"). This is exactly the condition Telethon's own downloader uses.
    exported = bool(dc_id) and dc_id != client.session.dc_id
    nparts = (total + _PART_SIZE - 1) // _PART_SIZE
    idx = part_index_path(part)
    done = _done_bitmap(part, total)

    part_fd = _open_rw(part)
    idx_fd = _open_rw(idx)
    try:
        os.ftruncate(part_fd, total)  # preallocate so out-of-order writes land right
        os.ftruncate(idx_fd, nparts)
        _pwrite(idx_fd, bytes(done), 0)

        pending: asyncio.Queue[int] = asyncio.Queue()
        for i in range(nparts):
            if not done[i]:
                pending.put_nowait(i)

        received = sum(_part_len(i, nparts, total) for i in range(nparts) if done[i])
        if progress_callback is not None:
            res = progress_callback(received, total)
            if inspect.isawaitable(res):
                await res

        if not pending.empty():
            state = {"received": received}

            async def pull() -> None:
                # Home-DC files share the main connection (no auth export); others
                # get a borrowed exported sender to the media DC.
                if exported:
                    sender = await client._borrow_exported_sender(dc_id)
                else:
                    sender = client._sender
                try:
                    while True:
                        try:
                            i = pending.get_nowait()
                        except asyncio.QueueEmpty:
                            return
                        want = _part_len(i, nparts, total)
                        result = await _send_part_request(
                            client,
                            sender,
                            GetFileRequest(location, offset=i * _PART_SIZE, limit=_PART_SIZE),
                            cancelled,
                        )
                        data = result.bytes
                        _pwrite(part_fd, data, i * _PART_SIZE)
                        if len(data) < want:  # short read mid-file — retry, don't mark
                            raise RuntimeError(f"short read on part {i}: {len(data)}/{want}")
                        _pwrite(idx_fd, b"\x01", i)  # durable: this part is complete
                        state["received"] += want
                        if progress_callback is not None:
                            res = progress_callback(state["received"], total)
                            if inspect.isawaitable(res):
                                await res
                finally:
                    # Only borrowed senders are returned; the main sender is never
                    # ours to release. Returning a sender must also never mask the
                    # real download error: a dropped connection triggers a full
                    # client reconnect, and Telethon's ``disconnect()`` **clears**
                    # ``_borrowed_senders`` — so a return that runs afterwards raises
                    # ``KeyError(dc_id)`` from ``self._borrowed_senders[sender.dc_id]``.
                    # Raised inside this ``finally`` it would *replace* the in-flight
                    # ``ConnectionError`` (which classifies as ``no_connection`` and
                    # is handled by the offline gate) with an ``unknown`` "error" that
                    # churns the retry loop instead. Swallow it — the pool is gone
                    # anyway, so there's nothing to release.
                    if exported:
                        try:
                            await client._return_exported_sender(sender)
                        except Exception:
                            pass

            workers = [asyncio.create_task(pull()) for _ in range(min(connections, nparts))]
            try:
                await asyncio.gather(*workers)
            except BaseException:
                for w in workers:
                    w.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                raise
    finally:
        os.close(part_fd)
        os.close(idx_fd)

    # Only reached on success, when every part is on disk. The handles are closed
    # first because Windows refuses to delete a file that is still open (WinError
    # 32), where POSIX would simply unlink it.
    idx.unlink(missing_ok=True)


async def download_item(
    client: TelegramClient,
    item: FileItem,
    download_dir: str | Path,
    progress_callback: Callable | None = None,
    *,
    retries: int = 4,
    connections: int = 1,
    cancelled: Callable[[], bool] | None = None,
    on_error: Callable[[object, int], None] | None = None,
) -> Path:
    """Download one FileItem, **resuming** a partial ``.part`` if one exists.

    Streams to ``.part`` and renames it onto the target only on completion, so an
    interrupted download picks up where it left off instead of starting over. With
    ``connections == 1`` (default) it appends via ``iter_download`` (one sender);
    with ``connections > 1`` it splits the file into ``_PART_SIZE`` parts and pulls
    them over that many simultaneous senders (see :func:`_stream_to_part_parallel`),
    which is several times faster for a single large file on a fast link. Both
    keep the ``.part`` (parallel adds an ``.idx`` sidecar) so resume works either
    way, and switching ``connections`` between runs resumes the same ``.part``. On
    failure the caller should *keep* the ``.part`` (that's what makes the next run
    resumable) — only delete it if the message is gone.

    Transient failures (Telegram timeouts / "internal issues", a **dropped
    connection**) are **retried** up to ``retries`` times with exponential
    backoff, each attempt resuming from the now-larger ``.part``. ``cancelled()``
    (if given) is polled during backoff so a pause/stop breaks out promptly as an
    ``asyncio.CancelledError`` rather than waiting out the retries.

    Every failed attempt is classified (:func:`tg_downloader.errors.classify`)
    and written to the run log. A **rate limit** (flood wait) is *not* retried
    here — it's raised at once as :class:`RateLimited` so the caller can pause the
    whole batch, since retrying locally would just hammer a throttled server. A
    **lost connection** *is* retried locally first (a single connection error is
    usually a transient server-side drop that resuming the ``.part`` heals); only
    once the retries are exhausted is it raised as :class:`ConnectionLost` for the
    caller's offline gate, so a routine drop doesn't flip the light red or pause
    the batch. ``on_error(cause, attempt)`` (if given) is called for each
    *retried* failure so the UI can say what's wrong ("no connection — retrying…").

    ``progress_callback(received, total)`` is called after each chunk with the
    running byte total (including whatever was already on disk); it may be sync
    or async, and may raise to abort (that's how pause/stop cancel mid-file).
    """
    target = item.target_path(download_dir)
    part = item.part_path(download_dir)
    target.parent.mkdir(parents=True, exist_ok=True)

    message = await client.get_messages(item.chat_id, ids=item.msg_id)
    if message is None or not message.media:
        raise RuntimeError("message no longer available")

    total = item.size or (message.file.size if message.file else 0)

    # Split a single file across several connections when asked and the size is
    # known (parallel needs a part count); fall back to the one-connection stream
    # for size-unknown media or if the media has no downloadable location.
    use_parallel = connections > 1 and total > 0
    if use_parallel:
        try:
            dc_id, _loc = utils.get_input_location(message)
            use_parallel = dc_id is not None
        except (TypeError, ValueError, AttributeError):
            use_parallel = False

    attempt = 0
    while True:
        try:
            if use_parallel:
                await _stream_to_part_parallel(
                    client, message, part, total, progress_callback, connections, cancelled
                )
            else:
                await _stream_to_part(client, message, part, total, progress_callback)
            os.replace(part, target)
            return target
        except asyncio.CancelledError:
            raise  # pause/stop — don't retry
        except Exception as exc:
            if cancelled and cancelled():
                raise asyncio.CancelledError() from None
            cause = classify(exc)
            log.warning(
                "Download failed [%s] %s (attempt %d/%d) — %s: %s",
                cause.kind,
                item.name,
                attempt + 1,
                retries + 1,
                type(exc).__name__,
                exc,
            )
            if cause.kind == "rate_limit":
                # Coordinated globally by the caller — surface it immediately.
                raise RateLimited(cause.retry_after, cause.detail, cause.label) from exc
            if on_error is not None:
                on_error(cause, attempt + 1)
            attempt += 1
            if attempt > retries:
                log.error(
                    "Giving up on %s after %d attempts (%s)", item.name, attempt, cause.detail
                )
                if cause.kind == "no_connection":
                    # Only now — after local retries couldn't ride it out — treat it
                    # as a real outage and hand it to the caller's offline gate
                    # (pause the batch, reconnect, resume this same file). A single
                    # connection error is usually a *transient server-side drop*
                    # (Telegram recycling a download connection); retrying locally,
                    # resuming the .part, heals that without a red light / global
                    # pause. Escalating only on persistence is what tells the two
                    # apart.
                    raise ConnectionLost(cause.detail, cause.label) from exc
                raise  # give up — the caller marks it "error" / sets it aside
            # Back off, but bail out fast if we're asked to stop meanwhile.
            for _ in range(min(2**attempt, 30)):
                await asyncio.sleep(1)
                if cancelled and cancelled():
                    raise asyncio.CancelledError() from None
