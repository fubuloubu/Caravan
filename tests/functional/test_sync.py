import trio
from ape.types import AddressType, HexBytes
from eip712 import EIP712Domain

from caravan.messages.execute import Call, Execute
from caravan.queue import QueueItem, QueueManager
from caravan.sync import QueueCacheStore, SyncNode, SyncPolicy, load_sync_policy


BASE = HexBytes("0x" + "00" * 32)


def _item(signer=None) -> QueueItem:
    domain = EIP712Domain(
        name="Caravan Wallet",
        version="1",
        chainId=1,
        verifyingContract=AddressType("0x1234567890123456789012345678901234567890"),
    )
    message = Execute(
        parent=BASE,
        calls=[
            Call(
                target=AddressType("0x1111111111111111111111111111111111111111"),
                value=0,
                success_required=True,
                data=b"\x12\x34\x56\x78",
            )
        ],
        eip712_domain=domain,
    )
    signatures = {}
    if signer:
        signatures[signer.address] = signer.sign_message(message.signable_message)

    return QueueItem(message=message, signatures=signatures)


class ChunkedStream:
    def __init__(self, chunks: list[bytes] | None = None):
        self.chunks = chunks or []
        self.writes = []
        self.reads = 0
        self.closed = False

    async def read(self, size: int | None = None) -> bytes:
        self.reads += 1
        if not self.chunks:
            return b""

        chunk = self.chunks.pop(0)
        if size is not None and len(chunk) > size:
            self.chunks.insert(0, chunk[size:])
            return chunk[:size]

        return chunk

    async def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def close(self) -> None:
        self.closed = True


class PeerStream(ChunkedStream):
    def __init__(self, peer_id: str, chunks: list[bytes] | None = None):
        super().__init__(chunks)
        self.muxed_conn = type("MuxedConn", (), {"peer_id": peer_id})()


class FakeHost:
    async def new_stream(self, peer_id, protocols):
        raise AssertionError(f"Should not open a stream to {peer_id}")


def _store_for_queue(tmp_path, queue: QueueManager) -> QueueCacheStore:
    queue.save(tmp_path)
    return QueueCacheStore.from_existing_cache(tmp_path)


def _subscribed_store(tmp_path) -> QueueCacheStore:
    store = QueueCacheStore(tmp_path, domain_ids=set())
    store.ensure_domain(_domain_for_tests())
    return store


def _domain_for_tests() -> EIP712Domain:
    return EIP712Domain(
        name="Caravan Wallet",
        version="1",
        chainId=1,
        verifyingContract=AddressType("0x1234567890123456789012345678901234567890"),
    )


def test_sync_state_merges_missing_queue_item(tmp_path):
    source_queue = QueueManager(base=BASE, queue={})
    source_queue.add(_item())

    source = SyncNode(_store_for_queue(tmp_path / "source", source_queue))
    target_store = _subscribed_store(tmp_path / "target")
    target = SyncNode(target_store)

    assert target._merge_state(source._encode_state()) is True
    assert target_store.item_count == 1


def test_sync_state_ignores_unsubscribed_domain(tmp_path):
    source_queue = QueueManager(base=BASE, queue={})
    source_queue.add(_item())

    source = SyncNode(_store_for_queue(tmp_path / "source", source_queue))
    target_store = QueueCacheStore(tmp_path / "target", domain_ids=set())
    target = SyncNode(target_store)

    assert target._merge_state(source._encode_state()) is False
    assert target_store.item_count == 0


def test_sync_state_reads_current_cache_before_advertising(tmp_path):
    disk_queue = QueueManager(base=BASE, queue={})
    disk_queue.add(_item())
    disk_queue.save(tmp_path)

    node = SyncNode(QueueCacheStore(tmp_path))
    target_store = _subscribed_store(tmp_path / "target")

    assert SyncNode(target_store)._merge_state(node._encode_state())
    assert target_store.item_count == 1


def test_sync_state_encodes_signed_queue_item(accounts, tmp_path):
    source_queue = QueueManager(base=BASE, queue={})
    source_queue.add(_item(accounts[0]))

    source = SyncNode(_store_for_queue(tmp_path / "source", source_queue))
    target_store = _subscribed_store(tmp_path / "target")

    assert SyncNode(target_store)._merge_state(source._encode_state())
    loaded = QueueManager.load(BASE, tmp_path / "target")
    assert next(iter(loaded.queue)).confirmations == 1


def test_sync_stream_framing_reads_chunked_messages():
    async def run_test():
        node = SyncNode(QueueCacheStore())
        message = b'{"ok":true}'
        framed = len(message).to_bytes(4, "big") + message
        stream = ChunkedStream([framed[:1], framed[1:3], framed[3:7], framed[7:]])

        assert await node._read_message(stream) == message

    trio.run(run_test)


def test_sync_stream_framing_rejects_incomplete_messages():
    async def run_test():
        node = SyncNode(QueueCacheStore())
        stream = ChunkedStream([(10).to_bytes(4, "big"), b"short"])

        try:
            await node._read_message(stream)
        except EOFError:
            pass
        else:
            raise AssertionError("Expected incomplete sync message to raise EOFError")

    trio.run(run_test)


def test_sync_policy_fails_closed_without_public_or_allowlist(tmp_path):
    config_path = tmp_path / "missing.toml"

    try:
        load_sync_policy(config_path)
    except RuntimeError as err:
        assert "permissioned by default" in str(err)
    else:
        raise AssertionError("Expected sync policy to fail closed")


