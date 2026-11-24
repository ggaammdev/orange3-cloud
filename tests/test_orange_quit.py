"""What happens when Orange quits or crashes, with Orange's own main window:
a workflow with a Cloud widget runs in another process, against a machine in
this process.

* Quitting cancels the running job (closing the main window closes the
  widgets, which cancel their jobs); after a crash, the machine cancels it
  once Orange has been silent for the idle timeout.
* Quitting while a machine that costs money runs asks whether to stop it:
  "Stop and quit" stops it and Orange quits, "Keep it running" quits and
  leaves it running, and Cancel keeps Orange open. A free machine (local
  mode), or none, quits without a question.

The other process runs this file: ``python tests/test_orange_quit.py URL
CERT WORKFLOW END``, with END ``quit``, ``crash`` or one of :data:`ANSWERS`.
"""

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from AnyQt.QtCore import QTimer
from conftest import https_server
from test_widget_base import TOKEN, SharedMachineProvider

from orangecontrib.cloud.client.api import CloudClient
from orangecontrib.cloud.protocol.jobs import JobState
from orangecontrib.cloud.server import app as server_app
from orangecontrib.cloud.server.app import ServerConfig, create_machine_app

WORKFLOW = """<?xml version='1.0' encoding='utf-8'?>
<scheme version="2.0" title="Quit test" description="A long job on the cloud machine.">
	<nodes>
		<node id="0" name="Cloud Sleep Test" qualified_name="orangecontrib.cloud.widgets.dev.owcloudsleeptest.OWCloudSleepTest" project_name="orange3-cloud" version="" title="Cloud Sleep Test" position="(100, 100)" />
	</nodes>
	<links />
	<annotations />
	<node_properties>
		<properties node_id="0" format="literal">{'seconds': 300.0, 'steps': 3000, 'fail': False}</properties>
	</node_properties>
</scheme>
"""  # noqa: E501


def run_orange(url: str, cert: str, folder: Path, end: str) -> Any:
    """Run Orange with the workflow in another process until it quits or
    crashes (``end``); returns the finished process."""
    (folder / "cert.pem").write_text(cert)
    (folder / "quit.ows").write_text(WORKFLOW)
    home = folder / "home"  # Orange's and Qt's settings
    home.mkdir()
    env = dict(
        os.environ,
        HOME=str(home),
        CFFIXED_USER_HOME=str(home),
        XDG_CONFIG_HOME=str(home / "config"),
        XDG_DATA_HOME=str(home / "data"),
        XDG_CACHE_HOME=str(home / "cache"),
        QT_QPA_PLATFORM="offscreen",
    )
    args = [sys.executable, __file__, url, str(folder / "cert.pem"), str(folder / "quit.ows"), end]
    return subprocess.run(
        args, env=env, cwd=Path(__file__).parent, capture_output=True, text=True, timeout=120
    )


def started_job(process: Any) -> str:
    """The ID of the job that Orange in ``process`` started."""
    found = re.search(r"^job (\S+)$", process.stdout, re.MULTILINE)
    assert found, f"no job started:\n{process.stdout}\n{process.stderr}"
    return found.group(1)


def machine(folder: Path, idle_timeout: float | None = None):
    """The machine's server in this process, with its job system."""
    config = ServerConfig(
        token=TOKEN,
        data_dir=folder / "machine",
        worker_threads=2,
        idle_timeout_seconds=idle_timeout,
    )
    app = create_machine_app(config)
    return app, https_server(app, folder)


def test_quitting_orange_cancels_the_job(tmp_path):
    app, server = machine(tmp_path)
    with server as (url, cert, _):
        process = run_orange(url, cert, tmp_path, "quit")
        job_id = started_job(process)
        assert process.returncode == 0, process.stderr
        assert "Orange quit" in process.stdout, process.stdout
        # The machine is free (local mode): no question.
        assert "question" not in process.stdout, process.stdout
        with CloudClient(url, TOKEN, cert) as client:
            job = client.job_info(job_id)
    assert job.state == JobState.CANCELLED, job
    assert job.error.message == "The job was cancelled."


