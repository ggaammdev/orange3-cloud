"""The demo workflows. The first (``examples/local-demo.ows``): its data
script, the workflow file itself, the same operations through the API on
the machine's server (with its job system) running in-process over HTTPS,
and opt-in runs on Google Cloud, AWS and Azure. The HIGGS demo
(``examples/higgs-demo.ows``): the workflow file, and an opt-in run on
Google Cloud.
"""

import importlib.util
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from conftest import https_server
from Orange.classification import LogisticRegressionLearner, RandomForestLearner
from Orange.evaluation import Results
from Orange.widgets.tests import base  # not WidgetTest itself: pytest would collect it
from orangecanvas.scheme.readwrite import loads, parse_ows_stream
from test_widget_base import TOKEN

from orangecontrib.cloud.client.api import CloudClient
from orangecontrib.cloud.client.providers.aws import AWSProvider
from orangecontrib.cloud.client.providers.aws_ec2 import BotoClients
from orangecontrib.cloud.client.providers.azure import AzureProvider
from orangecontrib.cloud.client.providers.azure_arm import make_arm
from orangecontrib.cloud.client.providers.azure_vm import vm_states
from orangecontrib.cloud.client.providers.gcp import OwnGCPProvider
from orangecontrib.cloud.client.providers.gcp_compute import NotFoundError
from orangecontrib.cloud.client.session import CloudSession, HandleStore
from orangecontrib.cloud.protocol.jobs import JobKind
from orangecontrib.cloud.protocol.tables import ColumnSpec
from orangecontrib.cloud.server.app import ServerConfig, create_machine_app
from orangecontrib.cloud.settings import CloudSettings, ProviderSettings

ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / "examples" / "local-demo.ows"
HIGGS_WORKFLOW = ROOT / "examples" / "higgs-demo.ows"
HIGGS_URL = "https://archive.ics.uci.edu/ml/machine-learning-databases/00280/HIGGS.csv.gz"
N_ROWS = 3_000


