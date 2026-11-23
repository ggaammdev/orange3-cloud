"""Recorded steps replayed on all rows (``derive``): the add-on's pickler,
the machine's stand-ins for Feature Constructor's classes, and Orange's own
Select Columns → Select Rows → Feature Constructor → Continuize on a cloud
table, then Cloud Test & Score, against the same widgets on the full table.
"""

import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest
from conftest import SPEC, https_server
from Orange.classification import LogisticRegressionLearner, TreeLearner
from Orange.data import ContinuousVariable, DiscreteVariable, Domain, Table
from Orange.data import filter as data_filter
from Orange.evaluation import CrossValidation
from Orange.preprocess import Normalize
from Orange.widgets.data.owcontinuize import OWContinuize
from Orange.widgets.data.owfeatureconstructor import (
    ContinuousDescriptor,
    DiscreteDescriptor,
    OWFeatureConstructor,
    construct_variables,
)
from Orange.widgets.data.owselectcolumns import OWSelectAttributes
from Orange.widgets.data.owselectrows import OWSelectRows
from Orange.widgets.tests import base  # not WidgetTest itself: pytest would collect it
from test_remote_table import write_csv
from test_widget_base import TOKEN, SharedMachineProvider

from orangecontrib.cloud.client.api import CloudUnsupportedStepError
from orangecontrib.cloud.client.session import CloudSession, HandleStore
from orangecontrib.cloud.data.remote_table import RemoteTable, is_cloud_table, resolve
from orangecontrib.cloud.protocol.errors import ErrorCode
from orangecontrib.cloud.protocol.pickles import digest, dumps, loads
from orangecontrib.cloud.protocol.tables import MAX_PAGE_ROWS, ColumnRole, ColumnSpec
from orangecontrib.cloud.server import expressions
from orangecontrib.cloud.server.app import ServerConfig, create_machine_app
from orangecontrib.cloud.server.ops.derive import LEARNED_MESSAGE, replay
from orangecontrib.cloud.server.worker import OperationError
from orangecontrib.cloud.widgets.owcloudtestandscore import OWCloudTestAndScore

FEATURE_CONSTRUCTOR = "Orange.widgets.data.owfeatureconstructor"
LEARNED = LEARNED_MESSAGE.format(name="x1")


def small_table():
    x1, x2 = ContinuousVariable("x1"), ContinuousVariable("x2")
    color = DiscreteVariable("color", values=("red", "green", "blue"))
    domain = Domain([x1, x2, color], DiscreteVariable("y", values=("no", "yes")))
    X = np.array([[1.0, 20, 0], [np.nan, 5, 1], [3, 16, 2], [4, 1, 0]])
    return Table.from_numpy(domain, X, np.array([0, 1, 1, 0]))


def constructed(table, *descriptors):
    """``table`` with Feature Constructor's new columns, as the widget makes them."""
    new = construct_variables(descriptors, table)
    domain = Domain(table.domain.attributes + new, table.domain.class_vars, table.domain.metas)
    return table.transform(domain)


# --------------------------------------------------------------------------
# Pickles


def test_filters_pickle():
    """Plain pickle can't handle Select Rows' conditions; the add-on's can."""
    conditions = data_filter.Values(
        [
            data_filter.FilterContinuous(0, data_filter.FilterContinuous.Between, 1, 5),
            data_filter.FilterString("id", data_filter.FilterString.StartsWith, "row1"),
            data_filter.FilterDiscrete(2, [0, 2]),
        ],
        negate=True,
    )
    restored = loads(dumps(("filter", conditions)))[1]
    assert restored.conditions[0].oper is data_filter.FilterContinuous.Between
    assert restored.conditions[1].oper is data_filter.FilterString.StartsWith
    assert restored.negate and restored.conditions[2].values == [0, 2]


def test_feature_constructor_without_qt(monkeypatch):
    """Where Orange's widget module can't be imported (the machine has no Qt),
    Feature Constructor's classes unpickle as the machine's stand-ins, which
    compute the same values and pickle back as Orange's own classes."""
    table = small_table()
    out = constructed(
        table,
        ContinuousDescriptor("sum", "x1 + x2", 3),
        DiscreteDescriptor("big", "yes if x2 > 10 else no", ("no", "yes"), False),
        DiscreteDescriptor("tone", "color", (), False),  # values from the data
    )
    data = dumps(out.domain)
    original = sys.modules[FEATURE_CONSTRUCTOR]
    monkeypatch.setitem(sys.modules, FEATURE_CONSTRUCTOR, None)  # import fails
    domain = loads(data)
    funcs = [var.compute_value for var in domain.attributes[3:]]
    assert all(type(f) is expressions.FeatureFunc for f in funcs)
    assert type(funcs[2].cast) is expressions.MappingTransformCast
    np.testing.assert_array_equal(table.transform(domain).X, out.X)
    back = dumps(domain)
    monkeypatch.setitem(sys.modules, FEATURE_CONSTRUCTOR, original)
    assert loads(back) == out.domain
    assert type(loads(back).attributes[3].compute_value).__module__ == FEATURE_CONSTRUCTOR


