"""What a machine on Google Cloud is made of: its sizes, the VM and firewall
requests, the machine image and the cloud-init that runs it. No API calls
(see :mod:`~orangecontrib.cloud.client.providers.gcp`).
"""

from __future__ import annotations

import datetime
import os
import shlex
from collections.abc import Iterable
from dataclasses import dataclass

from orangecontrib.cloud.client.providers.base import (
    PUBLIC_IMAGE,
    MachineType,
    is_release,
    utc_time,
)

__all__ = [
    "DEFAULT_MACHINE_TYPE",
    "FIREWALL_RULE",
    "LABEL",
    "LABEL_SESSION",
    "MACHINE_TYPES",
    "NETWORK_TAG",
    "MachineTarget",
    "cloud_init",
    "external_ip",
    "firewall_body",
    "instance_body",
    "is_release",
    "machine_image",
    "stop_time",
]

#: Machine sizes on Google Cloud, from smallest to largest memory. They are
#: fixed for the add-on (x86-64 machines only, no GPUs). Google Cloud's "GB"
#: of memory are GiB.
MACHINE_TYPES: tuple[MachineType, ...] = (
    MachineType("e2-standard-4", "Small (testing)", 4, 16),
    MachineType("n2-standard-16", "Medium", 16, 64),
    MachineType("n2-highmem-16", "Medium, high memory", 16, 128),
    MachineType("n2-standard-32", "Large", 32, 128),
    MachineType("n2-highmem-32", "Large, high memory", 32, 256),
    MachineType("n2d-standard-64", "Extra large", 64, 256),
    MachineType("n2-highmem-64", "Extra large, high memory", 64, 512),
    MachineType("c3-standard-88", "Huge", 88, 352),
    MachineType("n2-highmem-128", "Huge, high memory", 128, 864),
)
DEFAULT_MACHINE_TYPE = "n2-standard-16"

LABEL = "orange3-cloud"
LABEL_SESSION = "orange3-cloud-session"
NETWORK_TAG = "orange3-cloud"
FIREWALL_RULE = "orange3-cloud-https"
NETWORK = "default"
PORT = 8443
#: The VM's operating system.
OS_IMAGE = "projects/cos-cloud/global/images/family/cos-stable"
DISK_TYPE = "pd-balanced"
#: The VM's access: pull images from Artifact Registry and read buckets.
SCOPES = ("https://www.googleapis.com/auth/devstorage.read_only",)
#: Artifact Registry repository of development images (in the user's project).
DEV_REPOSITORY = "orange3-cloud"
#: Name of the container and the systemd unit on the VM.
UNIT = "orange3-cloud"
#: Prefix of VM names; the rest is the machine id.
NAME_PREFIX = "orange3-cloud-"


@dataclass
class MachineTarget:
    """Where and how big a new machine is."""

    project: str
    zone: str
    machine_type: str
    disk_gb: int

    @property
    def region(self) -> str:
        """The zone's region, e.g. ``us-central1`` for ``us-central1-a``."""
        return self.zone.rsplit("-", 1)[0]


def machine_image(version: str, project: str, region: str) -> str:
    """The machine image for add-on ``version``: ``ORANGE3_CLOUD_GCP_IMAGE`` if
    set, the public image of a release, else the private development image
    ``:dev`` in ``project``'s Artifact Registry in ``region``."""
    override = os.environ.get("ORANGE3_CLOUD_GCP_IMAGE", "").strip()
    if override:
        return override
    if is_release(version):
        return f"{PUBLIC_IMAGE}:{version}"
    return f"{region}-docker.pkg.dev/{project}/{DEV_REPOSITORY}/machine:dev"


def _registry(image: str) -> str | None:
    """The Google registry host of ``image`` that needs the VM's credentials."""
    host = image.split("/", 1)[0]
    if host.endswith("-docker.pkg.dev") or host == "gcr.io" or host.endswith(".gcr.io"):
        return host
    return None


