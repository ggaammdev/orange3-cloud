"""Cloud Preprocess: fit Orange's Preprocess recipe on all rows of a cloud table.

Steps that learn their settings from the data (normalizing, imputing,
discretizing, PCA, feature selection, ...) can't run in Orange's own widgets
on a cloud table: those see only the add-on's placeholder row. This widget
takes the recipe from Orange's Preprocess widget (its "Preprocessor" output)
and has the machine fit it on all rows with Orange's own code. The output is
a cloud table with the fitted columns and ``("preprocess", recipe)`` added to
its steps (:func:`~orangecontrib.cloud.data.remote_table.preprocessed`).
"""

from __future__ import annotations

from typing import Any

from AnyQt.QtWidgets import QLabel
from Orange.data import Table
from Orange.preprocess.preprocess import Preprocess
from Orange.widgets import gui
from Orange.widgets.settings import Setting
from Orange.widgets.utils.widgetpreview import WidgetPreview
from Orange.widgets.widget import Input, Output

from orangecontrib.cloud.data.remote_table import (
    LOCAL_DATA,
    RemoteTable,
    is_cloud_table,
    preprocessed,
)
from orangecontrib.cloud.widgets.base import CloudTask, OWCloudBaseWidget

__all__ = ["HELP", "OWCloudPreprocess", "recipe_names"]

HELP = (
    "Steps that learn from the data (normalize, impute, discretize, PCA,\n"
    "feature selection, ...) must see all rows. Build the recipe in Orange's\n"
    "Preprocess widget, without data, and connect its Preprocessor output\n"
    "to this widget's Preprocessor input. The cloud machine fits it on all\n"
    "rows of the cloud table."
)


def recipe_names(recipe: Preprocess) -> str:
    """The steps of a Preprocess recipe, e.g. "Impute, Normalize, Discretize"."""
    items = getattr(recipe, "preprocessors", None)
    items = [recipe] if items is None else items
    names = [type(item).__name__.lstrip("_") for item in items if item is not None]
    return ", ".join(names) or "nothing"


class OWCloudPreprocess(OWCloudBaseWidget):
    """Fits Orange's Preprocess recipe on all rows of a cloud table."""

    name = "Cloud Preprocess"
    description = (
        "Fit a recipe from Orange's Preprocess widget (normalize, impute, "
        "discretize, PCA, ...) on all rows of a cloud table, on the machine."
    )
    icon = "icons/CloudPreprocess.svg"
    priority = 25
    keywords = "cloud preprocess, normalize, impute, discretize, pca, feature selection"
    want_main_area = False
    resizing_enabled = False

    class Inputs:
        data = Input("Data", Table, doc="A cloud table (from Cloud File).")
        preprocessor = Input(
            "Preprocessor",
            Preprocess,
            doc="The recipe from Orange's Preprocess widget (its Preprocessor output).",
        )

    class Outputs:
        preprocessed_data = Output(
            "Preprocessed Data", Table, doc="The cloud table with the recipe fitted on all rows."
        )

    auto_apply: bool = Setting(True)

    def __init__(self):
        super().__init__()
        self.data: Table | None = None
        self.recipe: Preprocess | None = None
        #: The last output (``None`` until fitted).
        self.output: RemoteTable | None = None
        self._running = False

        box = gui.vBox(self.controlArea, "Preprocessing")
        self.info_label = QLabel()
        box.layout().addWidget(self.info_label)
        help_label = QLabel(HELP)
        help_label.setStyleSheet("color: gray")
        box.layout().addWidget(help_label)
        gui.auto_apply(self.buttonsArea, self, "auto_apply", commit=self.commit)
        self._update_info()

    # ------------------------------------------------------------------
    # Inputs

    @Inputs.data
    def set_data(self, data: Table | None) -> None:
        """Receive the Data input."""
        self.data = data

    @Inputs.preprocessor
    def set_preprocessor(self, recipe: Preprocess | None) -> None:
        """Receive the Preprocessor input."""
        self.recipe = recipe

    def handleNewSignals(self) -> None:
        self.cancel()
        self._send(None)
        self.commit.deferred()

    def on_cloud_connected(self) -> None:
        if self.output is None and self.task is None and self.data is not None:
            self.commit.now()

    # ------------------------------------------------------------------

    def _send(self, table: Table | None) -> None:
        self.output = table if isinstance(table, RemoteTable) else None
        self.Outputs.preprocessed_data.send(table)
        self._update_info()

    @gui.deferred
    def commit(self) -> None:
        """Fit the recipe on all rows on the machine (a ``derive`` job)."""
        self.cancel()
        self._clear_cloud_messages()
        data, recipe = self.data, self.recipe
        if data is None:
            self._send(None)
            return
        if not is_cloud_table(data):
            self._send(None)
            self._show_error(self.Error.cloud_error, LOCAL_DATA)
            return
        if recipe is None:  # nothing to fit: the data passes through
            self._send(data)
            return
        handle = self.session.handle
        machine_id = handle.machine_id if handle is not None else None

        def work(task: CloudTask) -> RemoteTable:
            task.set_status("Fitting the recipe on all rows")
            return preprocessed(data, recipe, task.client, machine_id, task.wait_for_job)

        self._running = self.run_cloud(work, on_done=self._send)
        self._update_info()

    # ------------------------------------------------------------------

    def cancel(self) -> None:
        super().cancel()
        self._running = False

    def on_done(self, outcome: Any) -> None:
        self._running = False
        super().on_done(outcome)

    def on_exception(self, ex: Exception) -> None:
        self._running = False
        super().on_exception(ex)
        self._update_info()

    def _update_info(self) -> None:
        data, recipe, output = self.data, self.recipe, self.output
        if data is None:
            text = "No data."
        elif recipe is None:
            text = "No recipe: the data passes through unchanged."
        elif self._running:
            text = f"Fitting {recipe_names(recipe)} on the cloud machine…"
        elif output is None:
            text = f"Recipe: {recipe_names(recipe)}.\nNot fitted."
        else:
            rows = output.ref.n_rows
            rows_text = f"{rows:,} rows, " if rows is not None else ""
            before = len(data.domain.attributes)
            after = len(output.domain.attributes)
            text = (
                f"Fitted {recipe_names(recipe)} on all rows.\n"
                f"{rows_text}{before} → {after} features."
            )
        self.info_label.setText(text)


if __name__ == "__main__":  # pragma: no cover
    WidgetPreview(OWCloudPreprocess).run()
