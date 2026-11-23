"""Cloud Discretize: Orange's Discretize for a cloud table, with cut points found on all rows.

The widget is Orange's Discretize: the same list of numeric and time columns
(features, target and metas), the same methods (equal frequency, equal
width, entropy-MDL, natural binning, fixed width, time interval, custom cut
points, keep, remove), a general preset and settings per column, built from
the parts of Orange's widget module. Its code follows Orange's widget
(``Orange/widgets/data/owdiscretize.py``, Orange 3.41, GPL-3.0-or-later),
which can't be subclassed.

Orange's widget finds the cut points on the data it sees; on a cloud table
that is the empty placeholder row. This widget sends its settings as a
recipe, :class:`~orangecontrib.cloud.protocol.recipes.DiscretizeColumns`,
and the machine finds the cut points on all rows with Orange's own
discretizers (a ``derive`` job with the step ``("preprocess", recipe)``, as
Cloud Preprocess does). The list then shows the cut points from the fitted
columns. The output is the cloud table with that step, which the machine
replays.
"""

from __future__ import annotations

from functools import cache
from typing import Any

from AnyQt.QtCore import Qt
from AnyQt.QtGui import QDoubleValidator, QIntValidator, QPalette, QValidator
from AnyQt.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QListView,
    QRadioButton,
    QSizePolicy,
    QSpinBox,
)
from Orange.data import ContinuousVariable, DiscreteVariable, Domain, Table, TimeVariable
from Orange.widgets import gui
from Orange.widgets.data.owdiscretize import (
    DefaultHint,
    DefaultKey,
    DiscDesc,
    DiscDomainModel,
    IncreasingNumbersListValidator,
    ListViewSearch,
    Methods,
    Options,
    VarHint,
    format_desc,
    time_units,
    variable_key,
)
from Orange.widgets.data.oweditdomain import FixedSizeButton
from Orange.widgets.settings import Setting
from Orange.widgets.utils import unique_everseen
from Orange.widgets.utils.widgetpreview import WidgetPreview
from Orange.widgets.widget import Input, Output

from orangecontrib.cloud.data.remote_table import (
    LOCAL_DATA,
    RemoteTable,
    is_cloud_table,
    preprocessed,
)
from orangecontrib.cloud.protocol.recipes import DiscretizeColumns, known_outcome
from orangecontrib.cloud.widgets.base import CloudTask, OWCloudBaseWidget

__all__ = ["OWCloudDiscretize", "fitted_notes", "make_recipe"]

#: The list's note for a column whose cut points the machine hasn't found yet.
PENDING = " …"


def make_recipe(var_hints: dict[Any, VarHint], domain: Domain) -> DiscretizeColumns:
    """The recipe for the widget's settings, with the settings of ``domain``'s
    columns only (settings are kept for columns of other data, too)."""
    default = var_hints.get(DefaultKey, DefaultHint)
    keys = {variable_key(var) for var in domain.variables + domain.metas}
    hints = {
        key: (int(hint.method_id), tuple(hint.args))
        for key, hint in var_hints.items()
        if key in keys
    }
    return DiscretizeColumns((int(default.method_id), tuple(default.args)), hints)


@cache
def _points_in_parentheses() -> bool:
    """Whether Orange's list of columns shows cut points as "x: method (1, 2)"
    (newer Orange) rather than "x (method): 1, 2": its model puts a space
    before the points in the first form and nothing in the second."""
    model = DiscDomainModel(valid_types=(ContinuousVariable,))
    model.set_domain(Domain([ContinuousVariable("x")]))
    model.setData(model.index(0), DiscDesc(None, "P", ()), Qt.ItemDataRole.UserRole)
    return str(model.data(model.index(0))).endswith(" P")


def points_note(var: ContinuousVariable, points: Any) -> str:
    """The cut points of ``var`` as Orange's list of columns shows them."""
    text = ", ".join(map(var.repr_val, points))
    return f"({text})" if _points_in_parentheses() else f": {text}"


