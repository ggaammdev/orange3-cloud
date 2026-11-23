"""Machine prices, the size picker with its suggestion, and recorded costs."""

import datetime
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest
from Orange.widgets.tests import base  # not WidgetTest itself: pytest would collect it

from orangecontrib.cloud.client.costs import (
    STOP_MARGIN,
    CostLog,
    cost_of,
    format_price,
    format_usd,
    machine_seconds,
    session_entry,
)
from orangecontrib.cloud.client.providers import aws_prices, azure_prices
from orangecontrib.cloud.client.providers.aws import AWSProvider
from orangecontrib.cloud.client.providers.aws_vm import MACHINE_TYPES as AWS_TYPES
from orangecontrib.cloud.client.providers.azure import AzureProvider
from orangecontrib.cloud.client.providers.azure_vm import MACHINE_TYPES as AZURE_TYPES
from orangecontrib.cloud.client.providers.base import (
    ClusterProvider,
    MachineHandle,
    MachineState,
    MachineStatus,
)
from orangecontrib.cloud.client.providers.gcp import OwnGCPProvider
from orangecontrib.cloud.client.providers.gcp_prices import load_prices
from orangecontrib.cloud.client.providers.gcp_vm import MACHINE_TYPES
from orangecontrib.cloud.client.providers.prices import load_price_file
from orangecontrib.cloud.client.providers.sizes import DISK_MAX_GB, DISK_MIN_GB, GIB
from orangecontrib.cloud.client.session import CloudSession, HandleStore
from orangecontrib.cloud.protocol.jobs import JobResult
from orangecontrib.cloud.settings import CloudSettings, ProviderSettings


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ORANGE3_CLOUD_SETTINGS_FILE", str(tmp_path / "settings.ini"))
    return tmp_path / "settings.ini"


def test_bundled_prices():
    prices = load_prices()
    datetime.date.fromisoformat(prices.date)
    # e2-standard-4 in us-central1: 4 vCPUs and 16 GiB (Google's list price ~$0.134/h)
    machine = prices.machine_per_hour("e2-standard-4", "us-central1")
    assert 0.10 < machine < 0.20
    full = prices.per_hour("e2-standard-4", "us-central1", 100)
    assert full == pytest.approx(machine + 100 * 0.1 / 730 + prices.external_ip_per_hour, 0.2)
    assert load_price_file("gcp").date == prices.date  # one loader for every cloud
    for t in MACHINE_TYPES:  # every size has a price in the default region
        assert prices.per_hour(t, "us-central1", 100) > 0
    assert prices.per_hour("e2-standard-4", "nowhere1", 100) is None
    assert prices.per_hour("n1-standard-1", "us-central1", 100) is None


def test_formatting():
    assert format_usd(0) == "$0"
    assert format_usd(0.00001) == "under $0.0001"
    assert format_usd(0.00123) == "$0.0012"
    assert format_usd(0.345) == "$0.34"
    assert format_usd(12.5) == "$12.50"
    assert format_price(0.7769) == "$0.78/h"
    assert format_price(0.0512) == "$0.051/h"
    assert format_price(None) == "price unknown"


def test_provider_prices(settings_file):
    gcp = ProviderSettings(zone="us-central1-a", disk_gb=200)
    CloudSettings(provider="gcp", providers={"gcp": gcp}).save()
    provider = OwnGCPProvider(project="p", api=object())
    prices = load_prices()
    assert provider.price_per_hour("n2-standard-16") == prices.per_hour(
        "n2-standard-16", "us-central1", 200
    )
    handle = MachineHandle(
        "gcp", "m", "vm", "", "", "", machine_type="e2-standard-4", extra={"zone": "europe-west1-b"}
    )
    assert provider.machine_price_per_hour(handle) == prices.per_hour(
        "e2-standard-4", "europe-west1", 100
    )
    handle.extra["price_per_hour"] = "0.5"  # recorded at start
    assert provider.machine_price_per_hour(handle) == 0.5
    assert provider._target(None).disk_gb == 200  # the dialog's disk size


