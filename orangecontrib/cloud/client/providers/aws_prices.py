"""Hourly prices of the AWS machine sizes, from the bundled price list.

``aws_prices.json`` (read by :func:`~orangecontrib.cloud.client.providers.
prices.load_price_file`, written by ``scripts/update-aws-prices.py`` from the
AWS Price List Query API) has, per region, the on-demand price of each of the
add-on's instance types (Linux, shared tenancy), of one GB-month of ``gp3``
volume storage (``gp3_gib_month``) and of one hour of an in-use public IPv4
address (``public_ipv4_hour``). A machine's hourly price is its instance,
its boot volume and its address. A stopped instance costs only its volume:
its public address is released when it stops. Discounts, free tiers,
network traffic and stored files aren't included, so costs are approximate.
"""

from __future__ import annotations

from orangecontrib.cloud.client.providers.prices import (
    HOURS_PER_MONTH,
    PriceFile,
    load_price_file,
)

__all__ = ["disk_per_hour", "instance_per_hour", "load_prices", "per_hour", "stopped_per_hour"]


def load_prices() -> PriceFile:
    """The bundled price list (read once)."""
    return load_price_file("aws")


def instance_per_hour(machine_type: str, region: str) -> float | None:
    """The instance of ``machine_type`` per hour in ``region``; ``None`` if unknown."""
    return load_prices().region(region).get(machine_type)


def disk_per_hour(disk_gb: float, region: str) -> float | None:
    """A ``gp3`` volume of ``disk_gb`` per hour in ``region``; ``None`` if unknown."""
    per_month = load_prices().region(region).get("gp3_gib_month")
    return None if per_month is None else disk_gb * per_month / HOURS_PER_MONTH


def per_hour(machine_type: str, region: str, disk_gb: float) -> float | None:
    """A running machine per hour in ``region``: the instance, its ``gp3``
    boot volume and its public IPv4 address; ``None`` if unknown there."""
    instance = instance_per_hour(machine_type, region)
    disk = disk_per_hour(disk_gb, region)
    if instance is None or disk is None:
        return None
    return instance + disk + load_prices().region(region).get("public_ipv4_hour", 0.0)


def stopped_per_hour(region: str, disk_gb: float) -> float | None:
    """A stopped instance per hour in ``region``: its ``gp3`` boot volume (its
    public address is released when it stops); ``None`` if unknown there."""
    return disk_per_hour(disk_gb, region)
