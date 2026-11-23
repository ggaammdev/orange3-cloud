"""What a machine on AWS is made of: its sizes, the fixed names of what the
add-on creates there, and the EC2 requests. No API calls (see
:mod:`~orangecontrib.cloud.client.providers.aws_ec2`,
:mod:`~orangecontrib.cloud.client.providers.aws_account` and
:mod:`~orangecontrib.cloud.client.providers.aws_setup`).
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import Any

from orangecontrib.cloud.client.providers.base import MachineType, utc_time

__all__ = [
    "AMI_PARAMETER",
    "DEFAULT_MACHINE_TYPE",
    "IMAGE_VAR",
    "MACHINE_TYPES",
    "NAME_PREFIX",
    "PORT",
    "SECURITY_GROUP",
    "SECURITY_GROUP_DESCRIPTION",
    "TAG",
    "TAG_SESSION",
    "USER_DATA_LIMIT",
    "VCPU_QUOTA_CODE",
    "VCPU_QUOTA_NAME",
    "VOLUME_TYPE",
    "Ami",
    "Ec2Target",
    "allows",
    "ingress_rule",
    "run_instances_request",
    "security_group_request",
    "stale_permissions",
    "stop_time",
    "tag_value",
]

#: Machine sizes on AWS, from smallest to largest memory: the same names as
#: on Google Cloud, with the closest EC2 instance types. They are fixed for
#: the add-on (x86-64 only, no GPUs). EC2's memory is GiB.
MACHINE_TYPES: tuple[MachineType, ...] = (
    MachineType("m7i.xlarge", "Small (testing)", 4, 16),
    MachineType("m7i.4xlarge", "Medium", 16, 64),
    MachineType("r7i.4xlarge", "Medium, high memory", 16, 128),
    MachineType("m7i.8xlarge", "Large", 32, 128),
    MachineType("r7i.8xlarge", "Large, high memory", 32, 256),
    MachineType("m7i.16xlarge", "Extra large", 64, 256),
    MachineType("r7i.16xlarge", "Extra large, high memory", 64, 512),
    MachineType("m7i.24xlarge", "Huge", 96, 384),
    MachineType("r7i.24xlarge", "Huge, high memory", 96, 768),
)
DEFAULT_MACHINE_TYPE = "m7i.4xlarge"

#: Tags of the add-on's instances, volumes and security group:
#: ``TAG=true`` and (instances and volumes) ``TAG_SESSION=<machine id>``.
TAG = "orange3-cloud"
TAG_SESSION = "orange3-cloud-session"
#: Prefix of the instances' ``Name`` tags; the rest is the machine id.
NAME_PREFIX = "orange3-cloud-"
#: The security group that lets this computer reach the machines.
SECURITY_GROUP = "orange3-cloud-https"
#: Its description (EC2 allows no apostrophes there).
SECURITY_GROUP_DESCRIPTION = (
    "orange3-cloud: HTTPS to the add-on machines, only from your computer address"
)
PORT = 8443
#: The public SSM parameter with the region's ECS-optimized Amazon Linux 2023
#: image (it has Docker).
AMI_PARAMETER = "/aws/service/ecs/optimized-ami/amazon-linux-2023/recommended/image_id"
#: The boot volume's type.
VOLUME_TYPE = "gp3"
#: EC2's limit of user data, before base64.
USER_DATA_LIMIT = 16 * 1024
#: Overrides the machine image (e.g. a development image ``:dev-<sha>``).
IMAGE_VAR = "ORANGE3_CLOUD_AWS_IMAGE"
#: The regional vCPU quota of all the sizes (Service Quotas, service ``ec2``).
VCPU_QUOTA_CODE = "L-1216C47A"
VCPU_QUOTA_NAME = "Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances"


@dataclass(frozen=True)
class Ec2Target:
    """Where and how big a new machine is."""

    #: The AWS CLI's profile it is started with.
    profile: str
    region: str
    machine_type: str
    disk_gb: int


@dataclass(frozen=True)
class Ami:
    """The operating system image of new machines."""

    image_id: str
    #: Its root device, e.g. ``/dev/xvda``, which gets the boot volume.
    root_device: str


def _tags(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"Key": key, "Value": value} for key, value in pairs]


def run_instances_request(
    target: Ec2Target, name: str, machine_id: str, ami: Ami, group_id: str, user_data: bytes
) -> dict[str, Any]:
    """The ``RunInstances`` request of a machine: the default subnet with a
    public address and the security group, a ``gp3`` boot volume deleted
    with the instance, IMDSv2 only, and an instance that stops (not
    terminates) when it powers off. The machine's ID is the request's
    idempotency token, so a retried request doesn't start a second instance."""
    tags = _tags(("Name", name), (TAG, "true"), (TAG_SESSION, machine_id))
    return {
        "ClientToken": machine_id,
        "ImageId": ami.image_id,
        "InstanceType": target.machine_type,
        "MinCount": 1,
        "MaxCount": 1,
        "UserData": user_data,
        "NetworkInterfaces": [
            {
                "DeviceIndex": 0,
                "AssociatePublicIpAddress": True,
                "Groups": [group_id],
                "DeleteOnTermination": True,
            }
        ],
        "BlockDeviceMappings": [
            {
                "DeviceName": ami.root_device,
                "Ebs": {
                    "VolumeSize": target.disk_gb,
                    "VolumeType": VOLUME_TYPE,
                    "DeleteOnTermination": True,
                },
            }
        ],
        "MetadataOptions": {
            "HttpTokens": "required",
            "HttpEndpoint": "enabled",
            "HttpPutResponseHopLimit": 1,
        },
        "InstanceInitiatedShutdownBehavior": "stop",
        "TagSpecifications": [
            {"ResourceType": "instance", "Tags": tags},
            {"ResourceType": "volume", "Tags": tags},
        ],
    }


