#!/usr/bin/env python
"""Write the bundled Google Cloud prices (``gcp_prices.json``) from the Cloud
Billing Catalog API.

The add-on shows on-demand hourly prices of its machine sizes for the chosen
region. They come from the bundled file, so Orange makes no billing calls.
Run this now and then to refresh it (it needs a Google sign-in:
``gcloud auth application-default login``)::

    .venv/bin/python scripts/update-gcp-prices.py

Per region, it keeps the on-demand price of one vCPU and one GiB of memory of
each machine family the add-on uses (E2, N2, N2D, C3) and of one GiB-month of
``pd-balanced`` disk, plus the global price of an external IPv4 address.
List prices in USD, without discounts.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

COMPUTE_SERVICE = "services/6F81-5844-456A"
CATALOG = f"https://cloudbilling.googleapis.com/v1/{COMPUTE_SERVICE}/skus"
OUT = Path(__file__).resolve().parents[1] / "orangecontrib/cloud/client/providers/gcp_prices.json"

#: SKU description -> (family, part). "Custom", "Sole Tenancy" and
#: "Extended" SKUs don't match.
_INSTANCE = re.compile(r"^(E2|N2|N2D AMD|C3) Instance (Core|Ram) running in ")
_FAMILY = {"E2": "e2", "N2": "n2", "N2D AMD": "n2d", "C3": "c3"}
_DISK = re.compile(r"^Balanced PD Capacity( in .*)?$")
_IP = "External IP Charge on a Standard VM"


def _unit_price(sku: dict) -> float:
    """USD per unit at the highest usage tier (free tiers don't count)."""
    rate = sku["pricingInfo"][0]["pricingExpression"]["tieredRates"][-1]["unitPrice"]
    return int(rate.get("units") or 0) + rate.get("nanos", 0) / 1e9


def fetch_skus() -> list[dict]:
    """All Compute Engine SKUs (about 35,000) with the user's sign-in."""
    sys.path.insert(0, str(OUT.parents[4]))
    from google.auth.transport.requests import AuthorizedSession

    from orangecontrib.cloud.client.providers.gcp_account import load_credentials

    credentials, _ = load_credentials()
    session = AuthorizedSession(credentials)
    skus: list[dict] = []
    params: dict[str, str | int] = {"pageSize": 5000, "currencyCode": "USD"}
    while True:
        response = session.get(CATALOG, params=params, timeout=120)
        response.raise_for_status()
        reply = response.json()
        skus += reply.get("skus", [])
        if not reply.get("nextPageToken"):
            return skus
        params["pageToken"] = reply["nextPageToken"]


def prices(skus: list[dict]) -> dict:
    """The bundled file's content from the SKUs."""
    regions: dict[str, dict[str, float]] = {}
    ip_per_hour = 0.0
    for sku in skus:
        if sku.get("category", {}).get("usageType") != "OnDemand":
            continue
        description = sku.get("description", "")
        instance = _INSTANCE.match(description)
        if instance:
            key = f"{_FAMILY[instance.group(1)]}_{'cpu' if instance.group(2) == 'Core' else 'gib'}"
        elif _DISK.match(description):
            key = "pd_balanced_gib_month"
        elif description == _IP:
            ip_per_hour = _unit_price(sku)
            continue
        else:
            continue
        for region in sku.get("serviceRegions", []):
            regions.setdefault(region, {})[key] = round(_unit_price(sku), 9)
    regions = {r: dict(sorted(p.items())) for r, p in sorted(regions.items()) if "-" in r}
    return {
        "date": datetime.date.today().isoformat(),
        "currency": "USD",
        "source": "Cloud Billing Catalog API, Compute Engine on-demand list prices",
        "external_ip_per_hour": ip_per_hour,
        "regions": regions,
    }


def main() -> None:
    """Fetch the prices and write the bundled file."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--skus", type=Path, help="read the SKUs from this JSON file instead")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    skus = json.loads(args.skus.read_text()) if args.skus else fetch_skus()
    data = prices(skus)
    args.out.write_text(json.dumps(data, indent=1) + "\n")
    print(f"{len(data['regions'])} regions written to {args.out}")


if __name__ == "__main__":
    main()
