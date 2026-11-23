"""Cloud Categories: Edit Domain's "as categorical" and "merge" for a cloud table, on all rows.

Orange's Edit Domain takes a column's categories ("as categorical") and the
counts by which its "merge" dialog groups infrequent values from the data it
sees; on a cloud table that is the empty placeholder row, so "as categorical"
gives no categories and "merge" merges nothing. This widget does these two
things on all rows: the user picks a column, the machine counts its values
on all rows (the ``values`` statistics) and the widget shows the counts and
what each value becomes. Its settings go as a recipe,
:class:`~orangecontrib.cloud.protocol.recipes.ColumnCategories`, which the
machine fits on all rows (a ``derive`` job with the step ``("preprocess",
recipe)``, as Cloud Preprocess does). The output is the cloud table with that
step, which the machine replays. Edit Domain keeps everything else.

The merge modes and their texts are those of Edit Domain's dialog
(``Orange/widgets/data/oweditdomain.py``, Orange 3.41, GPL-3.0-or-later).
"""

from __future__ import annotations

from functools import partial
from typing import Any

from AnyQt.QtCore import QItemSelectionModel, Qt
from AnyQt.QtGui import QStandardItem, QStandardItemModel
from AnyQt.QtWidgets import QAbstractItemView, QHeaderView, QTableView
from Orange.data import Table, Variable
from Orange.preprocess.transformation import Lookup
from Orange.widgets import gui
from Orange.widgets.settings import ContextSetting, DomainContextHandler, Setting
from Orange.widgets.utils.itemmodels import VariableListModel
from Orange.widgets.utils.widgetpreview import WidgetPreview
from Orange.widgets.widget import Input, Msg, Output

from orangecontrib.cloud.data.remote_table import (
    LOCAL_DATA,
    is_cloud_table,
    preprocessed,
    resolve,
)
from orangecontrib.cloud.protocol.recipes import (
    MERGED_NAME,
    ColumnCategories,
    Merge,
    ValueCounts,
    category_names,
)
from orangecontrib.cloud.protocol.tables import StatsKind, StatsRequest
from orangecontrib.cloud.widgets.base import CloudTask, OWCloudBaseWidget

__all__ = ["OWCloudCategories", "changes", "fetch_values", "new_values"]


def fetch_values(
    task: CloudTask, data: Table, var: Variable, machine_id: str | None
) -> dict[str, Any]:
    """The ``values`` statistics of the column ``var`` of the cloud table
    ``data``, counted on all rows on the machine (in the worker thread)."""
    task.set_status("Counting values on all rows")
    table_id = resolve(data, task.client, machine_id, task.wait_for_job)
    index = data.domain.index(var)
    request = StatsRequest(kind=StatsKind.VALUES, columns=[index])
    return task.client.stats(table_id, request)["columns"][index]


def new_values(recipe: ColumnCategories, var: Variable, counts: ValueCounts) -> list[str | None]:
    """For each value of ``var`` (as ``counts`` lists them), the value it
    becomes with ``recipe``, or ``None`` if it stays."""
    new = recipe.variable(var, counts)
    if not isinstance(new.compute_value, Lookup):  # nothing merged
        return [None] * len(counts.values)
    values = new.compute_value.variable.values  # the categories before merging
    targets = (new.values[int(code)] for code in new.compute_value.lookup_table)
    return [
        None if target == value else target for value, target in zip(values, targets, strict=True)
    ]


def changes(recipe: ColumnCategories | None, var: Variable, counts: ValueCounts | None) -> bool:
    """Whether ``recipe`` changes the column ``var``, whose values ``counts``
    counts on all rows (without counts: whether it may)."""
    if recipe is None:
        return False
    return counts is None or recipe.variable(var, counts) is not var


def _is_supported(var: Variable) -> bool:
    """Categorical, numeric (not time) and text columns."""
    return var.is_discrete or var.is_string or (var.is_continuous and not var.is_time)


