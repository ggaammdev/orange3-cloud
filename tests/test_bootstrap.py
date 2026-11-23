"""The machine's bootstrap picks the right Python, with uv's calls mocked (no Docker)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

BOOTSTRAP = Path(__file__).parents[1] / "docker" / "bootstrap.py"

#: What ``uv python list cpython@3.11 --only-downloads --output-format json`` lists.
DOWNLOADS_311 = [
    {"version": "3.11.15", "implementation": "cpython", "variant": "default"},
    {"version": "3.11.15", "implementation": "cpython", "variant": "debug"},
    {"version": "3.11.16", "implementation": "pypy", "variant": "default"},
    {"version": "3.11.9", "implementation": "cpython", "variant": "default"},
]


@pytest.fixture
def bootstrap(monkeypatch):
    """``docker/bootstrap.py``, imported as the image runs it: with its folder
    first on ``sys.path`` (for ``boot_platforms``)."""
    monkeypatch.syspath_prepend(str(BOOTSTRAP.parent))
    monkeypatch.delitem(sys.modules, "boot_platforms", raising=False)
    spec = importlib.util.spec_from_file_location("orange3_cloud_bootstrap", BOOTSTRAP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def platforms(bootstrap):
    """``docker/boot_platforms.py``, the module that ``bootstrap`` imported."""
    return sys.modules["boot_platforms"]


def fake_uv(bootstrap, available: set[str]):
    """A stand-in for ``bootstrap.uv`` whose ``python install`` knows only
    ``available``; returns it and the list of calls."""
    calls = []

    def uv(*args, check=True):
        calls.append(args)
        code, out, err = 0, "", ""
        if args[:2] == ("python", "install") and args[2] not in available:
            code, err = 2, f"error: No download found for request: cpython-{args[2]}"
        elif args[:2] == ("python", "list"):
            out = json.dumps(DOWNLOADS_311 if args[2] == "cpython@3.11" else [])
        if check and code:
            raise bootstrap.BootError(err)
        return subprocess.CompletedProcess(["uv", *args], code, out, err)

    return uv, calls


def test_missing_python_build_installs_newest_of_minor(bootstrap, monkeypatch):
    uv, calls = fake_uv(bootstrap, available={"3.11.9", "3.11.15"})
    monkeypatch.setattr(bootstrap, "uv", uv)

    assert bootstrap.install_python({"python_version": "3.11.2"}) == "3.11.15"
    assert ("python", "install", "3.11.2") in calls
    assert ("python", "install", "3.11.15") in calls
    venv = next(c for c in calls if c[0] == "venv")
    assert venv[venv.index("--python") + 1] == "3.11.15"


def test_exact_python_build_is_used(bootstrap, monkeypatch):
    uv, calls = fake_uv(bootstrap, available={"3.11.9", "3.11.15"})
    monkeypatch.setattr(bootstrap, "uv", uv)

    assert bootstrap.install_python({"python_version": "3.11.9"}) == "3.11.9"
    assert not any(c[:2] == ("python", "list") for c in calls)


def test_platforms(bootstrap, platforms, monkeypatch, tmp_path):
    """The settings come from one platform: environment variables locally, a
    cloud's own source on a cloud. Another cloud is one more Platform."""
    for name in ("CONFIG", "TOKEN", "TOKEN_FILE", "ENV_SPEC", "MACHINE"):
        monkeypatch.delenv(f"ORANGE3_CLOUD_{name}", raising=False)
    spec = json.dumps({"python_version": "3.12.0", "required": []})

    class Elsewhere(platforms.Platform):
        name = "elsewhere"
        source = "a fake cloud"
        stops_machine = True

        def detect(self):
            return True

        def read(self, name):
            return {"token": "t", "env-spec": spec, "idle-timeout": "60"}.get(name)

        def machine_type(self):
            return "big-1"

    monkeypatch.setattr(platforms, "PLATFORMS", (platforms.Environment(), Elsewhere()))
    platform = bootstrap.find_platform()  # auto: no token variable, so the cloud
    assert platform.name == "elsewhere" and platform.stops_machine
    config = bootstrap.read_config(platform)
    assert (config["token"], config["machine"], config["idle_timeout"]) == ("t", "big-1", "60")
    assert config["tls_cert"] is None and config["max_lifetime"] is None

    (tmp_path / "token").write_text("secret\n")
    monkeypatch.setenv("ORANGE3_CLOUD_TOKEN_FILE", str(tmp_path / "token"))
    monkeypatch.setenv("ORANGE3_CLOUD_ENV_SPEC", spec)
    platform = bootstrap.find_platform()
    assert isinstance(platform, bootstrap.Environment) and not platform.stops_machine
    config = bootstrap.read_config(platform)
    assert (config["token"], config["machine"]) == ("secret", "local")

    monkeypatch.setenv("ORANGE3_CLOUD_CONFIG", "elsewhere")
    assert bootstrap.find_platform().name == "elsewhere"
    monkeypatch.setenv("ORANGE3_CLOUD_CONFIG", "nowhere")
    with pytest.raises(bootstrap.BootError, match="one of auto, env, elsewhere"):
        bootstrap.find_platform()


