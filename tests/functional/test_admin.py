from packaging.version import Version
from caravan.messages import ActionType


def test_upgrade(VERSION, owners, create_release, singleton, van):
    new_version = Version(f"{VERSION}+post.0")
    new_impl = create_release(version=new_version)

    msg = ActionType.UPGRADE_IMPLEMENTATION(new_impl.address, van=van)
    assert van.head == msg.parent
    assert msg not in van.queue

    van.stage(msg)
    assert msg in van.queue

    receipt = van.commit(msg, sender=owners[0])
    assert van.head == msg.hash

    assert receipt.events == [
        van.contract.ImplementationUpgraded(
            executor=owners[0],
            old=singleton,
            new=new_impl,
        ),
    ]
    assert van.contract.IMPLEMENTATION() == new_impl


def test_rotate_signers(accounts, owners, van):
    msg = ActionType.ROTATE_SIGNERS(
        [accounts[len(owners)].address],
        [owners[0].address],
        van.threshold,
        van=van,
    )
    assert van.head == msg.parent
    assert msg not in van.queue

    van.stage(msg)
    assert msg in van.queue

    receipt = van.commit(msg, sender=owners[0])
    assert van.head == msg.hash

    assert receipt.events == [
        van.contract.SignersRotated(
            executor=owners[0],
            num_signers=len(owners),
            threshold=van.threshold,
            signers_added=[accounts[len(owners)]],
            signers_removed=[owners[0]],
        ),
    ]
    assert van.signers[0] == accounts[1]
    assert van.signers[len(owners) - 1] == accounts[len(owners)]
