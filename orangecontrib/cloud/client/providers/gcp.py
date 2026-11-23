"""The machine in the user's own Google Cloud project.

:class:`OwnGCPProvider` starts one Compute Engine VM per machine, with the
settings fixed for the add-on (see ``docs/dev.md``, "Google Cloud machines"):

* **VM:** the chosen machine type in the chosen zone, Container-Optimized OS
  (image family ``cos-stable`` of ``cos-cloud``), a ``pd-balanced`` boot disk
  that is deleted with the VM, the project's ``default`` network with an
  ephemeral external IP, the network tag ``orange3-cloud`` and the labels
  ``orange3-cloud=true`` and ``orange3-cloud-session=<machine id>``. It runs
  as the project's default Compute Engine service account with the read-only
  Cloud Storage scope (enough to pull a private development image and, later,
  to read the user's bucket).
* **Container:** cloud-init (the ``user-data`` metadata) writes a systemd
  unit that pulls the machine image and runs it with the host's network.
  When the container exits (idle timeout, maximum lifetime, a failed boot),
  systemd powers the VM off, so it stops costing CPU time.
* **Image:** released versions of the add-on use the public
  ``ghcr.io/ggaammdev/orange3-cloud-machine:<version>``; development versions
  use the private development image ``<region>-docker.pkg.dev/<project>/
  orange3-cloud/machine:dev``. ``ORANGE3_CLOUD_GCP_IMAGE`` overrides both.
* **Secrets** (custom metadata, read by ``docker/bootstrap.py``): a new token,
  and a self-signed certificate and key for the fixed name
  ``<vm name>.orange3-cloud`` (the IP is known only after the VM exists; the
  client checks the certificate against that name). Also the EnvironmentSpec
  and the time limits.
* **Firewall:** the rule ``orange3-cloud-https`` allows TCP 8443 to the
  ``orange3-cloud`` tag only from this computer's public IPv4 address
  (``/32``). :meth:`OwnGCPProvider.ensure_access` updates it when the address
  changes (the session calls it before reconnecting).

Deleting the machine deletes the VM with its disk, and the firewall rule when
no other machine of the add-on is left in the project.

Local files go to the project's storage bucket (created after the user
agrees; see :mod:`~orangecontrib.cloud.client.providers.gcp_storage`), which
the machine's service account may read. They stay there for later sessions
until the user deletes them.

The requests are built in :mod:`~orangecontrib.cloud.client.providers.gcp_vm`
and sent through :class:`~orangecontrib.cloud.client.providers.gcp_compute.
ComputeApi` (plain REST), which tests replace with a fake project.
"""

from __future__ import annotations

import contextlib
import datetime
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Sequence

from orangecontrib.cloud.client.api import CloudClient
from orangecontrib.cloud.client.providers.base import (
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_MAX_LIFETIME,
    ClusterProvider,
    MachineHandle,
    MachineState,
    MachineStatus,
    ProviderError,
    StatusCallback,
    StorageMissingError,
    StoredFile,
    UploadProgress,
    addon_version,
    new_token,
    self_signed_certificate,
)
from orangecontrib.cloud.client.providers.cloud_vm import (
    answers,
    boot_failed,
    log_tail,
    progress_message,
    public_ip,
)
from orangecontrib.cloud.client.providers.gcp_compute import (
    ComputeApi,
    GoogleComputeApi,
    NotFoundError,
)
from orangecontrib.cloud.client.providers.gcp_prices import load_prices
from orangecontrib.cloud.client.providers.gcp_storage import (
    BUCKET_PREFIX,
    ProjectStorage,
    new_bucket_name,
    parse_gs_uri,
    read_object_head,
)
from orangecontrib.cloud.client.providers.gcp_storage_api import StorageApi
from orangecontrib.cloud.client.providers.gcp_vm import (
    DEFAULT_MACHINE_TYPE,
    FIREWALL_RULE,
    LABEL,
    LABEL_SESSION,
    MACHINE_TYPES,
    NAME_PREFIX,
    PORT,
    MachineTarget,
    cloud_init,
    external_ip,
    firewall_body,
    instance_body,
    machine_image,
    stop_time,
)
from orangecontrib.cloud.client.providers.sizes import DISK_MAX_GB, DISK_MIN_GB, handle_disk_gb
from orangecontrib.cloud.env import EnvironmentSpec
from orangecontrib.cloud.settings import CloudSettings, ProviderSettings