# --------------------------------------------------------------------------
# Checks, on a local table


def test_keys_ignore_colors_that_widgets_drew():
    """A viewer caches the colors of the columns it draws on the variables
    themselves; the same steps keep their key afterwards (the pickle sent
    with them may change)."""
    from Orange.widgets.utils import colorpalettes  # noqa: F401 - gives variables colors

    var = DiscreteVariable("payment", values=("1", "2", "3"))
    steps = (("transform", Domain([var, ContinuousVariable("fare")])),)
    plain, key = dumps(steps), digest(steps)
    assert var.colors is not None  # a viewer draws the column
    assert dumps(steps) != plain and digest(steps) == key


def test_replay_and_checks():
    table = small_table()
    # Feature Constructor with a list of values, then a filter.
    out = constructed(
        table, DiscreteDescriptor("big", "yes if x2 > 10 else no", ("no", "yes"), False)
    )
    row_filter = data_filter.FilterContinuous("x2", data_filter.FilterContinuous.Greater, 4)
    steps = [("transform", out.domain), ("filter", data_filter.Values([row_filter]))]
    result = replay(table, steps)
    np.testing.assert_array_equal(result.get_column("big"), [1, 0, 1])
    # Without a list, the values came from the placeholder row: refused.
    nan = np.full((1, 3), np.nan)
    placeholder = Table.from_numpy(table.domain, nan, np.full(1, np.nan))
    guessed = constructed(
        placeholder, DiscreteDescriptor("big", "'yes' if x2 > 10 else 'no'", (), False)
    )
    with pytest.raises(
        OperationError, match=r"'big' gives values that aren't in its list \(no\)"
    ) as e:
        replay(table, [("transform", guessed.domain)])
    assert e.value.code == ErrorCode.UNSUPPORTED_STEP
    # A list, but the expression gives a value outside it on some rows.
    outside = constructed(
        placeholder, DiscreteDescriptor("big", "2 if x2 > 10 else no", ("no", "yes"), False)
    )
    with pytest.raises(OperationError, match="'big' gives values that aren't in its list"):
        replay(table, [("transform", outside.domain)])
    # Normalizing learned its numbers from the placeholder: refused.
    normalized = Normalize()(small_table())
    with pytest.raises(OperationError, match=LEARNED.replace("(", r"\(")):
        replay(table, [("transform", normalized.domain)])


# --------------------------------------------------------------------------
# Orange's widgets on a cloud table, then Cloud Test & Score


def test_tables_kept_for_waiting_jobs_are_released():
    """A table kept after a shared step for a waiting job is dropped once the
    jobs that wanted it are done, and deleted tables are forgotten."""
    from orangecontrib.cloud.server.ops import derive
    from orangecontrib.cloud.server.worker import JobContext, WorkerStore

    data = small_table()
    x1, x2, _ = data.domain.attributes
    shared = [
        ("transform", Domain([x1, x2], data.domain.class_var)),
        ("filter", data_filter.IsDefined()),
    ]
    store = WorkerStore()
    store.tables["t0"] = data

    def run(job_id, last, key):
        steps = [*shared, ("transform", Domain([last], data.domain.class_var))]
        params = {"prefix_keys": ["k1", "k2"], "steps_key": key}
        ctx = JobContext(job_id, "derive", {"data": "t0"}, params, {"steps": steps}, store)
        return ctx, derive.derive_operation(ctx)

    derive._WAITING["j-b"] = ("t0", ["k1", "k2", "kB"])  # waits while j-a replays
    try:
        ctx_a, result = run("j-a", x1, "kA")
    finally:
        del derive._WAITING["j-b"]
    kept = ctx_a.tables["steps-2"].id
    assert result["replayed_steps"] == 3 and kept in store.tables
    ctx_b, result = run("j-b", x2, "kB")
    assert result["replayed_steps"] == 1  # from the kept table
    assert ctx_b.released_tables == [kept] and kept not in store.tables
    assert ("t0", "k2") not in derive._DERIVED
    del store.tables[ctx_a.tables["table"].id]  # the user deleted it
    run("j-c", x2, "kB")
    assert ("t0", "kA") not in derive._DERIVED and ("t0", "kB") in derive._DERIVED
    derive._DERIVED.clear()


