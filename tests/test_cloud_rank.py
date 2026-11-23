"""Cloud Rank: on a small cloud table its scores equal Orange's Rank on the
full table (classification and regression methods, a learner-based scorer),
and its Reduced Data replays to Orange's Reduced Data. Orange's own Rank,
Discretize and Purge Domain stop with an error on a cloud table where they
compute distributions or contingencies from the placeholder row.
"""

import logging

import numpy as np
import pytest
from AnyQt.QtWidgets import QCheckBox
from Orange.classification import LogisticRegressionLearner
from Orange.data import ContinuousVariable, DiscreteVariable, Domain, StringVariable
from Orange.preprocess import Discretize, Remove, discretize, score
from Orange.statistics import basic_stats, contingency, distribution
from Orange.widgets.data.owrank import OWRank
from test_derive import CloudTableTest

from orangecontrib.cloud.data.remote_table import (
    LOCAL_DATA,
    CloudRef,
    PlaceholderStatisticsError,
    RemoteTable,
    resolve,
)
from orangecontrib.cloud.server.ops.rank import (
    DATA_COPIES,
    check_memory,
    json_scores,
    method_scores,
    scorer_scores,
)
from orangecontrib.cloud.server.ops.rank import METHODS as JOB_METHODS
from orangecontrib.cloud.server.worker import OperationError
from orangecontrib.cloud.widgets.owcloudrank import METHODS, OWCloudRank, decode_scores

CLASSIFICATION = ("Info. gain", "Gain ratio", "Gini", "ANOVA", "χ²", "ReliefF", "FCBF")
REGRESSION = ("Univar. reg.", "RReliefF")


def placeholder():
    domain = Domain(
        [ContinuousVariable("x1"), DiscreteVariable("color", values=("red", "green"))],
        DiscreteVariable("y", values=("no", "yes")),
        [StringVariable("id")],
    )
    return RemoteTable.placeholder(domain, CloudRef("t-1", "data", 1000))


def test_statistics_of_the_placeholder_raise():
    """Distributions and contingencies of a cloud table raise the add-on's
    error (Orange's Rank, Discretize and Purge Domain use them); basic
    statistics (Orange's Data Table) don't."""
    data = placeholder()
    for compute in (
        lambda: distribution.get_distribution(data, "x1"),
        lambda: contingency.get_contingency(data, "color"),
        lambda: score.GainRatio()(data),
        lambda: Discretize(method=discretize.EqualFreq(4))(data),
        lambda: Remove(Remove.RemoveConstant)(data),
    ):
        with pytest.raises(PlaceholderStatisticsError, match="Cloud Rank for Rank"):
            compute()
    assert basic_stats.BasicStats(data, "x1").nans == 1
    local = RemoteTable.from_numpy(
        data.domain, np.ones((2, 2)), np.ones(2), np.array([["a"], ["b"]])
    )
    assert distribution.get_distribution(local, "color")[1] == 2  # local data: as Orange


def test_scores_json_and_memory():
    """Scores survive JSON (missing and infinite ones too); the job refuses
    tables whose copies don't fit."""
    scores = np.array([[0.5, np.nan], [np.inf, -np.inf]])
    np.testing.assert_array_equal(decode_scores(json_scores(scores)), scores)
    assert {m.key for m in METHODS} == set(JOB_METHODS)

    class Data:
        domain = placeholder().domain

        def __len__(self):
            return 1_000_000

    needed = DATA_COPIES * 1_000_000 * 3 * 8
    assert check_memory(Data(), needed) == needed
    with pytest.raises(OperationError, match="needs about 0.3 GB of memory"):
        check_memory(Data(), needed - 1)


def test_failed_methods_are_logged_as_warnings(caplog):
    """A method or scorer that doesn't work on the data gives no scores, and
    the machine's log says why, as a warning."""
    from Orange.data import Table
    from Orange.regression import LinearRegressionLearner

    with caplog.at_level(logging.WARNING, logger="orangecontrib.cloud.server.ops.rank"):
        assert method_scores(Table("housing"), "InfoGain") is None
        assert scorer_scores(Table("iris"), LinearRegressionLearner()) is None
    assert [r.levelno for r in caplog.records] == [logging.WARNING] * 2
    assert caplog.records[0].getMessage().startswith("InfoGain doesn't work on this data: ")


