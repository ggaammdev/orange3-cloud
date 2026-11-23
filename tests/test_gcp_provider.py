"""The Google Cloud machine provider with Compute Engine replaced by a fake
project, whose "VM" runs the API server in-process over HTTPS with the
token, certificate and key from the VM's metadata: Start creates the VM and
the firewall rule for this computer's address only, ``/v1/info`` works,
the rule follows a new address, the session reattaches after a restart, and
Stop deletes everything. Also the image choice, cloud-init, a failed boot,
leftover machines and the dialog's Start button.

``test_real_machine`` starts a Small machine in the development test project;
it runs only with ``ORANGE3_CLOUD_GCP_VM_TESTS=1`` and
``ORANGE3_CLOUD_GCP_PROJECT``/``_ZONE`` set (never in CI).
"""

import contextlib
import datetime
import json
import os
import socket
import time

import pytest
from AnyQt.QtWidgets import QApplication
from conftest import SPEC, https_server

from orangecontrib.cloud.client.providers import cloud_vm, gcp_vm
from orangecontrib.cloud.client.providers.base import MachineState, ProviderError
from orangecontrib.cloud.client.providers.gcp import OwnGCPProvider
from orangecontrib.cloud.client.providers.gcp_compute import ComputeApi, NotFoundError
from orangecontrib.cloud.client.providers.gcp_prices import load_prices
from orangecontrib.cloud.client.providers.gcp_vm import cloud_init, machine_image
from orangecontrib.cloud.client.providers.setup_checks import Check, Status
from orangecontrib.cloud.client.session import CloudSession, HandleStore
from orangecontrib.cloud.env import Package
from orangecontrib.cloud.server.app import ServerConfig, create_app
from orangecontrib.cloud.settings import CloudSettings, ProviderSettings

PROJECT, ZONE = "my-project", "europe-west1-b"
IP = "93.184.216.34"
SPEC_WITH_ADDON = SPEC.model_copy(
    update={"required": [Package(name="orange3-cloud", version="0.0.1.dev0")]}
)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeCompute(ComputeApi):
    """A project in memory; a new VM runs the server from its metadata."""

    def __init__(self, folder, boot_ok=True):
        self.folder = folder
        self.port = free_port()
        self.boot_ok = boot_ok
        self.instances = {}
        self.firewalls = {}
        self.calls = []
        self._servers = {}

    def default_service_account(self, project):
        return "123-compute@developer.gserviceaccount.com"

    def get_instance(self, project, zone, name):
        try:
            return self.instances[name]
        except KeyError:
            raise NotFoundError("not found") from None

    def insert_instance(self, project, zone, body):
        self.calls.append(("insert_instance", project, zone))
        name = body["name"]
        metadata = {i["key"]: i["value"] for i in body["metadata"]["items"]}
        instance = json.loads(json.dumps(body))
        instance["zone"] = f"https://compute/projects/{project}/zones/{zone}"
        instance["networkInterfaces"][0]["accessConfigs"][0]["natIP"] = "127.0.0.1"
        if not self.boot_ok:
            instance["status"] = "RUNNING"
            instance["serial"] = (
                "[bootstrap 1.0s] ERROR: no Python\n[bootstrap 1.0s] the machine did not start\n"
            )
            self.instances[name] = instance
            return
        config = ServerConfig(token=metadata["orange3-cloud-token"], data_dir=self.folder / name)
        stack = contextlib.ExitStack()
        cert_key = (metadata["orange3-cloud-tls-cert"], metadata["orange3-cloud-tls-key"])
        folder = self.folder / f"{name}-tls"
        folder.mkdir()
        stack.enter_context(https_server(create_app(config), folder, cert_key, self.port))
        self._servers[name] = stack
        instance["status"] = "RUNNING"
        instance["serial"] = "docker[1056]: [bootstrap   2.0s] installing Python 3.12.0\n"
        self.instances[name] = instance

    def stop_instance(self, project, zone, name):
        self.calls.append(("stop_instance", name))
        if name not in self.instances:
            raise NotFoundError("not found")
        self.instances[name]["status"] = "STOPPING"

    def delete_instance(self, project, zone, name):
        self.calls.append(("delete_instance", name))
        if self.instances.pop(name, None) is None:
            raise NotFoundError("not found")
        stack = self._servers.pop(name, None)
        if stack is not None:
            stack.close()

    def list_instances(self, project, label):
        return [i for i in self.instances.values() if i.get("labels", {}).get(label) == "true"]

    def serial_output(self, project, zone, name, start=0):
        text = self.get_instance(project, zone, name).get("serial", "")
        return text[start:], len(text)

    def get_firewall(self, project, name):
        try:
            return self.firewalls[name]
        except KeyError:
            raise NotFoundError("not found") from None

    def insert_firewall(self, project, body):
        self.calls.append(("insert_firewall", body["sourceRanges"]))
        self.firewalls[body["name"]] = dict(body)

    def patch_firewall(self, project, name, body):
        self.calls.append(("patch_firewall", body["sourceRanges"]))
        self.firewalls[name].update(body)

    def delete_firewall(self, project, name):
        self.calls.append(("delete_firewall", name))
        if self.firewalls.pop(name, None) is None:
            raise NotFoundError("not found")

    def close(self):
        for stack in self._servers.values():
            stack.close()


