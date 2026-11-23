"""Cloud Distributions: Orange's Distributions for a cloud table.

The machine counts the values (categorical columns) or the rows per bin
(numeric columns) on all rows, split by a categorical column if chosen, and
fits the chosen curve to all rows. The bins are those Orange's Distributions
widget offers, computed from all rows; the "Histogram Data" output has the
same form as Orange's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pyqtgraph as pg
from AnyQt.QtCore import Qt
from AnyQt.QtGui import QColor
from Orange.data import ContinuousVariable, DiscreteVariable, Domain, Table
from Orange.widgets import gui
from Orange.widgets.settings import ContextSetting, DomainContextHandler, Setting
from Orange.widgets.utils.itemmodels import DomainModel
from Orange.widgets.utils.widgetpreview import WidgetPreview
from Orange.widgets.widget import Input, Output

from orangecontrib.cloud.protocol.tables import StatsKind, StatsRequest
from orangecontrib.cloud.widgets.viewer import OWCloudViewer, ViewerResult, column_index

__all__ = ["Bar", "OWCloudDistributions", "bars"]

#: Fitted curves: the name in the widget and the machine's ``fit``.
FITTERS = (
    ("None", None),
    ("Normal", "norm"),
    ("Beta", "beta"),
    ("Gamma", "gamma"),
    ("Rayleigh", "rayleigh"),
    ("Pareto", "pareto"),
    ("Exponential", "expon"),
    ("Kernel density", "kde"),
)
BLUE = QColor(0, 128, 255)


@dataclass
class Bar:
    """One bar: from ``x0`` to ``x1``, with a count per group."""

    x0: float
    x1: float
    freqs: np.ndarray
    desc: str


def interval_text(var: Any, x0: float, x1: float, where: tuple[bool, bool, bool, bool]) -> str:
    """A bin's description, as Orange's Distributions writes it; ``where`` is
    (first bin, last bin, one value per bin, cumulative)."""
    first, last, unique, cumulative = where
    sx0, sx1 = var.repr_val(x0), var.repr_val(x1)
    exact = type(var) is ContinuousVariable
    resolution = 10**-var.number_of_decimals if exact else 0
    if cumulative:
        return f"{var.name} < {sx1}"
    if first and last or unique:
        return f"{var.name} = {sx0}"
    if first:
        return f"{var.name} < {sx1}"
    if last:
        return f"{var.name} ≥ {sx0}"
    if sx0 == sx1 or x1 - x0 <= resolution:
        return f"{var.name} = {sx0}"
    return f"{sx0} ≤ {var.name} < {sx1}"


def bars(var: Any, column: dict, cumulative: bool = False) -> list[Bar]:
    """The bars of the machine's ``distribution`` of ``var`` (one column of
    the answer): one per value, or one per bin, cumulative if asked."""
    counts = column.get("counts")
    if counts is None:
        return []
    counts = counts.reshape(len(counts), -1)
    if var.is_discrete:
        return [Bar(i - 0.5, i + 0.5, counts[i], value) for i, value in enumerate(var.values)]
    edges = column["thresholds"]
    binnings, index = column.get("binnings"), column.get("binning")
    unique = index == 0 and binnings is not None and binnings[0].width is None
    freqs = np.cumsum(counts, axis=0) if cumulative else counts
    last = len(counts) - 1
    return [
        Bar(x0, x1, freqs[i], interval_text(var, x0, x1, (not i, i == last, unique, cumulative)))
        for i, (x0, x1) in enumerate(zip(edges, edges[1:], strict=False))
    ]


def histogram_table(bar_list: list[Bar], group: Any) -> Table:
    """The "Histogram Data" output, as Orange's Distributions makes it."""
    bin_var = DiscreteVariable("Bin", [bar.desc for bar in bar_list])
    count = ContinuousVariable("Count")
    if group is not None:
        rows = [[i, j, f] for i, bar in enumerate(bar_list) for j, f in enumerate(bar.freqs)]
        domain = Domain([bin_var, group, count])
    else:
        rows = [[i, bar.freqs[0]] for i, bar in enumerate(bar_list)]
        domain = Domain([bin_var, count])
    return Table.from_numpy(domain, np.array(rows, dtype=float).reshape(-1, len(domain)))


