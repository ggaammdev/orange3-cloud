"""Work that holds Python's GIL, in processes forked from the job worker.

The job worker is one process with many threads (:mod:`.jobs`). Code that
holds Python's GIL, such as Orange's own learners, runs in only one of those
threads at a time. :class:`ForkedParts` runs such work in child processes
instead. A child is a fork of the worker, so it shares the worker's memory
(the tables) copy-on-write: nothing is copied or pickled but the arguments
and the result, with the add-on's pickler
(:mod:`orangecontrib.cloud.protocol.pickles`), as every pickle.

**Forking safely.** A fork copies only the thread that calls it. A lock that
another thread held at that moment stays locked in the child forever, so a
child of a busy multi-threaded process may hang. Therefore:

- The worker forks only once per job: a *zygote*, when the job starts and
  before its own parts run. The zygote has a single thread; it uses no
  locks, BLAS or OpenMP, and only forks a child per piece of work, so the
  children are forks of a clean, single-threaded process.
- Children run OpenMP with one thread (GNU OpenMP's thread pool doesn't
  survive a fork; BLAS restarts its own), write their result to a file, and
  end with ``os._exit``: no ``atexit`` handlers and nothing of Dask's.
- The zygote and the children die with their parent (Linux's
  ``PR_SET_PDEATHSIG``), are the kernel OOM killer's first choice
  (``oom_score_adj``), so that it spares the worker and its tables, and form
  their own process group, which :meth:`ForkedParts.close` kills.

How a child ended tells what happened: a result; ``MemoryError``; another
exception (:class:`PartFailed`, with its traceback); stopped by
:meth:`ForkedParts.stop` (:class:`PartStopped`); killed with ``SIGKILL`` by
someone else, i.e. the OOM killer (:class:`PartKilled`, a ``MemoryError``);
or another signal, e.g. a segfault (:class:`PartCrashed`).

Linux only (:func:`available`).
"""

from __future__ import annotations

import ctypes
import faulthandler
import logging
import os
import shutil
import signal
import sys
import tempfile
import threading
import traceback
import warnings
from collections.abc import Callable
from typing import Any

from orangecontrib.cloud.protocol.pickles import dump, dumps, load, loads

__all__ = [
    "ForkedParts",
    "PartCrashed",
    "PartFailed",
    "PartKilled",
    "PartStopped",
    "available",
]

log = logging.getLogger(__name__)

#: Seconds between checks while waiting for a child.
POLL_SECONDS = 0.05

_EXIT_OK, _EXIT_ERROR, _EXIT_MEMORY = 0, 1, 3
_PR_SET_PDEATHSIG = 1
#: Children are killed first when the machine runs out of memory.
_OOM_SCORE_ADJ = "1000"


class PartStopped(Exception):
    """The child was stopped by :meth:`ForkedParts.stop` (or ``should_stop``)."""


class PartKilled(MemoryError):
    """The child was killed with ``SIGKILL``, by the kernel's OOM killer."""


class PartCrashed(RuntimeError):
    """The child ended by a signal other than ``SIGKILL`` (e.g. a segfault)."""


class PartFailed(RuntimeError):
    """The work raised an exception in the child; the message has its traceback."""


def available() -> bool:
    """Whether work can run in forked processes here (Linux)."""
    return sys.platform.startswith("linux") and hasattr(os, "fork")


def _die_with_parent(parent: int) -> None:
    """Have the kernel kill this process when its parent ends."""
    ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)
    if os.getppid() != parent:  # the parent ended before prctl
        os._exit(_EXIT_ERROR)


def _signal_name(code: int) -> str:
    try:
        return signal.Signals(-code).name
    except ValueError:
        return f"signal {-code}"


