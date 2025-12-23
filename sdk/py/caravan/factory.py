from typing import TYPE_CHECKING

from ape.types import AddressType
from ape.logging import logger
from ape.utils import ManagerAccessMixin, cached_property
from packaging.version import Version

from .main import Caravan
from .packages import STABLE_VERSION, PackageType
from .settings import FACTORY_DETERMINISTIC_ADDRESS, SINGLETON_DETERMINISTIC_ADDRESSES

if TYPE_CHECKING:
    from ape.contracts import ContractInstance


class Factory(ManagerAccessMixin):
    def __init__(self, address: AddressType | None = None):
        if address:
            self.address = address

        elif len((factory_type := PackageType.FACTORY()).deployments) == 0:
            self.address = FACTORY_DETERMINISTIC_ADDRESS

            if not len(self.provider.get_code(self.address)) > 0:
                raise RuntimeError("No CaravanFactory deployment on this chain")

        else:
            # NOTE: Override cached value of `contract`
            self.contract = factory_type.deployments[0]
            self.address = self.contract.address

        # NOTE: also lets us override for testing
        self._cached_releases: dict[Version, "ContractInstance"] = {
            Version(version): PackageType.SINGLETON(version).at(address)
            for version, address in SINGLETON_DETERMINISTIC_ADDRESSES.items()
        }

    @cached_property
    def contract(self) -> "ContractInstance":
        return PackageType.FACTORY().at(
            self.address,
            fetch_from_explorer=False,
            detect_proxy=False,
        )

    def get_release(self, version: Version) -> "ContractInstance | None":
        if (release := self._cached_releases.get(version)) and len(release.code) > 0:
            return release

        elif not (singleton_type := PackageType.SINGLETON(version)).deployments:
            return None

        elif not len((release := singleton_type.deployments[-1]).code) > 0:
            return None

        self._cached_releases[version] = release
        return release

    def new(
        self,
        signers: list[AddressType],
        threshold: int | None = None,
        version: Version | str = STABLE_VERSION,
        tag: str | None = None,
        **txn_args,
    ) -> Caravan:
        if threshold is None:
            threshold = len(signers) // 2

        if isinstance(version, str):
            version = Version(version.lstrip("v"))

        if not (release := self.get_release(version)):
            raise RuntimeError("No Caravan Singleton deployment on this chain")

        args = [release, signers, threshold]
        if tag is not None:
            args.append(tag)

        receipt = self.contract.new(*args, **txn_args)

        if len(events := self.contract.NewCaravan.from_receipt(receipt)) != 1:
            raise RuntimeError(f"No deployment detected in '{receipt.txn_hash}'")

        proxy = Caravan(address=events[0].new_proxy, version=version, factory=self)

        if not self.provider.network.is_dev and self.provider.network.explorer:
            try:
                self.provider.network.explorer.publish_contract(proxy.contract)

            except Exception as e:
                logger.warn_from_exception(e, f"Error verifying {proxy.contract}")

        return proxy
