import pytest

from caravan.settings import FACTORY_DETERMINISTIC_ADDRESS as FACTORY
from caravan.settings import SINGLETON_DETERMINISTIC_ADDRESSES as SINGLETONS


def test_deploy_factory(DEFAULT_ARGS, run_cmd):
    result = run_cmd("sudo", "deploy", "factory", *DEFAULT_ARGS)
    assert f"CaravanFactory deployed to {FACTORY}" in result.output, (
        result.exception or result.output
    )


@pytest.mark.parametrize("version", SINGLETONS)
def test_deploy_singleton(DEFAULT_ARGS, run_cmd, version):
    result = run_cmd("sudo", "deploy", "singleton", "--version", version, *DEFAULT_ARGS)
    assert f"Caravan v{version} deployed to {SINGLETONS[version]}" in result.output, (
        result.output
    )
