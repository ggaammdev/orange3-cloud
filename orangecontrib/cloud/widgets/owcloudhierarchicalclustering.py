"""Cloud Hierarchical Clustering: clusters from distances kept on the machine.

The input is a :class:`~orangecontrib.cloud.data.cloud_distances.CloudDistances`
(from Cloud Distances). The options are those of Orange's Hierarchical
Clustering widget: the linkage (single, average, weighted, complete or Ward)
and a cut, by the number of top clusters or by a share of the tree's height.
The machine clusters the way that widget does (an ``hclust`` job,
``server/ops/hclust.py``): SciPy's linkage and Orange's tree and cuts, with
the clusters numbered C1, C2, ... in the dendrogram's order.

Between rows, the output is a cloud table on the machine: the data the
distances were computed from, with a "Cluster" column. Between columns, it
is a small local table: one row per column, with its cluster. The widget
draws the top of the dendrogram (its top clusters) and lists the clusters'
sizes. Changing only the cut doesn't compute the linkage again.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

import numpy as np
from AnyQt.QtCore import Qt
from AnyQt.QtWidgets import QGraphicsScene, QHeaderView, QLabel, QTableWidget, QTableWidgetItem
from Orange.data import DiscreteVariable, Domain, StringVariable, Table
from Orange.widgets import gui
from Orange.widgets.settings import Setting
from Orange.widgets.utils.dendrogram import DendrogramWidget
from Orange.widgets.utils.graphicsview import GraphicsWidgetView
from Orange.widgets.utils.widgetpreview import WidgetPreview
from Orange.widgets.widget import Input, Msg, Output

from orangecontrib.cloud.client.api import CloudClient, CloudError, CloudNotFoundError
from orangecontrib.cloud.data.cloud_distances import CloudDistances
from orangecontrib.cloud.data.remote_table import RemoteTable
from orangecontrib.cloud.protocol.jobs import JobKind
from orangecontrib.cloud.widgets.base import CloudTask, OWCloudBaseWidget

__all__ = ["OWCloudHierarchicalClustering"]

#: Orange's linkages, in its order, and the job's names for them.
LINKAGES = (
    ("Single", "single"),
    ("Average", "average"),
    ("Weighted", "weighted"),
    ("Complete", "complete"),
    ("Ward", "ward"),
)
#: Orange's ``selection_method`` values (0, "Manual", needs the plot).
HEIGHT_RATIO, TOP_N = 1, 2
#: Leaves of the drawn dendrogram (more when the cut has more clusters).
TOP_CLUSTERS = 50
#: Clusters listed in the sizes table.
MAX_LISTED = 200
COLUMNS_INFO = (
    "Distances between columns: the output is a small local table "
    "of the columns and their clusters."
)


@dataclass
class _Clustered:
    """A finished ``hclust`` job."""

    values: dict[str, Any]
    outputs: dict[str, Any]
    table: Table | None  #: the output (a cloud table, or local between columns)
    #: What the job stored on the machine: (kind, ID), kind "table" or "object".
    stored: list[tuple[str, str]]
    linkage_id: str | None  #: the linkage object (new, or the reused one)


def _free(client: CloudClient, stored: list[tuple[str, str]]) -> None:
    """Delete replaced tables and objects on the machine (best effort)."""
    for kind, item_id in stored:
        with contextlib.suppress(CloudError):
            if kind == "table":
                client.delete_table(item_id)
            else:
                client.delete_object(item_id)


def columns_table(distances: CloudDistances, labels: np.ndarray, n_clusters: int) -> Table:
    """The local output between columns: each column's name and cluster."""
    cluster = DiscreteVariable("Cluster", values=[f"C{i + 1}" for i in range(n_clusters)])
    domain = Domain([], metas=[StringVariable("Column"), cluster])
    names = distances.labels or tuple(f"{i + 1}" for i in range(len(labels)))
    metas = np.array([list(names), list(labels)], dtype=object).T
    table = Table.from_numpy(domain, np.empty((len(labels), 0)), None, metas)
    table.name = "Column clusters"
    return table