@pytest.fixture
def compute(tmp_path, monkeypatch):
    monkeypatch.setenv("ORANGE3_CLOUD_PUBLIC_IP", IP)
    monkeypatch.delenv("ORANGE3_CLOUD_GCP_IMAGE", raising=False)
    gcp = ProviderSettings(PROJECT, "europe-west1", ZONE)
    CloudSettings(provider="gcp", providers={"gcp": gcp}).save()
    api = FakeCompute(tmp_path)
    yield api
    api.close()


def provider_for(api):
    return OwnGCPProvider(api=api, port=api.port, poll_interval=0.05)


def session_for(api, tmp_path):
    return CloudSession(
        store=HandleStore(tmp_path / "state" / "machine.json"),
        resolve_provider=lambda provider_id: provider_for(api),
    )


def test_start_reattach_and_stop(compute, tmp_path, monkeypatch):
    session = session_for(compute, tmp_path)
    messages = []
    session.messageChanged.connect(messages.append)
    handle = session.start_machine("e2-standard-4", spec=SPEC_WITH_ADDON, wait=True)

    # The VM: settings fixed for the add-on.
    body = compute.instances[handle.name]
    assert handle.name == f"orange3-cloud-{handle.machine_id}"
    assert body["machineType"] == f"zones/{ZONE}/machineTypes/e2-standard-4"
    assert body["labels"] == {"orange3-cloud": "true", "orange3-cloud-session": handle.machine_id}
    assert body["tags"] == {"items": ["orange3-cloud"]}
    disk = body["disks"][0]
    assert disk["boot"] and disk["autoDelete"]
    assert disk["initializeParams"]["sourceImage"].endswith(
        "cos-cloud/global/images/family/cos-stable"
    )
    assert disk["initializeParams"]["diskType"] == f"zones/{ZONE}/diskTypes/pd-balanced"
    assert disk["initializeParams"]["diskSizeGb"] == "100"
    nic = body["networkInterfaces"][0]
    assert nic["network"] == "global/networks/default"
    assert nic["accessConfigs"][0]["type"] == "ONE_TO_ONE_NAT"
    assert body["serviceAccounts"][0]["scopes"] == list(gcp_vm.SCOPES)
    metadata = {i["key"]: i["value"] for i in body["metadata"]["items"]}
    assert metadata["orange3-cloud-token"] == handle.token
    assert metadata["orange3-cloud-tls-cert"] == handle.cert_pem
    assert "PRIVATE KEY" in metadata["orange3-cloud-tls-key"]
    assert json.loads(metadata["orange3-cloud-env-spec"])["python_version"] == "3.12.0"
    assert metadata["orange3-cloud-idle-timeout"] == str(CloudSettings().idle_timeout)
    dev_image = f"europe-west1-docker.pkg.dev/{PROJECT}/orange3-cloud/machine:dev"
    assert dev_image in metadata["user-data"]
    # The handle: HTTPS to the VM's address, the certificate checked against
    # its fixed name; no key in the handle.
    assert handle.url == f"https://127.0.0.1:{compute.port}"
    assert handle.server_hostname == f"{handle.name}.orange3-cloud"
    assert handle.extra["project"] == PROJECT and handle.extra["zone"] == ZONE
    assert "PRIVATE KEY" not in json.dumps(handle.to_dict())
    assert session.info().api_version
    assert "Starting: installing Python 3.12.0" in messages

    # The firewall allows only this computer's address, to the tag, on 8443.
    rule = compute.firewalls["orange3-cloud-https"]
    assert rule["sourceRanges"] == [f"{IP}/32"]
    assert rule["targetTags"] == ["orange3-cloud"]
    assert rule["allowed"] == [{"IPProtocol": "tcp", "ports": ["8443"]}]
    assert rule["direction"] == "INGRESS"

    # Orange restarts on a new network: the rule follows, and the new
    # session reconnects to the same machine.
    session.shutdown()
    monkeypatch.setenv("ORANGE3_CLOUD_PUBLIC_IP", "8.8.4.4")
    again = session_for(compute, tmp_path)
    assert again.reattach(wait=True).machine_id == handle.machine_id
    assert again.is_connected and again.info().api_version
    assert compute.firewalls["orange3-cloud-https"]["sourceRanges"] == ["8.8.4.4/32"]
    assert again.leftovers() == []

    # Stop deletes the VM and the firewall rule.
    again.stop_machine(wait=True)
    assert compute.instances == {} and compute.firewalls == {}
    assert again.store.load() is None
    again.shutdown()


