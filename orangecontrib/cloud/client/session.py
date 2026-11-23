"""The cloud session: the one connection between Orange and its machine.

:class:`CloudSession` is a single shared object (:meth:`CloudSession.instance`)
that holds the machine provider, the handle of the running machine and the
:class:`~orangecontrib.cloud.client.api.CloudClient` connected to it. Widgets
and the settings dialog use it; they never talk to providers.

* **Threads.** Slow calls (starting, connecting, stopping) run on one
  background thread, one after the other. They return a
  ``concurrent.futures.Future`` and report through Qt signals, which Qt
  delivers to receivers in the GUI thread. With ``wait=True`` they run in the
  calling thread and return the result, which also works in a plain Python
  shell without a Qt event loop.
* **Heartbeat.** While connected, and when a Qt application exists, a timer
  calls ``GET /v1/info`` every 30 s (:meth:`check_connection`). It doesn't
  count as activity, so the machine still stops after its idle timeout; the
  heartbeat then notices that the machine is gone. It does tell the machine
  that Orange is still there: a machine that hears nothing from Orange for
  its idle timeout (Orange crashed, or the laptop slept) cancels its jobs.
  After a network failure the session reconnects with backoff (state
  ``reconnecting``).
* **Settings.** Unless given explicitly, the provider and the time limits of
  a new machine come from the user's settings (:class:`~orangecontrib.cloud.
  settings.CloudSettings`, chosen in the Cloud settings dialog).
* **Costs.** :meth:`record_jobs` records the cost of finished jobs (runtime
  times the machine's hourly price), and deleting a machine records the cost
  of its whole session, in :class:`~orangecontrib.cloud.client.costs.CostLog`:
  on Stop, when it stopped by itself (seen now, or when Orange opens again),
  and leftovers deleted from the Cloud settings. A machine that stopped by
  itself costs its full price until it stopped, then its stopped price
  (:func:`~orangecontrib.cloud.client.costs.session_entry`).
  :meth:`session_cost` is the current machine's cost so far.
* **Reattach.** The handle of the running machine, with its token and
  certificate, is kept in ``machine.json`` in the add-on's private folder
  (mode ``0600``; never in workflows). :meth:`reattach` (called once by
  :meth:`instance`) reconnects to that machine after Orange restarts, if it
  still runs.

Signals: ``statusChanged(state)`` (a :class:`SessionState` value),
``messageChanged(text)`` (progress, for the user), ``connected()``,
``disconnected()``, ``error(text)``, ``infoChanged(InfoResponse)`` and
``signInFinished(error)`` (:meth:`CloudSession.sign_in`: signing in to a
provider's account, e.g. Google's, which runs the provider's own sign-in on
a thread of its own).
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from AnyQt.QtCore import QCoreApplication, QObject, QTimer
from AnyQt.QtCore import pyqtSignal as Signal
from AnyQt.QtCore import pyqtSlot as Slot

from orangecontrib.cloud.client.api import (
    CloudAuthError,
    CloudClient,
    CloudConnectionError,
    CloudError,
)
from orangecontrib.cloud.client.costs import CostLog, SessionCosts, cost_of, machine_seconds
from orangecontrib.cloud.client.providers.base import (
    ClusterProvider,
    MachineHandle,
    ProviderError,
    state_dir,
    write_private,
)
from orangecontrib.cloud.client.providers.registry import get_provider
from orangecontrib.cloud.env import EnvironmentSpec, collect_environment
from orangecontrib.cloud.protocol.jobs import JobResult
from orangecontrib.cloud.protocol.machine import InfoResponse
from orangecontrib.cloud.settings import CloudSettings

__all__ = ["FROM_SETTINGS", "CloudSession", "HandleStore", "SessionState"]

log = logging.getLogger(__name__)

T = TypeVar("T")

#: Default of :meth:`CloudSession.start_machine`'s time limits: use the
#: user's settings.
FROM_SETTINGS: Any = object()


class SessionState(str, Enum):
    """The state of the session's connection to a machine."""

    DISCONNECTED = "disconnected"
    STARTING = "starting"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    STOPPING = "stopping"
    ERROR = "error"


