"""The async worker and the pause/stop protocol.

The download loop in app.py is hard to test directly (it needs Telethon and a
live session), so the pause/stop tests exercise the same control flow against a
stub. If you change `App._download`, mirror the change in `PausableJob` here.

The queue-scheduling half of the loop, by contrast, lives in
`worker.run_download_pool` — Telethon-free — so those tests drive the real
function `App._download` calls, no stub to keep in sync.
"""

import asyncio
import threading
import time

import pytest

from tg_downloader.worker import AsyncWorker, run_download_pool

SETTLE = 0.15  # generous slack so the tests aren't flaky on a loaded machine


@pytest.fixture
def worker():
    w = AsyncWorker()
    yield w
    w.stop()


class PausableJob:
    """Mirrors the control flow of App._download: check stop, await pause,
    do a unit of work, repeat."""

    def __init__(self):
        self.stop = False
        self.ev: asyncio.Event | None = None
        self.done: list[int] = []

    async def run(self, n: int):
        self.ev = asyncio.Event()
        self.ev.set()  # set == not paused
        for i in range(n):
            if self.stop:
                break
            await self.ev.wait()
            if self.stop:
                break
            await asyncio.sleep(0.02)  # stands in for downloading a file
            self.done.append(i)

    def shutdown(self, worker: AsyncWorker, fut) -> None:
        """Wind the job down so the loop isn't torn down mid-task."""
        self.stop = True
        if self.ev is not None:
            worker.call(self.ev.set)
        fut.result(timeout=5)


def test_worker_runs_coroutines_off_the_main_thread(worker):
    async def add(a, b):
        return a + b

    assert worker.submit(add(2, 3)).result(timeout=5) == 5
    assert worker.thread is not threading.current_thread()
    assert worker.thread.daemon


def test_pause_halts_progress(worker):
    job = PausableJob()
    fut = worker.submit(job.run(100))
    time.sleep(0.1)

    worker.call(job.ev.clear)  # PAUSE
    time.sleep(SETTLE)  # let the in-flight item finish
    settled = len(job.done)
    time.sleep(0.3)

    assert len(job.done) == settled, "kept downloading while paused"
    job.shutdown(worker, fut)


def test_resume_continues(worker):
    job = PausableJob()
    fut = worker.submit(job.run(100))
    time.sleep(0.1)
    worker.call(job.ev.clear)
    time.sleep(SETTLE)
    paused_at = len(job.done)

    worker.call(job.ev.set)  # RESUME
    time.sleep(SETTLE)

    assert len(job.done) > paused_at
    job.shutdown(worker, fut)


def test_stop_ends_the_run_early(worker):
    job = PausableJob()
    fut = worker.submit(job.run(1000))
    time.sleep(0.1)

    job.stop = True
    worker.call(job.ev.set)
    fut.result(timeout=5)

    assert len(job.done) < 1000


def test_stop_while_paused_does_not_deadlock(worker):
    """The regression this guards: a worker parked on `await ev.wait()` never
    sees the stop flag unless someone sets the event. App.on_stop does exactly
    that — if it ever stops doing so, this test hangs and fails."""
    job = PausableJob()
    fut = worker.submit(job.run(1000))
    time.sleep(0.05)
    worker.call(job.ev.clear)  # park it
    time.sleep(SETTLE)

    job.stop = True
    worker.call(job.ev.set)  # the unblock in App.on_stop()

    fut.result(timeout=3)  # raises TimeoutError if deadlocked


def test_call_marshals_onto_the_worker_loop(worker):
    seen = {}

    def note():
        seen["thread"] = threading.current_thread()

    worker.call(note)
    time.sleep(SETTLE)
    assert seen["thread"] is worker.thread


# --- run_download_pool: one priority queue, incomplete-before-fresh ---------
# This is the real function App._download drives (not a stub), so these lock in
# the simplified contract: one queue, one order — urgent, then incomplete
# (oldest first), then fresh — with no per-lane worker split and no partial cap.

# Lanes, mirroring app.LANE_URGENT / LANE_RESUME / LANE_FRESH (kept local so the
# pool tests stay Telethon-free — importing app would drag tkinter in).
URGENT, RESUME, FRESH = 0, 1, 2


class Filler:
    """Mirrors App._enqueue: push (lane, iid) onto a shared priority queue with a
    monotonic seq that preserves insertion order within a lane."""

    def __init__(self):
        self.q: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self.seq = 0
        self.front_seq = 0

    def put(self, lane, iid, front=False):
        if front:  # mirrors App._enqueue(front=True): head of the lane
            self.front_seq -= 1
            seq = self.front_seq
        else:
            self.seq += 1
            seq = self.seq
        self.q.put_nowait(((lane, seq), iid))


