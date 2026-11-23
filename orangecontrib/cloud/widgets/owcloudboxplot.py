"""Cloud Box Plot: Orange's Box Plot for a cloud table.

For a numeric column, the machine computes each group's count, extremes,
mean, variance and quartiles on all rows (``quantiles``); for a categorical
one, the counts per value and group (``distribution``). The widget draws
them and writes the same test (Student's t, ANOVA or χ²) that Orange's Box
Plot writes, computed from those numbers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pyqtgraph as pg
import scipy.special
from AnyQt.QtCore import Qt
from AnyQt.QtGui import QColor, QFontMetrics
from AnyQt.QtWidgets import QLabel
from Orange.data import DiscreteVariable, Table
from Orange.widgets import gui
from Orange.widgets.settings import ContextSetting, DomainContextHandler, Setting
from Orange.widgets.utils.itemmodels import DomainModel
from Orange.widgets.utils.widgetpreview import WidgetPreview
from Orange.widgets.widget import Input
from scipy.stats import chi2_contingency

from orangecontrib.cloud.protocol.tables import StatsKind, StatsRequest
from orangecontrib.cloud.widgets.viewer import OWCloudViewer, column_index

__all__ = ["Box", "OWCloudBoxPlot", "chi_square", "compare_text"]

COMPARE_NONE, COMPARE_MEDIANS, COMPARE_MEANS = 0, 1, 2
BOX_COLOR = QColor(0x33, 0x88, 0xFF, 0xC0)
MEDIAN_COLOR = QColor(0xFF, 0xFF, 0x00)
MEAN_COLOR = QColor(0x33, 0x00, 0xFF)


@dataclass
class Box:
    """One group's numbers, as Orange's Box Plot's ``BoxData``."""

    label: str
    n: int
    a_min: float
    a_max: float
    mean: float
    var: float
    q25: float
    median: float
    q75: float

    @property
    def dev(self) -> float:
        """Standard deviation."""
        return math.sqrt(self.var)


def boxes(groups: list[dict], labels: list[str]) -> list[Box]:
    """The boxes of the groups that have values, from the machine's ``quantiles``."""
    return [
        Box(label, g["n"], g["min"], g["max"], g["mean"], g["var"], g["q25"], g["median"], g["q75"])
        for g, label in zip(groups, labels, strict=False)
        if g["n"] > 0
    ]


def _ttest(d1: Box, d2: Box) -> tuple[float, float]:
    if d1.n < 2 or d2.n < 2:
        return np.nan, np.nan
    pooled_var = d1.var / d1.n + d2.var / d2.n
    if pooled_var == 0 or np.isnan(pooled_var):
        return np.nan, np.nan
    df = pooled_var**2 / ((d1.var / d1.n) ** 2 / (d1.n - 1) + (d2.var / d2.n) ** 2 / (d2.n - 1))
    t = abs(d1.mean - d2.mean) / math.sqrt(pooled_var)
    return t, 2 * (1 - scipy.special.stdtr(df, t))


def _anova(stats: list[Box]) -> tuple[float, float]:
    n = sum(s.n for s in stats)
    grand_avg = sum(s.n * s.mean for s in stats) / n
    var_between = sum(s.n * (s.mean - grand_avg) ** 2 for s in stats)
    var_within = sum(s.n * s.var for s in stats)
    df_between, df_within = len(stats) - 1, n - len(stats)
    if var_within == 0 or df_within == 0 or df_between == 0:
        return np.nan, np.nan
    f = (var_between / df_between) / (var_within / df_within)
    return f, 1 - scipy.special.fdtr(df_between, df_within, f)


