"""Where the machine runs: the platforms of :mod:`bootstrap`.

Each :class:`Platform` knows where the machine's settings come from and how
the machine stops at the end: environment variables locally
(:class:`Environment`), the GCE metadata server on Google Cloud
(:class:`GoogleCloud`), and files that cloud-init wrote on AWS and Azure
(:class:`AmazonEC2`, :class:`AzureVM`). Another cloud is another small
subclass in :data:`PLATFORMS`. See :mod:`bootstrap` for the settings.

Like :mod:`bootstrap`, it uses only the standard library and runs on the
image's own small Python.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

METADATA_URL = "http://metadata.google.internal/computeMetadata/v1/"
METADATA_HEADERS = {"Metadata-Flavor": "Google"}
#: EC2's instance metadata service (IMDSv2).
EC2_METADATA_URL = "http://169.254.169.254/latest/"
#: Azure's instance metadata service, and Resource Manager.
AZURE_METADATA_URL = "http://169.254.169.254/metadata/"
AZURE_MANAGEMENT = "https://management.azure.com"
COMPUTE_API = "https://compute.googleapis.com/compute/v1/"

_started = time.monotonic()


class BootError(Exception):
    """The machine can't start; the message says why."""


def log(message: str) -> None:
    """Print a line of the boot's log, with the seconds since it started."""
    print(f"[bootstrap {time.monotonic() - _started:7.1f}s] {message}", flush=True)


class Platform:
    """Where the machine runs: where its settings come from (:meth:`read`)
    and how it stops at the end (:meth:`power_off`)."""

    #: The value of ``ORANGE3_CLOUD_CONFIG`` that chooses it.
    name = ""
    #: Where the settings come from, for the log.
    source = ""
    #: Whether this script stops the machine when the server exits (on a
    #: cloud VM; never locally).
    stops_machine = False

    def detect(self) -> bool:
        """Whether the machine runs here (for ``ORANGE3_CLOUD_CONFIG=auto``)."""
        return False

    def read(self, name: str) -> str | None:
        """The setting ``name`` (e.g. ``tls-cert``), or None if it isn't set."""
        raise NotImplementedError

    def machine_type(self) -> str | None:
        """The machine's type, if the platform knows it."""
        return None

    def power_off(self) -> bool:
        """Stop the machine, so it stops costing compute. Best effort;
        returns True if a way was accepted."""
        return True


class Environment(Platform):
    """Local mode: settings from environment variables; the container just exits."""

    name = "env"
    source = "env"

    def detect(self) -> bool:
        return "ORANGE3_CLOUD_TOKEN" in os.environ or "ORANGE3_CLOUD_TOKEN_FILE" in os.environ

    def read(self, name: str) -> str | None:
        var = "ORANGE3_CLOUD_" + name.upper().replace("-", "_")
        if var in os.environ:
            return os.environ[var]
        if var + "_FILE" in os.environ:
            path = Path(os.environ[var + "_FILE"])
            try:
                return path.read_text()
            except OSError as exc:
                raise BootError(f"can't read {var}_FILE ({path}): {exc}") from exc
        return None


