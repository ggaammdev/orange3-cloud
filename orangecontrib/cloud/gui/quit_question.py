"""The question when Orange quits while a cloud machine runs: stop it, keep it
running, or don't quit.

Orange quits when its last canvas window (one per open workflow) closes:
File > Quit closes all windows (``QApplication.closeAllWindows``), and so do
Cmd+Q and the Dock's Quit on macOS (Qt sends the application a ``Quit``
event, which closes all windows). Each window gets a ``Close`` event first,
and ignoring it keeps the window, and Orange, open. ``aboutToQuit`` comes too
late to ask: Qt allows no dialogs then.

So :class:`QuitQuestion` is an event filter on the canvas windows (Orange's
``CanvasMainWindow``; filtering events changes nothing in Orange). On the
``Close`` event of the last visible canvas window, it asks before Orange's
own ``closeEvent`` runs (which may then ask to save the workflow). The other
windows close without a question, so Quit asks once. Every ``Close`` event
also installs the filter on the canvas windows opened since: Orange opens a
window only from another one, so the last window to close has it.

Cloud widgets call :func:`install_quit_question` when they are created. The
filter is a child of the application; Qt drops it from windows that are
deleted, and deletes it with the application.

A machine on this computer (local mode) costs nothing and stops by itself
after its idle timeout, so quitting asks only about machines that cost money
(or whose price is unknown).
"""

from __future__ import annotations

import sys
from concurrent.futures import Future
from typing import Any

from AnyQt.QtCore import QEvent, QObject, QTimer
from AnyQt.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QMessageBox,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)
from AnyQt.sip import isdeleted

from orangecontrib.cloud.client.session import CloudSession, SessionState
from orangecontrib.cloud.gui.stop_machine import costs_money, machine_summary
from orangecontrib.cloud.settings import CloudSettings

__all__ = [
    "QuitQuestion",
    "StopProgress",
    "ask_before_quit",
    "canvas_windows",
    "install_quit_question",
]

#: The module of Orange's canvas window, loaded once Orange's canvas runs.
CANVAS_MODULE = "orangecanvas.application.canvasmain"

_question: QuitQuestion | None = None


def canvas_windows() -> list[QWidget]:
    """Orange's canvas windows, one per open workflow (none when a widget
    runs without Orange's canvas)."""
    canvasmain = sys.modules.get(CANVAS_MODULE)
    if canvasmain is None:  # no canvas, so no canvas windows: no need to import it
        return []
    window_class = canvasmain.CanvasMainWindow
    return [w for w in QApplication.topLevelWidgets() if isinstance(w, window_class)]


def install_quit_question(session: CloudSession) -> None:
    """Ask about ``session``'s machine when Orange quits: watch the canvas
    windows open now. Call it whenever a Cloud widget is created."""
    global _question
    app = QApplication.instance()
    if app is None:
        return
    if _question is None or isdeleted(_question):
        _question = QuitQuestion(app)
    _question.session = session
    _question.watch_windows()


def closes_orange(window: QWidget) -> bool:
    """Whether closing ``window``, a canvas window, quits Orange: no other
    canvas window is open."""
    if not QApplication.quitOnLastWindowClosed():
        return False
    return not any(w is not window and w.isVisible() for w in canvas_windows())


class QuitQuestion(QObject):
    """The event filter on Orange's canvas windows (see the module's documentation)."""

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        #: The session whose machine quitting asks about.
        self.session: CloudSession | None = None
        self._asking = False

    def watch_windows(self) -> None:
        """Filter the events of the canvas windows open now (a window that
        has the filter keeps one)."""
        for window in canvas_windows():
            window.installEventFilter(self)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() != QEvent.Type.Close or self._asking or self.session is None:
            return False
        self.watch_windows()  # opened since: the last window to close needs the filter
        if not isinstance(obj, QWidget) or not closes_orange(obj):
            return False
        if not costs_money(self.session):  # no machine, or a free one
            return False
        self._asking = True
        try:
            go_on = ask_before_quit(obj, self.session)
        finally:
            self._asking = False
        if not go_on:
            event.ignore()  # the window, and Orange, stay open
        return not go_on


