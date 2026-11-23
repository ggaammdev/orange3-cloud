"""The base of the Cloud settings dialog's pages for clouds with an account
to sign in to (e.g. :mod:`~orangecontrib.cloud.gui.gcp_page`).

:class:`AccountPage` has what those pages share:

* **Background calls** (:meth:`AccountPage._submit`): finding the cloud's
  tool, reading the sign-in and listing accounts and regions are slow, so
  they run on the page's own thread, and their results come back to the GUI
  thread through :func:`~orangecontrib.cloud.gui.relay.receiver` (a page
  deleted meanwhile is left alone). Nothing runs until the page is first
  shown (:meth:`AccountPage.refresh`).
* **Signing in** goes through the session
  (:meth:`~orangecontrib.cloud.client.session.CloudSession.sign_in`), which
  runs the provider's ``sign_in`` (the cloud tool's browser sign-in) on a
  thread of its own and reports with a signal. The sign-in button becomes
  "Cancel sign-in" meanwhile. The GUI never waits for either.
* **The setup check:** the cloud's checker (a list of
  :class:`~orangecontrib.cloud.client.providers.setup_checks.Check`) runs
  in the background when what it depends on changes
  (:meth:`AccountPage.setup_key`) and on "Check again". The dialog's Start
  waits until everything passed (:attr:`AccountPage.ready`).
* A message line for progress and errors.
"""

from __future__ import annotations

import html
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from AnyQt.QtCore import Qt, QTimer
from AnyQt.QtCore import pyqtSlot as Slot
from AnyQt.QtGui import QShowEvent
from AnyQt.QtWidgets import QFormLayout, QHBoxLayout, QLabel, QPushButton, QWidget

from orangecontrib.cloud.client.providers.base import (
    ClusterProvider,
    ProviderError,
    SignInCancelledError,
)
from orangecontrib.cloud.client.providers.setup_checks import Check, Status, all_passed
from orangecontrib.cloud.client.session import CloudSession
from orangecontrib.cloud.gui.provider_page import ProviderPage
from orangecontrib.cloud.gui.relay import receiver
from orangecontrib.cloud.settings import CloudSettings

__all__ = ["ERROR_STYLE", "STATUS_MARK", "AccountPage", "checks_html"]

#: Style of error messages.
ERROR_STYLE = "color: #c62828;"
#: Each check status's mark.
STATUS_MARK = {
    Status.PASSED: '<span style="color: #2e7d32;">✓</span>',
    Status.FAILED: '<span style="color: #c62828;">✗</span>',
    Status.UNKNOWN: '<span style="color: #9e9e9e;">–</span>',
}


def checks_html(checks: list[Check]) -> str:
    """The checks as rich text for a label: a mark, the title and, unless the
    check passed, what was found, how to fix it and a link."""
    rows = []
    for check in checks:
        text = f"{STATUS_MARK[check.status]} {html.escape(check.title)}"
        notes = []
        if check.status is not Status.PASSED:
            notes = [html.escape(n) for n in (check.detail, check.fix) if n]
            if check.link:
                notes.append(f'<a href="{html.escape(check.link)}">Open</a>')
        if notes:
            text += "<br>&nbsp;&nbsp;&nbsp;&nbsp;" + " ".join(notes)
        rows.append(text)
    return "<br>".join(rows)


