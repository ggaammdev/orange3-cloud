"""RemoteTable: the placeholder table for cloud data.

The unit tests cover the overridden ``Table`` methods without a machine. The
widget tests open a table loaded on the machine's server (run in-process over
HTTPS) and drive Orange's own widgets with it. ``TestOrangeWidgetsMillion``
does the same on 1M rows: ``pytest -m slow tests/test_remote_table.py``.
"""

import copy
import pickle
import shutil
import tempfile
import time
from pathlib import Path

import numpy as np
import pytest
from conftest import https_server
from Orange.data import (
    ContinuousVariable,
    DiscreteVariable,
    Domain,
    StringVariable,
    Table,
)
from Orange.data import filter as data_filter
from Orange.preprocess import Continuize
from Orange.widgets.tests import base  # not WidgetTest itself: pytest would collect it

from orangecontrib.cloud.client.api import CloudClient, CloudNotFoundError
from orangecontrib.cloud.data.remote_table import (
    CloudRef,
    LocalDataError,
    RemoteTable,
    TableUnavailableError,
    is_cloud_table,
    resolve,
)
from orangecontrib.cloud.protocol.tables import ColumnRole, ColumnSpec
from orangecontrib.cloud.server.app import ServerConfig, create_machine_app

TOKEN = "remote-table-token"


def make_domain():
    return Domain(
        [ContinuousVariable(f"x{i}") for i in (1, 2, 3)]
        + [DiscreteVariable("color", values=("blue", "green", "red"))],
        DiscreteVariable("label", values=("no", "yes")),
        [StringVariable("id")],
    )


def make_table(n_rows=1_000_000):
    return RemoteTable.placeholder(make_domain(), CloudRef("t1", "data", n_rows, "m1"))


def is_placeholder(table):
    return (
        len(table) == 1
        and np.isnan(table.X).all()
        and np.isnan(table.Y).all()
        and all(
            v == var.Unknown or v is None
            for v, var in zip(table.metas[0], table.domain.metas, strict=True)
        )
    )


class FakeClient:
    def __init__(self, known=("t1",)):
        self.known = set(known)

    def table_info(self, table_id):
        if table_id not in self.known:
            raise CloudNotFoundError(None, "Unknown table")
        return table_id


# --------------------------------------------------------------------------
# The overridden methods


def test_placeholder():
    table = make_table()
    assert is_cloud_table(table)
    assert is_placeholder(table)
    assert table.domain == make_domain()
    assert table.ref.n_rows == 1_000_000
    assert table.name == "data (1,000,000 rows, in the cloud)"
    assert table.steps == ()


def test_transform_is_recorded():
    table = make_table()
    domain = Domain(table.domain.attributes[:2], table.domain.class_var)
    out = table.transform(domain)
    assert is_cloud_table(out) and is_placeholder(out)
    assert out.domain == domain
    assert [kind for kind, _ in out.steps] == ["transform"]
    assert out.steps[0][1] is domain
    assert out.ref.n_rows == 1_000_000  # transforms keep the row count
    assert table.steps == ()  # the source is unchanged

    # Computed columns (here one-hot coding of "color") are recorded too.
    continuized = Continuize()(table)
    assert is_cloud_table(continuized)
    assert [kind for kind, _ in continuized.steps] == ["transform"]
    assert "color=red" in continuized.domain


def test_same_domain_and_copies_keep_the_reference():
    table = make_table()
    for out in (table.copy(), RemoteTable(table), table.transform(table.domain)):
        assert is_cloud_table(out) and is_placeholder(out)
        assert out.ref == table.ref


def test_row_subsets_and_unknown_columns_give_local_data():
    table = make_table()
    selected = DiscreteVariable("Selected", values=("No", "Yes"))  # no compute_value
    domain = Domain(table.domain.attributes, table.domain.class_vars, [selected])
    for out in (
        table[[0]],
        table[:1],
        Table.from_table(table.domain, table, [0]),
        table.transform(domain),
        data_filter.Random(0.5)(table),  # Data Sampler and the like
    ):
        assert type(out) is Table, out
        assert not is_cloud_table(out)


def test_values_written_here_give_local_data():
    """Randomize writes its shuffled rows into a copy: the machine can't replay that."""
    table = make_table()
    out = table.copy()
    with out.unlocked():
        out.X[0, 0] = 1.0
    assert not is_cloud_table(out)
    assert is_cloud_table(table)


