"""CloudSession with a fake provider that runs the server in-process over
HTTPS, and LocalDockerProvider's ``docker run`` arguments.

The Docker test at the end starts the real local machine; it runs only with
``ORANGE3_CLOUD_DOCKER_TESTS=1`` (not in CI).
"""

import os
import stat
import subprocess
import threading
import time

import pytest
from AnyQt.QtWidgets import QApplication
from conftest import SPEC

from orangecontrib.cloud.client.providers.base import (
    MachineHandle,
    MachineState,
    ProviderError,
)
from orangecontrib.cloud.client.providers.local_docker import LocalDockerProvider
from orangecontrib.cloud.client.session import CloudSession, HandleStore, SessionState
from orangecontrib.cloud.env import Package
from orangecontrib.cloud.protocol.machine import InfoResponse
from orangecontrib.cloud.protocol.wire import API_VERSION


def make_session(fake, tmp_path, **kwargs):
    return CloudSession(
        fake,
        store=HandleStore(tmp_path / "state" / "machine.json"),
        resolve_provider=lambda provider_id: fake,
        **kwargs,
    )


def record(session):
    events = []
    session.statusChanged.connect(lambda state: events.append(("status", state)))
    session.connected.connect(lambda: events.append(("connected",)))
    session.disconnected.connect(lambda: events.append(("disconnected",)))
    session.error.connect(lambda message: events.append(("error", message)))
    session.infoChanged.connect(lambda info: events.append(("info", info.api_version)))
    return events


def test_connect_info_reattach_and_machine_gone(fake, tmp_path):
    session = make_session(fake, tmp_path)
    events = record(session)

    handle = session.start_machine(spec=SPEC, wait=True)
    assert session.is_connected and session.state is SessionState.CONNECTED
    assert session.info().api_version == API_VERSION
    assert ("status", "starting") in events and ("connected",) in events
    assert ("info", API_VERSION) in events

    # The handle with its secrets is persisted privately, for reattaching.
    path = session.store.path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert handle.token in path.read_text()
    assert handle.token not in repr(handle)

    # "Orange restarts": a new session finds the machine and reconnects.
    again = make_session(fake, tmp_path)
    assert again.reattach(wait=True).machine_id == handle.machine_id
    assert again.info().api_version == API_VERSION
    again.disconnect(wait=True)
    assert again.state is SessionState.DISCONNECTED and session.store.load() is not None

    # The machine goes away (e.g. idle timeout): the heartbeat notices.
    fake.stop()
    assert session.check_connection() is False
    assert session.state is SessionState.DISCONNECTED
    assert ("disconnected",) in events and events[-1][0] == "error"
    assert session.store.load() is None
    # Nothing to reattach to now.
    assert make_session(fake, tmp_path).reattach(wait=True) is None


def test_background_calls_and_heartbeat_in_qt(fake, tmp_path):
    app = QApplication.instance() or QApplication([])
    session = make_session(fake, tmp_path, heartbeat_interval=0.1)
    events = record(session)
    threads = []
    session.connected.connect(lambda: threads.append(threading.current_thread()))

    def spin(condition, timeout=30):
        deadline = time.monotonic() + timeout
        while not condition():
            assert time.monotonic() < deadline
            app.processEvents()
            time.sleep(0.01)

    future = session.start_machine(spec=SPEC)
    spin(future.done)
    spin(lambda: ("connected",) in events)
    # Signals from the background thread arrive in the GUI thread.
    assert threads == [threading.main_thread()]
    # The heartbeat refreshes the info.
    spin(lambda: sum(e[0] == "info" for e in events) >= 3)

    future = session.stop_machine()
    spin(future.done)
    future.result()
    spin(lambda: ("disconnected",) in events)
    assert session.state is SessionState.DISCONNECTED
    assert fake.status().state is MachineState.NOT_FOUND
    assert session.store.load() is None
    session.shutdown()


def test_sign_in_on_its_own_thread(fake, tmp_path, monkeypatch):
    """The provider's sign-in runs on a thread of its own (machine operations
    don't wait for it); ``signInFinished`` reports its end in the GUI thread,
    an unexpected error as a ProviderError."""
    app = QApplication.instance() or QApplication([])
    session = make_session(fake, tmp_path)
    results = []
    session.signInFinished.connect(
        lambda error: results.append((error, threading.current_thread()))
    )
    release = threading.Event()

    def sign_in(cancel):
        release.wait(10)
        raise RuntimeError("no browser")

    monkeypatch.setattr(fake, "sign_in", sign_in)
    future = session.sign_in("fake")
    assert session.sign_in("fake") is future and session.signing_in
    assert session.reattach().result(timeout=10) is None  # not queued behind it
    release.set()
    deadline = time.monotonic() + 10
    while not results:
        assert time.monotonic() < deadline
        app.processEvents()
        time.sleep(0.01)
    ((error, thread),) = results
    assert isinstance(error, ProviderError) and "no browser" in str(error)
    assert thread is threading.main_thread() and not session.signing_in
    session.shutdown()