def fitted_notes(
    recipe: DiscretizeColumns, before: Domain, after: Domain
) -> dict[Any, tuple[str, Any]]:
    """For each numeric column of ``before``, the list's note (cut points, or
    why it was kept or removed, as in Orange's widget) and the column the
    output has for it (``None``: removed), from ``after``, the domain the
    machine fitted with ``recipe``."""
    found = {var.name: var for var in after.variables + after.metas}
    notes: dict[Any, tuple[str, Any]] = {}
    for var in before.variables + before.metas:
        if not isinstance(var, ContinuousVariable):
            continue
        known = known_outcome(var, recipe.hint(var), before)
        dvar = found.get(var.name)
        if known is not None:
            notes[variable_key(var)] = (known[1], known[0])
        elif isinstance(dvar, DiscreteVariable):
            notes[variable_key(var)] = (points_note(var, dvar.compute_value.points), dvar)
        else:  # no cut points on all rows, or too many intervals
            notes[variable_key(var)] = (" <removed>", None)
    return notes


class OWCloudDiscretize(OWCloudBaseWidget):
    """Discretizes the numeric columns of a cloud table, with cut points found on all rows."""

    name = "Cloud Discretize"
    description = (
        "Discretize the numeric columns of a cloud table, as Orange's Discretize "
        "does, with cut points found on all rows on the machine."
    )
    icon = "icons/CloudDiscretize.svg"
    priority = 27
    keywords = "cloud discretize, bin, categorical, nominal, ordinal"
    want_main_area = False

    class Inputs:
        data = Input("Data", Table, doc="A cloud table (from Cloud File).")

    class Outputs:
        data = Output("Data", Table, doc="The cloud table with categorical features.")

    #: As Orange's Discretize: the general preset (key ``None``) and the
    #: settings of single columns, by name and whether it is a time column.
    var_hints: dict[Any, VarHint] = Setting({DefaultKey: DefaultHint}, schema_only=True)
    autosend: bool = Setting(True)

    def __init__(self):
        super().__init__()
        self.data: Table | None = None
        #: Per column key: the output's column (``None``: removed), once fitted.
        self.discretized_vars: dict[Any, Any] = {}
        #: The last recipe the machine fitted, and the domain it gave.
        self.fitted: tuple[DiscretizeColumns, Domain] | None = None
        self.output: Table | None = None
        self._interface_update = False

        box = gui.hBox(self.controlArea, True, spacing=8)
        self._create_var_list(box)
        self._create_buttons(box)
        gui.auto_apply(self.buttonsArea, self, "autosend")
        gui.rubber(self.buttonsArea)
        self.varview.select_default()

    # ------------------------------------------------------------------
    # Interface (as in Orange's Discretize)

    def _create_var_list(self, box: Any) -> None:
        self.varview = ListViewSearch(
            selectionMode=QListView.SelectionMode.ExtendedSelection, uniformItemSizes=True
        )
        self.varview.setModel(
            DiscDomainModel(
                valid_types=(ContinuousVariable, TimeVariable), order=DiscDomainModel.MIXED
            )
        )
        self.varview.selectionModel().selectionChanged.connect(self._var_selection_changed)
        self.varview.default_view.selectionModel().selectionChanged.connect(self._default_selected)
        self._update_default_model()
        box.layout().addWidget(self.varview)

    def _int_spin(self) -> tuple[QSpinBox, Any]:
        spin = QSpinBox(self, minimum=2, maximum=10)
        spin.setFixedWidth(60)
        spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        return spin, spin.valueChanged

    def _width_line(self, validator: QValidator) -> tuple[QLineEdit, Any]:
        line = QLineEdit(self)
        line.setFixedWidth(60)
        line.setAlignment(Qt.AlignmentFlag.AlignRight)
        line.setValidator(validator)
        return line, line.textChanged

    def _cut_points_line(self) -> tuple[QLineEdit, Any]:
        edit = QLineEdit(
            placeholderText="e.g. 0.0, 0.5, 1.0",
            toolTip='<p style="white-space:pre">Enter cut points as a comma-separate list of \n'
            "strictly increasing numbers e.g. 0.0, 0.5, 1.0).</p>",
        )
        edit.setValidator(IncreasingNumbersListValidator())
        edit.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Fixed)
        edit.textChanged.connect(lambda: self._show_cut_points_state(edit))
        return edit, edit.textChanged

    @staticmethod
    def _show_cut_points_state(edit: QLineEdit) -> None:
        """Colour the custom cut points as Orange's widget does while typing."""
        validator = edit.validator()
        state = QValidator.State.Acceptable
        if validator is not None and edit.text().strip():
            state = validator.validate(edit.text(), 0)[0]
        colors = {
            QValidator.State.Intermediate: (Qt.GlobalColor.yellow, Qt.GlobalColor.black),
            QValidator.State.Invalid: (Qt.GlobalColor.red, Qt.GlobalColor.black),
        }.get(state)
        palette = QPalette()
        if colors is not None:
            palette = edit.palette()
            palette.setColor(QPalette.ColorRole.Base, colors[0])
            palette.setColor(QPalette.ColorRole.Text, colors[1])
        edit.setPalette(palette)
        point = edit.mapToGlobal(edit.cursorRect().bottomRight())
        tip = edit.toolTip() if state != QValidator.State.Acceptable and edit.isVisible() else ""
        if validator is not None:
            validator.show_tip(edit, point, tip, textFormat=Qt.TextFormat.RichText)

    def _add_button(self, box: Any, method: Methods, *controls: Any, stretch: bool = True) -> Any:
        """A radio button for ``method`` with its ``(control, signal)`` pairs;
        returns the first control."""
        layout = QHBoxLayout()
        desc = Options[method]
        button = QRadioButton(desc.label)
        button.setToolTip(desc.tooltip)
        self.button_group.addButton(button, method)
        layout.addWidget(button)
        if controls and stretch:
            layout.addStretch(1)
        for control, signal in controls:
            layout.addWidget(control)
            if signal is not None:
                signal.connect(lambda *_, m=method: self._arg_changed(m))
        self._button_rows.append(layout)
        box.layout().addLayout(layout)
        return controls[0][0] if controls else None

    def _arg_changed(self, method: Methods) -> None:
        self.button_group.button(method).setChecked(True)
        self.update_hints(method)

    def _create_buttons(self, box: Any) -> None:
        button_box = gui.vBox(box)
        button_box.layout().setSpacing(0)
        button_box.setSizePolicy(
            QSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        )
        self.button_group = QButtonGroup(self)
        self.button_group.idClicked.connect(self.update_hints)
        self._button_rows: list[QHBoxLayout] = []
        add = self._add_button

        add(button_box, Methods.Default)
        add(button_box, Methods.Keep)
        add(button_box, Methods.Remove)
        self.binning_spin = add(button_box, Methods.Binning, self._int_spin())
        positive = QDoubleValidator()
        positive.setBottom(0)
        self.width_line = add(button_box, Methods.FixedWidth, self._width_line(positive))
        self.width_time_unit = unit = QComboBox(self)
        unit.addItems([f"{name}(s)" for name in time_units])
        whole = QIntValidator()
        whole.setBottom(1)
        self.width_time_line = add(
            button_box,
            Methods.FixedWidthTime,
            self._width_line(whole),
            (unit, unit.currentTextChanged),
        )
        self.freq_spin = add(button_box, Methods.EqualFreq, self._int_spin())
        self.width_spin = add(button_box, Methods.EqualWidth, self._int_spin())
        add(button_box, Methods.MDL)
        self.copy_to_custom = FixedSizeButton(
            text="CC", toolTip="Copy the current cut points to manual mode"
        )
        self.copy_to_custom.clicked.connect(self._copy_to_manual)
        self.threshold_line = add(
            button_box,
            Methods.Custom,
            self._cut_points_line(),
            (self.copy_to_custom, None),
            stretch=False,
        )
        heights = [row.sizeHint().height() for row in self._button_rows]
        middle = (min(heights) + max(heights)) // 2
        for row, height in zip(self._button_rows, heights, strict=True):
            row.itemAt(0).widget().setFixedHeight(max(height, middle))
        button_box.layout().addStretch(1)

    def _update_default_model(self) -> None:
        model = self.varview.default_view.model()
        model.setData(model.index(0), self.var_hints[DefaultKey], Qt.ItemDataRole.UserRole)

    def _set_mdl_button(self) -> None:
        """Entropy-MDL needs a categorical target."""
        mdl_button = self.button_group.button(Methods.MDL)
        if self.data is None or self.data.domain.has_discrete_class:
            mdl_button.setEnabled(True)
        else:
            if mdl_button.isChecked():
                self.button_group.button(Methods.Keep).setChecked(True)
            mdl_button.setEnabled(False)

    def _uncheck_all_buttons(self) -> None:
        button = self.button_group.checkedButton()
        if button is not None:
            self.button_group.setExclusive(False)
            button.setChecked(False)
            self.button_group.setExclusive(True)

    def _set_radio_enabled(self, method: Methods, value: bool) -> None:
        if self.button_group.button(method).isChecked() and not value:
            self._uncheck_all_buttons()
        self.button_group.button(method).setEnabled(value)
        for control_name in Options[method].controls:
            getattr(self, control_name).setEnabled(value)

    def _get_values(self, method: Methods) -> tuple:
        values = []
        for control_name in Options[method].controls:
            control = getattr(self, control_name)
            if isinstance(control, QSpinBox):
                values.append(control.value())
            elif isinstance(control, QComboBox):
                values.append(control.currentIndex())
            else:
                values.append(control.text())
        return tuple(values)

    def _set_values(self, method: Methods, values: tuple) -> None:
        for control_name, value in zip(Options[method].controls, values, strict=False):
            control = getattr(self, control_name)
            if isinstance(control, QSpinBox):
                control.setValue(value)
            elif isinstance(control, QComboBox):
                control.setCurrentIndex(value)
            else:
                control.setText(value)

    def varkeys_for_selection(self) -> list[Any]:
        """The keys of the selected columns, or ``[DefaultKey]`` for the general preset."""
        model = self.varview.model()
        keys = [
            variable_key(model[index.row()])
            for index in self.varview.selectionModel().selectedRows()
        ]
        return keys or [DefaultKey]

    def update_hints(self, method_id: int) -> None:
        """A method or its arguments changed: set it for the selected columns."""
        if self._interface_update:
            return
        method = Methods(method_id)
        args = self._get_values(method)
        keys = self.varkeys_for_selection()
        if method == Methods.Default:
            for key in keys:
                self.var_hints.pop(key, None)
        else:
            self.var_hints.update(dict.fromkeys(keys, VarHint(method, args)))
        if keys == [DefaultKey]:
            self._update_default_model()
        self._update_discretizations()
        self.commit.deferred()

    def _copy_to_manual(self) -> None:
        """Set the selected columns' method to custom cut points, with the cut
        points found on all rows."""
        texts = set()
        for key in self.varkeys_for_selection():
            dvar = self.discretized_vars.get(key)
            if isinstance(dvar, DiscreteVariable):
                fmt = self.data.domain[key[0]].repr_val
                text = ", ".join(map(fmt, dvar.compute_value.points))
                texts.add(text)
                self.var_hints[key] = VarHint(Methods.Custom, (text,))
        self._interface_update = True
        try:
            if len(texts) == 1:
                self.threshold_line.setText(texts.pop())
            else:
                self._uncheck_all_buttons()
        finally:
            self._interface_update = False
        self._update_discretizations()
        self.commit.deferred()

    def _default_selected(self, selected: Any) -> None:
        if not selected:
            return
        self.varview.selectionModel().clearSelection()
        self._update_interface()
        self._set_radio_enabled(Methods.Default, False)
        for method in (Methods.FixedWidth, Methods.FixedWidthTime, Methods.Custom):
            self._set_radio_enabled(method, True)
        self.copy_to_custom.setEnabled(False)

    def _var_selection_changed(self, _: Any) -> None:
        if not self.varview.selectionModel().selectedIndexes():
            return
        self.varview.default_view.selectionModel().clearSelection()
        self._update_interface()
        columns = [self.data.domain[name] for name, _ in self.varkeys_for_selection()]
        no_time = not any(isinstance(var, TimeVariable) for var in columns)
        all_time = all(isinstance(var, TimeVariable) for var in columns)
        self._set_radio_enabled(Methods.Default, True)
        self._set_radio_enabled(Methods.FixedWidth, no_time)
        self._set_radio_enabled(Methods.Custom, no_time)
        self.copy_to_custom.setEnabled(no_time)
        self._set_radio_enabled(Methods.FixedWidthTime, all_time)

    def _update_interface(self) -> None:
        """Check the selected columns' method, if they all have the same one."""
        if self._interface_update:
            return
        self._interface_update = True
        try:
            hints = list(unique_everseen(map(self.var_hints.get, self.varkeys_for_selection())))
            if len(hints) != 1:
                self._uncheck_all_buttons()
                return
            method, args = (Methods.Default, ()) if hints == [None] else hints[0]
            self.button_group.button(method).setChecked(True)
            self._set_values(method, args)
        finally:
            self._interface_update = False

    # ------------------------------------------------------------------
    # The list of columns: methods, and cut points found on all rows

    def _update_discretizations(self) -> None:
        """Show each column's method and, once the machine fitted it, its cut points."""
        if self.data is None:
            return
        domain = self.data.domain
        recipe = make_recipe(self.var_hints, domain)
        notes = {}
        if self.fitted is not None:
            fitted_recipe, fitted_domain = self.fitted
            notes = fitted_notes(fitted_recipe, domain, fitted_domain)
        model = self.varview.model()
        for row, var in enumerate(model):
            key = variable_key(var)
            hint = recipe.hint(var)
            if self.fitted is not None and self.fitted[0].hint(var) == hint:
                points, dvar = notes[key]
                self.discretized_vars[key] = dvar
            else:  # not fitted with this method yet
                self.discretized_vars.pop(key, None)
                known = known_outcome(var, hint, domain)
                points, dvar = (known[1], known[0]) if known is not None else (PENDING, None)
            values = getattr(dvar, "values", ())
            desc = DiscDesc(self.var_hints.get(key), points, values)
            model.setData(model.index(row), desc, Qt.ItemDataRole.UserRole)

    # ------------------------------------------------------------------
    # Inputs and output

    @Inputs.data
    def set_data(self, data: Table | None) -> None:
        """Receive the Data input."""
        self.cancel()
        self.data = data
        self.discretized_vars = {}
        self.fitted = None
        self.varview.model().set_domain(None if data is None else data.domain)
        self._update_discretizations()
        self._update_default_model()
        self.varview.select_default()
        self._set_mdl_button()
        self._send(None)
        self.commit.now()

    def on_cloud_connected(self) -> None:
        if self.output is None and self.task is None and self.data is not None:
            self.commit.now()

    def _send(self, table: Table | None) -> None:
        self.output = table
        self.Outputs.data.send(table)

    @gui.deferred
    def commit(self) -> None:
        """Find the cut points on all rows on the machine and send the cloud table."""
        self.cancel()
        self._clear_cloud_messages()
        data = self.data
        if data is None:
            self._send(None)
            return
        if not is_cloud_table(data):
            self._send(None)
            self._show_error(self.Error.cloud_error, LOCAL_DATA)
            return
        recipe = make_recipe(self.var_hints, data.domain)
        if not recipe.changes(data.domain):  # every column kept: nothing to fit
            self.fitted = (recipe, data.domain)
            self._update_discretizations()
            self._send(data)
            return
        handle = self.session.handle
        machine_id = handle.machine_id if handle is not None else None

        def work(task: CloudTask) -> tuple[DiscretizeColumns, RemoteTable]:
            task.set_status("Finding cut points on all rows")
            return recipe, preprocessed(data, recipe, task.client, machine_id, task.wait_for_job)

        self.run_cloud(work, on_done=self._fitted)

    def _fitted(self, result: tuple[DiscretizeColumns, RemoteTable]) -> None:
        recipe, output = result
        self.fitted = (recipe, output.domain)
        self._update_discretizations()
        self._send(output)

    def send_report(self) -> None:
        """Report the general preset and each column's values, as Orange's widget."""
        default_model = self.varview.default_view.model()
        desc = default_model.data(default_model.index(0))
        self.report_items((tuple(desc.split(": ", maxsplit=1)),))
        model = self.varview.model()
        reported = []
        for row in range(model.rowCount()):
            name = model[row].name
            desc = model.data(model.index(row), Qt.ItemDataRole.UserRole)
            if desc.hint is not None:
                name = f"{name} ({format_desc(desc.hint)})"
            reported.append((name, ", ".join(desc.values)))
        self.report_items("Variables", reported)


if __name__ == "__main__":  # pragma: no cover
    WidgetPreview(OWCloudDiscretize).run()
