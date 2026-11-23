"""Cloud Test & Score against the machine's server (with its job system)
running in-process over HTTPS: cross-validation with every learner × fold as
a Dask task, scores equal to Orange's own ``CrossValidation``, the Evaluation
Results in ROC Analysis, test on test data, and confirming large downloads.

``TestCloudTestAndScoreMillion`` runs 10-fold cross-validation of two learners
on 1M rows: ``pytest -m slow tests/test_cloud_test_and_score.py``.
"""

import os
import shutil
import tempfile
from pathlib import Path

import numpy as np
import pytest
from conftest import SPEC, https_server
from Orange.classification import LogisticRegressionLearner, TreeLearner
from Orange.data import Table
from Orange.evaluation import CrossValidation, Results
from Orange.regression import LinearRegressionLearner
from Orange.widgets.evaluate.owrocanalysis import OWROCAnalysis
from Orange.widgets.evaluate.utils import scorer_caller, usable_scorers
from Orange.widgets.tests import base  # not WidgetTest itself: pytest would collect it
from test_cloud_file import write_csv
from test_widget_base import TOKEN, SharedMachineProvider

from orangecontrib.cloud.client.session import CloudSession, HandleStore
from orangecontrib.cloud.data.remote_table import RemoteTable
from orangecontrib.cloud.protocol.tables import ColumnSpec
from orangecontrib.cloud.server import forked
from orangecontrib.cloud.server.app import ServerConfig, create_machine_app
from orangecontrib.cloud.server.ops.parts import parallel_plan
from orangecontrib.cloud.widgets import owcloudtestandscore
from orangecontrib.cloud.widgets.owcloudtestandscore import OWCloudTestAndScore


def test_parallel_plan():
    # 20 parts on 8 threads with plenty of memory: 8 at a time, 1 core each.
    assert parallel_plan(20, 8, 10**6, 10**12) == (8, 1)
    # 2 parts on 32 threads: each part's learner gets 16 cores.
    assert parallel_plan(2, 32, 10**6, 10**12) == (2, 16)
    # Memory for only 3 parts at a time.
    assert parallel_plan(20, 16, 10**9, 3 * 10**9 + 1) == (3, 5)
    assert parallel_plan(20, 16, 10**9, 0) == (1, 16)