def ask_before_quit(window: QWidget, session: CloudSession) -> bool:
    """Ask whether to stop ``session``'s machine before Orange quits, and stop
    it if asked; True when Orange may go on quitting. A machine that is
    stopping already is waited for without a question."""
    if session.state is SessionState.STOPPING:
        return stop_and_wait(window, session)
    box = QMessageBox(window)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle("Quit Orange")
    starting = session.state is SessionState.STARTING
    box.setText(f"A cloud machine is still {'starting' if starting else 'running'}.")
    box.setInformativeText(f"{machine_summary(session)}\n\n{keep_running_text(session)}")
    stop = box.addButton("Stop and quit", QMessageBox.ButtonRole.AcceptRole)
    keep = box.addButton("Keep it running", QMessageBox.ButtonRole.DestructiveRole)
    cancel = box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(stop)
    box.setEscapeButton(cancel)
    box.exec()
    clicked = box.clickedButton()
    box.deleteLater()
    if clicked is stop:
        return stop_and_wait(window, session)
    return clicked is keep


def keep_running_text(session: CloudSession) -> str:
    """What happens to a machine left running: when it stops by itself (its
    time limits, from the machine or else the Cloud settings)."""
    info = session.last_info
    if info is not None:
        idle, lifetime = info.idle_timeout_seconds, info.max_lifetime_seconds
    else:
        settings = CloudSettings.load()
        idle, lifetime = settings.idle_timeout, settings.max_lifetime
    keep, disk = "If you keep it running, it", "its disk stays until you open Orange again."
    if idle:
        return f"{keep} stops by itself after {_duration(idle)} without work; {disk}"
    if lifetime:
        return f"{keep} stops by itself after its maximum lifetime, {_duration(lifetime)}; {disk}"
    return f"{keep} doesn't stop by itself, and costs money until you stop it in Cloud settings."


def _duration(seconds: float) -> str:
    """A time limit for the user, e.g. "30 minutes", "12 hours", "1 h 30 min"."""
    minutes = max(1, round(seconds / 60))
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    if not rest:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{hours} h {rest} min"


def stop_and_wait(window: QWidget, session: CloudSession) -> bool:
    """Stop the machine, as Stop in Cloud settings does, showing progress
    until it's done; True when Orange may go on quitting (stopped, or the
    user chose to quit after an error)."""
    future = session.stop_machine()
    progress = StopProgress(window, session, future)
    progress.run()
    progress.deleteLater()
    error = _error(future)
    return error is None or _quit_anyway(window, error)


def _error(future: Future[Any]) -> str | None:
    """Why the stop failed (``None``: it didn't)."""
    if future.cancelled():
        return "The stop was cancelled."
    exc = future.exception()
    return (str(exc) or type(exc).__name__) if exc is not None else None


def _quit_anyway(window: QWidget, error: str) -> bool:
    """Say that the machine couldn't be stopped; True if the user quits anyway."""
    box = QMessageBox(window)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("Quit Orange")
    box.setText("Couldn't stop the cloud machine.")
    box.setInformativeText(
        f"{error}\n\nIf you quit anyway, the machine stops by itself after its idle "
        "timeout, and Orange deletes it when you open it again. Or cancel, and try "
        "Stop in Cloud settings again."
    )
    quit_anyway = box.addButton("Quit anyway", QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)
    box.exec()
    clicked = box.clickedButton()
    box.deleteLater()
    return clicked is quit_anyway


class StopProgress(QDialog):
    """A small modal "Stopping the cloud machine…" with the session's
    progress messages, until the stop's ``future`` is done; it can't be
    closed before."""

    def __init__(self, parent: QWidget | None, session: CloudSession, future: Future[Any]):
        super().__init__(parent)
        self.setWindowTitle("Quit Orange")
        self.setModal(True)
        self._future = future
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Stopping the cloud machine…"))
        bar = QProgressBar()
        bar.setRange(0, 0)  # busy
        layout.addWidget(bar)
        self.message_label = QLabel(session.message)
        layout.addWidget(self.message_label)
        session.messageChanged.connect(self.message_label.setText)
        self._timer = QTimer(self, interval=100)
        self._timer.timeout.connect(self._check)

    def run(self) -> None:
        """Show the dialog until the stop is done (returns at once if it is)."""
        if self._future.done():
            return
        self._timer.start()
        self.exec()

    def _check(self) -> None:
        if self._future.done():
            self._timer.stop()
            self.accept()

    def reject(self) -> None:
        """Escape and the window's close button do nothing until the stop is done."""
        if self._future.done():
            super().reject()
