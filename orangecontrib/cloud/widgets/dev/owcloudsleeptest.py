"""A developer test widget that runs a ``sleep`` job on the cloud machine,
through :class:`~orangecontrib.cloud.widgets.base.OWCloudBaseWidget`.

Run it with ``python -m orangecontrib.cloud.widgets.dev.owcloudsleeptest``.
"""

from __future__ import annotations

from Orange.widgets import gui
from Orange.widgets.settings import Setting
from Orange.widgets.utils.widgetpreview import WidgetPreview

from orangecontrib.cloud.protocol.jobs import JobKind, JobResult
from orangecontrib.cloud.widgets.base import OWCloudBaseWidget


class OWCloudSleepTest(OWCloudBaseWidget):
    name = "Cloud Sleep Test"
    description = "Developer test: runs a sleep job on the cloud machine."
    want_main_area = False
    resizing_enabled = False

    seconds: float = Setting(3.0)
    steps: int = Setting(10)
    fail: bool = Setting(False)

    def __init__(self):
        super().__init__()
        #: Results of the finished runs, newest last.
        self.results: list[JobResult] = []
        box = gui.vBox(self.controlArea, "Sleep job")
        gui.doubleSpin(box, self, "seconds", 0, 600, step=0.5, label="Seconds:")
        gui.spin(box, self, "steps", 1, 1000, label="Steps:")
        gui.checkBox(box, self, "fail", "Fail at the end")
        self.run_button = gui.button(box, self, "Run", callback=self.run, autoDefault=False)
        self.cancel_button = gui.button(
            box, self, "Cancel", callback=self.cancel, autoDefault=False
        )

    def run(self) -> bool:
        return self.run_cloud_job(
            JobKind.SLEEP, params={"seconds": self.seconds, "steps": self.steps, "fail": self.fail}
        )

    def on_cloud_result(self, result: JobResult) -> None:
        self.results.append(result)


if __name__ == "__main__":  # pragma: no cover
    WidgetPreview(OWCloudSleepTest).run()
