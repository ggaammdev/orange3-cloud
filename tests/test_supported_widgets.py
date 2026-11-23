"""Orange's own widgets on a cloud table, as docs/supported-widgets.md lists them.

Each check gives the cloud table (and, where needed, a learner, model,
distances or results) to one of Orange's widgets, offscreen, and looks at
what comes out:

* "replayed": an output is a cloud table with a new recorded step, the
  machine replays the steps, and the result equals the same widget's output
  on the full table here,
* "refused": an output is a cloud table that the machine refuses
  (``unsupported_step``),
* "nothing": no new step comes out (no output, local data, the input passed
  through, or an error message); the doc says which Cloud widget does the job,
* "stopped": the widget stopped with the add-on's error for statistics
  computed from the placeholder row (``PlaceholderStatisticsError``, which
  names the Cloud widget to use; Orange shows it in its error report),
* "crash": the widget raised another exception (Orange's error report).

A learner widget's Learner output must also work in Cloud Test & Score.

A replayed table that differs from Orange's output on the full table is a
wrong result. Only checks whose ``outcome`` is "wrong" may give one, and the
doc lists their widgets as not supported. The default run checks that the doc and the
checks name the same widgets, that the doc's "replayed" widgets replay and
that the widgets on Cloud Test & Score's results work;
``-m slow`` checks every widget and prints what each one did.
"""

from __future__ import annotations

import copy
import importlib
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from AnyQt.QtCore import Qt
from Orange.classification import TreeLearner
from Orange.data import Table
from Orange.widgets.data.owfeatureconstructor import ContinuousDescriptor, DiscreteDescriptor
from orangewidget.utils.signals import Input, MultiInput, Output
from test_derive import CloudTableTest

from orangecontrib.cloud.client.api import CloudUnsupportedStepError
from orangecontrib.cloud.data.remote_table import (
    PlaceholderStatisticsError,
    is_cloud_table,
    resolve,
)
from orangecontrib.cloud.widgets.owcloudtestandscore import OWCloudTestAndScore

DOC = Path(__file__).parents[1] / "docs" / "supported-widgets.md"

#: The doc's sections, by a word in their heading, and the outcomes their
#: widgets may give.
SECTIONS = {
    "replayed": {"replayed"},
    "Preprocess": {"refused", "nothing"},
    "nothing": {"nothing", "crash"},
    "outputs": {"nothing"},
    "Not supported": {"nothing", "refused", "crash", "stopped"},
}


@dataclass
class Check:
    """How to try one of Orange's widgets on cloud data."""

    widget: str  #: "module.Class" under Orange.widgets
    #: Sets the widget up after it got its inputs (and commits).
    configure: Callable[[Any], None] | None = None
    settings: dict = field(default_factory=dict)  #: stored settings
    #: Other inputs, by attribute name: (test, data) → {name: value}.
    extra: Callable[[Any, Table], dict[str, Any]] | None = None
    #: The outcome of a check, instead of its doc section's; "wrong" only
    #: this way, for widgets that choose columns from the placeholder's
    #: values in ways the add-on can't catch.
    outcome: str | None = None
    timeout: int = 60000  #: ms to wait for the widget (Orange's Rank stays busy after an error)


def _commit(w: Any) -> None:
    for name in ("commit", "apply", "unconditional_commit"):
        method = getattr(w, name, None)
        if method is not None:
            getattr(method, "now", method)()
            return


# --------------------------------------------------------------------------
# Set-ups that make Orange's widgets change something (defaults often don't)


def _select_columns(w):
    keep = [v for v in w.used_attrs if v.name != "x3"]
    del w.used_attrs[:]
    w.used_attrs.extend(keep)
    _commit(w)


def _select_rows(w):
    w.remove_all_rows()
    w.add_row(w.data.domain["x2"], 4, ("8",))  # x2 > 8
    w.add_row(w.data.domain["id"], 11, ("row1",))  # id doesn't start with "row1"
    w.conditions_changed()
    _commit(w)


def _feature_constructor(w):
    w.setDescriptors([
        ContinuousDescriptor("sum", "x1 + 2 * x2", 3),
        DiscreteDescriptor("big", "yes if x1 > 12 else no", ("no", "yes"), False),
    ])  # fmt: skip
    w.apply()


