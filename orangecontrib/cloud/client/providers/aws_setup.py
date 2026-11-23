"""The setup checker for AWS: what's missing in the user's AWS account before
a machine can start there.

:func:`run_checks` checks, in order: the sign-in, the region, the region's
default VPC, the user's permissions for machines, security groups and
storage, and the vCPU quota for the chosen size in the region. Each
:class:`~orangecontrib.cloud.client.providers.setup_checks.Check` says
whether it passed, what was found, how to fix it and where (a link to the
AWS console or its docs).

The permissions are tried, not read from policies: EC2 calls with
``DryRun`` (they change nothing and answer whether they would have been
allowed), and reads for the rest. Stopping and deleting instances and
reading their console can't be tried without an instance, and S3 has no dry
run, so those (:data:`SIMULATED`) are asked of IAM's policy simulator
(``iam:SimulatePrincipalPolicy`` for the signed-in user or role). Without
the right to use it, they are listed as not checked, and the check still
passes.

AWS is called through a :class:`SetupApi` (``boto3`` in
:class:`BotoSetupApi`), so tests can replace it with a fake account. Every
call may be slow, so the dialog runs the checks off the GUI thread.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from orangecontrib.cloud.client.providers.aws_account import (
    DENIED_CODES,
    SIGN_IN_BUTTON,
    Identity,
    NotSignedInError,
    aws_error,
    client,
    error_code,
    identity,
    list_regions,
)
from orangecontrib.cloud.client.providers.aws_vm import (
    AMI_PARAMETER,
    MACHINE_TYPES,
    PORT,
    SECURITY_GROUP,
    TAG,
    VCPU_QUOTA_CODE,
    VCPU_QUOTA_NAME,
)
from orangecontrib.cloud.client.providers.base import ProviderError
from orangecontrib.cloud.client.providers.setup_checks import Check, Status, not_checked

__all__ = [
    "PERMISSIONS",
    "TITLES",
    "BotoSetupApi",
    "DefaultVpc",
    "NotCheckedError",
    "NotSimulatedError",
    "SIMULATED",
    "SetupApi",
    "VcpuQuota",
    "counts_against_quota",
    "run_checks",
]

log = logging.getLogger(__name__)

#: The permissions the add-on needs, by what they are for.
PERMISSIONS: dict[str, tuple[str, ...]] = {
    "machines": (
        "ssm:GetParameter",
        "ec2:RunInstances",
        "ec2:CreateTags",
        "ec2:DescribeInstances",
        "ec2:StopInstances",
        "ec2:TerminateInstances",
        "ec2:GetConsoleOutput",
    ),
    "security groups": (
        "ec2:DescribeSecurityGroups",
        "ec2:CreateSecurityGroup",
        "ec2:AuthorizeSecurityGroupIngress",
        "ec2:RevokeSecurityGroupIngress",
        "ec2:DeleteSecurityGroup",
    ),
    "storage": ("s3:ListAllMyBuckets", "s3:CreateBucket", "s3:PutObject"),
}

#: The permissions asked of IAM's policy simulator: they can't be tried
#: without an instance (EC2 checks it exists first) or a dry run (S3).
SIMULATED = (
    "ec2:StopInstances",
    "ec2:TerminateInstances",
    "ec2:GetConsoleOutput",
    "s3:CreateBucket",
    "s3:PutObject",
)

#: Each check's key and title, in order.
TITLES: dict[str, str] = {
    "credentials": "Signed in to AWS",
    "region": "Region enabled",
    "vpc": "Default VPC in the region",
    "permissions": "Permissions for machines, security groups and storage",
    "quota": "vCPU quota in the region",
}

_SIGN_IN_DOCS = "https://docs.aws.amazon.com/cli/latest/userguide/cli-chap-authentication.html"
_REGION_DOCS = "https://docs.aws.amazon.com/accounts/latest/reference/manage-acct-regions.html"
_VPC_DOCS = "https://docs.aws.amazon.com/vpc/latest/userguide/work-with-default-vpc.html"
_IAM_CONSOLE = "https://console.aws.amazon.com/iam/home"


def _vpc_console(region: str) -> str:
    return f"https://{region}.console.aws.amazon.com/vpcconsole/home?region={region}#vpcs:"


def _quota_console(region: str) -> str:
    return (
        f"https://{region}.console.aws.amazon.com/servicequotas/home/services/ec2/quotas/"
        f"{VCPU_QUOTA_CODE}?region={region}"
    )


@dataclass(frozen=True)
class DefaultVpc:
    """A region's default VPC."""

    vpc_id: str
    #: Its default subnets (one per availability zone, normally).
    subnets: int
    #: Its security group named ``default``, which the permission checks
    #: use with ``DryRun`` (it is never changed).
    security_group: str


