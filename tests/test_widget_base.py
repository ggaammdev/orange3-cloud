"""The Cloud widgets' base class, through the developer sleep widget, against
the machine's server (with its job system) running in-process over HTTPS."""

import concurrent.futures
import shutil
import tempfile
import threading
import time
from importlib.metadata import version
from pathlib import Path

from AnyQt.QtCore import QTimer
from AnyQt.QtWidgets import QApplication, QMessageBox
from conftest import SPEC, CuttableProxy, https_server
from Orange.widgets.tests import base  # not WidgetTest itself: pytest would collect it

from orangecontrib.cloud.client.api import CloudClient
from orangecontrib.cloud.client.providers.base import (
    ClusterProvider,
    MachineHandle,
    MachineState,
    MachineStatus,
    MachineType,
    ProviderError,
)
from orangecontrib.cloud.client.session import CloudSession, HandleStore, SessionState
from orangecontrib.cloud.gui.quit_question import ask_before_quit
from orangecontrib.cloud.protocol.jobs import JobState
from orangecontrib.cloud.server.app import ServerConfig, create_machine_app
from orangecontrib.cloud.widgets.base import CANCEL_WAIT
from orangecontrib.cloud.widgets.dev.owcloudsleeptest import OWCloudSleepTest

TOKEN = "widget-test-token"


class SharedMachineProvider(ClusterProvider):
    """Hands out the one server the test class runs; "stopping" forgets it."""

    id = "shared"
    label = "Shared test machine"

    def __init__(self, url, cert):
        self.url, self.cert, self.running = url, cert, False
        self.price = 0.0

    def start(self, spec, machine_type=None, **kwargs):
        self.running = True
        return MachineHandle(self.id, "m1", "shared-1", self.url, TOKEN, self.cert)

    def status(self, handle=None):
        if self.running:
            return MachineStatus(MachineState.RUNNING)
        return MachineStatus(MachineState.NOT_FOUND, "Gone.")

    def stop(self, handle=None):
        self.running = False

    delete = stop

    def list_machines(self):
        return []

    def machine_types(self):
        return [MachineType("test", "Test")]

    def price_per_hour(self, machine_type=None):
        return self.price