class ForkedParts:
    """Runs ``work(args)`` in children of a zygote forked by :meth:`start`.

    :meth:`run` may be called from many threads at once; each call blocks
    until its child ends. Pieces of work are named (``name``, unique while
    running); the name is also the result file's name.
    """

    def __init__(self, work: Callable[[Any], Any]):
        self.work = work
        self.zygote: int | None = None  #: the zygote's process ID
        self.folder = ""
        self._requests: Any = None  # worker -> zygote: (name, args)
        self._status: Any = None  # zygote -> worker: ("started"|"exited", name, value)
        self._send_lock = threading.Lock()
        self._recv_lock = threading.Lock()
        self._pids: dict[str, int] = {}
        self._exits: dict[str, int] = {}
        self._stopping: set[str] = set()
        self._closed = False
        self._zygote_exit: int | None = None

    # ------------------------------------------------------------------
    # The worker's side

    def start(self) -> None:
        """Fork the zygote (call it where the job's own threads are idle)."""
        from multiprocessing.connection import Pipe

        self.folder = tempfile.mkdtemp(prefix="orange3-cloud-parts-")
        requests_in, requests_out = Pipe(duplex=False)
        status_in, status_out = Pipe(duplex=False)
        parent = os.getpid()
        with warnings.catch_warnings():
            # Python 3.12+ warns about forking a multi-threaded process; the
            # module's documentation says why this fork is safe.
            warnings.filterwarnings("ignore", "This process", DeprecationWarning)
            pid = os.fork()
        if pid == 0:
            code = _EXIT_ERROR
            try:
                requests_out.close()
                status_in.close()
                self._zygote_main(parent, requests_in, status_out)
                code = _EXIT_OK
            finally:
                os._exit(code)
        requests_in.close()
        status_out.close()
        try:
            os.setpgid(pid, pid)  # also done by the zygote: whichever runs first
        except OSError:
            log.debug("The zygote set its process group first")
        self.zygote, self._requests, self._status = pid, requests_out, status_in

    def run(self, name: str, args: Any, should_stop: Callable[[], bool] = lambda: False) -> Any:
        """Run ``work(args)`` in a new child and return its result. Raises
        :class:`PartStopped` if it was stopped, ``MemoryError`` (or
        :class:`PartKilled`), :class:`PartFailed` or :class:`PartCrashed`."""
        with self._send_lock:
            if self._closed:
                raise PartStopped(name)
            self._requests.send_bytes(dumps((name, args)))
        killed = False
        while (code := self._exit_code(name)) is None:
            if self._closed:
                raise PartStopped(name)
            if self._zygote_lost():
                self._raise_zygote_lost()
            if not killed and (name in self._stopping or should_stop()):
                killed = self._kill(name)
        self._pids.pop(name, None)
        killed = killed or name in self._stopping
        self._stopping.discard(name)
        return self._outcome(name, code, killed)

    def stop(self, name: str) -> None:
        """Stop the child running ``name``: its :meth:`run` raises
        :class:`PartStopped` (unless it finished first)."""
        self._stopping.add(name)

    def memory(self, name: str | None = None) -> int:
        """Memory used only by the child running ``name`` (its USS; 0 if it
        isn't running), or by the zygote and all children."""
        import psutil

        pids = [self._pids.get(name)] if name is not None else [self.zygote, *self._pids.values()]
        total = 0
        for pid in pids:
            if pid is None:
                continue
            try:
                total += psutil.Process(pid).memory_full_info().uss
            except psutil.NoSuchProcess:  # ended meanwhile: uses nothing
                continue
        return total

    def close(self) -> None:
        """Kill the zygote and the children, and delete their files."""
        if self.zygote is None:
            return
        with self._send_lock, self._recv_lock:
            self._closed = True
            for kill, target in ((os.killpg, self.zygote), (os.kill, self.zygote)):
                try:
                    kill(target, signal.SIGKILL)
                except ProcessLookupError:
                    log.debug("Forked processes already ended")
            self._reap_zygote(block=True)
            self._requests.close()
            self._status.close()
        shutil.rmtree(self.folder, ignore_errors=True)

    def _exit_code(self, name: str) -> int | None:
        """The child's exit code once it ended, reading the zygote's news."""
        if name not in self._exits and self._recv_lock.acquire(timeout=POLL_SECONDS):
            try:
                if not self._closed and self._status.poll(POLL_SECONDS):
                    while True:
                        kind, key, value = loads(self._status.recv_bytes())
                        (self._pids if kind == "started" else self._exits)[key] = value
                        if not self._status.poll():
                            break
            except (EOFError, OSError):  # the zygote ended: see _zygote_lost
                self._reap_zygote(block=True)
            finally:
                self._recv_lock.release()
        return self._exits.pop(name, None)

    def _kill(self, name: str) -> bool:
        pid = self._pids.get(name)
        if pid is None:  # not started yet
            return False
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            log.debug("Child %s ended before it was stopped", name)
        return True

    def _reap_zygote(self, block: bool = False) -> None:
        if self._zygote_exit is None and self.zygote is not None:
            pid, status = os.waitpid(self.zygote, 0 if block else os.WNOHANG)
            if pid:
                self._zygote_exit = os.waitstatus_to_exitcode(status)

    def _zygote_lost(self) -> bool:
        return self._zygote_exit is not None and not self._closed

    def _raise_zygote_lost(self) -> None:
        code = self._zygote_exit or 0
        if code == -signal.SIGKILL:
            raise PartKilled("The machine ran out of memory (the forked processes were killed).")
        what = _signal_name(code) if code < 0 else f"exit code {code}"
        raise PartCrashed(f"The forked processes ended ({what}).")

    def _path(self, name: str, suffix: str) -> str:
        return os.path.join(self.folder, f"{name}.{suffix}")

    def _outcome(self, name: str, code: int, killed: bool) -> Any:
        errors = self._read_and_delete(name, "error")
        if code == _EXIT_OK:
            with open(self._path(name, "result"), "rb") as f:
                result = load(f)
            os.unlink(self._path(name, "result"))
            return result
        if killed:
            raise PartStopped(name)
        if code == _EXIT_MEMORY:
            raise MemoryError()
        if code == _EXIT_ERROR:
            raise PartFailed(errors)
        if code == -signal.SIGKILL:
            raise PartKilled(f"Part {name} was killed (out of memory).")
        what = f"killed by {_signal_name(code)}" if code < 0 else f"exit code {code}"
        message = f"A part of the computation crashed on the machine ({what})."
        raise PartCrashed(f"{message}\n\n{errors}" if errors.strip() else message)

    def _read_and_delete(self, name: str, suffix: str) -> str:
        """The text of the child's file (empty if there is none)."""
        path = self._path(name, suffix)
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        os.unlink(path)
        return text

    # ------------------------------------------------------------------
    # The zygote and the children (forked processes)

    def _zygote_main(self, parent: int, requests: Any, status: Any) -> None:
        os.setpgid(0, 0)
        _die_with_parent(parent)
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write(_OOM_SCORE_ADJ)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, signal.SIG_DFL)
        me, children = os.getpid(), {}
        while True:
            if requests.poll(POLL_SECONDS):
                try:
                    name, args = loads(requests.recv_bytes())
                except EOFError:  # the worker closed: stop
                    break
                pid = os.fork()
                if pid == 0:
                    self._child_main(me, name, args, (requests, status))
                children[pid] = name
                status.send_bytes(dumps(("started", name, pid)))
            while children:
                pid, wait_status = os.waitpid(-1, os.WNOHANG)
                if not pid:
                    break
                exit_code = os.waitstatus_to_exitcode(wait_status)
                status.send_bytes(dumps(("exited", children.pop(pid), exit_code)))
        for pid in children:
            os.kill(pid, signal.SIGKILL)

    def _child_main(self, zygote: int, name: str, args: Any, pipes: tuple) -> None:
        code = _EXIT_ERROR
        try:
            for pipe in pipes:
                pipe.close()
            _die_with_parent(zygote)
            # Errors, and the traceback of a crash, go to a file for the worker.
            errors = open(self._path(name, "error"), "w", buffering=1, encoding="utf-8")  # noqa: SIM115
            faulthandler.enable(file=errors, all_threads=False)
            try:
                result = self.work(args)
            except MemoryError:
                code = _EXIT_MEMORY
                raise
            except BaseException:
                errors.write(traceback.format_exc())
                raise
            tmp = self._path(name, "tmp")
            with open(tmp, "wb") as f:
                dump(result, f)
            os.replace(tmp, self._path(name, "result"))
            code = _EXIT_OK
        finally:
            os._exit(code)
