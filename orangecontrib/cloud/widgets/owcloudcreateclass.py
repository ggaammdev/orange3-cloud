"""Cloud Create Class: Orange's Create Class for a cloud table, with its rules applied on all rows.

The widget is Orange's Create Class: a new class column from a text or
categorical column, with rules (a class name and a substring each), case
sensitivity, matching at the beginning only and regular expressions. Its
code follows Orange's widget (``Orange/widgets/data/owcreateclass.py``,
Orange 3.41, GPL-3.0-or-later), which can't be subclassed.

Orange's Create Class keeps a rule only while the rows it sees aren't all
matched by the rules above it; on a cloud table that is the empty
placeholder row, so it keeps only its first rule. This widget sends its
settings as a recipe, :class:`~orangecontrib.cloud.protocol.recipes.
CreateClass`, which the machine fits on all rows (a ``derive`` job with the
step ``("preprocess", recipe)``, as Cloud Preprocess does): it keeps the
rules as Create Class does on all rows and makes the same class column. The
output is the cloud table with that step, which the machine replays.

The counts next to each rule come from the column's values and their counts
on all rows (the ``values`` statistics), as Create Class counts them. A text
column with too many different values shows no counts; the machine still
applies the rules on all rows.
"""

from __future__ import annotations

import re
from functools import partial
from typing import Any

from AnyQt.QtCore import Qt
from AnyQt.QtWidgets import QGridLayout, QLabel, QLineEdit, QSizePolicy
from Orange.data import DiscreteVariable, StringVariable, Table
from Orange.widgets import gui
from Orange.widgets.settings import ContextSetting, DomainContextHandler, Setting
from Orange.widgets.utils.itemmodels import DomainModel
from Orange.widgets.utils.widgetpreview import WidgetPreview
from Orange.widgets.widget import Input, Msg, Output

from orangecontrib.cloud.data.remote_table import LOCAL_DATA, is_cloud_table, preprocessed
from orangecontrib.cloud.protocol.recipes import CreateClass, ValueCounts, class_labels
from orangecontrib.cloud.widgets.base import CloudTask, OWCloudBaseWidget
from orangecontrib.cloud.widgets.owcloudcategories import fetch_values

__all__ = ["OWCloudCreateClass"]


