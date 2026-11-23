"""Cloud Discretize: its recipe does what Orange's Discretize does (every
method, a general preset and settings per column, time columns), and on a
small cloud table the widget gives Orange's Discretize's columns and rows on
the full table, with cut points found on the machine; the step replays,
also under Orange's steps after it.
"""

import numpy as np
import pytest
from Orange.data import ContinuousVariable, DiscreteVariable, Domain, Table, TimeVariable
from Orange.widgets.data.owdiscretize import Methods, OWDiscretize, VarHint
from Orange.widgets.tests import base  # not WidgetTest itself: pytest would collect it
from test_derive import CloudTableTest

from orangecontrib.cloud.client.api import CloudUnsupportedStepError
from orangecontrib.cloud.data.remote_table import LOCAL_DATA, resolve
from orangecontrib.cloud.protocol.errors import ErrorCode
from orangecontrib.cloud.protocol.pickles import dumps, loads
from orangecontrib.cloud.server.ops.derive import check_fit_memory, replay
from orangecontrib.cloud.server.worker import JobCancelled, OperationError
from orangecontrib.cloud.widgets.owclouddiscretize import OWCloudDiscretize, make_recipe

#: Orange's Discretize settings: each method as the general preset, and a mix
#: of a preset and methods for single columns (by name and "is time").
PRESETS = [
    VarHint(Methods.EqualFreq, (3,)),
    VarHint(Methods.EqualWidth, (4,)),
    VarHint(Methods.MDL, ()),
    VarHint(Methods.Binning, (5,)),
    VarHint(Methods.FixedWidth, ("2.5",)),
    VarHint(Methods.FixedWidth, ("0.0001",)),  # too many intervals
    VarHint(Methods.FixedWidth, ("x",)),  # invalid
    VarHint(Methods.FixedWidthTime, ("2", 3)),  # 2 weeks: too many
    VarHint(Methods.FixedWidthTime, ("1", 0)),  # 1 year
    VarHint(Methods.Custom, ("5, 10, 15",)),
    VarHint(Methods.Custom, ("5, 1",)),  # invalid
    VarHint(Methods.Remove, ()),
    VarHint(Methods.Keep, ()),
]
MIXED = {
    None: VarHint(Methods.EqualFreq, (3,)),
    ("a", False): VarHint(Methods.Custom, ("8, 12",)),
    ("when", True): VarHint(Methods.FixedWidthTime, ("1", 1)),
    ("b", False): VarHint(Methods.Keep, ()),
    ("m", False): VarHint(Methods.Remove, ()),
}


def local_table():
    """Numeric, time, constant and categorical features, a categorical target
    and a numeric meta."""
    rng = np.random.default_rng(1)
    n = 300
    domain = Domain(
        [
            ContinuousVariable("a"),
            ContinuousVariable("b"),
            TimeVariable("when", have_date=True),
            ContinuousVariable("c"),
            DiscreteVariable("d", values=("x", "y")),
        ],
        DiscreteVariable("y", values=("n", "p")),
        [ContinuousVariable("m")],
    )
    X = np.column_stack([
        rng.normal(10, 3, n).round(2),
        rng.uniform(0, 1, n),
        1.6e9 + rng.uniform(0, 3e8, n),
        np.full(n, 5.0),
        rng.integers(0, 2, n),
    ])  # fmt: skip
    X[::7, 0] = np.nan
    return Table.from_numpy(domain, X, (X[:, 0] > 10).astype(float), rng.normal(size=(n, 1)))


def describe(domain):
    """Each column's role, name, values and cut points (Orange can't compare
    its time discretizers: their cut points are arrays)."""
    parts = (domain.attributes, domain.class_vars, domain.metas)
    return [
        (role, var.name, type(var).__name__, getattr(var, "values", None),
         list(getattr(var.compute_value, "points", [])))
        for role, part in enumerate(parts)
        for var in part
    ]  # fmt: skip


def same_rows(got, expected):
    assert describe(got.domain) == describe(expected.domain)
    np.testing.assert_array_equal(got.X, expected.X)
    np.testing.assert_array_equal(got.Y, expected.Y)
    np.testing.assert_array_equal(got.metas.astype(str), expected.metas.astype(str))


class TestRecipe(base.WidgetTest):
    """The recipe on a local table, against Orange's widget (no machine)."""

    def orange(self, table, hints):
        settings = {"var_hints": dict(hints), "__version__": 3}
        w = self.create_widget(OWDiscretize, stored_settings=settings)
        self.send_signal(w.Inputs.data, table, widget=w)
        return self.get_output(w.Outputs.data, widget=w)

    def test_same_as_orange(self):
        table = local_table()
        for hints in [{None: preset} for preset in PRESETS] + [MIXED]:
            recipe = loads(dumps(make_recipe(hints, table.domain)))  # as the machine gets it
            same_rows(recipe(table), self.orange(table, hints))
        assert b"Orange.widgets" not in dumps(make_recipe(MIXED, table.domain))

    def test_cancel_and_memory(self):
        """The machine checks the memory first and can cancel between columns."""
        table = local_table()
        recipe = make_recipe({None: VarHint(Methods.EqualFreq, (3,))}, table.domain)

        class Context:
            calls = 0

            def progress(self, fraction, status=None):
                pass

            def check_cancelled(self):
                self.calls += 1
                if self.calls > 2:
                    raise JobCancelled()

        with pytest.raises(JobCancelled):
            replay(table, [("preprocess", recipe)], Context())
        assert check_fit_memory(table, recipe, 10**9) == table.X.nbytes + table.Y.nbytes + (
            table.metas.nbytes + table.W.nbytes
        )
        with pytest.raises(OperationError, match="Fitting DiscretizeColumns on 300 rows") as e:
            check_fit_memory(table, recipe, 1000)
        assert e.value.code == ErrorCode.PAYLOAD_TOO_LARGE


