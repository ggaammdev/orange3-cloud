"""Cloud Categories: its recipe does what Edit Domain's "as categorical" and
"merge" do on the whole table (each merge mode, ties among the most frequent
values, values that no row has), and on a small cloud table the widget shows
the counts of all rows and gives Edit Domain's columns and rows on the full
table; the step replays, also under Orange's steps after it.
"""

from unittest import mock

import numpy as np
from AnyQt.QtCore import QItemSelectionModel, Qt
from Orange.data import ContinuousVariable, DiscreteVariable, Domain, StringVariable, Table
from Orange.widgets.data import oweditdomain as edit_domain
from Orange.widgets.data.owfeatureconstructor import (
    ContinuousDescriptor,
    DiscreteDescriptor,
    StringDescriptor,
)
from Orange.widgets.tests import base  # not WidgetTest itself: pytest would collect it
from test_derive import CloudTableTest

from orangecontrib.cloud.data.remote_table import LOCAL_DATA, resolve
from orangecontrib.cloud.protocol.recipes import ColumnCategories, Merge, value_counts
from orangecontrib.cloud.protocol.tables import StatsKind, StatsRequest
from orangecontrib.cloud.server.ops import stats
from orangecontrib.cloud.server.ops.stats import compute_stats
from orangecontrib.cloud.widgets import owcloudcategories
from orangecontrib.cloud.widgets.owcloudcategories import OWCloudCategories

#: Edit Domain's merge dialog: the mode's radio button and spin box, by mode.
DIALOG = {
    Merge.FEWER_THAN: ("frequent_abs_radio", "frequent_abs_spin"),
    Merge.PERCENT: ("frequent_rel_radio", "frequent_rel_spin"),
    Merge.ALL_BUT: ("n_values_radio", "n_values_spin"),
}
VECTORS = {"discrete": "CategoricalVector", "continuous": "RealVector", "string": "StringVector"}


def edit_domain_transforms(table, var, merge=Merge.NONE, amount=0, selected=(), name="other"):
    """Edit Domain's transforms for ``var`` on ``table``: "as categorical"
    for a numeric or text column, then the categories its merge dialog
    groups into ``name`` in mode ``merge``, from the values it sees."""
    kind = next(k for k in VECTORS if getattr(var, f"is_{k}"))
    vector = getattr(edit_domain, VECTORS[kind])(
        edit_domain.abstract(var), lambda: edit_domain.table_column_data(table, var)
    )
    transforms = [] if var.is_discrete else [edit_domain.AsCategorical()]
    categorical = edit_domain.AsCategorical()(vector)
    if merge == Merge.NONE:
        return transforms
    dialog = edit_domain.GroupItemsDialog(categorical.vtype, categorical.data(), list(selected), {})
    if merge != Merge.SELECTED:
        radio, spin = DIALOG[merge]
        getattr(dialog, spin).setValue(amount)
        getattr(dialog, radio).setChecked(True)
    merged = dialog.get_merge_attributes()
    mapping = [(c, name if c in merged else c) for c in categorical.vtype.categories]
    if all(old == new for old, new in mapping):
        return transforms  # Edit Domain's editor adds no mapping then
    return [*transforms, edit_domain.CategoriesMapping(mapping)]


def local_table():
    """A numeric column without decimals, a categorical one with skewed
    counts and a value that no row has, and a text column; some missing."""
    rng = np.random.default_rng(3)
    n = 400
    domain = Domain(
        [
            ContinuousVariable("k", number_of_decimals=0),
            DiscreteVariable("c", values=("a", "b", "c", "d", "unused")),
        ],
        metas=[StringVariable("s")],
    )
    X = np.column_stack([rng.integers(0, 7, n), rng.choice(4, n, p=[0.6, 0.2, 0.15, 0.05])]).astype(
        float
    )
    X[::17, 0] = np.nan
    words = rng.choice(np.array(["apple", "Banana", "cherry", "date", "fig"]), n).astype(object)
    words[::13] = ""
    return Table.from_numpy(domain, X, None, words.reshape(-1, 1))


def assert_same_variable(got, expected):
    assert got == expected and got.values == expected.values, (got.values, expected.values)


