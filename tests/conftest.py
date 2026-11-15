"""Keep the developer's real credentials out of the test run.

``env_file_path()`` falls back to ``./.env``, and pytest runs from the repo
root — so without this, a real ``.env`` (or an exported ``TG_*``) would bleed
into every config test. Point the loader at a path that doesn't exist instead;
tests that want an env file set ``TG_ENV_FILE`` themselves.
"""

import os

import pytest

from tg_downloader.config import ENV_FILE_VAR, ENV_VARS


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch, tmp_path_factory):
    for var in ENV_VARS.values():
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(ENV_FILE_VAR, str(tmp_path_factory.mktemp("noenv") / "absent.env"))
    yield
    # apply_to_environ() writes to os.environ directly, behind monkeypatch's back.
    # Clear it here so one test's save can't shadow the next test's env file.
    for var in ENV_VARS.values():
        os.environ.pop(var, None)
