"""Cloud viewers: Feature Statistics, Distributions, Box Plot and Scatter
Plot on a cloud table show the numbers that Orange's own widgets show on
the same table on this computer; the 1M-row class (slow) checks that all
four work on a big table.
"""

import time

import numpy as np
import pytest
import scipy.stats
from Orange.widgets.data.owfeaturestatistics import OWFeatureStatistics
from Orange.widgets.visualize.owboxplot import OWBoxPlot
from Orange.widgets.visualize.owdistributions import OWDistributions
from Orange.widgets.visualize.owscatterplot import OWScatterPlot
from test_derive import CloudTableTest

from orangecontrib.cloud.client.api import CloudError
from orangecontrib.cloud.data.remote_table import is_cloud_table, resolve
from orangecontrib.cloud.protocol.tables import StatsKind, StatsRequest
from orangecontrib.cloud.widgets import owcloudscatterplot
from orangecontrib.cloud.widgets.owcloudboxplot import OWCloudBoxPlot
from orangecontrib.cloud.widgets.owclouddistributions import OWCloudDistributions
from orangecontrib.cloud.widgets.owcloudfeaturestatistics import OWCloudFeatureStatistics
from orangecontrib.cloud.widgets.owcloudscatterplot import OWCloudScatterPlot


class ViewerTest(CloudTableTest):
    __test__ = False

    def cloud_widget(self, cls, data=None, **settings):
        """A Cloud viewer on the cloud table, after its first result."""
        w = self.create_widget(cls, stored_settings=settings or None)
        self.send_signal(w.Inputs.data, self.data if data is None else data, widget=w)
        self.wait(w)
        return w

    def wait(self, w):
        self.wait_until_finished(w, timeout=600000)
        assert not w.Error.active, [str(m) for m in w.Error.active]
        assert w.result is not None

    def set(self, w, **values):
        """Change the viewer's choices and wait for the machine."""
        for name, value in values.items():
            setattr(w, name, value)
        w.refresh()
        self.wait(w)


