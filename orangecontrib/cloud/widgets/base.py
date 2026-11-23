"""The base class of the Cloud widgets.

:class:`OWCloudBaseWidget` gives every Cloud widget the same behaviour:

* a **Cloud box** at the top of the control area: a connection indicator (a
  coloured dot and a short text that follow the shared
  :class:`~orangecontrib.cloud.client.session.CloudSession`), a
  "Cloud settings…" button, and a **status line** that says, after a run,
  where and with which versions the result was computed, and about what its
  jobs cost (recorded by the session);
* :meth:`~OWCloudBaseWidget.run_cloud_job` and the more general
  :meth:`~OWCloudBaseWidget.run_cloud`, which run calls to the machine off the
  GUI thread (with Orange's :class:`~Orange.widgets.utils.concurrent.
  ConcurrentWidgetMixin`), wired to the widget's progress bar and status text;
* cancelling: a new run, :meth:`cancel` and closing the widget stop the
  running task, and the job on the machine is cancelled too. Closing the
  widget (Orange closes them all when it quits) waits at most
  :data:`CANCEL_WAIT` seconds for that: a machine that can't be reached
  cancels the job itself once Orange has been silent for its idle timeout;
* Orange's error messages for "not connected", lost connections, errors on
  the machine (with the machine's traceback in the message's details),
  running out of memory, and jobs the machine cancelled (e.g. when Orange
  hadn't been in touch for its idle timeout, as after a long sleep).

Writing a Cloud widget::

    class OWCloudSomething(OWCloudBaseWidget):
        name = "Cloud Something"

        def __init__(self):
            super().__init__()
            ...  # own controls, below the Cloud box

        @Inputs.data
        def set_data(self, data):
            self.cancel()  # a new input makes the running job obsolete
            self.data = data
            self.commit()

        def commit(self):
            self.run_cloud_job("something", inputs={"table": self.data.table_id},
                               params={"k": self.k})

        def on_cloud_result(self, result):  # a JobResult, in the GUI thread
            self.Outputs.data.send(...)

        def onDeleteWidget(self):  # if overridden, call super()
            ...
            super().onDeleteWidget()

For several calls in one task (e.g. a job and then downloading its result),
use :meth:`OWCloudBaseWidget.run_cloud` with a function that gets a
:class:`CloudTask`.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from AnyQt.QtCore import Qt
from AnyQt.QtWidgets import QHBoxLayout, QLabel
from Orange.widgets import gui
from Orange.widgets.utils.concurrent import ConcurrentMixin, ConcurrentWidgetMixin, TaskState
from Orange.widgets.widget import Msg, OWWidget

from orangecontrib.cloud.client.api import (
    CloudCancelled,
    CloudClient,
    CloudConnectionError,
    CloudError,
    CloudOutOfMemoryError,
)
from orangecontrib.cloud.client.costs import format_price, format_usd
from orangecontrib.cloud.client.providers.base import (
    ClusterProvider,
    MachineHandle,
    MachineType,
    ProviderError,
)
from orangecontrib.cloud.client.session import CloudSession, SessionState
from orangecontrib.cloud.gui.machine_size import size_hints
from orangecontrib.cloud.gui.settings_dialog import show_settings_dialog
from orangecontrib.cloud.protocol.errors import ErrorCode
from orangecontrib.cloud.protocol.jobs import JobInfo, JobResult
from orangecontrib.cloud.protocol.machine import InfoResponse

__all__ = ["CloudTask", "OWCloudBaseWidget", "computed_with"]

log = logging.getLogger(__name__)

#: Seconds that closing a widget, or quitting Orange, waits for its task to
#: cancel its job on the machine.
CANCEL_WAIT = 3.0

#: Colour of the connection dot, per session state.
STATE_COLORS = {
    SessionState.CONNECTED: "#2e9e44",
    SessionState.STARTING: "#e0a000",
    SessionState.CONNECTING: "#e0a000",
    SessionState.RECONNECTING: "#e0a000",
    SessionState.STOPPING: "#e0a000",
    SessionState.DISCONNECTED: "#9e9e9e",
    SessionState.ERROR: "#d32f2f",
}

STATE_TEXTS = {
    SessionState.STARTING: "Starting the machine…",
    SessionState.CONNECTING: "Connecting…",
    SessionState.RECONNECTING: "Reconnecting…",
    SessionState.STOPPING: "Stopping the machine…",
    SessionState.DISCONNECTED: "Not connected",
    SessionState.ERROR: "Connection error",
}


def machine_label(info: InfoResponse | None, handle: MachineHandle | None) -> str:
    """A short description of the machine, e.g. ``n2-standard-16 (16 CPUs)``."""
    name = (info.machine if info is not None else None) or (handle.name if handle else None)
    if name == "local" or (handle is not None and handle.provider == "local"):
        name = "the local machine"
    name = name or "the cloud machine"
    if info is not None:
        name += f" ({info.cpu_count} CPU{'s' if info.cpu_count != 1 else ''})"
    return name


def computed_with(
    info: InfoResponse | None,
    handle: MachineHandle | None,
    runtime_seconds: float | None = None,
) -> str:
    """The status line: "Computed with Orange 3.x and scikit-learn 1.y on
    <machine> in 3.2 s." (versions on the machine)."""
    packages = info.packages if info is not None else {}
    versions = [
        f"{label} {packages[name]}"
        for name, label in (("orange3", "Orange"), ("scikit-learn", "scikit-learn"))
        if name in packages
    ]
    text = "Computed"
    if versions:
        text += " with " + " and ".join(versions)
    text += " on " + machine_label(info, handle)
    if runtime_seconds is not None:
        text += f" in {_duration(runtime_seconds)}"
    return text + "."


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, seconds = divmod(round(seconds), 60)
    if minutes < 60:
        return f"{minutes} min {seconds} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min"