def load_script():
    spec = importlib.util.spec_from_file_location(
        "make_demo_data", ROOT / "scripts" / "make-demo-data.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def demo_csv():
    folder = Path(tempfile.mkdtemp())
    yield load_script().write_demo_data(folder / "demo-data.csv.gz", rows=N_ROWS)
    shutil.rmtree(folder, ignore_errors=True)


def test_workflow_file():
    """The workflow has the demo's widgets and links, reads the data next to
    it, and holds no connection settings."""
    with open(WORKFLOW, "rb") as f:
        desc = parse_ows_stream(f)
    titles = {n.id: n.title for n in desc.nodes}
    assert sorted(titles.values()) == sorted([
        "Cloud File", "Random Forest", "Logistic Regression", "Cloud Test & Score",
        "ROC Analysis", "Cloud Learner", "Cloud Predictions", "Cloud Data Table",
    ])  # fmt: skip
    links = {(titles[ln.source_node_id], titles[ln.sink_node_id]) for ln in desc.links}
    assert {
        ("Cloud File", "Cloud Test & Score"),
        ("Random Forest", "Cloud Test & Score"),
        ("Logistic Regression", "Cloud Test & Score"),
        ("Cloud Test & Score", "ROC Analysis"),
        ("Cloud File", "Cloud Learner"),
        ("Logistic Regression", "Cloud Learner"),
        ("Cloud Learner", "Cloud Predictions"),
        ("Cloud File", "Cloud Predictions"),
        ("Cloud Predictions", "Cloud Data Table"),
    } == links
    props = {n.title: loads(n.data.data, n.data.format) for n in desc.nodes if n.data}
    cloud_file = props["Cloud File"]
    assert cloud_file["local_path"] == "demo-data.csv.gz"  # next to the workflow
    assert [r["name"] for r in cloud_file["column_rows"] if r["role"] == "target"] == ["outcome"]
    assert not cloud_file["table_id"] and not cloud_file["table_machine_id"]
    text = WORKFLOW.read_text().lower()
    for secret in ("token", "certificate", "https://", "gs://", "127.0.0.1", "localhost"):
        assert secret not in text


def test_higgs_workflow_file():
    """The HIGGS workflow reads the data from its URL, with the first column
    as the class, and holds no connection settings."""
    with open(HIGGS_WORKFLOW, "rb") as f:
        desc = parse_ows_stream(f)
    titles = {n.id: n.title for n in desc.nodes}
    links = {(titles[ln.source_node_id], titles[ln.sink_node_id]) for ln in desc.links}
    assert links == {
        ("Cloud File", "Cloud Test & Score"),
        ("Random Forest", "Cloud Test & Score"),
        ("Logistic Regression", "Cloud Test & Score"),
        ("Cloud Test & Score", "ROC Analysis"),
    }
    props = {n.title: loads(n.data.data, n.data.format) for n in desc.nodes if n.data}
    cloud_file = props["Cloud File"]
    assert cloud_file["url"] == cloud_file["columns_source"] == HIGGS_URL
    assert cloud_file["source"] == 1  # the URL
    rows = cloud_file["column_rows"]
    assert [r["name"] for r in rows] == [f"Feature {i}" for i in range(1, 30)]
    assert [r["name"] for r in rows if r["role"] == "target"] == ["Feature 1"]
    assert not cloud_file["table_id"] and not cloud_file["table_machine_id"]
    text = HIGGS_WORKFLOW.read_text().lower()
    for secret in ("token", "certificate", "gs://", "127.0.0.1", "localhost"):
        assert secret not in text


def test_demo_data(demo_csv):
    import pandas as pd

    data = pd.read_csv(demo_csv)
    assert len(data) == N_ROWS
    assert list(data.columns) == [f"x{i}" for i in range(1, 9)] + ["group", "outcome"]
    assert set(data["outcome"]) == {"no", "yes"} and set(data["group"]) == set("ABCD")
    # The same seed gives the same file.
    again = load_script().write_demo_data(demo_csv.with_name("again.csv.gz"), rows=N_ROWS)
    assert pd.read_csv(again).equals(data)


def test_demo_operations_through_the_api(demo_csv, tmp_path):
    """The workflow's operations: load with the target, cross-validate two
    learners and download the results, train, predict all rows and read a
    page of the predictions."""
    config = ServerConfig(
        token=TOKEN, machine="test-machine", data_dir=tmp_path / "machine", worker_threads=2
    )
    with (
        https_server(create_machine_app(config), tmp_path) as (url, cert, _),
        CloudClient(url, TOKEN, cert) as client,
    ):
        job = client.create_table(
            upload_id=client.upload(demo_csv).id,
            name="demo",
            columns=[ColumnSpec(name="outcome", role="target")],
        )
        info = client.wait_for_job(job, timeout=300).tables["table"]
        assert info.n_rows == N_ROWS and info.n_attributes == 9 and info.n_class_vars == 1

        learners = [RandomForestLearner(), LogisticRegressionLearner()]
        job = client.submit_job(
            JobKind.EVALUATE,
            inputs={"data": info.id},
            params={"method": "cross_validation", "folds": 5, "stratified": True},
            objects={"learners": learners},
        )
        result = client.wait_for_job(job, timeout=600)
        ids = [s["id"] for s in result.values["scorers"]]
        auc = [scores[ids.index("AUC")] for scores in result.values["scores"][""]]
        assert all(0.6 < a <= 1 for a in auc), auc
        results = client.get_object(result.objects["results"].id)
        assert isinstance(results, Results) and len(results.actual) == N_ROWS
        assert list(results.learner_names) == ["random forest", "logistic regression"]

        job = client.submit_job(
            JobKind.FIT, inputs={"data": info.id}, objects={"learner": learners[1]}
        )
        model_id = client.wait_for_job(job, timeout=300).objects["model"].id
        job = client.submit_job(
            JobKind.PREDICT,
            inputs={"data": info.id, "models": [model_id]},
            params={"names": ["logistic regression"]},
        )
        result = client.wait_for_job(job, timeout=300)
        predictions = result.tables["table"]
        assert predictions.n_rows == N_ROWS and result.values["scored_rows"] == N_ROWS
        page = client.table_rows(predictions.id, 0, 100)
        assert len(page) == 100
        probs = page.get_column("logistic regression (no)") + page.get_column(
            "logistic regression (yes)"
        )
        np.testing.assert_allclose(probs, 1.0)


# ----------------------------------------------------------------------
# The whole demo on a Small machine in the Google Cloud test project, the AWS
# test account or the Azure test subscription


class RealDemo:
    """The demo workflow on a real cloud, mixed into a ``WidgetTest``."""

    IDLE_TIMEOUT = 180  # seconds

    def spin(self, condition, timeout):
        deadline = time.monotonic() + timeout
        while not condition():
            assert time.monotonic() < deadline, "timed out"
            self.process_events()
            time.sleep(0.05)

    def run_demo(self, provider, page_ready, scheme, save_console):
        """``examples/local-demo.ows`` as Orange's canvas opens it, run on a
        Small machine of ``provider`` started from the Cloud settings dialog
        (once ``page_ready(page)``); then, with Orange still open, the machine
        stops by itself after its idle timeout and the session notices.
        ``save_console(handle, path)`` keeps the machine's console. Stops the
        machine and deletes the uploaded file in the end in any case; returns
        the machine's handle."""
        from orangecontrib.cloud.gui.settings_dialog import CloudSettingsDialog

        if not (ROOT / "examples" / "demo-data.csv.gz").exists():
            load_script().write_demo_data(ROOT / "examples" / "demo-data.csv.gz")
        folder = Path(tempfile.mkdtemp())
        session = CloudSession(store=HandleStore(folder / "machine.json"))
        self._previous, CloudSession._instance = CloudSession._instance, session
        messages = []
        session.messageChanged.connect(lambda m: messages.append((time.monotonic(), m)))
        workflow = load_workflow(WORKFLOW)
        widgets = {n.title: workflow.widget_for_node(n) for n in workflow.nodes}
        dialog = CloudSettingsDialog(None, session)
        page = dialog.pages[provider.id]
        try:
            # The user opens the workflow, then starts the machine in the dialog.
            dialog.show()
            self.spin(lambda: page_ready(page), 180)
            started = time.monotonic()
            dialog.start_machine()
            self.spin(lambda: session.is_connected, 15 * 60)
            connected = time.monotonic()
            print(f"Started in {connected - started:.0f} s")
            print_boot(messages, started)
            handle = session.handle
            save_console(handle, folder / "console.txt")
            self.check_workflow(widgets, connected, scheme)

            # Nothing more to do: the machine stops after its idle timeout,
            # and the heartbeat notices and deletes what is left.
            idle_from = time.monotonic()
            self.spin(lambda: session.state == "disconnected", self.IDLE_TIMEOUT + 10 * 60)
            print(f"Stopped by itself {time.monotonic() - idle_from:.0f} s after the last "
                  f"job: {session.message}")  # fmt: skip
            assert "idle" in session.message
            return handle
        finally:
            if session.handle is not None:  # a failure: keep the machine's log
                save_console(session.handle, folder / "console-end.txt")
            session.stop_machine(wait=True)
            if widgets["Cloud File"].staged_uri:
                provider.delete_stored_files([widgets["Cloud File"].staged_uri])
            page.shutdown()
            dialog.close()
            workflow.clear()
            session.shutdown()
            CloudSession._instance = self._previous

    def check_workflow(self, widgets, connected, scheme):
        """Wait for the workflow's widgets; check their results on all rows,
        and that Cloud File uploaded the file to ``scheme`` storage."""
        roc, table = widgets["ROC Analysis"], widgets["Cloud Data Table"]
        watch = WidgetWatch(widgets, connected)
        self.spin(lambda: watch.done(roc.results is not None and table.n_rows), 30 * 60)
        self.spin(lambda: all(w.task is None for w in cloud_widgets(widgets)), 60)
        print(f"Workflow done {time.monotonic() - connected:.0f} s after connecting")
        errors = {t: [str(m) for m in w.Error.active] for t, w in widgets.items()}
        assert not any(errors.values()), errors
        assert len(roc.results.actual) == 1_000_000 and len(roc.results.learners) == 2
        assert table.n_rows == 1_000_000
        for title in ("Cloud File", "Cloud Test & Score", "Cloud Learner", "Cloud Predictions"):
            print(f"{title}: {widgets[title].cloud_status}")
        uploaded = widgets["Cloud File"].staged_uri
        assert uploaded and uploaded.startswith(scheme)


@pytest.mark.skipif(
    os.environ.get("ORANGE3_CLOUD_GCP_VM_TESTS") != "1"
    or not os.environ.get("ORANGE3_CLOUD_GCP_PROJECT"),
    reason="starts a Small VM in the test project; set ORANGE3_CLOUD_GCP_VM_TESTS=1",
)
class TestRealDemo(RealDemo, base.WidgetTest):
    """``examples/local-demo.ows`` as Orange's canvas opens it, run on a Small
    machine started from the Cloud settings dialog; then the machine stops by
    itself after its idle timeout and nothing is left in the project."""

    def test_demo_on_gcp(self):
        project = os.environ["ORANGE3_CLOUD_GCP_PROJECT"]
        zone = os.environ["ORANGE3_CLOUD_GCP_ZONE"]
        region = zone.rsplit("-", 1)[0]
        gcp = ProviderSettings(project, region, zone, machine_type="e2-standard-4")
        providers = {"gcp": gcp}
        CloudSettings("gcp", self.IDLE_TIMEOUT, providers=providers).save()
        provider = OwnGCPProvider(project=project, zone=zone)

        def save_console(handle, path):
            save_serial_console(provider, handle, path)

        self.run_demo(provider, lambda page: page.setup_ok, "gs://", save_console)
        assert provider.list_machines() == []
        assert labelled_disks(project) == []
        with pytest.raises(NotFoundError):
            provider.api.get_firewall(project, "orange3-cloud-https")


@pytest.mark.skipif(
    os.environ.get("ORANGE3_CLOUD_AWS_VM_TESTS") != "1"
    or not os.environ.get("ORANGE3_CLOUD_AWS_PROFILE"),
    reason="starts a Small machine in the AWS test account; set ORANGE3_CLOUD_AWS_VM_TESTS=1",
)
class TestRealAwsDemo(RealDemo, base.WidgetTest):
    """The same on AWS: the demo on a Small machine, which stops by itself
    after its idle timeout. Also, a machine that stops while Orange is closed
    stays stopped (not running, not deleted) until Orange opens again and
    deletes it and records its cost. Nothing is left in the account
    afterwards."""

    def setUp(self):
        super().setUp()
        self.profile = os.environ["ORANGE3_CLOUD_AWS_PROFILE"]
        self.region = os.environ["ORANGE3_CLOUD_AWS_REGION"]
        aws = ProviderSettings(self.profile, self.region, machine_type="m7i.xlarge")
        providers = {"aws": aws}
        if os.environ.get("ORANGE3_CLOUD_GCP_PROJECT"):  # for the development image
            providers["gcp"] = ProviderSettings(
                os.environ["ORANGE3_CLOUD_GCP_PROJECT"], os.environ["ORANGE3_CLOUD_GCP_REGION"]
            )
        CloudSettings("aws", self.IDLE_TIMEOUT, providers=providers).save()
        self.ec2 = BotoClients()(self.profile, "ec2", self.region)

    def test_demo_on_aws(self):
        provider = AWSProvider()

        def save_console(handle, path):
            ec2 = provider.api(handle.extra["profile"], handle.extra["region"])
            path.write_text(ec2.console(handle.extra["instance_id"]))
            print(f"Console: {path}")

        with StateWatch(ec2_states(self.ec2)) as watch:
            handle = self.run_demo(provider, lambda page: page.setup_ok, "s3://", save_console)
        states = watch.states(handle.extra["instance_id"])
        print("Instance:", states)
        # It powered itself off (the instance was stopping or stopped by
        # itself; stopping may pass between two looks) before the session
        # deleted it.
        stopped = [s for s in states if s[1] in ("stopping", "stopped")]
        assert stopped and stopped[0][2] == "Client.InstanceInitiatedShutdown", states
        self.check_nothing_left(provider, handle)

    def test_stopped_while_orange_closed(self):
        from orangecontrib.cloud.env import collect_environment

        store = HandleStore(Path(tempfile.mkdtemp()) / "machine.json")
        session = CloudSession(store=store)
        handle = session.start_machine("m7i.xlarge", spec=collect_environment(), wait=True)
        instance_id = handle.extra["instance_id"]
        try:
            # Orange quits: the machine keeps running until its idle timeout,
            # then powers itself off, and the instance stays stopped.
            session.shutdown()
            closed = time.monotonic()
            deadline = closed + self.IDLE_TIMEOUT + 10 * 60
            while (instance := describe(self.ec2, instance_id))["State"]["Name"] != "stopped":
                assert time.monotonic() < deadline, instance["State"]
                time.sleep(5)
            print(f"Stopped {time.monotonic() - closed:.0f} s after Orange closed")
            assert instance["StateReason"]["Code"] == "Client.InstanceInitiatedShutdown"
            assert "PublicIpAddress" not in instance  # no address to pay for
            time.sleep(60)
            assert describe(self.ec2, instance_id)["State"]["Name"] == "stopped"

            # Orange opens again: it finds the machine stopped, deletes it and
            # records its cost: running, then stopped (its volume only). The
            # stop time comes from CloudWatch, if its values are in by now.
            session = CloudSession(store=store)
            assert session.reattach(wait=True) is None
            print("Reattach:", session.message)
            assert session.state == "disconnected" and session.message.startswith("The machine")
            assert store.load() is None
            (entry,) = session.costs.entries()
            print("Recorded:", entry)
            assert entry.what == "session" and entry.seconds > 0 and entry.provider == "aws"
            assert 0 < entry.stopped_price_per_hour < entry.price_per_hour
        finally:
            session.stop_machine(wait=True)
            session.shutdown()
        self.check_nothing_left(AWSProvider(), handle)

    def check_nothing_left(self, provider, handle):
        """The instance is terminated, and its volume and the security group are gone."""
        assert provider.list_machines() == []
        assert describe(self.ec2, handle.extra["instance_id"])["State"]["Name"] == "terminated"
        volumes = self.ec2.describe_volumes(
            Filters=[{"Name": "tag:orange3-cloud", "Values": ["true"]}]
        )["Volumes"]
        assert volumes == []
        groups = self.ec2.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": ["orange3-cloud-https"]}]
        )["SecurityGroups"]
        assert groups == []


