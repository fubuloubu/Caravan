import json
import math
from typing import TYPE_CHECKING
import runpy

from ape.exceptions import AccountsError, ConversionError
import click
from ape.api.address import Address
from ape.cli import (
    ConnectedProviderCommand,
    account_option,
    ape_cli_context,
    network_option,
)
from ape.types import AddressType, HexBytes
from packaging.version import Version


from .cli import version_option, caravan_argument, parent_option
from .factory import Factory
from .packages import PackageType
from .settings import (
    USER_CONFIG_DIR,
    FACTORY_DETERMINISTIC_ADDRESS,
    SINGLETON_DETERMINISTIC_ADDRESSES,
)

if TYPE_CHECKING:
    from ape.api.accounts import AccountAPI
    from ape.api.networks import NetworkAPI

    from .main import Caravan


@click.group()
def cli():
    """Manage Caravan wallets (https://caravan.box)"""


# TODO: Add to ape?
def _get_accounts(ctx, param, values):
    from ape import accounts, convert

    def get_account(value):
        if value in accounts.aliases:
            return accounts.load(value)

        elif value.startswith("TEST::"):
            return accounts.test_accounts[int(value[6:])]

        try:
            return convert(value, AddressType)

        except ConversionError as e:
            raise click.UsageError(
                ctx=ctx,
                message=f"Not a valid '{param.name}': {value}",
            ) from e

    return [get_account(v) for v in values]


@cli.command(name="new", cls=ConnectedProviderCommand)
@account_option()
@version_option()
@click.option(
    "--threshold",
    type=int,
    default=0,
    help="The value to use for the wallet's threshold. "
    "Defaults to half the number of signers (rounding up)",
)
@click.option("--tag", default=None)
@click.argument("signers", nargs=-1, callback=_get_accounts)
def new_wallet(network, version, threshold, tag, signers, account):
    """Create a new Wallet on the given network"""

    if len(signers) == 0:
        raise click.UsageError("Must provider at least one signer")

    elif len(signers) > 11:
        raise click.UsageError("Cannot create Wallet with more than 11 signers")

    if not threshold:
        threshold = math.ceil(len(signers) / 2)

    elif 0 > threshold or threshold > len(signers):
        raise click.UsageError("Cannot use a value higher than number of signers")

    factory = Factory()
    van = factory.new(
        signers,
        threshold=threshold,
        version=version,
        tag=tag,
        sender=account,
    )
    click.secho(f"CaravanProxy deployed: {van.address}", fg="green")

    if network.is_dev:
        return  # NOTE: Do not track emphemeral wallets

    elif (wallet_file := USER_CONFIG_DIR / f"{van.address}.json").exists():
        chain_ids = json.loads(wallet_file.read_text())
        chain_ids.append(network.chain_id)
        wallet_file.write_text(json.dumps(sorted(chain_ids)))
    else:
        wallet_file.write_text(json.dumps([network.chain_id]))


@cli.command(name="track")
@click.argument("address")
@click.argument("chain_ids", type=int, nargs=-1)
def track_wallet(address: AddressType, chain_ids: list[int]):
    """Track existing Wallet by ADDRESS"""

    if (wallet_file := USER_CONFIG_DIR / f"{address}.json").exists():
        raise click.UsageError("Cannot overwrite existing tracked wallet")

    elif not chain_ids:
        raise click.UsageError("Include at least 1 chain ID")

    wallet_file.write_text(json.dumps(chain_ids))


@cli.command(name="list")
@network_option(default=None)
def list_wallets(network):
    """List locally-tracked Wallets"""

    wallets_found = False
    for wallet_file in USER_CONFIG_DIR.glob("*.json"):
        chain_ids = json.loads(wallet_file.read_text())
        if network is None:
            click.echo(wallet_file.stem)
            wallets_found = True
        elif network.chain_id in chain_ids:
            click.echo(wallet_file.stem)
            wallets_found = True

    if not wallets_found:
        if network is None:
            click.secho("No wallets being tracked!", fg="red")

        else:
            network_str = f"{network.ecosystem.name}:{network.name}"
            click.secho(f"No wallets being tracked on '{network_str}'!", fg="red")


@cli.command(name="unlink")
@click.argument("address")
def unlink_wallet(address: AddressType):
    """Stop tracking wallet ADDRESS"""

    if not (wallet_file := USER_CONFIG_DIR / f"{address}.json").exists():
        raise click.UsageError("Cannot remove un-tracked wallet")

    elif click.confirm(f"Stop tracking {address}?"):
        wallet_file.unlink()


@cli.group()
def config():
    """Commands to modify on-chain configuration"""