__all__ = ["NotFoundError", "OwnGCPProvider"]

log = logging.getLogger(__name__)

#: Seconds to wait for a new machine to answer: the first boot pulls the
#: image and installs Python and all packages.
DEFAULT_START_TIMEOUT = 20 * 60

#: Compute Engine VM status -> machine state.
_STATES = {
    "PROVISIONING": MachineState.STARTING,
    "STAGING": MachineState.STARTING,
    "RUNNING": MachineState.RUNNING,
    "REPAIRING": MachineState.STARTING,
    "PENDING_STOP": MachineState.STOPPING,
    "STOPPING": MachineState.STOPPING,
    "SUSPENDING": MachineState.STOPPING,
    "SUSPENDED": MachineState.STOPPED,
    "TERMINATED": MachineState.STOPPED,
}


class OwnGCPProvider(ClusterProvider):
    """Machines as Compute Engine VMs in the user's own project.

    The project, zone and machine type default to the user's Cloud settings
    for ``gcp`` (the project is their ``account``), read at each :meth:`start`
    (its ``machine_type`` wins). The calls for an existing machine use the
    project and zone stored in its handle.

    :param api: Compute Engine calls; default :class:`GoogleComputeApi` with
        the signed-in credentials.
    :param image: the machine image; default :func:`machine_image`.
    :param port: the machine's HTTPS port (tests; the firewall opens 8443).
    """

    id = "gcp"
    label = "Google Cloud"
    stores_uploads = True
    machine_sizes = MACHINE_TYPES
    default_size = DEFAULT_MACHINE_TYPE
    storage_schemes = ("gs://",)
    finds_leftovers = True

    def __init__(
        self,
        *,
        project: str | None = None,
        zone: str | None = None,
        api: ComputeApi | None = None,
        image: str | None = None,
        poll_interval: float = 3.0,
        port: int = PORT,
    ):
        self._project = project
        self._zone = zone
        self._api = api
        self._image = image
        self.poll_interval = poll_interval
        self.port = port
        #: Boot disk size in GB for new machines (``None``: from the settings).
        self.disk_gb: int | None = None
        self._current: MachineHandle | None = None
        #: Cloud Storage calls; default :class:`GoogleStorageApi` (tests set a fake).
        self.storage_api: StorageApi | None = None
        #: Buckets whose reader access was checked: bucket -> service account.
        self._readers: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Google

    @property
    def api(self) -> ComputeApi:
        """The Compute Engine calls (signs in on first use)."""
        if self._api is None:
            from orangecontrib.cloud.client.providers.gcp_account import load_credentials

            credentials, _ = load_credentials()
            self._api = GoogleComputeApi(credentials)
        return self._api

    def storage(self, project: str) -> ProjectStorage:
        """The project's storage bucket and its files."""
        if self.storage_api is None:
            from orangecontrib.cloud.client.providers.gcp_account import load_credentials
            from orangecontrib.cloud.client.providers.gcp_storage_api import GoogleStorageApi

            credentials, _ = load_credentials()
            self.storage_api = GoogleStorageApi(credentials)
        return ProjectStorage(project, self.storage_api)

    def settings(self) -> ProviderSettings:
        """The user's Cloud settings for Google Cloud."""
        return CloudSettings.load().of(self.id)

    def _target(self, machine_type: str | None) -> MachineTarget:
        settings = self.settings()
        project = self._project or settings.account
        zone = self._zone or settings.zone
        if not project or not zone:
            raise ProviderError(
                "Choose a Google Cloud project, region and zone in the Cloud settings first."
            )
        machine_type = machine_type or self.size_or_default(settings.machine_type)
        if machine_type not in {t.name for t in MACHINE_TYPES}:
            raise ProviderError(f"The machine type {machine_type} isn't one the add-on offers.")
        disk_gb = int(self.disk_gb if self.disk_gb is not None else settings.disk_gb)
        if not DISK_MIN_GB <= disk_gb <= DISK_MAX_GB:
            raise ProviderError(
                f"The disk size must be {DISK_MIN_GB} to {DISK_MAX_GB} GB, not {disk_gb} GB."
            )
        return MachineTarget(project, zone, machine_type, disk_gb)

    @staticmethod
    def _where(handle: MachineHandle) -> tuple[str, str, str]:
        extra = handle.extra
        try:
            return extra["project"], extra["zone"], extra.get("instance") or handle.name
        except KeyError:
            raise ProviderError(f"The handle of {handle.name} has no project or zone.") from None

    # ------------------------------------------------------------------
    # ClusterProvider

    def price_per_hour(self, machine_type: str | None = None) -> float | None:
        """USD per hour of a new machine of ``machine_type`` (default: the
        chosen one) in the chosen region, with the chosen boot disk; ``None``
        if the price isn't known (see :mod:`~orangecontrib.cloud.client.
        providers.gcp_prices`)."""
        settings = self.settings()
        zone = self._zone or settings.zone
        disk_gb = self.disk_gb if self.disk_gb is not None else settings.disk_gb
        if not zone:
            return None
        machine_type = machine_type or self.size_or_default(settings.machine_type)
        return self.size_price(machine_type, _region(zone), disk_gb)

    def size_price(self, machine_type: str, region: str, disk_gb: int) -> float | None:
        """USD per hour of a machine of ``machine_type`` in ``region`` with a
        ``pd-balanced`` boot disk of ``disk_gb`` and an external address."""
        return load_prices().per_hour(machine_type, region, disk_gb)

    def prices_date(self) -> str:
        """The date of the bundled ``gcp_prices.json``."""
        return load_prices().date

    def machine_price_per_hour(self, handle: MachineHandle) -> float | None:
        """USD per hour of the machine of ``handle``: the price when it started."""
        recorded = handle.extra.get("price_per_hour")
        if recorded:
            return float(recorded)
        zone = handle.extra.get("zone")
        if not zone or not handle.machine_type:
            return None
        return load_prices().per_hour(handle.machine_type, _region(zone), handle_disk_gb(handle))

    def stopped_price_per_hour(self, handle: MachineHandle) -> float | None:
        """USD per hour of the stopped VM of ``handle``: its boot disk."""
        zone = handle.extra.get("zone")
        if not zone:
            return None
        return load_prices().stopped_per_hour(_region(zone), handle_disk_gb(handle))

    def stopped_at(self, handle: MachineHandle) -> datetime.datetime | None:
        """When the VM stopped: its ``lastStopTimestamp``."""
        project, zone, name = self._where(handle)
        try:
            return stop_time(self.api.get_instance(project, zone, name))
        except NotFoundError:
            return None

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
        project, zone = target.project, target.zone
        image = self._image or machine_image(addon_version(spec), project, target.region)

        report("Allowing this computer's IP address through the firewall")
        self._ensure_firewall(project, report)

        machine_id = uuid.uuid4().hex[:12]
        name = NAME_PREFIX + machine_id
        server_name = f"{name}.orange3-cloud"
        token = new_token()
        cert_pem, key_pem = self_signed_certificate((server_name,), ())
        service_account = self.api.default_service_account(project)
        if not service_account:
            raise ProviderError(
                f"Project {project} has no default Compute Engine service account, "
                "which the machine runs as."
            )
        metadata = {
            "user-data": cloud_init(image),
            "orange3-cloud-token": token,
            "orange3-cloud-tls-cert": cert_pem,
            "orange3-cloud-tls-key": key_pem,
            "orange3-cloud-env-spec": spec.to_json(),
            "orange3-cloud-idle-timeout": str(int(idle_timeout or 0)),
            "orange3-cloud-max-lifetime": str(int(max_lifetime or 0)),
        }
        body = instance_body(name, machine_id, target, metadata, service_account)
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
                "project": project,
                "zone": zone,
                "instance": name,
                "image": image,
                "disk_gb": str(target.disk_gb),
            },
        )
        price = load_prices().per_hour(target.machine_type, target.region, target.disk_gb)
        if price is not None:
            handle.extra["price_per_hour"] = f"{price:.6f}"
        report(f"Creating the machine {name} ({target.machine_type} in {zone})")
        started = time.monotonic()
        try:
            self.api.insert_instance(project, zone, body)
            self._current = handle
            self._wait_ready(handle, report, timeout or DEFAULT_START_TIMEOUT)
        except BaseException:
            self._delete_after_failure(handle)
            raise
        handle.extra["boot_seconds"] = f"{time.monotonic() - started:.0f}"
        return handle

    def status(self, handle: MachineHandle | None = None) -> MachineStatus:
        handle = handle or self._current
        if handle is None:
            return MachineStatus(MachineState.NOT_FOUND)
        project, zone, name = self._where(handle)
        try:
            instance = self.api.get_instance(project, zone, name)
        except NotFoundError:
            return MachineStatus(MachineState.NOT_FOUND)
        labels = instance.get("labels") or {}
        if labels.get(LABEL_SESSION) != handle.machine_id:
            return MachineStatus(MachineState.NOT_FOUND, "the machine was replaced")
        state = _STATES.get(instance.get("status", ""), MachineState.ERROR)
        if state is MachineState.STOPPED:
            reason = self._stop_reason(project, zone, name)
            return MachineStatus(state, f"The machine stopped{': ' + reason if reason else ''}.")
        if state is MachineState.ERROR:
            return MachineStatus(state, f"The machine is {instance.get('status', 'unknown')}.")
        return MachineStatus(state)

    def stop(self, handle: MachineHandle | None = None) -> None:
        """Ask the VM to stop; :meth:`delete` doesn't wait for it."""
        handle = handle or self._current
        if handle is None:
            return
        project, zone, name = self._where(handle)
        with contextlib.suppress(NotFoundError):  # already gone
            self.api.stop_instance(project, zone, name)

    def delete(self, handle: MachineHandle | None = None) -> None:
        """Delete the VM and its disk, then the firewall rule if no other
        machine of the add-on is left in the project."""
        handle = handle or self._current
        if handle is None:
            return
        project, zone, name = self._where(handle)
        if self.status(handle).exists:
            self._delete_instance(project, zone, name)
        if self._current is not None and self._current.machine_id == handle.machine_id:
            self._current = None
        if not self.api.list_instances(project, LABEL):
            self._delete_firewall(project)

    def list_machines(self) -> list[MachineHandle]:
        project = self._project or self._target(None).project
        return [self._listed_handle(project, i) for i in self.api.list_instances(project, LABEL)]

    def sign_in(self, cancel: threading.Event) -> None:
        """Run ``gcloud auth application-default login`` (Google's sign-in
        page opens in the browser) and wait until it ends."""
        from orangecontrib.cloud.client.providers import gcp_account

        gcloud = gcp_account.find_gcloud()
        if not gcloud:
            raise ProviderError(
                "Couldn't find the gcloud tool to sign in. Install the Google Cloud CLI: "
                + gcp_account.GCLOUD_INSTALL_URL
            )
        gcp_account.sign_in(gcloud, cancel)

    def ensure_access(self, handle: MachineHandle | None = None) -> None:
        """Let this computer reach the machine: update the firewall rule if
        the computer's public IP address changed."""
        handle = handle or self._current
        if handle is None or "project" not in handle.extra:
            return
        self._ensure_firewall(handle.extra["project"], lambda message: None)

    def delete_leftovers(self, keep: MachineHandle | None = None) -> list[str]:
        """Delete the add-on's other machines in the project (e.g. left by an
        Orange that quit while starting one); returns their names."""
        deleted = []
        for listed in self.list_machines():
            if keep is not None and listed.machine_id == keep.machine_id:
                continue
            project, zone, name = self._where(listed)
            self._delete_instance(project, zone, name)
            deleted.append(name)
        project = self._project or self._target(None).project
        if keep is None and not self.api.list_instances(project, LABEL):
            self._delete_firewall(project)
        return deleted

    def stage_file(
        self,
        handle: MachineHandle,
        path: str | os.PathLike,
        *,
        on_progress: UploadProgress | None = None,
        create_bucket: str | None = None,
        on_status: StatusCallback | None = None,
    ) -> str | None:
        """Upload a local file to the project's bucket (unless it's there
        already) and return its ``gs://`` URI. Without a bucket, raises
        :class:`StorageMissingError` until ``create_bucket`` names it."""
        project, zone, _ = self._where(handle)
        storage = self.storage(project)
        bucket = storage.find_bucket()
        if bucket is None:
            if not create_bucket:
                name = new_bucket_name(project)
                raise StorageMissingError(
                    f"Files from this computer go to a storage bucket in project {project}, "
                    f"and there is none yet. Create the bucket {name}?",
                    name,
                )
            if on_status is not None:
                on_status(f"Creating the bucket {create_bucket}")
            bucket = storage.create_bucket(create_bucket, zone.rsplit("-", 1)[0])
            if on_status is not None:
                on_status(f"Uploading the file to {self.label} storage")
        if self._readers.get(bucket) is None:
            account = self.api.default_service_account(project)
            storage.allow_reader(bucket, f"serviceAccount:{account}")
            self._readers[bucket] = account
        return storage.upload(bucket, path, on_progress)

    def read_stored_head(self, uri: str, length: int) -> tuple[int, bytes]:
        """``(size, first length bytes)`` of a ``gs://`` file, read with the
        user's Google sign-in."""
        return read_object_head(uri, length)

    def stored_files(self, handle: MachineHandle | None = None) -> list[StoredFile]:
        """The files in the project's bucket (none if there is no bucket)."""
        storage = self.storage(self._project_of(handle))
        bucket = storage.find_bucket()
        return storage.files(bucket) if bucket else []

    def delete_stored_files(self, uris: Sequence[str]) -> None:
        """Delete files from the add-on's buckets (no others)."""
        for uri in uris:
            if not parse_gs_uri(uri)[0].startswith(BUCKET_PREFIX):
                raise ProviderError(f"{uri} isn't in a bucket of the add-on.")
        if uris:
            bucket = parse_gs_uri(uris[0])[0]
            self.storage(bucket.removeprefix(BUCKET_PREFIX).rsplit("-", 1)[0]).delete(uris)

    # ------------------------------------------------------------------
    # Helpers

    def _project_of(self, handle: MachineHandle | None) -> str:
        if handle is not None and "project" in handle.extra:
            return handle.extra["project"]
        if self._project:
            return self._project
        project = self.settings().account
        if not project:
            raise ProviderError("Choose a Google Cloud project in the Cloud settings first.")
        return project

    def _ensure_firewall(self, project: str, report: StatusCallback) -> str:
        """Create or update the rule so that it allows exactly this
        computer's address; returns the address."""
        address = public_ip()
        source = [f"{address}/32"]
        try:
            rule = self.api.get_firewall(project, FIREWALL_RULE)
        except NotFoundError:
            report(f"Creating the firewall rule {FIREWALL_RULE} for {address}")
            self.api.insert_firewall(project, firewall_body(source))
        else:
            wanted = firewall_body(source)
            if any(
                rule.get(key) != wanted[key] for key in ("sourceRanges", "targetTags", "allowed")
            ):
                report(f"Updating the firewall rule {FIREWALL_RULE} for {address}")
                self.api.patch_firewall(project, FIREWALL_RULE, wanted)
        self._allowed_ip = address
        return address

    def _delete_firewall(self, project: str) -> None:
        with contextlib.suppress(NotFoundError):  # already gone
            self.api.delete_firewall(project, FIREWALL_RULE)

    def _delete_instance(self, project: str, zone: str, name: str) -> None:
        # A VM that is still stopping may refuse to be deleted for a moment.
        for delay in (5, 10, 20, 30, None):
            try:
                self.api.delete_instance(project, zone, name)
                return
            except NotFoundError:
                return
            except ProviderError as exc:
                if delay is None or "not ready" not in str(exc).lower().replace(
                    "notready", "not ready"
                ):
                    raise
                log.info("%s isn't ready to be deleted; retrying in %s s", name, delay)
            time.sleep(delay)

    def _delete_after_failure(self, handle: MachineHandle) -> None:
        """Clean up after a failed start; the start's own error is raised."""
        try:
            self.delete(handle)
        except (ProviderError, OSError) as exc:  # logged; the start's error is raised
            log.warning("Couldn't delete %s after it failed to start: %s", handle.name, exc)

    def _listed_handle(self, project: str, instance: dict) -> MachineHandle:
        name = instance.get("name", "")
        zone = instance.get("zone", "").rsplit("/", 1)[-1]
        labels = instance.get("labels") or {}
        ip = external_ip(instance)
        return MachineHandle(
            provider=self.id,
            machine_id=labels.get(LABEL_SESSION, name.removeprefix(NAME_PREFIX)),
            name=name,
            url=f"https://{ip}:{self.port}" if ip else "",
            token="",
            cert_pem="",
            machine_type=instance.get("machineType", "").rsplit("/", 1)[-1],
            created_at=instance.get("creationTimestamp", ""),
            extra={
                "project": project,
                "zone": zone,
                "instance": name,
                "state": instance.get("status", ""),
                "disk_gb": str((instance.get("disks") or [{}])[0].get("diskSizeGb", "")),
            },
        )

    def _serial_lines(
        self, project: str, zone: str, name: str, start: int
    ) -> tuple[list[str], int]:
        try:
            text, start = self.api.serial_output(project, zone, name, start)
        except ProviderError as exc:  # only progress messages are lost
            log.debug("Couldn't read the output of %s: %s", name, exc)
            return [], start
        return text.splitlines(), start

    def _stop_reason(self, project: str, zone: str, name: str) -> str:
        """Why the server stopped, from the console of a VM that hasn't powered
        off yet (e.g. a failed boot); powering off clears the console, and the
        session then explains the stop from the machine's last ``/v1/info``."""
        lines, _ = self._serial_lines(project, zone, name, 0)
        for line in reversed(lines):
            match = re.search(r"the server stopped because (.*?) \(exit code", line)
            if match:
                return match.group(1)
            if "the machine did not start" in line:
                return "it couldn't install your environment"
        return ""

    def _wait_ready(self, handle: MachineHandle, report: StatusCallback, timeout: float) -> None:
        """Wait until ``/v1/info`` answers; report the machine's progress
        from its serial console."""
        project, zone, name = self._where(handle)
        deadline = time.monotonic() + timeout
        offset, tail, last = 0, [], None
        client: CloudClient | None = None
        report("Booting the machine")
        try:
            while True:
                instance = self.api.get_instance(project, zone, name)
                status = instance.get("status", "")
                if client is None and status == "RUNNING":
                    ip = external_ip(instance)
                    if ip:
                        handle.url = f"https://{ip}:{self.port}"
                        client = CloudClient(
                            handle.url,
                            handle.token,
                            handle.cert_pem,
                            server_hostname=handle.server_hostname,
                            timeout=5.0,
                            retries=0,
                        )
                lines, offset = self._serial_lines(project, zone, name, offset)
                tail = (tail + lines)[-40:]
                if boot_failed(lines):
                    raise ProviderError(f"The machine couldn't start.\n\n{log_tail(tail)}")
                message = progress_message(lines)
                if message and message != last:
                    report(f"Starting: {message}")
                    last = message
                if client is not None and answers(client):
                    return
                if _STATES.get(status) in (MachineState.STOPPING, MachineState.STOPPED):
                    raise ProviderError(f"The machine stopped while starting.\n\n{log_tail(tail)}")
                if time.monotonic() > deadline:
                    raise ProviderError(
                        f"The machine didn't answer within {timeout:.0f} s.\n\n{log_tail(tail)}"
                    )
                time.sleep(self.poll_interval)
        finally:
            if client is not None:
                client.close()


def _region(zone: str) -> str:
    """``us-central1`` of ``us-central1-a``."""
    return zone.rsplit("-", 1)[0]
