"""Cloud Distances: a distance matrix of a cloud table, kept on the machine.

The options are those of Orange's Distances widget: compare rows or
columns, and its metrics (Euclidean and Manhattan also normalized). The
machine computes the matrix the way that widget does, with
``Orange.distance`` (a ``distances`` job, ``server/ops/distances.py``),
without its limit of 20,000 rows; the limit is the machine's memory. The
output is a :class:`~orangecontrib.cloud.data.cloud_distances.CloudDistances`,
a reference: the matrix stays on the machine, for the Cloud widgets that
use distances.

Before the job, the widget compares the memory the matrix needs (n x n x 8
bytes and the metric's working memory) with the machine's free memory, and
refuses with a message that says what fits; the machine checks again.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple

from AnyQt.QtCore import Qt
from AnyQt.QtWidgets import QButtonGroup, QLabel, QRadioButton
from Orange.data import Table
from Orange.widgets import gui
from Orange.widgets.settings import Setting
from Orange.widgets.utils.widgetpreview import WidgetPreview
from Orange.widgets.widget import Input, Msg, Output
from orangewidget.utils.signals import PartialSummary, summarize

from orangecontrib.cloud.client.api import CloudClient, CloudError
from orangecontrib.cloud.client.providers.sizes import DISK_DEFAULT_GB, MachineType
from orangecontrib.cloud.data.cloud_distances import (
    CloudDistances,
    DistancesTooLargeError,
    check_memory,
)
from orangecontrib.cloud.data.remote_table import LOCAL_DATA, is_cloud_table, resolve
from orangecontrib.cloud.gui.machine_size import SizeNeed, size_hints
from orangecontrib.cloud.protocol.jobs import JobKind
from orangecontrib.cloud.widgets.base import CloudTask, OWCloudBaseWidget

__all__ = ["METRICS", "OWCloudDistances"]


class Metric(NamedTuple):
    """A metric as Orange's Distances widget offers it."""

    id: int  #: Orange's id (the ``metric_id`` setting)
    name: str
    tooltip: str
    key: str  #: the ``distances`` job's metric
    normalize: bool = False


# Orange's ids (Orange.widgets.unsupervised.utils), so settings mean the same.
(
    EUCLIDEAN,
    EUCLIDEAN_NORMALIZED,
    MANHATTAN,
    MANHATTAN_NORMALIZED,
    COSINE,
    MAHALANOBIS,
    HAMMING,
    PEARSON,
    PEARSON_ABSOLUTE,
    SPEARMAN,
    SPEARMAN_ABSOLUTE,
    JACCARD,
) = range(12)

#: The metrics in the order of Orange's widget.
METRICS: dict[int, Metric] = {
    m.id: m
    for m in (
        Metric(
            EUCLIDEAN_NORMALIZED,
            "Euclidean (normalized)",
            "Square root of summed difference between normalized values",
            "euclidean",
            True,
        ),
        Metric(
            EUCLIDEAN, "Euclidean", "Square root of summed difference between values", "euclidean"
        ),
        Metric(
            MANHATTAN_NORMALIZED,
            "Manhattan (normalized)",
            "Sum of absolute differences between normalized values",
            "manhattan",
            True,
        ),
        Metric(MANHATTAN, "Manhattan", "Sum of absolute differences between values", "manhattan"),
        Metric(MAHALANOBIS, "Mahalanobis", "Mahalanobis distance", "mahalanobis"),
        Metric(HAMMING, "Hamming", "Hamming distance", "hamming"),
        Metric(COSINE, "Cosine", "Cosine distance", "cosine"),
        Metric(PEARSON, "Pearson", "Pearson correlation; distance = 1 - ρ/2", "pearson"),
        Metric(
            PEARSON_ABSOLUTE,
            "Pearson (absolute)",
            "Absolute value of Pearson correlation; distance = 1 - |ρ|",
            "pearson_absolute",
        ),
        Metric(SPEARMAN, "Spearman", "Spearman correlation; distance = 1 - ρ/2", "spearman"),
        Metric(
            SPEARMAN_ABSOLUTE,
            "Spearman (absolute)",
            "Absolute value of Spearman correlation; distance = 1 - |ρ|",
            "spearman_absolute",
        ),
        Metric(JACCARD, "Jaccard", "Jaccard distance", "jaccard"),
    )
}
AXES = ("rows", "columns")


