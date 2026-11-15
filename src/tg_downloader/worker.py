"""Background asyncio thread.

Tkinter owns the main thread and blocks in ``mainloop()``; Telethon needs a
running asyncio event loop. Rather than fight that, we run a second thread with
its own loop and marshal work across.

Rules of the road:

* Telethon calls only ever happen on the worker loop.
* Tkinter calls only ever happen on the main thread (via ``App.post``).
* Anything mutating an ``asyncio`` primitive from the UI thread must go
  through ``loop.call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from concurrent.futures import Future
from typing import Any


class AsyncWorker:
    """Owns a private event loop running on a daemon thread."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, name="tg-async-worker", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro: Coroutine[Any, Any, Any]) -> Future:
        """Schedule a coroutine on the worker loop from any thread."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def call(self, fn, *args) -> None:
        """Run a plain callable on the worker loop (e.g. Event.set/clear)."""
        self.loop.call_soon_threadsafe(fn, *args)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)


async def run_download_pool(
    queue: asyncio.PriorityQueue,
    concurrency: int,
    *,
    download_one,
    wait_ready=None,
    stop=None,
) -> None:
    """Drain a single priority queue with a pool of ``concurrency`` workers.

    The queue holds ``(priority, iid)`` pairs and a lower ``priority`` is served
    first. ``App._download`` fills it so one simple order falls out: a "Download
    now" file jumps to the very front, then every incomplete file (a ``.part`` on
    disk, oldest started first) finishes before any untouched file is started,
    then fresh files in queue order. A rate-limited file re-queued mid-run
    rejoins the incomplete group, so it still comes back ahead of fresh work. The
    pool itself is dumb — it just hands whichever item sorts next to
    ``download_one(iid)`` — which is the whole point of the simplification.

    Each worker loops: check ``stop`` → pop the next item (exit once the queue is
    empty) → ``wait_ready`` (the user-pause / rate-limit-cooldown gate) → check
    ``stop`` again → download. A re-queue happens synchronously inside
    ``download_one`` *before* it returns, so the worker that re-queued always
    sees the item on its next turn: the pool never drains while work remains.

    ``stop()`` (default: never) is polled at the top of each loop and again after
    ``wait_ready`` so a stop request empties the pool promptly. ``wait_ready()``
    (default: no-op) parks a worker before each download.
    """
    stop = stop or (lambda: False)

    async def worker() -> None:
        while not stop():
            try:
                _, iid = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if wait_ready is not None:
                await wait_ready()
            if stop():
                return
            await download_one(iid)

    await asyncio.gather(*(worker() for _ in range(concurrency)))