@dataclass(frozen=True)
class VcpuQuota:
    """The region's vCPU quota of the add-on's sizes (``L-1216C47A``)."""

    limit: float
    #: vCPUs of the running instances that count against it.
    used: float
    #: The value of an open request to raise it (``None``: none).
    requested: float | None = None


class NotCheckedError(ProviderError):
    """A permission couldn't be tried (e.g. it needs a default VPC); the
    message says why."""


class NotSimulatedError(NotCheckedError):
    """IAM's policy simulator can't be used (no right to, or a principal it
    doesn't know), so a permission of :data:`SIMULATED` wasn't checked; the
    check still passes."""


class SetupApi:
    """The AWS calls the checker makes. Each raises a :class:`ProviderError`
    (:class:`~orangecontrib.cloud.client.providers.aws_account.
    NotSignedInError` if the sign-in no longer works)."""

    def identity(self) -> Identity:
        """Who is signed in."""
        raise NotImplementedError

    def enabled_regions(self) -> list[str]:
        """The regions enabled for the account."""
        raise NotImplementedError

    def default_vpc(self, region: str) -> DefaultVpc | None:
        """The region's default VPC (``None``: there is none)."""
        raise NotImplementedError

    def allows(self, action: str, region: str, machine_type: str, vpc: DefaultVpc | None) -> bool:
        """Whether the user may do ``action`` (one of :data:`PERMISSIONS`) in
        ``region``; raises :class:`NotCheckedError` if it can't be tried."""
        raise NotImplementedError

    def vcpu_quota(self, region: str) -> VcpuQuota:
        """The region's vCPU quota and its use."""
        raise NotImplementedError


#: Families with quotas of their own, although their letter is in
#: "A, C, D, H, I, M, R, T, Z" (DL, HPC, Inf, Trn).
_OTHER_QUOTA_FAMILIES = ("dl", "hpc", "inf", "trn")


def counts_against_quota(instance_type: str) -> bool:
    """Whether instances of ``instance_type`` count against
    :data:`~orangecontrib.cloud.client.providers.aws_vm.VCPU_QUOTA_CODE`."""
    family = instance_type.split(".", 1)[0].lower()
    return family[:1] in set("acdhimrtz") and not family.startswith(_OTHER_QUOTA_FAMILIES)