def _edit_domain(*changes):
    """Edit Domain with ``(column index, transforms)`` changes."""

    def configure(w):
        from Orange.widgets.data.oweditdomain import TransformRole

        model = w.domain_view.model()
        for index, transforms in changes:
            model.setData(model.index(index, 0), transforms, TransformRole)
        _commit(w)

    return configure


def _rename(index, name):
    from Orange.widgets.data.oweditdomain import Rename

    return index, [Rename(name)]


def _merge_red_green(index):
    """Merge red and green into "warm", rename blue to "bleu"."""
    from Orange.widgets.data.oweditdomain import CategoriesMapping

    return index, [CategoriesMapping([("red", "warm"), ("green", "warm"), ("blue", "bleu")])]


def _merge_all(index):
    """Merge red, green and blue into one, by hand."""
    from Orange.widgets.data.oweditdomain import CategoriesMapping

    return index, [CategoriesMapping([(c, "any") for c in ("red", "green", "blue")])]


def _as_type(index, kind):
    """Edit Domain's type change of column ``index`` to ``kind`` (e.g. "AsString")."""
    from Orange.widgets.data import oweditdomain

    return index, [getattr(oweditdomain, kind)()]


def _create_class(w):
    w.attribute = w.data.domain["color"]
    w.class_name = "warm"
    w.active_rules[:] = [["yes", "red"], ["no", ""]]
    w.update_rules()
    w.apply()


def _aggregate_columns(w):
    w.variables = [w.data.domain["x1"], w.data.domain["x2"]]
    _commit(w)


def _discretize_settings():
    from Orange.widgets.data.owdiscretize import Methods, VarHint

    return {"var_hints": {None: VarHint(Methods.EqualFreq, (4,))}, "__version__": 3}


def _discretize_equal_width():
    from Orange.widgets.data.owdiscretize import Methods, VarHint

    return {"var_hints": {None: VarHint(Methods.EqualWidth, (4,))}, "__version__": 3}


def _discretize_custom():
    from Orange.widgets.data.owdiscretize import Methods, VarHint

    return {"var_hints": {None: VarHint(Methods.Custom, ("5, 10",))}, "__version__": 3}


def _rank(w):
    w.nSelected = 2
    w.setSelectionMethod(w.SelectNBest)
    _commit(w)


#: Orange's Rank with only ReliefF (names: its settings before version 5).
RANK_RELIEFF = {"selected_methods": {"ReliefF"}, "__version__": 4}
#: Orange's Purge Domain without removing constant columns.
PURGE_VALUES = {"removeAttributes": 0, "removeClassAttribute": 0, "removeMetaAttributes": 0}


def _color(w):
    w.disc_model.setData(w.disc_model.index(0, 0), "colour", Qt.ItemDataRole.EditRole)
    _commit(w)


def _learner(t, data):
    return {"learner": TreeLearner(max_depth=3)}


def _model(t, data):
    return {"predictors": TreeLearner(max_depth=3)(t.full)}


def _distances(t, data):
    from Orange.widgets.unsupervised.owdistances import OWDistances

    w = t.create_widget(OWDistances)
    t.send_signal(w.Inputs.data, data, widget=w)
    t.wait_until_finished(w)
    return {"distances": t.get_output(w.Outputs.distances, widget=w)}


def _results(t, data):
    return {"evaluation_results": t.cloud_results(TreeLearner(max_depth=3))}


