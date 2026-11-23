"""What a machine on Azure is made of: its sizes, the fixed names of what the
add-on creates there, the quotas the sizes count against, and the Resource
Manager request bodies. No API calls (see :mod:`~orangecontrib.cloud.client.
providers.azure_arm`, :mod:`~orangecontrib.cloud.client.providers.
azure_account` and :mod:`~orangecontrib.cloud.client.providers.azure_setup`).
"""

from __future__ import annotations

import base64
import datetime
import re
import uuid
from dataclasses import dataclass
from typing import Any

from orangecontrib.cloud.client.providers.base import MachineType, utc_time
from orangecontrib.cloud.client.providers.cloud_init import HostSetup

__all__ = [
    "AUTHORIZATION_API",
    "COMPUTE_API",
    "CUSTOM_DATA_LIMIT",
    "DEALLOCATE_SCRIPT",
    "DEFAULT_MACHINE_TYPE",
    "DEFAULT_REGION",
    "HOST",
    "IMAGE_VAR",
    "MACHINE_TYPES",
    "NAME_PREFIX",
    "NETWORK_API",
    "OS_IMAGE",
    "PORT",
    "POWER_ROLE",
    "POWER_ROLE_ID",
    "REGIONAL_QUOTA",
    "REGIONAL_QUOTA_NAME",
    "RESOURCES_API",
    "RESOURCE_GROUP",
    "RESOURCE_PROVIDERS",
    "SECURITY_GROUP",
    "SUBNET",
    "TAG",
    "TAG_SESSION",
    "VIRTUAL_NETWORK",
    "AzureTarget",
    "Paths",
    "allows_only",
    "failure_message",
    "family_quota",
    "network_body",
    "power_role_body",
    "role_assignment_id",
    "security_group_body",
    "stop_time",
    "throwaway_ssh_key",
    "vm_request",
    "vm_states",
]

#: Machine sizes on Azure, from smallest to largest memory: the same names
#: as on Google Cloud, with the closest Azure VM sizes. They are fixed for
#: the add-on (x86-64 only, no GPUs). Azure's memory is GiB.
MACHINE_TYPES: tuple[MachineType, ...] = (
    MachineType("Standard_D4s_v6", "Small (testing)", 4, 16),
    MachineType("Standard_D16s_v6", "Medium", 16, 64),
    MachineType("Standard_E16s_v6", "Medium, high memory", 16, 128),
    MachineType("Standard_D32s_v6", "Large", 32, 128),
    MachineType("Standard_E32s_v6", "Large, high memory", 32, 256),
    MachineType("Standard_D64s_v6", "Extra large", 64, 256),
    MachineType("Standard_E64s_v6", "Extra large, high memory", 64, 512),
    MachineType("Standard_D96s_v6", "Huge", 96, 384),
    MachineType("Standard_E96s_v6", "Huge, high memory", 96, 768),
)
DEFAULT_MACHINE_TYPE = "Standard_D16s_v6"

#: Used when the Azure CLI has no default location.
DEFAULT_REGION = "eastus"

#: Everything the add-on creates on Azure is in this resource group, in the
#: chosen region.
RESOURCE_GROUP = "orange3-cloud"
#: The virtual network of the machines, its address space and its one subnet.
VIRTUAL_NETWORK = "orange3-cloud"
ADDRESS_SPACE = "10.42.0.0/16"
SUBNET = "default"
SUBNET_PREFIX = "10.42.0.0/24"
#: The network security group that lets this computer reach the machines,
#: and its one rule.
SECURITY_GROUP = "orange3-cloud-https"
RULE_PRIORITY = 1000
PORT = 8443
#: Tags of what the add-on creates: ``TAG=true`` and (machines)
#: ``TAG_SESSION=<machine id>``.
TAG = "orange3-cloud"
TAG_SESSION = "orange3-cloud-session"
#: Prefix of the VMs' names; the rest is the machine id.
NAME_PREFIX = "orange3-cloud-"

#: The resource providers the subscription must have registered.
RESOURCE_PROVIDERS: dict[str, str] = {
    "Microsoft.Compute": "virtual machines and disks",
    "Microsoft.Network": "networks and public addresses",
    "Microsoft.Storage": "storage of uploaded files",
}

#: The built-in role that lets a machine deallocate its own VM (scoped to
#: the VM; the user who starts the machine assigns it).
POWER_ROLE = "Desktop Virtualization Power On Off Contributor"
POWER_ROLE_ID = "40c5ff49-9181-41f8-ae61-143b0e78555e"

