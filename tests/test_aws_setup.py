"""The AWS setup checker, with AWS replaced by a fake account: an unprepared
account lists exactly what's missing, a ready one passes, and boto3's dry-run
answers map to permissions.

``test_real_account_is_ready`` checks the development test account with this
computer's own sign-in (read-only and dry-run calls); it runs only when
``ORANGE3_CLOUD_AWS_PROFILE`` is set, so CI needs no AWS credentials."""

import os
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from orangecontrib.cloud.client.providers import aws_account, aws_setup
from orangecontrib.cloud.client.providers.aws_account import Identity, NotSignedInError
from orangecontrib.cloud.client.providers.aws_setup import (
    BotoSetupApi,
    DefaultVpc,
    NotCheckedError,
    VcpuQuota,
)
from orangecontrib.cloud.client.providers.setup_checks import Status, all_passed

VPC = DefaultVpc("vpc-1", subnets=6, security_group="sg-1")
QUOTA = VcpuQuota(32, 0)


class FakeAccount(aws_setup.SetupApi):
    """An account that is ready unless told otherwise."""

    def __init__(self, signed_in=True, regions=("us-east-1",), vpc=VPC, denied=(), quota=QUOTA):
        self.signed_in = signed_in
        self.regions = list(regions)
        self.vpc = vpc
        self.denied = set(denied)
        self.quota = quota

    def identity(self):
        if not self.signed_in:
            raise NotSignedInError("The AWS sign-in of the profile has expired.")
        return Identity("123456789012", "arn:aws:iam::123456789012:user/ana")

    def enabled_regions(self):
        return self.regions

    def default_vpc(self, region):
        return self.vpc

    def allows(self, action, region, machine_type, vpc):
        if vpc is None and "SecurityGroup" in action:
            raise NotCheckedError("the region has no default VPC")
        return action not in self.denied

    def vcpu_quota(self, region):
        return self.quota


def statuses(account, region="us-east-1", machine_type="m7i.4xlarge"):
    checks = aws_setup.run_checks(None, region, machine_type, api=account)
    assert [c.key for c in checks] == list(aws_setup.TITLES)
    return {c.key: c for c in checks}


def failed(checks):
    return {key for key, c in checks.items() if c.status is Status.FAILED}


def test_ready_account_passes():
    checks = statuses(FakeAccount())
    assert all_passed(checks.values())
    assert checks["credentials"].detail == "ana in account 123456789012"
    assert checks["quota"].detail == "32 of 32 vCPUs free; m7i.4xlarge needs 16."


def test_unprepared_account_lists_whats_missing():
    account = FakeAccount(
        vpc=None, denied={"ec2:RunInstances", "s3:ListAllMyBuckets"}, quota=VcpuQuota(16, 4, 32)
    )
    checks = statuses(account, machine_type="m7i.8xlarge")
    assert failed(checks) == {"vpc", "permissions", "quota"}
    assert checks["vpc"].detail == "Region us-east-1 has no default VPC."
    assert "Create default VPC" in checks["vpc"].fix
    permissions = checks["permissions"]
    assert permissions.missing == ("ec2:RunInstances", "s3:ListAllMyBuckets")
    assert permissions.detail.startswith("You can't manage machines, storage.")
    assert "Not checked: ec2:DescribeSecurityGroups, ec2:CreateSecurityGroup, " in (
        permissions.detail
    )
    assert "ec2:DeleteSecurityGroup (the region has no default VPC)" in permissions.detail
    quota = checks["quota"]
    assert quota.detail == (
        "12 of 16 vCPUs free; m7i.8xlarge needs 32. A request for 32 vCPUs is open."
    )
    assert "at least 36 vCPUs" in quota.fix and "L-1216C47A" in quota.fix
    assert "us-east-1.console.aws.amazon.com/servicequotas" in quota.link


def test_not_signed_in_or_no_region():
    checks = statuses(FakeAccount(signed_in=False))
    assert failed(checks) == {"credentials"}
    assert checks["credentials"].fix == "Click “Sign in to AWS”."
    assert {c.status for k, c in checks.items() if k != "credentials"} == {Status.UNKNOWN}
    assert checks["quota"].detail == "Not checked: sign in first."
    # No session at all (never signed in): no calls.
    checks = aws_setup.run_checks(None, "us-east-1", "m7i.xlarge")
    assert [c.status for c in checks] == [Status.FAILED] + [Status.UNKNOWN] * 4

    checks = statuses(FakeAccount(regions=("eu-west-1",)))
    assert failed(checks) == {"region"}
    assert checks["region"].detail == "Region us-east-1 isn't enabled for the account."
    assert checks["vpc"].detail == "Not checked: choose an enabled region first."


class FakeEc2:
    """EC2, SSM and S3 calls that answer with an error code per call."""

    def __init__(self, codes):
        self.codes = codes  # method name -> error code ("" for success)
        self.replies = {}  # method name -> reply of a successful call
        self.calls = []

    def __getattr__(self, name):
        def call(**kwargs):
            self.calls.append((name, kwargs))
            code = self.codes.get(name, "DryRunOperation")
            if code:
                raise ClientError({"Error": {"Code": code, "Message": code}}, name)
            if name in self.replies:
                return self.replies[name]
            return {"Parameter": {"Value": "ami-1"}} if name == "get_parameter" else {}

        return call


