"""Cloud Learner: train any Orange learner on a cloud table, on the machine.

The learner (from any of Orange's learner widgets, with its preprocessors)
is pickled and sent with the table's ID as a ``fit`` job; the model stays on
the machine and goes out as a :class:`CloudModel`. "Download model" brings
the real Orange model to this computer, on the second output, after showing
its size (and asking first when it's large: some models, such as
k-nearest neighbours or trees, hold the training data).
"""

from __future__ import annotations

import contextlib
from typing import Any

from AnyQt.QtWidgets import QLabel, QMessageBox
from Orange.base import Learner, Model
from Orange.data import Table
from Orange.widgets import gui
from Orange.widgets.settings import Setting
from Orange.widgets.utils.widgetpreview import WidgetPreview
from Orange.widgets.widget import Input, Msg, Output
from orangewidget.utils.signals import PartialSummary, summarize

from orangecontrib.cloud.client.api import CloudError
from orangecontrib.cloud.client.providers.base import MachineHandle
from orangecontrib.cloud.data.cloud_model import CloudModel
from orangecontrib.cloud.data.estimate import format_bytes
from orangecontrib.cloud.data.remote_table import LOCAL_DATA, is_cloud_table, resolve
from orangecontrib.cloud.protocol.jobs import JobKind
from orangecontrib.cloud.protocol.machine import InfoResponse
from orangecontrib.cloud.widgets.base import CloudTask, OWCloudBaseWidget, machine_label

__all__ = ["CONFIRM_DOWNLOAD_BYTES", "OWCloudLearner"]

#: Downloads larger than this ask the user first.
CONFIRM_DOWNLOAD_BYTES = 100 * 1024**2


@summarize.register
def summarize_cloud_model(model: CloudModel) -> PartialSummary:
    """The summary of a cloud model in the canvas's link and output info."""
    return PartialSummary(model.name, str(model))


