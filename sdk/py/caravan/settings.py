import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ape.types import AddressType

# NOTE: This is the deterministic deployment address via CreateX
FACTORY_DETERMINISTIC_ADDRESS: "AddressType" = (
    "0x04579FFC45fE10A7901B88EaEc8F4850b847D37c"
)

# NOTE: This is the deterministic deployment addresses for each version via CreateX
SINGLETON_DETERMINISTIC_ADDRESSES: dict[str, "AddressType"] = {
    "1": "0xB810c65972596d213DCdf0A73b27fa7be59Ef3E2",
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
