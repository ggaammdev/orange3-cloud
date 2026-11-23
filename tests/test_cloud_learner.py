"""Cloud Learner against the machine's server (with its job system) running
in-process over HTTPS: training on a cloud table, downloading the model
(Tree Viewer shows it), and confirming large downloads.

``TestCloudLearnerMillion`` trains on 1M rows: ``pytest -m slow tests/test_cloud_learner.py``.
"""

import shutil
import tempfile
from pathlib import Path

import pytest
from conftest import SPEC, https_server
from Orange.classification import KNNLearner, TreeLearner
from Orange.data import Table
from Orange.modelling import RandomForestLearner
from Orange.regression import LinearRegressionLearner
from Orange.tree import TreeModel
from Orange.widgets.tests import base  # not WidgetTest itself: pytest would collect it
from Orange.widgets.visualize.owtreeviewer import OWTreeGraph
from test_cloud_file import write_csv
from test_widget_base import TOKEN, SharedMachineProvider

from orangecontrib.cloud.client.session import CloudSession, HandleStore
from orangecontrib.cloud.data.cloud_model import CloudModel
from orangecontrib.cloud.data.remote_table import RemoteTable
from orangecontrib.cloud.protocol.tables import ColumnSpec
from orangecontrib.cloud.server.app import ServerConfig, create_machine_app
from orangecontrib.cloud.server.ops.learn import use_all_cores
from orangecontrib.cloud.widgets import owcloudlearner
from orangecontrib.cloud.widgets.owcloudlearner import OWCloudLearner


def test_use_all_cores():
    data = Table("iris")
    fitter = RandomForestLearner()
    use_all_cores(fitter, data)
    assert fitter.get_learner(data).params["n_jobs"] == -1
    assert fitter(data).skl_model.n_jobs == -1
    tree = TreeLearner()
    use_all_cores(tree, data)  # not a scikit-learn learner: unchanged
    assert "n_jobs" not in tree.params


class TestCloudLearner(base.WidgetTest):
    N_ROWS = 2_000

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.folder = folder = Path(tempfile.mkdtemp())
        cls.tear_down_stack.callback(shutil.rmtree, folder, ignore_errors=True)
        config = ServerConfig(
            token=TOKEN, machine="test-machine", data_dir=folder / "machine", worker_threads=2
        )
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
        if not hasattr(type(self), "table_id"):  # load once per class
            client = self.session.client
            job = client.create_table(
                upload_id=client.upload(self.csv).id,
                name="data",
                columns=[ColumnSpec(name="label", role="target")],
            )
            type(self).table_id = client.wait_for_job(job, timeout=300).tables["table"].id
        self.data = RemoteTable.open(
            self.session.client, self.table_id, self.session.handle.machine_id
        )

    def tearDown(self):
        super().tearDown()
        self.session.disconnect(forget=True, wait=True)
        self.session.shutdown()
        CloudSession._instance = self._previous
        shutil.rmtree(self.state, ignore_errors=True)

    def train(self, learner, timeout=120000):
        w = self.create_widget(OWCloudLearner)
        self.send_signal(w.Inputs.data, self.data, widget=w)
        self.send_signal(w.Inputs.learner, learner, widget=w)
        self.wait_until_finished(w, timeout=timeout)
        assert not w.Error.active, [str(m) for m in w.Error.active]
        model = self.get_output(w.Outputs.model, widget=w)
        assert isinstance(model, CloudModel)
        return w, model

    def download(self, w, timeout=120000):
        assert w.download()
        self.wait_until_finished(w, timeout=timeout)
        assert not w.Error.active, [str(m) for m in w.Error.active]
        return self.get_output(w.Outputs.local_model, widget=w)

    def test_random_forest(self):
        w, model = self.train(RandomForestLearner(n_estimators=10))
        assert model.n_rows == self.N_ROWS and model.table_id == self.table_id
        assert model.domain is self.data.domain and model.size_bytes > 0
        assert model.model_type.endswith("RandomForestClassifier")
        assert f"Trained random forest on {self.N_ROWS:,} rows in" in w.info_label.text()
        assert "Model size:" in w.info_label.text()
        assert w.cloud_status.startswith("Computed with Orange")
        assert self.get_output(w.Outputs.local_model, widget=w) is None
        # Training again (a new learner) replaces the model and frees the old one.
        old = model
        self.send_signal(w.Inputs.learner, RandomForestLearner(n_estimators=5), widget=w)
        self.wait_until_finished(w, timeout=120000)
        model = self.get_output(w.Outputs.model, widget=w)
        assert model.object_id != old.object_id
        with pytest.raises(Exception, match="no longer available"):
            old.download(self.session.client)

    def test_tree_download_shows_in_tree_viewer(self):
        w, model = self.train(TreeLearner(max_depth=4))
        confirmations = []
        w.confirm_download = lambda size: confirmations.append(size) or True
        local = self.download(w)
        assert not confirmations  # small: no question
        assert isinstance(local, TreeModel) and len(local.instances) == self.N_ROWS
        assert "Downloaded to this computer" in w.info_label.text()
        assert w.cloud_status.startswith("Downloaded from")
        viewer = self.create_widget(OWTreeGraph)
        self.send_signal(viewer.Inputs.tree, local, widget=viewer)
        assert viewer.root_node is not None and len(viewer.scene.nodes()) > 1
        assert viewer.tree_adapter.num_nodes > 1

    def large_learner(self):
        return KNNLearner()

    def test_large_download_asks_first(self):
        w, model = self.train(self.large_learner())
        threshold = owcloudlearner.CONFIRM_DOWNLOAD_BYTES
        if self.N_ROWS < 1_000_000:  # make the small model "large"
            owcloudlearner.CONFIRM_DOWNLOAD_BYTES = model.size_bytes - 1
            self.addCleanup(setattr, owcloudlearner, "CONFIRM_DOWNLOAD_BYTES", threshold)
        else:
            assert model.size_bytes > threshold
        asked = []
        w.confirm_download = lambda size: asked.append(size) or False
        assert not w.download() and w.task is None
        assert asked == [model.size_bytes]
        assert self.get_output(w.Outputs.local_model, widget=w) is None
        w.confirm_download = lambda size: asked.append(size) or True
        local = self.download(w)
        assert len(asked) == 2 and local is not None
        assert model.model_type == f"{type(local).__module__}.{type(local).__qualname__}"

    def test_errors(self):
        w = self.create_widget(OWCloudLearner)
        assert w.info_label.text() == "No data and no learner."
        self.send_signal(w.Inputs.data, Table("iris"), widget=w)
        self.send_signal(w.Inputs.learner, TreeLearner(), widget=w)
        assert "local data, not a cloud table" in str(w.Error.cloud_error)
        assert w.task is None
        # A regression learner can't use a categorical target: no job is sent.
        self.send_signal(w.Inputs.data, self.data, widget=w)
        self.send_signal(w.Inputs.learner, LinearRegressionLearner(), widget=w)
        assert w.Error.incompatible.is_shown() and not w.Error.cloud_error.is_shown()
        assert w.task is None and self.get_output(w.Outputs.model, widget=w) is None


@pytest.mark.slow
class TestCloudLearnerMillion(TestCloudLearner):
    """The same on about 1M rows. The large model is a random forest (about
    425 MB on 1M rows; k-nearest neighbours is about 60 MB), above the real
    download threshold."""

    N_ROWS = 1_000_000

    def large_learner(self):
        return RandomForestLearner(n_estimators=10)

    def test_errors(self):
        pass