def security_group_request(vpc_id: str) -> dict[str, Any]:
    """The ``CreateSecurityGroup`` request (no rules yet)."""
    return {
        "GroupName": SECURITY_GROUP,
        "Description": SECURITY_GROUP_DESCRIPTION,
        "VpcId": vpc_id,
        "TagSpecifications": [
            {
                "ResourceType": "security-group",
                "Tags": _tags((TAG, "true"), ("Name", SECURITY_GROUP)),
            }
        ],
    }


def ingress_rule(cidr: str) -> dict[str, Any]:
    """The security group's one rule: TCP :data:`PORT` from ``cidr``."""
    return {"IpProtocol": "tcp", "FromPort": PORT, "ToPort": PORT, "IpRanges": [{"CidrIp": cidr}]}


def stale_permissions(permissions: list[dict[str, Any]], cidr: str) -> list[dict[str, Any]]:
    """The group's ingress ``permissions`` (as ``DescribeSecurityGroups``
    gives them) other than TCP :data:`PORT` from ``cidr``, as a
    ``RevokeSecurityGroupIngress`` request's ``IpPermissions``."""
    stale = []
    for permission in permissions:
        ports = {k: permission[k] for k in ("IpProtocol", "FromPort", "ToPort") if k in permission}
        ours = ports == {"IpProtocol": "tcp", "FromPort": PORT, "ToPort": PORT}
        ranges = [
            {"CidrIp": r["CidrIp"]}
            for r in permission.get("IpRanges", [])
            if not (ours and r.get("CidrIp") == cidr)
        ]
        others = {
            key: permission[key]
            for key in ("Ipv6Ranges", "PrefixListIds", "UserIdGroupPairs")
            if permission.get(key)
        }
        if ranges or others:
            stale.append({**ports, **({"IpRanges": ranges} if ranges else {}), **others})
    return stale


def allows(permissions: list[dict[str, Any]], cidr: str) -> bool:
    """Whether the group's ingress ``permissions`` include TCP :data:`PORT`
    from ``cidr``."""
    wanted = ingress_rule(cidr)
    return any(
        all(p.get(k) == wanted[k] for k in ("IpProtocol", "FromPort", "ToPort"))
        and any(r.get("CidrIp") == cidr for r in p.get("IpRanges", []))
        for p in permissions
    )


def tag_value(resource: dict[str, Any], key: str) -> str:
    """The value of tag ``key`` of an EC2 resource (``""`` if it has none)."""
    return next((t.get("Value", "") for t in resource.get("Tags", []) if t.get("Key") == key), "")


#: The time in a stopped instance's ``StateTransitionReason``.
_REASON_TIME = re.compile(r"\((\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) GMT\)")


def stop_time(instance: dict[str, Any]) -> datetime.datetime | None:
    """When an instance was stopped, from its ``StateTransitionReason``
    (``User initiated (2026-11-23 10:15:00 GMT)`` after a stop request);
    ``None`` while it runs, and after it powered itself off, when EC2 writes
    "User initiated" without a time."""
    if instance.get("State", {}).get("Name") not in ("stopping", "stopped"):
        return None
    match = _REASON_TIME.search(instance.get("StateTransitionReason") or "")
    return utc_time(match.group(1)) if match else None