def _run(task: CloudTask, distances: CloudDistances, params: dict, linkage_id: str | None) -> Any:
    """Run the ``hclust`` job, reusing the linkage object if given (worker thread)."""
    inputs = {"distances": distances.resolve(distances.machine_id)}
    if distances.axis == "rows":
        inputs["data"] = distances.table_id
    if linkage_id is not None:
        try:
            return task.run_job(JobKind.HCLUST, {**inputs, "linkage": linkage_id}, params), True
        except CloudNotFoundError:  # the linkage is gone: compute it again
            return task.run_job(JobKind.HCLUST, inputs, params), False
    return task.run_job(JobKind.HCLUST, inputs, params), False


def _clustered(
    task: CloudTask, distances: CloudDistances, params: dict, linkage_id: str | None
) -> _Clustered:
    """Cluster on the machine and fetch the small outputs (worker thread)."""
    result, reused = _run(task, distances, params, linkage_id)
    stored = [("table", info.id) for info in result.tables.values()]
    stored += [("object", info.id) for name, info in result.objects.items() if name != "linkage"]
    task.set_status("Fetching the dendrogram")
    outputs = task.client.get_object(result.objects["outputs"].id)
    values = result.values
    table = None
    if "table" in result.tables:
        table = RemoteTable.from_info(
            result.tables["table"], outputs["domain"], distances.machine_id
        )
    elif outputs["labels"] is not None and values["clusters"]:
        table = columns_table(distances, outputs["labels"], values["clusters"])
    new_linkage = result.objects.get("linkage")
    return _Clustered(values, outputs, table, stored, new_linkage.id if new_linkage else linkage_id)


