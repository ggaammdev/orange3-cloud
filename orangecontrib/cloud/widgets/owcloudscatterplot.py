"""Cloud Scatter Plot: two numeric columns of a cloud table against each other.

The machine counts all rows on a grid (``histogram2d``), and the widget
draws that density: darker cells hold more rows, and with a colour column
each cell takes the colour of its most common value. A table small enough
(at most :data:`MAX_POINTS` rows) comes whole from the machine instead, and
every row is drawn as a point.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pyqtgraph as pg
from AnyQt.QtCore import QRectF, Qt
from AnyQt.QtGui import QColor
from Orange.data import ContinuousVariable, DiscreteVariable, Table
from Orange.widgets import gui
from Orange.widgets.settings import ContextSetting, DomainContextHandler, Setting
from Orange.widgets.utils.itemmodels import DomainModel
from Orange.widgets.utils.widgetpreview import WidgetPreview
from Orange.widgets.widget import Input, Msg

from orangecontrib.cloud.protocol.tables import StatsKind, StatsRequest
from orangecontrib.cloud.widgets.viewer import OWCloudViewer, ViewerResult, column_index

__all__ = ["MAX_POINTS", "OWCloudScatterPlot", "density_image"]

#: Tables with at most this many rows are drawn as points.
MAX_POINTS = 20_000
DENSITY_COLOR = QColor(0, 80, 200)
GRAY = QColor(128, 128, 128)


def density_image(
    counts: np.ndarray, group_counts: np.ndarray | None, colors: list[QColor]
) -> np.ndarray:
    """An RGBA image of a 2D histogram (x first): the opacity grows with the
    logarithm of the count; with ``group_counts``, a cell has the colour of
    its most common group."""
    image = np.zeros((*counts.shape, 4), dtype=np.ubyte)
    top = np.log1p(counts.max()) or 1.0
    image[..., 3] = (np.log1p(counts) / top * 230).astype(np.ubyte)
    image[..., 3][counts > 0] = np.maximum(image[..., 3][counts > 0], 25)
    if group_counts is None or not colors:
        rgb = np.array(DENSITY_COLOR.getRgb()[:3])
        image[..., :3] = rgb
    else:
        palette = np.array([c.getRgb()[:3] for c in colors], dtype=np.ubyte)
        image[..., :3] = palette[np.argmax(group_counts, axis=0)]
    return image


class OWCloudScatterPlot(OWCloudViewer):
    """Scatter plot of a cloud table: the density of all rows, or every point."""

    name = "Cloud Scatter Plot"
    description = "Scatter plot of a cloud table: the density of all rows, or every point."
    icon = "icons/CloudScatterPlot.svg"
    priority = 63
    keywords = "cloud, scatter plot, density, histogram"

    class Inputs:
        data = Input("Data", Table, doc="A cloud table (from Cloud File).")

    class Warning(OWCloudViewer.Warning):
        no_columns = Msg("The table needs at least one numeric column.")

    settingsHandler = DomainContextHandler()
    attr_x = ContextSetting(None)
    attr_y = ContextSetting(None)
    attr_color = ContextSetting(None)
    resolution = Setting(100)

    def __init__(self):
        super().__init__()
        #: The drawn points (x, y), or ``None`` when drawing the density.
        self.points: tuple[np.ndarray, np.ndarray] | None = None
        #: The drawn 2D histogram (x first), or ``None`` when drawing points.
        self.density: np.ndarray | None = None
        box = gui.vBox(self.controlArea, "Axes")
        self.xy_model = DomainModel(valid_types=ContinuousVariable, separators=False)
        for attr, label in (("attr_x", "X:"), ("attr_y", "Y:")):
            gui.comboBox(
                box, self, attr, model=self.xy_model, label=label,
                orientation=Qt.Orientation.Horizontal, callback=self.refresh,
            )  # fmt: skip
        box = gui.vBox(self.controlArea, "Attributes")
        self.color_model = DomainModel(placeholder="(Same color)", valid_types=DiscreteVariable)
        gui.comboBox(
            box, self, "attr_color", model=self.color_model, label="Color:",
            orientation=Qt.Orientation.Horizontal, callback=self.refresh,
        )  # fmt: skip
        gui.spin(
            box, self, "resolution", 20, 400, step=10, label="Density grid:",
            callback=self.refresh, keyboardTracking=False,
        )  # fmt: skip
        gui.rubber(self.controlArea)

        self.plot = pg.PlotWidget(background=None)
        self.plot.hideButtons()
        self.legend = self.plot.addLegend(offset=(-10, 10))
        self.mainArea.layout().addWidget(self.plot)

    @Inputs.data
    def set_data(self, data: Table | None) -> None:
        """Receive the Data input."""
        self.closeContext()
        self.Warning.no_columns.clear()
        self.attr_x = self.attr_y = self.attr_color = None
        domain = data.domain if self.set_cloud_data(data) else None
        self.xy_model.set_domain(domain)
        self.color_model.set_domain(domain)
        if domain is not None:
            numeric = list(self.xy_model)
            if not numeric:
                self.Warning.no_columns()
            else:
                self.attr_x, self.attr_y = numeric[0], numeric[min(1, len(numeric) - 1)]
            if domain.has_discrete_class:
                self.attr_color = domain.class_var
            self.openContext(domain)
        self.replot()
        self.refresh()

    def refresh(self) -> None:
        if self.data is None or self.attr_x is None or self.attr_y is None:
            return
        domain = self.data.domain
        request = StatsRequest(
            kind=StatsKind.HISTOGRAM2D,
            columns=[domain.index(self.attr_x), domain.index(self.attr_y)],
            group_by=column_index(domain, self.attr_color),
            grid=self.resolution,
        )
        self.fetch_stats({"density": request}, self.show_result, rows_up_to=MAX_POINTS)

    def show_result(self, result: ViewerResult) -> None:
        """Draw what the machine sent."""
        self.replot()

    # ------------------------------------------------------------------

    def _colors(self) -> list[QColor]:
        color = self.attr_color
        return [QColor(*c) for c in color.colors] if color is not None else []

    def replot(self) -> None:
        """Draw the last result: every point, or the density."""
        self.plot.clear()
        self.legend.clear()
        self.points = self.density = None
        result = self.result
        if result is None or self.attr_x is None or self.attr_y is None:
            return
        if result.rows is not None:
            self._draw_points(result.rows)
        else:
            self._draw_density(result.stats["density"])
        for value, color in zip(
            self.attr_color.values if self.attr_color else [], self._colors(), strict=False
        ):
            self.legend.addItem(pg.ScatterPlotItem(brush=color, pen=None, size=8), value)
        self.plot.getAxis("bottom").setLabel(self.attr_x.name)
        self.plot.getAxis("left").setLabel(self.attr_y.name)

    def _draw_points(self, rows: Table) -> None:
        x, y = rows.get_column(self.attr_x), rows.get_column(self.attr_y)
        valid = np.isfinite(x) & np.isfinite(y)
        self.points = (x[valid], y[valid])
        brushes: Any = pg.mkBrush(DENSITY_COLOR)
        if self.attr_color is not None:
            codes = rows.get_column(self.attr_color)[valid]
            palette = [pg.mkBrush(c) for c in self._colors()]
            gray = pg.mkBrush(GRAY)
            brushes = [palette[int(c)] if np.isfinite(c) else gray for c in codes]
        self.plot.addItem(
            pg.ScatterPlotItem(x=self.points[0], y=self.points[1], size=6, pen=None, brush=brushes)
        )

    def _draw_density(self, stats: dict) -> None:
        counts = stats.get("counts")
        if counts is None:
            return
        self.density = counts
        image = density_image(counts, stats.get("group_counts"), self._colors())
        item = pg.ImageItem(image, axisOrder="col-major")  # image[x, y]
        x_edges, y_edges = stats["x_edges"], stats["y_edges"]
        item.setRect(
            QRectF(x_edges[0], y_edges[0], x_edges[-1] - x_edges[0], y_edges[-1] - y_edges[0])
        )
        self.plot.addItem(item)


if __name__ == "__main__":  # pragma: no cover
    WidgetPreview(OWCloudScatterPlot).run()
