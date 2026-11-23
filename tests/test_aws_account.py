"""AWS sign-in, profiles and regions: the AWS CLI's configuration, the
sign-in command, boto3's errors, and the dialog's AWS page (with the account
functions and the setup checker's AWS replaced, and a fake ``aws``).

``test_real_account`` uses this computer's own sign-in, read-only; it runs
only when ``ORANGE3_CLOUD_AWS_PROFILE`` is set (the development env file),
so CI needs no AWS credentials."""

import os
import stat
import sys
from types import SimpleNamespace

import pytest
from AnyQt.QtCore import QSettings, Qt
from AnyQt.QtTest import QTest
from AnyQt.QtWidgets import QApplication
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError
from test_aws_setup import FakeAccount
from test_gcp_account import spin

from orangecontrib.cloud.client.providers import aws_account, aws_prices, aws_setup
from orangecontrib.cloud.client.providers.aws_account import (
    AwsProfile,
    Identity,
    NotSignedInError,
)
from orangecontrib.cloud.client.providers.aws_vm import MACHINE_TYPES
from orangecontrib.cloud.client.providers.base import ProviderError
from orangecontrib.cloud.settings import CloudSettings


def test_profiles_and_sign_in_command(tmp_path, monkeypatch):
    """Profiles come from the CLI's files; IAM Identity Center profiles sign
    in with ``aws sso login``, others with ``aws login`` (with a region when
    the profile has none, since the CLI would ask for one)."""
    config, credentials = tmp_path / "config", tmp_path / "credentials"
    config.write_text(
        "[default]\nregion = eu-west-1\n\n"
        "[profile work]\nsso_session = corp\nsso_account_id = 1\nregion = us-west-2\n\n"
        "[profile new]\n"
    )
    credentials.write_text("[keys]\naws_access_key_id = x\naws_secret_access_key = y\n")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials))
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)

    profiles = aws_account.list_profiles()
    assert profiles == [
        AwsProfile("default", "eu-west-1"),
        AwsProfile("keys"),
        AwsProfile("new"),
        AwsProfile("work", "us-west-2", sso=True),
    ]
    assert aws_account.pick_profile(profiles) == "default"
    assert aws_account.pick_profile(profiles, "gone", "work") == "work"
    monkeypatch.setenv("AWS_PROFILE", "new")
    assert aws_account.pick_profile(profiles) == "new"
    assert aws_account.pick_profile([]) == "default"

    command = aws_account.sign_in_command
    assert command("aws", profiles[0]) == ["aws", "login", "--profile", "default"]
    assert command("aws", profiles[2], "ap-south-1") == [
        "aws", "login", "--profile", "new", "--region", "ap-south-1"
    ]  # fmt: skip
    assert command("aws", profiles[2])[-1] == "us-east-1"
    assert command("aws", profiles[3]) == ["aws", "sso", "login", "--profile", "work"]

    assert aws_account.pick_region(["eu-west-1", "us-east-1"]) == "us-east-1"
    assert aws_account.pick_region(["eu-west-1", "us-east-1"], "", "eu-west-1") == "eu-west-1"
    assert aws_account.pick_region(["eu-west-1"], "mars-1") == "eu-west-1"
    assert aws_account.pick_region([]) == ""


def test_identity_and_errors():
    """Who is signed in, for the user; boto3's errors as messages that say
    what to do."""
    assert Identity("1", "arn:aws:iam::1:user/team/ana").label == "ana in account 1"
    role = Identity("1", "arn:aws:sts::1:assumed-role/Admin/ana@example.com")
    assert role.name == "ana@example.com (Admin)"
    assert Identity("1", "arn:aws:iam::1:root").name == "root user"

    def client_error(code, message="message"):
        return ClientError({"Error": {"Code": code, "Message": message}}, "Operation")

    error = aws_account.aws_error(NoCredentialsError(), "list the regions", "p")
    assert isinstance(error, NotSignedInError) and "Sign in to AWS" in str(error)
    error = aws_account.aws_error(client_error("ExpiredToken"), "list the regions", "p")
    assert isinstance(error, NotSignedInError) and "has expired" in str(error)
    error = aws_account.aws_error(client_error("AccessDenied", "No."), "list the regions")
    assert str(error) == "Couldn't list the regions: no permission. No."
    error = aws_account.aws_error(EndpointConnectionError(endpoint_url="x"), "list the regions")
    assert str(error) == "Couldn't reach AWS to list the regions. Check the connection."
    assert aws_account.error_code(client_error("Throttling")) == "Throttling"
    assert aws_account.error_code(ValueError()) == ""


