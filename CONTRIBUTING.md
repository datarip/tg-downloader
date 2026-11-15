# Contributing to TDM

Thanks for taking the time to help. This page covers the development setup, the
checks a change has to pass, and the rules that keep the repository safe to
publish.

## Setup

TDM uses [uv](https://docs.astral.sh/uv/) for everything: it installs a
suitable Python, the virtual environment and the dependencies.

```bash
git clone https://github.com/datarip/tg-downloader.git
cd tg-downloader
uv sync
```

`uv sync` installs the runtime dependencies and the `dev` group. Run the app
with `uv run tg-downloader`, and the CLI with `uv run tg-download --help`.

Add a runtime dependency with `uv add <package>` and a development one with
`uv add --dev <package>`. Edit `pyproject.toml` through those commands, not by
hand, so `uv.lock` stays in step.

Dependency resolution is capped at the date in `[tool.uv] exclude-newer`, so a
lock generated here matches one generated in CI. Bump that date deliberately
when you add or upgrade a package, or the resolver will refuse a newer version.

## Checks before you open a pull request

```bash
uv run pytest                 # unit tests and doctests
uv run ruff check .           # lint
uv run ruff format --check .  # formatting
python3 scripts/public_audit.py --tracked   # no secrets or personal data
```

All four must pass. Run `uv run ruff format .` to fix formatting. The public
audit runs automatically on commit through the `.githooks/pre-commit` hook,
which is enabled by `git config core.hooksPath .githooks`.

The Tkinter widget layer and live Telegram calls are not covered by the test
suite: the first needs a display, the second needs a real session. Say plainly
in the pull request when a change is only checked by hand.

## Branches and pull requests

- `main` is the development branch. Every feature and fix lands there.
- `release` is the stable branch. It only receives merges from `main`, and it
  is where release tags are made.
- Branch off `main` with a short-lived name: `feat/…`, `fix/…` or `chore/…`.
  Delete the branch after it merges.
- Keep a pull request focused on one change. Describe what it does, why, and
  how you checked it.

## No secrets, no personal data

The repository is public, so anything committed is permanent. Do not commit:

- `*.session` files, `config.json`, `.env` in any form other than
  `.env.example`, or any credential or login code;
- screenshots or logs containing your account name, phone number, user id,
  private chat titles, invite links, or third-party filenames;
- absolute paths from your own machine, email addresses, or phone numbers in
  source, tests or fixtures.

Use placeholders in examples: `1234567` for an `api_id` and
`0123456789abcdef0123456789abcdef` for an `api_hash`. Add a screenshot only
when the file list in it is safe to publish.

The pre-commit hook blocks the most common cases, but it is a net, not a
guarantee. Check the diff yourself.

## Commit conventions

- One logical change per commit, with a plain message describing the change:
  `fix: resume a part file after a hard kill`. Prefixes in use are `feat:`,
  `fix:`, `docs:`, `chore:`, `build:`, `ci:` and `release:`.
- Add `Github-Issue:#<number>` as a trailer when the commit closes an issue,
  and `Reported-by:<name>` when it fixes a problem someone reported.
- Do not add attribution trailers naming a tool or a co-author.

## Documentation

`README.md` and `docs/` describe behaviour that has shipped. Write the
user-facing part there when a feature lands, and keep design notes and plans
out of the repository.
