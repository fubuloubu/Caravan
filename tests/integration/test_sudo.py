import pytest

from caravan.settings import FACTORY_DETERMINISTIC_ADDRESS as FACTORY
from caravan.settings import SINGLETON_DETERMINISTIC_ADDRESSES as SINGLETONS


def test_deploy_factory(DEFAULT_ARGS, runner, cli):
    cmd = ["sudo", "deploy", "factory", *DEFAULT_ARGS]
    assert (result := runner.invoke(cli, cmd)).exit_code == 0
    assert f"CaravanFactory deployed to {FACTORY}" in result.output, result.output


@pytest.mark.parametrize("version", SINGLETONS)
def test_deploy_singleton(DEFAULT_ARGS, runner, cli, version):
    cmd = ["sudo", "deploy", "singleton", "--version", version, *DEFAULT_ARGS]
    assert (result := runner.invoke(cli, cmd)).exit_code == 0
    assert f"Caravan v{version} deployed to {SINGLETONS[version]}" in result.output, (
        result.output
    )
