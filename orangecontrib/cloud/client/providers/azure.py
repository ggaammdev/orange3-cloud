"""The machine in the user's own Azure subscription.

:class:`AzureProvider` signs in with the Azure CLI (see
:mod:`~orangecontrib.cloud.client.providers.azure_account`), works in the
subscription chosen in the Cloud settings (its ``account``), offers the
Azure sizes (:mod:`~orangecontrib.cloud.client.providers.azure_vm`), and the
settings dialog's Azure page runs the setup checker
(:mod:`~orangecontrib.cloud.client.providers.azure_setup`). It starts one VM
per machine, with the settings fixed for the add-on (see ``docs/dev.md``,
"Azure machines"):

* **Shared pieces** in the resource group ``orange3-cloud``, created once in
  the chosen region (:mod:`~orangecontrib.cloud.client.providers.
  azure_network`): the group, the virtual network ``orange3-cloud`` with one
  subnet, and the network security group ``orange3-cloud-https``, which
  allows TCP 8443 only from this computer's public IPv4 address (``/32``).
  :meth:`AzureProvider.ensure_access` updates it when the address changes,
  and it is deleted with the last machine.
* **VM:** the chosen size, the Ubuntu LTS image, a Premium SSD OS disk of
  the chosen size, a network interface with a Standard public IP address
  (the disk, interface and address are deleted with the VM), Trusted
  Launch, boot diagnostics (the progress comes from its serial log), the
  tags ``orange3-cloud=true`` and ``orange3-cloud-session=<machine id>``,
  and a system-assigned managed identity with the built-in role "Desktop
  Virtualization Power On Off Contributor" on the VM itself, so the machine
  can deallocate its own VM, and nothing else.
* **Container:** the custom data (:mod:`~orangecontrib.cloud.client.
  providers.cloud_init`) installs Docker, writes the machine's settings to
  files and a systemd unit that pulls the machine image and runs it, for
  the bootstrap's ``azure`` platform. When the container exits (idle
  timeout, maximum lifetime, a failed boot), the unit deallocates the VM
  with its identity: a VM that only powers off stays allocated and costs.
* **Image and secrets:** as on AWS (:mod:`~orangecontrib.cloud.client.
  providers.machine_images`; ``ORANGE3_CLOUD_AZURE_IMAGE`` overrides the
  image), and a certificate for the fixed name ``<name>.orange3-cloud``.

Stop deallocates the VM; deleting a machine deletes its role assignment,
then the VM (its disk, interface and address go with it), waits until it is
gone, and deletes the network security group when no machine of the add-on
is left. The resource group and the virtual network stay for the next
machine. The calls go through :class:`~orangecontrib.cloud.client.providers.
azure_arm.Arm`, whose ``send`` tests replace with a fake.

Local files go to a storage account in the subscription (created after the
user agrees, with the user's data role on it; see :mod:`~orangecontrib.cloud.
client.providers.azure_storage`), which keeps them for later sessions. The
machine reads them, and the ``az://<account>/<container>/<path>`` files the
user gives, through links signed with the user's sign-in at each load.

Prices come from the bundled list (:mod:`~orangecontrib.cloud.client.
providers.azure_prices`): the VM, its OS disk and its public address. A new
machine's handle keeps its price, from which the session works out the
costs. A deallocated VM costs its disk and its address; the instance view
says when it was deallocated.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from orangecontrib.cloud.client.api import CloudClient
from orangecontrib.cloud.client.providers import azure_account, azure_arm, azure_prices
from orangecontrib.cloud.client.providers.azure_arm import Arm, ArmError
from orangecontrib.cloud.client.providers.azure_network import (
    DELETE_TIMEOUT,
    delete_security_group_if_unused,
    ensure_security_group,
    ensure_shared,
)
from orangecontrib.cloud.client.providers.azure_storage import (
    AzureStorage,
    MakeBlobService,
    blob_service,
    is_addon_file,
)
from orangecontrib.cloud.client.providers.azure_vm import (
    AUTHORIZATION_API,
    COMPUTE_API,
    CUSTOM_DATA_LIMIT,
    DEFAULT_MACHINE_TYPE,
    HOST,
    IMAGE_VAR,
    MACHINE_TYPES,
    NAME_PREFIX,
    NETWORK_API,
    PORT,
    TAG_SESSION,
    AzureTarget,
    failure_message,
    power_role_body,
    role_assignment_id,
    stop_time,
    throwaway_ssh_key,
    vm_request,
    vm_states,
)
from orangecontrib.cloud.client.providers.base import (
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_MAX_LIFETIME,
    ClusterProvider,
    MachineHandle,
    MachineState,
    MachineStatus,
    ProviderError,
    StatusCallback,
    StoredFile,
    UploadProgress,
    addon_version,
    new_token,
    self_signed_certificate,
)
from orangecontrib.cloud.client.providers.cloud_init import user_data
from orangecontrib.cloud.client.providers.cloud_vm import (
    answers,
    boot_failed,
    log_tail,
    progress_message,
)
from orangecontrib.cloud.client.providers.machine_images import machine_image, registry_token
from orangecontrib.cloud.client.providers.sizes import DISK_MAX_GB, DISK_MIN_GB, handle_disk_gb
from orangecontrib.cloud.env import EnvironmentSpec
from orangecontrib.cloud.settings import CloudSettings, ProviderSettings

__all__ = ["AzureProvider", "MakeArm"]

log = logging.getLogger(__name__)

#: ``make_arm(subscription_id)``: the Resource Manager calls in a subscription.
MakeArm = Callable[[str], Arm]

#: The bootstrap's platform that reads the settings files.
PLATFORM = "azure"
#: Seconds to wait for a new machine to answer: the first boot installs
#: Docker, pulls the image and installs Python and all packages.
DEFAULT_START_TIMEOUT = 20 * 60
#: Seconds to retry the role assignment while Azure doesn't know the VM's
#: new identity yet.
ROLE_TIMEOUT = 90.0
#: Seconds between reads of the serial log (it lags).
CONSOLE_INTERVAL = 15.0

#: VM power state -> machine state ("": not running yet).
_STATES = {
    "": MachineState.STARTING,
    "starting": MachineState.STARTING,
    "running": MachineState.RUNNING,
    "stopping": MachineState.STOPPING,
    "deallocating": MachineState.STOPPING,
    "stopped": MachineState.STOPPED,
    "deallocated": MachineState.STOPPED,
}
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class AzureProvider(ClusterProvider):
    """Machines as VMs in the user's own Azure subscription.

    The subscription (the settings' ``account``), region, size and disk come
    from the user's Cloud settings for ``azure``, read at each :meth:`start`
    (its ``machine_type`` wins). The calls for an existing machine use the
    subscription stored in its handle.

    :param make_arm: ``make_arm(subscription_id)``, the Resource Manager
        calls; default :func:`~orangecontrib.cloud.client.providers.
        azure_arm.make_arm` (tests give fakes).
    :param make_blob_service: ``make_blob_service(credential, account)``,
        the Blob Storage calls; default :func:`~orangecontrib.cloud.client.
        providers.azure_storage.blob_service` (tests give fakes).
    :param image: the machine image; default :func:`machine_image`.
    :param port: the machine's HTTPS port (tests; the rule opens 8443).
    """

    id = "azure"
    label = "Azure"
    stores_uploads = True
    machine_sizes = MACHINE_TYPES
    default_size = DEFAULT_MACHINE_TYPE
    storage_schemes = ("az://",)
    finds_leftovers = True

    def __init__(
        self,
        *,
        make_arm: MakeArm | None = None,
        make_blob_service: MakeBlobService = blob_service,
        image: str | None = None,
        poll_interval: float = 3.0,
        port: int = PORT,
    ):
        self._make_arm = make_arm or azure_arm.make_arm
        self._make_blob_service = make_blob_service
        self._image = image
        self.poll_interval = poll_interval
        self.console_interval = CONSOLE_INTERVAL
        self.port = port
        #: OS disk size in GB for new machines (``None``: from the settings).
        self.disk_gb: int | None = None
        self._current: MachineHandle | None = None
        self._arms: dict[str, Arm] = {}
        self._storages: dict[tuple[str, str], AzureStorage] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Azure

    def settings(self) -> ProviderSettings:
        """The user's Cloud settings for Azure."""
        return CloudSettings.load().of(self.id)

    def arm(self, subscription_id: str) -> Arm:
        """The Resource Manager calls in ``subscription_id``."""
        with self._lock:
            if subscription_id not in self._arms:
                arm = self._make_arm(subscription_id)
                arm.poll_interval = self.poll_interval
                self._arms[subscription_id] = arm
            return self._arms[subscription_id]

    def storage(self, subscription_id: str, region: str) -> AzureStorage:
        """The add-on's storage account in ``subscription_id`` (a new one
        goes to ``region``), and the ``az://`` files the user can read."""
        key, arm = (subscription_id, region), self.arm(subscription_id)
        with self._lock:
            if key not in self._storages:
                self._storages[key] = AzureStorage(arm, region, self._make_blob_service)
            return self._storages[key]

    def _storage_of(self, handle: MachineHandle | None) -> AzureStorage:
        """The storage of the machine of ``handle``, else of the chosen subscription."""
        handle = handle or self._current
        if handle is not None and "subscription" in handle.extra and "region" in handle.extra:
            return self.storage(handle.extra["subscription"], handle.extra["region"])
        return self.storage(*self._chosen())

    def _chosen(self, settings: ProviderSettings | None = None) -> tuple[str, str]:
        """The chosen subscription and region (default: the saved ones)."""
        settings = settings or self.settings()
        if not settings.account or not settings.region:
            raise ProviderError(
                "Choose an Azure subscription and region in the Cloud settings first."
            )
        return settings.account, settings.region

    def _target(self, machine_type: str | None) -> AzureTarget:
        settings = self.settings()
        subscription, region = self._chosen(settings)
        machine_type = machine_type or self.size_or_default(settings.machine_type)
        if machine_type not in {t.name for t in MACHINE_TYPES}:
            raise ProviderError(f"The machine type {machine_type} isn't one the add-on offers.")
        disk_gb = int(self.disk_gb if self.disk_gb is not None else settings.disk_gb)
        if not DISK_MIN_GB <= disk_gb <= DISK_MAX_GB:
            raise ProviderError(
                f"The disk size must be {DISK_MIN_GB} to {DISK_MAX_GB} GB, not {disk_gb} GB."
            )
        return AzureTarget(subscription, region, machine_type, disk_gb)

    def _arm_of(self, handle: MachineHandle) -> Arm:
        """The calls for the machine of ``handle``."""
        subscription = handle.extra.get("subscription")
        if not subscription:
            raise ProviderError(f"The handle of {handle.name} has no Azure subscription.")
        return self.arm(subscription)

    # ------------------------------------------------------------------
    # ClusterProvider

    def price_per_hour(self, machine_type: str | None = None) -> float | None:
        """USD per hour of a new machine of ``machine_type`` (default: the
        chosen one) in the chosen region, with the chosen OS disk; ``None``
        if the price isn't known (see :mod:`~orangecontrib.cloud.client.
        providers.azure_prices`)."""
        settings = self.settings()
        if not settings.region:
            return None
        disk_gb = self.disk_gb if self.disk_gb is not None else settings.disk_gb
        machine_type = machine_type or self.size_or_default(settings.machine_type)
        return self.size_price(machine_type, settings.region, disk_gb)

    def size_price(self, machine_type: str, region: str, disk_gb: int) -> float | None:
        """USD per hour of a VM of ``machine_type`` in ``region`` with a
        Premium SSD OS disk of ``disk_gb`` and a Standard public IP address."""
        return azure_prices.per_hour(machine_type, region, disk_gb)

    def prices_date(self) -> str:
        """The date of the bundled ``azure_prices.json``."""
        return azure_prices.load_prices().date

    def machine_price_per_hour(self, handle: MachineHandle) -> float | None:
        """USD per hour of the machine of ``handle``: the price when it started."""
        recorded = handle.extra.get("price_per_hour")
        if recorded:
            return float(recorded)
        region = handle.extra.get("region")
        if not region or not handle.machine_type:
            return None
        return azure_prices.per_hour(handle.machine_type, region, handle_disk_gb(handle))

    def stopped_price_per_hour(self, handle: MachineHandle) -> float | None:
        """USD per hour of the deallocated VM of ``handle``: its disk and address."""
        region = handle.extra.get("region")
        return azure_prices.stopped_per_hour(region, handle_disk_gb(handle)) if region else None

    def stopped_at(self, handle: MachineHandle) -> datetime.datetime | None:
        """When the VM was deallocated, from its instance view."""
        vm = self._arm_of(handle).vm(handle.name, instance_view=True)
        return stop_time(vm) if vm is not None else None

    def start(
        self,
        spec: EnvironmentSpec,
        machine_type: str | None = None,
        *,
        idle_timeout: float | None = DEFAULT_IDLE_TIMEOUT,
        max_lifetime: float | None = DEFAULT_MAX_LIFETIME,
        on_status: StatusCallback | None = None,
        timeout: float | None = None,
    ) -> MachineHandle:
        report = on_status or (lambda message: None)
        target = self._target(machine_type)
        arm = self.arm(target.subscription)
        image = self._image or machine_image(addon_version(spec), IMAGE_VAR)
        handle, data = self._new_machine(spec, target, image, idle_timeout, max_lifetime)
        price = azure_prices.per_hour(target.machine_type, target.region, target.disk_gb)
        if price is not None:
            handle.extra["price_per_hour"] = f"{price:.6f}"
        ensure_shared(arm, target.region, report)
        body = vm_request(target, handle.machine_id, data, throwaway_ssh_key())
        report(f"Creating the machine {handle.name} ({target.machine_type} in {target.region})")
        started = time.monotonic()
        try:
            self._current = handle
            created = arm.put(
                arm.paths.vm(handle.name), body, "create the machine", api=COMPUTE_API
            )
            report("Allowing the machine to deallocate itself")
            handle.extra["role_assignment"] = self._assign_power_role(arm, handle, created)
            self._wait_ready(arm, handle, report, timeout or DEFAULT_START_TIMEOUT)
        except BaseException:
            self._delete_after_failure(handle)
            raise
        handle.extra["boot_seconds"] = f"{time.monotonic() - started:.0f}"
        return handle

    def status(self, handle: MachineHandle | None = None) -> MachineStatus:
        handle = handle or self._current
        if handle is None:
            return MachineStatus(MachineState.NOT_FOUND)
        vm = self._arm_of(handle).vm(handle.name, instance_view=True)
        if vm is None:
            return MachineStatus(MachineState.NOT_FOUND)
        if (vm.get("tags") or {}).get(TAG_SESSION) != handle.machine_id:
            return MachineStatus(MachineState.NOT_FOUND, "the machine was replaced")
        provisioning, power = vm_states(vm)
        if provisioning == "Failed":
            return MachineStatus(MachineState.ERROR, f"Azure failed: {failure_message(vm)}")
        if provisioning == "Deleting":
            return MachineStatus(MachineState.STOPPING)
        state = _STATES.get(power, MachineState.ERROR)
        if state is MachineState.STOPPED:
            # No reason: the session explains it from the last /v1/info.
            return MachineStatus(state, "The machine stopped.")
        if state is MachineState.ERROR:
            return MachineStatus(state, f"The machine is {power}.")
        return MachineStatus(state)

    def stop(self, handle: MachineHandle | None = None) -> None:
        """Ask Azure to deallocate the VM; :meth:`delete` doesn't wait for it."""
        handle = handle or self._current
        if handle is None or not self.status(handle).alive:
            return
        arm = self._arm_of(handle)
        try:
            arm.request("POST", f"{arm.paths.vm(handle.name)}/deallocate", "stop the machine")
        except ArmError as exc:  # gone, or busy (e.g. still being created): delete follows
            if exc.status not in (404, 409):
                raise
            log.info("Didn't deallocate %s: %s", handle.name, exc)

    def delete(self, handle: MachineHandle | None = None) -> None:
        """Delete the VM's role assignment, then the VM (with its disk,
        network interface and public address) and wait until it is gone, then
        the network security group if no other machine of the add-on is left."""
        handle = handle or self._current
        if handle is None:
            return
        arm = self._arm_of(handle)
        vm = arm.vm(handle.name)
        if vm is not None:
            self._delete_vms(arm, [vm])
        if self._current is not None and self._current.machine_id == handle.machine_id:
            self._current = None
        delete_security_group_if_unused(arm)

    def list_machines(self) -> list[MachineHandle]:
        subscription, _ = self._chosen()
        return [self._listed_handle(subscription, vm) for vm in self.arm(subscription).vms()]

    def sign_in(self, cancel: threading.Event) -> None:
        """Run ``az login`` (Microsoft's sign-in page opens in the browser)
        and wait until it ends."""
        az = azure_account.find_az()
        if not az:
            raise ProviderError(
                "Couldn't find the Azure CLI to sign in. Install it: "
                + azure_account.AZURE_CLI_INSTALL_URL
            )
        azure_account.sign_in(az, cancel)

    def ensure_access(self, handle: MachineHandle | None = None) -> None:
        """Let this computer reach the machine: update the network security
        group if the computer's public IP address changed."""
        handle = handle or self._current
        if handle is None or "region" not in handle.extra:
            return
        ensure_security_group(self._arm_of(handle), handle.extra["region"], lambda m: None)

    def delete_leftovers(self, keep: MachineHandle | None = None) -> list[str]:
        """Delete the add-on's other machines in the resource group (e.g.
        left by an Orange that quit while starting one); returns their names."""
        subscription, _ = self._chosen()
        arm = self.arm(subscription)
        keep_id = keep.machine_id if keep is not None else None
        others = [vm for vm in arm.vms() if (vm.get("tags") or {}).get(TAG_SESSION) != keep_id]
        if others:
            self._delete_vms(arm, others)
        if keep is None:
            delete_security_group_if_unused(arm)
        return [vm["name"] for vm in others]

    # ------------------------------------------------------------------
    # Storage

    def stage_file(
        self,
        handle: MachineHandle,
        path: str | os.PathLike,
        *,
        on_progress: UploadProgress | None = None,
        create_bucket: str | None = None,
        on_status: StatusCallback | None = None,
    ) -> str | None:
        """Upload a local file to the subscription's storage account (unless
        it's there already) and return its ``az://`` URI. Raises
        :class:`~orangecontrib.cloud.client.providers.base.StorageMissingError`
        until ``create_bucket`` names the account the user agreed to create,
        or to get the role on. ``on_status`` gets the wait for a new role."""
        storage = self._storage_of(handle)
        account = storage.prepare(create_bucket, on_status)
        return storage.upload(account, path, on_progress, on_status)

    def machine_location(self, location: str, handle: MachineHandle | None = None) -> str:
        """For an ``az://`` file, a read-only link for the machine signed
        with the user's sign-in (a user delegation SAS), made anew at each
        load. Other locations as they are."""
        if not location.lower().startswith(self.storage_schemes):
            return location
        return self._storage_of(handle).signed_link(location)

    def read_stored_head(self, uri: str, length: int) -> tuple[int, bytes]:
        """``(size, first length bytes)`` of an ``az://`` file, read with
        the user's sign-in."""
        return self._storage_of(None).read_head(uri, length)

    def stored_files(self, handle: MachineHandle | None = None) -> list[StoredFile]:
        """The files in the storage account (none if there is no account)."""
        storage = self._storage_of(handle)
        account = storage.find_account()
        return storage.files(account) if account else []

    def delete_stored_files(self, uris: Sequence[str]) -> None:
        """Delete files from the add-on's storage account (no others)."""
        for uri in uris:
            if not is_addon_file(uri):
                raise ProviderError(f"{uri} isn't in the storage account of the add-on.")
        if uris:
            self._storage_of(None).delete(uris)

    def storage_examples(self) -> list[str]:
        """An ``az://`` location, for the user."""
        return ["az://account/container/data.csv"]

    # ------------------------------------------------------------------
    # Helpers

    def _new_machine(
        self,
        spec: EnvironmentSpec,
        target: AzureTarget,
        image: str,
        idle_timeout: float | None,
        max_lifetime: float | None,
    ) -> tuple[MachineHandle, bytes]:
        """A new machine's handle (no VM yet) and its custom data."""
        machine_id = uuid.uuid4().hex[:12]
        name = NAME_PREFIX + machine_id
        server_name = f"{name}.orange3-cloud"
        token = new_token()
        cert_pem, key_pem = self_signed_certificate((server_name,), ())
        settings = {
            "token": token,
            "tls-cert": cert_pem,
            "tls-key": key_pem,
            "env-spec": spec.to_json(),
            "idle-timeout": str(int(idle_timeout or 0)),
            "max-lifetime": str(int(max_lifetime or 0)),
            "machine": target.machine_type,
        }
        data = user_data(image, settings, PLATFORM, registry_token(image), HOST)
        if len(data) > CUSTOM_DATA_LIMIT:
            raise ProviderError(
                f"The machine's settings (mostly your list of packages) take "
                f"{len(data) / 1024:.1f} KB compressed, more than the 64 KB that Azure "
                "allows. Uninstall packages that Orange doesn't need."
            )
        handle = MachineHandle(
            provider=self.id,
            machine_id=machine_id,
            name=name,
            url="",
            token=token,
            cert_pem=cert_pem,
            server_hostname=server_name,
            machine_type=target.machine_type,
            extra={
                "subscription": target.subscription,
                "region": target.region,
                "image": image,
                "disk_gb": str(target.disk_gb),
            },
        )
        return handle, data

    def _assign_power_role(self, arm: Arm, handle: MachineHandle, vm: dict[str, Any]) -> str:
        """Let the VM's identity deallocate the VM (only it); returns the
        role assignment's path. Retries while Azure doesn't know the new
        identity yet."""
        principal = (vm.get("identity") or {}).get("principalId")
        if not principal:
            vm = arm.vm(handle.name) or {}
            principal = (vm.get("identity") or {}).get("principalId")
        if not principal or not vm.get("id"):
            raise ProviderError("Azure didn't give the machine its identity.")
        path = role_assignment_id(vm["id"], handle.machine_id)
        body = power_role_body(arm.paths, principal)
        deadline = time.monotonic() + ROLE_TIMEOUT
        what = "allow the machine to deallocate itself"
        while True:
            try:
                arm.put(path, body, what, api=AUTHORIZATION_API)
                return path
            except ArmError as exc:
                if exc.code != "PrincipalNotFound" or time.monotonic() > deadline:
                    raise
            time.sleep(self.poll_interval)

    def _delete_vms(self, arm: Arm, vms: list[dict[str, Any]]) -> None:
        """Delete the VMs' role assignments, then the VMs (their disks,
        network interfaces and addresses go with them), and wait until they
        are gone."""
        for vm in vms:
            machine_id = (vm.get("tags") or {}).get(TAG_SESSION, "")
            arm.delete(
                role_assignment_id(vm["id"], machine_id),
                "remove the machine's role",
                api=AUTHORIZATION_API,
            )
            self._request_deletion(arm, vm["name"])
        for vm in vms:
            arm.wait_gone(
                arm.paths.vm(vm["name"]),
                "delete the machine",
                api=COMPUTE_API,
                timeout=DELETE_TIMEOUT,
            )

    def _request_deletion(self, arm: Arm, name: str) -> None:
        """Ask Azure to delete the VM ``name``; waits while another operation
        on it (e.g. the deallocation that Stop asked for) doesn't let it."""
        deadline = time.monotonic() + DELETE_TIMEOUT
        while True:
            try:
                arm.delete(arm.paths.vm(name), "delete the machine", api=COMPUTE_API)
                return
            except ArmError as exc:
                if exc.status != 409 or time.monotonic() > deadline:
                    raise
                log.info("Waiting to delete %s: %s", name, exc)
            time.sleep(self.poll_interval)

    def _delete_after_failure(self, handle: MachineHandle) -> None:
        """Clean up after a failed start; the start's own error is raised."""
        try:
            self.delete(handle)
        except (ProviderError, OSError) as exc:  # logged; the start's error is raised
            log.warning("Couldn't delete %s after it failed to start: %s", handle.name, exc)

    def _listed_handle(self, subscription: str, vm: dict[str, Any]) -> MachineHandle:
        tags = vm.get("tags") or {}
        properties = vm.get("properties") or {}
        return MachineHandle(
            provider=self.id,
            machine_id=tags.get(TAG_SESSION) or vm["name"],
            name=vm["name"],
            url="",
            token="",
            cert_pem="",
            machine_type=(properties.get("hardwareProfile") or {}).get("vmSize"),
            created_at=properties.get("timeCreated", ""),
            extra={
                "subscription": subscription,
                "region": vm.get("location", ""),
                "disk_gb": str(
                    ((properties.get("storageProfile") or {}).get("osDisk") or {}).get(
                        "diskSizeGB", ""
                    )
                ),
            },
        )

    def _address(self, arm: Arm, vm: dict[str, Any]) -> str | None:
        """The VM's public IP address, through its network interface (Azure
        names the address with a random suffix); ``None`` if it has none yet."""
        interfaces = (vm.get("properties", {}).get("networkProfile") or {}).get(
            "networkInterfaces"
        ) or []
        if not interfaces:
            return None
        nic = arm.get(interfaces[0]["id"], "read the machine's network", api=NETWORK_API) or {}
        configurations = nic.get("properties", {}).get("ipConfigurations") or [{}]
        address = configurations[0].get("properties", {}).get("publicIPAddress") or {}
        if not address.get("id"):
            return None
        found = arm.get(address["id"], "read the machine's address", api=NETWORK_API) or {}
        return found.get("properties", {}).get("ipAddress")

    def _client_when_running(
        self, arm: Arm, handle: MachineHandle, vm: dict[str, Any]
    ) -> CloudClient | None:
        """A client of the machine once it runs and has its address."""
        if vm_states(vm)[1] != "running":
            return None
        ip = self._address(arm, vm)
        if not ip:
            return None
        handle.url = f"https://{ip}:{self.port}"
        return CloudClient(
            handle.url,
            handle.token,
            handle.cert_pem,
            server_hostname=handle.server_hostname,
            timeout=5.0,
            retries=0,
        )

    def _wait_ready(
        self, arm: Arm, handle: MachineHandle, report: StatusCallback, timeout: float
    ) -> None:
        """Wait until ``/v1/info`` answers; report the machine's progress
        from its serial log."""
        deadline = time.monotonic() + timeout
        console = _Console(arm, handle.name, report, self.console_interval)
        client: CloudClient | None = None
        report("Booting the machine")
        try:
            while True:
                vm = arm.vm(handle.name, instance_view=True) or {}
                provisioning, power = vm_states(vm)
                if provisioning == "Failed":
                    raise ProviderError(f"Azure couldn't create the machine: {failure_message(vm)}")
                if client is None:
                    client = self._client_when_running(arm, handle, vm)
                console.read()
                if client is not None and answers(client):
                    return
                if power not in ("", "starting", "running"):
                    raise ProviderError(
                        f"The machine stopped while starting ({power}).\n\n{console.tail()}"
                    )
                if time.monotonic() > deadline:
                    raise ProviderError(
                        f"The machine didn't answer within {timeout:.0f} s.\n\n{console.tail()}"
                    )
                time.sleep(self.poll_interval)
        finally:
            if client is not None:
                client.close()


class _Console:
    """A starting machine's serial log, read now and then: reports its
    progress, and raises :class:`ProviderError` when the boot failed."""

    def __init__(self, arm: Arm, name: str, report: StatusCallback, interval: float):
        self.arm = arm
        self.name = name
        self.report = report
        self.interval = interval
        self.lines: list[str] = []
        self._last: str | None = None
        self._next = 0.0

    def read(self) -> None:
        """Read the serial log if it is time to."""
        if time.monotonic() < self._next:
            return
        self._next = time.monotonic() + self.interval
        try:
            text = self.arm.boot_log(self.name)
        except ProviderError as exc:  # only progress messages are lost
            log.debug("Couldn't read the output of %s: %s", self.name, exc)
            return
        if text:
            self.lines = _ANSI.sub("", text).splitlines()
        if boot_failed(self.lines):
            raise ProviderError(f"The machine couldn't start.\n\n{self.tail()}")
        message = progress_message(self.lines)
        if message and message != self._last:
            self.report(f"Starting: {message}")
            self._last = message

    def tail(self) -> str:
        """The machine's last lines, for an error message."""
        return log_tail(self.lines)