def test_machine_cancels_the_job_of_a_crashed_orange(tmp_path, monkeypatch):
    monkeypatch.setattr(server_app, "WATCHDOG_INTERVAL", 0.2)
    # A long idle timeout while Orange starts, so the machine doesn't stop
    # before the job does, and a short one once Orange has crashed.
    app, server = machine(tmp_path, idle_timeout=600)
    with server as (url, cert, _):
        process = run_orange(url, cert, tmp_path, "crash")
        job_id = started_job(process)
        assert process.returncode == -signal.SIGKILL, process.stderr
        jobs = app.state.server.job_manager  # read in this process: no request
        assert jobs.info(job_id).state == JobState.RUNNING
        app.state.config.idle_timeout_seconds = 1.0
        deadline = time.monotonic() + 10
        while not jobs.info(job_id).state.finished:
            assert time.monotonic() < deadline, "the machine didn't cancel the job"
            time.sleep(0.05)
    job = jobs.info(job_id)
    assert job.state == JobState.CANCELLED, job
    assert job.error.message.endswith("Orange hadn't been in touch for 1 second.")


def quit_with_question(tmp_path, end: str) -> Any:
    """Quit Orange in another process with a machine that costs money (or,
    for ``no-machine``, after stopping it); returns the finished process."""
    _, server = machine(tmp_path, idle_timeout=600)
    with server as (url, cert, _):
        process = run_orange(url, cert, tmp_path, end)
    assert process.returncode == 0, f"{process.stdout}\n{process.stderr}"
    assert "Orange quit" in process.stdout, process.stdout
    return process


def test_quit_question_stop_and_quit(tmp_path):
    out = quit_with_question(tmp_path, "stop").stdout
    question = re.search(r"^question: (.*)$", out, re.MULTILINE)
    assert question, out
    assert question.group(1).startswith("A cloud machine is still running. ")
    assert "$1.20/h" in question.group(1) and "so far" in question.group(1)
    assert "stops by itself after 10 minutes without work" in question.group(1)
    assert "progress: Stopping the cloud machine…" in out  # until the stop was done
    assert "machine running: False" in out and "session: disconnected" in out


def test_quit_question_cancel_then_keep_it_running(tmp_path):
    out = quit_with_question(tmp_path, "cancel-keep").stdout
    assert out.count("question: ") == 2, out  # Cancel kept Orange open; quit again
    assert "after Cancel, Orange is open: True" in out
    assert "progress:" not in out
    assert "machine running: True" in out and "session: connected" in out


def test_no_question_without_a_machine(tmp_path):
    out = quit_with_question(tmp_path, "no-machine").stdout
    assert "question" not in out, out


# --------------------------------------------------------------------------
# The other process: Orange


#: The buttons that answer the questions as Orange quits, in order, by END.
ANSWERS = {
    "stop": ["Stop and quit"],
    "cancel-keep": ["Cancel", "Keep it running"],
    "no-machine": [],
}


class SlowStopProvider(SharedMachineProvider):
    """Stopping takes a moment, so the progress shows."""

    def stop(self, handle=None):
        time.sleep(0.5)
        super().stop(handle)


def orange(url: str, cert_file: str, workflow: str, end: str) -> int:
    """Open ``workflow`` in Orange's main window, connected to the machine at
    ``url``, and quit Orange as File > Quit does, or crash: once its widget's
    job runs (``quit``, ``crash``), or answering the questions of
    :data:`ANSWERS` with a machine that costs money."""
    from conftest import SPEC
    from orangecanvas.application.application import CanvasApplication

    from orangecontrib.cloud.client.session import CloudSession

    application = CanvasApplication(sys.argv[:1])
    provider = SlowStopProvider(url, Path(cert_file).read_text())
    if end in ANSWERS:
        provider.price = 1.2
    session = CloudSession(provider)
    CloudSession._instance = session
    session.start_machine(spec=SPEC, wait=True)
    window = open_workflow(workflow)
    if end in ANSWERS:
        quit_and_answer(application, window, session, ANSWERS[end])
    else:
        quit_once_running(application, window, session, end)
    QTimer.singleShot(60_000, lambda: application.exit(3))  # never hang
    status = application.exec()
    print(f"machine running: {provider.running}", flush=True)
    print(f"session: {session.state.value}", flush=True)
    print("Orange quit", flush=True)
    return status