class BotoSetupApi(SetupApi):
    """:class:`SetupApi` with ``boto3``, using a session from
    :func:`~orangecontrib.cloud.client.providers.aws_account.load_session`."""

    def __init__(self, session: Any):
        self.session = session
        self._images: dict[str, str] = {}
        self._clients: dict[tuple[str, str], Any] = {}
        #: :meth:`allows` may run on several threads, but a boto3 session
        #: can't make clients on several at once.
        self._lock = threading.Lock()
        #: The simulator's answers for :data:`SIMULATED` (asked once), or why
        #: it couldn't answer.
        self._simulated: dict[str, bool] | NotSimulatedError | None = None
        self._simulate_lock = threading.Lock()

    def _client(self, service: str, region: str) -> Any:
        """A client of ``service`` in ``region`` (made once: making one is slow)."""
        key = (service, region)
        with self._lock:
            if key not in self._clients:
                self._clients[key] = client(self.session, service, region)
            return self._clients[key]

    def _error(self, exc: Exception, what: str) -> ProviderError:
        return aws_error(exc, what, self.session.profile_name)

    def identity(self) -> Identity:
        return identity(self.session)

    def enabled_regions(self) -> list[str]:
        return list_regions(self.session)

    def default_vpc(self, region: str) -> DefaultVpc | None:
        from botocore.exceptions import BotoCoreError, ClientError

        ec2 = self._client("ec2", region)
        try:
            vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
            if not vpcs:
                return None
            vpc_id = vpcs[0]["VpcId"]
            in_vpc = {"Name": "vpc-id", "Values": [vpc_id]}
            subnets = ec2.describe_subnets(
                Filters=[in_vpc, {"Name": "default-for-az", "Values": ["true"]}]
            )["Subnets"]
            groups = ec2.describe_security_groups(
                Filters=[in_vpc, {"Name": "group-name", "Values": ["default"]}]
            )["SecurityGroups"]
        except (BotoCoreError, ClientError) as exc:
            raise self._error(exc, f"read the default VPC of {region}") from exc
        return DefaultVpc(vpc_id, len(subnets), groups[0]["GroupId"] if groups else "")

    def allows(self, action: str, region: str, machine_type: str, vpc: DefaultVpc | None) -> bool:
        from botocore.exceptions import BotoCoreError, ClientError

        if action in SIMULATED:
            return self._simulate(action)
        call = self._calls(region, machine_type, vpc)[action]
        try:
            call()
        except ClientError as exc:
            code = error_code(exc)
            if code == "DryRunOperation":
                return True
            if code in DENIED_CODES:
                return False
            error = self._error(exc, f"try {action}")
            if isinstance(error, NotSignedInError):
                raise error from exc
            raise NotCheckedError(str(error)) from exc
        except BotoCoreError as exc:
            raise self._error(exc, f"try {action}") from exc
        return True  # a read that worked

    def _calls(
        self, region: str, machine_type: str, vpc: DefaultVpc | None
    ) -> dict[str, Callable[[], Any]]:
        """For each permission, a call that tries it (EC2 with ``DryRun``)."""
        ec2, s3 = self._client("ec2", region), self._client("s3", region)

        def in_vpc(call: Callable[..., Any], **kwargs: Any) -> Callable[[], Any]:
            def run() -> Any:
                if vpc is None or not vpc.security_group:
                    raise NotCheckedError("the region has no default VPC")
                return call(DryRun=True, **kwargs)

            return run

        group = vpc.security_group if vpc else ""
        rule = [
            {
                "IpProtocol": "tcp",
                "FromPort": PORT,
                "ToPort": PORT,
                "IpRanges": [{"CidrIp": "192.0.2.1/32"}],  # a documentation address
            }
        ]
        tags = [
            {"ResourceType": kind, "Tags": [{"Key": TAG, "Value": "true"}]}
            for kind in ("instance", "volume")
        ]
        return {
            "ssm:GetParameter": lambda: self._image_id(region),
            "ec2:RunInstances": lambda: self._run_instance(ec2, region, machine_type, vpc),
            "ec2:CreateTags": lambda: self._run_instance(ec2, region, machine_type, vpc, tags),
            "ec2:DescribeInstances": lambda: ec2.describe_instances(DryRun=True, MaxResults=5),
            "ec2:DescribeSecurityGroups": lambda: ec2.describe_security_groups(
                DryRun=True, MaxResults=5
            ),
            "ec2:CreateSecurityGroup": in_vpc(
                ec2.create_security_group,
                GroupName=SECURITY_GROUP,
                Description="orange3-cloud machines",
                VpcId=vpc.vpc_id if vpc else "",
            ),
            "ec2:AuthorizeSecurityGroupIngress": in_vpc(
                ec2.authorize_security_group_ingress, GroupId=group, IpPermissions=rule
            ),
            "ec2:RevokeSecurityGroupIngress": in_vpc(
                ec2.revoke_security_group_ingress, GroupId=group, IpPermissions=rule
            ),
            "ec2:DeleteSecurityGroup": in_vpc(ec2.delete_security_group, GroupId=group),
            "s3:ListAllMyBuckets": lambda: s3.list_buckets(MaxBuckets=1),
        }

    def _simulate(self, action: str) -> bool:
        """IAM's policy simulator's answer for ``action`` (one of :data:`SIMULATED`)."""
        with self._simulate_lock:
            if self._simulated is None:
                try:
                    self._simulated = self._simulation()
                except NotSimulatedError as exc:
                    self._simulated = exc
        if isinstance(self._simulated, NotSimulatedError):
            raise NotSimulatedError(str(self._simulated))
        return self._simulated.get(action, False)

    def _simulation(self) -> dict[str, bool]:
        """Ask IAM's policy simulator about :data:`SIMULATED` for the
        signed-in user or role (the root user may do everything)."""
        from botocore.exceptions import BotoCoreError, ClientError

        iam = self._client("iam", self.session.region_name or "us-east-1")
        try:
            principal = self._principal(iam, identity(self.session).arn)
            if principal is None:  # the root user
                return dict.fromkeys(SIMULATED, True)
            reply = iam.simulate_principal_policy(
                PolicySourceArn=principal, ActionNames=list(SIMULATED)
            )
        except ClientError as exc:
            if error_code(exc) in DENIED_CODES:
                raise NotSimulatedError(
                    "checking them needs iam:SimulatePrincipalPolicy and iam:GetRole"
                ) from exc
            error = self._error(exc, "check the permissions")
            if isinstance(error, NotSignedInError):
                raise error from exc
            raise NotSimulatedError(str(error)) from exc
        except BotoCoreError as exc:
            raise NotSimulatedError(str(self._error(exc, "check the permissions"))) from exc
        return {
            r["EvalActionName"]: r["EvalDecision"] == "allowed"
            for r in reply.get("EvaluationResults", [])
        }

    @staticmethod
    def _principal(iam: Any, arn: str) -> str | None:
        """The IAM ARN of the user or role of the sign-in ``arn`` (``None``
        for the root user): a role session's role, with its path (e.g. IAM
        Identity Center's ``/aws-reserved/sso.amazonaws.com/``)."""
        kind, _, rest = arn.split(":", 5)[-1].partition("/")
        if kind == "root":
            return None
        if kind == "user":
            return arn
        if kind == "assumed-role":
            return iam.get_role(RoleName=rest.split("/", 1)[0])["Role"]["Arn"]
        raise NotSimulatedError(f"IAM's policy simulator doesn't know {kind} sign-ins")

    def _image_id(self, region: str) -> str:
        """The machine image of the region (public SSM parameter)."""
        if region not in self._images:
            ssm = self._client("ssm", region)
            self._images[region] = ssm.get_parameter(Name=AMI_PARAMETER)["Parameter"]["Value"]
        return self._images[region]

    def _run_instance(
        self,
        ec2: Any,
        region: str,
        machine_type: str,
        vpc: DefaultVpc | None,
        tags: list[dict] | None = None,
    ) -> Any:
        """Start an instance with ``DryRun`` (with ``tags``, if given)."""
        from botocore.exceptions import ClientError

        if vpc is None or not vpc.subnets:
            raise NotCheckedError("the region has no default VPC")
        try:
            image = self._image_id(region)
        except ClientError as exc:
            if error_code(exc) in DENIED_CODES:
                raise NotCheckedError("finding the machine image needs ssm:GetParameter") from exc
            raise
        extra = {"TagSpecifications": tags} if tags else {}
        return ec2.run_instances(
            DryRun=True, ImageId=image, InstanceType=machine_type, MinCount=1, MaxCount=1, **extra
        )

    def vcpu_quota(self, region: str) -> VcpuQuota:
        from botocore.exceptions import BotoCoreError, ClientError

        quotas = self._client("service-quotas", region)
        key = {"ServiceCode": "ec2", "QuotaCode": VCPU_QUOTA_CODE}
        try:
            try:
                limit = quotas.get_service_quota(**key)["Quota"]["Value"]
            except ClientError as exc:
                if error_code(exc) != "NoSuchResourceException":
                    raise
                limit = quotas.get_aws_default_service_quota(**key)["Quota"]["Value"]
            used = self._vcpus_in_use(region)
            requested = self._open_request(quotas, key)
        except (BotoCoreError, ClientError) as exc:
            raise self._error(exc, f"read the vCPU quota of {region}") from exc
        return VcpuQuota(float(limit), used, requested)

    def _vcpus_in_use(self, region: str) -> float:
        """vCPUs of the region's running instances that count against the quota."""
        pages = (
            self._client("ec2", region)
            .get_paginator("describe_instances")
            .paginate(Filters=[{"Name": "instance-state-name", "Values": ["pending", "running"]}])
        )
        instances = (
            instance
            for page in pages
            for reservation in page.get("Reservations", [])
            for instance in reservation.get("Instances", [])
        )
        total = 0.0
        for instance in instances:
            if instance.get("InstanceLifecycle") == "spot":
                continue  # Spot instances have quotas of their own
            if counts_against_quota(instance.get("InstanceType", "")):
                cpu = instance.get("CpuOptions", {})
                total += cpu.get("CoreCount", 0) * cpu.get("ThreadsPerCore", 1)
        return total

    @staticmethod
    def _open_request(quotas: Any, key: dict[str, str]) -> float | None:
        """The value of an open request to raise the quota (``None``: none,
        or the user may not list the requests)."""
        from botocore.exceptions import ClientError

        try:
            history = quotas.list_requested_service_quota_change_history_by_quota(**key)
        except ClientError as exc:
            if error_code(exc) not in DENIED_CODES:
                raise
            log.info("Can't list the quota requests: %s", exc)
            return None
        open_values = [
            float(r["DesiredValue"])
            for r in history.get("RequestedQuotas", [])
            if r.get("Status") in ("PENDING", "CASE_OPENED")
        ]
        return max(open_values, default=None)