def _metadata(path: str, timeout: float = 5.0) -> str | None:
    request = urllib.request.Request(METADATA_URL + path, headers=METADATA_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


class GoogleCloud(Platform):
    """A Compute Engine VM: settings from the GCE metadata server; at the
    end the VM powers off."""

    name = "gce"
    source = "GCE metadata"
    stops_machine = True

    def detect(self) -> bool:
        try:
            return _metadata("instance/id", timeout=2.0) is not None
        except (OSError, ValueError):
            return False

    def read(self, name: str) -> str | None:
        return _metadata(f"instance/attributes/orange3-cloud-{name}")

    def machine_type(self) -> str | None:
        return (_metadata("instance/machine-type") or "").rsplit("/", 1)[-1] or None

    def power_off(self) -> bool:
        """Power off the VM: through the host's systemd, else with the
        Compute Engine API's ``instances.stop`` on this VM, as the VM's
        service account (token from the metadata server). That works without
        privileges when the account may stop the VM
        (``compute.instances.stop``) and the VM has the ``cloud-platform``
        scope. See :func:`power_off_host` and :func:`host_powers_off`."""
        if host_powers_off():
            log("the VM powers off when this container exits")
            return True
        log("powering off the VM")
        if power_off_host():
            return True
        try:
            _stop_instance_via_api()
        except (OSError, ValueError, BootError) as exc:
            log(f"couldn't stop the VM through the Compute Engine API: {exc}")
        else:
            log("the Compute Engine API is stopping the VM")
            return True
        log("ERROR: could not power off the VM; it keeps running until it is stopped or deleted")
        return False


def settings_dir() -> Path:
    """The folder of the settings files (:class:`SettingsFiles`)."""
    return Path(os.environ.get("ORANGE3_CLOUD_SETTINGS_DIR", "/run/orange3-cloud-settings"))


class SettingsFiles(Platform):
    """A VM whose settings cloud-init wrote to files, one per setting, in
    :func:`settings_dir` (mounted read-only into the container). Whoever
    started the container powers the VM off when it exits."""

    source = "settings files"
    stops_machine = True

    def read(self, name: str) -> str | None:
        path = settings_dir() / name
        try:
            return path.read_text()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise BootError(f"can't read the setting {name} ({path}): {exc}") from exc

    def power_off(self) -> bool:
        """Leave it to the host (``ORANGE3_CLOUD_POWER_OFF=host``), else
        power the host off (:func:`power_off_host`)."""
        if host_powers_off():
            log("the VM powers off when this container exits")
            return True
        log("powering off the VM")
        if power_off_host():
            return True
        log("ERROR: could not power off the VM; it keeps running until it is stopped or deleted")
        return False


def _ec2_metadata_token(timeout: float = 2.0) -> str | None:
    """An IMDSv2 session token, or None if EC2's metadata service doesn't answer."""
    request = urllib.request.Request(
        EC2_METADATA_URL + "api/token",
        method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode()
    except (OSError, ValueError):
        return None


class AmazonEC2(SettingsFiles):
    """An EC2 instance: settings from files; the instance stops (it isn't
    terminated) when the VM powers off."""

    name = "aws"
    source = "settings files on EC2"

    def detect(self) -> bool:
        return settings_dir().is_dir() and _ec2_metadata_token() is not None


def _azure_metadata(path: str, timeout: float = 5.0) -> str:
    request = urllib.request.Request(AZURE_METADATA_URL + path, headers={"Metadata": "true"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode()


def _deallocate_via_api() -> None:
    """Ask Azure Resource Manager to deallocate this VM, as the VM's managed
    identity (token from the instance metadata service)."""
    resource = f"{AZURE_MANAGEMENT}/"
    token = json.loads(
        _azure_metadata(f"identity/oauth2/token?api-version=2018-02-01&resource={resource}")
    )
    vm = _azure_metadata("instance/compute/resourceId?api-version=2021-02-01&format=text")
    _post(f"{AZURE_MANAGEMENT}{vm}/deallocate?api-version=2024-07-01", token["access_token"])


class AzureVM(SettingsFiles):
    """An Azure VM: settings from files; at the end the VM is deallocated
    (a VM that only powers off stays allocated, and costs compute)."""

    name = "azure"
    source = "settings files on Azure"

    def detect(self) -> bool:
        try:
            return settings_dir().is_dir() and bool(
                _azure_metadata("instance/compute/vmId?api-version=2021-02-01&format=text", 2.0)
            )
        except (OSError, ValueError):
            return False

    def power_off(self) -> bool:
        """Leave it to the host (``ORANGE3_CLOUD_POWER_OFF=host``: the add-on's
        unit deallocates the VM when the container exits), else deallocate
        the VM with its managed identity, which may do that, else power off."""
        if host_powers_off():
            log("the VM is deallocated when this container exits")
            return True
        log("deallocating the VM")
        try:
            _deallocate_via_api()
        except (OSError, ValueError, KeyError) as exc:
            log(f"couldn't deallocate the VM through Azure Resource Manager: {exc}")
        else:
            log("Azure is deallocating the VM")
            return True
        return super().power_off()


#: The platforms, in the order ``auto`` tries them (after a token variable).
PLATFORMS: tuple[Platform, ...] = (Environment(), GoogleCloud(), AmazonEC2(), AzureVM())


def find_platform() -> Platform:
    """The platform ``ORANGE3_CLOUD_CONFIG`` names, or the one found (``auto``)."""
    wanted = os.environ.get("ORANGE3_CLOUD_CONFIG", "auto")
    by_name = {p.name: p for p in PLATFORMS}
    if wanted != "auto":
        if wanted not in by_name:
            names = ", ".join(["auto", *by_name])
            raise BootError(f"ORANGE3_CLOUD_CONFIG must be one of {names}, not {wanted!r}")
        return by_name[wanted]
    for platform in PLATFORMS:
        if platform.detect():
            return platform
    raise BootError(
        "no configuration: set ORANGE3_CLOUD_TOKEN or ORANGE3_CLOUD_TOKEN_FILE, "
        "or run on a cloud machine started by the add-on"
    )


def _stop_instance_via_api() -> None:
    """Ask the Compute Engine API to stop this VM, as its service account."""
    token = json.loads(_metadata("instance/service-accounts/default/token") or "{}")
    project = _metadata("project/project-id")
    zone = (_metadata("instance/zone") or "").rsplit("/", 1)[-1]
    name = _metadata("instance/name")
    if not (token.get("access_token") and project and zone and name):
        raise BootError("the metadata server didn't give a token, project, zone and name")
    _post(
        f"{COMPUTE_API}projects/{project}/zones/{zone}/instances/{name}/stop", token["access_token"]
    )


def _post(url: str, token: str) -> None:
    """An empty ``POST`` to a cloud's API with a bearer ``token``."""
    request = urllib.request.Request(
        url, data=b"", method="POST", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(request, timeout=30):
        pass


def host_powers_off() -> bool:
    """Whether the host powers the VM off when the container exits
    (``ORANGE3_CLOUD_POWER_OFF=host``), so this script needn't."""
    return os.environ.get("ORANGE3_CLOUD_POWER_OFF", "") == "host"


def power_off_host() -> bool:
    """Power off the host with ``systemctl poweroff`` in its namespaces
    (``nsenter`` into PID 1), on any cloud. A container can't power off its
    host by itself: this works only when it runs as root with ``--privileged
    --pid=host``. Whoever starts the container can instead power the VM off
    when the container exits (e.g. a systemd unit with
    ``SuccessAction=poweroff`` and ``FailureAction=poweroff``) and set
    ``ORANGE3_CLOUD_POWER_OFF=host``."""
    if os.geteuid() != 0 or not shutil.which("nsenter"):
        return False
    namespaces = [f"--{ns}" for ns in ("mount", "uts", "ipc", "net", "pid")]
    command = ["nsenter", "--target", "1", *namespaces, "--", "systemctl", "poweroff"]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode == 0:
        log("the host is powering off")
        return True
    log(f"couldn't power off through the host's systemd: {result.stderr.strip()}")
    return False