@pytest.mark.skipif(
    os.environ.get("ORANGE3_CLOUD_AZURE_VM_TESTS") != "1"
    or not os.environ.get("ORANGE3_CLOUD_AZURE_SUBSCRIPTION"),
    reason="starts a Small machine in the Azure test subscription; "
    "set ORANGE3_CLOUD_AZURE_VM_TESTS=1",
)
class TestRealAzureDemo(RealDemo, base.WidgetTest):
    """The same on Azure: the demo on a Small machine, with the data uploaded
    to the subscription's storage account; after its idle timeout the machine
    deallocates its own VM, the session notices and deletes it, and nothing
    of the machine is left. (``test_real_machine_deallocates_itself`` in
    ``test_azure_provider.py`` checks a machine that stops while Orange is
    closed.)"""

    def test_demo_on_azure(self):
        from test_azure_provider import check_nothing_left

        subscription = os.environ["ORANGE3_CLOUD_AZURE_SUBSCRIPTION"]
        region = os.environ["ORANGE3_CLOUD_AZURE_REGION"]
        azure = ProviderSettings(subscription, region, machine_type="Standard_D4s_v6")
        providers = {"azure": azure}
        if os.environ.get("ORANGE3_CLOUD_GCP_PROJECT"):  # for the development image
            providers["gcp"] = ProviderSettings(
                os.environ["ORANGE3_CLOUD_GCP_PROJECT"], os.environ["ORANGE3_CLOUD_GCP_REGION"]
            )
        CloudSettings("azure", self.IDLE_TIMEOUT, providers=providers).save()
        provider = AzureProvider()

        def save_console(handle, path):
            path.write_text(provider.arm(subscription).boot_log(handle.name))
            print(f"Serial log: {path}")

        def page_ready(page):
            return page.setup_ok and (subscription, region) in page._region_sizes

        with StateWatch(azure_states(make_arm(subscription))) as watch:
            handle = self.run_demo(provider, page_ready, "az://", save_console)
        states = watch.states(handle.name)
        print("VM:", states)
        # It deallocated itself (the session never deallocates a machine that
        # stopped by itself; it deletes it) before the session deleted it.
        deleting = next((i for i, s in enumerate(states) if s[1] == "Deleting"), len(states))
        assert any(s[2] in ("deallocating", "deallocated") for s in states[:deleting]), states
        check_nothing_left(provider.arm(subscription), handle)