class TestCloudTestAndScore(base.WidgetTest):
    N_ROWS = 2_000
    WORKER_THREADS = 2

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.folder = folder = Path(tempfile.mkdtemp())
        cls.tear_down_stack.callback(shutil.rmtree, folder, ignore_errors=True)
        config = ServerConfig(
            token=TOKEN,
            machine="test-machine",
            data_dir=folder / "machine",
            worker_threads=cls.WORKER_THREADS,
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

    def learners(self):
        return [LogisticRegressionLearner(), TreeLearner(max_depth=5)]

    def evaluate(self, learners, test_data=None, timeout=300000, **settings):
        w = self.create_widget(OWCloudTestAndScore, stored_settings=settings)
        self.send_signal(w.Inputs.train_data, self.data, widget=w)
        if test_data is not None:
            self.send_signal(w.Inputs.test_data, test_data, widget=w)
        for i, learner in enumerate(learners):
            self.send_signal(w.Inputs.learner, learner, i, widget=w)
        self.wait_until_finished(w, timeout=timeout)
        assert not w.Error.active, [str(m) for m in w.Error.active]
        assert w.evaluation is not None
        return w

    def test_cross_validation(self):
        learners = self.learners()
        w = self.evaluate(learners)
        values = w.evaluation.values
        assert values["rows"] == self.N_ROWS and values["folds"] == 10
        # The first part of each learner alone, then side by side.
        assert 1 < values["parallel"] <= self.WORKER_THREADS
        assert all(b > 0 for b in values["part_memory_bytes"])
        # Orange's tree holds the GIL: in forked processes on Linux.
        assert values["processes"] == [False, forked.available()]
        assert f"10-fold cross-validation on {self.N_ROWS:,} rows: 20 parts" in w.info_label.text()
        assert w.cloud_status.startswith("Computed with Orange")
        # The full results, downloaded (small) and sent; learners are the inputs'.
        results = self.get_output(w.Outputs.evaluations_results, widget=w)
        assert isinstance(results, Results) and results.predicted.shape == (2, self.N_ROWS)
        assert results.learners == learners
        assert sorted(results.row_indices) == list(range(self.N_ROWS))
        # The scores table: one row per learner, the classification scores.
        model = w.score_table.model
        assert model.rowCount() == 2
        header = [model.horizontalHeaderItem(c).text() for c in range(model.columnCount())]
        assert {"CA", "AUC", "F1", "Prec", "Recall"} <= set(header)
        scores = w.scores()
        assert set(scores) == {"logistic regression", "tree"}
        assert all(0 <= scores[n]["CA"] <= 1 for n in scores)
        # Target class scores (one against the rest) without a new job.
        assert set(w.scores("yes")) == set(scores)
        w.class_selection = "yes"
        w._update_scores()
        assert model.rowCount() == 2 and w.task is None
        # ROC Analysis shows the output.
        roc = self.create_widget(OWROCAnalysis)
        self.send_signal(roc.Inputs.evaluation_results, results, widget=roc)
        assert roc.results is not None and not roc.Error.active
        assert roc.classifier_names == ["logistic regression", "tree"]
        assert roc._curve_data

    def test_scores_match_orange(self):
        """On a small table, the predictions and scores equal those of
        Orange's own CrossValidation (Test & Score's seed) on the same rows."""
        if self.N_ROWS > 100_000:
            self.skipTest("compared on the small table")
        learners = self.learners()
        w = self.evaluate(learners, n_folds=2, stratified=True)  # index 2: 5 folds
        local = self.session.client.table_rows(self.table_id, 0, self.N_ROWS)
        reference = CrossValidation(k=5, stratified=True, random_state=42)(local, learners)
        results = self.get_output(w.Outputs.evaluations_results, widget=w)
        np.testing.assert_array_equal(results.row_indices, reference.row_indices)
        np.testing.assert_array_equal(results.actual, reference.actual)
        np.testing.assert_allclose(results.predicted, reference.predicted)
        np.testing.assert_allclose(results.probabilities, reference.probabilities)
        scorers = usable_scorers(local.domain)
        for learner, single in zip(learners, reference.split_by_model(), strict=True):
            expected = {s.__name__: scorer_caller(s, single)()[0] for s in scorers}
            got = w.scores()[learner.name]
            for name, value in expected.items():
                assert got[name] == pytest.approx(value), name

    def test_one_part_at_a_time(self):
        """``max_parallel`` 1 gives the same results as parts side by side."""
        client = self.session.client
        results = []
        for params in ({"max_parallel": 1}, {}):
            job = client.submit_job(
                "evaluate",
                inputs={"data": self.table_id},
                params={"folds": 4, **params},
                objects={"learners": self.learners()},
            )
            done = client.wait_for_job(job, timeout=300)
            assert (done.values["parallel"] == 1) == bool(params)
            results.append(client.get_object(done.objects["results"].id))
            client.delete_object(done.objects["results"].id)
        np.testing.assert_array_equal(results[0].predicted, results[1].predicted)
        np.testing.assert_array_equal(results[0].probabilities, results[1].probabilities)

    def test_test_on_test_data(self):
        w = self.evaluate(self.learners(), test_data=self.data, resampling=1)
        values = w.evaluation.values
        assert values["method"] == "test_on_test_data" and values["folds"] == 1
        assert values["parallel"] == 1  # one part per learner: each alone
        results = self.get_output(w.Outputs.evaluations_results, widget=w)
        assert results.predicted.shape == (2, self.N_ROWS)
        assert "Tested on test data" in w.info_label.text()

    def test_large_results_ask_first(self):
        threshold = owcloudtestandscore.CONFIRM_DOWNLOAD_BYTES
        owcloudtestandscore.CONFIRM_DOWNLOAD_BYTES = 1000  # make the results "large"
        self.addCleanup(setattr, owcloudtestandscore, "CONFIRM_DOWNLOAD_BYTES", threshold)
        w = self.evaluate(self.learners())
        size = w.evaluation.results_info.size_bytes
        assert size > 1000 and w.evaluation.local is None
        assert self.get_output(w.Outputs.evaluations_results, widget=w) is None
        assert w.Information.not_downloaded.is_shown() and w.download_button.isEnabled()
        assert w.scores()["tree"]["CA"] is not None  # the scores don't need the download
        asked = []
        w.confirm_download = lambda size: asked.append(size) or False
        assert not w.download() and w.task is None and asked == [size]
        w.confirm_download = lambda size: asked.append(size) or True
        assert w.download()
        self.wait_until_finished(w, timeout=120000)
        results = self.get_output(w.Outputs.evaluations_results, widget=w)
        assert isinstance(results, Results) and len(asked) == 2
        assert not w.Information.not_downloaded.is_shown()
        assert w.cloud_status.startswith("Downloaded from")

    def test_errors(self):
        w = self.create_widget(OWCloudTestAndScore)
        assert w.info_label.text() == "No data and no learners."
        self.send_signal(w.Inputs.train_data, Table("iris"), widget=w)
        self.send_signal(w.Inputs.learner, TreeLearner(), 0, widget=w)
        assert "local data, not a cloud table" in str(w.Error.cloud_error)
        assert w.task is None
        self.send_signal(w.Inputs.train_data, self.data, widget=w)
        w.resampling = w.TestOnTest
        w.evaluate()
        assert w.Error.test_data_missing.is_shown() and w.task is None
        # A learner that fails (regression on a categorical target) fails alone.
        w.resampling = w.KFold
        self.send_signal(w.Inputs.learner, LinearRegressionLearner(), 1, widget=w)
        self.wait_until_finished(w, timeout=120000)
        assert w.Error.learners_failed.is_shown()
        assert "linear regression failed" in str(w.Error.learners_failed)
        assert w.scores()["tree"]["CA"] is not None
        assert w.scores()["linear regression"]["CA"] is None
        assert w.score_table.model.item(1, 0).text() == "linear regression (error)"
        # Like Orange's Test & Score, the output leaves the failed learner out.
        results = self.get_output(w.Outputs.evaluations_results, widget=w)
        assert results.learner_names == ["tree"] and results.predicted.shape[0] == 1


@pytest.mark.slow
class TestCloudTestAndScoreMillion(TestCloudTestAndScore):
    """10-fold cross-validation of two learners on 1M rows, with the
    machine's default threads (all CPUs)."""

    N_ROWS = 1_000_000
    WORKER_THREADS = os.cpu_count()

    def test_large_results_ask_first(self):
        pass

    def test_errors(self):
        pass

    def test_one_part_at_a_time(self):
        pass