def compare_text(stats: list[Box], compare: int, n_rows: int) -> str:
    """The test that Orange's Box Plot writes for a numeric column."""
    if compare == COMPARE_NONE or len(stats) < 2:
        return ""
    if any(s.n <= 1 for s in stats):
        return "At least one group has just one instance, cannot compute significance"
    if compare == COMPARE_MEDIANS:  # Orange's Box Plot writes no test for medians
        return ""
    if len(stats) == 2:
        t, p = _ttest(*stats)
        return "" if np.isnan(t) else f"Student's t: {t:.3f} (p={p:.3f}, N={n_rows})"
    f, p = _anova(stats)
    return "" if np.isnan(f) else f"ANOVA: {f:.3f} (p={p:.3f}, N={n_rows})"


def chi_square(counts: np.ndarray) -> str:
    """The χ² test that Orange's Box Plot writes for a categorical column;
    ``counts`` are per value (rows) and group (columns)."""
    observed = counts[counts.sum(axis=1) != 0, :]
    observed = observed[:, observed.sum(axis=0) != 0]
    if min(observed.shape) < 2:
        chi, p, dof = 0, 2, 0
    else:
        chi, p, dof = chi2_contingency(observed)[:3]
    return "" if np.isnan(p) else f"χ²: {chi:.2f} (p={p:.3f}, dof={dof})"


