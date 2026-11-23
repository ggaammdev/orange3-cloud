"""The interface every machine provider implements.

A :class:`ClusterProvider` starts, finds and stops the one cloud machine of a
session (in local Docker, in the user's Google Cloud project, ...). Widgets
never talk to providers: they use :class:`~orangecontrib.cloud.client.session.
CloudSession`, which holds the provider and the :class:`MachineHandle` of the
running machine.

Everything that differs between clouds is behind this interface, so another
cloud is a new provider module (registered in :mod:`~orangecontrib.cloud.
client.providers.registry`) and, if it has an account to choose, its page in
the Cloud settings dialog: its machine sizes (:attr:`ClusterProvider.
machine_sizes`), their prices (:meth:`ClusterProvider.size_price`), its
storage (:meth:`ClusterProvider.stage_file`, :meth:`ClusterProvider.
machine_location`) and its settings (``CloudSettings.of(<provider id>)``,
see :mod:`orangecontrib.cloud.settings`).

When a machine stops by itself, its cloud bills only what it keeps while
stopped (e.g. its disk) until the add-on deletes it: :meth:`ClusterProvider.
stopped_at` and :meth:`ClusterProvider.stopped_price_per_hour` tell the
session when that began and what it costs, so that it can record the cost.

A handle has everything needed to connect: the URL, the token and the
machine's self-signed certificate. The token and the certificate are secrets
of the user's computer. They are stored only in the add-on's private folder
(:func:`state_dir`, files with mode ``0600``), never in workflows.
"""

from __future__ import annotations

import datetime
import ipaddress
import os
import re
import secrets
import tempfile
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from orangecontrib.cloud.env import EnvironmentSpec
from orangecontrib.cloud.settings import DEFAULT_IDLE_TIMEOUT, DEFAULT_MAX_LIFETIME

__all__ = [
    "DEFAULT_IDLE_TIMEOUT",
    "DEFAULT_MAX_LIFETIME",
    "ClusterProvider",
    "MachineHandle",
    "MachineState",
    "MachineStatus",
    "MachineType",
    "PUBLIC_IMAGE",
    "ProviderError",
    "SignInCancelledError",
    "StatusCallback",
    "StorageMissingError",
    "StoredFile",
    "UploadProgress",
    "addon_version",
    "is_release",
    "new_token",
    "self_signed_certificate",
    "state_dir",
    "utc_time",
    "write_private",
]

#: ``on_status(message)``: progress of a slow provider call, for the user.
StatusCallback = Callable[[str], None]
#: ``on_progress(bytes_sent, total_bytes)`` of an upload; it may raise to stop
#: the upload (e.g. when the user cancels).
UploadProgress = Callable[[int, int], None]

#: The released machine image (public), tagged with the add-on's version.
PUBLIC_IMAGE = "ghcr.io/ggaammdev/orange3-cloud-machine"


def is_release(version: str) -> bool:
    """Whether ``version`` of the add-on is a release (it has a public image)."""
    from packaging.version import InvalidVersion, Version

    try:
        parsed = Version(version)
    except InvalidVersion:
        return False
    return not (parsed.is_devrelease or parsed.local)


def addon_version(spec: EnvironmentSpec) -> str:
    """The add-on's version in ``spec``, or ``"0"`` if it isn't listed."""
    addon = spec.get("orange3-cloud")
    return getattr(addon, "version", None) or "0"


@dataclass(frozen=True)
class MachineType:
    """A machine size the user can choose."""

    name: str  #: e.g. ``n2-standard-16``, or ``local``
    label: str  #: for the user, e.g. "Medium"
    cpus: int | None = None
    memory_gb: float | None = None


class ProviderError(Exception):
    """A provider couldn't do what was asked; the message says why, for the user."""


class SignInCancelledError(ProviderError):
    """The user cancelled signing in (:meth:`ClusterProvider.sign_in`)."""