class OWCloudCategories(OWCloudBaseWidget):
    """Makes a column categorical and merges infrequent values, on all rows of a cloud table."""

    name = "Cloud Categories"
    description = (
        "Make a numeric or text column of a cloud table categorical and merge "
        "infrequent values, as Orange's Edit Domain does, with the values and "
        "counts of all rows."
    )
    icon = "icons/CloudCategories.svg"
    priority = 29
    keywords = "cloud categories, edit domain, merge, infrequent, group, categorical, counts"

    class Inputs:
        data = Input("Data", Table, doc="A cloud table (from Cloud File).")

    class Outputs:
        data = Output("Data", Table, doc="The cloud table with the column changed.")

    class Error(OWCloudBaseWidget.Error):
        too_many_values = Msg("'{}' has {:,} different values, too many for a categorical column.")

    settingsHandler = DomainContextHandler()
    column: Variable | None = ContextSetting(None, schema_only=True)
    #: Edit Domain's "as categorical", for a numeric or text column.
    as_categorical: bool = Setting(False, schema_only=True)
    merge: int = Setting(int(Merge.NONE), schema_only=True)
    fewer_than: int = Setting(10, schema_only=True)
    percent: float = Setting(10.0, schema_only=True)
    keep: int = Setting(10, schema_only=True)
    merged_name: str = Setting(MERGED_NAME, schema_only=True)
    #: The values to merge in the mode "Group selected values".
    selected_values: list[str] = Setting([], schema_only=True)
    auto_apply: bool = Setting(True)

    #: Edit Domain's texts for its merge modes, and the spin box of each.
    merge_modes = (
        (Merge.NONE, "Don't merge", None),
        (Merge.SELECTED, "Group selected values", None),
        (Merge.FEWER_THAN, "Group values with less than", ("fewer_than", "occurrences")),
        (Merge.PERCENT, "Group values with less than", ("percent", "occurrences")),
        (Merge.ALL_BUT, "Group all except", ("keep", "most frequent values")),
    )

    def __init__(self):
        super().__init__()
        self.data: Table | None = None
        self.output: Table | None = None
        #: The ``values`` statistics of the column, ``None`` until they arrive.
        self.column_stats: dict[str, Any] | None = None

        box = gui.vBox(self.controlArea, "Column")
        self.column_model = VariableListModel()
        gui.comboBox(
            box, self, "column", model=self.column_model, callback=self._column_changed,
            searchable=True,
        )  # fmt: skip
        self.as_categorical_box = gui.checkBox(
            box, self, "as_categorical", "As categorical", callback=self._settings_changed
        )
        self._init_merge_box()
        gui.rubber(self.controlArea)
        gui.auto_apply(self.buttonsArea, self, "auto_apply")

        self.values_model = QStandardItemModel(0, 3)
        self.values_model.setHorizontalHeaderLabels(["Value", "Rows", "Becomes"])
        self.values_view = view = QTableView(
            selectionBehavior=QAbstractItemView.SelectionBehavior.SelectRows,
            selectionMode=QAbstractItemView.SelectionMode.ExtendedSelection,
            editTriggers=QAbstractItemView.EditTrigger.NoEditTriggers,
        )
        view.setModel(self.values_model)
        view.verticalHeader().hide()
        view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        view.horizontalHeader().setStretchLastSection(True)
        view.selectionModel().selectionChanged.connect(self._selection_changed)
        self.mainArea.layout().addWidget(view)
        self.summary = gui.widgetLabel(self.mainArea, "")
        self.summary.setWordWrap(True)
        self._update_controls()

    def _init_merge_box(self) -> None:
        box = gui.radioButtons(
            self.controlArea, self, "merge", box="Merge", callback=self._settings_changed
        )
        self.merge_buttons = {}
        for mode, text, spin in self.merge_modes:
            row = gui.hBox(box)
            self.merge_buttons[mode] = gui.appendRadioButton(box, text, insertInto=row)
            if spin is not None:
                name, unit = spin
                if name == "percent":
                    gui.doubleSpin(
                        row, self, name, 0, 100, 0.1, decimals=1, suffix=" %",
                        callback=partial(self._spin_changed, mode),
                    )  # fmt: skip
                else:
                    gui.spin(
                        row, self, name, 0 if name == "keep" else 1, 10**9,
                        callback=partial(self._spin_changed, mode),
                    )  # fmt: skip
                gui.widgetLabel(row, unit)
            gui.rubber(row)
        gui.lineEdit(
            box, self, "merged_name", "New value name:", orientation=Qt.Orientation.Horizontal,
            callback=self._settings_changed, placeholderText=MERGED_NAME,
        )  # fmt: skip

    # ------------------------------------------------------------------
    # The settings and the list of values

    def recipe(self) -> ColumnCategories | None:
        """The recipe for the widget's settings; ``None`` when there is
        nothing to do (no column, or a numeric or text column that stays)."""
        if not self._categorical():
            return None
        amounts = {Merge.FEWER_THAN: self.fewer_than, Merge.PERCENT: self.percent}
        return ColumnCategories(
            self.column.name,
            self.merge,
            amounts.get(Merge(self.merge), self.keep),
            self.selected_values,
            self.merged_name,
        )

    def _categorical(self) -> bool:
        """Whether the column is (or becomes) categorical."""
        var = self.column
        return var is not None and (var.is_discrete or self.as_categorical)

    def counts(self) -> ValueCounts | None:
        """The counts of the column's values on all rows, if known and listed."""
        return None if self.column_stats is None else ValueCounts.from_stats(self.column_stats)

    def _update_controls(self) -> None:
        var = self.column
        self.as_categorical_box.setEnabled(var is not None and not var.is_discrete)
        categorical = self._categorical()
        for mode, button in self.merge_buttons.items():
            listed = mode != Merge.SELECTED or self.counts() is not None
            button.setEnabled(categorical and listed)

    def _value_names(self, counts: ValueCounts) -> tuple[str, ...]:
        var = self.column
        if var.is_discrete:
            return tuple(var.values)
        if self.as_categorical:
            return category_names(var, counts.values)
        return tuple(var.str_val(value) for value in counts.values)

    def _show_values(self) -> None:
        """Fill the list of values: each value's rows and what it becomes."""
        selection = self.values_view.selectionModel()
        selection.blockSignals(True)  # keep the selected values
        try:
            self._fill_values()
        finally:
            selection.blockSignals(False)
        self.summary.setText(self._summary())

    def _fill_values(self) -> None:
        model, var, counts = self.values_model, self.column, self.counts()
        model.removeRows(0, model.rowCount())
        if var is None or counts is None:
            return
        names = self._value_names(counts)
        recipe = self.recipe()
        targets = new_values(recipe, var, counts) if recipe is not None else ()
        selected = set(self.selected_values)
        for row, (name, n) in enumerate(zip(names, counts.counts, strict=True)):
            number = QStandardItem(f"{int(n):,}")
            number.setData(int(n), Qt.ItemDataRole.UserRole)
            number.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            target = targets[row] if targets else None
            model.appendRow([QStandardItem(name), number, QStandardItem(target or "")])
            if name in selected:
                self.values_view.selectionModel().select(
                    model.index(row, 0),
                    QItemSelectionModel.SelectionFlag.Select
                    | QItemSelectionModel.SelectionFlag.Rows,
                )
        self.values_view.viewport().update()

    def _summary(self) -> str:
        var, stats = self.column, self.column_stats
        if var is None or stats is None:
            return ""
        values = stats["n_values"]
        kind = "values" if var.is_discrete else "different values"
        text = f"'{var.name}' has {values:,} {kind}"
        if stats["missing"]:
            text += f", and {stats['missing']:,} rows where it is missing"
        if stats.get("values") is None:
            return text + ": too many to list here."
        recipe = self.recipe()
        if recipe is not None:
            new = recipe.variable(var, self.counts())
            text += f". The output has {len(new.values):,} categories"
        return text + "."

    def _selection_changed(self, *_: Any) -> None:
        rows = {index.row() for index in self.values_view.selectionModel().selectedRows()}
        model = self.values_model
        self.selected_values = [model.item(row, 0).text() for row in sorted(rows)]
        if self.merge == Merge.SELECTED:
            self._settings_changed()

    def _spin_changed(self, mode: Merge) -> None:
        self.merge = int(mode)
        self._settings_changed()

    def _settings_changed(self) -> None:
        self._update_controls()
        self._show_values()
        self.commit.deferred()

    def _column_changed(self) -> None:
        self.column_stats = None
        self.selected_values = []
        self._update_controls()
        self._show_values()
        self._refresh()

    def _refresh(self) -> None:
        """Count the values (and apply, if automatic) after a change of input or column."""
        if self.auto_apply:
            self.commit.now()
        else:
            self._start(apply=False)
            self.commit.deferred()

    # ------------------------------------------------------------------
    # Input, machine and output

    @Inputs.data
    def set_data(self, data: Table | None) -> None:
        """Receive the Data input."""
        self.cancel()
        self.closeContext()
        self.data = data
        self.column_stats = None
        domain = data.domain if data is not None else None
        variables = [] if domain is None else domain.variables + domain.metas
        self.column_model[:] = columns = [var for var in variables if _is_supported(var)]
        categorical = [var for var in columns if var.is_discrete]
        self.column = (categorical or columns or [None])[0]
        self.openContext(domain)
        self._send(None)
        self._update_controls()
        self._show_values()
        self._refresh()

    def on_cloud_connected(self) -> None:
        if self.output is None and self.task is None and self.data is not None:
            self._refresh()

    def _send(self, table: Table | None) -> None:
        self.output = table
        self.Outputs.data.send(table)

    @gui.deferred
    def commit(self) -> None:
        """Change the column on all rows on the machine and send the cloud table."""
        self._start(apply=True)

    def _start(self, apply: bool) -> None:
        """Count the column's values on the machine if they aren't known yet,
        and with ``apply``, fit the recipe on all rows and send the result."""
        self.cancel()
        self._clear_cloud_messages()
        self.Error.too_many_values.clear()
        data, var, stats, recipe = self.data, self.column, self.column_stats, self.recipe()
        if data is None or var is None:
            self._send(data)
            return
        if not is_cloud_table(data):
            self._send(None)
            self._show_error(self.Error.cloud_error, LOCAL_DATA)
            return
        if stats is not None and (not apply or not changes(recipe, var, self.counts())):
            if apply and self._check_listed():
                self._send(data)
            return
        handle = self.session.handle
        machine_id = handle.machine_id if handle is not None else None

        def work(task: CloudTask) -> tuple[dict[str, Any], Table | None]:
            found = stats if stats is not None else fetch_values(task, data, var, machine_id)
            counts = ValueCounts.from_stats(found)
            if not apply or counts is None or not changes(recipe, var, counts):
                return found, data if apply else None
            task.set_status("Changing the column on all rows")
            return found, preprocessed(data, recipe, task.client, machine_id, task.wait_for_job)

        self.run_cloud(work, on_done=partial(self._done, apply))

    def _done(self, apply: bool, result: tuple[dict[str, Any], Table | None]) -> None:
        self.column_stats, output = result
        self._update_controls()
        self._show_values()
        if apply and self._check_listed():
            self._send(output)

    def _check_listed(self) -> bool:
        """Show an error, and send nothing, when the column has too many
        values to become categorical; whether it hasn't."""
        stats = self.column_stats
        if self.recipe() is None or stats is None or stats.get("values") is not None:
            return True
        self._send(None)
        self.Error.too_many_values(self.column.name, stats["n_values"])
        return False

    def send_report(self) -> None:
        """Report the column, the settings and the summary."""
        if self.column is None:
            return
        items = [("Column", self.column.name)]
        if not self.column.is_discrete:
            items.append(("As categorical", "yes" if self.as_categorical else "no"))
        if self._categorical():
            mode, text, spin = self.merge_modes[self.merge]
            if spin is not None:
                text = f"{text} {getattr(self, spin[0])} {spin[1]}"
            items.append(("Merge", text))
            if self.merge != Merge.NONE:
                items.append(("New value name", self.merged_name or MERGED_NAME))
        self.report_items("Settings", items)
        if self.summary.text():
            self.report_paragraph("Values", self.summary.text())


if __name__ == "__main__":  # pragma: no cover
    WidgetPreview(OWCloudCategories).run()
