import json
from pathlib import Path
from typing import Annotated, Self

from ape.types import AddressType, HexBytes, MessageSignature
from ape.types.signatures import recover_signer
from ape.utils import to_int
from eip712 import EIP712Domain
from eth_account import Account
from eth_account._utils.encode_typed_data.encoding_and_hashing import (
    hash_domain as web3_hash_domain,
)
from pydantic import BaseModel, PlainSerializer, model_validator

from .messages.admin import Modify
from .messages.execute import Execute
from .settings import USER_CACHE_DIR


def signature_serialize(s: MessageSignature) -> str:
    return s.encode_rsv().hex()


def hash_domain(domain_type: dict) -> HexBytes:
    return HexBytes(web3_hash_domain(domain_type))


class QueueItem(BaseModel):
    message: Execute | Modify
    signatures: dict[
        AddressType, Annotated[MessageSignature, PlainSerializer(signature_serialize)]
    ] = {}

    @classmethod
    def load(cls, path: Path, eip712_domain: EIP712Domain) -> Self:
        message = json.loads((path / "message.json").read_text())
        if "action" in message:
            message = Modify(**message, eip712_domain=eip712_domain)
        elif "calls" in message:
            message = Execute(**message, eip712_domain=eip712_domain)
        else:
            raise ValueError

        if message.hash.hex() != path.name:
            raise RuntimeError(f"Corrupted message at {path}")

        signatures = {
            AddressType(f.name): MessageSignature(r=raw[:32], s=raw[32:64], v=raw[-1])
            for f in (path / "signatures").iterdir()
            if len((raw := f.read_bytes())) == 65
        }
        if any(
            recover_signer(message.signable_message, sig) != signer
            for signer, sig in signatures.items()
        ):
            raise RuntimeError(f"Corrupted signature(s) at {path}")

        return cls(message=message, signatures=signatures)

    @classmethod
    def decode(cls, encoded_item: str, eip712_domain: EIP712Domain) -> Self:
        item = json.loads(encoded_item)
        if "action" in item["message"]:
            message = Modify(**item["message"], eip712_domain=eip712_domain)
        elif "calls" in item["message"]:
            message = Execute(**item["message"], eip712_domain=eip712_domain)
        else:
            raise ValueError

        signatures = {
            AddressType(signer): MessageSignature(r=raw[:32], s=raw[32:64], v=raw[-1])
            for signer, signature in item["signatures"].items()
            if len(raw := bytes.fromhex(signature)) == 65
        }
        if any(
            recover_signer(message.signable_message, sig) != signer
            for signer, sig in signatures.items()
        ):
            raise RuntimeError("Corrupted signature(s)")

        return cls(message=message, signatures=signatures)

    def save(self, path: Path):
        (path / "message.json").write_text(self.message.model_dump_json())
        (sigs_folder := path / "signatures").mkdir(exist_ok=True)
        for signer, sig in self.signatures.items():
            (sigs_folder / str(signer)).write_bytes(sig.encode_rsv())

    @model_validator(mode="after")
    def validate_duplicate_signers(self) -> Self:
        for signer, signature in self.signatures.items():
            if (
                Account.recover_message(
                    self.message.signable_message, vrs=tuple(signature)
                )
                != signer
            ):
                raise AssertionError("Invalid signer")

        return self

    @property
    def message_type(self) -> str:
        return self.message.__class__.__name__

    @property
    def parent(self) -> HexBytes:
        return self.message.parent

    @property
    def hash(self) -> HexBytes:
        return self.message.hash

    @property
    def confirmations(self) -> int:
        return len(self.signatures)

    def __hash__(self) -> int:
        return to_int(self.hash)

    def __repr__(self) -> str:
        return f"<{self.message_type} {self.hash.to_0x_hex()}>"

    def __str__(self) -> str:
        return self.hash.to_0x_hex()