@pytest.mark.skipif(
    os.environ.get("ORANGE3_CLOUD_GCP_HIGGS") != "1"
    or not os.environ.get("ORANGE3_CLOUD_GCP_PROJECT"),
    reason="runs the HIGGS demo on a Medium VM (~30 min); set ORANGE3_CLOUD_GCP_HIGGS=1",
)
class TestRealHiggs(base.WidgetTest):
    """``examples/higgs-demo.ows`` on a Medium machine, as a user runs it:
    start the machine in the dialog, let the workflow load HIGGS and
    evaluate, download the results for ROC Analysis, then Stop."""

    MACHINE_TYPE = os.environ.get("ORANGE3_CLOUD_HIGGS_MACHINE", "n2-standard-16")

    spin = TestRealDemo.spin

    def test_higgs_on_gcp(self):
        from orangecontrib.cloud.gui.settings_dialog import CloudSettingsDialog

        project = os.environ["ORANGE3_CLOUD_GCP_PROJECT"]
        zone = os.environ["ORANGE3_CLOUD_GCP_ZONE"]
        region = zone.rsplit("-", 1)[0]
        gcp = ProviderSettings(project, region, zone, machine_type=self.MACHINE_TYPE)
        CloudSettings("gcp", 1800, providers={"gcp": gcp}).save()
        session = CloudSession(store=HandleStore(Path(tempfile.mkdtemp()) / "machine.json"))
        self._previous, CloudSession._instance = CloudSession._instance, session
        scheme = load_workflow(HIGGS_WORKFLOW)
        widgets = {n.title: scheme.widget_for_node(n) for n in scheme.nodes}
        dialog = CloudSettingsDialog(None, session)
        try:
            dialog.show()
            self.spin(lambda: dialog.pages["gcp"].setup_ok, 180)
            started = time.monotonic()
            dialog.start_machine()
            self.spin(lambda: session.is_connected, 15 * 60)
            print(f"Started in {time.monotonic() - started:.0f} s")
            self.run_workflow(widgets)
        finally:
            session.stop_machine(wait=True)
            dialog.pages["gcp"].shutdown()
            dialog.close()
            scheme.clear()
            session.shutdown()
            CloudSession._instance = self._previous
        provider = OwnGCPProvider(project=project, zone=zone)
        assert provider.list_machines() == []
        assert labelled_disks(project) == []

    def run_workflow(self, widgets):
        """Wait for the evaluation, download the results, check ROC Analysis."""
        test_and_score, roc = widgets["Cloud Test & Score"], widgets["ROC Analysis"]
        since = time.monotonic()
        watch = WidgetWatch(widgets, since, every=60)
        self.spin(lambda: watch.done(test_and_score.evaluation is not None), 120 * 60)
        evaluated = time.monotonic()
        print(f"Loaded and evaluated {evaluated - since:.0f} s after connecting")
        for title in ("Cloud File", "Cloud Test & Score"):
            print(f"{title}: {widgets[title].cloud_status}")
        size = test_and_score.evaluation.results_info.size_bytes
        assert test_and_score.Information.not_downloaded.is_shown()  # over 100 MB: asks first
        test_and_score.confirm_download = lambda size_bytes: True
        assert test_and_score.download()
        self.spin(lambda: watch.done(roc.results is not None), 60 * 60)
        print(f"Results ({size / 1e6:.0f} MB) downloaded in {time.monotonic() - evaluated:.0f} s")
        assert len(roc.results.actual) == 11_000_000 and len(roc.results.learners) == 2
        print("Scores:", test_and_score.scores(None))


