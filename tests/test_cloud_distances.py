"""Cloud Distances: on a small cloud table the matrix equals Orange's
Distances widget on the same table (metrics, rows or columns, normalized,
its warnings and errors); distances that don't fit in the machine's memory
are refused before the job runs, by the widget and by the machine itself.
"""

import numpy as np
import pytest
from Orange.widgets.unsupervised.owdistances import OWDistances
from test_derive import CloudTableTest
from test_remote_table import write_csv

from orangecontrib.cloud.client.api import CloudError, CloudNotFoundError
from orangecontrib.cloud.client.providers.gcp_vm import MACHINE_TYPES
from orangecontrib.cloud.data.cloud_distances import CloudDistances, DistancesUnavailableError
from orangecontrib.cloud.protocol.errors import ErrorCode
from orangecontrib.cloud.protocol.jobs import JobKind
from orangecontrib.cloud.protocol.machine import InfoResponse
from orangecontrib.cloud.protocol.memory import DISTANCE_METRICS, distances_memory_bytes
from orangecontrib.cloud.server.ops.distances import METRIC_CLASSES
from orangecontrib.cloud.widgets.owclouddistances import (
    COSINE,
    JACCARD,
    MANHATTAN_NORMALIZED,
    METRICS,
    PEARSON,
    SPEARMAN_ABSOLUTE,
    OWCloudDistances,
)


def text(message) -> str:
    """A widget message's text, as a plain ``str`` (messages compare by identity)."""
    return "".join(str(message))


def test_metrics_and_memory():
    """Every metric of the widget is one the machine knows, with its memory."""
    assert set(METRIC_CLASSES) == set(DISTANCE_METRICS)
    assert {m.key for m in METRICS.values()} == set(DISTANCE_METRICS)
    assert distances_memory_bytes(50_000, "euclidean") == int(2.1 * 50_000**2 * 8)
    assert distances_memory_bytes(10, "manhattan", 1000) == int(1.2 * 800) + 4000


def test_dist_matrix_pickle():
    """DistMatrix travels as a plain array plus its items (no copy when pickled)."""
    from Orange.data import Table
    from Orange.distance import Euclidean

    from orangecontrib.cloud.protocol.pickles import dumps, loads

    data = Table("iris")
    for axis in (0, 1):
        matrix = Euclidean(data, axis=axis)
        got = loads(dumps(matrix))
        assert type(got) is type(matrix) and got.axis == axis
        np.testing.assert_array_equal(got, matrix)
        assert got.row_items.domain == data.domain


def test_check_memory_message():
    from orangecontrib.cloud.data.cloud_distances import DistancesTooLargeError, check_memory

    info = InfoResponse(
        server_version="0",
        python_version="3.12",
        cpu_count=8,
        memory_total_bytes=8 * 2**30,
        memory_free_bytes=6 * 2**30,
        uptime_seconds=1,
    )
    assert check_memory(10_000, "rows", "euclidean", 0, info) < 6 * 2**30
    with pytest.raises(DistancesTooLargeError) as exc:
        check_memory(50_000, "rows", "euclidean", 0, info, MACHINE_TYPES)
    assert "Distances between 50,000 rows need about 39.1 GB" in exc.value.message
    assert "can give it 5.4 GB" in exc.value.message
    assert "The smallest that fits: Medium (64 GB)." in exc.value.message
    with pytest.raises(DistancesTooLargeError) as exc:  # no sizes (this computer)
        check_memory(50_000, "rows", "euclidean", 0, info)
    assert "smallest" not in exc.value.message and "largest" not in exc.value.message