class StorageMissingError(ProviderError):
    """The provider's storage for uploaded files doesn't exist yet. Ask the
    user, then call :meth:`ClusterProvider.stage_file` again with
    ``create_bucket=bucket``."""

    def __init__(self, message: str, bucket: str):
        super().__init__(message)
        #: The name the new storage bucket would get.
        self.bucket = bucket


@dataclass(frozen=True)
class StoredFile:
    """A file uploaded to the provider's storage."""

    uri: str  #: what the machine loads, e.g. ``gs://bucket/uploads/…/data.csv``
    name: str  #: the file's name, for the user
    size_bytes: int
    updated: str = ""  #: when it was uploaded (ISO 8601)


class MachineState(str, Enum):
    """Where a machine is in its life."""

    NOT_FOUND = "not_found"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass(frozen=True)
class MachineStatus:
    """A machine's state, and details for the user."""

    state: MachineState
    #: Details for the user, e.g. why the machine stopped.
    message: str = ""

    @property
    def exists(self) -> bool:
        """True unless the machine wasn't found."""
        return self.state is not MachineState.NOT_FOUND

    @property
    def alive(self) -> bool:
        """Running, or on the way there."""
        return self.state in (MachineState.STARTING, MachineState.RUNNING)


@dataclass
class MachineHandle:
    """How to find and reach one machine.

    ``token`` and ``cert_pem`` are secrets: :meth:`to_dict` includes them, so
    the result may only be written with :func:`write_private`.
    """

    provider: str  #: provider id, e.g. ``local``
    machine_id: str  #: unique per machine start
    name: str  #: e.g. the container or VM name
    url: str  #: base URL, without ``/v1``
    token: str = field(repr=False)
    cert_pem: str = field(repr=False)
    #: Name the certificate is checked against, if not the URL's host.
    server_hostname: str | None = None
    machine_type: str | None = None
    created_at: str = field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"
        )
    )
    #: Provider-specific details, e.g. a GCP project and zone.
    extra: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """The handle as JSON values."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MachineHandle:
        """A handle from :meth:`to_dict`'s values; unknown keys are ignored."""
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in data.items() if k in known})


