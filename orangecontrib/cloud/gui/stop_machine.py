"""Stopping the machine, and what to say about it: when Stop can stop it, its
cost so far, and the question before a Cloud widget's "Stop machine" button
stops it.

The Cloud settings dialog's Stop, the Cloud widgets' "Stop machine" button
and the question when Orange quits
(:mod:`~orangecontrib.cloud.gui.quit_question`) use these, so they agree on
when there is a machine to stop and on what it costs.
"""

from __future__ import annotations

from AnyQt.QtWidgets import QMessageBox, QWidget

from orangecontrib.cloud.client.costs import format_price, format_usd
from orangecontrib.cloud.client.providers.base import (
    ClusterProvider,
    MachineHandle,
    ProviderError,
)
from orangecontrib.cloud.client.session import CloudSession, SessionState
from orangecontrib.cloud.settings import CloudSettings

__all__ = [
    "can_stop",
    "confirm_stop",
    "cost_so_far",
    "costs_money",
    "machine_summary",
    "session_machine",
]


def session_machine(session: CloudSession) -> MachineHandle | None:
    """The session's machine: the connected one, else the one saved when it
    started (e.g. while Orange reconnects to it, or can't)."""
    return session.handle or session.store.load()


def can_stop(session: CloudSession) -> bool:
    """Whether Stop has a machine to stop now: not while one starts or stops."""
    if session.state in (SessionState.STARTING, SessionState.STOPPING):
        return False
    return session_machine(session) is not None


def _price(session: CloudSession, handle: MachineHandle | None) -> float | None:
    """USD per hour of the machine of ``handle``, or of the one the session
    starts (``None``: unknown)."""
    if handle is not None:
        return session.price_per_hour(handle)
    try:
        return session.provider.price_per_hour()
    except ProviderError:
        return None


def costs_money(session: CloudSession) -> bool:
    """Whether the session's machine, running or starting, isn't free (a
    machine on this computer is): its price is above 0 or unknown."""
    handle = session_machine(session)
    if handle is None and session.state is not SessionState.STARTING:
        return False
    return _price(session, handle) != 0


def cost_so_far(session: CloudSession, handle: MachineHandle) -> str:
    """What the machine of ``handle`` has cost since it was created, as Cloud
    settings shows it: e.g. "$0.42", with the jobs' share "$0.42 (jobs:
    $0.012)", or "unknown"."""
    cost = session.session_cost(handle)
    text = format_usd(cost) if cost is not None else "unknown"
    jobs = session.jobs_cost(handle)
    return f"{text} (jobs: {format_usd(jobs)})" if jobs else text


def _provider(session: CloudSession, handle: MachineHandle | None) -> ClusterProvider | None:
    """The provider of the machine of ``handle``, or of the one the session
    starts (``None``: not available)."""
    try:
        provider = session.machine_provider or session.provider
    except ProviderError:
        return None
    return provider if handle is None or provider.id == handle.provider else None


def _size(session: CloudSession, handle: MachineHandle | None) -> tuple[str, str]:
    """The machine's size, e.g. "Medium (n2-standard-16)", and where it runs,
    e.g. "Google Cloud"; for a machine that starts, the size chosen in the
    Cloud settings."""
    provider = _provider(session, handle)
    name = handle.machine_type or "" if handle is not None else ""
    if provider is None:
        return name, ""
    if handle is None:
        name = provider.size_or_default(CloudSettings.load().of(provider.id).machine_type)
    labels = {size.name: size.label for size in provider.machine_sizes}
    return (f"{labels[name]} ({name})" if name in labels else name), provider.label


def machine_summary(session: CloudSession) -> str:
    """The session's machine for the user: its size, where it runs, its price
    and its cost so far, e.g. "Medium (n2-standard-16) on Google Cloud:
    $0.78/h, $0.42 so far."."""
    handle = session_machine(session)
    size, where = _size(session, handle)
    what = " on ".join(text for text in (size or "The machine", where) if text)
    price = _price(session, handle)
    if price is None:
        return f"{what}; its price is unknown."
    if handle is None or session.session_cost(handle) is None:
        return f"{what}: {format_price(price)}."
    return f"{what}: {format_price(price)}, {cost_so_far(session, handle)} so far."


def confirm_stop(parent: QWidget | None, session: CloudSession) -> bool:
    """Ask whether to stop the session's machine; True if the user chose Stop."""
    handle = session_machine(session)
    free = handle is not None and not costs_money(session)
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle("Stop machine")
    if free:
        box.setText("Stop the machine on this computer?")
        details = "Running jobs are cancelled, and the cloud tables and models on it are gone."
    else:
        box.setText("Stop the cloud machine?")
        details = (
            "The machine, its disk and its network rule are deleted. Running jobs are "
            "cancelled, and the cloud tables and models on it are gone."
        )
        if handle is not None:
            details += "\n\n" + machine_summary(session)
    box.setInformativeText(details)
    stop = box.addButton("Stop", QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)
    box.exec()
    return box.clickedButton() is stop
