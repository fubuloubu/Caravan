"""P2P synchronization for Caravan off-chain queues.

The sync daemon exchanges public queue data only: EIP-712 messages and their
signatures. It never handles wallet private keys.
"""

from __future__ import annotations

import json
import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import trio
from ape.types import HexBytes
from eip712 import EIP712Domain

from .queue import QueueItem
from .settings import USER_CACHE_DIR, USER_CONFIG_DIR

if TYPE_CHECKING:
    from libp2p.crypto.keys import KeyPair
    from libp2p.host.host_interface import IHost
    from libp2p.network.stream.net_stream import INetStream
    from libp2p.peer.id import ID
    from multiaddr import Multiaddr

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
PROTOCOL_ID = "/caravan/queue-sync/1.0.0"
DEFAULT_LISTEN_ADDR = "/ip4/0.0.0.0/tcp/0"
DEFAULT_PUBLIC_BOOT_PEERS: tuple[str, ...] = ()
DEFAULT_SYNC_CONFIG = USER_CONFIG_DIR / "sync.toml"
DEFAULT_SYNC_INTERVAL = 10.0
MAX_SYNC_MESSAGE_SIZE = 16 * 1024 * 1024
SYNC_LENGTH_PREFIX_SIZE = 4


class SyncDependencyError(RuntimeError):
    """Raised when the optional sync dependencies are not installed."""


@dataclass(frozen=True)
class SyncPolicy:
    """Peer privacy policy for queue sync."""

    public: bool = False
    allowed_peers: frozenset[str] = frozenset()
    subnet: str | None = None
    boot_peers: tuple[str, ...] = ()
    listen_addrs: tuple[str, ...] = ()
    advertise_addrs: tuple[str, ...] = ()

    @property
    def mode(self) -> str:
        return "public" if self.public else "permissioned"

    @property
    def is_configured(self) -> bool:
        return self.public or bool(self.allowed_peers)

    def require_configured(self) -> None:
        if not self.is_configured:
            raise RuntimeError(
                "Queue sync is permissioned by default. Configure allowed peers "
                "with --allow-peer or sync.toml, or explicitly enable --public."
            )

    def allows(self, peer_id: Any) -> bool:
        return self.public or str(peer_id) in self.allowed_peers

    def merge(
        self,
        *,
        public: bool = False,
        allowed_peers: list[str] | tuple[str, ...] = (),
    ) -> "SyncPolicy":
        return SyncPolicy(
            public=self.public or public,
            allowed_peers=frozenset((*self.allowed_peers, *map(str, allowed_peers))),
            subnet=self.subnet,
            boot_peers=self.boot_peers,
            listen_addrs=self.listen_addrs,
            advertise_addrs=self.advertise_addrs,
        )


def load_sync_policy(
    config_path: Path | None = None,
    *,
    subnet: str | None = None,
    allow_peers: list[str] | tuple[str, ...] = (),
    peer_addrs: list[str] | tuple[str, ...] = (),
    public: bool = False,
    default_public_boot_peers: tuple[str, ...] = DEFAULT_PUBLIC_BOOT_PEERS,
) -> SyncPolicy:
    """Load sync policy from TOML and apply CLI overrides."""

    config_path = config_path or DEFAULT_SYNC_CONFIG
    configured: dict[str, Any] = {}
    configured_boot_peers: tuple[str, ...] = ()
    selected_subnet = subnet
    if config_path.exists():
        raw_config = tomllib.loads(config_path.read_text())
        subnets = raw_config.get("subnets", {})
        if selected_subnet is None and len(subnets) == 1:
            selected_subnet = next(iter(subnets))
        if selected_subnet:
            try:
                configured = subnets[selected_subnet]
            except KeyError as err:
                raise RuntimeError(
                    f"Sync subnet '{selected_subnet}' not found in {config_path}."
                ) from err

    configured_boot_peers = tuple(map(str, configured.get("boot_peers", ())))
    cli_peer_ids = tuple(
        peer_id
        for peer_addr in peer_addrs
        if (peer_id := peer_id_from_multiaddr(peer_addr))
    )
    boot_peers = (
        configured_boot_peers
        if configured_boot_peers or selected_subnet
        else tuple(default_public_boot_peers if public else ())
    )
    policy = SyncPolicy(
        public=bool(configured.get("public", False)),
        allowed_peers=frozenset(map(str, configured.get("allowed_peers", ()))),
        subnet=selected_subnet,
        boot_peers=boot_peers,
        listen_addrs=tuple(map(str, configured.get("listen", ()))),
        advertise_addrs=tuple(map(str, configured.get("advertise", ()))),
    ).merge(public=public, allowed_peers=(*allow_peers, *cli_peer_ids))
    policy.require_configured()
    return policy