class OWCloudCreateClass(OWCloudBaseWidget):
    """Creates a class column from substrings of a column, on all rows of a cloud table."""

    name = "Cloud Create Class"
    description = (
        "Create a class column from a text or categorical column of a cloud "
        "table by substrings, as Orange's Create Class does, applied on all rows."
    )
    icon = "icons/CloudCreateClass.svg"
    priority = 29
    keywords = "cloud create class, class, substring, rules, target"
    want_main_area = False

    class Inputs:
        data = Input("Data", Table, doc="A cloud table (from Cloud File).")

    class Outputs:
        data = Output("Data", Table, doc="The cloud table with the new class column.")

    # Orange's Create Class's settings, with its names.
    settingsHandler = DomainContextHandler()
    attribute = ContextSetting(None, schema_only=True)
    class_name: str = Setting("class", schema_only=True)
    rules: dict[str | None, list[list[str]]] = Setting({}, schema_only=True)
    match_beginning: bool = Setting(False, schema_only=True)
    case_sensitive: bool = Setting(False, schema_only=True)
    regular_expressions: bool = Setting(False, schema_only=True)

    class Warning(OWCloudBaseWidget.Warning):
        no_nonnumeric_vars = Msg("Data contains only numeric variables.")

    class Error(OWCloudBaseWidget.Error):
        class_name_duplicated = Msg("Class name duplicated.")
        class_name_empty = Msg("Class name should not be empty.")
        invalid_regular_expression = Msg("Invalid regular expression: {}")

    class Information(OWCloudBaseWidget.Information):
        no_counts = Msg("'{}' has {:,} different values, too many to count here.")

    def __init__(self):
        super().__init__()
        self.data: Table | None = None
        self.output: Table | None = None
        #: The ``values`` statistics of the columns counted so far, by name.
        self.column_stats: dict[str, dict[str, Any]] = {}
        #: Create Class's counts for each rule (matched here, all matching).
        self.match_counts: list[tuple[int, int]] = []
        self.line_edits: list[list[QLineEdit]] = []
        self.remove_buttons: list[Any] = []
        self.counts: list[list[QLabel]] = []

        gui.lineEdit(
            self.controlArea, self, "class_name", orientation=Qt.Orientation.Horizontal,
            box="New Class Name",
        )  # fmt: skip
        box = gui.vBox(self.controlArea, "Source column and patterns")
        combo = gui.comboBox(
            box, self, "attribute", searchable=True, callback=self._attribute_changed,
            model=DomainModel(valid_types=(StringVariable, DiscreteVariable)),
        )  # fmt: skip
        combo.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Preferred)
        self.rules_box = grid = QGridLayout()
        grid.setSpacing(4)
        grid.setColumnStretch(2, 100)
        for column, text in ((1, "Name"), (2, "Substring"), (3, "Count")):
            grid.addWidget(QLabel(text), 0, column, 1, 1 if column < 3 else 2)
        box.layout().addLayout(grid)
        row = gui.hBox(box)
        gui.rubber(row)
        gui.button(row, self, "+", callback=self.add_row, autoDefault=False, width=34)

        options = gui.vBox(self.controlArea, "Options")
        gui.checkBox(
            options, self, "regular_expressions", "Use regular expressions",
            callback=self.options_changed,
        )  # fmt: skip
        gui.checkBox(
            options, self, "match_beginning", "Match only at the beginning",
            stateWhenDisabled=False, callback=self.options_changed,
        )  # fmt: skip
        gui.checkBox(
            options, self, "case_sensitive", "Case sensitive", callback=self.options_changed
        )
        gui.rubber(self.controlArea)
        gui.button(self.buttonsArea, self, "Apply", callback=self.apply)
        self.update_rules()

    # ------------------------------------------------------------------
    # Rules (as in Orange's Create Class)

    @property
    def active_rules(self) -> list[list[str]]:
        """The rules of the selected column: [name, substring] pairs."""
        return self.rules.setdefault(self.attribute and self.attribute.name, [["", ""], ["", ""]])

    def update_rules(self) -> None:
        """Adjust the rows of rules to the selected column's and update the counts."""
        self.adjust_n_rule_rows()
        for edits, rule in zip(self.line_edits, self.active_rules, strict=True):
            for edit, text in zip(edits, rule, strict=True):
                edit.setText(text)
        self.update_counts()

    def adjust_n_rule_rows(self) -> None:
        """Add or remove rows of line edits to match the rules."""
        while len(self.active_rules) > len(self.line_edits):
            self._add_line()
        while len(self.line_edits) > len(self.active_rules):
            for widget in (*self.line_edits.pop(), self.remove_buttons.pop(), *self.counts.pop()):
                widget.deleteLater()
        for edits, rule in zip(self.line_edits, self.active_rules, strict=True):
            for column, edit in enumerate(edits):
                edit.row, edit.col_idx = rule, column

    def _add_line(self) -> None:
        row = len(self.line_edits) + 1
        edits = []
        for column in (1, 2):
            edit = QLineEdit()
            edit.textChanged.connect(partial(self.sync_edit, edit))
            self.rules_box.addWidget(edit, row, column)
            edits.append(edit)
        self.line_edits.append(edits)
        button = gui.button(None, self, "×", width=33, autoDefault=False)
        button.clicked.connect(lambda *_, b=button: self.remove_row(b))
        self.remove_buttons.append(button)
        self.rules_box.addWidget(button, row, 0)
        labels = [QLabel(alignment=Qt.AlignmentFlag.AlignCenter) for _ in range(2)]
        labels[1].setStyleSheet("color: gray")
        for column, label in enumerate(labels, start=3):
            self.rules_box.addWidget(label, row, column)
        self.counts.append(labels)

    def add_row(self) -> None:
        """Append an empty rule."""
        self.active_rules.append(["", ""])
        self.adjust_n_rule_rows()
        self.update_counts()

    def remove_row(self, button: Any) -> None:
        """Remove the rule of ``button``'s row."""
        del self.active_rules[self.remove_buttons.index(button)]
        self.update_rules()

    def sync_edit(self, edit: QLineEdit, text: str) -> None:
        """Copy an edited name or substring into the rules."""
        edit.row[edit.col_idx] = text
        self.update_counts()

    def options_changed(self) -> None:
        """Update the counts after a change of the options."""
        self.controls.match_beginning.setEnabled(not self.regular_expressions)
        self.update_counts()

    def class_labels(self) -> list[str]:
        """The class names, with C1, C2, ... for empty ones, as Create Class names them."""
        return class_labels([tuple(rule) for rule in self.active_rules])

    def invalid_patterns(self) -> str | None:
        """The first substring that isn't a valid regular expression, if they are used."""
        if not self.regular_expressions:
            return None
        for _, pattern in self.active_rules:
            try:
                re.compile(pattern)
            except re.error:
                return pattern
        return None

    def recipe(self) -> CreateClass:
        """The recipe for the widget's settings."""
        return CreateClass(
            self.attribute.name,
            self.class_name,
            [tuple(rule) for rule in self.active_rules],
            case_sensitive=self.case_sensitive,
            match_beginning=self.match_beginning,
            regular_expressions=self.regular_expressions,
        )

    def _value_counts(self) -> ValueCounts | None:
        stats = self.attribute and self.column_stats.get(self.attribute.name)
        return ValueCounts.from_stats(stats) if stats else None

    def update_counts(self) -> None:
        """Show Create Class's counts for each rule, from the counts of all rows."""
        for labels in self.counts:
            for label in labels:
                label.setText("")
                label.setToolTip("")
        self.match_counts = []
        self.Information.no_counts.clear()
        if (invalid := self.invalid_patterns()) is not None:
            self.Error.invalid_regular_expression(invalid)
            return
        self.Error.invalid_regular_expression.clear()
        if self.attribute is None:
            return
        counts = self._value_counts()
        stats = self.column_stats.get(self.attribute.name)
        if counts is None and stats is not None:
            self.Information.no_counts(self.attribute.name, stats["n_values"])
        if counts is not None:
            self.match_counts = self.recipe().match_counts(counts.values, counts.counts)
            self._set_labels()
        self._set_placeholders(counts is not None)

    def _set_labels(self) -> None:
        for (matched, total), labels, (name, pattern) in zip(
            self.match_counts, self.counts, self.active_rules, strict=False
        ):
            before = total - matched
            labels[0].setText(f"{matched}")
            if before and (name or pattern):
                labels[1].setText(f"+ {before}")
                if matched:
                    rows = "instance" if total == 1 else "instances"
                    verb = "is" if before == 1 else "are"
                    tip = f"{before} of {total} matching {rows} {verb} already covered above."
                else:
                    tip = "All matching instances are already covered above"
                for label in labels:
                    label.setToolTip(tip)

    def _set_placeholders(self, counted: bool) -> None:
        if counted:
            matches = [n for n, _ in self.match_counts] + [0] * len(self.line_edits)
            for matched, (_, pattern) in zip(matches, self.line_edits, strict=False):
                if not pattern.text():
                    pattern.setPlaceholderText("(remaining instances)" if matched else "(unused)")
        for label, (name, _) in zip(self.class_labels(), self.line_edits, strict=False):
            if not name.text():
                name.setPlaceholderText(label)

    # ------------------------------------------------------------------
    # Input, machine and output

    @Inputs.data
    def set_data(self, data: Table | None) -> None:
        """Receive the Data input."""
        self.cancel()
        self.closeContext()
        self.data = data
        self.column_stats = {}
        self._send(None)  # nothing from the previous data until the rules are applied
        model = self.controls.attribute.model()
        model.set_domain(data.domain if data is not None else None)
        self.Warning.no_nonnumeric_vars(shown=data is not None and not model)
        if not model:
            self.attribute = None
            return
        self.attribute = model[0]
        self.openContext(data)
        self.update_rules()
        self.apply()

    def _attribute_changed(self) -> None:
        self.update_rules()
        self._start(apply=False)

    def on_cloud_connected(self) -> None:
        if self.output is None and self.task is None and self.data is not None:
            self.apply()

    def _send(self, table: Table | None) -> None:
        self.output = table
        self.Outputs.data.send(table)

    def apply(self) -> None:
        """Check the settings, then apply the rules on all rows on the machine."""
        for message in (
            self.Error.class_name_duplicated,
            self.Error.class_name_empty,
            self.Error.invalid_regular_expression,
        ):
            message.clear()
        if (invalid := self.invalid_patterns()) is not None:
            self.Error.invalid_regular_expression(invalid)
            self._send(None)
            return
        self.class_name = self.class_name.strip()
        if not self.attribute:
            self._send(None)
            return
        if not self.class_name:
            self.Error.class_name_empty()
        if self.class_name in self.data.domain:
            self.Error.class_name_duplicated()
        if not self.class_name or self.class_name in self.data.domain:
            self._send(None)
            return
        self._start(apply=True)

    def _start(self, apply: bool) -> None:
        """Count the column's values on the machine if they aren't known yet,
        and with ``apply``, fit the recipe on all rows and send the result."""
        self.cancel()
        self._clear_cloud_messages()
        data, var = self.data, self.attribute
        if data is None or var is None:
            return
        if not is_cloud_table(data):
            self._send(None)
            self._show_error(self.Error.cloud_error, LOCAL_DATA)
            return
        stats = self.column_stats.get(var.name)
        if stats is not None and not apply:
            return
        recipe, handle = self.recipe(), self.session.handle
        machine_id = handle.machine_id if handle is not None else None

        def work(task: CloudTask) -> tuple[dict[str, Any], Table | None]:
            found = stats if stats is not None else fetch_values(task, data, var, machine_id)
            if not apply:
                return found, None
            task.set_status("Applying the rules on all rows")
            return found, preprocessed(data, recipe, task.client, machine_id, task.wait_for_job)

        self.run_cloud(work, on_done=partial(self._done, var.name, apply))

    def _done(self, name: str, apply: bool, result: tuple[dict[str, Any], Table | None]) -> None:
        self.column_stats[name], output = result
        self.update_counts()
        if apply:
            self._send(output)

    def send_report(self) -> None:
        """Report the column and the rules with their counts, as Orange's widget."""
        if not self.attribute:
            return
        self.report_items("Input", [("Source attribute", self.attribute.name)])
        rules = []
        for (matched, total), name, (label, pattern) in zip(
            self.match_counts, self.class_labels(), self.active_rules, strict=False
        ):
            if label or pattern or total:
                condition = (
                    f"if {self.attribute.name} contains {pattern}" if pattern else "otherwise"
                )
                rules.append(f"<li><b>{name}</b> {condition}; {matched} of {total} matching</li>")
        if rules:
            self.report_items("Output", [("Class name", self.class_name)])
            self.report_raw(f"<ol>{''.join(rules)}</ol>")


if __name__ == "__main__":  # pragma: no cover
    WidgetPreview(OWCloudCreateClass).run()
