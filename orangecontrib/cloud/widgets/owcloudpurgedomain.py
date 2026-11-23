"""Cloud Purge Domain: Orange's Purge Domain for a cloud table, decided on all rows.

The options are Orange's Purge Domain's: sort the values of categorical
columns, remove unused values, and remove constant columns, for features,
the target and metas; its settings, texts and report follow Orange's widget
(``Orange/widgets/data/owpurgedomain.py``, Orange 3.41, GPL-3.0-or-later).
Orange's widget decides what is unused or constant from the data it sees;
on a cloud table that is the empty placeholder row, so it would remove
every column. This widget sends Orange's own ``Remove`` with those options
as a recipe, and the machine fits it on all rows (a ``derive`` job with the
step ``("preprocess", Remove(...))``, as Cloud Preprocess does). The output
is the cloud table with that step, which the machine replays.

The counts of sorted, reduced and removed columns come from comparing the
input's columns with the fitted ones that the machine sends back.
"""

from __future__ import annotations

from AnyQt.QtWidgets import QFrame
from Orange.data import Domain, Table
from Orange.preprocess.remove import Remove
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

__all__ = ["OWCloudPurgeDomain", "purge_counts"]


def purge_counts(before: tuple, after: tuple) -> dict[str, int]:
    """Orange's ``Remove`` counts of ``removed``, ``reduced`` and ``sorted``
    columns, from the columns ``before`` and ``after`` it (one part of a
    domain: features, targets or metas)."""
    found = {var.name: var for var in after}
    counts = {"removed": 0, "reduced": 0, "sorted": 0}
    for var in before:
        new = found.get(var.name)
        if new is None:
            counts["removed"] += 1
        elif var.is_discrete and new.is_discrete:
            kept = [value for value in var.values if value in set(new.values)]
            counts["reduced"] += len(new.values) < len(var.values)
            counts["sorted"] += list(new.values) != kept
    return counts


