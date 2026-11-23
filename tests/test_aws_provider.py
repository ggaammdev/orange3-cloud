"""The AWS machine provider with EC2 and SSM replaced by a fake account (no
network). Every request is checked against botocore's own model of the API,
and a new instance runs the API server in-process over HTTPS with the
token, certificate and key from its user data: Start creates the instance
and the security group for this computer's address only, ``/v1/info``
works, the group follows a new address, the session reattaches after a
restart, and Stop deletes everything. Also a failed boot, leftover machines,
the user data and the image choice.

``test_real_machine`` starts a Small machine in the development test
account; it runs only with ``ORANGE3_CLOUD_AWS_VM_TESTS=1`` and
``ORANGE3_CLOUD_AWS_PROFILE``/``_REGION`` set (never in CI).
"""

import base64
import contextlib
import datetime
import gzip
import itertools
import json
import os
import time

import botocore.session
import pytest
from botocore.exceptions import ClientError
from botocore.validate import ParamValidator
from conftest import https_server
from test_gcp_provider import SPEC_WITH_ADDON, free_port

from orangecontrib.cloud.client.providers import aws, aws_prices, cloud_vm, machine_images
from orangecontrib.cloud.client.providers.aws import AWSProvider
from orangecontrib.cloud.client.providers.aws_vm import tag_value
from orangecontrib.cloud.client.providers.base import MachineHandle, MachineState, ProviderError
from orangecontrib.cloud.client.providers.cloud_init import SETTINGS_DIR, user_data
from orangecontrib.cloud.client.session import CloudSession, HandleStore
from orangecontrib.cloud.server.app import ServerConfig, create_app
from orangecontrib.cloud.settings import CloudSettings, ProviderSettings

PROFILE, REGION = "orange3-cloud", "eu-west-1"
IP = "93.184.216.34"
AMI = "ami-0123456789abcdef0"
_MODELS = {
    s: botocore.session.get_session().get_service_model(s) for s in ("ec2", "ssm", "cloudwatch")
}
_OPERATIONS = {
    "get_parameter": "GetParameter",
    "describe_images": "DescribeImages",
    "describe_vpcs": "DescribeVpcs",
    "describe_security_groups": "DescribeSecurityGroups",
    "create_security_group": "CreateSecurityGroup",
    "authorize_security_group_ingress": "AuthorizeSecurityGroupIngress",
    "revoke_security_group_ingress": "RevokeSecurityGroupIngress",
    "delete_security_group": "DeleteSecurityGroup",
    "run_instances": "RunInstances",
    "describe_instances": "DescribeInstances",
    "stop_instances": "StopInstances",
    "terminate_instances": "TerminateInstances",
    "get_console_output": "GetConsoleOutput",
    "get_metric_data": "GetMetricData",
}


def error(code, operation):
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


def cloud_config(data):
    text = gzip.decompress(data).decode()
    assert text.startswith("#cloud-config\n")
    return json.loads(text.split("\n", 1)[1])


def settings_files(config):
    prefix = SETTINGS_DIR + "/"
    return {
        f["path"][len(prefix) :]: f["content"]
        for f in config["write_files"]
        if f["path"].startswith(prefix)
    }


class FakeClient:
    """A boto3 client of ``service`` on a :class:`FakeAws`: checks each
    request against botocore's model, then lets the account answer it."""

    def __init__(self, account, service):
        self.account, self.service = account, service

    def __getattr__(self, method):
        operation = _OPERATIONS[method]

        def call(**params):
            checked = dict(params)
            if isinstance(checked.get("UserData"), bytes):  # boto3 encodes it
                checked["UserData"] = base64.b64encode(checked["UserData"]).decode()
            shape = _MODELS[self.service].operation_model(operation).input_shape
            report = ParamValidator().validate(checked, shape)
            assert not report.has_errors(), report.generate_report()
            self.account.calls.append(method)
            return getattr(self.account, method)(**params)

        return call