class OWCloudDistributions(OWCloudViewer):
    """Distribution of a column of a cloud table, computed on all rows."""

    name = "Cloud Distributions"
    description = "Distribution of a column of a cloud table, computed on all rows."
    icon = "icons/CloudDistributions.svg"
    priority = 61
    keywords = "cloud, distributions, histogram"

    class Inputs:
        data = Input("Data", Table, doc="A cloud table (from Cloud File).")

    class Outputs:
        histogram_data = Output("Histogram Data", Table)

    settingsHandler = DomainContextHandler()
    var = ContextSetting(None)
    cvar = ContextSetting(None)
    number_of_bins = Setting(5)
    fitted_distribution = Setting(0)
    kde_smoothing = Setting(10)
    stacked_columns = Setting(False)
    show_probs = Setting(False)
    cumulative_distr = Setting(False)

    def __init__(self):
        super().__init__()
        self.bars: list[Bar] = []
        self.binnings: list[Any] = []
        box = gui.vBox(self.controlArea, "Variable")
        self.var_model = DomainModel(valid_types=DomainModel.PRIMITIVE, separators=False)
        gui.comboBox(box, self, "var", model=self.var_model, callback=self._var_changed)
        self.continuous_box = box = gui.vBox(self.controlArea, "Distribution")
        gui.comboBox(
            box, self, "fitted_distribution", label="Fitted distribution",
            orientation=Qt.Orientation.Horizontal, items=[name for name, _ in FITTERS],
            callback=self.refresh,
        )  # fmt: skip
        slider = gui.hSlider(
            box, self, "number_of_bins", label="Bin width", minValue=0, maxValue=1,
            createLabel=False, callback=self.refresh,
        )  # fmt: skip
        slider.setTracking(False)
        self.bin_width_label = gui.widgetLabel(slider.box)
        gui.hSlider(
            box, self, "kde_smoothing", label="Smoothing", minValue=2, maxValue=20,
            callback=self.refresh,
        )  # fmt: skip
        box = gui.vBox(self.controlArea, "Columns")
        self.cvar_model = DomainModel(placeholder="(None)", valid_types=DiscreteVariable)
        gui.comboBox(
            box, self, "cvar", label="Split by", orientation=Qt.Orientation.Horizontal,
            model=self.cvar_model, callback=self.refresh,
        )  # fmt: skip
        gui.checkBox(box, self, "stacked_columns", "Stack columns", callback=self.replot)
        gui.checkBox(box, self, "show_probs", "Show probabilities", callback=self.replot)
        gui.checkBox(
            box, self, "cumulative_distr", "Show cumulative distribution", callback=self.replot
        )
        gui.rubber(self.controlArea)

        self.plot = pg.PlotWidget(background=None)
        self.plot.setMouseEnabled(False, False)
        self.plot.hideButtons()
        self.legend = self.plot.addLegend(offset=(-10, 10))
        self.mainArea.layout().addWidget(self.plot)

    # ------------------------------------------------------------------

    @Inputs.data
    def set_data(self, data: Table | None) -> None:
        """Receive the Data input."""
        self.closeContext()
        self.var = self.cvar = None
        self.binnings = []
        domain = data.domain if self.set_cloud_data(data) else None
        self.var_model.set_domain(domain)
        self.cvar_model.set_domain(domain)
        if domain is not None:
            if self.var_model:
                self.var = self.var_model[min(len(domain.class_vars), len(self.var_model) - 1)]
            if domain.has_discrete_class:
                self.cvar = domain.class_var
            self.openContext(domain)
        self.replot()
        self.refresh()

    def _var_changed(self) -> None:
        self.binnings = []
        self.refresh()

    def refresh(self) -> None:
        if self.data is None or self.var is None:
            return
        self.continuous_box.setDisabled(not self.var.is_continuous)
        domain = self.data.domain
        request = StatsRequest(
            kind=StatsKind.DISTRIBUTION,
            columns=[domain.index(self.var)],
            group_by=column_index(domain, self.cvar),
            binning=self.number_of_bins if self.var.is_continuous else None,
            fit=FITTERS[self.fitted_distribution][1] if self.var.is_continuous else None,
            smoothing=self.kde_smoothing,
        )
        self.fetch_stats({"distribution": request}, self.show_distribution)

    def show_distribution(self, result: ViewerResult) -> None:
        """Draw the machine's distribution and send the histogram."""
        column = next(iter(result.stats["distribution"]["columns"].values()))
        self.binnings = column.get("binnings") or []
        if self.binnings:
            slider = self.controls.number_of_bins
            slider.blockSignals(True)
            slider.setMaximum(len(self.binnings) - 1)
            self.number_of_bins = column["binning"]
            slider.blockSignals(False)
            self.bin_width_label.setText(self.binnings[self.number_of_bins].width_label)
        self.replot()

    @property
    def column(self) -> dict | None:
        """The shown column's distribution from the machine."""
        if self.result is None:
            return None
        return next(iter(self.result.stats["distribution"]["columns"].values()))

    # ------------------------------------------------------------------
    # Plot

    def replot(self) -> None:
        """Draw the bars and curves of the last result with the current options."""
        self.plot.clear()
        self.legend.clear()
        column, var = self.column, self.var
        self.bars = [] if column is None or var is None else bars(var, column, self._cumulative)
        if self.bars:
            colors = [QColor(*c) for c in self.cvar.colors] if self.cvar else [BLUE]
            self._draw_bars(colors)
            self._draw_curves(column, colors)
            self._set_axes(var)
        self.commit()

    @property
    def _cumulative(self) -> bool:
        return self.cumulative_distr and self.var is not None and self.var.is_continuous

    def _draw_bars(self, colors: list[QColor]) -> None:
        split = self.cvar is not None and len(colors) > 1
        for bar in self.bars:
            freqs = bar.freqs.astype(float)
            if split and self.show_probs and freqs.sum():
                freqs = freqs / freqs.sum()
            if split and not self.stacked_columns:
                width = (bar.x1 - bar.x0) * 0.9 / len(freqs)
                x = bar.x0 + (bar.x1 - bar.x0) * 0.05 + width * (np.arange(len(freqs)) + 0.5)
                bottoms = np.zeros(len(freqs))
            else:
                width, x = (bar.x1 - bar.x0) * 0.95, np.full(len(freqs), (bar.x0 + bar.x1) / 2)
                bottoms = np.concatenate([[0], np.cumsum(freqs)[:-1]])
            for xi, f, b, color in zip(x, freqs, bottoms, colors, strict=False):
                self.plot.addItem(
                    pg.BarGraphItem(x=[xi], height=[f], y0=[b], width=width, brush=color)
                )
        if split:
            for value, color in zip(self.cvar.values, colors, strict=False):
                self.legend.addItem(pg.ScatterPlotItem(symbol="s", brush=color, pen=None), value)

    def _draw_curves(self, column: dict, colors: list[QColor]) -> None:
        curves = column.get("curves")
        if not curves or self.show_probs and self.cvar is not None:
            return
        x = column["curve_x"]
        ys = [np.zeros_like(x) if c is None else np.asarray(c["y"], dtype=float) for c in curves]
        if self._cumulative:
            ys = [np.cumsum(y) for y in ys]
        # Like Orange's, the curves have their own scale: as high as the bars.
        top = max(float(bar.freqs.max()) for bar in self.bars)
        peak = max(float(y.max()) for y in ys) or 1.0
        for y, color in zip(ys, colors if len(curves) > 1 else [QColor(0, 0, 0)], strict=False):
            self.plot.addItem(pg.PlotCurveItem(x=x, y=y * top / peak, pen=pg.mkPen(color, width=3)))

    def _set_axes(self, var: Any) -> None:
        bottom = self.plot.getAxis("bottom")
        bottom.setLabel(var.name)
        bottom.setTicks([list(enumerate(var.values))] if var.is_discrete else None)
        probs = self.show_probs and self.cvar is not None
        self.plot.getAxis("left").setLabel(
            f"Probability of '{self.cvar.name}' at given '{var.name}'" if probs else "Frequency"
        )

    def commit(self) -> None:
        """Send the histogram of the shown bars."""
        if not self.bars:
            self.Outputs.histogram_data.send(None)
        else:
            self.Outputs.histogram_data.send(histogram_table(self.bars, self.cvar))


if __name__ == "__main__":  # pragma: no cover
    WidgetPreview(OWCloudDistributions).run()
