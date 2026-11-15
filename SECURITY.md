# Security policy

## Reporting a vulnerability

Report security issues through GitHub's **private vulnerability reporting**:
open the repository's **Security** tab and choose **Report a vulnerability**.
That creates a private advisory only the maintainers can see.

Do not open a public issue for a security problem, and never paste a live
credential into one. That includes:

- a `tg_session.session` file or its contents;
- an `api_hash` or `api_id`;
- a Telegram login code or two-step verification password;
- a copy of `~/.tg_downloader/` with the session or config still in it.

If a session or `api_hash` has already leaked, treat it as compromised before
reporting: end the session from **Telegram → Settings → Devices**, and request
a new `api_hash` at <https://my.telegram.org>.

## Supported versions

Fixes land on `main` and on the latest release. Older releases are not
patched, so please confirm the problem on the latest version first.

## What this project will not do

TDM does not bypass Telegram's access controls. Content marked "restrict
saving content", hidden channel history, and account bans are server-side
decisions, not vulnerabilities to route around.

## Scope

In scope: the application code in this repository, the published installers,
and the packaging scripts. Out of scope: Telegram's own servers and clients,
Telethon and cryptg upstream (report those to their maintainers), and any
problem that needs an already-compromised machine to reproduce.
