"""Cloud t-SNE: on a small cloud table the embedding equals Orange's t-SNE
widget on the same table (its TSNE, options and seed), with the default and
with other options; Cloud Scatter Plot shows the output; the slow class
embeds 100,000 rows.
"""

import time

import numpy as np
import pytest
from Orange.widgets.unsupervised.owtsne import OWtSNE
from test_derive import CloudTableTest

from orangecontrib.cloud.data.remote_table import is_cloud_table, resolve
from orangecontrib.cloud.widgets.owcloudscatterplot import OWCloudScatterPlot
from orangecontrib.cloud.widgets.owcloudtsne import OWCloudTSNE, rough_runtime

OPTIONS = {
    "use_pca_preprocessing": True,
    "pca_components": 3,
    "perplexity": 12,
    "exaggeration": 1.5,
    "initialization_method_idx": 1,  # spectral
    "distance_metric_idx": 2,  # cosine
}


def test_rough_runtime():
    """More rows, a higher perplexity and fewer CPUs take longer."""
    base = rough_runtime(100_000, 30, False, 8)
    assert rough_runtime(200_000, 30, False, 8) == pytest.approx(2 * base)
    assert rough_runtime(100_000, 100, False, 8) > base > rough_runtime(100_000, 30, False, 32)


class TSNETest(CloudTableTest):
    __test__ = False

    def cloud_tsne(self, **options):
        """Cloud t-SNE on the cloud table (with ``options``), after its result."""
        w = self.create_widget(OWCloudTSNE)
        self.send_signal(w.Inputs.data, self.data, widget=w)
        if options:
            self.wait_until_finished(w, timeout=1_800_000)
            for name, value in options.items():
                setattr(w, name, value)
            w.run()
        self.wait_until_finished(w, timeout=1_800_000)
        assert not w.Error.active, [str(m) for m in w.Error.active]
        assert w.output is not None
        return w

    def scatter_plot(self, data):
        """Cloud Scatter Plot of t-SNE-y against t-SNE-x of ``data``."""
        w = self.create_widget(OWCloudScatterPlot)
        self.send_signal(w.Inputs.data, data, widget=w)
        self.wait_until_finished(w, timeout=600000)
        w.attr_x, w.attr_y = data.domain["t-SNE-x"], data.domain["t-SNE-y"]
        w.refresh()
        self.wait_until_finished(w, timeout=600000)
        assert not w.Error.active, [str(m) for m in w.Error.active]
        return w


class TestCloudTSNE(TSNETest):
    __test__ = True
    N_ROWS = 500  # exact nearest neighbours: the same embedding with any number of threads

    def orange_tsne(self, **options):
        """Orange's t-SNE widget on the full table (with ``options``)."""
        w = self.create_widget(OWtSNE)
        self.send_signal(w.Inputs.data, self.full, widget=w)
        self.wait_until_finished(w, timeout=60000)
        if options:
            for name, value in options.items():
                setattr(w, name, value)
            w._invalidated = True
            w.run()
            self.wait_until_finished(w, timeout=60000)
        w.commit.now()
        return self.get_output(w.Outputs.annotated_data, widget=w)

    def output_rows(self, w):
        out = self.get_output(w.Outputs.annotated_data, widget=w)
        assert is_cloud_table(out)
        return self.client.table_rows(resolve(out, self.client, self.machine_id), 0, self.N_ROWS)

    def assert_same_embedding(self, rows, expected):
        names = ["id", "t-SNE-x", "t-SNE-y"]  # Orange's also has "Selected"
        assert [v.name for v in rows.domain.metas] == names
        assert [v.name for v in expected.domain.metas][:3] == names
        for name in names[1:]:
            np.testing.assert_allclose(
                rows.get_column(name), expected.get_column(name), rtol=1e-10, atol=1e-10
            )

    def test_default_equals_orange(self):
        expected = self.orange_tsne()
        w = self.cloud_tsne()
        self.assert_same_embedding(self.output_rows(w), expected)
        assert w.values["iterations"] == 750 and w.values["kl_divergence"] > 0
        assert not w.Warning.long_runtime.is_shown()
        # Steps recorded on the output replay on the machine.
        reduced = self.select_columns(self.get_output(w.Outputs.annotated_data, widget=w))
        rows = self.client.table_rows(resolve(reduced, self.client, self.machine_id), 0, 10)
        assert "x3" not in rows.domain and "t-SNE-x" in rows.domain

    def test_options_equal_orange(self):
        expected = self.orange_tsne(**OPTIONS)
        w = self.cloud_tsne(**OPTIONS)
        rows = self.output_rows(w)
        self.assert_same_embedding(rows, expected)
        default = self.orange_tsne()
        assert not np.allclose(rows.get_column("t-SNE-x"), default.get_column("t-SNE-x"))

    def test_scatter_plot_shows_the_output(self):
        w = self.cloud_tsne()
        out = self.get_output(w.Outputs.annotated_data, widget=w)
        plot = self.scatter_plot(out)
        rows = self.output_rows(w)
        x, y = plot.points  # small: every point
        np.testing.assert_array_equal(x, rows.get_column("t-SNE-x"))
        np.testing.assert_array_equal(y, rows.get_column("t-SNE-y"))

    def test_options_wait_for_start_and_warning(self):
        w = self.cloud_tsne()
        w.controls.perplexity.setValue(20)  # as the user would: waits for Start
        assert w.Information.modified.is_shown() and w.task is None
        w.on_partial_result(1_000_000)
        assert w.Warning.long_runtime.is_shown()
        assert "rough estimate" in str(w.Warning.long_runtime)

    def test_local_data(self):
        w = self.create_widget(OWCloudTSNE)
        self.send_signal(w.Inputs.data, self.full, widget=w)
        assert "local data" in str(w.Error.cloud_error)
        assert self.get_output(w.Outputs.annotated_data, widget=w) is None


@pytest.mark.slow
class TestCloudTSNE100kRows(TSNETest):
    """100,000 rows with the default options; Cloud Scatter Plot shows the density."""

    __test__ = True
    N_ROWS = 100_000

    def test_embed_and_show(self):
        started = time.monotonic()
        w = self.cloud_tsne()
        seconds = time.monotonic() - started
        out = self.get_output(w.Outputs.annotated_data, widget=w)
        assert out.ref.n_rows == self.N_ROWS
        plot = self.scatter_plot(out)
        assert plot.points is None and plot.density.sum() == self.N_ROWS
        timings = {k: round(v, 1) for k, v in w.values["seconds"].items()}
        print(
            f"\nCloud t-SNE, 100k rows: {seconds:.1f} s in all {timings}, "
            f"{w.values['n_jobs']} threads, KL {w.values['kl_divergence']:.3f}"
        )
