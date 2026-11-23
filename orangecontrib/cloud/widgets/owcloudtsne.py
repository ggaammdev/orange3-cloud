"""Cloud t-SNE: a t-SNE embedding of a cloud table on all rows, on the machine.

The options are those of Orange's t-SNE widget (normalizing, PCA
preprocessing and its components, initialization, distance metric,
perplexity, "Preserve global structure", exaggeration), and the machine
embeds the way that widget does, with ``Orange.projection.TSNE`` and all its
cores (a ``tsne`` job, ``server/ops/tsne.py``). The widget doesn't draw the
map: its output, the data with "t-SNE-x" and "t-SNE-y" columns, stays on the
machine as a ``RemoteTable``; view it with Cloud Scatter Plot.

As in Orange, new data starts the embedding; changed options wait for
"Start". Before a long run the widget warns with a rough estimate of the
time it will take.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

from AnyQt.QtCore import Qt
from AnyQt.QtWidgets import QFormLayout, QLabel
from Orange.data import Table
from Orange.widgets import gui
from Orange.widgets.settings import ContextSetting, DomainContextHandler
from Orange.widgets.utils.annotated_data import ANNOTATED_DATA_SIGNAL_NAME
from Orange.widgets.utils.widgetpreview import WidgetPreview
from Orange.widgets.widget import Input, Msg, Output

from orangecontrib.cloud.client.api import CloudClient, CloudError
from orangecontrib.cloud.data.remote_table import LOCAL_DATA, RemoteTable, is_cloud_table, resolve
from orangecontrib.cloud.protocol.jobs import JobKind
from orangecontrib.cloud.widgets.base import CloudTask, OWCloudBaseWidget

__all__ = ["OWCloudTSNE", "rough_runtime"]

INITIALIZATIONS = (("PCA", "pca"), ("Spectral", "spectral"))
DISTANCE_METRICS = (("Euclidean", "l2"), ("Manhattan", "l1"), ("Cosine", "cosine"))
MAX_PCA_COMPONENTS = 50
DEFAULT_PCA_COMPONENTS = 20
#: Warn before runs that will take longer than this (seconds, rough).
LONG_RUNTIME = 60

#: Rough seconds per row on one machine of :data:`MEASURED_CPUS` CPUs at
#: perplexity 30: finding the neighbours, and the 750 iterations.
SECONDS_PER_ROW = (0.0002, 0.0012)
MEASURED_CPUS = 8


def rough_runtime(rows: int, perplexity: float, multiscale: bool, cpus: int) -> float:
    """A rough guess of the seconds t-SNE takes on the machine.

    From measurements on 8 CPUs: finding the neighbours grows with the rows
    and the perplexity, the iterations with the rows; more CPUs help, but
    less than in proportion (assumed: with the square root)."""
    neighbours, iterations = SECONDS_PER_ROW
    if multiscale:
        perplexity = min((rows - 1) / 3, 500)
    speed = (max(cpus, 1) / MEASURED_CPUS) ** 0.5
    return rows * (neighbours * max(perplexity, 1) / 30 + iterations) / speed


def _duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 2:
        return "about a minute"
    if minutes < 90:
        return f"about {minutes} minutes"
    return f"about {seconds / 3600:.0f} hours"


@dataclass
class _Embedded:
    """A finished ``tsne`` job."""

    values: dict[str, Any]
    table: RemoteTable
    machine_id: str | None


def _embed(task: CloudTask, data: Table, params: dict, machine_id: str | None) -> _Embedded:
    """Run the ``tsne`` job on ``data`` (worker thread); first hands the row
    count to the widget (for the runtime warning)."""
    client = task.client
    table_id = resolve(data, client, machine_id, wait=task.wait_for_job)
    rows = data.ref.n_rows
    task.send_partial(rows if rows is not None else client.table_info(table_id).n_rows)
    result = task.run_job(
        JobKind.TSNE, inputs={"data": table_id}, params=params, progress_range=(0.0, 0.98)
    )
    info = result.tables["table"]
    task.set_status("Fetching the domain")
    table = RemoteTable.from_info(info, client.table_domain(info.id), machine_id)
    return _Embedded(result.values, table, machine_id)


def _free(client: CloudClient, table_ids: list[str]) -> None:
    """Delete replaced tables on the machine (best effort)."""
    for table_id in table_ids:
        with contextlib.suppress(CloudError):
            client.delete_table(table_id)


class OWCloudTSNE(OWCloudBaseWidget):
    """t-SNE embedding of a cloud table on all rows, on the machine."""

    name = "Cloud t-SNE"
    description = (
        "t-SNE embedding of a cloud table on all rows, on the machine. View the "
        "result with Cloud Scatter Plot."
    )
    icon = "icons/CloudTSNE.svg"
    priority = 71
    keywords = "cloud t-sne, tsne, embedding, projection"
    resizing_enabled = False

    class Inputs:
        data = Input("Data", Table, doc="A cloud table (from Cloud File).")

    class Outputs:
        annotated_data = Output(
            ANNOTATED_DATA_SIGNAL_NAME,
            Table,
            default=True,
            doc="The data with t-SNE-x and t-SNE-y columns, a table on the cloud machine.",
        )

    class Information(OWCloudBaseWidget.Information):
        modified = Msg(
            'The parameter settings have been changed. Press "Start" to rerun with '
            "the new settings."
        )

    class Warning(OWCloudBaseWidget.Warning):
        long_runtime = Msg(
            "t-SNE of {:,} rows on {} CPUs will take {} (a rough estimate). Press Stop to cancel."
        )

    settingsHandler = DomainContextHandler()
    perplexity = ContextSetting(30)
    multiscale = ContextSetting(False)
    exaggeration = ContextSetting(1)
    initialization_method_idx = ContextSetting(0)
    distance_metric_idx = ContextSetting(0)
    normalize = ContextSetting(True)
    use_pca_preprocessing = ContextSetting(True)
    pca_components = ContextSetting(DEFAULT_PCA_COMPONENTS)

    def __init__(self):
        super().__init__()
        self.data: Table | None = None
        #: The last finished job's values (rows, timings, KL divergence).
        self.values: dict[str, Any] = {}
        self.output: RemoteTable | None = None
        #: Tables on the machine to delete at the next run: (machine ID, ID).
        self._stale: list[tuple[str | None, str]] = []
        self._init_controls()
        self._update_info()

    def _init_controls(self) -> None:
        box = gui.vBox(self.controlArea, box="Preprocessing")
        gui.checkBox(box, self, "normalize", "Normalize data", callback=self._invalidate)
        gui.checkBox(
            box,
            self,
            "use_pca_preprocessing",
            "Apply PCA preprocessing",
            callback=self._pca_changed,
        )
        self.pca_slider = gui.hSlider(
            box,
            self,
            "pca_components",
            label="PCA Components:",
            minValue=2,
            maxValue=MAX_PCA_COMPONENTS,
            step=1,
            callback=self._invalidate,
        )
        self.parameter_box = box = gui.vBox(self.controlArea, box="Parameters")
        form = QFormLayout(
            labelAlignment=Qt.AlignmentFlag.AlignLeft,
            formAlignment=Qt.AlignmentFlag.AlignLeft,
            fieldGrowthPolicy=QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow,
        )
        for label, attr, items in (
            ("Initialization:", "initialization_method_idx", INITIALIZATIONS),
            ("Distance metric:", "distance_metric_idx", DISTANCE_METRICS),
        ):
            combo = gui.comboBox(
                None, self, attr, items=[m[0] for m in items], callback=self._invalidate
            )
            form.addRow(label, combo)
        self.perplexity_spin = gui.spin(
            None, self, "perplexity", 1, 500, step=1, callback=self._invalidate, addToLayout=False
        )
        form.addRow("Perplexity:", self.perplexity_spin)
        form.addRow(
            gui.checkBox(
                None,
                self,
                "multiscale",
                label="Preserve global structure",
                callback=self._multiscale_changed,
                addToLayout=False,
            )
        )
        exaggeration = gui.hBox(None, False, addToLayout=False)
        gui.hSlider(
            exaggeration,
            self,
            "exaggeration",
            minValue=1,
            maxValue=4,
            step=0.25,
            intOnly=False,
            labelFormat="%.2f",
            callback=self._invalidate,
        )
        form.addRow("Exaggeration:", exaggeration)
        box.layout().addLayout(form)
        self.run_button = gui.button(box, self, "Start", callback=self.toggle_run)
        self.info_label = QLabel()
        gui.vBox(self.controlArea, "Result").layout().addWidget(self.info_label)
        self._enable_controls()

    def _enable_controls(self) -> None:
        self.pca_slider.parent().setEnabled(self.use_pca_preprocessing)
        self.perplexity_spin.setDisabled(self.multiscale)

    # ------------------------------------------------------------------
    # Options (as in Orange's t-SNE widget)

    def _pca_changed(self) -> None:
        self._enable_controls()
        self._invalidate()

    def _multiscale_changed(self) -> None:
        self._enable_controls()
        self._invalidate()

    def _invalidate(self) -> None:
        """Options changed: stop a running embedding and wait for Start."""
        if self.task is not None:
            self.cancel()
        self.Information.modified(shown=self.data is not None)

    def _init_attr_values(self, data: Table) -> None:
        """Defaults for new data, as Orange's t-SNE widget sets them."""
        n_attrs = len(data.domain.attributes)
        self.pca_slider.setMaximum(min(MAX_PCA_COMPONENTS, max(n_attrs, 2)))
        self.pca_components = DEFAULT_PCA_COMPONENTS
        self.exaggeration = 1
        self.normalize = True
        self.use_pca_preprocessing = n_attrs > 10
        self.distance_metric_idx = 0
        self.initialization_method_idx = 0

    def _params(self) -> dict[str, Any]:
        return {
            "perplexity": self.perplexity,
            "multiscale": self.multiscale,
            "exaggeration": self.exaggeration,
            "initialization": INITIALIZATIONS[self.initialization_method_idx][1],
            "metric": DISTANCE_METRICS[self.distance_metric_idx][1],
            "normalize": self.normalize,
            "pca": self.use_pca_preprocessing,
            "pca_components": self.pca_components,
        }

    # ------------------------------------------------------------------
    # Inputs

    @Inputs.data
    def set_data(self, data: Table | None) -> None:
        """Receive the Data input."""
        self.cancel()
        self.closeContext()
        self.data = data
        self.values = {}
        self._clear_output()
        self.Information.modified.clear()
        self._clear_cloud_messages()
        if data is not None and is_cloud_table(data):
            self._init_attr_values(data)
            self.openContext(data)
            self._enable_controls()
            self.run()
        elif data is not None:
            self._show_error(self.Error.cloud_error, LOCAL_DATA)
        self._update_info()

    def on_cloud_connected(self) -> None:
        if self.output is None and self.task is None and is_cloud_table(self.data):
            self.run()

    # ------------------------------------------------------------------
    # Running

    def toggle_run(self) -> None:
        """Start, or stop a running embedding."""
        if self.task is not None:
            self.cancel()
        elif is_cloud_table(self.data):
            self.run()

    def run(self) -> None:
        """Embed the data on the machine with the current options."""
        self.Information.modified.clear()
        self.Warning.long_runtime.clear()
        self._clear_output()
        handle = self.session.handle
        machine_id = handle.machine_id if handle is not None else None
        stale = [i for m, i in self._stale if m == machine_id]
        self._stale = []
        data, params = self.data, self._params()

        def work(task: CloudTask) -> _Embedded:
            _free(task.client, stale)
            return _embed(task, data, params, machine_id)

        if self.run_cloud(work, on_done=self._embedded):
            self.run_button.setText("Stop")
        self._update_info()

    def _clear_output(self) -> None:
        """Send nothing; the replaced table is deleted at the next run."""
        if self.output is not None:
            self._stale.append((self.output.ref.machine_id, self.output.ref.table_id))
        self.output = None
        self.Outputs.annotated_data.send(None)

    def on_partial_result(self, result: Any) -> None:
        """The row count, before the job starts: warn if it will take long."""
        info = self.session.last_info
        cpus = info.cpu_count if info is not None else 1
        seconds = rough_runtime(result, self.perplexity, self.multiscale, cpus)
        if seconds >= LONG_RUNTIME:
            self.Warning.long_runtime(result, cpus, _duration(seconds))

    def _embedded(self, embedded: _Embedded) -> None:
        self.run_button.setText("Start")
        self.Warning.long_runtime.clear()
        self.values = embedded.values
        self.output = embedded.table
        self.Outputs.annotated_data.send(embedded.table)
        self._update_info()

    def cancel(self) -> None:
        super().cancel()
        self.run_button.setText("Start")
        self.Warning.long_runtime.clear()
        self._update_info()

    def on_exception(self, ex: Exception) -> None:
        self.run_button.setText("Start")
        self.Warning.long_runtime.clear()
        super().on_exception(ex)
        self._update_info()

    def _update_info(self) -> None:
        values = self.values
        if self.data is None:
            text = "No data."
        elif self.task is not None:
            text = "Embedding on the cloud machine…"
        elif not values:
            text = "Not embedded."
        else:
            seconds = sum(values.get("seconds", {}).values())
            text = f"Embedded {values.get('rows', 0):,} rows in {seconds:.1f} s"
            kl = values.get("kl_divergence")
            text += f",\nKL divergence {kl:.3f}." if kl is not None else "."
        self.info_label.setText(text)

    def send_report(self) -> None:
        self.report_items(
            (
                ("Normalize data", self.normalize),
                ("PCA components", self.use_pca_preprocessing and self.pca_components),
                ("Initialization", INITIALIZATIONS[self.initialization_method_idx][0]),
                ("Distance metric", DISTANCE_METRICS[self.distance_metric_idx][0]),
                ("Perplexity", "global structure" if self.multiscale else self.perplexity),
                ("Exaggeration", self.exaggeration),
            )
        )


if __name__ == "__main__":  # pragma: no cover
    WidgetPreview(OWCloudTSNE).run()
