# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-11-15

First public release.

### Added

- Tkinter GUI with Files, Channels and Settings tabs.
- Scanning of all joined Telegram groups and channels for files matching an
  extension allow-list, with a full rescan and a watermark-based update scan.
- Batch downloads with several files at once, resumable `.part` files and
  pause/stop that takes effect mid-file.
- Pipelined single-file downloads: 512 KiB parts with several requests in
  flight, and a `.part.idx` bitmap so a resumed run only fetches what is
  missing.
- Per-column filters and sorting, including a status filter.
- Favourites for channels, independent of the scan list.
- Persistent scan cache: files, ticks and discards survive a restart, and files
  already on disk come back marked as done.
- Portable export/import of settings, channels and the download list, without
  the login session.
- Headless CLI: scan, dry-run, download, de-duplicate, export and import.
- Batch-wide rate-limit coordination, automatic retries, error classification
  and a rotating run log.
- Prebuilt installers for Windows x64, macOS Apple Silicon and Linux x86_64,
  plus a source install for everything else.

[Unreleased]: https://github.com/datarip/tg-downloader/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/datarip/tg-downloader/releases/tag/v0.1.0
