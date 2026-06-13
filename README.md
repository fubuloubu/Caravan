# Overview

Caravan: A shared wallet for everyday adventures

## Design

Caravan is a simple, yet flexible, multi-owner smart contract wallet designed for high-value, day-to-day activities.
It is designed to support multiple users (up to 11) working together to co-sign and execute important transactions.

Out of the box, Caravan is designed to be used via [`CaravanProxy`](./contracts/CaravanProxy.vy),
which is a simple, upgradeable forwarding proxy that calls (via delegateproxy)
to a singleton deployment of [`Caravan`](./contracts/Caravan.vy).
It is only designed to be used via the Proxy, as it is intended to be a long-term stateful contract containing important assets
that we don't want to lose when upgrading to a newer version of the code.

The singleton deployment is deployed once per chain and is used as that chain's official copy of that particular version.
The versioning of this repo matches the versioning of the deployments on-chain, where the git tag should match 1:1 to
the value of `VERSION()` on the singleton (as well as your personal proxy).

To create a new Caravan, this project has a simplified factory [`CaravanFactory`](./contracts/CaravanFactory.vy)
intended to serve as the officially recommended way to create new instances of the proxy contract.
It also serves as the official registry of released versions, allowing users to discover new releases to upgrade to purely on-chain.
Proxy instances are deployed using [`CREATE2`](https://eips.ethereum.org/EIPS/eip-1014) with a salt chosen by the initial
set of signers, initial signer threshold, and a user-specifiable `tag` (which allows the creation of multiple wallets per combo).
This makes it possible to recreate the same Caravan on multiple chains, without worrying about having a specific nonce.

There are two types of EIP712 structures used within Caravan: `Update` and `Execute`.
They are designed to ensure that only important administrative updates occur via `Update`,
and common, non-admin transactions occur via `Execute`.
This is done to create a physical separation between critical, configuration-modifying transactions, and non-critical ones.
**It is highly recommended** to downstream signing infra that works w/ Caravan to create a clear UX distinction between
these two types of calls, making it clear that critical actions can impact the operational safety of a Caravan.

> [!NOTE]
> The way that signatures are collected to be placed on-chain is out of scope for this specification.

Additionally, both types of transactions have "Guards" which are 3rd party contracts that should implement pre- and post-execution
checks on their respective transaction types.
Use cases for Guards can include (for admin `Update`s) adding timelocked update restrictions, blacklisting certain addresses,
or (for normal `Execute`s) adding per diem limits on asset transfers, restricting calls to certain contracts, etc.
Having two separate Guards, one for each transaction type, is useful because an `Execute` Guard being non-functional does
not represent an existential threat to the operation of the wallet, only with an `Update` Guard.
**This should encourage the use of Guards for proper operation of the wallet in day-to-day operational scenarios**,
increasing overall safety when using Caravan.

Finally, Caravan implements "Modules" which are contracts that can be enabled in the wallet (through an `Update` action)
that are allowed to bypass the signer signature check when commiting arbitrary `Execute` transactions.
This functionality is extremely useful for adding automation to your day-to-day operations, making your operation of the
wallet safer and less prone to social engineering exploits.

> [!NOTE]
> Technically, while it is possible to use Caravan for a "personal" multisig (where you own all the signers on the wallet),
> it is suggested to make use of something like [`Purse`](https://github.com/fubuloubu/Purse) (with a secure cold wallet instead)
> to add automation and advanced capabilities to your personal, high-valued wallets.

---

Caravan is inspired by [Safe Smart Account](https://github.com/safe-global/safe-smart-account).

## Offline Queue Sync

Caravan can run an optional peer-to-peer sync daemon for its off-chain queue cache.
The daemon starts a libp2p node and exchanges queue data with peers over a direct stream protocol:

```text
/caravan/queue-sync/1.0.0
```

Each sync payload contains one or more cached queue domains, known off-chain queue items, known signatures, the sender peer ID, sync mode, optional subnet name, and optional advertised peer multiaddrs.
Received items are decoded through Caravan's existing `QueueItem.decode()` path, which validates message hashes and signatures before anything is merged locally.

The daemon does not use signer private keys, does not submit transactions, and does not require centralized infrastructure.
Queue payloads are plaintext within the trusted sync set in this version.

Queue storage is keyed by EIP-712 domain hash:

```text
~/.cache/caravan/
  <domain-hash>/
    domain.json
    <message-hash>/
      message.json
      signatures/
```

This lets one sync service handle multiple wallets and chains without the sync layer needing to understand chain providers.
Running `caravan queue sync WALLET ... --chain-id CHAIN_ID` derives domain subscriptions for the given wallets and watches those cache domains.
By default, wallet subscriptions use the latest packaged Caravan implementation version.
Running `caravan queue sync` with no wallets serves only the domain folders already present in the cache at startup.
The sync command does not connect to an Ape provider.

### Privacy Model

Sync is permissioned by default.
A node only sends queue state to trusted libp2p peer IDs and rejects unauthorized inbound sync streams before reading or writing queue payloads.

Use `--public` only for public boot/index nodes or open queue discovery.
Public mode allows any peer that speaks the protocol and shares a subscribed cache domain.
When packaged public boot peers are available, public mode uses them by default.
Configured subnet boot peers replace those public defaults completely.

Permissioned sync can be configured in `~/.config/caravan/sync.toml`:

```toml
[subnets.company]
public = false
boot_peers = [
  "/ip4/10.0.0.20/tcp/9876/p2p/16Uiu2HBootNode"
]
allowed_peers = [
  "16Uiu2HSignerA",
  "16Uiu2HSignerB",
  "16Uiu2HBootNode"
]
listen = ["/ip4/10.0.0.15/tcp/9876"]
advertise = ["/ip4/10.0.0.15/tcp/9876"]
```

CLI overrides:

```bash
caravan queue sync <wallet-address> \
  --chain-id 1 \
  --sync-config ~/.config/caravan/sync.toml \
  --subnet company \
  --allow-peer 16Uiu2HLocalSigner
```

Use `--chain-id` more than once to derive subscriptions for the same wallet addresses on multiple chains:

```bash
caravan queue sync 0xWalletA 0xWalletB --chain-id 1 --chain-id 8453
```

Use `--version` more than once to sync multiple Caravan implementation domain versions:

```bash
caravan queue sync 0xWalletA --chain-id 1 --version 1 --version 2
```

`--peer` tells the node where to dial and authorizes that peer ID for this local process.
Configured `boot_peers` are different: in permissioned mode, their peer IDs should also be listed in `allowed_peers` because they are part of the subnet's standing trust policy.
Trusted boot nodes are treated as sync indexers: they may receive, store, serve, and advertise queue payloads and known peer multiaddrs.
Nodes only dial advertised peers when public mode is enabled or the advertised peer ID is allowlisted.

For corporate LAN/VPN deployments, run a private boot/index node on RFC1918 or VPN addresses, put that boot node and signer nodes in `allowed_peers`, and use private `listen`/`advertise` addresses.
Do not use public boot peers in permissioned mode.
The boot node can be firewalled so it is unreachable from the public internet.

### Operating Modes

For local testing or personal single-computer use, sync is optional because the local queue cache is already shared by local commands.
The smoke test uses public mode only to exercise the transport.

For a small trusted group getting started, each signer can run:

```bash
caravan queue sync <wallet-address> --chain-id 1 --public
```

Public mode can use packaged public boot peers once configured.
The tradeoff is that queue payloads shared with public boot/index nodes should be considered visible to others.

For a trusted group with a private cloud boot node, run the same sync service with a stable identity and a permissioned subnet config.
Signer nodes use `caravan queue sync <wallet-address> --chain-id 1 --subnet group`.
The boot node can use `caravan queue sync --subnet group` after its cache has been seeded with the domains it should serve, or it can be started with the wallet addresses to derive those subscriptions.

For corporate deployments, use a private subnet config and corporate network controls so sync traffic stays within the organization.
Custom approval, audit, pruning, or policy logic should remain outside the transport layer and operate against the same cache layout.

### Install Sync Dependencies

```bash
uv sync --extra sync
```

or:

```bash
pip install -e ".[sync]"
```

### Local Smoke Test

Run the included two-node smoke test:

```bash
uv run --extra sync python scripts/test_sync.py
```

The script starts two local libp2p nodes.
Node A has one queue item, node B starts empty, node B connects to node A, and the script exits after node B receives the item.

Expected output includes:

```text
Connected to peer ...
Merged synced queue item ...
Synced 1 queue item(s)
```

### Manual CLI Test

Start the first node:

```bash
uv run --extra sync caravan queue sync <wallet-address> \
  --chain-id 1 \
  --listen /ip4/127.0.0.1/tcp/9876 \
  --allow-peer <peer-id-from-second-node> \
  --identity /tmp/caravan-node-a.key
```

Copy the printed `Listening:` multiaddr, then start the second node:

```bash
uv run --extra sync caravan queue sync <wallet-address> \
  --chain-id 1 \
  --listen /ip4/127.0.0.1/tcp/9877 \
  --identity /tmp/caravan-node-b.key \
  --peer /ip4/127.0.0.1/tcp/9876/p2p/<peer-id-from-first-node>
```

Passing `--peer` authorizes that peer for the local node, so the second command does not also need `--allow-peer <peer-id-from-first-node>`.
The first node still needs to trust the second node before it accepts inbound queue sync; for repeatable private testing, prefer a small `sync.toml` containing both peer IDs.
For quick local testing without an allowlist, add `--public` to both nodes.

Use separate identity files when running two nodes on one machine; otherwise both processes will share the same libp2p peer ID.

Sync is scoped by subscribed EIP-712 domain hashes, so peers only exchange queue domains they are configured to watch.
Direct streams are used for the first implementation because they are easier to reason about and verify locally than pubsub mesh timing.
IPFS content addressing and broader peer discovery can be layered on later once local and hosted daemon peering is stable.

## Contributing

This project is written in [Vyper](https://docs.vyperlang.org/en/stable).

This project uses [`ape`](https://apeworx.io/framework) to compile, test and script it.
See the [Installation Guide](https://docs.apeworx.io/ape/latest/userguides/quickstart#installation) for help installing it.
