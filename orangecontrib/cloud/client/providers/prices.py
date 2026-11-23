"""The providers' bundled price lists: one loader for all clouds.

Each cloud's prices are in ``<cloud>_prices.json`` next to this module,
written by a script in ``scripts/`` from the cloud's own price list, with the
date they were read. Prices are read from the file, not asked for at run
time, so they work without a sign-in and cost no calls. The file holds

* ``date``: when the prices were read (ISO date),
* ``currency`` (default ``USD``) and ``source`` (where they came from),
* ``regions``: per region, named prices (what the names mean is up to the
  cloud's own module, e.g. :mod:`~orangecontrib.cloud.client.providers.
  gcp_prices`),
* anything else the cloud needs, e.g. the price of a public IP address.

Discounts, network traffic and stored files aren't included, so the costs
computed from these prices are approximate.
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from orangecontrib.cloud.client.providers.base import ProviderError

__all__ = ["HOURS_PER_MONTH", "PriceFile", "load_price_file", "price_file_path"]

#: Hours in a month, for prices given per month (e.g. of disks).
HOURS_PER_MONTH = 730


@dataclass(frozen=True)
class PriceFile:
    """A cloud's bundled prices."""

    cloud: str  #: the provider's id, e.g. ``gcp``
    date: str  #: when the prices were read, ISO date
    currency: str = "USD"
    source: str = ""
    #: region -> price name -> price
    regions: dict[str, dict[str, float]] = field(default_factory=dict)
    #: The file's other top-level values.
    extra: dict[str, Any] = field(default_factory=dict)

    def region(self, region: str) -> dict[str, float]:
        """The prices in ``region`` (empty if it has none)."""
        return self.regions.get(region, {})


def price_file_path(cloud: str) -> Path:
    """``<cloud>_prices.json`` next to this module."""
    return Path(__file__).with_name(f"{cloud}_prices.json")


@cache
def load_price_file(cloud: str, path: Path | None = None) -> PriceFile:
    """The bundled prices of ``cloud`` (default file :func:`price_file_path`)."""
    path = path or price_file_path(cloud)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        datetime.date.fromisoformat(data["date"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ProviderError(f"The bundled prices in {path.name} can't be read: {exc}") from exc
    known = ("date", "currency", "source", "regions")
    return PriceFile(
        cloud=cloud,
        date=data["date"],
        currency=data.get("currency", "USD"),
        source=data.get("source", ""),
        regions=data.get("regions", {}),
        extra={k: v for k, v in data.items() if k not in known},
    )