def test_offered_instance_types(monkeypatch):
    """The add-on's sizes a region offers: one call, checked against
    botocore's model by its Stubber."""
    import boto3
    from botocore.stub import Stubber

    session = boto3.Session(aws_access_key_id="a", aws_secret_access_key="b")
    ec2 = session.client("ec2", region_name="eu-west-1")
    monkeypatch.setattr(aws_account, "client", lambda session, service, region: ec2)
    request = {
        "LocationType": "region",
        "Filters": [{"Name": "instance-type", "Values": sorted(t.name for t in MACHINE_TYPES)}],
    }
    offerings = [
        {"InstanceType": name, "LocationType": "region", "Location": "eu-west-1"}
        for name in ("m7i.xlarge", "r7i.4xlarge")
    ]
    with Stubber(ec2) as stubber:
        stubber.add_response(
            "describe_instance_type_offerings", {"InstanceTypeOfferings": offerings}, request
        )
        stubber.add_client_error("describe_instance_type_offerings", "UnauthorizedOperation")
        assert aws_account.offered_instance_types(session, "eu-west-1") == {
            "m7i.xlarge",
            "r7i.4xlarge",
        }
        with pytest.raises(ProviderError, match="list the machine sizes of eu-west-1: no perm"):
            aws_account.offered_instance_types(session, "eu-west-1")


def fake_tool(path, body):
    """A shell script standing in for aws."""
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.mark.skipif(sys.platform == "win32", reason="the fake aws is a shell script")
def test_sign_in_process(tmp_path):
    """The sign-in runs the CLI's command without a console; a failure says why."""
    aws = fake_tool(tmp_path / "aws", 'read answer; echo "args: $@ [$answer]"\n')
    profile = AwsProfile("p", "eu-west-1")
    assert aws_account.sign_in(aws, profile).strip() == "args: login --profile p []"
    fake_tool(tmp_path / "aws", "echo 'Profile p is already configured'; exit 252\n")
    with pytest.raises(ProviderError, match=r"\(aws exit code 252\)\.\nProfile p is already"):
        aws_account.sign_in(aws, profile)
    with pytest.raises(ProviderError, match="Couldn't run aws to sign in.*AWS CLI installed"):
        aws_account.sign_in(str(tmp_path / "missing"), profile)


@pytest.fixture
def fake_aws(tmp_path, monkeypatch):
    """A fake aws script and fake account functions: two profiles, not
    signed in until the script has run, then the regions; the setup checker
    sees a ready account."""
    marker = tmp_path / "signed-in"
    script = fake_tool(tmp_path / "aws", f'echo "$@" > "{marker}"\n')

    def identity(session):
        if not marker.exists():
            raise NotSignedInError('Not signed in to AWS with the profile "x".')
        return Identity("123456789012", f"arn:aws:iam::123456789012:user/{session.profile_name}")

    monkeypatch.setattr(aws_account, "find_aws", lambda: script)
    monkeypatch.setattr(
        aws_account,
        "list_profiles",
        lambda: [AwsProfile("orange3-cloud", "eu-west-1"), AwsProfile("other")],
    )
    monkeypatch.setattr(
        aws_account,
        "load_session",
        lambda name: SimpleNamespace(profile_name=name, region_name=None),
    )
    monkeypatch.setattr(aws_account, "identity", identity)
    monkeypatch.setattr(aws_account, "list_regions", lambda s: ["eu-west-1", "us-east-1"])
    sizes = {t.name for t in MACHINE_TYPES}
    offered = {"us-east-1": sizes, "eu-west-1": sizes - {"r7i.24xlarge"}}
    monkeypatch.setattr(aws_account, "offered_instance_types", lambda s, region: offered[region])
    # The checker's calls go to a fake account (tested in test_aws_setup).
    monkeypatch.setattr(aws_setup, "BotoSetupApi", lambda session: FakeAccount())
    return SimpleNamespace(marker=marker, script=script)


