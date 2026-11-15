# Telethon archived — impact and migration options

**Status:** Reference note. No action required yet.
**Recorded:** 2026-07-15

## The issue

On **2026-02-21** the upstream Telethon repository ([LonamiWebs/Telethon](https://github.com/lonamiwebs/telethon))
was **archived by its owner**. Archived means the maintainer stopped accepting
issues and pull requests — it does **not** mean the library is broken.

Telethon **1.44 still works** against Telegram's current MTProto API. This project
keeps running. There is no immediate breakage and no forced deadline; the risk is
purely long-term: if Telegram changes MTProto in a way that needs a library-side fix,
upstream won't ship one.

## Why a migration is not a find-replace for this project

This app leans on Telethon-specific behavior that is baked deep into the architecture,
so swapping libraries touches more than one import:

- **Resumable downloads** — `iter_download` streaming with resume-from-`.part`
  (4 KiB-aligned prefix) in `telegram.download_item`.
- **Rate-limit contract** — the whole coordinator depends on Telethon's
  `flood_sleep_threshold` (= 20s here) deciding which FloodWaits it *swallows*
  vs. *raises*. A different library has different flood semantics.
- **Error classification** — `errors.py` duck-types `RPCError.code` and
  `FloodWaitError.seconds` (deliberately no `isinstance` against Telethon classes).
- **Native crypto** — `cryptg` auto-detection via `telethon.crypto.aes.cryptg`.

**What helps:** the project's Telethon-free invariant means only `telegram.py` imports
the library. That's the one seam a migration would rewrite; the tested logic modules
(`filters`, `config`, `models`, `store`, `worker`, `errors`) stay put — though
`errors.py`'s classification and `worker.py`'s rate-limit tuning would need
re-validating against the new library's behavior.

## Options

### Stay on Telethon (lowest effort)
- **Telethon 1.44 as-is** — keeps working; pin the version.
- **[Codeberg mirror](https://codeberg.org/Lonami/Telethon)** — may see occasional life.
- **[Telethon-MCUB](https://github.com/hairpin01/Telethon-MCUB)** — community fork,
  same `import telethon` path, so effectively a zero-code-change maintained line.

### Migrate to the Pyrogram ecosystem (larger effort, more future-proof)
Pyrogram itself also went dormant, but its forks are the healthiest actively-maintained
Python MTProto libraries right now:

| Library | What it is | Notes |
|---|---|---|
| **[Kurigram](https://github.com/KurimuzonAkuma/kurigram)** | Actively-maintained Pyrogram fork | Strongest target. Drop-in Pyrogram replacement; tracks latest Telegram features (Stories, Topics, Business, Gifts). `pip install kurigram` |
| **[pyrofork](https://github.com/Mayuri-Chan/pyrofork)** | Active Pyrogram fork | MongoDB session storage, story/topic support, own docs |

A Pyrogram-family library has a **different API surface and different flood/download
semantics**, so migrating means rewriting `telegram.py` and re-validating `errors.py`,
`worker.py`, and the rate-limit coordinator.

## Recommendation

1. **Short term** — stay on Telethon 1.44 (or the MCUB fork / Codeberg mirror for a
   maintained line with no code changes). It will work for a good while.
2. **When migrating** — target **Kurigram**: most active, largest community, closest to
   a maintained mainline. Prototype a Kurigram-based `telegram.py` behind the existing
   interface and A/B it before committing, so the rest of the app is undisturbed.

## Sources

- [Telethon (archived)](https://github.com/lonamiwebs/telethon) · [Codeberg mirror](https://codeberg.org/Lonami/Telethon)
- [Kurigram](https://github.com/KurimuzonAkuma/kurigram) · [pyrofork](https://github.com/Mayuri-Chan/pyrofork) · [Telethon-MCUB](https://github.com/hairpin01/Telethon-MCUB)
