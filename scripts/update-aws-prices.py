#!/usr/bin/env python
"""Write the bundled AWS prices (``aws_prices.json``) from the AWS Price List
Query API.

The add-on shows on-demand hourly prices of its machine sizes for the chosen
region. They come from the bundled file, so Orange makes no pricing calls.
Run this now and then to refresh it, with an AWS CLI profile that is signed
in (any account; the Price List API is free and needs ``pricing:GetProducts``)::

    .venv/bin/python scripts/update-aws-prices.py --profile <profile>

Per region, it keeps the on-demand price of each of the add-on's EC2
instance types (Linux, shared tenancy, no preinstalled software), of one
GB-month of ``gp3`` volume storage, and of one hour of an in-use public IPv4
address. List prices in USD, without discounts or free tiers.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "orangecontrib/cloud/client/providers/aws_prices.json"
#: The Price List Query API's endpoints are in a few regions only.
PRICING_REGION = "us-east-1"
#: On-demand Linux instances in shared tenancy, without licensed software.
INSTANCE_FILTERS = {
    "operatingSystem": "Linux",
    "tenancy": "Shared",
    "preInstalledSw": "NA",
    "licenseModel": "No License required",
    "capacitystatus": "Used",
    "marketoption": "OnDemand",
    "locationType": "AWS Region",
}
GP3_FILTERS = {"volumeApiName": "gp3", "productFamily": "Storage", "locationType": "AWS Region"}
IPV4_FILTERS = {"group": "VPCPublicIPv4Address", "locationType": "AWS Region"}
#: Usage type endings -> price names in the file.
GP3_USAGE = "VolumeUsage.gp3"
IPV4_USAGE = "PublicIPv4:InUseAddress"


def instance_types() -> list[str]:
    """The add-on's instance types."""
    sys.path.insert(0, str(OUT.parents[4]))
    from orangecontrib.cloud.client.providers.aws_vm import MACHINE_TYPES

    return [t.name for t in MACHINE_TYPES]


def _queries(types: Iterable[str]) -> list[tuple[str, dict[str, str]]]:
    """``(service code, filters)`` of each query."""
    queries = [("AmazonEC2", {"instanceType": t, **INSTANCE_FILTERS}) for t in types]
    return [*queries, ("AmazonEC2", GP3_FILTERS), ("AmazonVPC", IPV4_FILTERS)]


def fetch_products(profile: str, types: Iterable[str]) -> list[dict]:
    """The products of the add-on's prices in every region (a few hundred),
    with ``profile``'s sign-in."""
    import boto3

    pricing = boto3.Session(profile_name=profile).client("pricing", region_name=PRICING_REGION)
    paginator = pricing.get_paginator("get_products")
    products: list[dict] = []
    for service, filters in _queries(types):
        terms = [{"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in filters.items()]
        for page in paginator.paginate(ServiceCode=service, Filters=terms, FormatVersion="aws_v1"):
            products += [json.loads(p) for p in page["PriceList"]]
    return products


def on_demand_price(product: dict) -> float | None:
    """USD per unit of the product's one on-demand price (``None``: none,
    tiered, or in another currency, as in China's regions)."""
    terms = product.get("terms", {}).get("OnDemand", {})
    dimensions = [d for term in terms.values() for d in term.get("priceDimensions", {}).values()]
    if len(dimensions) != 1 or "USD" not in dimensions[0].get("pricePerUnit", {}):
        return None
    return float(dimensions[0]["pricePerUnit"]["USD"])


def price_name(attributes: dict, types: Iterable[str]) -> str | None:
    """The product's name in the file: the instance type, ``gp3_gib_month``
    or ``public_ipv4_hour`` (``None``: not one of the add-on's prices)."""
    usage = attributes.get("usagetype", "")
    instance_type = attributes.get("instanceType", "")
    if instance_type in types and usage.endswith(f"BoxUsage:{instance_type}"):
        return instance_type
    if usage.endswith(GP3_USAGE):
        return "gp3_gib_month"
    if usage.endswith(IPV4_USAGE):
        return "public_ipv4_hour"
    return None


def prices(products: list[dict], types: Iterable[str]) -> dict:
    """The bundled file's content from the products."""
    types = list(types)
    regions: dict[str, dict[str, float]] = {}
    for product in products:
        attributes = product.get("product", {}).get("attributes", {})
        name = price_name(attributes, types)
        price = on_demand_price(product)
        region = attributes.get("regionCode", "")
        if name is None or price is None or not region:
            continue
        known = regions.setdefault(region, {}).get(name)
        if known is not None and known != price:
            raise ValueError(f"Two prices of {name} in {region}: {known} and {price}")
        regions[region][name] = price
    # A region without instances or the disk price has nothing to offer.
    regions = {
        r: dict(sorted(p.items()))
        for r, p in sorted(regions.items())
        if "gp3_gib_month" in p and any(t in p for t in types)
    }
    return {
        "date": datetime.date.today().isoformat(),
        "currency": "USD",
        "source": (
            "AWS Price List Query API: EC2 on-demand Linux instances in shared tenancy, "
            "EBS gp3 storage, in-use public IPv4 addresses"
        ),
        "regions": regions,
    }


def main() -> None:
    """Fetch the prices and write the bundled file."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--profile",
        default=os.environ.get("ORANGE3_CLOUD_AWS_PROFILE") or os.environ.get("AWS_PROFILE"),
        help="the AWS CLI profile to sign in with (default: $ORANGE3_CLOUD_AWS_PROFILE, "
        "else $AWS_PROFILE, else the default profile)",
    )
    parser.add_argument("--products", type=Path, help="read the products from this JSON file")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    types = instance_types()
    if args.products:
        products = json.loads(args.products.read_text())
    else:
        products = fetch_products(args.profile or "default", types)
    data = prices(products, types)
    args.out.write_text(json.dumps(data, indent=1) + "\n")
    print(f"{len(products)} products, {len(data['regions'])} regions written to {args.out}")


if __name__ == "__main__":
    main()