class TestCloudRank(CloudTableTest):
    __test__ = True

    def scores(self, cls, data, methods, scorer=None):
        """The widget's outputs with only ``methods`` (short names) checked
        and the best 2 features chosen."""
        w = self.create_widget(cls)
        if scorer is not None:
            self.send_signal(w.Inputs.scorer, scorer, 0, widget=w)
        self.send_signal(w.Inputs.data, data, widget=w)
        self.wait_until_finished(w, timeout=120000)
        page = w.measuresStack.currentWidget()
        for box in page.findChildren(QCheckBox):
            box.setChecked(box.objectName() in methods)
            self.wait_until_finished(w, timeout=120000)
        w.nSelected = 2
        w.setSelectionMethod(w.SelectNBest)
        w.commit.now()
        assert not w.Error.active, [str(m) for m in w.Error.active]
        return {
            "scores": self.get_output(w.Outputs.scores, widget=w),
            "reduced": self.get_output(w.Outputs.reduced_data, widget=w),
            "features": self.get_output(w.Outputs.features, widget=w),
        }

    def check_same(self, data, full, methods, scorer=None):
        cloud = self.scores(OWCloudRank, data, methods, scorer)
        local = self.scores(OWRank, full, methods, scorer)
        got, expected = cloud["scores"], local["scores"]
        assert [v.name for v in got.domain.attributes] == [
            v.name for v in expected.domain.attributes
        ]
        np.testing.assert_allclose(got.X, expected.X, rtol=1e-9, equal_nan=True)
        np.testing.assert_array_equal(got.metas, expected.metas)
        assert list(cloud["features"]) == list(local["features"])
        # Reduced Data is a recorded step that replays to Orange's output.
        reduced = self.client.table_rows(resolve(cloud["reduced"], self.client), 0, self.N_ROWS)
        assert reduced.domain == local["reduced"].domain
        np.testing.assert_array_equal(reduced.X, local["reduced"].X)
        return cloud

    def test_classification(self):
        """Every classification method and a learner's scores equal Orange's Rank."""
        learner = LogisticRegressionLearner()
        cloud = self.check_same(self.data, self.full, CLASSIFICATION, learner)
        labels = [v.name for v in cloud["scores"].domain.attributes]
        assert labels[: len(CLASSIFICATION)] == list(CLASSIFICATION)
        assert len(labels) == len(CLASSIFICATION) + 1  # logistic regression, binary target

    def test_regression(self):
        """On a recorded step that makes x3 the target, the regression
        methods equal Orange's Rank on the same table."""
        d = self.data.domain
        domain = Domain([d["x1"], d["x2"], d["color"]], d["x3"], [d["label"], d["id"]])
        self.check_same(self.data.transform(domain), self.full.transform(domain), REGRESSION)

    def test_local_data(self):
        w = self.create_widget(OWCloudRank)
        self.send_signal(w.Inputs.data, self.full, widget=w)
        assert LOCAL_DATA in str(w.Error.cloud_error)
        assert self.get_output(w.Outputs.reduced_data, widget=w) is None

    def test_scorer_removed_while_scoring(self):
        """A job still scoring a scorer that was removed meanwhile is dropped,
        instead of filing its scores under an index that no longer exists."""
        w = self.create_widget(OWCloudRank)
        self.send_signal(w.Inputs.scorer, LogisticRegressionLearner(), 0, widget=w)
        self.send_signal(w.Inputs.data, self.data, widget=w)
        self.wait_until_finished(w, timeout=120000)
        # A new scorer at index 0 starts a job for it alone; remove it before
        # the job's result reaches the widget.
        self.send_signal(w.Inputs.scorer, LogisticRegressionLearner(C=2), 0, widget=w)
        assert w.task is not None
        self.send_signal(w.Inputs.scorer, w.Inputs.scorer.closing_sentinel, 0, widget=w)
        self.wait_until_finished(w, timeout=120000)
        self.process_events()
        assert w.scorers == [] and w.scorer_results == {}
        assert not w.Error.active, [str(m) for m in w.Error.active]