CHECKS: dict[str, Check] = {
    # Data
    "Data Table": Check("data.owtable.OWTable"),
    "Data Info": Check("data.owdatainfo.OWDataInfo"),
    "Feature Statistics": Check("data.owfeaturestatistics.OWFeatureStatistics"),
    "Select Columns": Check("data.owselectcolumns.OWSelectAttributes", _select_columns),
    "Select Rows": Check("data.owselectrows.OWSelectRows", _select_rows),
    "Feature Constructor": Check(
        "data.owfeatureconstructor.OWFeatureConstructor", _feature_constructor
    ),
    "Edit Domain": Check(
        "data.oweditdomain.OWEditDomain", _edit_domain(_rename(0, "first"), _rename(4, "y"))
    ),
    "Edit Domain: categories": Check(
        "data.oweditdomain.OWEditDomain", _edit_domain(_merge_red_green(3))
    ),
    "Edit Domain: merge all": Check("data.oweditdomain.OWEditDomain", _edit_domain(_merge_all(3))),
    # Not "as text" here: Orange 3.39 can't compare its domain (test_value_mappings.py).
    "Edit Domain: as numeric": Check(
        "data.oweditdomain.OWEditDomain", _edit_domain(_as_type(3, "AsContinuous"))
    ),
    "Edit Domain: as categorical": Check(
        "data.oweditdomain.OWEditDomain",
        _edit_domain(_as_type(0, "AsCategorical")),
        outcome="refused",
    ),
    "Edit Domain: text as numeric": Check(
        "data.oweditdomain.OWEditDomain",
        _edit_domain(_as_type(5, "AsContinuous")),
        outcome="refused",
    ),
    "Continuize": Check("data.owcontinuize.OWContinuize"),
    "Color": Check("data.owcolor.OWColor", _color),
    # Discretize, Rank and Purge Domain: stopped where they compute
    # distributions or contingencies, else wrong (or refused).
    "Discretize": Check(
        "data.owdiscretize.OWDiscretize", settings=_discretize_settings(), outcome="stopped"
    ),
    "Discretize: equal width": Check(
        "data.owdiscretize.OWDiscretize", settings=_discretize_equal_width(), outcome="wrong"
    ),
    "Discretize: custom": Check(
        "data.owdiscretize.OWDiscretize", settings=_discretize_custom(), outcome="refused"
    ),
    "Impute": Check("data.owimpute.OWImpute"),
    "Preprocess": Check(
        "data.owpreprocess.OWPreprocess",
        settings={"storedsettings": {"preprocessors": [("orange.preprocess.scale", {})]}},
    ),
    # With data connected, a counting step stops it (build the recipe without data).
    "Preprocess: discretize": Check(
        "data.owpreprocess.OWPreprocess",
        settings={"storedsettings": {"preprocessors": [("orange.preprocess.discretize", {})]}},
        outcome="stopped",
    ),
    "Create Class": Check("data.owcreateclass.OWCreateClass", _create_class),
    "Aggregate Columns": Check("data.owaggregatecolumns.OWAggregateColumns", _aggregate_columns),
    "Rank": Check("data.owrank.OWRank", _rank, outcome="stopped", timeout=5000),
    "Rank: ReliefF": Check("data.owrank.OWRank", _rank, RANK_RELIEFF, outcome="wrong"),
    "Purge Domain": Check("data.owpurgedomain.OWPurgeDomain", outcome="stopped"),
    "Purge Domain: values": Check(
        "data.owpurgedomain.OWPurgeDomain", settings=PURGE_VALUES, outcome="refused"
    ),
    "Randomize": Check("data.owrandomize.OWRandomize"),
    "Data Sampler": Check("data.owdatasampler.OWDataSampler"),
    "Merge Data": Check("data.owmergedata.OWMergeData"),
    "Concatenate": Check("data.owconcatenate.OWConcatenate"),
    "Select by Data Index": Check("data.owselectbydataindex.OWSelectByDataIndex"),
    "Transpose": Check("data.owtranspose.OWTranspose"),
    "Unique": Check("data.owunique.OWUnique"),
    "Group By": Check("data.owgroupby.OWGroupBy"),
    "Pivot Table": Check("data.owpivot.OWPivot"),
    "Melt": Check("data.owmelt.OWMelt"),
    "Outliers": Check("data.owoutliers.OWOutliers"),
    "Correlations": Check("data.owcorrelations.OWCorrelations"),
    "Neighbors": Check("data.owneighbors.OWNeighbors"),
    "Create Instance": Check("data.owcreateinstance.OWCreateInstance"),
    "Save Data": Check("data.owsave.OWSave"),
    # Visualize
    "Box Plot": Check("visualize.owboxplot.OWBoxPlot"),
    "Violin Plot": Check("visualize.owviolinplot.OWViolinPlot"),
    "Distributions": Check("visualize.owdistributions.OWDistributions"),
    "Scatter Plot": Check("visualize.owscatterplot.OWScatterPlot"),
    "Line Plot": Check("visualize.owlineplot.OWLinePlot"),
    "Bar Plot": Check("visualize.owbarplot.OWBarPlot"),
    "Heat Map": Check("visualize.owheatmap.OWHeatMap"),
    "Mosaic Display": Check("visualize.owmosaic.OWMosaicDisplay"),
    "Sieve Diagram": Check("visualize.owsieve.OWSieveDiagram"),
    "Linear Projection": Check("visualize.owlinearprojection.OWLinearProjection"),
    "Venn Diagram": Check("visualize.owvenndiagram.OWVennDiagram"),
    "Silhouette Plot": Check("visualize.owsilhouetteplot.OWSilhouettePlot"),
    # Model: the learner widgets
    "Logistic Regression": Check("model.owlogisticregression.OWLogisticRegression"),
    "Tree": Check("model.owtree.OWTreeLearner"),
    "Random Forest": Check("model.owrandomforest.OWRandomForest"),
    "kNN": Check("model.owknn.OWKNNLearner"),
    "Naive Bayes": Check("model.ownaivebayes.OWNaiveBayes"),
    "SVM": Check("model.owsvm.OWSVM"),
    "Gradient Boosting": Check("model.owgradientboosting.OWGradientBoosting"),
    "Neural Network": Check("model.owneuralnetwork.OWNNLearner"),
    "Constant": Check("model.owconstant.OWConstant"),
    # Evaluate
    "Test and Score": Check("evaluate.owtestandscore.OWTestAndScore", extra=_learner),
    "Predictions": Check("evaluate.owpredictions.OWPredictions", extra=_model),
    "Confusion Matrix": Check("evaluate.owconfusionmatrix.OWConfusionMatrix", extra=_results),
    "ROC Analysis": Check("evaluate.owrocanalysis.OWROCAnalysis", extra=_results),
    "Lift Curve": Check("evaluate.owliftcurve.OWLiftCurve", extra=_results),
    "Calibration Plot": Check("evaluate.owcalibrationplot.OWCalibrationPlot", extra=_results),
    # Unsupervised
    # Cloud Distances: test_cloud_distances.py
    "Distances": Check("unsupervised.owdistances.OWDistances"),
    # Cloud Hierarchical Clustering: test_cloud_hclust.py
    "Hierarchical Clustering": Check(
        "unsupervised.owhierarchicalclustering.OWHierarchicalClustering", extra=_distances
    ),
    "k-Means": Check("unsupervised.owkmeans.OWKMeans"),  # Cloud k-Means: test_cloud_kmeans.py
    "PCA": Check("unsupervised.owpca.OWPCA"),
    "t-SNE": Check("unsupervised.owtsne.OWtSNE"),  # Cloud t-SNE: test_cloud_tsne.py
    "MDS": Check("unsupervised.owmds.OWMDS"),  # Cloud MDS: test_cloud_mds.py
    "Louvain Clustering": Check("unsupervised.owlouvainclustering.OWLouvainClustering"),
    "DBSCAN": Check("unsupervised.owdbscan.OWDBSCAN"),
    "Manifold Learning": Check("unsupervised.owmanifoldlearning.OWManifoldLearning"),
    "Correspondence Analysis": Check("unsupervised.owcorrespondence.OWCorrespondenceAnalysis"),
}


