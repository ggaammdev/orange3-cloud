"""Why the job worker died: out of memory, or a crash.

Jobs run in one Dask worker process under a nanny (see :mod:`.jobs`). When
that process dies, Dask only says that the worker was lost (``KilledWorker``).
This module finds out why:

- **Memory limit.** The nanny's memory monitor logs the event
  ``worker-restart-memory`` just before it terminates a worker that went over
  its limit. :class:`MonitoredNanny` notes it.
- **Kernel OOM killer.** On Linux, the ``oom_kill`` count in the cgroup's
  ``memory.events`` goes up when the kernel kills a process for memory.
  :class:`MonitoredNanny` compares it at the worker's start and death.
- **Anything else** (a segfault in a compiled library, ``os._exit``, ...) is a
  crash. :class:`CrashLogPlugin` writes the worker's warnings and a
  ``faulthandler`` traceback to a file, and the nanny keeps its last lines.

The nanny runs in the API server's process (``LocalCluster``), so it hands
each :class:`WorkerDeath` to the job manager directly.
"""

from __future__ import annotations

import faulthandler
import logging
import signal
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from distributed import Nanny, WorkerPlugin

from orangecontrib.cloud.protocol.errors import OUT_OF_MEMORY_MESSAGE, ErrorCode

log = logging.getLogger(__name__)

#: Lines of the worker's log kept when it dies.
LOG_TAIL_LINES = 50
_LOG_TAIL_BYTES = 64 * 1024

#: Topic of the event that the nanny's memory monitor logs before a restart.
MEMORY_RESTART_TOPIC = "worker-restart-memory"


@dataclass(frozen=True)
class WorkerDeath:
    """How a worker process ended, as the nanny saw it."""

    address: str | None
    #: The process's exit code; negative for a signal (``-11`` is SIGSEGV).
    exitcode: int | None
    #: The nanny stopped it for going over its memory limit.
    memory_limit: bool = False
    #: The kernel's OOM killer killed a process in this cgroup meanwhile;
    #: ``None`` when that can't be told (not Linux, no cgroup v2).
    oom_killed: bool | None = None
    #: The last lines of the worker's log; ``None`` if unreadable.
    log_tail: str | None = None

    @property
    def out_of_memory(self) -> bool:
        return self.memory_limit or (bool(self.oom_killed) and self.exitcode == -signal.SIGKILL)

    def describe_exit(self) -> str:
        code = self.exitcode
        if code is None:
            return "exit status unknown"
        if code < 0:
            try:
                name = signal.Signals(-code).name
            except ValueError:
                name = f"signal {-code}"
            return f"killed by {name}"
        return f"exit code {code}"

    def error(self) -> tuple[ErrorCode, str, str]:
        """Error code, message and details (for ``remote_traceback``)."""
        if self.out_of_memory:
            cause = (
                "it went over its memory limit"
                if self.memory_limit
                else "the system's out-of-memory killer stopped it"
            )
            details = f"The worker process on the machine stopped because {cause}."
            return ErrorCode.OUT_OF_MEMORY, OUT_OF_MEMORY_MESSAGE, self._details(details)
        what = self.describe_exit()
        if self.exitcode == -signal.SIGKILL and self.oom_killed is None:
            what += "; the system may have stopped it for using too much memory"
        message = (
            f"The computation crashed on the machine ({what}). "
            "Tables and models in memory were lost."
        )
        status = self.describe_exit()
        if self.exitcode is not None and self.exitcode < 0:
            status += f" (exit code {self.exitcode})"
        details = f"The worker process on the machine ({self.address}) stopped: {status}."
        return ErrorCode.WORKER_CRASHED, message, self._details(details)

    def _details(self, first: str) -> str:
        if self.log_tail is None:
            tail = "The worker's log isn't available."
        elif not self.log_tail.strip():
            tail = "The worker's log is empty."
        else:
            tail = f"Last lines of the worker's log:\n{self.log_tail.rstrip()}"
        return f"{first}\n\n{tail}\n"


