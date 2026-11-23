"""A developer test widget that opens the Cloud settings dialog.

Run it with ``python -m orangecontrib.cloud.widgets.dev.owcloudsettingstest``.
"""

from __future__ import annotations

from Orange.widgets import gui
from Orange.widgets.utils.widgetpreview import WidgetPreview
from Orange.widgets.widget import OWWidget

from orangecontrib.cloud.client.session import CloudSession
from orangecontrib.cloud.gui.settings_dialog import CloudSettingsDialog, show_settings_dialog


class OWCloudSettingsTest(OWWidget):
    name = "Cloud Settings Test"
    description = "Developer test: opens the Cloud settings dialog."
    want_main_area = False
    resizing_enabled = False

    def __init__(self):
        super().__init__()
        #: The session the dialog uses; default the shared one.
        self.session: CloudSession | None = None
        self.dialog: CloudSettingsDialog | None = None
        box = gui.vBox(self.controlArea, "Cloud")
        gui.widgetLabel(box, "Start and stop the cloud machine in the Cloud settings.")
        self.settings_button = gui.button(
            box, self, "Cloud settings…", callback=self.open_settings, autoDefault=False
        )

    def open_settings(self) -> None:
        self.dialog = show_settings_dialog(self, session=self.session)


if __name__ == "__main__":  # pragma: no cover
    WidgetPreview(OWCloudSettingsTest).run()