#: Orange's widgets that have a Cloud widget for (part of) their job: the doc points to it.
CLOUD_VERSIONS = {
    "Rank": "Cloud Rank",
    "Discretize": "Cloud Discretize",
    "Purge Domain": "Cloud Purge Domain",
    "Edit Domain": "Cloud Categories",
    "Create Class": "Cloud Create Class",
}


def widget_class(path: str) -> type:
    """Orange's widget class from "module.Class" under Orange.widgets."""
    module, name = path.rsplit(".", 1)
    return getattr(importlib.import_module(f"Orange.widgets.{module}"), name)


def doc_entries() -> dict[str, str]:
    """{widget name: section, a key of :data:`SECTIONS`} from the doc."""
    entries, section = {}, None
    for line in DOC.read_text().splitlines():
        if line.startswith("## "):
            section = next((key for key in SECTIONS if key in line), None)
        elif section and (m := re.match(r"- \*\*(.+?)\*\*", line)):
            for name in m.group(1).split(", "):
                entries[name] = section
    return entries


def same_table(got: Table, expected: Table | None) -> bool:
    """Whether two tables have the same domain and values."""
    return (
        expected is not None
        and got.domain == expected.domain
        and len(got) == len(expected)
        and np.array_equal(got.X, expected.X, equal_nan=True)
        and np.array_equal(got.Y, expected.Y, equal_nan=True)
        and np.array_equal(got.metas.astype(str), expected.metas.astype(str))
    )


