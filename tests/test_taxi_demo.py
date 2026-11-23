"""The NYC taxi demo (``examples/taxi-demo.ows``, and ``taxi-demo-january.ows``
for a Small machine): the workflow files, and an opt-in run on a real cloud,
as a user runs it.

``ORANGE3_CLOUD_TAXI=gcp`` (or ``aws``, ``azure``) starts a machine in the
test project, account or subscription from the Cloud settings dialog: by
default a Medium machine with all of 2025 on Google Cloud, and a Small one
with January on AWS and Azure (``ORANGE3_CLOUD_TAXI_MONTHS`` and
``ORANGE3_CLOUD_TAXI_MACHINE`` change them). It waits for every Cloud widget,
prints the machine's job times and the cost of each step, measures each
viewer again on the derived tables, optionally saves the widgets' windows
(``ORANGE3_CLOUD_TAXI_SCREENSHOTS=<folder>``) and the timings
(``ORANGE3_CLOUD_TAXI_REPORT=<file.json>``), stops the machine, and checks
that nothing of it is left.
"""

import io
import json
import os
import tempfile
import time
from pathlib import Path

import pytest
from Orange.widgets.tests import base  # not WidgetTest itself: pytest would collect it
from orangecanvas.scheme.readwrite import loads, parse_ows_stream
from test_demo import (
    ROOT,
    RealDemo,
    WidgetWatch,
    cloud_widgets,
    describe,
    labelled_disks,
    load_workflow,
)

from orangecontrib.cloud.client.session import CloudSession, HandleStore
from orangecontrib.cloud.settings import CloudSettings, ProviderSettings

WORKFLOW = ROOT / "examples" / "taxi-demo.ows"
JANUARY = ROOT / "examples" / "taxi-demo-january.ows"
TLC = "https://d37ci6vzurychx.cloudfront.net/trip-data/"
YEAR_URL = TLC + "yellow_tripdata_2025-{01..12}.parquet"
CATEGORICAL = {"VendorID", "RatecodeID", "PULocationID", "DOLocationID", "payment_type"}
SKIPPED = {
    "store_and_fwd_flag", "extra", "mta_tax", "tolls_amount", "improvement_surcharge",
    "total_amount", "cbd_congestion_fee",
}  # fmt: skip
LINKS = {
    ("Cloud File", "Feature Constructor"),
    ("Feature Constructor", "Possible Trips"),
    ("Possible Trips", "Tips by Payment Type"),
    ("Possible Trips", "Distance and Fare"),
    ("Possible Trips", "Trip Columns"),
    ("Trip Columns", "Cloud k-Means"),
    ("Cloud k-Means", "Kinds of Trips"),
    ("Possible Trips", "Card Trips"),
    ("Card Trips", "Tips by Hour"),
    ("Card Trips", "Tips by Weekday"),
    ("Card Trips", "Model Columns"),
    ("Model Columns", "Cloud Test & Score"),
    ("Logistic Regression", "Cloud Test & Score"),
    ("Random Forest", "Cloud Test & Score"),
}
VIEWERS = ("Tips by Payment Type", "Distance and Fare", "Tips by Hour", "Tips by Weekday")
CLOUD = os.environ.get("ORANGE3_CLOUD_TAXI", "")


def test_taxi_workflow_file():
    """Cloud File reads the twelve months from the TLC, with the zones and
    codes as categories and the unused columns skipped; Orange's own widgets
    change the data, Cloud widgets look at it; no connection settings."""
    with open(WORKFLOW, "rb") as f:
        desc = parse_ows_stream(f)
    titles = {n.id: n.title for n in desc.nodes}
    links = {(titles[ln.source_node_id], titles[ln.sink_node_id]) for ln in desc.links}
    assert links == LINKS
    props = {n.title: loads(n.data.data, n.data.format) for n in desc.nodes if n.data}
    cloud_file = props["Cloud File"]
    assert cloud_file["url"] == cloud_file["columns_source"] == YEAR_URL
    assert cloud_file["source"] == 1  # the URL
    rows = {r["name"]: r for r in cloud_file["column_rows"]}
    assert {n for n, r in rows.items() if r["role"] == "skip"} == SKIPPED
    assert {n for n, r in rows.items() if r["type"] == "categorical"} >= CATEGORICAL
    assert not cloud_file["table_id"] and not cloud_file["table_machine_id"]
    names = {n.title: n.qualified_name for n in desc.nodes}
    assert names["Feature Constructor"].startswith("Orange.widgets.data.")
    assert names["Tips by Hour"].startswith("orangecontrib.cloud.widgets.")
    text = WORKFLOW.read_text().lower()
    for secret in ("token", "certificate", "gs://", "s3://", "az://", "127.0.0.1", "localhost"):
        assert secret not in text


