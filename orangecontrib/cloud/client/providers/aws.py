"""The machine in the user's own AWS account.

:class:`AWSProvider` signs in with the AWS CLI's profile chosen in the Cloud
settings (its ``account``; see :mod:`~orangecontrib.cloud.client.providers.
aws_account`), and the settings dialog's AWS page runs the setup checker
(:mod:`~orangecontrib.cloud.client.providers.aws_setup`). It starts one EC2
instance per machine in the chosen region, with the settings fixed for the
add-on (see ``docs/dev.md``, "AWS machines"):

* **Instance:** the chosen size (:mod:`~orangecontrib.cloud.client.
  providers.aws_vm`), the ECS-optimized Amazon Linux 2023 image (it has
  Docker; its ID comes from a public SSM parameter), a ``gp3`` boot volume
  of the chosen size that is deleted with the instance, the region's
  default VPC with a public IPv4 address, instance metadata only through
  IMDSv2, and the tags ``orange3-cloud=true``, ``orange3-cloud-session=
  <machine id>`` and ``Name=orange3-cloud-<machine id>``. When it powers
  off, the instance stops (it isn't terminated), so compute charges end.
* **Container:** the user data (:mod:`~orangecontrib.cloud.client.providers.
  cloud_init`) writes the machine's settings to files and a systemd unit
  that pulls the machine image and runs it, for the bootstrap's ``aws``
  platform. When the container exits (idle timeout, maximum lifetime, a
  failed boot), the VM powers off.
* **Image:** a released add-on runs the public image; a development
  version the private development image, pulled with a short-lived token
  (:mod:`~orangecontrib.cloud.client.providers.machine_images`).
  ``ORANGE3_CLOUD_AWS_IMAGE`` overrides both.
* **Secrets:** a new token, and a self-signed certificate and key for the
  fixed name ``<name>.orange3-cloud`` (the address is known only after the
  instance exists; the client checks the certificate against that name).
* **Security group** ``orange3-cloud-https`` in the default VPC: TCP 8443
  only from this computer's public IPv4 address (``/32``).
  :meth:`AWSProvider.ensure_access` updates it when the address changes.

Deleting a machine terminates the instance (its volume goes with it), waits
until it is gone, and deletes the security group when no other machine of
the add-on is left in the region. The calls go through
:class:`~orangecontrib.cloud.client.providers.aws_ec2.Ec2`, whose ``boto3``
clients tests replace with fakes.

Local files go to the account's S3 bucket (created after the user agrees;
see :mod:`~orangecontrib.cloud.client.providers.aws_storage`), which keeps
them for later sessions. The machine reads them, and the ``s3://`` files the
user gives, through links signed with the user's credentials at each load.

Prices come from the bundled list (:mod:`~orangecontrib.cloud.client.
providers.aws_prices`): the instance, its volume and its public address. A
new machine's handle keeps its price, from which the session works out the
costs. A stopped instance costs only its volume; when it stopped comes from
EC2, or, after it powered itself off, from its CPU metric in CloudWatch.
"""

from __future__ import annotations

import contextlib
import datetime
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from orangecontrib.cloud.client.api import CloudClient
from orangecontrib.cloud.client.providers import aws_account, aws_prices
from orangecontrib.cloud.client.providers.aws_ec2 import BotoClients, Ec2, NotFoundError
from orangecontrib.cloud.client.providers.aws_storage import (
    BUCKET_PREFIX,
    S3Storage,
    new_bucket_name,
    parse_s3_uri,
)
from orangecontrib.cloud.client.providers.aws_vm import (
    DEFAULT_MACHINE_TYPE,
    IMAGE_VAR,
    MACHINE_TYPES,
    NAME_PREFIX,
    PORT,
    SECURITY_GROUP,
    TAG_SESSION,
    USER_DATA_LIMIT,
    Ami,
    Ec2Target,
    allows,
    run_instances_request,
    stale_permissions,
    stop_time,
    tag_value,
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
    StorageMissingError,
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
    public_ip,
)
from orangecontrib.cloud.client.providers.machine_images import machine_image, registry_token
from orangecontrib.cloud.client.providers.sizes import DISK_MAX_GB, DISK_MIN_GB, handle_disk_gb
from orangecontrib.cloud.env import EnvironmentSpec
from orangecontrib.cloud.settings import CloudSettings, ProviderSettings

__all__ = ["AWSProvider", "MakeClient"]

log = logging.getLogger(__name__)

