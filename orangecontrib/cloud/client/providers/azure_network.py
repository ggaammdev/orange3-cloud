"""What the Azure machines share, in the resource group ``orange3-cloud``: the
group itself, the virtual network ``orange3-cloud`` and the network security
group ``orange3-cloud-https``, which allows TCP 8443 only from this
computer's public IPv4 address (``/32``).

:func:`ensure_shared` creates them once in the chosen region (moving them
when the user chose another region and no machine is left),
:func:`ensure_security_group` updates the rule when the computer's address
changes, and :func:`delete_security_group_if_unused` deletes the network
security group with the last machine. The resource group and the virtual
network stay for the next machine. The calls go through
:class:`~orangecontrib.cloud.client.providers.azure_arm.Arm`.
"""

from __future__ import annotations

import time
from typing import Any

from orangecontrib.cloud.client.providers.azure_arm import Arm, ArmError
from orangecontrib.cloud.client.providers.azure_vm import (
    NETWORK_API,
    RESOURCE_GROUP,
    SECURITY_GROUP,
    VIRTUAL_NETWORK,
    allows_only,
    network_body,
    security_group_body,
    vm_states,
)
from orangecontrib.cloud.client.providers.base import ProviderError, StatusCallback
from orangecontrib.cloud.client.providers.cloud_vm import public_ip

__all__ = [
    "DELETE_TIMEOUT",
    "delete_security_group_if_unused",
    "ensure_security_group",
    "ensure_shared",
]

#: Seconds to wait until deleted resources are gone, and for a running
#: operation (e.g. a deallocation) to let a deletion start.
DELETE_TIMEOUT = 5 * 60


def ensure_shared(arm: Arm, region: str, report: StatusCallback) -> None:
    """Create what the machines share, unless it exists: the resource
    group, the virtual network and the network security group (which then
    allows exactly this computer's address)."""
    report(f"Preparing the resource group {RESOURCE_GROUP}")
    arm.ensure_group(region)
    what = f"create the virtual network {VIRTUAL_NETWORK}"
    network = arm.get(arm.paths.network, what, api=NETWORK_API)
    if network is not None and network.get("location") != region:
        _remove_from_other_region(arm, arm.paths.network, network, region)
        network = None
    if network is None:
        report(f"Creating the virtual network {VIRTUAL_NETWORK} in {region}")
        arm.put(arm.paths.network, network_body(region), what, api=NETWORK_API)
        arm.wait_provisioned(arm.paths.network, what, api=NETWORK_API)
    ensure_security_group(arm, region, report)


def ensure_security_group(arm: Arm, region: str, report: StatusCallback) -> None:
    """Create or update the network security group so that it allows
    exactly this computer's address."""
    address = public_ip()
    cidr = f"{address}/32"
    path = arm.paths.security_group
    what = f"update the network security group {SECURITY_GROUP}"
    group = arm.get(path, what, api=NETWORK_API)
    if group is not None and group.get("location") != region:
        _remove_from_other_region(arm, path, group, region)
        group = None
    if group is not None and allows_only(group, cidr):
        return
    verb = "Creating" if group is None else "Updating"
    report(f"{verb} the network security group {SECURITY_GROUP} for {address}")
    arm.put(path, security_group_body(region, cidr), what, api=NETWORK_API)
    arm.wait_provisioned(path, what, api=NETWORK_API)


def _remove_from_other_region(arm: Arm, path: str, resource: dict[str, Any], region: str) -> None:
    """Delete a shared piece that is in another region than ``region`` (the
    user chose another one), unless a machine still uses it."""
    where = resource.get("location", "")
    if arm.vms():
        raise ProviderError(
            f"A machine of the add-on runs in {where}, and machines can't be in two "
            f"regions at once. Stop it, or choose {where} in the Cloud settings."
        )
    what = f"move {resource.get('name', path)} to {region}"
    arm.delete(path, what, api=NETWORK_API)
    arm.wait_gone(path, what, api=NETWORK_API)


def delete_security_group_if_unused(arm: Arm) -> None:
    """Delete the network security group when no machine of the add-on is
    left, or only ones being deleted (their network interfaces may still use
    it for a while: the deletion is retried)."""
    staying = [vm for vm in arm.vms() if vm_states(vm)[0] != "Deleting"]
    if staying:
        return
    path, what = arm.paths.security_group, f"delete the network security group {SECURITY_GROUP}"
    deadline = time.monotonic() + DELETE_TIMEOUT
    while True:
        try:
            arm.delete(path, what, api=NETWORK_API)
            break
        except ArmError as exc:
            if exc.code != "InUseNetworkSecurityGroup" or time.monotonic() > deadline:
                raise
        time.sleep(arm.poll_interval)
    arm.wait_gone(path, what, api=NETWORK_API)