@config.command(cls=ConnectedProviderCommand)
@version_option()
@parent_option()
@caravan_argument()
def migrate(version: Version, parent: HexBytes | None, caravan: "Caravan"):
    """Migrate the version of your Wallet"""

    if (current_version := caravan.version) == version:
        raise click.UsageError("Cannot migrate to the same version")

    if click.confirm(f"Migrate from {current_version} to {version}?"):
        item = caravan.migrate(new_version=version, parent=parent)
        click.echo(
            click.style("SUCCESS: ", fg="green") + f"proposed '{item.hash.hex()}'."
        )


@config.command(cls=ConnectedProviderCommand)
@click.option(
    "--add",
    "signers_to_add",
    multiple=True,
    callback=_get_accounts,
    help="Add signers to Wallet",
)
@click.option(
    "--remove",
    "signers_to_remove",
    multiple=True,
    callback=_get_accounts,
    help="Remove signers from Wallet",
)
@click.option("--threshold", type=int, default=None, help="Change signing threshold")
@parent_option()
@caravan_argument()
def signers(
    signers_to_add: list[str],
    signers_to_remove: list[str],
    threshold: int | None,
    parent: HexBytes | None,
    caravan: "Caravan",
):
    """Rotate Wallet signers and/or change Wallet threshold"""

    if not signers_to_add and not signers_to_remove and not threshold:
        raise click.UsageError("No modifications detected")

    click.echo("Current signers:\n- " + "\n- ".join(caravan.signers))

    if signers_to_remove:
        click.echo("Remove:\n- " + "\n- ".join(signers_to_remove))

    if signers_to_add:
        click.echo("Add:\n- " + "\n- ".join(signers_to_add))

    if threshold:
        click.echo(f"Modify threshold from {caravan.threshold} to {threshold}")

    if click.confirm("Proceed?"):
        item = caravan.rotate_signers(
            signers_to_add=signers_to_add,
            signers_to_remove=signers_to_remove,
            threshold=threshold,
            parent=parent,
        )
        click.echo(
            click.style("SUCCESS: ", fg="green") + f"proposed '{item.hash.hex()}'."
        )


@config.group()
def guards():
    """View and configure Guards in Wallet"""


@guards.group(name="admin")
def admin_guard():
    """View and Configure the Admin Guard in Wallet"""


@admin_guard.command(name="view", cls=ConnectedProviderCommand)
@caravan_argument()
def view_admin_guard(caravan: "Caravan"):
    """Show Admin Guard in Wallet (if any)"""

    if admin_guard := caravan.admin_guard:
        click.echo(str(admin_guard))


@admin_guard.command(name="set", cls=ConnectedProviderCommand)
@parent_option()
@caravan_argument()
@click.argument("new_guard")
def set_admin_guard(
    parent: HexBytes | None, caravan: "Caravan", new_guard: AddressType
):
    """Set Admin Guard in Wallet"""

    if admin_guard := caravan.admin_guard:
        click.echo(f"Old: {admin_guard}")

    click.echo(f"New: {new_guard}")
    if click.confirm("Proceed?"):
        item = caravan.set_admin_guard(new_guard, parent=parent)
        click.echo(
            click.style("SUCCESS: ", fg="green") + f"proposed '{item.hash.hex()}'."
        )


@admin_guard.command(name="rm", cls=ConnectedProviderCommand)
@parent_option()
@caravan_argument()
def remove_admin_guard(parent: HexBytes | None, caravan: "Caravan"):
    """Remove Admin Guard in Wallet"""

    if admin_guard := caravan.admin_guard:
        click.echo(f"Old: {admin_guard}")

    else:
        raise click.UsageError("No op")

    if click.confirm("Proceed?"):
        item = caravan.set_admin_guard(parent=parent)
        click.echo(
            click.style("SUCCESS: ", fg="green") + f"proposed '{item.hash.hex()}'."
        )


@guards.group(name="execute")
def execute_guard():
    """View and configure the Execute Guard in Wallet"""


@execute_guard.command(name="view", cls=ConnectedProviderCommand)
@caravan_argument()
def view_execute_guard(caravan: "Caravan"):
    """Show Execute Guard in Wallet (if any)"""

    if execute_guard := caravan.execute_guard:
        click.echo(str(execute_guard))


@execute_guard.command(name="set", cls=ConnectedProviderCommand)
@parent_option()
@caravan_argument()
@click.argument("new_guard")
def set_execute_guard(
    parent: HexBytes | None, caravan: "Caravan", new_guard: AddressType
):
    """Set Admin Guard in Wallet"""

    if execute_guard := caravan.execute_guard:
        click.echo(f"Old: {execute_guard}")

    click.echo(f"New: {new_guard}")
    if click.confirm("Proceed?"):
        item = caravan.set_execute_guard(new_guard, parent=parent)
        click.echo(
            click.style("SUCCESS: ", fg="green") + f"proposed '{item.hash.hex()}'."
        )