def test_january_workflow_is_the_same_on_one_month():
    """``taxi-demo-january.ows``, for a Small machine, is the year's workflow
    on January: the same widgets, links and settings, and Cloud File keeps
    its columns for January's file."""
    year, january = (parse_ows_stream(io.BytesIO(p.read_bytes())) for p in (WORKFLOW, JANUARY))
    assert [(n.title, n.qualified_name, n.position) for n in january.nodes] == [
        (n.title, n.qualified_name, n.position) for n in year.nodes
    ]
    assert [(ln.source_node_id, ln.sink_node_id, ln.sink_channel) for ln in january.links] == [
        (ln.source_node_id, ln.sink_node_id, ln.sink_channel) for ln in year.links
    ]
    files = []
    for y, j in zip(year.nodes, january.nodes, strict=True):
        if y.title == "Cloud File":
            files = [loads(n.data.data, n.data.format) for n in (y, j)]
        else:
            assert (j.data.format, j.data.data) == (y.data.format, y.data.data), y.title
    assert files[1]["url"] == files[1]["columns_source"] == months_url(1)
    assert {**files[1], "url": YEAR_URL, "columns_source": YEAR_URL} == files[0]


# ----------------------------------------------------------------------
# On a real cloud


def months_url(months: int) -> str:
    """The URL of the first ``months`` months of 2025."""
    if months == 1:
        return TLC + "yellow_tripdata_2025-01.parquet"
    return TLC + f"yellow_tripdata_2025-{{01..{months:02}}}.parquet"


class Cloud:
    """One cloud's settings for the test, and its check that nothing is left."""

    def __init__(self, name):
        self.name = name
        env = os.environ
        dev_registry = {}
        if env.get("ORANGE3_CLOUD_GCP_PROJECT"):  # the development image
            dev_registry["gcp"] = ProviderSettings(
                env["ORANGE3_CLOUD_GCP_PROJECT"], env["ORANGE3_CLOUD_GCP_REGION"]
            )
        if name == "gcp":
            self.machine_type = env.get("ORANGE3_CLOUD_TAXI_MACHINE", "n2-standard-16")
            zone = env["ORANGE3_CLOUD_GCP_ZONE"]
            settings = ProviderSettings(
                env["ORANGE3_CLOUD_GCP_PROJECT"], zone.rsplit("-", 1)[0], zone,
                machine_type=self.machine_type,
            )  # fmt: skip
        elif name == "aws":
            self.machine_type = env.get("ORANGE3_CLOUD_TAXI_MACHINE", "m7i.xlarge")
            settings = ProviderSettings(
                env["ORANGE3_CLOUD_AWS_PROFILE"], env["ORANGE3_CLOUD_AWS_REGION"],
                machine_type=self.machine_type,
            )  # fmt: skip
        else:
            self.machine_type = env.get("ORANGE3_CLOUD_TAXI_MACHINE", "Standard_D4s_v6")
            settings = ProviderSettings(
                env["ORANGE3_CLOUD_AZURE_SUBSCRIPTION"], env["ORANGE3_CLOUD_AZURE_REGION"],
                machine_type=self.machine_type,
            )  # fmt: skip
        CloudSettings(name, 1800, providers={**dev_registry, name: settings}).save()
        self.months = int(env.get("ORANGE3_CLOUD_TAXI_MONTHS", 12 if name == "gcp" else 1))

    def page_ready(self, page):
        if self.name == "azure":
            return page.setup_ok and bool(page._region_sizes)
        return page.setup_ok

    def check_nothing_left(self, handle):
        if self.name == "gcp":
            from orangecontrib.cloud.client.providers.gcp import OwnGCPProvider
            from orangecontrib.cloud.client.providers.gcp_compute import NotFoundError

            project, zone = handle.extra["project"], handle.extra["zone"]
            provider = OwnGCPProvider(project=project, zone=zone)
            assert provider.list_machines() == []
            assert labelled_disks(project) == []
            with pytest.raises(NotFoundError):
                provider.api.get_firewall(project, "orange3-cloud-https")
        elif self.name == "aws":
            from orangecontrib.cloud.client.providers.aws import AWSProvider
            from orangecontrib.cloud.client.providers.aws_ec2 import BotoClients

            ec2 = BotoClients()(handle.extra["profile"], "ec2", handle.extra["region"])
            assert AWSProvider().list_machines() == []
            assert describe(ec2, handle.extra["instance_id"])["State"]["Name"] == "terminated"
            tagged = [{"Name": "tag:orange3-cloud", "Values": ["true"]}]
            assert ec2.describe_volumes(Filters=tagged)["Volumes"] == []
            groups = [{"Name": "group-name", "Values": ["orange3-cloud-https"]}]
            assert ec2.describe_security_groups(Filters=groups)["SecurityGroups"] == []
        else:
            from test_azure_provider import check_nothing_left

            from orangecontrib.cloud.client.providers.azure import AzureProvider

            check_nothing_left(AzureProvider().arm(handle.extra["subscription"]), handle)


