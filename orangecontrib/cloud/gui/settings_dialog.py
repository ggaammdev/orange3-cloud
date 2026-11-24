"""The Cloud settings dialog: where the machine runs, its time limits, and
starting and stopping it.

The choice of where the machine runs lists the registered providers
(:func:`~orangecontrib.cloud.client.providers.registry.registered_providers`),
each with its page (:mod:`~orangecontrib.cloud.gui.provider_page`), so a new
cloud needs no change here.

:func:`show_settings_dialog` opens the one shared, non-modal dialog (Cloud
widgets call it from their "Cloud settings…" button). The dialog uses the
shared :class:`~orangecontrib.cloud.client.session.CloudSession`; starting
and stopping run on the session's background thread, and the dialog follows
the session's signals, so the GUI never waits.

The choices are saved in Orange's settings
(:class:`~orangecontrib.cloud.settings.CloudSettings`) as soon as they
change, never in workflow files. The machine's token is never shown.
"""

from __future__ import annotations

import hashlib
import html
import platform
import ssl
import threading
import time
from collections.abc import Callable
from importlib import metadata

from AnyQt.QtCore import QSize, Qt, QTimer, QUrl
from AnyQt.QtCore import pyqtSlot as Slot
from AnyQt.QtGui import QBrush, QColor, QDesktopServices, QFont, QHideEvent, QShowEvent
from AnyQt.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from AnyQt.sip import isdeleted

from orangecontrib.cloud.client.costs import format_price, machine_seconds
from orangecontrib.cloud.client.providers.registry import registered_providers
from orangecontrib.cloud.client.session import CloudSession, SessionState
from orangecontrib.cloud.gui.provider_page import ProviderPage, make_page
from orangecontrib.cloud.gui.relay import receiver
from orangecontrib.cloud.gui.stop_machine import can_stop, cost_so_far, session_machine
from orangecontrib.cloud.protocol.machine import InfoResponse
from orangecontrib.cloud.settings import CloudSettings, hosted_interest_url

__all__ = ["CloudSettingsDialog", "show_settings_dialog"]

#: Packages compared in the versions table: (PEP 503 name, label).
VERSION_PACKAGES = (
    ("python", "Python"),
    ("orange3", "Orange"),
    ("numpy", "NumPy"),
    ("scipy", "SciPy"),
    ("scikit-learn", "scikit-learn"),
    ("pandas", "pandas"),
    ("orange3-cloud", "orange3-cloud"),
)

#: Session state -> text for the user.
STATE_TEXT = {
    SessionState.DISCONNECTED: "Not running",
    SessionState.STARTING: "Starting",
    SessionState.CONNECTING: "Connecting",
    SessionState.CONNECTED: "Running",
    SessionState.RECONNECTING: "Reconnecting",
    SessionState.STOPPING: "Stopping",
    SessionState.ERROR: "Error",
}

_BUSY = (SessionState.STARTING, SessionState.CONNECTING, SessionState.STOPPING)
_MISMATCH = QColor("#c62828")


def local_versions() -> dict[str, str | None]:
    """Versions on this computer, by PEP 503 name (``None``: not installed)."""
    versions: dict[str, str | None] = {"python": platform.python_version()}
    for name, _ in VERSION_PACKAGES:
        if name == "python":
            continue
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def machine_versions(info: InfoResponse) -> dict[str, str | None]:
    """Versions on the machine, from its ``/v1/info``."""
    versions: dict[str, str | None] = {"python": info.python_version}
    for name, _ in VERSION_PACKAGES:
        if name != "python":
            versions[name] = info.packages.get(name)
    versions["orange3-cloud"] = info.server_version or versions.get("orange3-cloud")
    return versions


def python_substituted(info: InfoResponse | None, mine: str | None) -> bool:
    """Whether the machine runs another release of this computer's Python minor
    version because uv has no build of the exact one (e.g. 3.11.15 for 3.11.2).
    Releases within a minor version only fix bugs, so that isn't a mismatch."""
    if info is None or mine is None or info.python_version_requested != mine:
        return False
    return info.python_version.split(".")[:2] == mine.split(".")[:2]


