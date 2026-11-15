#!/usr/bin/env python3
"""Audit the files that would be published for secrets and personal data.

The repository is public, so anything that reaches a commit is permanent. This
script is the second of three layers (the others are `.gitignore` and GitHub
push protection): it refuses a tree that contains a live credential, a login
session, a private path or a phone number.

Two modes, both read-only and both stdlib-only so this can run without `uv`:

    python3 scripts/public_audit.py            # every tracked file (CI)
    python3 scripts/public_audit.py --tracked  # same, explicit
    python3 scripts/public_audit.py --staged   # what is staged right now (hook)

Exit status: 0 clean, 1 findings, 2 the audit could not run.

There is deliberately no `--fix`: deciding what a secret should become is a
human's call, and silently rewriting it is how a leak becomes a wrong fix.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: Files that must never be published, whatever they contain. Matched against
#: the repository-relative path *and* the basename, so `config.json` catches
#: `config.json` at any depth.
DENIED_PATHS: tuple[str, ...] = (
    "*.session",
    "*.session-journal",
    "config.json",
    ".env",
    ".env.*",
    "AGENTS.md",
    "CLAUDE.md",
    "docs/implementation-plan/**",
    ".claude/**",
    "*.part",
    "*.part.idx",
    "*.log",
    "downloads/**",
    "dist/**",
    "build/**",
    "*.csv",
)

#: The one documented env-file example, which is meant to be committed.
PATH_EXCEPTIONS: tuple[str, ...] = (".env.example",)

#: Reading a whole 2 MB file to grep it is pointless; nothing secret is that big
#: and the binaries (screenshots, icons) are filtered as binary anyway.
MAX_BYTES = 2 * 1024 * 1024

#: Shapes that look numeric but are not phone numbers: an IPv4 address or
#: host:port, a Telegram chat id (a negative integer), a date, a version, and a
#: list of decimal coordinates (SVG/plot output).
_NOT_PHONE = (
    re.compile(r"-?\d{1,3}(?:\.\d{1,3}){3}(?:/\d+)?"),  # 149.154.160.0/20
    re.compile(r"-\d{5,}"),  # -1001234567890, a chat id
    re.compile(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}"),  # 2026-11-15
    re.compile(r"\d+\.\d+\.\d+"),  # 1.44.0
    re.compile(r"\d+\.\d+(?:\s+\d+\.\d+)+"),  # 210.29 842.00
    re.compile(r"\d+(?:\.\d+){2,}"),  # 1.2.3.4.5
)

#: A run of digits with phone punctuation. Anchoring to those characters is what
#: stops this from matching a fragment of a hash or a word.
_PHONE_TOKEN = re.compile(r"[+(]?\d[\d\s().+\-]{6,}\d")

#: A hex UUID is not a phone number. Letters split it into numeric pieces that
#: look phone-shaped on their own, so it is scrubbed before the phone rule runs.
_UUID = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)

#: Numbers reserved for documentation, so an example can never ring a real
#: person: 555-01xx in North America, 020 7946 0xxx and 07700 900xxx in the UK
#: (the Ofcom drama ranges). Matched against the digits alone.
_FICTIONAL = (
    re.compile(r"55501\d{2}"),
    re.compile(r"2079460\d{3}"),
    re.compile(r"7700900\d{3}"),
)


def _digits_only(token: str) -> str:
    return "".join(char for char in token if char.isdigit())


def _is_fictional_number(token: str) -> bool:
    """True for a number from a reserved example range, which is safe to publish."""
    digits = _digits_only(token)
    if digits.startswith("44"):
        digits = digits[2:]
    digits = digits.lstrip("0")
    candidates = {digits}
    if digits.startswith("1"):
        candidates.add(digits[1:])
    return any(
        pattern.fullmatch(candidate) for pattern in _FICTIONAL for candidate in candidates
    )


def _phone_like(token: str) -> bool:
    """True when ``token`` has the shape of a phone number and nothing else.

    A phone number has 9 to 15 digits and is written with a leading ``+``, an
    area code in brackets, separators, or digit groups. Four things keep the
    rule usable on a real tree: a hash has letters in it, a chat id is one long
    digit run, a coordinate list has decimals or one-digit groups, and a table
    row is longer than a number or padded with double spaces. A
    space-separated national number is caught only when the groups are real
    (three or more groups of two digits or more), so a padded coordinate list
    such as ``0 0 1024 1024`` is not mistaken for one.
    """
    token = token.strip(" .,;:'\"()[]")
    if len(token) > 20 or "  " in token:
        # A phone number is short and uses single spaces. This rejects a row of
        # space-padded numeric columns, such as a table of release years.
        return False
    if any(pattern.fullmatch(token) for pattern in _NOT_PHONE):
        return False
    groups = [group for group in re.split(r"[\s\-()]+", token) if group]
    if any(len(group) > 5 for group in groups):
        return False
    digits = _digits_only(token)
    if not 9 <= len(digits) <= 15:
        return False
    if any(char in token for char in "+-()"):
        return True
    return len(groups) >= 3 and all(len(group) >= 2 for group in groups)


#: Content checks. Each rule is (id, what it means, pattern, allowed paths).
#: Allowed paths are fnmatch globs; a match there suppresses that rule only.
RULES: tuple[tuple[str, str, re.Pattern[str], tuple[str, ...]], ...] = (
    (
        "api-hash",
        "a Telegram API hash (use the documented placeholder instead)",
        re.compile(r"TG_API_HASH\s*=\s*(?!0123456789abcdef0123456789abcdef)[0-9a-fA-F]{32}"),
        (),
    ),
    (
        "api-hash-quoted",
        "a Telegram API hash",
        re.compile(r"api_hash[\"']?\s*[:=]\s*[\"'][0-9a-fA-F]{32}[\"']"),
        (),
    ),
    (
        "phone-number",
        "a phone number (use a reserved example: 555-01xx, 020 7946 0xxx, 07700 900xxx)",
        _PHONE_TOKEN,
        (),
    ),
    (
        "email-address",
        "an email address (use example.com or a noreply address)",
        re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
        (),
    ),
    (
        "local-path",
        "an absolute path from a developer machine",
        re.compile(r"(?:/Users/[^/\s]+|/home/[^/\s]+)"),
        ("tests/test_portable.py", "scripts/public_audit.py"),
    ),
    (
        "private-key",
        "a private key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        (),
    ),
)

#: Addresses that are not personal data. Checked against the whole match.
ALLOWED_EMAILS: tuple[re.Pattern[str], ...] = (
    re.compile(r"noreply@github\.com$", re.I),
    re.compile(r"[A-Za-z0-9._%+-]+@users\.noreply\.github\.com$", re.I),
    re.compile(r"[A-Za-z0-9._%+-]+@example\.(?:com|org|net)$", re.I),
    re.compile(r"[A-Za-z0-9._%+-]+@example$", re.I),
)


@dataclass(frozen=True)
class Finding:
    """One problem, located but without the secret itself."""

    path: str
    line: int
    rule: str
    message: str
    preview: str

    def render(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{where}: {self.rule}: {self.message} [{self.preview}]"


def _git(*args: str) -> str:
    """Run git and return stdout, or exit 2 with a clear message."""
    try:
        proc = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        sys.exit("public-audit: git is not installed")
    if proc.returncode != 0:
        sys.exit(f"public-audit: `git {' '.join(args)}` failed: {proc.stderr.strip()}")
    return proc.stdout


def changed_paths(staged: bool) -> list[str]:
    """The files to audit: the index for a hook run, the tree for CI."""
    if staged:
        out = _git("diff", "--cached", "--name-only", "--diff-filter=ACM")
    else:
        out = _git("ls-files")
    return [line for line in out.splitlines() if line]


def read_staged(path: str) -> bytes:
    """The staged blob, so a hook checks what is about to be committed."""
    proc = subprocess.run(["git", "show", f":{path}"], capture_output=True, check=False)
    if proc.returncode == 0:
        return proc.stdout
    return Path(path).read_bytes()


def denied_path(path: str) -> str | None:
    """The glob that forbids ``path``, or None when it is allowed."""
    name = path.rsplit("/", 1)[-1]
    for exception in PATH_EXCEPTIONS:
        if fnmatch.fnmatchcase(path, exception):
            return None
    for pattern in DENIED_PATHS:
        if fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(name, pattern):
            return pattern
    return None


def _allowed(rule_paths: tuple[str, ...], path: str) -> bool:
    return any(fnmatch.fnmatchcase(path, glob) for glob in rule_paths)


def _looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def _preview(text: str) -> str:
    """Show enough to locate the problem, never enough to leak it."""
    text = text.strip()
    if len(text) <= 6:
        return "*" * len(text)
    return f"{text[:4]}…{len(text)} chars"


def scan_content(path: str, data: bytes) -> list[Finding]:
    """Content findings for one file."""
    findings: list[Finding] = []
    if len(data) > MAX_BYTES or _looks_binary(data):
        return findings
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return findings

    for lineno, line in enumerate(text.splitlines(), start=1):
        for rule_id, message, pattern, allowed_paths in RULES:
            if _allowed(allowed_paths, path):
                continue
            if rule_id == "email-address":
                hits = [
                    m
                    for m in pattern.finditer(line)
                    if not any(a.match(m.group(0)) for a in ALLOWED_EMAILS)
                ]
            elif rule_id == "phone-number":
                hits = [
                    m
                    for m in pattern.finditer(_UUID.sub(" ", line))
                    if _phone_like(m.group(0)) and not _is_fictional_number(m.group(0))
                ]
            else:
                hits = list(pattern.finditer(line))
            for hit in hits:
                findings.append(Finding(path, lineno, rule_id, message, _preview(hit.group(0))))
    return findings


def audit(paths: list[str], staged: bool) -> list[Finding]:
    """Every finding across ``paths``, path findings first."""
    findings: list[Finding] = []
    for path in paths:
        pattern = denied_path(path)
        if pattern is not None:
            findings.append(
                Finding(
                    path,
                    0,
                    "denied-path",
                    f"this file must never be published (matches {pattern})",
                    "path",
                )
            )
            continue
        try:
            data = read_staged(path) if staged else Path(path).read_bytes()
        except FileNotFoundError:
            continue
        findings.extend(scan_content(path, data))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="public_audit",
        description="Refuse secrets and personal data in files that would be published.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--staged",
        action="store_true",
        help="audit what is staged right now (used by the pre-commit hook)",
    )
    mode.add_argument(
        "--tracked",
        action="store_true",
        help="audit every tracked file (default; used in CI)",
    )
    args = parser.parse_args(argv)
    staged = args.staged

    paths = changed_paths(staged)
    findings = audit(paths, staged)

    if not findings:
        scope = "staged files" if staged else "tracked files"
        print(f"public-audit: clean ({len(paths)} {scope} checked)")
        return 0

    print("public-audit: refused — fix these before committing:", file=sys.stderr)
    for finding in findings:
        print(f"  {finding.render()}", file=sys.stderr)
    print(
        "\nNo secret is printed in full. Remove the value, or replace it with the "
        "documented placeholder. If this is a false positive, widen the rule in "
        "scripts/public_audit.py with a comment explaining why it is safe.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
