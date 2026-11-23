"""Cloud Data Table: look at the rows of a cloud table, page by page.

The rows come from the machine (``GET /v1/tables/{id}/rows``); sorting by a
column (click its header) sorts all rows on the machine. The widget shows the
exact row and column counts and computes nothing on this computer.
"""

from __future__ import annotations

from typing import Any

from AnyQt.QtCore import QAbstractTableModel, QModelIndex, Qt
from AnyQt.QtGui import QBrush, QColor
from AnyQt.QtWidgets import QHeaderView, QLabel, QTableView
from Orange.data import Table
from Orange.widgets import gui
from Orange.widgets.settings import Setting
from Orange.widgets.utils.widgetpreview import WidgetPreview
from Orange.widgets.widget import Input

from orangecontrib.cloud.client.providers.base import MachineHandle
from orangecontrib.cloud.data.remote_table import LOCAL_DATA, is_cloud_table, resolve
from orangecontrib.cloud.protocol.machine import InfoResponse
from orangecontrib.cloud.protocol.tables import TableInfo
from orangecontrib.cloud.widgets.base import CloudTask, OWCloudBaseWidget, machine_label

__all__ = ["OWCloudDataTable"]

#: Background of the target and meta columns, as in Orange's Data Table.
CLASS_BRUSH = QBrush(QColor(160, 160, 160, 40))
META_BRUSH = QBrush(QColor(220, 220, 200, 70))