def test_aws_platform_reads_settings_files(bootstrap, monkeypatch, tmp_path):
    """On AWS the settings are files that cloud-init wrote, one per setting,
    and the host powers the VM off when the container exits."""
    for name in ("TOKEN", "TOKEN_FILE", "ENV_SPEC", "MACHINE"):
        monkeypatch.delenv(f"ORANGE3_CLOUD_{name}", raising=False)
    monkeypatch.setenv("ORANGE3_CLOUD_CONFIG", "aws")
    monkeypatch.setenv("ORANGE3_CLOUD_SETTINGS_DIR", str(tmp_path))
    monkeypatch.setenv("ORANGE3_CLOUD_POWER_OFF", "host")
    (tmp_path / "token").write_text("secret")
    (tmp_path / "env-spec").write_text(json.dumps({"python_version": "3.12.0", "required": []}))
    (tmp_path / "machine").write_text("m7i.xlarge")
    (tmp_path / "idle-timeout").write_text("1800")
    platform = bootstrap.find_platform()
    assert platform.name == "aws" and platform.stops_machine
    config = bootstrap.read_config(platform)
    assert (config["token"], config["machine"], config["idle_timeout"]) == (
        "secret",
        "m7i.xlarge",
        "1800",
    )
    assert config["tls_cert"] is None and config["max_lifetime"] is None
    assert platform.power_off()  # left to the host's systemd unit


def test_azure_platform_deallocates(bootstrap, platforms, monkeypatch, tmp_path):
    """On Azure the settings are files too. The host's unit deallocates the
    VM; without it, the machine deallocates its own VM with its managed
    identity through Resource Manager."""
    monkeypatch.setenv("ORANGE3_CLOUD_CONFIG", "azure")
    monkeypatch.setenv("ORANGE3_CLOUD_SETTINGS_DIR", str(tmp_path))
    monkeypatch.setenv("ORANGE3_CLOUD_POWER_OFF", "host")
    (tmp_path / "token").write_text("secret")
    (tmp_path / "machine").write_text("Standard_D4s_v6")
    platform = bootstrap.find_platform()
    assert platform.name == "azure" and platform.stops_machine
    assert platform.read("machine") == "Standard_D4s_v6"
    posts = []
    monkeypatch.setattr(platforms, "_post", lambda url, token: posts.append((url, token)))
    assert platform.power_off() and posts == []  # left to the host's systemd unit

    vm = "/subscriptions/s/resourceGroups/g/providers/Microsoft.Compute/virtualMachines/v"
    metadata = {
        "identity/oauth2/token": json.dumps({"access_token": "identity-token"}),
        "instance/compute/resourceId": vm,
    }
    monkeypatch.setattr(
        platforms, "_azure_metadata", lambda path, timeout=5: metadata[path.split("?")[0]]
    )
    monkeypatch.delenv("ORANGE3_CLOUD_POWER_OFF")
    assert platform.power_off()
    assert posts == [
        (f"https://management.azure.com{vm}/deallocate?api-version=2024-07-01", "identity-token")
    ]
