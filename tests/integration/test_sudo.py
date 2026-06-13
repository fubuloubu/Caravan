import pytest
from ape.api.address import Address

from caravan.settings import FACTORY_DETERMINISTIC_ADDRESS as FACTORY
from caravan.settings import SINGLETON_DETERMINISTIC_ADDRESSES as SINGLETONS


def _get_deployed_address(output: str) -> str:
    for line in output.splitlines():
        if " deployed to " in line:
            return line.rsplit(" ", 1)[1]

    raise AssertionError(output)


def test_deploy_factory(DEFAULT_ARGS, run_cmd):
    result = run_cmd("sudo", "deploy", "factory", *DEFAULT_ARGS)
    assert _get_deployed_address(result.output) == FACTORY, result.output
    assert Address(FACTORY).is_contract


@pytest.mark.parametrize("version", SINGLETONS)
def test_deploy_singleton(DEFAULT_ARGS, run_cmd, version):
    result = run_cmd("sudo", "deploy", "singleton", "--version", version, *DEFAULT_ARGS)
    expected = SINGLETONS[version]
    assert _get_deployed_address(result.output) == expected, result.output
    assert Address(expected).is_contract