class MemoryLog:
    """The machine's memory in use, and in tables, sampled in a thread from
    ``GET /v1/info`` (``with MemoryLog(handle) as log:``)."""

    def __init__(self, handle, every=20.0):
        import threading

        from orangecontrib.cloud.client.api import CloudClient

        self.client = CloudClient(
            handle.url, handle.token, handle.cert_pem, server_hostname=handle.server_hostname
        )
        self.every, self.since = every, time.monotonic()
        self.samples, self.failures = [], []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        from orangecontrib.cloud.client.api import CloudError

        while not self._stop.wait(self.every):
            try:
                info = self.client.info()
            except CloudError as exc:  # one missed sample; kept for the report
                self.failures.append(str(exc))
                continue
            used = info.memory_total_bytes - info.memory_free_bytes
            self.samples.append((round(time.monotonic() - self.since), round(used / 1e9, 1),
                                 round(info.memory_tables_bytes / 1e9, 1)))  # fmt: skip

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc_info):
        self._stop.set()
        self._thread.join(timeout=60)
        self.client.close()

    def report(self):
        """Peak memory in use and in tables (GB), and every sample."""
        return {
            "memory_peak_gb": max((s[1] for s in self.samples), default=None),
            "tables_peak_gb": max((s[2] for s in self.samples), default=None),
            "memory_samples": self.samples,
            "memory_sample_failures": len(self.failures),
        }


class StepLog:
    """When each Cloud widget finished, and the machine's jobs it ran."""

    def __init__(self, since):
        self.since = since
        self.steps = []

    def watch(self, title, widget):
        original = widget.on_done

        def on_done(outcome):
            jobs = [self.job(r) for r in outcome.job_results]
            self.steps.append(
                {"widget": title, "done_at": round(time.monotonic() - self.since), "jobs": jobs}
            )
            original(outcome)

        widget.on_done = on_done

    @staticmethod
    def job(result):
        """A job's kind and run time, and what the machine said about it."""
        details = ("replayed_steps", "parallel", "threads_per_part", "part_memory_bytes")
        job = {"kind": result.kind, "seconds": round(result.runtime_seconds or 0.0, 1)}
        job.update({k: result.values[k] for k in details if k in result.values})
        if "learners" in result.values:
            job["train_seconds"] = [round(x["train_time"]) for x in result.values["learners"]]
        return job


