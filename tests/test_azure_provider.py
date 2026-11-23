"""The Azure machine provider with Resource Manager replaced by a fake
subscription (no network): the fake answers the provider's REST requests,
reads the VM's body through the Azure SDK's own model, and a new VM runs the
API server in-process over HTTPS with the token, certificate and key from
its custom data. Start creates the shared pieces, the VM and its role, the
network security group allows only this computer's address, ``/v1/info``
works, the rule follows a new address, the session reattaches after a
restart, and Stop deletes everything but the resource group and the
network. Also a failed boot, leftover machines and a change of region.

``test_real_machine`` and ``test_real_machine_deallocates_itself`` start a
Small machine in the development test subscription; they run only with
``ORANGE3_CLOUD_AZURE_VM_TESTS=1`` and ``ORANGE3_CLOUD_AZURE_SUBSCRIPTION``/
``_REGION`` set (never in CI).
"""

import base64
import contextlib
import datetime
import json
import os
import time
import urllib.parse

import pytest
from azure.mgmt.compute.models import VirtualMachine
from conftest import https_server
from test_aws_provider import cloud_config, settings_files
from test_gcp_provider import SPEC_WITH_ADDON, free_port

from orangecontrib.cloud.client.providers import azure, azure_prices, cloud_vm
from orangecontrib.cloud.client.providers.azure import AzureProvider
from orangecontrib.cloud.client.providers.azure_arm import Arm
from orangecontrib.cloud.client.providers.azure_vm import POWER_ROLE_ID, allows_only
from orangecontrib.cloud.client.providers.base import MachineState, ProviderError
from orangecontrib.cloud.client.providers.cloud_init import SETTINGS_DIR
from orangecontrib.cloud.client.session import CloudSession, HandleStore
from orangecontrib.cloud.server.app import ServerConfig, create_app
from orangecontrib.cloud.settings import CloudSettings, ProviderSettings

SUBSCRIPTION, REGION = "00000000-0000-0000-0000-00000000000a", "westeurope"
IP = "93.184.216.34"
GROUP = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/orange3-cloud"
NETWORK = f"{GROUP}/providers/Microsoft.Network/virtualNetworks/orange3-cloud"
NSG = f"{GROUP}/providers/Microsoft.Network/networkSecurityGroups/orange3-cloud-https"
#: The API version of each resource provider's requests.
API = {
    "": "2021-04-01",
    "Microsoft.Compute": "2024-07-01",
    "Microsoft.Network": "2024-05-01",
    "Microsoft.Authorization": "2022-04-01",
    "Microsoft.Storage": "2025-01-01",
}


class Response:
    def __init__(self, status, body=None):
        self.status_code = status
        self.content = json.dumps(body).encode() if body is not None else b""


def failed(status, code):
    return Response(status, {"error": {"code": code, "message": f"{code} (fake)"}})


def copy(value):
    return json.loads(json.dumps(value))


