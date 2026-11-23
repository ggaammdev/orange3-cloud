"""Cloud MDS: on a small cloud table the positions equal Orange's MDS widget
on the same data (up to rotation; same Kruskal stress), from the data and
from Cloud Distances, with PCA and random initial positions; the output
replays recorded steps; between columns the output is a small local table;
MDS that doesn't fit in memory is refused before it runs.
"""

from types import SimpleNamespace

import numpy as np
import pytest
from Orange.widgets.unsupervised.owmds import OWMDS
from scipy.spatial import procrustes
from test_derive import CloudTableTest

from orangecontrib.cloud.client.api import CloudNotFoundError
from orangecontrib.cloud.client.providers.gcp_vm import MACHINE_TYPES
from orangecontrib.cloud.data.cloud_distances import DistancesTooLargeError, check_mds_memory
from orangecontrib.cloud.data.remote_table import is_cloud_table, resolve
from orangecontrib.cloud.protocol.errors import ErrorCode
from orangecontrib.cloud.protocol.memory import mds_memory_bytes
from orangecontrib.cloud.server.ops.mds import check_memory
from orangecontrib.cloud.server.worker import OperationError
from orangecontrib.cloud.widgets.owclouddistances import OWCloudDistances
from orangecontrib.cloud.widgets.owcloudmds import RANDOM, OWCloudMDS, rough_runtime


def test_memory_check():
    gb = 2**30
    assert mds_memory_bytes(20_000) == 6 * 20_000**2 * 8
    assert mds_memory_bytes(20_000, 10**6) == 7 * 20_000**2 * 8 + 4 * 10**6
    with pytest.raises(OperationError) as exc:
        check_memory(20_000, "rows", None, 8 * gb)
    assert exc.value.code == ErrorCode.PAYLOAD_TOO_LARGE
    assert "MDS of 20,000 rows needs about 17.9 GB" in exc.value.message
    info = SimpleNamespace(memory_free_bytes=8 * gb, memory_tables_bytes=0)
    with pytest.raises(DistancesTooLargeError, match="The smallest that fits: Medium"):
        check_mds_memory(20_000, "rows", None, info, MACHINE_TYPES)
    assert check_mds_memory(5_000, "rows", None, info) == 6 * 5_000**2 * 8


def test_rough_runtime():
    base = rough_runtime(10_000, 300)
    assert rough_runtime(20_000, 300) == pytest.approx(4 * base)
    assert rough_runtime(10_000, 600) == pytest.approx(2 * base)
    assert 400 < rough_runtime(20_000, 300) < 900  # 554-777 s on Google Cloud


def assert_same_positions(got, expected, got_stress, expected_stress):
    """Equal up to rotation, reflection and scale (Procrustes), with the same stress."""
    *_, disparity = procrustes(expected, got)
    assert disparity < 1e-9
    np.testing.assert_allclose(got, expected, rtol=1e-7, atol=1e-7)
    assert got_stress == pytest.approx(expected_stress, rel=1e-9)