def test_failed_boot_deletes_the_machine(tmp_path, monkeypatch):
    monkeypatch.setenv("ORANGE3_CLOUD_PUBLIC_IP", IP)
    api = FakeCompute(tmp_path, boot_ok=False)
    provider = OwnGCPProvider(api=api, project=PROJECT, zone=ZONE, poll_interval=0.01)
    with pytest.raises(ProviderError, match="couldn't start") as error:
        provider.start(SPEC_WITH_ADDON, "e2-standard-4", timeout=30)
    assert "ERROR: no Python" in str(error.value)
    assert api.instances == {} and api.firewalls == {}
    with pytest.raises(ProviderError, match="isn't one the add-on offers"):
        provider.start(SPEC_WITH_ADDON, "n2-highmem-256")


def test_leftovers(compute, tmp_path):
    provider = provider_for(compute)
    handle = provider.start(SPEC_WITH_ADDON, "e2-standard-4")
    compute.instances["orange3-cloud-0123456789ab"] = {
        "name": "orange3-cloud-0123456789ab",
        "zone": f"zones/{ZONE}",
        "status": "TERMINATED",
        "labels": {"orange3-cloud": "true", "orange3-cloud-session": "0123456789ab"},
    }
    listed = {h.name: h for h in provider.list_machines()}
    assert set(listed) == {handle.name, "orange3-cloud-0123456789ab"}
    assert listed[handle.name].url == handle.url and not listed[handle.name].token
    assert provider.status(listed["orange3-cloud-0123456789ab"]).state is MachineState.STOPPED
    assert provider.delete_leftovers(keep=handle) == ["orange3-cloud-0123456789ab"]
    assert set(compute.instances) == {handle.name} and compute.firewalls
    provider.delete(handle)
    assert compute.instances == {} and compute.firewalls == {}


def test_stop_time_and_stopped_price(compute):
    """When a stopped VM stopped: its ``lastStopTimestamp``. Stopped, it costs
    its disk (its ephemeral address is gone)."""
    provider = provider_for(compute)
    compute.instances["vm"] = {  # as Compute Engine describes one that powered itself off
        "name": "vm",
        "zone": f"https://compute/projects/{PROJECT}/zones/{ZONE}",
        "machineType": f"https://compute/projects/{PROJECT}/zones/{ZONE}/machineTypes/e2-standard-4",
        "status": "TERMINATED",
        "creationTimestamp": "2026-11-20T00:08:24.970-08:00",
        "lastStartTimestamp": "2026-11-20T00:08:32.784-08:00",
        "lastStopTimestamp": "2026-11-20T00:13:22.656-08:00",
        "disks": [{"boot": True, "diskSizeGb": "200"}],
    }
    handle = provider._listed_handle(PROJECT, compute.instances["vm"])
    utc = datetime.timezone.utc
    stopped = datetime.datetime(2026, 11, 20, 8, 13, 22, 656000, tzinfo=utc)
    assert provider.stopped_at(handle) == stopped
    disk = load_prices().disk_per_hour(200, "europe-west1")
    assert provider.stopped_price_per_hour(handle) == disk > 0
    compute.instances["vm"]["status"] = "RUNNING"
    assert provider.stopped_at(handle) is None
    del compute.instances["vm"]
    assert provider.stopped_at(handle) is None