def test_sync_policy_loads_subnet_and_merges_cli_allowlist(tmp_path):
    config_path = tmp_path / "sync.toml"
    config_path.write_text(
        """
        [subnets.company]
        public = false
        boot_peers = ["/ip4/10.0.0.20/tcp/9876/p2p/16Uiu2HBoot"]
        allowed_peers = ["16Uiu2HSignerA", "16Uiu2HBoot"]
        listen = ["/ip4/10.0.0.15/tcp/9876"]
        advertise = ["/ip4/10.0.0.15/tcp/9876"]
        """
    )

    policy = load_sync_policy(
        config_path,
        subnet="company",
        allow_peers=("16Uiu2HSignerB",),
    )

    assert policy.public is False
    assert policy.subnet == "company"
    assert policy.boot_peers == ("/ip4/10.0.0.20/tcp/9876/p2p/16Uiu2HBoot",)
    assert policy.listen_addrs == ("/ip4/10.0.0.15/tcp/9876",)
    assert policy.advertise_addrs == ("/ip4/10.0.0.15/tcp/9876",)
    assert policy.allowed_peers == frozenset(
        {"16Uiu2HSignerA", "16Uiu2HSignerB", "16Uiu2HBoot"}
    )


def test_sync_policy_authorizes_explicit_cli_peer_once(tmp_path):
    policy = load_sync_policy(
        tmp_path / "missing.toml",
        peer_addrs=("/ip4/127.0.0.1/tcp/9876/p2p/16Uiu2HDirect",),
    )

    assert policy.public is False
    assert policy.boot_peers == ()
    assert policy.allowed_peers == frozenset({"16Uiu2HDirect"})


def test_sync_policy_uses_public_boot_defaults_only_in_public_mode(tmp_path):
    policy = load_sync_policy(
        tmp_path / "missing.toml",
        public=True,
        default_public_boot_peers=(
            "/dns4/bootstrap.caravan.box/tcp/9876/p2p/16Uiu2HPublic",
        ),
    )

    assert policy.public is True
    assert policy.boot_peers == (
        "/dns4/bootstrap.caravan.box/tcp/9876/p2p/16Uiu2HPublic",
    )


def test_sync_policy_configured_subnet_replaces_public_boot_defaults(tmp_path):
    config_path = tmp_path / "sync.toml"
    config_path.write_text(
        """
        [subnets.company]
        public = false
        boot_peers = ["/ip4/10.0.0.20/tcp/9876/p2p/16Uiu2HPrivateBoot"]
        allowed_peers = ["16Uiu2HPrivateBoot"]
        """
    )

    policy = load_sync_policy(
        config_path,
        subnet="company",
        public=True,
        default_public_boot_peers=(
            "/dns4/bootstrap.caravan.box/tcp/9876/p2p/16Uiu2HPublic",
        ),
    )

    assert policy.public is True
    assert policy.boot_peers == ("/ip4/10.0.0.20/tcp/9876/p2p/16Uiu2HPrivateBoot",)


def test_public_policy_permits_non_allowlisted_peer():
    policy = SyncPolicy(public=True)

    assert policy.allows("16Uiu2HUnknown")


def test_permissioned_node_rejects_inbound_before_reading_payload(tmp_path):
    async def run_test():
        node = SyncNode(
            QueueCacheStore(tmp_path),
            policy=SyncPolicy(allowed_peers=frozenset({"16Uiu2HAllowed"})),
        )
        stream = PeerStream("16Uiu2HUnauthorized", [b"not-read"])

        await node._stream_handler(stream)

        assert stream.reads == 0
        assert stream.writes == []
        assert stream.closed is True

    trio.run(run_test)


def test_permissioned_node_refuses_outbound_sync_to_non_allowlisted_peer(tmp_path):
    async def run_test():
        node = SyncNode(
            QueueCacheStore(tmp_path),
            policy=SyncPolicy(allowed_peers=frozenset({"16Uiu2HAllowed"})),
        )
        node.host = FakeHost()

        await node._sync_with_peer("16Uiu2HUnauthorized")

    trio.run(run_test)


def test_advertised_peer_addr_is_not_authorized_without_allowlist(tmp_path):
    node = SyncNode(
        QueueCacheStore(tmp_path),
        policy=SyncPolicy(allowed_peers=frozenset({"16Uiu2HAllowed"})),
    )

    assert node._filter_peer_addrs(
        [
            "/ip4/127.0.0.1/tcp/9876/p2p/16Uiu2HUnauthorized",
            "/ip4/127.0.0.1/tcp/9877/p2p/16Uiu2HAllowed",
        ]
    ) == ["/ip4/127.0.0.1/tcp/9877/p2p/16Uiu2HAllowed"]


def test_boot_index_response_only_advertises_allowed_peers(tmp_path):
    node = SyncNode(
        QueueCacheStore(tmp_path),
        policy=SyncPolicy(allowed_peers=frozenset({"16Uiu2HAllowed"})),
    )
    node._known_peer_addrs.update(
        {
            "/ip4/10.0.0.20/tcp/9876/p2p/16Uiu2HAllowed",
            "/ip4/10.0.0.21/tcp/9876/p2p/16Uiu2HUnauthorized",
        }
    )

    payload = node._encode_state()
    advertised = node._advertised_peers(payload)

    assert advertised == ["/ip4/10.0.0.20/tcp/9876/p2p/16Uiu2HAllowed"]


def test_public_node_dials_advertised_peers_with_peer_ids(tmp_path):
    node = SyncNode(
        QueueCacheStore(tmp_path),
        policy=SyncPolicy(public=True),
    )

    assert node._filter_peer_addrs(
        [
            "/ip4/127.0.0.1/tcp/9876/p2p/16Uiu2HAny",
            "/ip4/127.0.0.1/tcp/9877",
        ]
    ) == ["/ip4/127.0.0.1/tcp/9876/p2p/16Uiu2HAny"]
