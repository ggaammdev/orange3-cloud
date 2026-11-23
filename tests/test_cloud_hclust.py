"""Cloud Hierarchical Clustering: on a small cloud table the clusters equal
SciPy's ``fcluster`` for every linkage (by number of clusters and by
height) and Orange's Hierarchical Clustering widget; changing only the cut
reuses the linkage; between columns the output is a small local table; a
linkage that doesn't fit in memory is refused before it runs.
"""

import numpy as np
import pytest
from Orange.widgets.unsupervised.owhierarchicalclustering import OWHierarchicalClustering
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import squareform
from test_derive import CloudTableTest

from orangecontrib.cloud.client.api import CloudNotFoundError
from orangecontrib.cloud.data.remote_table import is_cloud_table, resolve
from orangecontrib.cloud.protocol.errors import ErrorCode
from orangecontrib.cloud.server.ops.hclust import (
    LINKAGES,
    check_matrix,
    check_memory,
    memory_bytes,
)
from orangecontrib.cloud.server.worker import OperationError
from orangecontrib.cloud.widgets.owclouddistances import OWCloudDistances
from orangecontrib.cloud.widgets.owcloudhierarchicalclustering import (
    HEIGHT_RATIO,
    OWCloudHierarchicalClustering,
)


def same_partition(a, b) -> bool:
    """Whether two labelings group the items the same way (names aside)."""
    pairs = set(zip(np.asarray(a).tolist(), np.asarray(b).tolist(), strict=True))
    return len(pairs) == len(set(a)) == len(set(b))


def test_memory_check():
    assert memory_bytes(50_000, "ward") == int(2.2 * 49_999 * 25_000 * 8)
    assert memory_bytes(50_000, "single") < memory_bytes(50_000, "average")
    with pytest.raises(OperationError) as exc:
        check_memory(50_000, "ward", "rows", 8 * 2**30)
    assert exc.value.code == ErrorCode.PAYLOAD_TOO_LARGE
    assert "of 50,000 rows needs about 20.5 GB" in exc.value.message


def test_matrix_check():
    """Orange's widget's refusals, found tile by tile (the tiles are 1024 wide)."""
    rng = np.random.default_rng(0)
    m = squareform(rng.random(2500 * 2499 // 2))
    check_matrix(m)
    for value, message in ((7.0, "not symmetric"), (np.inf, "infinite")):
        bad = m.copy()
        bad[2400, 1100] = value
        if value == np.inf:
            bad[1100, 2400] = value
        with pytest.raises(OperationError, match=message):
            check_matrix(bad)
    with pytest.raises(OperationError, match="empty"):
        check_matrix(np.zeros((1, 1)))


class TestCloudHierarchicalClustering(CloudTableTest):
    __test__ = True

    def setUp(self):
        super().setUp()
        if not hasattr(type(self), "matrix"):  # once per class
            type(self).cloud_distances = self.compute_distances()
            type(self).matrix = self.cloud_distances.download(self.client, self.machine_id)

    def compute_distances(self, **settings):
        w = self.create_widget(OWCloudDistances, stored_settings=settings or None)
        self.send_signal(w.Inputs.data, self.data, widget=w)
        self.wait_until_finished(w, timeout=300000)
        return self.get_output(w.Outputs.distances, widget=w)

    def cluster(self, distances=None, **settings):
        """Cloud Hierarchical Clustering on the distances, finished."""
        w = self.create_widget(OWCloudHierarchicalClustering, stored_settings=settings or None)
        self.send_signal(w.Inputs.distances, distances or self.cloud_distances, widget=w)
        self.wait_until_finished(w, timeout=300000)
        assert not w.Error.active, [str(m) for m in w.Error.active]
        return w

    def clusters(self, w):
        """The output's cluster column, on all rows."""
        out = self.get_output(w.Outputs.data, widget=w)
        assert is_cloud_table(out)
        rows = self.client.table_rows(resolve(out, self.client, self.machine_id), 0, self.N_ROWS)
        assert rows.domain == out.domain and len(rows) == self.N_ROWS
        return rows.get_column("Cluster"), out

    def test_labels_equal_scipy(self):
        condensed = squareform(np.asarray(self.matrix), checks=False)
        for index, method in enumerate(LINKAGES):
            z = linkage(condensed, method=method)
            w = self.cluster(linkage=index, top_n=4)
            got, _ = self.clusters(w)
            assert same_partition(got, fcluster(z, 4, "maxclust")), method
            assert len(set(got)) == 4 and w.values["clusters"] == 4
            w.selection_method, w.cut_ratio = HEIGHT_RATIO, 30.0
            w.commit.now()
            self.wait_until_finished(w, timeout=300000)
            assert w.values["linkage_reused"]
            got, _ = self.clusters(w)
            assert same_partition(got, fcluster(z, 0.3 * z[-1, 2], "distance")), method

    def test_equals_orange_and_replays(self):
        orange = self.create_widget(
            OWHierarchicalClustering, stored_settings={"selection_method": 2, "top_n": 5}
        )
        self.send_signal(orange.Inputs.distances, self.matrix, widget=orange)
        expected = self.get_output(orange.Outputs.selected_data, widget=orange)
        w = self.cluster(top_n=5)
        got, out = self.clusters(w)
        np.testing.assert_array_equal(got, expected.get_column("Cluster"))
        assert out.domain["Cluster"].values == expected.domain["Cluster"].values
        assert out.domain["Cluster"].compute_value is None
        sizes = [int(np.sum(got == i)) for i in range(5)]
        assert w.clustering["sizes"] == sizes and w.sizes.rowCount() == 5
        reduced = self.select_columns(out)  # a recorded step on the output replays
        rows = self.client.table_rows(resolve(reduced, self.client, self.machine_id), 0, 10)
        assert "x3" not in rows.domain and "Cluster" in rows.domain

    def test_new_cut_reuses_the_linkage(self):
        w = self.cluster(top_n=3)
        assert not w.values["linkage_reused"]
        first = self.get_output(w.Outputs.data, widget=w)
        w.top_n = 6
        w.commit.now()
        self.wait_until_finished(w, timeout=300000)
        assert w.values["linkage_reused"] and w.values["clusters"] == 6
        w.commit.now()  # the next run deletes the replaced table
        self.wait_until_finished(w, timeout=300000)
        with pytest.raises(CloudNotFoundError):
            self.client.table_info(first.ref.table_id)

    def test_columns(self):
        distances = self.compute_distances(axis=1)
        w = self.cluster(distances, top_n=2)
        out = self.get_output(w.Outputs.data, widget=w)
        assert not is_cloud_table(out) and len(out) == 3
        assert list(out.get_column("Column")) == ["x1", "x2", "x3"]
        assert w.Information.columns.is_shown()
