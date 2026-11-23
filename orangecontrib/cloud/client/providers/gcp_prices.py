"""Hourly prices of the Google Cloud machine sizes, from the bundled price list.

``gcp_prices.json`` (read by :func:`~orangecontrib.cloud.client.providers.
prices.load_price_file`, written by ``scripts/update-gcp-prices.py`` from the
Cloud Billing Catalog API) has, per region, the on-demand list price of one
vCPU and one GiB of memory of each machine family, and of one GiB-month of
``pd-balanced`` disk, plus the price of an external IPv4 address. A machine's
hourly price is its vCPUs and memory, its boot disk and its external address.
A stopped machine costs only its disk: its ephemeral external address is
released when it stops.
Discounts, network traffic and stored files aren't included, so costs are
approximate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from orangecontrib.cloud.client.providers.base import MachineType
from orangecontrib.cloud.client.providers.gcp_vm import MACHINE_TYPES
from orangecontrib.cloud.client.providers.prices import HOURS_PER_MONTH, load_price_file

__all__ = ["HOURS_PER_MONTH", "PriceList", "load_prices"]


@dataclass(frozen=True)
class PriceList:
    """On-demand list prices per region (USD)."""

    date: str  #: when the prices were read, ISO date
    currency: str = "USD"
    external_ip_per_hour: float = 0.0
    #: region -> ``{"<family>_cpu": per vCPU-hour, "<family>_gib": per GiB-hour,
    #: "pd_balanced_gib_month": per GiB-month}``
    regions: dict[str, dict[str, float]] = field(default_factory=dict)

    def machine_per_hour(self, machine_type: str | MachineType, region: str) -> float | None:
        """vCPUs and memory of ``machine_type`` per hour in ``region``;
        ``None`` if unknown."""
        if isinstance(machine_type, str):
            known = {t.name: t for t in MACHINE_TYPES}
            if machine_type not in known:
                return None
            machine_type = known[machine_type]
        rates = self.regions.get(region, {})
        family = machine_type.name.split("-", 1)[0]
        cpu, gib = rates.get(f"{family}_cpu"), rates.get(f"{family}_gib")
        if cpu is None or gib is None or not machine_type.cpus or not machine_type.memory_gb:
            return None
        return machine_type.cpus * cpu + machine_type.memory_gb * gib

    def disk_per_hour(self, disk_gb: float, region: str) -> float | None:
        """A ``pd-balanced`` disk of ``disk_gb`` per hour; ``None`` if unknown."""
        per_month = self.regions.get(region, {}).get("pd_balanced_gib_month")
        return None if per_month is None else disk_gb * per_month / HOURS_PER_MONTH

    def stopped_per_hour(self, region: str, disk_gb: float) -> float | None:
        """A stopped machine per hour: its boot disk (its ephemeral external
        address is released when it stops); ``None`` if unknown in ``region``."""
        return self.disk_per_hour(disk_gb, region)

    def per_hour(
        self, machine_type: str | MachineType, region: str, disk_gb: float
    ) -> float | None:
        """A running machine per hour: vCPUs, memory, boot disk and external
        address; ``None`` if unknown in ``region``."""
        machine = self.machine_per_hour(machine_type, region)
        disk = self.disk_per_hour(disk_gb, region)
        if machine is None or disk is None:
            return None
        return machine + disk + self.external_ip_per_hour


@lru_cache(maxsize=1)
def load_prices() -> PriceList:
    """The bundled price list."""
    prices = load_price_file("gcp")
    return PriceList(
        date=prices.date,
        currency=prices.currency,
        external_ip_per_hour=float(prices.extra.get("external_ip_per_hour", 0.0)),
        regions=prices.regions,
    )
