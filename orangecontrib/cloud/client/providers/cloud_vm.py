"""What the providers of cloud machines share: this computer's public address,
which their firewall rules allow, and following a machine's boot from what
its console prints (the bootstrap's ``[bootstrap …]`` lines).
"""

from __future__ import annotations

import ipaddress
import logging
import os
from collections.abc import Iterable

from orangecontrib.cloud.client.api import CloudClient, CloudError
from orangecontrib.cloud.client.providers.base import ProviderError

__all__ = [
    "BOOT_FAILED",
    "PUBLIC_IP_SERVICES",
    "answers",
    "boot_failed",
    "log_tail",
    "progress_message",
    "public_ip",
]

log = logging.getLogger(__name__)

#: Services that tell this computer's public IPv4 address, tried in order.
PUBLIC_IP_SERVICES = ("https://api.ipify.org", "https://checkip.amazonaws.com")
#: What the bootstrap prints when the machine can't start.
BOOT_FAILED = "the machine did not start"


def public_ip(timeout: float = 10) -> str:
    """This computer's public IPv4 address, as the internet sees it.

    ``ORANGE3_CLOUD_PUBLIC_IP`` overrides it (e.g. behind a proxy that the
    services below don't see through)."""
    override = os.environ.get("ORANGE3_CLOUD_PUBLIC_IP", "").strip()
    candidates = [override] if override else []
    if not candidates:
        import requests

        for url in PUBLIC_IP_SERVICES:
            try:
                response = requests.get(url, timeout=timeout)
                if response.ok:
                    candidates.append(response.text.strip())
                    break
            except requests.RequestException:
                continue
    for text in candidates:
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            continue
        if address.version == 4 and address.is_global:
            return str(address)
    raise ProviderError(
        "Couldn't find this computer's public IP address, which the firewall must allow. "
        "Check the internet connection, or set ORANGE3_CLOUD_PUBLIC_IP."
    )


def answers(client: CloudClient) -> bool:
    """Whether the machine's API answers yet."""
    try:
        client.info()
    except CloudError as exc:
        log.debug("Not answering yet: %s", exc)
        return False
    return True


def boot_failed(lines: Iterable[str]) -> bool:
    """Whether console lines say that the machine couldn't start."""
    return any(BOOT_FAILED in line for line in lines)


def progress_message(lines: list[str]) -> str | None:
    """The latest progress message in console lines."""
    for line in reversed(lines):
        if "[bootstrap" in line:
            # e.g. "docker[1056]: [bootstrap   3.4s] installing 94 required packages"
            rest = line[line.index("[bootstrap") :]
            return rest.split("]", 1)[-1].strip() if "]" in rest else None
        if "[orange3-cloud] pulling the machine image" in line:
            return "downloading the machine image"
    return None


def log_tail(lines: list[str], count: int = 15) -> str:
    """The last of the machine's own lines in its console, for an error message."""
    keep = [ln for ln in lines if "[bootstrap" in ln or "orange3-cloud" in ln or "docker" in ln]
    return "\n".join((keep or lines)[-count:])