#: The regional quota every size counts against (Compute usage name).
REGIONAL_QUOTA = "cores"
REGIONAL_QUOTA_NAME = "Total Regional vCPUs"

#: Resource Manager API versions of the add-on's requests.
COMPUTE_API = "2024-07-01"
NETWORK_API = "2024-05-01"
RESOURCES_API = "2021-04-01"
AUTHORIZATION_API = "2022-04-01"

#: The operating system: Canonical's newest Ubuntu LTS server image (Gen2,
#: with cloud-init; the v6 sizes need its NVMe disk interface).
OS_IMAGE = {
    "publisher": "Canonical",
    "offer": "ubuntu-26_04-lts",
    "sku": "server",
    "version": "latest",
}
#: The OS disk's type (Premium SSD).
DISK_TYPE = "Premium_LRS"
#: Azure's limit of custom data (the user data, before base64).
CUSTOM_DATA_LIMIT = 65535
#: The VM's administrator, which Azure requires; nobody signs in (port 22
#: stays closed, and the SSH key's private half is thrown away).
ADMIN_USER = "orange"
#: Overrides the machine image (e.g. a development image ``:dev-<sha>``).
IMAGE_VAR = "ORANGE3_CLOUD_AZURE_IMAGE"

#: Where the deallocation script goes on the VM.
DEALLOCATE_PATH = "/usr/local/sbin/orange3-cloud-deallocate"
#: Deallocates the VM with its managed identity when the container exits,
#: for any reason (a VM that only powers off stays allocated and costs
#: compute). Powers off if that fails; does nothing while the VM shuts
#: down (Azure, or the add-on, is stopping it already). Never prints the token.
DEALLOCATE_SCRIPT = """#!/bin/sh
case "$(systemctl is-system-running)" in stopping) exit 0 ;; esac
md() { curl -sf --max-time 10 -H Metadata:true "http://169.254.169.254/metadata/$1"; }
echo "[orange3-cloud] deallocating the VM"
token=$(md "identity/oauth2/token?api-version=2018-02-01&resource=https://management.azure.com/" \\
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["access_token"])')
id=$(md "instance/compute/resourceId?api-version=2021-02-01&format=text")
if [ -n "$token" ] && [ -n "$id" ] && curl -sf --max-time 30 -o /dev/null -X POST \\
    -H "Authorization: Bearer $token" -H "Content-Length: 0" \\
    "https://management.azure.com${id}/deallocate?api-version=2024-07-01"; then
  echo "[orange3-cloud] Azure is deallocating the VM"
  exit 0
fi
echo "[orange3-cloud] ERROR: could not deallocate the VM; powering off (it stays allocated)"
systemctl poweroff
"""
#: What Azure adds to the add-on's VMs: Docker (Ubuntu doesn't have it), and
#: the deallocation when the container exits.
HOST = HostSetup(
    service=(f"ExecStopPost={DEALLOCATE_PATH}",),
    files=({"path": DEALLOCATE_PATH, "permissions": "0755", "content": DEALLOCATE_SCRIPT},),
    packages=("docker.io",),
)

_SIZE = re.compile(r"^Standard_([A-Z]+)\d+([a-z]*)_(v\d+)$")


def family_quota(machine_type: str) -> tuple[str, str]:
    """The family quota a size counts against: its Compute usage name and
    its name in the Azure portal, e.g. ``("StandardDsv6Family", "Standard
    Dsv6 Family vCPUs")`` for ``Standard_D4s_v6``."""
    match = _SIZE.match(machine_type)
    if match is None:
        raise ValueError(f"Not an Azure VM size of the add-on: {machine_type}")
    family = "".join(match.groups())
    return f"Standard{family}Family", f"Standard {family} Family vCPUs"


@dataclass(frozen=True)
class AzureTarget:
    """Where and how big a new machine is."""

    subscription: str
    region: str
    machine_type: str
    disk_gb: int