class FakeAws:
    """An AWS region in memory; a new instance runs the server from its user data."""

    def __init__(self, folder, boot_ok=True):
        self.folder, self.boot_ok = folder, boot_ok
        self.port = free_port()
        self.instances, self.groups, self.requests, self.calls = {}, {}, {}, []
        self.cpu_metric = {}  # instance ID -> times of its values (CloudWatch)
        self._ids = itertools.count(1)
        self._servers = {}

    def client(self, profile, service, region):
        assert (profile, region) == (PROFILE, REGION)
        return FakeClient(self, service)

    def close(self):
        for stack in self._servers.values():
            stack.close()

    # SSM and images

    def get_parameter(self, Name):
        return {"Parameter": {"Name": Name, "Value": AMI}}

    def describe_images(self, ImageIds):
        return {"Images": [{"ImageId": AMI, "RootDeviceName": "/dev/xvda"}]}

    def describe_vpcs(self, Filters):
        return {"Vpcs": [{"VpcId": "vpc-1", "IsDefault": True}]}

    # Security groups

    def describe_security_groups(self, Filters):
        wanted = {f["Name"]: f["Values"][0] for f in Filters}
        found = [
            g
            for g in self.groups.values()
            if g["VpcId"] == wanted["vpc-id"]
            and g["GroupName"] == wanted["group-name"]
            and {"Key": "orange3-cloud", "Value": "true"} in g["Tags"]
        ]
        return {"SecurityGroups": json.loads(json.dumps(found))}

    def create_security_group(self, GroupName, Description, VpcId, TagSpecifications):
        assert "'" not in Description  # EC2 refuses apostrophes there
        group_id = f"sg-{next(self._ids)}"
        tags = TagSpecifications[0]["Tags"]
        self.groups[group_id] = {
            "GroupId": group_id,
            "GroupName": GroupName,
            "VpcId": VpcId,
            "Tags": tags,
            "IpPermissions": [],
        }
        return {"GroupId": group_id}

    def authorize_security_group_ingress(self, GroupId, IpPermissions):
        self.groups[GroupId]["IpPermissions"] += json.loads(json.dumps(IpPermissions))
        return {"Return": True}

    def revoke_security_group_ingress(self, GroupId, IpPermissions):
        revoked = {r["CidrIp"] for p in IpPermissions for r in p.get("IpRanges", [])}
        group = self.groups[GroupId]
        for permission in group["IpPermissions"]:
            permission["IpRanges"] = [
                r for r in permission["IpRanges"] if r["CidrIp"] not in revoked
            ]
        group["IpPermissions"] = [p for p in group["IpPermissions"] if p["IpRanges"]]
        return {"Return": True}

    def delete_security_group(self, GroupId):
        if GroupId not in self.groups:
            raise error("InvalidGroup.NotFound", "DeleteSecurityGroup")
        used = [
            i
            for i in self.instances.values()
            if i["State"]["Name"] != "terminated" and GroupId in i["Groups"]
        ]
        if used:
            raise error("DependencyViolation", "DeleteSecurityGroup")
        del self.groups[GroupId]
        return {}

    # Instances

    def run_instances(self, **request):
        instance_id = f"i-{next(self._ids):017x}"
        config = cloud_config(request["UserData"])
        tags = request["TagSpecifications"][0]["Tags"]
        instance = {
            "InstanceId": instance_id,
            "InstanceType": request["InstanceType"],
            "State": {"Name": "running"},
            "PublicIpAddress": "127.0.0.1",
            "Tags": tags,
            "LaunchTime": datetime.datetime.now(datetime.timezone.utc),
            "Groups": request["NetworkInterfaces"][0]["Groups"],
            "Console": "",
        }
        self.instances[instance_id] = instance
        self.requests[instance_id] = request
        if not self.boot_ok:
            instance["Console"] = (
                "[bootstrap    1.0s] ERROR: no Python\n"
                "[bootstrap    1.0s] the machine did not start\n"
            )
            return {"Instances": [dict(instance)]}
        settings = settings_files(config)
        folder = self.folder / instance_id
        folder.mkdir()
        stack = contextlib.ExitStack()
        app = create_app(ServerConfig(token=settings["token"], data_dir=folder))
        cert_key = (settings["tls-cert"], settings["tls-key"])
        stack.enter_context(https_server(app, folder, cert_key, self.port))
        self._servers[instance_id] = stack
        instance["Console"] = "docker[1056]: [bootstrap    2.0s] installing Python 3.12.0\n"
        return {"Instances": [dict(instance)]}

    def describe_instances(self, InstanceIds=None, Filters=None):
        if InstanceIds:
            if InstanceIds[0] not in self.instances:
                raise error("InvalidInstanceID.NotFound", "DescribeInstances")
            found = [self.instances[i] for i in InstanceIds]
        else:
            wanted = {f["Name"]: f["Values"] for f in Filters}
            sessions = wanted.get("tag:orange3-cloud-session")
            found = [
                i
                for i in self.instances.values()
                if {"Key": "orange3-cloud", "Value": "true"} in i["Tags"]
                and i["State"]["Name"] in wanted["instance-state-name"]
                and (sessions is None or tag_value(i, "orange3-cloud-session") in sessions)
            ]
        reply = {"Reservations": [{"Instances": [dict(i) for i in found]}]}
        for instance in found:  # terminating takes one look
            if instance["State"]["Name"] == "shutting-down":
                instance["State"] = {"Name": "terminated"}
        return reply

    def stop_instances(self, InstanceIds):
        for instance_id in InstanceIds:
            self.instances[instance_id]["State"] = {"Name": "stopping"}
        return {"StoppingInstances": []}

    def terminate_instances(self, InstanceIds):
        for instance_id in InstanceIds:
            self.instances[instance_id]["State"] = {"Name": "shutting-down"}
            stack = self._servers.pop(instance_id, None)
            if stack is not None:
                stack.close()
        return {"TerminatingInstances": []}

    def get_console_output(self, InstanceId, Latest):
        return {"InstanceId": InstanceId, "Output": self.instances[InstanceId]["Console"]}

    def get_metric_data(self, MetricDataQueries, StartTime, EndTime, ScanBy):
        ((query,),) = [MetricDataQueries]
        (dimension,) = query["MetricStat"]["Metric"]["Dimensions"]
        assert (StartTime.minute % 5, StartTime.second, ScanBy) == (0, 0, "TimestampDescending")
        times = [t for t in self.cpu_metric.get(dimension["Value"], []) if t >= StartTime]
        result = {"Id": query["Id"], "Timestamps": sorted(times, reverse=True)}
        return {"MetricDataResults": [dict(result, Values=[1.0] * len(times))]}

    def live(self):
        return [i for i in self.instances.values() if i["State"]["Name"] != "terminated"]


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("ORANGE3_CLOUD_PUBLIC_IP", IP)
    monkeypatch.delenv("ORANGE3_CLOUD_AWS_IMAGE", raising=False)
    monkeypatch.setattr(aws, "registry_token", lambda image: "reader-token")
    providers = {
        "aws": ProviderSettings(PROFILE, REGION),
        "gcp": ProviderSettings("my-project", "europe-west1", "europe-west1-b"),
    }
    CloudSettings(provider="aws", providers=providers).save()
    account = FakeAws(tmp_path)
    yield account
    account.close()


