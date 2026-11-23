"""Cloud MDS: multidimensional scaling of cloud data or cloud distances, on the machine.

The input is a cloud table (the machine computes Euclidean distances
between its rows, as Orange's MDS widget does) or a
:class:`~orangecontrib.cloud.data.cloud_distances.CloudDistances` (from
Cloud Distances), or both: then the distances are embedded and the table is
the output's data. The options are those of Orange's MDS widget: the
initial positions (PCA, which is Torgerson's classical MDS, or random) and
the maximum number of iterations. The machine optimizes the way that widget
does (an ``mds`` job, ``server/ops/mds.py``): ``Orange.projection.MDS`` in
steps of 25 iterations. The widget doesn't draw the map: its output, the
data with "mds-x" and "mds-y" columns, stays on the machine as a
``RemoteTable``; view it with Cloud Scatter Plot. Between columns, the
output is a small local table of the columns and their positions.

As in Orange, new inputs start the optimization; changed options wait for
"Start". Before the job, the widget checks that the machine has the memory
SMACOF needs (several n x n matrices), and warns before long runs.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from AnyQt.QtWidgets import QLabel
from Orange.data import ContinuousVariable, Domain, StringVariable, Table
from Orange.widgets import gui
from Orange.widgets.settings import Setting
from Orange.widgets.utils.annotated_data import ANNOTATED_DATA_SIGNAL_NAME
from Orange.widgets.utils.widgetpreview import WidgetPreview
from Orange.widgets.widget import Input, Msg, Output

from orangecontrib.cloud.client.api import CloudClient, CloudError
from orangecontrib.cloud.client.providers.sizes import DISK_DEFAULT_GB, MachineType
from orangecontrib.cloud.data.cloud_distances import (
    CloudDistances,
    DistancesTooLargeError,
    check_mds_memory,
)
from orangecontrib.cloud.data.remote_table import LOCAL_DATA, RemoteTable, is_cloud_table, resolve
from orangecontrib.cloud.gui.machine_size import SizeNeed, size_hints
from orangecontrib.cloud.protocol.jobs import JobKind
from orangecontrib.cloud.widgets.base import CloudTask, OWCloudBaseWidget

__all__ = ["OWCloudMDS", "rough_runtime"]

#: Orange's ``initialization`` values ("Jitter", 2, needs earlier positions).
PCA, RANDOM = 0, 1
INITIALIZATIONS = (("PCA (Torgerson)", "pca"), ("Random", "random"))
#: Warn before runs that may take longer than this (seconds, rough).
LONG_RUNTIME = 60
#: Rough seconds per iteration and per item squared (the steps start SMACOF
#: again, so this includes their setup): measured 4.6e-9 to 6.5e-9 on Google
#: Cloud's n2-highmem-16, 2.1e-9 on an Apple M-series laptop. More CPUs don't help:
#: SMACOF's work is mostly on one CPU and memory-bound.
SECONDS_PER_ITERATION = 6.5e-9


def rough_runtime(items: int, max_iter: int) -> float:
    """A rough guess of the most seconds MDS takes on the machine; it may
    stop earlier, but the steps often run to the maximum."""
    return max_iter * items * items * SECONDS_PER_ITERATION


def _duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 2:
        return "about a minute"
    if minutes < 90:
        return f"{minutes} minutes"
    return f"{seconds / 3600:.0f} hours"


def columns_table(distances: CloudDistances, embedding: np.ndarray) -> Table:
    """The local output between columns: each column's name and position,
    as Orange's widget outputs them."""
    positions = [ContinuousVariable("mds-x"), ContinuousVariable("mds-y")]
    domain = Domain([], metas=[StringVariable("labels"), *positions])
    names = distances.labels or tuple(f"{i + 1}" for i in range(len(embedding)))
    metas = np.empty((len(embedding), 3), dtype=object)
    metas[:, 0], metas[:, 1:] = names, embedding
    table = Table.from_numpy(domain, np.empty((len(embedding), 0)), None, metas)
    table.name = "Column positions"
    return table


@dataclass
class _Embedded:
    """A finished ``mds`` job."""

    values: dict[str, Any]
    table: Table  #: a cloud table, or local between columns
    stored: list[tuple[str, str]]  #: what the job kept on the machine: (kind, ID)