def test_aws_prices(settings_file):
    prices = aws_prices.load_prices()
    datetime.date.fromisoformat(prices.date)
    assert prices.source.startswith("AWS Price List")
    # m7i.xlarge in us-east-1 (AWS's list price ~$0.20/h), a 100 GB gp3
    # volume and the public IPv4 address.
    instance = aws_prices.instance_per_hour("m7i.xlarge", "us-east-1")
    assert 0.15 < instance < 0.30
    rates = prices.region("us-east-1")
    assert 0 < rates["public_ipv4_hour"] < 0.02
    assert aws_prices.per_hour("m7i.xlarge", "us-east-1", 100) == pytest.approx(
        instance + 100 * rates["gp3_gib_month"] / 730 + rates["public_ipv4_hour"]
    )
    for t in AWS_TYPES:  # every size has a price in the default region
        assert aws_prices.per_hour(t.name, "us-east-1", 100) > 0
    assert aws_prices.per_hour("m7i.xlarge", "nowhere-1", 100) is None
    assert aws_prices.per_hour("t2.micro", "us-east-1", 100) is None

    # The provider: the chosen region and disk; a machine's price when it started.
    aws = ProviderSettings("p", "eu-west-1", disk_gb=200)
    CloudSettings(provider="aws", providers={"aws": aws}).save()
    provider = AWSProvider()
    assert provider.prices_date() == prices.date
    assert provider.price_per_hour("m7i.4xlarge") == aws_prices.per_hour(
        "m7i.4xlarge", "eu-west-1", 200
    )
    handle = MachineHandle(
        "aws", "m", "vm", "", "", "", machine_type="m7i.xlarge", extra={"region": "us-west-2"}
    )
    assert provider.machine_price_per_hour(handle) == aws_prices.per_hour(
        "m7i.xlarge", "us-west-2", 100
    )
    handle.extra["price_per_hour"] = "0.5"
    assert provider.machine_price_per_hour(handle) == 0.5


def test_azure_prices(settings_file):
    prices = azure_prices.load_prices()
    datetime.date.fromisoformat(prices.date)
    assert prices.source.startswith("Azure Retail Prices API")
    # Premium SSD is billed per tier: the smallest that holds the disk.
    assert [azure_prices.disk_tier(gb) for gb in (DISK_MIN_GB, 64, 100, 129, DISK_MAX_GB)] == [
        "P6", "P6", "P10", "P15", "P40",
    ]  # fmt: skip
    assert azure_prices.disk_tier(5000) is None
    # Standard_D4s_v6 in eastus (Azure's list price ~$0.20/h), a 100 GB
    # Premium SSD (P10) and the Standard public IP address.
    vm = azure_prices.vm_per_hour("Standard_D4s_v6", "eastus")
    assert 0.15 < vm < 0.30
    rates = prices.region("eastus")
    assert 0 < rates["public_ip_hour"] < 0.02
    assert azure_prices.per_hour("Standard_D4s_v6", "eastus", 100) == pytest.approx(
        vm + rates["premium_ssd_p10_month"] / 730 + rates["public_ip_hour"]
    )
    for t in AZURE_TYPES:  # every size has a price in the default region
        assert azure_prices.per_hour(t.name, "eastus", 100) > 0
    assert azure_prices.per_hour("Standard_D4s_v6", "nowhere", 100) is None
    assert azure_prices.per_hour("Standard_B1s", "eastus", 100) is None

    # The provider: the chosen region and disk; a machine's price when it started.
    azure = ProviderSettings("sub", "westeurope", disk_gb=200)
    CloudSettings(provider="azure", providers={"azure": azure}).save()
    provider = AzureProvider()
    assert provider.prices_date() == prices.date
    assert provider.price_per_hour("Standard_E16s_v6") == azure_prices.per_hour(
        "Standard_E16s_v6", "westeurope", 200
    )
    handle = MachineHandle(
        "azure", "m", "vm", "", "", "", machine_type="Standard_D4s_v6",
        extra={"region": "eastus", "disk_gb": "300"},
    )  # fmt: skip
    assert provider.machine_price_per_hour(handle) == azure_prices.per_hour(
        "Standard_D4s_v6", "eastus", 300
    )
    handle.extra["price_per_hour"] = "0.5"
    assert provider.machine_price_per_hour(handle) == 0.5