# ----------------------------------------------------------------------
# The checks


def _skipped(keys: Iterable[str], why: str) -> list[Check]:
    return [Check(key, TITLES[key], Status.UNKNOWN, detail=f"Not checked: {why}.") for key in keys]


def run_checks(
    session: Any,
    region: str,
    machine_type: str,
    *,
    api: SetupApi | None = None,
    make_api: Callable[[Any], SetupApi] = BotoSetupApi,
) -> list[Check]:
    """Check everything a machine of ``machine_type`` in ``region`` needs in
    the account of ``session`` (a ``boto3`` session; ``None`` when the user
    isn't signed in). ``api`` replaces AWS (tests)."""
    keys = list(TITLES)
    if session is None and api is None:
        signed_out = NotSignedInError("No AWS sign-in on this computer for this profile.")
        return [_credentials_failed(signed_out), *_skipped(keys[1:], "sign in first")]
    api = api if api is not None else make_api(session)
    checks = [_credentials_check(api)]
    if not checks[-1].passed:
        return checks + _skipped(keys[1:], "sign in first")
    checks.append(_region_check(api, region))
    if not checks[-1].passed:
        return checks + _skipped(keys[2:], "choose an enabled region first")
    vpc_check, vpc = _vpc_check(api, region)
    checks.append(vpc_check)
    checks.append(_permissions_check(api, region, machine_type, vpc))
    checks.append(_quota_check(api, region, machine_type))
    return checks


