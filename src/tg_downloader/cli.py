"""Headless downloader — same engine, no GUI.

Useful for cron jobs, servers, or when you just want to grab everything without
reviewing it first.

    tg-download --list-chats
    tg-download --chat -1001234567890
    tg-download --chat @somegroup --ext .csv,.json --limit 500 --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

from .config import (
    ENV_FILE_VAR,
    ENV_STORED,
    SESSION_PATH,
    apply_to_environ,
    load_config,
    save_config,
    to_env_values,
    write_env_file,
)
from .errors import ConnectionLost, RateLimited, classify, configure_logging, log
from .filters import human_size, parse_extensions
from .models import download_dir_problem, duplicate_key
from .portable import (
    export_bundle,
    import_bundle,
    merge_channels,
    merge_imported_scans,
    read_bundle,
    write_bundle,
)
from .store import load_scans, save_scans
from .telegram import (
    FLOOD_SLEEP_THRESHOLD,
    build_client,
    download_item,
    is_content_protected,
    iter_matching_files,
    part_index_path,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tg-download",
        description="Download files from Telegram groups/channels you belong to.",
    )
    p.add_argument(
        "--chat",
        action="append",
        default=[],
        help="Chat @username, numeric id, or link. Repeatable. "
        "Defaults to the channels saved in your config.",
    )
    p.add_argument(
        "--ext", default=None, help="Comma-separated extensions. Defaults to the config value."
    )
    p.add_argument("--out", default=None, help="Download directory.")
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max messages to scan per chat (default: whole history).",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="List what would be downloaded, then exit."
    )
    p.add_argument(
        "--connections",
        type=int,
        default=None,
        help="Connections per file (1–16): keep N GetFile requests in flight per "
        "file for faster single-file downloads. Default from config (4).",
    )
    p.add_argument(
        "--dedupe",
        action="store_true",
        help="Skip reposts: files with the same name and size as one "
        "already seen this run (across all chats).",
    )
    p.add_argument(
        "--list-chats",
        action="store_true",
        help="Print your groups/channels with their ids and exit.",
    )
    p.add_argument(
        "--env-file",
        default=None,
        help="Env file with TG_API_ID / TG_API_HASH / TG_CHANNELS. "
        "Defaults to ./.env, else ~/.tg_downloader/.env.",
    )
    p.add_argument(
        "--export",
        metavar="FILE",
        default=None,
        help="Export settings, channels and download list to a portable JSON file, "
        "then exit. Your login session is never included.",
    )
    p.add_argument(
        "--import",
        dest="import_path",
        metavar="FILE",
        default=None,
        help="Import a bundle written by --export (merged with local state), then exit.",
    )
    p.add_argument(
        "--include-secrets",
        action="store_true",
        help="With --export, also include the API id/hash in the bundle.",
    )
    return p


_RATE_DEFAULT = 5.0


class _StorageAbort(Exception):
    """The output folder became unusable mid-run — stop, don't fail every file."""


async def _download_with_retry(client, item, out_dir, connections: int = 1) -> bool:
    """Download one item, waiting out Telegram rate limits. Returns True on success.

    Mirrors the GUI's coordination policy for the headless path: a flood wait or
    a lost connection is a *wait*, not a failure — the ``.part`` grows on every
    attempt — so a rate-limited, speed-capped, or offline item is **never** given
    up on. It honours Telegram's own ``retry_after`` when named, else sleeps a
    short fixed cooldown, and retries indefinitely. Other errors are reported with
    their classified cause and the ``.part`` is kept so a re-run resumes — unless
    the message is truly gone.
    """
    while True:
        try:
            await download_item(client, item, out_dir, connections=connections)
            print(f"   ok    {item.name}  ({human_size(item.size)})")
            return True
        except RateLimited as e:
            wait = e.retry_after or _RATE_DEFAULT
            print(f"   wait  {item.name}: {e.label} — pausing {wait:.0f}s then retrying")
            log.warning("CLI %s on %s — waiting %.0fs", e.label, item.name, wait)
            await asyncio.sleep(wait)
        except ConnectionLost as e:
            # A lost link is a wait, not a failure — the .part is kept, so retry
            # the same file indefinitely once the connection is back (mirrors the
            # GUI's offline gate for the headless path).
            print(f"   wait  {item.name}: {e.label} — link down, retry in {_RATE_DEFAULT:.0f}s")
            log.warning("CLI connection lost on %s — waiting %.0fs", item.name, _RATE_DEFAULT)
            await asyncio.sleep(_RATE_DEFAULT)
        except Exception as e:
            cause = classify(e)
            log.error("CLI download failed for %s: %s", item.name, e)
            if cause.kind == "no_storage":
                # The output folder is gone or unwritable — every remaining file
                # would fail identically, so abort the run instead of printing one
                # failure per queued file.
                raise _StorageAbort(cause.detail) from e
            if "no longer available" in str(e):  # unresumable — drop the stub
                part = item.part_path(out_dir)
                part.unlink(missing_ok=True)
                part_index_path(part).unlink(missing_ok=True)
            print(f"   FAIL  {item.name}: {cause.detail}", file=sys.stderr)
            return False