def _inputs(
    task: CloudTask, data: Table | None, distances: CloudDistances | None, machine_id: str | None
) -> tuple[dict[str, str], int, str, int | None]:
    """The job's inputs, the number of items, the axis and the data's bytes
    (when the machine computes the distances)."""
    client, inputs = task.client, {}
    if data is not None:
        inputs["data"] = resolve(data, client, machine_id, wait=task.wait_for_job)
    if distances is not None:
        inputs["distances"] = distances.resolve(machine_id)
        if data is None and distances.axis == "rows":
            inputs["data"] = distances.table_id
        return inputs, distances.n_items, distances.axis, None
    rows = client.table_info(inputs["data"]).n_rows
    return inputs, rows, "rows", rows * len(data.domain.attributes) * 8


def _embed(
    task: CloudTask,
    data: Table | None,
    distances: CloudDistances | None,
    params: dict,
    machine_id: str | None,
    sizes: Sequence[MachineType],
) -> _Embedded:
    """Check the memory, then run the ``mds`` job (worker thread); first hands
    the number of items to the widget (for the runtime warning). ``sizes``:
    the machine sizes to suggest if it doesn't fit."""
    client = task.client
    inputs, items, axis, data_bytes = _inputs(task, data, distances, machine_id)
    task.send_partial(items)
    task.set_status("Checking the machine's memory")
    check_mds_memory(items, axis, data_bytes, client.info(), sizes)
    result = task.run_job(JobKind.MDS, inputs=inputs, params=params, progress_range=(0.0, 0.98))
    if "table" in result.tables:
        info = result.tables["table"]
        task.set_status("Fetching the domain")
        table = RemoteTable.from_info(info, client.table_domain(info.id), machine_id)
        return _Embedded(result.values, table, [("table", info.id)])
    info = result.objects["embedding"]
    task.set_status("Fetching the positions")
    embedding = client.get_object(info.id)
    return _Embedded(result.values, columns_table(distances, embedding), [("object", info.id)])


def _free(client: CloudClient, stored: list[tuple[str, str]]) -> None:
    """Delete replaced tables and objects on the machine (best effort)."""
    for kind, item_id in stored:
        with contextlib.suppress(CloudError):
            if kind == "table":
                client.delete_table(item_id)
            else:
                client.delete_object(item_id)


