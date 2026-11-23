"""The EC2, SSM and CloudWatch calls of the AWS provider, in one region.

:class:`Ec2` wraps ``boto3`` clients (made by :class:`BotoClients` from the
AWS CLI's profile) and turns their errors into
:class:`~orangecontrib.cloud.client.providers.base.ProviderError` with a
message for the user (:class:`NotFoundError` for a resource that doesn't
exist). Tests give it fake clients. Every call goes to AWS, so the provider
makes them off the GUI thread.
"""

from __future__ import annotations

import datetime
import threading
from typing import Any

from orangecontrib.cloud.client.providers.aws_account import (
    aws_error,
    client,
    error_code,
    load_session,
)
from orangecontrib.cloud.client.providers.aws_vm import (
    AMI_PARAMETER,
    SECURITY_GROUP,
    TAG,
    TAG_SESSION,
    ingress_rule,
    security_group_request,
)
from orangecontrib.cloud.client.providers.base import ProviderError

__all__ = ["LIVE_STATES", "BotoClients", "Ec2", "NotFoundError"]

#: Instance states of machines that exist (not terminated or terminating).
LIVE_STATES = ("pending", "running", "stopping", "stopped")
#: Seconds per value of EC2's basic monitoring (free, for every instance).
METRICS_PERIOD = 300
_NOT_FOUND = {"InvalidInstanceID.NotFound", "InvalidGroup.NotFound"}


class NotFoundError(ProviderError):
    """The instance or security group doesn't exist."""


class BotoClients:
    """Makes ``boto3`` clients for a profile, service and region, once each
    (making one is slow, and a ``boto3`` session can't make them on several
    threads at once)."""

    def __init__(self) -> None:
        self._sessions: dict[str, Any] = {}
        self._clients: dict[tuple[str, str, str], Any] = {}
        self._lock = threading.Lock()

    def __call__(self, profile: str, service: str, region: str) -> Any:
        """The client of ``service`` in ``region`` with ``profile``'s credentials."""
        key = (profile, service, region)
        with self._lock:
            if key not in self._clients:
                if profile not in self._sessions:
                    self._sessions[profile] = load_session(profile)
                self._clients[key] = client(self._sessions[profile], service, region)
            return self._clients[key]