class TestCloudDistances(CloudTableTest):
    __test__ = True

    def cloud_distances(self, data=None, **settings):
        """Cloud Distances on ``data`` (default: the cloud table), finished."""
        w = self.create_widget(OWCloudDistances, stored_settings=settings or None)
        self.send_signal(w.Inputs.data, self.data if data is None else data, widget=w)
        self.wait_until_finished(w, timeout=300000)
        return w

    def orange_distances(self, **settings):
        """Orange's Distances widget on the full table: its output and warnings."""
        settings = {"__version__": 4, **settings}  # else Orange migrates them
        w = self.create_widget(OWDistances, stored_settings=settings)
        self.send_signal(w.Inputs.data, self.full, widget=w)
        self.wait_until_finished(w, timeout=60000)
        warnings = [text(m) for m in w.Warning.active]
        errors = [text(m) for m in w.Error.active]
        return self.get_output(w.Outputs.distances, widget=w), warnings, errors

    def assert_same(self, **settings):
        expected, warnings, _ = self.orange_distances(**settings)
        w = self.cloud_distances(**settings)
        assert not w.Error.active, [str(m) for m in w.Error.active]
        out = self.get_output(w.Outputs.distances, widget=w)
        assert isinstance(out, CloudDistances) and out is w.output
        got = out.download(self.client, self.machine_id)
        np.testing.assert_allclose(got, expected, rtol=1e-10, atol=1e-12)
        assert got.axis == expected.axis and out.shape == expected.shape
        assert got.row_items.domain == expected.row_items.domain
        shown = w.Warning.data_changed.is_shown() and text(w.Warning.data_changed).splitlines()
        assert (shown or []) == warnings
        return w, out

    def test_default_equals_orange(self):
        w, out = self.assert_same()
        assert out.n_items == self.N_ROWS and out.axis == "rows" and out.labels is None
        assert out.metric == "Euclidean (normalized)" and out.table_id == self.table_id
        assert f"{self.N_ROWS:,} rows" in w.info_label.text()
        assert out.resolve(self.machine_id) == out.object_id
        with pytest.raises(DistancesUnavailableError):
            out.resolve("another-machine")

    def test_options_equal_orange(self):
        for axis, metric_id in ((1, MANHATTAN_NORMALIZED), (0, PEARSON), (0, COSINE)):
            self.assert_same(axis=axis, metric_id=metric_id)
        _, out = self.assert_same(axis=1, metric_id=SPEARMAN_ABSOLUTE)
        assert out.labels == ("x1", "x2", "x3") and out.axis == "columns"

    def test_orange_errors(self):
        _, _, errors = self.orange_distances(metric_id=JACCARD)
        w = self.cloud_distances(metric_id=JACCARD)
        assert errors == ["No binary features"] == [text(w.Error.cloud_error)]
        assert self.get_output(w.Outputs.distances, widget=w) is None

    def test_rerun_deletes_the_replaced_matrix(self):
        w = self.cloud_distances()
        first = w.output
        w.axis = 1
        w.commit.now()
        self.wait_until_finished(w, timeout=300000)
        assert w.output.axis == "columns"
        with pytest.raises(CloudNotFoundError):
            self.client.get_object(first.object_id)

    def test_too_large_is_refused_before_running(self):
        big = self.state / "big.csv"
        write_csv(big, 100_000)  # 100,000² x 8 bytes = 75 GB: more than any test machine
        job = self.client.create_table(upload_id=self.client.upload(big).id, name="big")
        table_id = self.client.wait_for_job(job, timeout=300).tables["table"].id
        big_data = type(self.data).open(self.client, table_id, self.machine_id)
        # The widget refuses before it submits the job (its message names a
        # size of the machine's provider that fits; the machine's doesn't) ...
        self.provider.machine_sizes = MACHINE_TYPES
        try:
            w = self.cloud_distances(big_data)
        finally:
            del self.provider.machine_sizes
        message = str(w.Error.cloud_error)
        assert message.startswith("Distances between 100,000 rows need about 156.5 GB")
        assert "The smallest that fits: Large, high memory (256 GB)." in message
        assert self.get_output(w.Outputs.distances, widget=w) is None
        # ... and so does the machine, for a direct API call.
        job = self.client.submit_job(JobKind.DISTANCES, inputs={"data": table_id})
        with pytest.raises(CloudError) as exc:
            self.client.wait_for_job(job, timeout=60)
        assert exc.value.code == ErrorCode.PAYLOAD_TOO_LARGE
        assert exc.value.message.startswith("Distances between 100,000 rows need about")

    def test_local_data(self):
        w = self.cloud_distances(self.full)
        assert "local data" in str(w.Error.cloud_error)
        assert self.get_output(w.Outputs.distances, widget=w) is None