class OWCloudBoxPlot(OWCloudViewer):
    """Box plots of a column of a cloud table, computed on all rows."""

    name = "Cloud Box Plot"
    description = "Box plots of a column of a cloud table, computed on all rows."
    icon = "icons/CloudBoxPlot.svg"
    priority = 62
    keywords = "cloud, box plot, whisker"

    class Inputs:
        data = Input("Data", Table, doc="A cloud table (from Cloud File).")

    settingsHandler = DomainContextHandler()
    attribute = ContextSetting(None)
    group_var = ContextSetting(None)
    compare = Setting(COMPARE_MEANS)
    stretched = Setting(True)

    def __init__(self):
        super().__init__()
        #: Boxes (numeric column) or rows of counts (categorical) shown.
        self.boxes: list[Box] = []
        self.rows: list[tuple[str, np.ndarray]] = []
        self.stat_test = ""
        box = gui.vBox(self.controlArea, "Variable")
        self.attr_model = DomainModel(valid_types=DomainModel.PRIMITIVE, separators=False)
        gui.comboBox(box, self, "attribute", model=self.attr_model, callback=self.refresh)
        box = gui.vBox(self.controlArea, "Subgroups")
        self.group_model = DomainModel(placeholder="None", valid_types=DiscreteVariable)
        gui.comboBox(box, self, "group_var", model=self.group_model, callback=self.refresh)
        box = gui.vBox(self.controlArea, "Display")
        gui.radioButtons(
            box, self, "compare",
            btnLabels=["No comparison", "Compare medians", "Compare means"],
            callback=self.replot,
        )  # fmt: skip
        gui.checkBox(box, self, "stretched", "Stretch bars", callback=self.replot)
        gui.rubber(self.controlArea)

        self.test_label = QLabel()
        self.test_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.mainArea.layout().addWidget(self.test_label)
        self.plot = pg.PlotWidget(background=None)
        self.plot.setMouseEnabled(False, False)
        self.plot.hideButtons()
        self.mainArea.layout().addWidget(self.plot)

    @Inputs.data
    def set_data(self, data: Table | None) -> None:
        """Receive the Data input."""
        self.closeContext()
        self.attribute = self.group_var = None
        domain = data.domain if self.set_cloud_data(data) else None
        self.attr_model.set_domain(domain)
        self.group_model.set_domain(domain)
        if domain is not None:
            n_class = len(domain.class_vars)
            if self.attr_model:
                self.attribute = self.attr_model[min(n_class, len(self.attr_model) - 1)]
            if domain.has_discrete_class:
                self.group_var = domain.class_var
            self.openContext(domain)
        self.replot()
        self.refresh()

    def refresh(self) -> None:
        if self.data is None or self.attribute is None:
            return
        domain = self.data.domain
        kind = StatsKind.QUANTILES if self.attribute.is_continuous else StatsKind.DISTRIBUTION
        request = StatsRequest(
            kind=kind,
            columns=[domain.index(self.attribute)],
            group_by=column_index(domain, self.group_var),
        )
        self.fetch_stats({"stats": request}, lambda _: self.replot())

    # ------------------------------------------------------------------

    def _compute(self) -> None:
        """Boxes or rows of counts and the test, from the last result."""
        self.boxes, self.rows, self.stat_test = [], [], ""
        attr, group = self.attribute, self.group_var
        if self.result is None or attr is None:
            return
        column = next(iter(self.result.stats["stats"]["columns"].values()))
        n_rows = self.result.info.n_rows
        if attr.is_continuous:
            labels = list(group.values) if group is not None else [""]
            self.boxes = boxes(column["groups"], labels)
            self.stat_test = compare_text(self.boxes, self.compare, n_rows)
        elif group is None:
            self.rows = [("", column["counts"])]
        else:
            counts = column["counts"]
            rows = [
                *zip(group.values, counts.T, strict=False),
                (f"missing '{group.name}'", column["group_missing"]),
            ]
            self.rows = [(label, c) for label, c in rows if c.sum() > 0]
            self.stat_test = chi_square(counts)

    def replot(self) -> None:
        """Draw the last result with the current options."""
        self._compute()
        self.plot.clear()
        self.test_label.setText(self.stat_test)
        # The first group at the top.
        top = max(len(self.boxes), len(self.rows)) - 1
        for i, box in enumerate(self.boxes):
            self._draw_box(top - i, box)
        for i, (_, counts) in enumerate(self.rows):
            self._draw_counts(top - i, counts)
        labels = [box.label for box in self.boxes] or [
            f"{label} ({int(counts.sum()):,})" if label else f"{int(counts.sum()):,}"
            for label, counts in self.rows
        ]
        axis = self.plot.getAxis("left")
        axis.setTicks([[(top - i, text) for i, text in enumerate(labels)]])
        # Wide enough for the longest label (pyqtgraph hides labels that don't fit).
        metrics = QFontMetrics(axis.style["tickFont"] or self.font())
        axis.setWidth(max([metrics.horizontalAdvance(text) for text in labels], default=0) + 20)
        self.plot.getAxis("bottom").setLabel(self.attribute.name if self.attribute else "")

    def _draw_box(self, y: float, box: Box) -> None:
        plot = self.plot
        plot.addItem(pg.PlotCurveItem([box.a_min, box.a_max], [y, y], pen=pg.mkPen("k", width=1)))
        plot.addItem(
            pg.BarGraphItem(
                x0=[box.q25], x1=[box.q75], y=[y], height=0.5, brush=BOX_COLOR, pen=None
            )
        )
        for x, color, height in ((box.median, MEDIAN_COLOR, 0.5), (box.mean, MEAN_COLOR, 0.7)):
            plot.addItem(
                pg.PlotCurveItem(
                    [x, x], [y - height / 2, y + height / 2], pen=pg.mkPen(color, width=2)
                )
            )
        text = pg.TextItem(
            f"{self.attribute.str_val(box.mean)} ± {self.attribute.str_val(box.dev)}",
            anchor=(0.5, 1),
            color="k",
        )
        text.setPos(box.mean, y + 0.4)
        plot.addItem(text)

    def _draw_counts(self, y: float, counts: np.ndarray) -> None:
        total = counts.sum()
        widths = counts / total if self.stretched else counts
        left = np.concatenate([[0], np.cumsum(widths)[:-1]])
        for x0, width, count, color in zip(
            left, widths, counts, self.attribute.colors, strict=False
        ):
            if count:
                self.plot.addItem(
                    pg.BarGraphItem(x0=[x0], width=[width], y=[y], height=0.6, brush=QColor(*color))
                )


if __name__ == "__main__":  # pragma: no cover
    WidgetPreview(OWCloudBoxPlot).run()
