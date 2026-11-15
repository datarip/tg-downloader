# Pull request

## What this changes

<!-- One paragraph. What is different after this PR, and why. -->

## Why

<!-- The problem it solves, or the issue it closes. Use "Closes #123" to link it. -->

## How it was checked

<!--
Commands you ran, plus anything you checked by hand. Say plainly when a change
is only checked by hand: the Tkinter widget layer needs a display and live
Telegram calls need a session, so neither is covered by the test suite.
-->

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
python3 scripts/public_audit.py --tracked
```

## Checklist

- [ ] Tests pass, and new logic has a test where it reasonably can.
- [ ] No secret, session file, `config.json` or real `.env` is included.
- [ ] No personal data in the diff: account name, phone number, user id, private
      chat titles, invite links, or third-party filenames in code, logs or
      screenshots.
- [ ] No absolute path from my machine, email address or phone number in source,
      tests or fixtures.
- [ ] `CHANGELOG.md` has an entry under `[Unreleased]` for a user-visible change.
- [ ] The README or `docs/` is updated when behaviour a user relies on changed.
- [ ] Commits have plain messages and no attribution trailer naming a tool.