def peer_id_from_multiaddr(addr: str) -> str | None:
    parts = addr.split("/")
    try:
        p2p_index = parts.index("p2p")
        return parts[p2p_index + 1]
    except (ValueError, IndexError):
        return None


def _load_sync_dependencies():
    try:
        from libp2p import new_host
        from libp2p.crypto.keys import KeyPair
        from libp2p.crypto.secp256k1 import Secp256k1PrivateKey, create_new_key_pair
        from libp2p.custom_types import TProtocol
        from multiaddr import Multiaddr
    except ImportError as err:
        raise SyncDependencyError(
            "Queue sync requires optional dependencies. Install with "
            "`pip install 'caravan-py[sync]'` or run with `uv run --extra sync`."
        ) from err

    return {
        "new_host": new_host,
        "KeyPair": KeyPair,
        "Secp256k1PrivateKey": Secp256k1PrivateKey,
        "create_new_key_pair": create_new_key_pair,
        "TProtocol": TProtocol,
        "Multiaddr": Multiaddr,
    }


def _load_or_create_identity(identity_file: Path | None = None) -> "KeyPair":
    deps = _load_sync_dependencies()
    identity_file = identity_file or USER_CONFIG_DIR / "sync-node.key"
    identity_file.parent.mkdir(parents=True, exist_ok=True)

    if identity_file.exists():
        private_key = deps["Secp256k1PrivateKey"].from_bytes(identity_file.read_bytes())
        return deps["KeyPair"](private_key, private_key.get_public_key())

    keypair = deps["create_new_key_pair"]()
    identity_file.write_bytes(keypair.private_key.to_bytes())
    identity_file.chmod(0o600)
    return keypair


def _domain_for_item(item: QueueItem) -> EIP712Domain:
    domain = item.message._eip712_domain_
    if not isinstance(domain, EIP712Domain):
        raise RuntimeError(f"Queue item {item} is missing its EIP-712 domain")

    return domain


def domain_id(domain: EIP712Domain) -> str:
    return HexBytes(domain.separator).hex()