class CloudTableTest(base.WidgetTest):
    """A machine in a thread, a session connected to it, and the cloud table
    ``self.data`` of ``N_ROWS`` rows (``self.full`` on this computer)."""

    __test__ = False  # only its subclasses run
    N_ROWS = 3_000

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        folder = Path(tempfile.mkdtemp())
        cls.tear_down_stack.callback(shutil.rmtree, folder, ignore_errors=True)
        config = ServerConfig(token=TOKEN, data_dir=folder / "machine", worker_threads=2)
        url, cert, _ = cls.tear_down_stack.enter_context(
            https_server(create_machine_app(config), folder)
        )
        cls.provider = SharedMachineProvider(url, cert)
        cls.csv = folder / "data.csv"
        write_csv(cls.csv, cls.N_ROWS)

    def setUp(self):
        super().setUp()
        self.state = Path(tempfile.mkdtemp())
        self.session = CloudSession(self.provider, store=HandleStore(self.state / "m.json"))
        self._previous, CloudSession._instance = CloudSession._instance, self.session
        self.session.start_machine(spec=SPEC, wait=True)
        self.process_events()
        self.client = self.session.client
        if "table_id" not in vars(type(self)):  # load once per class
            job = self.client.create_table(
                upload_id=self.client.upload(self.csv).id,
                name="data",
                columns=[ColumnSpec(name="label", role=ColumnRole.TARGET)],
            )
            type(self).table_id = self.client.wait_for_job(job, timeout=300).tables["table"].id
        self.machine_id = self.session.handle.machine_id
        self.data = RemoteTable.open(self.client, self.table_id, self.machine_id)
        # The whole table on this computer, when it fits in one page.
        small = self.N_ROWS <= MAX_PAGE_ROWS
        self.full = self.client.table_rows(self.table_id, 0, self.N_ROWS) if small else None

    def tearDown(self):
        super().tearDown()
        self.session.disconnect(forget=True, wait=True)
        self.session.shutdown()
        CloudSession._instance = self._previous
        shutil.rmtree(self.state, ignore_errors=True)

    # Orange's own widgets

    def select_columns(self, data, drop="x3"):
        w = self.create_widget(OWSelectAttributes)
        self.send_signal(w.Inputs.data, data, widget=w)
        keep = [v for v in w.used_attrs if v.name != drop]
        del w.used_attrs[:]
        w.used_attrs.extend(keep)
        w.commit.now()
        return self.get_output(w.Outputs.data, widget=w)

    def select_rows(self, data):
        w = self.create_widget(OWSelectRows)
        self.send_signal(w.Inputs.data, data, widget=w)
        w.remove_all_rows()
        w.add_row(data.domain["x2"], 4, ("8",))  # x2 > 8
        w.add_row(data.domain["id"], 11, ("row1",))  # id doesn't start with "row1"
        w.conditions_changed()
        w.commit.now()
        return self.get_output(w.Outputs.matching_data, widget=w)

    def feature_constructor(self, data, *descriptors):
        w = self.create_widget(OWFeatureConstructor)
        self.send_signal(w.Inputs.data, data, widget=w)
        w.setDescriptors(list(descriptors))
        w.apply()
        self.wait_until_finished(w)
        assert not w.Error.active, [str(m) for m in w.Error.active]
        return self.get_output(w.Outputs.data, widget=w)

    def continuize(self, data, normalize=False):
        settings = {"cont_var_hints": {"": 1}, "__version__": 3} if normalize else {}
        w = self.create_widget(OWContinuize, stored_settings=settings)
        self.send_signal(w.Inputs.data, data, widget=w)
        w.commit.now()
        return self.get_output(w.Outputs.data, widget=w)


