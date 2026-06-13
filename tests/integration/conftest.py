import json

import pytest
from click.testing import CliRunner
from ape.utils.os import create_tempdir


def _clear_ape_local_caches():
    from ape import chain

    chain.contracts.clear_local_caches()


@pytest.fixture(scope="session")
def config_manager():
    from ape.utils.basemodel import ManagerAccessMixin

    return ManagerAccessMixin.config_manager


@pytest.fixture(scope="session")
def foundry_provider(config_manager, networks):
    with config_manager.isolate_data_folder():
        networks.__dict__.pop("running_nodes", None)
        provider_settings = {"host": "auto"}
        with networks.parse_network_choice(
            "::foundry",
            provider_settings=provider_settings,
            disconnect_after=True,
        ) as provider:
            yield provider


@pytest.fixture(scope="session")
def DEFAULT_ARGS(foundry_provider):
    # NOTE: Must be done w/ Foundry, otherwise doesn't support `CreateX.inject()`
    provider_settings = {"host": foundry_provider.uri, "manage_process": False}
    return [
        "--network",
        "::foundry",
        "--provider-settings",
        json.dumps(provider_settings),
        "--account",
        "TEST::0",
    ]


@pytest.fixture()
def cli(runner):
    from caravan.__main__ import cli

    yield cli


@pytest.fixture()
def runner(monkeypatch, foundry_provider):
    with (
        create_tempdir() as XDG_CONFIG_HOME,
        create_tempdir() as XDG_CACHE_HOME,
    ):
        from caravan import __main__ as caravan_cli
        from caravan import settings

        monkeypatch.setattr("caravan.settings.USER_CONFIG_DIR", XDG_CONFIG_HOME)
        monkeypatch.setattr("caravan.settings.USER_CACHE_DIR", XDG_CACHE_HOME)
        monkeypatch.setattr(caravan_cli, "USER_CONFIG_DIR", XDG_CONFIG_HOME)
        monkeypatch.setattr(settings, "USER_CONFIG_DIR", XDG_CONFIG_HOME)
        monkeypatch.setattr(settings, "USER_CACHE_DIR", XDG_CACHE_HOME)

        runner = CliRunner(
            env={
                "XDG_CONFIG_HOME": str(XDG_CONFIG_HOME),
                "XDG_CACHE_HOME": str(XDG_CACHE_HOME),
            }
        )

        with runner.isolated_filesystem():
            _clear_ape_local_caches()
            try:
                yield runner
            finally:
                _clear_ape_local_caches()


@pytest.fixture()
def run_cmd(cli, runner):
    def run_cmd(*args, prompt_args=None):
        result = runner.invoke(cli, args, input=prompt_args)
        if result.exception:
            raise result.exception

        elif result.exit_code != 0:
            raise RuntimeError(f"Error:\n{result.output}")

        return result

    return run_cmd


@pytest.fixture()
def deploy_system(DEFAULT_ARGS, run_cmd):
    def deploy_system():
        run_cmd("sudo", "deploy", "factory", *DEFAULT_ARGS)
        run_cmd("sudo", "deploy", "singleton", *DEFAULT_ARGS)

    yield deploy_system


@pytest.fixture()
def create_wallet(DEFAULT_ARGS, run_cmd, deploy_system):
    def create_wallet():
        deploy_system()

        return run_cmd("new", "TEST::0", "TEST::1", "TEST::2", *DEFAULT_ARGS)

    yield create_wallet