def test_cost_log_and_session(tmp_path):
    class PricedProvider:
        id = "priced"

        def machine_price_per_hour(self, handle):
            return 7.2  # $0.002 per second

        def stopped_price_per_hour(self, handle):
            return 0.36  # e.g. its disk: $0.0001 per second

    created = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
    handle = MachineHandle(
        "priced",
        "m1",
        "vm-1",
        "",
        "",
        "",
        machine_type="e2-standard-4",
        created_at=created.isoformat(),
    )
    session = CloudSession(PricedProvider(), store=HandleStore(tmp_path / "machine.json"))
    try:
        session._handle = handle
        results = [JobResult(job_id="j1", kind="evaluate", runtime_seconds=10.0)]
        assert session.record_jobs(handle, results) == pytest.approx(0.02)
        assert session.jobs_cost() == pytest.approx(0.02)
        assert session.session_cost() == pytest.approx(7.2, rel=0.01)
        # It ran for 30 minutes, then was stopped for 30 minutes.
        stopped = created + datetime.timedelta(minutes=30)
        session.machine_costs.record_session(handle, stopped)
    finally:
        session.shutdown()
    job, whole = CostLog(lambda: tmp_path).entries()
    assert (job.what, job.job_kind, job.machine, job.cost) == ("job", "evaluate", "vm-1", 0.02)
    assert (whole.what, whole.seconds, whole.stopped_price_per_hour) == ("session", 1800, 0.36)
    assert whole.stopped_seconds == pytest.approx(1800, abs=60)
    assert whole.cost == pytest.approx(3.6 + 0.18, abs=0.01) and not whole.approximate
    assert datetime.datetime.fromisoformat(whole.stopped_at) == stopped.replace(microsecond=0)
    # Costs are recorded per provider; a line recorded before stopped times
    # (and one with a field unknown here) still counts.
    assert job.provider == whole.provider == "priced"
    old = {"what": "session", "machine": "vm-0", "machine_type": None, "seconds": 60}
    old.update(price_per_hour=3.6, cost=0.06, provider="priced", later=1)
    with open(tmp_path / "costs.jsonl", "a") as f:
        f.write(json.dumps(old) + "\n")
    assert CostLog(lambda: tmp_path).entries()[-1].stopped_seconds == 0
    assert CostLog(lambda: tmp_path).totals() == {"priced": pytest.approx(3.84, abs=0.01)}
    assert (tmp_path / "costs.jsonl").stat().st_mode & 0o777 == 0o600
    assert cost_of(None, 1.0) is None and cost_of(3600, None) is None
    assert machine_seconds(handle) == pytest.approx(3600, abs=60)


class StoppedCloud(ClusterProvider):
    """A cloud whose machines stopped by themselves while Orange was closed:
    $7.20 an hour running, $0.36 stopped."""

    id = "stopped"
    finds_leftovers = True

    def __init__(self, stopped_at, *handles):
        self.when, self.machines = stopped_at, {h.name: h for h in handles}

    def start(self, spec, machine_type=None, **kwargs):
        raise NotImplementedError

    def status(self, handle=None):
        if handle.name not in self.machines:
            return MachineStatus(MachineState.NOT_FOUND)
        return MachineStatus(MachineState.STOPPED, "The machine stopped.")

    def stop(self, handle=None):
        pass

    def delete(self, handle=None):
        self.machines.pop(handle.name)

    def list_machines(self):
        return list(self.machines.values())

    def delete_leftovers(self, keep=None):
        names = list(self.machines)
        self.machines.clear()
        return names

    def price_per_hour(self, machine_type=None):
        return 7.2

    def stopped_at(self, handle):
        return self.when

    def stopped_price_per_hour(self, handle):
        return 0.36