class WidgetWatch:
    """Prints the widgets' state now and then, and fails at the first error."""

    def __init__(self, widgets, since, every=30):
        self.widgets, self.since, self.every = widgets, since, every
        self.last = since

    def done(self, finished):
        errors = {t: [str(m) for m in w.Error.active] for t, w in self.widgets.items()}
        now = time.monotonic()
        if finished or any(errors.values()) or now - self.last > self.every:
            self.last = now
            print(f"-- {now - self.since:.0f} s")
            for title, w in self.widgets.items():
                status = getattr(w, "statusMessage", lambda: "")()
                print(f"  {title}: {status!r} {getattr(w, 'cloud_status', '')!r}")
        assert not any(errors.values()), errors
        return finished


def load_workflow(path):
    """The workflow as Orange's canvas loads it: all widgets created, with the
    workflow's folder as ``basedir``."""
    from Orange.canvas.config import Config
    from orangecanvas import config as canvas_config
    from orangecanvas.registry import WidgetRegistry
    from orangewidget.workflow.widgetsscheme import WidgetsScheme

    canvas_config.set_default(Config())
    registry = WidgetRegistry()
    Config.widget_discovery(registry).run(Config.widgets_entry_points())
    scheme = WidgetsScheme()
    scheme.widget_manager.set_creation_policy(scheme.widget_manager.Immediate)
    scheme.set_runtime_env("basedir", str(path.parent))
    with open(path, "rb") as f:
        scheme.load_from(f, registry=registry)
    return scheme