def _credentials_failed(error: Exception) -> Check:
    return Check(
        "credentials",
        TITLES["credentials"],
        Status.FAILED,
        detail=str(error),
        fix=f"Click “{SIGN_IN_BUTTON}”.",
        link=_SIGN_IN_DOCS,
    )


def _credentials_check(api: SetupApi) -> Check:
    try:
        who = api.identity()
    except NotSignedInError as exc:
        return _credentials_failed(exc)
    except ProviderError as exc:
        return not_checked("credentials", TITLES["credentials"], exc, _SIGN_IN_DOCS)
    return Check("credentials", TITLES["credentials"], Status.PASSED, detail=who.label)


def _region_check(api: SetupApi, region: str) -> Check:
    title = TITLES["region"]
    if not region:
        return Check(
            "region", title, Status.FAILED, detail="No region is selected.", fix="Choose a region."
        )
    try:
        enabled = api.enabled_regions()
    except ProviderError as exc:
        return not_checked("region", title, exc, _REGION_DOCS)
    if region in enabled:
        return Check("region", title, Status.PASSED, detail=region)
    return Check(
        "region",
        title,
        Status.FAILED,
        detail=f"Region {region} isn't enabled for the account.",
        fix="Enable it in the AWS account's settings, or choose another region.",
        link=_REGION_DOCS,
    )


def _vpc_check(api: SetupApi, region: str) -> tuple[Check, DefaultVpc | None]:
    title = TITLES["vpc"]
    try:
        vpc = api.default_vpc(region)
    except ProviderError as exc:
        return not_checked("vpc", title, exc, _vpc_console(region)), None
    if vpc is None:
        return Check(
            "vpc",
            title,
            Status.FAILED,
            detail=f"Region {region} has no default VPC.",
            fix="Create one in the VPC console (Actions, Create default VPC), "
            "or choose another region.",
            link=_vpc_console(region),
        ), None
    if not vpc.subnets:
        return Check(
            "vpc",
            title,
            Status.FAILED,
            detail=f"The default VPC {vpc.vpc_id} has no default subnets.",
            fix="Create a default subnet in the region (aws ec2 create-default-subnet).",
            link=_VPC_DOCS,
        ), vpc
    return Check("vpc", title, Status.PASSED, detail=vpc.vpc_id), vpc