@pytest.mark.skipif(
    CLOUD not in ("gcp", "aws", "azure"),
    reason="runs the taxi demo on a cloud machine; set ORANGE3_CLOUD_TAXI=gcp, aws or azure",
)
class TestRealTaxi(base.WidgetTest):
    """``examples/taxi-demo.ows`` on a real cloud machine (see the module)."""

    spin = RealDemo.spin

    def test_taxi_on_cloud(self):
        from orangecontrib.cloud.gui.settings_dialog import CloudSettingsDialog

        cloud = Cloud(CLOUD)
        session = CloudSession(store=HandleStore(Path(tempfile.mkdtemp()) / "machine.json"))
        self._previous, CloudSession._instance = CloudSession._instance, session
        workflow = load_workflow(WORKFLOW)
        widgets = {n.title: workflow.widget_for_node(n) for n in workflow.nodes}
        if cloud.months != 12:
            url = months_url(cloud.months)
            widgets["Cloud File"].columns_source = url  # keep the workflow's columns
            widgets["Cloud File"].set_url(url)
        dialog = CloudSettingsDialog(None, session)
        page = dialog.pages[CLOUD]
        handle, started = None, time.monotonic()
        report = {"cloud": CLOUD, "machine_type": cloud.machine_type, "months": cloud.months}
        try:
            dialog.show()
            self.spin(lambda: cloud.page_ready(page), 180)
            started = time.monotonic()
            dialog.start_machine()
            self.spin(lambda: session.is_connected, 15 * 60)
            connected = time.monotonic()
            handle = session.handle
            report["start_seconds"] = round(connected - started)
            report["price_per_hour"] = session.price_per_hour(handle)
            print(f"Started in {report['start_seconds']} s, ${report['price_per_hour']}/h")
            with MemoryLog(handle) as memory:
                try:
                    self.run_workflow(widgets, connected, report)
                    self.measure_viewers(widgets, session, report)
                finally:
                    report.update(memory.report())
            self.screenshots(widgets)
        finally:
            session.stop_machine(wait=True)
            report["machine_seconds"] = round(time.monotonic() - started)
            page.shutdown()
            dialog.close()
            workflow.clear()
            session.shutdown()
            CloudSession._instance = self._previous
            print(json.dumps(report, indent=1))
            if path := os.environ.get("ORANGE3_CLOUD_TAXI_REPORT"):
                Path(path).write_text(json.dumps(report, indent=1))
        cloud.check_nothing_left(handle)

    def run_workflow(self, widgets, connected, report):
        """Wait until every Cloud widget has its result; check them."""
        log, cloud = StepLog(connected), cloud_widgets(widgets)
        for title, widget in widgets.items():
            if widget in cloud:
                log.watch(title, widget)
        ts, kmeans, f = (
            widgets["Cloud Test & Score"],
            widgets["Cloud k-Means"],
            widgets["Cloud File"],
        )

        def finished():
            viewers = all(widgets[t].result is not None for t in VIEWERS)
            return viewers and ts.evaluation is not None and bool(kmeans.results)

        watch = WidgetWatch(widgets, connected, every=60)
        self.spin(lambda: watch.done(finished()), 4 * 3600)
        self.spin(lambda: all(w.task is None for w in cloud_widgets(widgets)), 600)
        report["workflow_seconds"] = round(time.monotonic() - connected)
        report["steps"] = log.steps
        report["rows"] = f.table_info.n_rows
        report["memory_gb"] = round(f.table_info.memory_bytes / 1e9, 2)
        report["scores"] = ts.scores(None)
        report["rows_evaluated"] = ts.evaluation.values["rows"]
        report["statuses"] = {
            t: w.cloud_status for t, w in widgets.items() if hasattr(w, "cloud_status")
        }
        print(f"Workflow done {report['workflow_seconds']} s after connecting")
        for step in log.steps:
            print(step)
        print("Scores:", report["scores"])
        errors = {t: [str(m) for m in w.Error.active] for t, w in widgets.items()}
        assert not any(errors.values()), errors
        expected = {12: 48_722_602, 1: 3_475_226}.get(report["months"])
        assert expected is None or report["rows"] == expected

    def measure_viewers(self, widgets, session, report):
        """Each viewer's statistics once more, one at a time, on the tables
        already derived: the time the machine takes to compute them on all
        rows. (``smoothing`` doesn't change these statistics; another value
        only keeps the machine from answering from its cache.)"""
        from orangecontrib.cloud.data.remote_table import resolve
        from orangecontrib.cloud.protocol.tables import StatsKind, StatsRequest
        from orangecontrib.cloud.widgets.viewer import column_index

        def distribution(w, d):
            return StatsRequest(kind=StatsKind.DISTRIBUTION, columns=[d.index(w.var)],
                                group_by=column_index(d, w.cvar), binning=w.number_of_bins,
                                smoothing=11)  # fmt: skip

        def density(w, d):
            return StatsRequest(kind=StatsKind.HISTOGRAM2D, grid=w.resolution, smoothing=11,
                                columns=[d.index(w.attr_x), d.index(w.attr_y)])  # fmt: skip

        def boxes(w, d):
            return StatsRequest(kind=StatsKind.QUANTILES, columns=[d.index(w.attribute)],
                                group_by=column_index(d, w.group_var), smoothing=11)  # fmt: skip

        requests = dict(zip(VIEWERS, (distribution, density, boxes, boxes), strict=True))
        client, machine_id = session.client, session.handle.machine_id
        report["viewer_seconds"] = {}
        for title in VIEWERS:
            w = widgets[title]
            table_id = resolve(w.data, client, machine_id)
            t0 = time.monotonic()
            client.stats(table_id, requests[title](w, w.data.domain))
            report["viewer_seconds"][title] = round(time.monotonic() - t0, 1)
        print("Viewers:", report["viewer_seconds"])

    def screenshots(self, widgets):
        folder = os.environ.get("ORANGE3_CLOUD_TAXI_SCREENSHOTS")
        if not folder:
            return
        Path(folder).mkdir(parents=True, exist_ok=True)
        for title in (*VIEWERS, "Cloud k-Means", "Cloud Test & Score", "Kinds of Trips",
                      "Cloud File"):  # fmt: skip
            w = widgets[title]
            w.show()
            w.resize(max(w.width(), 900), max(w.height(), 560))
            for _ in range(20):
                self.process_events()
                time.sleep(0.05)
            name = title.lower().replace(" & ", "-").replace(" ", "-")
            w.grab().save(str(Path(folder) / f"taxi-{name}.png"))
            w.hide()