def stopped_handle(name, created, **extra):
    return MachineHandle(
        "stopped", name, name, "https://x", "token", "cert", machine_type="small",
        created_at=created.isoformat(), extra=extra,
    )  # fmt: skip


def test_machine_stopped_while_orange_closed(tmp_path):
    """Orange opens again and finds its machine stopped: the session deletes
    it and records its running time at the full price and its stopped time
    at the stopped price. Leftovers deleted from the dialog too."""
    now = datetime.datetime.now(datetime.timezone.utc)
    hours = lambda n: datetime.timedelta(hours=n)  # noqa: E731
    handle = stopped_handle("vm-1", now - hours(3), max_lifetime="43200")
    store = HandleStore(tmp_path / "machine.json")
    store.save(handle)
    cloud = StoppedCloud(now - hours(2), handle)
    session = CloudSession(store=store, resolve_provider=lambda provider_id: cloud)
    try:
        assert session.reattach(wait=True) is None
        assert cloud.machines == {} and store.load() is None
        cloud.machines["vm-2"] = stopped_handle("vm-2", now - hours(4))
        assert session.delete_leftovers(wait=True) == ["vm-2"]
    finally:
        session.shutdown()
    mine, leftover = session.costs.entries()
    # 1 hour running and 2 stopped; 2 hours running and 2 stopped.
    assert (mine.seconds, mine.price_per_hour, mine.stopped_price_per_hour) == (3600, 7.2, 0.36)
    assert mine.stopped_seconds == pytest.approx(7200, abs=60) and not mine.approximate
    assert mine.cost == pytest.approx(7.2 + 0.72, abs=0.01)
    assert (leftover.machine, leftover.seconds, leftover.cost) == (
        "vm-2", 7200, pytest.approx(14.4 + 0.72, abs=0.01)
    )  # fmt: skip
    assert session.costs.totals() == {"stopped": pytest.approx(23.04, abs=0.02)}


def test_unknown_stop_time():
    """Without a stop time from the cloud, a machine counts as running until
    it is deleted, but no longer than its maximum lifetime; the entry says
    it's approximate."""
    now = datetime.datetime(2026, 11, 23, 12, tzinfo=datetime.timezone.utc)
    created = now - datetime.timedelta(hours=20)
    handle = stopped_handle("vm-1", created, max_lifetime="36000", boot_seconds="100")
    entry = session_entry(handle, (7.2, 0.36), None, now=now)
    running = 10 * 3600 + 100 + STOP_MARGIN
    assert (entry.seconds, entry.stopped_seconds) == (running, 20 * 3600 - running)
    assert entry.approximate and entry.cost == pytest.approx(running * 0.002 + 9.6 * 0.36, 0.01)
    # Without a maximum lifetime: until it was deleted.
    del handle.extra["max_lifetime"]
    entry = session_entry(handle, (7.2, 0.36), None, now=now)
    assert (entry.seconds, entry.stopped_seconds, entry.approximate) == (72000, 0, True)
    # Stopped at a known time, at an unknown stopped price: the full price.
    entry = session_entry(handle, (7.2, None), now - datetime.timedelta(hours=1), now=now)
    assert entry.cost == pytest.approx(144) and entry.approximate
    # Stopped by Orange: no stopped time; a free machine is never approximate.
    entry = session_entry(handle, (7.2, 0.36), None, running=True, now=now)
    assert (entry.seconds, entry.stopped_at, entry.approximate) == (72000, "", False)
    assert not session_entry(handle, (0.0, None), None, now=now).approximate
    assert session_entry(handle, (None, 0.36), None, now=now) is None