def provider_for(account):
    provider = AWSProvider(make_client=account.client, port=account.port, poll_interval=0.01)
    provider.console_interval = 0
    return provider


def session_for(account, tmp_path):
    return CloudSession(
        store=HandleStore(tmp_path / "state" / "machine.json"),
        resolve_provider=lambda provider_id: provider_for(account),
    )


def test_start_reattach_and_stop(fake, tmp_path, monkeypatch):
    session = session_for(fake, tmp_path)
    messages = []
    session.messageChanged.connect(messages.append)
    handle = session.start_machine("m7i.xlarge", spec=SPEC_WITH_ADDON, wait=True)

    # The instance: settings fixed for the add-on (the fake checked the
    # request's shape against botocore's model).
    instance_id = handle.extra["instance_id"]
    request = fake.requests[instance_id]
    assert (request["ImageId"], request["InstanceType"]) == (AMI, "m7i.xlarge")
    assert request["BlockDeviceMappings"] == [
        {
            "DeviceName": "/dev/xvda",
            "Ebs": {"VolumeSize": 100, "VolumeType": "gp3", "DeleteOnTermination": True},
        }
    ]
    nic = request["NetworkInterfaces"][0]
    assert nic["AssociatePublicIpAddress"] and nic["DeleteOnTermination"]
    assert request["MetadataOptions"]["HttpTokens"] == "required"
    assert request["InstanceInitiatedShutdownBehavior"] == "stop"
    for spec in request["TagSpecifications"]:  # the instance and its volume
        tags = {t["Key"]: t["Value"] for t in spec["Tags"]}
        assert tags["orange3-cloud"] == "true"
        assert tags["orange3-cloud-session"] == handle.machine_id
    assert {s["ResourceType"] for s in request["TagSpecifications"]} == {"instance", "volume"}

    # The user data: settings files, and the unit that pulls the development
    # image with the reader's token and runs it for the bootstrap's aws platform.
    assert len(request["UserData"]) < 16 * 1024
    config = cloud_config(request["UserData"])
    settings = settings_files(config)
    assert settings["token"] == handle.token and settings["tls-cert"] == handle.cert_pem
    assert "PRIVATE KEY" in settings["tls-key"] and settings["machine"] == "m7i.xlarge"
    assert json.loads(settings["env-spec"])["python_version"] == "3.12.0"
    assert settings["idle-timeout"] == str(CloudSettings().idle_timeout)
    files = {f["path"]: f for f in config["write_files"]}
    assert files["/run/orange3-cloud/registry-token"]["content"] == "reader-token"
    assert all(files[f"{SETTINGS_DIR}/{n}"]["permissions"] == "0600" for n in settings)
    unit = files["/etc/systemd/system/orange3-cloud.service"]["content"]
    dev_image = "europe-west1-docker.pkg.dev/my-project/orange3-cloud/machine:dev"
    assert f"docker pull --quiet {dev_image}" in unit
    assert "docker login --username oauth2accesstoken" in unit
    assert "chown -R 1000:1000 /run/orange3-cloud/settings" in unit
    assert "ORANGE3_CLOUD_CONFIG=aws" in unit and "ORANGE3_CLOUD_POWER_OFF=host" in unit
    assert "SuccessAction=poweroff" in unit and "FailureAction=poweroff" in unit

    # The handle: HTTPS to the instance's address, the certificate checked
    # against its fixed name; no key in the handle.
    assert handle.url == f"https://127.0.0.1:{fake.port}"
    assert handle.server_hostname == f"{handle.name}.orange3-cloud"
    assert (handle.extra["profile"], handle.extra["region"]) == (PROFILE, REGION)
    assert "PRIVATE KEY" not in json.dumps(handle.to_dict())
    # Its price when it started (instance, 100 GB gp3, public IPv4), for the costs.
    price = aws_prices.per_hour("m7i.xlarge", REGION, 100)
    assert handle.extra["price_per_hour"] == f"{price:.6f}"
    assert session.price_per_hour() == pytest.approx(price)
    assert session.info().api_version
    assert "Starting: installing Python 3.12.0" in messages

    # The security group allows only this computer's address, on 8443.
    (group,) = fake.groups.values()
    assert request["NetworkInterfaces"][0]["Groups"] == [group["GroupId"]]
    assert group["GroupName"] == "orange3-cloud-https"
    assert group["IpPermissions"] == [
        {
            "IpProtocol": "tcp",
            "FromPort": 8443,
            "ToPort": 8443,
            "IpRanges": [{"CidrIp": f"{IP}/32"}],
        }
    ]

    # Orange restarts on a new network: the group follows, and the new
    # session reconnects to the same machine.
    session.shutdown()
    monkeypatch.setenv("ORANGE3_CLOUD_PUBLIC_IP", "8.8.4.4")
    again = session_for(fake, tmp_path)
    assert again.reattach(wait=True).machine_id == handle.machine_id
    assert again.is_connected and again.info().api_version
    ranges = [r["CidrIp"] for p in group["IpPermissions"] for r in p["IpRanges"]]
    assert ranges == ["8.8.4.4/32"]
    assert again.leftovers() == []

    # Stop terminates the instance (its volume goes with it) and deletes the group.
    again.stop_machine(wait=True)
    assert fake.live() == [] and fake.groups == {}
    assert "stop_instances" in fake.calls
    assert again.store.load() is None
    again.shutdown()