class FakeAzure:
    """The add-on's resource group in a subscription, in memory; a new VM
    runs the server from its custom data."""

    def __init__(self, folder, boot_ok=True):
        self.folder, self.boot_ok = folder, boot_ok
        self.port = free_port()
        self.resources, self.calls, self.vm_requests, self.logs = {}, [], {}, {}
        self.power, self.unknown_principals = {}, 1
        self.provisioned = {}  # VM name -> time of its provisioning status
        self._servers = {}

    def arm(self, subscription):
        assert subscription == SUBSCRIPTION
        return Arm(self.send, subscription, fetch=self.fetch)

    def close(self):
        for stack in self._servers.values():
            stack.close()

    def fetch(self, url):
        return self.logs[url.split("/")[-1].split(".log")[0]]

    def of_type(self, kind):
        return {p: r for p, r in self.resources.items() if f"/{kind}/" in p}

    # Requests

    def send(self, request):
        url = urllib.parse.urlsplit(request.url)
        path, query = url.path, dict(urllib.parse.parse_qsl(url.query))
        namespace = path.rsplit("/providers/", 1)[-1].split("/")[0] if "/providers/" in path else ""
        assert query["api-version"] == API[namespace], (path, query)
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path))
        if request.method == "POST":
            return self.action(path)
        if "/roleAssignments/" in path:
            return self.role_assignment(request.method, path, body)
        if path.endswith("/virtualMachines"):
            return self.list_vms()
        if "/virtualMachines/" in path:
            return getattr(self, f"{request.method.lower()}_vm")(path, body, query)
        return getattr(self, request.method.lower())(path, body)

    def get(self, path, body=None):
        if path not in self.resources:
            return failed(404, "ResourceNotFound")
        return Response(200, self.resources[path])

    def put(self, path, body):
        if path != GROUP and GROUP not in self.resources:
            return failed(404, "ResourceGroupNotFound")
        name = path.rsplit("/", 1)[-1]
        properties = {**body.get("properties", {}), "provisioningState": "Succeeded"}
        self.resources[path] = {**copy(body), "id": path, "name": name, "properties": properties}
        return Response(201, self.resources[path])

    def delete(self, path, body=None):
        users = [n for n in self.of_type("networkInterfaces").values() if n["nsg"] == path]
        if users:
            return failed(400, "InUseNetworkSecurityGroup")
        return Response(202 if self.resources.pop(path, None) else 204)

    def list_vms(self):
        if GROUP not in self.resources:
            return failed(404, "ResourceGroupNotFound")
        return Response(200, {"value": list(self.of_type("virtualMachines").values())})

    # VMs

    def put_vm(self, path, body, query):
        name = path.rsplit("/", 1)[-1]
        self.vm_requests[name] = body
        model = VirtualMachine(body)  # the SDK's model reads every field the tests check
        nic_id = f"{GROUP}/providers/Microsoft.Network/networkInterfaces/{name}-nic"
        ip_id = f"{GROUP}/providers/Microsoft.Network/publicIPAddresses/{name}-ip-1a2b3c4d"
        interface = model.properties.network_profile.network_interface_configurations[0]
        self.resources[nic_id] = {
            "id": nic_id,
            "nsg": interface.properties.network_security_group.id,
            "properties": {
                "ipConfigurations": [{"properties": {"publicIPAddress": {"id": ip_id}}}]
            },
        }
        self.resources[ip_id] = {"id": ip_id, "properties": {"ipAddress": "127.0.0.1"}}
        properties = {k: v for k, v in body["properties"].items() if k != "osProfile"}
        properties["networkProfile"] = {"networkInterfaces": [{"id": nic_id}]}
        properties["provisioningState"] = "Succeeded"
        self.resources[path] = vm = {
            **{k: v for k, v in body.items() if k != "properties"},
            "id": path,
            "name": name,
            "identity": {"type": "SystemAssigned", "principalId": f"principal-{name}"},
            "properties": properties,
        }
        self.power[name] = "running"
        self.boot(name, body["properties"]["osProfile"]["customData"])
        return Response(201, vm)

    def boot(self, name, custom_data):
        if not self.boot_ok:
            self.logs[name] = (
                "[bootstrap    1.0s] ERROR: no Python\n"
                "[bootstrap    1.0s] the machine did not start\n"
            )
            return
        settings = settings_files(cloud_config(base64.b64decode(custom_data)))
        folder = self.folder / name
        folder.mkdir()
        stack = contextlib.ExitStack()
        app = create_app(ServerConfig(token=settings["token"], data_dir=folder))
        cert_key = (settings["tls-cert"], settings["tls-key"])
        stack.enter_context(https_server(app, folder, cert_key, self.port))
        self._servers[name] = stack
        self.logs[name] = (
            "docker[1056]: \x1b[0m[bootstrap    2.0s] installing Python 3.12.0\x1b[0m\n"
        )

    def get_vm(self, path, body, query):
        if path not in self.resources:
            return failed(404, "ResourceNotFound")
        vm = copy(self.resources[path])
        if query.get("$expand") == "instanceView":
            statuses = [{"code": "ProvisioningState/succeeded"}]
            if vm["name"] in self.provisioned:
                statuses[0]["time"] = self.provisioned[vm["name"]]
            statuses.append({"code": f"PowerState/{self.power[vm['name']]}"})
            vm["properties"]["instanceView"] = {"statuses": statuses}
        return Response(200, vm)

    def delete_vm(self, path, body, query):
        vm = self.resources.pop(path, None)
        if vm is None:
            return Response(204)
        assert not [p for p in self.resources if p.startswith(path + "/")], "role assignment left"
        name = vm["name"]
        for kind in ("networkInterfaces", "publicIPAddresses"):
            for found in [p for p in self.of_type(kind) if f"/{name}-" in p]:
                del self.resources[found]  # deleted with the VM
        self.stop_server(name)
        return Response(202)

    def stop_server(self, name):
        stack = self._servers.pop(name, None)
        if stack is not None:
            stack.close()

    def action(self, path):
        vm_path, action = path.rsplit("/", 1)
        name = vm_path.rsplit("/", 1)[-1]
        if vm_path not in self.resources:
            return failed(404, "ResourceNotFound")
        if action == "deallocate":
            self.power[name] = "deallocated"
            self.stop_server(name)
            return Response(202)
        assert action == "retrieveBootDiagnosticsData"
        return Response(200, {"serialConsoleLogBlobUri": f"https://blob.example/{name}.log?sig=s"})

    def role_assignment(self, method, path, body):
        scope = path.split("/providers/Microsoft.Authorization/")[0]
        if method == "DELETE":
            return Response(200 if self.resources.pop(path, None) else 204)
        assert method == "PUT" and scope in self.resources
        if self.unknown_principals:  # Azure doesn't know the new identity yet
            self.unknown_principals -= 1
            return failed(400, "PrincipalNotFound")
        self.resources[path] = {"id": path, "scope": scope, **copy(body)}
        return Response(201, self.resources[path])


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("ORANGE3_CLOUD_PUBLIC_IP", IP)
    monkeypatch.delenv("ORANGE3_CLOUD_AZURE_IMAGE", raising=False)
    monkeypatch.setattr(azure, "registry_token", lambda image: "reader-token")
    providers = {
        "azure": ProviderSettings(SUBSCRIPTION, REGION),
        "gcp": ProviderSettings("my-project", "europe-west1", "europe-west1-b"),
    }
    CloudSettings(provider="azure", providers=providers).save()
    account = FakeAzure(tmp_path)
    yield account
    account.close()