class CloudTask:
    """What a function run by :meth:`OWCloudBaseWidget.run_cloud` gets. It
    runs in a worker thread: don't touch the widget's GUI from it.

    * :attr:`client`: the machine's :class:`CloudClient`,
    * :meth:`run_job`: submit a job and wait for it, with progress, status
      text and cancelling wired to the widget,
    * :meth:`set_progress` / :meth:`set_status` for other steps,
    * :meth:`send_partial`: hand something to the widget before the end,
    * :meth:`is_cancelled`: check it in long loops of your own.
    """

    def __init__(self, client: CloudClient, state: TaskState):
        self.client = client
        self._state = state
        #: Results of the jobs run so far (for the status line's runtime).
        self.job_results: list[JobResult] = []

    def is_cancelled(self) -> bool:
        """True once the user cancelled the task."""
        return self._state.is_interruption_requested()

    # Once cancelled, these do nothing: the widget may be gone with the state.

    def set_progress(self, fraction: float) -> None:
        """Progress of the whole task, from 0 to 1."""
        if not self.is_cancelled():
            self._state.set_progress_value(100 * max(0.0, min(1.0, fraction)))

    def set_status(self, text: str) -> None:
        """Show ``text`` as the task's status."""
        if not self.is_cancelled():
            self._state.set_status(text)

    def send_partial(self, value: Any) -> None:
        """Hand ``value`` to the widget's ``on_partial_result`` (GUI thread),
        e.g. something learned before the job finishes."""
        if not self.is_cancelled():
            self._state.set_partial_result(value)

    def run_job(
        self,
        kind: str,
        inputs: Mapping[str, str | list[str]] | None = None,
        params: Mapping[str, Any] | None = None,
        objects: Mapping[str, Any] | None = None,
        *,
        progress_range: tuple[float, float] = (0.0, 1.0),
    ) -> JobResult:
        """Submit a job and wait until it finishes; returns its result.

        The job's progress fills ``progress_range`` of the widget's progress
        bar. If the task is cancelled (new run, :meth:`~OWCloudBaseWidget.
        cancel`, closing the widget), the job is cancelled on the machine
        and :class:`CloudCancelled` is raised. Failed jobs raise their
        :class:`CloudError`.
        """
        if self.is_cancelled():
            raise CloudCancelled(ErrorCode.CANCELLED, "The job was cancelled.")
        self.set_status("Sending the job to the cloud machine")
        job = self.client.submit_job(kind, inputs=inputs, params=params, objects=objects)
        return self.wait_for_job(job, progress_range=progress_range)

    def wait_for_job(
        self, job: JobInfo, *, progress_range: tuple[float, float] = (0.0, 1.0)
    ) -> JobResult:
        """Wait for a job started otherwise (e.g. ``derive``), as
        :meth:`run_job` does, and return its result."""
        low, high = progress_range

        def on_progress(fraction: float, text: str) -> None:
            self.set_progress(low + (high - low) * fraction)
            if text:
                self.set_status(text)

        result = self.client.wait_for_job(job, on_progress, should_cancel=self.is_cancelled)
        self.set_progress(high)
        self.job_results.append(result)
        return result

    def get_object(
        self, object_id: str, *, progress_range: tuple[float, float] = (0.0, 1.0)
    ) -> Any:
        """Download a stored object; the download fills ``progress_range`` of
        the progress bar and stops with :class:`CloudCancelled` when the task
        is cancelled."""
        low, high = progress_range

        def on_progress(received: int, total: int | None) -> None:
            if self.is_cancelled():
                raise CloudCancelled(ErrorCode.CANCELLED, "The download was cancelled.")
            if total:
                self.set_progress(low + (high - low) * received / total)

        return self.client.get_object(object_id, on_progress)