def test_failed_boot_deletes_the_machine(tmp_path, monkeypatch, fake):
    account = FakeAws(tmp_path / "failing", boot_ok=False)
    provider = provider_for(account)
    with pytest.raises(ProviderError, match="couldn't start") as raised:
        provider.start(SPEC_WITH_ADDON, "m7i.xlarge", timeout=30)
    assert "ERROR: no Python" in str(raised.value)
    assert account.live() == [] and account.groups == {}
    with pytest.raises(ProviderError, match="isn't one the add-on offers"):
        provider.start(SPEC_WITH_ADDON, "r7i.48xlarge")


def test_start_that_fails_after_aws_created_the_instance(fake, monkeypatch):
    """RunInstances carries the machine ID as its idempotency token, and when
    the call fails after AWS created the instance (e.g. the reply was lost),
    the cleanup finds the instance by its session tag and terminates it."""
    created = fake.run_instances

    def lost_reply(**request):
        created(**request)
        raise error("RequestLimitExceeded", "RunInstances")

    monkeypatch.setattr(fake, "run_instances", lost_reply)
    provider = provider_for(fake)
    with pytest.raises(ProviderError, match="create the machine"):
        provider.start(SPEC_WITH_ADDON, "m7i.xlarge")
    ((instance_id, request),) = fake.requests.items()
    assert request["ClientToken"] == tag_value(fake.instances[instance_id], "orange3-cloud-session")
    assert fake.live() == [] and fake.groups == {}


