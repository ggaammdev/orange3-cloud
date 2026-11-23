"""Cloud Rank: Orange's Rank for a cloud table, with scores computed on all rows.

The scoring methods (information gain, gain ratio, Gini, ANOVA, χ², ReliefF,
FCBF, univariate regression, RReliefF) and the learner-based scorers on the
Scorer input are Orange's own; the machine computes them on all rows of the
table, as Orange's Rank does (a ``rank`` job, ``server/ops/rank.py``). The
table of scores, its sorting and the selection (none, all, manual, the best
N) are Orange's Rank's own model and view, and the widget's code follows
Orange's Rank (``Orange/widgets/data/owrank.py``, Orange 3.41,
GPL-3.0-or-later). As in Orange, scores are kept per method and scorer, so
checking another method scores only that one.

Outputs: Reduced Data, the cloud table with the chosen columns (a recorded
step that the machine replays); Scores, Orange's table of the scores (local,
one row per feature); Features, the chosen columns.
"""

from __future__ import annotations

from functools import partial
from itertools import chain
from typing import Any, NamedTuple

import numpy as np
from AnyQt.QtCore import QItemSelection, QItemSelectionModel, QItemSelectionRange, Qt
from AnyQt.QtWidgets import QButtonGroup, QCheckBox, QGridLayout, QRadioButton, QStackedWidget
from Orange.data import ContinuousVariable, Domain, StringVariable, Table
from Orange.data.util import get_unique_names_duplicates
from Orange.preprocess import score
from Orange.widgets import gui, report
from Orange.widgets.data.owrank import (
    CLS_SCORES,
    REG_SCORES,
    ProblemType,
    RankTableModel,
    TableView,
)
from Orange.widgets.settings import ContextSetting, DomainContextHandler, Setting
from Orange.widgets.utils import enum2int
from Orange.widgets.utils.widgetpreview import WidgetPreview
from Orange.widgets.widget import AttributeList, Input, Msg, MultiInput, Output

from orangecontrib.cloud.data.remote_table import LOCAL_DATA, is_cloud_table, resolve
from orangecontrib.cloud.protocol.jobs import JobKind
from orangecontrib.cloud.widgets.base import CloudTask, OWCloudBaseWidget

__all__ = ["METHODS", "Method", "OWCloudRank", "decode_scores"]


class Method(NamedTuple):
    """A scoring method of Orange's Rank, or a learner-based scorer."""

    name: str
    shortname: str
    #: The ``rank`` job's name of the method (its ``Orange.preprocess.score``
    #: class); empty for a scorer from the input.
    key: str
    problem_type: int
    is_default: bool = False
    scorer: Any = None  #: the scorer (a learner), for scorers from the input


#: Orange's Rank's methods, in its order.
METHODS: tuple[Method, ...] = tuple(
    Method(m.name, m.shortname, m.scorer.__name__, m.problem_type, m.is_default)
    for m in (*CLS_SCORES, *REG_SCORES)
)


def decode_scores(values: Any) -> np.ndarray:
    """The ``rank`` job's JSON scores as numbers (``None`` → NaN, "inf" → ∞)."""
    if values is None:
        return np.array([])
    if values and isinstance(values[0], list):
        return np.array([decode_scores(row) for row in values])
    return np.array([np.nan if v is None else float(v) for v in values], dtype=float)


class _Scored(NamedTuple):
    """A finished ``rank`` job: which methods and scorers it scored, and its values."""

    methods: list[str]
    scorers: list[int]
    values: dict[str, Any]


def _score(
    task: CloudTask, data: Table, machine_id: str | None, methods: list[str], scorers: list[Any]
) -> dict[str, Any]:
    """Resolve the table, then run the ``rank`` job (worker thread)."""
    table_id = resolve(data, task.client, machine_id, wait=task.wait_for_job)
    objects = {"scorers": scorers} if scorers else None
    result = task.run_job(
        JobKind.RANK, inputs={"data": table_id}, params={"methods": methods}, objects=objects
    )
    return result.values