class TestCloudBaseWidget(base.WidgetTest):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        folder = Path(tempfile.mkdtemp())
        cls.tear_down_stack.callback(shutil.rmtree, folder, ignore_errors=True)
        config = ServerConfig(
            token=TOKEN, machine="test-machine", data_dir=folder, worker_threads=2
        )
        url, cert, _ = cls.tear_down_stack.enter_context(
            https_server(create_machine_app(config), folder)
        )
        cls.provider = SharedMachineProvider(url, cert)

    def setUp(self):
        super().setUp()
        self.folder = Path(tempfile.mkdtemp())
        self.session = CloudSession(self.provider, store=HandleStore(self.folder / "m.json"))
        self._previous, CloudSession._instance = CloudSession._instance, self.session
        self.job_ids = []

    def tearDown(self):
        super().tearDown()  # closes the widgets first
        self.session.disconnect(forget=True, wait=True)
        self.session.shutdown()
        CloudSession._instance = self._previous
        shutil.rmtree(self.folder, ignore_errors=True)

    def connect(self):
        self.session.start_machine(spec=SPEC, wait=True)
        # Record the IDs of the jobs the widget submits.
        client = self.session.client
        submit = client.submit_job

        def recording_submit(*args, **kwargs):
            job = submit(*args, **kwargs)
            self.job_ids.append(job.id)
            return job

        client.submit_job = recording_submit
        self.process_events()  # deliver the session's signals
        return client

    def wait_for_job_state(self, client, index, state, timeout=10):
        deadline = time.monotonic() + timeout
        while True:
            self.process_events()
            if len(self.job_ids) > index and client.job_info(self.job_ids[index]).state == state:
                return
            assert time.monotonic() < deadline, f"job {index} never got {state}"
            time.sleep(0.05)

    def test_runs_a_job(self):
        self.connect()
        w = self.create_widget(OWCloudSleepTest)
        assert w.connection_label.text().startswith("Connected to test-machine (")
        progress, statuses = [], []
        w.progressBarValueChanged.connect(progress.append)
        set_status = w.setStatusMessage
        w.setStatusMessage = lambda text: (statuses.append(text), set_status(text))
        w.seconds, w.steps = 0.6, 3
        assert w.run()
        self.wait_until_finished(w, timeout=20000)

        assert [r.values for r in w.results] == [{"seconds": 0.6, "steps": 3}]
        assert any(0 < p < 100 for p in progress), progress
        assert "Step 3 of 3" in statuses or "Step 2 of 3" in statuses, statuses
        assert w.cloud_status.startswith(f"Computed with Orange {version('orange3')}")
        assert f"scikit-learn {version('scikit-learn')}" in w.cloud_status
        assert " on test-machine (" in w.cloud_status and w.cloud_status.endswith(" s.")
        assert w.cloud_status_label.text().replace("\n", " ") == w.cloud_status
        assert not w.Error.active and w.cloud_error_details is None

    def test_job_cost(self):
        """With a price, the status line shows the job's cost, and it's recorded."""
        self.provider.price = 3.6  # USD per hour: $0.001 per second
        try:
            self.connect()
            w = self.create_widget(OWCloudSleepTest)
            w.seconds, w.steps = 0.5, 1
            assert w.run()
            self.wait_until_finished(w, timeout=20000)
        finally:
            self.provider.price = 0.0
        runtime = w.results[0].runtime_seconds
        assert w.cloud_cost is not None and abs(w.cloud_cost - runtime / 1000) < 1e-9
        assert w.cloud_status.endswith(" at $3.60/h.") and " Cost about $0.000" in w.cloud_status
        assert "\nCost about $" in w.cloud_status_label.text()
        assert w.cloud_status_label.text().replace("\n", " ") == w.cloud_status
        (entry,) = self.session.costs.entries()
        assert (entry.what, entry.job_kind, entry.price_per_hour) == ("job", "sleep", 3.6)
        assert entry.cost == w.cloud_cost and self.session.jobs_cost() == w.cloud_cost

    def test_not_connected(self):
        w = self.create_widget(OWCloudSleepTest)
        assert w.connection_label.text() == "Not connected"
        assert not w.run()
        assert w.Error.not_connected.is_shown()
        assert "Cloud settings" in str(w.Error.not_connected)
        # The message goes away once a machine is connected.
        self.connect()
        self.process_events(lambda: not w.Error.not_connected.is_shown())
        assert w.connection_label.text().startswith("Connected to test-machine")

    def test_new_run_and_close_cancel_the_remote_job(self):
        client = self.connect()
        w = self.create_widget(OWCloudSleepTest)
        w.seconds, w.steps = 60, 600
        w.run()
        self.wait_for_job_state(client, 0, JobState.RUNNING)
        w.run()  # a new run cancels the first job on the machine
        self.wait_for_job_state(client, 0, JobState.CANCELLED)
        self.wait_for_job_state(client, 1, JobState.RUNNING)
        assert w.task is not None and w.isInvalidated()

        w.onDeleteWidget()  # closing waits until the job is cancelled
        assert client.job_info(self.job_ids[1]).state == JobState.CANCELLED
        assert w.results == [] and not w.Error.active

    def test_closing_with_an_unreachable_machine_is_quick(self):
        """The network drops packets while a job runs: closing the widget (as
        quitting Orange does) waits at most CANCEL_WAIT, and the task's calls
        aren't repeated, so its thread ends as soon as its call fails."""
        proxy = CuttableProxy(self.provider.url)
        self.addCleanup(proxy.close)
        self.session.provider = SharedMachineProvider(proxy.url, self.provider.cert)
        self.connect()
        direct = CloudClient(self.provider.url, TOKEN, self.provider.cert)
        self.addCleanup(direct.close)
        w = self.create_widget(OWCloudSleepTest)
        w.seconds, w.steps = 60, 600
        assert w.run()
        self.wait_for_job_state(direct, 0, JobState.RUNNING)
        future = w.task.future

        with proxy.cut():
            start = time.monotonic()
            w.onDeleteWidget()
            assert time.monotonic() - start < CANCEL_WAIT + 1
            # The cancel didn't get through; the task is still waiting for an answer.
            assert direct.job_info(self.job_ids[0]).state == JobState.RUNNING
            assert not future.done()
            proxy.drop_connections()  # its call fails ...
            concurrent.futures.wait([future], timeout=2)
            assert future.done()  # ... and isn't repeated
        direct.cancel_job(self.job_ids[0])

    def test_failing_job_shows_the_remote_traceback(self):
        self.connect()
        w = self.create_widget(OWCloudSleepTest)
        w.seconds, w.fail = 0, True
        w.run()
        self.wait_until_finished(w, timeout=20000)
        assert w.Error.cloud_error.is_shown()
        assert "failed on request" in str(w.Error.cloud_error)
        assert "RuntimeError: The sleep job failed on request." in w.cloud_error_details
        assert w.Error.cloud_error.tb == w.cloud_error_details  # "Show details"
        assert w.cloud_status == "" and w.results == []

        # A successful run clears the error.
        w.fail = False
        w.run()
        self.wait_until_finished(w, timeout=20000)
        assert not w.Error.active and len(w.results) == 1

    def test_job_cancelled_while_orange_slept(self):
        """A laptop asleep for longer than the idle timeout: the machine
        cancels the job, and on waking the widget's next poll says why."""
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        config = ServerConfig(
            token=TOKEN, data_dir=folder, worker_threads=1, idle_timeout_seconds=2.0
        )
        app = create_machine_app(config)
        server = https_server(app, folder)
        url, cert, _ = server.__enter__()
        self.addCleanup(server.__exit__, None, None, None)
        self.session.provider = SharedMachineProvider(url, cert)
        client = self.connect()
        jobs = app.state.server.job_manager
        w = self.create_widget(OWCloudSleepTest)
        w.seconds, w.steps = 60, 600
        assert w.run()

        def job_state():  # read in this process: a request would be Orange in touch
            return jobs.info(self.job_ids[0]).state if self.job_ids else None

        deadline = time.monotonic() + 10
        while job_state() != JobState.RUNNING:
            assert time.monotonic() < deadline, "the job didn't start"
            time.sleep(0.05)
        # Asleep: the widget's next poll waits until the laptop wakes up.
        awake = threading.Event()
        job_info = client.job_info
        client.job_info = lambda job_id: awake.wait() and job_info(job_id)
        deadline = time.monotonic() + 10
        while job_state() != JobState.CANCELLED:
            assert time.monotonic() < deadline, "the machine didn't cancel the job"
            time.sleep(0.05)
        awake.set()
        self.wait_until_finished(w, timeout=10000)

        assert w.Error.job_cancelled.is_shown() and not w.Error.cloud_error.is_shown()
        assert str(w.Error.job_cancelled) == (
            "The machine cancelled the job: Orange hadn't been in touch for 2 seconds. "
            "Run it again."
        )
        assert w.results == [] and w.cloud_status == ""

    def test_stop_machine_button(self):
        """The Stop machine button follows the session's state, asks first, and
        stops the machine as Stop in Cloud settings does; a failed stop shows."""
        w = self.create_widget(OWCloudSleepTest)
        assert not w.stop_button.isEnabled()  # no machine
        seen = []
        self.session.statusChanged.connect(
            lambda state: seen.append((state, w.stop_button.isEnabled()))
        )
        self.provider.price = 1.2  # a cloud machine
        self.addCleanup(setattr, self.provider, "price", 0.0)
        self.connect()
        assert ("starting", False) in seen and w.stop_button.isEnabled()

        questions = answer_question("Cancel")
        w.stop_button.click()
        assert questions[0].startswith("Stop the cloud machine? The machine, its disk and")
        assert "$1.20/h" in questions[0] and "so far" in questions[0]
        assert self.session.is_connected and self.provider.running

        def fail(handle=None):
            raise ProviderError("No permission to stop it.")

        self.provider.stop = fail
        answer_question("Stop")
        w.stop_button.click()
        self.process_events(lambda: w.Error.cloud_error.is_shown())
        assert str(w.Error.cloud_error) == "Couldn't stop the machine: No permission to stop it."
        del self.provider.stop

        assert w.stop_button.isEnabled()  # to try again
        answer_question("Stop")
        w.stop_button.click()
        self.process_events(lambda: self.session.state is SessionState.DISCONNECTED)
        assert ("stopping", False) in seen and not w.stop_button.isEnabled()
        assert not self.provider.running and self.session.store.load() is None

    def test_quit_question_when_the_stop_fails(self):
        """Quitting with "Stop and quit": a stop that fails says why, and
        Orange quits only if the user says so."""
        self.provider.price = 1.2
        self.addCleanup(setattr, self.provider, "price", 0.0)
        self.connect()
        w = self.create_widget(OWCloudSleepTest)

        def fail(handle=None):
            raise ProviderError("No permission to stop it.")

        self.provider.stop = fail
        self.addCleanup(delattr, self.provider, "stop")
        for answer, quits in (("Cancel", False), ("Quit anyway", True)):
            questions = answer_question("Stop and quit", answer)
            assert ask_before_quit(w, self.session) is quits
            assert questions[1].startswith("Couldn't stop the cloud machine. No permission")
        assert self.provider.running


def answer_question(*buttons):
    """Answer the next questions (message boxes) with these buttons; returns
    the questions' texts, as they come."""
    texts, buttons = [], list(buttons)

    def answer():
        box = QApplication.activeModalWidget()
        if not isinstance(box, QMessageBox) or not box.isVisible():
            QTimer.singleShot(10, answer)
            return
        texts.append(f"{box.text()} {box.informativeText()}".replace("\n", " "))
        name = buttons.pop(0)
        (button,) = [b for b in box.buttons() if b.text().replace("&", "") == name]
        button.click()
        if buttons:
            QTimer.singleShot(10, answer)

    QTimer.singleShot(0, answer)
    return texts