class ClusterProvider(ABC):
    """Starts, finds and stops machines in one place (local Docker, a GCP
    project, ...).

    Calls may be slow (seconds to minutes); :class:`CloudSession` runs them on
    a background thread, so they must not touch the GUI. ``on_status`` gets
    progress messages for the user.
    """

    #: Short id stored in handles, e.g. ``local`` or ``gcp``.
    id: str = ""
    #: For the user, e.g. "Local (Docker)".
    label: str = ""
    #: Whether local files go to the provider's storage (:meth:`stage_file`),
    #: which keeps them for later sessions, rather than straight to the machine.
    stores_uploads: bool = False
    #: The machine sizes offered here, from the least memory to the most
    #: (empty: one machine of a fixed size, e.g. this computer).
    machine_sizes: tuple[MachineType, ...] = ()
    #: The name of the size used unless the user chooses another.
    default_size: str = ""
    #: Schemes of locations in the provider's storage that the machine can
    #: load (see :meth:`machine_location`), e.g. ``("gs://",)``. Any machine
    #: also loads ``https://`` and ``http://`` URLs.
    storage_schemes: tuple[str, ...] = ()
    #: Whether machines left behind here cost money, so the Cloud settings
    #: dialog looks for them (:meth:`list_machines`) and offers to delete them.
    finds_leftovers: bool = False

    @abstractmethod
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
        """Start a machine with the user's environment and wait until its API
        answers. ``None`` or 0 turns a time limit off. ``timeout``: seconds to
        wait for the machine, ``None`` for the provider's default."""

    @abstractmethod
    def status(self, handle: MachineHandle | None = None) -> MachineStatus:
        """The state of the machine of ``handle`` (default: the provider's
        current machine)."""

    @abstractmethod
    def stop(self, handle: MachineHandle | None = None) -> None:
        """Stop the machine. It may still exist afterwards (an exited
        container, a stopped VM); :meth:`delete` removes it."""

    @abstractmethod
    def delete(self, handle: MachineHandle | None = None) -> None:
        """Remove the machine and everything created for it. A machine that
        is already gone is not an error."""

    @abstractmethod
    def list_machines(self) -> list[MachineHandle]:
        """The add-on's machines that exist here, running or not. Handles
        listed here may have no token or certificate (they can't be read
        back from the machine)."""

    def machine_types(self) -> list[MachineType]:
        """The machine sizes to choose from, from the least memory to the most.
        Default: :attr:`machine_sizes`."""
        return list(self.machine_sizes)

    def size_or_default(self, name: str | None) -> str:
        """``name`` if it is one of :attr:`machine_sizes`, else :attr:`default_size`."""
        return name if name in {t.name for t in self.machine_sizes} else self.default_size

    @abstractmethod
    def price_per_hour(self, machine_type: str | None = None) -> float | None:
        """USD per hour for ``machine_type``; 0 for free, ``None`` if unknown."""

    def size_price(self, machine_type: str, region: str, disk_gb: int) -> float | None:
        """USD per hour of a new machine of ``machine_type`` in ``region`` with
        a boot disk of ``disk_gb`` GB, from the provider's bundled prices
        (:mod:`~orangecontrib.cloud.client.providers.prices`); ``None`` if
        unknown. Default: unknown."""
        return None

    def prices_date(self) -> str:
        """When the prices of :meth:`size_price` were read (ISO date); ``""``
        without prices."""
        return ""

    def machine_price_per_hour(self, handle: MachineHandle) -> float | None:
        """USD per hour of the machine of ``handle`` (``None``: unknown).
        Default: :meth:`price_per_hour` of its machine type."""
        return self.price_per_hour(handle.machine_type)

    def stopped_at(self, handle: MachineHandle) -> datetime.datetime | None:
        """When the machine of ``handle``, which exists and isn't running,
        stopped costing its full price, from its cloud's records; ``None`` if
        it is still running or the cloud doesn't say. Default: ``None``."""
        return None

    def stopped_price_per_hour(self, handle: MachineHandle) -> float | None:
        """USD per hour of the machine of ``handle`` while it is stopped (what
        its cloud still bills, e.g. its disk); ``None`` if unknown. Default:
        unknown."""
        return None

    def sign_in(self, cancel: threading.Event) -> None:
        """Sign the user in to the provider's account and wait until that
        ends (e.g. Google's sign-in page in the browser). Setting ``cancel``
        stops it with :class:`SignInCancelledError`; other failures raise
        :class:`ProviderError`. Default: nothing to sign in to."""
        return None

    def ensure_access(self, handle: MachineHandle | None = None) -> None:
        """Make sure this computer may reach the machine, e.g. update a
        firewall rule after the computer's IP address changed. The session
        calls it before reattaching and while reconnecting. Default: nothing."""
        return None

    def delete_leftovers(self, keep: MachineHandle | None = None) -> list[str]:
        """Delete the add-on's machines here other than ``keep`` (left behind,
        e.g., by an Orange that quit while starting one); return their names.
        Default: nothing to clean up."""
        return []

    def stage_file(
        self,
        handle: MachineHandle,
        path: str | os.PathLike,
        *,
        on_progress: UploadProgress | None = None,
        create_bucket: str | None = None,
        on_status: StatusCallback | None = None,
    ) -> str | None:
        """Put a local file where the machine of ``handle`` can load it and
        return its URI, or ``None`` if the file goes straight to the machine
        (``POST /v1/uploads``; the default).

        Raises :class:`StorageMissingError` if the storage must be created
        first; ``create_bucket`` is the name the user agreed to create.
        ``on_status(text)`` gets what it does besides uploading (creating the
        storage, waiting for access to it).
        """
        return None

    def machine_location(self, location: str, handle: MachineHandle | None = None) -> str:
        """What the machine of ``handle`` loads for ``location``: a URL the
        user gave, or a file that :meth:`stage_file` uploaded.

        A provider whose machine can't read its storage by itself turns a
        location there (:attr:`storage_schemes`) into one it can, e.g. a
        short-lived signed ``https://`` link, made anew at each load. Cloud
        File asks the machine to copy a location changed so to its disk with
        one request before reading it, since the link may expire before a
        long load ends. Raises :class:`ProviderError` if the location can't
        be read. Default: the location as it is.
        """
        return location

    def read_stored_head(self, uri: str, length: int) -> tuple[int, bytes]:
        """``(size, first length bytes)`` of a file in the provider's storage
        (:attr:`storage_schemes`), read with the user's sign-in, for the
        memory estimate before loading. Default: no storage to read."""
        raise ProviderError(f"{self.label or self.id} can't read {uri}.")

    def storage_examples(self) -> list[str]:
        """Examples of locations in the provider's storage, for the user: one
        per :attr:`storage_schemes` (default ``<scheme>bucket/data.csv``)."""
        return [f"{scheme}bucket/data.csv" for scheme in self.storage_schemes]

    def stored_files(self, handle: MachineHandle | None = None) -> list[StoredFile]:
        """The files uploaded to the provider's storage (default: none)."""
        return []

    def delete_stored_files(self, uris: Sequence[str]) -> None:
        """Delete uploaded files from the provider's storage."""
        if uris:
            raise ProviderError(f"{self.label or self.id} doesn't keep uploaded files.")


