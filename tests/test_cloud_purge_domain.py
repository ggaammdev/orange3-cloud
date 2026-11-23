"""Cloud Purge Domain: on a small cloud table with unused values, constant
columns and unsorted values, it gives Orange's Purge Domain's columns, rows
and counts on the full table, decided on the machine with Orange's own
``Remove``; the step replays, also when it keeps none of a column's values,
while the same choice made on the laptop's placeholder row stays refused.
"""

import numpy as np
import pytest
from Orange.data import filter as data_filter
from Orange.preprocess import Remove
from Orange.widgets.data.owfeatureconstructor import ContinuousDescriptor, DiscreteDescriptor
from Orange.widgets.data.owpurgedomain import OWPurgeDomain
from test_derive import CloudTableTest

from orangecontrib.cloud.client.api import CloudUnsupportedStepError
from orangecontrib.cloud.data.remote_table import LOCAL_DATA, resolve
from orangecontrib.cloud.widgets.owcloudpurgedomain import OWCloudPurgeDomain, purge_counts

#: Orange's Purge Domain removing only unused values.
UNUSED_VALUES = {"removeAttributes": 0, "removeClassAttribute": 0, "removeMetaAttributes": 0}


class TestCloudPurgeDomain(CloudTableTest):
    __test__ = True

    def prepared(self, data):
        """Rows without blue, a constant numeric column, a categorical one
        with an unused value and unsorted values, and a constant one."""
        color = data.domain["color"]
        keep = [i for i, value in enumerate(color.values) if value != "blue"]
        data = data_filter.Values([data_filter.FilterDiscrete(color, keep)])(data)
        return self.feature_constructor(
            data,
            ContinuousDescriptor("one", "1", 3),
            DiscreteDescriptor(
                "size", "small if x1 < 10 else big", ("small", "big", "huge"), False
            ),
            DiscreteDescriptor("same", "a", ("a", "b"), False),
        )

    def purge(self, cls, data, settings=None):
        w = self.create_widget(cls, stored_settings=settings)
        self.send_signal(w.Inputs.data, data, widget=w)
        self.wait_until_finished(w, timeout=120000)
        assert not w.Error.active, [str(m) for m in w.Error.active]
        counts = {name: getattr(w, name) for _, name in w.stat_labels}
        return self.get_output(w.Outputs.data, widget=w), counts

    def rows(self, data):
        return self.client.table_rows(resolve(data, self.client, self.machine_id), 0, self.N_ROWS)

    def assert_same(self, got, expected):
        assert got.domain == expected.domain
        np.testing.assert_array_equal(got.X, expected.X)
        np.testing.assert_array_equal(got.Y, expected.Y)
        np.testing.assert_array_equal(got.metas, expected.metas)

    def test_same_as_orange(self):
        cloud, cloud_counts = self.purge(OWCloudPurgeDomain, self.prepared(self.data))
        local, local_counts = self.purge(OWPurgeDomain, self.prepared(self.full))
        assert [kind for kind, _ in cloud.steps][-1] == "preprocess"
        assert cloud.domain == local.domain
        names = [var.name for var in local.domain.attributes]
        assert "one" not in names and "same" not in names
        assert local.domain["color"].values == ("green", "red")
        assert local.domain["size"].values == ("big", "small")
        assert cloud_counts == local_counts
        assert local_counts["removedAttrs"] == 2 and local_counts["resortedAttrs"] >= 1
        self.assert_same(self.rows(cloud), local)
        # Orange's own steps after it replay on the purged columns.
        self.assert_same(
            self.rows(self.select_columns(cloud, drop="size")),
            self.select_columns(local, drop="size"),
        )

    def test_keeps_no_values(self):
        """With no rows left, purging unused values keeps none of a column's
        values. Decided on the machine, the step replays; decided on the
        laptop's placeholder row, it stays refused."""
        x2 = self.data.domain["x2"]
        empty = data_filter.Values(
            [data_filter.FilterContinuous(x2, data_filter.FilterContinuous.Greater, 1000)]
        )
        cloud, _ = self.purge(OWCloudPurgeDomain, empty(self.data), UNUSED_VALUES)
        local, _ = self.purge(OWPurgeDomain, empty(self.full), UNUSED_VALUES)
        assert cloud.domain["color"].values == local.domain["color"].values == ()
        rows = self.rows(cloud)
        assert len(rows) == 0 and rows.domain == local.domain
        flags = Remove.RemoveUnusedValues
        on_placeholder = Remove(flags, flags, flags)(empty(self.data))
        with pytest.raises(CloudUnsupportedStepError, match="keeps none of the values"):
            resolve(on_placeholder, self.client, self.machine_id)

    def test_nothing_to_do_and_local_data(self):
        """Without options the table passes through; local data is refused."""
        cls = OWCloudPurgeDomain
        options = cls.feature_options + cls.class_options + cls.meta_options
        off = {name: 0 for name, _ in options}
        w = self.create_widget(OWCloudPurgeDomain, stored_settings=off)
        self.send_signal(w.Inputs.data, self.data, widget=w)
        assert w.task is None and self.get_output(w.Outputs.data, widget=w) is self.data
        assert w.removedAttrs == 0
        self.send_signal(w.Inputs.data, self.full, widget=w)
        assert LOCAL_DATA in str(w.Error.cloud_error)
        assert self.get_output(w.Outputs.data, widget=w) is None


def test_counts_as_orange():
    """The counts from the columns before and after equal Orange's Remove's."""
    from Orange.data import ContinuousVariable, DiscreteVariable, Domain, Table

    domain = Domain(
        [
            DiscreteVariable("a", values=("z", "y", "x")),  # unused x, unsorted
            DiscreteVariable("b", values=("p", "q")),  # constant
            DiscreteVariable("c", values=("m", "n")),  # unchanged
            ContinuousVariable("d"),  # constant
        ],
        DiscreteVariable("y", values=("t", "s")),  # unsorted
    )
    X = np.array([[0, 0, 0, 1.0], [1, 0, 1, 1.0], [0, 0, 1, 1.0]])
    table = Table.from_numpy(domain, X, np.array([0, 1, 0]))
    remover = Remove(7, 7, 6)
    out = remover(table)
    assert purge_counts(domain.attributes, out.domain.attributes) == remover.attr_results
    assert purge_counts(domain.class_vars, out.domain.class_vars) == remover.class_results
