from collections.abc import Iterator
from functools import partial
import itertools
from typing import TYPE_CHECKING, Any

from ape.contracts import ContractCall, ContractInstance
from ape.exceptions import AccountsError
from ape.types import AddressType, HexBytes, MessageSignature
from ape.types.signatures import recover_signer
from ape.utils import ZERO_ADDRESS, ManagerAccessMixin, cached_property
from ape_ethereum import multicall
from ape_ethereum.multicall.exceptions import UnsupportedChainError
from ethpm_types.abi import ABIType, MethodABI
from eth_utils import to_int, to_bytes, keccak
from packaging.version import Version

from .messages import ActionType, Execute
from .modules import ModuleManager
from .packages import PackageType, STABLE_VERSION

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ape.api import AccountAPI, ReceiptAPI

    from .factory import Factory
    from .messages.admin import Modify
    from .queue import QueueManager, QueueItem


# TODO: Subclass Ape's AccountAPI and make it a plugin
class Caravan(ManagerAccessMixin):
    def __init__(
        self,
        address: AddressType,
        version: Version | None = None,
        factory: "Factory | None" = None,
        queue: "QueueManager | None" = None,
    ):
        self.address = address

        if factory:
            # NOTE: Override cached value (useful for testing)
            self.factory = factory

        if version:
            # NOTE: Override cached value (useful for testing)
            self.version = version

        if queue:
            # NOTE: Override cached value (useful for testing)
            self.queue = queue

    @cached_property
    def factory(self) -> "Factory":
        from .factory import Factory

        return Factory()

    @cached_property
    def queue(self) -> "QueueManager":
        # NOTE: This lets us more easily test
        from .queue import QueueManager

        return QueueManager.load(base=self.head)

    @cached_property
    def version(self) -> Version:
        # NOTE: Do this raw so we can determine the proper version to use
        call = ContractCall(
            abi=MethodABI(
                name="VERSION",
                stateMutability="pure",
                outputs=[ABIType(type="string")],
            ),
            address=self.address,
        )
        return Version(call())

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.address} version={self.version})"

    @cached_property
    def contract(self) -> ContractInstance:
        proxy_code = PackageType.PROXY().contract_type.get_runtime_bytecode()
        if self.provider.get_code(self.address) != proxy_code:
            raise RuntimeError(f"{self.address} is not a CaravanProxy")

        return PackageType.SINGLETON(self.version).at(
            self.address,
            fetch_from_explorer=False,
            detect_proxy=False,
            # TODO: `proxy_info=self.contract.implementation()` using EIP-1967
        )

    @property
    def threshold(self) -> int:
        return self.contract.threshold()

    @property
    def signers(self) -> list[AddressType]:
        return self.contract.signers()

    @property
    def head(self) -> HexBytes:
        return self.contract.head()

    def set_head(self, new_head: HexBytes):
        # NOTE: allows modifying head for local simulation and testing
        # NOTE: Storage slot 1 in contract is head
        self.provider.set_storage(self.address, 1, new_head)

    @property
    def local_signers(self) -> list["AccountAPI"]:
        from ape.api.accounts import ImpersonatedAccount

        local_signers = []
        for signer_address in self.signers:
            try:
                signer = self.account_manager[signer_address]

            except AccountsError:
                continue

            if not isinstance(signer, ImpersonatedAccount):
                local_signers.append(signer)

        return local_signers

    def onchain_approvals(self, msghash: "HexBytes") -> list[AddressType]:
        call = multicall.Call()

        for signer in (signers := self.signers):
            call.add(self.contract.approved, msghash, signer)

        try:
            approved = dict(zip(signers, call()))
            get_approval = approved.__getitem__

        except UnsupportedChainError:
            get_approval = partial(self.contract.approved, msghash)

        return list(filter(get_approval, signers))

    def impersonate_signature(self, msghash: "HexBytes", signer: AddressType):
        # NOTE: `approved` is `msg.hash => address => bool` @ slot 2
        slot = b"\x00" * 31 + to_bytes(2)

        slot = keccak(msghash + slot)

        address_bytes32 = to_bytes(hexstr=signer)
        address_bytes32 = b"\x00" * (32 - len(address_bytes32)) + address_bytes32
        slot = keccak(address_bytes32 + slot)

        self.provider.set_storage(self.address, to_int(slot), b"\x01")
        # TODO: Use native ape slot indexing, once available
        #       e.g. `self.contract.approved[msg.hash][signer] = bool`

    def get_signatures(
        self, msg: "Modify | Execute", skip: set[AddressType] | None = None
    ) -> Iterator[tuple[AddressType, MessageSignature]]:
        """Yield off-chain signatures first from queue confirmations then ask local signers"""

        if skip is None:
            skip = set(self.onchain_approvals(msg.hash))

        try:
            signatures = self.queue.find(msg.hash).signatures

        except IndexError:
            # NOTE: Not in queue yet
            signatures = {}

        # First, yield all existing signatures in-queue (if any)
        for signer, sig in signatures.items():
            if recover_signer(msg.signable_message, sig) != signer:
                raise RuntimeError(f"Corrupted signature: {sig}")

            elif signer not in skip:
                yield signer, sig

            skip.add(signer)

        # Then, collect new signatures from local signers
        for signer in set(self.local_signers) - skip:
            if sig := signer.sign_message(msg):
                yield signer.address, sig

        # NOTE: If we made it here, we probably needed more signatures

    def stage(self, msg: "Modify | Execute") -> "QueueItem":
        """Stage message ``msg`` into queue, after collecting signatures from available local signers."""
        signatures = dict(
            # NOTE: `islice` only asks for up to N
            itertools.islice(self.get_signatures(msg), self.threshold)
        )

        # NOTE: Otherwise we'd have an import cycle
        from .queue import QueueItem

        # TODO: Add logging?
        self.queue.add(item := QueueItem(message=msg, signatures=signatures))

        if not self.provider.network.is_dev:
            # NOTE: Don't save permanent changes on ephemeral networks
            self.queue.save()

        return item

    def commit(self, msg: "Modify | Execute | HexBytes", **txn_args) -> "ReceiptAPI":
        """Submit message ``msg`` on-chain, collecting signatures if needed."""

        if isinstance(msg, HexBytes):
            msg = self.queue.find(msg).message

        if msg.parent != self.head:
            raise RuntimeError("Cannot execute call, wrong head")

        # TODO: Support `impersonate=True`
        #       (set storage directly, so `len(approved) >= self.threshold`)
        approved = self.onchain_approvals(msg.hash)

        try:
            signatures = self.queue.find(msg.hash).signatures

        except IndexError:
            raise RuntimeError(f"Message {msg} not in queue. Please stage first")

        if (have := len(approved) + len(signatures)) < (threshold := self.threshold):
            raise RuntimeError(f"Not enough signatures. Need {threshold - have} more.")

        # NOTE: Skip `.parent`, contract implicitly uses `.head`
        fn_args = list(msg)[1:]
        if signatures:
            # TODO: Add logging?
            fn_args.append([sig.encode_rsv() for sig in signatures.values()])

        fn = getattr(self.contract, msg.__class__.__name__.lower())
        receipt = fn(*fn_args, **txn_args)

        if not self.provider.network.is_dev:
            # NOTE: Don't save permanent changes on ephemeral networks
            self.queue.rebase(self.head)
            self.queue.save()

        return receipt

    def merge(self, new_head: HexBytes, **txn_args) -> "ReceiptAPI":
        """Commit **all** messages in branch from ``self.head`` to ``new_head`` on-chain."""

        txn = multicall.Transaction()
        threshold = self.threshold

        for item in self.queue.get_branch(new_head):
            fn = getattr(self.contract, item.message_type.lower())
            # NOTE: Skip `.parent`, contract implicitly uses `.head`
            fn_args = list(item.message)[1:]

            if len(approvals := self.onchain_approvals(item.hash)) >= threshold:
                txn.add(fn, *fn_args)

            elif len(set(approvals) | set(item.signatures)) >= threshold:
                signatures = [sig.encode_rsv() for sig in item.signatures.values()]
                txn.add(fn, *fn_args, signatures)

            else:
                raise RuntimeError(f"Cannot merge {item}: not enough signatures")

            # TODO: Look for modified `threshold` or `self.signers`
            # TODO: Look for version migration

        receipt = txn(**txn_args)

        if not self.provider.network.is_dev:
            # NOTE: Don't save permanent changes on ephemeral networks
            self.queue.rebase(self.head)
            self.queue.save()

        return receipt

    #### Admin methods (uses `Modify` message type) ####

    def migrate(
        self,
        new_version: Version | str = STABLE_VERSION,
        parent: HexBytes | None = None,
    ) -> "QueueItem":
        if not (release := self.factory.get_release(new_version)):
            raise ValueError(f"No release for {new_version} deployed on this chain")

        return self.stage(
            ActionType.UPGRADE_IMPLEMENTATION(
                release.address,
                van=self,
                parent=parent,
            ),
        )

    def rotate_signers(
        self,
        signers_to_add: "Iterable[Any] | None" = None,
        signers_to_remove: "Iterable[Any] | None" = None,
        threshold: int | None = 0,
        parent: HexBytes | None = None,
    ) -> "QueueItem":
        signers_to_add = (
            [self.conversion_manager.convert(s, AddressType) for s in signers_to_add]
            if signers_to_add is not None
            else []
        )
        signers_to_remove = (
            [self.conversion_manager.convert(s, AddressType) for s in signers_to_remove]
            if signers_to_remove is not None
            else []
        )

        if invalid_signers := set(signers_to_remove) - set(signers := self.signers):
            raise ValueError(f"Can't remove signers: {', '.join(invalid_signers)}")

        elif invalid_signers := set(signers) & set(signers_to_add):
            raise ValueError(f"Can't add signers: {', '.join(invalid_signers)}")

        elif (
            threshold
            and (
                max_threshold := len(signers)
                + len(signers_to_add)
                - len(signers_to_remove)
            )
            < threshold
        ):
            raise ValueError(
                f"Can't set threshold to {threshold}, must be less than/equal to {max_threshold}."
            )

        return self.stage(
            ActionType.ROTATE_SIGNERS(
                signers_to_add,
                signers_to_remove,
                threshold or self.threshold,
                van=self,
                parent=parent,
            ),
        )

    def add_signers(self, *signers: Any, parent: HexBytes | None = None) -> "QueueItem":
        return self.rotate_signers(signers_to_add=signers, parent=parent)

    def remove_signers(
        self, *signers: Any, parent: HexBytes | None = None
    ) -> "QueueItem":
        return self.rotate_signers(signers_to_remove=signers, parent=parent)

    def change_threshold(
        self, threshold: int, parent: HexBytes | None = None
    ) -> "QueueItem":
        return self.rotate_signers(threshold=threshold, parent=parent)

    @property
    def modules(self) -> ModuleManager:
        return ModuleManager(self)

    @property
    def admin_guard(self) -> ContractInstance | None:
        if (admin_guard := self.contract.admin_guard()) != ZERO_ADDRESS:
            return self.chain_manager.contracts.instance_at(admin_guard)

        return None

    def set_admin_guard(
        self,
        new_guard: Any = ZERO_ADDRESS,
        parent: HexBytes | None = None,
    ) -> "QueueItem":
        new_guard = self.conversion_manager.convert(new_guard, AddressType)
        return self.stage(
            ActionType.SET_ADMIN_GUARD(new_guard, van=self, parent=parent)
        )

    @admin_guard.setter
    def assign_admin_guard(self, new_guard: Any):
        self.set_admin_guard(new_guard)

    @admin_guard.deleter
    def delete_admin_guard(self):
        self.set_admin_guard()

    @property
    def execute_guard(self) -> ContractInstance | None:
        if (execute_guard := self.contract.execute_guard()) != ZERO_ADDRESS:
            return self.chain_manager.contracts.instance_at(execute_guard)

        return None

    def set_execute_guard(
        self,
        new_guard: Any = ZERO_ADDRESS,
        parent: HexBytes | None = None,
    ) -> "QueueItem":
        new_guard = self.conversion_manager.convert(new_guard, AddressType)
        return self.stage(
            ActionType.SET_EXECUTE_GUARD(new_guard, van=self, parent=parent)
        )

    @execute_guard.setter
    def assign_execute_guard(self, new_guard: Any):
        self.set_execute_guard(new_guard)

    @execute_guard.deleter
    def delete_execute_guard(self):
        self.set_execute_guard()

    #### Normal transactions (uses `Execute` message type)

    def new_batch(self, parent: HexBytes | None = None) -> Execute:
        return Execute.new(van=self, parent=parent)