def test_filters_are_recorded_and_the_placeholder_row_stays():
    table = make_table()
    x2 = table.domain["x2"]
    continuous = data_filter.FilterContinuous(x2, data_filter.FilterContinuous.Greater, 10)
    values = data_filter.Values([continuous])
    filters = [
        data_filter.IsDefined(),
        data_filter.HasClass(),
        data_filter.SameValue(table.domain["color"], "red"),
        values,
    ]
    for f in filters:
        out = f(table)
        assert is_cloud_table(out) and is_placeholder(out), f
        assert [kind for kind, _ in out.steps] == ["filter"]
        assert out.ref.n_rows is None
        assert out.name == "data (in the cloud)"

    # Select Rows sets negate on its filter after using it: the recorded
    # step must keep the filter as it was.
    matching = values(table)
    values.negate = True
    unmatched = values(table)
    assert matching.steps[0][1].negate is False
    assert unmatched.steps[0][1].negate is True
    assert matching.steps[0][1] is not values


def test_pickle_keeps_the_reference():
    table = make_table()
    out = table.transform(Domain(table.domain.attributes[:2], table.domain.class_var))
    restored = pickle.loads(pickle.dumps(out))
    assert is_cloud_table(restored) and is_placeholder(restored)
    assert restored.ref.table_id == "t1"
    assert [kind for kind, _ in restored.steps] == ["transform"]
    assert copy.deepcopy(out).ref.table_id == "t1"


def test_resolve():
    table = make_table()
    assert table.resolve(FakeClient()) == "t1"
    assert resolve(table, FakeClient(), machine_id="m1") == "t1"
    with pytest.raises(LocalDataError, match="local data, not a cloud table"):
        resolve(Table.from_table(table.domain, table, [0]), FakeClient())
    with pytest.raises(TableUnavailableError, match="no longer available. Run Cloud File again"):
        table.resolve(FakeClient(known=()))
    with pytest.raises(TableUnavailableError):
        table.resolve(FakeClient(), machine_id="another machine")


# --------------------------------------------------------------------------
# Orange's own widgets on a table loaded on the machine


def write_csv(path, n_rows):
    rng = np.random.default_rng(0)
    x = rng.normal(10, 5, size=(n_rows, 3)).round(4).astype(str)
    x[::20, 0] = ""  # 5% missing in x1
    color = rng.choice(np.array(["red", "green", "blue"]), n_rows)
    label = rng.choice(np.array(["no", "yes"]), n_rows)
    ids = np.char.add("row", np.arange(n_rows).astype(str))
    rows = np.column_stack([x, color, label, ids])
    with open(path, "w") as f:
        f.write("x1,x2,x3,color,label,id\n")
        np.savetxt(f, rows, fmt="%s", delimiter=",")