@dataclass(frozen=True)
class Paths:
    """Resource Manager paths of what the add-on creates in a subscription."""

    subscription: str

    @property
    def group(self) -> str:
        """The resource group :data:`RESOURCE_GROUP`."""
        return f"/subscriptions/{self.subscription}/resourceGroups/{RESOURCE_GROUP}"

    @property
    def vms(self) -> str:
        """The VMs of the resource group."""
        return f"{self.group}/providers/Microsoft.Compute/virtualMachines"

    def vm(self, name: str) -> str:
        """The VM ``name``."""
        return f"{self.vms}/{name}"

    @property
    def network(self) -> str:
        """The virtual network :data:`VIRTUAL_NETWORK`."""
        return f"{self.group}/providers/Microsoft.Network/virtualNetworks/{VIRTUAL_NETWORK}"

    @property
    def subnet(self) -> str:
        """Its subnet :data:`SUBNET`."""
        return f"{self.network}/subnets/{SUBNET}"

    @property
    def security_group(self) -> str:
        """The network security group :data:`SECURITY_GROUP`."""
        return f"{self.group}/providers/Microsoft.Network/networkSecurityGroups/{SECURITY_GROUP}"

    @property
    def power_role(self) -> str:
        """The role definition :data:`POWER_ROLE`."""
        return (
            f"/subscriptions/{self.subscription}/providers/Microsoft.Authorization/"
            f"roleDefinitions/{POWER_ROLE_ID}"
        )


def _tags(machine_id: str | None = None) -> dict[str, str]:
    return {TAG: "true", **({TAG_SESSION: machine_id} if machine_id else {})}


def network_body(region: str) -> dict[str, Any]:
    """The virtual network: one address space and one subnet."""
    return {
        "location": region,
        "tags": _tags(),
        "properties": {
            "addressSpace": {"addressPrefixes": [ADDRESS_SPACE]},
            "subnets": [{"name": SUBNET, "properties": {"addressPrefix": SUBNET_PREFIX}}],
        },
    }


def security_group_body(region: str, cidr: str) -> dict[str, Any]:
    """The network security group with its one rule: TCP :data:`PORT` from
    ``cidr`` (a ``PUT`` replaces all other rules)."""
    rule = {
        "name": SECURITY_GROUP,
        "properties": {
            "description": "orange3-cloud: HTTPS to the add-on's machines, only from your computer",
            "priority": RULE_PRIORITY,
            "direction": "Inbound",
            "access": "Allow",
            "protocol": "Tcp",
            "sourceAddressPrefix": cidr,
            "sourcePortRange": "*",
            "destinationAddressPrefix": "*",
            "destinationPortRange": str(PORT),
        },
    }
    return {"location": region, "tags": _tags(), "properties": {"securityRules": [rule]}}


def allows_only(group: dict[str, Any], cidr: str) -> bool:
    """Whether the network security group (as Resource Manager gives it)
    has exactly one rule, which allows TCP :data:`PORT` from ``cidr``."""
    rules = group.get("properties", {}).get("securityRules") or []
    if len(rules) != 1:
        return False
    rule = rules[0].get("properties", {})
    sources = rule.get("sourceAddressPrefixes") or [rule.get("sourceAddressPrefix")]
    kind = (rule.get("direction"), rule.get("access"), rule.get("protocol"))
    return (
        kind == ("Inbound", "Allow", "Tcp")
        and rule.get("destinationPortRange") == str(PORT)
        and not rule.get("destinationPortRanges")
        and sources == [cidr]
    )