def provider_for(account):
    provider = AzureProvider(make_arm=account.arm, port=account.port, poll_interval=0.01)
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
    handle = session.start_machine("Standard_D4s_v6", spec=SPEC_WITH_ADDON, wait=True)

    # The VM, read through the SDK's model: settings fixed for the add-on.
    vm = VirtualMachine(fake.vm_requests[handle.name])
    properties = vm.properties
    assert (vm.location, properties.hardware_profile.vm_size) == (REGION, "Standard_D4s_v6")
    assert vm.tags == {"orange3-cloud": "true", "orange3-cloud-session": handle.machine_id}
    assert vm.identity.type == "SystemAssigned"
    image = properties.storage_profile.image_reference
    assert (image.publisher, image.offer, image.sku) == ("Canonical", "ubuntu-26_04-lts", "server")
    disk = properties.storage_profile.os_disk
    assert (disk.disk_size_gb, disk.managed_disk.storage_account_type) == (100, "Premium_LRS")
    assert properties.storage_profile.disk_controller_type == "NVMe"
    nic = properties.network_profile.network_interface_configurations[0].properties
    address = nic.ip_configurations[0].properties.public_ip_address_configuration
    assert disk.delete_option == nic.delete_option == address.properties.delete_option == "Delete"
    assert (address.sku.name, address.properties.public_ip_allocation_method) == (
        "Standard",
        "Static",
    )
    assert nic.network_security_group.id == NSG
    assert nic.ip_configurations[0].properties.subnet.id == f"{NETWORK}/subnets/default"
    assert properties.security_profile.security_type == "TrustedLaunch"
    assert properties.os_profile.linux_configuration.disable_password_authentication

    # The custom data: settings files, Docker, and the unit that runs the
    # development image for the bootstrap's azure platform and deallocates
    # the VM when the container exits.
    data = base64.b64decode(properties.os_profile.custom_data)
    assert len(data) < 64 * 1024
    config = cloud_config(data)
    settings = settings_files(config)
    assert settings["token"] == handle.token and settings["machine"] == "Standard_D4s_v6"
    assert "PRIVATE KEY" in settings["tls-key"]
    assert config["packages"] == ["docker.io"]
    files = {f["path"]: f for f in config["write_files"]}
    assert files["/run/orange3-cloud/registry-token"]["content"] == "reader-token"
    assert all(files[f"{SETTINGS_DIR}/{n}"]["permissions"] == "0600" for n in settings)
    unit = files["/etc/systemd/system/orange3-cloud.service"]["content"]
    assert "ORANGE3_CLOUD_CONFIG=azure" in unit and "ORANGE3_CLOUD_POWER_OFF=host" in unit
    assert "ExecStopPost=/usr/local/sbin/orange3-cloud-deallocate" in unit
    assert "SuccessAction=poweroff" not in unit
    script = files["/usr/local/sbin/orange3-cloud-deallocate"]
    assert script["permissions"] == "0755" and "/deallocate?api-version=" in script["content"]

    # The VM's identity may deallocate the VM, and nothing else.
    (assignment,) = [r for p, r in fake.resources.items() if "/roleAssignments/" in p]
    assert (
        assignment["scope"] == f"{GROUP}/providers/Microsoft.Compute/virtualMachines/{handle.name}"
    )
    role = assignment["properties"]
    assert role["roleDefinitionId"].endswith(f"/roleDefinitions/{POWER_ROLE_ID}")
    assert role["principalId"] == f"principal-{handle.name}"
    assert role["principalType"] == "ServicePrincipal"
    assert handle.extra["role_assignment"] == assignment["id"]

    # The handle: HTTPS to the VM's address, the certificate checked against
    # its fixed name; no key in the handle.
    assert handle.url == f"https://127.0.0.1:{fake.port}"
    assert handle.server_hostname == f"{handle.name}.orange3-cloud"
    assert (handle.extra["subscription"], handle.extra["region"]) == (SUBSCRIPTION, REGION)
    assert "PRIVATE KEY" not in json.dumps(handle.to_dict())
    # Its price when it started (VM, 100 GB Premium SSD, public IP), for the costs.
    price = azure_prices.per_hour("Standard_D4s_v6", REGION, 100)
    assert handle.extra["price_per_hour"] == f"{price:.6f}"
    assert session.price_per_hour() == pytest.approx(price, abs=1e-6)
    assert session.info().api_version
    assert "Starting: installing Python 3.12.0" in messages

    # The shared pieces; the network security group allows only this
    # computer's address, on 8443.
    assert fake.resources[GROUP]["tags"] == {"orange3-cloud": "true"}
    assert fake.resources[NETWORK]["location"] == REGION
    assert allows_only(fake.resources[NSG], f"{IP}/32")

    # Orange restarts on a new network: the rule follows, and the new
    # session reconnects to the same machine.
    session.shutdown()
    monkeypatch.setenv("ORANGE3_CLOUD_PUBLIC_IP", "8.8.4.4")
    again = session_for(fake, tmp_path)
    assert again.reattach(wait=True).machine_id == handle.machine_id
    assert again.is_connected and again.info().api_version
    assert allows_only(fake.resources[NSG], "8.8.4.4/32")
    assert again.leftovers() == []

    # Stop deallocates, removes the role, deletes the VM (its disk, interface
    # and address go with it) and the network security group; the resource
    # group and the network stay for the next machine.
    again.stop_machine(wait=True)
    assert (
        "POST",
        f"{GROUP}/providers/Microsoft.Compute/virtualMachines/{handle.name}/deallocate",
    ) in fake.calls
    assert set(fake.resources) == {GROUP, NETWORK}
    assert again.store.load() is None
    again.shutdown()