def _do_export(cfg: dict, path: str, include_secrets: bool) -> int:
    """Write a portable bundle and exit. No client, no network."""
    scans = load_scans()
    bundle = export_bundle(
        cfg,
        scans,
        include_secrets=include_secrets,
        exported_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    try:
        written = write_bundle(bundle, path)
    except OSError as e:
        print(f"! could not write {path}: {e}", file=sys.stderr)
        return 1
    n_files = sum(len(s.files) for s in scans.values())
    note = " (with API credentials)" if include_secrets else ""
    print(
        f"Exported {len(bundle['channels'])} channel(s) and {n_files} file(s){note} "
        f"to {written}"
    )
    print("The login session was not included — sign in on the other machine.")
    return 0


def _do_import(cfg: dict, path: str) -> int:
    """Read a bundle, merge it into local config/channels/scans, and exit."""
    try:
        imported = import_bundle(read_bundle(path))
    except (OSError, ValueError) as e:  # ValueError covers json.JSONDecodeError
        print(f"! could not import {path}: {e}", file=sys.stderr)
        return 1

    cfg.update(imported.config)
    if imported.credentials:
        cfg.update(imported.credentials)
    cfg["channels"] = merge_channels(cfg.get("channels", []), imported.channels)

    values = to_env_values(cfg, ENV_STORED)
    write_env_file(values)
    apply_to_environ(values)
    save_config(cfg)

    save_scans(merge_imported_scans(load_scans(), imported.scans))

    creds = " and API credentials" if imported.credentials else ""
    print(
        f"Imported {len(imported.channels)} channel(s), {imported.file_count()} file(s){creds}."
    )
    print("Your download folder and login were left unchanged.")
    return 0


async def run(args: argparse.Namespace) -> int:
    configure_logging()
    cfg = load_config()
    if args.export:
        return _do_export(cfg, args.export, args.include_secrets)
    if args.import_path:
        return _do_import(cfg, args.import_path)
    try:
        flood_threshold = max(
            0.0, float(cfg.get("flood_sleep_threshold", FLOOD_SLEEP_THRESHOLD))
        )
    except (ValueError, TypeError):
        flood_threshold = float(FLOOD_SLEEP_THRESHOLD)
    client = await build_client(
        cfg["api_id"],
        cfg["api_hash"],
        session=SESSION_PATH,
        flood_sleep_threshold=flood_threshold,
    )

    if args.list_chats:
        print(f"{'ID':<16} {'TYPE':<9} TITLE")
        print("-" * 58)
        async for d in client.iter_dialogs():
            if d.is_group or d.is_channel:
                kind = "group" if d.is_group else "channel"
                print(f"{d.id:<16} {kind:<9} {d.name}")
        await client.disconnect()
        return 0

    chats = args.chat or [c["id"] for c in cfg["channels"]]
    if not chats:
        print("No chats given. Use --chat, or add some via the GUI.", file=sys.stderr)
        await client.disconnect()
        return 2

    allowed = parse_extensions(args.ext or cfg["extensions"])
    out_dir = args.out or cfg["download_dir"]
    limit = args.limit or (int(cfg["scan_limit"]) or None)
    try:
        connections = args.connections or int(cfg.get("download_connections", "4"))
    except (ValueError, TypeError):
        connections = 4
    connections = max(1, min(16, connections))

    # Same pre-flight as the GUI's Start: a download folder that's gone (an
    # unmounted drive) fails every file identically, so bail out once instead of
    # printing one error per file for the whole run.
    if not args.dry_run and not args.list_chats:
        problem = download_dir_problem(out_dir)
        if problem:
            print(f"! {problem}", file=sys.stderr)
            log.error("Refusing to start: %s", problem)
            await client.disconnect()
            return 2

    print(f"Extensions: {', '.join(sorted(allowed))}")
    print(f"Output:     {out_dir}\n")

    total_done = total_found = total_dupes = 0
    seen: set[tuple[str, int]] = set()
    aborted = ""
    try:
        for chat in chats:
            try:
                entity = await client.get_entity(chat)
            except Exception as e:
                print(f"! cannot open {chat}: {e}", file=sys.stderr)
                continue

            title = getattr(entity, "title", str(chat))
            if is_content_protected(entity):
                print(
                    f"! warning: '{title}' has content-saving restricted; "
                    "downloads may be blocked."
                )
            print(f"== {title}")

            async for item in iter_matching_files(client, entity, allowed, limit):
                total_found += 1
                if args.dedupe:
                    key = duplicate_key(item)
                    if key in seen:
                        total_dupes += 1
                        print(f"   dup   {item.name}  (same name and size already seen)")
                        continue
                    seen.add(key)
                target = item.target_path(out_dir)
                if target.exists():
                    print(f"   skip  {item.name}  (already have)")
                    continue
                dup = item.existing_duplicate(out_dir)
                if dup:
                    print(f"   skip  {item.name}  (same name+size on disk: {dup.name})")
                    continue
                if args.dry_run:
                    print(f"   would download  {item.name}  ({human_size(item.size)})")
                    continue
                if await _download_with_retry(client, item, out_dir, connections):
                    total_done += 1
    except _StorageAbort as e:
        aborted = str(e)
        print(f"! {aborted} — stopping.", file=sys.stderr)

    save_config(cfg)
    await client.disconnect()
    verb = "would download" if args.dry_run else "downloaded"
    tail = f", {total_dupes} duplicate(s) skipped" if args.dedupe else ""
    print(f"\n{total_found} matching files found, {total_done} {verb}{tail}.")
    return 1 if aborted else 0


def main() -> None:
    args = build_parser().parse_args()
    if args.env_file:
        os.environ[ENV_FILE_VAR] = args.env_file  # config.env_file_path() reads this
    try:
        sys.exit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