def cloud_init(image: str) -> str:
    """The ``user-data`` that runs ``image`` on Container-Optimized OS.

    A systemd unit opens port 8443 in the host's firewall, signs Docker in to
    Google's registry with the VM's service account (only for images there),
    pulls the image (with retries), and runs it with the host's network, so
    the bootstrap reads its configuration from the metadata server. Its output
    goes to the serial console, where the add-on reads the progress. When the
    container exits for any reason, the VM powers off.
    """
    q = shlex.quote(image)
    pre = [
        "/bin/mkdir -p /home/orange3-cloud",
        "/bin/sh -c 'iptables -w -C INPUT -p tcp --dport 8443 -j ACCEPT 2>/dev/null"
        " || iptables -w -A INPUT -p tcp --dport 8443 -j ACCEPT'",
        f"-/usr/bin/docker rm --force {UNIT}",
    ]
    registry = _registry(image)
    if registry:
        pre.append(f"/usr/bin/docker-credential-gcr configure-docker --registries={registry}")
    pre.append(
        '/bin/sh -c \'for i in 1 2 3 4 5; do echo "[orange3-cloud] pulling the machine image";'
        f" /usr/bin/docker pull --quiet {q} && exit 0; sleep 10; done; exit 1'"
    )
    run = (
        f"/usr/bin/docker run --rm --init --name {UNIT} --network host"
        " --env ORANGE3_CLOUD_CONFIG=gce --env ORANGE3_CLOUD_POWER_OFF=host"
        f" {q}"
    )
    unit = "\n".join(
        [
            "[Unit]",
            "Description=orange3-cloud machine",
            "Wants=network-online.target docker.service",
            "After=network-online.target docker.service",
            "SuccessAction=poweroff",
            "FailureAction=poweroff",
            "",
            "[Service]",
            "Environment=HOME=/home/orange3-cloud",
            "StandardOutput=journal+console",
            "StandardError=journal+console",
            *(f"ExecStartPre={line}" for line in pre),
            f"ExecStart={run}",
            f"ExecStop=-/usr/bin/docker stop --time 40 {UNIT}",
            "TimeoutStartSec=infinity",
            "TimeoutStopSec=60",
        ]
    )
    content = "\n".join("    " + line if line else "" for line in unit.splitlines())
    return (
        "#cloud-config\n"
        "write_files:\n"
        f"- path: /etc/systemd/system/{UNIT}.service\n"
        '  permissions: "0644"\n'
        "  owner: root\n"
        "  content: |\n"
        f"{content}\n"
        "runcmd:\n"
        "- systemctl daemon-reload\n"
        f"- systemctl start --no-block {UNIT}.service\n"
    )


def instance_body(
    name: str,
    machine_id: str,
    target: MachineTarget,
    metadata: dict[str, str],
    service_account: str,
) -> dict:
    """The ``instances.insert`` request for a machine."""
    zone = target.zone
    return {
        "name": name,
        "description": "orange3-cloud machine (deleted when you stop it in Orange)",
        "machineType": f"zones/{zone}/machineTypes/{target.machine_type}",
        "labels": {LABEL: "true", LABEL_SESSION: machine_id},
        "tags": {"items": [NETWORK_TAG]},
        "disks": [
            {
                "boot": True,
                "autoDelete": True,
                "initializeParams": {
                    "sourceImage": OS_IMAGE,
                    "diskSizeGb": str(target.disk_gb),
                    "diskType": f"zones/{zone}/diskTypes/{DISK_TYPE}",
                    "labels": {LABEL: "true", LABEL_SESSION: machine_id},
                },
            }
        ],
        "networkInterfaces": [
            {
                "network": f"global/networks/{NETWORK}",
                "accessConfigs": [{"name": "External NAT", "type": "ONE_TO_ONE_NAT"}],
            }
        ],
        "metadata": {"items": [{"key": k, "value": v} for k, v in metadata.items()]},
        "serviceAccounts": [{"email": service_account, "scopes": list(SCOPES)}],
        "scheduling": {"automaticRestart": False, "onHostMaintenance": "MIGRATE"},
    }


def firewall_body(source_ranges: Iterable[str]) -> dict:
    """The firewall rule: HTTPS to the add-on's machines from ``source_ranges``."""
    return {
        "name": FIREWALL_RULE,
        "description": "orange3-cloud: the add-on's machines, only from your computer's address",
        "network": f"global/networks/{NETWORK}",
        "direction": "INGRESS",
        "priority": 1000,
        "allowed": [{"IPProtocol": "tcp", "ports": [str(PORT)]}],
        "sourceRanges": list(source_ranges),
        "targetTags": [NETWORK_TAG],
    }


def external_ip(instance: dict) -> str:
    """The instance's external IP address (``""`` if it has none)."""
    for interface in instance.get("networkInterfaces", []):
        for access in interface.get("accessConfigs", []):
            if access.get("natIP"):
                return access["natIP"]
    return ""


def stop_time(instance: dict) -> datetime.datetime | None:
    """When a stopped VM stopped: its ``lastStopTimestamp`` (``None`` while it
    runs, or if Google hasn't set it yet)."""
    if instance.get("status") not in ("STOPPING", "TERMINATED"):
        return None
    return utc_time(instance.get("lastStopTimestamp"))