def test_failed_boot_deletes_the_machine(tmp_path, fake):
    account = FakeAzure(tmp_path / "failing", boot_ok=False)
    provider = provider_for(account)
    with pytest.raises(ProviderError, match="couldn't start") as raised:
        provider.start(SPEC_WITH_ADDON, "Standard_D4s_v6", timeout=30)
    assert "ERROR: no Python" in str(raised.value)
    assert set(account.resources) == {GROUP, NETWORK}
    with pytest.raises(ProviderError, match="isn't one the add-on offers"):
        provider.start(SPEC_WITH_ADDON, "Standard_E192s_v6")


def test_leftovers_and_regions(fake):
    provider = provider_for(fake)
    handle = provider.start(SPEC_WITH_ADDON, "Standard_D4s_v6")
    left = f"{GROUP}/providers/Microsoft.Compute/virtualMachines/orange3-cloud-0123456789ab"
    fake.resources[left] = {
        "id": left,
        "name": "orange3-cloud-0123456789ab",
        "location": REGION,
        "tags": {"orange3-cloud": "true", "orange3-cloud-session": "0123456789ab"},
        "properties": {"hardwareProfile": {"vmSize": "Standard_D4s_v6"}},
    }
    fake.power["orange3-cloud-0123456789ab"] = "deallocated"
    listed = {h.name: h for h in provider.list_machines()}
    assert set(listed) == {handle.name, "orange3-cloud-0123456789ab"}
    assert listed[handle.name].machine_type == "Standard_D4s_v6" and not listed[handle.name].token
    status = provider.status(listed["orange3-cloud-0123456789ab"])
    assert (status.state, status.message) == (MachineState.STOPPED, "The machine stopped.")
    assert provider.delete_leftovers(keep=handle) == ["orange3-cloud-0123456789ab"]
    assert left not in fake.resources and NSG in fake.resources

    # Machines can't be in two regions at once; once the last one is gone,
    # the shared pieces move to the region chosen now.
    settings = CloudSettings.load()
    settings.providers["azure"] = ProviderSettings(SUBSCRIPTION, "uksouth")
    settings.save()
    with pytest.raises(ProviderError, match="runs in westeurope"):
        provider.start(SPEC_WITH_ADDON, "Standard_D4s_v6")
    provider.delete(handle)
    handle = provider.start(SPEC_WITH_ADDON, "Standard_D4s_v6")
    assert fake.resources[NETWORK]["location"] == fake.resources[NSG]["location"] == "uksouth"
    provider.delete(handle)
    assert set(fake.resources) == {GROUP, NETWORK}