class OWCloudHierarchicalClustering(OWCloudBaseWidget):
    """Hierarchical clustering of cloud distances, on the machine."""

    name = "Cloud Hierarchical Clustering"
    description = (
        "Hierarchical clustering of distances kept on the cloud machine (from Cloud "
        "Distances), cut by the number of clusters or by height."
    )
    icon = "icons/CloudHierarchicalClustering.svg"
    priority = 73
    keywords = "cloud hierarchical clustering, dendrogram, linkage"

    class Inputs:
        distances = Input(
            "Distances", CloudDistances, doc="Distances kept on the machine (Cloud Distances)."
        )

    class Outputs:
        data = Output(
            "Data",
            Table,
            default=True,
            doc="The data with a Cluster column: a cloud table between rows, "
            "a small local table of the columns between columns.",
        )

    class Warning(OWCloudBaseWidget.Warning):
        no_clusters = Msg("No clusters below this height: raise the height ratio.")
        many_clusters = Msg(
            "Variables with too many values may degrade the performance of downstream widgets."
        )

    class Information(OWCloudBaseWidget.Information):
        columns = Msg(COLUMNS_INFO)

    linkage: int = Setting(4)  # Ward, as in Orange
    selection_method: int = Setting(TOP_N)
    cut_ratio: float = Setting(75.0)
    top_n: int = Setting(3)
    autocommit: bool = Setting(True)

    def __init__(self):
        super().__init__()
        self.distances: CloudDistances | None = None
        self.values: dict[str, Any] = {}
        self.clustering: dict[str, Any] = {}
        #: The linkage on the machine: ((machine, distances, linkage), object ID).
        self._linkage: tuple[tuple, str] | None = None
        #: Tables and objects on the machine to delete at the next run:
        #: (machine ID, kind, ID); ``_stored`` the current output's.
        self._stored: list[tuple[str | None, str, str]] = []
        self._stale: list[tuple[str | None, str, str]] = []
        self._init_controls()
        self._init_plot()
        self._update_info()

    def _init_controls(self) -> None:
        box = gui.vBox(self.controlArea, "Linkage")
        gui.comboBox(
            box, self, "linkage", items=[m[0] for m in LINKAGES], callback=self._invalidate
        )
        box = gui.radioButtons(
            self.controlArea, self, "selection_method", box="Cut", callback=self._invalidate
        )
        # Orange's ids; "Manual" (0) is hidden: it needs a selection in the plot.
        gui.appendRadioButton(box, "Manual").hide()
        row = gui.hBox(box)
        gui.appendRadioButton(box, "Height ratio:", insertInto=row)
        gui.spin(
            row, self, "cut_ratio", 0, 100, step=0.1, spinType=float, controlWidth=80,
            callback=self._ratio_changed, keyboardTracking=False,
        ).setSuffix(" %")  # fmt: skip
        row = gui.hBox(box)
        gui.appendRadioButton(box, "Top N:", insertInto=row)
        gui.spin(
            row, self, "top_n", 1, 1000, controlWidth=80,
            callback=self._top_n_changed, keyboardTracking=False,
        )  # fmt: skip
        self.info_label = QLabel()
        gui.vBox(self.controlArea, "Result").layout().addWidget(self.info_label)
        gui.auto_apply(self.buttonsArea, self, "autocommit")

    def _init_plot(self) -> None:
        self.dendrogram = DendrogramWidget(selectionMode=DendrogramWidget.ExtendedSelection)
        self.scene = QGraphicsScene(self)
        self.scene.addItem(self.dendrogram)
        self.view = GraphicsWidgetView(self.scene, widgetResizable=True)
        self.view.setCentralWidget(self.dendrogram)
        self.view.setInteractive(False)  # the cut is set with the options
        self.view.setMinimumWidth(300)
        self.plot_title = QLabel()
        self.sizes = QTableWidget(0, 2)
        self.sizes.setHorizontalHeaderLabels(["Cluster", "Rows"])
        self.sizes.verticalHeader().hide()
        self.sizes.verticalHeader().setDefaultSectionSize(20)
        self.sizes.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.sizes.setFixedWidth(160)
        self.sizes.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        plot = gui.vBox(self.mainArea)
        plot.layout().addWidget(self.plot_title)
        row = gui.hBox(plot)
        row.layout().addWidget(self.view)
        row.layout().addWidget(self.sizes)

    def _ratio_changed(self) -> None:
        self.selection_method = HEIGHT_RATIO
        self._invalidate()

    def _top_n_changed(self) -> None:
        self.selection_method = TOP_N
        self._invalidate()

    def _invalidate(self) -> None:
        self.commit.deferred()

    def _params(self) -> dict[str, Any]:
        return {
            "linkage": LINKAGES[self.linkage][1],
            "cut": "height" if self.selection_method == HEIGHT_RATIO else "clusters",
            "clusters": self.top_n,
            "height_ratio": self.cut_ratio,
            "top": TOP_CLUSTERS,
        }

    # ------------------------------------------------------------------
    # Inputs

    @Inputs.distances
    def set_distances(self, distances: CloudDistances | None) -> None:
        """Receive the Distances input."""
        self.cancel()
        self.distances = distances
        self.commit.now()

    def on_cloud_connected(self) -> None:
        if not self.values and self.task is None and self.distances is not None:
            self.commit.now()

    # ------------------------------------------------------------------
    # Clustering

    @gui.deferred
    def commit(self) -> None:
        """Cluster on the machine with the current options."""
        self.cancel()
        self._clear_output()
        self._clear_cloud_messages()
        distances = self.distances
        if distances is not None:
            handle = self.session.handle
            machine_id = handle.machine_id if handle is not None else None
            stale = [(kind, i) for m, kind, i in self._stale if m == machine_id]
            self._stale = []
            params = self._params()
            key = (distances.machine_id, distances.object_id, params["linkage"])
            linkage_id = self._reusable_linkage(key)

            def work(task: CloudTask) -> _Clustered:
                _free(task.client, stale)
                return _clustered(task, distances, params, linkage_id)

            self.run_cloud(work, on_done=lambda result: self._clustered(result, key))
        self._update_info()

    def _reusable_linkage(self, key: tuple) -> str | None:
        """The linkage object for ``key``; another one becomes stale."""
        if self._linkage is None:
            return None
        old_key, object_id = self._linkage
        if old_key == key:
            return object_id
        self._stale.append((old_key[0], "object", object_id))
        self._linkage = None
        return None

    def _clear_output(self) -> None:
        """Send nothing and clear the plot; the replaced outputs are deleted
        at the next run."""
        self._stale += self._stored
        self._stored = []
        self.values = {}
        self.clustering = {}
        self.Warning.clear()
        self.Information.columns.clear()
        self.dendrogram.set_root(None)
        self.sizes.setRowCount(0)
        self.plot_title.setText("")
        self.Outputs.data.send(None)

    def _clustered(self, clustered: _Clustered, key: tuple) -> None:
        self.values, self.clustering = clustered.values, clustered.outputs
        machine_id = key[0]
        self._stored = [(machine_id, kind, i) for kind, i in clustered.stored]
        if clustered.linkage_id is not None:
            self._linkage = (key, clustered.linkage_id)
        n_clusters = clustered.values["clusters"]
        self.Warning.no_clusters(shown=not n_clusters)
        self.Warning.many_clusters(shown=n_clusters > 20)
        self.Information.columns(shown=clustered.values["axis"] == "columns")
        self._show_plot()
        self.Outputs.data.send(clustered.table)
        self._update_info()

    def _show_plot(self) -> None:
        """Draw the top of the dendrogram with the cut's clusters, and list their sizes."""
        from Orange.clustering.hierarchical import leaves, postorder

        tree, cut = self.clustering["tree"], self.clustering["cut"]
        self.dendrogram.set_root(tree)
        nodes = {node.value.range: node for node in postorder(tree)}
        shown = [nodes[r] for r in map(tuple, cut) if r in nodes]
        self.dendrogram.set_selected_clusters(shown)
        n_leaves = len(list(leaves(tree)))
        self.plot_title.setText(
            f"The top {n_leaves} clusters of {self.values['items']:,} {self.values['axis']}"
            + ("" if len(shown) == len(cut) else f"; {len(shown)} of the {len(cut)} cut clusters")
        )
        sizes = self.clustering["sizes"][:MAX_LISTED]
        self.sizes.setRowCount(len(sizes))
        for i, size in enumerate(sizes):
            self.sizes.setItem(i, 0, QTableWidgetItem(f"C{i + 1}"))
            item = QTableWidgetItem(f"{size:,}")
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.sizes.setItem(i, 1, item)

    # ------------------------------------------------------------------

    def on_exception(self, ex: Exception) -> None:
        super().on_exception(ex)
        self._update_info()

    def cancel(self) -> None:
        super().cancel()
        if hasattr(self, "info_label"):
            self._update_info()

    def _update_info(self) -> None:
        values = self.values
        if self.distances is None:
            text = "No distances."
        elif self.task is not None:
            text = "Clustering on the cloud machine…"
        elif not values:
            text = "Not clustered."
        else:
            linkage = (
                "the linkage reused"
                if values["linkage_reused"]
                else f"linkage {values['linkage_seconds']:.1f} s"
            )
            text = (
                f"{values['clusters']:,} clusters of {values['items']:,} {values['axis']}\n"
                f"in {values['seconds']:.1f} s ({linkage})."
            )
        self.info_label.setText(text)

    def send_report(self) -> None:
        cut = (
            f"height ratio {self.cut_ratio:.1f} %"
            if self.selection_method == HEIGHT_RATIO
            else f"top {self.top_n}"
        )
        self.report_items((("Linkage", LINKAGES[self.linkage][0]), ("Cut", cut)))


if __name__ == "__main__":  # pragma: no cover
    WidgetPreview(OWCloudHierarchicalClustering).run()
