#!/usr/bin/env python
"""Write the bundled Azure prices (``azure_prices.json``) from Azure's public
Retail Prices API.

The add-on shows pay-as-you-go hourly prices of its machine sizes for the
chosen region. They come from the bundled file, so Orange makes no pricing
calls. Run this now and then to refresh it (the API is public: no sign-in)::

    .venv/bin/python scripts/update-azure-prices.py

Per region, it keeps the pay-as-you-go price of each of the add-on's VM sizes
(Linux, not Spot or Low Priority), of one month of each Premium SSD tier the
add-on's disk sizes fall in (LRS), and of one hour of a Standard static
public IPv4 address. List prices in USD, without discounts.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import urllib.parse
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "orangecontrib/cloud/client/providers/azure_prices.json"
API = "https://prices.azure.com/api/retail/prices"
#: Pay-as-you-go prices only (no reservations or savings plans).
CONSUMPTION = "priceType eq 'Consumption'"
IP_METER = "Standard IPv4 Static Public IP"


def add_on() -> ModuleType:
    """The add-on's ``azure_prices`` module (its tiers and price names)."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from orangecontrib.cloud.client.providers import azure_prices

    return azure_prices


def add_on_names() -> tuple[list[str], list[str]]:
    """The add-on's VM sizes and its Premium SSD tiers."""
    add_on()
    from orangecontrib.cloud.client.providers.azure_vm import MACHINE_TYPES

    return [t.name for t in MACHINE_TYPES], [tier for tier, _ in add_on().PREMIUM_SSD_TIERS]


def _any_of(field: str, values: Iterable[str]) -> str:
    return "(" + " or ".join(f"{field} eq '{v}'" for v in values) + ")"


def filters(sizes: Iterable[str], tiers: Iterable[str]) -> list[str]:
    """The API's ``$filter`` of each query: the VM sizes, the disk tiers and
    the public address."""
    return [
        f"serviceName eq 'Virtual Machines' and {CONSUMPTION} and {_any_of('armSkuName', sizes)}",
        f"serviceName eq 'Storage' and productName eq 'Premium SSD Managed Disks' and "
        f"{CONSUMPTION} and {_any_of('meterName', (f'{t} LRS Disk' for t in tiers))}",
        f"serviceName eq 'Virtual Network' and productName eq 'IP Addresses' and "
        f"{CONSUMPTION} and meterName eq '{IP_METER}'",
    ]


def fetch_items(sizes: Iterable[str], tiers: Iterable[str]) -> list[dict]:
    """The price items of the add-on's prices in every region (a few
    thousand, in pages of 1000)."""
    items: list[dict] = []
    for query in filters(sizes, tiers):
        url: str | None = API + "?" + urllib.parse.urlencode({"$filter": query})
        while url:
            with urllib.request.urlopen(url, timeout=60) as response:
                page = json.load(response)
            items += page.get("Items", [])
            url = page.get("NextPageLink")
    return items


def price_name(item: dict, sizes: Iterable[str], tiers: Iterable[str]) -> str | None:
    """The item's name in the file: the VM size, ``premium_ssd_<tier>_month``
    or ``public_ip_hour`` (``None``: not one of the add-on's prices, e.g.
    Windows, Spot or another currency)."""
    names = add_on()
    if item.get("type") != "Consumption" or item.get("currencyCode", "USD") != "USD":
        return None
    sku, meter = item.get("armSkuName", ""), item.get("meterName", "")
    unit = item.get("unitOfMeasure")
    linux = not item.get("productName", "").endswith(" Windows")
    regular = not item.get("skuName", "").endswith((" Spot", " Low Priority"))
    if sku in sizes and linux and regular and unit == "1 Hour":
        return sku
    tier = meter.removesuffix(" LRS Disk")
    if tier in tiers and unit == "1/Month":
        return names.disk_price_name(tier)
    if meter == IP_METER and unit == "1 Hour":
        return names.PUBLIC_IP
    return None


def prices(items: list[dict], sizes: Iterable[str], tiers: Iterable[str]) -> dict:
    """The bundled file's content from the price items."""
    sizes, tiers = list(sizes), list(tiers)
    regions: dict[str, dict[str, float]] = {}
    for item in items:
        name, region = price_name(item, sizes, tiers), item.get("armRegionName", "")
        if name is None or not region:
            continue
        price = float(item["retailPrice"])
        known = regions.setdefault(region, {}).get(name)
        if known is not None and known != price:
            raise ValueError(f"Two prices of {name} in {region}: {known} and {price}")
        regions[region][name] = price
    return {
        "date": datetime.date.today().isoformat(),
        "currency": "USD",
        "source": (
            "Azure Retail Prices API: pay-as-you-go Linux VMs, Premium SSD LRS managed disks, "
            "Standard static public IPv4 addresses"
        ),
        "regions": complete_regions(regions, sizes, tiers),
    }


def complete_regions(
    regions: dict[str, dict[str, float]], sizes: list[str], tiers: list[str]
) -> dict[str, dict[str, float]]:
    """The regions with VM sizes, every disk tier and the address (the
    "Global" address price fills the gaps), sorted."""
    names = add_on()
    fallback = regions.get("Global", {}).get(names.PUBLIC_IP)
    complete = {}
    for region, found in sorted(regions.items()):
        if fallback is not None:
            found.setdefault(names.PUBLIC_IP, fallback)
        has_disks = all(names.disk_price_name(t) in found for t in tiers)
        if any(s in found for s in sizes) and has_disks and names.PUBLIC_IP in found:
            complete[region] = dict(sorted(found.items()))
    return complete


def main() -> None:
    """Fetch the prices and write the bundled file."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--items", type=Path, help="read the price items from this JSON file")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    sizes, tiers = add_on_names()
    items = json.loads(args.items.read_text()) if args.items else fetch_items(sizes, tiers)
    data = prices(items, sizes, tiers)
    args.out.write_text(json.dumps(data, indent=1) + "\n")
    print(f"{len(items)} price items, {len(data['regions'])} regions written to {args.out}")


if __name__ == "__main__":
    main()