@dataclass
class _Computed:
    """A finished ``distances`` job."""

    values: dict[str, Any]
    distances: CloudDistances


def _compute(
    task: CloudTask,
    data: Table,
    params: dict,
    machine_id: str | None,
    sizes: Sequence[MachineType],
) -> _Computed:
    """Check the memory, then run the ``distances`` job (worker thread).
    ``sizes``: the machine sizes to suggest if it doesn't fit."""
    client = task.client
    table_id = resolve(data, client, machine_id, wait=task.wait_for_job)
    task.set_status("Checking the machine's memory")
    rows = client.table_info(table_id).n_rows
    columns = len(data.domain.attributes)
    items = columns if params["axis"] == "columns" else rows
    check_memory(items, params["axis"], params["metric"], rows * columns * 8, client.info(), sizes)
    result = task.run_job(JobKind.DISTANCES, inputs={"data": table_id}, params=params)
    info, values = result.objects["distances"], result.values
    labels = values.get("labels")
    distances = CloudDistances(
        object_id=info.id,
        n_items=values["items"],
        metric=values["metric"],
        axis=values["axis"],
        table_id=table_id,
        domain=data.domain,
        labels=tuple(labels) if labels is not None else None,
        machine_id=machine_id,
        size_bytes=info.size_bytes,
    )
    return _Computed(values, distances)


@summarize.register
def summarize_cloud_distances(distances: CloudDistances) -> PartialSummary:
    """The output's summary in Orange's canvas."""
    return PartialSummary(f"{distances.n_items:,}×{distances.n_items:,}", str(distances))


def _free(client: CloudClient, object_ids: list[str]) -> None:
    """Delete replaced distance matrices on the machine (best effort)."""
    for object_id in object_ids:
        with contextlib.suppress(CloudError):
            client.delete_object(object_id)


def _size(n_bytes: float) -> str:
    gb = n_bytes / 1024**3
    return f"{gb:.1f} GB" if gb >= 0.1 else f"{n_bytes / 1024**2:.1f} MB"