def cloud_widgets(widgets):
    from orangecontrib.cloud.widgets.base import OWCloudBaseWidget

    return [w for w in widgets.values() if isinstance(w, OWCloudBaseWidget)]


def print_boot(messages, started):
    """The machine's progress messages, with seconds since Start."""
    for at, message in messages:
        print(f"  {at - started:6.1f} s  {message}")


def save_serial_console(provider, handle, path):
    """The VM's serial console so far (boot timings), for the record."""
    project, zone = handle.extra["project"], handle.extra["zone"]
    text, _ = provider.api.serial_output(project, zone, handle.extra["instance"])
    path.write_text(text)
    print(f"Serial console: {path}")


def describe(ec2, instance_id):
    """The EC2 instance (also a terminated one, for about an hour)."""
    (reservation,) = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"]
    return reservation["Instances"][0]


class StateWatch:
    """Records, in a thread, the states the add-on's machines go through,
    without changing anything. ``look()`` returns ``{name: state}``, where a
    state is a tuple (e.g. the EC2 state and why, or Azure's provisioning
    and power states)."""

    def __init__(self, look, every=3.0):
        self.look, self.every = look, every
        self.since = time.monotonic()
        self.seen = {}  # name -> [(seconds, *state)]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc_info):
        self._stop.set()
        self._thread.join(timeout=60)

    def states(self, name):
        """The states the machine went through: (seconds, *state)."""
        return self.seen.get(name, [])

    def _run(self):
        while not self._stop.wait(self.every):
            for name, state in self.look().items():
                history = self.seen.setdefault(name, [])
                if not history or history[-1][1:] != state:
                    history.append((round(time.monotonic() - self.since), *state))