def test_local_docker_run_args(tmp_path):
    provider = LocalDockerProvider(source=tmp_path, config_dir=tmp_path / "cfg", docker="docker")
    handle = MachineHandle("local", "abc123", "orange3-cloud-local", provider.url, "s3cret", "x")
    args = provider.run_args(handle, idle_timeout=60, max_lifetime=None)
    assert args[:3] == ["run", "--detach", "--init"]
    assert "127.0.0.1:8443:8443" in args
    assert "orange3-cloud=true" in args and "orange3-cloud-machine=abc123" in args
    assert f"{tmp_path}:/src:ro" in args
    assert "ORANGE3_CLOUD_IDLE_TIMEOUT=60" in args and "ORANGE3_CLOUD_MAX_LIFETIME=0" in args
    assert "s3cret" not in " ".join(args)
    assert provider.price_per_hour() == 0.0


@pytest.mark.skipif(
    os.environ.get("ORANGE3_CLOUD_DOCKER_TESTS") != "1",
    reason="starts the local machine in Docker; set ORANGE3_CLOUD_DOCKER_TESTS=1",
)
def test_local_docker_machine(tmp_path, monkeypatch):
    monkeypatch.setenv("ORANGE3_CLOUD_STATE_DIR", str(tmp_path))
    session = CloudSession(LocalDockerProvider())
    try:
        session.start_machine(idle_timeout=300, wait=True)
        info = session.info()
        assert info.machine == "local" and info.api_version == API_VERSION
    finally:
        session.stop_machine(wait=True)
    assert session.provider.status().state is MachineState.NOT_FOUND


def test_stop_reason_from_the_last_info():
    """A machine that stopped without saying why (a powered-off GCP VM) is
    explained by its last /v1/info: its idle timeout or lifetime was due."""
    from orangecontrib.cloud.client.session import _explain_stop

    def info(idle_left, lifetime_left, jobs=0):
        return InfoResponse.model_construct(
            idle_time_left_seconds=idle_left,
            lifetime_left_seconds=lifetime_left,
            idle_timeout_seconds=1800.0,
            jobs_running=jobs,
        )

    stopped = "The machine stopped."
    assert "idle" in _explain_stop(stopped, info(12.0, 4000.0), 60)
    assert "lifetime" in _explain_stop(stopped, info(None, 20.0), 60)
    assert _explain_stop(stopped, info(500.0, 4000.0), 60) == stopped
    assert _explain_stop(stopped, None, 60) == stopped
    known = "The machine stopped: it was asked to stop."
    assert _explain_stop(known, info(12.0, None), 60) == known
    # Asleep for two hours (the window is the time since the last info):
    # idle, or with a job running, out of touch for the idle timeout twice.
    assert "idle" in _explain_stop(stopped, info(1700.0, 40000.0), 7260)
    assert _explain_stop(stopped, info(None, 40000.0, jobs=1), 7260) == (
        "The machine stopped: Orange hadn't been in touch for longer than its idle timeout."
    )
    assert _explain_stop(stopped, info(None, 40000.0, jobs=1), 3000) == stopped
    assert "lifetime" in _explain_stop(stopped, info(None, 3000.0, jobs=1), 7260)


def test_local_docker_image_of_installed_addon(tmp_path, monkeypatch):
    """An installed release pulls its public image; a dev version needs a local one."""
    monkeypatch.delenv("ORANGE3_CLOUD_LOCAL_IMAGE", raising=False)
    provider = LocalDockerProvider(source=False, config_dir=tmp_path / "cfg", docker="docker")
    calls = []

    def fake_run(*args, check=True, timeout=120):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1 if args[0] == "image" else 0, "", "")

    monkeypatch.setattr(provider, "_run", fake_run)
    release = SPEC.model_copy(update={"required": [Package(name="orange3-cloud", version="1.0.0")]})
    provider._prepare_image(release, lambda message: None)
    assert provider.image == "ghcr.io/ggaammdev/orange3-cloud-machine:1.0.0"
    assert calls[-1] == ("pull", provider.image)

    dev = SPEC.model_copy(update={"required": [Package(name="orange3-cloud", version="1.1.dev0")]})
    with pytest.raises(ProviderError, match="isn't available"):
        provider._prepare_image(dev, lambda message: None)