class OWCloudPurgeDomain(OWCloudBaseWidget):
    """Removes unused values and constant columns of a cloud table, decided on all rows."""

    name = "Cloud Purge Domain"
    description = (
        "Remove unused values and constant columns of a cloud table and sort "
        "values, as Orange's Purge Domain does, decided on all rows on the machine."
    )
    icon = "icons/CloudPurgeDomain.svg"
    priority = 28
    keywords = "cloud purge domain, remove, delete, unused, constant, sort"
    want_main_area = False
    resizing_enabled = False

    class Inputs:
        data = Input("Data", Table, doc="A cloud table (from Cloud File).")

    class Outputs:
        data = Output("Data", Table, doc="The cloud table without unused values and columns.")

    # Orange's Purge Domain's settings, with its names.
    removeValues: int = Setting(1)
    removeAttributes: int = Setting(1)
    removeClasses: int = Setting(1)
    removeClassAttribute: int = Setting(1)
    removeMetaAttributeValues: int = Setting(1)
    removeMetaAttributes: int = Setting(1)
    autoSend: bool = Setting(True)
    sortValues: bool = Setting(True)
    sortClasses: bool = Setting(True)

    feature_options = (
        ("sortValues", "Sort categorical feature values"),
        ("removeValues", "Remove unused feature values"),
        ("removeAttributes", "Remove constant features"),
    )
    class_options = (
        ("sortClasses", "Sort categorical class values"),
        ("removeClasses", "Remove unused class variable values"),
        ("removeClassAttribute", "Remove constant class variables"),
    )
    meta_options = (
        ("removeMetaAttributeValues", "Remove unused meta attribute values"),
        ("removeMetaAttributes", "Remove constant meta attributes"),
    )
    stat_labels = (
        ("Sorted features", "resortedAttrs"),
        ("Reduced features", "reducedAttrs"),
        ("Removed features", "removedAttrs"),
        ("Sorted classes", "resortedClasses"),
        ("Reduced classes", "reducedClasses"),
        ("Removed classes", "removedClasses"),
        ("Reduced metas", "reducedMetas"),
        ("Removed metas", "removedMetas"),
    )

    def __init__(self):
        super().__init__()
        self.data: Table | None = None
        self.output: Table | None = None
        self._clear_counts()
        for title, options, label in (
            (
                "Features",
                self.feature_options,
                "Sorted: %(resortedAttrs)s, reduced: %(reducedAttrs)s, removed: %(removedAttrs)s",
            ),
            (
                "Classes",
                self.class_options,
                "Sorted: %(resortedClasses)s, reduced: %(reducedClasses)s, "
                "removed: %(removedClasses)s",
            ),
            (
                "Meta attributes",
                self.meta_options,
                "Reduced: %(reducedMetas)s, removed: %(removedMetas)s",
            ),
        ):
            box = gui.vBox(self.controlArea, title)
            for value, text in options:
                gui.checkBox(box, self, value, text, callback=self.commit.deferred)
            line = QFrame()
            line.setFrameShape(QFrame.Shape.HLine)
            line.setFrameShadow(QFrame.Shadow.Sunken)
            box.layout().addWidget(line)
            gui.label(box, self, label)
        gui.auto_send(self.buttonsArea, self, "autoSend")

    def _clear_counts(self) -> None:
        for _, name in self.stat_labels:
            setattr(self, name, "-")

    def _show_counts(self, before: Domain, after: Domain) -> None:
        parts = (
            ("Attrs", before.attributes, after.attributes),
            ("Classes", before.class_vars, after.class_vars),
            ("Metas", before.metas, after.metas),
        )
        for suffix, old, new in parts:
            counts = purge_counts(old, new)
            setattr(self, f"removed{suffix}", counts["removed"])
            setattr(self, f"reduced{suffix}", counts["reduced"])
            if suffix != "Metas":
                setattr(self, f"resorted{suffix}", counts["sorted"])

    # ------------------------------------------------------------------

    @Inputs.data
    def set_data(self, data: Table | None) -> None:
        """Receive the Data input."""
        self.cancel()
        self.data = data
        self._clear_counts()
        self._send(None)
        if data is not None:
            self.commit.now()

    def on_cloud_connected(self) -> None:
        if self.output is None and self.task is None and self.data is not None:
            self.commit.now()

    def remover(self) -> Remove:
        """Orange's ``Remove`` with the widget's options, as Orange's Purge Domain makes it."""
        attr_flags = sum([
            Remove.SortValues * self.sortValues,
            Remove.RemoveConstant * self.removeAttributes,
            Remove.RemoveUnusedValues * self.removeValues,
        ])  # fmt: skip
        class_flags = sum([
            Remove.SortValues * self.sortClasses,
            Remove.RemoveConstant * self.removeClassAttribute,
            Remove.RemoveUnusedValues * self.removeClasses,
        ])  # fmt: skip
        meta_flags = sum([
            Remove.RemoveConstant * self.removeMetaAttributes,
            Remove.RemoveUnusedValues * self.removeMetaAttributeValues,
        ])  # fmt: skip
        return Remove(attr_flags, class_flags, meta_flags)

    def _send(self, table: Table | None) -> None:
        self.output = table
        self.Outputs.data.send(table)

    @gui.deferred
    def commit(self) -> None:
        """Purge on all rows on the machine and send the cloud table."""
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
        remover = self.remover()
        if not (remover.attr_flags or remover.class_flags or remover.meta_flags):
            self._show_counts(data.domain, data.domain)  # nothing to do
            self._send(data)
            return
        handle = self.session.handle
        machine_id = handle.machine_id if handle is not None else None

        def work(task: CloudTask) -> RemoteTable:
            task.set_status("Purging on all rows")
            return preprocessed(data, remover, task.client, machine_id, task.wait_for_job)

        self.run_cloud(work, on_done=self._purged)

    def _purged(self, output: RemoteTable) -> None:
        self._show_counts(self.data.domain, output.domain)
        self._send(output)

    def send_report(self) -> None:
        """Report the options and the counts, as Orange's widget."""

        def list_opts(opts: tuple) -> str:
            chosen = [label.lower() for value, label in opts if getattr(self, value)]
            return "; ".join(chosen) or "no changes"

        self.report_items(
            "Settings",
            (
                ("Features", list_opts(self.feature_options)),
                ("Classes", list_opts(self.class_options)),
                ("Metas", list_opts(self.meta_options)),
            ),
        )
        if self.data:
            self.report_items(
                "Statistics", ((label, getattr(self, value)) for label, value in self.stat_labels)
            )

    def on_exception(self, ex: Exception) -> None:
        self._clear_counts()
        super().on_exception(ex)


if __name__ == "__main__":  # pragma: no cover
    WidgetPreview(OWCloudPurgeDomain).run()