def test_boto_dry_runs(monkeypatch):
    """A dry run that would have worked passes, a refused one is missing,
    another error leaves it unchecked, and an expired sign-in stops."""
    fake = FakeEc2(
        {
            "get_parameter": "",
            "run_instances": "UnauthorizedOperation",
            "list_buckets": "",
            "delete_security_group": "InvalidGroup.InUse",
            "describe_instances": "ExpiredToken",
        }
    )
    session = SimpleNamespace(profile_name="p", region_name=None)
    monkeypatch.setattr(aws_setup, "client", lambda session, service, region: fake)
    api = BotoSetupApi(session)
    assert api.allows("ssm:GetParameter", "us-east-1", "m7i.xlarge", VPC)
    assert not api.allows("ec2:RunInstances", "us-east-1", "m7i.xlarge", VPC)
    assert api.allows("ec2:CreateSecurityGroup", "us-east-1", "m7i.xlarge", VPC)
    assert api.allows("s3:ListAllMyBuckets", "us-east-1", "m7i.xlarge", VPC)
    run = next(kwargs for name, kwargs in fake.calls if name == "run_instances")
    assert run["DryRun"] and run["ImageId"] == "ami-1" and run["InstanceType"] == "m7i.xlarge"
    with pytest.raises(NotCheckedError, match="InvalidGroup.InUse"):
        api.allows("ec2:DeleteSecurityGroup", "us-east-1", "m7i.xlarge", VPC)
    with pytest.raises(NotCheckedError, match="no default VPC"):
        api.allows("ec2:AuthorizeSecurityGroupIngress", "us-east-1", "m7i.xlarge", None)
    with pytest.raises(NotSignedInError):
        api.allows("ec2:DescribeInstances", "us-east-1", "m7i.xlarge", VPC)

    # Stopping, deleting and the console, and S3 writes: IAM's policy
    # simulator, asked once, for the role of a role session (with its path).
    role = "arn:aws:iam::123456789012:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_Admin_1"
    fake.codes["get_role"] = ""
    fake.codes["simulate_principal_policy"] = ""
    results = [
        {"EvalActionName": a, "EvalDecision": "implicitDeny" if a == "s3:PutObject" else "allowed"}
        for a in aws_setup.SIMULATED
    ]
    replies = {
        "get_role": {"Role": {"Arn": role}},
        "simulate_principal_policy": {"EvaluationResults": results},
    }
    fake.replies = replies
    session_arn = "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_Admin_1/ana"
    monkeypatch.setattr(aws_setup, "identity", lambda s: Identity("123456789012", session_arn))
    assert api.allows("ec2:TerminateInstances", "us-east-1", "m7i.xlarge", VPC)
    assert not api.allows("s3:PutObject", "us-east-1", "m7i.xlarge", VPC)
    simulated = [kwargs for name, kwargs in fake.calls if name == "simulate_principal_policy"]
    assert simulated == [{"PolicySourceArn": role, "ActionNames": list(aws_setup.SIMULATED)}]
    # Without the right to simulate: not checked, and the check still passes.
    api = BotoSetupApi(session)
    fake.codes["simulate_principal_policy"] = "AccessDenied"
    with pytest.raises(aws_setup.NotSimulatedError, match="iam:SimulatePrincipalPolicy"):
        api.allows("ec2:StopInstances", "us-east-1", "m7i.xlarge", VPC)

    # Only the standard families count against the quota.
    assert aws_setup.counts_against_quota("m7i.xlarge")
    assert aws_setup.counts_against_quota("t3.micro")
    assert not aws_setup.counts_against_quota("p5.48xlarge")
    assert not aws_setup.counts_against_quota("inf2.xlarge")


@pytest.mark.skipif(
    not os.environ.get("ORANGE3_CLOUD_AWS_PROFILE"),
    reason="needs this computer's AWS sign-in (ORANGE3_CLOUD_AWS_PROFILE)",
)
def test_real_account_is_ready():
    """The test account passes every check for the Small size (read-only
    and dry-run calls: nothing is created)."""
    session = aws_account.load_session(os.environ["ORANGE3_CLOUD_AWS_PROFILE"])
    checks = aws_setup.run_checks(session, os.environ["ORANGE3_CLOUD_AWS_REGION"], "m7i.xlarge")
    assert all_passed(checks), [c for c in checks if not c.passed]


def test_permissions_the_simulator_couldnt_check_still_pass():
    class NoSimulator(FakeAccount):
        def allows(self, action, region, machine_type, vpc):
            if action in aws_setup.SIMULATED:
                raise aws_setup.NotSimulatedError("checking them needs iam:SimulatePrincipalPolicy")
            return super().allows(action, region, machine_type, vpc)

    permissions = statuses(NoSimulator())["permissions"]
    assert permissions.status is Status.PASSED
    assert permissions.detail.startswith("Not checked: ec2:StopInstances, ec2:TerminateInstances")
