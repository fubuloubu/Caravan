import pytest
from ape.utils import ZERO_ADDRESS
from packaging.version import Version
from caravan.messages import Execute


def test_size_limits():
    # NOTE: Use `Execute` directly to avoid parametrized fixture setup
    txn = Execute.new(
        parent=b"\x00" * 32,
        version=Version("0.1"),
        address=ZERO_ADDRESS,
        chain_id=1,
    )

    with pytest.raises(RuntimeError):
        # Can't add `.data` larger than `Execute.MAX_CALLDATA_SIZE`
        txn.add_raw(ZERO_ADDRESS, data=b"\x00" * (Execute.MAX_CALLDATA_SIZE + 1))

    for _ in range(Execute.MAX_CALLS):
        txn.add_raw(ZERO_ADDRESS, data=b"\x00" * Execute.MAX_CALLDATA_SIZE)

    with pytest.raises(RuntimeError):
        # Can't add more than `Execute.MAX_CALLS`
        txn.add_raw(ZERO_ADDRESS, data=b"\x00" * Execute.MAX_CALLDATA_SIZE)
