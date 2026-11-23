"""Cloud Predictions: the ``predict`` operation, and the widget against the
machine's server (with its job system) running in-process over HTTPS:
predictions kept on the machine as a new cloud table that Cloud Data Table
shows, with the same predictions and scores as Orange's own Predictions
widget.

``TestCloudPredictionsMillion`` predicts 1M rows:
``pytest -m slow tests/test_cloud_predictions.py``.
"""

import shutil
import tempfile
from pathlib import Path

import numpy as np
import pytest
from conftest import SPEC, https_server
from Orange.base import Model
from Orange.classification import LogisticRegressionLearner, TreeLearner
from Orange.data import Table
from Orange.regression import LinearRegressionLearner
from Orange.widgets.evaluate.owpredictions import OWPredictions
from Orange.widgets.evaluate.utils import SelectableColumnsHeader
from Orange.widgets.tests import base  # not WidgetTest itself: pytest would collect it
from test_cloud_file import write_csv
from test_widget_base import TOKEN, SharedMachineProvider

from orangecontrib.cloud.client.session import CloudSession, HandleStore
from orangecontrib.cloud.data.cloud_model import CloudModel
from orangecontrib.cloud.data.remote_table import RemoteTable, is_cloud_table
from orangecontrib.cloud.protocol.jobs import JobKind
from orangecontrib.cloud.protocol.tables import ColumnSpec
from orangecontrib.cloud.server.app import ServerConfig, create_machine_app
from orangecontrib.cloud.server.ops import predict
from orangecontrib.cloud.server.worker import JobContext, WorkerStore
from orangecontrib.cloud.widgets.owclouddatatable import OWCloudDataTable
from orangecontrib.cloud.widgets.owcloudpredictions import OWCloudPredictions


def test_predict_operation_in_blocks(monkeypatch):
    """Predicting block by block gives the predictions of one call on all
    rows; a model for another target fails alone."""
    monkeypatch.setattr(predict, "BLOCK_BYTES", 1)  # blocks of 1000 rows
    data = Table("housing")
    data = Table.concatenate([data] * 5)  # 2,530 rows: 3 blocks
    store = WorkerStore()
    store.tables["t-1"] = data
    store.objects["o-1"] = model = LinearRegressionLearner()(data)
    store.objects["o-2"] = LogisticRegressionLearner()(Table("iris"))
    ctx = JobContext("j-1", "predict", {"data": "t-1", "models": ["o-1", "o-2"]}, {}, {}, store)
    values = predict.predict_operation(ctx)
    assert values["rows"] == len(data)
    assert values["models"][0]["failed"] is None and values["models"][0]["scored"]
    assert values["models"][1]["failed"] and not values["models"][1]["scored"]
    table = store.tables[ctx.tables["table"].id]
    assert [v.name for v in table.domain.metas] == ["linear regression"]
    np.testing.assert_allclose(table.get_column("linear regression"), model(data))
    assert np.shares_memory(table.X, data.X)  # the attributes aren't copied
    ids = [s["id"] for s in values["scorers"]]
    rmse = values["scores"][""][0][ids.index("RMSE")]
    assert rmse == pytest.approx(np.sqrt(np.mean((model(data) - data.Y) ** 2)))
    assert values["scores"][""][1] is None