def test_doc_points_to_cloud_widgets():
    """The doc's entries for Rank, Discretize, Purge Domain, Edit Domain and
    Create Class name their Cloud widgets, and those exist; the machine's
    refusals of their steps too."""
    from orangecontrib.cloud.server.ops.derive import (
        AS_CATEGORICAL_MESSAGE,
        CREATE_CLASS_MESSAGE,
        DISCRETIZED_MESSAGE,
        NO_VALUES_MESSAGE,
    )
    from orangecontrib.cloud.widgets import (
        owcloudcategories,
        owcloudcreateclass,
        owclouddiscretize,
        owcloudpurgedomain,
        owcloudrank,
    )

    entries = {
        m.group(1): line
        for line in DOC.read_text().splitlines()
        if (m := re.match(r"- \*\*(.+?)\*\*", line))
    }
    widgets = {
        module.__dict__[name].name
        for module in (
            owcloudrank,
            owclouddiscretize,
            owcloudpurgedomain,
            owcloudcategories,
            owcloudcreateclass,
        )
        for name in module.__all__
        if name.startswith("OW")
    }
    for orange, cloud in CLOUD_VERSIONS.items():
        assert cloud in entries[orange] and cloud in widgets, orange
    assert "Cloud Discretize" in DISCRETIZED_MESSAGE
    assert "Cloud Purge Domain" in NO_VALUES_MESSAGE
    assert "Cloud Categories" in AS_CATEGORICAL_MESSAGE
    assert "Cloud Create Class" in CREATE_CLASS_MESSAGE