def test_leftovers(fake):
    provider = provider_for(fake)
    handle = provider.start(SPEC_WITH_ADDON, "m7i.xlarge")
    fake.instances["i-left"] = {
        "InstanceId": "i-left",
        "InstanceType": "m7i.xlarge",
        "State": {"Name": "stopped"},
        "StateReason": {"Code": "Client.InstanceInitiatedShutdown"},
        "Tags": [
            {"Key": "Name", "Value": "orange3-cloud-0123456789ab"},
            {"Key": "orange3-cloud", "Value": "true"},
            {"Key": "orange3-cloud-session", "Value": "0123456789ab"},
        ],
        "Groups": [],
    }
    listed = {h.name: h for h in provider.list_machines()}
    assert set(listed) == {handle.name, "orange3-cloud-0123456789ab"}
    assert listed[handle.name].url == handle.url and not listed[handle.name].token
    status = provider.status(listed["orange3-cloud-0123456789ab"])
    assert (status.state, status.message) == (MachineState.STOPPED, "The machine stopped.")
    assert provider.delete_leftovers(keep=handle) == ["orange3-cloud-0123456789ab"]
    assert [i["InstanceId"] for i in fake.live()] == [handle.extra["instance_id"]]
    assert fake.groups
    provider.delete(handle)
    assert fake.live() == [] and fake.groups == {}


def test_stop_time_and_stopped_price(fake):
    """When a stopped instance stopped: after a stop request, the time in its
    state transition reason; after it powered itself off (no time there),
    the end of the last 5-minute period of its CPU metric. Stopped, it costs
    its volume (its public address is gone)."""
    provider = provider_for(fake)
    utc = datetime.timezone.utc
    fake.instances["i-1"] = {  # as EC2 describes one that powered itself off
        "InstanceId": "i-1",
        "State": {"Code": 80, "Name": "stopped"},
        "StateTransitionReason": "User initiated",
        "StateReason": {
            "Code": "Client.InstanceInitiatedShutdown",
            "Message": "Client.InstanceInitiatedShutdown: Instance initiated shutdown",
        },
        "LaunchTime": datetime.datetime(2026, 11, 20, 7, 55, 29, tzinfo=utc),
    }
    fake.cpu_metric["i-1"] = [datetime.datetime(2026, 11, 20, 7, 55, tzinfo=utc)]
    extra = {"profile": PROFILE, "region": REGION, "instance_id": "i-1", "disk_gb": "200"}
    handle = MachineHandle("aws", "m", "vm", "", "", "", machine_type="m7i.xlarge", extra=extra)
    assert provider.stopped_at(handle) == datetime.datetime(2026, 11, 20, 8, 0, tzinfo=utc)
    fake.instances["i-1"]["StateTransitionReason"] = "User initiated (2026-11-20 08:10:42 GMT)"
    assert provider.stopped_at(handle) == datetime.datetime(2026, 11, 20, 8, 10, 42, tzinfo=utc)
    fake.instances["i-1"]["State"] = {"Code": 16, "Name": "running"}
    assert provider.stopped_at(handle) is None
    assert provider.stopped_price_per_hour(handle) == aws_prices.disk_per_hour(200, REGION)


