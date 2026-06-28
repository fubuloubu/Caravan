# pragma version >=0.4.3
"""
@title Caravan Factory
@license Apache-2.0
@author ApeWorX LTD.
"""

PROXY_INITCODE: public(immutable(Bytes[1024]))

from . import ICaravan

event NewCaravan:
    release: indexed(address)
    deployer: indexed(address)
    salt: indexed(bytes32)
    version: String[12]
    tag: String[64]
    signers: DynArray[address, 11]
    threshold: uint256
    new_proxy: address


@deploy
def __init__(proxy_initcode: Bytes[1024]):
    PROXY_INITCODE = proxy_initcode


@external
def new(
    release: ICaravan,
    signers: DynArray[address, 11],
    threshold: uint256,
    tag: String[64] = "",
) -> address:
    # NOTE: This does *not* depend on the release chosen by the user
    salt: bytes32 = keccak256(
        concat(
            abi_encode(signers, threshold),
            convert(tag, Bytes[64]),
        )
    )

    new_proxy: address = raw_create(
        PROXY_INITCODE,
        release.address,
        signers,
        threshold,
        salt=salt,
    )
    log NewCaravan(
        release=release.address,
        deployer=msg.sender,
        salt=salt,
        version=staticcall release.VERSION(),
        tag=tag,
        signers=signers,
        threshold=threshold,
        new_proxy=new_proxy,
    )

    return new_proxy
