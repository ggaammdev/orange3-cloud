"""Cloud Test & Score: evaluate learners on a cloud table, in parallel, on
the machine.

The learners are pickled and sent with the table's ID as an ``evaluate``
job (``server/ops/evaluate.py``): cross-validation or test on test data,
with every learner × fold as its own task on the machine. The scores table
is computed there on all rows, the same scores as Orange's Test & Score.
The full Evaluation Results (Orange's ``Results``, never sampled) stay on
the machine; they are downloaded to the output automatically when they're
small, and after "Download results" (which asks first above 100 MB) when
they're large.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np
from AnyQt.QtCore import Qt
from AnyQt.QtGui import QBrush, QStandardItem
from AnyQt.QtWidgets import QLabel, QMessageBox
from Orange.base import Learner
from Orange.data import Domain, Table
from Orange.evaluation import Results
from Orange.evaluation.scoring import Score
from Orange.widgets import gui
from Orange.widgets.evaluate.utils import ScoreTable
from Orange.widgets.settings import Setting, SettingProvider
from Orange.widgets.utils.widgetpreview import WidgetPreview
from Orange.widgets.widget import Input, Msg, MultiInput, Output

from orangecontrib.cloud.client.api import CloudError
from orangecontrib.cloud.client.providers.base import MachineHandle
from orangecontrib.cloud.data.estimate import format_bytes
from orangecontrib.cloud.data.remote_table import LOCAL_DATA, is_cloud_table, resolve
from orangecontrib.cloud.protocol.jobs import JobKind, ObjectInfo
from orangecontrib.cloud.protocol.machine import InfoResponse
from orangecontrib.cloud.widgets import owcloudlearner
from orangecontrib.cloud.widgets.base import CloudTask, OWCloudBaseWidget, machine_label

__all__ = ["CONFIRM_DOWNLOAD_BYTES", "OWCloudTestAndScore"]

log = logging.getLogger(__name__)

RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter

#: Evaluation results larger than this aren't downloaded automatically, and
#: "Download results" asks first (the same limit as Cloud Learner's).
CONFIRM_DOWNLOAD_BYTES = owcloudlearner.CONFIRM_DOWNLOAD_BYTES


def target_table(results: Results) -> Table | None:
    """A local table with only the target column, from ``results.actual``.

    The machine's results don't hold the data (it stays on the machine), but
    Orange's Confusion Matrix needs ``results.data`` for the target's values
    and indexes it with ``row_indices``.
    """
    class_var = results.domain.class_var if results.domain is not None else None
    if class_var is None or results.row_indices is None:
        return None
    n_rows = int(results.row_indices.max()) + 1 if len(results.row_indices) else 0
    y = np.full(n_rows, np.nan)
    y[results.row_indices] = results.actual
    return Table.from_numpy(Domain([], class_var), np.empty((n_rows, 0)), y)


@dataclass
class _Evaluation:
    """A finished evaluation: the job's values and the results on the machine."""

    values: dict[str, Any]
    results_info: ObjectInfo | None  #: None if every learner failed
    learners: list[Learner]  #: the learners in the results (not failed)
    machine_id: str | None
    local: Results | None = None  #: the downloaded results