def test_size_picker_suggests_and_warns(settings_file):
    from AnyQt.QtWidgets import QApplication

    from orangecontrib.cloud.gui.machine_size import MachineSizePicker, SizeNeed, size_hints

    _app = QApplication.instance() or QApplication([])

    settings = ProviderSettings()
    saved = []
    picker = MachineSizePicker(
        OwnGCPProvider(), settings, lambda: saved.append(settings.machine_type)
    )
    owner = object()
    try:
        picker.set_region("us-central1", {t.name for t in MACHINE_TYPES} - {"c3-standard-88"})
        assert picker.combo.count() == len(MACHINE_TYPES) - 1  # not offered: hidden
        texts = [picker.combo.itemText(i) for i in range(picker.combo.count())]
        assert all("/h" in t and "$" in t for t in texts), texts
        assert picker.machine_type == "n2-standard-16"
        assert not picker.suggestion_label.isVisibleTo(None)

        # A large file in Cloud File: 90-130 GiB of memory, a 300 GB disk.
        size_hints().set(owner, SizeNeed(90 * GIB, 130 * GIB, 300, "big.csv (Cloud File)"))
        suggested, _ = picker.suggestion()
        assert suggested.name == "n2-highmem-32"  # the least memory with room for 130 GiB
        marked = [
            picker.combo.itemData(i)
            for i in range(picker.combo.count())
            if "fits your data" in picker.combo.itemText(i)
        ]
        assert marked == [suggested.name]
        text = picker.suggestion_label.text()
        assert "big.csv (Cloud File)" in text and "too small" in text
        assert picker.use_button.isEnabled()

        picker.use_suggestion()
        assert settings.machine_type == suggested.name and saved[-1] == suggested.name
        assert settings.disk_gb == 300
        assert "too small" not in picker.suggestion_label.text()
        assert not picker.use_button.isEnabled()
    finally:
        size_hints().set(owner, None)
    assert not picker.use_button.isVisibleTo(None)


class TestCloudSizeSuggestion(base.WidgetTest):
    """With AWS or Azure chosen in the Cloud settings, a large file in Cloud
    File makes the size picker suggest a size of that cloud that fits."""

    def test_cloud_file_suggests_an_aws_size(self):
        self.check_suggestion(AWSProvider(), "us-east-1", AWS_TYPES, "r7i.8xlarge")

    def test_cloud_file_suggests_an_azure_size(self):
        self.check_suggestion(AzureProvider(), "eastus", AZURE_TYPES, "Standard_E32s_v6")

    def check_suggestion(self, provider, region, types, expected):
        from test_cloud_file import write_csv

        from orangecontrib.cloud.gui.machine_size import MachineSizePicker, size_hints
        from orangecontrib.cloud.widgets.owcloudfile import OWCloudFile

        folder = Path(tempfile.mkdtemp())
        chosen = ProviderSettings("account", region)
        CloudSettings(provider=provider.id, providers={provider.id: chosen}).save()
        session = CloudSession(store=HandleStore(folder / "machine.json"))  # no machine
        previous, CloudSession._instance = CloudSession._instance, session
        try:
            big = folder / "big.csv"
            write_csv(big, 2000)
            with open(big, "r+b") as f:  # sparse: 10 GB on paper, 2000 real rows
                f.truncate(10 * GIB)
            w = self.create_widget(OWCloudFile)
            w.set_local_path(str(big))
            assert w.size_choices() == (provider.label, list(types))
            # About 134-201 GiB of memory: "Large, high memory" has 256.
            suggested = w.estimate.suggested_machine_type(types)
            assert suggested.name == expected
            assert suggested.memory_gb * GIB >= w.estimate.needed_high

            picker = MachineSizePicker(provider, chosen, lambda: None)
            picker.set_region(region, {t.name for t in types})
            assert picker.suggestion()[0] == suggested
            item = picker.combo.itemText(picker.combo.findData(suggested.name))
            assert item.endswith("fits your data") and "/h" in item
            assert "big.csv (Cloud File) probably needs" in picker.suggestion_label.text()
            assert "The chosen size is probably too small." in picker.suggestion_label.text()
            w.onDeleteWidget()
            assert size_hints().largest() is None
        finally:
            CloudSession._instance = previous
            session.shutdown()
            shutil.rmtree(folder, ignore_errors=True)