class TestCloudMDS(CloudTableTest):
    __test__ = True
    N_ROWS = 600

    def orange_mds(self, data=None, distances=None, random=False):
        """Orange's MDS widget, finished; its positions and Kruskal stress."""
        w = self.create_widget(OWMDS)
        if data is not None:
            self.send_signal(w.Inputs.data, data, widget=w)
        if distances is not None:
            self.send_signal(w.Inputs.distances, distances, widget=w)
        self.wait_until_finished(w, timeout=120000)
        if random:  # Orange's "Randomize" (unseeded); Cloud MDS seeds it with 0
            np.random.seed(0)
            w.do_random()
            w._run()
            self.wait_until_finished(w, timeout=120000)
        return w.embedding, w._compute_stress()

    def cloud_mds(self, data=None, distances=None, **settings):
        """Cloud MDS, finished."""
        w = self.create_widget(OWCloudMDS, stored_settings=settings or None)
        if data is not None:
            self.send_signal(w.Inputs.data, data, widget=w)
        if distances is not None:
            self.send_signal(w.Inputs.distances, distances, widget=w)
        self.wait_until_finished(w, timeout=300000)
        assert not w.Error.active, [str(m) for m in w.Error.active]
        return w

    def positions(self, w):
        """The output's mds-x and mds-y on all rows, and the output."""
        out = self.get_output(w.Outputs.annotated_data, widget=w)
        assert is_cloud_table(out)
        rows = self.client.table_rows(resolve(out, self.client, self.machine_id), 0, self.N_ROWS)
        assert rows.domain == out.domain and len(rows) == self.N_ROWS
        assert [v.name for v in rows.domain.metas] == ["id", "mds-x", "mds-y"]
        return np.column_stack([rows.get_column("mds-x"), rows.get_column("mds-y")]), out

    def cloud_distances(self, **settings):
        w = self.create_widget(OWCloudDistances, stored_settings=settings)
        self.send_signal(w.Inputs.data, self.data, widget=w)
        self.wait_until_finished(w, timeout=300000)
        return self.get_output(w.Outputs.distances, widget=w)

    def test_data_equals_orange_and_replays(self):
        expected, stress = self.orange_mds(data=self.full)
        w = self.cloud_mds(data=self.data)
        got, out = self.positions(w)
        assert_same_positions(got, expected, w.values["stress"], stress)
        assert w.values["items"] == self.N_ROWS and w.values["iterations"] > 0
        assert out.domain["mds-x"].compute_value is None
        reduced = self.select_columns(out)  # a recorded step on the output replays
        rows = self.client.table_rows(resolve(reduced, self.client, self.machine_id), 0, 10)
        assert "x3" not in rows.domain and "mds-y" in rows.domain

    def test_random_initialization_equals_orange(self):
        expected, stress = self.orange_mds(data=self.full, random=True)
        w = self.cloud_mds(data=self.data, initialization=RANDOM)
        got, _ = self.positions(w)
        assert_same_positions(got, expected, w.values["stress"], stress)

    def test_distances_equal_orange(self):
        distances = self.cloud_distances(metric_id=2)  # Manhattan
        matrix = distances.download(self.client, self.machine_id)
        expected, stress = self.orange_mds(distances=matrix)
        w = self.cloud_mds(distances=distances)
        got, _ = self.positions(w)  # the table the distances were computed from
        assert_same_positions(got, expected, w.values["stress"], stress)
        # Rerunning deletes the replaced table.
        first = self.get_output(w.Outputs.annotated_data, widget=w)
        w.run()
        self.wait_until_finished(w, timeout=300000)
        w.run()
        self.wait_until_finished(w, timeout=300000)
        with pytest.raises(CloudNotFoundError):
            self.client.table_info(first.ref.table_id)

    def test_columns(self):
        distances = self.cloud_distances(axis=1)
        expected, stress = self.orange_mds(distances=distances.download(self.client))
        w = self.cloud_mds(distances=distances)
        out = self.get_output(w.Outputs.annotated_data, widget=w)
        assert not is_cloud_table(out) and len(out) == 3
        assert list(out.get_column("labels")) == ["x1", "x2", "x3"]
        got = np.column_stack([out.get_column("mds-x"), out.get_column("mds-y")]).astype(float)
        assert_same_positions(got, expected, w.values["stress"], stress)

    def test_options_wait_for_start_and_warning(self):
        w = self.cloud_mds(data=self.data)
        w.controls.max_iter.setValue(100)  # as the user would: waits for Start
        assert w.Information.modified.is_shown() and w.task is None
        w.on_partial_result(20_000)
        assert w.Warning.long_runtime.is_shown()
        assert "rough estimate" in str(w.Warning.long_runtime)

    def test_local_data(self):
        w = self.create_widget(OWCloudMDS)
        self.send_signal(w.Inputs.data, self.full, widget=w)
        assert "local data" in str(w.Error.cloud_error)
        assert self.get_output(w.Outputs.annotated_data, widget=w) is None