@pytest.mark.skipif(sys.platform == "win32", reason="the fake aws is a shell script")
def test_dialog_lists_profiles_and_regions(fake_aws, fake, tmp_path):
    from orangecontrib.cloud.client.session import CloudSession, HandleStore
    from orangecontrib.cloud.gui.settings_dialog import CloudSettingsDialog

    app = QApplication.instance() or QApplication([])
    session = CloudSession(fake, store=HandleStore(tmp_path / "machine.json"))
    dialog = CloudSettingsDialog(session=session)
    dialog.show()
    page = dialog.pages["aws"]
    assert dialog.provider_buttons["aws"].text() == "AWS"
    assert not page._started  # nothing runs while the Local page shows

    dialog.provider_buttons["aws"].setChecked(True)
    spin(app, lambda: page._started and not page.busy and page.setup_checks is not None)
    assert page.aws_label.text() == fake_aws.script
    assert [page.profile_combo.itemText(i) for i in range(2)] == ["orange3-cloud", "other"]
    assert page.account_label.text() == "Not signed in"
    assert "Not signed in to AWS" in page.message_label.text()
    assert page.region_combo.count() == 0 and not page.region_combo.isEnabled()
    assert page.setup_checks[0].key == "credentials" and not page.setup_ok
    assert not dialog.start_button.isEnabled()

    # Signing in runs the CLI for the chosen profile, then lists the regions.
    QTest.mouseClick(page.sign_in_button, Qt.MouseButton.LeftButton)
    spin(app, lambda: page.region_combo.count() > 0 and not page.busy and page.setup_ok)
    assert fake_aws.marker.read_text().split() == ["login", "--profile", "orange3-cloud"]
    assert page.account_label.text() == "orange3-cloud in account 123456789012"
    assert page.sign_in_button.text() == "Sign in again"
    assert (page.profile, page.region) == ("orange3-cloud", "us-east-1")
    assert page.size_picker.machine_type == "m7i.4xlarge"  # Medium
    assert "The AWS account is ready" in page.setup_label.text()
    # The setup check passed, so Start is enabled.
    assert dialog.start_button.isEnabled()

    # The sizes the region offers, with their prices there.
    picker = page.size_picker
    items = [picker.combo.itemText(i) for i in range(picker.combo.count())]
    assert len(items) == 9 and all("/h" in t and "$" in t for t in items), items
    assert items[0].startswith("Small (testing) (m7i.xlarge): 4 CPUs, 16 GB, $0.2")
    assert f"in us-east-1 on {aws_prices.load_prices().date}" in picker.note_label.text()
    # The quota check is for the chosen size: 64 vCPUs don't fit the quota of 32.
    picker.combo.setCurrentIndex(picker.combo.findData("m7i.16xlarge"))
    picker.combo.activated.emit(picker.combo.currentIndex())
    spin(app, lambda: page.setup_checks is not None and not page.busy)
    quota = next(c for c in page.setup_checks if c.key == "quota")
    assert quota.detail == "32 of 32 vCPUs free; m7i.16xlarge needs 64."
    assert not page.setup_ok and not dialog.start_button.isEnabled()
    picker.combo.setCurrentIndex(picker.combo.findData("m7i.4xlarge"))
    picker.combo.activated.emit(picker.combo.currentIndex())
    spin(app, lambda: page.setup_checks is not None and not page.busy)
    assert page.setup_ok and dialog.start_button.isEnabled()

    # Choices are saved at once in the settings store, never in a workflow.
    page.region_combo.setCurrentText("eu-west-1")
    page.region_combo.activated.emit(page.region_combo.currentIndex())
    page.profile_combo.setCurrentIndex(1)
    page.profile_combo.activated.emit(1)
    spin(app, lambda: not page.busy and page.account_label.text().startswith("other"))
    # A size eu-west-1 doesn't offer is hidden.
    assert picker.combo.count() == 8 and picker.combo.findData("r7i.24xlarge") == -1
    assert "Sizes not offered here are hidden (1)." in picker.note_label.text()
    store = QSettings(os.environ["ORANGE3_CLOUD_SETTINGS_FILE"], QSettings.Format.IniFormat)
    assert store.value("orange3-cloud/provider") == "aws"
    assert store.value("orange3-cloud/providers/aws/account") == "other"
    assert store.value("orange3-cloud/providers/aws/region") == "eu-west-1"
    assert CloudSettings.load().of("aws").account == "other"

    # A profile without a region signs in with the chosen one.
    QTest.mouseClick(page.sign_in_button, Qt.MouseButton.LeftButton)
    spin(app, lambda: not page.busy)
    assert fake_aws.marker.read_text().split()[-2:] == ["--region", "eu-west-1"]
    dialog.close()
    page.shutdown()
    session.shutdown()


@pytest.mark.skipif(
    not os.environ.get("ORANGE3_CLOUD_AWS_PROFILE"),
    reason="needs this computer's AWS sign-in (ORANGE3_CLOUD_AWS_PROFILE)",
)
def test_real_account():
    """Read-only: the profile signs in to the test account, whose regions
    include the test region, which offers all the sizes."""
    profile = os.environ["ORANGE3_CLOUD_AWS_PROFILE"]
    region = os.environ["ORANGE3_CLOUD_AWS_REGION"]
    assert profile in {p.name for p in aws_account.list_profiles()}
    session = aws_account.load_session(profile)
    assert aws_account.identity(session).account == os.environ["ORANGE3_CLOUD_AWS_ACCOUNT"]
    assert region in aws_account.list_regions(session)
    assert aws_account.offered_instance_types(session, region) == {t.name for t in MACHINE_TYPES}
