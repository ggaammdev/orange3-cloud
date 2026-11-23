"""The Cloud settings dialog, opened from the developer test widget, starts
and stops a (fake, in-process) machine and follows the session live; its
choices are saved in the settings store, which the session uses."""

import os
import time

from AnyQt.QtCore import QSettings, Qt
from AnyQt.QtTest import QTest
from AnyQt.QtWidgets import QApplication, QLabel

from orangecontrib.cloud.client.session import CloudSession, HandleStore
from orangecontrib.cloud.gui.settings_dialog import local_versions
from orangecontrib.cloud.settings import CloudSettings
from orangecontrib.cloud.widgets.dev.owcloudsettingstest import OWCloudSettingsTest


def spin(app, condition, timeout=30):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        app.processEvents()
        time.sleep(0.01)


def test_dialog_starts_and_stops_the_machine(fake, tmp_path):
    app = QApplication.instance() or QApplication([])
    CloudSettings(idle_timeout=10 * 60, max_lifetime=3 * 3600).save()
    session = CloudSession(fake, store=HandleStore(tmp_path / "machine.json"))
    states = []
    session.statusChanged.connect(states.append)

    widget = OWCloudSettingsTest()
    widget.session = session
    widget.show()
    QTest.mouseClick(widget.settings_button, Qt.MouseButton.LeftButton)
    dialog = widget.dialog
    assert dialog is not None and dialog.isVisible() and not dialog.isModal()
    assert dialog.idle_spin.value() == 10 and dialog.lifetime_spin.value() == 3
    assert dialog.state_label.text() == "Not running"
    assert dialog.start_button.isEnabled() and not dialog.stop_button.isEnabled()

    # Changes are saved at once, in the settings store, not in the widget.
    dialog.idle_spin.setValue(15)
    store = QSettings(os.environ["ORANGE3_CLOUD_SETTINGS_FILE"], QSettings.Format.IniFormat)
    assert int(store.value("orange3-cloud/idle_timeout")) == 15 * 60
    assert int(store.value("orange3-cloud/max_lifetime")) == 3 * 3600
    assert store.value("orange3-cloud/provider") == "local"

    QTest.mouseClick(dialog.start_button, Qt.MouseButton.LeftButton)
    assert not dialog.start_button.isEnabled()  # the GUI didn't wait
    spin(app, lambda: dialog.state_label.text() == "Running")
    assert states[:3] == ["starting", "connecting", "connected"]
    # The session started the machine with the settings' limits.
    assert fake.start_kwargs["idle_timeout"] == 15 * 60
    assert fake.start_kwargs["max_lifetime"] == 3 * 3600

    # Connection details, without the token; versions on both sides match.
    assert dialog.detail_labels["url"].text() == session.handle.url
    assert dialog.detail_labels["certificate"].text().startswith("SHA-256 ")
    # Price and cost so far (the fake machine is free; then with a price).
    assert dialog.detail_labels["price"].text() == "free (this computer)"
    session.machine_costs.prices[session.handle.machine_id] = 0.78
    dialog._update_cost()
    assert dialog.detail_labels["price"].text().startswith("$0.78/h")
    assert "$" in dialog.detail_labels["cost"].text()  # e.g. "under $0.0001"
    session.machine_costs.prices.clear()
    texts = " ".join(label.text() for label in dialog.findChildren(QLabel))
    assert session.handle.token not in texts
    view = dialog.versions_view
    rows = {
        view.topLevelItem(i).text(0): view.topLevelItem(i) for i in range(view.topLevelItemCount())
    }
    mine = local_versions()
    assert rows["orange3-cloud"].text(2) == mine["orange3-cloud"]
    assert rows["Python"].text(2) == mine["python"]
    assert "same versions" in dialog.versions_label.text()
    assert dialog.stop_button.isEnabled() and not dialog.start_button.isEnabled()

    QTest.mouseClick(dialog.stop_button, Qt.MouseButton.LeftButton)
    spin(app, lambda: dialog.state_label.text() == "Not running")
    assert states[-2:] == ["stopping", "disconnected"]
    assert fake.status().state.value == "not_found"
    assert dialog.detail_labels["url"].text() == "—"
    assert dialog.start_button.isEnabled()

    dialog.close()
    widget.close()
    session.shutdown()