class TestCloudDiscretize(CloudTableTest):
    __test__ = True

    def run_widgets(self, data, full, hints):
        """Cloud Discretize on ``data`` and Orange's Discretize on ``full``,
        with the same settings: both widgets and their outputs."""
        cloud = self.create_widget(OWCloudDiscretize, stored_settings={"var_hints": dict(hints)})
        self.send_signal(cloud.Inputs.data, data, widget=cloud)
        self.wait_until_finished(cloud, timeout=120000)
        assert not cloud.Error.active, [str(m) for m in cloud.Error.active]
        settings = {"var_hints": dict(hints), "__version__": 3}
        local = self.create_widget(OWDiscretize, stored_settings=settings)
        self.send_signal(local.Inputs.data, full, widget=local)
        return (
            cloud,
            local,
            self.get_output(cloud.Outputs.data, widget=cloud),
            self.get_output(local.Outputs.data, widget=local),
        )

    def rows(self, data):
        return self.client.table_rows(resolve(data, self.client, self.machine_id), 0, self.N_ROWS)

    def test_same_as_orange(self):
        """Per-column methods and a preset: Orange's columns and rows on all
        rows, the same cut points in the list, and Orange's steps after it."""
        for hints in (
            {
                None: VarHint(Methods.EqualFreq, (3,)),
                ("x1", False): VarHint(Methods.Custom, ("8, 12",)),
                ("x2", False): VarHint(Methods.MDL, ()),
                ("x3", False): VarHint(Methods.FixedWidth, ("2.5",)),
            },
            {
                None: VarHint(Methods.EqualWidth, (4,)),
                ("x2", False): VarHint(Methods.Binning, (5,)),
            },
        ):
            cloud, local, out, expected = self.run_widgets(self.data, self.full, hints)
            assert [kind for kind, _ in out.steps] == ["preprocess"]
            assert out.ref.n_rows == self.N_ROWS
            same_rows(self.rows(out), expected)
            for model, other in ((cloud.varview.model(), local.varview.model()),):
                texts = [model.data(model.index(i)) for i in range(model.rowCount())]
                assert texts == [other.data(other.index(i)) for i in range(other.rowCount())]
            same_rows(
                self.rows(self.select_columns(out, drop="x1")),
                self.select_columns(expected, drop="x1"),
            )

    def test_interface(self):
        """Choosing a method for a selected column fits it on the machine; CC
        copies the cut points found on all rows into custom cut points."""
        from AnyQt.QtCore import QItemSelectionModel

        w = self.create_widget(OWCloudDiscretize)
        self.send_signal(w.Inputs.data, self.data, widget=w)
        model = w.varview.model()
        row = [var.name for var in model].index("x1")
        w.varview.selectionModel().select(
            model.index(row), QItemSelectionModel.SelectionFlag.Select
        )
        w.freq_spin.setValue(3)  # checks Equal frequency for x1
        self.wait_until_finished(w, timeout=120000)
        assert w.var_hints[("x1", False)] == VarHint(Methods.EqualFreq, (3,))
        out = self.get_output(w.Outputs.data, widget=w)
        assert len(out.domain["x1"].values) == 3 and out.domain["x2"].is_continuous
        points = out.domain["x1"].compute_value.points
        assert "(" in model.data(model.index(row))
        w.copy_to_custom.click()
        self.wait_until_finished(w, timeout=120000)
        method, (text,) = w.var_hints[("x1", False)]
        assert method == Methods.Custom and w.threshold_line.text() == text
        copied = self.get_output(w.Outputs.data, widget=w).domain["x1"].compute_value.points
        np.testing.assert_allclose(copied, points, rtol=1e-3)

    def test_orange_discretize_points_here(self):
        """Orange's Discretize's custom cut points on a cloud table are refused
        with a message that names Cloud Discretize."""
        settings = {"var_hints": {None: VarHint(Methods.Custom, ("5, 10",))}, "__version__": 3}
        w = self.create_widget(OWDiscretize, stored_settings=settings)
        self.send_signal(w.Inputs.data, self.data, widget=w)
        with pytest.raises(CloudUnsupportedStepError, match="use Cloud Discretize"):
            resolve(self.get_output(w.Outputs.data, widget=w), self.client, self.machine_id)

    def test_keep_everything_and_local_data(self):
        """With every column kept, the table passes through without a job;
        local data is refused."""
        w = self.create_widget(OWCloudDiscretize)
        self.send_signal(w.Inputs.data, self.data, widget=w)
        assert w.task is None and self.get_output(w.Outputs.data, widget=w) is self.data
        self.send_signal(w.Inputs.data, self.full, widget=w)
        assert LOCAL_DATA in str(w.Error.cloud_error)
        assert self.get_output(w.Outputs.data, widget=w) is None
