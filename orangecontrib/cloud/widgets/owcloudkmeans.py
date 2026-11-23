"""Cloud k-Means: k-Means clustering of a cloud table on all rows, on the machine.

The options are those of Orange's k-Means widget (a fixed number of clusters
or a range, normalizing, initialization, re-runs, maximum iterations), and
the machine clusters the way that widget does (a ``kmeans`` job,
``server/ops/kmeans.py``). Silhouettes are computed on all rows: the exact
silhouette up to a row limit (a setting), above it the simplified
(centroid-based) silhouette, which the scores table and the output column
name as such. The clustered data stays on the machine and goes out as a
``RemoteTable``; the centroids are a small local table.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

from AnyQt.QtCore import Qt, QTimer
from AnyQt.QtGui import QIntValidator
from AnyQt.QtWidgets import QGridLayout, QLabel, QTableView
from Orange.data import Table
from Orange.widgets import gui
from Orange.widgets.settings import Setting
from Orange.widgets.unsupervised.owkmeans import ClusterTableModel
from Orange.widgets.utils.annotated_data import ANNOTATED_DATA_SIGNAL_NAME
from Orange.widgets.utils.widgetpreview import WidgetPreview
from Orange.widgets.widget import Input, Msg, Output

from orangecontrib.cloud.client.api import CloudClient, CloudError
from orangecontrib.cloud.data.remote_table import LOCAL_DATA, RemoteTable, is_cloud_table, resolve
from orangecontrib.cloud.protocol.jobs import JobKind
from orangecontrib.cloud.widgets.base import CloudTask, OWCloudBaseWidget

__all__ = ["OWCloudKMeans"]

SIMPLIFIED_MESSAGE = (
    "More than {} rows: showing the simplified silhouette (centroid-based), computed on all rows."
)


@dataclass
class _KResult:
    """One k's clustering, as the machine gave it."""

    k: int
    score: float | None
    failed: str | None
    silhouette: str  #: "exact" or "simplified"
    table: RemoteTable | None = None
    centroids: Table | None = None


@dataclass
class _Clustered:
    """A finished ``kmeans`` job."""

    values: dict[str, Any]
    results: dict[int, _KResult]
    #: What the job stored on the machine: (kind, ID), kind "table" or "object".
    stored: list[tuple[str, str]]
    machine_id: str | None


def _free(client: CloudClient, stored: list[tuple[str, str]]) -> None:
    """Delete replaced tables and objects on the machine (best effort)."""
    for kind, item_id in stored:
        with contextlib.suppress(CloudError):
            if kind == "table":
                client.delete_table(item_id)
            else:
                client.delete_object(item_id)


def _clustered(task: CloudTask, data: Table, params: dict, machine_id: str | None) -> _Clustered:
    """Run the ``kmeans`` job on ``data`` and fetch its outputs (worker thread)."""
    client = task.client
    table_id = resolve(data, client, machine_id, wait=task.wait_for_job)
    result = task.run_job(
        JobKind.KMEANS, inputs={"data": table_id}, params=params, progress_range=(0.0, 0.95)
    )
    stored = [("table", info.id) for info in result.tables.values()]
    stored += [("object", info.id) for info in result.objects.values()]
    outputs: dict[int, dict[str, Any]] = {}
    if "outputs" in result.objects:
        task.set_status("Fetching the centroids")
        outputs = client.get_object(result.objects["outputs"].id)
    values = result.values
    results = {}
    for entry in values.get("clusterings", []):
        k = entry["k"]
        k_result = _KResult(k, entry["score"], entry["failed"], values["silhouette"])
        info = result.tables.get(f"k{k}")
        if info is not None and k in outputs:
            k_result.table = RemoteTable.from_info(info, outputs[k]["domain"], machine_id)
            k_result.centroids = outputs[k]["centroids"]
        results[k] = k_result
    return _Clustered(values, results, stored, machine_id)