def certificate_fingerprint(cert_pem: str) -> str:
    """SHA-256 fingerprint of a PEM certificate, as ``AB:CD:...``."""
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert_pem)).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def format_duration(seconds: float) -> str:
    """A duration as hours and minutes, or minutes and seconds."""
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min {secs} s"
    return f"{secs} s"


class _PageScroll(QScrollArea):
    """A scroll area as tall as its page would like (Qt's default is about
    24 lines), so it scrolls only when the dialog can't be that tall."""

    def sizeHint(self) -> QSize:
        page = self.widget()
        if page is None:
            return super().sizeHint()
        hint = page.sizeHint()
        margins = self.contentsMargins()
        return QSize(
            hint.width() + margins.left() + margins.right(),
            hint.height() + margins.top() + margins.bottom(),
        )


class CloudSettingsDialog(QDialog):
    """Choose where the machine runs, set its limits, start and stop it."""

    def __init__(self, parent: QWidget | None = None, session: CloudSession | None = None):
        super().__init__(parent)
        self.setWindowTitle("Cloud settings")
        self.setModal(False)
        # Orange quits when its last workflow window closes, also while this
        # top-level dialog is open (which the question on quitting expects).
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.session = session if session is not None else CloudSession.instance()
        self.settings = CloudSettings.load()
        self._info: InfoResponse | None = None
        self._info_time = 0.0
        self._local_versions = local_versions()

        layout = QVBoxLayout(self)
        limits = self._limits_box()  # first: the pages may save the settings
        layout.addWidget(self._provider_box(), 1)  # gets the room the dialog grows by
        layout.addWidget(limits)
        layout.addWidget(self._machine_box())
        layout.addWidget(self._versions_box())
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        buttons.addButton(self._hosted_interest_button(), QDialogButtonBox.ButtonRole.HelpRole)
        layout.addWidget(buttons)

        # Uptime and time left count down between heartbeats.
        self._clock = QTimer(self, interval=1000)
        self._clock.timeout.connect(self._update_times)

        s = self.session
        s.statusChanged.connect(self._on_status)
        s.messageChanged.connect(self._on_message)
        s.error.connect(self._on_error)
        s.infoChanged.connect(self._on_info)
        s.connected.connect(self._update_connection)
        s.disconnected.connect(self._update_connection)
        self._leftover_check = False

        self._on_status(s.state.value)
        self._on_message(s.message)
        self._on_info(s.last_info)
        self.resize(560, self.sizeHint().height())

    # ------------------------------------------------------------------
    # Layout

    def _provider_box(self) -> QGroupBox:
        box = QGroupBox("Where the machine runs")
        layout = QVBoxLayout(box)
        row = QHBoxLayout()
        self.provider_group = QButtonGroup(self)
        self.provider_buttons: dict[str, QRadioButton] = {}
        #: Each provider's page, by provider id.
        self.pages: dict[str, ProviderPage] = {}
        self.provider_pages = QStackedWidget()
        entries = registered_providers()
        self._provider_ids = [entry.id for entry in entries]
        for index, entry in enumerate(entries):
            button = QRadioButton(entry.label)
            self.provider_group.addButton(button, index)
            self.provider_buttons[entry.id] = button
            row.addWidget(button)
            page = make_page(entry.factory(), self.settings, self._save, self.session)
            page.setupChanged.connect(self._on_setup_changed)
            self.pages[entry.id] = page
            self.provider_pages.addWidget(page)
        row.addStretch(1)
        layout.addLayout(row)
        # The pages scroll when the setup check's results don't fit; the
        # dialog first grows for them (_fit_page).
        self.provider_scroll = _PageScroll(widgetResizable=True, frameShape=QFrame.Shape.NoFrame)
        self.provider_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.provider_scroll.setWidget(self.provider_pages)
        # The group box's background, not the scroll area's.
        self.provider_scroll.viewport().setAutoFillBackground(False)
        self.provider_pages.setAutoFillBackground(False)
        layout.addWidget(self.provider_scroll, 1)

        ids = self._provider_ids
        index = ids.index(self.settings.provider) if self.settings.provider in ids else 0
        self.provider_group.button(index).setChecked(True)
        self.provider_pages.setCurrentIndex(index)
        self.provider_group.idToggled.connect(self._on_provider_toggled)
        return box

    def _limits_box(self) -> QGroupBox:
        box = QGroupBox("Time limits")
        form = QFormLayout(box)
        self.idle_spin = QSpinBox(
            minimum=0, maximum=24 * 60, suffix=" min", specialValueText="Off", singleStep=5
        )
        self.idle_spin.setValue(round(self.settings.idle_timeout / 60))
        self.idle_spin.setToolTip(
            "The machine stops by itself after this long without requests or jobs. "
            "It also cancels its jobs when Orange hasn't been in touch for this long, "
            "for example after Orange crashed."
        )
        self.lifetime_spin = QSpinBox(
            minimum=0, maximum=7 * 24, suffix=" h", specialValueText="Off"
        )
        self.lifetime_spin.setValue(round(self.settings.max_lifetime / 3600))
        self.lifetime_spin.setToolTip(
            "The machine stops after this long, even if jobs are running."
        )
        form.addRow("Stop when idle for:", self.idle_spin)
        form.addRow("Stop after at most:", self.lifetime_spin)
        note = QLabel("Changes apply to the next machine you start.")
        note.setEnabled(False)
        form.addRow(note)
        self.idle_spin.valueChanged.connect(self._on_limits_changed)
        self.lifetime_spin.valueChanged.connect(self._on_limits_changed)
        return box

    def _machine_box(self) -> QGroupBox:
        box = QGroupBox("Machine")
        layout = QVBoxLayout(box)
        row = QHBoxLayout()
        self.state_label = QLabel()
        font = QFont(self.state_label.font())
        font.setBold(True)
        self.state_label.setFont(font)
        self.start_button = QPushButton("Start")
        self.start_button.clicked.connect(self.start_machine)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop_machine)
        row.addWidget(self.state_label)
        row.addStretch(1)
        row.addWidget(self.start_button)
        row.addWidget(self.stop_button)
        layout.addLayout(row)

        self.message_label = QLabel(wordWrap=True)
        self.message_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.message_label)

        # The saved record of the last machine, if it can't be read.
        self.record_label = QLabel(wordWrap=True)
        self.record_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.record_label.setStyleSheet(f"color: {_MISMATCH.name()};")
        self.record_label.setVisible(False)
        layout.addWidget(self.record_label)

        # Machines of the add-on that this session doesn't use (on a cloud).
        row = QHBoxLayout()
        self.leftovers_label = QLabel(wordWrap=True)
        self.leftovers_label.setStyleSheet(f"color: {_MISMATCH.name()};")
        self.leftovers_button = QPushButton("Delete them…")
        self.leftovers_button.clicked.connect(self.delete_leftovers)
        row.addWidget(self.leftovers_label, 1)
        row.addWidget(self.leftovers_button)
        layout.addLayout(row)
        self.leftovers: list[str] = []
        self._show_leftovers([])

        form = QFormLayout()
        self.detail_labels: dict[str, QLabel] = {}
        for key, label in (
            ("name", "Name:"),
            ("url", "Address:"),
            ("uptime", "Running for:"),
            ("time_left", "Stops in:"),
            ("price", "Price:"),
            ("cost", "Cost so far:"),
            ("certificate", "Certificate:"),
            ("token", "Token:"),
        ):
            value = QLabel()
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            form.addRow(label, value)
            self.detail_labels[key] = value
        layout.addLayout(form)
        return box

    def _hosted_interest_button(self) -> QPushButton:
        self.hosted_interest_url = hosted_interest_url()
        self.hosted_button = QPushButton("Notify me about the hosted version")
        self.hosted_button.setAutoDefault(False)
        if self.hosted_interest_url:
            self.hosted_button.setToolTip(
                "Opens a page in your browser where you can show interest in a hosted "
                "version (no cloud account of your own) and hear when it is available."
            )
        else:
            self.hosted_button.setEnabled(False)
            self.hosted_button.setToolTip(
                "There is no sign-up page for the hosted version yet in this version "
                "of orange3-cloud."
            )
        self.hosted_button.clicked.connect(self.open_hosted_interest)
        return self.hosted_button

    def _versions_box(self) -> QGroupBox:
        box = QGroupBox("Versions")
        layout = QVBoxLayout(box)
        self.versions_view = QTreeWidget(rootIsDecorated=False, uniformRowHeights=True)
        self.versions_view.setHeaderLabels(["Package", "This computer", "Machine"])
        self.versions_view.header().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for name, label in VERSION_PACKAGES:
            item = QTreeWidgetItem([label, self._local_versions.get(name) or "—", ""])
            item.setData(0, Qt.ItemDataRole.UserRole, name)
            self.versions_view.addTopLevelItem(item)
        rows = len(VERSION_PACKAGES)
        height = self.versions_view.sizeHintForRow(0) * rows
        self.versions_view.setFixedHeight(
            height + self.versions_view.header().sizeHint().height() + 6
        )
        layout.addWidget(self.versions_view)
        self.versions_label = QLabel(wordWrap=True)
        layout.addWidget(self.versions_label)
        self.failures_label = QLabel(wordWrap=True)
        self.failures_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.failures_label)
        return box

    # ------------------------------------------------------------------
    # Actions

    @property
    def provider_id(self) -> str:
        """The ID of the chosen provider."""
        return self._provider_ids[max(0, self.provider_group.checkedId())]

    @property
    def page(self) -> ProviderPage:
        """The chosen provider's page."""
        return self.pages[self.provider_id]

    def _finds_leftovers(self) -> bool:
        """Whether the chosen provider's machines left behind are looked for."""
        return self.page.provider.finds_leftovers

    def start_machine(self) -> None:
        """Start the machine on the session's background thread."""
        self._save()
        self.message_label.setStyleSheet("")
        self.session.start_machine()
        self._update_buttons()

    def stop_machine(self) -> None:
        """Stop the machine on the session's background thread."""
        self.message_label.setStyleSheet("")
        self.session.stop_machine()
        self._update_buttons()

    def open_hosted_interest(self) -> None:
        """Open the hosted-interest page in the user's browser."""
        if self.hosted_interest_url:
            QDesktopServices.openUrl(QUrl(self.hosted_interest_url))

    def check_leftovers(self) -> None:
        """Look for machines of the add-on that the session doesn't use (in
        the background); they show with a button that deletes them."""
        if not self._finds_leftovers() or self._leftover_check:
            return
        if self.session.state not in (SessionState.DISCONNECTED, SessionState.CONNECTED):
            return
        self._leftover_check = True
        session, deliver = self.session, self._leftovers_receiver()

        def run() -> None:
            try:
                result: object = [handle.name for handle in session.leftovers()]
            except Exception as exc:  # noqa: BLE001 - shown as "couldn't check"
                result = exc
            deliver(result)

        threading.Thread(target=run, name="orange3-cloud-leftovers", daemon=True).start()

    def delete_leftovers(self) -> None:
        """Delete the leftover machines after the user confirms."""
        names = list(self.leftovers)
        if not names:
            return
        answer = QMessageBox.question(
            self,
            "Delete machines",
            "Delete these orange3-cloud machines and their disks?\n\n"
            + "\n".join(f"• {name}" for name in names)
            + "\n\nAnything on them is lost.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.leftovers_button.setEnabled(False)
        self.leftovers_label.setText("Deleting " + ", ".join(names) + "…")
        deliver = self._leftovers_receiver()
        future = self.session.delete_leftovers()
        future.add_done_callback(lambda _: deliver(None))

    def _leftovers_receiver(self) -> Callable[[object], None]:
        """A function for a thread to call with a leftover check's result (or
        ``None`` after deleting): :meth:`_on_leftovers` gets it in the GUI
        thread if the dialog still exists (see :mod:`~orangecontrib.cloud.
        gui.relay`)."""
        return receiver(self, CloudSettingsDialog._on_leftovers)

    @Slot(object)
    def _on_leftovers(self, result: object) -> None:
        if result is None:  # deleted: look again
            self._show_leftovers([])
            self.check_leftovers()
            return
        self._leftover_check = False
        if isinstance(result, Exception) or self.session.state not in (
            SessionState.DISCONNECTED,
            SessionState.CONNECTED,
        ):
            result = []
        self._show_leftovers(list(result))

    def _show_leftovers(self, names: list[str]) -> None:
        self.leftovers = names
        visible = bool(names) and self._finds_leftovers()
        self.leftovers_label.setVisible(visible)
        self.leftovers_button.setVisible(visible)
        self.leftovers_button.setEnabled(True)
        if visible:
            self.leftovers_label.setText(
                f"Other orange3-cloud machines are on {self.page.provider.label}: "
                f"{html.escape(', '.join(names))}. They cost money until they are deleted."
            )

    @Slot()
    def _on_setup_changed(self) -> None:
        self._update_buttons()
        QTimer.singleShot(0, self._fit_page)  # once the page's layout has its new text
        if self.page.ready:
            self.check_leftovers()

    def _fit_page(self) -> None:
        """Grow the dialog until the provider's page shows whole (e.g. long
        setup check results), as far as the screen allows; beyond that the
        page scrolls."""
        if isdeleted(self) or not self.isVisible():
            return
        viewport = self.provider_scroll.viewport()
        page = self.provider_pages.currentWidget()
        needed = page.heightForWidth(viewport.width()) if page.hasHeightForWidth() else -1
        extra = max(needed, page.sizeHint().height()) - viewport.height()
        screen = self.screen()
        if extra <= 0 or screen is None:
            return
        frame = self.frameGeometry().height() - self.height()
        room = screen.availableGeometry().height() - frame
        self.resize(self.width(), min(self.height() + extra, max(room, self.height())))

    def _save(self) -> None:
        if self.provider_group.checkedId() >= 0:  # not while the pages are made
            self.settings.provider = self.provider_id
        self.settings.idle_timeout = self.idle_spin.value() * 60
        self.settings.max_lifetime = self.lifetime_spin.value() * 3600
        self.settings.save()

    # ------------------------------------------------------------------
    # Session signals and updates

    @Slot(int, bool)
    def _on_provider_toggled(self, index: int, checked: bool) -> None:
        if checked:
            self.provider_pages.setCurrentIndex(index)
            QTimer.singleShot(0, self._fit_page)
            self._save()
            self._update_buttons()
            self._show_leftovers(self.leftovers if self._finds_leftovers() else [])

    @Slot()
    def _on_limits_changed(self) -> None:
        self._save()

    @Slot(str)
    def _on_status(self, state: str) -> None:
        state = SessionState(state)
        text = STATE_TEXT.get(state, state.value)
        if state is SessionState.DISCONNECTED and self.session.store.load() is not None:
            text = "Not connected"
        self.state_label.setText(text)
        self._show_record_error()
        self._update_buttons()
        self._update_connection()
        if state is SessionState.DISCONNECTED and self._finds_leftovers():
            self.check_leftovers()

    def _show_record_error(self) -> None:
        """Say so when the saved record of the last machine can't be read."""
        store, error = self.session.store, None
        if self.session.handle is None and store.load() is None:
            error = store.load_error
        self.record_label.setVisible(error is not None)
        if error is not None:
            self.record_label.setText(
                f"The saved record of the last machine can't be read: {store.path} ({error}). "
                "Orange can't reconnect to that machine. If it still runs, it costs money "
                "until it's deleted: in a cloud account, it is listed below as another "
                "machine to delete; a local machine is replaced when you start the next one."
            )

    @Slot(str)
    def _on_message(self, message: str) -> None:
        self.message_label.setText(message)

    @Slot(str)
    def _on_error(self, message: str) -> None:
        self.message_label.setText(message)
        self.message_label.setStyleSheet(f"color: {_MISMATCH.name()};")

    @Slot(object)
    def _on_info(self, info: InfoResponse | None) -> None:
        self._info = info
        self._info_time = time.monotonic()
        self._update_versions()
        self._update_times()
        if info is not None and self.session.is_connected:
            self._clock.start()
        else:
            self._clock.stop()

    @Slot()
    def _update_connection(self) -> None:
        """Connection details from the session's handle."""
        handle = self.session.handle if self.session.is_connected else None
        labels = self.detail_labels
        if handle is None:
            for label in labels.values():
                label.setText("—")
                label.setToolTip("")
            if not self.session.is_connected:
                self._on_info(None)
            return
        labels["name"].setText(handle.name)
        labels["url"].setText(handle.url)
        try:
            fingerprint = certificate_fingerprint(handle.cert_pem)
        except (ValueError, ssl.SSLError):
            labels["certificate"].setText("unreadable")
        else:
            labels["certificate"].setText(f"SHA-256 {fingerprint[:23]}…")
            labels["certificate"].setToolTip(
                f"The machine's own certificate, the only one trusted for it.\n"
                f"SHA-256 {fingerprint}"
            )
        # Never the whole token: it's the machine's password.
        labels["token"].setText(f"••••{handle.token[-4:]}" if handle.token else "none")
        labels["token"].setToolTip(
            "The machine's access token is kept only in the add-on's private folder."
        )
        self._update_times()

    @Slot()
    def _update_times(self) -> None:
        info = self._info
        uptime, left = self.detail_labels["uptime"], self.detail_labels["time_left"]
        self._update_cost()
        if info is None or not self.session.is_connected:
            uptime.setText("—")
            left.setText("—")
            return
        elapsed = time.monotonic() - self._info_time
        uptime.setText(format_duration(self._running_seconds(info.uptime_seconds + elapsed)))
        if info.time_left_seconds is None:
            left.setText("no limit" if not info.jobs_running else "no limit while jobs run")
            return
        reason = (
            "idle timeout"
            if info.idle_time_left_seconds is not None
            and info.idle_time_left_seconds <= (info.lifetime_left_seconds or float("inf"))
            else "maximum lifetime"
        )
        left.setText(f"{format_duration(info.time_left_seconds - elapsed)} ({reason})")

    def _running_seconds(self, server_uptime: float) -> float:
        """How long the machine has run: since it was created, as "Cost so
        far" counts, else (no creation time) the server's uptime."""
        handle = self.session.handle
        created = machine_seconds(handle) if handle is not None else None
        self.detail_labels["uptime"].setToolTip(
            "Since the machine was created, as the cost so far counts; its server "
            f"started {format_duration(server_uptime)} ago."
            if created is not None
            else "Since the machine's server started."
        )
        return created if created is not None else server_uptime

    def _update_cost(self) -> None:
        """The machine's price and what it has cost since it was created."""
        session = self.session
        handle = session.handle if session.is_connected else None
        price_label, cost_label = self.detail_labels["price"], self.detail_labels["cost"]
        if handle is None:
            price_label.setText("—")
            cost_label.setText("—")
            return
        price = session.price_per_hour(handle)
        if price == 0:
            price_label.setText("free (this computer)")
            cost_label.setText("—")
            return
        price_label.setText(f"{format_price(price)} (list price, approximate)")
        cost_label.setText(cost_so_far(session, handle))
        cost_label.setToolTip(
            "The machine costs its hourly price from when it was created until it is "
            "deleted, whether it computes or not. Jobs: the share of the run time of "
            "the jobs run from this Orange. Costs are recorded in costs.jsonl in the "
            "add-on's folder."
        )

    def _update_versions(self) -> None:
        info = self._info
        remote = machine_versions(info) if info is not None else {}
        mismatches = []
        for i in range(self.versions_view.topLevelItemCount()):
            item = self.versions_view.topLevelItem(i)
            name = item.data(0, Qt.ItemDataRole.UserRole)
            mine, theirs = self._local_versions.get(name), remote.get(name)
            item.setText(2, theirs or ("—" if info is not None else ""))
            differs = info is not None and mine is not None and mine != theirs
            substituted = name == "python" and differs and python_substituted(info, mine)
            differs = differs and not substituted
            tooltip = ""
            if differs:
                tooltip = "Different versions on this computer and the machine"
            elif substituted:
                tooltip = (
                    f"There's no build of Python {mine} for the machine, so it runs {theirs}, "
                    "the newest release of the same minor version. Those releases only fix bugs."
                )
            font = item.font(0)
            font.setBold(differs)
            for column in range(3):
                item.setFont(column, font)
                item.setForeground(column, QBrush(_MISMATCH) if differs else QBrush())
                item.setToolTip(column, tooltip)
            if differs:
                mismatches.append(item.text(0))
        python_note = ""
        if python_substituted(info, self._local_versions.get("python")) and (
            info.python_version != info.python_version_requested
        ):
            python_note = (
                f" Python {info.python_version} replaces {info.python_version_requested}, "
                "which has no build for the machine."
            )
        if info is None:
            self.versions_label.setText("The machine's versions show when it runs.")
            self.versions_label.setStyleSheet("")
        elif mismatches:
            self.versions_label.setText(
                f"Different versions: {', '.join(mismatches)}. Results may differ from "
                "Orange on this computer. Stop and start the machine to copy your "
                "current versions." + python_note
            )
            self.versions_label.setStyleSheet(f"color: {_MISMATCH.name()};")
        else:
            self.versions_label.setText(
                "The machine has the same versions as this computer." + python_note
            )
            self.versions_label.setStyleSheet("")

        failures = info.install_failures if info is not None else []
        self.failures_label.setVisible(bool(failures))
        if failures:
            names = ", ".join(f"{f.name} {f.version}" for f in failures)
            self.failures_label.setText(
                f"Couldn't install on the machine (optional packages): {names}"
            )
            self.failures_label.setToolTip(
                "\n\n".join(f"{f.name} {f.version}:\n{f.error.strip()}" for f in failures)
            )

    def _update_buttons(self) -> None:
        state = self.session.state
        available = self.page.ready
        self.start_button.setToolTip("" if available else self.page.not_ready_text)
        has_machine = session_machine(self.session) is not None
        idle = state in (SessionState.DISCONNECTED, SessionState.ERROR)
        self.start_button.setEnabled(available and idle and not self.session.is_connected)
        self.stop_button.setEnabled(can_stop(self.session))
        # The provider can't change while a machine runs.
        for button in self.provider_buttons.values():
            button.setEnabled(idle and not has_machine)

    # ------------------------------------------------------------------

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self._fit_page)
        if self._info is not None and self.session.is_connected:
            self._clock.start()

    def hideEvent(self, event: QHideEvent) -> None:
        self._clock.stop()
        super().hideEvent(event)


_dialog: CloudSettingsDialog | None = None


def show_settings_dialog(
    parent: QWidget | None = None, session: CloudSession | None = None
) -> CloudSettingsDialog:
    """Show the shared Cloud settings dialog (create it on first use) and
    bring it to the front. ``session``: default the shared session.

    The dialog is a top-level window, not a child of ``parent``, so it stays
    open when the widget that opened it is closed.
    """
    global _dialog
    session = session if session is not None else CloudSession.instance()
    if _dialog is None or isdeleted(_dialog) or _dialog.session is not session:
        if _dialog is not None and not isdeleted(_dialog):
            _dialog.close()
            _dialog.deleteLater()
        _dialog = CloudSettingsDialog(None, session)
        if parent is not None:
            center = parent.window().frameGeometry().center()
            _dialog.move(center - _dialog.rect().center())
    _dialog.show()
    _dialog.raise_()
    _dialog.activateWindow()
    return _dialog
