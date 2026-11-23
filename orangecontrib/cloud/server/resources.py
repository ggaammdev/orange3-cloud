"""What the job worker has to compute with: its threads and its memory.

Outside a Dask worker (e.g. an operation called directly in a test), the
machine's cores and memory stand in for the worker's.
"""

from __future__ import annotations

import os
import threading

from orangecontrib.cloud.server.worker import current_worker

__all__ = ["PeakMemory", "memory_budget", "memory_limit", "rss", "worker_threads"]

#: Share of the worker's memory limit that work may fill; Dask stops the
#: worker at 95%.
LIMIT_SHARE = 0.9


def worker_threads() -> int:
    """The worker's threads (the machine's cores outside a worker)."""
    worker = current_worker()
    threads = getattr(getattr(worker, "state", None), "nthreads", None) or getattr(
        worker, "nthreads", None
    )
    return int(threads or os.cpu_count() or 1)


def memory_limit() -> int:
    """The worker's memory limit (the machine's memory outside a worker)."""
    import psutil

    worker = current_worker()
    limit = getattr(getattr(worker, "memory_manager", None), "memory_limit", None)
    return int(limit or psutil.virtual_memory().total)


def rss() -> int:
    """Memory this process uses now."""
    import psutil

    return int(psutil.Process().memory_info().rss)


def memory_budget() -> int:
    """Memory new work in this process may still use: the room below
    :data:`LIMIT_SHARE` of the worker's limit, and no more than the machine
    has available."""
    from orangecontrib.cloud.server.app import machine_memory

    room = LIMIT_SHARE * memory_limit() - rss()
    return int(max(0, min(room, machine_memory()[1])))


class PeakMemory:
    """Measure the highest resident memory of this process above its level
    at the start, sampled in a thread (``with PeakMemory() as peak: ...``)."""

    def __init__(self, interval: float = 0.01):
        import psutil

        self._process = psutil.Process()
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.baseline = 0
        self.peak = 0

    def _sample(self) -> None:
        rss = self._process.memory_info().rss
        if rss > self.peak:
            self.peak = rss

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._sample()

    def __enter__(self) -> PeakMemory:
        self.baseline = self.peak = self._process.memory_info().rss
        self._thread = threading.Thread(target=self._run, name="peak-memory", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        self._sample()

    @property
    def extra_bytes(self) -> int:
        """Highest memory measured above the baseline."""
        return max(0, self.peak - self.baseline)
