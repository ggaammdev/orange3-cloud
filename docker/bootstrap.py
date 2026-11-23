"""Turn the generic machine image into the user's environment, then start the server.

This is the image's entry point. It uses only the standard library, because it
runs before anything else is installed (on a small Python that the image
contains only for this script). Steps:

1. Read the configuration, from environment variables (local mode) or from the
   cloud the machine runs on (on Google Cloud, the GCE metadata server; on
   AWS and Azure, files that cloud-init wrote). Each source is a small
   :class:`Platform` (in :mod:`boot_platforms`, next to this script), which
   also knows how the machine stops.
2. Install the exact Python version of the user's EnvironmentSpec with
   ``uv python install`` and create a fresh virtual environment. If uv has no
   build of that version (e.g. 3.11.2), install the newest release of the same
   minor version instead (e.g. the newest 3.11); ``/v1/info`` shows both.
3. Install the **required** packages at the user's exact versions. If that
   fails, the machine doesn't start (exit code 1).
4. Install ``orange3-cloud[server]`` at the user's version, from the first of:
   the source folder mounted in local mode, a wheel bundled in a development
   image whose version matches, or the package index.
5. Install the **best-effort** packages, all at once and, if that fails, one
   at a time. The ones that can't be installed are passed to the server, which
   lists them in ``/v1/info``.
6. Run the API server, serving HTTPS on port 8443, as a child process, and
   pass SIGTERM and SIGINT on to it.
7. When the server has exited, on a cloud, stop the VM so it stops costing
   compute (the platform's :meth:`Platform.power_off`; on Google Cloud
   :meth:`GoogleCloud.power_off`). Locally the container just exits. The
   server stops by itself after the idle timeout or the maximum lifetime, and
   this script exits with the server's exit code: 0 after a shutdown request,
   3 after the idle timeout, 4 after the maximum lifetime, 128 + the signal
   number after SIGTERM or SIGINT, anything else after a crash.
   On a cloud a failed boot also stops the VM, after ``BOOT_FAILURE_WAIT``
   seconds, so that the error can be read meanwhile. With
   ``ORANGE3_CLOUD_POWER_OFF=host`` this script leaves powering off to whoever
   started the container (the add-on's VMs on Google Cloud and AWS power off
   when it exits, and those on Azure deallocate themselves).

uv's download cache, the Python installs and the virtual environment live in
the cache folder (a volume), so the next start installs from disk.

Configuration
-------------
Each setting is read from one platform (``boot_platforms.PLATFORMS``):

* ``env`` (:class:`Environment`): ``ORANGE3_CLOUD_<NAME>``, the value itself,
  or ``ORANGE3_CLOUD_<NAME>_FILE``, a file that contains the value,
* ``gce`` (:class:`GoogleCloud`): the GCE instance attribute
  ``orange3-cloud-<name>`` (custom metadata),
* ``aws`` (:class:`AmazonEC2`) and ``azure`` (:class:`AzureVM`): the file
  ``<name>`` (``token``, ``tls-cert``, ``tls-key``, ``env-spec``,
  ``idle-timeout``, ``max-lifetime``, ``machine``) in the folder
  ``ORANGE3_CLOUD_SETTINGS_DIR`` (``/run/orange3-cloud-settings``), which
  cloud-init wrote and the host mounted read-only.

=================  ==========================  ==========================
Setting            Environment variable        GCE metadata attribute
=================  ==========================  ==========================
token (required)   ``ORANGE3_CLOUD_TOKEN``     ``orange3-cloud-token``
TLS certificate    ``ORANGE3_CLOUD_TLS_CERT``  ``orange3-cloud-tls-cert``
TLS key            ``ORANGE3_CLOUD_TLS_KEY``   ``orange3-cloud-tls-key``
EnvironmentSpec    ``ORANGE3_CLOUD_ENV_SPEC``  ``orange3-cloud-env-spec``
idle timeout (s)   ``ORANGE3_CLOUD_IDLE_TIMEOUT``   ``orange3-cloud-idle-timeout``
max lifetime (s)   ``ORANGE3_CLOUD_MAX_LIFETIME``   ``orange3-cloud-max-lifetime``
machine name       ``ORANGE3_CLOUD_MACHINE``   the instance's machine type
=================  ==========================  ==========================

The certificate and key are PEM text; the EnvironmentSpec is JSON. Without a
certificate and key the server serves plain HTTP (only for tests).
``ORANGE3_CLOUD_CONFIG`` chooses the platform: its name, or ``auto`` (the
default: ``env`` if a token variable is set, else the first cloud whose
:meth:`Platform.detect` finds it, e.g. ``gce`` if the metadata server
answers). Another cloud is another small :class:`Platform` subclass in
``boot_platforms.PLATFORMS``.

Paths (environment variables with defaults): ``ORANGE3_CLOUD_CACHE_DIR``
(``/var/cache/orange3-cloud``), ``ORANGE3_CLOUD_SRC`` (``/src``),
``ORANGE3_CLOUD_WHEELS`` (``/opt/orange3-cloud/wheels``),
``ORANGE3_CLOUD_DATA_DIR`` (``/var/lib/orange3-cloud``); and
``ORANGE3_CLOUD_PORT`` (``8443``).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

# The platforms, next to this script (its folder is first on sys.path).
from boot_platforms import BootError, Environment, Platform, find_platform, log

#: This add-on's distribution name, PEP 503 normalized (its metadata spells
#: it ``Orange3-Cloud``).
SELF_NAME = "orange3-cloud"

CACHE_DIR = Path(os.environ.get("ORANGE3_CLOUD_CACHE_DIR", "/var/cache/orange3-cloud"))
SRC_DIR = Path(os.environ.get("ORANGE3_CLOUD_SRC", "/src"))
WHEELS_DIR = Path(os.environ.get("ORANGE3_CLOUD_WHEELS", "/opt/orange3-cloud/wheels"))
DATA_DIR = Path(os.environ.get("ORANGE3_CLOUD_DATA_DIR", "/var/lib/orange3-cloud"))
PORT = os.environ.get("ORANGE3_CLOUD_PORT", "8443")
VENV_DIR = CACHE_DIR / "venv"

#: Longest error text kept per failed package.
MAX_ERROR_CHARS = 2000

#: The server's exit codes when it stopped by itself (``orangecontrib.cloud.
#: server.__main__``; this script can't import the package).
EXIT_REASONS = {
    0: "it was asked to stop",
    3: "it was idle for longer than the idle timeout",
    4: "its maximum lifetime ended",
}

#: On a cloud, seconds between a failed boot and stopping the VM.
BOOT_FAILURE_WAIT = 600


def _seconds(name: str, platform: Platform) -> str | None:
    value = platform.read(name)
    if value is None or not value.strip():
        return None
    try:
        float(value)
    except ValueError as exc:
        raise BootError(f"{name} must be a number of seconds, not {value!r}") from exc
    return value.strip()


def read_config(platform: Platform) -> dict:
    log(f"reading the configuration from {platform.source}")
    token = (platform.read("token") or "").strip()
    if not token:
        raise BootError("the token is missing")
    spec_text = platform.read("env-spec")
    if not spec_text:
        raise BootError("the EnvironmentSpec (env-spec) is missing")
    try:
        spec = json.loads(spec_text)
    except ValueError as exc:
        raise BootError(f"the EnvironmentSpec isn't valid JSON: {exc}") from exc
    cert, key = platform.read("tls-cert"), platform.read("tls-key")
    if (cert is None) != (key is None):
        raise BootError("give both the TLS certificate and key, or neither")
    machine = platform.read("machine") or platform.machine_type()
    return {
        "token": token,
        "spec": spec,
        "tls_cert": cert,
        "tls_key": key,
        "idle_timeout": _seconds("idle-timeout", platform),
        "max_lifetime": _seconds("max-lifetime", platform),
        "machine": machine or ("local" if isinstance(platform, Environment) else None),
    }


# --------------------------------------------------------------------------
# Installing
# --------------------------------------------------------------------------


def uv(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run uv; output is captured and returned (and raised on failure if ``check``)."""
    result = subprocess.run(["uv", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise BootError(f"uv {' '.join(args[:2])} failed:\n{result.stderr.strip()}")
    return result


def pip_install(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return uv("pip", "install", "--python", str(VENV_DIR / "bin" / "python"), *args, check=check)


def _summary(result: subprocess.CompletedProcess) -> str:
    """uv's short summary lines (e.g. ``Installed 95 packages in 1.2s``)."""
    lines = [ln.strip() for ln in result.stderr.splitlines()]
    return "; ".join(ln for ln in lines if ln.split(" ", 1)[0] in ("Resolved", "Installed"))


def _short_error(text: str) -> str:
    text = text.strip()
    return text if len(text) <= MAX_ERROR_CHARS else "..." + text[-MAX_ERROR_CHARS:]


def _version_key(version: str) -> tuple[int, ...] | None:
    """``(3, 11, 15)`` for ``3.11.15``; None for pre-releases and odd strings."""
    parts = version.split(".")
    return tuple(int(p) for p in parts) if all(p.isdigit() for p in parts) else None


def newest_python_download(minor: str) -> str | None:
    """The newest CPython ``minor`` (e.g. ``3.11``) that uv can install, or None."""
    query = (f"cpython@{minor}", "--only-downloads", "--output-format", "json")
    result = uv("python", "list", *query, check=False)
    if result.returncode != 0:
        return None
    try:
        entries = json.loads(result.stdout)
    except ValueError:
        return None
    versions = [
        (key, entry["version"])
        for entry in entries
        if entry.get("implementation") == "cpython"
        and entry.get("variant", "default") == "default"
        and (key := _version_key(entry.get("version", ""))) is not None
        and key[:2] == _version_key(minor)
    ]
    return max(versions)[1] if versions else None


def install_python(spec: dict) -> str:
    """Install the user's Python and create the venv; return the installed version.

    That is the user's exact version, or, if uv has no build of it, the newest
    release of the same minor version: releases within a minor version only
    fix bugs, so pickles and package installs aren't affected.
    """
    impl = spec.get("python_implementation", "CPython")
    if impl != "CPython":
        raise BootError(f"only CPython is supported, not {impl}")
    requested = spec["python_version"]
    log(f"installing Python {requested}")
    result = uv("python", "install", requested, check=False)
    installed = requested
    if result.returncode != 0:
        minor = ".".join(requested.split(".")[:2])
        newest = newest_python_download(minor) if _version_key(minor) else None
        if newest is None or newest == requested:
            raise BootError(f"uv python install failed:\n{result.stderr.strip()}")
        log(
            f"uv has no build of Python {requested}; installing {newest}, "
            f"the newest {minor}, instead"
        )
        uv("python", "install", newest)
        installed = newest
    uv(
        "venv",
        "--clear",
        "--python",
        installed,
        "--python-preference",
        "only-managed",
        str(VENV_DIR),
    )
    return installed


def write_requirements(path: Path, packages: list[dict]) -> Path:
    path.write_text("".join(f"{p['name']}=={p['version']}\n" for p in packages))
    return path


def install_required(packages: list[dict], workdir: Path) -> Path:
    req = write_requirements(workdir / "required.txt", packages)
    log(f"installing {len(packages)} required packages")
    result = pip_install("-r", str(req), check=False)
    if result.returncode != 0:
        raise BootError(
            "the required packages couldn't be installed, so the machine can't copy your "
            f"environment:\n{result.stderr.strip()}"
        )
    log(f"required packages: {_summary(result)}")
    return req


def normalized(name: str) -> str:
    """A distribution name as PEP 503 normalizes it, e.g. ``orange3-cloud`` for
    ``Orange3-Cloud`` or ``orange3_cloud`` (this script has no ``packaging``)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _source_version(src: Path) -> str | None:
    import tomllib  # here, so the unit tests can import this file on Python 3.10

    try:
        project = tomllib.loads((src / "pyproject.toml").read_text())["project"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return None
    if normalized(str(project.get("name", ""))) != SELF_NAME:
        return None
    return project.get("version", "?")


def _copy_source(src: Path, dest: Path) -> Path:
    """Copy what the build needs; the mounted folder may be read-only."""
    dest.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        if (src / name).exists():
            shutil.copy2(src / name, dest / name)
    shutil.copytree(
        src / "orangecontrib",
        dest / "orangecontrib",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
    )
    return dest


def _bundled_wheel(version: str) -> Path | None:
    """This add-on's wheel of ``version`` in ``WHEELS_DIR``, whichever way its
    file name spells the name (``orange3_cloud-…``, ``Orange3_Cloud-…``)."""
    for wheel in sorted(WHEELS_DIR.glob("*.whl")):
        name, _, rest = wheel.name.partition("-")
        if normalized(name) == SELF_NAME and rest.split("-", 1)[0] == version:
            return wheel
    return None


def install_self(version: str, constraints: list[Path], workdir: Path) -> str:
    """Install ``orange3-cloud[server]``; return where it came from."""
    extra = ["--refresh-package", SELF_NAME, "--reinstall-package", SELF_NAME]
    src_version = _source_version(SRC_DIR)
    if src_version is not None:
        if src_version != version:
            log(f"note: the source folder has {SELF_NAME} {src_version}, the user has {version}")
        target = f"{SELF_NAME}[server] @ {_copy_source(SRC_DIR, workdir / 'src').as_uri()}"
        origin = f"the source folder {SRC_DIR} ({src_version})"
    elif (wheel := _bundled_wheel(version)) is not None:
        target = f"{SELF_NAME}[server] @ {wheel.as_uri()}"
        origin = f"the bundled wheel {wheel.name}"
    else:
        target = f"{SELF_NAME}[server]=={version}"
        extra = []
        origin = "the package index"
    log(f"installing {SELF_NAME}[server] {version} from {origin}")
    cons = [arg for c in constraints for arg in ("-c", str(c))]
    result = pip_install(target, *extra, *cons, check=False)
    if result.returncode != 0 and len(constraints) > 1:
        # The server's dependencies don't fit the user's best-effort versions:
        # the server wins, and those packages are reported later.
        log("retrying without the best-effort versions as constraints")
        result = pip_install(target, *extra, "-c", str(constraints[0]), check=False)
    if result.returncode != 0:
        raise BootError(
            f"{SELF_NAME}[server] {version} couldn't be installed:\n{result.stderr.strip()}"
        )
    log(f"{SELF_NAME}: {_summary(result)}")
    return origin


def install_best_effort(packages: list[dict], required: Path, workdir: Path) -> list[dict]:
    """Install what can be installed; return the failures."""
    if not packages:
        return []
    # Constraints: the required pins and everything installed so far, so a
    # best-effort package can't change the server's environment.
    frozen = uv("pip", "freeze", "--python", str(VENV_DIR / "bin" / "python")).stdout
    installed = workdir / "installed.txt"
    installed.write_text("".join(ln + "\n" for ln in frozen.splitlines() if "==" in ln))
    cons = ["-c", str(required), "-c", str(installed)]

    req = write_requirements(workdir / "best-effort.txt", packages)
    log(f"installing {len(packages)} best-effort packages")
    result = pip_install("-r", str(req), *cons, check=False)
    if result.returncode == 0:
        log(f"best-effort packages: {_summary(result)}")
        return []

    log("the batch failed; installing the best-effort packages one at a time")
    failures = []
    for package in packages:
        pin = f"{package['name']}=={package['version']}"
        result = pip_install(pin, *cons, check=False)
        if result.returncode != 0:
            log(f"  could not install {pin}")
            failures.append(
                {
                    "name": package["name"],
                    "version": package["version"],
                    "error": _short_error(result.stderr),
                }
            )
    log(f"best-effort packages: {len(packages) - len(failures)} installed, {len(failures)} failed")
    return failures


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def _write_private(path: Path, text: str) -> Path:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return path


def boot(config: dict) -> tuple[list[str], dict[str, str]]:
    """Install everything; return the server's command line and environment."""
    spec = config["spec"]
    workdir = Path(tempfile.mkdtemp(prefix="orange3-cloud-boot-"))  # mode 0700

    required = [p for p in spec.get("required", []) if normalized(p["name"]) != SELF_NAME]
    best_effort = [p for p in spec.get("best_effort", []) if normalized(p["name"]) != SELF_NAME]
    self_pkg = next(
        (p for p in spec.get("required", []) if normalized(p["name"]) == SELF_NAME), None
    )
    if self_pkg is None:
        raise BootError(f"the EnvironmentSpec doesn't list {SELF_NAME}")

    install_python(spec)
    required_txt = install_required(required, workdir)
    best_txt = write_requirements(workdir / "best-effort-constraints.txt", best_effort)
    install_self(self_pkg["version"], [required_txt, best_txt], workdir)
    failures = install_best_effort(best_effort, required_txt, workdir)

    failures_file = workdir / "install-failures.json"
    failures_file.write_text(json.dumps(failures, indent=1))
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    python = str(VENV_DIR / "bin" / "python")
    argv = [
        python,
        "-m",
        "orangecontrib.cloud.server",
        "--host",
        "0.0.0.0",
        "--port",
        PORT,
        "--data-dir",
        str(DATA_DIR),
        "--install-failures",
        str(failures_file),
    ]
    if config["tls_cert"] is not None:
        cert = _write_private(workdir / "cert.pem", config["tls_cert"])
        key = _write_private(workdir / "key.pem", config["tls_key"])
        argv += ["--tls-cert", str(cert), "--tls-key", str(key)]
    if config["machine"]:
        argv += ["--machine", config["machine"]]
    if config["idle_timeout"]:
        argv += ["--idle-timeout", config["idle_timeout"]]
    if config["max_lifetime"]:
        argv += ["--max-lifetime", config["max_lifetime"]]

    env = {k: v for k, v in os.environ.items() if not k.startswith("ORANGE3_CLOUD_")}
    env["ORANGE3_CLOUD_TOKEN"] = config["token"]  # not on the command line
    # For /v1/info, next to the installed version. An environment variable,
    # not a flag, so that servers that don't know it still start.
    env["ORANGE3_CLOUD_PYTHON_REQUESTED"] = spec["python_version"]
    env["VIRTUAL_ENV"] = str(VENV_DIR)
    env["PATH"] = f"{VENV_DIR / 'bin'}:{env.get('PATH', '')}"
    return argv, env


#: Signals passed on to the server.
_forwarded: set[int] = set()


def run_server(argv: list[str], env: dict[str, str]) -> int:
    """Run the server until it exits, passing SIGTERM and SIGINT on to it."""
    # Its own process group: a Ctrl-C in a terminal reaches it only once,
    # through us (a second one would make uvicorn stop without cleaning up).
    server = subprocess.Popen(argv, env=env, process_group=0)

    def forward(signum, frame):
        log(f"passing {signal.Signals(signum).name} on to the server")
        _forwarded.add(signum)
        server.send_signal(signum)

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, forward)
    return server.wait()


def main() -> int:
    platform = None
    try:
        platform = find_platform()
        argv, env = boot(read_config(platform))
    except Exception as exc:  # noqa: BLE001 - the boot's boundary: logged, the VM powers off
        log(f"ERROR: {exc}" if isinstance(exc, BootError) else traceback.format_exc())
        log("the machine did not start")
        if platform is not None and platform.stops_machine:
            log(f"stopping the VM in {BOOT_FAILURE_WAIT} s")
            time.sleep(BOOT_FAILURE_WAIT)
            platform.power_off()
        return 1
    log(f"starting the server on port {PORT}")
    code = run_server(argv, env)
    if code < 0:  # ended by a signal
        name = signal.Signals(-code).name
        if -code in _forwarded:  # uvicorn re-raises it after stopping cleanly
            log(f"the server stopped after {name}")
        else:
            log(f"ERROR: the server was killed by {name}")
        code = 128 - code
    elif code in EXIT_REASONS:
        log(f"the server stopped because {EXIT_REASONS[code]} (exit code {code})")
    else:
        log(f"ERROR: the server stopped unexpectedly (exit code {code})")
    if platform.stops_machine:  # never locally
        platform.power_off()
    return code


if __name__ == "__main__":
    sys.exit(main())