class TestRecipe(base.WidgetTest):
    """The recipe on a local table equals Edit Domain on it."""

    def test_same_as_edit_domain(self):
        table = local_table()
        cases = [
            (Merge.NONE, 0),
            (Merge.FEWER_THAN, 60),
            (Merge.PERCENT, 16.0),
            (Merge.ALL_BUT, 2),
            (Merge.ALL_BUT, 0),
        ]
        for var in (*table.domain.attributes, *table.domain.metas):
            for merge, amount in cases:
                transforms = edit_domain_transforms(table, var, merge, amount)
                expected = edit_domain.apply_transform(var, table, transforms)
                got = ColumnCategories(var.name, merge, amount).variable(
                    var, value_counts(table, var)
                )
                assert_same_variable(got, expected)
                if merge == Merge.NONE and var.is_discrete:
                    assert got is var  # nothing to change
                np.testing.assert_array_equal(
                    table.transform(Domain([got])).X, table.transform(Domain([expected])).X
                )
            selected = ColumnCategories(var.name, Merge.SELECTED, selected=["2", "c", "fig"])
            transforms = edit_domain_transforms(table, var, Merge.SELECTED, 0, ["2", "c", "fig"])
            expected = edit_domain.apply_transform(var, table, transforms)
            assert_same_variable(selected.variable(var, value_counts(table, var)), expected)

    def test_ties_and_values_no_row_has(self):
        """With equal counts, the values that appear first stay; values that
        no row has are never merged, as with Edit Domain's ``Counter``."""
        var = DiscreteVariable("c", values=("a", "b", "c", "d", "e"))
        table = Table.from_numpy(Domain([var]), np.array([[2, 1, 1, 2, 0, 3, 3]]).T)
        for merge, amount in ((Merge.ALL_BUT, 1), (Merge.ALL_BUT, 2), (Merge.FEWER_THAN, 2)):
            transforms = edit_domain_transforms(table, var, merge, amount)
            expected = edit_domain.apply_transform(var, table, transforms)
            got = ColumnCategories("c", merge, amount).variable(var, value_counts(table, var))
            assert_same_variable(got, expected)
            assert "e" in got.values
        assert ColumnCategories("c", Merge.ALL_BUT, 1).variable(
            var, value_counts(table, var)
        ).values == ("other", "c", "e")

    def test_recipe_replaces_the_column_in_place(self):
        table = local_table()
        out = ColumnCategories("s", Merge.FEWER_THAN, 70, name="rare")(table)
        assert [v.name for v in out.domain.metas] == ["s"] and out.domain["s"].is_discrete
        assert "rare" in out.domain["s"].values
        assert out.domain.attributes == table.domain.attributes