class OWCloudRank(OWCloudBaseWidget):
    """Rank the features of a cloud table by scores computed on all rows."""

    name = "Cloud Rank"
    description = "Rank the features of a cloud table by scores computed on all rows."
    icon = "icons/CloudRank.svg"
    priority = 26
    keywords = "cloud rank, feature scoring, feature selection, filter"
    buttons_area_orientation = Qt.Orientation.Vertical

    class Inputs:
        data = Input("Data", Table, doc="A cloud table (from Cloud File).")
        scorer = MultiInput(
            "Scorer", score.Scorer, filter_none=True, doc="Learners that score features."
        )

    class Outputs:
        reduced_data = Output("Reduced Data", Table, default=True)
        scores = Output("Scores", Table, dynamic=False)
        features = Output("Features", AttributeList, dynamic=False)

    SelectNone, SelectAll, SelectManual, SelectNBest = range(4)

    nSelected = ContextSetting(5)
    auto_apply = Setting(True)
    sorting = Setting((0, enum2int(Qt.SortOrder.DescendingOrder)))
    #: The checked methods, by their ``key``.
    selected_methods = Setting({m.key for m in METHODS if m.is_default})
    settingsHandler = DomainContextHandler()
    selected_attrs = ContextSetting([], schema_only=True)
    selectionMethod = ContextSetting(SelectNBest)

    class Information(OWCloudBaseWidget.Information):
        no_target_var = Msg("Data does not have a (single) target variable.")
        missings_imputed = Msg("Missing values will be imputed as needed.")

    class Error(OWCloudBaseWidget.Error):
        invalid_type = Msg("Cannot handle target variable type {}")
        inadequate_learner = Msg("Scorer {} inadequate: {}")
        no_attributes = Msg("Data does not have a single attribute.")

    class Warning(OWCloudBaseWidget.Warning):
        renamed_variables = Msg("Variables with duplicated names have been renamed.")
        failed = Msg("No scores from {}: it doesn't work on this data.")

    def __init__(self):
        super().__init__()
        self.data: Table | None = None
        self.scorers: list[Method] = []
        self.problem_type_mode = ProblemType.CLASSIFICATION
        #: Scores per method key, and per scorer (its index) with labels.
        self.method_results: dict[str, np.ndarray] = {}
        self.scorer_results: dict[int, tuple[np.ndarray, tuple[str, ...]]] = {}
        self.failed: set[str] = set()
        self.out_domain_desc = None
        self._init_view()
        self._init_controls()

    def _init_view(self) -> None:
        self.ranksModel = RankTableModel(parent=self)
        self.ranksView = TableView(self)
        self.mainArea.layout().addWidget(self.ranksView)
        self.ranksView.setModel(self.ranksModel)
        self.ranksView.setColumnWidth(1, 30)
        self.ranksView.selectionModel().selectionChanged.connect(self.on_select)
        self.ranksView.manualSelection.connect(lambda: self.setSelectionMethod(self.SelectManual))
        self.ranksView.verticalHeader().sectionClicked.connect(
            lambda *_: self.setSelectionMethod(self.SelectManual)
        )
        self.ranksView.horizontalHeader().sectionClicked.connect(self.headerClick)

    def _init_controls(self) -> None:
        self.measuresStack = QStackedWidget(self)
        self.controlArea.layout().addWidget(self.measuresStack)
        for problem_type in (ProblemType.CLASSIFICATION, ProblemType.REGRESSION, None):
            methods = [m for m in METHODS if m.problem_type == problem_type]
            box = gui.vBox(None, "Scoring Methods" if methods else None)
            self.measuresStack.addWidget(box)
            for method in methods:
                box.layout().addWidget(
                    QCheckBox(
                        method.name,
                        self,
                        objectName=method.shortname,
                        checked=method.key in self.selected_methods,
                        stateChanged=partial(self.methodSelectionChanged, key=method.key),
                    )
                )
            gui.rubber(box)
        gui.rubber(self.controlArea)
        self.switchProblemType(ProblemType.CLASSIFICATION)

        box = gui.vBox(self.buttonsArea, "Select Attributes")
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        self.selectButtons = QButtonGroup()
        self.selectButtons.idClicked.connect(self.setSelectionMethod)
        for row, (text, button_id) in enumerate(
            (
                ("None", self.SelectNone),
                ("All", self.SelectAll),
                ("Manual", self.SelectManual),
                ("Best ranked:", self.SelectNBest),
            )
        ):
            button = QRadioButton(text)
            self.selectButtons.addButton(button, button_id)
            grid.addWidget(button, row, 0)
        spin = gui.spin(
            box, self, "nSelected", 1, 999, addToLayout=False,
            callback=lambda: self.setSelectionMethod(self.SelectNBest),
        )  # fmt: skip
        grid.addWidget(spin, 3, 1)
        self.selectButtons.button(self.selectionMethod).setChecked(True)
        box.layout().addLayout(grid)
        gui.auto_send(self.buttonsArea, self, "auto_apply")

    def switchProblemType(self, index: int) -> None:
        """Show the methods for classification, regression or no target."""
        self.measuresStack.setCurrentIndex(index)
        self.problem_type_mode = index

    # ------------------------------------------------------------------
    # Inputs

    @Inputs.data
    def set_data(self, data: Table | None) -> None:
        """Receive the Data input."""
        self.closeContext()
        self.cancel()
        self.selected_attrs = []
        self.ranksModel.clear()
        self.ranksModel.resetSorting(True)
        self.method_results, self.scorer_results, self.failed = {}, {}, set()
        self.Error.clear()
        self.Information.clear()
        self.Warning.failed.clear()
        if data is not None and not is_cloud_table(data):
            self._show_error(self.Error.cloud_error, LOCAL_DATA)
            data = None
        if data is not None and not data.domain.attributes:
            self.Error.no_attributes()
            data = None
        self.data = data
        self.switchProblemType(ProblemType.CLASSIFICATION)
        if data is not None:
            self._set_problem_type(data.domain)
            self.selectionMethod = self.SelectNBest
        self.openContext(data)
        self.selectButtons.button(self.selectionMethod).setChecked(True)

    def _set_problem_type(self, domain: Domain) -> None:
        if domain.has_discrete_class:
            self.switchProblemType(ProblemType.CLASSIFICATION)
        elif domain.has_continuous_class:
            self.switchProblemType(ProblemType.REGRESSION)
        elif not domain.class_var:
            self.Information.no_target_var()
            self.switchProblemType(ProblemType.UNSUPERVISED)
        else:
            self.Error.invalid_type(type(domain.class_var).__name__)

    @Inputs.scorer
    def set_learner(self, index: int, scorer: score.Scorer) -> None:
        """Replace the scorer at ``index``."""
        self.scorers[index] = self._scorer_method(scorer)
        self.scorer_results = {}

    @Inputs.scorer.insert
    def insert_learner(self, index: int, scorer: score.Scorer) -> None:
        """Add a scorer at ``index``."""
        self.scorers.insert(index, self._scorer_method(scorer))
        self.scorer_results = {}

    @Inputs.scorer.remove
    def remove_learner(self, index: int) -> None:
        """Remove the scorer at ``index``."""
        self.scorers.pop(index)
        self.scorer_results = {}

    @staticmethod
    def _scorer_method(scorer: score.Scorer) -> Method:
        problem_type = ProblemType.from_variable(scorer.class_type)
        return Method(scorer.name, scorer.name, "", problem_type, scorer=scorer)

    def handleNewSignals(self) -> None:
        self.update_scores()
        self.commit.deferred()  # new data: nothing selected until it's scored

    def on_cloud_connected(self) -> None:
        if self.data is not None and self.task is None and self._missing() != ([], []):
            self.update_scores()

    # ------------------------------------------------------------------
    # Scoring

    def _get_methods(self) -> list[Method]:
        return [
            m
            for m in METHODS
            if m.key in self.selected_methods and m.problem_type == self.problem_type_mode
        ]

    def _get_scorers(self) -> list[tuple[int, Method]]:
        """The adequate scorers, with their index; shows an error for the others."""
        scorers = []
        for index, scorer in enumerate(self.scorers):
            if scorer.problem_type in (self.problem_type_mode, ProblemType.UNSUPERVISED):
                scorers.append((index, scorer))
            else:
                self.Error.inadequate_learner(scorer.name, scorer.scorer.learner_adequacy_err_msg)
        return scorers

    def _missing(self) -> tuple[list[str], list[int]]:
        """Keys of the methods and indices of the scorers without scores yet."""
        methods = [m.key for m in self._get_methods() if m.key not in self.method_results]
        scorers = [i for i, _ in self._get_scorers() if i not in self.scorer_results]
        return methods, scorers

    def update_scores(self) -> None:
        """Score the missing methods and scorers on the machine, then show all."""
        if self.data is None:
            self.ranksModel.clear()
            self.Outputs.scores.send(None)
            return
        self.Error.inadequate_learner.clear()
        methods, scorers = self._missing()
        if not methods and not scorers:
            # A running job scores only methods and scorers no longer shown
            # (e.g. a removed scorer, whose index may be gone): drop it.
            self.cancel()
            self._show_scores()
            return
        data, learners = self.data, [self.scorers[i].scorer for i in scorers]
        handle = self.session.handle
        machine_id = handle.machine_id if handle is not None else None

        def work(task: CloudTask) -> _Scored:
            return _Scored(methods, scorers, _score(task, data, machine_id, methods, learners))

        self.run_cloud(work, on_done=self._scored)

    def _scored(self, scored: _Scored) -> None:
        values, n = scored.values, len(self.data.domain.attributes)
        names = {m.key: m.name for m in METHODS}
        for key in scored.methods:
            scores = values["methods"].get(key)
            self.method_results[key] = (
                np.full(n, np.nan) if scores is None else decode_scores(scores)
            )
            if scores is None:
                self.failed.add(names[key])
        for index, scores in zip(scored.scorers, values["scorers"], strict=True):
            self.scorer_results[index] = self._scorer_scores(self.scorers[index], scores)
            if scores is None:
                self.failed.add(self.scorers[index].name)
        self.Information.missings_imputed(shown=bool(values.get("has_missing")))
        self._show_scores()

    def _scorer_scores(self, scorer: Method, scores: Any) -> tuple[np.ndarray, tuple[str, ...]]:
        """A scorer's scores (features × scores) and column labels, as Orange's Rank."""
        n = len(self.data.domain.attributes)
        array = np.full((n, 1), np.nan) if scores is None else decode_scores(scores).T
        if array.shape[1] == 1:
            return array, (scorer.shortname,)
        return array, tuple(f"{scorer.shortname}_{i}" for i in range(1, 1 + array.shape[1]))

    def _show_scores(self) -> None:
        """Fill the table with the scores, as Orange's Rank does, and select."""
        methods = self._get_methods()
        scores = [self.scorer_results[i] for i, _ in self._get_scorers()]
        scorer_scores, scorer_labels = zip(*scores, strict=True) if scores else ((), ())
        labels = tuple(m.shortname for m in methods) + tuple(chain.from_iterable(scorer_labels))
        attributes = self.data.domain.attributes
        model_array = np.column_stack(
            (list(attributes),)
            + ([float(len(a.values)) if a.is_discrete else np.nan for a in attributes],)
            + tuple(self.method_results[m.key] for m in methods)
            + scorer_scores
        )
        for column, values in enumerate(model_array.T[2:].astype(float), start=2):
            self.ranksModel.setExtremesFrom(column, values)
        self.ranksModel.wrap(model_array.tolist())
        self.ranksModel.setHorizontalHeaderLabels(("", "#", *labels))
        self.ranksView.setColumnWidth(1, 40)
        self.ranksView.resizeColumnToContents(0)
        self._restore_sorting(len(labels))
        shown = {m.name for m in methods} | {s.name for _, s in self._get_scorers()}
        failed = sorted(self.failed & shown)
        self.Warning.failed(", ".join(failed), shown=bool(failed))
        self.autoSelection()
        self.Outputs.scores.send(self.create_scores_table(labels))

    def _restore_sorting(self, n_labels: int) -> None:
        sort_column, sort_order = self.sorting
        if sort_column < n_labels:
            order = Qt.SortOrder(sort_order)
            self.ranksModel.sort(sort_column + 2, order)
            self.ranksView.horizontalHeader().setSortIndicator(sort_column + 2, order)

    # ------------------------------------------------------------------
    # Selection and outputs (as in Orange's Rank)

    def on_select(self, *_: Any) -> None:
        """Remember the selected features and send them."""
        rows = [i.row() for i in self.ranksView.selectionModel().selectedRows(0)]
        indices = self.ranksModel.mapToSourceRows(rows)
        self.selected_attrs = [self.data.domain[i] for i in indices] if self.data else []
        self.commit.deferred()

    def setSelectionMethod(self, method: int) -> None:
        """Select none, all, manually or the best N."""
        self.selectionMethod = method
        self.selectButtons.button(method).setChecked(True)
        self.autoSelection()

    def autoSelection(self) -> None:
        """Select the rows that the selection method says."""
        model = self.ranksModel
        rows, columns = model.rowCount(), model.columnCount()
        selection = QItemSelection()
        if self.selectionMethod == self.SelectAll and rows:
            selection = QItemSelection(model.index(0, 0), model.index(rows - 1, columns - 1))
        elif self.selectionMethod == self.SelectNBest and rows:
            n = min(self.nSelected, rows)
            selection = QItemSelection(model.index(0, 0), model.index(n - 1, columns - 1))
        elif self.selectionMethod == self.SelectManual and self.data is not None:
            attributes = self.data.domain.attributes
            indices = [attributes.index(v) for v in self.selected_attrs if v in attributes]
            for row in model.mapFromSourceRows(indices):
                selection.append(
                    QItemSelectionRange(model.index(row, 0), model.index(row, columns - 1))
                )
        self.ranksView.selectionModel().select(
            selection, QItemSelectionModel.SelectionFlag.ClearAndSelect
        )

    def headerClick(self, index: int) -> None:
        """Re-select the best N after sorting by another score, and remember the order."""
        if index >= 2 and self.selectionMethod == self.SelectNBest:
            self.autoSelection()
        self.sorting = (self.ranksModel.sortColumn() - 2, enum2int(self.ranksModel.sortOrder()))

    def methodSelectionChanged(self, state: int, key: str) -> None:
        """Check or uncheck a method, and score it if needed."""
        if Qt.CheckState(state) == Qt.CheckState.Checked:
            self.selected_methods.add(key)
        else:
            self.selected_methods.discard(key)
        self.update_scores()

    @gui.deferred
    def commit(self) -> None:
        """Send the cloud table with the chosen columns (a recorded step)."""
        if self.data is None or not self.selected_attrs:
            self.Outputs.reduced_data.send(None)
            self.Outputs.features.send(None)
            self.out_domain_desc = None
            return
        domain = self.data.domain
        reduced = self.data.transform(Domain(self.selected_attrs, domain.class_var, domain.metas))
        self.Outputs.reduced_data.send(reduced)
        self.Outputs.features.send(AttributeList(self.selected_attrs))
        self.out_domain_desc = report.describe_domain(reduced.domain)

    def create_scores_table(self, labels: tuple[str, ...]) -> Table | None:
        """Orange's table of the scores: a row per feature, a column per score."""
        self.Warning.renamed_variables.clear()
        model_list = self.ranksModel.tolist()
        if not model_list or len(model_list[0]) == 2:
            return None
        unique, renamed = get_unique_names_duplicates((*labels, "Feature"), return_duplicated=True)
        if renamed:
            self.Warning.renamed_variables(", ".join(renamed))
        domain = Domain(
            [ContinuousVariable(label) for label in unique[:-1]], metas=[StringVariable(unique[-1])]
        )
        # As Orange: clipping the object array also turns missing scores into
        # the smallest float, so the output is the same as Orange's.
        finfo = np.finfo(np.float64)
        scores = np.clip(np.array(model_list)[:, 2:], finfo.min, finfo.max)
        names = np.array([a.name for a in self.data.domain.attributes])[:, None]
        table = Table(domain, scores, metas=names)
        table.name = "Feature Scores"
        return table

    def send_report(self) -> None:
        if not self.data:
            return
        self.report_domain("Input", self.data.domain)
        self.report_table("Ranks", self.ranksView, num_format="{:.3f}")
        if self.out_domain_desc is not None:
            self.report_items("Output", self.out_domain_desc)


if __name__ == "__main__":  # pragma: no cover
    WidgetPreview(OWCloudRank).run()
