"""The machine in Docker on the user's own computer ("local mode").

:class:`LocalDockerProvider` runs the generic machine image (``docker/``) with
the ``docker`` command-line tool, so it needs no Python Docker library. There
is one local machine at a time, a container named ``orange3-cloud-local``:

* a new random token and a self-signed certificate for ``localhost`` and
  ``127.0.0.1`` on every start; the certificate's key and the EnvironmentSpec
  are written to ``<state dir>/local-machine/`` (mode ``0700``) and mounted
  read-only into the container,
* the API at ``https://127.0.0.1:8443``, published on 127.0.0.1 only,
* the volume ``orange3-cloud-cache`` for downloads, Pythons and the virtual
  environment, so only the first start downloads everything,
* labels ``orange3-cloud=true`` and ``orange3-cloud-machine=<id>``,
* ``--init``, so the Dask processes are reaped, and no restart policy, so the
  machine stays stopped after the idle timeout or the maximum lifetime.

**Image.** When the add-on runs from a source checkout (the repository with
``docker/Dockerfile``), the image ``orange3-cloud-machine:local`` is built
from it before each start (cached, so usually a second), and the checkout is
mounted at ``/src``, so the machine runs the code of the working copy. An
installed add-on uses ``ORANGE3_CLOUD_LOCAL_IMAGE`` or the ``image`` argument
if set; else a release uses the public image of its version
(``ghcr.io/ggaammdev/orange3-cloud-machine:<version>``, pulled on the first
start), and a development version ``orange3-cloud-machine:local``.

``scripts/local-machine.sh`` starts the same image with Docker Compose
(container ``orange3-cloud-local-machine-1``, secrets in
``~/.orange3-cloud/local-machine``). Both publish port 8443, so only one of
them can run at a time; :meth:`LocalDockerProvider.start` says so when the
other one runs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from orangecontrib.cloud.client.api import CloudClient, CloudError
from orangecontrib.cloud.client.providers.base import (
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_MAX_LIFETIME,
    PUBLIC_IMAGE,
    ClusterProvider,
    MachineHandle,
    MachineState,
    MachineStatus,
    MachineType,
    ProviderError,
    StatusCallback,
    addon_version,
    is_release,
    new_token,
    self_signed_certificate,
    state_dir,
    write_private,
)
from orangecontrib.cloud.env import EnvironmentSpec

__all__ = ["LocalDockerProvider", "source_checkout"]

CONTAINER_NAME = "orange3-cloud-local"
DEFAULT_IMAGE = "orange3-cloud-machine:local"
CACHE_VOLUME = "orange3-cloud-cache"
LABEL = "orange3-cloud"
LABEL_MACHINE = "orange3-cloud-machine"
LABEL_CREATED = "orange3-cloud-created"
#: The compose project of ``scripts/local-machine.sh``.
COMPOSE_PROJECT = "orange3-cloud-local"

#: Where the container reads its configuration.
CONFIG_MOUNT = "/run/orange3-cloud"

#: Seconds to wait for a starting machine; the first start downloads Python
#: and all packages.
DEFAULT_START_TIMEOUT = 600
#: ``docker stop`` waits this long before killing; the server's graceful
#: shutdown is capped at 30 s.
STOP_GRACE = 40

#: Why the container exited, by exit code (see docker/bootstrap.py).
EXIT_REASONS = {
    0: "it was asked to stop",
    3: "it was idle for longer than the idle timeout",
    4: "its maximum lifetime ended",
    137: "it was killed",
    143: "it was stopped",
}

#: Where Docker's CLI may be when Orange is started from the Dock (with a
#: minimal PATH).
_DOCKER_CANDIDATES = (
    "/usr/local/bin/docker",
    "/opt/homebrew/bin/docker",
    "/Applications/Docker.app/Contents/Resources/bin/docker",
    "~/.orbstack/bin/docker",
    "~/.docker/bin/docker",
)


def source_checkout() -> Path | None:
    """The repository root when the add-on runs from a source checkout."""
    import orangecontrib.cloud

    root = Path(orangecontrib.cloud.__file__).resolve().parents[2]
    if (root / "docker" / "Dockerfile").is_file() and (root / "pyproject.toml").is_file():
        return root
    return None


def find_docker() -> str | None:
    """The ``docker`` command: on the PATH, else in the usual install places."""
    found = shutil.which("docker")
    if found:
        return found
    for candidate in _DOCKER_CANDIDATES:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


class LocalDockerProvider(ClusterProvider):
    """The machine in a Docker container on this computer."""

    id = "local"
    label = "Local (Docker)"

    def __init__(
        self,
        *,
        image: str | None = None,
        port: int = 8443,
        container_name: str = CONTAINER_NAME,
        source: Path | None | bool = True,
        config_dir: Path | None = None,
        docker: str | None = None,
    ):
        """
        :param image: image to run; default ``ORANGE3_CLOUD_LOCAL_IMAGE``, else
            chosen at start (see the module's docstring).
        :param source: ``True``: build the image from the source checkout and
            mount it, if there is one; ``False``/``None``: never; or the
            repository root.
        :param config_dir: where the key and the EnvironmentSpec go; default
            ``<state dir>/local-machine``.
        """
        self._image_override = image or os.environ.get("ORANGE3_CLOUD_LOCAL_IMAGE")
        self.image = self._image_override or DEFAULT_IMAGE
        self.port = port
        self.container_name = container_name
        if source is True:
            source = source_checkout()
        self.source: Path | None = Path(source) if source else None
        self._config_dir = config_dir
        self._docker = docker

    # ------------------------------------------------------------------
    # docker CLI

    @property
    def docker(self) -> str:
        """The ``docker`` command; raises ``ProviderError`` if Docker isn't installed."""
        if self._docker is None:
            self._docker = find_docker()
        if self._docker is None:
            raise ProviderError(
                "Docker isn't installed (the 'docker' command wasn't found). Install "
                "Docker Desktop or OrbStack to run the machine on this computer."
            )
        return self._docker

    def _run(self, *args: str, check: bool = True, timeout: float = 120):
        try:
            result = subprocess.run(
                [self.docker, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(f"'docker {args[0]}' didn't finish in {timeout:.0f} s.") from exc
        if check and result.returncode != 0:
            raise ProviderError(_docker_error(args[0], result))
        return result

    def _check_daemon(self) -> None:
        result = self._run("info", "--format", "{{.ServerVersion}}", check=False, timeout=30)
        if result.returncode != 0:
            raise ProviderError(
                "Docker isn't running. Start Docker Desktop (or OrbStack) and try again."
            )

    def _inspect(self) -> dict | None:
        result = self._run("container", "inspect", self.container_name, check=False)
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)[0]

    @property
    def config_dir(self) -> Path:
        """The folder for the local machine's token and certificate."""
        return self._config_dir or state_dir() / "local-machine"

    @property
    def url(self) -> str:
        """The local machine's API address."""
        return f"https://127.0.0.1:{self.port}"

    # ------------------------------------------------------------------
    # ClusterProvider

    def machine_types(self) -> list[MachineType]:
        return [MachineType("local", "This computer (Docker)")]

    def price_per_hour(self, machine_type: str | None = None) -> float | None:
        return 0.0

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
        report("Checking Docker")
        self._check_daemon()
        self._check_port_free()
        # A container from an earlier start (stopped or not) goes first.
        self._run("rm", "--force", self.container_name, check=False)
        self._prepare_image(spec, report)

        machine_id = uuid.uuid4().hex[:12]
        token = new_token()
        cert_pem, key_pem = self_signed_certificate(("localhost",), ("127.0.0.1",))
        folder = self.config_dir
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
        os.chmod(folder, 0o700)
        write_private(folder / "token", token)
        write_private(folder / "cert.pem", cert_pem)
        write_private(folder / "key.pem", key_pem)
        write_private(folder / "env-spec.json", spec.to_json())

        handle = MachineHandle(
            provider=self.id,
            machine_id=machine_id,
            name=self.container_name,
            url=self.url,
            token=token,
            cert_pem=cert_pem,
            machine_type=machine_type or "local",
        )
        report("Starting the container")
        self._run(*self.run_args(handle, idle_timeout, max_lifetime))
        self._wait_ready(handle, report, timeout or DEFAULT_START_TIMEOUT)
        return handle

    def run_args(
        self,
        handle: MachineHandle,
        idle_timeout: float | None = DEFAULT_IDLE_TIMEOUT,
        max_lifetime: float | None = DEFAULT_MAX_LIFETIME,
    ) -> list[str]:
        """Arguments of ``docker`` that start the machine. No secrets: those
        are in files in the mounted config folder."""
        env = {
            "ORANGE3_CLOUD_CONFIG": "env",
            "ORANGE3_CLOUD_TOKEN_FILE": f"{CONFIG_MOUNT}/token",
            "ORANGE3_CLOUD_TLS_CERT_FILE": f"{CONFIG_MOUNT}/cert.pem",
            "ORANGE3_CLOUD_TLS_KEY_FILE": f"{CONFIG_MOUNT}/key.pem",
            "ORANGE3_CLOUD_ENV_SPEC_FILE": f"{CONFIG_MOUNT}/env-spec.json",
            "ORANGE3_CLOUD_IDLE_TIMEOUT": str(int(idle_timeout or 0)),
            "ORANGE3_CLOUD_MAX_LIFETIME": str(int(max_lifetime or 0)),
            "ORANGE3_CLOUD_MACHINE": "local",
        }
        args = [
            "run", "--detach", "--init",
            "--name", self.container_name,
            "--label", f"{LABEL}=true",
            "--label", f"{LABEL_MACHINE}={handle.machine_id}",
            "--label", f"{LABEL_CREATED}={handle.created_at}",
            "--publish", f"127.0.0.1:{self.port}:8443",
            "--volume", f"{self.config_dir}:{CONFIG_MOUNT}:ro",
            "--volume", f"{CACHE_VOLUME}:/var/cache/orange3-cloud",
        ]  # fmt: skip
        if self.source is not None:
            args += ["--volume", f"{self.source}:/src:ro"]
        for name, value in env.items():
            args += ["--env", f"{name}={value}"]
        args.append(self.image)
        return args

    def status(self, handle: MachineHandle | None = None) -> MachineStatus:
        container = self._inspect()
        if container is None:
            return MachineStatus(MachineState.NOT_FOUND)
        labels = container["Config"].get("Labels") or {}
        if handle is not None and labels.get(LABEL_MACHINE) != handle.machine_id:
            # The same name, but a machine from another start.
            return MachineStatus(MachineState.NOT_FOUND, "the machine was replaced")
        state = container["State"]
        status = state.get("Status")
        if status == "running":
            return MachineStatus(MachineState.RUNNING)
        if status in ("created", "restarting"):
            return MachineStatus(MachineState.STARTING)
        if status == "removing":
            return MachineStatus(MachineState.STOPPING)
        if status in ("exited", "paused"):
            code = state.get("ExitCode")
            reason = EXIT_REASONS.get(code, f"it exited with code {code}")
            return MachineStatus(MachineState.STOPPED, f"The machine stopped: {reason}.")
        return MachineStatus(MachineState.ERROR, state.get("Error") or str(status))

    def stop(self, handle: MachineHandle | None = None) -> None:
        if handle is not None and not self.status(handle).exists:
            return
        self._run(
            "stop", "--time", str(STOP_GRACE), self.container_name,
            check=False, timeout=STOP_GRACE + 30,
        )  # fmt: skip

    def delete(self, handle: MachineHandle | None = None) -> None:
        if handle is not None and not self.status(handle).exists:
            return
        self._run("rm", "--force", self.container_name, check=False, timeout=STOP_GRACE + 30)
        folder = self.config_dir
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)

    def list_machines(self) -> list[MachineHandle]:
        result = self._run(
            "ps", "--all", "--filter", f"label={LABEL}=true", "--format", "{{json .}}"
        )
        handles = []
        for line in result.stdout.splitlines():
            row = json.loads(line)
            labels = dict(
                item.split("=", 1) for item in row.get("Labels", "").split(",") if "=" in item
            )
            handles.append(
                MachineHandle(
                    provider=self.id,
                    machine_id=labels.get(LABEL_MACHINE, row.get("ID", "")),
                    name=row.get("Names", ""),
                    url=self.url,
                    token="",
                    cert_pem="",
                    machine_type="local",
                    created_at=labels.get(LABEL_CREATED, row.get("CreatedAt", "")),
                    extra={"state": row.get("State", "")},
                )
            )
        return handles

    # ------------------------------------------------------------------
    # Helpers

    def _check_port_free(self) -> None:
        """Only one local machine can publish the port."""
        result = self._run(
            "ps", "--filter", f"publish={self.port}", "--format", "{{.Names}}", check=False
        )
        others = [n for n in result.stdout.split() if n != self.container_name]
        if not others:
            return
        if any(n.startswith(COMPOSE_PROJECT + "-") for n in others):
            raise ProviderError(
                "The local machine started by scripts/local-machine.sh is running on "
                f"port {self.port}. Stop it first: scripts/local-machine.sh down"
            )
        raise ProviderError(
            f"Port {self.port} is used by the container {', '.join(others)}. Stop it first."
        )

    def _prepare_image(self, spec: EnvironmentSpec, report: StatusCallback) -> None:
        """Build, find or pull the image to run (see the module's docstring)."""
        if self.source is not None:
            report("Building the machine image")
            self._build_image()
            return
        if not self._image_override:
            version = addon_version(spec)
            self.image = f"{PUBLIC_IMAGE}:{version}" if is_release(version) else DEFAULT_IMAGE
        if self._run("image", "inspect", self.image, check=False).returncode == 0:
            return
        if self.image == DEFAULT_IMAGE:
            raise ProviderError(
                f"The machine image {self.image} isn't available. Build it from a "
                "source checkout of orange3-cloud (see docs/dev.md), or set "
                "ORANGE3_CLOUD_LOCAL_IMAGE to an image you have."
            )
        report("Downloading the machine image")
        result = self._run("pull", self.image, check=False, timeout=1800)
        if result.returncode != 0:
            raise ProviderError(_docker_error("pull", result))

    def _build_image(self) -> None:
        assert self.source is not None
        result = self._run(
            "build", "--quiet",
            "--file", str(self.source / "docker" / "Dockerfile"),
            "--tag", self.image,
            str(self.source),
            check=False, timeout=1800,
        )  # fmt: skip
        if result.returncode != 0:
            raise ProviderError(_docker_error("build", result))

    def _last_log_line(self) -> str | None:
        result = self._run("logs", "--tail", "5", self.container_name, check=False)
        lines = [ln for ln in (result.stdout + result.stderr).splitlines() if ln.strip()]
        for line in reversed(lines):
            if line.startswith("[bootstrap"):
                return line.split("]", 1)[-1].strip()
        return None

    def _tail_logs(self, lines: int = 30) -> str:
        result = self._run("logs", "--tail", str(lines), self.container_name, check=False)
        return (result.stdout + result.stderr).strip()

    def _wait_ready(self, handle: MachineHandle, report: StatusCallback, timeout: float) -> None:
        """Wait until ``/v1/info`` answers, reporting the bootstrap's progress."""
        deadline = time.monotonic() + timeout
        last_message = None
        error: CloudError | None = None
        with CloudClient(
            handle.url, handle.token, handle.cert_pem, timeout=5.0, retries=0
        ) as client:
            while True:
                try:
                    client.info()
                    return
                except CloudError as exc:  # not up yet: check the container, then retry
                    error = exc
                status = self.status(handle)
                if not status.alive:
                    logs = self._tail_logs()
                    self.delete(handle)
                    raise ProviderError(
                        f"The machine stopped while starting. {status.message}\n\n{logs}"
                    )
                if time.monotonic() > deadline:
                    logs = self._tail_logs()
                    self.delete(handle)
                    raise ProviderError(
                        f"The machine didn't answer within {timeout:.0f} s ({error}).\n\n{logs}"
                    )
                message = self._last_log_line()
                if message and message != last_message:
                    report(f"Starting: {message}")
                    last_message = message
                time.sleep(2)


def _docker_error(command: str, result: subprocess.CompletedProcess) -> str:
    text = (result.stderr or result.stdout).strip().splitlines()[-10:]
    return f"'docker {command}' failed: " + "\n".join(text)