def unknown_death_error(address: str | None) -> tuple[ErrorCode, str, str]:
    """The error when the nanny didn't report how the worker ended."""
    message = (
        "The computation crashed on the machine (the cause is unknown). "
        "Tables and models in memory were lost."
    )
    details = (
        f"The worker process on the machine ({address}) stopped; its exit status is unknown.\n"
    )
    return ErrorCode.WORKER_CRASHED, message, details


def oom_kill_count() -> int | None:
    """Processes in this cgroup killed by the kernel's OOM killer so far.

    ``None`` when not available (not Linux, no cgroup v2, the root cgroup).
    """
    try:
        lines = Path("/proc/self/cgroup").read_text().splitlines()
        path = next(line[3:] for line in lines if line.startswith("0::"))
        events = Path("/sys/fs/cgroup", path.strip().lstrip("/"), "memory.events")
        for line in events.read_text().splitlines():
            key, _, value = line.partition(" ")
            if key == "oom_kill":
                return int(value)
    except (OSError, StopIteration, ValueError):  # not available: unknown
        return None
    return None


def read_log_tail(path: str | Path, lines: int = LOG_TAIL_LINES) -> str | None:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - _LOG_TAIL_BYTES))
            data = f.read()
    except OSError:
        return None
    return "\n".join(data.decode("utf-8", "replace").splitlines()[-lines:])


class CrashLogPlugin(WorkerPlugin):
    """Runs in the worker: writes its warnings and errors, and the traceback
    of a fatal signal (``faulthandler``), to ``path`` (truncated per process).
    """

    name = "orange3-cloud-crash-log"

    def __init__(self, path: str):
        self.path = path

    def setup(self, worker) -> None:
        # Line-buffered and kept open for the life of the process, so the
        # lines are on disk when it dies.
        stream = open(self.path, "w", buffering=1, encoding="utf-8", errors="replace")  # noqa: SIM115
        # Only the crashing thread: the others are mostly Dask's own.
        faulthandler.enable(file=stream, all_threads=False)
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.WARNING)
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s: %(message)s"))
        # Dask's loggers don't propagate to the root logger.
        for name in ("", "distributed"):
            logging.getLogger(name).addHandler(handler)
        self._stream = stream


class MonitoredNanny(Nanny):
    """A nanny that reports how its worker process ended to ``on_death``."""

    def __init__(
        self,
        *args,
        crash_log: str,
        on_death: Callable[[WorkerDeath], None],
        **kwargs,
    ):
        self._crash_log = crash_log
        self._on_death = on_death
        self._memory_restart_address: str | None = None
        self._oom_kills_at_start: int | None = None
        super().__init__(*args, **kwargs)

    async def instantiate(self):
        self._oom_kills_at_start = oom_kill_count()
        return await super().instantiate()

    def log_event(self, topic, msg):
        # The memory monitor logs this just before it terminates the worker.
        topics = [topic] if isinstance(topic, str) else list(topic)
        if MEMORY_RESTART_TOPIC in topics and isinstance(msg, dict):
            self._memory_restart_address = msg.get("worker")
        return super().log_event(topic, msg)

    def _on_worker_exit_sync(self, exitcode):
        try:
            before, after = self._oom_kills_at_start, oom_kill_count()
            address = self.worker_address
            death = WorkerDeath(
                address=address,
                exitcode=exitcode,
                memory_limit=address is not None and self._memory_restart_address == address,
                oom_killed=None if before is None or after is None else after > before,
                log_tail=read_log_tail(self._crash_log),
            )
            self._on_death(death)
        except Exception:  # noqa: BLE001 - nanny callback: logged, the restart goes on
            log.exception("Couldn't record how the worker process ended")
        return super()._on_worker_exit_sync(exitcode)
