"""Cloud Feature Statistics: Orange's Feature Statistics for a cloud table.

The machine computes the numbers (``basic``) and the small histograms
(``distribution``, split by the colour column) on all rows. The table, its
sorting and the "Statistics" output are Orange's own Feature Statistics
model and view, with the machine's numbers in place of numbers computed
from the placeholder row.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from AnyQt.QtCore import QModelIndex, QRectF, Qt
from AnyQt.QtGui import QBrush, QColor, QPen
from AnyQt.QtWidgets import QGraphicsRectItem, QGraphicsScene, QHeaderView
from Orange.data import DiscreteVariable, Table
from Orange.widgets import gui
from Orange.widgets.data.owfeaturestatistics import (
    FeatureStatisticsTableModel,
    FeatureStatisticsTableView,
)
from Orange.widgets.settings import ContextSetting, DomainContextHandler, Setting
from Orange.widgets.utils.itemmodels import DomainModel
from Orange.widgets.utils.widgetpreview import WidgetPreview
from Orange.widgets.widget import Input, Output

from orangecontrib.cloud.protocol.tables import StatsKind, StatsRequest
from orangecontrib.cloud.widgets.viewer import OWCloudViewer, ViewerResult, column_index

__all__ = ["CloudStatisticsModel", "OWCloudFeatureStatistics"]

#: The model's arrays and the keys of the machine's ``basic`` statistics.
STAT_ARRAYS = (
    ("_min", "min"),
    ("_max", "max"),
    ("_center", "mean"),
    ("_median", "median"),
    ("_mode", "mode"),
    ("_dispersion", "dispersion"),
)

#: Size of a small histogram's scene (the view stretches it into the cell).
SCENE_WIDTH, SCENE_HEIGHT = 300.0, 100.0
GRAY = QColor("#ccc")


def histogram_scene(counts: np.ndarray | None, colors: list[QColor] | None) -> QGraphicsScene:
    """A small histogram: one bar per bin (rows of ``counts``), stacked by
    group (its columns) when ``colors`` are given."""
    scene = QGraphicsScene()
    scene.setSceneRect(0, 0, SCENE_WIDTH, SCENE_HEIGHT)
    if counts is None or not np.sum(counts):
        return scene
    counts = counts.reshape(len(counts), -1)
    colors = colors if colors and counts.shape[1] == len(colors) else [GRAY] * counts.shape[1]
    width = SCENE_WIDTH / len(counts)
    scale = (SCENE_HEIGHT - 4) / counts.sum(axis=1).max()
    for i, row in enumerate(counts):
        bottom = SCENE_HEIGHT
        for count, color in zip(row, colors, strict=False):
            height = count * scale
            rect = QGraphicsRectItem(QRectF(i * width + 2, bottom - height, width - 4, height))
            rect.setBrush(QBrush(color))
            rect.setPen(QPen(Qt.PenStyle.NoPen))
            scene.addItem(rect)
            bottom -= height
    return scene


class CloudStatisticsModel(FeatureStatisticsTableModel):
    """Orange's Feature Statistics model with numbers from the machine.

    :meth:`set_cloud_stats` lets Orange's model set up the columns from the
    placeholder table, then puts the machine's numbers (all rows) in its
    arrays. The small histograms come from the machine too.
    """

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self._histograms: dict[int, Any] = {}
        self._colors: list[QColor] | None = None
        self._scenes: dict[int, QGraphicsScene] = {}

    def set_cloud_stats(
        self, data: Table, n_rows: int, basic: dict, histograms: dict | None = None
    ) -> None:
        """Show the machine's ``basic`` statistics of ``data``'s ``n_rows`` rows."""
        super().set_data(data)
        self.beginResetModel()
        self.n_instances = n_rows
        stats = [basic["columns"][data.domain.index(var)] for var in self.variables]
        for attr, key in STAT_ARRAYS:
            setattr(self, attr, np.array([s[key] for s in stats], dtype=float))
        self._missing = np.array([s["missing"] for s in stats], dtype=int)
        self._histograms = (histograms or {}).get("columns", {})
        self._scenes = {}
        self.endResetModel()

    def set_colors(self, colors: list[QColor] | None) -> None:
        """Colours of the groups of the small histograms."""
        self._colors = colors
        self._scenes = {}

    def clear(self) -> None:
        self._histograms, self._scenes = {}, {}
        super().clear()

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if (
            index.isValid()
            and index.column() == self.Columns.DISTRIBUTION
            and role == Qt.ItemDataRole.DisplayRole
        ):
            return self._scene(self.mapToSourceRows(index.row()))
        return super().data(index, role)

    def _scene(self, row: int) -> QGraphicsScene | None:
        var = self.variables[row]
        if not var.is_primitive():
            return None
        if row not in self._scenes:
            column = self._histograms.get(self.domain.index(var), {})
            scene = histogram_scene(column.get("counts"), self._colors)
            scene.setParent(self)
            self._scenes[row] = scene
        return self._scenes[row]


class OWCloudFeatureStatistics(OWCloudViewer):
    """Basic statistics of the columns of a cloud table, computed on all rows."""

    name = "Cloud Feature Statistics"
    description = "Basic statistics of the columns of a cloud table, computed on all rows."
    icon = "icons/CloudFeatureStatistics.svg"
    priority = 60
    keywords = "cloud, feature, column, statistics"

    class Inputs:
        data = Input("Data", Table, doc="A cloud table (from Cloud File).")

    class Outputs:
        reduced_data = Output("Reduced Data", Table, default=True)
        statistics = Output("Statistics", Table)

    settingsHandler = DomainContextHandler()
    color_var = ContextSetting(None)
    sorting = Setting((0, Qt.SortOrder.AscendingOrder))
    selected_vars = ContextSetting([], schema_only=True)

    def __init__(self):
        super().__init__()
        self.model = CloudStatisticsModel(parent=self)
        self.table_view = FeatureStatisticsTableView(self.model, parent=self)
        self.table_view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table_view.selectionModel().selectionChanged.connect(self.on_select)
        self.table_view.horizontalHeader().sectionClicked.connect(self.on_header_click)
        self.mainArea.layout().addWidget(self.table_view)

        box = gui.vBox(self.controlArea, "Histograms")
        self.color_var_model = DomainModel(valid_types=DiscreteVariable, placeholder="None")
        gui.comboBox(
            box, self, "color_var", model=self.color_var_model, label="Color:",
            orientation=Qt.Orientation.Horizontal, callback=self.refresh,
        )  # fmt: skip
        gui.rubber(self.controlArea)

    @Inputs.data
    def set_data(self, data: Table | None) -> None:
        """Receive the Data input."""
        self.closeContext()
        self.selected_vars = []
        self.model.resetSorting()
        self.model.clear()
        self.Outputs.reduced_data.send(None)
        self.Outputs.statistics.send(None)
        if self.set_cloud_data(data):
            domain = self.data.domain
            self.color_var_model.set_domain(domain)
            class_var = domain.class_var
            self.color_var = class_var if class_var is not None and class_var.is_discrete else None
            self.openContext(self.data)
        else:
            self.color_var_model.set_domain(None)
            self.color_var = None
        self.refresh()

    def refresh(self) -> None:
        if self.data is None:
            return
        group = column_index(self.data.domain, self.color_var)
        self.fetch_stats(
            {
                "basic": StatsRequest(kind=StatsKind.BASIC),
                "histograms": StatsRequest(kind=StatsKind.DISTRIBUTION, group_by=group),
            },
            self.show_stats,
        )

    def show_stats(self, result: ViewerResult) -> None:
        """Show the machine's statistics and send the outputs."""
        color = self.color_var
        self.model.set_colors([QColor(*c) for c in color.colors] if color else None)
        self.model.set_cloud_stats(
            self.data, result.info.n_rows, result.stats["basic"], result.stats["histograms"]
        )
        self._restore_selection()
        self._restore_sorting()
        self.Outputs.statistics.send(self.model.get_statistics_table())
        self.commit()

    def _restore_selection(self) -> None:
        rows = [i for i, var in enumerate(self.model.variables) if var in self.selected_vars]
        selection = self.table_view.selectionModel()
        selection.clearSelection()
        for row in self.model.mapFromSourceRows(rows):
            selection.select(
                self.model.index(row, 0),
                selection.SelectionFlag.Select | selection.SelectionFlag.Rows,
            )

    def _restore_sorting(self) -> None:
        column, order = self.sorting
        if self.model.n_attributes and column < self.model.columnCount():
            self.model.sort(column, order)
            self.table_view.horizontalHeader().setSortIndicator(column, order)

    def on_header_click(self, *_: Any) -> None:
        """Remember the sort order."""
        self.sorting = (self.model.sortColumn(), self.model.sortOrder())

    def on_select(self, *_: Any) -> None:
        """Output the selected columns."""
        rows = [i.row() for i in self.table_view.selectionModel().selectedRows()]
        variables = self.model.variables
        self.selected_vars = [variables[i] for i in self.model.mapToSourceRows(rows)]
        self.commit()

    def commit(self) -> None:
        """Send the selected columns of the cloud table (a recorded step)."""
        if self.data is None or not self.selected_vars:
            self.Outputs.reduced_data.send(None)
        else:
            self.Outputs.reduced_data.send(self.data[:, self.selected_vars])


if __name__ == "__main__":  # pragma: no cover
    WidgetPreview(OWCloudFeatureStatistics).run()