class TestRecordedSteps(CloudTableTest):
    __test__ = True

    def chain(self, data):
        data = self.select_rows(self.select_columns(data))
        data = self.feature_constructor(
            data,
            ContinuousDescriptor("sum", "x1 + 2 * x2", 3),
            DiscreteDescriptor("big", "yes if x1 > 12 else no", ("no", "yes"), False),
        )
        return self.continuize(data)

    def test_steps_on_all_rows(self):
        cloud = self.chain(self.data)
        local = self.chain(self.full)
        assert is_cloud_table(cloud) and len(cloud) == 1
        assert [kind for kind, _ in cloud.steps] == [
            "transform",
            "filter",
            "transform",
            "transform",
        ]
        assert 0 < len(local) < self.N_ROWS

        table_id = resolve(cloud, self.client, self.machine_id)
        assert table_id != self.table_id
        assert resolve(cloud, self.client, self.machine_id) == table_id  # kept, not replayed
        info = self.client.table_info(table_id)
        assert info.parent_id == self.table_id and info.n_rows == len(local)
        assert info.steps[1] == "filter: Values" and info.steps_key
        derived = self.client.table_rows(table_id, 0, self.N_ROWS)
        assert derived.domain == local.domain
        np.testing.assert_array_equal(derived.X, local.X)
        np.testing.assert_array_equal(derived.Y, local.Y)
        np.testing.assert_array_equal(derived.metas, local.metas)

        # Cloud Test & Score computes on exactly these rows and columns.
        learners = [LogisticRegressionLearner(), TreeLearner(max_depth=5)]
        w = self.create_widget(OWCloudTestAndScore, stored_settings={"n_folds": 2})
        self.send_signal(w.Inputs.train_data, cloud, widget=w)
        for i, learner in enumerate(learners):
            self.send_signal(w.Inputs.learner, learner, i, widget=w)
        self.wait_until_finished(w, timeout=300000)
        assert not w.Error.active, [str(m) for m in w.Error.active]
        assert w.evaluation.values["rows"] == len(local)
        results = self.get_output(w.Outputs.evaluations_results, widget=w)
        reference = CrossValidation(k=5, stratified=True, random_state=42)(local, learners)
        np.testing.assert_array_equal(results.row_indices, reference.row_indices)
        np.testing.assert_array_equal(results.actual, reference.actual)
        np.testing.assert_allclose(results.probabilities, reference.probabilities)

    def test_branches_replay_their_shared_steps_once(self):
        """Two branches after the same Feature Constructor and Select Rows:
        the second one starts from the table the first one derived."""
        shared = self.select_rows(
            self.feature_constructor(self.data, ContinuousDescriptor("sum", "x1 + x2", 3))
        )
        ((steps, columns),) = shared.ref.segments()
        first = self.client.derive(self.table_id, steps, columns=columns)
        first = self.client.wait_for_job(first, timeout=300)
        assert first.values["replayed_steps"] == 2
        branch = self.select_columns(shared)
        ((steps, columns),) = branch.ref.segments()
        job = self.client.derive(self.table_id, steps, columns=columns)
        result = self.client.wait_for_job(job, timeout=300)
        assert result.values["replayed_steps"] == 1
        local = self.select_columns(
            self.select_rows(
                self.feature_constructor(self.full, ContinuousDescriptor("sum", "x1 + x2", 3))
            )
        )
        derived = self.client.table_rows(result.tables["table"].id, 0, self.N_ROWS)
        assert derived.domain == local.domain
        np.testing.assert_array_equal(derived.X, local.X)

    def test_learned_steps_are_refused(self):
        columns = self.select_columns(self.data)
        for normalized in (self.continuize(columns, normalize=True), Normalize()(columns)):
            assert is_cloud_table(normalized)
            with pytest.raises(CloudUnsupportedStepError, match=LEARNED.replace("(", r"\(")):
                resolve(normalized, self.client, self.machine_id)
        # Cloud Test & Score shows the message.
        w = self.create_widget(OWCloudTestAndScore)
        self.send_signal(w.Inputs.train_data, normalized, widget=w)
        self.send_signal(w.Inputs.learner, TreeLearner(), 0, widget=w)
        self.wait_until_finished(w, timeout=60000)
        assert "Cloud Preprocess" in str(w.Error.cloud_error)

    def test_categorical_values_are_checked(self):
        columns = self.select_columns(self.data)
        # Without a list of values: the list came from the placeholder.
        guessed = self.feature_constructor(
            columns, DiscreteDescriptor("big", "'yes' if x1 > 12 else 'no'", (), False)
        )
        with pytest.raises(CloudUnsupportedStepError, match="'big' gives values that aren't"):
            resolve(guessed, self.client, self.machine_id)
        # With a list: checked on all rows and replayed.
        listed = self.feature_constructor(
            columns, DiscreteDescriptor("big", "yes if x1 > 12 else no", ("no", "yes"), False)
        )
        table_id = resolve(listed, self.client, self.machine_id)
        big = self.client.table_rows(table_id, 0, self.N_ROWS).get_column("big")
        x1 = self.full.get_column("x1")
        np.testing.assert_array_equal(big, np.where(x1 > 12, 1.0, 0.0))
        # With a list that misses a value the expression gives on some rows.
        outside = self.feature_constructor(
            columns, DiscreteDescriptor("big", "2 if x1 > 12 else no", ("no", "yes"), False)
        )
        with pytest.raises(CloudUnsupportedStepError, match=r"its list \(no, yes\)"):
            resolve(outside, self.client, self.machine_id)