class TestCloudPredictions(base.WidgetTest):
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
        client = self.session.client
        if not hasattr(type(self), "table_id"):  # load once per class
            job = client.create_table(
                upload_id=client.upload(self.csv).id,
                name="data",
                columns=[ColumnSpec(name="label", role="target")],
            )
            type(self).table_id = client.wait_for_job(job, timeout=300).tables["table"].id
        self.machine_id = self.session.handle.machine_id
        self.data = RemoteTable.open(client, self.table_id, self.machine_id)

    def tearDown(self):
        super().tearDown()
        self.session.disconnect(forget=True, wait=True)
        self.session.shutdown()
        CloudSession._instance = self._previous
        shutil.rmtree(self.state, ignore_errors=True)

    def fit(self, learner) -> CloudModel:
        """Train on the machine, as Cloud Learner does."""
        client = self.session.client
        job = client.submit_job(
            JobKind.FIT, inputs={"data": self.table_id}, objects={"learner": learner}
        )
        result = client.wait_for_job(job, timeout=600)
        info = result.objects["model"]
        return CloudModel(
            object_id=info.id,
            name=learner.name,
            domain=self.data.domain,
            learner_name=type(learner).__name__,
            model_type=info.type,
            table_id=self.table_id,
            n_rows=self.N_ROWS,
            machine_id=self.machine_id,
            size_bytes=info.size_bytes,
        )

    def predict(self, models, timeout=300000):
        w = self.create_widget(OWCloudPredictions)
        self.send_signal(w.Inputs.data, self.data, widget=w)
        for i, model in enumerate(models):
            self.send_signal(w.Inputs.predictors, model, i, widget=w)
        self.wait_until_finished(w, timeout=timeout)
        assert not w.Error.active, [str(m) for m in w.Error.active]
        assert w.predicted is not None
        return w

    def models(self):
        return [self.fit(LogisticRegressionLearner()), self.fit(TreeLearner(max_depth=5))]

    def test_predictions(self):
        models = self.models()
        w = self.predict(models)
        out = self.get_output(w.Outputs.predictions, widget=w)
        # A new cloud table with all rows, on the machine.
        assert is_cloud_table(out) and out.ref.n_rows == self.N_ROWS
        assert out.ref.table_id != self.table_id and out.ref.machine_id == self.machine_id
        metas = [v.name for v in out.domain.metas]
        assert metas == [
            "id",
            "logistic regression",
            "logistic regression (no)",
            "logistic regression (yes)",
            "tree",
            "tree (no)",
            "tree (yes)",
        ]
        assert f"Predicted {self.N_ROWS:,} rows with 2 models" in w.info_label.text()
        assert f"Scores on {self.N_ROWS:,} rows" in w.info_label.text()
        assert w.cloud_status.startswith("Computed with Orange")
        # Scores on all rows.
        model = w.score_table.model
        assert model.rowCount() == 2
        header = [model.horizontalHeaderItem(c).text() for c in range(model.columnCount())]
        assert {"CA", "AUC", "F1", "Prec", "Recall"} <= set(header)
        scores = w.scores()
        assert set(scores) == {"logistic regression", "tree"}
        assert all(0 <= s["CA"] <= 1 for s in scores.values())
        assert set(w.scores("yes")) == set(scores)

        # Cloud Data Table shows the predictions.
        table = self.create_widget(OWCloudDataTable)
        self.send_signal(table.Inputs.data, out, widget=table)
        self.wait_until_finished(table, timeout=60000)
        assert not table.Error.active
        assert table.n_rows == self.N_ROWS
        assert f"{self.N_ROWS:,} rows, 11 columns" in table.info_label.text()
        page = table.model.page
        np.testing.assert_allclose(
            page.get_column("tree (no)") + page.get_column("tree (yes)"), 1.0
        )

        # Replaced predictions are deleted on the machine at the next run.
        self.send_signal(w.Inputs.predictors, models[0], 1, widget=w)
        self.wait_until_finished(w, timeout=120000)
        listed = {t.id for t in self.session.client.list_tables()}
        assert out.ref.table_id not in listed

    def test_same_as_orange_predictions(self):
        """The predictions and scores equal those of Orange's Predictions
        widget on the same rows with the downloaded models."""
        if self.N_ROWS > 100_000:
            self.skipTest("compared on the small table")
        models = self.models()
        w = self.predict(models)
        client = self.session.client
        out = self.get_output(w.Outputs.predictions, widget=w)
        cloud = client.table_rows(out.ref.table_id, 0, self.N_ROWS)
        local = client.table_rows(self.table_id, 0, self.N_ROWS)
        local_models = [m.download(client, self.machine_id) for m in models]
        assert all(isinstance(m, Model) for m in local_models)

        ow = self.create_widget(OWPredictions)
        self.send_signal(ow.Inputs.data, local, widget=ow)
        for i, m in enumerate(local_models):
            self.send_signal(ow.Inputs.predictors, m, i, widget=ow)
        orange = self.get_output(ow.Outputs.selected_predictions, widget=ow)
        shared = [v.name for v in orange.domain.metas if v.name in cloud.domain]
        assert "tree" in shared and "logistic regression (yes)" in shared
        for name in shared:
            if name == "id":
                assert list(cloud.get_column(name)) == list(orange.get_column(name))
            else:
                np.testing.assert_allclose(cloud.get_column(name), orange.get_column(name))
        # Orange's scores table shows three decimals.
        omodel = ow.score_table.model
        role = SelectableColumnsHeader.ShownHintRole
        header = [omodel.horizontalHeaderItem(c).data(role) for c in range(omodel.columnCount())]
        compared = 0
        for row, m in enumerate(models):
            ours = w.scores()[m.name]
            for col, scorer_id in enumerate(header):
                if scorer_id in ours and ours[scorer_id] is not None:
                    assert omodel.item(row, col).text() == f"{ours[scorer_id]:.3f}", scorer_id
                    compared += 1
        assert compared >= 2 * 5  # CA, AUC, F1, Prec, Recall at least

    def test_errors(self):
        w = self.create_widget(OWCloudPredictions)
        assert w.info_label.text() == "No data and no models."
        model = self.fit(TreeLearner(max_depth=3))
        self.send_signal(w.Inputs.data, Table("iris"), widget=w)
        self.send_signal(w.Inputs.predictors, model, 0, widget=w)
        assert "local data, not a cloud table" in str(w.Error.cloud_error)
        assert w.task is None
        # A model deleted on the machine.
        self.session.client.delete_object(model.object_id)
        self.send_signal(w.Inputs.data, self.data, widget=w)
        self.wait_until_finished(w, timeout=60000)
        assert "no longer available" in str(w.Error.cloud_error)
        assert self.get_output(w.Outputs.predictions, widget=w) is None


@pytest.mark.slow
class TestCloudPredictionsMillion(TestCloudPredictions):
    """Predictions for 1M rows, viewed in Cloud Data Table, with scores."""

    N_ROWS = 1_000_000

    def test_errors(self):
        pass