def test_user_data_and_image(monkeypatch):
    monkeypatch.delenv("ORANGE3_CLOUD_AWS_IMAGE", raising=False)
    public = "ghcr.io/ggaammdev/orange3-cloud-machine:1.0.0"
    assert machine_images.machine_image("1.0.0", "ORANGE3_CLOUD_AWS_IMAGE") == public
    assert machine_images.registry_token(public) is None  # no credentials
    with pytest.raises(ProviderError, match="Google Cloud page"):
        machine_images.machine_image("0.0.1.dev0", "ORANGE3_CLOUD_AWS_IMAGE")
    google = ProviderSettings("p", "us-east1", "us-east1-b")
    CloudSettings(providers={"gcp": google}).save()
    dev = "us-east1-docker.pkg.dev/p/orange3-cloud/machine:dev"
    assert machine_images.machine_image("0.0.1.dev0", "ORANGE3_CLOUD_AWS_IMAGE") == dev
    monkeypatch.setenv("ORANGE3_CLOUD_AWS_IMAGE", "example.org/machine:x")
    assert (
        machine_images.machine_image("1.0.0", "ORANGE3_CLOUD_AWS_IMAGE") == "example.org/machine:x"
    )

    # A public image needs no sign-in: no token file, no docker login.
    config = cloud_config(user_data(public, {"token": "t"}, "aws"))
    unit = config["write_files"][-1]["content"]
    assert "docker login" not in unit and f"docker pull --quiet {public}" in unit
    assert [f["path"] for f in config["write_files"]] == [
        f"{SETTINGS_DIR}/token",
        "/etc/systemd/system/orange3-cloud.service",
    ]
    assert config["runcmd"][-1] == ["systemctl", "start", "--no-block", "orange3-cloud.service"]


@pytest.mark.skipif(
    os.environ.get("ORANGE3_CLOUD_AWS_VM_TESTS") != "1"
    or not os.environ.get("ORANGE3_CLOUD_AWS_PROFILE"),
    reason="starts a Small instance in the test account; set ORANGE3_CLOUD_AWS_VM_TESTS=1",
)
def test_real_machine(tmp_path):
    from orangecontrib.cloud.client.providers.aws_ec2 import BotoClients
    from orangecontrib.cloud.env import collect_environment

    profile = os.environ["ORANGE3_CLOUD_AWS_PROFILE"]
    region = os.environ["ORANGE3_CLOUD_AWS_REGION"]
    providers = {"aws": ProviderSettings(profile, region)}
    if os.environ.get("ORANGE3_CLOUD_GCP_PROJECT"):  # for the development image
        providers["gcp"] = ProviderSettings(
            os.environ["ORANGE3_CLOUD_GCP_PROJECT"], os.environ["ORANGE3_CLOUD_GCP_REGION"]
        )
    CloudSettings(provider="aws", providers=providers).save()
    store = HandleStore(tmp_path / "machine.json")
    session = CloudSession(store=store)
    messages = []
    session.messageChanged.connect(messages.append)
    ec2 = BotoClients()(profile, "ec2", region)
    handle = None
    try:
        handle = session.start_machine("m7i.xlarge", spec=collect_environment(), wait=True)
        print(f"/v1/info after {handle.extra['boot_seconds']} s", *messages, sep="\n  ")
        assert any(m.startswith("Starting: ") for m in messages)  # from the console
        assert session.info().machine == "m7i.xlarge"
        (group,) = ec2.describe_security_groups(GroupIds=[handle.extra["security_group"]])[
            "SecurityGroups"
        ]
        permissions = group["IpPermissions"]
        rules = [
            (p["IpProtocol"], p["FromPort"], p["ToPort"], r["CidrIp"])
            for p in permissions
            for r in p["IpRanges"]
        ]
        assert rules == [("tcp", 8443, 8443, f"{cloud_vm.public_ip()}/32")]
        assert not any(p.get("Ipv6Ranges") or p.get("UserIdGroupPairs") for p in permissions)
        session.shutdown()
        session = CloudSession(store=store)
        assert session.reattach(wait=True).machine_id == handle.machine_id
    finally:
        stopping = time.monotonic()
        session.stop_machine(wait=True)
        print(f"stopped and deleted in {time.monotonic() - stopping:.0f} s")
        assert AWSProvider().list_machines() == []
        if handle is not None:
            (instance,) = ec2.describe_instances(InstanceIds=[handle.extra["instance_id"]])[
                "Reservations"
            ][0]["Instances"]
            assert instance["State"]["Name"] == "terminated"
            volumes = ec2.describe_volumes(
                Filters=[{"Name": "tag:orange3-cloud-session", "Values": [handle.machine_id]}]
            )["Volumes"]
            assert volumes == []
        groups = ec2.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": ["orange3-cloud-https"]}]
        )["SecurityGroups"]
        assert groups == []