@dataclass
class _Outcome:
    """What a finished task hands to the GUI thread."""

    value: Any
    callback: Callable[[Any], None]
    info: InfoResponse | None
    handle: MachineHandle | None
    job_results: list[JobResult] = field(default_factory=list)

    @property
    def runtime_seconds(self) -> float | None:
        times = [r.runtime_seconds for r in self.job_results if r.runtime_seconds is not None]
        return sum(times) if times else None


class OWCloudBaseWidget(OWWidget, ConcurrentWidgetMixin, openclass=True):
    """Base class of the Cloud widgets (see the module's documentation)."""

    class Error(OWWidget.Error):
        not_connected = Msg("Not connected to a cloud machine. Open Cloud settings… to start one.")
        connection_lost = Msg("Lost the connection to the cloud machine: {}")
        cloud_error = Msg("{}")
        out_of_memory = Msg(
            "The cloud machine ran out of memory. Use a machine with more memory "
            "(Cloud settings…) or less data.\n{}"
        )
        #: The job was cancelled on the machine, not by the widget.
        job_cancelled = Msg("{} Run it again.")

    def __init__(self):
        OWWidget.__init__(self)
        ConcurrentWidgetMixin.__init__(self)
        #: The shared session (tests may put their own in ``CloudSession._instance``).
        self.session: CloudSession = CloudSession.instance()
        #: The status line's text after the last successful run.
        self.cloud_status = ""
        #: USD the last run's jobs cost (``None``: unknown or nothing ran).
        self.cloud_cost: float | None = None
        #: Details (the machine's traceback) of the last error message.
        self.cloud_error_details: str | None = None
        #: The tasks started, including cancelled ones that are still ending.
        self._task_futures: list[concurrent.futures.Future] = []
        self._init_cloud_box()
        self._connect_session()
        self._update_connection_indicator()

    # ------------------------------------------------------------------
    # GUI

    def _init_cloud_box(self) -> None:
        self.cloud_box = box = gui.vBox(self.controlArea, "Cloud")
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self.connection_dot = QLabel()
        self.connection_dot.setTextFormat(Qt.TextFormat.RichText)
        self.connection_label = QLabel()
        row.addWidget(self.connection_dot)
        row.addWidget(self.connection_label, 1)
        box.layout().addLayout(row)
        self.settings_button = gui.button(
            box, self, "Cloud settings…", callback=self.open_cloud_settings, autoDefault=False
        )
        # No word wrap: wrapped labels don't resize fixed-size widgets well.
        self.cloud_status_label = QLabel()
        self.cloud_status_label.setVisible(False)
        box.layout().addWidget(self.cloud_status_label)

    def open_cloud_settings(self) -> None:
        """Open the Cloud settings dialog."""
        self.settings_dialog = show_settings_dialog(self, session=self.session)

    def _connect_session(self) -> None:
        s = self.session
        s.statusChanged.connect(self._update_connection_indicator)
        s.messageChanged.connect(self._update_connection_indicator)
        s.infoChanged.connect(self._update_connection_indicator)
        s.connected.connect(self._on_session_connected)
        s.disconnected.connect(self._on_session_disconnected)

    def _disconnect_session(self) -> None:
        s = self.session
        for signal, slot in (
            (s.statusChanged, self._update_connection_indicator),
            (s.messageChanged, self._update_connection_indicator),
            (s.infoChanged, self._update_connection_indicator),
            (s.connected, self._on_session_connected),
            (s.disconnected, self._on_session_disconnected),
        ):
            with contextlib.suppress(TypeError, RuntimeError):
                signal.disconnect(slot)

    def _update_connection_indicator(self, *_: Any) -> None:
        s = self.session
        state = s.state
        color = STATE_COLORS.get(state, "#9e9e9e")
        self.connection_dot.setText(f'<span style="color:{color}; font-size:14px">●</span>')
        if s.is_connected and state is SessionState.CONNECTED:
            text = "Connected to " + machine_label(s.last_info, s.handle)
        else:
            text = STATE_TEXTS.get(state, state.value)
        self.connection_label.setText(text)
        self.connection_label.setToolTip(s.message or text)
        self.connection_dot.setToolTip(s.message or text)

    def _set_cloud_status(self, text: str) -> None:
        self.cloud_status = text
        self.cloud_status_label.setText(
            text.replace(" on ", "\non ", 1).replace(" Cost ", "\nCost ", 1)
        )
        self.cloud_status_label.setVisible(bool(text))

    # ------------------------------------------------------------------
    # Session events

    def _on_session_connected(self) -> None:
        self.Error.not_connected.clear()
        self._update_connection_indicator()
        self.on_cloud_connected()

    def _on_session_disconnected(self) -> None:
        self._update_connection_indicator()
        if self.task is not None:
            self.cancel()
            self._show_error(
                self.Error.connection_lost,
                "the machine was stopped or can't be reached. Run again when it's back.",
            )

    def on_cloud_connected(self) -> None:
        """Called when the session connects to a machine. Override, e.g. to
        run again a job that couldn't run without a machine."""

    # ------------------------------------------------------------------
    # Running

    def run_cloud_job(
        self,
        kind: str,
        inputs: Mapping[str, str | list[str]] | None = None,
        params: Mapping[str, Any] | None = None,
        objects: Mapping[str, Any] | None = None,
        *,
        on_done: Callable[[JobResult], None] | None = None,
    ) -> bool:
        """Run one job on the machine, off the GUI thread.

        ``inputs`` reference tables and objects on the machine by ID,
        ``params`` are JSON options, ``objects`` are pickled and sent. The
        job's :class:`JobResult` goes to ``on_done`` (default:
        :meth:`on_cloud_result`) in the GUI thread. A running task is
        cancelled first. Returns False (and shows "not connected") when
        there's no machine.
        """
        return self.run_cloud(
            lambda task: task.run_job(kind, inputs, params, objects), on_done=on_done
        )

    def run_cloud(
        self,
        fn: Callable[[CloudTask], Any],
        *,
        on_done: Callable[[Any], None] | None = None,
    ) -> bool:
        """Run ``fn(task)`` (a :class:`CloudTask`) off the GUI thread.

        Its return value goes to ``on_done`` (default: :meth:`on_cloud_result`)
        in the GUI thread; exceptions become the widget's error messages. A
        running task is cancelled first (and its job on the machine).
        Returns False (and shows "not connected") when there's no machine.
        """
        self.cancel()
        self._clear_cloud_messages()
        self._set_cloud_status("")
        self.cloud_cost = None
        session = self.session
        if not session.is_connected:
            self.Error.not_connected()
            return False
        client, info, handle = session.client, session.last_info, session.handle
        callback = on_done or self.on_cloud_result

        def work(state: TaskState) -> _Outcome:
            task = CloudTask(client, state)
            # Once cancelled, its failed calls aren't repeated, so it ends soon.
            with client.giving_up_when(task.is_cancelled):
                value = fn(task)
            return _Outcome(value, callback, info, handle, task.job_results)

        self.start(work)
        self._task_futures = [f for f in self._task_futures if not f.done()]
        self._task_futures.append(self.task.future)
        return True

    def on_cloud_result(self, result: Any) -> None:
        """The result of :meth:`run_cloud_job` (a :class:`JobResult`) or of
        :meth:`run_cloud`'s function, in the GUI thread. Override."""

    def cloud_provider(self) -> ClusterProvider | None:
        """The current machine's provider, else the one chosen in the Cloud
        settings (``None`` if that one isn't available)."""
        provider = self.session.machine_provider
        if provider is not None:
            return provider
        try:
            return self.session.provider
        except ProviderError:
            return None

    def size_choices(self) -> tuple[str, list[MachineType]]:
        """Where a bigger machine could run and its sizes, for suggesting one:
        the current machine's provider, or if it has no sizes (e.g. this
        computer), the one chosen in the Cloud settings. ``("", [])`` if
        neither has sizes."""
        providers = [self.session.machine_provider]
        try:
            providers.append(self.session.provider)
        except ProviderError as exc:
            log.debug("No provider chosen: %s", exc)
        for provider in providers:
            if provider is not None and provider.machine_sizes:
                return provider.label, list(provider.machine_sizes)
        return "", []

    def cloud_cost_text(self, handle: MachineHandle | None) -> str:
        """The last run's cost for the status line, e.g. "Cost about $0.0012
        at $0.14/h." or "Cost under $0.0001 at $0.15/h." (empty for free or
        unknown costs)."""
        cost = self.cloud_cost
        if not cost:
            return ""
        price = self.session.price_per_hour(handle)
        amount = format_usd(cost)
        amount = amount if amount.startswith("under") else f"about {amount}"
        return f"Cost {amount} at {format_price(price)}."

    def cloud_status_text(
        self, info: InfoResponse | None, handle: MachineHandle | None, runtime: float | None
    ) -> str:
        """The status line after a successful run."""
        text = computed_with(info, handle, runtime)
        cost = self.cloud_cost_text(handle)
        return f"{text} {cost}" if cost else text

    # ConcurrentWidgetMixin callbacks (GUI thread)

    def on_done(self, outcome: _Outcome) -> None:
        self.cloud_cost = self.session.record_jobs(outcome.handle, outcome.job_results)
        self._set_cloud_status(
            self.cloud_status_text(outcome.info, outcome.handle, outcome.runtime_seconds)
        )
        outcome.callback(outcome.value)

    def on_partial_result(self, result: Any) -> None:
        pass

    def on_exception(self, ex: Exception) -> None:
        if isinstance(ex, CloudOutOfMemoryError):
            self._show_error(self.Error.out_of_memory, ex.message, ex.remote_traceback)
        elif isinstance(ex, CloudCancelled):
            # The widget's own cancelling never gets here, so the machine
            # cancelled the job, e.g. after Orange was silent for too long.
            self._show_error(self.Error.job_cancelled, ex.message)
        elif isinstance(ex, CloudConnectionError):
            self._show_error(self.Error.connection_lost, ex.message)
        elif isinstance(ex, CloudError):
            self._show_error(self.Error.cloud_error, ex.message, ex.remote_traceback)
        else:
            self._show_error(self.Error.cloud_error, str(ex) or type(ex).__name__, exc=ex)

    def _show_error(
        self,
        message: Msg,
        text: str,
        remote_traceback: str | None = None,
        exc: BaseException | None = None,
    ) -> None:
        self._clear_cloud_messages()
        message(text, exc_info=exc)
        if remote_traceback:
            # Shown under "Show details" in the widget's message bar.
            message.tb = "Traceback on the cloud machine:\n" + remote_traceback
            self.update_message_state()
        self.cloud_error_details = message.tb
        log.error("%s: %s%s", self.name, text, f"\n{message.tb}" if message.tb else "")

    def _clear_cloud_messages(self) -> None:
        for message in (
            self.Error.not_connected,
            self.Error.connection_lost,
            self.Error.cloud_error,
            self.Error.out_of_memory,
            self.Error.job_cancelled,
        ):
            message.clear()
        self.cloud_error_details = None

    # ------------------------------------------------------------------

    def onDeleteWidget(self) -> None:
        self._stop_tasks()
        self._disconnect_session()
        size_hints().set(self, None)  # what its data needed
        super().onDeleteWidget()

    def _stop_tasks(self) -> None:
        """Cancel the running task and wait, at most :data:`CANCEL_WAIT`
        seconds, until the tasks have cancelled their jobs on the machine."""
        ConcurrentMixin.cancel(self)  # without the widget's "ready" updates
        _, late = concurrent.futures.wait(self._task_futures, timeout=CANCEL_WAIT)
        if late:
            # Each ends once its call to the machine fails, without retries.
            log.warning(
                "%s: the machine didn't confirm the cancel in %s s; it cancels the "
                "job itself once Orange has been silent for its idle timeout.",
                self.name,
                CANCEL_WAIT,
            )
            return
        self.shutdown()  # nothing left to wait for
