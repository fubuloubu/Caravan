# pragma version >=0.4.3
"""
@title Caravan
@license Apache-2.0
@author ApeWorX LTD.
"""

from . import ICaravan
implements: ICaravan

from .guards import IAdminGuard
from .guards import IExecuteGuard

NAME: constant(String[15]) = "Caravan Wallet"
NAMEHASH: constant(bytes32) = keccak256(NAME)
# NOTE: Update this before each release (controls EIP712 Domain)
VERSION: public(immutable(String[12]))
VERSIONHASH: immutable(bytes32)

EIP712_DOMAIN_TYPEHASH: constant(bytes32) = keccak256(
    "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)

MODIFY_TYPEHASH: constant(bytes32) = keccak256(
    "Modify(bytes32 parent,uint256 action,bytes data)"
)

CALL_TYPEHASH: constant(bytes32) = keccak256(
    "Call(address target,uint256 value,bool success_required,bytes data)"
)
EXECUTE_TYPEHASH: constant(bytes32) = keccak256(
    "Execute(bytes32 parent,Call[] calls)Call(address target,uint256 value,bool success_required,bytes data)"
)

# @dev The current implementation address for `CaravanProxy`
IMPLEMENTATION: public(address)
# NOTE: Must be first slot, this will be used by upgradeable proxy for delegation

# @dev The last message hash (`Modify` or `Execute` struct) that was executed
head: public(bytes32)

# @dev Mapping of pre-approved transaction hashes to approval timestamps, indexed by signer
approved: public(HashMap[bytes32, HashMap[address, uint256]])

# Signer properties
# @dev All current signers (unordered)
_signers: DynArray[address, 11]

# @dev Number of signers required to execute an action
threshold: public(uint256)
# NOTE: invariant `0 < threshold <= len(signers)`

# @dev Before/after checker for Update actions
admin_guard: public(IAdminGuard)

# @dev Before/after checker for Execute actions
execute_guard: public(IExecuteGuard)

# @dev Modules enabled for this wallet
module_enabled: public(HashMap[address, bool])

# NOTE: Future variables (used for new core features) must be added below



@deploy
def __init__(version: String[12]):
    VERSION = version
    VERSIONHASH = keccak256(version)


# NOTE: IERC5267
@view
@external
def eip712Domain() -> (
    bytes1,
    String[50],
    String[20],
    uint256,
    address,
    bytes32,
    DynArray[uint256, 32],
):
    return (
        # NOTE: `0x0f` equals `01111` (`salt` is not used)
        0x0f,
        NAME,
        VERSION,
        chain.id,
        self,
        empty(bytes32), # Salt is ignored
        empty(DynArray[uint256, 32]),  # No extensions
    )


@view
def _DOMAIN_SEPARATOR() -> bytes32:
    return keccak256(
        abi_encode(EIP712_DOMAIN_TYPEHASH, NAMEHASH, VERSIONHASH, chain.id, self)
    )


@view
@external
def DOMAIN_SEPARATOR() -> bytes32:
    return self._DOMAIN_SEPARATOR()


@view
def _hash_typed_data_v4(struct_hash: bytes32) -> bytes32:
    return keccak256(concat(x"1901", self._DOMAIN_SEPARATOR(), struct_hash))


@external
def initialize(signers: DynArray[address, 11], threshold: uint256):
    assert self.IMPLEMENTATION != empty(address)  # dev: only Proxy can initialize
    assert self.threshold == 0  # dev: can only initialize once
    assert threshold > 0 and threshold <= len(signers)

    self._signers = signers
    self.threshold = threshold

    # NOTE: Initialize head to non-zero, network-specific value as if this action was performed
    self.head = self._hash_typed_data_v4(
        keccak256(
            abi_encode(
                MODIFY_TYPEHASH,
                empty(bytes32),
                ICaravan.ActionType.ROTATE_SIGNERS,
                # NOTE: Per EIP712, Dynamic structures are encoded as the hash of their contents
                keccak256(
                    abi_encode(
                        signers, empty(DynArray[address, 11]), threshold
                    )
                ),
            )
        )
    )


@view
@external
def signers() -> DynArray[address, 11]:
    return self._signers


@external
def set_approval(msghash: bytes32, approved: bool = True):
    assert msg.sender in self._signers, "Not a signer"
    if approved:
        self.approved[msghash][msg.sender] = block.timestamp
    else:
        self.approved[msghash][msg.sender] = 0


def _verify_signatures(msghash: bytes32, signatures: DynArray[Bytes[65], 11]):
    approvals_needed: uint256 = self.threshold
    signers: DynArray[address, 11] = self._signers
    already_approved: DynArray[address, 11] = []

    for signer: address in signers:
        approved_at: uint256 = self.approved[msghash][signer]
        if 0 < approved_at and approved_at <= block.timestamp:
            already_approved.append(signer)  # NOTE: Track for use in next loop
            approvals_needed -= 1  # dev: underflow
            # NOTE: Get some gas back by deleting storage
            self.approved[msghash][signer] = 0

        if approvals_needed == 0:
            return  # Skip signature verification because we have enough pre-approvals

    assert len(signatures) >= approvals_needed, "Not enough approvals"

    # NOTE: We already checked that we have enough signatures,
    #       this loops checks uniqueness/membership of recovered signers
    for sig: Bytes[65] in signatures:
        # NOTE: Signatures should be 65 bytes in RSV order
        r: bytes32 = convert(slice(sig, 0, 32), bytes32)
        s: bytes32 = convert(slice(sig, 32, 32), bytes32)
        v: uint8 = convert(slice(sig, 64, 1), uint8)
        signer: address = ecrecover(msghash, v, r, s)
        assert signer in signers, "Invalid Signer"
        assert signer not in already_approved, "Signer cannot approve twice"
        already_approved.append(signer)


def _rotate_signers(
    signers_to_add: DynArray[address, 11],
    signers_to_rm: DynArray[address, 11],
    threshold: uint256,
):
    current_signers: DynArray[address, 11] = self._signers
    new_signers: DynArray[address, 11] = []

    for signer: address in current_signers:
        if signer not in signers_to_rm:
            new_signers.append(signer)
        # else: skips adding `signer` to `new_signers`

    # NOTE: Ignores if `signer` in `signers_to_rm` not in `current_signers`

    for signer: address in signers_to_add:
        assert signer not in new_signers, "Signer cannot be added twice"
        new_signers.append(signer)

    if threshold > 0:
        assert threshold <= len(new_signers), "Invalid threshold"
        self.threshold = threshold

    self._signers = new_signers

    log ICaravan.SignersRotated(
        executor=msg.sender,
        num_signers=len(new_signers),
        threshold=self.threshold,  # NOTE: In case there was no change
        signers_added=signers_to_add,
        signers_removed=signers_to_rm,
    )


@external
def modify(
    action: ICaravan.ActionType,
    data: Bytes[65535],
    signatures: DynArray[Bytes[65], 11] = [],
    # NOTE: Skip argument to use on-chain approvals
):
    msghash: bytes32 = self._hash_typed_data_v4(
        # NOTE: Per EIP712, Dynamic structures are encoded as the hash of their contents
        keccak256(abi_encode(MODIFY_TYPEHASH, self.head, action, keccak256(data)))
    )
    self._verify_signatures(msghash, signatures)
    self.head = msghash

    admin_guard: IAdminGuard = self.admin_guard
    if admin_guard.address != empty(address):
        extcall admin_guard.preUpdateCheck(action, data)

    if action == ICaravan.ActionType.UPGRADE_IMPLEMENTATION:
        new: address = abi_decode(data, address)
        log ICaravan.ImplementationUpgraded(
            executor=msg.sender,
            old=self.IMPLEMENTATION,
            new=new,
        )
        self.IMPLEMENTATION = new

    elif action == ICaravan.ActionType.ROTATE_SIGNERS:
        signers_to_add: DynArray[address, 11] = []
        signers_to_rm: DynArray[address, 11] = []
        threshold: uint256 = 0
        signers_to_add, signers_to_rm, threshold = abi_decode(
            data,
            (DynArray[address, 11], DynArray[address, 11], uint256),
        )
        self._rotate_signers(signers_to_add, signers_to_rm, threshold)

    elif action == ICaravan.ActionType.CONFIGURE_MODULE:
        module: address = empty(address)
        enabled: bool = False
        module, enabled = abi_decode(data, (address, bool))
        log ICaravan.ModuleUpdated(
            executor=msg.sender,
            module=module,
            enabled=enabled,
        )
        self.module_enabled[module] = enabled

    elif action == ICaravan.ActionType.SET_ADMIN_GUARD:
        # NOTE: Don't use `admin_guard` as it would override above
        guard: IAdminGuard = abi_decode(data, IAdminGuard)
        log ICaravan.AdminGuardUpdated(
            executor=msg.sender,
            old=admin_guard.address,
            new=guard.address,
        )
        self.admin_guard = guard

    elif action == ICaravan.ActionType.SET_EXECUTE_GUARD:
        guard: IExecuteGuard = abi_decode(data, IExecuteGuard)
        log ICaravan.ExecuteGuardUpdated(
            executor=msg.sender,
            old=self.execute_guard.address,
            new=guard.address,
        )
        self.execute_guard = guard

    else:
        raise "Unsupported"

    if admin_guard.address != empty(address):
        # NOTE: We use the old admin guard to execute the check
        extcall admin_guard.postUpdateCheck()


@external
def execute(
    calls: DynArray[ICaravan.Call, 8],
    signatures: DynArray[Bytes[65], 11] = [],
    # NOTE: Skip argument to use on-chain approvals, or for module use
):
    if not self.module_enabled[msg.sender]:
        # Hash message and validate signatures

        # Step 1: Encode struct to list of 32 byte hash of items
        encoded_call_members: DynArray[bytes32, 8] = []
        for call: ICaravan.Call in calls:
            encoded_call_members.append(
                # NOTE: Per EIP712, structs are encoded as the hash of their contents (incl. Typehash)
                keccak256(
                    abi_encode(
                        CALL_TYPEHASH,
                        call.target,
                        call.value,
                        call.success_required,
                        # NOTE: Per EIP712, Dynamic ABI types are encoded as the hash of their contents
                        keccak256(call.data),
                    )
                )
            )

        # Step 2: Encode list of 32 byte items into single bytestring
        # NOTE: bytestring length including length because it's encoded as an array
        encoded_call_array: Bytes[32 * (8 + 1)] = abi_encode(encoded_call_members, ensure_tuple=False)
        # NOTE: Skip encoded length of encoded bytestring by slicing it off (start at byte 32)
        encoded_call_array = slice(encoded_call_array, 32, len(encoded_call_array) - 32)
        assert len(encoded_call_array) == 32 * len(calls)

        # Step 3: Hash concatenated item hashes, together with typehash, then with domain to get msghash
        msghash: bytes32 = self._hash_typed_data_v4(
            # NOTE: Per EIP712, Arrays are encoded as the hash of their encoded members, concated together
            keccak256(abi_encode(EXECUTE_TYPEHASH, self.head, keccak256(encoded_call_array)))
        )
        self._verify_signatures(msghash, signatures)
        self.head = msghash

    guard: IExecuteGuard = self.execute_guard
    for call: ICaravan.Call in calls:
        if guard.address != empty(address):
            extcall guard.preExecuteCheck(call)

        # NOTE: No delegatecalls allowed (cannot modify configuration via `update` this way)
        success: bool = True
        if call.success_required:
            # NOTE: Allow failure to bubble up normally
            raw_call(call.target, call.data, value=call.value)

        else:
            success = raw_call(
                call.target,
                call.data,
                value=call.value,
                revert_on_failure=False,
            )


        if guard.address != empty(address):
            extcall guard.postExecuteCheck()

        log ICaravan.Executed(
            executor=msg.sender,
            success=success,
            target=call.target,
            value=call.value,
            data=call.data,
        )
