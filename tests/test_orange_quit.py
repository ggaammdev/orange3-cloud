"""What happens to a running job when Orange quits or crashes, with Orange's
own main window: a workflow with a Cloud widget runs in another process,
against a machine in this process. Quitting cancels the job (closing the
main window closes the widgets, which cancel their jobs); after a crash, the
machine cancels it once Orange has been silent for the idle timeout.

The other process runs this file: ``python tests/test_orange_quit.py URL
CERT WORKFLOW quit|crash``.
"""

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from conftest import https_server
from test_widget_base import TOKEN

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


def run_orange(url: str, cert: str, folder: Path, end: str) -> tuple[str, Any]:
    """Run Orange with the workflow in another process until it quits or
    crashes (``end``) while the job runs; returns the job's ID and the
    finished process."""
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
    process = subprocess.run(
        args, env=env, cwd=Path(__file__).parent, capture_output=True, text=True, timeout=120
    )
    found = re.search(r"^job (\S+)$", process.stdout, re.MULTILINE)
    assert found, f"no job started:\n{process.stdout}\n{process.stderr}"
    return found.group(1), process


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
        job_id, process = run_orange(url, cert, tmp_path, "quit")
        assert process.returncode == 0, process.stderr
        assert "Orange quit" in process.stdout, process.stdout
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
        job_id, process = run_orange(url, cert, tmp_path, "crash")
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


# --------------------------------------------------------------------------
# The other process: Orange


def orange(url: str, cert_file: str, workflow: str, end: str) -> int:
    """Open ``workflow`` in Orange's main window, connected to the machine at
    ``url``, start its widget's job, and quit Orange as File > Quit does, or
    crash, once the job runs."""
    from AnyQt.QtCore import QTimer
    from conftest import SPEC
    from Orange.canvas.config import Config
    from Orange.canvas.mainwindow import MainWindow
    from orangecanvas import config as canvas_config
    from orangecanvas.application.application import CanvasApplication
    from orangecanvas.registry import CategoryDescription, WidgetRegistry
    from orangewidget.workflow.discovery import widget_desc_from_module
    from test_widget_base import SharedMachineProvider

    from orangecontrib.cloud.client.session import CloudSession

    application = CanvasApplication(sys.argv[:1])
    config = Config()
    config.init()
    canvas_config.set_default(config)
    registry = WidgetRegistry()
    registry.register_category(CategoryDescription(name="Cloud"))
    description = widget_desc_from_module("orangecontrib.cloud.widgets.dev.owcloudsleeptest")
    description.category = "Cloud"
    registry.register_widget(description)

    session = CloudSession(SharedMachineProvider(url, Path(cert_file).read_text()))
    CloudSession._instance = session
    session.start_machine(spec=SPEC, wait=True)
    submit = session.client.submit_job
    jobs = []

    def submit_and_tell(*args, **kwargs):
        job = submit(*args, **kwargs)
        jobs.append(job.id)
        print(f"job {job.id}", flush=True)
        return job

    session.client.submit_job = submit_and_tell

    window = MainWindow()
    window.set_widget_registry(registry)
    window.load_scheme(workflow)
    window.show()
    document = window.current_document()
    widget = document.scheme().widget_for_node(document.scheme().nodes[0])
    assert widget.run()

    def end_once_running() -> None:
        if not jobs or session.client.job_info(jobs[0]).state != "running":
            return
        timer.stop()
        if end == "crash":
            os.kill(os.getpid(), signal.SIGKILL)
        document.setModified(False)  # as if saved: no question about saving
        window.quit_action.trigger()

    timer = QTimer(interval=100, timeout=end_once_running)
    timer.start()
    QTimer.singleShot(60_000, lambda: application.exit(3))  # never hang
    status = application.exec()
    print("Orange quit", flush=True)
    return status


if __name__ == "__main__":
    sys.exit(orange(*sys.argv[1:]))