def throwaway_ssh_key() -> str:
    """A new SSH public key (OpenSSH format) whose private key is thrown
    away: Azure needs one for a Linux VM, and nobody signs in."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    key = ed25519.Ed25519PrivateKey.generate().public_key()
    return key.public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    ).decode()


def _network_profile(name: str, paths: Paths) -> dict[str, Any]:
    """A network interface in the subnet, with the network security group
    and a Standard static public IP address, both deleted with the VM."""
    address = {
        "name": f"{name}-ip",
        "sku": {"name": "Standard", "tier": "Regional"},
        "properties": {"deleteOption": "Delete", "publicIPAllocationMethod": "Static"},
    }
    configuration = {
        "name": "ipconfig1",
        "properties": {
            "primary": True,
            "subnet": {"id": paths.subnet},
            "publicIPAddressConfiguration": address,
        },
    }
    return {
        "networkApiVersion": "2022-11-01",
        "networkInterfaceConfigurations": [
            {
                "name": f"{name}-nic",
                "properties": {
                    "primary": True,
                    "deleteOption": "Delete",
                    "networkSecurityGroup": {"id": paths.security_group},
                    "ipConfigurations": [configuration],
                },
            }
        ],
    }


def vm_request(target: AzureTarget, machine_id: str, custom_data: bytes, ssh_key: str) -> dict:
    """The VM's ``PUT`` body: the chosen size, the Ubuntu LTS image, a
    Premium SSD OS disk of the chosen size, its network interface and public
    address (all three deleted with the VM), a system-assigned identity (it
    gets the role that deallocates the VM), Trusted Launch, boot diagnostics
    (the serial log shows the progress), the tags, and ``custom_data``
    (cloud-init's user data)."""
    name = NAME_PREFIX + machine_id
    linux = {
        "disablePasswordAuthentication": True,
        "ssh": {
            "publicKeys": [{"path": f"/home/{ADMIN_USER}/.ssh/authorized_keys", "keyData": ssh_key}]
        },
    }
    os_disk = {
        "name": f"{name}-disk",
        "createOption": "FromImage",
        "diskSizeGB": target.disk_gb,
        "managedDisk": {"storageAccountType": DISK_TYPE},
        "deleteOption": "Delete",
    }
    return {
        "location": target.region,
        "tags": _tags(machine_id),
        "identity": {"type": "SystemAssigned"},
        "properties": {
            "hardwareProfile": {"vmSize": target.machine_type},
            "storageProfile": {
                "imageReference": OS_IMAGE,
                "osDisk": os_disk,
                "diskControllerType": "NVMe",
            },
            "osProfile": {
                "computerName": name,
                "adminUsername": ADMIN_USER,
                "customData": base64.b64encode(custom_data).decode(),
                "linuxConfiguration": linux,
            },
            "securityProfile": {
                "securityType": "TrustedLaunch",
                "uefiSettings": {"secureBootEnabled": True, "vTpmEnabled": True},
            },
            "diagnosticsProfile": {"bootDiagnostics": {"enabled": True}},
            "networkProfile": _network_profile(name, Paths(target.subscription)),
        },
    }


def role_assignment_id(vm_id: str, machine_id: str) -> str:
    """The path of the role assignment that lets the VM ``vm_id`` deallocate
    itself. Its name is a GUID made from the VM and the machine id, so a
    leftover VM's assignment can be found without listing."""
    name = uuid.uuid5(uuid.NAMESPACE_URL, f"{vm_id.lower()}#{machine_id}")
    return f"{vm_id}/providers/Microsoft.Authorization/roleAssignments/{name}"


def power_role_body(paths: Paths, principal_id: str) -> dict[str, Any]:
    """The role assignment of :data:`POWER_ROLE` to a VM's identity."""
    return {
        "properties": {
            "roleDefinitionId": paths.power_role,
            "principalId": principal_id,
            "principalType": "ServicePrincipal",
        }
    }


def vm_states(vm: dict[str, Any]) -> tuple[str, str]:
    """A VM's provisioning state (e.g. ``Succeeded``) and power state (e.g.
    ``running``, ``deallocated``; ``""`` if the instance view has none)."""
    properties = vm.get("properties", {})
    statuses = (properties.get("instanceView") or {}).get("statuses") or []
    codes = [s.get("code", "") for s in statuses]
    power = next((c.split("/", 1)[1] for c in codes if c.startswith("PowerState/")), "")
    return properties.get("provisioningState", ""), power


def stop_time(vm: dict[str, Any]) -> datetime.datetime | None:
    """When a VM (with its instance view) stopped costing compute: once it is
    deallocated, the time of its provisioning status, which the deallocation
    set; ``None`` while it runs or deallocates. A VM that only powered off
    stays allocated and costs compute until it is deleted: then the current
    time."""
    power = vm_states(vm)[1]
    if power == "stopped":
        return datetime.datetime.now(datetime.timezone.utc)
    if power != "deallocated":
        return None
    statuses = (vm.get("properties", {}).get("instanceView") or {}).get("statuses") or []
    times = [s.get("time") for s in statuses if s.get("code", "").startswith("ProvisioningState/")]
    return utc_time(times[0]) if times else None


def failure_message(vm: dict[str, Any]) -> str:
    """Why a VM's provisioning failed (from its instance view)."""
    statuses = (vm.get("properties", {}).get("instanceView") or {}).get("statuses") or []
    errors = [s for s in statuses if s.get("level") == "Error"]
    texts = [s.get("message") or s.get("displayStatus") or s.get("code") or "" for s in errors]
    return "; ".join(texts)