class OWCloudKMeans(OWCloudBaseWidget):
    """k-Means clustering of a cloud table on all rows, on the machine."""

    name = "Cloud k-Means"
    description = (
        "k-Means clustering of a cloud table on all rows, on the machine, with "
        "silhouette scores computed on all rows."
    )
    icon = "icons/CloudKMeans.svg"
    priority = 70
    keywords = "cloud k-means, kmeans, clustering, silhouette"
    resizing_enabled = False

    class Inputs:
        data = Input("Data", Table, doc="A cloud table (from Cloud File).")

    class Outputs:
        annotated_data = Output(
            ANNOTATED_DATA_SIGNAL_NAME,
            Table,
            default=True,
            doc="The data with cluster and silhouette columns, a table on the cloud machine.",
        )
        centroids = Output(
            "Centroids", Table, dynamic=False, doc="The cluster centres, a small local table."
        )

    class Error(OWCloudBaseWidget.Error):
        failed = Msg("Clustering failed\nError: {}")
        no_attributes = Msg("Data is missing features.")

    class Information(OWCloudBaseWidget.Information):
        simplified = Msg(SIMPLIFIED_MESSAGE)

    INIT_METHODS = (("Initialize with KMeans++", "k-means++"), ("Random initialization", "random"))

    k: int = Setting(3)
    k_from: int = Setting(2)
    k_to: int = Setting(8)
    optimize_k: bool = Setting(False)
    max_iterations: int = Setting(300)
    n_init: int = Setting(10)
    smart_init: int = Setting(0)  # KMeans++
    selection: int | None = Setting(None, schema_only=True)
    auto_commit: bool = Setting(True)
    normalize: bool = Setting(True)
    #: Exact silhouette up to this many rows, the simplified one above.
    silhouette_rows: int = Setting(100_000)

    def __init__(self):
        super().__init__()
        self.data: Table | None = None
        #: Clusterings of the current data and options, by k.
        self.results: dict[int, _KResult] = {}
        #: The last job's values (rows, silhouette kind, timings).
        self.values: dict[str, Any] = {}
        #: Tables and objects on the machine: (machine ID, kind, ID).
        self._stored: list[tuple[str | None, str, str]] = []
        self._stale: list[tuple[str | None, str, str]] = []
        self._pending_selection = self.selection
        self._running = False
        self._init_controls()
        self._init_scores()
        self.apply_button = gui.auto_apply(
            self.buttonsArea, self, "auto_commit", commit=self.commit
        )
        self._show_main_area(False)
        self._update_info()

    def _init_controls(self) -> None:
        layout = QGridLayout()
        bg = gui.radioButtonsInBox(
            self.controlArea,
            self,
            "optimize_k",
            orientation=layout,
            box="Number of Clusters",
            callback=self.update_method,
        )
        layout.addWidget(gui.appendRadioButton(bg, "Fixed:", addToLayout=False), 1, 1)
        sb = gui.hBox(None, margin=0)
        gui.spin(sb, self, "k", minv=2, maxv=30, controlWidth=60, callback=self.update_k)
        gui.rubber(sb)
        layout.addWidget(sb, 1, 2)
        layout.addWidget(gui.appendRadioButton(bg, "From", addToLayout=False), 2, 1)
        ftobox = gui.hBox(None)
        ftobox.layout().setContentsMargins(0, 0, 0, 0)
        layout.addWidget(ftobox, 2, 2)
        gui.spin(
            ftobox, self, "k_from", minv=2, maxv=29, controlWidth=60, callback=self.update_from
        )
        gui.widgetLabel(ftobox, "to")
        gui.spin(ftobox, self, "k_to", minv=3, maxv=30, controlWidth=60, callback=self.update_to)
        gui.rubber(ftobox)

        box = gui.vBox(self.controlArea, "Preprocessing")
        gui.checkBox(box, self, "normalize", "Normalize columns", callback=self.invalidate)

        box = gui.vBox(self.controlArea, "Initialization")
        gui.comboBox(
            box,
            self,
            "smart_init",
            items=[m[0] for m in self.INIT_METHODS],
            callback=self.invalidate,
        )
        grid = QGridLayout()
        gui.widgetBox(box, orientation=grid)
        for row, (label, attr) in enumerate(
            (("Re-runs: ", "n_init"), ("Maximum iterations: ", "max_iterations"))
        ):
            grid.addWidget(gui.widgetLabel(None, label), row, 0, Qt.AlignmentFlag.AlignLeft)
            sb = gui.hBox(None, margin=0)
            grid.addWidget(sb, row, 1)
            gui.lineEdit(
                sb,
                self,
                attr,
                controlWidth=60,
                valueType=int,
                validator=QIntValidator(1, 1_000_000),
                callback=self.invalidate,
            )

        box = gui.vBox(self.controlArea, "Silhouette")
        gui.spin(
            box,
            self,
            "silhouette_rows",
            minv=0,
            maxv=1_000_000,
            step=10_000,
            label="Exact silhouette up to (rows):",
            controlWidth=90,
            callback=self.invalidate,
        )
        note = QLabel("Above it: the simplified silhouette\n(centroid-based), on all rows.")
        note.setStyleSheet("color: gray")
        box.layout().addWidget(note)
        self.info_label = QLabel()
        gui.vBox(self.controlArea, "Result").layout().addWidget(self.info_label)

    def _init_scores(self) -> None:
        self.scores_box = box = gui.vBox(self.mainArea, box="Silhouette Scores")
        self.table_model = ClusterTableModel(self)
        table = self.table_view = QTableView(self.mainArea)
        table.setModel(self.table_model)
        table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        table.setItemDelegate(gui.ColoredBarItemDelegate(self, color=Qt.GlobalColor.cyan))
        table.selectionModel().selectionChanged.connect(self.select_row)
        table.setMaximumWidth(220)
        table.horizontalHeader().setStretchLastSection(True)
        table.horizontalHeader().hide()
        table.setShowGrid(False)
        box.layout().addWidget(table)

    def _show_main_area(self, show: bool) -> None:
        self.mainArea.setVisible(show)
        self.left_side.setContentsMargins(0, 0, 0 if show else 4, 0)

    # ------------------------------------------------------------------
    # Options (as in Orange's k-Means widget)

    def update_method(self) -> None:
        """Rerun after the choice between a fixed and an optimized number of clusters."""
        self.table_model.clear_scores()
        self.commit.deferred()

    def update_k(self) -> None:
        """Rerun with the fixed number of clusters."""
        self.optimize_k = False
        self.table_model.clear_scores()
        self.commit.deferred()

    def update_from(self) -> None:
        """Rerun with the range's new lower end (the upper end follows)."""
        self.k_to = max(self.k_from + 1, self.k_to)
        self.optimize_k = True
        self.table_model.clear_scores()
        self.commit.deferred()

    def update_to(self) -> None:
        """Rerun with the range's new upper end (the lower end follows)."""
        self.k_from = min(self.k_from, self.k_to - 1)
        self.optimize_k = True
        self.table_model.clear_scores()
        self.commit.deferred()

    def invalidate(self, unconditional: bool = False) -> None:
        """Forget the clusterings (new data or options) and cluster again."""
        self.cancel()
        self.Error.clear()
        self.Information.simplified.clear()
        self.results = {}
        self.values = {}
        self._stale += self._stored
        self._stored = []
        self.table_model.clear_scores()
        if unconditional:
            self.commit.now()
        else:
            self.commit.deferred()

    def _ks(self) -> list[int]:
        if self.optimize_k:
            return list(range(self.k_from, self.k_to + 1))
        return [self.k]

    def _params(self, ks: list[int]) -> dict[str, Any]:
        return {
            "ks": ks,
            "init": self.INIT_METHODS[self.smart_init][1],
            "n_init": self.n_init,
            "max_iter": self.max_iterations,
            "normalize": self.normalize,
            "silhouette_rows": self.silhouette_rows,
        }

    # ------------------------------------------------------------------
    # Inputs

    @Inputs.data
    def set_data(self, data: Table | None) -> None:
        """Receive the Data input."""
        self.data = data
        self.selection = None
        self.invalidate(unconditional=True)

    def on_cloud_connected(self) -> None:
        if not self.results and self.task is None and self.data is not None:
            self.commit.now()

    # ------------------------------------------------------------------
    # Clustering

    @gui.deferred
    def commit(self) -> None:
        """Cluster on the machine the ks that aren't clustered yet."""
        self.cancel()
        self.Error.failed.clear()
        self.Error.no_attributes.clear()
        self._clear_cloud_messages()
        self.table_model.clear_scores()
        data = self.data
        self._show_main_area(self.optimize_k and data is not None)
        if data is None:
            self.send_data()
            return
        if not is_cloud_table(data):
            self.send_data()
            self._show_error(self.Error.cloud_error, LOCAL_DATA)
            return
        if not data.domain.attributes:
            self.Error.no_attributes()
            self.send_data()
            return
        needed = [k for k in self._ks() if k not in self.results]
        if not needed:
            self._show_results()
            return
        self._run(data, needed)
        QTimer.singleShot(100, self.adjustSize)

    def _run(self, data: Table, ks: list[int]) -> None:
        handle = self.session.handle
        machine_id = handle.machine_id if handle is not None else None
        stale = [(kind, i) for m, kind, i in self._stale if m == machine_id]
        self._stale = []
        params = self._params(ks)
        self.Outputs.annotated_data.send(None)  # the replaced tables are deleted
        self.Outputs.centroids.send(None)

        def work(task: CloudTask) -> _Clustered:
            _free(task.client, stale)
            return _clustered(task, data, params, machine_id)

        self._running = self.run_cloud(work, on_done=self._clustered)
        self._update_info()

    def _clustered(self, clustered: _Clustered) -> None:
        self._running = False
        self.values = clustered.values
        self.results.update(clustered.results)
        self._stored += [(clustered.machine_id, kind, i) for kind, i in clustered.stored]
        self._show_results()

    def _show_results(self) -> None:
        if self.optimize_k:
            self._update_scores()
            ks = self._ks()
            if all(self.results[k].failed for k in ks):
                self.Error.failed(self.results[ks[-1]].failed)
        elif self.results[self.k].failed:
            self.Error.failed(self.results[self.k].failed)
        self.send_data()
        self._update_info()

    def _simplified(self) -> bool:
        return any(r.silhouette == "simplified" for r in self.results.values())

    def _update_scores(self) -> None:
        title = "Simplified Silhouette Scores" if self._simplified() else "Silhouette Scores"
        self.scores_box.setTitle(title)
        results = [self.results[k] for k in self._ks()]
        scores = [r.failed if r.failed else r.score for r in results]
        best_row = max(
            range(len(scores)),
            default=0,
            key=lambda i: 0 if isinstance(scores[i], str) else scores[i],
        )
        self.table_model.set_scores(scores, self.k_from)
        pending, self._pending_selection = self._pending_selection, None
        self.table_view.selectRow(best_row if pending is None else pending)
        self.table_view.resizeRowsToContents()

    def selected_row(self) -> int | None:
        """The selected row of the scores table."""
        indices = self.table_view.selectedIndexes()
        return indices[0].row() if indices else None

    def select_row(self) -> None:
        """Output the clustering of the selected number of clusters."""
        self.selection = self.selected_row()
        self.send_data()

    def selected_k(self) -> int | None:
        """The k whose clustering goes to the outputs."""
        if not self.optimize_k:
            return self.k
        row = self.selected_row()
        return self.k_from + row if row is not None else None

    def send_data(self) -> None:
        """Send the selected k's clustered data and centroids."""
        result = self.results.get(self.selected_k())
        if self.data is None or result is None or result.failed or self._running:
            self.Information.simplified.clear()
            self.Outputs.annotated_data.send(None)
            self.Outputs.centroids.send(None)
            return
        if result.silhouette == "simplified":
            self.Information.simplified(f"{self.silhouette_rows:,}")
        else:
            self.Information.simplified.clear()
        self.Outputs.annotated_data.send(result.table)
        self.Outputs.centroids.send(result.centroids)

    # ------------------------------------------------------------------

    def cancel(self) -> None:
        super().cancel()
        self._running = False

    def on_exception(self, ex: Exception) -> None:
        self._running = False
        super().on_exception(ex)
        self._update_info()

    def _update_info(self) -> None:
        values = self.values
        if self.data is None:
            text = "No data."
        elif self._running:
            text = "Clustering on the cloud machine…"
        elif not values:
            text = "Not clustered."
        else:
            seconds = values.get("fit_seconds", 0) + values.get("silhouette_seconds", 0)
            kind = "exact" if values.get("silhouette") == "exact" else "simplified"
            text = (
                f"Clustered {values.get('rows', 0):,} rows in {seconds:.1f} s,\n"
                f"{kind} silhouette on all rows."
            )
        self.info_label.setText(text)

    def send_report(self) -> None:
        k = self.selected_k()
        init = self.INIT_METHODS[self.smart_init][0]
        init = init[0].lower() + init[1:]
        steps = f"{init}, {self.n_init} re-runs limited to {self.max_iterations} steps"
        self.report_items((("Number of clusters", k), ("Optimization", steps)))
        if self.data is not None and self.optimize_k:
            self.report_table(self.scores_box.title(), self.table_view)


if __name__ == "__main__":  # pragma: no cover
    WidgetPreview(OWCloudKMeans).run()