def _fill(pairs):
    f = Filler()
    for lane, iid in pairs:
        f.put(lane, iid)
    return f


def test_incomplete_files_drain_before_fresh():
    """One worker empties the RESUME lane before it touches a fresh file, and the
    urgent lane jumps ahead of both."""

    async def go():
        done: list[str] = []
        f = _fill([(RESUME, "r1"), (RESUME, "r2"), (FRESH, "f1"), (URGENT, "U")])

        async def download_one(iid):
            done.append(iid)

        await run_download_pool(f.q, 1, download_one=download_one)
        return done

    assert asyncio.run(go()) == ["U", "r1", "r2", "f1"]


def test_every_item_completes_across_the_pool():
    """With several workers and only resumes, every item runs (nothing idles or
    is dropped) — order is unconstrained once files run in parallel."""

    async def go():
        done: list[str] = []

        async def download_one(iid):
            await asyncio.sleep(0)  # yield so all four workers get a turn
            done.append(iid)

        f = _fill([(RESUME, f"r{i}") for i in range(5)])
        await run_download_pool(f.q, 4, download_one=download_one)
        return done

    assert sorted(asyncio.run(go())) == [f"r{i}" for i in range(5)]


def test_requeued_file_jumps_ahead_of_fresh_files():
    """A file re-queued mid-download (the rate-limit path) is pushed back onto the
    RESUME lane, so it finishes before an untouched fresh file is started."""

    async def go():
        done: list[str] = []
        seen: set[str] = set()
        f = _fill([(FRESH, "A"), (FRESH, "B")])

        async def download_one(iid):
            if iid == "A" and iid not in seen:
                seen.add(iid)
                f.put(RESUME, iid)  # stand in for a rate-limit re-queue
                return
            done.append(iid)

        await run_download_pool(f.q, 1, download_one=download_one)
        return done

    # Without RESUME-priority the order would be ["B", "A"].
    assert asyncio.run(go()) == ["A", "B"]


def test_rate_limited_file_resumes_before_other_resumes():
    """A rate-limited file is re-queued at the *head* of the RESUME lane, so with
    one worker the same file resumes — not another already-queued resume. A plain
    re-queue (monotonic seq) would send it to the back of the lane and pick r2."""

    async def go():
        done: list[str] = []
        seen: set[str] = set()
        f = _fill([(RESUME, "r1"), (RESUME, "r2")])

        async def download_one(iid):
            if iid == "r1" and iid not in seen:
                seen.add(iid)
                f.put(RESUME, iid, front=True)  # stand in for a rate-limit re-queue
                return
            done.append(iid)

        await run_download_pool(f.q, 1, download_one=download_one)
        return done

    # r1 parks on the cooldown and resumes first; only then does r2 start.
    assert asyncio.run(go()) == ["r1", "r2"]


def test_stop_drains_the_pool_promptly():
    """stop() is polled at the top of each worker loop, so a stop request ends
    the run without churning through the rest of the queue."""

    async def go():
        done: list[str] = []
        flag = {"stop": False}

        async def download_one(iid):
            done.append(iid)
            if len(done) >= 3:
                flag["stop"] = True

        f = _fill([(RESUME, f"r{i}") for i in range(100)])
        await run_download_pool(f.q, 1, download_one=download_one, stop=lambda: flag["stop"])
        return done

    assert len(asyncio.run(go())) == 3


def test_partials_in_flight_never_exceed_the_worker_count():
    """No partial cap any more: incomplete-first ordering already bounds the live
    `.part` count at the number of workers, since a fresh file is opened only
    when a worker is free and no incomplete work is waiting."""

    async def go():
        inflight = 0
        peak = 0

        async def download_one(iid):
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            await asyncio.sleep(0.01)
            inflight -= 1

        f = _fill([(FRESH, f"f{i}") for i in range(20)])
        await run_download_pool(f.q, 4, download_one=download_one)
        return peak

    assert asyncio.run(go()) == 4  # never more partials than workers


def test_mixed_backlog_and_fresh_all_completes():
    """A mixed set of resumes and fresh files all finishes — no deadlock."""

    async def go():
        done: list[str] = []

        async def download_one(iid):
            await asyncio.sleep(0)
            done.append(iid)

        f = _fill([(RESUME, f"r{i}") for i in range(6)] + [(FRESH, f"F{i}") for i in range(6)])
        await run_download_pool(f.q, 8, download_one=download_one)
        return done

    expected = [f"r{i}" for i in range(6)] + [f"F{i}" for i in range(6)]
    assert sorted(asyncio.run(go())) == sorted(expected)