class QueueCacheStore:
    """Disk-backed sync store keyed by EIP-712 domain hash."""

    def __init__(
        self,
        path: Path = USER_CACHE_DIR,
        *,
        domain_ids: set[str] | frozenset[str] | None = None,
    ):
        self.path = path
        self.path.mkdir(parents=True, exist_ok=True)
        self.domain_ids = set(domain_ids) if domain_ids is not None else None

    @classmethod
    def from_existing_cache(cls, path: Path = USER_CACHE_DIR) -> "QueueCacheStore":
        path.mkdir(parents=True, exist_ok=True)
        return cls(path, domain_ids=cls.existing_domain_ids(path))

    @staticmethod
    def existing_domain_ids(path: Path = USER_CACHE_DIR) -> set[str]:
        if not path.exists():
            return set()

        return {
            folder.name
            for folder in path.iterdir()
            if folder.is_dir() and (folder / "domain.json").exists()
        }

    @property
    def is_filtered(self) -> bool:
        return self.domain_ids is not None

    def ensure_domain(self, domain: EIP712Domain) -> str:
        item_domain_id = domain_id(domain)
        domain_folder = self.path / item_domain_id
        domain_folder.mkdir(parents=True, exist_ok=True)
        domain_file = domain_folder / "domain.json"
        if not domain_file.exists():
            domain_file.write_text(domain.model_dump_json(exclude_none=True))

        if self.domain_ids is not None:
            self.domain_ids.add(item_domain_id)

        return item_domain_id

    def iter_queues(self) -> list[dict[str, Any]]:
        queues = []
        for domain_folder in self._domain_folders():
            try:
                domain = EIP712Domain.model_validate_json(
                    (domain_folder / "domain.json").read_text()
                )
            except Exception:
                logger.exception("Ignoring invalid sync domain %s", domain_folder.name)
                continue

            items = []
            for item_folder in domain_folder.iterdir():
                if not item_folder.is_dir():
                    continue

                try:
                    item = QueueItem.load(item_folder, domain)
                except Exception:
                    logger.exception("Ignoring invalid sync item %s", item_folder)
                    continue

                items.append(_encode_item(item))

            if items:
                queues.append(
                    {
                        "domain_id": domain_folder.name,
                        "domain": domain.model_dump(mode="json", exclude_none=True),
                        "items": items,
                    }
                )

        return queues

    def merge_queue(self, payload: dict[str, Any]) -> bool:
        try:
            domain = EIP712Domain.model_validate(payload["domain"])
        except Exception:
            logger.exception("Ignoring sync queue with invalid domain")
            return False

        item_domain_id = domain_id(domain)
        if payload.get("domain_id") != item_domain_id:
            logger.warning("Ignoring sync queue with mismatched domain_id")
            return False

        if self.domain_ids is not None and item_domain_id not in self.domain_ids:
            logger.debug("Ignoring sync queue outside local subscriptions")
            return False

        self.ensure_domain(domain)
        changed = False
        for item_data in payload.get("items", []):
            changed |= self.merge_item(item_domain_id, domain, item_data)

        return changed

    def merge_item(
        self, item_domain_id: str, domain: EIP712Domain, data: dict[str, Any]
    ) -> bool:
        item_hash = data.get("hash")
        if not item_hash:
            logger.debug("Ignoring sync item without hash")
            return False

        try:
            item = QueueItem.decode(data["item"], domain)
        except Exception:
            logger.exception("Ignoring invalid sync item %s", item_hash)
            return False

        if item.hash.hex() != HexBytes(item_hash).hex():
            logger.warning("Ignoring sync item with mismatched hash %s", item_hash)
            return False

        item_folder = self.path / item_domain_id / item.hash.hex()
        if item_folder.exists():
            try:
                existing = QueueItem.load(item_folder, domain)
            except Exception:
                logger.exception("Ignoring corrupted cached sync item %s", item_folder)
                return False

            new_signatures = {
                signer: signature
                for signer, signature in item.signatures.items()
                if signer not in existing.signatures
            }
            if not new_signatures:
                return False

            existing.signatures.update(new_signatures)
            item = existing

        item_folder.mkdir(parents=True, exist_ok=True)
        item.save(item_folder)
        logger.info("Merged synced queue item %s", item)
        return True

    @property
    def queue_count(self) -> int:
        return len(self._domain_folders())

    @property
    def item_count(self) -> int:
        count = 0
        for domain_folder in self._domain_folders():
            count += len([path for path in domain_folder.iterdir() if path.is_dir()])

        return count

    def _domain_folders(self) -> list[Path]:
        domain_ids = (
            self.existing_domain_ids(self.path)
            if self.domain_ids is None
            else self.domain_ids
        )
        return [
            self.path / item_domain_id
            for item_domain_id in sorted(domain_ids)
            if (self.path / item_domain_id / "domain.json").exists()
        ]


def _encode_item(item: QueueItem) -> dict[str, Any]:
    _domain_for_item(item)
    return {
        "hash": item.hash.hex(),
        "item": item.model_dump_json(),
    }


