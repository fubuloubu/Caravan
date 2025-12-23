import subprocess

import pytest
from click.testing import CliRunner
from ape.utils.os import create_tempdir


@pytest.fixture(scope="session")
def DEFAULT_ARGS():
    # NOTE: Must be done w/ Foundry, otherwise doesn't support `CreateX.inject()`
    return ["--network", "::foundry", "--account", "TEST::0"]


@pytest.fixture(scope="session")
def cli():
    from caravan.__main__ import cli

    yield cli


@pytest.fixture()
def runner(monkeypatch):
    with (
        create_tempdir() as XDG_CONFIG_HOME,
        create_tempdir() as XDG_CACHE_HOME,
    ):
        monkeypatch.setattr("caravan.settings.USER_CONFIG_DIR", XDG_CONFIG_HOME)
        monkeypatch.setattr("caravan.settings.USER_CACHE_DIR", XDG_CACHE_HOME)

        runner = CliRunner(
            env={
                "XDG_CONFIG_HOME": str(XDG_CONFIG_HOME),
                "XDG_CACHE_HOME": str(XDG_CACHE_HOME),
            }
        )

        with runner.isolated_filesystem():
            p = subprocess.Popen(
                ["ape", "networks", "run", "--network", "::foundry"], shell=True
            )
            yield runner
            p.terminate()