# --------------------------------------------------------------------------
# On Google Cloud, AWS and Azure (opt-in; never in CI)


@pytest.mark.skipif(
    os.environ.get("ORANGE3_CLOUD_GCP_VM_TESTS") != "1"
    or not os.environ.get("ORANGE3_CLOUD_GCP_PROJECT"),
    reason="starts a Small VM in the test project; set ORANGE3_CLOUD_GCP_VM_TESTS=1",
)
class TestRealCost(base.WidgetTest):
    """The dialog lists the zone's sizes with prices; a Test & Score run on a
    Small machine shows its cost; Stop records the session's cost."""

    def test_test_and_score_cost(self):
        project = os.environ["ORANGE3_CLOUD_GCP_PROJECT"]
        zone = os.environ["ORANGE3_CLOUD_GCP_ZONE"]
        region = zone.rsplit("-", 1)[0]
        gcp = ProviderSettings(project, region, zone, machine_type="e2-standard-4")
        CloudSettings(provider="gcp", providers={"gcp": gcp}).save()
        check_real_cost(
            self,
            OwnGCPProvider(project=project, zone=zone),
            lambda page: page.setup_ok and (project, zone) in page._zone_types,
            "$0.1",
        )


@pytest.mark.skipif(
    os.environ.get("ORANGE3_CLOUD_AWS_VM_TESTS") != "1"
    or not os.environ.get("ORANGE3_CLOUD_AWS_PROFILE"),
    reason="starts a Small machine in the AWS test account; set ORANGE3_CLOUD_AWS_VM_TESTS=1",
)
class TestRealAwsCost(base.WidgetTest):
    """The same on AWS: the region's sizes with prices, a Test & Score run's
    cost on a Small machine, and the session's cost after Stop."""

    def test_test_and_score_cost(self):
        profile = os.environ["ORANGE3_CLOUD_AWS_PROFILE"]
        region = os.environ["ORANGE3_CLOUD_AWS_REGION"]
        aws = ProviderSettings(profile, region, machine_type="m7i.xlarge")
        CloudSettings(provider="aws", providers={"aws": aws}).save()
        check_real_cost(
            self,
            AWSProvider(),
            lambda page: page.setup_ok and (profile, region) in page._region_types,
            "$0.2",
        )


@pytest.mark.skipif(
    os.environ.get("ORANGE3_CLOUD_AZURE_VM_TESTS") != "1"
    or not os.environ.get("ORANGE3_CLOUD_AZURE_SUBSCRIPTION"),
    reason="starts a Small machine in the Azure test subscription; "
    "set ORANGE3_CLOUD_AZURE_VM_TESTS=1",
)
class TestRealAzureCost(base.WidgetTest):
    """The same on Azure: the sizes the region offers to the subscription,
    with prices, a Test & Score run's cost on a Small machine, and the
    session's cost after Stop."""

    def test_test_and_score_cost(self):
        subscription = os.environ["ORANGE3_CLOUD_AZURE_SUBSCRIPTION"]
        region = os.environ["ORANGE3_CLOUD_AZURE_REGION"]
        azure = ProviderSettings(subscription, region, machine_type="Standard_D4s_v6")
        CloudSettings(provider="azure", providers={"azure": azure}).save()
        check_real_cost(
            self,
            AzureProvider(),
            lambda page: page.setup_ok and (subscription, region) in page._region_sizes,
            "$0.2",
        )


def spin(test, condition, timeout):
    """Process events until ``condition()`` holds."""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        test.process_events()
        time.sleep(0.05)