@execute_guard.command(name="rm", cls=ConnectedProviderCommand)
@parent_option()
@caravan_argument()
def remove_execute_guard(parent: HexBytes | None, caravan: "Caravan"):
    """Remove Admin Guard in Wallet"""

    if execute_guard := caravan.execute_guard:
        click.echo(f"Old: {execute_guard}")

    else:
        raise click.UsageError("No op")

    if click.confirm("Proceed?"):
        item = caravan.set_execute_guard(parent=parent)
        click.echo(
            click.style("SUCCESS: ", fg="green") + f"proposed '{item.hash.hex()}'."
        )


@config.group()
def modules():
    """View and configure Modules in a Wallet"""


@modules.command(name="list", cls=ConnectedProviderCommand)
@caravan_argument()
def list_modules(caravan: "Caravan"):
    """List Modules in Wallet (if any)"""

    for module in caravan.modules:
        click.echo(str(module))


@modules.command(name="enable", cls=ConnectedProviderCommand)
@account_option("--submitter")
@caravan_argument()
@click.argument("module")
def enable_module(parent: HexBytes | None, caravan: "Caravan", module: AddressType):
    """Enable Module in Wallet"""

    if module in caravan.modules:
        raise click.UsageError(f"Module {module} already enabled")

    elif click.confirm(f"Enable module {module}?"):
        item = caravan.modules.enable(module, parent=parent)
        click.echo(
            click.style("SUCCESS: ", fg="green") + f"proposed '{item.hash.hex()}'."
        )


@modules.command(name="disable", cls=ConnectedProviderCommand)
@parent_option()
@caravan_argument()
@click.argument("module")
def disable_module(parent: HexBytes | None, caravan: "Caravan", module: AddressType):
    """Disable Module in Wallet"""

    if module not in caravan.modules:
        raise click.UsageError(f"Module {module} is not enabled")

    elif click.confirm(f"Disable module {module}?"):
        item = caravan.modules.disable(module, parent=parent)
        click.echo(
            click.style("SUCCESS: ", fg="green") + f"proposed '{item.hash.hex()}'."
        )


@cli.group()
def queue():
    """Commands to manage off-chain queue"""


@queue.command(cls=ConnectedProviderCommand)
@ape_cli_context()
@account_option("--proposer")
@click.option("--submit", is_flag=True, default=False)
@click.option("--stop-at", default=None)
@caravan_argument()
def run(cli_ctx, network, proposer, submit, stop_at, caravan):
    """
    Run all scripts to ensure local Wallet queue matches

    This command uses scripts from `scripts/q*.py` and "replays" them from the Wallet's current
    on-chain head, up to the last script found in the folder or until the new head is `--stop-at`.
    When executed using a fork network, it will only perform a simulated validation of the script.
    When executed using a live network, it will publish the transaction ONLY IF the transaction at
    that msghash does not exist in the off-chain queue.

    To enable this, scripts under `scripts/` must be properly named, and all use
    `caravan.cli.propose_from_simulation`.
    """

    if not (
        available_queue_scripts := {
            HexBytes(script.stem[1:]): script.relative_to(cli_ctx.local_project.path)
            for script in cli_ctx.local_project.scripts_folder.glob("q*.py")
        }
    ):
        raise click.UsageError("No scripts to queue detected in 'scripts/'.")

    elif (len(hashlen_sizes := set(len(s) for s in available_queue_scripts))) > 1:
        raise click.UsageError("All hashes in script filenames must be same length")

    # NOTE: Only one option, so pop it
    hashlen = hashlen_sizes.pop()
    assert stop_at is None or len(stop_at) == hashlen, (
        f"Does not match hash length of {hashlen}: '{stop_at}'"
    )

    parent = caravan.head
    cli_ctx.logger.info(f"Current head: {parent.to_0x_hex()}")

    while available_queue_scripts and parent[:hashlen] != stop_at:
        if not (script := available_queue_scripts.pop(parent[:hashlen], None)):
            raise click.UsageError(
                f"No command `q{parent[:hashlen]}.py` in `scripts/`."
            )

        elif not (cmd := runpy.run_path(str(script), run_name=script.stem).get("cli")):
            raise click.UsageError(f"No command `cli` detected in {script}.")

        cli_ctx.logger.info(f"Running '{script}':\n\n  {cmd.help}\n")
        # NOTE: This matches signature from `caravan.cli:propose_from_simulation`
        parent = cmd.callback.__wrapped__(
            cli_ctx, network, proposer, parent, submit, caravan
        )

        cli_ctx.logger.success(f"New head set: {parent.to_0x_hex()}")

    cli_ctx.logger.success("All script executed!")