class RowsModel(QAbstractTableModel):
    """One page of rows: target, meta and feature columns, as Orange shows
    them; the row headers are the rows' positions in the whole table."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.page: Table | None = None
        self.page_start = 0
        self.columns: list = []
        self._values: list = []
        self._kinds: list[str] = []

    def set_page(self, page: Table | None, start: int = 0) -> None:
        """Show ``page``, whose first row is row ``start`` of the table."""
        self.beginResetModel()
        self.page, self.page_start = page, start
        if page is None:
            self.columns, self._values, self._kinds = [], [], []
        else:
            domain = page.domain
            groups = (("class", domain.class_vars), ("meta", domain.metas),
                      ("attribute", domain.attributes))  # fmt: skip
            self.columns = [var for _, variables in groups for var in variables]
            self._kinds = [kind for kind, variables in groups for _ in variables]
            self._values = [page.get_column(var) for var in self.columns]
        self.endResetModel()

    def rowCount(self, parent: QModelIndex | None = None) -> int:
        valid = parent is not None and parent.isValid()
        return 0 if valid or self.page is None else len(self.page)

    def columnCount(self, parent: QModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self.columns)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        col = index.column()
        var = self.columns[col]
        if role == Qt.ItemDataRole.DisplayRole:
            return var.str_val(self._values[col][index.row()])
        if role == Qt.ItemDataRole.TextAlignmentRole and var.is_continuous:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.BackgroundRole:
            kind = self._kinds[col]
            return CLASS_BRUSH if kind == "class" else META_BRUSH if kind == "meta" else None
        return None

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self.columns[section].name if section < len(self.columns) else None
        return f"{self.page_start + section + 1:,}"


class OWCloudDataTable(OWCloudBaseWidget):
    """Show the rows of a cloud table, a page at a time, fetched from the machine."""

    name = "Cloud Data Table"
    description = "Show the rows of a cloud table, a page at a time, fetched from the machine."
    icon = "icons/CloudDataTable.svg"
    priority = 20
    keywords = "cloud data table, view, rows, spreadsheet"

    class Inputs:
        data = Input("Data", Table, doc="A cloud table (from Cloud File).")

    rows_per_page: int = Setting(1000)

    def __init__(self):
        super().__init__()
        self.data: Table | None = None
        #: The table's metadata on the machine, after the first page.
        self.table_info: TableInfo | None = None
        self.page_start = 0
        self.sort_column: str | None = None
        self.descending = False

        box = gui.vBox(self.controlArea, "Info")
        self.info_label = QLabel("No data.")
        box.layout().addWidget(self.info_label)

        box = gui.vBox(self.controlArea, "Rows")
        gui.spin(
            box, self, "rows_per_page", 100, 10000, step=100, label="Rows per page:",
            callback=self._rows_per_page_changed, keyboardTracking=False,
        )  # fmt: skip
        self.page_label = QLabel()
        box.layout().addWidget(self.page_label)
        row = gui.hBox(box)
        self.first_button = gui.button(row, self, "First", callback=self.first_page, width=60)
        self.prev_button = gui.button(row, self, "Previous", callback=self.previous_page)
        self.next_button = gui.button(row, self, "Next", callback=self.next_page)
        self.last_button = gui.button(row, self, "Last", callback=self.last_page, width=60)
        gui.rubber(self.controlArea)

        self.model = RowsModel(self)
        self.view = QTableView()
        self.view.setModel(self.model)
        self.view.setSortingEnabled(False)  # sorting happens on the machine
        self.view.setWordWrap(False)
        self.view.verticalHeader().setDefaultSectionSize(22)
        header = self.view.horizontalHeader()
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.sectionClicked.connect(self._header_clicked)
        self.mainArea.layout().addWidget(self.view)
        self._update_controls()

    # ------------------------------------------------------------------
    # Input

    @Inputs.data
    def set_data(self, data: Table | None) -> None:
        """Receive the Data input."""
        self.cancel()
        self.data = data
        self.table_info = None
        self.page_start, self.sort_column, self.descending = 0, None, False
        self._set_sort_indicator()
        self.model.set_page(None)
        self._clear_cloud_messages()
        if data is not None and not is_cloud_table(data):
            self._show_error(self.Error.cloud_error, LOCAL_DATA)
        self._update_controls()
        self.fetch()

    def on_cloud_connected(self) -> None:
        if self.data is not None and self.model.page is None and self.task is None:
            self.fetch()

    # ------------------------------------------------------------------
    # Pages

    @property
    def n_rows(self) -> int | None:
        """The table's number of rows (``None`` without a table)."""
        return self.table_info.n_rows if self.table_info is not None else None

    def fetch(self) -> bool:
        """Fetch the page that starts at ``self.page_start`` from the machine."""
        data = self.data
        if not is_cloud_table(data):
            return False
        handle = self.session.handle
        machine_id = handle.machine_id if handle is not None else None
        start, stop = self.page_start, self.page_start + self.rows_per_page
        sort, descending = self.sort_column, self.descending

        def work(task: CloudTask):
            task.set_status("Fetching rows from the machine")
            table_id = resolve(data, task.client, machine_id)
            info = task.client.table_info(table_id)
            page = task.client.table_rows(table_id, start, stop, sort, descending)
            return info, page, start

        return self.run_cloud(work)

    def on_cloud_result(self, result: Any) -> None:
        info, page, start = result
        self.table_info = info
        self.model.set_page(page, start)
        self.view.resizeColumnsToContents()
        self._update_controls()

    def go_to(self, start: int) -> None:
        """Show the page that starts at row ``start``."""
        n = self.n_rows or 0
        last = max(0, (n - 1) // self.rows_per_page * self.rows_per_page)
        start = max(0, min(start, last))
        if start != self.page_start or self.model.page is None:
            self.page_start = start
            self.fetch()

    def first_page(self) -> None:
        """Show the first page."""
        self.go_to(0)

    def previous_page(self) -> None:
        """Show the previous page."""
        self.go_to(self.page_start - self.rows_per_page)

    def next_page(self) -> None:
        """Show the next page."""
        self.go_to(self.page_start + self.rows_per_page)

    def last_page(self) -> None:
        """Show the last page."""
        self.go_to(self.n_rows or 0)

    def _rows_per_page_changed(self) -> None:
        self.page_start = self.page_start // self.rows_per_page * self.rows_per_page
        self.fetch()

    # ------------------------------------------------------------------
    # Sorting

    def sort_by(self, column: str | None, descending: bool = False) -> None:
        """Sort all rows on the machine by ``column`` (``None``: the table's
        order) and show the first page."""
        self.sort_column, self.descending, self.page_start = column, descending, 0
        self._set_sort_indicator()
        self.fetch()

    def _header_clicked(self, section: int) -> None:
        if not 0 <= section < len(self.model.columns):
            return
        name = self.model.columns[section].name
        descending = name == self.sort_column and not self.descending
        self.sort_by(name, descending)

    def _set_sort_indicator(self) -> None:
        header = self.view.horizontalHeader()
        names = [var.name for var in self.model.columns]
        section = names.index(self.sort_column) if self.sort_column in names else -1
        order = Qt.SortOrder.DescendingOrder if self.descending else Qt.SortOrder.AscendingOrder
        header.blockSignals(True)
        header.setSortIndicator(section, order)
        header.blockSignals(False)

    # ------------------------------------------------------------------

    def _update_controls(self) -> None:
        info = self.table_info
        if info is None:
            self.info_label.setText("No data." if self.data is None else "")
            self.page_label.setText("")
        else:
            n_columns = info.n_attributes + info.n_class_vars + info.n_metas
            self.info_label.setText(
                f"{info.n_rows:,} rows, {n_columns} columns\n"
                f"({info.n_attributes} features, {info.n_class_vars} target, "
                f"{info.n_metas} meta)"
            )
            shown = len(self.model.page) if self.model.page is not None else 0
            if shown:
                self.page_label.setText(
                    f"Rows {self.page_start + 1:,}–{self.page_start + shown:,} of {info.n_rows:,}"
                )
            else:
                self.page_label.setText(f"No rows here (the table has {info.n_rows:,}).")
        self._set_sort_indicator()
        n = self.n_rows or 0
        self.first_button.setEnabled(self.page_start > 0)
        self.prev_button.setEnabled(self.page_start > 0)
        has_next = self.page_start + self.rows_per_page < n
        self.next_button.setEnabled(has_next)
        self.last_button.setEnabled(has_next)

    def cloud_status_text(
        self, info: InfoResponse | None, handle: MachineHandle | None, runtime: float | None
    ) -> str:
        return f"Rows from {machine_label(info, handle)}."


if __name__ == "__main__":  # pragma: no cover
    WidgetPreview(OWCloudDataTable).run()