def test_stop_time_and_stopped_price(fake):
    """When a deallocated VM stopped: the time of its provisioning status in
    its instance view, which the deallocation sets. Deallocated, it costs
    its disk and its public address; powered off but still allocated, it
    costs compute until it is deleted."""
    provider = provider_for(fake)
    vm = {  # as Resource Manager describes one that deallocated itself
        "name": "vm",
        "location": REGION,
        "tags": {"orange3-cloud": "true", "orange3-cloud-session": "0123456789ab"},
        "properties": {
            "timeCreated": "2026-11-20T08:15:16.2209629+00:00",
            "hardwareProfile": {"vmSize": "Standard_D4s_v6"},
            "storageProfile": {"osDisk": {"diskSizeGB": 200}},
        },
    }
    fake.resources[f"{GROUP}/providers/Microsoft.Compute/virtualMachines/vm"] = vm
    fake.power["vm"], fake.provisioned["vm"] = "deallocated", "2026-11-20T08:19:02.6156023+00:00"
    handle = provider._listed_handle(SUBSCRIPTION, vm)
    utc = datetime.timezone.utc
    assert provider.stopped_at(handle) == datetime.datetime(2026, 11, 20, 8, 19, 2, 615602, utc)
    stopped = (
        azure_prices.disk_per_hour(200, REGION)
        + azure_prices.load_prices().region(REGION)["public_ip_hour"]
    )
    assert provider.stopped_price_per_hour(handle) == pytest.approx(stopped)
    fake.power["vm"] = "running"
    assert provider.stopped_at(handle) is None
    fake.power["vm"] = "stopped"
    now = datetime.datetime.now(utc)
    assert now <= provider.stopped_at(handle) <= now + datetime.timedelta(seconds=10)


# ----------------------------------------------------------------------
# On Azure

REAL = pytest.mark.skipif(
    os.environ.get("ORANGE3_CLOUD_AZURE_VM_TESTS") != "1"
    or not os.environ.get("ORANGE3_CLOUD_AZURE_SUBSCRIPTION"),
    reason="starts a Small VM in the test subscription; set ORANGE3_CLOUD_AZURE_VM_TESTS=1",
)


def real_settings(idle_timeout=1800):
    """The test subscription and region (and the Google Cloud project, for
    the development image) in the Cloud settings."""
    subscription = os.environ["ORANGE3_CLOUD_AZURE_SUBSCRIPTION"]
    providers = {"azure": ProviderSettings(subscription, os.environ["ORANGE3_CLOUD_AZURE_REGION"])}
    if os.environ.get("ORANGE3_CLOUD_GCP_PROJECT"):
        providers["gcp"] = ProviderSettings(
            os.environ["ORANGE3_CLOUD_GCP_PROJECT"], os.environ["ORANGE3_CLOUD_GCP_REGION"]
        )
    CloudSettings("azure", idle_timeout, providers=providers).save()
    return AzureProvider().arm(subscription)


