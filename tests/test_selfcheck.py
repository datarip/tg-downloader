"""The parts of the packaged-build self-check that need no display.

``run_self_check`` opens a real Tk root, so it is deliberately never called
here: pytest must stay headless. The frozen build is what exercises it end to
end (`--self-check`), and this file covers the pieces that can be checked
without a window: the crypto backend, the version source, asset lookup and the
argument parsing that gets to the self-check in the first place.
"""

from __future__ import annotations

import sys

import pytest

from tg_downloader import __version__, app, resources, telegram


def test_crypto_backend_reports_the_native_backend() -> None:
    """The dev environment installs cryptg, so the fast path must be the one in use."""
    assert telegram.crypto_backend() == "cryptg"


def test_version_comes_from_the_package() -> None:
    assert isinstance(__version__, str)
    assert __version__.count(".") >= 2


def test_version_flag_exits_before_opening_a_window(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        app.main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_asset_roots_include_the_source_checkout() -> None:
    roots = resources.asset_roots()
    assert any(root.name == "icons" for root in roots), roots


def test_window_icon_resolves_from_the_source_tree() -> None:
    icon = resources.asset_path("tdm.png")
    assert icon.is_file()
    assert icon.name == "tdm.png"


def test_asset_path_prefers_the_frozen_bundle(tmp_path, monkeypatch) -> None:
    """`sys._MEIPASS/assets` is where PyInstaller puts the datas, so it wins."""
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "tdm.png").write_bytes(b"png")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert resources.asset_path("tdm.png") == assets / "tdm.png"


def test_missing_asset_raises_instead_of_returning_a_bad_path() -> None:
    with pytest.raises(FileNotFoundError):
        resources.asset_path("there-is-no-such-asset.png")