class Ec2:
    """The provider's calls in one region, with clients of ``ec2``, ``ssm``
    and ``cloudwatch`` (``profile``: for messages)."""

    def __init__(self, ec2: Any, ssm: Any, profile: str = "", cloudwatch: Any = None):
        self.ec2 = ec2
        self.ssm = ssm
        self.cloudwatch = cloudwatch
        self.profile = profile
        self._vpc: str | None = None

    def _call(self, api: Any, operation: str, what: str, **kwargs: Any) -> dict[str, Any]:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return getattr(api, operation)(**kwargs)
        except ClientError as exc:
            if error_code(exc) in _NOT_FOUND:
                raise NotFoundError(f"Couldn't {what}: it doesn't exist.") from exc
            raise aws_error(exc, what, self.profile) from exc
        except BotoCoreError as exc:
            raise aws_error(exc, what, self.profile) from exc

    # ------------------------------------------------------------------
    # Image and network

    def image(self) -> tuple[str, str]:
        """The region's machine image (public SSM parameter) and its root device."""
        what = "find the machine image"
        image_id = self._call(self.ssm, "get_parameter", what, Name=AMI_PARAMETER)
        image_id = image_id["Parameter"]["Value"]
        images = self._call(self.ec2, "describe_images", what, ImageIds=[image_id])["Images"]
        if not images:
            raise ProviderError(f"Couldn't {what}: {image_id} isn't available.")
        return image_id, images[0].get("RootDeviceName", "/dev/xvda")

    def default_vpc(self) -> str:
        """The region's default VPC, where machines start."""
        if self._vpc is None:
            vpcs = self._call(
                self.ec2,
                "describe_vpcs",
                "find the default VPC",
                Filters=[{"Name": "isDefault", "Values": ["true"]}],
            )["Vpcs"]
            if not vpcs:
                raise ProviderError(
                    "The region has no default VPC, which machines start in. "
                    "Create one in the VPC console, or choose another region."
                )
            self._vpc = vpcs[0]["VpcId"]
        return self._vpc

    def security_group(self) -> dict[str, Any] | None:
        """The add-on's security group in the default VPC (``None``: none)."""
        groups = self._call(
            self.ec2,
            "describe_security_groups",
            "read the security group",
            Filters=[
                {"Name": "vpc-id", "Values": [self.default_vpc()]},
                {"Name": "group-name", "Values": [SECURITY_GROUP]},
                {"Name": f"tag:{TAG}", "Values": ["true"]},
            ],
        )["SecurityGroups"]
        return groups[0] if groups else None

    def create_security_group(self) -> str:
        """Create the add-on's security group (no rules); return its ID."""
        request = security_group_request(self.default_vpc())
        reply = self._call(
            self.ec2, "create_security_group", "create the security group", **request
        )
        return reply["GroupId"]

    def authorize(self, group_id: str, cidr: str) -> None:
        """Allow TCP 8443 from ``cidr``."""
        self._call(
            self.ec2,
            "authorize_security_group_ingress",
            "update the security group",
            GroupId=group_id,
            IpPermissions=[ingress_rule(cidr)],
        )

    def revoke(self, group_id: str, permissions: list[dict[str, Any]]) -> None:
        """Remove ingress ``permissions``."""
        self._call(
            self.ec2,
            "revoke_security_group_ingress",
            "update the security group",
            GroupId=group_id,
            IpPermissions=permissions,
        )

    def delete_security_group(self, group_id: str) -> None:
        """Delete the security group (no machine may use it)."""
        self._call(self.ec2, "delete_security_group", "delete the security group", GroupId=group_id)

    # ------------------------------------------------------------------
    # Instances

    def run_instance(self, request: dict[str, Any]) -> dict[str, Any]:
        """Start an instance from a ``RunInstances`` request; return it."""
        return self._call(self.ec2, "run_instances", "create the machine", **request)["Instances"][
            0
        ]

    def instance(self, instance_id: str) -> dict[str, Any] | None:
        """The instance (``None`` if AWS doesn't know it, e.g. yet)."""
        try:
            reply = self._call(
                self.ec2, "describe_instances", "read the machine", InstanceIds=[instance_id]
            )
        except NotFoundError:
            return None
        found = [i for r in reply.get("Reservations", []) for i in r.get("Instances", [])]
        return found[0] if found else None

    def instances(
        self, states: tuple[str, ...] = LIVE_STATES, session: str | None = None
    ) -> list[dict[str, Any]]:
        """The add-on's instances (tagged ``orange3-cloud=true``) in ``states``;
        with ``session``, only those of that machine ID."""
        filters = [
            {"Name": f"tag:{TAG}", "Values": ["true"]},
            {"Name": "instance-state-name", "Values": list(states)},
        ]
        if session is not None:
            filters.append({"Name": f"tag:{TAG_SESSION}", "Values": [session]})
        found: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {"Filters": filters}
        while True:
            reply = self._call(self.ec2, "describe_instances", "list the machines", **kwargs)
            found += [i for r in reply.get("Reservations", []) for i in r.get("Instances", [])]
            if not reply.get("NextToken"):
                return found
            kwargs["NextToken"] = reply["NextToken"]

    def stop(self, instance_id: str) -> None:
        """Ask the instance to stop, without waiting."""
        self._call(self.ec2, "stop_instances", "stop the machine", InstanceIds=[instance_id])

    def terminate(self, instance_ids: list[str]) -> None:
        """Ask the instances to terminate (with their boot volumes), without waiting."""
        self._call(self.ec2, "terminate_instances", "delete the machine", InstanceIds=instance_ids)

    def console(self, instance_id: str) -> str:
        """The latest console output of the instance (it lags a minute)."""
        reply = self._call(
            self.ec2,
            "get_console_output",
            "read the machine's output",
            InstanceId=instance_id,
            Latest=True,
        )
        return reply.get("Output") or ""

    def metrics_end(self, instance_id: str, since: datetime.datetime) -> datetime.datetime | None:
        """The end of the last 5-minute period since ``since`` in which the
        instance ran, from its CPU metric in EC2's basic monitoring
        (CloudWatch); ``None`` if it has no values."""
        if self.cloudwatch is None:
            return None
        start = since.astimezone(datetime.timezone.utc).replace(second=0, microsecond=0)
        start -= datetime.timedelta(minutes=start.minute % 5)  # whole periods
        cpu = {
            "Namespace": "AWS/EC2",
            "MetricName": "CPUUtilization",
            "Dimensions": [{"Name": "InstanceId", "Value": instance_id}],
        }
        reply = self._call(
            self.cloudwatch,
            "get_metric_data",
            "read the machine's metrics",
            MetricDataQueries=[
                {
                    "Id": "cpu",
                    "MetricStat": {"Metric": cpu, "Period": METRICS_PERIOD, "Stat": "Maximum"},
                }
            ],
            StartTime=start,
            EndTime=datetime.datetime.now(datetime.timezone.utc),
            ScanBy="TimestampDescending",
        )
        results = reply.get("MetricDataResults") or [{}]
        times = results[0].get("Timestamps") or []
        if not times:
            return None
        end = times[0] + datetime.timedelta(seconds=METRICS_PERIOD)
        return end.astimezone(datetime.timezone.utc)
