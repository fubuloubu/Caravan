import ape
import pytest

from caravan.messages import ActionType


def test_configuration(networks, van, VERSION, THRESHOLD, owners):
    assert set(van.signers) == set(o.address for o in owners)
    assert van.threshold == THRESHOLD

    assert van.admin_guard is None
    assert van.execute_guard is None

    enabled, name, version, chain_id, address, salt, extensions = (
        van.contract.eip712Domain()
    )
    assert enabled == b"\x0f"  # NOTE: all but `salt` is enabled
    assert name == "Caravan Wallet"
    assert version == str(VERSION)
    assert chain_id == networks.provider.chain_id
    assert address == van.address
    assert salt == b"\x00" * 32
    assert extensions == []

    # NOTE: Initial head should be as if it did a ROTATE_SIGNERS modification
    assert (
        van.head
        == ActionType.ROTATE_SIGNERS(
            [o.address for o in owners],
            [],
            THRESHOLD,
            address=address,
            chain_id=chain_id,
            version=version,
            parent=b"\x00" * 32,
        ).hash
    )


def test_initialize(singleton, van, THRESHOLD, owners):
    assert van.contract.IMPLEMENTATION() == singleton

    with ape.reverts():  # dev_message="only Proxy can initialize"):
        # NOTE: Can't initialize singleton
        singleton.initialize(owners, THRESHOLD, sender=owners[0])

    with ape.reverts():  # dev_message="can only initialize once"):
        # NOTE: Can't initialize proxy a second time
        van.contract.initialize(owners, THRESHOLD, sender=owners[0])


@pytest.mark.parametrize("calls", ["0_calls", "1_call", "2_calls"])
def test_execute(accounts, van, owners, calls):
    msg = van.new_batch()

    accounts[-1].transfer(van.contract, "10 ether")
    starting_balance = {van.contract.address: van.contract.balance}
    for idx in range(total_calls := int(calls.split("_")[0])):
        msg.add_transfer(
            target=accounts[idx],
            value=f"{idx + 1} ether",
            data=f"{idx}".encode("utf-8"),
        )

        # NOTE: track for later assertion
        starting_balance[a.address] = (a := accounts[idx]).balance

    assert van.head == msg.parent
    assert msg not in van.queue

    van.stage(msg)
    assert msg in van.queue

    receipt = van.commit(msg, sender=owners[0])
    if total_calls > 0:
        # NOTE: We need this to ensure accurate tracking for later in `sum()`
        starting_balance[owners[0].address] -= receipt.gas_used * receipt.gas_price
    assert van.head == msg.hash

    if total_calls > 0:
        assert receipt.events == [
            van.contract.Executed(
                executor=owners[0],
                target=account,
                value=(idx + 1) * int(1e18),
                data=f"{idx}".encode("utf-8"),
            )
            for idx, account in enumerate(accounts[:total_calls])
        ]
        assert all(
            a.balance == starting_balance[a.address] + (idx + 1) * int(1e18)
            for idx, a in enumerate(accounts[:total_calls])
        )
        assert van.contract.balance == (
            starting_balance[van.contract.address]
            - sum(
                a.balance - starting_balance[a.address] for a in accounts[:total_calls]
            )
        )

    else:
        assert receipt.events == []
        assert van.contract.balance == starting_balance[van.contract.address]
