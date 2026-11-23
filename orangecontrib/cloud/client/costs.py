"""The cost of jobs and machines, recorded on the user's computer.

A job costs its runtime on the machine times the machine's hourly price. A
session (one machine, from its creation until Orange deletes it) costs its
running time at that price, plus the time it was stopped at its stopped
price: a machine that stopped by itself (idle timeout, maximum lifetime) is
billed only for what it keeps until it is deleted, e.g. its disk, and on
Azure its address (:func:`session_entry`). Its provider says when it stopped
(:meth:`~orangecontrib.cloud.client.providers.base.ClusterProvider.
stopped_at`); if it doesn't, the entry is ``approximate``: the machine counts
as running until it was deleted, but no longer than its maximum lifetime
(:func:`latest_stop`). The prices come from the provider (list prices
without discounts, see :mod:`~orangecontrib.cloud.client.providers.prices`),
so costs are approximate.

:class:`CostLog` appends one JSON line per job and per deleted machine to
``costs.jsonl`` in the add-on's private folder, for the user to look at later.
Each line names the machine's provider, so the costs of each cloud can be
added up separately (:meth:`CostLog.totals`). :class:`SessionCosts` keeps the
prices of a session's machines and records their costs there.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from orangecontrib.cloud.client.providers.base import (
    ClusterProvider,
    MachineHandle,
    ProviderError,
    state_dir,
    utc_time,
)
from orangecontrib.cloud.protocol.jobs import JobResult

__all__ = [
    "CostEntry",
    "CostLog",
    "SessionCosts",
    "cost_of",
    "format_price",
    "format_usd",
    "latest_stop",
    "machine_seconds",
    "session_entry",
]

log = logging.getLogger(__name__)

#: Seconds a machine may take to power off (or, on Azure, to deallocate)
#: after its maximum lifetime ends.
STOP_MARGIN = 5 * 60


def cost_of(seconds: float | None, price_per_hour: float | None) -> float | None:
    """USD for ``seconds`` at ``price_per_hour`` (``None`` if either is unknown)."""
    if seconds is None or price_per_hour is None:
        return None
    return max(0.0, seconds) * price_per_hour / 3600


def machine_seconds(handle: MachineHandle, now: datetime.datetime | None = None) -> float | None:
    """Seconds since the machine of ``handle`` was created (``None``: unknown)."""
    created = utc_time(handle.created_at)
    if created is None:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return max(0.0, (now - created).total_seconds())


def latest_stop(handle: MachineHandle) -> datetime.datetime | None:
    """The latest time the machine of ``handle`` can have stopped by itself:
    its maximum lifetime after it answered, plus :data:`STOP_MARGIN`;
    ``None`` without a maximum lifetime (or if the handle doesn't say)."""
    created = utc_time(handle.created_at)
    try:
        lifetime = float(handle.extra.get("max_lifetime") or 0)
        boot = float(handle.extra.get("boot_seconds") or 0)
    except ValueError:
        return None
    if created is None or lifetime <= 0:
        return None
    return created + datetime.timedelta(seconds=boot + lifetime + STOP_MARGIN)


def format_usd(amount: float) -> str:
    """A cost for the user, e.g. "$1.23", "$0.034", "$0.0012", "under $0.0001"."""
    if amount <= 0:
        return "$0"
    if amount < 0.0001:
        return "under $0.0001"
    if amount < 1:
        return f"${amount:.2g}"
    return f"${amount:,.2f}"


def format_price(per_hour: float | None) -> str:
    """An hourly price, e.g. "$0.78/h" ("price unknown" for ``None``)."""
    if per_hour is None:
        return "price unknown"
    return f"${per_hour:.3f}/h" if per_hour < 0.1 else f"${per_hour:.2f}/h"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class CostEntry:
    """One recorded cost: a job, or a whole session of one machine."""

    what: str  #: ``job`` or ``session``
    machine: str  #: the machine's name
    machine_type: str | None
    seconds: float
    price_per_hour: float
    cost: float  #: USD
    job_kind: str = ""  #: e.g. ``evaluate`` (jobs only)
    job_id: str = ""
    time: str = field(default_factory=_now)
    #: The machine's provider, e.g. ``gcp`` ("" in costs recorded before
    #: providers were named).
    provider: str = ""
    #: Sessions: when the machine stopped by itself (ISO 8601, UTC; "" if it
    #: ran until Orange stopped it). ``seconds`` is its running time before.
    stopped_at: str = ""
    #: Sessions: seconds from then until Orange deleted the machine.
    stopped_seconds: float = 0.0
    #: Sessions: USD per hour while stopped (what still bills, e.g. the disk).
    stopped_price_per_hour: float = 0.0
    #: Sessions: the provider didn't say when the machine stopped (or what it
    #: costs stopped), so it counts as running (see :func:`session_entry`).
    approximate: bool = False


def session_entry(
    handle: MachineHandle,
    prices: tuple[float | None, float | None],
    stopped_at: datetime.datetime | None,
    *,
    running: bool = False,
    now: datetime.datetime | None = None,
) -> CostEntry | None:
    """The cost of the whole session of the machine of ``handle``, deleted
    ``now``, at ``prices`` (USD per hour running, and stopped); ``None`` if
    its price or creation time is unknown.

    ``running``: it ran until now (Orange stopped it). Otherwise it stopped
    at ``stopped_at``, as its provider says; if that is ``None``, it counts
    as running until now, but no longer than :func:`latest_stop`, and the
    entry is ``approximate``. So is one whose stopped price is unknown: its
    stopped time counts at the full price.
    """
    price, stopped_price = prices
    created = utc_time(handle.created_at)
    if price is None or created is None:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    approximate = False
    if stopped_at is None and not running:
        latest = latest_stop(handle)
        stopped_at = min(now, latest) if latest else now
        approximate = True
    stop = min(max(stopped_at or now, created), now).astimezone(datetime.timezone.utc)
    if stopped_price is None:
        stopped_price, approximate = price, approximate or stop < now
    seconds = round(max(0.0, (stop - created).total_seconds()), 1)
    stopped_seconds = round((now - stop).total_seconds(), 1)
    return CostEntry(
        "session",
        handle.name,
        handle.machine_type,
        seconds,
        price,
        (seconds * price + stopped_seconds * stopped_price) / 3600,
        provider=handle.provider,
        stopped_at=stop.isoformat(timespec="seconds") if not running else "",
        stopped_seconds=stopped_seconds,
        stopped_price_per_hour=stopped_price,
        approximate=approximate and price > 0,
    )


class CostLog:
    """Appends costs to ``costs.jsonl`` (one JSON object per line, mode ``0600``).

    :param folder: returns the folder of the file (default: :func:`state_dir`).
    """

    FILENAME = "costs.jsonl"

    def __init__(self, folder: Callable[[], Path] | None = None):
        self._folder = folder or state_dir
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """The ``costs.jsonl`` file."""
        return self._folder() / self.FILENAME

    def record(self, entry: CostEntry) -> None:
        """Append ``entry``. A file that can't be written is logged, not raised:
        recording a cost must never fail a job."""
        line = json.dumps(asdict(entry)) + "\n"
        with self._lock:
            try:
                path = self.path
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError as exc:
                log.warning("Couldn't record the cost in %s: %s", self.FILENAME, exc)

    def entries(self) -> list[CostEntry]:
        """All recorded costs, oldest first (unreadable lines are skipped)."""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        known = {f.name for f in fields(CostEntry)}
        entries = []
        for line in lines:
            try:
                values = json.loads(line)
                entries.append(CostEntry(**{k: v for k, v in values.items() if k in known}))
            except (ValueError, TypeError, AttributeError) as exc:
                log.warning("Skipping an unreadable line in %s: %s", self.FILENAME, exc)
        return entries

    def totals(self) -> dict[str, float]:
        """USD of the recorded sessions (running and stopped), per provider."""
        totals: dict[str, float] = {}
        for entry in self.entries():
            if entry.what == "session":
                totals[entry.provider] = totals.get(entry.provider, 0.0) + entry.cost
        return totals


class SessionCosts:
    """The prices of a session's machines, and their costs recorded in ``log``.

    :param provider_for: the provider of a machine's handle.
    """

    def __init__(self, log: CostLog, provider_for: Callable[[MachineHandle], ClusterProvider]):
        self.log = log
        self._provider_for = provider_for
        #: machine id -> USD per hour (``None``: unknown)
        self.prices: dict[str, float | None] = {}
        self._jobs: dict[str, float] = {}  # machine id -> USD of its jobs

    def price_per_hour(self, handle: MachineHandle) -> float | None:
        """USD per hour of the machine of ``handle``; 0 for a free one,
        ``None`` if unknown (asked of its provider once)."""
        if handle.machine_id not in self.prices:
            try:
                price = self._provider_for(handle).machine_price_per_hour(handle)
            except ProviderError as exc:
                log.warning("No price for %s: %s", handle.name, exc)
                price = None
            self.prices[handle.machine_id] = price
        return self.prices[handle.machine_id]

    def record_jobs(self, handle: MachineHandle, results: list[JobResult]) -> float | None:
        """Record the cost of finished jobs run on the machine of ``handle``;
        returns their total in USD (``None`` if unknown)."""
        price = self.price_per_hour(handle)
        total = None
        for result in results:
            cost = cost_of(result.runtime_seconds, price)
            if cost is None:
                continue
            total = (total or 0.0) + cost
            self._jobs[handle.machine_id] = self._jobs.get(handle.machine_id, 0.0) + cost
            self.log.record(
                CostEntry(
                    "job",
                    handle.name,
                    handle.machine_type,
                    result.runtime_seconds or 0.0,
                    price or 0.0,
                    cost,
                    job_kind=result.kind,
                    job_id=result.job_id,
                    provider=handle.provider,
                )
            )
        return total

    def jobs_cost(self, handle: MachineHandle) -> float:
        """USD of the jobs recorded so far on the machine of ``handle``."""
        return self._jobs.get(handle.machine_id, 0.0)

    def stopped_at(
        self, handle: MachineHandle, seen: datetime.datetime | None = None
    ) -> datetime.datetime | None:
        """When the machine of ``handle`` stopped, as its provider says, and
        not before ``seen`` running (``None``: unknown, e.g. not readable)."""
        try:
            stopped = self._provider_for(handle).stopped_at(handle)
        except (ProviderError, OSError) as exc:  # the entry says it's approximate
            log.warning("Couldn't read when %s stopped: %s", handle.name, exc)
            return None
        return max(stopped, seen) if stopped is not None and seen is not None else stopped

    def record_session(
        self,
        handle: MachineHandle,
        stopped_at: datetime.datetime | None,
        *,
        running: bool = False,
    ) -> None:
        """Record the cost of the machine of ``handle``, just deleted, which
        ran until now (``running``) or stopped at ``stopped_at`` (``None``:
        unknown; see :func:`session_entry`)."""
        try:
            stopped_price = self._provider_for(handle).stopped_price_per_hour(handle)
        except ProviderError as exc:  # its stopped time counts at the full price
            log.warning("No stopped price for %s: %s", handle.name, exc)
            stopped_price = None
        prices = self.price_per_hour(handle), stopped_price
        entry = session_entry(handle, prices, stopped_at, running=running)
        if entry is not None:
            self.log.record(entry)
