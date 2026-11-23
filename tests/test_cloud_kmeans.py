"""Cloud k-Means: on a small cloud table the clusters, the exact silhouette
and the centroids equal Orange's k-Means widget on the same table; above
the row limit the widget shows the simplified silhouette, computed on all
rows; the slow class clusters 1,000,000 rows for a range of k.
"""

import time

import numpy as np
import pytest
from Orange.clustering import KMeans
from Orange.preprocess import Normalize
from Orange.widgets.unsupervised.owkmeans import OWKMeans
from sklearn.metrics import silhouette_samples
from test_derive import CloudTableTest

from orangecontrib.cloud.data.remote_table import is_cloud_table, resolve
from orangecontrib.cloud.server.ops.kmeans import exact_silhouettes
from orangecontrib.cloud.widgets.owcloudkmeans import OWCloudKMeans


def test_exact_silhouettes_equal_scikit_learn():
    """One pass over the distances gives ``silhouette_samples`` for every k,
    and the errors it gives (one label only)."""
    rng = np.random.default_rng(1)
    X = rng.normal(size=(500, 4))
    labelings = [rng.integers(0, k, 500) for k in (2, 3, 7)] + [np.zeros(500, dtype=int)]
    results = exact_silhouettes(X, labelings)
    for labels, result in zip(labelings[:3], results[:3], strict=True):
        np.testing.assert_allclose(result, silhouette_samples(X, labels), rtol=1e-12, atol=1e-14)
    assert results[3].startswith("Number of labels is 1.")


class KMeansTest(CloudTableTest):
    __test__ = False

    def cloud_kmeans(self, **settings):
        """Cloud k-Means on the cloud table, after its result."""
        w = self.create_widget(OWCloudKMeans, stored_settings=settings or None)
        self.send_signal(w.Inputs.data, self.data, widget=w)
        self.wait_until_finished(w, timeout=1_800_000)
        assert not w.Error.active, [str(m) for m in w.Error.active]
        return w

    def output_rows(self, w):
        """The clustered table's rows (resolved on the machine) and the centroids."""
        out = self.get_output(w.Outputs.annotated_data, widget=w)
        assert is_cloud_table(out)
        rows = self.client.table_rows(resolve(out, self.client, self.machine_id), 0, self.N_ROWS)
        return rows, self.get_output(w.Outputs.centroids, widget=w)


class TestCloudKMeans(KMeansTest):
    __test__ = True

    def orange_kmeans(self, **settings):
        w = self.create_widget(OWKMeans, stored_settings=settings)
        self.send_signal(w.Inputs.data, self.full, widget=w, wait=60000)
        return w

    def test_fixed_k_equals_orange(self):
        orange = self.orange_kmeans(k=3)
        expected = self.get_output(orange.Outputs.annotated_data, widget=orange)
        expected_centroids = self.get_output(orange.Outputs.centroids, widget=orange)
        w = self.cloud_kmeans(k=3)
        rows, centroids = self.output_rows(w)
        names = [v.name for v in rows.domain.metas]
        assert names == [v.name for v in expected.domain.metas] == ["id", "Cluster", "Silhouette"]
        assert rows.domain["Cluster"].values == ("C1", "C2", "C3")
        np.testing.assert_array_equal(rows.get_column("Cluster"), expected.get_column("Cluster"))
        np.testing.assert_allclose(
            rows.get_column("Silhouette"), expected.get_column("Silhouette"), rtol=1e-12
        )
        assert [v.name for v in centroids.domain.variables + centroids.domain.metas] == [
            v.name for v in expected_centroids.domain.variables + expected_centroids.domain.metas
        ]
        np.testing.assert_allclose(centroids.X, expected_centroids.X, rtol=1e-12)
        np.testing.assert_allclose(
            centroids.metas[:, 1:].astype(float),
            expected_centroids.metas[:, 1:].astype(float),
            rtol=1e-12,
        )
        assert not w.Information.simplified.is_shown()
        # Steps recorded on the output replay on the machine.
        reduced = self.select_columns(self.get_output(w.Outputs.annotated_data, widget=w))
        rows = self.client.table_rows(resolve(reduced, self.client, self.machine_id), 0, 10)
        assert "x3" not in rows.domain and "Cluster" in rows.domain

    def test_range_equals_orange(self):
        settings = {"optimize_k": True, "k_from": 2, "k_to": 4}
        orange = self.orange_kmeans(**settings)
        w = self.cloud_kmeans(**settings)
        np.testing.assert_allclose(w.table_model.scores, orange.table_model.scores, rtol=1e-12)
        assert w.selected_row() == orange.selected_row()
        assert w.scores_box.title() == "Silhouette Scores"
        # Another row sends that k's clustering, without a new job.
        w.table_view.selectRow(2)
        rows, centroids = self.output_rows(w)
        assert len(rows.domain["Cluster"].values) == len(centroids) == 4

    def test_simplified_above_the_limit(self):
        w = self.cloud_kmeans(optimize_k=True, k_from=2, k_to=3, silhouette_rows=1000)
        assert w.Information.simplified.is_shown()
        assert "More than 1,000 rows" in str(w.Information.simplified)
        assert w.scores_box.title() == "Simplified Silhouette Scores"
        w.table_view.selectRow(1)  # k = 3
        rows, centroids = self.output_rows(w)
        # The simplified silhouette, from Orange's own preprocessing of all rows.
        data = Normalize()(self.full)
        for preprocessor in KMeans.preprocessors:
            data = preprocessor(data)
        dist = np.linalg.norm(data.X[:, None, :] - centroids.X[None, :, :], axis=2)
        own = rows.get_column("Cluster").astype(int)
        a = dist[np.arange(len(own)), own]
        dist[np.arange(len(own)), own] = np.inf
        b = dist.min(axis=1)
        silhouettes = (b - a) / np.maximum(a, b)
        np.testing.assert_allclose(w.table_model.scores[1], silhouettes.mean(), rtol=1e-10)
        np.testing.assert_allclose(
            rows.get_column("Simplified silhouette"),
            np.arctan(silhouettes) / np.pi + 0.5,
            rtol=1e-10,
        )

    def test_local_data(self):
        w = self.create_widget(OWCloudKMeans)
        self.send_signal(w.Inputs.data, self.full, widget=w)
        assert "local data" in str(w.Error.cloud_error)
        assert self.get_output(w.Outputs.annotated_data, widget=w) is None


@pytest.mark.slow
class TestCloudKMeansMillionRows(KMeansTest):
    """1,000,000 rows, k from 2 to 6: simplified silhouette scores per k."""

    __test__ = True
    N_ROWS = 1_000_000

    def test_range_of_k(self):
        started = time.monotonic()
        w = self.cloud_kmeans(optimize_k=True, k_from=2, k_to=6)
        seconds = time.monotonic() - started
        scores = w.table_model.scores
        assert len(scores) == 5 and all(isinstance(s, float) for s in scores)
        assert w.Information.simplified.is_shown()
        assert w.scores_box.title() == "Simplified Silhouette Scores"
        out = self.get_output(w.Outputs.annotated_data, widget=w)
        assert out.ref.n_rows == self.N_ROWS
        assert out.domain.metas[-1].name == "Simplified silhouette"
        values = w.values
        print(
            f"\nCloud k-Means, 1M rows, k 2-6: {seconds:.1f} s in all "
            f"(fit {values['fit_seconds']:.1f} s, silhouette {values['silhouette_seconds']:.1f} s)"
            f"\nscores: {[round(s, 3) for s in scores]}"
        )