# --------------------------------------------------------------------------
# Times

_TIMESTAMP = re.compile(
    r"(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d:\d\d)(?:\.(\d+))?\s*(Z|GMT|UTC|[+-]\d\d:?\d\d)?",
    re.IGNORECASE,
)


def utc_time(text: str | None) -> datetime.datetime | None:
    """A timestamp of a cloud (ISO 8601 or RFC 3339, e.g. ``2026-11-23T10:15:00Z``,
    ``…T02:15:00.123-08:00`` or ``…T10:15:00.1234567+00:00``) in UTC;
    ``None`` if missing or unreadable. One without a time zone is UTC."""
    match = _TIMESTAMP.fullmatch(text.strip()) if text else None
    if match is None:
        return None
    day, clock, fraction, zone = match.groups()
    zone = (zone or "Z").upper()
    if zone in ("Z", "GMT", "UTC"):
        zone = "+00:00"
    elif ":" not in zone:
        zone = f"{zone[:3]}:{zone[3:]}"
    micro = f".{(fraction + '00000')[:6]}" if fraction else ""
    try:
        parsed = datetime.datetime.fromisoformat(f"{day}T{clock}{micro}{zone}")
    except ValueError:
        return None
    return parsed.astimezone(datetime.timezone.utc)


# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------


def new_token() -> str:
    """A random bearer token for a new machine."""
    return secrets.token_hex(32)


def self_signed_certificate(
    hostnames: Sequence[str] = ("localhost",),
    ips: Sequence[str] = ("127.0.0.1",),
    days: int = 30,
) -> tuple[str, str]:
    """A self-signed certificate and its private key, both PEM text.

    The key is EC P-256 with a named curve (TLS 1.3 rejects explicit curve
    parameters); the certificate's subject alternative names are
    ``hostnames`` and ``ips``.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    common_name = hostnames[0] if hostnames else ips[0]
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    alt_names: list[x509.GeneralName] = [x509.DNSName(h) for h in hostnames]
    alt_names += [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ips]
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode(), key_pem.decode()


def state_dir() -> Path:
    """The add-on's private folder on the user's computer (mode ``0700``).

    ``ORANGE3_CLOUD_STATE_DIR`` if set, else ``orange3-cloud`` in Orange's
    data folder (e.g. ``~/Library/Application Support/Orange/orange3-cloud``
    on macOS). It holds the handle of the current machine and the local
    machine's key: never in workflows.
    """
    folder = os.environ.get("ORANGE3_CLOUD_STATE_DIR")
    if folder:
        path = Path(folder).expanduser()
    else:
        from Orange.misc.environ import data_dir

        path = Path(data_dir(versioned=False)) / "orange3-cloud"
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def write_private(path: Path, text: str) -> Path:
    """Write ``text`` to ``path`` atomically, readable only by the user."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")  # mode 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path