def test_image_and_cloud_init(monkeypatch):
    monkeypatch.delenv("ORANGE3_CLOUD_GCP_IMAGE", raising=False)
    public = "ghcr.io/ggaammdev/orange3-cloud-machine:1.0.0"
    assert machine_image("1.0.0", "p", "us-east1") == public
    dev = "us-east1-docker.pkg.dev/p/orange3-cloud/machine:dev"
    assert machine_image("0.0.1.dev0", "p", "us-east1") == dev
    assert machine_image("1.0.0+local", "p", "us-east1") == dev
    monkeypatch.setenv("ORANGE3_CLOUD_GCP_IMAGE", "example.org/machine:x")
    assert machine_image("1.0.0", "p", "us-east1") == "example.org/machine:x"

    private = cloud_init(dev)
    assert private.startswith("#cloud-config\n")
    assert "docker-credential-gcr configure-docker --registries=us-east1-docker.pkg.dev" in private
    assert f"docker pull --quiet {dev}" in private
    assert "--network host" in private and "ORANGE3_CLOUD_POWER_OFF=host" in private
    assert "SuccessAction=poweroff" in private and "FailureAction=poweroff" in private
    assert "--dport 8443 -j ACCEPT" in private
    assert "configure-docker" not in cloud_init(public)


def test_dialog_start_needs_the_setup_check(compute, tmp_path):
    from orangecontrib.cloud.gui.settings_dialog import CloudSettingsDialog

    app = QApplication.instance() or QApplication([])
    session = session_for(compute, tmp_path)
    compute.instances["orange3-cloud-0123456789ab"] = {
        "name": "orange3-cloud-0123456789ab",
        "zone": f"zones/{ZONE}",
        "status": "RUNNING",
        "labels": {"orange3-cloud": "true", "orange3-cloud-session": "0123456789ab"},
    }
    dialog = CloudSettingsDialog(None, session)
    assert dialog.provider_id == "gcp"
    assert not dialog.start_button.isEnabled()  # not checked yet

    dialog.pages["gcp"].setup_checks = [Check("credentials", "Signed in", Status.PASSED)]
    dialog.pages["gcp"].setupChanged.emit()
    assert dialog.start_button.isEnabled()
    # The leftover machine shows, with a button to delete it.
    deadline = time.monotonic() + 10
    while not dialog.leftovers:
        assert time.monotonic() < deadline
        app.processEvents()
        time.sleep(0.01)
    assert dialog.leftovers == ["orange3-cloud-0123456789ab"]
    assert not dialog.leftovers_button.isHidden()

    dialog.pages["gcp"].setup_checks = [Check("billing", "Billing", Status.FAILED)]
    dialog.pages["gcp"].setupChanged.emit()
    assert not dialog.start_button.isEnabled()
    dialog.pages["gcp"].shutdown()
    dialog.close()
    session.shutdown()


@pytest.mark.skipif(
    os.environ.get("ORANGE3_CLOUD_GCP_VM_TESTS") != "1"
    or not os.environ.get("ORANGE3_CLOUD_GCP_PROJECT"),
    reason="starts a Small VM in the test project; set ORANGE3_CLOUD_GCP_VM_TESTS=1",
)
def test_real_machine(tmp_path):
    from orangecontrib.cloud.env import collect_environment

    project = os.environ["ORANGE3_CLOUD_GCP_PROJECT"]
    zone = os.environ["ORANGE3_CLOUD_GCP_ZONE"]
    gcp = ProviderSettings(account=project, zone=zone)
    CloudSettings(provider="gcp", providers={"gcp": gcp}).save()
    store = HandleStore(tmp_path / "machine.json")
    session = CloudSession(store=store)
    try:
        handle = session.start_machine("e2-standard-4", spec=collect_environment(), wait=True)
        assert session.info().machine == "e2-standard-4"
        rule = session.provider.api.get_firewall(project, "orange3-cloud-https")
        assert rule["sourceRanges"] == [f"{cloud_vm.public_ip()}/32"]
        session.shutdown()
        session = CloudSession(store=store)
        assert session.reattach(wait=True).machine_id == handle.machine_id
    finally:
        session.stop_machine(wait=True)
        provider = OwnGCPProvider(project=project, zone=zone)
        assert provider.list_machines() == []
        with pytest.raises(NotFoundError):
            provider.api.get_firewall(project, "orange3-cloud-https")