class TestOrangeWidgets(base.WidgetTest):
    N_ROWS = 2_000

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        folder = Path(tempfile.mkdtemp())
        cls.tear_down_stack.callback(shutil.rmtree, folder, ignore_errors=True)
        config = ServerConfig(token=TOKEN, data_dir=folder / "machine", worker_threads=2)
        url, cert, _ = cls.tear_down_stack.enter_context(
            https_server(create_machine_app(config), folder)
        )
        cls.client = cls.tear_down_stack.enter_context(CloudClient(url, TOKEN, cert))
        csv = folder / "data.csv"
        write_csv(csv, cls.N_ROWS)
        started = time.monotonic()
        upload = cls.client.upload(csv)
        job = cls.client.create_table(
            upload_id=upload.id,
            name="data",
            columns=[ColumnSpec(name="label", role=ColumnRole.TARGET)],
        )
        result = cls.client.wait_for_job(job, timeout=600, max_poll_interval=0.5)
        cls.table_id = result.tables["table"].id
        cls.load_seconds = time.monotonic() - started
        cls.data = RemoteTable.open(cls.client, cls.table_id, machine_id="m1")

    def test_row_count_and_domain(self):
        data = self.data
        assert is_placeholder(data)
        assert data.ref.n_rows == self.N_ROWS
        assert data.name == f"data ({self.N_ROWS:,} rows, in the cloud)"
        assert data.domain == self.client.table_domain(self.table_id)
        assert [v.name for v in data.domain.attributes] == ["x1", "x2", "x3", "color"]
        assert data.domain.class_var.name == "label"
        assert [v.name for v in data.domain.metas] == ["id"]
        assert data.resolve(self.client, "m1") == self.table_id

    def select_columns(self, data):
        from Orange.widgets.data.owselectcolumns import OWSelectAttributes

        w = self.create_widget(OWSelectAttributes)
        self.send_signal(w.Inputs.data, data, widget=w)
        keep = [v for v in w.used_attrs if v.name in ("x1", "x2")]
        del w.used_attrs[:]
        w.used_attrs.extend(keep)
        w.commit.now()
        return self.get_output(w.Outputs.data, widget=w)

    def test_select_columns_and_select_rows_record_steps(self):
        from Orange.widgets.data.owselectrows import OWSelectRows

        columns = self.select_columns(self.data)
        assert is_cloud_table(columns) and is_placeholder(columns)
        assert [v.name for v in columns.domain.attributes] == ["x1", "x2"]
        assert [kind for kind, _ in columns.steps] == ["transform"]
        assert columns.ref.n_rows == self.N_ROWS

        w = self.create_widget(OWSelectRows)
        self.send_signal(w.Inputs.data, columns, widget=w)
        w.remove_all_rows()
        w.add_row(columns.domain["x2"], 4, ("10",))  # x2 > 10
        w.conditions_changed()
        w.commit.now()
        matching = self.get_output(w.Outputs.matching_data, widget=w)
        unmatched = self.get_output(w.Outputs.unmatched_data, widget=w)
        for out, negate in ((matching, False), (unmatched, True)):
            assert is_cloud_table(out) and is_placeholder(out)
            assert [kind for kind, _ in out.steps] == ["transform", "filter"]
            assert out.steps[1][1].negate is negate
        # The annotated output fills in a column locally: local data.
        assert not is_cloud_table(self.get_output(w.Outputs.annotated_data, widget=w))
        # The machine replays the steps on all rows.
        derived = self.client.table_info(matching.resolve(self.client, "m1"))
        assert derived.parent_id == self.table_id and 0 < derived.n_rows < self.N_ROWS

    def test_learner_computes_nothing(self):
        from Orange.widgets.model.owlogisticregression import OWLogisticRegression

        w = self.create_widget(OWLogisticRegression)
        self.send_signal(w.Inputs.data, self.data, widget=w)
        self.wait_until_finished(w)
        assert self.get_output(w.Outputs.model, widget=w) is None
        assert w.Error.active, "the learner should refuse the placeholder"

    def test_test_and_score_computes_nothing(self):
        from Orange.classification import LogisticRegressionLearner
        from Orange.widgets.evaluate.owtestandscore import OWTestAndScore

        w = self.create_widget(OWTestAndScore)
        self.send_signal(w.Inputs.train_data, self.data, widget=w)
        self.send_signal(w.Inputs.learner, LogisticRegressionLearner(), 0, widget=w)
        self.wait_until_finished(w, timeout=20000)
        assert self.get_output(w.Outputs.evaluations_results, widget=w) is None
        assert w.Error.active, "Test & Score should refuse the placeholder"

    def test_scatter_plot_computes_nothing(self):
        from Orange.widgets.visualize.owscatterplot import OWScatterPlot

        w = self.create_widget(OWScatterPlot)
        self.send_signal(w.Inputs.data, self.data, widget=w)
        self.wait_until_finished(w)
        messages = [w.Warning.missing_coords]
        if hasattr(w.Information, "missing_coords"):
            messages.append(w.Information.missing_coords)
        assert any(m.is_shown() for m in messages)
        assert not np.any(w.valid_data)


@pytest.mark.slow
class TestOrangeWidgetsMillion(TestOrangeWidgets):
    N_ROWS = 1_000_000


def test_unavailable_after_the_table_is_deleted(tmp_path):
    config = ServerConfig(token=TOKEN, data_dir=tmp_path / "machine", worker_threads=1)
    with (
        https_server(create_machine_app(config), tmp_path) as (url, cert, _),
        CloudClient(url, TOKEN, cert) as client,
    ):
        csv = tmp_path / "small.csv"
        write_csv(csv, 50)
        job = client.create_table(upload_id=client.upload(csv).id, name="small")
        table_id = client.wait_for_job(job, max_poll_interval=0.2).tables["table"].id
        data = RemoteTable.open(client, table_id)
        assert data.ref.n_rows == 50
        client.delete_table(table_id)
        with pytest.raises(TableUnavailableError):
            data.resolve(client)
        with pytest.raises(TableUnavailableError):
            RemoteTable.open(client, table_id)
