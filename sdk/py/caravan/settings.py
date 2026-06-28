import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ape.types import AddressType

# NOTE: This is the deterministic deployment address via CreateX
FACTORY_DETERMINISTIC_ADDRESS: "AddressType" = (
    "0xdb987cC31270E974057DBD259e392Af8485355b5"
)

# NOTE: This is the deterministic deployment addresses for each version via CreateX
SINGLETON_DETERMINISTIC_ADDRESSES: dict[str, "AddressType"] = {
    "1": "0x265A7B39E6D6b68d0018eeFBca364085b1EF92aE",
}

USER_CACHE_DIR: Path = (
    path
    if (
        (value := os.environ.get("XDG_CACHE_HOME"))
        and (path := Path(value)).exists()
        and path.is_absolute()
    )
    else (Path.home() / ".cache")
) / "caravan"
USER_CACHE_DIR.mkdir(exist_ok=True)

USER_CONFIG_DIR: Path = (
    path
    if (
        (value := os.environ.get("XDG_CONFIG_HOME"))
        and (path := Path(value)).exists()
        and path.is_absolute()
    )
    else (Path.home() / ".config")
) / "caravan"
USER_CONFIG_DIR.mkdir(exist_ok=True)