def open_workflow(workflow: str) -> Any:
    """Orange's main window with ``workflow`` and its Cloud widget."""
    from Orange.canvas.config import Config
    from Orange.canvas.mainwindow import MainWindow
    from orangecanvas import config as canvas_config
    from orangecanvas.registry import CategoryDescription, WidgetRegistry
    from orangewidget.workflow.discovery import widget_desc_from_module

    config = Config()
    config.init()
    canvas_config.set_default(config)
    registry = WidgetRegistry()
    registry.register_category(CategoryDescription(name="Cloud"))
    description = widget_desc_from_module("orangecontrib.cloud.widgets.dev.owcloudsleeptest")
    description.category = "Cloud"
    registry.register_widget(description)
    window = MainWindow()
    window.set_widget_registry(registry)
    window.load_scheme(workflow)
    window.show()
    document = window.current_document()
    document.scheme().widget_for_node(document.scheme().nodes[0])  # created now
    document.setModified(False)  # as if saved: no question about saving
    return window


def quit_once_running(application: Any, window: Any, session: Any, end: str) -> None:
    """Start the widget's job, and quit Orange or crash once it runs."""
    submit = session.client.submit_job
    jobs = []

    def submit_and_tell(*args, **kwargs):
        job = submit(*args, **kwargs)
        jobs.append(job.id)
        print(f"job {job.id}", flush=True)
        return job

    session.client.submit_job = submit_and_tell
    document = window.current_document()
    widget = document.scheme().widget_for_node(document.scheme().nodes[0])
    assert widget.run()

    def end_once_running() -> None:
        if not jobs or session.client.job_info(jobs[0]).state != "running":
            return
        timer.stop()
        if end == "crash":
            os.kill(os.getpid(), signal.SIGKILL)
        document.setModified(False)
        window.quit_action.trigger()

    timer = QTimer(application, interval=100, timeout=end_once_running)
    timer.start()


def quit_and_answer(application: Any, window: Any, session: Any, answers: list[str]) -> None:
    """Quit Orange and answer its questions with the buttons ``answers``
    (none: stop the machine first, and expect no question). After Cancel,
    check that Orange is still open, and quit again."""
    from AnyQt.QtWidgets import QApplication, QLabel, QMessageBox

    from orangecontrib.cloud.gui.quit_question import StopProgress

    answers = list(answers)
    if not answers:
        session.stop_machine(wait=True)
    seen = set()

    def after_cancel() -> None:
        print(f"after Cancel, Orange is open: {window.isVisible()}", flush=True)
        window.quit_action.trigger()

    def answer() -> None:
        modal = QApplication.activeModalWidget()
        if isinstance(modal, StopProgress) and modal not in seen:
            seen.add(modal)
            texts = [label.text() for label in modal.findChildren(QLabel)]
            print(f"progress: {texts[0]}", flush=True)
        if not isinstance(modal, QMessageBox) or not modal.isVisible():
            return
        text = f"{modal.text()} {modal.informativeText()}".replace("\n", " ")
        print(f"question: {text}", flush=True)
        if not answers:
            print("unexpected question", flush=True)
            application.exit(4)
            return
        name = answers.pop(0)
        if name == "Cancel":
            QTimer.singleShot(300, after_cancel)
        (button,) = [b for b in modal.buttons() if b.text().replace("&", "") == name]
        button.click()

    QTimer(application, interval=100, timeout=answer).start()
    QTimer.singleShot(500, window.quit_action.trigger)


if __name__ == "__main__":
    sys.exit(orange(*sys.argv[1:]))