# ----------------------------------------------------------------------
# On a machine in this process, with made-up trips


def write_trips(path, n, month, seed):
    """A month of made-up trips with the columns and types of the TLC's
    2025 files, including impossible ones (negative fares, zero distance)."""
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    rng = np.random.default_rng(seed)
    start = np.datetime64(f"2025-{month:02}-01T00:00:00")
    pickup = start + rng.integers(0, 28 * 86400, n).astype("timedelta64[s]")
    minutes = rng.exponential(15, n)
    minutes[::50] = -3
    distance = rng.exponential(3, n).round(2)
    distance[::40] = 0
    fare = (3 + 2.5 * distance + rng.normal(0, 1, n)).round(2)
    fare[::60] = -10
    payment = rng.choice(np.array([0, 1, 1, 1, 1, 2, 2, 3, 4]), n)
    tip = np.where(payment == 1, fare * rng.uniform(0, 0.3, n) * (rng.random(n) > 0.2), 0).round(2)
    passengers = pa.array(rng.integers(1, 5, n), mask=rng.random(n) < 0.05)
    money = {name: rng.choice(np.array(values), n) for name, values in (
        ("extra", [0, 1, 2.5]), ("mta_tax", [0.5]), ("tolls_amount", [0, 6.94]),
        ("improvement_surcharge", [1.0]), ("congestion_surcharge", [0, 2.5]),
        ("Airport_fee", [0, 1.75]), ("cbd_congestion_fee", [0, 0.75]),
    )}  # fmt: skip
    table = pa.table({
        "VendorID": pa.array(rng.choice(np.array([1, 2, 6, 7]), n), pa.int32()),
        "tpep_pickup_datetime": pickup.astype("datetime64[us]"),
        "tpep_dropoff_datetime": (pickup + (minutes * 60).astype("timedelta64[s]"))
        .astype("datetime64[us]"),
        "passenger_count": passengers,
        "trip_distance": distance,
        "RatecodeID": rng.choice(np.array([1, 1, 1, 2, 5, 99]), n),
        "store_and_fwd_flag": pa.array(rng.choice(np.array(["N", "Y"]), n), pa.large_string()),
        "PULocationID": pa.array(rng.integers(1, 266, n), pa.int32()),
        "DOLocationID": pa.array(rng.integers(1, 266, n), pa.int32()),
        "payment_type": payment,
        "fare_amount": fare,
        "extra": money["extra"], "mta_tax": money["mta_tax"], "tip_amount": tip,
        "tolls_amount": money["tolls_amount"],
        "improvement_surcharge": money["improvement_surcharge"],
        "total_amount": fare + tip, "congestion_surcharge": money["congestion_surcharge"],
        "Airport_fee": money["Airport_fee"], "cbd_congestion_fee": money["cbd_congestion_fee"],
    })  # fmt: skip
    pq.write_table(table, path)