class TestCloudCategories(CloudTableTest):
    __test__ = True

    def prepared(self, data):
        """A numeric column with few values, a categorical one with skewed
        counts and a value no row has, and a text column with few values."""
        return self.feature_constructor(
            data,
            ContinuousDescriptor("k", "x2 // 4", 0),
            DiscreteDescriptor(
                "band",
                "a if x2 < 5 else b if x2 < 9 else c if x2 < 16 else d",
                ("a", "b", "c", "d", "e"),
                False,
            ),
            StringDescriptor("word", "id[-1:] if x3 > 8 else 'z'"),
        )

    def categories(self, data, name=None, settings=None):
        """Cloud Categories on ``data``, with column ``name`` and ``settings``."""
        w = self.create_widget(OWCloudCategories, stored_settings=settings)
        self.send_signal(w.Inputs.data, data, widget=w)
        self.wait_until_finished(w, timeout=120000)
        if name is not None:
            w.column = data.domain[name]
            w._column_changed()
            self.wait_until_finished(w, timeout=120000)
        assert not w.Error.active, [str(m) for m in w.Error.active]
        return w, self.get_output(w.Outputs.data, widget=w)

    def edit_domain(self, data, name, transforms):
        """Orange's Edit Domain with ``transforms`` for column ``name``."""
        w = self.create_widget(edit_domain.OWEditDomain)
        self.send_signal(w.Inputs.data, data, widget=w)
        index = [v.name for v in data.domain.variables + data.domain.metas].index(name)
        model = w.domain_view.model()
        model.setData(model.index(index, 0), transforms, edit_domain.TransformRole)
        w.commit()
        return self.get_output(w.Outputs.data, widget=w)

    def rows(self, data):
        return self.client.table_rows(resolve(data, self.client, self.machine_id), 0, self.N_ROWS)

    def assert_same(self, got, expected):
        assert got.domain == expected.domain
        for var in got.domain.variables + got.domain.metas:
            assert var.values == expected.domain[var.name].values if var.is_discrete else True
        np.testing.assert_array_equal(got.X, expected.X)
        np.testing.assert_array_equal(got.Y, expected.Y)
        np.testing.assert_array_equal(got.metas, expected.metas)

    def test_same_as_edit_domain(self):
        cloud_data, full = self.prepared(self.data), self.prepared(self.full)
        cases = [
            ("k", {}, Merge.NONE, 0),
            ("word", {"merge": Merge.PERCENT, "percent": 6.6, "merged_name": "few"}, None, 0),
            ("band", {"merge": Merge.ALL_BUT, "keep": 2}, Merge.ALL_BUT, 2),
            ("k", {"merge": Merge.FEWER_THAN, "fewer_than": 100}, Merge.FEWER_THAN, 100),
        ]
        for name, settings, merge, amount in cases:
            w, cloud = self.categories(cloud_data, name, {"as_categorical": True, **settings})
            var = full.domain[name]
            if merge is None:  # merged into "few" instead of "other"
                transforms = edit_domain_transforms(full, var, Merge.PERCENT, 6.6, name="few")
                assert isinstance(transforms[-1], edit_domain.CategoriesMapping)
            else:
                transforms = edit_domain_transforms(full, var, merge, amount)
            local = self.edit_domain(full, name, transforms)
            assert [kind for kind, _ in cloud.steps][-1] == "preprocess", name
            self.assert_same(self.rows(cloud), local)
            # The list shows the counts of all rows.
            counts = value_counts(full, var)
            assert w.column_stats["n_values"] == len(counts.values)
            model = w.values_model
            shown = [
                model.item(i, 1).data(Qt.ItemDataRole.UserRole) for i in range(model.rowCount())
            ]
            assert shown == counts.counts.tolist()
        # Orange's own steps after it replay on the changed column.
        self.assert_same(
            self.rows(self.select_columns(cloud, drop="x1")),
            self.select_columns(local, drop="x1"),
        )

    def test_selected_values(self):
        """ "Group selected values" merges the values selected in the list,
        and the selection stays when the list is filled again."""
        w, _ = self.categories(self.data, "color", {"merge": Merge.SELECTED})
        view, model = w.values_view, w.values_model
        flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        for row in range(model.rowCount()):
            if model.item(row, 0).text() in ("red", "blue"):
                view.selectionModel().select(model.index(row, 0), flags)
        self.wait_until_finished(w, timeout=120000)
        assert sorted(w.selected_values) == ["blue", "red"]
        assert [model.item(i, 2).text() for i in range(3)] == ["other", "", "other"]
        cloud = self.get_output(w.Outputs.data, widget=w)
        var = self.full.domain["color"]
        transforms = edit_domain_transforms(self.full, var, Merge.SELECTED, 0, ["red", "blue"])
        self.assert_same(self.rows(cloud), self.edit_domain(self.full, "color", transforms))
        assert cloud.domain["color"].values == ("other", "green")

    def test_nothing_to_do_and_local_data(self):
        """A categorical column without merging passes through, and so does
        a numeric one that isn't made categorical; a column with too many
        values for a categorical one is refused; local data is refused."""
        w, out = self.categories(self.data)
        assert w.column.name == "color" and out is self.data
        assert w.summary.text().startswith("'color' has 3 values")
        w, out = self.categories(self.data, "x2")
        assert out is self.data and not w.as_categorical_box.isChecked()
        n_values = len(value_counts(self.full, self.full.domain["x2"]).values)
        assert w.summary.text() == f"'x2' has {n_values:,} different values."
        self.send_signal(w.Inputs.data, self.full, widget=w)
        assert LOCAL_DATA in str(w.Error.cloud_error)
        assert self.get_output(w.Outputs.data, widget=w) is None

    def test_too_many_values(self):
        """A column with more values than the machine lists isn't made categorical."""
        too_many = {"values": None, "counts": None, "first_seen": None}
        too_many.update(missing=0, n_values=200_000)
        w = self.create_widget(OWCloudCategories, stored_settings={"as_categorical": True})
        with mock.patch.object(owcloudcategories, "fetch_values", return_value=too_many):
            self.send_signal(w.Inputs.data, self.data, widget=w)
            self.wait_until_finished(w)
            w.column = self.data.domain["x2"]
            w._column_changed()
            self.wait_until_finished(w)
        message = "'x2' has 200,000 different values, too many for a categorical column."
        assert str(w.Error.too_many_values) == message
        assert self.get_output(w.Outputs.data, widget=w) is None
        assert w.values_model.rowCount() == 0


def test_values_statistics():
    """The machine's ``values`` statistics: Edit Domain's counts, and no list
    for a column with too many values."""
    table = local_table()
    request = StatsRequest(kind=StatsKind.VALUES, columns=[0, 1, -1])
    columns = compute_stats(table, request)["columns"]
    assert columns[1]["values"] == ("a", "b", "c", "d", "unused")
    assert columns[1]["counts"][-1] == 0 and 4 not in columns[1]["first_seen"]
    assert columns[-1]["missing"] == len(range(0, 400, 13))
    assert list(columns[0]["values"]) == [0, 1, 2, 3, 4, 5, 6]
    with mock.patch.object(stats, "MAX_VALUES", 6):
        column = compute_stats(table, request)["columns"][0]
    assert column["values"] is None and column["n_values"] == 7
