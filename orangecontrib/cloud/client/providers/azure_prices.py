"""Hourly prices of the Azure machine sizes, from the bundled price list.

``azure_prices.json`` (read by :func:`~orangecontrib.cloud.client.providers.
prices.load_price_file`, written by ``scripts/update-azure-prices.py`` from
Azure's public Retail Prices API) has, per region, the pay-as-you-go price of
each of the add-on's VM sizes (Linux), of one month of each Premium SSD tier
(``premium_ssd_p10_month``, …) and of one hour of a Standard static public
IPv4 address (``public_ip_hour``). A machine's hourly price is its VM, its OS
disk and its address.

Premium SSD disks are billed per tier, by the tier's size: a 100 GB disk is a
P10 (128 GiB). The address is billed while it exists, also while the VM is
deallocated: a deallocated VM costs its disk and its address. Discounts,
free amounts, network traffic and stored files aren't included, so costs
are approximate.
"""

from __future__ import annotations

from orangecontrib.cloud.client.providers.prices import (
    HOURS_PER_MONTH,
    PriceFile,
    load_price_file,
)

__all__ = [
    "PREMIUM_SSD_TIERS",
    "disk_per_hour",
    "disk_price_name",
    "disk_tier",
    "load_prices",
    "per_hour",
    "stopped_per_hour",
    "vm_per_hour",
]

#: The Premium SSD tiers that the add-on's disk sizes (50–2000 GB) fall in,
#: with their sizes in GiB, smallest first. Azure bills a disk as the
#: smallest tier that holds it.
PREMIUM_SSD_TIERS: tuple[tuple[str, int], ...] = (
    ("P6", 64),
    ("P10", 128),
    ("P15", 256),
    ("P20", 512),
    ("P30", 1024),
    ("P40", 2048),
)
#: The price name of a Standard static public IPv4 address per hour.
PUBLIC_IP = "public_ip_hour"


def load_prices() -> PriceFile:
    """The bundled price list (read once)."""
    return load_price_file("azure")


def disk_tier(disk_gb: float) -> str | None:
    """The Premium SSD tier of a disk of ``disk_gb``, e.g. ``P10`` for 100
    GB; ``None`` if it is larger than the add-on's tiers."""
    return next((tier for tier, size in PREMIUM_SSD_TIERS if disk_gb <= size), None)


def disk_price_name(tier: str) -> str:
    """The price name of a month of the Premium SSD ``tier``, e.g.
    ``premium_ssd_p10_month``."""
    return f"premium_ssd_{tier.lower()}_month"


def vm_per_hour(machine_type: str, region: str) -> float | None:
    """The VM of ``machine_type`` per hour in ``region``; ``None`` if unknown."""
    return load_prices().region(region).get(machine_type)


def disk_per_hour(disk_gb: float, region: str) -> float | None:
    """A Premium SSD OS disk of ``disk_gb`` per hour in ``region`` (its
    tier's monthly price); ``None`` if unknown."""
    tier = disk_tier(disk_gb)
    per_month = load_prices().region(region).get(disk_price_name(tier)) if tier else None
    return None if per_month is None else per_month / HOURS_PER_MONTH


def per_hour(machine_type: str, region: str, disk_gb: float) -> float | None:
    """A running machine per hour in ``region``: the VM, its Premium SSD OS
    disk and its public IPv4 address; ``None`` if unknown there."""
    vm = vm_per_hour(machine_type, region)
    disk = disk_per_hour(disk_gb, region)
    if vm is None or disk is None:
        return None
    return vm + disk + load_prices().region(region).get(PUBLIC_IP, 0.0)


def stopped_per_hour(region: str, disk_gb: float) -> float | None:
    """A deallocated VM per hour in ``region``: its Premium SSD OS disk and
    its public IPv4 address, which stay; ``None`` if unknown there."""
    disk = disk_per_hour(disk_gb, region)
    return None if disk is None else disk + load_prices().region(region).get(PUBLIC_IP, 0.0)
