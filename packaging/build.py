#!/usr/bin/env python3
"""Build the TDM GUI with PyInstaller, then prove the result runs.

    uv run python packaging/build.py [--version X] [--package] [--skip-smoke]

The frozen app is checked with ``--self-check``, which opens and closes a real
Tk root and asserts the native cryptg backend made it into the bundle. A build
that loses either one fails here rather than in a user's hands.

On Linux the build also reports the glibc floor of the bundle and fails when it
is above the version the README promises. On Windows it generates the version
resource PyInstaller embeds in the executable.

``--package`` then calls the per-OS wrapper: Inno Setup, the DMG script, or the
AppImage script. Without the flag the build stops after the smoke test.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
WORK = ROOT / "build" / "pyinstaller"
SPEC = ROOT / "packaging" / "tdm.spec"

#: Shipped beside the executable so a Windows or Linux install carries the
#: licence and the third-party notices. On macOS the DMG places them next to
#: the app bundle instead.
DOCS = ("LICENSE", "THIRD_PARTY_NOTICES.md")

#: Wrapper per platform, used by --package.
PACKAGERS = {
    "win32": ROOT / "packaging" / "windows" / "package.ps1",
    "darwin": ROOT / "packaging" / "macos" / "make_dmg.sh",
}

#: Windows version resource. Generated from the build version, never committed:
#: a stale copy would ship the wrong number.
VERSION_INFO = ROOT / "packaging" / "windows" / "version_info.txt"

#: The glibc floor the README promises for the Linux build. Measured, not
#: inherited from the runner: every bundled file is prebuilt, and the highest
#: requirement among them is cryptg's manylinux_2_28 wheel.
GLIBC_FLOOR = (2, 28)


def version_quad(version: str) -> str:
    """``"0.1.0"`` to the four comma-separated numbers a Windows resource wants."""
    core = version.split("-", 1)[0]
    parts = [int(part) for part in re.findall(r"\d+", core)][:4]
    parts += [0] * (4 - len(parts))
    return ", ".join(str(part) for part in parts)


def write_version_info(version: str) -> None:
    """Write the PyInstaller version resource for the Windows executable."""
    quad = version_quad(version)
    VERSION_INFO.parent.mkdir(parents=True, exist_ok=True)
    VERSION_INFO.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({quad}), prodvers=({quad}),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'Datarip'),
        StringStruct('FileDescription', 'TDM - Telegram file download manager'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('InternalName', 'TDM'),
        StringStruct('LegalCopyright', 'Copyright (c) 2026 Datarip'),
        StringStruct('OriginalFilename', 'TDM.exe'),
        StringStruct('ProductName', 'TDM'),
        StringStruct('ProductVersion', '{version}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""",
        encoding="utf-8",
    )


def highest_glibc(objdump_output: str) -> tuple[int, int] | None:
    """Highest ``GLIBC_x.y`` version named in ``objdump -T`` output."""
    found = [
        (int(major), int(minor))
        for major, minor in re.findall(r"GLIBC_(\d+)\.(\d+)", objdump_output)
    ]
    return max(found) if found else None


def needed_libraries(objdump_output: str) -> list[str]:
    """``DT_NEEDED`` library names from ``objdump -p`` output."""
    return re.findall(r"\bNEEDED\s+(\S+)", objdump_output)