class OWCloudTestAndScore(OWCloudBaseWidget):
    """Cross-validate learners or test them on test data, on the cloud machine."""

    name = "Cloud Test & Score"
    description = (
        "Cross-validate learners or test them on test data, in parallel on the "
        "cloud machine, with scores computed on all rows."
    )
    icon = "icons/CloudTestAndScore.svg"
    priority = 40
    keywords = "cloud test and score, cross validation, cv, evaluate, scores"
    want_main_area = True

    class Inputs:
        train_data = Input("Data", Table, default=True, doc="A cloud table (from Cloud File).")
        test_data = Input("Test Data", Table, doc="A cloud table to test on (optional).")
        learner = MultiInput(
            "Learner", Learner, filter_none=True, doc="Orange learners to evaluate."
        )

    class Outputs:
        evaluations_results = Output(
            "Evaluation Results",
            Results,
            doc="All rows' predictions, for ROC Analysis, Confusion Matrix and similar.",
        )

    class Error(OWCloudBaseWidget.Error):
        no_target = Msg("The data has no target variable.")
        test_data_missing = Msg("Test on test data needs Test Data.")
        test_data_local = Msg("Test Data is local data, not a cloud table.")
        class_inconsistent = Msg("Test and training data have different target variables.")
        learners_failed = Msg("{}")

    class Warning(OWCloudBaseWidget.Warning):
        test_data_unused = Msg(
            "Test data is present but unused. Select 'Test on test data' to use it."
        )
        sampling = Msg("{}")

    class Information(OWCloudBaseWidget.Information):
        not_downloaded = Msg(
            "The evaluation results ({}) weren't downloaded. Download results sends "
            "them to Evaluation Results."
        )

    KFold, TestOnTest = 0, 1
    NFolds = [2, 3, 5, 10, 20]
    TARGET_AVERAGE = "(Average over classes)"

    score_table = SettingProvider(ScoreTable)
    resampling: int = Setting(KFold)
    n_folds: int = Setting(3)  # index in NFolds: 10
    stratified: bool = Setting(True)
    class_selection: str = Setting(TARGET_AVERAGE)

    _running = ""  # "evaluate" or "download" while a task runs

    def __init__(self):
        super().__init__()
        self.data: Table | None = None
        self.test_data: Table | None = None
        self.learners: list[Learner] = []
        self.evaluation: _Evaluation | None = None
        #: Replaced results, deleted on the machine at the next evaluation.
        self._stale: list[_Evaluation] = []

        box = gui.vBox(self.controlArea, "Sampling")
        rbox = gui.radioButtons(box, self, "resampling", callback=self._param_changed)
        gui.appendRadioButton(rbox, "Cross validation")
        ibox = gui.indentedBox(rbox)
        gui.comboBox(
            ibox,
            self,
            "n_folds",
            label="Number of folds: ",
            items=[str(k) for k in self.NFolds],
            orientation=Qt.Orientation.Horizontal,
            callback=self._kfold_changed,
        )
        gui.checkBox(ibox, self, "stratified", "Stratified", callback=self._kfold_changed)
        gui.appendRadioButton(rbox, "Test on test data")

        box = gui.vBox(self.controlArea, "Target class")
        self.class_selection_combo = gui.comboBox(
            box,
            self,
            "class_selection",
            items=[],
            sendSelectedValue=True,
            callback=self._update_scores,
        )

        box = gui.vBox(self.controlArea, "Evaluation results")
        self.results_label = QLabel()
        box.layout().addWidget(self.results_label)
        self.download_button = gui.button(
            box, self, "Download results", callback=self.download, autoDefault=False
        )
        gui.rubber(self.controlArea)

        self.score_table = ScoreTable(self)
        self.info_label = QLabel()
        self.mainArea.layout().addWidget(self.info_label)
        self.mainArea.layout().addWidget(self.score_table.view)
        self._update_class_selection()
        self._update_info()

    # ------------------------------------------------------------------
    # Inputs

    @Inputs.train_data
    def set_train_data(self, data: Table | None) -> None:
        """Receive the Data input."""
        self.data = data
        self._update_class_selection()

    @Inputs.test_data
    def set_test_data(self, data: Table | None) -> None:
        """Receive the Test Data input."""
        self.test_data = data

    @Inputs.learner
    def set_learner(self, index: int, learner: Learner) -> None:
        """Replace the learner at ``index``."""
        self.learners[index] = learner

    @Inputs.learner.insert
    def insert_learner(self, index: int, learner: Learner) -> None:
        """Receive a new Learner input."""
        self.learners.insert(index, learner)

    @Inputs.learner.remove
    def remove_learner(self, index: int) -> None:
        """Remove a Learner input."""
        self.learners.pop(index)

    def handleNewSignals(self) -> None:
        self.evaluate()

    def on_cloud_connected(self) -> None:
        if self.evaluation is None and self.task is None and self.data is not None:
            self.evaluate()

    def _kfold_changed(self) -> None:
        self.resampling = self.KFold
        self._param_changed()

    def _param_changed(self) -> None:
        self.evaluate()

    def _update_class_selection(self) -> None:
        combo = self.class_selection_combo
        combo.clear()
        class_var = self.data.domain.class_var if self.data is not None else None
        if class_var is None or not class_var.is_discrete:
            combo.parent().setEnabled(False)
            return
        combo.parent().setEnabled(True)
        items = [self.TARGET_AVERAGE, *class_var.values]
        combo.addItems(items)
        if self.class_selection not in items:
            self.class_selection = self.TARGET_AVERAGE
        combo.setCurrentIndex(items.index(self.class_selection))

    # ------------------------------------------------------------------
    # Evaluation

    def _clear(self) -> None:
        if self.evaluation is not None:
            self._stale.append(self.evaluation)
        self.evaluation = None
        self.Outputs.evaluations_results.send(None)
        for message in (
            self.Error.no_target,
            self.Error.test_data_missing,
            self.Error.test_data_local,
            self.Error.class_inconsistent,
            self.Error.learners_failed,
            self.Warning.test_data_unused,
            self.Warning.sampling,
            self.Information.not_downloaded,
        ):
            message.clear()

    def _check(self) -> bool:
        """Show why the inputs can't be evaluated, without the machine."""
        data, test = self.data, self.test_data
        if data is None or not self.learners:
            return False
        if not is_cloud_table(data):
            self._show_error(self.Error.cloud_error, LOCAL_DATA)
            return False
        if data.domain.class_var is None:
            self.Error.no_target()
            return False
        if self.resampling == self.TestOnTest:
            if test is None:
                self.Error.test_data_missing()
                return False
            if not is_cloud_table(test):
                self.Error.test_data_local()
                return False
            if test.domain.class_var != data.domain.class_var:
                self.Error.class_inconsistent()
                return False
        elif test is not None:
            self.Warning.test_data_unused()
        return True

    def evaluate(self) -> None:
        """Evaluate the learners on the machine (an ``evaluate`` job)."""
        self.cancel()
        self._clear_cloud_messages()
        self._clear()
        self._update_scores()
        if not self._check():
            self._update_info()
            return
        data, test, learners = self.data, self.test_data, list(self.learners)
        test_on_test = self.resampling == self.TestOnTest
        params: dict[str, Any] = (
            {"method": "test_on_test_data"}
            if test_on_test
            else {
                "method": "cross_validation",
                "folds": self.NFolds[self.n_folds],
                "stratified": self.stratified,
            }
        )
        handle = self.session.handle
        machine_id = handle.machine_id if handle is not None else None
        stale, self._stale = self._stale, []
        limit = CONFIRM_DOWNLOAD_BYTES

        def work(task: CloudTask) -> _Evaluation:
            for old in stale:  # free the replaced results' memory (best effort)
                if old.machine_id == machine_id:
                    try:
                        if old.results_info is not None:
                            task.client.delete_object(old.results_info.id)
                    except CloudError as exc:  # best effort: the job still runs
                        log.warning("Couldn't delete the replaced results: %s", exc)
            inputs = {"data": resolve(data, task.client, machine_id)}
            if test_on_test:
                inputs["test_data"] = resolve(test, task.client, machine_id)
            result = task.run_job(
                JobKind.EVALUATE,
                inputs=inputs,
                params=params,
                objects={"learners": learners},
                progress_range=(0.0, 0.95),
            )
            failed = [bool(lr.get("failed")) for lr in result.values.get("learners", [])]
            ok = [lr for lr, f in zip(learners, failed, strict=False) if not f]
            info = result.objects.get("results")
            evaluation = _Evaluation(result.values, info, ok, machine_id)
            size = info.size_bytes if info is not None else None
            if info is not None and size is not None and size <= limit:
                task.set_status(f"Downloading the evaluation results ({format_bytes(size)})")
                evaluation.local = task.get_object(info.id, progress_range=(0.95, 1.0))
            return evaluation

        if self.run_cloud(work, on_done=self._evaluated):
            self._running = "evaluate"  # after run_cloud, whose cancel() resets it
        else:
            self._stale = stale + self._stale
        self._update_info()

    def _evaluated(self, evaluation: _Evaluation) -> None:
        self._running = ""
        self.evaluation = evaluation
        values = evaluation.values
        if values.get("warnings"):
            self.Warning.sampling(" ".join(values["warnings"]))
        failed = [
            f"{learner['name']} failed with error:\n{learner['failed']}"
            for learner in values.get("learners", [])
            if learner.get("failed")
        ]
        if failed:
            self.Error.learners_failed("\n".join(failed))
        if evaluation.local is not None:
            self._send_results(evaluation)
        elif evaluation.results_info is not None:
            size = evaluation.results_info.size_bytes
            self.Information.not_downloaded(format_bytes(size) if size is not None else "large")
        self._update_scores()
        self._update_info()

    def _send_results(self, evaluation: _Evaluation) -> None:
        results = evaluation.local
        # The machine's results hold copies of the learners; use the inputs'
        # learners, as Orange's Test & Score does.
        if results.learners is not None and len(results.learners) == len(evaluation.learners):
            results.learners = list(evaluation.learners)
            results.learner_names = [learner.name for learner in evaluation.learners]
        if results.data is None:
            results.data = target_table(results)
        self.Information.not_downloaded.clear()
        self.Outputs.evaluations_results.send(results)

    # ------------------------------------------------------------------
    # Download

    def confirm_download(self, size_bytes: int) -> bool:
        """Ask before downloading large evaluation results."""
        answer = QMessageBox.question(
            self,
            "Download evaluation results",
            f"The evaluation results are {format_bytes(size_bytes)}: a prediction "
            "for every row and learner.\n\nDownload them to this computer?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def download(self) -> bool:
        """Download the evaluation results and send them to the output. Asks
        first when they're larger than :data:`CONFIRM_DOWNLOAD_BYTES`."""
        evaluation = self.evaluation
        if (
            evaluation is None
            or evaluation.results_info is None
            or evaluation.local is not None
            or self.task is not None
        ):
            return False
        size = evaluation.results_info.size_bytes
        if size is not None and size > CONFIRM_DOWNLOAD_BYTES and not self.confirm_download(size):
            return False
        handle = self.session.handle
        machine_id = handle.machine_id if handle is not None else None
        size_text = f" ({format_bytes(size)})" if size is not None else ""

        def work(task: CloudTask) -> Results:
            if evaluation.machine_id is not None and machine_id != evaluation.machine_id:
                raise CloudError(None, "These results are no longer available. Run again.")
            task.set_status(f"Downloading the evaluation results{size_text}")
            return task.get_object(evaluation.results_info.id)

        started = self.run_cloud(work, on_done=lambda local: self._downloaded(evaluation, local))
        if started:
            self._running = "download"
        self._update_info()
        return started

    def _downloaded(self, evaluation: _Evaluation, local: Results) -> None:
        self._running = ""
        if evaluation is not self.evaluation:  # evaluated again meanwhile
            return
        evaluation.local = local
        self._send_results(evaluation)
        self._update_info()

    # ------------------------------------------------------------------
    # Scores table

    def _update_scores(self) -> None:
        model = self.score_table.model
        model.removeRows(0, model.rowCount())
        evaluation = self.evaluation
        if evaluation is None:
            self.score_table.update_header([])
            return
        values = evaluation.values
        scorers = [Score.registry.get(s["id"]) for s in values.get("scorers", [])]
        shown = [i for i, s in enumerate(scorers) if s is not None]
        self.score_table.update_header([scorers[i] for i in shown])
        key = "" if self.class_selection == self.TARGET_AVERAGE else self.class_selection
        scores = values.get("scores", {})
        table = scores.get(key, scores.get(""))
        for i, learner in enumerate(values.get("learners", [])):
            head = QStandardItem(learner["name"])
            row = [head]
            if learner.get("failed"):
                head.setText(f"{learner['name']} (error)")
                head.setToolTip(learner["failed"])
                head.setForeground(QBrush(Qt.GlobalColor.red))
            else:
                for seconds in (learner["train_time"], learner["test_time"]):
                    item = QStandardItem(f"{seconds:.3f}")
                    item.setTextAlignment(RIGHT)
                    row.append(item)
                for col in shown:
                    item = QStandardItem()
                    item.setTextAlignment(RIGHT)
                    value = table[i][col] if table is not None else None
                    if value is not None:
                        item.setData(float(value), Qt.ItemDataRole.DisplayRole)
                    row.append(item)
            model.appendRow(row)
        self.score_table.view.resizeColumnsToContents()

    def scores(self, target: str | None = None) -> dict[str, dict[str, float | None]]:
        """The scores shown for ``target`` (default: the average over
        classes), by learner name and scorer ID. For tests and scripts."""
        if self.evaluation is None:
            return {}
        values = self.evaluation.values
        table = values["scores"].get(target or "", {})
        ids = [s["id"] for s in values["scorers"]]
        return {
            learner["name"]: dict(zip(ids, table[i], strict=True))
            for i, learner in enumerate(values["learners"])
        }

    # ------------------------------------------------------------------

    def cancel(self) -> None:
        super().cancel()
        self._running = ""

    def on_exception(self, ex: Exception) -> None:
        self._running = ""
        super().on_exception(ex)
        self._update_info()

    def cloud_status_text(
        self, info: InfoResponse | None, handle: MachineHandle | None, runtime: float | None
    ) -> str:
        if self._running == "download":
            return f"Downloaded from {machine_label(info, handle)}."
        return super().cloud_status_text(info, handle, runtime)

    def _update_info(self) -> None:
        evaluation = self.evaluation
        if self.data is None or not self.learners:
            missing = [n for n, v in (("data", self.data), ("learners", self.learners)) if not v]
            text = "No " + " and no ".join(missing) + "."
        elif self._running == "evaluate":
            text = "Evaluating on the cloud machine…"
        elif evaluation is None:
            text = "Not evaluated."
        else:
            values = evaluation.values
            if values.get("method") == "test_on_test_data":
                how = "Tested on test data"
            else:
                how = f"{values.get('folds')}-fold cross-validation"
            n_parts = values.get("folds", 1) * len(values.get("learners", []))
            text = (
                f"{how} on {values.get('rows', 0):,} rows: {n_parts} parts, "
                f"up to {values.get('parallel', 1)} at a time on the machine."
            )
        self.info_label.setText(text)

        info = evaluation.results_info if evaluation is not None else None
        size = info.size_bytes if info is not None else None
        size_text = format_bytes(size) if size is not None else ""
        if info is None:
            results = "No results."
        elif self._running == "download":
            results = f"Downloading ({size_text})…"
        elif evaluation.local is not None:
            results = f"{size_text}, downloaded (Evaluation Results)."
        else:
            results = f"{size_text}, on the cloud machine."
        self.results_label.setText(results)
        self.download_button.setEnabled(
            info is not None and evaluation.local is None and not self._running
        )

    def send_report(self) -> None:
        if self.evaluation is not None:
            self.report_items((("Evaluation", self.info_label.text()),))
            self.report_table("Scores", self.score_table.view)


if __name__ == "__main__":  # pragma: no cover
    from Orange.classification import LogisticRegressionLearner, TreeLearner

    WidgetPreview(OWCloudTestAndScore).run(
        insert_learner=[(0, LogisticRegressionLearner()), (1, TreeLearner())]
    )