def ec2_states(ec2):
    """``look()`` for :class:`StateWatch`: the add-on's EC2 instances, their
    state and why (``StateReason``)."""
    tagged = [{"Name": "tag:orange3-cloud", "Values": ["true"]}]

    def look():
        reply = ec2.describe_instances(Filters=tagged)
        return {
            i["InstanceId"]: (i["State"]["Name"], (i.get("StateReason") or {}).get("Code"))
            for r in reply["Reservations"]
            for i in r["Instances"]
        }

    return look


def azure_states(arm):
    """``look()`` for :class:`StateWatch`: the add-on's Azure VMs, their
    provisioning state (e.g. ``Deleting``) and power state (e.g.
    ``deallocated``)."""

    def look():
        views = {vm["name"]: arm.vm(vm["name"], instance_view=True) for vm in arm.vms()}
        return {name: vm_states(vm) for name, vm in views.items() if vm is not None}

    return look


def labelled_disks(project):
    """Names of the project's disks labelled ``orange3-cloud=true``."""
    from google.auth.transport.requests import AuthorizedSession

    from orangecontrib.cloud.client.providers.gcp_account import load_credentials

    credentials, _ = load_credentials()
    reply = AuthorizedSession(credentials).get(
        f"https://compute.googleapis.com/compute/v1/projects/{project}/aggregated/disks",
        params={"filter": "labels.orange3-cloud=true", "returnPartialSuccess": "true"},
        timeout=60,
    )
    reply.raise_for_status()
    return [d["name"] for s in reply.json().get("items", {}).values() for d in s.get("disks", [])]