#: ``make_client(profile, service, region)``: a ``boto3`` client.
MakeClient = Callable[[str, str, str], Any]

#: The bootstrap's platform that reads the settings files.
PLATFORM = "aws"
#: Seconds to wait for a new machine to answer: the first boot pulls the
#: image and installs Python and all packages.
DEFAULT_START_TIMEOUT = 20 * 60
#: Seconds to wait until deleted instances are gone.
TERMINATE_TIMEOUT = 5 * 60
#: Seconds between reads of the console (EC2 updates it about once a minute).
CONSOLE_INTERVAL = 15.0

#: EC2 instance state -> machine state ("terminated": not found).
_STATES = {
    "pending": MachineState.STARTING,
    "running": MachineState.RUNNING,
    "stopping": MachineState.STOPPING,
    "shutting-down": MachineState.STOPPING,
    "stopped": MachineState.STOPPED,
}
#: States of instances that still use the security group.
_USING_GROUP = ("pending", "running", "stopping", "stopped", "shutting-down")


class AWSProvider(ClusterProvider):
    """Machines as EC2 instances in the user's own AWS account.

    The profile (the settings' ``account``), region, size and disk come
    from the user's Cloud settings for ``aws``, read at each :meth:`start`
    (its ``machine_type`` wins). The calls for an existing machine use the
    profile and region stored in its handle.

    :param make_client: ``make_client(profile, service, region)``, a
        ``boto3`` client; default :class:`BotoClients` (tests give fakes).
    :param image: the machine image; default :func:`machine_image`.
    :param port: the machine's HTTPS port (tests; the security group opens 8443).
    """

    id = "aws"
    label = "AWS"
    stores_uploads = True
    machine_sizes = MACHINE_TYPES
    default_size = DEFAULT_MACHINE_TYPE
    storage_schemes = ("s3://",)
    finds_leftovers = True

    def __init__(
        self,
        *,
        make_client: MakeClient | None = None,
        image: str | None = None,
        poll_interval: float = 3.0,
        port: int = PORT,
    ):
        self._make_client = make_client or BotoClients()
        self._image = image
        self.poll_interval = poll_interval
        self.console_interval = CONSOLE_INTERVAL
        self.port = port
        #: Boot disk size in GB for new machines (``None``: from the settings).
        self.disk_gb: int | None = None
        self._current: MachineHandle | None = None
        self._apis: dict[tuple[str, str], Ec2] = {}
        self._storages: dict[tuple[str, str], S3Storage] = {}

    # ------------------------------------------------------------------
    # AWS

    def settings(self) -> ProviderSettings:
        """The user's Cloud settings for AWS."""
        return CloudSettings.load().of(self.id)

    def api(self, profile: str, region: str) -> Ec2:
        """The EC2 calls in ``region`` with ``profile``'s credentials."""
        key = (profile, region)
        if key not in self._apis:
            make = self._make_client
            self._apis[key] = Ec2(
                make(profile, "ec2", region),
                make(profile, "ssm", region),
                profile,
                make(profile, "cloudwatch", region),
            )
        return self._apis[key]

    def storage(self, profile: str, region: str) -> S3Storage:
        """The add-on's bucket in ``profile``'s account (a new one goes to
        ``region``), and the account's ``s3://`` files."""
        key = (profile, region)
        if key not in self._storages:
            self._storages[key] = S3Storage(self._make_client, profile, region)
        return self._storages[key]

    def _storage_of(self, handle: MachineHandle | None) -> S3Storage:
        """The storage of the machine of ``handle``, else of the chosen profile."""
        handle = handle or self._current
        if handle is not None and "profile" in handle.extra and "region" in handle.extra:
            return self.storage(handle.extra["profile"], handle.extra["region"])
        return self.storage(*self._chosen())

    def _chosen(self, settings: ProviderSettings | None = None) -> tuple[str, str]:
        """The chosen profile and region (from ``settings``, default the saved ones)."""
        settings = settings or self.settings()
        if not settings.region:
            raise ProviderError("Choose an AWS profile and region in the Cloud settings first.")
        return settings.account or aws_account.default_profile(), settings.region

    def _target(self, machine_type: str | None) -> Ec2Target:
        settings = self.settings()
        profile, region = self._chosen(settings)
        machine_type = machine_type or self.size_or_default(settings.machine_type)
        if machine_type not in {t.name for t in MACHINE_TYPES}:
            raise ProviderError(f"The machine type {machine_type} isn't one the add-on offers.")
        disk_gb = int(self.disk_gb if self.disk_gb is not None else settings.disk_gb)
        if not DISK_MIN_GB <= disk_gb <= DISK_MAX_GB:
            raise ProviderError(
                f"The disk size must be {DISK_MIN_GB} to {DISK_MAX_GB} GB, not {disk_gb} GB."
            )
        return Ec2Target(profile, region, machine_type, disk_gb)

    def _where(self, handle: MachineHandle) -> tuple[Ec2, str]:
        """The calls for the machine of ``handle``, and its instance ID."""
        try:
            profile, region = handle.extra["profile"], handle.extra["region"]
        except KeyError:
            raise ProviderError(
                f"The handle of {handle.name} has no AWS profile or region."
            ) from None
        return self.api(profile, region), handle.extra.get("instance_id", "")

    # ------------------------------------------------------------------
    # ClusterProvider

    def price_per_hour(self, machine_type: str | None = None) -> float | None:
        """USD per hour of a new machine of ``machine_type`` (default: the
        chosen one) in the chosen region, with the chosen boot volume;
        ``None`` if the price isn't known (see :mod:`~orangecontrib.cloud.
        client.providers.aws_prices`)."""
        settings = self.settings()
        if not settings.region:
            return None
        disk_gb = self.disk_gb if self.disk_gb is not None else settings.disk_gb
        machine_type = machine_type or self.size_or_default(settings.machine_type)
        return self.size_price(machine_type, settings.region, disk_gb)

    def size_price(self, machine_type: str, region: str, disk_gb: int) -> float | None:
        """USD per hour of an instance of ``machine_type`` in ``region`` with a
        ``gp3`` boot volume of ``disk_gb`` and a public IPv4 address."""
        return aws_prices.per_hour(machine_type, region, disk_gb)

    def prices_date(self) -> str:
        """The date of the bundled ``aws_prices.json``."""
        return aws_prices.load_prices().date

    def machine_price_per_hour(self, handle: MachineHandle) -> float | None:
        """USD per hour of the machine of ``handle``: the price when it started."""
        recorded = handle.extra.get("price_per_hour")
        if recorded:
            return float(recorded)
        region = handle.extra.get("region")
        if not region or not handle.machine_type:
            return None
        return aws_prices.per_hour(handle.machine_type, region, handle_disk_gb(handle))

    def stopped_price_per_hour(self, handle: MachineHandle) -> float | None:
        """USD per hour of the stopped instance of ``handle``: its volume."""
        region = handle.extra.get("region")
        return aws_prices.stopped_per_hour(region, handle_disk_gb(handle)) if region else None

    def stopped_at(self, handle: MachineHandle) -> datetime.datetime | None:
        """When the instance stopped: the time in its state transition reason
        after a stop request; after it powered itself off (no time there), the
        end of the last 5-minute period of its CPU metric (CloudWatch)."""
        ec2, instance_id = self._where(handle)
        instance = ec2.instance(instance_id) if instance_id else None
        if instance is None or instance.get("State", {}).get("Name") != "stopped":
            return None
        launched = instance.get("LaunchTime")
        found = stop_time(instance)
        if found is None and isinstance(launched, datetime.datetime):
            found = ec2.metrics_end(instance_id, launched)
        return min(found, datetime.datetime.now(datetime.timezone.utc)) if found else None

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
        ec2 = self.api(target.profile, target.region)
        image = self._image or machine_image(addon_version(spec), IMAGE_VAR)
        handle, data = self._new_machine(spec, target, image, idle_timeout, max_lifetime)
        price = aws_prices.per_hour(target.machine_type, target.region, target.disk_gb)
        if price is not None:
            handle.extra["price_per_hour"] = f"{price:.6f}"

        ami = Ami(*ec2.image())

        report("Allowing this computer's IP address through the security group")
        group_id = self._ensure_security_group(ec2, report)
        handle.extra["security_group"] = group_id
        request = run_instances_request(target, handle.name, handle.machine_id, ami, group_id, data)
        report(f"Creating the machine {handle.name} ({target.machine_type} in {target.region})")
        started = time.monotonic()
        try:
            handle.extra["instance_id"] = ec2.run_instance(request)["InstanceId"]
            self._current = handle
            self._wait_ready(ec2, handle, report, timeout or DEFAULT_START_TIMEOUT)
        except BaseException:
            self._delete_after_failure(handle)
            raise
        handle.extra["boot_seconds"] = f"{time.monotonic() - started:.0f}"
        return handle

    def status(self, handle: MachineHandle | None = None) -> MachineStatus:
        handle = handle or self._current
        if handle is None:
            return MachineStatus(MachineState.NOT_FOUND)
        ec2, instance_id = self._where(handle)
        instance = ec2.instance(instance_id) if instance_id else None
        if instance is None:
            return MachineStatus(MachineState.NOT_FOUND)
        if tag_value(instance, TAG_SESSION) != handle.machine_id:
            return MachineStatus(MachineState.NOT_FOUND, "the machine was replaced")
        name = instance.get("State", {}).get("Name", "")
        if name == "terminated":
            return MachineStatus(MachineState.NOT_FOUND)
        state = _STATES.get(name, MachineState.ERROR)
        if state is MachineState.STOPPED:
            return MachineStatus(state, _stopped_message(instance))
        if state is MachineState.ERROR:
            return MachineStatus(state, f"The machine is {name or 'in an unknown state'}.")
        return MachineStatus(state)

    def stop(self, handle: MachineHandle | None = None) -> None:
        """Ask the instance to stop; :meth:`delete` doesn't wait for it."""
        handle = handle or self._current
        if handle is None:
            return
        ec2, instance_id = self._where(handle)
        if instance_id and self.status(handle).alive:
            with contextlib.suppress(NotFoundError):  # already gone
                ec2.stop(instance_id)

    def delete(self, handle: MachineHandle | None = None) -> None:
        """Terminate the instance (with its volume) and wait until it is
        gone, then delete the security group if no other machine of the
        add-on is left in the region."""
        handle = handle or self._current
        if handle is None:
            return
        ec2, instance_id = self._where(handle)
        if not instance_id:  # e.g. the start failed after AWS created it
            instance_id = self._find_instance(ec2, handle)
        if instance_id and self.status(handle).exists:
            self._terminate(ec2, [instance_id])
        if self._current is not None and self._current.machine_id == handle.machine_id:
            self._current = None
        self._delete_security_group_if_unused(ec2)

    def list_machines(self) -> list[MachineHandle]:
        profile, region = self._chosen()
        return [
            self._listed_handle(profile, region, i) for i in self.api(profile, region).instances()
        ]

    def sign_in(self, cancel: threading.Event) -> None:
        """Run the AWS CLI's browser sign-in for the chosen profile (``aws
        login``, or ``aws sso login``) and wait until it ends."""
        aws = aws_account.find_aws()
        if not aws:
            raise ProviderError(
                "Couldn't find the AWS CLI to sign in. Install it: "
                + aws_account.AWS_CLI_INSTALL_URL
            )
        chosen = self.settings()
        name = chosen.account or aws_account.default_profile()
        profiles = {p.name: p for p in aws_account.list_profiles()}
        profile = profiles.get(name, aws_account.AwsProfile(name))
        aws_account.sign_in(aws, profile, chosen.region, cancel)

    def ensure_access(self, handle: MachineHandle | None = None) -> None:
        """Let this computer reach the machine: update the security group if
        the computer's public IP address changed."""
        handle = handle or self._current
        if handle is None or "region" not in handle.extra:
            return
        ec2, _ = self._where(handle)
        self._ensure_security_group(ec2, lambda message: None)

    def delete_leftovers(self, keep: MachineHandle | None = None) -> list[str]:
        """Delete the add-on's other machines in the chosen region (e.g. left
        by an Orange that quit while starting one); returns their names."""
        profile, region = self._chosen()
        ec2 = self.api(profile, region)
        keep_id = keep.machine_id if keep is not None else None
        others = [i for i in ec2.instances() if tag_value(i, TAG_SESSION) != keep_id]
        if others:
            self._terminate(ec2, [i["InstanceId"] for i in others])
        if keep is None:
            self._delete_security_group_if_unused(ec2)
        return [tag_value(i, "Name") or i["InstanceId"] for i in others]

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
        """Upload a local file to the account's bucket (unless it's there
        already) and return its ``s3://`` URI. Without a bucket, raises
        :class:`StorageMissingError` until ``create_bucket`` names it."""
        storage = self._storage_of(handle)
        bucket = storage.find_bucket()
        if bucket is None:
            if not create_bucket:
                name = new_bucket_name()
                raise StorageMissingError(
                    "Files from this computer go to a storage bucket in your AWS account, "
                    f"and there is none yet. Create the bucket {name} in {storage.region}?",
                    name,
                )
            if on_status is not None:
                on_status(f"Creating the bucket {create_bucket}")
            bucket = storage.create_bucket(create_bucket)
            if on_status is not None:
                on_status(f"Uploading the file to {self.label} storage")
        return storage.upload(bucket, path, on_progress)

    def machine_location(self, location: str, handle: MachineHandle | None = None) -> str:
        """For an ``s3://`` file, a link signed for the machine with the
        user's credentials, made anew at each load (it lives an hour at most,
        less with temporary credentials). Other locations as they are."""
        if not location.lower().startswith(self.storage_schemes):
            return location
        return self._storage_of(handle).signed_link(location)

    def read_stored_head(self, uri: str, length: int) -> tuple[int, bytes]:
        """``(size, first length bytes)`` of an ``s3://`` file, read with
        the user's credentials."""
        return self._storage_of(None).read_head(uri, length)

    def stored_files(self, handle: MachineHandle | None = None) -> list[StoredFile]:
        """The files in the account's bucket (none if there is no bucket)."""
        storage = self._storage_of(handle)
        bucket = storage.find_bucket()
        return storage.files(bucket) if bucket else []

    def delete_stored_files(self, uris: Sequence[str]) -> None:
        """Delete files from the add-on's bucket (no others)."""
        for uri in uris:
            if not parse_s3_uri(uri)[0].startswith(BUCKET_PREFIX):
                raise ProviderError(f"{uri} isn't in the bucket of the add-on.")
        if uris:
            self._storage_of(None).delete(uris)

    # ------------------------------------------------------------------
    # Helpers

    def _new_machine(
        self,
        spec: EnvironmentSpec,
        target: Ec2Target,
        image: str,
        idle_timeout: float | None,
        max_lifetime: float | None,
    ) -> tuple[MachineHandle, bytes]:
        """A new machine's handle (no instance yet) and its user data."""
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
        data = user_data(image, settings, PLATFORM, registry_token(image))
        if len(data) > USER_DATA_LIMIT:
            raise ProviderError(
                f"The machine's settings (mostly your list of packages) take "
                f"{len(data) / 1024:.1f} KB compressed, more than the {USER_DATA_LIMIT // 1024} KB "
                "that AWS allows. Uninstall packages that Orange doesn't need."
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
                "profile": target.profile,
                "region": target.region,
                "instance_id": "",
                "image": image,
                "disk_gb": str(target.disk_gb),
            },
        )
        return handle, data

    def _ensure_security_group(self, ec2: Ec2, report: StatusCallback) -> str:
        """Create or update the security group so that it allows exactly
        this computer's address; returns its ID."""
        address = public_ip()
        cidr = f"{address}/32"
        group = ec2.security_group()
        if group is None:
            report(f"Creating the security group {SECURITY_GROUP} for {address}")
            group_id = ec2.create_security_group()
            ec2.authorize(group_id, cidr)
            return group_id
        group_id, permissions = group["GroupId"], group.get("IpPermissions", [])
        if not allows(permissions, cidr):
            report(f"Updating the security group {SECURITY_GROUP} for {address}")
            ec2.authorize(group_id, cidr)
        stale = stale_permissions(permissions, cidr)
        if stale:
            ec2.revoke(group_id, stale)
        return group_id

    def _delete_security_group_if_unused(self, ec2: Ec2) -> None:
        if ec2.instances(_USING_GROUP):
            return
        group = ec2.security_group()
        if group is not None:
            with contextlib.suppress(NotFoundError):  # already gone
                ec2.delete_security_group(group["GroupId"])

    def _terminate(self, ec2: Ec2, instance_ids: list[str]) -> None:
        """Terminate the instances and wait until they are gone, so that
        nothing uses the security group any more."""
        with contextlib.suppress(NotFoundError):  # already gone
            ec2.terminate(instance_ids)
        deadline = time.monotonic() + TERMINATE_TIMEOUT
        waiting = list(instance_ids)
        while waiting:
            waiting = [
                i
                for i in waiting
                if (ec2.instance(i) or {}).get("State", {}).get("Name", "terminated")
                != "terminated"
            ]
            if not waiting:
                return
            if time.monotonic() > deadline:
                raise ProviderError(
                    f"AWS didn't delete {', '.join(waiting)} within {TERMINATE_TIMEOUT} s."
                )
            time.sleep(self.poll_interval)

    def _find_instance(self, ec2: Ec2, handle: MachineHandle) -> str:
        """The ID of the instance tagged with the handle's machine ID (``""``
        if there is none), kept in the handle."""
        found = ec2.instances(session=handle.machine_id)
        if found:
            handle.extra["instance_id"] = found[0]["InstanceId"]
        return handle.extra.get("instance_id", "")

    def _delete_after_failure(self, handle: MachineHandle) -> None:
        """Clean up after a failed start; the start's own error is raised."""
        try:
            self.delete(handle)
        except (ProviderError, OSError) as exc:  # logged; the start's error is raised
            log.warning("Couldn't delete %s after it failed to start: %s", handle.name, exc)

    def _listed_handle(self, profile: str, region: str, instance: dict[str, Any]) -> MachineHandle:
        machine_id = tag_value(instance, TAG_SESSION) or instance["InstanceId"]
        ip = instance.get("PublicIpAddress", "")
        launched = instance.get("LaunchTime")
        return MachineHandle(
            provider=self.id,
            machine_id=machine_id,
            name=tag_value(instance, "Name") or NAME_PREFIX + machine_id,
            url=f"https://{ip}:{self.port}" if ip else "",
            token="",
            cert_pem="",
            machine_type=instance.get("InstanceType"),
            created_at=launched.isoformat() if hasattr(launched, "isoformat") else "",
            extra={
                "profile": profile,
                "region": region,
                "instance_id": instance["InstanceId"],
                "state": instance.get("State", {}).get("Name", ""),
            },
        )

    def _client_when_running(
        self, handle: MachineHandle, instance: dict[str, Any]
    ) -> CloudClient | None:
        """A client of the machine once it runs and has its address."""
        ip = instance.get("PublicIpAddress")
        if instance.get("State", {}).get("Name") != "running" or not ip:
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
        self, ec2: Ec2, handle: MachineHandle, report: StatusCallback, timeout: float
    ) -> None:
        """Wait until ``/v1/info`` answers; report the machine's progress
        from its console."""
        instance_id = handle.extra["instance_id"]
        deadline = time.monotonic() + timeout
        console = _Console(ec2, instance_id, report, self.console_interval)
        client: CloudClient | None = None
        report("Booting the machine")
        try:
            while True:
                instance = ec2.instance(instance_id) or {}  # AWS may not know it at once
                state = instance.get("State", {}).get("Name", "pending")
                if client is None:
                    client = self._client_when_running(handle, instance)
                console.read()
                if client is not None and answers(client):
                    return
                if state not in ("pending", "running"):
                    raise ProviderError(
                        f"The machine stopped while starting ({state}).\n\n{console.tail()}"
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
    """A starting machine's console, read now and then: reports its
    progress, and raises :class:`ProviderError` when the boot failed."""

    def __init__(self, ec2: Ec2, instance_id: str, report: StatusCallback, interval: float):
        self.ec2 = ec2
        self.instance_id = instance_id
        self.report = report
        self.interval = interval
        self.lines: list[str] = []
        self._last: str | None = None
        self._next = 0.0

    def read(self) -> None:
        """Read the console if it is time to."""
        if time.monotonic() < self._next:
            return
        self._next = time.monotonic() + self.interval
        try:
            text = self.ec2.console(self.instance_id)
        except ProviderError as exc:  # only progress messages are lost
            log.debug("Couldn't read the output of %s: %s", self.instance_id, exc)
            return
        if text:
            self.lines = text.splitlines()
        if boot_failed(self.lines):
            raise ProviderError(f"The machine couldn't start.\n\n{self.tail()}")
        message = progress_message(self.lines)
        if message and message != self._last:
            self.report(f"Starting: {message}")
            self._last = message

    def tail(self) -> str:
        """The machine's last lines, for an error message."""
        return log_tail(self.lines)


def _stopped_message(instance: dict[str, Any]) -> str:
    """Why a stopped instance stopped, for the user. A machine that powered
    itself off doesn't say why (EC2 clears its console), so the session
    explains it from the machine's last ``/v1/info``."""
    reason = instance.get("StateReason") or {}
    code = reason.get("Code", "")
    if code == "Client.InstanceInitiatedShutdown" or not code:
        return "The machine stopped."
    if code == "Client.UserInitiatedShutdown":
        return "The machine stopped: it was stopped outside Orange."
    return f"The machine stopped: {reason.get('Message') or code}."