class OWCloudLearner(OWCloudBaseWidget):
    """Train any Orange learner on a cloud table, on the machine."""

    name = "Cloud Learner"
    description = (
        "Train any Orange learner on a cloud table, on the machine. The model "
        "stays in the cloud; it can be downloaded."
    )
    icon = "icons/CloudLearner.svg"
    priority = 30
    keywords = "cloud learner, train, fit, model, download model"
    want_main_area = False
    resizing_enabled = False

    class Inputs:
        data = Input("Data", Table, doc="A cloud table (from Cloud File).")
        learner = Input("Learner", Learner, doc="Any Orange learner, e.g. Random Forest.")

    class Outputs:
        model = Output("Model", CloudModel, doc="The model, kept on the cloud machine.")
        local_model = Output(
            "Model (local)", Model, doc="The model on this computer, after Download model."
        )

    class Error(OWCloudBaseWidget.Error):
        incompatible = Msg("{}")

    auto_apply: bool = Setting(True)

    _running = ""  # "fit" or "download" while a task runs

    def __init__(self):
        super().__init__()
        self.data: Table | None = None
        self.learner: Learner | None = None
        #: The trained model on the machine, and its downloaded copy.
        self.model: CloudModel | None = None
        self.local_model: Model | None = None
        #: Replaced models, deleted on the machine at the next training.
        self._stale: list[CloudModel] = []

        box = gui.vBox(self.controlArea, "Model")
        self.info_label = QLabel()
        box.layout().addWidget(self.info_label)
        self.download_button = gui.button(
            box, self, "Download model", callback=self.download, autoDefault=False
        )
        gui.auto_apply(self.buttonsArea, self, "auto_apply", commit=self.commit)
        self._update_info()

    # ------------------------------------------------------------------
    # Inputs

    @Inputs.data
    def set_data(self, data: Table | None) -> None:
        """Receive the Data input."""
        self.data = data

    @Inputs.learner
    def set_learner(self, learner: Learner | None) -> None:
        """Receive the Learner input."""
        self.learner = learner

    def handleNewSignals(self) -> None:
        self.cancel()
        self._clear_model()
        self._update_info()
        self.commit.deferred()

    def on_cloud_connected(self) -> None:
        if self.model is None and self.task is None and self._ready():
            self.commit.now()

    # ------------------------------------------------------------------
    # Training

    def _ready(self) -> bool:
        return self.data is not None and self.learner is not None

    def _clear_model(self) -> None:
        if self.model is not None:
            self._stale.append(self.model)
        self.model = self.local_model = None
        self.Outputs.model.send(None)
        self.Outputs.local_model.send(None)

    def _check(self) -> bool:
        """Show why the inputs can't be trained on, without the machine."""
        self.Error.incompatible.clear()
        if self.data is None or self.learner is None:
            return False
        if not is_cloud_table(self.data):
            self._show_error(self.Error.cloud_error, LOCAL_DATA)
            return False
        check = getattr(self.learner, "incompatibility_reason", None)
        reason = check(self.data.domain) if check is not None else None
        if reason:
            self.Error.incompatible(f"{self.learner.name}: {reason}")
            return False
        return True

    @gui.deferred
    def commit(self) -> None:
        """Train the learner on the cloud table (a ``fit`` job)."""
        self.cancel()
        self._clear_cloud_messages()
        self._clear_model()
        self._update_info()
        if not self._check():
            return
        data, learner = self.data, self.learner
        handle = self.session.handle
        machine_id = handle.machine_id if handle is not None else None
        stale, self._stale = self._stale, []

        def work(task: CloudTask) -> CloudModel:
            for old in stale:  # free the replaced models' memory (best effort)
                if old.machine_id == machine_id:
                    with contextlib.suppress(CloudError):
                        task.client.delete_object(old.object_id)
            table_id = resolve(data, task.client, machine_id)
            result = task.run_job(
                JobKind.FIT, inputs={"data": table_id}, objects={"learner": learner}
            )
            info, values = result.objects["model"], result.values
            return CloudModel(
                object_id=info.id,
                name=str(values.get("learner") or learner.name),
                domain=data.domain,
                learner_name=type(learner).__name__,
                model_type=info.type,
                table_id=table_id,
                n_rows=int(values.get("rows", 0)),
                machine_id=machine_id,
                size_bytes=info.size_bytes,
                training_seconds=values.get("training_seconds"),
            )

        if self.run_cloud(work, on_done=self._trained):
            self._running = "fit"  # after run_cloud, whose cancel() resets it
        else:
            self._stale = stale + self._stale
        self._update_info()

    def _trained(self, model: CloudModel) -> None:
        self._running = ""
        self.model = model
        self.Outputs.model.send(model)
        self._update_info()

    # ------------------------------------------------------------------
    # Download

    def confirm_download(self, size_bytes: int) -> bool:
        """Ask before downloading a large model."""
        answer = QMessageBox.question(
            self,
            "Download model",
            f"The model is {format_bytes(size_bytes)}. Some models, such as "
            "k-nearest neighbours and trees, contain the training data.\n\n"
            "Download it to this computer?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def download(self) -> bool:
        """Download the model to this computer and send it to "Model (local)".
        Asks first when it's larger than :data:`CONFIRM_DOWNLOAD_BYTES`."""
        model = self.model
        if model is None or self.task is not None:
            return False
        size = model.size_bytes
        if size is not None and size > CONFIRM_DOWNLOAD_BYTES and not self.confirm_download(size):
            return False
        handle = self.session.handle
        machine_id = handle.machine_id if handle is not None else None
        size_text = f" ({format_bytes(size)})" if size is not None else ""

        def work(task: CloudTask) -> Any:
            task.set_status(f"Downloading the model{size_text}")
            return model.download(task.client, machine_id)

        started = self.run_cloud(work, on_done=lambda local: self._downloaded(model, local))
        if started:
            self._running = "download"
        self._update_info()
        return started

    def _downloaded(self, model: CloudModel, local: Model) -> None:
        self._running = ""
        if model is not self.model:  # trained again meanwhile
            return
        self.local_model = local
        self.Outputs.local_model.send(local)
        self._update_info()

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
        model = self.model
        if self.data is None or self.learner is None:
            missing = [n for n, v in (("data", self.data), ("learner", self.learner)) if v is None]
            text = "No " + " and no ".join(missing) + "."
        elif self._running == "fit":
            text = "Training on the cloud machine…"
        elif model is None:
            text = "Not trained."
        else:
            seconds = model.training_seconds
            took = f" in {seconds:.1f} s" if seconds is not None else ""
            text = f"Trained {model.name} on {model.n_rows:,} rows{took}."
            if model.size_bytes is not None:
                text += f"\nModel size: {format_bytes(model.size_bytes)}."
            if self._running == "download":
                text += "\nDownloading…"
            elif self.local_model is not None:
                text += "\nDownloaded to this computer (Model (local))."
        self.info_label.setText(text)
        self.download_button.setEnabled(model is not None and not self._running)


if __name__ == "__main__":  # pragma: no cover
    from Orange.classification import TreeLearner

    WidgetPreview(OWCloudLearner).run(set_learner=TreeLearner())