class OWCloudDistances(OWCloudBaseWidget):
    """Distances between the rows or columns of a cloud table, kept on the machine."""

    name = "Cloud Distances"
    description = (
        "Distances between the rows or columns of a cloud table, computed and kept on the machine."
    )
    icon = "icons/CloudDistances.svg"
    priority = 72
    keywords = "cloud distances, distance matrix"
    want_main_area = False
    resizing_enabled = False

    class Inputs:
        data = Input("Data", Table, doc="A cloud table (from Cloud File).")

    class Outputs:
        distances = Output(
            "Distances",
            CloudDistances,
            dynamic=False,
            doc="The distance matrix, kept on the cloud machine.",
        )

    class Warning(OWCloudBaseWidget.Warning):
        data_changed = Msg("{}")

    axis: int = Setting(0)
    metric_id: int = Setting(EUCLIDEAN_NORMALIZED)
    autocommit: bool = Setting(True)

    def __init__(self):
        super().__init__()
        self.data: Table | None = None
        self.values: dict[str, Any] = {}
        self.output: CloudDistances | None = None
        #: Matrices on the machine to delete at the next run: (machine ID, ID).
        self._stale: list[tuple[str | None, str]] = []
        self._init_controls()
        self._update_info()

    def _init_controls(self) -> None:
        gui.radioButtons(
            self.controlArea,
            self,
            "axis",
            ["Rows", "Columns"],
            box="Compare",
            orientation=Qt.Orientation.Horizontal,
            callback=self._invalidate,
        )
        box = gui.hBox(self.controlArea, "Distance Metric")
        self.metric_buttons = QButtonGroup()
        width = 0
        for i, metric in enumerate(METRICS.values()):
            if i % 6 == 0:
                column = gui.vBox(box)
            button = QRadioButton(metric.name)
            button.setChecked(self.metric_id == metric.id)
            button.setToolTip(metric.tooltip)
            column.layout().addWidget(button)
            width = max(width, button.sizeHint().width())
            self.metric_buttons.addButton(button, metric.id)
        for button in self.metric_buttons.buttons():
            button.setFixedWidth(width)
        self.metric_buttons.idClicked.connect(self._metric_changed)
        self.info_label = QLabel()
        gui.vBox(self.controlArea, "Result").layout().addWidget(self.info_label)
        gui.auto_apply(self.buttonsArea, self, "autocommit")

    def _metric_changed(self, metric_id: int) -> None:
        self.metric_id = metric_id
        self._invalidate()

    def _invalidate(self) -> None:
        self.commit.deferred()

    def _params(self) -> dict[str, Any]:
        metric = METRICS[self.metric_id]
        return {"metric": metric.key, "normalize": metric.normalize, "axis": AXES[self.axis]}

    # ------------------------------------------------------------------
    # Inputs

    @Inputs.data
    def set_data(self, data: Table | None) -> None:
        """Receive the Data input."""
        self.cancel()
        self.data = data
        self.commit.now()

    def on_cloud_connected(self) -> None:
        if self.output is None and self.task is None and is_cloud_table(self.data):
            self.commit.now()

    # ------------------------------------------------------------------
    # Running

    @gui.deferred
    def commit(self) -> None:
        """Compute the distances on the machine with the current options."""
        self.cancel()
        self._clear_output()
        self._clear_cloud_messages()
        self.Warning.data_changed.clear()
        size_hints().set(self, None)
        data = self.data
        if data is not None and not is_cloud_table(data):
            self._show_error(self.Error.cloud_error, LOCAL_DATA)
        elif data is not None:
            handle = self.session.handle
            machine_id = handle.machine_id if handle is not None else None
            stale = [i for m, i in self._stale if m == machine_id]
            self._stale = []
            params = self._params()
            _, sizes = self.size_choices()

            def work(task: CloudTask) -> _Computed:
                _free(task.client, stale)
                return _compute(task, data, params, machine_id, sizes)

            self.run_cloud(work, on_done=self._computed)
        self._update_info()

    def _clear_output(self) -> None:
        """Send nothing; the replaced matrix is deleted at the next run."""
        if self.output is not None:
            self._stale.append((self.output.machine_id, self.output.object_id))
        self.output = None
        self.values = {}
        self.Outputs.distances.send(None)

    def _computed(self, computed: _Computed) -> None:
        self.values = computed.values
        self.output = computed.distances
        warnings = computed.values.get("warnings") or []
        self.Warning.data_changed("\n".join(warnings), shown=bool(warnings))
        self.Outputs.distances.send(computed.distances)
        self._update_info()

    def on_exception(self, ex: Exception) -> None:
        super().on_exception(ex)
        if isinstance(ex, DistancesTooLargeError):
            # The Cloud settings' size picker suggests a machine that fits.
            need = SizeNeed(ex.needed_bytes, ex.needed_bytes, DISK_DEFAULT_GB, "Cloud Distances")
            size_hints().set(self, need)
        self._update_info()

    def cancel(self) -> None:
        super().cancel()
        self._update_info()

    def _update_info(self) -> None:
        values = self.values
        if self.data is None:
            text = "No data."
        elif self.task is not None:
            text = "Computing on the cloud machine…"
        elif not values:
            text = "Not computed."
        else:
            n = values["items"]
            text = (
                f"{values['metric']} distances between {n:,} {values['axis']}:\n"
                f"a {n:,} × {n:,} matrix ({_size(n * n * 8)}) on the machine,\n"
                f"computed in {values['seconds']:.1f} s."
            )
        self.info_label.setText(text)

    def send_report(self) -> None:
        self.report_items(
            (
                ("Distances Between", ["Rows", "Columns"][self.axis]),
                ("Metric", METRICS[self.metric_id].name),
            )
        )


if __name__ == "__main__":  # pragma: no cover
    WidgetPreview(OWCloudDistances).run()