def test_hosted_interest_button(fake, tmp_path, monkeypatch):
    """The "Notify me" button opens the roadmap issue, or the page in the settings
    store; without either it's disabled."""
    from orangecontrib.cloud import settings
    from orangecontrib.cloud.gui import settings_dialog

    app = QApplication.instance() or QApplication([])
    opened = []
    monkeypatch.setattr(settings_dialog.QDesktopServices, "openUrl", opened.append)
    session = CloudSession(fake, store=HandleStore(tmp_path / "machine.json"))

    assert settings.hosted_interest_url().startswith(
        "https://github.com/ggaammdev/orange3-cloud/issues/"
    )
    monkeypatch.setattr(settings, "HOSTED_INTEREST_URL", "")
    dialog = settings_dialog.CloudSettingsDialog(None, session)
    assert not dialog.hosted_button.isEnabled()
    assert "no sign-up page" in dialog.hosted_button.toolTip()
    dialog.close()

    url = "https://example.org/hosted"
    store = QSettings(os.environ["ORANGE3_CLOUD_SETTINGS_FILE"], QSettings.Format.IniFormat)
    store.setValue("orange3-cloud/hosted_interest_url", url)
    store.sync()
    dialog = settings_dialog.CloudSettingsDialog(None, session)
    dialog.show()
    assert dialog.hosted_button.isEnabled()
    QTest.mouseClick(dialog.hosted_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert [u.toString() for u in opened] == [url]
    dialog.close()
    session.shutdown()


def test_unreadable_machine_record(fake, tmp_path):
    """An unreadable record of the last machine doesn't stop the add-on: the
    dialog names the file and says the machine may be left over, until the
    next machine's record replaces it."""
    from orangecontrib.cloud.gui.settings_dialog import CloudSettingsDialog

    app = QApplication.instance() or QApplication([])
    path = tmp_path / "machine.json"
    path.write_text('{"provider": "gcp", "name": "orange3-cloud-1')  # cut off
    session = CloudSession(fake, store=HandleStore(path))
    assert session.reattach(wait=True) is None  # nothing to reconnect to, no error
    assert session.store.load_error.startswith("JSONDecodeError")

    dialog = CloudSettingsDialog(None, session)
    dialog.show()
    text = dialog.record_label.text()
    assert not dialog.record_label.isHidden()
    assert str(path) in text and "JSONDecodeError" in text
    assert "another machine to delete" in text
    assert dialog.start_button.isEnabled()

    QTest.mouseClick(dialog.start_button, Qt.MouseButton.LeftButton)
    spin(app, lambda: dialog.state_label.text() == "Running")
    assert dialog.record_label.isHidden() and session.store.load_error is None
    session.stop_machine(wait=True)
    dialog.close()
    session.shutdown()


def test_leftover_check_outlives_the_dialog(fake, tmp_path):
    """A leftover check that ends after its dialog was deleted (a slow cloud
    while Orange quits) leaves the deleted dialog alone; a live dialog shows
    the result."""
    import threading

    from AnyQt.sip import delete

    from orangecontrib.cloud.gui.settings_dialog import CloudSettingsDialog

    app = QApplication.instance() or QApplication([])
    session = CloudSession(fake, store=HandleStore(tmp_path / "machine.json"))

    def from_thread(deliver):
        thread = threading.Thread(target=deliver, args=(["orange3-cloud-0123"],))
        thread.start()
        thread.join()

    dialog = CloudSettingsDialog(None, session)
    deliver = dialog._leftovers_receiver()
    delete(dialog)
    from_thread(deliver)
    app.processEvents()  # the result reaches the GUI thread, and no dialog

    dialog = CloudSettingsDialog(None, session)
    from_thread(dialog._leftovers_receiver())
    spin(app, lambda: dialog.leftovers == ["orange3-cloud-0123"])
    dialog.close()
    session.shutdown()


def test_long_setup_results_show_whole(fake, tmp_path):
    """The provider's page scrolls inside the dialog, and the dialog grows
    for long setup check results as far as the screen allows."""
    from orangecontrib.cloud.client.providers.setup_checks import Check, Status
    from orangecontrib.cloud.gui.settings_dialog import CloudSettingsDialog

    app = QApplication.instance() or QApplication([])
    CloudSettings(provider="aws").save()
    session = CloudSession(fake, store=HandleStore(tmp_path / "machine.json"))
    dialog = CloudSettingsDialog(None, session)
    dialog.show()
    page = dialog.pages["aws"]
    page.shutdown()  # no background checks: this test shows its own results
    app.processEvents()
    dialog.resize(dialog.width(), 500)
    long_text = "You can't manage machines. Missing: " + ", ".join(["ec2:SomeAction"] * 60)
    page._show_checks([Check("permissions", "Permissions", Status.FAILED, detail=long_text)])
    page.setupChanged.emit()
    spin(app, lambda: dialog.height() > 500, timeout=5)
    scroll = dialog.provider_scroll
    assert scroll.widget() is dialog.provider_pages
    bottom = page.setup_label.mapTo(dialog.provider_pages, page.setup_label.rect().bottomLeft())
    assert bottom.y() <= scroll.viewport().height() + scroll.verticalScrollBar().maximum()
    dialog.close()