class OWCloudMDS(OWCloudBaseWidget):
    """MDS of a cloud table or of cloud distances, on the machine."""

    name = "Cloud MDS"
    description = (
        "Multidimensional scaling of a cloud table or of distances kept on the cloud "
        "machine. View the result with Cloud Scatter Plot."
    )
    icon = "icons/CloudMDS.svg"
    priority = 74
    keywords = "cloud mds, multidimensional scaling, projection"
    resizing_enabled = False

    class Inputs:
        data = Input("Data", Table, default=True, doc="A cloud table (from Cloud File).")
        distances = Input(
            "Distances", CloudDistances, doc="Distances kept on the machine (Cloud Distances)."
        )

    class Outputs:
        annotated_data = Output(
            ANNOTATED_DATA_SIGNAL_NAME,
            Table,
            default=True,
            doc="The data with mds-x and mds-y columns, a table on the cloud machine "
            "(between columns: a small local table).",
        )

    class Information(OWCloudBaseWidget.Information):
        modified = Msg(
            'The parameter settings have been changed. Press "Start" to rerun with '
            "the new settings."
        )

    class Warning(OWCloudBaseWidget.Warning):
        long_runtime = Msg(
            "MDS of {:,} items may take up to {} (a rough estimate). Press Stop to cancel."
        )

    initialization: int = Setting(PCA)
    max_iter: int = Setting(300)

    def __init__(self):
        super().__init__()
        self.data: Table | None = None
        self.distances: CloudDistances | None = None
        #: The last finished job's values (items, iterations, stress, timings).
        self.values: dict[str, Any] = {}
        self.output: Table | None = None
        #: Tables and objects on the machine to delete at the next run:
        #: (machine ID, kind, ID); ``_stored`` the current output's.
        self._stored: list[tuple[str | None, str, str]] = []
        self._stale: list[tuple[str | None, str, str]] = []
        self._init_controls()
        self._update_info()

    def _init_controls(self) -> None:
        box = gui.vBox(self.controlArea, "Optimize")
        gui.radioButtons(
            box,
            self,
            "initialization",
            [m[0] for m in INITIALIZATIONS],
            label="Initialization:",
            callback=self._invalidate,
        )
        gui.spin(
            box, self, "max_iter", 10, 10_000, step=10, label="Max iterations:",
            callback=self._invalidate, keyboardTracking=False,
        )  # fmt: skip
        self.run_button = gui.button(box, self, "Start", callback=self.toggle_run)
        self.info_label = QLabel()
        gui.vBox(self.controlArea, "Result").layout().addWidget(self.info_label)

    def _invalidate(self) -> None:
        """Options changed: stop a running optimization and wait for Start."""
        if self.task is not None:
            self.cancel()
        self.Information.modified(shown=self._has_input())

    def _params(self) -> dict[str, Any]:
        return {
            "initialization": INITIALIZATIONS[self.initialization][1],
            "max_iter": self.max_iter,
        }

    # ------------------------------------------------------------------
    # Inputs

    @Inputs.data
    def set_data(self, data: Table | None) -> None:
        """Receive the Data input."""
        self.data = data

    @Inputs.distances
    def set_distances(self, distances: CloudDistances | None) -> None:
        """Receive the Distances input."""
        self.distances = distances

    def handleNewSignals(self) -> None:
        self.cancel()
        self.values = {}
        self._clear_output()
        self.Information.modified.clear()
        self._clear_cloud_messages()
        if self.data is not None and not is_cloud_table(self.data):
            self._show_error(self.Error.cloud_error, LOCAL_DATA)
        elif self._has_input():
            self.run()
        self._update_info()

    def _has_input(self) -> bool:
        """Whether there are distances or a cloud table (and no local data)."""
        if self.data is not None and not is_cloud_table(self.data):
            return False
        return self.distances is not None or self.data is not None

    def on_cloud_connected(self) -> None:
        if self.output is None and self.task is None and self._has_input():
            self.run()

    # ------------------------------------------------------------------
    # Running

    def toggle_run(self) -> None:
        """Start, or stop a running optimization."""
        if self.task is not None:
            self.cancel()
        elif self._has_input():
            self.run()

    def run(self) -> None:
        """Embed on the machine with the current options."""
        self.Information.modified.clear()
        self.Warning.long_runtime.clear()
        size_hints().set(self, None)
        self._clear_output()
        handle = self.session.handle
        machine_id = handle.machine_id if handle is not None else None
        stale = [(kind, i) for m, kind, i in self._stale if m == machine_id]
        self._stale = []
        data, distances, params = self.data, self.distances, self._params()
        _, sizes = self.size_choices()

        def work(task: CloudTask) -> _Embedded:
            _free(task.client, stale)
            return _embed(task, data, distances, params, machine_id, sizes)

        if self.run_cloud(work, on_done=lambda result: self._embedded(result, machine_id)):
            self.run_button.setText("Stop")
        self._update_info()

    def _clear_output(self) -> None:
        """Send nothing; the replaced output is deleted at the next run."""
        self._stale += self._stored
        self._stored = []
        self.output = None
        self.Outputs.annotated_data.send(None)

    def on_partial_result(self, result: Any) -> None:
        """The number of items, before the job starts: warn if it may take long."""
        seconds = rough_runtime(result, self.max_iter)
        if seconds >= LONG_RUNTIME:
            self.Warning.long_runtime(result, _duration(seconds))

    def _embedded(self, embedded: _Embedded, machine_id: str | None) -> None:
        self.run_button.setText("Start")
        self.Warning.long_runtime.clear()
        self.values = embedded.values
        self.output = embedded.table
        self._stored = [(machine_id, kind, i) for kind, i in embedded.stored]
        self.Outputs.annotated_data.send(embedded.table)
        self._update_info()

    def cancel(self) -> None:
        super().cancel()
        if hasattr(self, "run_button"):
            self.run_button.setText("Start")
            self.Warning.long_runtime.clear()
            self._update_info()

    def on_exception(self, ex: Exception) -> None:
        self.run_button.setText("Start")
        self.Warning.long_runtime.clear()
        super().on_exception(ex)
        if isinstance(ex, DistancesTooLargeError):
            # The Cloud settings' size picker suggests a machine that fits.
            need = SizeNeed(ex.needed_bytes, ex.needed_bytes, DISK_DEFAULT_GB, "Cloud MDS")
            size_hints().set(self, need)
        self._update_info()

    def _update_info(self) -> None:
        values = self.values
        if not self._has_input():
            text = "No data."
        elif self.task is not None:
            text = "Embedding on the cloud machine…"
        elif not values:
            text = "Not embedded."
        else:
            seconds = sum(values.get("seconds", {}).values())
            text = (
                f"Embedded {values['items']:,} {values['axis']} in {seconds:.1f} s\n"
                f"({values['iterations']} iterations),\n"
                f"Kruskal stress {values['stress']:.3f}."
            )
        self.info_label.setText(text)

    def send_report(self) -> None:
        self.report_items(
            (
                ("Initialization", INITIALIZATIONS[self.initialization][0]),
                ("Max iterations", self.max_iter),
            )
        )


if __name__ == "__main__":  # pragma: no cover
    WidgetPreview(OWCloudMDS).run()