class QueueManager(BaseModel):
    """Specialized Manager for indexing Caravan's off-chain message queue & signatures"""

    # QueueItem => list[QueueItem.hash]
    queue: dict[QueueItem, list[HexBytes]] = dict()
    base: HexBytes  # NOTE: Must always provide head!

    @model_validator(mode="after")
    def ensure_base(self) -> Self:
        self.rebase(self.base)
        return self

    @classmethod
    def load(cls, base: HexBytes, path: Path | str = USER_CACHE_DIR) -> Self:
        """
        Load queue from dir-like path ``path``, with base ``base``.

        NOTE: **Must** follow the following structure, either as a directory or zip file
        ```
        <msg.eip712_domain.hash>/
            domain.json => msg.eip712_domain.model_dump_json()
            <msg.hash>.json => msg.model_dump_json()
        ...  # For other domains (supports multiple wallets and versions)
        ```
        """

        # NOTE: Meant to pass `base` as the latest on-chain head
        if isinstance(path, str):
            path = Path(path)

        if not path.exists():
            raise RuntimeError(f"Path '{path}' does not exist, cannot load queue.")

        elif not path.is_dir():
            raise RuntimeError(f"Path '{path}' must be a directory, cannot load queue.")

        queue_items = []
        # First, parse all folders in directory/archive
        for domain_folder in path.iterdir():
            # NOTE: Each message is located at `<root> / <domainSeparator> / <messageHash>`
            #       This allows us to have messages indexed by `<domainSeparator>`, which
            #       supports indexing version upgrades, as well as more than one wallet at time
            if not domain_folder.is_dir():
                raise RuntimeError(
                    f"Corrupted queue: '{domain_folder.name}' is not a folder."
                )

            elif not (domain_file := domain_folder / "domain.json").exists():
                raise RuntimeError(
                    f"Corrupted queue: '{domain_folder.name}' does not contain domain file."
                )

            eip712_domain = EIP712Domain.model_validate_json(domain_file.read_text())

            for file in domain_folder.iterdir():
                if file.is_dir():
                    queue_items.append(
                        QueueItem.load(file, eip712_domain=eip712_domain)
                    )

        # Then re-create linked-list structure
        queue = {
            item: list(i.hash for i in queue_items if i.parent == item.hash)
            for item in queue_items
        }

        # Finally, rebase onto the specific base that we care about (dropping rest)
        return cls(queue=queue, base=base)

    def save(self, path: Path | str = USER_CACHE_DIR):
        if isinstance(path, str):
            return self.save(Path(path))

        elif not path.exists():
            path.mkdir(parents=True)

        elif not path.is_dir():
            raise RuntimeError(f"Cannot save queue to '{path}'.")

        for item in self.queue.keys():
            assert isinstance(domain := item.message._eip712_domain_, EIP712Domain)
            domain_separator = hash_domain(domain.model_dump(exclude_none=True))
            (domain_folder := path / domain_separator.hex()).mkdir(exist_ok=True)
            if not (domain_file := domain_folder / "domain.json").exists():
                domain_file.write_text(domain.model_dump_json(exclude_none=True))
            (domain_folder / item.hash.hex()).mkdir(exist_ok=True)
            item.save(domain_folder / item.hash.hex())

    @property
    def size(self) -> int:
        return len(self.queue)

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} size={self.size} base={self.base.to_0x_hex()}>"
        )

    def find(self, itemhash: HexBytes) -> QueueItem:
        try:
            return next(i for i in self.queue.keys() if i.hash == itemhash)
        except StopIteration:
            raise IndexError(f"{itemhash.to_0x_hex()} not in {self.__class__.__name__}")

    def __contains__(self, item: QueueItem | Modify | Execute | HexBytes) -> bool:
        if isinstance(item, QueueItem):
            return item in self.queue

        elif isinstance(item, HexBytes):
            try:
                self.find(item)
                return True

            except IndexError:
                return False

        return self.__contains__(item.hash)

    def parent(self, item: QueueItem | HexBytes) -> QueueItem:
        """Get parent of item in queue (if exists)"""
        # NOTE: Cannot run this on `self.base`

        if not isinstance(item, QueueItem):
            item = self.find(item)

        return self.find(item.parent)

    def children(self, item: QueueItem | HexBytes) -> list[QueueItem]:
        if isinstance(item, QueueItem):
            return [self.find(child_hash) for child_hash in self.queue[item]]
        # NOTE: Makes sure this works for `self.base` (which is not in .keys())
        return [k for k in self.queue.keys() if k.parent == item]

    def add(self, item: QueueItem):
        """Add and item to the queue, creating a new entry for itself as a parent"""

        if item in self.queue:
            raise IndexError(f"{item} already in {self}")

        if item.parent != self.base:
            # NOTE: Try to append to children of parent first, as it might raise
            try:
                self.queue[self.parent(item)].append(item.hash)

            except KeyError as e:
                raise IndexError(f"{item.parent} not in {self}") from e

        # NOTE: New queue item has no children
        self.queue[item] = list()

    def add_confirmations(
        self, itemhash: HexBytes, signatures: dict[AddressType, MessageSignature]
    ):
        self.find(itemhash).signatures.update(signatures)

    def get_branch(self, head: HexBytes) -> tuple[QueueItem, ...]:
        """Get sequence of QueueItems that take you from ``self.base`` to ``head``."""

        if head == self.base:
            return tuple()

        item = self.find(head)
        return tuple([*self.get_branch(item.parent), item])

    def rebase(self, new_base: HexBytes) -> int:
        """Update to ``new_base``, pruning all stale items from index"""

        if new_base == self.base:
            return 0  # noop

        stale_items = self.children(self.base)

        items_dropped = 0
        while stale_items:
            if (item := stale_items.pop()).hash != new_base:
                stale_items.extend(self.children(item))

            del self.queue[item]
            items_dropped += 1

        self.base = new_base
        return items_dropped
