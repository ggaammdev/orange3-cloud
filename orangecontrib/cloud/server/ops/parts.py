"""The parts of an ``evaluate`` job, run side by side in the job worker.

A part is one learner on one fold (or on the test data), and every part is
its own Dask task: the job's task secedes from the worker's thread pool
(``worker_client``) and submits the parts to the same worker, which holds
the table. The parts read the table and the splits from this process
(:data:`RUNS`), so nothing is copied between processes.

**Threads and processes.** scikit-learn learners release Python's GIL while
they fit, so their parts compute side by side in the worker's threads. Other
learners (e.g. Orange's own tree) hold the GIL. On Linux their parts run in
processes forked from the worker (:mod:`..forked`), which share its memory
copy-on-write. Elsewhere at most :data:`MAX_PARALLEL_GIL_BOUND` of them run
at a time, in threads, which gains no speed.

**Memory.** A part starts only while the memory counted for the running
parts and the new one fits in the budget: 0.9 × the worker's memory limit,
less what the worker and the forked processes use when the parts start. One
part always runs.

- *In threads,* the first part of each learner runs alone with all cores
  while the worker's memory is sampled. Its parts then count with that peak,
  at least :data:`MIN_PART_MEMORY_FACTOR` × the tables, plus
  :data:`MEMORY_MARGIN`.
- *In processes,* each process's own memory (USS) is sampled while it runs,
  so the first parts don't need to run alone. A learner's parts count with
  the floor above until one of them uses more, then with the most that one
  used (plus the margin). When the processes together use more than the
  budget, the part started last is stopped and runs again later, and from
  then on no more processes run at a time than were left. A part that the
  system's OOM killer stops runs again in the same way; if it ran alone, the
  job fails with ``out_of_memory``.

**Cores.** Each part's learner gets ``threads // parallel`` cores (``n_jobs``
of scikit-learn learners whose default is one core or all cores), and BLAS
and OpenMP are limited the same way (OpenMP to one thread in processes), so
the parts don't oversubscribe the CPUs.
"""

from __future__ import annotations

import asyncio
import functools
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from orangecontrib.cloud.protocol.errors import ErrorCode
from orangecontrib.cloud.protocol.pickles import loads
from orangecontrib.cloud.server import forked
from orangecontrib.cloud.server.forked import ForkedParts, PartCrashed, PartKilled, PartStopped
from orangecontrib.cloud.server.resources import LIMIT_SHARE, memory_limit, rss, worker_threads
from orangecontrib.cloud.server.worker import (
    JobContext,
    OperationError,
    current_worker,
    worker_store,
)

__all__ = [
    "MAX_PARALLEL_GIL_BOUND",
    "MIN_PART_MEMORY_FACTOR",
    "Part",
    "Run",
    "Schedule",
    "gil_bound",
    "parallel_plan",
    "run_parts",
]

#: Parts of learners that hold Python's GIL gain nothing from running side
#: by side in threads, only memory use, so at most this many of them run at
#: a time where they can't run in processes.
MAX_PARALLEL_GIL_BOUND = 2

#: Least memory counted for one part, in multiples of the tables' memory
#: (the copied training and test rows), whatever the measurement says.
MIN_PART_MEMORY_FACTOR = 2

#: Memory counted for a part: the measured memory plus this share.
MEMORY_MARGIN = 0.1

#: The forked processes' memory is sampled this often (seconds).
SAMPLE_SECONDS = 1.0

#: Outcomes of :func:`run_part`: done; the job is gone (cancelled); stopped
#: to run again later; killed by the system's OOM killer.
DONE, GONE, AGAIN, KILLED = "done", "gone", "again", "killed"

Key = tuple[int, int]  # (fold, learner)