class HandleStore:
    """The persisted handle of the current machine (a ``0600`` JSON file).

    A file that can't be read doesn't stop the add-on: :meth:`load` returns
    ``None`` and :attr:`load_error` says why, for the Cloud settings dialog
    (the machine it pointed to may still run, as a leftover).
    """

    FILENAME = "machine.json"

    def __init__(self, path: Path | None = None):
        self._path = Path(path) if path is not None else None
        #: Why the last :meth:`load` couldn't read the file (``None``: it could,
        #: or there is none).
        self.load_error: str | None = None

    @property
    def path(self) -> Path:
        """The file that keeps the handle."""
        return self._path or state_dir() / self.FILENAME

    def load(self) -> MachineHandle | None:
        """The saved handle; ``None`` if there is none or it can't be read."""
        try:
            handle = MachineHandle.from_dict(json.loads(self.path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            handle = None
        except (OSError, ValueError, TypeError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            if error != self.load_error:  # once, not at every look
                log.warning("Ignoring the unreadable machine handle %s: %s", self.path, error)
            self.load_error = error
            return None
        self.load_error = None
        return handle

    def save(self, handle: MachineHandle) -> None:
        """Save the handle (readable only by the user)."""
        write_private(self.path, json.dumps(handle.to_dict(), indent=2))
        self.load_error = None

    def clear(self) -> None:
        """Delete the saved handle."""
        self.path.unlink(missing_ok=True)
        self.load_error = None


class CloudSession(QObject):
    """Holds the provider, the machine and the connection to it."""

    statusChanged = Signal(str)
    messageChanged = Signal(str)
    connected = Signal()
    disconnected = Signal()
    error = Signal(str)
    infoChanged = Signal(object)
    #: :meth:`sign_in` ended: ``None``, or the ``ProviderError`` (a
    #: ``SignInCancelledError`` after :meth:`cancel_sign_in`).
    signInFinished = Signal(object)

    # Internal: (re)starts or stops the heartbeat in the session's thread.
    _connectionChanged = Signal(bool)

    #: Seconds between heartbeats.
    HEARTBEAT_INTERVAL = 30.0
    #: Seconds between reconnection attempts after a network failure.
    RECONNECT_DELAYS = (1, 2, 5, 10, 20, 30, 30, 30, 30, 30)

    _instance: CloudSession | None = None
    _instance_lock = threading.Lock()

    @classmethod
    def instance(cls) -> CloudSession:
        """The shared session. On first use it reattaches, in the background,
        to a machine that was running when Orange last closed."""
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
                if cls._instance.store.load() is not None:
                    cls._instance.reattach()
            return cls._instance

    def __init__(
        self,
        provider: ClusterProvider | None = None,
        *,
        store: HandleStore | None = None,
        resolve_provider: Callable[[str], ClusterProvider] = get_provider,
        heartbeat_interval: float | None = None,
        reconnect_delays: tuple[float, ...] | None = None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._provider = provider  # fixed by the caller; else from the settings
        self._providers: dict[str, ClusterProvider] = {}
        self.store = store or HandleStore()
        #: Where job and session costs are recorded (next to the handle).
        self.costs = CostLog(lambda: self.store.path.parent)
        #: The machines' prices, and their costs recorded in :attr:`costs`.
        self.machine_costs = SessionCosts(self.costs, self._provider_for)
        self._resolve_provider = resolve_provider
        self.heartbeat_interval = heartbeat_interval or self.HEARTBEAT_INTERVAL
        self.reconnect_delays = reconnect_delays or self.RECONNECT_DELAYS

        self._lock = threading.RLock()  # one lifecycle operation at a time
        self._executor = ThreadPoolExecutor(1, thread_name_prefix="orange3-cloud-session")
        self._cancel = threading.Event()  # stops a reconnect loop
        self._generation = 0  # changes with every connect and disconnect
        self._state = SessionState.DISCONNECTED
        self._message = ""
        self._handle: MachineHandle | None = None
        self._client: CloudClient | None = None
        self._info: InfoResponse | None = None
        self._alive_at: datetime.datetime | None = None  # the last /v1/info
        self._timer: QTimer | None = None
        self._heartbeat_pending = False
        # Signing in waits for the user's browser: its own thread, so machine
        # operations don't queue behind it.
        self._sign_in_executor = ThreadPoolExecutor(1, thread_name_prefix="orange3-cloud-sign-in")
        self._sign_in: tuple[Future[None], threading.Event] | None = None

        self._connectionChanged.connect(self._on_connection_changed)
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

    # ------------------------------------------------------------------
    # Properties

    @property
    def provider(self) -> ClusterProvider:
        """The provider for new machines: the one set on the session, else
        the one chosen in the settings (default: local Docker)."""
        if self._provider is not None:
            return self._provider
        provider_id = CloudSettings.load().provider
        try:
            return self._get_provider(provider_id)
        except ProviderError:
            raise ProviderError(
                f"The machine provider chosen in the Cloud settings ('{provider_id}') isn't "
                "available in this version. Choose Local (Docker) there."
            ) from None

    @provider.setter
    def provider(self, provider: ClusterProvider | None) -> None:
        self._provider = provider

    @property
    def machine_provider(self) -> ClusterProvider | None:
        """The provider of the current machine (``None`` without one)."""
        handle = self._handle
        return self._provider_for(handle) if handle is not None else None

    def _get_provider(self, provider_id: str) -> ClusterProvider:
        if provider_id not in self._providers:
            self._providers[provider_id] = self._resolve_provider(provider_id)
        return self._providers[provider_id]

    @property
    def state(self) -> SessionState:
        """The connection's state."""
        return self._state

    @property
    def message(self) -> str:
        """The last status or error message."""
        return self._message

    @property
    def handle(self) -> MachineHandle | None:
        """The machine's handle, while there is one."""
        return self._handle

    @property
    def is_connected(self) -> bool:
        """True while a client to a running machine is open."""
        return self._client is not None and self._state in (
            SessionState.CONNECTED,
            SessionState.RECONNECTING,
        )

    @property
    def client(self) -> CloudClient:
        """The client of the connected machine."""
        client = self._client
        if client is None:
            raise CloudConnectionError("Not connected to a cloud machine.")
        return client

    @property
    def last_info(self) -> InfoResponse | None:
        """``/v1/info`` from the last connect or heartbeat."""
        return self._info

    # ------------------------------------------------------------------
    # Operations

    def start_machine(
        self,
        machine_type: str | None = None,
        *,
        spec: EnvironmentSpec | None = None,
        idle_timeout: float | None = FROM_SETTINGS,
        max_lifetime: float | None = FROM_SETTINGS,
        timeout: float | None = None,
        wait: bool = False,
    ) -> MachineHandle | Future[MachineHandle]:
        """Start a machine with the user's environment and connect to it.

        The time limits are seconds (``None`` or 0: off); by default they
        come from the settings. If the session is already connected, its
        machine is returned.
        """
        if idle_timeout is FROM_SETTINGS or max_lifetime is FROM_SETTINGS:
            settings = CloudSettings.load()
            if idle_timeout is FROM_SETTINGS:
                idle_timeout = settings.idle_timeout
            if max_lifetime is FROM_SETTINGS:
                max_lifetime = settings.max_lifetime

        def op() -> MachineHandle:
            with self._lock:
                if self._client is not None and self._handle is not None:
                    return self._handle
                provider = self.provider
                self._set_state(SessionState.STARTING, "Collecting your package versions")
                env = spec or collect_environment()
                handle = provider.start(
                    env,
                    machine_type,
                    idle_timeout=idle_timeout,
                    max_lifetime=max_lifetime,
                    on_status=self._set_message,
                    timeout=timeout,
                )
                handle.extra["max_lifetime"] = f"{max_lifetime or 0:.0f}"  # for its cost
                # Saved first, so a machine that can't be reached can still be
                # found and stopped later.
                self.store.save(handle)
                self._connect(handle)
                return handle

        return self._run(op, wait)

    def connect(
        self, handle: MachineHandle, *, wait: bool = False
    ) -> InfoResponse | Future[InfoResponse]:
        """Connect to a running machine and remember it for reattaching."""

        def op() -> InfoResponse:
            with self._lock:
                return self._connect(handle)

        return self._run(op, wait)

    def disconnect(self, *, forget: bool = False, wait: bool = False) -> None | Future[None]:
        """Close the connection; the machine keeps running. ``forget`` also
        drops the persisted handle, so :meth:`reattach` won't find it."""
        self._cancel.set()

        def op() -> None:
            with self._lock:
                self._drop_client()
                if forget:
                    self.store.clear()
                    self._handle = None
                self._set_state(SessionState.DISCONNECTED, "")

        return self._run(op, wait)

    def stop_machine(self, *, wait: bool = False) -> None | Future[None]:
        """Stop and delete the machine (the connected or the persisted one)."""
        self._cancel.set()

        def op() -> None:
            with self._lock:
                handle = self._handle or self.store.load()
                running = self._client is not None
                self._drop_client()
                if handle is None:
                    self._set_state(SessionState.DISCONNECTED, "")
                    return
                self._set_state(SessionState.STOPPING, "Stopping the machine")
                provider = self._provider_for(handle)
                stopped = None if running else self._stopped_at(handle)
                provider.stop(handle)
                provider.delete(handle)
                self.machine_costs.record_session(handle, stopped, running=running)
                self.store.clear()
                self._handle = None
                self._set_state(SessionState.DISCONNECTED, "The machine was stopped.")

        return self._run(op, wait)

    def reattach(self, *, wait: bool = False) -> MachineHandle | None | Future:
        """Reconnect to the persisted machine if it still runs; otherwise
        clean it up and forget it. Returns the handle, or ``None``."""

        def op() -> MachineHandle | None:
            with self._lock:
                if self._client is not None:
                    return self._handle
                handle = self.store.load()
                if handle is None:
                    return None
                self._set_state(SessionState.CONNECTING, "Looking for the running machine")
                provider = self._provider_for(handle)
                status = provider.status(handle)
                if not status.alive:
                    if status.exists:  # it stopped by itself while Orange was closed
                        stopped = self._stopped_at(handle)
                        provider.delete(handle)
                        self.machine_costs.record_session(handle, stopped)
                    self.store.clear()
                    self._handle = None
                    self._set_state(SessionState.DISCONNECTED, status.message)
                    return None
                provider.ensure_access(handle)
                self._connect(handle)
                return handle

        return self._run(op, wait)

    @property
    def signing_in(self) -> bool:
        """Whether a :meth:`sign_in` runs."""
        return self._sign_in is not None and not self._sign_in[0].done()

    def sign_in(self, provider_id: str) -> Future[None]:
        """Sign in to the account of provider ``provider_id`` (e.g. Google's
        sign-in page in the browser) on a background thread, where the
        provider waits for the user; ``signInFinished`` reports the end. If a
        sign-in already runs, returns it."""
        if self._sign_in is not None and not self._sign_in[0].done():
            return self._sign_in[0]
        cancel = threading.Event()

        def op() -> None:
            error: ProviderError | None = None
            try:
                self._provider_by_id(provider_id).sign_in(cancel)
            except ProviderError as exc:
                error = exc
            except Exception as exc:  # noqa: BLE001 - reported to the user through the signal
                log.exception("Signing in failed")
                error = ProviderError(f"Signing in failed: {exc}")
            self.signInFinished.emit(error)

        future = self._sign_in_executor.submit(op)
        self._sign_in = future, cancel
        return future

    def cancel_sign_in(self) -> None:
        """Stop a running :meth:`sign_in`; ``signInFinished`` follows."""
        if self._sign_in is not None:
            self._sign_in[1].set()

    def leftovers(self) -> list[MachineHandle]:
        """The chosen provider's machines that the session doesn't use (e.g.
        left by an Orange that quit while starting one). Blocks; doesn't
        change the session's state."""
        provider = self.provider
        keep = self._handle or self.store.load()
        keep_id = keep.machine_id if keep is not None else None
        return [h for h in provider.list_machines() if h.machine_id != keep_id]

    def delete_leftovers(self, *, wait: bool = False) -> list[str] | Future[list[str]]:
        """Delete the machines of the chosen provider that the session doesn't
        use (e.g. left by an Orange that quit while starting one). Returns
        their names."""

        def op() -> list[str]:
            with self._lock:
                provider = self.provider
                keep = self._handle or self.store.load()
                if keep is not None and keep.provider != provider.id:
                    keep = None
                keep_id = keep.machine_id if keep is not None else None
                others = provider.list_machines() if provider.finds_leftovers else []
                stops = [(h, self._stopped_at(h)) for h in others if h.machine_id != keep_id]
                deleted = provider.delete_leftovers(keep)
                for handle, stopped in stops:  # left out if the price is unknown
                    if handle.name in deleted:
                        self.machine_costs.record_session(handle, stopped)
                return deleted

        return self._run(op, wait)

    def price_per_hour(self, handle: MachineHandle | None = None) -> float | None:
        """USD per hour of the machine of ``handle`` (default: the current
        one); 0 for a free one, ``None`` if unknown."""
        handle = handle or self._handle
        return self.machine_costs.price_per_hour(handle) if handle is not None else None

    def session_cost(self, handle: MachineHandle | None = None) -> float | None:
        """USD the machine of ``handle`` (default: the current one) has cost
        since it was created; ``None`` if unknown."""
        handle = handle or self._handle
        if handle is None:
            return None
        return cost_of(machine_seconds(handle), self.price_per_hour(handle))

    def record_jobs(self, handle: MachineHandle | None, results: list[JobResult]) -> float | None:
        """Record the cost of finished jobs run on the machine of ``handle``;
        returns their total in USD (``None`` if unknown)."""
        return self.machine_costs.record_jobs(handle, results) if handle is not None else None

    def jobs_cost(self, handle: MachineHandle | None = None) -> float:
        """USD of the jobs recorded so far on the machine of ``handle``
        (default: the current one) by this session."""
        handle = handle or self._handle
        return self.machine_costs.jobs_cost(handle) if handle is not None else 0.0

    def _stopped_at(self, handle: MachineHandle) -> datetime.datetime | None:
        """When the machine of ``handle`` stopped, as its provider says, and
        not before the session last saw it running (``None``: unknown)."""
        seen = self._alive_at if self._handle is handle else None
        return self.machine_costs.stopped_at(handle, seen)

    def info(self, *, refresh: bool = True) -> InfoResponse:
        """``/v1/info`` of the connected machine; blocks when ``refresh``."""
        if not refresh and self._info is not None:
            return self._info
        info = self.client.info()
        self._info = info
        self.infoChanged.emit(info)
        return info

    def check_connection(self) -> bool:
        """The heartbeat: is the machine still there? Reconnects after
        network failures, and disconnects when the machine is gone. Blocks;
        returns whether the session is connected afterwards."""
        with self._lock:
            client, handle, generation = self._client, self._handle, self._generation
        if client is None or handle is None:
            return False
        try:
            info = client.info()
        except CloudError as exc:
            return self._recover(client, handle, generation, exc)
        if generation == self._generation:
            self._info, self._alive_at = info, _utc_now()
            self.infoChanged.emit(info)
        return True

    def shutdown(self) -> None:
        """Stop the background work (when Orange quits). The machine keeps
        running and is reattached on the next start."""
        self._cancel.set()
        self.cancel_sign_in()
        if self._timer is not None:
            self._timer.stop()
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._sign_in_executor.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------
    # Internals (called with self._lock held, unless noted)

    def _run(self, op: Callable[[], T], wait: bool) -> T | Future[T]:
        def guarded() -> T:
            try:
                return op()
            except Exception as exc:
                message = str(exc) or type(exc).__name__
                self._set_state(
                    SessionState.CONNECTED if self._client is not None else SessionState.ERROR,
                    message,
                )
                self.error.emit(message)
                raise

        if wait:
            return guarded()
        return self._executor.submit(guarded)

    def _provider_for(self, handle: MachineHandle) -> ClusterProvider:
        return self._provider_by_id(handle.provider)

    def _provider_by_id(self, provider_id: str) -> ClusterProvider:
        if self._provider is not None and self._provider.id == provider_id:
            return self._provider
        return self._get_provider(provider_id)

    def _connect(self, handle: MachineHandle) -> InfoResponse:
        self._set_state(SessionState.CONNECTING, f"Connecting to {handle.name}")
        client = CloudClient(
            handle.url, handle.token, handle.cert_pem, server_hostname=handle.server_hostname
        )
        try:
            info = client.info()
        except BaseException:
            client.close()
            raise
        self._drop_client()
        self._cancel.clear()
        self._generation += 1
        self._handle, self._client, self._info = handle, client, info
        self._alive_at = _utc_now()
        self.store.save(handle)
        self._set_state(SessionState.CONNECTED, f"Connected to {handle.name}")
        self.connected.emit()
        self.infoChanged.emit(info)
        self._connectionChanged.emit(True)
        return info

    def _drop_client(self) -> None:
        client, self._client = self._client, None
        self._info = None
        self._generation += 1
        if client is not None:
            client.close()
            self._connectionChanged.emit(False)
            self.disconnected.emit()

    def _recover(
        self, client: CloudClient, handle: MachineHandle, generation: int, exc: CloudError
    ) -> bool:
        """After a failed heartbeat (without the lock: it waits)."""
        self._cancel.clear()
        self._set_state(SessionState.RECONNECTING, f"Lost the connection; reconnecting ({exc})")
        provider = self._provider_for(handle)
        for delay in (0, *self.reconnect_delays):
            if self._cancel.wait(delay) or generation != self._generation:
                return False
            try:
                status = provider.status(handle)
            except (ProviderError, OSError):  # e.g. no network to the provider either
                status = None
            if status is not None and not status.alive:
                # Since the last /v1/info, which may be long ago after a sleep.
                silent = (_utc_now() - self._alive_at).total_seconds() if self._alive_at else 0
                window = 2 * self.heartbeat_interval + silent
                reason = _explain_stop(status.message, self._info, window)
                self._machine_gone(provider, handle, generation, reason)
                return False
            if status is not None:
                try:  # e.g. this computer's IP address changed
                    provider.ensure_access(handle)
                except (ProviderError, OSError) as access_exc:  # tried again next round
                    log.warning("Couldn't update the access to %s: %s", handle.name, access_exc)
            try:
                info = client.info()
            except CloudAuthError:
                self._machine_gone(provider, handle, generation, "The machine was replaced.")
                return False
            except CloudError as retry_exc:
                exc = retry_exc
                continue
            with self._lock:
                if generation != self._generation:
                    return False
                self._info, self._alive_at = info, _utc_now()
                self._set_state(SessionState.CONNECTED, f"Reconnected to {handle.name}")
            self.infoChanged.emit(info)
            return True
        with self._lock:
            if generation != self._generation:
                return False
            self._drop_client()
            message = f"Can't reach the machine {handle.name}: {exc}"
            self._set_state(SessionState.ERROR, message)
        self.error.emit(message)
        return False

    def _machine_gone(
        self, provider: ClusterProvider, handle: MachineHandle, generation: int, reason: str
    ) -> None:
        with self._lock:
            if generation != self._generation:
                return
            # It stopped by itself since the last heartbeat (now, if its
            # provider doesn't say); recorded once deleted, else it's a leftover.
            stopped = self._stopped_at(handle) or _utc_now()
            self._drop_client()
            self._handle = None
            self.store.clear()
            try:
                provider.delete(handle)
            except (ProviderError, OSError) as exc:  # best effort: it's gone already
                log.warning("Couldn't clean up %s: %s", handle.name, exc)
            else:
                self.machine_costs.record_session(handle, stopped)
            message = reason or "The machine is gone."
            self._set_state(SessionState.DISCONNECTED, message)
        self.error.emit(message)

    def _set_state(self, state: SessionState, message: str | None = None) -> None:
        changed = state is not self._state
        self._state = state
        if changed:
            self.statusChanged.emit(state.value)
        if message is not None:
            self._set_message(message)

    def _set_message(self, message: str) -> None:
        if message != self._message:
            self._message = message
            self.messageChanged.emit(message)

    # ------------------------------------------------------------------
    # Heartbeat (in the session's thread)

    @Slot(bool)
    def _on_connection_changed(self, connected: bool) -> None:
        if QCoreApplication.instance() is None:
            return  # no event loop, e.g. a plain Python shell
        if self._timer is None:
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._heartbeat)
        if connected and self._client is not None:
            self._timer.start(int(self.heartbeat_interval * 1000))
        elif not connected and self._client is None:
            self._timer.stop()

    def _heartbeat(self) -> None:
        if self._heartbeat_pending or self._client is None:
            return
        self._heartbeat_pending = True

        def done(_: Any) -> None:
            self._heartbeat_pending = False

        try:
            future = self._executor.submit(self.check_connection)
        except RuntimeError:  # shut down
            self._heartbeat_pending = False
            return
        future.add_done_callback(done)


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _explain_stop(message: str, info: InfoResponse | None, window: float) -> str:
    """Add why a machine stopped by itself when its provider can't tell: the
    last ``/v1/info`` shows that its idle timeout or maximum lifetime was
    due within ``window`` seconds. (A Google Cloud VM's console, which has
    the reason, is cleared when the VM powers off.) With jobs running, the
    idle timeout was due after Orange had been silent for it twice: the
    machine cancels the jobs, then stops when idle."""
    if info is None or ":" in message:  # no info, or the reason is known
        return message
    silent = None
    if info.idle_timeout_seconds and (info.jobs_running or info.jobs_queued):
        silent = 2 * info.idle_timeout_seconds
    reasons = [
        (info.idle_time_left_seconds, "it was idle for longer than the idle timeout"),
        (silent, "Orange hadn't been in touch for longer than its idle timeout"),
        (info.lifetime_left_seconds, "its maximum lifetime ended"),
    ]
    due = [(left, why) for left, why in reasons if left is not None and left <= window]
    return f"The machine stopped: {min(due)[1]}." if due else message