class SyncNode:
    """A libp2p node that synchronizes cached Caravan queues."""

    def __init__(
        self,
        store: QueueCacheStore,
        *,
        identity_file: Path | None = None,
        sync_interval: float = DEFAULT_SYNC_INTERVAL,
        policy: SyncPolicy | None = None,
    ):
        self.store = store
        self.identity_file = identity_file
        self.sync_interval = sync_interval
        self.policy = policy or SyncPolicy()

        self.host: "IHost | None" = None
        self._running = False
        self._peer_ids: set["ID"] = set()
        self._known_peer_addrs: set[str] = set()

    @property
    def peer_id(self) -> str:
        if not self.host:
            raise RuntimeError("Sync node has not started")

        return str(self.host.get_id())

    @property
    def multiaddrs(self) -> list[str]:
        if not self.host:
            raise RuntimeError("Sync node has not started")

        return [str(addr) for addr in self.host.get_addrs()]

    @property
    def peer_multiaddrs(self) -> list[str]:
        return [self._with_peer_id(addr) for addr in self.multiaddrs]

    @property
    def peer_count(self) -> int:
        if not self.host:
            return 0

        return len(self.host.get_network().connections)

    async def start(
        self,
        *,
        listen_addrs: list[str] | None = None,
        bootstrap_peers: list[str] | None = None,
        nursery: trio.Nursery,
    ) -> None:
        """Start the node and schedule sync tasks in ``nursery``."""

        if self._running:
            raise RuntimeError("Sync node is already running")

        self.policy.require_configured()
        deps = _load_sync_dependencies()
        listen_multiaddrs = [
            deps["Multiaddr"](addr)
            for addr in (
                listen_addrs or list(self.policy.listen_addrs) or [DEFAULT_LISTEN_ADDR]
            )
        ]

        self.host = deps["new_host"](
            key_pair=_load_or_create_identity(self.identity_file)
        )
        self.host.set_stream_handler(
            deps["TProtocol"](PROTOCOL_ID), self._stream_handler
        )
        self._running = True

        host_started = trio.Event()
        nursery.start_soon(self._run_host, listen_multiaddrs, host_started)
        await host_started.wait()

        configured_peers = list(bootstrap_peers or []) + list(self.policy.boot_peers)
        if configured_peers:
            await self._connect_to_peers(configured_peers)

        nursery.start_soon(self._sync_loop)
        await self.sync_once()

    async def _run_host(
        self, listen_multiaddrs: list["Multiaddr"], started: trio.Event
    ) -> None:
        if not self.host:
            return

        async with self.host.run(listen_addrs=listen_multiaddrs):
            started.set()
            while self._running:
                await trio.sleep(0.2)

    async def _connect_to_peers(self, peer_addrs: list[str]) -> None:
        if not self.host:
            return

        deps = _load_sync_dependencies()
        from libp2p.peer.peerinfo import info_from_p2p_addr

        for peer_addr in peer_addrs:
            try:
                peer_info = info_from_p2p_addr(deps["Multiaddr"](peer_addr))
                if not self.policy.allows(peer_info.peer_id):
                    logger.warning(
                        "Refusing to dial unauthorized peer %s", peer_info.peer_id
                    )
                    continue

                await self.host.connect(peer_info)
                self._peer_ids.add(peer_info.peer_id)
                self._known_peer_addrs.add(peer_addr)
                logger.info("Connected to peer %s", peer_info.peer_id)
            except Exception:
                logger.exception("Failed to connect to peer %s", peer_addr)

    async def _sync_loop(self) -> None:
        while self._running:
            await trio.sleep(self.sync_interval)
            await self.sync_once()

    async def sync_once(self) -> None:
        for peer_id in list(self._peer_ids):
            try:
                await self._sync_with_peer(peer_id)
            except Exception:
                logger.exception("Failed to sync with peer %s", peer_id)

    async def _sync_with_peer(self, peer_id: "ID") -> None:
        if not self.host:
            return

        if not self.policy.allows(peer_id):
            logger.warning("Refusing outbound sync to unauthorized peer %s", peer_id)
            return

        deps = _load_sync_dependencies()
        stream = await self.host.new_stream(peer_id, [deps["TProtocol"](PROTOCOL_ID)])
        try:
            await self._write_message(stream, self._encode_state())
            response = await self._read_message(stream)
            if response:
                self._merge_state(response)
                await self._connect_to_peers(self._advertised_peers(response))
        except EOFError as err:
            logger.debug("Peer %s closed sync stream early: %s", peer_id, err)
        finally:
            await stream.close()

    async def _stream_handler(self, stream: "INetStream") -> None:
        try:
            peer_id = stream.muxed_conn.peer_id
            if not self.policy.allows(peer_id):
                logger.warning(
                    "Rejecting inbound sync from unauthorized peer %s", peer_id
                )
                return

            self._peer_ids.add(peer_id)
            request = await self._read_message(stream)
            if request:
                self._merge_state(request)

            await self._write_message(stream, self._encode_state())
        except EOFError as err:
            logger.debug("Peer closed inbound sync stream early: %s", err)
        except Exception:
            logger.exception("Failed to handle inbound sync stream")
        finally:
            await stream.close()

    async def _read_message(self, stream: "INetStream") -> bytes:
        header = await self._read_exact(stream, SYNC_LENGTH_PREFIX_SIZE)
        message_size = int.from_bytes(header, "big")
        if message_size > MAX_SYNC_MESSAGE_SIZE:
            raise RuntimeError(
                f"Sync message exceeds {MAX_SYNC_MESSAGE_SIZE} byte limit: "
                f"{message_size}"
            )

        return await self._read_exact(stream, message_size)

    async def _read_exact(self, stream: "INetStream", size: int) -> bytes:
        chunks = []
        remaining = size
        while remaining:
            chunk = await stream.read(remaining)
            if not chunk:
                raise EOFError(
                    f"Peer closed stream before sending full sync message; "
                    f"missing {remaining} byte(s)."
                )

            chunks.append(chunk)
            remaining -= len(chunk)

        return b"".join(chunks)

    async def _write_message(self, stream: "INetStream", message: bytes) -> None:
        if len(message) > MAX_SYNC_MESSAGE_SIZE:
            raise RuntimeError(
                f"Sync message exceeds {MAX_SYNC_MESSAGE_SIZE} byte limit: "
                f"{len(message)}"
            )

        await stream.write(len(message).to_bytes(SYNC_LENGTH_PREFIX_SIZE, "big"))
        await stream.write(message)

    def _encode_state(self) -> bytes:
        payload = {
            "version": PROTOCOL_VERSION,
            "mode": self.policy.mode,
            "subnet": self.policy.subnet,
            "sender_peer_id": self.peer_id if self.host else None,
            "queues": self.store.iter_queues(),
            "peers": self._advertised_multiaddrs(),
        }
        return json.dumps(payload, separators=(",", ":")).encode("utf-8")

    def _advertised_peers(self, raw_message: bytes) -> list[str]:
        try:
            payload = json.loads(raw_message.decode("utf-8"))
        except ValueError:
            return []

        return self._filter_peer_addrs(payload.get("peers", []))

    def _advertised_multiaddrs(self) -> list[str]:
        addrs = set(self._known_peer_addrs)
        if self.host:
            addrs.update(
                self._with_peer_id(addr) for addr in self.policy.advertise_addrs
            )
            addrs.update(self.peer_multiaddrs)

        return sorted(self._filter_peer_addrs(addrs))

    def _filter_peer_addrs(self, peer_addrs: Any) -> list[str]:
        if not isinstance(peer_addrs, (list, tuple, set)):
            return []

        filtered = []
        for peer_addr in peer_addrs:
            if not isinstance(peer_addr, str):
                continue

            peer_id = peer_id_from_multiaddr(peer_addr)
            if peer_id and self.policy.allows(peer_id):
                filtered.append(peer_addr)

        return filtered

    def _merge_state(self, raw_message: bytes) -> bool:
        payload = json.loads(raw_message.decode("utf-8"))
        if payload.get("version") != PROTOCOL_VERSION:
            logger.debug("Ignoring unsupported sync protocol message: %s", payload)
            return False

        changed = False
        for queue_data in payload.get("queues", []):
            changed |= self.store.merge_queue(queue_data)

        return changed

    async def stop(self) -> None:
        self._running = False
        if self.host:
            await self.host.close()

    def _with_peer_id(self, addr: str) -> str:
        if "/p2p/" in addr:
            return addr

        return f"{addr}/p2p/{self.peer_id}"