class TestTaxiWorkflow(base.WidgetTest):
    """The workflow on two months of made-up trips, as two files read as one
    table by a machine in this process: every widget gets its result, and
    the steps of Orange's own widgets are replayed on all rows."""

    spin = RealDemo.spin

    def test_workflow_on_made_up_trips(self):
        import functools
        import http.server
        import shutil
        import threading

        from conftest import SPEC, https_server
        from test_widget_base import TOKEN, SharedMachineProvider

        from orangecontrib.cloud.server.app import ServerConfig, create_machine_app

        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        (folder / "web").mkdir()
        for month in (1, 2):
            write_trips(folder / "web" / f"trips-2025-{month:02}.parquet", 1500, month, month)
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=folder / "web")
        handler.log_message = lambda *args: None
        web = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=web.serve_forever, daemon=True).start()
        self.addCleanup(web.shutdown)
        config = ServerConfig(token=TOKEN, data_dir=folder / "machine", worker_threads=2)
        server = https_server(create_machine_app(config), folder)
        url, cert, _ = server.__enter__()
        self.addCleanup(server.__exit__, None, None, None)
        session = CloudSession(SharedMachineProvider(url, cert), store=HandleStore(folder / "m"))
        previous, CloudSession._instance = CloudSession._instance, session
        self.addCleanup(setattr, CloudSession, "_instance", previous)
        self.addCleanup(session.shutdown)
        self.addCleanup(session.disconnect, forget=True, wait=True)
        session.start_machine(spec=SPEC, wait=True)
        workflow = load_workflow(WORKFLOW)
        self.addCleanup(workflow.clear)
        widgets = {n.title: workflow.widget_for_node(n) for n in workflow.nodes}
        source = f"http://127.0.0.1:{web.server_address[1]}/trips-2025-{{01..02}}.parquet"
        widgets["Cloud File"].columns_source = source  # keep the workflow's columns
        widgets["Cloud File"].set_url(source)
        log = StepLog(time.monotonic())
        for title, widget in widgets.items():
            if widget in cloud_widgets(widgets):
                log.watch(title, widget)
        assert widgets["Cloud File"].load()
        ts, kmeans = widgets["Cloud Test & Score"], widgets["Cloud k-Means"]

        def finished():
            viewers = all(widgets[t].result is not None for t in VIEWERS)
            return viewers and ts.evaluation is not None and bool(kmeans.results)

        watch = WidgetWatch(widgets, time.monotonic(), every=600)
        self.spin(lambda: watch.done(finished()), 600)
        self.spin(lambda: all(w.task is None for w in cloud_widgets(widgets)), 120)
        assert widgets["Cloud File"].table_info.n_rows == 3000
        trips = widgets["Tips by Payment Type"].result.info.n_rows
        card = widgets["Tips by Hour"].result.info.n_rows
        assert 0 < card < trips < 3000
        assert widgets["Distance and Fare"].result.info.n_rows == trips
        assert ts.evaluation.values["rows"] == card
        assert set(ts.scores(None)) == {"Logistic Regression", "Random Forest"}
        # The viewers' recorded steps ran once on the machine: Feature
        # Constructor and Select Rows for two viewers, then only Card Trips'
        # Select Rows for the other two.
        jobs = [
            j for step in log.steps if step["widget"] in VIEWERS
            for j in step["jobs"] if j["kind"] == "derive"
        ]  # fmt: skip
        assert sum(j.get("replayed_steps", 0) for j in jobs) == 3, log.steps
