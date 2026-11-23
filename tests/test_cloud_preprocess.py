"""Cloud Preprocess: Orange's Preprocess recipe fitted on all rows on the
machine, against Orange's Preprocess on the full table; steps after it; the
recipe and fitted columns travelling without Qt on the machine; and Orange's
Preprocess with a cloud table connected to it.
"""

import sys

import numpy as np
import pytest
from Orange.data import Table
from Orange.preprocess import Discretize, Impute, Normalize
from Orange.preprocess.preprocess import PreprocessorList
from Orange.widgets.data.owpreprocess import OWPreprocess, _NoneDisc
from Orange.widgets.tests.base import WidgetTest
from test_derive import CloudTableTest

from orangecontrib.cloud.client.api import CloudUnsupportedStepError
from orangecontrib.cloud.data.remote_table import (
    CloudRef,
    PlaceholderStatisticsError,
    RemoteTable,
    is_cloud_table,
    resolve,
)
from orangecontrib.cloud.protocol.pickles import dumps, loads
from orangecontrib.cloud.server.preprocessors import RemoveNumeric
from orangecontrib.cloud.widgets.owcloudpreprocess import OWCloudPreprocess

PREPROCESS = "Orange.widgets.data.owpreprocess"

# Orange's Preprocess widget's saved recipes: impute (average), standardize,
# equal-frequency discretization into 4 intervals; and PCA.
IMPUTE_NORMALIZE_DISCRETIZE = [
    ("orange.preprocess.impute", {"method": 2}),
    ("orange.preprocess.scale", {"method": 2}),
    ("orange.preprocess.discretize", {"method": 2, "n": 4}),
]
PCA = [("orange.preprocess.pca", {"n_components": 3})]


def test_recipe_without_qt(monkeypatch):
    """ "Remove numeric features" is a class of Orange's widget module, which
    the machine (no Qt) can't import: the stand-in takes its place there."""
    table = Table("heart_disease")
    recipe = PreprocessorList([Impute(), Discretize(_NoneDisc(), remove_const=False)])
    data = dumps(recipe)
    original = sys.modules[PREPROCESS]
    monkeypatch.setitem(sys.modules, PREPROCESS, None)  # import fails
    restored = loads(data)
    assert type(restored.preprocessors[1].method) is RemoveNumeric
    assert restored(table).domain == recipe(table).domain
    back = dumps(restored)
    monkeypatch.setitem(sys.modules, PREPROCESS, original)
    assert type(loads(back).preprocessors[1].method) is _NoneDisc


def test_refs():
    """Objects both sides have travel as references to them."""
    table = Table("iris")
    columns = [*table.domain.variables, *table.domain.metas]
    data = dumps(("transform", table.domain), {"data": columns})
    assert loads(data, {"data": columns})[1].attributes[0] is columns[0]
    with pytest.raises(Exception, match="refers to an unknown object"):
        loads(data)


class TestOrangePreprocessWithCloudData(WidgetTest):
    """Orange's Preprocess sends its recipe only after applying it to its
    data, so with a cloud table connected a counting step stops it with the
    add-on's error, which says to build the recipe without data."""

    def test_counting_step(self):
        domain = Table("heart_disease").domain
        data = RemoteTable.placeholder(domain, CloudRef("t-1", "heart", 303))
        recipe = [("orange.preprocess.scale", {})]
        settings = {"storedsettings": {"preprocessors": recipe}}
        w = self.create_widget(OWPreprocess, stored_settings=settings)
        self.send_signal(w.Inputs.data, data, widget=w)
        assert isinstance(self.get_output(w.Outputs.preprocessor, widget=w), Normalize)

        # The user adds Discretize (equal frequency) to the recipe.
        w.set_model(w.load({"preprocessors": [*recipe, ("orange.preprocess.discretize", {})]}))
        with pytest.raises(PlaceholderStatisticsError, match="Preprocess without data connected"):
            w.apply()
        # The changed recipe isn't sent (the doc says so) ...
        assert isinstance(self.get_output(w.Outputs.preprocessor, widget=w), Normalize)
        # ... until the data is disconnected.
        self.send_signal(w.Inputs.data, None, widget=w)
        assert isinstance(self.get_output(w.Outputs.preprocessor, widget=w), PreprocessorList)


