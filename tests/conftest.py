"""Shared test helpers: an in-process HTTPS server, a fake machine provider,
and settings and state folders that keep the user's own out of the tests."""

import contextlib
import socket
import threading
import time
import uuid

import pytest
import uvicorn

from orangecontrib.cloud.client.providers.base import (
    ClusterProvider,
    MachineHandle,
    MachineState,
    MachineStatus,
    MachineType,
    new_token,
    self_signed_certificate,
)
from orangecontrib.cloud.env import EnvironmentSpec
from orangecontrib.cloud.server.app import ServerConfig, create_app

SPEC = EnvironmentSpec(python_version="3.12.0", platform="darwin", machine="arm64")


@pytest.fixture(autouse=True)
def _private_settings(tmp_path, monkeypatch):
    """Never read or write the user's Orange settings or machine handle."""
    monkeypatch.setenv("ORANGE3_CLOUD_SETTINGS_FILE", str(tmp_path / "settings.ini"))
    monkeypatch.setenv("ORANGE3_CLOUD_STATE_DIR", str(tmp_path / "state"))


@contextlib.contextmanager
def https_server(app, folder, cert_key=None, port=0):
    """Serve ``app`` with uvicorn in a thread over HTTPS on ``port`` (default
    a random one) of 127.0.0.1, with ``cert_key`` (PEM texts) or a fresh
    self-signed certificate for localhost and 127.0.0.1. Yields (base URL,
    certificate PEM, uvicorn server)."""
    cert, key = cert_key or self_signed_certificate()
    (folder / "cert.pem").write_text(cert)
    (folder / "key.pem").write_text(key)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            ssl_certfile=str(folder / "cert.pem"),
            ssl_keyfile=str(folder / "key.pem"),
            log_level="warning",
            # Don't wait for a client's open keep-alive connection, whose TLS
            # shutdown would take 30 s.
            timeout_graceful_shutdown=1,
        )
    )
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    port = sock.getsockname()[1]
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 60
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "the server didn't start"
        time.sleep(0.05)
    try:
        yield f"https://127.0.0.1:{port}", cert, server
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        sock.close()
        assert not thread.is_alive(), "the server didn't stop"


class CuttableProxy:
    """A TCP proxy to a server on 127.0.0.1. Within :meth:`cut`, it acts like
    a network that drops packets: it passes nothing on and holds every
    connection, old and new, open without an answer."""

    def __init__(self, target_url):
        self._target = ("127.0.0.1", int(target_url.rsplit(":", 1)[1]))
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.url = f"https://127.0.0.1:{self._listener.getsockname()[1]}"
        self._cut = threading.Event()
        self._sockets = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:  # closed
                return
            self._sockets.append(conn)
            if self._cut.is_set():
                continue  # never answered
            upstream = socket.create_connection(self._target)
            self._sockets.append(upstream)
            for source, sink in ((conn, upstream), (upstream, conn)):
                threading.Thread(target=self._pipe, args=(source, sink), daemon=True).start()

    def _pipe(self, source, sink):
        with contextlib.suppress(OSError):  # a connection dropped
            while data := source.recv(65536):
                if not self._cut.is_set():
                    sink.sendall(data)
        with contextlib.suppress(OSError):  # pass the end on
            sink.shutdown(socket.SHUT_WR)

    @contextlib.contextmanager
    def cut(self):
        """Within the block, pass nothing on; afterwards, start afresh."""
        self._cut.set()
        try:
            yield
        finally:
            self.drop_connections()
            self._cut.clear()

    def drop_connections(self):
        """Close every connection, which the client notices at once."""
        sockets, self._sockets = self._sockets, []
        for sock in sockets:
            with contextlib.suppress(OSError):  # already closed by the other end
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()

    def close(self):
        """Stop accepting connections and close them all."""
        self._listener.close()
        self.drop_connections()


class FakeProvider(ClusterProvider):
    """Runs the API server (without the job system) in a thread."""

    id = "fake"
    label = "Fake"

    def __init__(self, folder):
        self.folder = folder
        self.machine_id = None
        self.start_kwargs = None
        self._stack = None

    def start(self, spec, machine_type=None, *, on_status=None, **kwargs):
        self.start_kwargs = kwargs
        on_status and on_status("Starting the fake machine")
        token = new_token()
        self.machine_id = uuid.uuid4().hex
        # data_dir doesn't exist yet: /v1/info must still work.
        config = ServerConfig(token=token, data_dir=self.folder / "data" / self.machine_id)
        self._stack = contextlib.ExitStack()
        url, cert, _ = self._stack.enter_context(https_server(create_app(config), self.folder))
        return MachineHandle(self.id, self.machine_id, "fake-1", url, token, cert)

    def status(self, handle=None):
        if self._stack is None or (handle and handle.machine_id != self.machine_id):
            return MachineStatus(MachineState.NOT_FOUND, "The fake machine is gone.")
        return MachineStatus(MachineState.RUNNING)

    def stop(self, handle=None):
        if self._stack is not None:
            self._stack.close()
            self._stack = None

    delete = stop

    def list_machines(self):
        return []

    def machine_types(self):
        return [MachineType("fake", "Fake")]

    def price_per_hour(self, machine_type=None):
        return 0.0


@pytest.fixture
def fake(tmp_path):
    provider = FakeProvider(tmp_path)
    yield provider
    provider.stop()