class TestViewers(ViewerTest):
    __test__ = True

    def test_feature_statistics(self):
        orange = self.create_widget(OWFeatureStatistics)
        self.send_signal(orange.Inputs.data, self.full, widget=orange)
        expected = self.get_output(orange.Outputs.statistics, widget=orange)
        w = self.cloud_widget(OWCloudFeatureStatistics)
        stats = self.get_output(w.Outputs.statistics, widget=w)
        assert stats.domain == expected.domain
        np.testing.assert_allclose(stats.X, expected.X, rtol=1e-12, equal_nan=True)
        np.testing.assert_array_equal(stats.metas, expected.metas)
        assert w.model.n_instances == self.N_ROWS
        assert "From all 3,000 rows." in w.cloud_status
        # The selected columns go out as a recorded step on the cloud table.
        w.selected_vars = [self.data.domain["x2"]]
        w.commit()
        reduced = self.get_output(w.Outputs.reduced_data, widget=w)
        assert is_cloud_table(reduced) and [v.name for v in reduced.domain.variables] == ["x2"]

    def orange_distributions(self, var, cvar, bins=None):
        w = self.create_widget(OWDistributions)
        self.send_signal(w.Inputs.data, self.full, widget=w)
        w.var, w.cvar = self.full.domain[var], cvar and self.full.domain[cvar]
        w._on_var_changed()
        if bins is not None:
            w.number_of_bins = bins
            w._on_bins_changed()
        w.apply.now()
        return self.get_output(w.Outputs.histogram_data, widget=w)

    def test_distributions(self):
        domain = self.data.domain
        w = self.cloud_widget(OWCloudDistributions)
        for var, cvar, bins in (("x1", None, 3), ("x1", "label", 0), ("color", "label", None)):
            expected = self.orange_distributions(var, cvar, bins)
            changes = {"var": domain[var], "cvar": cvar and domain[cvar]}
            self.set(w, **changes, **({} if bins is None else {"number_of_bins": bins}))
            table = self.get_output(w.Outputs.histogram_data, widget=w)
            assert table.domain.attributes[0].values == expected.domain.attributes[0].values
            np.testing.assert_array_equal(table.X, expected.X)
        # A fitted normal curve: the parameters Orange fits on all rows.
        self.set(w, var=domain["x2"], cvar=None, fitted_distribution=1)
        column = next(iter(w.result.stats["distribution"]["columns"].values()))
        x2 = self.full.get_column("x2")
        np.testing.assert_allclose(column["curves"][0]["params"], scipy.stats.norm.fit(x2))

    def orange_box_plot(self, attr, group):
        w = self.create_widget(OWBoxPlot)
        self.send_signal(w.Inputs.data, self.full, widget=w)
        w.attribute, w.group_var = self.full.domain[attr], self.full.domain[group]
        w.compute_box_data()
        w.update_graph()
        return w

    def test_box_plot(self):
        domain = self.data.domain
        w = self.cloud_widget(OWCloudBoxPlot)
        for attr, group in (("x1", "label"), ("x2", "color")):
            orange = self.orange_box_plot(attr, group)
            self.set(w, attribute=domain[attr], group_var=domain[group])
            assert [b.label for b in w.boxes] == orange.label_txts
            for box, expected in zip(w.boxes, orange.stats, strict=True):
                for name in ("n", "a_min", "a_max", "mean", "dev", "median"):
                    np.testing.assert_allclose(getattr(box, name), getattr(expected, name))
                for name in ("q25", "q75"):  # Orange's is None where it equals the median
                    np.testing.assert_allclose(
                        getattr(box, name), getattr(expected, name) or expected.median
                    )
            assert w.stat_test == orange.stat_test and w.stat_test
        orange = self.orange_box_plot("color", "label")
        self.set(w, attribute=domain["color"], group_var=domain["label"])
        assert w.stat_test == orange.stat_test and w.stat_test.startswith("χ²")
        expected = orange.conts.array_with_unknowns  # its last column: values missing
        expected = expected[expected.sum(axis=1) > 0, :-1]
        np.testing.assert_array_equal(np.array([c for _, c in w.rows]), expected)

    def test_scatter_plot(self):
        domain = self.data.domain
        orange = self.create_widget(OWScatterPlot)
        self.send_signal(orange.Inputs.data, self.full, widget=orange)
        orange.attr_x, orange.attr_y = self.full.domain["x1"], self.full.domain["x3"]
        orange.attr_changed()
        w = self.cloud_widget(OWCloudScatterPlot)
        self.set(w, attr_x=domain["x1"], attr_y=domain["x3"])
        assert w.result.rows is not None and w.density is None  # small: every point
        np.testing.assert_array_equal(np.vstack(w.points), orange.get_coordinates_data())

    def test_scatter_plot_density(self):
        owcloudscatterplot.MAX_POINTS, previous = 0, owcloudscatterplot.MAX_POINTS
        try:
            w = self.cloud_widget(OWCloudScatterPlot, resolution=40)
        finally:
            owcloudscatterplot.MAX_POINTS = previous
        assert w.points is None and w.result.rows is None
        x, y = self.full.get_column("x1"), self.full.get_column("x2")
        valid = np.isfinite(x) & np.isfinite(y)
        x, y = x[valid], y[valid]
        edges = [np.linspace(v.min(), v.max(), 41) for v in (x, y)]
        np.testing.assert_array_equal(w.density, np.histogram2d(x, y, edges)[0])
        group_counts = w.result.stats["density"]["group_counts"]
        np.testing.assert_array_equal(group_counts.sum(axis=0), w.density)

    def test_local_data_and_bad_requests(self):
        w = self.create_widget(OWCloudBoxPlot)
        self.send_signal(w.Inputs.data, self.full, widget=w)
        assert "local data" in str(w.Error.cloud_error)
        table_id = resolve(self.data, self.client, self.machine_id)
        with pytest.raises(CloudError, match="no column 99"):
            self.client.stats(table_id, StatsRequest(kind=StatsKind.BASIC, columns=[99]))
        with pytest.raises(CloudError, match="isn't numeric"):
            self.client.stats(table_id, StatsRequest(kind=StatsKind.QUANTILES, columns=[3]))


@pytest.mark.slow
class TestViewersMillionRows(ViewerTest):
    """All four viewers on 1,000,000 rows (density, not points)."""

    __test__ = True
    N_ROWS = 1_000_000

    def test_all_viewers(self):
        domain = self.data.domain
        timings = {}
        for cls, settings in (
            (OWCloudFeatureStatistics, {}),
            (OWCloudDistributions, {}),
            (OWCloudBoxPlot, {}),
            (OWCloudScatterPlot, {}),
        ):
            started = time.monotonic()
            w = self.cloud_widget(cls, **settings)
            timings[cls.name] = time.monotonic() - started
            assert w.result.info.n_rows == self.N_ROWS
        assert w.density is not None and w.density.sum() > 0.9 * self.N_ROWS
        started = time.monotonic()
        self.set(w, attr_color=domain["color"], resolution=200)
        timings["Cloud Scatter Plot, by color, 200×200"] = time.monotonic() - started
        print("\n" + "\n".join(f"{name}: {s:.1f} s" for name, s in timings.items()))