class SupportedWidgetsTest(CloudTableTest):
    __test__ = False

    def cloud_results(self, learner):
        """Cloud Test & Score's results for ``learner`` on the cloud table."""
        w = self.create_widget(OWCloudTestAndScore, stored_settings={"n_folds": 2})
        self.send_signal(w.Inputs.train_data, self.data, widget=w)
        self.send_signal(w.Inputs.learner, learner, 0, widget=w)
        self.wait_until_finished(w, timeout=300000)
        assert not w.Error.active, [str(m) for m in w.Error.active]
        return self.get_output(w.Outputs.evaluations_results, widget=w)

    def run_widget(self, check: Check, data: Table) -> tuple[Any, dict[str, Any]]:
        """The widget after it got ``data`` (and ``check.extra``), and its outputs."""
        settings = copy.deepcopy(check.settings) or None  # Orange changes them
        w = self.create_widget(widget_class(check.widget), stored_settings=settings)
        values = {
            name: data
            for name, sig in vars(w.Inputs).items()
            if isinstance(sig, (Input, MultiInput)) and issubclass(Table, sig.type)
        }
        values.update(check.extra(self, data) if check.extra else {})
        for name, value in values.items():
            sig = getattr(w.Inputs, name)
            args = (0,) if isinstance(sig, MultiInput) else ()
            self.send_signal(sig, value, *args, widget=w)
        self.wait_until_finished(w, timeout=check.timeout)
        if check.configure:
            check.configure(w)
            self.wait_until_finished(w, timeout=check.timeout)
        outputs = {
            name: self.get_output(sig, widget=w)
            for name, sig in vars(w.Outputs).items()
            if isinstance(sig, Output)
        }
        return w, outputs

    def observe(self, name: str) -> tuple[str, str]:
        """What widget ``name`` does with the cloud table: the outcome and a note."""
        check = CHECKS[name]
        crashes: list[str] = []
        previous = sys.excepthook  # an error in a Qt slot would abort the tests
        sys.excepthook = lambda kind, exc, tb: crashes.append(f"{kind.__name__}: {exc}")
        try:
            w, outputs = self.run_widget(check, self.data)
        except Exception as exc:  # noqa: BLE001 (a widget that fails is an outcome)
            crashes.append(f"{type(exc).__name__}: {exc}")
        finally:
            sys.excepthook = previous
        if crashes:
            stopped = any(c.startswith(PlaceholderStatisticsError.__name__) for c in crashes)
            return "stopped" if stopped else "crash", "; ".join(crashes)
        messages = [str(m) for m in (*w.Error.active, *w.Warning.active)]
        tables = {out: t for out, t in outputs.items() if t is None or isinstance(t, Table)}
        kinds = {
            out: None if t is None else "cloud" if is_cloud_table(t) else "local"
            for out, t in tables.items()
        }
        note = f"outputs {kinds}; messages {messages}"
        if outputs.get("learner") is not None:
            results = self.cloud_results(outputs["learner"])
            if len(results.actual) != self.N_ROWS:
                return "learner fails", note
            note += "; its learner works in Cloud Test & Score"
        new = {
            out: t
            for out, t in tables.items()
            if is_cloud_table(t) and len(t.steps) > len(self.data.steps)
        }
        return self.replay(check, new, note) if new else ("nothing", note)

    def replay(self, check: Check, new: dict[str, Table], note: str) -> tuple[str, str]:
        """Replay the ``new`` cloud outputs and compare them with Orange's
        outputs on the full table."""
        _, local = self.run_widget(check, self.full)
        outcome = "replayed"
        for out, table in new.items():
            try:
                table_id = resolve(table, self.client, self.machine_id)
            except CloudUnsupportedStepError as exc:
                outcome = "refused"
                note += f"; {out}: {exc}"
                continue
            if not same_table(self.client.table_rows(table_id, 0, self.N_ROWS), local[out]):
                return "wrong", note + f"; {out} isn't Orange's output on all rows"
        return outcome, note

    def check_documented(self, name: str, section: str | None, outcome: str, note: str) -> None:
        """Fail unless ``outcome`` is one that ``name``'s doc section allows."""
        allowed = {CHECKS[name].outcome} if CHECKS[name].outcome else SECTIONS.get(section, ())
        assert outcome in allowed, f"{name}: {outcome}, but the doc says {section}. {note}"


def test_doc_lists_the_checked_widgets():
    """The doc lists exactly the checked widgets, each in a known section."""
    documented = doc_entries()
    assert set(documented) == {name.split(":")[0] for name in CHECKS}
    wrong = {name.split(":")[0] for name, check in CHECKS.items() if check.outcome == "wrong"}
    assert wrong and all(documented[name] == "Not supported" for name in wrong)


class TestReplayed(SupportedWidgetsTest):
    """The doc's "replayed on all rows" widgets replay, with Orange's results,
    and the widgets it lists for the Cloud widgets' outputs work."""

    __test__ = True

    def test_replayed(self):
        sections = doc_entries()
        for name in CHECKS:
            section = sections[name.split(":")[0]]
            if section in ("replayed", "outputs"):
                self.check_documented(name, section, *self.observe(name))


@pytest.mark.slow
class TestAllWidgets(SupportedWidgetsTest):
    """Every listed widget does what the doc says (``-s`` prints what each did)."""

    __test__ = True

    def test_all_widgets(self):
        sections = doc_entries()
        lines, failures = [], []
        for name in CHECKS:
            outcome, note = self.observe(name)
            lines.append(f"{name:26} {outcome:13} {note}")
            try:
                self.check_documented(name, sections.get(name.split(":")[0]), outcome, note)
            except AssertionError as exc:
                failures.append(str(exc))
        print("\n" + "\n".join(lines))
        assert not failures, "\n".join(failures)