@queue.command(cls=ConnectedProviderCommand)
@caravan_argument()
def status(caravan: "Caravan"):
    """View the current state of the Wallet's off-chain queue"""

    def traverse_queue(parent: HexBytes, depth: int = 0):
        for item in caravan.queue.children(parent):
            click.echo(
                f"{'  ' * depth}﹂{item}: ({item.confirmations}/{caravan.threshold})"
            )
            for field, value in item.message.render().items():
                click.echo(f"{'  ' * depth}  {field}: {value}")
            traverse_queue(item.hash, depth=depth + 1)

    current_head = caravan.head
    click.echo(f"{current_head.hex()}: (on-chain)")
    traverse_queue(current_head)


@queue.command(cls=ConnectedProviderCommand)
@caravan_argument()
@click.argument("itemhash", type=HexBytes)
def show(caravan: "Caravan", itemhash: HexBytes):
    item = caravan.queue.find(itemhash)
    click.echo(f"Confirmations: {item.confirmations}/{caravan.threshold}")

    for field, value in item.message.render().items():
        click.echo(f"  {field}: {value}")


@queue.command(cls=ConnectedProviderCommand)
@account_option("--submitter")
@caravan_argument()
@click.argument("new_head", type=HexBytes)
def merge(submitter: "AccountAPI", caravan: "Caravan", new_head: HexBytes):
    caravan.merge(new_head, sender=submitter)


@cli.group()
def sudo():
    """Manage the system contracts [ADVANCED]"""


@sudo.command(cls=ConnectedProviderCommand)
def check():
    """Check Proxy Factory and versions on the specified network"""

    click.echo(
        "Factory: "
        + (
            "deployed"
            if Address(FACTORY_DETERMINISTIC_ADDRESS).is_contract
            else "missing"
        )
    )

    for version, address in SINGLETON_DETERMINISTIC_ADDRESSES.items():
        click.echo(
            f"Singleton v{version}: "
            + ("deployed" if Address(address).is_contract else "missing")
        )


@sudo.group()
def deploy():
    """
    Deploy the system contracts

    NOTE: **Anyone can deploy these** (if CreateX is supported on network)
    """


@deploy.command(cls=ConnectedProviderCommand)
@ape_cli_context()
@account_option()
def factory(cli_ctx, network: "NetworkAPI", account: "AccountAPI"):
    """Deploy Proxy Factory to the specified network"""

    if Address(FACTORY_DETERMINISTIC_ADDRESS).is_contract:
        cli_ctx.logger.info("Factory already deployed")
        return

    try:
        factory = PackageType.FACTORY.deploy(sender=account)

    except AccountsError as e:
        if not network.is_dev:
            raise click.UsageError(
                "CreateX (https://createx.rocks) is not available on this chain."
            ) from e

        click.echo(
            click.style("WARNING:", fg="yellow") + "  Using non-determinstic deployment"
        )
        proxy_initcode = PackageType.PROXY().contract_type.get_deployment_bytecode()
        factory = PackageType.FACTORY().deploy(proxy_initcode, sender=account)

    click.secho(
        f"{factory.contract_type.name} deployed to {factory.address}", fg="green"
    )

    if not network.is_dev and network.explorer:
        click.secho(f"Publishing to {network.explorer.name}", fg="green")
        try:
            network.explorer.publish_contract(factory)
        except Exception as e:
            raise click.UsageError(f"Unable to verify {factory}") from e


@deploy.command(cls=ConnectedProviderCommand)
@ape_cli_context()
@version_option()
@account_option()
def singleton(cli_ctx, network: "NetworkAPI", version: Version, account: "AccountAPI"):
    """Deploy the given version of singleton contract"""

    if Address(SINGLETON_DETERMINISTIC_ADDRESSES[str(version)]).is_contract:
        cli_ctx.logger.info(f"Singleton {version} already deployed")
        return

    try:
        singleton = PackageType.SINGLETON.deploy(version=version, sender=account)

    except AccountsError as e:
        if not network.is_dev:
            raise click.UsageError(
                "CreateX (https://createx.rocks) is not available on this chain."
            ) from e

        click.echo(
            click.style("WARNING:", fg="yellow") + "  Using non-determinstic deployment"
        )
        singleton = PackageType.SINGLETON(version=version).deploy(
            str(version), sender=account
        )

    click.secho(
        f"{singleton.contract_type.name} v{version} deployed to {singleton.address}",
        fg="green",
    )

    if not network.is_dev and network.explorer:
        click.secho(f"Publishing to {network.explorer.name}", fg="green")
        try:
            network.explorer.publish_contract(singleton)
        except Exception as e:
            raise click.UsageError(f"Unable to verify {singleton}") from e
