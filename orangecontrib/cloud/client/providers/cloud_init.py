"""The cloud-init user data of machines that get their settings as files
(on AWS and Azure). No API calls.

The user data is ``#cloud-config`` followed by JSON (which is valid YAML and
needs no escaping), compressed with gzip, which cloud-init unpacks: EC2
allows only 16 KB, and a typical environment's settings are about that.

* **Settings:** cloud-init writes each setting (token, certificate and key,
  EnvironmentSpec, time limits, machine size) to a file in
  :data:`SETTINGS_DIR` (in memory, readable only by root). The systemd unit
  gives them to the image's user (uid 1000) before the container starts,
  and mounts the folder read-only into the container at
  :data:`CONTAINER_SETTINGS_DIR`, where the bootstrap's platform (e.g.
  ``aws``) reads them.
* **Image:** the unit signs Docker in to the registry when a registry token
  is given (a private development image) and pulls the image, both with
  retries, and runs it with the host's network. Its output goes to the console, where
  the add-on reads the progress.
* **Stopping:** the container never powers off the VM itself
  (``ORANGE3_CLOUD_POWER_OFF=host``); the cloud's :class:`HostSetup` does,
  when the container exits for any reason (e.g. :data:`POWER_OFF`).
"""

from __future__ import annotations

import gzip
import json
import shlex
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "CONTAINER_SETTINGS_DIR",
    "CONTAINER_UID",
    "POWER_OFF",
    "REGISTRY_TOKEN_FILE",
    "SETTINGS_DIR",
    "UNIT",
    "HostSetup",
    "unit_file",
    "user_data",
]

#: Name of the container and the systemd unit on the VM.
UNIT = "orange3-cloud"
#: The settings files on the VM (tmpfs).
SETTINGS_DIR = "/run/orange3-cloud/settings"
#: Where the container sees them (the bootstrap's default).
CONTAINER_SETTINGS_DIR = "/run/orange3-cloud-settings"
#: The registry token on the VM (only for private images).
REGISTRY_TOKEN_FILE = "/run/orange3-cloud/registry-token"
#: The image's user, who must read the settings.
CONTAINER_UID = 1000


@dataclass(frozen=True)
class HostSetup:
    """What a cloud adds to its VMs: lines of the unit's ``[Unit]`` and
    ``[Service]`` sections, more cloud-init ``write_files`` entries, and
    packages to install."""

    unit: tuple[str, ...] = ()
    service: tuple[str, ...] = ()
    files: tuple[dict[str, str], ...] = ()
    packages: tuple[str, ...] = ()


#: The VM powers off when the container exits (on AWS, the instance stops).
POWER_OFF = HostSetup(unit=("SuccessAction=poweroff", "FailureAction=poweroff"))


def unit_file(image: str, platform: str, private: bool, host: HostSetup = POWER_OFF) -> str:
    """The systemd unit that runs ``image`` with the settings files for the
    bootstrap's ``platform``; ``private``: sign Docker in to the image's
    registry with the token in :data:`REGISTRY_TOKEN_FILE` first."""
    q = shlex.quote(image)
    pre = [
        f"-/usr/bin/docker rm --force {UNIT}",
        f"/bin/chown -R {CONTAINER_UID}:{CONTAINER_UID} {SETTINGS_DIR}",
        f"/bin/chmod 0700 {SETTINGS_DIR}",
    ]
    # Signing in is retried with the pull: right after boot, the network may
    # not reach the registry yet.
    login = ""
    if private:
        registry = image.split("/", 1)[0]
        login = (
            "/usr/bin/docker login --username oauth2accesstoken --password-stdin "
            f"https://{registry} < {REGISTRY_TOKEN_FILE} && "
        )
    pre.append(
        "/bin/sh -c 'for i in 1 2 3 4 5 6 7 8 9 10; do"
        ' echo "[orange3-cloud] pulling the machine image";'
        f" {login}/usr/bin/docker pull --quiet {q} && exit 0; sleep 10; done; exit 1'"
    )
    run = (
        f"/usr/bin/docker run --rm --init --name {UNIT} --network host"
        f" --volume {SETTINGS_DIR}:{CONTAINER_SETTINGS_DIR}:ro"
        f" --env ORANGE3_CLOUD_CONFIG={platform} --env ORANGE3_CLOUD_POWER_OFF=host {q}"
    )
    lines = [
        "[Unit]",
        "Description=orange3-cloud machine",
        "Wants=network-online.target docker.service",
        "After=network-online.target docker.service",
        *host.unit,
        "",
        "[Service]",
        f"Environment=DOCKER_CONFIG=/run/{UNIT}/docker",
        "StandardOutput=journal+console",
        "StandardError=journal+console",
        *(f"ExecStartPre={line}" for line in pre),
        f"ExecStart={run}",
        f"ExecStop=-/usr/bin/docker stop --time 40 {UNIT}",
        *host.service,
        "TimeoutStartSec=infinity",
        "TimeoutStopSec=120",
    ]
    return "\n".join(lines) + "\n"


def user_data(
    image: str,
    settings: Mapping[str, str],
    platform: str,
    registry_token: str | None = None,
    host: HostSetup = POWER_OFF,
) -> bytes:
    """The gzip-compressed cloud-init user data that writes ``settings``
    (name -> value) to files and runs ``image`` for the bootstrap's
    ``platform``, signing in to its registry with ``registry_token`` if
    given."""
    private = {"permissions": "0600", "owner": "root:root"}
    files = [
        {"path": f"{SETTINGS_DIR}/{name}", **private, "content": value}
        for name, value in settings.items()
    ]
    if registry_token:
        files.append({"path": REGISTRY_TOKEN_FILE, **private, "content": registry_token})
    files.append(
        {
            "path": f"/etc/systemd/system/{UNIT}.service",
            "permissions": "0644",
            "content": unit_file(image, platform, bool(registry_token), host),
        }
    )
    files.extend(host.files)
    config: dict = {"write_files": files}
    if host.packages:
        config["packages"] = list(host.packages)
    config["runcmd"] = [
        ["systemctl", "daemon-reload"],
        ["systemctl", "start", "--no-block", f"{UNIT}.service"],
    ]
    text = "#cloud-config\n" + json.dumps(config, separators=(",", ":")) + "\n"
    return gzip.compress(text.encode(), mtime=0)