def _objdump(objdump: str, *args: str, path: Path) -> str:
    """Run objdump, returning stdout. Unreadable files yield an empty string."""
    proc = subprocess.run(
        [objdump, *args, str(path)],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    return proc.stdout


def check_glibc_floor() -> None:
    """Report the bundle's glibc floor, and fail when it is above the promise.

    The floor is the oldest version stamped into the bundled binaries. It is
    not the build host's glibc, because PyInstaller copies prebuilt files
    instead of compiling them.
    """
    if not sys.platform.startswith("linux"):
        return
    objdump = shutil.which("objdump")
    if objdump is None:
        print("glibc floor: objdump is missing, so the floor is unverified")
        return
    worst = (0, 0)
    source = ""
    for path in sorted((DIST / "TDM").rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        required = highest_glibc(_objdump(objdump, "-T", path=path))
        if required and required > worst:
            worst, source = required, path.name
    print(f"glibc floor: {worst[0]}.{worst[1]:02d} (from {source})")
    if worst > GLIBC_FLOOR:
        for path in sorted((DIST / "TDM").rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if source in needed_libraries(_objdump(objdump, "-p", path=path)):
                print(f"  {source} is needed by {path.relative_to(DIST)}")
        raise SystemExit(
            f"glibc floor: the bundle needs {worst[0]}.{worst[1]:02d}, above the "
            f"documented {GLIBC_FLOOR[0]}.{GLIBC_FLOOR[1]:02d}. Update the README, "
            "or pin the dependency that raised it."
        )


def resolve_version(explicit: str | None) -> str:
    """``--version``, then ``$TDM_VERSION``, then the package's own version."""
    for candidate in (explicit, os.environ.get("TDM_VERSION")):
        if candidate and candidate.strip():
            return candidate.strip().lstrip("v")
    sys.path.insert(0, str(ROOT / "src"))
    from tg_downloader import __version__

    return __version__


def frozen_binary() -> Path:
    """Where PyInstaller puts the executable for this platform."""
    if sys.platform == "darwin":
        return DIST / "TDM.app" / "Contents" / "MacOS" / "TDM"
    if sys.platform == "win32":
        return DIST / "TDM" / "TDM.exe"
    return DIST / "TDM" / "TDM"


def run(command: list[str], *, env: dict[str, str], timeout: int | None = None) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, env=env, check=True, timeout=timeout)


def build(version: str) -> None:
    shutil.rmtree(DIST, ignore_errors=True)
    shutil.rmtree(WORK, ignore_errors=True)
    if sys.platform == "win32":
        write_version_info(version)
    env = {**os.environ, "TDM_VERSION": version}
    try:
        run(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                str(SPEC),
                "--noconfirm",
                "--clean",
                "--distpath",
                str(DIST),
                "--workpath",
                str(WORK),
            ],
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"build: PyInstaller failed (exit {exc.returncode})") from exc


def self_check_command(binary: Path, json_path: Path) -> list[str]:
    """The smoke test, wrapped in xvfb on a headless Linux box."""
    command = [
        str(binary),
        "--self-check",
        "--self-check-json",
        str(json_path),
    ]
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        xvfb = shutil.which("xvfb-run")
        if xvfb:
            command = [xvfb, "-a", *command]
    return command


def smoke_test(version: str) -> dict[str, object]:
    """Run the frozen app's self-check and refuse a build that cannot start."""
    binary = frozen_binary()
    if not binary.is_file():
        raise SystemExit(f"smoke: expected the build at {binary}, but it is missing")

    json_path = DIST / "selfcheck.json"
    json_path.unlink(missing_ok=True)
    command = self_check_command(binary, json_path)
    try:
        proc = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(
            "smoke: the frozen app did not finish its self-check in 5 min"
        ) from exc

    if proc.stdout.strip():
        print(proc.stdout.rstrip())
    if proc.stderr.strip():
        print(proc.stderr.rstrip(), file=sys.stderr)
    if proc.returncode != 0:
        raise SystemExit(f"smoke: self-check exited {proc.returncode}")

    if not json_path.is_file():
        raise SystemExit(f"smoke: self-check wrote no JSON to {json_path}")
    report = json.loads(json_path.read_text(encoding="utf-8"))
    if report.get("cryptg") is not True:
        raise SystemExit(
            "smoke: the bundle lost the native cryptg AES backend "
            "(downloads would be CPU-bound); check hiddenimports in packaging/tdm.spec"
        )
    if report.get("version") != version:
        raise SystemExit(
            f"smoke: the bundle reports version {report.get('version')!r}, expected {version!r}"
        )
    return report


def copy_docs() -> None:
    """Put the licence and notices beside the executable (Windows/Linux tree)."""
    target = DIST / "TDM"
    if not target.is_dir():
        return
    for name in DOCS:
        shutil.copy2(ROOT / name, target / name)


def package(version: str) -> None:
    wrapper = PACKAGERS.get(sys.platform, ROOT / "packaging" / "linux" / "make_appimage.sh")
    if not wrapper.is_file():
        raise SystemExit(
            f"package: {wrapper.relative_to(ROOT)} is missing; cannot build the installer"
        )
    env = {**os.environ, "TDM_VERSION": version}
    if sys.platform == "win32":
        run(
            ["pwsh", "-NoProfile", "-File", str(wrapper), "-Version", version],
            env=env,
        )
    else:
        run(["bash", str(wrapper), version], env=env)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def report_artifacts(binary: Path) -> None:
    print("\nartifacts:")
    release = DIST / "release"
    if release.is_dir():
        for path in sorted(release.iterdir()):
            if path.is_file():
                print(f"  {path.relative_to(ROOT)}  sha256={sha256(path)}")
    bundle = DIST / "TDM.app"
    if bundle.is_dir():
        print(f"  {bundle.relative_to(ROOT)}")
    tree = DIST / "TDM"
    if tree.is_dir():
        print(f"  {tree.relative_to(ROOT)}")
    if binary.is_file():
        print(f"  {binary.relative_to(ROOT)}  sha256={sha256(binary)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build", description="Build and smoke-test the TDM GUI bundle."
    )
    parser.add_argument("--version", help="version to stamp into the build")
    parser.add_argument(
        "--package",
        action="store_true",
        help="also build the platform installer",
    )
    parser.add_argument(
        "--skip-smoke",
        action="store_true",
        help="skip the frozen --self-check (not recommended)",
    )
    args = parser.parse_args(argv)

    version = resolve_version(args.version)
    print(f"building TDM {version} for {sys.platform}")

    build(version)
    binary = frozen_binary()
    copy_docs()
    check_glibc_floor()
    if args.skip_smoke:
        print("smoke: skipped")
    else:
        report = smoke_test(version)
        print(f"smoke: ok (tk {report.get('tk')}, cryptg {report.get('cryptg')})")

    if args.package:
        package(version)

    report_artifacts(binary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
