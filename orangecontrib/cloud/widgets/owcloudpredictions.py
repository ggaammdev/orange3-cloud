"""Cloud Predictions: predict a cloud table with cloud models, on the machine.

The table's ID and the models' IDs (from Cloud Learner) go to the machine as
a ``predict`` job (``server/ops/predict.py``). The predictions stay there as
a new cloud table, the data plus a prediction column and, for a categorical
target, probability columns per model, like the output of Orange's
Predictions widget; it goes out as a ``RemoteTable`` (view it with Cloud
Data Table). When the data has the target the models predict, the scores
table shows their scores on all rows, computed on the machine.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

from AnyQt.QtCore import Qt
from AnyQt.QtGui import QBrush, QStandardItem
from AnyQt.QtWidgets import QLabel
from Orange.data import Table
from Orange.evaluation.scoring import Score
from Orange.widgets import gui
from Orange.widgets.evaluate.utils import ScoreTable
from Orange.widgets.settings import Setting, SettingProvider
from Orange.widgets.utils.widgetpreview import WidgetPreview
from Orange.widgets.widget import Input, Msg, MultiInput, Output

from orangecontrib.cloud.client.api import CloudError, CloudNotFoundError
from orangecontrib.cloud.data.cloud_model import CloudModel, ModelUnavailableError
from orangecontrib.cloud.data.remote_table import LOCAL_DATA, RemoteTable, is_cloud_table, resolve
from orangecontrib.cloud.protocol.jobs import JobKind
from orangecontrib.cloud.protocol.tables import TableInfo
from orangecontrib.cloud.widgets import owcloudlearner  # noqa: F401 (CloudModel summaries)
from orangecontrib.cloud.widgets.base import CloudTask, OWCloudBaseWidget

__all__ = ["OWCloudPredictions"]

RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter


@dataclass
class _Predicted:
    """A finished ``predict`` job: its values and the output table."""

    values: dict[str, Any]
    table: RemoteTable | None  #: None if every model failed
    info: TableInfo | None
    machine_id: str | None


class OWCloudPredictions(OWCloudBaseWidget):
    """Predict a cloud table with cloud models, on the machine."""

    name = "Cloud Predictions"
    description = (
        "Predict a cloud table with cloud models, on the machine. The predictions "
        "stay in the cloud; scores are computed on all rows."
    )
    icon = "icons/CloudPredictions.svg"
    priority = 50
    keywords = "cloud predictions, predict, apply model, score"
    want_main_area = True

    class Inputs:
        data = Input("Data", Table, default=True, doc="A cloud table (from Cloud File).")
        predictors = MultiInput(
            "Predictors", CloudModel, filter_none=True, doc="Cloud models (from Cloud Learner)."
        )

    class Outputs:
        predictions = Output(
            "Predictions",
            Table,
            doc="The data with predictions and probabilities, a table on the cloud machine.",
        )

    class Error(OWCloudBaseWidget.Error):
        predictor_failed = Msg("{}")

    class Warning(OWCloudBaseWidget.Warning):
        wrong_targets = Msg("Some models predict a different target (no scores):\n{}")
        missing_targets = Msg(
            "{} rows have no target value; the scores are computed on the other {} rows."
        )

    TARGET_AVERAGE = "(Average over classes)"

    score_table = SettingProvider(ScoreTable)
    target_class: str = Setting(TARGET_AVERAGE)

    _running = False

    def __init__(self):
        super().__init__()
        self.data: Table | None = None
        self.predictors: list[CloudModel] = []
        self.predicted: _Predicted | None = None
        #: Replaced prediction tables, deleted on the machine at the next run.
        self._stale: list[_Predicted] = []

        box = gui.vBox(self.controlArea, "Scores")
        self.target_combo = gui.comboBox(
            box,
            self,
            "target_class",
            label="Target class:",
            items=[],
            sendSelectedValue=True,
            callback=self._update_scores,
        )
        box = gui.vBox(self.controlArea, "Predictions")
        self.output_label = QLabel()
        self.output_label.setWordWrap(False)
        box.layout().addWidget(self.output_label)
        gui.rubber(self.controlArea)

        self.score_table = ScoreTable(self)
        self.info_label = QLabel()
        self.mainArea.layout().addWidget(self.info_label)
        self.mainArea.layout().addWidget(self.score_table.view)
        self._update_target_combo()
        self._update_info()

    # ------------------------------------------------------------------
    # Inputs

    @Inputs.data
    def set_data(self, data: Table | None) -> None:
        """Receive the Data input."""
        self.data = data
        self._update_target_combo()

    @Inputs.predictors
    def set_predictor(self, index: int, predictor: CloudModel) -> None:
        """Replace the predictor at ``index``."""
        self.predictors[index] = predictor

    @Inputs.predictors.insert
    def insert_predictor(self, index: int, predictor: CloudModel) -> None:
        """Receive a new Predictors input."""
        self.predictors.insert(index, predictor)

    @Inputs.predictors.remove
    def remove_predictor(self, index: int) -> None:
        """Remove a Predictors input."""
        self.predictors.pop(index)

    def handleNewSignals(self) -> None:
        self.predict()

    def on_cloud_connected(self) -> None:
        if self.predicted is None and self.task is None and self._ready():
            self.predict()

    def _ready(self) -> bool:
        return self.data is not None and bool(self.predictors)

    def _update_target_combo(self) -> None:
        combo = self.target_combo
        combo.clear()
        class_var = self.data.domain.class_var if self.data is not None else None
        if class_var is None or not class_var.is_discrete:
            combo.setEnabled(False)
            return
        combo.setEnabled(True)
        items = [self.TARGET_AVERAGE, *class_var.values]
        combo.addItems(items)
        if self.target_class not in items:
            self.target_class = self.TARGET_AVERAGE
        combo.setCurrentIndex(items.index(self.target_class))

    # ------------------------------------------------------------------
    # Predicting

    def _clear(self) -> None:
        if self.predicted is not None:
            self._stale.append(self.predicted)
        self.predicted = None
        self.Outputs.predictions.send(None)
        self.Error.predictor_failed.clear()
        self.Warning.wrong_targets.clear()
        self.Warning.missing_targets.clear()

    def predict(self) -> None:
        """Predict the cloud table with the cloud models (a ``predict`` job)."""
        self.cancel()
        self._clear_cloud_messages()
        self._clear()
        self._update_scores()
        if not self._ready():
            self._update_info()
            return
        if not is_cloud_table(self.data):
            self._show_error(self.Error.cloud_error, LOCAL_DATA)
            self._update_info()
            return
        data, models = self.data, list(self.predictors)
        handle = self.session.handle
        machine_id = handle.machine_id if handle is not None else None
        stale, self._stale = self._stale, []

        def work(task: CloudTask) -> _Predicted:
            client = task.client
            for old in stale:  # free the replaced predictions (best effort)
                if old.info is not None and old.machine_id == machine_id:
                    with contextlib.suppress(CloudError):
                        client.delete_table(old.info.id)
            table_id = resolve(data, client, machine_id)
            model_ids = [model.resolve(machine_id) for model in models]
            try:
                result = task.run_job(
                    JobKind.PREDICT,
                    inputs={"data": table_id, "models": model_ids},
                    params={"names": [model.name for model in models]},
                    progress_range=(0.0, 0.95),
                )
            except CloudNotFoundError as exc:  # the table was there a moment ago
                raise ModelUnavailableError() from exc
            info = result.tables.get("table")
            table = None
            if info is not None:
                task.set_status("Fetching the predictions' columns")
                table = RemoteTable.from_info(info, client.table_domain(info.id), machine_id)
            return _Predicted(result.values, table, info, machine_id)

        if self.run_cloud(work, on_done=self._predicted):
            self._running = True  # after run_cloud, whose cancel() resets it
        else:
            self._stale = stale + self._stale
        self._update_info()

    def _predicted(self, predicted: _Predicted) -> None:
        self._running = False
        self.predicted = predicted
        values = predicted.values
        models = values.get("models", [])
        failed = [f"{m['name']}: {m['failed']}" for m in models if m.get("failed")]
        if failed:
            self.Error.predictor_failed("\n".join(failed))
        if self.data is not None and self.data.domain.class_var is not None:
            wrong = [
                f"{m['name']} predicts '{m['target']}'"
                for m in models
                if not m.get("failed") and not m.get("scored")
            ]
            if wrong:
                self.Warning.wrong_targets("\n".join(wrong))
            missing = values.get("missing_targets", 0)
            if missing and any(m.get("scored") for m in models):
                self.Warning.missing_targets(f"{missing:,}", f"{values.get('scored_rows', 0):,}")
        self.Outputs.predictions.send(predicted.table)
        self._update_scores()
        self._update_info()

    # ------------------------------------------------------------------
    # Scores table

    def _update_scores(self) -> None:
        model = self.score_table.model
        model.removeRows(0, model.rowCount())
        predicted = self.predicted
        values = predicted.values if predicted is not None else {}
        scorers = [Score.registry.get(s["id"]) for s in values.get("scorers", [])]
        shown = [i for i, s in enumerate(scorers) if s is not None]
        self.score_table.update_header([scorers[i] for i in shown])
        key = "" if self.target_class == self.TARGET_AVERAGE else self.target_class
        scores = values.get("scores", {})
        table = scores.get(key, scores.get(""))
        for i, m in enumerate(values.get("models", [])):
            if not m.get("scored") and not m.get("failed"):
                continue  # predicts another target: no scores, as in Orange
            head = QStandardItem(m["name"])
            row = [head, QStandardItem("N/A")]
            if m.get("failed"):
                head.setText(f"{m['name']} (error)")
                head.setToolTip(m["failed"])
                head.setForeground(QBrush(Qt.GlobalColor.red))
            else:
                item = QStandardItem(f"{m['seconds']:.3f}")
                item.setTextAlignment(RIGHT)
                row.append(item)
                for col in shown:
                    item = QStandardItem()
                    item.setTextAlignment(RIGHT)
                    value = table[i][col] if table is not None and table[i] is not None else None
                    if value is not None:
                        item.setData(float(value), Qt.ItemDataRole.DisplayRole)
                    row.append(item)
            model.appendRow(row)
        self.score_table.view.setVisible(bool(shown) and model.rowCount() > 0)
        self.score_table.view.resizeColumnsToContents()

    def scores(self, target: str | None = None) -> dict[str, dict[str, float | None]]:
        """The scores for ``target`` (default: the average over classes), by
        model name and scorer ID; models without scores are left out. For
        tests and scripts."""
        if self.predicted is None:
            return {}
        values = self.predicted.values
        table = values.get("scores", {}).get(target or "")
        if table is None:
            return {}
        ids = [s["id"] for s in values["scorers"]]
        return {
            m["name"]: dict(zip(ids, table[i], strict=True))
            for i, m in enumerate(values["models"])
            if table[i] is not None
        }

    # ------------------------------------------------------------------

    def cancel(self) -> None:
        super().cancel()
        self._running = False

    def on_exception(self, ex: Exception) -> None:
        self._running = False
        super().on_exception(ex)
        self._update_info()

    def _update_info(self) -> None:
        predicted = self.predicted
        if not self._ready():
            missing = [n for n, v in (("data", self.data), ("models", self.predictors)) if not v]
            text = "No " + " and no ".join(missing) + "."
        elif self._running:
            text = "Predicting on the cloud machine…"
        elif predicted is None:
            text = "Not predicted."
        else:
            values = predicted.values
            n_models = len(values.get("models", []))
            seconds = values.get("predict_seconds")
            took = f" in {seconds:.1f} s" if seconds is not None else ""
            text = (
                f"Predicted {values.get('rows', 0):,} rows with {n_models} "
                f"model{'s' if n_models != 1 else ''}{took}."
            )
            if values.get("scorers") and any(m.get("scored") for m in values.get("models", [])):
                text += f" Scores on {values.get('scored_rows', 0):,} rows."
        self.info_label.setText(text)

        info = predicted.info if predicted is not None else None
        if info is None:
            output = "No predictions."
        else:
            n_columns = info.n_attributes + info.n_class_vars + info.n_metas
            output = (
                f"{info.n_rows:,} rows, {n_columns:,} columns,\n"
                "on the cloud machine (view with Cloud Data Table)."
            )
        self.output_label.setText(output)

    def send_report(self) -> None:
        if self.predicted is not None:
            self.report_items((("Predictions", self.info_label.text()),))
            if self.score_table.view.isVisible():
                self.report_table("Scores", self.score_table.view)


if __name__ == "__main__":  # pragma: no cover
    WidgetPreview(OWCloudPredictions).run()