def check_real_cost(test, provider, page_ready, price):
    """With ``provider`` chosen in the settings: the dialog lists its sizes
    with prices once ``page_ready(page)``; a Small machine started from the
    dialog costs about ``price`` (its start) an hour; Cloud File and Cloud
    Test & Score on the demo data show the job's cost; Stop records the
    session's cost and leaves no machine."""
    from Orange.classification import LogisticRegressionLearner

    from orangecontrib.cloud.gui.settings_dialog import CloudSettingsDialog
    from orangecontrib.cloud.widgets.owcloudfile import OWCloudFile
    from orangecontrib.cloud.widgets.owcloudtestandscore import OWCloudTestAndScore

    chosen = CloudSettings.load().of(provider.id)
    folder = Path(tempfile.mkdtemp())
    session = CloudSession(store=HandleStore(folder / "machine.json"))
    previous, CloudSession._instance = CloudSession._instance, session
    dialog = CloudSettingsDialog(None, session)
    page = dialog.pages[provider.id]
    uploaded = []
    try:
        dialog.show()
        spin(test, lambda: page_ready(page), 180)
        picker = page.size_picker
        texts = [picker.combo.itemText(i) for i in range(picker.combo.count())]
        print("\n".join(texts), picker.note_label.text(), sep="\n")
        assert picker.machine_type == chosen.machine_type and all("$" in t for t in texts)

        dialog.start_machine()
        spin(test, lambda: session.is_connected, 15 * 60)
        spin(test, lambda: dialog.detail_labels["price"].text() != "—", 10)
        print("Price:", dialog.detail_labels["price"].text())
        assert dialog.detail_labels["price"].text().startswith(price)

        csv = load_demo_script().write_demo_data(folder / "demo-data.csv.gz", rows=20_000)
        w = test.create_widget(OWCloudFile, stored_settings=demo_file_settings(csv))
        assert w.load(force=True) or w.task is not None
        test.wait_until_finished(w, timeout=600000)
        assert not w.Error.active, [str(m) for m in w.Error.active]
        uploaded.append(w.staged_uri)
        data = test.get_output(w.Outputs.data, widget=w)

        ts = test.create_widget(OWCloudTestAndScore)
        test.send_signal(ts.Inputs.train_data, data, widget=ts)
        test.send_signal(ts.Inputs.learner, LogisticRegressionLearner(), 0, widget=ts)
        test.wait_until_finished(ts, timeout=600000)
        assert not ts.Error.active, [str(m) for m in ts.Error.active]
        print("Test & Score:", ts.cloud_status_label.text())
        assert ts.cloud_cost and " Cost " in ts.cloud_status and "/h." in ts.cloud_status
        dialog._update_cost()
        print("Cost so far:", dialog.detail_labels["cost"].text())
        assert "(jobs: " in dialog.detail_labels["cost"].text()
    finally:
        session.stop_machine(wait=True)
        if any(uploaded):
            provider.delete_stored_files(uploaded)
        page.shutdown()
        dialog.close()
        session.shutdown()
        CloudSession._instance = previous
    entries = session.costs.entries()
    print(*entries, sep="\n")
    assert [e.what for e in entries][-1] == "session" and entries[-1].cost > 0
    assert {e.provider for e in entries} == {provider.id}
    assert provider.list_machines() == []


def load_demo_script():
    """``scripts/make-demo-data.py`` as a module."""
    import importlib.util

    path = Path(__file__).parent.parent / "scripts" / "make-demo-data.py"
    spec = importlib.util.spec_from_file_location("make_demo_data", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def demo_file_settings(path):
    """Cloud File's settings for the demo data, with ``outcome`` as the target."""
    numeric = {"type": "numeric", "role": "feature", "values": "", "numeric_ok": True}
    rows = [dict(numeric, name=f"x{i}") for i in range(1, 9)]
    categorical = {"type": "categorical", "role": "feature", "numeric_ok": False}
    rows += [
        dict(categorical, name="group", values="A, B, C, D"),
        dict(categorical, name="outcome", values="no, yes", role="target"),
    ]
    inferred = [dict(r, role="feature") for r in rows]
    return {
        "source": 0, "local_path": str(path), "columns_source": str(path),
        "column_rows": rows, "inferred_rows": inferred,
    }  # fmt: skip