class TestCloudPreprocess(CloudTableTest):
    __test__ = True

    def recipe(self, items, data=None):
        """Orange's Preprocess widget: its recipe, or its output for ``data``."""
        settings = {"storedsettings": {"preprocessors": items}}
        w = self.create_widget(OWPreprocess, stored_settings=settings)
        if data is None:
            return self.get_output(w.Outputs.preprocessor, widget=w)
        self.send_signal(w.Inputs.data, data, widget=w)
        return self.get_output(w.Outputs.preprocessed_data, widget=w)

    def cloud_preprocess(self, data, recipe):
        w = self.create_widget(OWCloudPreprocess)
        self.send_signal(w.Inputs.data, data, widget=w)
        self.send_signal(w.Inputs.preprocessor, recipe, widget=w)
        self.wait_until_finished(w, timeout=120000)
        assert not w.Error.active, [str(m) for m in w.Error.active]
        return self.get_output(w.Outputs.preprocessed_data, widget=w)

    def rows(self, data):
        table_id = resolve(data, self.client, self.machine_id)
        return self.client.table_rows(table_id, 0, self.N_ROWS)

    def assert_same(self, cloud, local):
        assert cloud.domain == local.domain
        np.testing.assert_array_equal(cloud.X, local.X)
        np.testing.assert_array_equal(cloud.Y, local.Y)
        np.testing.assert_array_equal(cloud.metas, local.metas)

    def test_same_as_orange(self):
        recipe = self.recipe(IMPUTE_NORMALIZE_DISCRETIZE)
        local = self.recipe(IMPUTE_NORMALIZE_DISCRETIZE, self.full)
        cloud = self.cloud_preprocess(self.data, recipe)
        assert is_cloud_table(cloud) and len(cloud) == 1
        assert [kind for kind, _ in cloud.steps] == ["preprocess"]
        assert cloud.ref.n_rows == self.N_ROWS and cloud.domain == local.domain
        assert all(var.is_discrete for var in cloud.domain.attributes)
        self.assert_same(self.rows(cloud), local)
        # Orange's own steps after it replay on the fitted columns.
        self.assert_same(self.rows(self.select_columns(cloud)), self.select_columns(local))
        continuized = self.continuize(cloud)
        self.assert_same(self.rows(continuized), self.continuize(local))
        # ... but a step learned from the placeholder after it is still refused.
        normalized = Normalize()(self.cloud_preprocess(self.data, self.recipe(PCA)))
        with pytest.raises(CloudUnsupportedStepError, match="made by a step that learns"):
            resolve(normalized, self.client, self.machine_id)

    def test_steps_after_pca(self):
        """PCA's columns compare equal only to themselves, so the steps after
        it refer to the machine's fitted columns rather than copies."""
        cloud = self.cloud_preprocess(self.data, self.recipe(PCA))
        assert [v.name for v in cloud.domain.attributes] == ["PC1", "PC2", "PC3"]
        fitted = self.rows(cloud)
        selected = self.select_columns(cloud, drop="PC2")
        np.testing.assert_array_equal(self.rows(selected).X, fitted.X[:, [0, 2]])
        info = self.client.table_info(resolve(selected, self.client, self.machine_id))
        assert info.steps == ["transform: 4 columns"]  # from the fitted table

    def test_local_data_and_no_recipe(self):
        w = self.create_widget(OWCloudPreprocess)
        self.send_signal(w.Inputs.data, self.full, widget=w)
        assert "local data" in str(w.Error.cloud_error)
        assert self.get_output(w.Outputs.preprocessed_data, widget=w) is None
        self.send_signal(w.Inputs.data, self.data, widget=w)
        assert self.get_output(w.Outputs.preprocessed_data, widget=w) is self.data
