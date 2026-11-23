"""Orange's widget stack starts without a display (QT_QPA_PLATFORM=offscreen in CI)."""

from AnyQt.QtWidgets import QApplication
from Orange.widgets.widget import OWWidget


class OWSmoke(OWWidget):
    name = "Smoke"
    want_main_area = False


def test_widget_shows():
    app = QApplication.instance() or QApplication([])
    widget = OWSmoke()
    widget.show()
    app.processEvents()
    assert widget.isVisible()
    widget.close()
    widget.deleteLater()