def check_nothing_left(arm, handle):
    """The machine's VM, disk, network interface, public address and role
    assignment, and the network security group, are gone (the resource
    group, the virtual network and the storage account for uploads may
    stay)."""
    assert AzureProvider().list_machines() == []
    network = "/providers/Microsoft.Network"
    gone = {
        arm.paths.vm(handle.name): "2024-07-01",
        f"{arm.paths.group}/providers/Microsoft.Compute/disks/{handle.name}-disk": "2024-07-01",
        f"{arm.paths.group}{network}/networkInterfaces/{handle.name}-nic": "2024-05-01",
        arm.paths.security_group: "2024-05-01",
        handle.extra["role_assignment"]: "2022-04-01",
    }
    for path, api in gone.items():
        assert arm.get(path, "check", api=api) is None, path
    addresses = arm.list(f"{arm.paths.group}{network}/publicIPAddresses", "list", api="2024-05-01")
    assert addresses == []
    # Resource Manager's list of everything lags a little.
    staying = {"Microsoft.Network/virtualNetworks", "Microsoft.Storage/storageAccounts"}
    deadline = time.monotonic() + 120
    while not set(kinds := resource_types(arm)) <= staying:
        assert time.monotonic() < deadline, kinds
        time.sleep(10)
    print("Left in the resource group:", kinds)


def resource_types(arm):
    """The types of the resources Resource Manager lists in the resource group."""
    resources = arm.list(f"{arm.paths.group}/resources", "list", api="2021-04-01")
    return sorted(r["type"] for r in resources)


@REAL
def test_real_machine(tmp_path):
    from orangecontrib.cloud.env import collect_environment

    arm = real_settings()
    store = HandleStore(tmp_path / "machine.json")
    session = CloudSession(store=store)
    messages = []
    session.messageChanged.connect(messages.append)
    try:
        handle = session.start_machine("Standard_D4s_v6", spec=collect_environment(), wait=True)
        print(f"/v1/info after {handle.extra['boot_seconds']} s", *messages, sep="\n  ")
        assert session.info().machine == "Standard_D4s_v6"
        group = arm.get(arm.paths.security_group, "read the rule", api="2024-05-01")
        assert allows_only(group, f"{cloud_vm.public_ip()}/32")
        assert arm.get(handle.extra["role_assignment"], "read the role", api="2022-04-01")
        session.shutdown()
        session = CloudSession(store=store)
        assert session.reattach(wait=True).machine_id == handle.machine_id
    finally:
        stopping = time.monotonic()
        session.stop_machine(wait=True)
        print(f"stopped and deleted in {time.monotonic() - stopping:.0f} s")
        session.shutdown()
    check_nothing_left(arm, handle)


@REAL
def test_real_machine_deallocates_itself(tmp_path):
    """Orange quits; after its idle timeout the machine deallocates its own
    VM, which stays deallocated (with its disk and public address) until the
    next session deletes it and records its cost: running until it was
    deallocated, then its disk and address."""
    from orangecontrib.cloud.env import collect_environment

    idle = 120
    arm = real_settings(idle)
    store = HandleStore(tmp_path / "machine.json")
    session = CloudSession(store=store)
    try:
        handle = session.start_machine("Standard_D4s_v6", spec=collect_environment(), wait=True)
        print(f"/v1/info after {handle.extra['boot_seconds']} s")
        session.shutdown()
        closed = time.monotonic()
        states = []
        while (power := real_power(arm, handle.name)) != "deallocated":
            if not states or states[-1][1] != power:
                states.append((round(time.monotonic() - closed), power))
            assert time.monotonic() - closed < idle + 600, states
            time.sleep(5)
        print(f"deallocated {time.monotonic() - closed:.0f} s after Orange closed:", states)
        # It stays deallocated, with its disk and public address, until
        # Orange opens again.
        time.sleep(60)
        assert real_power(arm, handle.name) == "deallocated"
        network = f"{arm.paths.group}/providers/Microsoft.Network"
        addresses = arm.list(f"{network}/publicIPAddresses", "list", api="2024-05-01")
        names = [a["name"] for a in addresses]  # Azure adds a suffix to the name
        assert len(names) == 1 and names[0].startswith(f"{handle.name}-ip"), names
        session = CloudSession(store=store)
        reattaching = time.monotonic()
        assert session.reattach(wait=True) is None
        print(f"Reattach ({time.monotonic() - reattaching:.0f} s):", session.message)
        assert session.message.startswith("The machine stopped") and store.load() is None
        (entry,) = session.costs.entries()
        print("Recorded:", entry)
        assert entry.stopped_seconds >= 60 and not entry.approximate
        assert 0 < entry.stopped_price_per_hour < entry.price_per_hour
    finally:
        session.stop_machine(wait=True)
        session.shutdown()
    check_nothing_left(arm, handle)


def real_power(arm, name):
    from orangecontrib.cloud.client.providers.azure_vm import vm_states

    return vm_states(arm.vm(name, instance_view=True) or {})[1]