def _not_tried(results: list[tuple[str, bool, NotCheckedError | None]], simulated: bool) -> str:
    """E.g. "a, b (why); c (why)": the permissions that weren't tried, with
    why, those with the same reason together; only those the simulator
    couldn't check, or only the others."""
    reasons: dict[str, list[str]] = {}
    for action, _, error in results:
        if error is not None and isinstance(error, NotSimulatedError) == simulated:
            reasons.setdefault(str(error), []).append(action)
    return "; ".join(f"{', '.join(actions)} ({why})" for why, actions in reasons.items())


def _try_permissions(
    api: SetupApi, region: str, machine_type: str, vpc: DefaultVpc | None
) -> tuple[list[str], str, str]:
    """The denied permissions, those that couldn't be tried and those that
    the policy simulator couldn't check (each text with why). They are
    tried a few at a time: each is a call to AWS."""

    def attempt(action: str) -> tuple[str, bool, NotCheckedError | None]:
        try:
            return action, api.allows(action, region, machine_type, vpc), None
        except NotCheckedError as exc:
            return action, True, exc

    actions = [a for group in PERMISSIONS.values() for a in group]
    with ThreadPoolExecutor(4, thread_name_prefix="orange3-cloud-aws-check") as pool:
        results = list(pool.map(attempt, actions))
    denied = [action for action, allowed, _ in results if not allowed]
    return denied, _not_tried(results, False), _not_tried(results, True)


def _permissions_check(
    api: SetupApi, region: str, machine_type: str, vpc: DefaultVpc | None
) -> Check:
    title = TITLES["permissions"]
    try:
        denied, unchecked, unsimulated = _try_permissions(api, region, machine_type, vpc)
    except ProviderError as exc:
        return not_checked("permissions", title, exc, _IAM_CONSOLE)
    not_tried = f" Not checked: {'; '.join(filter(None, [unchecked, unsimulated]))}."
    not_tried = not_tried if unchecked or unsimulated else ""
    if denied:
        groups = [g for g, actions in PERMISSIONS.items() if set(actions) & set(denied)]
        return Check(
            "permissions",
            title,
            Status.FAILED,
            detail=f"You can't manage {', '.join(groups)}. Missing: {', '.join(denied)}."
            + not_tried,
            fix="Ask the account's administrator for these permissions "
            "(e.g. the AmazonEC2FullAccess, AmazonS3FullAccess and AmazonSSMReadOnlyAccess "
            "policies).",
            link=_IAM_CONSOLE,
            missing=tuple(denied),
        )
    if unchecked:
        return Check(
            "permissions", title, Status.UNKNOWN, detail=not_tried.strip(), link=_IAM_CONSOLE
        )
    return Check("permissions", title, Status.PASSED, detail=not_tried.strip())


def _quota_check(api: SetupApi, region: str, machine_type: str) -> Check:
    title = TITLES["quota"]
    size = next((t for t in MACHINE_TYPES if t.name == machine_type), None)
    cpus = (size.cpus if size else None) or 0
    link = _quota_console(region)
    try:
        quota = api.vcpu_quota(region)
    except ProviderError as exc:
        return not_checked("quota", title, exc, link)
    free = quota.limit - quota.used
    detail = f"{free:g} of {quota.limit:g} vCPUs free; {machine_type} needs {cpus}."
    if free >= cpus:
        return Check("quota", title, Status.PASSED, detail=detail)
    if quota.requested:
        detail += f" A request for {quota.requested:g} vCPUs is open."
    return Check(
        "quota",
        title,
        Status.FAILED,
        detail=detail,
        fix=f"Request a quota of at least {quota.used + cpus:g} vCPUs for "
        f"“{VCPU_QUOTA_NAME}” ({VCPU_QUOTA_CODE}) in Service Quotas, "
        "stop other instances, or choose a smaller size.",
        link=link,
    )