def parallel_plan(n_parts: int, threads: int, part_bytes: int, free_bytes: int) -> tuple[int, int]:
    """How many parts run at a time, and how many cores each part's learner
    gets: ``(parallel, threads_per_part)``."""
    parallel = max(1, min(n_parts, threads))
    if part_bytes > 0:
        parallel = max(1, min(parallel, free_bytes // part_bytes))
    return parallel, max(1, threads // parallel)


def gil_bound(learner: Any, data: Any) -> bool:
    """False for scikit-learn learners, whose fitting releases Python's GIL,
    so that their parts compute in parallel in the worker's threads."""
    from Orange.base import SklLearner
    from Orange.modelling import Fitter

    if isinstance(learner, Fitter):
        try:
            learner = learner.get_learner(data)
        except TypeError:
            return True
    return not isinstance(learner, SklLearner)


def _limit_cores(learner: Any, data: Any, n_jobs: int) -> None:
    """Give a scikit-learn learner ``n_jobs`` cores, unless it was set to
    fewer (``n_jobs`` of 1, ``None`` and -1 are defaults: one or all)."""
    from Orange.base import SklLearner
    from Orange.modelling import Fitter

    if isinstance(learner, Fitter):
        try:
            learner = learner.get_learner(data)  # cached, so the fit uses it
        except TypeError:
            return
    params = getattr(learner, "params", None)
    if isinstance(learner, SklLearner) and isinstance(params, dict) and "n_jobs" in params:
        current = params["n_jobs"]
        if current in (1, None, -1) or (isinstance(current, int) and current > n_jobs):
            params["n_jobs"] = n_jobs


# --------------------------------------------------------------------------
# One part


@dataclass
class Part:
    """The predictions of one learner on one fold."""

    fold: int
    learner: int
    n_rows: int
    failed: str | None = None
    predicted: Any = None
    probabilities: Any = None
    train_time: float = 0.0
    test_time: float = 0.0


@dataclass
class Run:
    """What the parts of one ``evaluate`` job share in this process."""

    data: Any
    test_data: Any
    indices: list[tuple[Any, Any]] | None  # (train, test) per fold; None: test data
    learners: list[bytes]  # pickles: every part unpickles its own copy
    names: list[str]
    gil_bound: list[bool]
    #: Per learner: its parts run in forked processes (set by run_parts).
    in_process: list[bool] = field(default_factory=list)
    forked: ForkedParts | None = None
    parts: dict[Key, Part] = field(default_factory=dict)


#: Running ``evaluate`` jobs by job ID (their parts look them up here).
RUNS: dict[str, Run] = {}
RUNS_LOCK = threading.Lock()


def _part_name(key: Key) -> str:
    return f"{key[0]}-{key[1]}"


def run_part(job_id: str, fold: int, learner_i: int, n_jobs: int) -> str:
    """Train learner ``learner_i`` on fold ``fold``'s training rows with
    ``n_jobs`` cores and predict its test rows (as Orange's ``Validation``),
    in this thread or in a forked process. A Dask task; returns
    :data:`DONE`, :data:`GONE`, :data:`AGAIN` or :data:`KILLED`."""
    with RUNS_LOCK:
        run = RUNS.get(job_id)
    cancelled = worker_store().cancelled
    if run is None or job_id in cancelled:
        return GONE
    if run.forked is not None and run.in_process[learner_i]:
        try:
            name, args = _part_name((fold, learner_i)), (fold, learner_i, n_jobs)
            part = run.forked.run(name, args, lambda: job_id in cancelled)
        except PartStopped:
            return GONE if job_id in cancelled else AGAIN
        except PartKilled:
            return KILLED
    else:
        from threadpoolctl import threadpool_limits

        with threadpool_limits(limits=n_jobs, user_api="openmp"):
            part = _fit_predict(run, fold, learner_i, n_jobs)
        _trim_memory()
    with RUNS_LOCK:
        run.parts[(fold, learner_i)] = part
    return DONE


def _part_in_child(run: Run, args: tuple[int, int, int]) -> Part:
    """:func:`_fit_predict` in a forked process (one OpenMP thread)."""
    from threadpoolctl import threadpool_limits

    fold, learner_i, n_jobs = args
    with (
        threadpool_limits(limits=1, user_api="openmp"),
        threadpool_limits(limits=n_jobs, user_api="blas"),
    ):
        return _fit_predict(run, fold, learner_i, n_jobs)


def _trim_memory() -> None:
    """Give memory freed by the part back to the system (Linux). glibc keeps
    a heap per thread and would otherwise keep each thread's peak, so a
    learner that needs a lot of memory would, part by part, in thread after
    thread, use up the machine's memory."""
    global _malloc_trim
    if _malloc_trim is None:
        try:
            import ctypes

            _malloc_trim = ctypes.CDLL("libc.so.6").malloc_trim
        except (OSError, AttributeError):
            _malloc_trim = False
    if _malloc_trim:
        _malloc_trim(0)


_malloc_trim: Any = None


def _fit_predict(run: Run, fold: int, learner_i: int, n_jobs: int) -> Part:
    if run.indices is None:
        train, test = run.data, run.test_data
    else:
        train_i, test_i = run.indices[fold]
        train, test = run.data[train_i], run.data[test_i]
    part = Part(fold, learner_i, len(test))
    learner = loads(run.learners[learner_i])
    try:
        if not train or not test:
            raise RuntimeError("Test fold is empty")
        _limit_cores(learner, train, n_jobs)
        started = time.perf_counter()
        model = learner(train)
        part.train_time = time.perf_counter() - started
        started = time.perf_counter()
        class_var = train.domain.class_var
        if class_var is not None and class_var.is_discrete:
            part.predicted, part.probabilities = model(test, model.ValueProbs)
        else:
            part.predicted = model(test, model.Value)
        part.test_time = time.perf_counter() - started
    except MemoryError:
        raise
    except Exception as exc:  # noqa: BLE001 - job runner: reported as the learner's error
        part.failed = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return part


# --------------------------------------------------------------------------
# All parts


@dataclass
class Schedule:
    """How the parts ran: values of the job's result."""

    parallel: int = 1  #: most parts that ran at a time
    threads_per_part: int = 1  #: cores of each part after the first ones
    part_memory: dict[int, int] = field(default_factory=dict)  #: by learner
    run_again: int = 0  #: parts stopped for memory that ran again


def run_parts(
    ctx: JobContext,
    run: Run,
    keys: list[Key],
    min_part_bytes: int,
    max_parallel: int | None = None,
) -> Schedule:
    """Run the parts as Dask tasks in this worker, at most ``max_parallel``
    at a time, with progress after each (one after another in this thread
    when not in a Dask worker: tests). See the module's documentation."""
    runner = _PartRunner(ctx, run, keys, max_parallel)
    if current_worker() is None:
        runner.run_here()
        return runner.schedule

    from distributed import worker_client

    run.in_process = [bound and forked.available() for bound in run.gil_bound]
    if any(run.in_process):
        _trim_memory()  # so that the zygote doesn't keep freed memory
        run.forked = ForkedParts(functools.partial(_part_in_child, run))
        run.forked.start()
    try:
        with worker_client() as client:
            runner.client = client
            try:
                runner.measure_first_parts(min_part_bytes)
                if runner.todo:
                    runner.run_the_rest()
            finally:
                if runner.running:
                    client.cancel(list(runner.running))
    finally:
        if run.forked is not None:
            run.forked.close()  # also stops parts still running
    return runner.schedule


class _PartRunner:
    """The state of :func:`run_parts`: parts to do, running and done."""

    def __init__(self, ctx: JobContext, run: Run, keys: list[Key], max_parallel: int | None):
        self.ctx, self.run = ctx, run
        self.total = len(keys)
        self.done = 0
        self.todo = list(keys)
        self.threads = worker_threads()
        #: Most parts at a time, and most of them in processes.
        self.limit = min(self.threads, max_parallel or self.threads)
        self.process_cap = self.limit
        self.schedule = Schedule(threads_per_part=self.threads)
        self.running: dict[Any, Key] = {}
        self.started: dict[Key, float] = {}
        self.sampled = 0.0
        self.client: Any = None  #: the worker's Dask client

    def report(self, key: Key) -> None:
        """Progress after the part ``key`` (fold, learner) is done."""
        self.done += 1
        fold, learner_i = key
        where = f"fold {fold + 1}" if self.run.indices is not None else "test data"
        self.ctx.progress(
            0.98 * self.done / self.total,
            f"{self.done} of {self.total} done ({self.run.names[learner_i]}, {where})",
        )

    def run_here(self) -> None:
        """Run the parts one after another in this thread (tests)."""
        for key in self.todo:
            self.ctx.check_cancelled()
            run_part(self.ctx.job_id, *key, self.threads)
            self.report(key)

    def submit(self, key: Key, n_jobs: int) -> None:
        """Start the part ``key`` as a Dask task with ``n_jobs`` cores."""
        self.todo.remove(key)
        job_id = self.ctx.job_id
        future = self.client.submit(
            run_part,
            job_id,
            *key,
            n_jobs,
            key=f"evaluate-{job_id}-{key[0]}-{key[1]}-{time.monotonic_ns()}",
            pure=False,
        )
        self.running[future] = key
        self.started[key] = time.monotonic()

    def collect(self, timeout: float) -> None:
        """Wait up to ``timeout`` seconds for running parts and handle the
        finished ones: done, or back to the parts to do."""
        from distributed import wait

        try:
            finished = wait(list(self.running), timeout=timeout, return_when="FIRST_COMPLETED").done
        except (TimeoutError, asyncio.TimeoutError):  # different before Python 3.11
            finished = set()
        self.ctx.check_cancelled()
        for future in finished:
            key = self.running.pop(future)
            if self._outcome(future) == AGAIN:
                self.todo.insert(0, key)
                self.schedule.run_again += 1
            else:
                self.report(key)

    def _outcome(self, future: Any) -> str:
        try:
            outcome = future.result()  # raises the part's exception (e.g. MemoryError)
        except PartCrashed as exc:
            raise OperationError(ErrorCode.WORKER_CRASHED, str(exc)) from exc
        if outcome == KILLED:  # by the OOM killer: again, with fewer processes
            if self.process_cap <= 1:
                raise MemoryError("A part ran out of memory on its own.")
            self.process_cap = max(1, len(self._in_process(self.running.values())))
            return AGAIN
        return outcome

    def _in_process(self, keys: Any) -> list[Key]:
        return [key for key in keys if self.run.in_process[key[1]]]

    def _gil_bound_thread(self, key: Key) -> bool:
        return self.run.gil_bound[key[1]] and not self.run.in_process[key[1]]

    def measure_first_parts(self, min_part_bytes: int) -> None:
        """Step 1: run the first part of each learner in threads alone and
        measure its memory; the parts in processes start with the floor."""
        first: dict[int, Key] = {}
        for key in self.todo:
            first.setdefault(key[1], key)
        for learner_i, key in first.items():
            if self.run.in_process[learner_i]:
                self.schedule.part_memory[learner_i] = int((1 + MEMORY_MARGIN) * min_part_bytes)
                continue
            _trim_memory()
            base = peak = rss()
            self.submit(key, self.threads)
            while self.running:
                self.collect(0.1)
                peak = max(peak, rss())
            needed = max(peak - base, min_part_bytes)
            self.schedule.part_memory[learner_i] = int((1 + MEMORY_MARGIN) * needed)

    def run_the_rest(self) -> None:
        """Step 2: run the other parts side by side."""
        from threadpoolctl import threadpool_limits

        schedule, todo = self.schedule, self.todo
        _trim_memory()
        processes = self.run.forked.memory() if self.run.forked is not None else 0
        budget = LIMIT_SHARE * memory_limit() - rss() - processes
        n_gil = sum(map(self._gil_bound_thread, todo))
        possible = min(self.limit, len(todo) - n_gil + min(n_gil, MAX_PARALLEL_GIL_BOUND))
        # Cores per part: for the parallelism that the parts' average
        # memory allows.
        mean = sum(schedule.part_memory[k[1]] for k in todo) // len(todo)
        _, per_part = parallel_plan(possible, self.threads, mean, int(max(budget, 0)))
        schedule.threads_per_part = per_part
        # BLAS limits are for the whole process; parts set OpenMP's themselves.
        with threadpool_limits(limits=per_part, user_api="blas"):
            while self.todo or self.running:
                while (key := self._startable(budget)) is not None:
                    self.submit(key, per_part)
                schedule.parallel = max(schedule.parallel, len(self.running))
                self.collect(0.5)
                self._watch_processes(budget)

    def _startable(self, budget: float) -> Key | None:
        """The next part that may start now, if any."""
        if not self.todo or len(self.running) >= self.limit:
            return None
        part_memory = self.schedule.part_memory
        running = list(self.running.values())
        committed = sum(part_memory[k[1]] for k in running)
        n_gil = sum(map(self._gil_bound_thread, running))
        n_processes = len(self._in_process(running))
        for key in self.todo:
            if self._gil_bound_thread(key) and n_gil >= MAX_PARALLEL_GIL_BOUND:
                continue
            if self.run.in_process[key[1]] and n_processes >= self.process_cap:
                continue
            if running and committed + part_memory[key[1]] > budget:
                continue
            return key
        return None

    def _watch_processes(self, budget: float) -> None:
        """Sample the processes' memory: raise their learners' estimates, and
        stop the part started last if they use more than the budget."""
        running = self._in_process(self.running.values())
        now = time.monotonic()
        if not running or now - self.sampled < SAMPLE_SECONDS:
            return
        self.sampled = now
        part_memory = self.schedule.part_memory
        used = {key: self.run.forked.memory(_part_name(key)) for key in running}
        for (_, learner_i), uss in used.items():
            part_memory[learner_i] = max(part_memory[learner_i], int((1 + MEMORY_MARGIN) * uss))
        in_threads = [k for k in self.running.values() if not self.run.in_process[k[1]]]
        threads_memory = sum(part_memory[k[1]] for k in in_threads)
        if len(running) > 1 and threads_memory + sum(used.values()) > budget:
            last = max(running, key=self.started.__getitem__)
            self.run.forked.stop(_part_name(last))
            self.process_cap = len(running) - 1
