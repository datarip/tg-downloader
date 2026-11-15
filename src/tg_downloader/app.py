"""Tkinter GUI — three tabs: Files, Channels, Settings.

Threading model (see worker.py): Tk on the main thread, Telethon on the worker
loop, all cross-thread UI updates funnelled through ``App.post``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import queue
import random
import shutil
import threading
import time
import tkinter as tk
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import __version__
from .config import (
    ENV_STORED,
    ENV_VARS,
    SESSION_PATH,
    apply_to_environ,
    env_overrides,
    load_config,
    save_config,
    to_env_values,
    write_env_file,
)
from .errors import (
    ConnectionLost,
    RateLimited,
    classify,
    configure_logging,
    connection_state,
    log,
)
from .filtering import STATUS_CHOICES, STATUS_LABELS, FileFilter, status_category, status_note
from .filters import human_duration, human_size, parse_extensions
from .models import FileItem, dedupe, download_dir_problem, split_resume_first
from .portable import (
    export_bundle,
    import_bundle,
    merge_channels,
    merge_imported_scans,
    read_bundle,
    write_bundle,
)
from .store import ChannelScan, load_scans, merge_scan, save_scans
from .telegram import (
    build_client,
    check_internet,
    connect_if_authorized,
    download_item,
    downloaded_bytes,
    ensure_connected,
    is_content_protected,
    iter_matching_files,
    latest_message_id,
    part_index_path,
    probe_endpoint,
    telegram_endpoint,
)
from .worker import AsyncWorker, run_download_pool

COMPLETED = "completed"  # finished this run, or the exact file is already on disk
ON_DISK = "on disk"  # a same name+size file is already present — skipped for safety
MOVED = "moved"  # finished once, but the file has left the folder

# Statuses that mean "don't download this again" (a moved file may be re-added)
SETTLED = (COMPLETED, ON_DISK)

# Rate-limit cooldown: how long to wait when Telegram doesn't name a duration.
# We use the official API, which hands back an explicit retry_after on a real
# flood wait, so there's no need to escalate — we simply honour that duration
# when given, else fall back to a short fixed retry and try again. A throttle is
# a *wait*, not a failure, so a file is never given up on for it: the .part grows
# on every attempt and the file is always re-queued to retry.
RATE_LIMIT_DEFAULT = 5.0

# JDownloader-style auto-start: at launch, if there's pending work, a centred
# countdown modal ticks down from this many seconds and then presses Start on its
# own — unless the user cancels it first.
AUTOSTART_SECONDS = 5

# Auto-restart after a mid-run outage. When a run ends with files still queued
# (a systemic no-connection failure keeps them ticked — e.g. a nightly router
# reboot knocked the link out), wait for the connection to come back and press
# Start again on its own, so an overnight run heals itself. Bounded: after this
# many consecutive restarts that download *nothing* we stop and leave it to a
# manual Start, so a permanent failure can't spin forever.
AUTO_RESTART_MAX_STALLED = 5
AUTO_RESTART_POLL = 10.0  # seconds between connection checks while waiting
AUTO_RESTART_SETTLE = 3.0  # let a freshly-restored link settle before retrying

# How often the connectivity monitor actively TCP-probes Telegram (and a public
# host) to keep the light honest and drive the offline gate. is_connected()
# reflects intent, not the live socket, so it can't be trusted for this.
CONNECTIVITY_POLL = 2.5

# Consecutive failed DC probes before the monitor declares an outage. A fresh TCP
# connect to the DC times out intermittently on a throttled account or a link
# saturated by parallel downloads — even while the established download senders
# keep flowing — so one miss must not park the batch and paint the light red.
# Requiring several in a row filters that blip while still catching a genuine
# outage within a handful of poll intervals (the .part is kept the whole time).
OFFLINE_PROBE_THRESHOLD = 3

# Download-queue lanes (lower value = served first). One priority queue, one
# order: a "Download now" file jumps the whole queue; every incomplete file
# (a `.part` on disk) finishes before any untouched file is started; fresh files
# go last, in queue order. A rate-limited file is re-queued into RESUME, so it
# rejoins the incomplete group ahead of fresh work.
LANE_URGENT, LANE_RESUME, LANE_FRESH = 0, 1, 2


# The config keys the Settings tab owns — what _pull_settings commits and what
# _commit_settings watches for changes when autosaving on field-leave.
_SETTINGS_KEYS = (
    "api_id",
    "api_hash",
    "download_dir",
    "extensions",
    "scan_limit",
    "download_concurrency",
    "download_connections",
    "flood_sleep_threshold",
)


class _EntryHistory:
    """Undo/redo history for a single Entry widget.

    Tk gives ``Text`` a built-in undo stack but leaves ``Entry`` without one.
    This records committed snapshots of the entry's ``(text, caret)`` and
    coalesces runs of typing into one undo unit — breaking on whitespace, on a
    switch between inserting and deleting, and after a short idle gap — so one
    Ctrl-Z rolls back a word, not a single letter. Attached lazily per widget by
    :meth:`App._install_entry_undo`.
    """

    COALESCE_GAP = 0.6  # seconds; a longer pause between edits starts a fresh unit

    def __init__(self, widget: tk.Entry) -> None:
        self.w = widget
        val = widget.get()
        self._undo: list[tuple[str, int]] = [(val, self._caret())]
        self._redo: list[tuple[str, int]] = []
        self._prev = val
        self._t = 0.0
        self._kind = ""  # "ins" | "del" | "rep" — the last edit's flavour
        self._applying = False  # guard so our own delete/insert doesn't re-record

    def _caret(self) -> int:
        try:
            return int(self.w.index("insert"))
        except tk.TclError:
            return len(self.w.get())

    def record(self) -> None:
        """Snapshot the entry after a keystroke changed it (bound to KeyRelease)."""
        if self._applying:
            return
        val = self.w.get()
        if val == self._prev:
            return  # navigation key, modifier, or our own undo/redo — nothing new
        caret = self._caret()
        now = time.monotonic()
        if len(val) > len(self._prev):
            kind = "ins"
        elif len(val) < len(self._prev):
            kind = "del"
        else:
            kind = "rep"
        # Start a new undo unit at a natural boundary; otherwise fold this edit
        # into the current one so a whole word undoes at once.
        boundary = (
            now - self._t > self.COALESCE_GAP
            or kind != self._kind
            or (kind == "ins" and caret > 0 and val[caret - 1 : caret].isspace())
        )
        state = (val, caret)
        if boundary:
            self._undo.append(state)
        else:
            self._undo[-1] = state
        self._redo.clear()
        self._prev, self._t, self._kind = val, now, kind

    def undo(self) -> None:
        if len(self._undo) <= 1:
            return  # nothing before the baseline
        self._redo.append(self._undo.pop())
        self._apply(self._undo[-1])

    def redo(self) -> None:
        if not self._redo:
            return
        state = self._redo.pop()
        self._undo.append(state)
        self._apply(state)

    def _apply(self, state: tuple[str, int]) -> None:
        text, caret = state
        self._applying = True
        try:
            self.w.delete(0, "end")
            self.w.insert(0, text)
            self.w.icursor(caret)
        finally:
            self._applying = False
        # Reset the coalescing window so the next typed char starts a fresh unit.
        self._prev, self._t, self._kind = text, 0.0, ""


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("TG Download Manager")
        self.minsize(820, 500)
        # Open stretched across the whole screen (not fullscreen mode) so the
        # file/channel lists get the full estate. Fall back to a sane default.
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{sw}x{sh}+0+0")

        # Window icon, from packaging/icons when running from source and from
        # _MEIPASS/assets in a frozen build. Optional on purpose: a missing asset
        # must never stop the app from starting.
        self._icon: tk.PhotoImage | None = None
        try:
            from .resources import asset_path

            self._icon = tk.PhotoImage(file=str(asset_path("tdm.png")))
            self.iconphoto(True, self._icon)  # the reference must outlive this call
        except Exception:
            pass

        configure_logging()  # log the whole run to ~/.tg_downloader/tg_downloader.log
        log.info("App starting")

        self.cfg = load_config()
        # Keys the environment supplies. Those in ENV_STORED we own — editing them
        # here rewrites the .env. Anything else set in the env was set by hand
        # (or exported in a shell), so we show it read-only rather than fight it.
        self.env_keys = set(env_overrides())
        # Snapshot *before* persist() starts writing to os.environ itself: a var
        # exported in the shell outranks the file, so an edit here couldn't win.
        self.shell_vars = {v for v in ENV_VARS.values() if v in os.environ}
        self.worker = AsyncWorker()
        self.client = None
        self.ui_queue: queue.Queue = queue.Queue()

        self.items: dict[str, FileItem] = {}
        self.scans = load_scans()  # cached file lists, keyed by str(chat_id)
        self.state = "idle"  # idle | running | paused | waiting (for reconnect)
        # Launch auto-start countdown: one-shot (only the startup connect arms it),
        # and _autostart_after holds the pending after() tick id so a cancel can
        # stop the clock.
        self._autostart_done = False
        self._autostart_after: str | None = None
        self._pause_event: asyncio.Event | None = None
        self._stop_flag = False
        # Auto-restart after a mid-run outage (e.g. the nightly router reboot):
        # _reconnect_gen is a monotonic token — every arm bumps it, and a manual
        # Start/Stop bumps it too, so a pending waiter that sees a stale token
        # bails instead of firing a duplicate run. _auto_restart_attempts counts
        # consecutive zero-progress restarts, capped at AUTO_RESTART_MAX_STALLED.
        self._reconnect_gen = 0
        self._auto_restart_attempts = 0
        # Global offline gate: set by the connectivity monitor (and, for instant
        # reaction, by a worker that hits ConnectionLost) while Telegram is
        # unreachable. Every download worker parks in _wait_until_ready until it
        # clears, so a mid-run outage stalls the batch in place — resuming the
        # same file — instead of churning every queued file into an error.
        self._offline = False
        # Consecutive failed DC probes seen by the connectivity monitor. A single
        # miss is a blip — opening a *fresh* socket to the DC routinely times out on
        # a throttled account or a saturated link while the live downloads flow
        # fine — so we only declare an outage after OFFLINE_PROBE_THRESHOLD in a row.
        self._probe_failures = 0
        # Set (to the reason) when a worker hits a local disk fault — the download
        # folder unmounted or turned read-only mid-run. Systemic, not this file's
        # fault, so it stops the run rather than letting every remaining file fail
        # in turn and get set aside. Cleared at the start of each run.
        self._storage_fault = ""
        self._closing = False  # set on window close so the monitor loop exits
        # Per-file download controls (right-click ▸ Pause / Stop / Download now).
        # These live *alongside* the batch-wide _pause_event/_stop_flag: an iid in
        # _file_pause parks just that one download, an iid in _file_stop cancels it
        # (keeping the .part). _active_iids is every file a worker is currently
        # downloading — the guard that stops a "Download now" from opening a second
        # copy of a file already in flight (which would corrupt the shared .part).
        # All three are read/written only on the worker loop, so UI-thread handlers
        # mutate them via worker.call. _dl_queue is the run's single priority queue
        # (set while a run is live); _enqueue pushes onto it, _dl_seq keeps items in
        # insertion order within a lane. _dl_front_seq decrements so a rate-limited
        # re-queue lands at the *head* of its lane (the same file resumes, rather
        # than a different same-lane file being picked ahead of it).
        self._file_pause: set[str] = set()
        self._file_stop: set[str] = set()
        self._active_iids: set[str] = set()
        self._dl_queue: asyncio.PriorityQueue | None = None
        self._dl_seq = 0
        self._dl_front_seq = 0
        # Global rate-limit cooldown: a monotonic deadline every worker parks
        # behind while a throttled file waits out its retry_after.
        self._rate_until = 0.0
        # First worker out of a cooldown resumes with no stagger (throughput ramps
        # instantly); the rest stagger. Reset to False each time the deadline moves.
        self._rate_lead_taken = False
        self._account_name = ""  # filled in once we know who we're logged in as
        self._sort = (None, False)  # (column, reverse) for the Files tree
        self._client_lock = None  # asyncio.Lock, made lazily on the worker loop
        self._bars: dict[str, tk.Canvas] = {}  # iid -> green progress-bar overlay
        self._bar_active: set[str] = set()  # iids that currently want a bar
        self._done_count = 0
        self._dl_start = 0.0  # time.monotonic when the current run began
        self._dl_bytes = 0  # network bytes downloaded this run (for the ETA)
        # Windowed speed meter: last (_dl_bytes, monotonic) sample so each tick can
        # report bytes-per-second over the interval since the previous tick, rather
        # than the run-long average the ETA uses.
        self._speed_bytes = 0
        self._speed_t = 0.0
        self._speed_now = 0.0  # last windowed bytes/s; 0 ⇒ stalled (throttle/cap)
        # Per-file speed, measured the same accumulator way as the batch meter (not
        # a 1s window inside on_progress): the worker records each file's cumulative
        # recv here, and _update_file_speeds diffs it on the _tick_counter timer.
        # Timer-driven so a short throttle (bytes briefly stop) dips the number
        # instead of blanking it, and it survives on_progress not firing every 1s.
        self._file_recv: dict[str, int] = {}  # iid -> cumulative recv (worker writes)
        self._file_spd: dict[str, tuple[int, float]] = {}  # iid -> last (recv, t) sample

        self._build_ui()
        self._load_filters()  # restore last session's column filters before first paint
        self._load_view()  # restore last session's column order + widths
        self._load_items_from_scans()  # show last session's list without rescanning
        self._set_light(False)
        self.after(100, self._drain_queue)
        self.after(1000, self._tick_counter)  # live counts/size/free while downloading
        self.worker.submit(self._startup_connect())  # go green at launch if logged in
        self.worker.submit(self._connectivity_monitor())  # keep the light honest
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def _locked(self, key: str) -> bool:
        """True when editing this field here couldn't actually take effect.

        Two cases: the variable is exported in the shell (it outranks our file),
        or it's an env-supplied key we don't own (ENV_STORED is what we write).
        Both mean a Save would appear to work and change nothing — so the field
        is shown read-only instead. Keys that aren't env-managed at all (e.g.
        ``download_concurrency``) are never locked.
        """
        if key not in ENV_VARS:
            return False
        return ENV_VARS[key] in self.shell_vars or (
            key in self.env_keys and key not in ENV_STORED
        )

    def persist(self) -> None:
        """Save everything: ENV_STORED keys to the .env, the rest to config.json.

        Call this instead of ``save_config`` — on its own, ``save_config`` would
        silently drop the credentials and channels, because it refuses to write
        env-managed keys into config.json. This is the other half of that deal.

        ``apply_to_environ`` matters for more than tidiness: ``env_overrides``
        reads ``os.environ`` ahead of the file, so without it a value we just
        wrote would stay shadowed by the stale one for the rest of the session.
        """
        values = to_env_values(self.cfg, ENV_STORED)
        write_env_file(values)
        apply_to_environ(values)
        save_config(self.cfg)
        self.env_keys = set(env_overrides())

    def _load_items_from_scans(self) -> None:
        """Rebuild the file list from the cache, re-deriving state from disk.

        Read from disk so the cache is never blindly trusted:

        - file present → **completed** (unticked, 100%);
        - we finished it before but it's gone from the folder → **moved**;
        - the app set it aside on a past failure (``set_aside``) → **skipped**,
          with the reason kept in the Error column and the ``.part`` left on disk,
          so it waits for a manual re-tick instead of a leftover ``.part``
          re-deriving it back to "incomplete" and quietly rejoining the queue;
        - a leftover ``.part`` → **incomplete**, progress restored, still ticked
          so it re-queues and resumes;
        - otherwise → fresh.

        Ticks are otherwise preserved.
        """
        self.items.clear()
        dl = self.cfg["download_dir"]
        for i, f in enumerate(f for s in self.scans.values() for f in s.files):
            part = f.part_path(dl)
            # downloaded_bytes honours the parallel .idx sidecar (a preallocated
            # .part's size isn't its byte count); the gate stays "a .part exists".
            part_pct = (
                min(99, int(downloaded_bytes(part, f.size) * 100 / f.size))
                if part.exists() and f.size
                else -1
            )
            if f.target_path(dl).exists():
                f.status, f.selected, f.progress = COMPLETED, False, 100
                f.downloaded, f.set_aside = True, ""  # disk truth clears a stale marker
            elif f.downloaded:
                f.status, f.selected, f.progress, f.set_aside = MOVED, False, -1, ""
            elif f.set_aside:
                # Keep it set aside: skipped, reason in the Error column, .part kept.
                # ``selected`` is left as saved (False for a genuine set-aside; True
                # if the user re-ticked it to retry), so this doesn't undo a re-tick.
                f.status, f.progress = f"error: {f.set_aside}", part_pct
            elif part_pct >= 0:
                f.status, f.progress = "incomplete", part_pct
            else:
                f.status, f.progress = "", -1
            self.items[f"i{i}"] = f
        self._refresh_tree()

    def _persist_scans(self) -> None:
        """Fold the live file list (with current ticks) back into the cache, save.

        Channels whose files are all gone are kept anyway — they still carry the
        watermark and the discard tombstones, which must outlive an empty list.
        """
        by_chat: dict[str, list[FileItem]] = {}
        for it in self.items.values():
            by_chat.setdefault(str(it.chat_id), []).append(it)
        for cid, scan in self.scans.items():
            scan.files = by_chat.pop(cid, [])
            if scan.files:
                scan.last_msg_id = max(scan.last_msg_id, max(f.msg_id for f in scan.files))
                scan.title = scan.files[0].chat_title or scan.title
        for cid, files in by_chat.items():  # defensive: an item with no channel
            self.scans[cid] = ChannelScan(
                chat_id=files[0].chat_id,
                title=files[0].chat_title,
                last_msg_id=max(f.msg_id for f in files),
                files=files,
            )
        save_scans(self.scans)

    def _tombstone(self, items) -> None:
        """Record msg_ids as discarded so no future scan re-adds them."""
        for it in items:
            cid = str(it.chat_id)
            scan = self.scans.get(cid)
            if scan is None:
                scan = ChannelScan(chat_id=it.chat_id, title=it.chat_title)
                self.scans[cid] = scan
            scan.discarded.add(it.msg_id)

    # ------------------------------------------------------------------
    # cross-thread plumbing
    # ------------------------------------------------------------------

    def post(self, fn) -> None:
        """Queue a callable to run on the UI thread."""
        self.ui_queue.put(fn)

    def _drain_queue(self) -> None:
        try:
            while True:
                self.ui_queue.get_nowait()()
        except queue.Empty:
            pass
        except Exception as e:  # a bad callback shouldn't kill the pump
            print("UI callback error:", e)
        self.after(100, self._drain_queue)

    def _ask_blocking(self, prompt: str, secret: bool = False) -> str:
        """Called from the worker thread; blocks it until the user answers."""
        box: dict = {}
        ev = threading.Event()

        def do():
            box["v"] = simpledialog.askstring(
                "Telegram Login", prompt, show="*" if secret else None, parent=self
            )
            ev.set()

        self.post(do)
        ev.wait()
        return box.get("v") or ""

    async def ask(self, prompt: str, secret: bool = False) -> str:
        """Async wrapper so Telethon's callbacks can prompt via the GUI."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._ask_blocking, prompt, secret)

    def set_status(self, text: str) -> None:
        self.post(lambda: self.status_var.set(text))

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=(8, 0))

        self.tab_files = ttk.Frame(nb)
        self.tab_channels = ttk.Frame(nb)
        self.tab_settings = ttk.Frame(nb)
        nb.add(self.tab_files, text="  Files  ")
        nb.add(self.tab_channels, text="  Channels  ")
        nb.add(self.tab_settings, text="  Settings  ")

        self._build_files_tab()
        self._build_channels_tab()
        self._build_settings_tab()

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=6)
        # Always-visible connection light, mirrored in the Settings tab.
        self.conn_dot = tk.Label(bar, text="●")
        self.conn_dot.pack(side="left", padx=(0, 6))
        self.status_var = tk.StringVar(value="Not connected — add your API keys in Settings.")
        ttk.Label(bar, textvariable=self.status_var).pack(side="left")
        self.progress = ttk.Progressbar(bar, length=220, mode="determinate")
        self.progress.pack(side="right")
        # ETA sits just to the left of the bar (packed after → appears left of it).
        self.eta_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.eta_var, foreground="#8a8a8a").pack(
            side="right", padx=(0, 8)
        )
        # Live download speed, left of the ETA. Windowed (updated every tick from
        # _tick_counter), so it tracks the current connection rate, not the average.
        self.speed_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.speed_var, foreground="#8a8a8a").pack(
            side="right", padx=(0, 8)
        )

        self._install_entry_undo()

    # -- Text-entry undo/redo ------------------------------------------

    def _install_entry_undo(self) -> None:
        """Give every Entry/ttk.Entry in the app Ctrl/⌘-Z undo + Ctrl/⌘-Y redo.

        Tk builds an undo stack into ``Text`` but leaves ``Entry`` without one, so
        a mistype in the API-ID field (or any other input) had nothing to roll
        back. These are *class-level* bindings, so they cover entries created
        later too — the Settings fields, the Files-tab filter popups, and the
        ``simpledialog`` login/2FA prompts — without touching each call site. A
        per-widget :class:`_EntryHistory` is created on first focus (capturing the
        pristine value) and updated on every key release.
        """

        def history(widget: tk.Entry) -> _EntryHistory:
            hist = getattr(widget, "_tg_history", None)
            if hist is None:
                hist = _EntryHistory(widget)
                widget._tg_history = hist
            return hist

        def on_focus(event) -> None:
            history(event.widget)  # capture the baseline before any edit

        def on_key(event) -> None:
            history(event.widget).record()

        def on_undo(event) -> str:
            history(event.widget).undo()
            return "break"

        def on_redo(event) -> str:
            history(event.widget).redo()
            return "break"

        for cls in ("Entry", "TEntry"):
            self.bind_class(cls, "<FocusIn>", on_focus, add="+")
            self.bind_class(cls, "<KeyRelease>", on_key, add="+")
            for seq in ("<Control-z>", "<Command-z>"):
                self.bind_class(cls, seq, on_undo, add="+")
            for seq in (
                "<Control-y>",
                "<Command-y>",
                "<Control-Shift-Z>",
                "<Command-Shift-Z>",
            ):
                self.bind_class(cls, seq, on_redo, add="+")

    # -- Tab 1: Files --------------------------------------------------

    def _build_files_tab(self) -> None:
        f = self.tab_files

        top = ttk.Frame(f)
        top.pack(fill="x", padx=6, pady=6)
        ttk.Button(top, text="Update Scan", command=self.on_update_scan).pack(side="left")
        ttk.Button(top, text="Full Rescan", command=self.on_full_rescan).pack(
            side="left", padx=(4, 0)
        )
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=8)
        # Global action (operates on the whole list), so it's a button, not a
        # per-row right-click item.
        ttk.Button(top, text="Discard Duplicates", command=self.on_discard_duplicates).pack(
            side="left"
        )

        self._init_filter_vars()

        # Control bar first, pinned to the bottom at full width — pack it before
        # the tree so the tree fills the whole area above it, not just the left.
        ctrl = ttk.Frame(f)
        ctrl.pack(side="bottom", fill="x", padx=6, pady=6)
        self.btn_start = ttk.Button(ctrl, text="▶  Start", command=self.on_start)
        self.btn_pause = ttk.Button(
            ctrl, text="⏸  Pause", command=self.on_pause, state="disabled"
        )
        self.btn_stop = ttk.Button(ctrl, text="⏹  Stop", command=self.on_stop, state="disabled")
        for b in (self.btn_start, self.btn_pause, self.btn_stop):
            b.pack(side="left", padx=3)
        self.count_var = tk.StringVar(value="0 files")
        self.count_label = ttk.Label(ctrl, textvariable=self.count_var)
        self.count_label.pack(side="right")
        # What the *highlighted* rows add up to — sits beside the transport
        # buttons, left of the whole-list counts on the right.
        self.sel_var = tk.StringVar(value="")
        ttk.Label(ctrl, textvariable=self.sel_var, foreground="#8a8a8a").pack(
            side="left", padx=(12, 0)
        )

        mid = ttk.Frame(f)
        mid.pack(side="top", fill="both", expand=True, padx=6, pady=(0, 6))

        cols = (
            "queue",
            "name",
            "channel",
            "size",
            "progress",
            "speed",
            "eta",
            "status",
            "note",
        )
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
        self._col_titles = {
            "queue": "#",
            "name": "File",
            "channel": "Channel",
            "size": "Size",
            "progress": "Progress",
            "speed": "Speed",
            "eta": "Time Left",
            "status": "Status",
            "note": "Error",
        }
        self._default_col_widths: dict[str, int] = {}  # for "Reset to Default View"
        for c, w, anchor, stretch in (
            ("queue", 44, "e", False),
            ("name", 380, "w", True),
            ("channel", 220, "w", True),
            ("size", 90, "e", False),
            ("progress", 150, "w", False),
            ("speed", 90, "e", False),
            ("eta", 90, "e", False),
            ("status", 100, "w", False),
            ("note", 130, "w", False),
        ):
            self._default_col_widths[c] = w
            # anchor="center" centres the header content (label + leading sort
            # arrow + funnel icon) horizontally in the cell. ttk lays text and
            # image out as one block, so the funnel sits centred beside the label.
            self.tree.heading(
                c,
                text=self._col_titles[c],
                anchor="center",
                command=lambda c=c: self._sort_by(c),
            )
            self.tree.column(c, width=w, anchor=anchor, stretch=stretch)

        vs = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        # Wrap the scrollbar callback so the overlay bars follow the rows on scroll.
        self.tree.configure(yscrollcommand=lambda *a: (vs.set(*a), self._reposition_bars()))
        vs.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)

        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_selection_size())
        self.tree.bind("<space>", lambda e: self._toggle_highlighted())
        self.tree.bind("<Configure>", lambda e: self._reposition_bars())
        self.tree.bind("<Control-a>", self._select_all_rows)
        self.tree.bind("<Command-a>", self._select_all_rows)  # macOS
        self._install_multiselect(self.tree)
        # Hovering a filterable header pops a small hint that you can filter it.
        self.tree.bind("<Motion>", self._tree_header_motion, add="+")
        self.tree.bind("<Leave>", lambda e: self._hide_header_tip(), add="+")
        # A click anywhere outside an open filter panel dismisses it — the same as
        # its Close button. bind_all catches clicks on any widget (other tabs, the
        # status bar, a row); clicks inside the panel are let through. Escape closes
        # it too. Both no-op when no panel is open.
        self.bind_all("<Button-1>", self._autoclose_header_filter, add="+")
        self.bind_all("<Escape>", self._close_header_filter, add="+")
        # Press-drag a header left/right to reorder columns (the order persists as
        # part of the view state). Bound at the widget level so a completed drag can
        # `return "break"` and suppress the class-level sort-command on release.
        self._col_drag: str | None = None  # display cid ("#N") being dragged, or None
        self._col_dragging = False  # moved past the threshold → a real reorder
        self._col_drag_x = 0
        self.tree.bind("<ButtonPress-1>", self._on_header_press, add="+")
        self.tree.bind("<B1-Motion>", self._on_header_drag, add="+")
        self.tree.bind("<ButtonRelease-1>", self._on_header_release, add="+")
        # Right-click dispatches on region: a header opens that column's filter
        # dropdown, a row opens the usual context menu.
        self.tree.bind("<Button-3>", self._tree_right_click)
        if self.tk.call("tk", "windowingsystem") == "aqua":
            self.tree.bind("<Button-2>", self._tree_right_click)
            self.tree.bind("<Control-Button-1>", self._tree_right_click)
        # Mid-tone colours: dark enough for the white background, still legible
        # on the blue selected-row background.
        self.tree.tag_configure("done", foreground="#2ea043")
        self.tree.tag_configure("moved", foreground="#e0a44a")
        self.tree.tag_configure("error", foreground="#ff8a8a")
        self.tree.tag_configure("waiting", foreground="#d98a1f")  # rate-limited / retrying
        self.tree.tag_configure("skipped", foreground="#6b6b6b")  # greyed out

    # -- per-column header filters -------------------------------------
    #
    # The predicate logic is pure and lives in filtering.py; the filter state is
    # held in the Tk vars below and edited through a small dropdown popup opened
    # by right-clicking a column header (Soulseek-style), rather than a permanent
    # filter bar — that keeps the vertical space for the file list. Every var
    # re-renders the tree live, so a filter takes effect as you type/tick.

    _TEXT_MODES = ("Contains", "Does not contain")

    #: Which columns are filterable, and by what kind of popup.
    _FILTER_KINDS = {
        "name": "text",
        "channel": "text",
        "size": "num",
        "progress": "num",
        "status": "status",
    }

    def _init_filter_vars(self) -> None:
        """Create the filter Tk vars once and wire each to a live re-render."""

        def live(*_):
            # Mirror the change into cfg (memory) so it's ready to persist, then
            # re-render. The disk write happens on panel dismiss / app close.
            self._snapshot_filters()
            self._refresh_tree()

        self.f_name_mode = tk.StringVar(value=self._TEXT_MODES[0])
        self.f_chan_mode = tk.StringVar(value=self._TEXT_MODES[0])
        # The name/channel text filters are keyword *pill* lists (add with Enter,
        # remove with the ✕), not single strings. They're plain Python lists — not
        # Tk vars — because a list can't drive trace_add; add/remove call
        # _pills_changed to re-render + re-filter explicitly instead.
        self.f_name_pills: list[str] = []
        self.f_chan_pills: list[str] = []
        # The live pill-editor widgets, set while a text-column panel is open so
        # _render_pills can find them; cleared on close.
        self._pill_container: ttk.Frame | None = None
        self._pill_col: str | None = None
        self.f_size_min = tk.StringVar()
        self.f_size_max = tk.StringVar()
        self.f_prog_min = tk.StringVar()
        self.f_prog_max = tk.StringVar()
        # Key-validation for the numeric (Size MB / Progress %) filter entries:
        # reject anything that isn't a non-negative integer at the keystroke, so a
        # stray letter or a "." can never land in the box. Registered once; %P is
        # the value the edit *would* produce, "" (a cleared box) is allowed.
        self._int_vcmd = (self.register(self._is_int_entry), "%P")
        # All categories ticked by default: the Status filter is a negative
        # filter — the view shows everything until you untick a category to hide
        # it (_build_filter turns the unticked boxes into FileFilter.status_exclude).
        self.f_status = {cat: tk.BooleanVar(value=True) for cat in STATUS_CHOICES}
        self._filter_popup: tk.Frame | None = None  # in-window filter overlay
        self._header_tip: tk.Label | None = None  # lazily-made hover hint
        self._header_tip_col: str | None = None
        # A green funnel shown in a header only while that column has a live
        # filter — a clearer "this is filtered" cue than a text glyph (ttk can't
        # colour one heading's text differently from the rest). Kept as an
        # attribute so it isn't garbage-collected while a heading references it.
        self._filter_on_img = self._make_filter_icon()
        for var in (
            self.f_name_mode,
            self.f_chan_mode,
            self.f_size_min,
            self.f_size_max,
            self.f_prog_min,
            self.f_prog_max,
            *self.f_status.values(),
        ):
            var.trace_add("write", live)

    @staticmethod
    def _is_int_entry(proposed: str) -> bool:
        """Allow a numeric-filter edit only if the result is empty or all digits.

        ``proposed`` is Tk's ``%P`` — the box's would-be contents. ASCII digits
        only: ``str.isdigit`` alone accepts unicode oddballs like ``²`` that
        ``int()`` then chokes on, so guard with ``isascii``. No sign, no decimal
        point — Size (MB) and Progress (%) filters are whole numbers.
        """
        return proposed == "" or (proposed.isascii() and proposed.isdigit())

    @staticmethod
    def _make_filter_icon(
        color: str = "#2ea043", size: int = 12, pad: int = 4
    ) -> tk.PhotoImage:
        """Draw a small green funnel on a transparent ground for the header marker.

        ttk bottom-anchors a heading image on Aqua, which parks the funnel at the
        very bottom of the header. ``pad`` transparent rows *below* the funnel lift
        it back toward the vertical middle of the header.
        """
        img = tk.PhotoImage(width=size, height=size + pad)
        # Per-row [left, right] inclusive spans tracing a funnel: wide rim → stem.
        rows = {1: (1, 10), 2: (1, 10), 3: (2, 9), 4: (3, 8), 5: (4, 7)}
        rows.update({y: (5, 6) for y in range(6, 11)})  # the stem
        for y, (lo, hi) in rows.items():
            img.put(color, to=(lo, y, hi + 1, y + 1))
        return img

    def _display_columns(self) -> list[str]:
        """Column names in current on-screen order (honours drag-reordering).

        ``displaycolumns`` is ``("#all",)`` until we set an explicit order, in
        which case the order is just the definition order (``self.tree["columns"]``).
        """
        disp = self.tree["displaycolumns"]
        if not disp or tuple(disp) == ("#all",):
            return list(self.tree["columns"])
        return list(disp)

    def _col_at(self, x: int) -> str | None:
        """Map an event x-coordinate to the tree column id under it (or None)."""
        cid = self.tree.identify_column(x)  # e.g. "#3" — position among displayed cols
        try:
            return self._display_columns()[int(cid[1:]) - 1]
        except (ValueError, IndexError):
            return None

    def _header_cell_x0(self, col: str) -> int:
        """Left x (in tree pixels) of ``col``'s header cell.

        No horizontal scrollbar is wired, so the display never scrolls sideways
        and a running sum of the preceding columns' *current* widths (Tk updates
        these as stretch columns resize) matches the on-screen left edge. Columns
        are summed in on-screen order so a drag-reordered header still lines up.
        """
        x0 = 0
        for c in self._display_columns():
            if c == col:
                break
            x0 += int(self.tree.column(c, "width"))
        return x0

    # -- column drag-to-reorder ----------------------------------------
    #
    # ttk.Treeview has no built-in column reordering, so we drive it by hand
    # through ``displaycolumns``. A press on a header body (not a resize separator)
    # arms a drag; once the pointer moves past a small threshold it becomes a real
    # reorder, and the release swaps the dragged column to the pointer's column and
    # returns "break" so the header's sort command doesn't also fire.

    _DRAG_THRESHOLD = 6  # px of movement before a header press counts as a drag

    def _on_header_press(self, event):
        """Arm a possible column drag when a header body is pressed."""
        self._col_drag = None
        self._col_dragging = False
        if self.tree.identify_region(event.x, event.y) != "heading":
            return  # a row, or a resize separator — leave the default behaviour
        self._col_drag = self.tree.identify_column(event.x)  # "#N" display position
        self._col_drag_x = event.x

    def _on_header_drag(self, event):
        """Mark the press as a real drag once it moves past the threshold."""
        if self._col_drag is None:
            return
        if not self._col_dragging and abs(event.x - self._col_drag_x) >= self._DRAG_THRESHOLD:
            self._col_dragging = True
            self._hide_header_tip()
            try:
                self.tree.configure(cursor="exchange")
            except tk.TclError:
                pass

    def _on_header_release(self, event):
        """Finish a column drag: reorder to the pointer's column, suppress sort."""
        if self._col_drag is None:
            return
        dragging = self._col_dragging
        src = self._col_drag
        self._col_drag = None
        self._col_dragging = False
        if not dragging:
            return  # a plain click — let the sort command run
        try:
            self.tree.configure(cursor="")
        except tk.TclError:
            pass
        if self.tree.identify_region(event.x, event.y) == "heading":
            self._reorder_columns(src, self.tree.identify_column(event.x))
        return "break"  # don't let the release also trigger the header's sort

    def _reorder_columns(self, src_cid: str, dst_cid: str) -> None:
        """Move the column at display position ``src_cid`` to ``dst_cid`` (both "#N")."""
        order = self._display_columns()
        try:
            si, di = int(src_cid[1:]) - 1, int(dst_cid[1:]) - 1
        except ValueError:
            return
        if not (0 <= si < len(order)) or not (0 <= di < len(order)) or si == di:
            return
        order.insert(di, order.pop(si))
        self.tree["displaycolumns"] = order
        self._reposition_bars()  # progress-bar overlays follow the moved cells
        self._snapshot_view()
        try:
            save_config(self.cfg)
        except OSError:
            pass

    # -- column view state (order + widths) ----------------------------

    def _snapshot_view(self) -> None:
        """Mirror the live column order and widths into ``cfg["view"]``.

        Called after a drag-reorder and on close (to catch resized widths). Widths
        are read live from Tk, so a user-dragged column border is remembered too.
        """
        try:
            self.cfg["view"] = {
                "order": self._display_columns(),
                "widths": {c: int(self.tree.column(c, "width")) for c in self.tree["columns"]},
            }
        except tk.TclError:
            pass

    def _load_view(self) -> None:
        """Restore the saved column order and widths. Best-effort: unknown columns
        are dropped and missing ones appended in definition order, so an older or
        hand-edited config can't crash startup or lose a column."""
        saved = self.cfg.get("view") or {}
        cols = list(self.tree["columns"])
        order = saved.get("order")
        if isinstance(order, list):
            kept = [c for c in order if c in cols]
            rest = [c for c in cols if c not in kept]  # newly-added columns go last
            if kept:
                self.tree["displaycolumns"] = kept + rest
        widths = saved.get("widths")
        if isinstance(widths, dict):
            for c, w in widths.items():
                if c in cols and isinstance(w, int) and w > 0:
                    self.tree.column(c, width=w)

    def _reset_view(self) -> None:
        """Restore the code-defined column order and widths (the default layout)."""
        self.tree["displaycolumns"] = list(self.tree["columns"])
        for c, w in self._default_col_widths.items():
            self.tree.column(c, width=w)
        self._reposition_bars()  # progress-bar overlays follow the reset cells
        self.cfg["view"] = {}
        try:
            save_config(self.cfg)
        except OSError:
            pass

    def _autoclose_header_filter(self, event):
        """Dismiss an open filter dropdown when the click lands outside it.

        Bound app-wide (bind_all) so a click on any other widget closes the popup
        like its Close button does. Clicks inside the popup are ignored so ticking
        a box or picking a mode doesn't dismiss it.
        """
        win = getattr(self, "_filter_popup", None)
        if win is None:
            return
        w, root = str(event.widget), str(win)
        if w == root or w.startswith(root + "."):
            return  # inside the popup (or its combobox popdown) — leave it open
        self._close_header_filter(restore_focus=False)

    # -- header hover hint ---------------------------------------------

    def _tree_header_motion(self, event):
        """Show the filter hint while hovering a filterable header, else hide it."""
        if self.tree.identify_region(event.x, event.y) == "heading":
            col = self._col_at(event.x)
            if col in self._FILTER_KINDS:
                self._show_header_tip(col)
                return
        self._hide_header_tip()

    def _show_header_tip(self, col: str) -> None:
        """A small hint under a filterable header: click the icon or right-click.

        A plain ``tk.Label`` placed on the tree (like the progress bars), not a
        Toplevel — so it can't steal the window's focus the way the dropdown could.
        """
        if self._header_tip_col == col and self._header_tip is not None:
            return
        tip = self._header_tip
        if tip is None:
            tip = tk.Label(
                self.tree,
                background="#ffffe0",
                foreground="#333",
                relief="solid",
                borderwidth=1,
                font=("", 10),
                padx=6,
                pady=2,
            )
            self._header_tip = tip
        tip.configure(text="Right-click to filter")
        # Just below the header row: the first data row's top is the header height;
        # fall back to a typical header height when the list is empty.
        kids = self.tree.get_children()
        bbox = self.tree.bbox(kids[0]) if kids else None
        y = (bbox[1] if bbox else 22) + 1
        tip.place(x=self._header_cell_x0(col) + 2, y=y)
        tip.lift()
        self._header_tip_col = col

    def _hide_header_tip(self, *_) -> None:
        if self._header_tip is not None:
            self._header_tip.place_forget()
        self._header_tip_col = None

    def _tree_right_click(self, event):
        """Header → open that column's filter dropdown; row → context menu."""
        if self.tree.identify_region(event.x, event.y) == "heading":
            col = self._col_at(event.x)
            if col in self._FILTER_KINDS:
                self._open_header_filter(col, event)
            return "break"
        # A row: mirror _bind_context_menu's popup so the menu reflects selection.
        self._select_under_pointer(self.tree, event)
        menu = tk.Menu(self, tearoff=0)
        self._files_context(menu, event)
        if menu.index("end") is not None:
            menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def _col_filter_active(self, col: str) -> bool:
        """Whether ``col`` currently constrains anything (drives the ● marker)."""
        f = self._build_filter()
        return {
            "name": bool(f.name_contains or f.name_excludes),
            "channel": bool(f.channel_contains or f.channel_excludes),
            "size": f.size_min_mb is not None or f.size_max_mb is not None,
            "progress": f.progress_min is not None or f.progress_max is not None,
            "status": bool(f.status_exclude),
        }.get(col, False)

    def _pill_list(self, col: str) -> list[str]:
        """The backing keyword list for a text column ('name' or 'channel')."""
        return self.f_name_pills if col == "name" else self.f_chan_pills

    def _add_pill(self, col: str, text: str) -> None:
        """Add a keyword pill (case-insensitively de-duplicated); no-op if blank."""
        text = text.strip()
        if not text:
            return
        pills = self._pill_list(col)
        if any(text.lower() == p.lower() for p in pills):
            return
        pills.append(text)
        self._pills_changed(col)

    def _remove_pill(self, col: str, word: str) -> None:
        """Remove a keyword pill (the ✕ on a chip, or Backspace on empty input)."""
        pills = self._pill_list(col)
        try:
            pills.remove(word)
        except ValueError:
            return
        self._pills_changed(col)

    def _pills_changed(self, col: str) -> None:
        """Re-render the open pill panel, then persist-to-memory + re-filter.

        Mirrors the ``live`` trace used by the other filter widgets — pill lists
        can't drive ``trace_add``, so add/remove calls this by hand.
        """
        self._render_pills(col)
        self._snapshot_filters()
        self._refresh_tree()

    def _render_pills(self, col: str) -> None:
        """Rebuild the chips in the open text-filter panel from the keyword list.

        A no-op unless that column's panel is currently open (``_pill_container``
        points at a live frame for ``col``). Each pill is a small coloured chip
        with the word and a ✕ that removes it. Chips stack one per line so a long
        keyword never overflows the narrow panel.
        """
        box = self._pill_container
        if box is None or self._pill_col != col:
            return
        try:
            for child in box.winfo_children():
                child.destroy()
        except tk.TclError:
            return
        pills = self._pill_list(col)
        if not pills:
            ttk.Label(box, text="No keywords yet", foreground="grey").pack(anchor="w")
            return
        for word in pills:
            chip = tk.Frame(box, background="#2f6feb")
            tk.Label(chip, text=word, background="#2f6feb", foreground="white", padx=6).pack(
                side="left"
            )
            cross = tk.Label(
                chip,
                text="✕",
                background="#2f6feb",
                foreground="white",
                padx=5,
                cursor="hand2",
            )
            cross.pack(side="left")
            cross.bind("<Button-1>", lambda _e, w=word: self._remove_pill(col, w))
            chip.pack(side="top", anchor="w", pady=1)

    @staticmethod
    def _attach_placeholder(entry: tk.Entry, text: str) -> None:
        """Show grey placeholder ``text`` in ``entry`` whenever it's empty.

        Tk's ``Entry`` has no native placeholder, so this is the standard
        focus-in/out grey-text trick. ``entry._placeholder_on`` flags when the
        shown text is the placeholder (not a real keyword) so callers can ignore
        it on Enter.
        """
        default_fg = entry.cget("foreground") or "black"

        def show() -> None:
            entry.delete(0, "end")
            entry.insert(0, text)
            entry.configure(foreground="grey")
            entry._placeholder_on = True

        def on_in(_event) -> None:
            if getattr(entry, "_placeholder_on", False):
                entry.delete(0, "end")
                entry.configure(foreground=default_fg)
                entry._placeholder_on = False

        def on_out(_event) -> None:
            if not entry.get():
                show()

        entry.bind("<FocusIn>", on_in)
        entry.bind("<FocusOut>", on_out)
        show()

    def _open_header_filter(self, col: str, event) -> None:
        """Drop a small filter panel for ``col`` just under its header.

        It's an in-window overlay ``Frame``, not a ``Toplevel`` — deliberately. A
        separate borderless window becomes its own "key window" on macOS Aqua and
        then mishandles focus: rows stop selecting, and the panel won't dismiss
        after you tick a box (the dismissing click arrives as a window switch that
        never reaches our bindings). A child frame shares the main window's focus
        and event stream, so none of that arises and a plain outside-click closes
        it. It's placed over the tree and lifted above it.
        """
        self._close_header_filter()
        win = tk.Frame(self, background="#9a9a9a")  # 1px hairline via the padding
        self._filter_popup = win
        frame = ttk.Frame(win, padding=8)
        frame.pack(padx=1, pady=1)
        ttk.Label(frame, text=f"Filter · {self._col_titles[col]}", font=("", 11, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6)
        )

        kind = self._FILTER_KINDS[col]
        first = None
        if kind == "text":
            mode = self.f_name_mode if col == "name" else self.f_chan_mode
            ttk.Combobox(
                frame,
                textvariable=mode,
                values=self._TEXT_MODES,
                width=17,
                state="readonly",
            ).grid(row=1, column=0, columnspan=2, sticky="ew")
            # The current keywords, each a removable pill, above the input box.
            self._pill_col = col
            self._pill_container = ttk.Frame(frame)
            self._pill_container.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
            self._render_pills(col)
            # Type a keyword, press Enter to turn it into a pill. Backspace on an
            # empty box removes the last pill. We deliberately DON'T autofocus
            # (first stays None) so the "add keyword + enter" placeholder stays
            # visible until the user clicks in.
            entry = tk.Entry(frame, width=26)
            entry.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(4, 0))
            self._attach_placeholder(entry, "add keyword + enter")

            def commit(_event=None, e=entry, c=col):
                if getattr(e, "_placeholder_on", False):
                    return "break"
                self._add_pill(c, e.get())
                e.delete(0, "end")
                return "break"

            def backspace(_event, e=entry, c=col):
                if not e.get() and self._pill_list(c):
                    self._remove_pill(c, self._pill_list(c)[-1])

            entry.bind("<Return>", commit)
            entry.bind("<BackSpace>", backspace)
        elif kind == "num":
            lo = self.f_size_min if col == "size" else self.f_prog_min
            hi = self.f_size_max if col == "size" else self.f_prog_max
            unit = "MB" if col == "size" else "%"
            ttk.Label(frame, text=f"Min ({unit})").grid(row=1, column=0, sticky="w")
            first = ttk.Entry(
                frame, textvariable=lo, width=10, validate="key", validatecommand=self._int_vcmd
            )
            first.grid(row=1, column=1, sticky="ew", pady=2, padx=(6, 0))
            ttk.Label(frame, text=f"Max ({unit})").grid(row=2, column=0, sticky="w")
            ttk.Entry(
                frame, textvariable=hi, width=10, validate="key", validatecommand=self._int_vcmd
            ).grid(row=2, column=1, sticky="ew", pady=2, padx=(6, 0))
        else:  # status checkboxes
            for i, cat in enumerate(STATUS_CHOICES, start=1):
                ttk.Checkbutton(
                    frame, text=STATUS_LABELS[cat], variable=self.f_status[cat]
                ).grid(row=i, column=0, columnspan=2, sticky="w")

        btns = ttk.Frame(frame)
        btns.grid(row=99, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(
            btns, text="Clear", width=7, command=lambda: self._clear_col_filter(col)
        ).pack(side="left")
        ttk.Button(btns, text="Close", width=7, command=self._close_header_filter).pack(
            side="right"
        )

        # Place just under the header at the click, in main-window coordinates,
        # kept fully on-screen, and lifted above the tree so it isn't drawn behind
        # rows. event.*_root are screen pixels; subtract the window origin.
        win.update_idletasks()
        x = event.x_root - self.winfo_rootx()
        y = event.y_root - self.winfo_rooty()
        x = min(x, self.winfo_width() - win.winfo_reqwidth() - 4)
        win.place(x=max(0, x), y=y + 6)
        win.lift()
        if first is not None:
            first.focus_set()

    def _close_header_filter(self, *_, restore_focus: bool = True):
        win = getattr(self, "_filter_popup", None)
        if win is not None:
            self._filter_popup = None
            # The pill editor lived inside this panel; drop the stale refs so a
            # later _render_pills doesn't poke destroyed widgets.
            self._pill_container = None
            self._pill_col = None
            try:
                win.destroy()
            except tk.TclError:
                pass
            # Dismissing a panel is the natural "done adjusting" moment — persist
            # the filters now so a mid-session change survives even without a clean
            # quit (the trace already keeps cfg current in memory; write it out).
            try:
                self._snapshot_filters()
                save_config(self.cfg)
            except OSError:
                pass
            # Hand keyboard focus back to the file list. Without this the focus is
            # left orphaned on the just-destroyed popup and the main window never
            # reclaims it on macOS — so clicks, ⌘-A and space stop reaching the
            # tree until you re-activate the window by hand. Skipped when the close
            # was triggered by clicking another widget, which takes focus itself.
            if not restore_focus:
                return
            try:
                self.tree.focus_set()
            except tk.TclError:
                pass

    def _clear_col_filter(self, col: str) -> None:
        """Reset just this column's filter (the popup stays open to show it)."""
        if col == "name":
            self.f_name_pills.clear()
            self.f_name_mode.set(self._TEXT_MODES[0])
            self._pills_changed("name")
        elif col == "channel":
            self.f_chan_pills.clear()
            self.f_chan_mode.set(self._TEXT_MODES[0])
            self._pills_changed("channel")
        elif col == "size":
            self.f_size_min.set("")
            self.f_size_max.set("")
        elif col == "progress":
            self.f_prog_min.set("")
            self.f_prog_max.set("")
        elif col == "status":
            # Re-tick every category: cleared status filter = show all.
            for var in self.f_status.values():
                var.set(True)

    def _clear_filters(self) -> None:
        for var in (
            self.f_size_min,
            self.f_size_max,
            self.f_prog_min,
            self.f_prog_max,
        ):
            var.set("")
        self.f_name_pills.clear()
        self.f_chan_pills.clear()
        self._render_pills("name")
        self._render_pills("channel")
        self.f_name_mode.set(self._TEXT_MODES[0])
        self.f_chan_mode.set(self._TEXT_MODES[0])
        for var in self.f_status.values():
            var.set(True)
        self._refresh_tree()

    def _build_filter(self) -> FileFilter:
        """Snapshot the filter widgets into a pure FileFilter for _refresh_tree."""

        def num(var):
            t = var.get().strip()
            if not t:
                return None
            try:
                return float(t)
            except ValueError:  # a half-typed number constrains nothing
                return None

        name = tuple(self.f_name_pills)
        chan = tuple(self.f_chan_pills)
        name_is_contains = self.f_name_mode.get() == self._TEXT_MODES[0]
        chan_is_contains = self.f_chan_mode.get() == self._TEXT_MODES[0]
        return FileFilter(
            name_contains=name if name_is_contains else (),
            name_excludes=() if name_is_contains else name,
            channel_contains=chan if chan_is_contains else (),
            channel_excludes=() if chan_is_contains else chan,
            size_min_mb=num(self.f_size_min),
            size_max_mb=num(self.f_size_max),
            progress_min=num(self.f_prog_min),
            progress_max=num(self.f_prog_max),
            # Unticked boxes → categories to hide (negative filter). All ticked
            # → empty → hide nothing; all unticked → hide every category.
            status_exclude=frozenset(c for c, v in self.f_status.items() if not v.get()),
        )

    def _snapshot_filters(self) -> None:
        """Mirror the live filter widgets into ``cfg["filters"]`` (memory only).

        Called on every filter change so the value is ready to persist; the disk
        write is deferred to panel dismiss / app close. Status is stored as the
        *excluded* categories (what's unticked) so a category added to
        STATUS_CHOICES later simply defaults to shown.
        """
        self.cfg["filters"] = {
            "name_mode": self.f_name_mode.get(),
            "name_pills": list(self.f_name_pills),
            "chan_mode": self.f_chan_mode.get(),
            "chan_pills": list(self.f_chan_pills),
            "size_min": self.f_size_min.get(),
            "size_max": self.f_size_max.get(),
            "prog_min": self.f_prog_min.get(),
            "prog_max": self.f_prog_max.get(),
            "status_exclude": [c for c, v in self.f_status.items() if not v.get()],
        }

    def _load_filters(self) -> None:
        """Restore saved column filters into the Tk vars. Best-effort: unknown or
        wrong-typed values are ignored, so a hand-edited or older config can't crash
        startup. Setting the vars fires their trace, which re-renders the tree; the
        pill lists don't trace, but startup renders the tree once anyway."""
        saved = self.cfg.get("filters") or {}

        def restore(var, key, allowed=None):
            val = saved.get(key)
            if isinstance(val, str) and (allowed is None or val in allowed):
                var.set(val)

        def restore_pills(pills, key, legacy_key):
            val = saved.get(key)
            if isinstance(val, list):
                pills[:] = [s.strip() for s in val if isinstance(s, str) and s.strip()]
                return
            # Migrate an older single-string "*_text" filter into one pill.
            legacy = saved.get(legacy_key)
            if isinstance(legacy, str) and legacy.strip():
                pills[:] = [legacy.strip()]

        restore(self.f_name_mode, "name_mode", self._TEXT_MODES)
        restore_pills(self.f_name_pills, "name_pills", "name_text")
        restore(self.f_chan_mode, "chan_mode", self._TEXT_MODES)
        restore_pills(self.f_chan_pills, "chan_pills", "chan_text")
        restore(self.f_size_min, "size_min")
        restore(self.f_size_max, "size_max")
        restore(self.f_prog_min, "prog_min")
        restore(self.f_prog_max, "prog_max")
        hidden = saved.get("status_exclude")
        if isinstance(hidden, list):
            hidden = set(hidden)
            for cat, var in self.f_status.items():
                var.set(cat not in hidden)

    def _row_tag(self, it: FileItem) -> str:
        """Which colour tag a row wears: finished/moved/error/waiting, or greyed if skipped."""
        if it.status in (COMPLETED, ON_DISK):
            return "done"
        if it.status == MOVED:
            return "moved"
        # An unticked row is one the user set aside: greyed as "skipped",
        # whatever status it carried (mirrors _status_text / status_category).
        # Checked after done/moved so a finished-then-unticked row keeps its own
        # colour, and before error/waiting so a skipped error doesn't read red.
        if not it.selected:
            return "skipped"
        if it.status.startswith("error"):
            return "error"
        # Rate-limited, speed-capped, offline, or mid-retry: amber, so a transient
        # stall reads differently from a hard error and from a plain "downloading…"
        # (and from a plain user pause, which stays uncoloured).
        if it.status.startswith(("rate limited", "speed capped", "no connection", "retry")):
            return "waiting"
        return ""

    def _set_selected(self, iids, value: bool) -> None:
        """Add (True) or skip (False) rows — finished rows are left alone."""
        for iid in iids:
            it = self.items.get(iid)
            if it and it.status not in SETTLED:
                it.selected = value
                if self.tree.exists(iid):
                    self.tree.item(iid, tags=(self._row_tag(it),))
                    # Skipping/adding flips a fresh row between blank and "queued".
                    self.tree.set(iid, "status", self._status_text(it))
                # Skip/Add changes the derived status category (queued↔skipped),
                # which may put the row on the hidden side of a Status filter — so
                # re-apply it now instead of waiting for the next full refresh.
                self._apply_row_filter(iid)
        self._reindex_queue()  # membership changed → renumber the queue
        self._update_count()

    def _set_aside_items(self) -> list[FileItem]:
        """Every row the app auto-skipped after an error and that's still unticked."""
        return [
            it
            for it in self.items.values()
            if it.set_aside and not it.selected and it.status not in SETTLED
        ]

    def _restore_set_aside(self) -> None:
        """Re-tick every set-aside file in one pass, and persist it.

        The per-row path (:meth:`_set_selected`) re-evaluates the filter row by
        row, and a row that becomes visible again costs a full tree rebuild — fine
        for a handful, quadratic for the thousands a systemic failure can set
        aside. So flip the ticks in bulk and rebuild once.

        ``set_aside`` itself is left on the item: it's the reason shown in the
        Error column, and ``_run_download`` clears it when the retry actually
        starts.
        """
        restored = self._set_aside_items()
        if not restored:
            return
        for it in restored:
            it.selected = True
        self._refresh_tree()
        self._persist_scans()  # survive a restart, like any other tick change
        self.set_status(f"Restored {len(restored)} set-aside file(s) to the queue.")

    def _toggle(self, iid: str) -> None:
        it = self.items.get(iid)
        if it and it.status not in SETTLED:
            self._set_selected([iid], not it.selected)

    def _toggle_highlighted(self) -> None:
        for iid in self.tree.selection():
            self._toggle(iid)

    def _select_all_rows(self, event=None):
        """⌘/Ctrl-A highlights every row, so you can then right-click to act on all."""
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def _files_context(self, menu, event) -> None:
        """Right-click menu: add to / skip / clear the list, discard duplicates."""
        rows = list(self.tree.selection())
        actionable = [r for r in rows if (it := self.items.get(r)) and it.status not in SETTLED]
        n = len(actionable)
        menu.add_command(
            label=f"✅  Add to List ({n})",
            command=lambda: self._set_selected(actionable, True),
            state="normal" if n else "disabled",
        )
        menu.add_command(
            label=f"⏩  Skip ({n})",
            command=lambda: self._set_selected(actionable, False),
            state="normal" if n else "disabled",
        )
        # Set-aside rows are usually invisible here — they read as "skipped", and
        # the Status filter hides that category by default — so re-ticking them
        # can't go through the selection. This acts on all of them at once.
        aside = self._set_aside_items()
        menu.add_command(
            label=f"♻️  Restore Set-Aside Files ({len(aside)})",
            command=self._restore_set_aside,
            state="normal" if aside else "disabled",
        )
        menu.add_separator()
        # Per-file download controls. Download-now works whether or not a run is
        # live (idle → starts one for just these files); Pause/Stop only bite an
        # in-flight or queued download, so they're offered only during a run.
        running = self.state in ("running", "paused")
        menu.add_command(
            label=f"▶️  Start ({n})",
            command=lambda: self.on_file_download(actionable),
            state="normal" if n else "disabled",
        )
        menu.add_command(
            label=f"⏸️  Pause ({n})",
            command=lambda: self.on_file_pause(actionable),
            state="normal" if n and running else "disabled",
        )
        menu.add_command(
            label=f"⏹️  Stop ({n})",
            command=lambda: self.on_file_stop(actionable),
            state="normal" if n and running else "disabled",
        )
        menu.add_separator()
        # Reset flushes a row's state + wipes its file/.part but keeps the row;
        # Delete removes the row *and* its file, behind a confirmation.
        menu.add_command(
            label=f"🔄  Reset ({len(rows)})",
            command=lambda: self.on_file_reset(rows),
            state="normal" if rows else "disabled",
        )
        menu.add_command(
            label=f"❌  Delete ({len(rows)})",
            command=lambda: self.on_file_delete(rows),
            state="normal" if rows else "disabled",
        )
        menu.add_separator()
        # Queue management: reorder the download order (effective on the next Start).
        menu.add_command(
            label=f"⬆️  Move to Top ({len(rows)})",
            command=lambda: self._move_in_queue(rows, to_top=True),
            state="normal" if rows else "disabled",
        )
        menu.add_command(
            label=f"⬇️  Move to Bottom ({len(rows)})",
            command=lambda: self._move_in_queue(rows, to_top=False),
            state="normal" if rows else "disabled",
        )
        menu.add_separator()
        menu.add_command(
            label=f"⏏️  Clear From List ({len(rows)})",
            command=self.on_clear_selected,
            state="normal" if rows else "disabled",
        )
        # Column filters live on the header right-click; offer a global reset here
        # too, since a stray filter is otherwise cleared column-by-column.
        if not self._build_filter().is_empty():
            menu.add_separator()
            menu.add_command(label="Clear All Filters", command=self._clear_filters)
        # Column layout is drag-reordered/resized in place; offer a one-click reset
        # to the code-defined default order and widths.
        menu.add_separator()
        menu.add_command(label="Reset to Default View", command=self._reset_view)

    def _move_in_queue(self, iids, *, to_top: bool) -> None:
        """Reorder ``self.items`` so the selected rows lead (or trail) the list.

        Download order for fresh files follows ``self.items`` insertion order (see
        ``_download``), so moving rows here is what "manage the queue" means — it
        takes effect on the next Start, not on an in-flight run. Selection order is
        preserved within the moved block; everything else keeps its relative order.
        This is a session reorder — it isn't persisted to the scan cache.
        """
        picked = [iid for iid in iids if iid in self.items]
        if not picked:
            return
        moved = set(picked)
        rest = [(iid, it) for iid, it in self.items.items() if iid not in moved]
        block = [(iid, self.items[iid]) for iid in picked]
        self.items = dict(block + rest if to_top else rest + block)
        self._refresh_tree()

    def on_clear_selected(self) -> None:
        """Remove the highlighted rows from the list for good (tombstoned)."""
        removed = []
        for iid in self.tree.selection():
            it = self.items.pop(iid, None)
            if it:
                removed.append(it)
            self.tree.delete(iid)
        self._after_discard(removed)

    # -- per-file download controls ------------------------------------
    #
    # Five row-level actions off the Files right-click menu, mirroring the batch
    # controls but scoped to the highlighted rows. The pause/stop *signals* are
    # sets read on the worker loop (see __init__), so every mutation here is
    # marshalled through worker.call; the file-touching work (reset/delete) runs
    # as a coroutine on the worker loop so it can wait for an in-flight download
    # to let go of the .part before deleting it.

    def _unsettled(self, iids) -> list[str]:
        """The subset of ``iids`` that name a live, not-yet-finished row."""
        return [iid for iid in iids if (it := self.items.get(iid)) and it.status not in SETTLED]

    def on_file_download(self, iids) -> None:
        """Force these files to download now, ahead of the normal queue order.

        Clears any per-file pause/stop on them, ticks them, then: if a run is
        live, pushes them onto the pool's urgent lane (picked next by a free
        worker); if idle, starts a run for just these files. A batch that's
        globally paused is resumed first so the urgent files can actually move.
        """
        picked = self._unsettled(iids)
        if not picked:
            return
        for iid in picked:
            self.worker.call(self._file_stop.discard, iid)
            self.worker.call(self._file_pause.discard, iid)
        self._set_selected(picked, True)  # ensure ticked; repaints the queue #
        if self.state in ("running", "paused"):
            if self.state == "paused":
                self.on_start()  # resume the batch so the urgent file can run
            for iid in picked:
                self.worker.call(self._enqueue, LANE_URGENT, iid)
            self.set_status(f"Downloading {len(picked)} file(s) now.")
        else:
            self._reconnect_gen += 1  # supersede any pending auto-restart wait
            self._move_in_queue(picked, to_top=True)
            self.state = "running"
            self._stop_flag = False
            self._sync_buttons()
            self.worker.submit(self._download(picked))

    def on_file_pause(self, iids) -> None:
        """Pause just these downloads; each parks until Download-now resumes it."""
        picked = self._unsettled(iids)
        if not picked:
            return
        for iid in picked:
            self.worker.call(self._file_pause.add, iid)
            self._set_row(iid, "paused")
        self.set_status(f"Paused {len(picked)} file(s) — right-click ▸ Download now to resume.")

    def on_file_stop(self, iids) -> None:
        """Force-stop these downloads. The .part is kept, so Start/Download resumes."""
        picked = self._unsettled(iids)
        if not picked:
            return
        for iid in picked:
            self.worker.call(self._file_pause.discard, iid)
            self.worker.call(self._file_stop.add, iid)
            self._set_row(iid, "stopped")
        self.set_status(f"Stopped {len(picked)} file(s).")

    def on_file_reset(self, iids) -> None:
        """Flush a row's state and wipe its file/.part from disk, keeping the row."""
        picked = [iid for iid in iids if iid in self.items]
        if not picked:
            return
        self.set_status(f"Resetting {len(picked)} file(s)…")
        self.worker.submit(self._do_reset(picked))

    def on_file_delete(self, iids) -> None:
        """Delete these files (row + on-disk copy) after a confirmation prompt."""
        picked = [iid for iid in iids if iid in self.items]
        if not picked:
            return
        if not self._confirm_delete(len(picked)):
            return
        self.set_status(f"Deleting {len(picked)} file(s)…")
        self.worker.submit(self._do_delete(picked))

    async def _purge_files(self, iids: list[str]) -> list[str]:
        """Stop any in-flight download for each iid, then delete its file + .part.

        Runs on the worker loop. A file being actively written is first signalled
        to stop (via ``_file_stop``) and given a moment to release the ``.part``;
        if it won't let go we skip it rather than delete a file out from under a
        live writer. Returns the iids actually purged.
        """
        dl = self.cfg["download_dir"]
        purged: list[str] = []
        for iid in iids:
            it = self.items.get(iid)
            if not it:
                continue
            self._file_pause.discard(iid)
            self._file_stop.add(iid)
            for _ in range(200):  # ~20s: let the download observe the stop and unwind
                if iid not in self._active_iids:
                    break
                await asyncio.sleep(0.1)
            self._file_stop.discard(iid)
            if iid in self._active_iids:
                log.warning("Reset/delete skipped %s — still downloading", it.name)
                self.set_status(f"“{it.name}” is still downloading — try Stop first.")
                continue
            part = it.part_path(dl)
            for p in (part, part_index_path(part), it.target_path(dl)):
                try:
                    p.unlink(missing_ok=True)
                except OSError as e:
                    log.warning("Could not delete %s: %s", p, e)
            purged.append(iid)
        return purged

    async def _do_reset(self, iids: list[str]) -> None:
        purged = await self._purge_files(iids)
        for iid in purged:
            self.post(lambda iid=iid: self._reset_row_ui(iid))
        self.post(self._persist_scans)
        self.post(lambda: self.set_status(f"Reset {len(purged)} file(s)."))

    async def _do_delete(self, iids: list[str]) -> None:
        purged = await self._purge_files(iids)
        self.post(lambda: self._remove_rows_ui(purged))

    def _reset_row_ui(self, iid: str) -> None:
        """Return a row to its just-scanned state (queued, no progress, no file)."""
        it = self.items.get(iid)
        if not it:
            return
        it.status = ""
        it.progress = -1
        it.speed = 0.0
        it.downloaded = False
        it.selected = True  # a flushed file is fresh work again
        self._destroy_bar(iid)
        if self.tree.exists(iid):
            self.tree.item(iid, tags=(self._row_tag(it),))
            self.tree.set(iid, "status", self._status_text(it))
            self.tree.set(iid, "note", self._note_text(it))
            self.tree.set(iid, "progress", self._progress_text(it))
            self.tree.set(iid, "speed", "")
            self.tree.set(iid, "eta", "")
        self._reindex_queue()
        self._update_count()
        # Reset returns the row to "queued"; if that category is filtered out it
        # should hide (or, if it was hidden as skipped/completed, reappear) now.
        self._apply_row_filter(iid)

    def _remove_rows_ui(self, iids: list[str]) -> None:
        """Drop rows from the list for good (tombstoned so a rescan won't re-add)."""
        removed = []
        for iid in iids:
            it = self.items.pop(iid, None)
            if it:
                removed.append(it)
            self._destroy_bar(iid)
            if self.tree.exists(iid):
                self.tree.delete(iid)
        self._after_discard(removed)
        self.set_status(f"Deleted {len(removed)} file(s).")

    def _confirm_delete(self, count: int) -> bool:
        """A centred, modal 'are you sure' with a red warning mark. Blocks for the answer."""
        win = tk.Toplevel(self)
        win.title("Confirm Delete")
        win.transient(self)
        win.resizable(False, False)
        result = {"ok": False}

        frame = ttk.Frame(win, padding=24)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text="✕", foreground="#c62828", font=("", 44, "bold")).pack(pady=(0, 8))
        what = "this file" if count == 1 else f"these {count} files"
        ttk.Label(
            frame,
            justify="center",
            text=f"Do you really want to delete {what}?\n"
            "Click Yes to delete it. This change is irreversible.",
        ).pack(pady=(0, 18))

        btns = ttk.Frame(frame)
        btns.pack()

        def yes():
            result["ok"] = True
            win.destroy()

        # tk.Button (not ttk) so the destructive action can carry a red cue. Aqua
        # ignores bg on a native button, so a white fg there paints white-on-white
        # (an invisible label) — use red *text* on the default fill instead, and
        # keep the red fill + white text where the platform honours bg. The big red
        # ✕ above is the cross-platform cue either way.
        if self.tk.call("tk", "windowingsystem") == "aqua":
            yes_colors = {"fg": "#c62828", "activeforeground": "#a91d1d"}
        else:
            yes_colors = {
                "bg": "#c62828",
                "fg": "white",
                "activebackground": "#a91d1d",
                "activeforeground": "white",
            }
        tk.Button(
            btns,
            text="Yes",
            command=yes,
            width=10,
            **yes_colors,
        ).pack(side="left", padx=8)
        ttk.Button(btns, text="Cancel", width=10, command=win.destroy).pack(side="left", padx=8)

        win.bind("<Escape>", lambda e: win.destroy())
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - w) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - h) // 2)
        win.geometry(f"+{x}+{y}")
        win.grab_set()
        win.wait_window()
        return result["ok"]

    def on_discard_duplicates(self) -> None:
        """Drop reposts: same filename and size, keeping the first-scanned copy.

        Works on every scanned item, not just the rows the filter is showing, so
        it rebuilds the tree from ``self.items`` rather than editing rows in place.
        """
        _, discarded = dedupe(self.items.values())
        if not discarded:
            self.set_status("No duplicates found.")
            return
        freed = sum(it.size for it in discarded)
        drop = {id(it) for it in discarded}
        self.items = {iid: it for iid, it in self.items.items() if id(it) not in drop}
        self._tombstone(discarded)
        self._refresh_tree()
        self._persist_scans()
        self.set_status(
            f"Discarded {len(discarded)} duplicate(s) — {human_size(freed)} of repeated files."
        )

    def _after_discard(self, removed: list[FileItem]) -> None:
        """Shared tail for the discard buttons: tombstone, save, refresh count.

        Tombstoning is what makes a discard durable — the msg_ids are recorded so
        a later full rescan won't pull them back in.
        """
        if removed:
            self._tombstone(removed)
            self._persist_scans()
        self._reindex_queue()  # rows left the list → renumber the queue
        self._update_count()

    def _update_count(self) -> None:
        items = list(self.items.values())
        total = len(items)
        # "To download" = ticked and not yet finished, so the count and size
        # shrink in real time as files complete (a completed file keeps
        # selected=True but its status lands in SETTLED).
        # Lead with what you *have*: settled = files actually on disk. `done_size`
        # is summed from those rows directly (the true downloaded bytes), not
        # derived as all-minus-todo, so skipped/unselected files don't inflate it.
        done = [i for i in items if i.status in SETTLED]
        done_size = sum(i.size for i in done)
        # "To download" = ticked and not yet finished, so the count and size shrink
        # in real time as files complete (a completed file keeps selected=True but
        # its status lands in SETTLED).
        todo = [i for i in items if i.selected and i.status not in SETTLED]
        todo_size = sum(i.size for i in todo)
        # Denominator is the *download plan* — what's on disk plus what's still
        # queued — not every scanned row. Skipped/unselected files are excluded so
        # the "X of Y" ratio measures progress toward what you actually asked for.
        plan_size = done_size + todo_size

        parts = [
            f"{len(done)}/{total} downloaded",
            f"{human_size(done_size)} of {human_size(plan_size)}",
        ]
        if todo:
            parts.append(f"{len(todo)} left ({human_size(todo_size)})")
        free = self._free_space()
        over = free is not None and todo_size > free
        if free is not None:
            parts.append(f"{human_size(free)} free")
        msg = "  ·  ".join(parts)
        if over:
            msg += "   ⚠ won't fit"
        self.count_var.set(msg)
        self.count_label.configure(foreground="#c62828" if over else "")

    def _update_selection_size(self) -> None:
        """Show what the highlighted rows add up to, beside the transport buttons.

        Highlight ≠ tick: this is the raw selection (what a right-click would act
        on), so it's a separate readout from the ticked-queue figures on the
        right. Blank when nothing is highlighted.
        """
        rows = [it for iid in self.tree.selection() if (it := self.items.get(iid))]
        if not rows:
            self.sel_var.set("")
            return
        total = sum(it.size for it in rows)
        self.sel_var.set(f"{len(rows)} selected  ·  {human_size(total)}")

    def _tick_counter(self) -> None:
        """Refresh the count/size/free readout on a timer while downloading.

        A completing file shrinks the to-download figures, and free space drops as
        bytes land in the ``.part`` — neither fires a UI event, so poll for it.
        """
        if self.state in ("running", "paused"):
            self._update_count()
        if self.state == "running":
            self._update_speed()  # samples _speed_now, which _update_eta reads
            self._update_eta()
            self._update_file_speeds()  # per-row Speed/Time-left, same accumulator way
        self.after(1500, self._tick_counter)

    def _update_speed(self) -> None:
        """Report live download speed = bytes pulled since the previous tick ÷ elapsed.

        Windowed rather than run-long (unlike the ETA's average), so it reflects the
        connection *right now* and reacts to stalls/bursts. Cleared while paused (no
        bytes flow) and needs one interval to seed before it shows anything.
        """
        now = time.monotonic()
        if not self._speed_t:
            self._speed_bytes, self._speed_t = self._dl_bytes, now
            self._speed_now = 0.0
            return
        dt = now - self._speed_t
        delta = self._dl_bytes - self._speed_bytes
        self._speed_bytes, self._speed_t = self._dl_bytes, now
        if dt <= 0 or not self._pause_event.is_set():
            self._speed_now = 0.0
            self.speed_var.set("")
            return
        self._speed_now = delta / dt
        self.speed_var.set(f"{human_size(self._speed_now)}/s")

    def _update_eta(self) -> None:
        """Estimate time left = remaining bytes ÷ this run's average throughput.

        Average bandwidth is measured live (bytes actually pulled over the network
        this run over elapsed time), so it adapts to the real connection rather
        than guessing. Shows nothing until there's enough data to be meaningful.

        When no bytes are flowing right now (``_speed_now == 0`` — a throttle or
        the premium speed cap has parked the batch), the run-long average would
        still yield a confident-looking figure like "~2d 1h left" beside a live
        "0 B/s". That's misleading, so show ``∞`` instead until throughput resumes.
        """
        elapsed = time.monotonic() - self._dl_start
        if not self._dl_start or elapsed < 2 or self._dl_bytes <= 0:
            self.eta_var.set("estimating…")
            return
        speed = self._dl_bytes / elapsed  # bytes per second
        remaining = 0
        for it in self.items.values():
            if it.selected and it.status not in SETTLED:
                pct = it.progress if it.progress and it.progress > 0 else 0
                remaining += it.size * (100 - pct) // 100
        if remaining <= 0 or speed <= 0:
            self.eta_var.set("")
            return
        if self._speed_now <= 0:  # stalled now — a finite ETA would be a lie
            self.eta_var.set("~∞ left")
            return
        self.eta_var.set(f"~{human_duration(remaining / speed)} left")

    def _free_space(self) -> int | None:
        """Bytes free on the download volume, or None if it can't be determined.

        The configured folder may not exist yet, so walk up to the nearest parent
        that does — that's the same filesystem the download will land on.
        """
        p = Path(self.cfg["download_dir"] or os.path.expanduser("~"))
        while not p.exists() and p != p.parent:
            p = p.parent
        try:
            return shutil.disk_usage(p).free
        except OSError:
            return None

    #: How each sortable column orders rows. Channel falls back to filename, so
    #: sorting by channel groups a channel's files and alphabetises within it.
    _SORT_KEYS = {
        "name": lambda it: it.name.lower(),
        "channel": lambda it: (it.chat_title.lower(), it.name.lower()),
        "size": lambda it: it.size,
        "progress": lambda it: 100 if it.status in (COMPLETED, ON_DISK) else it.progress,
        "speed": lambda it: it.speed,
        # Idle rows (no live ETA) sort to the bottom, ascending or descending.
        "eta": lambda it: App._eta_seconds(it) or float("inf"),
        "status": lambda it: it.status,
    }

    @staticmethod
    def _progress_text(it: FileItem) -> str:
        """Plain percentage shown in the cell (a fallback under the green bar)."""
        pct = 100 if it.status in (COMPLETED, ON_DISK) else it.progress
        return f"{pct}%" if pct is not None and pct >= 0 else ""

    @staticmethod
    def _progress_pct(it: FileItem) -> float:
        """The 0–100 number the Progress filter compares against (never negative)."""
        pct = 100 if it.status in (COMPLETED, ON_DISK) else it.progress
        return float(pct) if pct is not None and pct >= 0 else 0.0

    @staticmethod
    def _is_downloading(it: FileItem) -> bool:
        """True while the file is actively pulling bytes (speed/ETA are live)."""
        return it.status.strip().lower().startswith("downloading") and it.speed > 0

    @staticmethod
    def _speed_text(it: FileItem) -> str:
        """Per-file transfer speed, shown only while actively downloading."""
        return f"{human_size(it.speed)}/s" if App._is_downloading(it) else ""

    @staticmethod
    def _eta_seconds(it: FileItem) -> float | None:
        """Seconds of download left = remaining bytes ÷ current speed, or None.

        None whenever there's no live estimate — the row isn't downloading, speed
        hasn't settled yet, or it's already complete. Percentage-based, so it's a
        rough figure (good enough for a moving readout) rather than byte-exact.
        """
        if not App._is_downloading(it):
            return None
        pct = it.progress if it.progress and it.progress > 0 else 0
        remaining = it.size * (100 - pct) / 100
        return remaining / it.speed if remaining > 0 else None

    @staticmethod
    def _eta_text(it: FileItem) -> str:
        """Per-file time left, shown only while actively downloading."""
        secs = App._eta_seconds(it)
        return f"~{human_duration(secs)}" if secs is not None else ""

    @staticmethod
    def _status_text(it: FileItem) -> str:
        """Status-column text — the lifecycle state, with the reason split out.

        The raw ``status`` model is left untouched (fresh files store ``""``).
        The column shows only the high-level state: a ticked, not-yet-started file
        reads "queued"; downloading/completed keep their verbose text; other
        lifecycle states (moved/incomplete/paused/stopped) show as-is. A throttled
        (rate-limited / speed-capped) but still ticked file reads "paused": its
        download is parked on the rate-limit cooldown, so it flips to "paused"
        while it waits and back to "downloading…" when it resumes. A mid-retry or
        still-ticked error reads "queued": it's unfinished work that re-downloads,
        and *why* it stalled goes in the Error column (:meth:`_note_text`), not
        here. This mirrors ``filtering.status_category`` so the Status column and
        the Status filter always agree. An unticked file the user set aside reads
        "skipped" (the greyed row underlines it), whatever status it carried.
        """
        low = it.status.strip().lower()
        if not low:
            return "queued" if it.selected else "skipped"
        if low.startswith("downloading") or low in ("completed", "on disk"):
            return it.status
        # A throttle or a dropped link is a *wait* (parked on the cooldown / offline
        # gate), so a ticked such file reads "paused" and flips back to "downloading…".
        if low.startswith(("rate limited", "speed capped", "no connection")):
            return "paused" if it.selected else "skipped"
        if low.startswith(("retry", "error")):
            return "queued" if it.selected else "skipped"
        return it.status  # moved / incomplete / paused / stopped

    @staticmethod
    def _note_text(it: FileItem) -> str:
        """Error-column text: the stall reason (error/retry detail), else blank."""
        return status_note(it.status)

    def _queue_index_map(self) -> dict[str, int]:
        """1-based download position for every to-download row, in queue order.

        A row counts as "in the queue" while it's ticked and not settled — so
        queued, incomplete, downloading, waiting and error rows all get a number,
        and a completed/skipped row gets none. The order is ``self.items``
        insertion order, which is also the order Start feeds fresh files to the
        pool (see ``_download``), so the column is an honest at-a-glance plan.
        Re-ordering ``self.items`` (Move to top/bottom) re-numbers accordingly.
        """
        idx: dict[str, int] = {}
        n = 0
        for iid, it in self.items.items():
            if it.selected and it.status not in SETTLED:
                n += 1
                idx[iid] = n
        return idx

    def _reindex_queue(self) -> None:
        """Repaint the ``#`` column of visible rows after queue membership shifts."""
        idx = self._queue_index_map()
        for iid in self.tree.get_children():
            self.tree.set(iid, "queue", str(idx[iid]) if iid in idx else "")

    # -- green progress-bar overlays -----------------------------------
    #
    # ttk.Treeview can't colour a single cell, so the bar is a small Canvas
    # placed over the Progress cell of each in-flight/incomplete row. Only such
    # rows get one (a handful — bounded by the download concurrency plus any
    # resumable leftovers), and only while visible, so this stays cheap.

    @staticmethod
    def _bar_pct(it: FileItem) -> int | None:
        """The percentage to draw a bar for, or None if the row shouldn't have one."""
        p = it.progress
        return p if p is not None and 0 <= p < 100 else None

    def _draw_bar(self, iid: str) -> None:
        it = self.items.get(iid)
        pct = self._bar_pct(it) if it else None
        if pct is None or not self.tree.exists(iid):  # gone, or filtered out of the tree
            self._destroy_bar(iid)
            return
        bbox = self.tree.bbox(iid, "progress")
        cv = self._bars.get(iid)
        if not bbox:  # row scrolled out of view
            if cv:
                cv.place_forget()
            return
        x, y, w, h = bbox
        if cv is None:
            cv = tk.Canvas(self.tree, highlightthickness=0, bd=0, takefocus=0)
            cv.bind("<Button-1>", lambda e, i=iid: self.tree.selection_set(i))
            self._bars[iid] = cv
        cv.place(x=x, y=y, width=w, height=h)
        cv.delete("all")
        gap = 2  # vertical breathing room so adjacent rows' bars don't touch
        top, bot = gap, h - gap
        cv.create_rectangle(0, top, w, bot, fill="#3a3a3a", outline="")  # track
        fill = int(w * pct / 100)
        if fill > 0:
            cv.create_rectangle(0, top, fill, bot, fill="#37c871", outline="")  # green line
        cv.create_text(w // 2, h // 2, text=f"{pct}%", fill="#ffffff")  # centred %

    def _destroy_bar(self, iid: str) -> None:
        cv = self._bars.pop(iid, None)
        if cv:
            cv.destroy()
        self._bar_active.discard(iid)

    def _reposition_bars(self) -> None:
        for iid in list(self._bar_active):
            self._draw_bar(iid)

    def _sync_bars(self) -> None:
        """Rebuild the set of rows that should carry a bar, then (re)place them."""
        active = {iid for iid, it in self.items.items() if self._bar_pct(it) is not None}
        for iid in list(self._bars):
            if iid not in active:
                self._destroy_bar(iid)
        self._bar_active = active
        self._reposition_bars()

    def _sort_by(self, col: str) -> None:
        """Sort the file list by a column; clicking the same header again reverses."""
        cur_col, cur_rev = self._sort
        self._sort = (col, not cur_rev if cur_col == col else False)
        self._refresh_tree()

    def _row_matches_filter(self, it: FileItem, filt: FileFilter) -> bool:
        """Whether ``it`` passes the active header filters (empty filter → always)."""
        return filt.is_empty() or filt.matches(
            name=it.name,
            channel=it.chat_title,
            size=it.size,
            progress=self._progress_pct(it),
            category=status_category(it.status, it.selected),
        )

    def _apply_row_filter(self, iid: str) -> None:
        """Re-evaluate one row against the active filters after a filterable value
        changed, hiding a row that no longer matches and restoring one that now
        does — without a full rebuild in the common (hide) case.

        This is what makes the filtered view honest live: e.g. a download that
        completes into a filtered-out ``completed`` category disappears at once
        instead of lingering until the next full refresh.
        """
        it = self.items.get(iid)
        if not it:
            return
        matches = self._row_matches_filter(it, self._build_filter())
        exists = self.tree.exists(iid)
        if exists and not matches:
            self._destroy_bar(iid)
            self.tree.delete(iid)
            self._update_count()
        elif matches and not exists:
            self._refresh_tree()  # rebuild so the row reappears in sort order

    def _refresh_tree(self) -> None:
        filt = self._build_filter()
        qidx = self._queue_index_map()
        col, reverse = self._sort
        rows = list(self.items.items())
        if col == "queue":
            # Queued rows first in queue order; everything else (no index) after.
            rows.sort(key=lambda kv: qidx.get(kv[0], 1 << 30), reverse=reverse)
        elif col in self._SORT_KEYS:
            rows.sort(key=lambda kv: self._SORT_KEYS[col](kv[1]), reverse=reverse)

        self.tree.delete(*self.tree.get_children())
        for iid, it in rows:
            if not self._row_matches_filter(it, filt):
                continue
            self.tree.insert(
                "",
                "end",
                iid=iid,
                tags=(self._row_tag(it),),
                values=(
                    str(qidx[iid]) if iid in qidx else "",
                    it.name,
                    it.chat_title,
                    human_size(it.size),
                    self._progress_text(it),
                    self._speed_text(it),
                    self._eta_text(it),
                    self._status_text(it),
                    self._note_text(it),
                ),
            )
        # Header shows a sort arrow on the active column and a green funnel icon
        # on any column with a live filter (image cleared with "" otherwise), so
        # both the sort and the filtered-state are visible at a glance.
        for c, base in self._col_titles.items():
            arrow = ("▼  " if reverse else "▲  ") if c == col else ""
            icon = self._filter_on_img if self._col_filter_active(c) else ""
            self.tree.heading(c, text=arrow + base, image=icon)
        self.after_idle(self._sync_bars)  # rows are placed; overlay bars follow
        self._update_count()
        # Rebuilding the tree drops the highlight (and a filter can hide rows that
        # were in it), so re-derive the selection readout rather than let it go stale.
        self._update_selection_size()

    # -- Tab 2: Channels -----------------------------------------------

    def _build_channels_tab(self) -> None:
        f = self.tab_channels

        ttk.Label(f, text="Channels and Groups to Scan for Files:", font=("", 12, "bold")).pack(
            anchor="w", padx=10, pady=(12, 4)
        )
        ttk.Label(
            f,
            foreground="#666",
            text="Fetch Telegram groups and pick from the list.",
        ).pack(anchor="w", padx=10)
        row = ttk.Frame(f)
        row.pack(fill="x", padx=10, pady=8)
        # Fetch/star don't touch the scan list, so they stay enabled even when
        # TG_CHANNELS is locked — you can still browse and star.
        ttk.Button(row, text="Fetch My Groups…", command=self.on_fetch_dialogs).pack(
            side="right"
        )

        # extended → Shift-click selects a range, Ctrl/Cmd-click toggles single rows.
        self.chan_list = tk.Listbox(
            f, height=14, activestyle="none", selectmode="extended", highlightthickness=0, bd=0
        )
        self.chan_list.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self._install_multiselect(self.chan_list)
        self._bind_context_menu(self.chan_list, self._channels_context)
        for ch in self.cfg["channels"]:
            self.chan_list.insert("end", self._chan_label(ch))

    def _chan_label(self, ch: dict) -> str:
        star = "★ " if self._is_favourite(ch["id"]) else ""
        body = f"{ch['title']}   ({ch['id']})" if ch.get("title") else str(ch["id"])
        return star + body

    # -- favourites ----------------------------------------------------

    def _is_favourite(self, chat_id) -> bool:
        return any(str(c["id"]) == str(chat_id) for c in self.cfg["favourites"])

    def _set_favourite(self, ch: dict, want: bool) -> bool:
        """Add or remove one favourite in memory. Returns True if it changed.

        No disk write — callers batch a single ``save_config`` so bulk starring a
        hundred rows doesn't rewrite config.json a hundred times.
        """
        favs = self.cfg["favourites"]
        cid = str(ch["id"])
        idx = next((i for i, c in enumerate(favs) if str(c["id"]) == cid), None)
        if want and idx is None:
            favs.append({"id": ch["id"], "title": ch.get("title", "")})
            return True
        if not want and idx is not None:
            del favs[idx]
            return True
        return False

    def _toggle_favourite(self, ch: dict) -> bool:
        """Flip one channel's star and persist. Returns the new state.

        Favourites live in config.json, not the .env, so this saves with plain
        ``save_config`` — no need to rewrite the credential file on a star click.
        """
        new = not self._is_favourite(ch["id"])
        self._set_favourite(ch, new)
        save_config(self.cfg)
        return new

    def _bulk_star(self, channels: list[dict], want: bool) -> int:
        """Star (or unstar) many channels at once. Returns how many changed."""
        changed = 0
        for ch in channels:
            changed += self._set_favourite(ch, want)
        if changed:
            save_config(self.cfg)
            self._refresh_channel_list()  # repaint ★ on any listed channels
        return changed

    # -- selection -----------------------------------------------------

    def _install_multiselect(self, widget) -> None:
        """Give a Treeview/Listbox proper multi-range selection.

        Tk's ``extended`` mode already does contiguous **Shift**-range and, on
        non-Mac, discontiguous **Ctrl**-toggle. What this fills in:

        * **Press-drag range** — press a row and drag up/down to sweep out a
          contiguous range, *replacing* the selection (standard file-manager
          gesture). Dragging past the top/bottom edge autoscrolls so the range
          keeps growing through a long list.
        * **Ctrl/⌘ press-drag adds a range** — the same sweep with the modifier
          held *adds* the swept rows on top of the existing selection instead of
          replacing, so you can build several non-contiguous ranges. The prior
          ranges stay put until the next *plain* click/drag replaces them.
        * **⌘-toggle on macOS** — Tk only wires Control there, but Mac users
          expect Command (Control is right-click on Aqua).
        * **Shift-click/drag extends the *current* range only.** Tk's own
          Shift-1 does ``selection set span(anchor, row)`` — it both replaces
          everything else *and* fills the gap back to the original anchor, so a
          second range built after a ⌘-click swallowed the unselected rows in
          between. We extend from our own anchor over a preserved *base* (the
          selection as it stood when that anchor was set), which is the
          Finder/Explorer rule: plain click → shift-click replaces; ⌘-click →
          shift-click adds a second range and leaves the gap alone.
        * **Ctrl/⌘-Shift-click** does the same (click-only counterpart of the
          modified drag).

        We keep a per-widget anchor (the drag/last-clicked row) since Tk's own
        anchor isn't reachable and drifts after a toggle, plus ``_msel_base``,
        the selection to preserve while extending from it. ``_msel_drag`` holds
        the live drag state (fixed anchor + that base); each motion recomputes
        ``base ∪ span(anchor, row)`` so dragging back shrinks the range cleanly
        instead of leaving stragglers.
        """
        aqua = self.tk.call("tk", "windowingsystem") == "aqua"
        is_lb = isinstance(widget, tk.Listbox)
        widget._msel_anchor = None
        widget._msel_base: list = []
        widget._msel_drag = None
        widget._msel_scroll_job = None

        def at(y):
            if is_lb:
                i = widget.nearest(y)
                return i if i >= 0 else None
            return widget.identify_row(y) or None

        def items():
            return list(range(widget.size())) if is_lb else list(widget.get_children())

        def is_selected(one):
            return one in (widget.curselection() if is_lb else widget.selection())

        def selected_now():
            return list(widget.curselection() if is_lb else widget.selection())

        def span(a, b):
            seq = items()
            try:
                lo, hi = sorted((seq.index(a), seq.index(b)))
            except ValueError:
                return [b]
            return seq[lo : hi + 1]

        def add(these):
            for i in these:
                widget.selection_set(i) if is_lb else widget.selection_add(i)

        def apply(want):
            """Set the selection to exactly ``want`` (minimal diff, no flicker)."""
            want = list(dict.fromkeys(want))
            if is_lb:
                widget.selection_clear(0, "end")
                for i in want:
                    widget.selection_set(i)
                return
            cur, keep = set(widget.selection()), set(want)
            if cur - keep:
                widget.selection_remove(*(cur - keep))
            if keep - cur:
                widget.selection_add(*(keep - cur))

        def stop_autoscroll():
            if widget._msel_scroll_job is not None:
                widget.after_cancel(widget._msel_scroll_job)
                widget._msel_scroll_job = None

        def drag_to(it):
            st = widget._msel_drag
            if st is not None and it is not None:
                apply(st["base"] + span(st["anchor"], it))

        def autoscroll_tick():
            st = widget._msel_drag
            if st is None or not st.get("dir"):
                widget._msel_scroll_job = None
                return
            widget.yview_scroll(st["dir"], "units")
            drag_to(at(0 if st["dir"] < 0 else widget.winfo_height() - 1))
            widget._msel_scroll_job = widget.after(60, autoscroll_tick)

        def press_plain(e):
            it = at(e.y)
            widget._msel_anchor = it
            # base empty → the drag (and any later Shift-click off this anchor)
            # *replaces*; the native class binding does the single-row select for
            # a plain click that never becomes a drag.
            if it is not None:
                widget._msel_base = []
            widget._msel_drag = {"anchor": it, "base": []} if it is not None else None

        def press_add(e):
            it = at(e.y)
            if it is None:
                widget._msel_drag = None
                return "break"
            base = selected_now()  # snapshot BEFORE the toggle, preserved by a drag
            if is_selected(it):
                widget.selection_clear(it) if is_lb else widget.selection_remove(it)
            else:
                add([it])  # NB: Treeview.selection_set would *replace* — add() won't
            if is_lb:
                widget.activate(it)
            widget._msel_anchor = it
            widget._msel_base = base  # a Shift-extend off this anchor keeps it
            widget._msel_drag = {"anchor": it, "base": base}
            return "break"

        def motion(e):
            st = widget._msel_drag
            if st is None:  # press landed on a heading/separator — let Tk resize
                return None
            st["dir"] = -1 if e.y < 0 else 1 if e.y > widget.winfo_height() else 0
            if st["dir"]:
                if widget._msel_scroll_job is None:
                    autoscroll_tick()
            else:
                stop_autoscroll()
                drag_to(at(e.y))
            return "break"

        def release(e):
            stop_autoscroll()
            widget._msel_drag = None

        def extend(e):
            """Shift-press: re-span from the live anchor, over the preserved base.

            The anchor deliberately does *not* move, so shift-clicking again
            adjusts the same range rather than chaining a new one — and the base
            keeps every earlier range intact, gap and all.
            """
            it = at(e.y)
            if it is None:
                return "break"
            anchor = widget._msel_anchor
            if anchor is None:
                anchor = widget._msel_anchor = it
            base = widget._msel_base
            apply(base + span(anchor, it))
            if is_lb:
                widget.activate(it)
            widget._msel_drag = {"anchor": anchor, "base": base}  # shift-drag too
            return "break"

        widget.bind("<Button-1>", press_plain, add="+")
        widget.bind("<B1-Motion>", motion)
        widget.bind("<ButtonRelease-1>", release, add="+")
        widget.bind("<Shift-Button-1>", extend)  # replaces Tk's gap-filling Shift-1
        widget.bind("<Shift-B1-Motion>", motion)
        widget.bind("<Shift-ButtonRelease-1>", release, add="+")
        mod = "Command" if aqua else "Control"
        widget.bind(f"<{mod}-Button-1>", press_add)  # on Aqua Control = right-click
        widget.bind(f"<{mod}-B1-Motion>", motion)
        widget.bind(f"<{mod}-ButtonRelease-1>", release, add="+")
        widget.bind(f"<{mod}-Shift-Button-1>", extend)
        widget.bind(f"<{mod}-Shift-B1-Motion>", motion)
        widget.bind(f"<{mod}-Shift-ButtonRelease-1>", release, add="+")

    def _bind_context_menu(self, widget, builder) -> None:
        """Wire right-click to a popup menu that ``builder(menu, event)`` fills.

        The menu is rebuilt on every click so entries can reflect the current
        selection (e.g. the Favourite checkmark). On macOS, right-click also
        arrives as Button-2 or Ctrl-click, so bind those too (Ctrl-click is free
        for this on Mac — ⌘-click does the multi-select there, not Ctrl).
        """

        def popup(event):
            self._select_under_pointer(widget, event)
            menu = tk.Menu(self, tearoff=0)
            builder(menu, event)
            if menu.index("end") is not None:
                menu.tk_popup(event.x_root, event.y_root)
            return "break"

        widget.bind("<Button-3>", popup)
        if self.tk.call("tk", "windowingsystem") == "aqua":
            widget.bind("<Button-2>", popup)
            widget.bind("<Control-Button-1>", popup)

    @staticmethod
    def _select_under_pointer(widget, event) -> None:
        """Right-clicking a row that isn't selected selects just it first.

        Right-clicking inside an existing multi-selection leaves it intact, so the
        menu acts on everything highlighted.
        """
        if isinstance(widget, ttk.Treeview):
            row = widget.identify_row(event.y)
            if row and row not in widget.selection():
                widget.selection_set(row)
        else:  # tk.Listbox
            idx = widget.nearest(event.y)
            if idx >= 0 and idx not in widget.curselection():
                widget.selection_clear(0, "end")
                widget.selection_set(idx)
                widget.activate(idx)

    def on_add_favourites(self) -> None:
        """Add every starred channel that isn't already in the scan list."""
        if self._locked("channels"):
            return
        n = self._add_channels_to_scan(self.cfg["favourites"])
        self.set_status(
            f"Added {n} favourite(s) to the scan list." if n else "No new favourites to add."
        )

    def _add_channels_to_scan(self, channels: list[dict]) -> int:
        """Append channels to the scan list, skipping ids already there. Persists."""
        existing = {str(c["id"]) for c in self.cfg["channels"]}
        added = 0
        for d in channels:
            if str(d["id"]) in existing:
                continue
            self.cfg["channels"].append({"id": d["id"], "title": d.get("title", "")})
            existing.add(str(d["id"]))
            added += 1
        if added:
            self._refresh_channel_list()
            self.persist()
        return added

    def _refresh_channel_list(self) -> None:
        """Repaint the scan-list box from cfg — picks up star changes and adds."""
        self.chan_list.delete(0, "end")
        for ch in self.cfg["channels"]:
            self.chan_list.insert("end", self._chan_label(ch))

    def on_remove_channel(self) -> None:
        # reversed so earlier deletions don't shift the indices still to come.
        for idx in reversed(self.chan_list.curselection()):
            self.chan_list.delete(idx)
            del self.cfg["channels"][idx]
        self.persist()

    def _channels_context(self, menu, event) -> None:
        """Right-click menu for the scan list: favourite toggle, remove, bulk add."""
        channels = [self.cfg["channels"][i] for i in self.chan_list.curselection()]
        locked = self._locked("channels")
        if channels:
            all_fav = all(self._is_favourite(c["id"]) for c in channels)
            # A checkmark shows when every selected channel is already a favourite;
            # toggling favourites (or unfavourites) the whole selection.
            self._fav_var = tk.BooleanVar(value=all_fav)
            menu.add_checkbutton(
                label="Favourite",
                variable=self._fav_var,
                command=lambda chs=channels: self._bulk_star(chs, self._fav_var.get()),
            )
            menu.add_command(
                label=f"Remove From Scan List ({len(channels)})",
                command=self.on_remove_channel,
                state="disabled" if locked else "normal",
            )
            menu.add_separator()
        favs = len(self.cfg["favourites"])
        menu.add_command(
            label=f"Add All Favourites to Scan List ({favs})",
            command=self.on_add_favourites,
            state="disabled" if locked or not favs else "normal",
        )

    def on_fetch_dialogs(self) -> None:
        self.set_status("Fetching your chats…")
        self.worker.submit(self._fetch_dialogs())

    async def _fetch_dialogs(self) -> None:
        client = await self._ensure_client()
        if not client:
            return
        found = [
            {"id": d.id, "title": d.name or str(d.id)}
            async for d in client.iter_dialogs()
            if d.is_group or d.is_channel
        ]
        self.set_status(f"Found {len(found)} groups/channels.")
        self.post(lambda: self._show_dialog_picker(found))

    def _show_dialog_picker(self, found: list[dict]) -> None:
        win = tk.Toplevel(self)
        win.title("Your Groups and Channels")
        win.transient(self)  # keep it above the main window
        w, h = 640, 540
        self.update_idletasks()  # so the parent's geometry is current
        x = self.winfo_rootx() + max(0, (self.winfo_width() - w) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - h) // 2)
        win.geometry(f"{w}x{h}+{x}+{y}")  # centred on the main window
        locked = self._locked("channels")

        head = ttk.Frame(win)
        head.pack(fill="x", padx=10, pady=8)
        ttk.Label(
            head,
            text="Click Favourite to toggle. Shift = range, Ctrl/⌘ = "
            "one, Ctrl/⌘-Shift = add a range; then right-click.",
        ).pack(side="left")
        ttk.Label(head, text="Filter:").pack(side="left", padx=(12, 2))
        filt = tk.StringVar()
        ttk.Entry(head, textvariable=filt, width=16).pack(side="left")

        cols = ("fav", "name", "id", "in")
        tree = ttk.Treeview(win, columns=cols, show="headings", selectmode="extended")
        for c, txt, w, anchor in (
            ("fav", "Favourite", 74, "center"),
            ("name", "Group / Channel", 290, "w"),
            ("id", "ID", 130, "w"),
            ("in", "In List", 56, "center"),
        ):
            tree.heading(c, text=txt)
            tree.column(c, width=w, anchor=anchor, stretch=(c == "name"))
        tree.tag_configure("fav", foreground="#b8860b")
        tree.pack(fill="both", expand=True, padx=10)
        self._install_multiselect(tree)

        # iid == str(chat_id); this maps a row back to its dialog dict.
        by_id = {str(d["id"]): d for d in found}

        def render() -> None:
            term = filt.get().lower()
            tracked = {str(c["id"]) for c in self.cfg["channels"]}
            # Favourites first, then alphabetical — computed once, on (re)render,
            # so toggling one doesn't make rows jump around under the cursor.
            ordered = sorted(
                found,
                key=lambda d: (not self._is_favourite(d["id"]), str(d["title"]).lower()),
            )
            tree.delete(*tree.get_children())
            for d in ordered:
                cid = str(d["id"])
                if term and term not in f"{d['title']} {cid}".lower():
                    continue
                fav = self._is_favourite(d["id"])
                tree.insert(
                    "",
                    "end",
                    iid=cid,
                    tags=(("fav",) if fav else ()),
                    values=(
                        "✓" if fav else "",
                        d["title"],
                        cid,
                        "✓" if cid in tracked else "",
                    ),
                )

        def on_click(event):
            if tree.identify_region(event.x, event.y) != "cell":
                return
            if tree.identify_column(event.x) != "#1":  # only the Favourite column
                return
            cid = tree.identify_row(event.y)
            if not cid:
                return
            fav = self._toggle_favourite(by_id[cid])
            tree.set(cid, "fav", "✓" if fav else "")
            tree.item(cid, tags=(("fav",) if fav else ()))
            self._refresh_channel_list()  # ✓ may show on a listed channel
            return "break"

        def selected() -> list[dict]:
            return [by_id[cid] for cid in tree.selection()]

        def add_selected():
            n = self._add_channels_to_scan(selected())
            render()
            self.set_status(f"Added {n} channel(s) to the scan list.")

        def context(menu, event):
            picked = selected()
            if picked:
                all_fav = all(self._is_favourite(d["id"]) for d in picked)
                self._fav_var = tk.BooleanVar(value=all_fav)
                menu.add_checkbutton(
                    label="Favourite",
                    variable=self._fav_var,
                    command=lambda p=picked: (
                        self._bulk_star(p, self._fav_var.get()),
                        render(),
                    ),
                )
                menu.add_command(
                    label=f"Add to Scan List ({len(picked)})",
                    command=add_selected,
                    state="disabled" if locked else "normal",
                )
                menu.add_separator()
            favs = len(self.cfg["favourites"])
            menu.add_command(
                label=f"Add All Favourites to Scan List ({favs})",
                command=lambda: (self._add_channels_to_scan(self.cfg["favourites"]), render()),
                state="disabled" if locked or not favs else "normal",
            )

        tree.bind("<Button-1>", on_click)
        self._bind_context_menu(tree, context)
        filt.trace_add("write", lambda *_: render())
        render()

        def add_and_close():
            add_selected()
            win.destroy()

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=10, pady=10)
        # The button commits and closes; the right-click "Add to scan list" stays
        # open so you can add several batches without re-fetching.
        ttk.Button(
            bar,
            text="Add Selected to Scan List",
            command=add_and_close,
            state="disabled" if locked else "normal",
        ).pack(side="left")
        ttk.Button(bar, text="Close", command=win.destroy).pack(side="right")
        if locked:
            ttk.Label(
                win,
                foreground="#b26a00",
                text="Scan list is locked by TG_CHANNELS in your shell — starring still works.",
            ).pack(anchor="w", padx=10, pady=(0, 8))

    # -- Tab 3: Settings -----------------------------------------------

    def _build_settings_tab(self) -> None:
        f = self.tab_settings
        pad = {"padx": 10, "pady": 6}

        ttk.Label(f, text="Telegram API Credentials", font=("", 12, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", **pad
        )
        ttk.Label(
            f,
            foreground="#666",
            justify="left",
            text="Get these from https://my.telegram.org → API development tools.",
        ).grid(row=1, column=0, columnspan=3, sticky="w", padx=10)

        self.api_id_var = tk.StringVar(value=self.cfg["api_id"])
        self.api_hash_var = tk.StringVar(value=self.cfg["api_hash"])
        self.dir_var = tk.StringVar(value=self.cfg["download_dir"])
        self.ext_var = tk.StringVar(value=self.cfg["extensions"])
        self.limit_var = tk.StringVar(value=self.cfg["scan_limit"])

        def entry_state(key: str) -> str:
            return "readonly" if self._locked(key) else "normal"

        def entry(**kw) -> ttk.Entry:
            """A Settings Entry that autosaves the moment focus leaves it."""
            e = ttk.Entry(f, **kw)
            e.bind("<FocusOut>", self._autosave)
            e.bind("<Return>", self._autosave)
            return e

        def env_note(key: str, row: int) -> None:
            if self._locked(key):
                ttk.Label(f, foreground="#666", text="set in environment").grid(
                    row=row, column=2, sticky="w"
                )

        ttk.Label(f, text="API ID").grid(row=2, column=0, sticky="e", **pad)
        entry(textvariable=self.api_id_var, width=42, state=entry_state("api_id")).grid(
            row=2, column=1, sticky="w", **pad
        )
        env_note("api_id", 2)

        ttk.Label(f, text="API Hash").grid(row=3, column=0, sticky="e", **pad)
        entry(
            textvariable=self.api_hash_var, width=42, show="•", state=entry_state("api_hash")
        ).grid(row=3, column=1, sticky="w", **pad)
        env_note("api_hash", 3)

        ttk.Separator(f, orient="horizontal").grid(
            row=4, column=0, columnspan=3, sticky="ew", padx=10, pady=10
        )

        ttk.Label(f, text="Download Folder").grid(row=5, column=0, sticky="e", **pad)
        entry(textvariable=self.dir_var, width=42, state=entry_state("download_dir")).grid(
            row=5, column=1, sticky="w", **pad
        )
        ttk.Button(
            f, text="Browse…", command=self.on_browse, state=entry_state("download_dir")
        ).grid(row=5, column=2, sticky="w")

        ttk.Label(f, text="Extensions").grid(row=6, column=0, sticky="ne", **pad)
        entry(textvariable=self.ext_var, width=42, state=entry_state("extensions")).grid(
            row=6, column=1, sticky="w", **pad
        )
        ttk.Label(f, foreground="#666", text="File types to scan for, comma separated").grid(
            row=6, column=2, sticky="w"
        )

        ttk.Label(f, text="Scan Limit").grid(row=7, column=0, sticky="e", **pad)
        entry(textvariable=self.limit_var, width=12, state=entry_state("scan_limit")).grid(
            row=7, column=1, sticky="w", **pad
        )
        ttk.Label(f, foreground="#666", text="Messages per channel; 0 = entire history").grid(
            row=7, column=2, sticky="w"
        )

        self.conc_var = tk.StringVar(value=self.cfg["download_concurrency"])
        ttk.Label(f, text="Number of Simultaneous Downloads").grid(
            row=8, column=0, sticky="e", **pad
        )
        entry(textvariable=self.conc_var, width=12).grid(row=8, column=1, sticky="w", **pad)
        ttk.Label(
            f,
            foreground="#666",
            text=(
                "Files in parallel (1–16); 1 is best for free Telegram accounts, "
                "4–8 only suits Premium"
            ),
        ).grid(row=8, column=2, sticky="w")

        self.conns_var = tk.StringVar(value=self.cfg["download_connections"])
        ttk.Label(f, text="Connections per File").grid(row=9, column=0, sticky="e", **pad)
        entry(textvariable=self.conns_var, width=12).grid(row=9, column=1, sticky="w", **pad)
        ttk.Label(
            f,
            foreground="#666",
            text="Parallel requests per file (1–16); 4 is a good default",
        ).grid(row=9, column=2, sticky="w")

        self.flood_var = tk.StringVar(value=self.cfg["flood_sleep_threshold"])
        ttk.Label(f, text="Rate-Limit Wait").grid(row=10, column=0, sticky="e", **pad)
        entry(textvariable=self.flood_var, width=12).grid(row=10, column=1, sticky="w", **pad)
        ttk.Label(
            f,
            foreground="#666",
            text="Waits up to this (s) are absorbed; longer ones pause the batch. 20 is good",
        ).grid(row=10, column=2, sticky="w")

        # -- Backup & Transfer: move this setup to another machine ---------
        ttk.Separator(f, orient="horizontal").grid(
            row=11, column=0, columnspan=3, sticky="ew", padx=10, pady=10
        )
        ttk.Label(f, text="Backup & Transfer", font=("", 12, "bold")).grid(
            row=12, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 2)
        )
        ttk.Label(
            f,
            foreground="#666",
            justify="left",
            text=(
                "Export your settings, channels and download list to a file, then import it "
                "on another computer.\nYour login session is never included — sign in again on "
                "the other machine."
            ),
        ).grid(row=13, column=0, columnspan=3, sticky="w", padx=10)

        self._export_secrets_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            f,
            text="Include API credentials (API ID / Hash) in the export",
            variable=self._export_secrets_var,
        ).grid(row=14, column=0, columnspan=3, sticky="w", padx=10, pady=(6, 2))

        transfer = ttk.Frame(f)
        transfer.grid(row=15, column=0, columnspan=3, sticky="w", padx=10, pady=(2, 12))
        ttk.Button(transfer, text="Export…", command=self.on_export_settings).pack(side="left")
        ttk.Button(transfer, text="Import…", command=self.on_import_settings).pack(
            side="left", padx=6
        )

        # -- Save / connect / connection light: always the bottom-most block
        ttk.Separator(f, orient="horizontal").grid(
            row=16, column=0, columnspan=3, sticky="ew", padx=10, pady=10
        )

        btns = ttk.Frame(f)
        btns.grid(row=17, column=0, columnspan=3, sticky="w", padx=10)
        ttk.Button(btns, text="Save Settings", command=self.on_save).pack(side="left")
        ttk.Button(btns, text="Connect / Log In", command=self.on_connect).pack(
            side="left", padx=6
        )
        ttk.Button(btns, text="Log Out", command=self.on_logout).pack(side="left")

        conn = ttk.Frame(f)
        conn.grid(row=18, column=0, columnspan=3, sticky="w", padx=10, pady=12)
        self.conn_light = tk.Label(conn, text="●", font=("", 14))
        self.conn_light.pack(side="left")
        self.conn_var = tk.StringVar(value="Not connected")
        ttk.Label(conn, textvariable=self.conn_var).pack(side="left", padx=(4, 0))

    def on_browse(self) -> None:
        d = filedialog.askdirectory(initialdir=self.dir_var.get() or os.path.expanduser("~"))
        if d:
            self.dir_var.set(d)
            self.on_save()  # picking a folder is a deliberate commit — save it now

    def _pull_settings(self) -> None:
        """Copy the Settings widgets into cfg, skipping env-locked (read-only) fields.

        The single place field → cfg happens, so on_save *and* the close handler
        both commit whatever's on screen (otherwise a typed-but-unsaved folder is
        lost when persist() writes the still-stale cfg).
        """
        edited = {
            "api_id": self.api_id_var.get().strip(),
            "api_hash": self.api_hash_var.get().strip(),
            "download_dir": self.dir_var.get().strip(),
            "extensions": self.ext_var.get().strip(),
            "scan_limit": self.limit_var.get().strip() or "0",
            "download_concurrency": self.conc_var.get().strip() or "1",
            "download_connections": self.conns_var.get().strip() or "4",
            "flood_sleep_threshold": self.flood_var.get().strip() or "5",
        }
        self.cfg.update({k: v for k, v in edited.items() if not self._locked(k)})

    def _commit_settings(self) -> bool:
        """Pull the Settings widgets into cfg and persist when something changed.

        The single write path shared by on_save, the folder picker, autosave and
        close. Live-applies the flood knob and re-derives on a download-folder
        change. Returns True if it actually wrote — so a mere focus shuffle that
        touched nothing doesn't rewrite the .env/config every time.
        """
        before = {k: self.cfg.get(k) for k in _SETTINGS_KEYS}
        self._pull_settings()
        if all(self.cfg.get(k) == before[k] for k in _SETTINGS_KEYS):
            return False
        self.persist()
        self._apply_flood_threshold()  # live-apply the rate-limit cutoff to the client
        if self.cfg.get("download_dir") != before["download_dir"]:
            # New folder → re-check every file against it: statuses (a moved file
            # that's actually present becomes "completed" again) and the free-space
            # readout both key off download_dir, so re-derive from the new path.
            self._load_items_from_scans()
        return True

    def on_save(self) -> None:
        self._commit_settings()
        self.set_status(f"Saved to config — downloads go to {self.cfg['download_dir']}")

    def _autosave(self, _event: object = None) -> None:
        """Persist a Settings edit the instant the user leaves the field.

        Bound to ``<FocusOut>``/``<Return>`` on every Settings entry so changes
        stick on restart without clicking Save — the same commit path as on_save,
        just a quieter status line, and silent when nothing actually changed.
        Autosave is why an edit survives a macOS ⌘-Q that never fires the close
        handler.
        """
        try:
            if self._commit_settings():
                self.set_status("Settings saved.")
        except (OSError, tk.TclError):
            pass

    def _sync_settings_widgets(self) -> None:
        """Push cfg back into the Settings entry vars — after an import replaces them."""
        self.api_id_var.set(self.cfg["api_id"])
        self.api_hash_var.set(self.cfg["api_hash"])
        self.ext_var.set(self.cfg["extensions"])
        self.limit_var.set(self.cfg["scan_limit"])
        self.conc_var.set(self.cfg["download_concurrency"])
        self.conns_var.set(self.cfg["download_connections"])
        self.flood_var.set(self.cfg["flood_sleep_threshold"])
        # download_dir is intentionally never imported — leave dir_var untouched.

    def on_export_settings(self) -> None:
        """Write a portable bundle of settings + channels + download list to a file.

        The login session is never included (see portable.py). Credentials ride
        along only when the checkbox is ticked.
        """
        self._commit_settings()  # flush any typed-but-unsaved field into cfg first
        path = filedialog.asksaveasfilename(
            title="Export Settings",
            defaultextension=".json",
            initialfile="tg-downloader-export.json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        include = bool(self._export_secrets_var.get())
        bundle = export_bundle(
            self.cfg,
            self.scans,
            include_secrets=include,
            exported_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        try:
            write_bundle(bundle, path)
        except OSError as e:
            messagebox.showerror("Export Failed", str(e))
            return
        n_files = sum(len(s.files) for s in self.scans.values())
        note = " with credentials" if include else ""
        self.set_status(
            f"Exported {len(bundle['channels'])} channel(s), {n_files} file(s){note} to {path}"
        )
        messagebox.showinfo(
            "Export Complete",
            f"Saved your settings, channels and download list to:\n{path}\n\n"
            "Your login session was not included — sign in again on the other machine.",
        )

    def on_import_settings(self) -> None:
        """Merge a bundle written by Export into this machine's config and scans."""
        path = filedialog.askopenfilename(
            title="Import Settings",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            imported = import_bundle(read_bundle(path))
        except (OSError, ValueError) as e:  # ValueError covers json.JSONDecodeError
            messagebox.showerror("Import Failed", f"Could not read this export file.\n\n{e}")
            return

        creds_note = " and API credentials" if imported.credentials else ""
        if not messagebox.askyesno(
            "Import Settings",
            f"Import {len(imported.channels)} channel(s), {imported.file_count()} file(s)"
            f"{creds_note}?\n\n"
            "This merges with your current settings and download list. Your download "
            "folder and login are left unchanged.",
        ):
            return

        self.cfg.update(imported.config)
        if imported.credentials:
            self.cfg.update(imported.credentials)
        self.cfg["channels"] = merge_channels(self.cfg["channels"], imported.channels)
        self.scans = merge_imported_scans(self.scans, imported.scans)

        self.persist()
        self._persist_scans()
        self._apply_flood_threshold()  # a new rate-limit knob applies live
        self._sync_settings_widgets()
        self._refresh_channel_list()
        self._load_items_from_scans()  # re-derive statuses against this machine's folder
        self.set_status(
            f"Imported {len(imported.channels)} channel(s) and {imported.file_count()} file(s)."
        )

    def on_connect(self) -> None:
        self.on_save()
        self.worker.submit(self._connect())

    async def _connect(self) -> None:
        client = await self._ensure_client()
        if client:
            self.set_status(f"Connected as {self._account_name}.")

    def on_logout(self) -> None:
        async def _out():
            if self.client:
                await self.client.log_out()
                self.client = None
            self._account_name = ""
            self.post(lambda: self._set_light(False))
            self.set_status("Logged out.")

        self.worker.submit(_out())

    # -- connection light ----------------------------------------------

    def _set_light(self, connected: bool, detail: str = "") -> None:
        """Paint both indicators (bottom bar + Settings) green/red. Main thread only.

        ``detail`` is the offline reason from :func:`connection_state` (e.g. "No
        internet connection" vs "Telegram unreachable"); shown next to the red dot
        so the light says *why*, not just that we're down.
        """
        colour = "#2e7d32" if connected else "#c62828"
        self.conn_dot.configure(foreground=colour)
        self.conn_light.configure(foreground=colour)
        if connected:
            self.conn_var.set(
                f"Connected as {self._account_name}" if self._account_name else "Connected"
            )
        else:
            self.conn_var.set(detail or "Not connected")

    async def _connectivity_monitor(self) -> None:
        """Keep the light honest and drive the offline gate.

        Runs for the app's lifetime on the worker loop. ``is_connected()`` reports
        *intent* — it stays True right through an outage while Telethon's
        auto-reconnect spins — so we can't trust it. Each tick
        (:data:`CONNECTIVITY_POLL` s):

        * **Path down** (can't TCP-reach the DC) for :data:`OFFLINE_PROBE_THRESHOLD`
          polls in a row: set ``_offline`` (parks every worker in
          :meth:`_wait_until_ready`) and paint the light red, telling "no internet"
          from "Telegram unreachable" via a public-host probe. A *single* miss is
          ignored — opening a fresh socket to the DC times out intermittently on a
          throttled account or a saturated link while the live download senders flow
          fine, so one blip must not falsely flip the app offline.
        * **Path back but still gated**: a reachable socket is *not* enough — the
          MTProto session may still be dead, so releasing workers here is what made
          a resumed file flicker paused↔downloading. Instead
          :func:`ensure_connected` forces a real reconnect and confirms a request
          round-trips; only then is ``_offline`` cleared and the light greened.
        * **Healthy**: keep the light green with a cheap probe — no reconnect churn.

        ``_offline`` is also set the instant a worker hits ``ConnectionLost``
        (:meth:`_handle_offline`); the recovery path above is the only thing that
        clears it, so the gate never reopens before the link genuinely works.
        """
        while not self._closing:
            client = self.client
            # No client yet (not logged in) isn't an "outage" — leave the gate open
            # and let _startup_connect / _ensure_client own the light.
            if client is None:
                await asyncio.sleep(CONNECTIVITY_POLL)
                continue
            try:
                path_up = await probe_endpoint(*telegram_endpoint(client))
            except Exception:  # a probe blowing up must not kill the monitor
                path_up = False

            if not path_up:
                # Debounce: a single failed probe is usually a blip (a fresh socket
                # to the DC refused under throttling, not a real outage), so don't
                # park the batch or flip the light red until we've missed several in
                # a row. The live download senders keep working through a blip, which
                # is why the user sees "no internet" while downloads are fine.
                self._probe_failures += 1
                if self._probe_failures >= OFFLINE_PROBE_THRESHOLD:
                    try:
                        net_ok = await check_internet()
                    except Exception:
                        net_ok = False
                    _, detail = connection_state(net_ok, False)
                    self._offline = True
                    self.post(lambda d=detail: self._set_light(False, d))
                # else: below threshold — leave the light as it is and keep going.
            elif self._offline:
                # Socket reachable but the gate's still closed: force a full
                # reconnect (main *and* media-DC senders — a bare get_me on the main
                # DC would pass while downloads still fail, flickering) and only
                # release the workers once a request round-trips. This cancels any
                # in-flight download, but the gate is still closed here, so each
                # cancelled file re-queues itself and resumes (see _run_download's
                # CancelledError branch) rather than being stranded or overtaken.
                self._probe_failures = 0
                self.post(lambda: self._set_light(False, "Reconnecting to Telegram…"))
                if await ensure_connected(client):
                    self._offline = False
                    self.post(lambda: self._set_light(True))
                    self.set_status("Back online — resuming downloads.")
            else:
                self._probe_failures = 0
                self.post(lambda: self._set_light(True))
            await asyncio.sleep(CONNECTIVITY_POLL)

    # ------------------------------------------------------------------
    # Telethon client
    # ------------------------------------------------------------------

    async def _startup_connect(self) -> None:
        """At launch, reconnect from the saved session without prompting."""
        client = await self._ensure_client(interactive=False)
        if client:
            self.set_status(f"Connected as {self._account_name}.")
            # Now that we're logged in and the cached list is loaded, offer the
            # countdown that auto-presses Start (only if there's pending work).
            self.post(self._maybe_autostart_countdown)

    async def _ensure_client(self, interactive: bool = True):
        """Return a live client. ``interactive`` may prompt for login; silent won't.

        The lock serialises the connect: a startup reconnect and a user clicking
        Fetch at the same instant must not build two clients on one session file.
        """
        if self._client_lock is None:
            self._client_lock = asyncio.Lock()  # first use is on the worker loop
        async with self._client_lock:
            if self.client and self.client.is_connected():
                self.post(lambda: self._set_light(True))
                return self.client
            # Replacing a dead client: disconnect the old one first so Telethon
            # cancels its Connection/MTProtoSender loops (and any borrowed exported
            # senders from parallel downloads) cleanly. Just dropping the reference
            # orphans those tasks — they get GC'd as "Task was destroyed but it is
            # pending", and the GeneratorExit that finalises the orphaned coroutines
            # can surface on the *main* thread with a misleading traceback (e.g. in
            # models.as_record, whatever it happened to be running during GC).
            if self.client is not None:
                old, self.client = self.client, None
                try:
                    await asyncio.wait_for(old.disconnect(), timeout=5)
                except Exception:
                    pass  # best-effort teardown — never block the reconnect on it
            try:
                if interactive:
                    self.client = await build_client(
                        self.cfg["api_id"],
                        self.cfg["api_hash"],
                        phone=lambda: self.ask("Phone number (with country code):"),
                        code_callback=lambda: self.ask("Login code Telegram sent you:"),
                        password=lambda: self.ask("Two-factor password:", secret=True),
                        session=SESSION_PATH,
                        flood_sleep_threshold=self._flood_threshold(),
                    )
                else:
                    client = await connect_if_authorized(
                        self.cfg["api_id"],
                        self.cfg["api_hash"],
                        session=SESSION_PATH,
                        flood_sleep_threshold=self._flood_threshold(),
                    )
                    if not client:
                        return None  # not logged in yet — stay red, no popup
                    self.client = client
                me = await self.client.get_me()
                self._account_name = (
                    getattr(me, "first_name", "") or getattr(me, "username", "") or "you"
                )
                self.post(lambda: self._set_light(True))
                return self.client
            except Exception as e:
                msg = str(e)
                if interactive:
                    self.post(
                        lambda: (
                            self._set_light(False),
                            messagebox.showerror("Connection Failed", msg),
                        )
                    )
                    self.set_status("Connection failed.")
                else:
                    self.post(lambda: self._set_light(False))
                return None

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def on_update_scan(self) -> None:
        """Fetch only messages newer than each channel's watermark."""
        self._begin_scan(full=False)

    def on_full_rescan(self) -> None:
        """Re-walk each channel's whole history, rebuilding its list."""
        self._begin_scan(full=True)

    def _begin_scan(self, full: bool) -> None:
        self.on_save()
        if not self.cfg["channels"]:
            messagebox.showinfo("No Channels", "Add at least one channel in the Channels tab.")
            return
        self.set_status("Rescanning…" if full else "Checking for new files…")
        self.worker.submit(self._scan(full))

    async def _scan(self, full: bool) -> None:
        client = await self._ensure_client()
        if not client:
            return

        allowed = parse_extensions(self.cfg["extensions"])
        try:
            full_limit = int(self.cfg["scan_limit"]) or None
        except ValueError:
            full_limit = None

        for ch in list(self.cfg["channels"]):
            cid = str(ch["id"])
            try:
                entity = await client.get_entity(ch["id"])
            except Exception as e:
                self.set_status(f"Cannot open {ch['id']}: {e}")
                continue

            title = getattr(entity, "title", str(ch["id"]))
            ch["title"] = ch.get("title") or title

            if is_content_protected(entity):
                self.post(
                    lambda t=title: messagebox.showwarning(
                        "Content Protected",
                        f"“{t}” has ‘restrict saving content’ enabled.\n"
                        "Telegram may block downloads from it.",
                    )
                )

            existing = self.scans.get(cid)
            # Update scans start above the last seen id; full scans start at 0 and
            # honour the user's scan_limit for bounding a huge history.
            min_id = 0 if full else (existing.last_msg_id if existing else 0)
            limit = full_limit if full else None
            watermark = await latest_message_id(client, entity)

            found: list[FileItem] = []
            async for item in iter_matching_files(client, entity, allowed, limit, min_id):
                found.append(item)
                if len(found) % 25 == 0:
                    self.set_status(f"Scanning {title}… {len(found)} files")
            self.scans[cid] = merge_scan(existing, found, watermark=watermark, full=full)
            kind = "matching" if full else "new"
            self.set_status(f"{title}: {len(found)} {kind} file(s)")

        self.persist()  # the scan filled in channel titles; keep them
        self.post(self._finish_scan)

    def _finish_scan(self) -> None:
        self._load_items_from_scans()
        self._persist_scans()
        self.set_status(f"{len(self.items)} file(s) across {len(self.scans)} channel(s).")

    # ------------------------------------------------------------------
    # Downloading
    # ------------------------------------------------------------------

    def _maybe_autostart_countdown(self) -> None:
        """Right after a launch reconnect, offer a JDownloader-style countdown that
        kicks the queue off on its own — unless the user cancels first.

        One-shot (armed only by the startup connect) and gated: we must be idle
        and have pending work, or there'd be nothing to auto-start. Runs on the
        main thread (posted from the worker), so it can touch Tk directly.
        """
        if self._autostart_done or self.state != "idle":
            return
        pending = any(it.selected and it.status not in SETTLED for it in self.items.values())
        if not pending:
            return
        self._autostart_done = True
        self._show_autostart_countdown()

    def _show_autostart_countdown(self) -> None:
        """A centred, modal countdown that presses Start when it reaches zero.

        Cancellable by button, Escape, or the window close box — a present user
        can veto it, while an unattended boot resumes downloading on its own.
        """
        win = tk.Toplevel(self)
        win.title("Auto-Start Downloads")
        win.transient(self)
        win.resizable(False, False)

        frame = ttk.Frame(win, padding=24)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame, justify="center", text=f"Auto-start downloads in {AUTOSTART_SECONDS} seconds"
        ).pack(pady=(0, 4))
        count = tk.Label(frame, text=str(AUTOSTART_SECONDS), font=("", 44, "bold"))
        count.pack(pady=(0, 18))

        def close() -> None:
            if self._autostart_after is not None:
                self.after_cancel(self._autostart_after)
                self._autostart_after = None
            win.destroy()

        def start_now() -> None:
            close()
            self.on_start()

        def cancel() -> None:
            close()
            self.set_status("Auto-start cancelled.")

        btns = ttk.Frame(frame)
        btns.pack()
        ttk.Button(btns, text="Start Now", width=12, command=start_now).pack(
            side="left", padx=8
        )
        ttk.Button(btns, text="Cancel", width=12, command=cancel).pack(side="left", padx=8)

        win.bind("<Escape>", lambda e: cancel())
        win.protocol("WM_DELETE_WINDOW", cancel)

        remaining = [AUTOSTART_SECONDS]

        def tick() -> None:
            remaining[0] -= 1
            count.config(text=str(max(0, remaining[0])))
            if remaining[0] <= 0:
                self._autostart_after = None
                close()
                self.on_start()
            else:
                self._autostart_after = self.after(1000, tick)

        self._autostart_after = self.after(1000, tick)

        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - w) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - h) // 2)
        win.geometry(f"+{x}+{y}")
        win.grab_set()

    def on_start(self) -> None:
        # Supersede any pending auto-restart waiter so a manual Start (including
        # the one the waiter itself fires) can't be shadowed by a stale gen.
        self._reconnect_gen += 1
        if self.state == "paused":
            self.state = "running"
            self.worker.call(self._pause_event.set)
            self._sync_buttons()
            self.set_status("Resumed.")
            return
        if self.state == "running":
            return
        todo = [
            iid for iid, it in self.items.items() if it.selected and it.status not in SETTLED
        ]
        if not todo:
            messagebox.showinfo("Nothing to Do", "Tick at least one file first.")
            return
        # Pre-flight the download folder *before* a single worker starts. A folder
        # that's gone (an unmounted drive) fails every file identically, and a run
        # launched into one would march through the whole queue converting it to
        # errors — so refuse to start and leave every tick where it is.
        problem = download_dir_problem(self.cfg["download_dir"])
        if problem:
            log.error("Refusing to start: %s", problem)
            messagebox.showerror("Download Folder Unavailable", problem)
            self.set_status(f"⚠ {problem}")
            return
        self.state = "running"
        self._stop_flag = False
        self._sync_buttons()
        self.worker.submit(self._download(todo))

    def on_pause(self) -> None:
        if self.state != "running":
            return
        self.state = "paused"
        self.worker.call(self._pause_event.clear)
        self._sync_buttons()
        self.set_status("Paused — press Resume to continue.")

    def on_stop(self) -> None:
        if self.state == "idle":
            return
        self._reconnect_gen += 1  # cancel any pending auto-restart wait
        if self.state == "waiting":
            # No run is live — just tear down the reconnect wait.
            self._auto_restart_attempts = 0
            self.state = "idle"
            self._sync_buttons()
            self.set_status("Auto-retry cancelled.")
            return
        self._stop_flag = True
        if self._pause_event:
            # Unblock a paused worker so it can observe the stop flag.
            self.worker.call(self._pause_event.set)
        self.state = "idle"
        self._sync_buttons()
        self.set_status("Stopping…")

    def _sync_buttons(self) -> None:
        def do():
            running = self.state == "running"
            paused = self.state == "paused"
            waiting = self.state == "waiting"  # auto-restart waiting on reconnect
            self.btn_start.config(
                text="▶  Resume" if paused else "▶  Start",
                state="disabled" if running else "normal",
            )
            self.btn_pause.config(state="normal" if running else "disabled")
            self.btn_stop.config(state="normal" if running or paused or waiting else "disabled")

        self.post(do)

    #: Statuses that mean the row is finished — the overlay bar comes off.
    _BAR_DONE = (COMPLETED, ON_DISK)

    def _set_row(self, iid: str, status: str) -> None:
        def do():
            it = self.items.get(iid)
            if not it:
                return
            it.status = status
            # Leaving the downloading state stops the flow of bytes, so the speed
            # and ETA readouts are stale — drop them (the cells go blank below).
            if not it.status.strip().lower().startswith("downloading"):
                it.speed = 0.0
            if self.tree.exists(iid):
                self.tree.set(iid, "status", self._status_text(it))
                self.tree.set(iid, "note", self._note_text(it))
                self.tree.set(iid, "progress", self._progress_text(it))
                self.tree.set(iid, "speed", self._speed_text(it))
                self.tree.set(iid, "eta", self._eta_text(it))
                # Always recompute the colour from the current status — otherwise a
                # retry back to "downloading…" would keep a stale red error tag.
                self.tree.item(iid, tags=(self._row_tag(it),))
            if status in self._BAR_DONE:
                self._destroy_bar(iid)
                self._reindex_queue()  # a settled file drops out — renumber
            else:
                self._draw_bar(iid)  # e.g. paused/stopped/error keep their bar
            # A status change can flip which side of a header filter this row is
            # on (e.g. it just became "completed"); re-apply so it hides/shows now.
            self._apply_row_filter(iid)

        self.post(do)

    def _set_progress(self, iid: str, pct: int) -> None:
        """Update the Progress cell + its bar — cheap enough for every chunk."""

        def do():
            it = self.items.get(iid)
            if not it:
                return
            it.progress = pct
            if self.tree.exists(iid):
                self.tree.set(iid, "progress", self._progress_text(it))
            if self._bar_pct(it) is not None:
                self._bar_active.add(iid)
                self._draw_bar(iid)
            else:
                self._destroy_bar(iid)

        self.post(do)

    def _update_file_speeds(self) -> None:
        """Repaint each active file's Speed + Time-left from the per-file accumulator.

        Runs on the ``_tick_counter`` timer (main thread), mirroring the batch
        meter's :meth:`_update_speed`: speed = bytes pulled since the previous tick
        ÷ elapsed. Because it's timer-driven rather than a 1s window inside
        ``on_progress``, a short throttle (bytes briefly stop) makes the number dip
        toward 0 for a tick instead of blanking the cell — and a burst shorter than
        the old 1s window still gets a reading. A file that isn't actively pulling
        (paused, or a long throttle flipped its status off "downloading…") re-anchors
        and shows nothing, so we never report a stale rate beside a parked row.
        """
        now = time.monotonic()
        running = self._pause_event is not None and self._pause_event.is_set()
        for iid, recv in self._file_recv.copy().items():  # copy: worker may add keys
            it = self.items.get(iid)
            if it is None:
                continue
            prev = self._file_spd.get(iid)
            self._file_spd[iid] = (recv, now)
            downloading = running and it.status.strip().lower().startswith("downloading")
            if not downloading or prev is None:
                continue  # parked (or first sample) — anchored above, show nothing new
            prev_recv, prev_t = prev
            dt = now - prev_t
            if dt <= 0:
                continue
            it.speed = max(0.0, (recv - prev_recv) / dt)
            if self.tree.exists(iid):
                self.tree.set(iid, "speed", self._speed_text(it))
                self.tree.set(iid, "eta", self._eta_text(it))

    def _concurrency(self) -> int:
        try:
            return max(1, min(16, int(self.cfg.get("download_concurrency", "1"))))
        except (ValueError, TypeError):
            return 4

    def _connections(self) -> int:
        """Connections per file (1–16). Pipelines N requests per file — see
        ``telegram._stream_to_part_parallel``. 1 keeps the one-connection path."""
        try:
            return max(1, min(16, int(self.cfg.get("download_connections", "4"))))
        except (ValueError, TypeError):
            return 4

    def _flood_threshold(self) -> float:
        """Seconds below which Telethon absorbs a flood/premium wait itself.

        Waits longer than this are raised and handled by our coordinator (pause
        all, show the cause, retry). See telegram.FLOOD_SLEEP_THRESHOLD.
        """
        try:
            return max(0.0, float(self.cfg.get("flood_sleep_threshold", "5")))
        except (ValueError, TypeError):
            return 5.0

    def _apply_flood_threshold(self) -> None:
        """Push the current threshold onto a live client so a Save takes effect now.

        ``flood_sleep_threshold`` is a settable Telethon property, so a running
        download starts honouring the new cutoff immediately — no reconnect. It
        touches the client, so the assignment runs on the worker loop; a no-op
        when nothing's connected (the value is picked up when a client is built).
        """
        client = self.client
        if client is None:
            return
        threshold = self._flood_threshold()
        self.worker.call(lambda: setattr(client, "flood_sleep_threshold", threshold))

    async def _download(self, todo: list[str]) -> None:
        client = await self._ensure_client()
        if not client:
            self.state = "idle"
            self._sync_buttons()
            return

        self._pause_event = asyncio.Event()
        self._pause_event.set()
        # Per-file controls are live-run state — clear any stale intents so a file
        # left "paused"/"stopped" by a previous run downloads normally now.
        self._file_pause.clear()
        self._file_stop.clear()
        self._active_iids.clear()
        self._dl_queue = asyncio.PriorityQueue()  # (priority, iid); lower runs first
        self._dl_seq = 0
        self._dl_front_seq = 0
        self._rate_until = 0.0
        self._rate_lead_taken = False
        self._storage_fault = ""
        self._done_count = 0
        self._dl_start = time.monotonic()  # reset the ETA clock/byte counter
        self._dl_bytes = 0
        self._speed_bytes = 0  # re-seed the speed meter on the next tick
        self._speed_t = 0.0
        self._speed_now = 0.0
        self._file_recv.clear()
        self._file_spd.clear()
        total = len(todo)
        self.post(lambda: self.progress.config(maximum=total, value=0))
        log.info("Run started: %d file(s), concurrency %d", total, self._concurrency())

        # Finishing a half-started file takes priority over opening a fresh one, so
        # a backlog of stuck-at-4% partials drains instead of growing. One priority
        # queue expresses the whole plan: `split_resume_first` sorts the files that
        # already have a `.part` (oldest first, so the longest-stalled finish
        # first) into the RESUME lane and the rest into FRESH, in queue order. The
        # pool just drains that queue; "Download now" (LANE_URGENT) and a
        # rate-limited re-queue (back to LANE_RESUME) slot in by priority alone. No
        # per-lane worker split, no partial cap — incomplete-first ordering already
        # keeps the number of live `.part`s at the worker count. Pause, stop and the
        # shared cooldown are threaded in via _wait_until_ready.
        dl_dir = self.cfg["download_dir"]
        pairs = ((iid, self.items[iid]) for iid in todo if iid in self.items)
        resume_iids, fresh_iids = split_resume_first(pairs, dl_dir)
        for iid in resume_iids:
            self._enqueue(LANE_RESUME, iid)
        for iid in fresh_iids:
            self._enqueue(LANE_FRESH, iid)

        await run_download_pool(
            self._dl_queue,
            self._concurrency(),
            download_one=lambda iid: self._download_one(client, iid, total),
            wait_ready=self._wait_until_ready,
            stop=lambda: self._stop_flag,
        )

        user_stopped = self._stop_flag  # capture before the teardown resets it
        self.state = "idle"
        self._stop_flag = False
        self._dl_queue = None
        self._file_pause.clear()
        self._file_stop.clear()
        self._rate_until = 0.0
        self._rate_lead_taken = False
        self._sync_buttons()
        self.post(self._persist_scans)  # remember what finished (drives "moved")
        self.post(self._update_count)  # final tally once the ticker has stopped
        self.post(lambda: self.eta_var.set(""))  # clear the ETA when idle
        self.post(lambda: self.speed_var.set(""))  # and the speed meter
        # Errored rows split two ways: still-ticked ones (a systemic no-connection
        # failure kept selected) that Start retries, and auto-set-aside ones
        # (unticked → "skipped") a re-tick retries. Report them separately so the
        # advice matches what pressing Start would actually do.
        errored = [it for it in self.items.values() if it.status.startswith("error")]
        retriable = [it for it in errored if it.selected]
        set_aside = [it for it in errored if not it.selected]
        log.info(
            "Run finished: %d/%d done, %d to retry, %d set aside",
            self._done_count,
            total,
            len(retriable),
            len(set_aside),
        )
        if self._storage_fault:
            # A disk fault ended the run, not an outage: waiting for the network
            # would never fix it, and the fix (plug the drive back in) is the
            # user's. Say so and leave the queue ticked for the next Start.
            self.set_status(
                f"⚠ Stopped — {self._storage_fault}. "
                f"{self._done_count}/{total} done; the rest stayed queued."
            )
            return
        if not retriable:
            self._auto_restart_attempts = 0  # clean finish → fresh slate
        # A run the user didn't stop that still has queued (ticked) files is a
        # mid-run outage — wait for the link to return and Start again on its own,
        # rather than stranding an overnight batch until a manual Start.
        elif not user_stopped and self._arm_auto_restart(len(retriable)):
            return  # _await_reconnect now owns the status line and the restart
        segments = []
        if retriable:
            seg = f"{len(retriable)} failed, press Start to retry"
            if any("no connection" in it.status for it in retriable):
                seg += " · check your internet connection"
            segments.append(seg)
        if set_aside:
            segments.append(f"{len(set_aside)} set aside after errors — re-tick to retry")
        tail = " — " + "; ".join(segments) if segments else f" in {self.cfg['download_dir']}"
        self.set_status(f"Finished — {self._done_count}/{total} files{tail}")

    def _arm_auto_restart(self, pending: int) -> bool:
        """Arm a wait-for-reconnect-then-Start after a mid-run outage.

        Called on the worker loop from :meth:`_download`'s teardown when a run
        the user didn't stop ended with ``pending`` files still queued. Returns
        ``True`` when a restart is armed (the caller skips its "Finished"
        message), ``False`` when we've given up. Bounded by
        ``AUTO_RESTART_MAX_STALLED`` consecutive restarts that downloaded nothing
        — the ``_done_count > 0`` reset means real progress refills the budget,
        so only a genuinely stuck situation exhausts it.
        """
        if self._done_count > 0:
            self._auto_restart_attempts = 0  # this run made progress — not stalled
        if self._auto_restart_attempts >= AUTO_RESTART_MAX_STALLED:
            log.warning(
                "Auto-restart gave up after %d stalled attempts — %d file(s) still queued",
                self._auto_restart_attempts,
                pending,
            )
            self._auto_restart_attempts = 0
            return False
        self._auto_restart_attempts += 1
        self._reconnect_gen += 1
        gen = self._reconnect_gen
        self.state = "waiting"
        self._sync_buttons()
        self.worker.submit(self._await_reconnect(gen, pending))
        return True

    async def _await_reconnect(self, gen: int, pending: int) -> None:
        """Poll the connection on the worker loop, then re-press Start.

        Bails without firing if ``gen`` goes stale — a manual Start or Stop (or
        the app closing) bumps ``_reconnect_gen`` to cancel a pending waiter, so
        we never launch a duplicate or unwanted run.
        """
        log.info("Auto-restart armed: waiting for connection to retry %d file(s)", pending)
        self.set_status(
            f"Connection lost — {pending} file(s) queued; auto-retrying when back online…"
        )
        while gen == self._reconnect_gen:
            client = self.client
            if client and client.is_connected():
                await asyncio.sleep(AUTO_RESTART_SETTLE)  # let the link settle
                break
            await asyncio.sleep(AUTO_RESTART_POLL)
        if gen != self._reconnect_gen:
            return  # superseded by a manual Start/Stop or shutdown
        self.post(lambda: self._fire_auto_restart(gen))

    def _fire_auto_restart(self, gen: int) -> None:
        """On the UI thread, launch the retry run — unless something pre-empted us."""
        if gen != self._reconnect_gen or self.state != "waiting":
            return  # a manual Start/Stop got there first
        log.info("Auto-restart firing — connection restored")
        self.state = "idle"  # let on_start proceed past its running/paused guards
        self.on_start()

    def _enqueue(self, lane: int, iid: str, *, front: bool = False) -> None:
        """Push ``iid`` onto the run's priority queue in ``lane``.

        ``_dl_seq`` keeps items in insertion order within a lane and breaks ties,
        so the FileItem payload never has to be comparable. ``front=True`` uses a
        separate *decrementing* counter so the item sorts ahead of everything else
        in its lane — the rate-limit re-queue path, so the file that just parked on
        the cooldown resumes next instead of a different same-lane file being
        picked ahead of it (visible mainly at concurrency 1). Touches the queue, so
        callers on the UI thread go through ``worker.call``; a no-op if no run is
        live (``_dl_queue is None``).
        """
        q = self._dl_queue
        if q is None:
            return
        if front:
            self._dl_front_seq -= 1
            seq = self._dl_front_seq
        else:
            self._dl_seq += 1
            seq = self._dl_seq
        q.put_nowait(((lane, seq), iid))

    async def _wait_until_ready(self) -> None:
        """Park a worker while paused, offline, or a rate-limit cooldown is on.

        Returns as soon as we're clear to download again (or a stop is asked).
        Three gates, any of which parks the worker: the user pause (`_pause_event`),
        the global offline flag (`_offline`, owned by the connectivity monitor —
        so one dropped link stalls the whole batch in place), and the shared
        rate-limit deadline (`_rate_until`). When a rate-limit cooldown lapses the
        first worker resumes immediately (so a download is flowing again with no
        added delay) and the rest stagger their wake by a small random window, so
        they don't all hit Telegram on the same tick and re-trigger a throttle (or
        a reconnect storm).
        """
        loop = asyncio.get_running_loop()
        waited = False
        while not self._stop_flag:
            await self._pause_event.wait()  # user pause
            if self._offline:  # link down — park until the monitor clears it
                waited = True
                await asyncio.sleep(0.5)  # re-check often; wake fast on reconnect
                continue
            remaining = self._rate_until - loop.time()  # rate-limit cooldown
            if remaining <= 0:
                # The first worker out of the cooldown resumes instantly so a
                # download is flowing again immediately; the rest stagger over a
                # small window so they don't hit Telegram in unison and re-trip the
                # throttle. `_rate_lead_taken` is set with no await between the
                # check and the assignment, so exactly one worker takes the lead
                # (the pool runs single-threaded on the worker loop).
                if waited and self._rate_lead_taken:
                    await asyncio.sleep(random.uniform(0, 0.3))
                self._rate_lead_taken = True
                return
            waited = True
            await asyncio.sleep(min(remaining, 0.5))  # re-check often; wake fast on resume

    def _handle_rate_limit(self, iid: str, it: FileItem, exc: RateLimited) -> None:
        """A file hit a flood wait: extend the global cooldown and re-queue it.

        Every worker parks behind ``_rate_until`` (via :meth:`_wait_until_ready`),
        so one file's rate limit pauses the whole batch. The file is **always** put
        back at the *head* of the RESUME lane to retry once the wait elapses (it now
        has a ``.part``, so it jumps ahead of fresh files, and it resumes before any
        other queued file so the same download continues) — a throttle is a *wait*, not a
        failure, and the ``.part`` grows on every attempt, so a rate-limited or
        speed-capped file is never given up on. We honour Telegram's own
        ``retry_after`` when it names one, else wait a short fixed cooldown
        (``RATE_LIMIT_DEFAULT``) and retry; the per-worker wake stagger in
        :meth:`_wait_until_ready` keeps the batch from re-tripping the throttle.
        """
        wait = exc.retry_after or RATE_LIMIT_DEFAULT
        loop = asyncio.get_running_loop()
        self._rate_until = max(self._rate_until, loop.time() + wait)
        self._rate_lead_taken = False  # grant one instant resume when this lapses
        self._set_row(iid, exc.label)  # "rate limited" or "speed capped"
        remaining = int(max(0, self._rate_until - loop.time()))
        log.warning(
            "%s on %s — global cooldown ~%.0fs (will keep retrying)",
            exc.label,
            it.name,
            wait,
        )
        self.set_status(
            f"⚠ {exc} — pausing all downloads ~{remaining}s, then resuming ({it.name})."
        )
        # Re-queue at the head of the RESUME lane so this exact file resumes once
        # the cooldown lapses — not a different queued file (matters at concurrency
        # 1, where jumping files would leave this one half-downloaded).
        self._enqueue(LANE_RESUME, iid, front=True)

    def _handle_offline(self, iid: str, it: FileItem, exc: ConnectionLost) -> None:
        """The link dropped mid-download: pause the whole batch and re-queue this file.

        The connectivity analogue of :meth:`_handle_rate_limit` — the gate is
        reachability, not a timer. We set ``_offline`` at once for instant reaction
        (the monitor would catch it within a probe cycle anyway, but a fast worker
        could pop several fresh files first), mark the row as parked-for-connection
        (it reads "paused", amber, *not* an error — a dropped link isn't this file's
        fault), and put the file back at the head of the RESUME lane so **this exact
        download resumes with its ``.part``** the moment we're back — instead of
        advancing to the next file and churning the whole queue into "no connection"
        errors. Every worker then parks in :meth:`_wait_until_ready` until the
        monitor clears ``_offline``.
        """
        self._offline = True
        self._set_row(iid, exc.label)  # "no connection" → displays as paused (amber)
        log.warning("Connection lost on %s — pausing the batch until the link returns", it.name)
        self.set_status(
            f"⚠ Connection lost — pausing all downloads until back online ({it.name})."
        )
        self._enqueue(LANE_RESUME, iid, front=True)

    async def _download_one(self, client, iid: str, total: int) -> None:
        it = self.items.get(iid)
        if not it:
            return
        # A file can reach here twice at once — e.g. "Download now" pushes an iid
        # onto the urgent lane while it's still sitting in the fresh queue. Both
        # coroutines would append to the same .part and corrupt it, so the second
        # one bows out. Also honour a stop asked for before the download started.
        if iid in self._active_iids:
            return
        if iid in self._file_stop:
            self._file_stop.discard(iid)
            self._set_row(iid, "stopped")
            return
        self._active_iids.add(iid)
        try:
            await self._run_download(client, it, iid, total)
        finally:
            self._active_iids.discard(iid)
            self._file_recv.pop(iid, None)  # stop ticking speed for a finished file
            self._file_spd.pop(iid, None)

    async def _run_download(self, client, it: FileItem, iid: str, total: int) -> None:
        # We only reach here for a ticked file, so any past "set aside" verdict has
        # been overruled (the user re-ticked to retry) — clear it. If this attempt
        # fails again, _auto_skip_failed re-stamps it.
        it.set_aside = ""
        download_dir = self.cfg["download_dir"]
        if it.target_path(download_dir).exists():
            it.downloaded = True
            self._set_row(iid, COMPLETED)
            self._bump_done(total)
            return
        # Safety net: a same name+size file is already here (a repost under a
        # different id, or one placed by hand) — skip rather than overwrite it.
        if it.existing_duplicate(download_dir):
            self._set_row(iid, ON_DISK)
            self._bump_done(total)
            return

        part = it.part_path(download_dir)
        # Honour the parallel sparse format: a preallocated .part's size isn't its
        # byte count, so ask the telegram layer (reads the .idx sidecar).
        start_bytes = downloaded_bytes(part, it.size)
        start = int(start_bytes * 100 / it.size) if it.size else 0
        last = start
        prev_bytes = start_bytes  # recv is cumulative incl. resumed bytes
        self._file_recv[iid] = start_bytes  # seed the per-file speed accumulator

        async def on_progress(recv, tot):
            nonlocal last, prev_bytes
            # Honour stop (batch-wide or this one file) mid-file, not just between
            # files. A per-file stop keeps the .part, exactly like a batch stop.
            if self._stop_flag or iid in self._file_stop:
                raise asyncio.CancelledError()
            paused = False
            # Per-file pause: park *this* download alone until it's resumed
            # ("Download now") or stopped. Polled, since there's no per-file event.
            while (
                iid in self._file_pause and not self._stop_flag and iid not in self._file_stop
            ):
                if not paused:
                    self._set_row(iid, "paused")
                    paused = True
                await asyncio.sleep(0.25)
            # Batch-wide pause parks every download on the shared event.
            if not self._pause_event.is_set():
                if not paused:
                    self._set_row(iid, "paused")
                    paused = True
                await self._pause_event.wait()
            if self._stop_flag or iid in self._file_stop:
                raise asyncio.CancelledError()
            if paused:
                self._set_row(iid, "downloading…")
            # Count only newly-pulled bytes (not what was resumed from disk) so the
            # measured bandwidth reflects the network. Both accumulators are plain
            # writes read by the main-thread speed ticks (GIL-atomic, like _dl_bytes).
            if recv > prev_bytes:
                self._dl_bytes += recv - prev_bytes
                prev_bytes = recv
            self._file_recv[iid] = recv  # _update_file_speeds diffs this on its timer
            if tot:
                pct = recv * 100 // tot
                if pct != last:
                    last = pct
                    self._set_progress(iid, pct)

        def on_error(cause, attempt: int) -> None:
            # A transient, retried failure (timeout / dropped connection): show
            # the cause on the row and in the status bar so a stall isn't silent.
            # A mid-download drop ("Cannot send requests while disconnected") is
            # *not* "no internet" — Telegram routinely recycles a download
            # connection, and the retry (resuming the .part) rides it out. Word it
            # as a recoverable reconnect, not an outage, so a still-online user
            # isn't wrongly told their connection is down. A *genuine* outage only
            # reads "no connection" once local retries are exhausted and the batch
            # parks on the offline gate (see _handle_offline / the finish advice).
            if cause.kind == "no_connection":
                self._set_row(iid, "retry: reconnecting…")
                self.set_status(
                    f"⚠ Connection dropped — reconnecting for “{it.name}” (attempt {attempt})…"
                )
            else:
                self._set_row(iid, f"retry: {cause.label}")
                self.set_status(f"⚠ {cause.detail} — retrying “{it.name}” (attempt {attempt})…")

        try:
            self._set_row(iid, "downloading…")
            self._set_progress(iid, start)
            await download_item(
                client,
                it,
                download_dir,
                on_progress,
                connections=self._connections(),
                cancelled=lambda: self._stop_flag or iid in self._file_stop,
                on_error=on_error,
            )
            it.downloaded = True
            self._set_row(iid, COMPLETED)
            self._bump_done(total)
        except asyncio.CancelledError:
            # Keep the .part — that's what lets the next run resume this file.
            stopped = self._stop_flag or iid in self._file_stop
            if self._offline and not stopped:
                # Not a user pause/stop but a cancel during an outage (e.g. a forced
                # reconnect tore the connection down under us). Treat it exactly like
                # any offline stall: re-queue *this* file at the head of the RESUME
                # lane so it resumes, instead of stranding it as "paused".
                self._enqueue(LANE_RESUME, iid, front=True)
                self._set_row(iid, "no connection")
            else:
                # A per-file Stop reads "stopped" too; only a batch pause reads "paused".
                self._file_stop.discard(iid)
                self._set_row(iid, "stopped" if stopped else "paused")
        except RateLimited as e:
            # Pause every worker, wait, and re-queue this file — see the method.
            self._handle_rate_limit(iid, it, e)
        except ConnectionLost as e:
            # Link down: pause the whole batch and re-queue *this* file, rather
            # than advancing and churning the queue into errors — see the method.
            self._handle_offline(iid, it, e)
        except Exception as e:
            cause = classify(e)
            log.error("Download failed for %s: %s", it.name, e)
            if cause.kind == "no_storage":
                # The download folder itself is gone or unwritable. Every other
                # queued file would fail the same way, so stop the batch — see
                # the method.
                self._handle_storage_fault(iid, it, cause)
                return
            if "no longer available" in str(e):  # unresumable — drop the stub
                part.unlink(missing_ok=True)
                part_index_path(part).unlink(missing_ok=True)
            # A per-file failure that won't heal by waiting (not-found, protected,
            # timeout, or an unknown error): set it aside so the rest of the batch
            # finishes first and completes sooner. Auto-skip (untick → "skipped")
            # and drop it to the bottom for a later manual retry; the .part is kept
            # so a re-tick resumes it. (no_connection no longer reaches here — it's
            # raised as ConnectionLost and handled by the offline gate above.)
            self._auto_skip_failed(iid, cause.label)
            self.set_status(
                f"Set aside “{it.name}” after repeated errors ({cause.detail}) "
                "— re-tick to retry"
            )

    def _handle_storage_fault(self, iid: str, it: FileItem, cause) -> None:
        """Stop the whole run when the download folder becomes unusable.

        Runs on the worker loop. A disk fault — the external drive unmounted, the
        volume went read-only, the disk filled — is systemic: it will fail every
        remaining file in the queue in exactly the same way. Letting it run its
        course is what once turned a batch of 3,693 queued files into 3,693
        "set aside" rows in a few seconds, silently unticking the whole queue and
        leaving nothing for the next launch (or the auto-start countdown) to pick
        up. So: keep this row **ticked**, raise the stop flag, and leave every
        other tick untouched, so pressing Start once the drive is back resumes the
        batch exactly where it was.

        ``_pause_event.set()`` matters for the same reason it does in
        :meth:`on_stop` — a worker parked on the pause gate will never observe the
        stop flag unless something wakes it.
        """
        self._storage_fault = cause.detail
        log.error("Storage fault — stopping the run: %s", cause.detail)
        self._stop_flag = True
        if self._pause_event:
            self._pause_event.set()  # already on the worker loop; wake the parked workers
        it.speed = 0.0
        self._set_row(iid, f"error: {cause.label}")  # stays selected → Start retries it
        self.set_status(f"⚠ {cause.detail} — stopped; your queue is intact.")

    def _auto_skip_failed(self, iid: str, label: str) -> None:
        """Set a genuinely-failed download aside and drop it to the bottom of the queue.

        Called on the worker loop after a non-rate-limit error survives the retry
        loop. Unticking the row makes it read (and grey) as "skipped" so it leaves
        the download queue and isn't re-attempted on the next Start — while the kept
        ``error:`` status still feeds the Error column the reason it was set aside.
        Moving it to the end of ``self.items`` means that if the user does re-tick
        it, Start comes to it last, behind everything still worth trying. Hops to the
        main thread (via ``post``) for the Tk / ``self.items`` mutation, like
        :meth:`_set_row`.
        """

        def do():
            it = self.items.get(iid)
            if not it:
                return
            it.status = f"error: {label}"  # kept so the Error column shows the reason
            it.set_aside = label  # persisted → reloads as "skipped" with the reason
            it.selected = False  # → "skipped": greyed, and dropped from the queue #
            it.speed = 0.0
            self._move_in_queue([iid], to_top=False)  # to the bottom; refreshes the tree

        self.post(do)

    def _bump_done(self, total: int) -> None:
        self._done_count += 1
        d = self._done_count
        self.post(lambda: self.progress.config(value=d))
        self.set_status(f"{d}/{total} downloaded")

    # ------------------------------------------------------------------

    def _on_close(self) -> None:
        self._stop_flag = True
        self._closing = True  # let the connectivity monitor loop exit
        self._reconnect_gen += 1  # cancel any pending auto-restart wait
        try:
            self._pull_settings()  # keep an edited-but-unsaved folder/setting
            self._snapshot_view()  # capture any resized column widths
            self.persist()
            self._persist_scans()  # keep ticks/discards for next launch
        except (OSError, tk.TclError):
            pass
        self.worker.stop()
        self.destroy()


def main(argv: Sequence[str] | None = None) -> int:
    """GUI entry point, plus the two flags a packaged build needs.

    ``--version`` identifies the build. ``--self-check`` proves that Tk, cryptg
    and Telethon all work without opening the UI, and ``--self-check-json``
    writes the result to a file, which is the only signal available from the
    console-less Windows build.
    """
    parser = argparse.ArgumentParser(prog="tg-downloader")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--self-check", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--self-check-json", metavar="PATH", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.self_check or args.self_check_json:
        from .selfcheck import run_self_check

        return run_self_check(args.self_check_json)
    App().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