class AccountPage(ProviderPage):
    """A cloud's page: sign in, choose the account and region, and see the
    setup check. ``setupChanged``: the setup check finished (or was
    invalidated); see :attr:`setup_ok`.

    Subclasses build the form (with :attr:`sign_in_button` and
    :meth:`add_setup_rows`) and implement :meth:`refresh`, :meth:`setup_key`,
    :meth:`setup_job`, :meth:`_show_account` and :meth:`_update_enabled`.
    """

    #: The one line shown instead of the checks when all of them passed.
    ready_text = "Everything is set."

    def __init__(
        self,
        provider: ClusterProvider,
        settings: CloudSettings,
        save: Callable[[], None],
        session: CloudSession,
        parent: QWidget | None = None,
    ):
        super().__init__(provider, settings, save, session, parent)
        self._executor = ThreadPoolExecutor(1, thread_name_prefix=f"orange3-cloud-{provider.id}")
        self._callbacks: dict[int, Callable[[Any, BaseException | None], None]] = {}
        self._next_request = 0
        self._lock = threading.Lock()
        self._started = False
        self._shut_down = False
        #: What the background check of the account found (``None``: not yet).
        self._account: Any = None
        self._signing_in = False  # this page started the session's sign-in
        #: The setup checker's latest results (``None``: not checked yet).
        self.setup_checks: list[Check] | None = None
        self._check_serial = 0
        #: :meth:`setup_key` of the latest check.
        self._checked_for: tuple[str, ...] | None = None
        self._check_timer = QTimer(self, singleShot=True, interval=300)
        self._check_timer.timeout.connect(self.check_setup)

        self.sign_in_button = QPushButton()
        self.sign_in_button.clicked.connect(self._on_sign_in_clicked)
        self.message_label = QLabel(wordWrap=True)
        self.message_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setup_title = QLabel("<b>Setup check</b>")
        self.check_button = QPushButton("Check again")
        self.check_button.clicked.connect(self.check_setup)
        self.setup_label = QLabel(wordWrap=True, openExternalLinks=True)
        self.setup_label.setTextFormat(Qt.TextFormat.RichText)
        self.setup_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        session.signInFinished.connect(self._on_sign_in_finished)

    def add_setup_rows(self, form: QFormLayout, *buttons: QPushButton) -> None:
        """Add the message line and the setup check (with ``buttons`` next to
        "Check again") to ``form``."""
        form.addRow(self.message_label)
        row = QHBoxLayout()
        row.addWidget(self.setup_title, 1)
        for button in buttons:
            row.addWidget(button)
        row.addWidget(self.check_button)
        form.addRow(row)
        form.addRow(self.setup_label)

    # ------------------------------------------------------------------
    # Public

    @property
    def busy(self) -> bool:
        """A background call or the sign-in is running."""
        return bool(self._callbacks) or self._signing_in

    @property
    def setup_ok(self) -> bool:
        """The latest setup check passed everything."""
        return self.setup_checks is not None and all_passed(self.setup_checks)

    @property
    def ready(self) -> bool:
        """A machine can start when the setup check passed."""
        return self.setup_ok

    def refresh(self) -> None:
        """Check the cloud's tool and the sign-in, and list the accounts again."""
        raise NotImplementedError

    def setup_key(self) -> tuple[str, ...]:
        """What the setup check depends on (e.g. the account, region and
        machine size); it runs again when this changes."""
        raise NotImplementedError

    def setup_job(self) -> Callable[[], list[Check]]:
        """The setup check for the current choices, to run in the background."""
        raise NotImplementedError

    @Slot()
    def check_setup(self) -> None:
        """Run the setup checker for the current choices (in the background);
        the results replace the shown ones."""
        self._check_timer.stop()
        self._check_serial += 1
        serial = self._check_serial
        self._checked_for = self.setup_key()
        job = self.setup_job()
        self.setup_title.setText("<b>Setup check</b> (checking…)")

        def done(checks: list[Check] | None, error: BaseException | None) -> None:
            if serial != self._check_serial:
                return  # the choices changed meanwhile
            self.setup_title.setText("<b>Setup check</b>")
            if error is not None:
                self.setup_checks = None
                self.setup_label.setText(
                    f'<span style="{ERROR_STYLE}">'
                    f"Couldn't check the setup: {html.escape(str(error))}</span>"
                )
            else:
                self._show_checks(checks or [])
            self._update_enabled()
            self.setupChanged.emit()

        self._submit(job, done)

    def shutdown(self) -> None:
        """Stop a running sign-in and the background thread."""
        if self._signing_in:
            self._session.cancel_sign_in()
        self._check_timer.stop()
        self._shut_down = True
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------
    # The setup check

    def _show_checks(self, checks: list[Check]) -> None:
        self.setup_checks = checks
        text = checks_html(checks)
        if all_passed(checks):
            # One line when all is well; the checks are in the tooltip.
            self.setup_label.setToolTip(text)
            text = f"{STATUS_MARK[Status.PASSED]} {html.escape(self.ready_text)}"
        else:
            self.setup_label.setToolTip("")
        self.setup_label.setText(text)

    def _schedule_check(self) -> None:
        self._check_serial += 1  # results of a running check are now stale
        self._checked_for = self.setup_key()
        if self.setup_checks is not None:
            self.setup_checks = None
            self.setupChanged.emit()
        self._check_timer.start()

    def _schedule_check_if_changed(self) -> None:
        """Check again if what the check depends on changed since the last one."""
        if self.setup_key() != self._checked_for:
            self._schedule_check()

    # ------------------------------------------------------------------
    # Background calls

    def _submit(
        self, fn: Callable[[], Any], callback: Callable[[Any, BaseException | None], None]
    ) -> None:
        if self._shut_down:
            return
        with self._lock:
            self._next_request += 1
            number = self._next_request
        self._callbacks[number] = callback
        deliver = receiver(self, type(self)._on_done)

        def run() -> None:
            try:
                result, error = fn(), None
            except BaseException as exc:  # noqa: BLE001 - shown to the user
                result, error = None, exc
            deliver(number, result, error)

        self._executor.submit(run)

    def _on_done(self, number: int, result: Any, error: BaseException | None) -> None:
        callback = self._callbacks.pop(number, None)
        if callback is not None:
            callback(result, error)
        self._update_enabled()

    # ------------------------------------------------------------------
    # Signing in

    @property
    def can_sign_in(self) -> bool:
        """Whether the sign-in button may start a sign-in (e.g. the cloud's
        tool was found)."""
        return False

    #: The message while the user signs in in the browser.
    sign_in_message = "Finish signing in in your browser. The sign-in page has opened there."

    @Slot()
    def _on_sign_in_clicked(self) -> None:
        if self._signing_in:
            self._session.cancel_sign_in()
            return
        if not self.can_sign_in:
            return
        self._signing_in = True
        self.sign_in_button.setText("Cancel sign-in")
        self._set_message(self.sign_in_message)
        self._session.sign_in(self.provider.id)
        self._update_enabled()

    @Slot(object)
    def _on_sign_in_finished(self, error: ProviderError | None) -> None:
        if not self._signing_in:
            return  # another page's sign-in
        self._signing_in = False
        if error is None:
            self.refresh()
            return
        self._show_account(self._account)
        self._set_message(str(error), error=not isinstance(error, SignInCancelledError))
        self._update_enabled()

    def _show_account(self, info: Any) -> None:
        """Show what the background check of the account found (``None``:
        not checked yet)."""
        raise NotImplementedError

    # ------------------------------------------------------------------

    def _set_message(self, text: str, error: bool = False) -> None:
        self.message_label.setText(text)
        self.message_label.setStyleSheet(ERROR_STYLE if error else "")
        self.message_label.setVisible(bool(text))

    def _update_enabled(self, loading: bool = False) -> None:
        """Enable the controls that may be used now (``loading``: a
        background call is about to start)."""
        raise NotImplementedError

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not self._started:
            self.refresh()
