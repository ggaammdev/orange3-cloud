"""Handing results from background threads to dialogs and pages in the GUI
thread, safely when the dialog may be deleted meanwhile.

A thread that emits a signal of a deleted Qt object crashes Python, and a
thread that holds the last reference to a dialog deletes it outside the GUI
thread. :func:`receiver` gives a thread a function that holds only a weak
reference to its target and goes through :func:`gui_thread_relay`, one
long-lived object in the GUI thread, which calls the target only if it still
exists.
"""

from __future__ import annotations

import weakref
from collections.abc import Callable
from typing import Any, TypeVar

from AnyQt.QtCore import QObject
from AnyQt.QtCore import pyqtSignal as Signal
from AnyQt.QtCore import pyqtSlot as Slot
from AnyQt.sip import isdeleted

__all__ = ["gui_thread_relay", "receiver"]

_Target = TypeVar("_Target", bound=QObject)


class _GuiThreadRelay(QObject):
    """Runs functions in the GUI thread for other threads. There is one,
    made in the GUI thread and kept while the application lives."""

    call = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.call.connect(self._run)

    @Slot(object)
    def _run(self, fn: Callable[[], None]) -> None:
        fn()


_relay: _GuiThreadRelay | None = None


def gui_thread_relay() -> _GuiThreadRelay:
    """The relay (made when needed, which must be in the GUI thread; Qt
    deletes it with the application)."""
    global _relay
    if _relay is None or isdeleted(_relay):
        _relay = _GuiThreadRelay()
    return _relay


def receiver(target: _Target, method: Callable[..., None]) -> Callable[..., None]:
    """A function for another thread: called with some arguments, it calls
    ``method(target, *arguments)`` in the GUI thread if ``target`` still
    exists. Make it in the GUI thread."""
    target_ref, relay = weakref.ref(target), gui_thread_relay()

    def deliver(*args: Any) -> None:
        def call() -> None:
            obj = target_ref()
            if obj is not None and not isdeleted(obj):
                method(obj, *args)

        if not isdeleted(relay):  # else the application is gone
            relay.call.emit(call)

    return deliver
