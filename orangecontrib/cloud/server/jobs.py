"""Jobs: operations that run on the machine's Dask cluster.

Layout
------
The machine runs one ``distributed.LocalCluster`` with **one worker process
and many threads** (one per CPU). Tables and stored objects (models, distance
matrices, ...) live in that worker's memory, in :class:`~.worker.WorkerStore`, so every
job reads them without copying. NumPy and scikit-learn release the GIL, so
the threads compute in parallel. The worker has a memory limit; when it goes
over, Dask's nanny restarts it and the jobs that were running fail with
``out_of_memory``. If the worker dies for another reason (e.g. a segfault in
a compiled library), they fail with ``worker_crashed`` and the end of the
worker's log (:mod:`.crashes`). Either way the API server stays up, but the
tables and objects held by the worker are gone.

Operations and what they get (:class:`~.worker.JobContext`) are in
:mod:`.worker`. The server side is :class:`JobManager`
(``app.state.server.job_manager``) and the routes in :data:`router`.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import shutil
import signal
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ValidationError
from starlette.datastructures import UploadFile

from orangecontrib.cloud.protocol.errors import ErrorCode, ErrorResponse
from orangecontrib.cloud.protocol.jobs import (
    JobInfo,
    JobKind,
    JobRequest,
    JobResult,
    JobState,
    ObjectInfo,
    ObjectKind,
)
from orangecontrib.cloud.protocol.tables import TableInfo
from orangecontrib.cloud.protocol.wire import PAYLOAD_HEADER, PAYLOAD_PICKLE, SPEC_PART
from orangecontrib.cloud.server.errors import ApiError
from orangecontrib.cloud.server.worker import (
    EVENT_TOPIC,
    OPERATIONS,
    JobContext,
    Operation,
    delete_object,
    delete_table,
    error_dict,
    in_worker_thread,
    mark_cancelled,
    new_id,
    operation,
    run_operation,
    utc_now,
)

__all__ = [
    "JobManager",
    "get_jobs",
    "job_system",
    "read_multipart",
    "router",
]

log = logging.getLogger(__name__)

#: Largest pickle part accepted in ``POST /v1/jobs``.
MAX_PART_BYTES = 4 * 1024**3


# --------------------------------------------------------------------------
# Server side: the job manager
# --------------------------------------------------------------------------


@dataclass
class _Job:
    id: str
    kind: str
    state: JobState = JobState.QUEUED
    progress: float = 0.0
    status: str = ""
    error: ErrorResponse | None = None
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: JobResult | None = None
    future: Any = None

    def info(self) -> JobInfo:
        return JobInfo(
            id=self.id,
            kind=self.kind,
            state=self.state,
            progress=self.progress,
            status=self.status,
            error=self.error,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
        )

    def finish(self, state: JobState, error: ErrorResponse | None = None) -> None:
        self.state = state
        self.error = error
        self.finished_at = utc_now()
        self.future = None


def _default_worker_memory(total: int) -> int:
    """Memory limit of the worker: all but a reserve for the API server."""
    reserve = min(max(total // 20, 512 * 1024**2), 4 * 1024**3)
    return max(total - reserve, total // 2)


class JobManager:
    """Runs jobs on a local Dask cluster and keeps their state and results.

    Also holds the metadata of the tables and objects that jobs stored in the
    worker (:attr:`tables`, :attr:`objects`); their data stays in the worker.
    Thread-safe: routes, Dask callbacks and events all use it.
    """

    def __init__(
        self,
        *,
        n_threads: int | None = None,
        memory_limit: int | None = None,
        debug_operations: bool = False,
        max_finished_jobs: int = 1000,
    ):
        from orangecontrib.cloud.server.app import machine_cpu_count, machine_memory

        self.n_threads = n_threads or machine_cpu_count()
        self.memory_limit = memory_limit or _default_worker_memory(machine_memory()[0])
        self.debug_operations = debug_operations
        self.max_finished_jobs = max_finished_jobs
        self.cluster = None
        self.client = None
        self._lock = threading.RLock()
        self._jobs: dict[str, _Job] = {}
        #: Metadata of stored tables and objects, by ID.
        self.tables: dict[str, TableInfo] = {}
        self.objects: dict[str, ObjectInfo] = {}
        #: Address of the worker that holds each table or object.
        self._holder: dict[str, str] = {}
        #: How recent worker processes ended, by address (from the nanny).
        self._deaths: dict[str, Any] = {}
        self._deaths_changed = threading.Condition()
        self._crash_dir: str | None = None

    # Cluster

    def dask_config(self) -> dict[str, Any]:
        """Dask settings for the cluster: no spilling, one worker that uses all cores."""
        threads = str(self.n_threads)
        return {
            # Tables live in the worker for as long as the user keeps them,
            # so don't spill, pause or rebalance; only stop a runaway worker.
            "distributed.worker.memory.target": False,
            "distributed.worker.memory.spill": False,
            "distributed.worker.memory.pause": False,
            "distributed.worker.memory.terminate": 0.95,
            # A task whose worker died fails at once (it most likely ran the
            # machine out of memory), instead of being retried.
            "distributed.scheduler.allowed-failures": 0,
            # One worker owns all cores, so let BLAS and OpenMP use them.
            "distributed.nanny.pre-spawn-environ": {
                "MALLOC_TRIM_THRESHOLD_": 65536,
                "OMP_NUM_THREADS": threads,
                "MKL_NUM_THREADS": threads,
                "OPENBLAS_NUM_THREADS": threads,
            },
        }

    def start(self) -> None:
        """Start the cluster (blocking, a few seconds)."""
        import dask
        from distributed import Client, LocalCluster

        from orangecontrib.cloud.server.crashes import CrashLogPlugin, MonitoredNanny
        from orangecontrib.cloud.server.ops.registration import register_operations

        register_operations()

        self._crash_dir = tempfile.mkdtemp(prefix="orange3-cloud-worker-")
        crash_log = os.path.join(self._crash_dir, "worker.log")
        with dask.config.set(self.dask_config()):
            self.cluster = LocalCluster(
                n_workers=1,
                threads_per_worker=self.n_threads,
                processes=True,
                memory_limit=self.memory_limit,
                host="127.0.0.1",
                scheduler_port=0,
                dashboard_address=None,
                silence_logs=logging.WARNING,
                worker_class=MonitoredNanny,
                # For the nanny (in this process), so it can tell why the
                # worker died ...
                crash_log=crash_log,
                on_death=self._on_worker_death,
                # ... and for the worker, which writes that log.
                plugins=(CrashLogPlugin(crash_log),),
            )
            self.client = Client(self.cluster, set_as_default=False)
        self.client.subscribe_topic(EVENT_TOPIC, self._on_event)
        log.info(
            "Dask cluster: 1 worker, %d threads, memory limit %d bytes",
            self.n_threads,
            self.memory_limit,
        )

    def close(self) -> None:
        """Cancel unfinished jobs and stop the cluster."""
        with self._lock:
            for job in self._jobs.values():
                if not job.state.finished:
                    job.finish(JobState.CANCELLED, _cancelled_error("The machine shut down."))
        if self.client is not None:
            self.client.close()
            self.client = None
        if self.cluster is not None:
            self.cluster.close()
            self.cluster = None
        if self._crash_dir is not None:
            shutil.rmtree(self._crash_dir, ignore_errors=True)
            self._crash_dir = None

    def run_on_worker(self, func: Callable, *args: Any) -> Any:
        """Run ``func(*args)`` in the worker process, now, and return its result.

        For quick calls on stored data (samples, downloads, deletes). It
        doesn't wait for a free task thread. Raises ``ApiError``
        (``shutting_down``) while the worker is restarting.
        """
        if self.client is None:
            raise ApiError(ErrorCode.SHUTTING_DOWN, "The job system isn't running.")
        results = self.client.run(in_worker_thread, func, *args)
        if not results:
            raise ApiError(
                ErrorCode.SHUTTING_DOWN,
                "The machine's worker is restarting. Try again in a moment.",
                status_code=503,
            )
        return next(iter(results.values()))

    # Jobs

    def operation(self, kind: str, *, public_only: bool = False) -> Operation:
        """The registered operation ``kind``; ``bad_request`` if there is none."""
        op = OPERATIONS.get(kind)
        if op is None or (op.debug and not self.debug_operations):
            raise ApiError(ErrorCode.BAD_REQUEST, f"Unknown job kind '{kind}'.")
        if public_only and not op.public:
            raise ApiError(
                ErrorCode.BAD_REQUEST, f"Jobs of kind '{kind}' can't be started this way."
            )
        return op

    def submit(
        self,
        kind: str,
        inputs: dict[str, str | list[str]] | None = None,
        params: dict[str, Any] | None = None,
        parts: dict[str, bytes] | None = None,
        *,
        public_only: bool = False,
    ) -> JobInfo:
        """Start a job; ``parts`` are pickles, unpickled in the worker."""
        if self.client is None:
            raise ApiError(ErrorCode.SHUTTING_DOWN, "The job system isn't running.")
        op = self.operation(kind, public_only=public_only)
        inputs = dict(inputs or {})
        with self._lock:
            for ref in inputs.values():
                for ref_id in ref if isinstance(ref, list) else [ref]:
                    if ref_id not in self.tables and ref_id not in self.objects:
                        raise ApiError(
                            ErrorCode.NOT_FOUND, f"No table or object with ID '{ref_id}'."
                        )
            job = _Job(id=new_id("j"), kind=op.kind)
            self._jobs[job.id] = job
            self._prune()
        future = self.client.submit(
            run_operation,
            job.id,
            op.kind,
            op.func,
            inputs,
            dict(params or {}),
            dict(parts or {}),
            key=f"job-{job.id}",
            pure=False,
        )
        with self._lock:
            job.future = future
        future.add_done_callback(lambda f, job_id=job.id: self._on_done(job_id, f))
        return job.info()

    def _job(self, job_id: str) -> _Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"No job with ID '{job_id}'.")
        return job

    def info(self, job_id: str) -> JobInfo:
        """The job's state and progress."""
        with self._lock:
            return self._job(job_id).info()

    def result(self, job_id: str) -> JobResult:
        """The result of a finished job; raises its error if it failed."""
        with self._lock:
            job = self._job(job_id)
            if job.state == JobState.DONE:
                return job.result
            if job.error is not None:
                e = job.error
                raise ApiError(e.code, e.message, remote_traceback=e.remote_traceback)
            raise ApiError(ErrorCode.CONFLICT, f"Job '{job_id}' hasn't finished.")

    def cancel(self, job_id: str, message: str = "The job was cancelled.") -> JobInfo:
        """Cancel a queued or running job, with ``message`` as its error; a
        finished job stays as it is."""
        with self._lock:
            job = self._job(job_id)
            if job.state.finished:
                return job.info()
            future = job.future
            job.finish(JobState.CANCELLED, _cancelled_error(message))
            info = job.info()
        # Tell a running operation to stop at its next check, then drop it.
        try:
            self.client.run(mark_cancelled, job_id, on_error="ignore")
        except Exception:  # noqa: BLE001 - the worker may be restarting; the job is cancelled anyway
            log.debug("Couldn't mark job %s as cancelled on the worker", job_id, exc_info=True)
        if future is not None:
            future.cancel()
        return info

    def unfinished(self) -> list[str]:
        """IDs of the queued and running jobs."""
        with self._lock:
            return [job.id for job in self._jobs.values() if not job.state.finished]

    def counts(self) -> tuple[int, int]:
        """Numbers of running and queued jobs."""
        with self._lock:
            running = sum(job.state == JobState.RUNNING for job in self._jobs.values())
            queued = sum(job.state == JobState.QUEUED for job in self._jobs.values())
        return running, queued

    @property
    def busy(self) -> bool:
        """True while any job is queued or running."""
        return any(self.counts())

    def _prune(self) -> None:
        finished = [j for j in self._jobs.values() if j.state.finished]
        for job in finished[: max(0, len(finished) - self.max_finished_jobs)]:
            del self._jobs[job.id]

    # Stored objects

    def object_info(self, object_id: str) -> ObjectInfo:
        """Metadata of a stored object; ``not_found`` if there is none."""
        with self._lock:
            info = self.objects.get(object_id)
        if info is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"No object with ID '{object_id}'.")
        return info

    def delete_object(self, object_id: str) -> None:
        """Delete a stored object from the worker."""
        self.object_info(object_id)
        self.run_on_worker(delete_object, object_id)
        with self._lock:
            self.objects.pop(object_id, None)
            self._holder.pop(object_id, None)

    def table_info(self, table_id: str) -> TableInfo:
        """Metadata of a stored table; ``not_found`` if there is none."""
        with self._lock:
            info = self.tables.get(table_id)
        if info is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"No table with ID '{table_id}'.")
        return info

    def find_derived(self, parent_id: str, steps_key: str) -> TableInfo | None:
        """The table that ``derive`` made from ``parent_id`` with ``steps_key``."""
        with self._lock:
            for info in self.tables.values():
                if info.parent_id == parent_id and info.steps_key == steps_key:
                    return info
        return None

    def finished_job(self, kind: str, tables: dict[str, TableInfo]) -> JobInfo:
        """A job that is done at once, with existing ``tables`` as its result."""
        with self._lock:
            job = _Job(id=new_id("j"), kind=kind)
            job.result = JobResult(job_id=job.id, kind=kind, tables=tables, runtime_seconds=0.0)
            job.progress = 1.0
            job.started_at = utc_now()
            job.finish(JobState.DONE)
            self._jobs[job.id] = job
            self._prune()
            return job.info()

    def list_tables(self) -> list[TableInfo]:
        """All stored tables, oldest first."""
        with self._lock:
            return sorted(self.tables.values(), key=lambda t: t.created_at)

    def delete_table(self, table_id: str) -> None:
        """Delete a stored table from the worker."""
        with self._lock:
            if table_id not in self.tables:
                raise ApiError(ErrorCode.NOT_FOUND, f"No table with ID '{table_id}'.")
        self.run_on_worker(delete_table, table_id)
        with self._lock:
            self.tables.pop(table_id, None)
            self._holder.pop(table_id, None)

    @property
    def tables_memory(self) -> int:
        """Memory of all stored tables."""
        with self._lock:
            return sum(t.memory_bytes for t in self.tables.values())

    # Dask callbacks

    def _on_event(self, event) -> None:
        _, msg = event
        with self._lock:
            job = self._jobs.get(msg.get("job"))
            if job is None or job.state.finished:
                return
            if msg.get("event") == "started":
                job.state = JobState.RUNNING
                job.started_at = job.started_at or utc_now()
            if "progress" in msg:
                job.state = JobState.RUNNING
                job.progress = msg["progress"]
            if "status" in msg:
                job.status = msg["status"]

    def _on_worker_death(self, death) -> None:
        # Called by the nanny, on the cluster's event loop.
        with self._deaths_changed:
            self._deaths[death.address] = death
            while len(self._deaths) > 20:
                del self._deaths[next(iter(self._deaths))]
            self._deaths_changed.notify_all()
        log.info("The worker process %s ended: %s", death.address, death.describe_exit())

    def _worker_death(self, address: str | None, timeout: float = 5.0):
        """How the worker at ``address`` ended; waits for the nanny's report,
        which may come a moment after the scheduler noticed."""
        with self._deaths_changed:
            self._deaths_changed.wait_for(lambda: address in self._deaths, timeout)
            return self._deaths.get(address)

    def _on_done(self, job_id: str, future) -> None:
        from distributed.scheduler import KilledWorker

        payload = exc = error = None
        status = future.status
        try:
            if status == "finished":
                payload = future.result()
            elif status != "cancelled":
                exc = future.exception()
        except Exception as e:  # noqa: BLE001 - job runner: reported as the job's error
            exc = e
        lost_worker = isinstance(exc, KilledWorker)
        if lost_worker:
            # Outside the lock: this may wait a moment for the nanny.
            address = getattr(exc.last_worker, "address", None)
            error = self._lost_worker_error(address)
        with self._lock:
            if lost_worker:
                # Whatever the cause, the worker's tables and objects are gone.
                self._forget_worker(address)
            job = self._jobs.get(job_id)
            if job is None or job.state.finished:
                return
            if status == "cancelled":
                job.finish(JobState.CANCELLED, _cancelled_error("The job was cancelled."))
            elif payload is not None and payload.get("ok"):
                self._finish_ok(job, payload)
            elif payload is not None:
                error = ErrorResponse.model_validate(payload["error"])
                state = JobState.CANCELLED if error.code == ErrorCode.CANCELLED else JobState.FAILED
                job.finish(state, error)
            elif error is not None:
                job.finish(JobState.FAILED, error)
            else:
                job.finish(JobState.FAILED, self._dask_error(exc))

    def _finish_ok(self, job: _Job, payload: dict) -> None:
        worker = payload.get("worker")
        try:
            result = JobResult(
                job_id=job.id,
                kind=job.kind,
                tables=payload["tables"],
                objects=payload["objects"],
                values=payload["values"],
                runtime_seconds=payload["runtime_seconds"],
            )
        except ValidationError as exc:
            message = "The operation returned values that aren't JSON."
            job.finish(JobState.FAILED, _error_model(ErrorCode.JOB_FAILED, message, exc))
            return
        for info in result.tables.values():
            self.tables[info.id] = info
            if worker:
                self._holder[info.id] = worker
        for info in result.objects.values():
            self.objects[info.id] = info
            if worker:
                self._holder[info.id] = worker
        for table_id in payload.get("released_tables", []):
            self.tables.pop(table_id, None)
            self._holder.pop(table_id, None)
        job.result = result
        job.progress = 1.0
        job.finish(JobState.DONE)

    def _lost_worker_error(self, address: str | None) -> ErrorResponse:
        """The error for jobs whose worker process died: ``out_of_memory`` when
        the nanny or the kernel stopped it for memory, else ``worker_crashed``."""
        from orangecontrib.cloud.server.crashes import unknown_death_error

        death = self._worker_death(address)
        code, message, details = (
            death.error() if death is not None else unknown_death_error(address)
        )
        return ErrorResponse(code=code, message=message, remote_traceback=details)

    def _dask_error(self, exc: BaseException | None) -> ErrorResponse:
        if exc is None:
            return ErrorResponse(code=ErrorCode.INTERNAL, message="The job was lost.")
        message = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        return _error_model(ErrorCode.INTERNAL, message, exc)

    def _forget_worker(self, address: str | None) -> None:
        lost = [i for i, a in self._holder.items() if address is None or a == address]
        for item_id in lost:
            self._holder.pop(item_id, None)
            self.tables.pop(item_id, None)
            self.objects.pop(item_id, None)
        if lost:
            log.warning("The worker restarted; %d tables and objects were lost", len(lost))


def _error_model(code: ErrorCode, message: str, exc: BaseException | None) -> ErrorResponse:
    return ErrorResponse.model_validate(error_dict(code, message, exc))


def _cancelled_error(message: str) -> ErrorResponse:
    return ErrorResponse(code=ErrorCode.CANCELLED, message=message)


@asynccontextmanager
async def job_system(app: FastAPI) -> AsyncIterator[None]:
    """App lifespan: start the cluster with the server, stop it after."""
    config = app.state.config
    manager = JobManager(
        n_threads=config.worker_threads,
        memory_limit=config.worker_memory_bytes,
        debug_operations=config.debug_operations,
    )
    await asyncio.to_thread(manager.start)
    app.state.server.job_manager = manager
    try:
        yield
    finally:
        app.state.server.job_manager = None
        await asyncio.to_thread(manager.close)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

router = APIRouter()


def get_jobs(request: Request) -> JobManager:
    """The app's job manager; ``shutting_down`` while it isn't running."""
    manager = request.app.state.server.job_manager
    if manager is None:
        raise ApiError(ErrorCode.SHUTTING_DOWN, "The job system isn't running.")
    return manager


async def read_multipart(
    request: Request, model: type[BaseModel], part_names: Iterable[str] | None = None
) -> tuple[Any, dict[str, bytes]]:
    """The ``spec`` part of a multipart request as ``model``, and the pickle
    parts named ``part_names`` (default: the spec's ``parts``) as bytes."""
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("multipart/form-data"):
        raise ApiError(ErrorCode.BAD_REQUEST, "Send the request as multipart/form-data.")
    async with request.form(max_part_size=MAX_PART_BYTES) as form:
        spec = form.get(SPEC_PART)
        if spec is None:
            raise ApiError(ErrorCode.BAD_REQUEST, f"The '{SPEC_PART}' part is missing.")
        if isinstance(spec, UploadFile):
            spec = await spec.read()
        try:
            parsed = model.model_validate_json(spec)
        except ValidationError as exc:
            raise ApiError(ErrorCode.BAD_REQUEST, f"Invalid spec: {exc}") from None
        parts: dict[str, bytes] = {}
        for name in parsed.parts if part_names is None else part_names:
            part = form.get(name)
            # Only file parts carry headers; unpickle only marked ones.
            if not isinstance(part, UploadFile) or (
                part.headers.get(PAYLOAD_HEADER) != PAYLOAD_PICKLE
            ):
                raise ApiError(
                    ErrorCode.BAD_REQUEST,
                    f"Part '{name}' is missing or isn't marked as a pickle "
                    f"({PAYLOAD_HEADER}: {PAYLOAD_PICKLE}).",
                )
            parts[name] = await part.read()
    return parsed, parts


@router.post("/jobs", status_code=202, response_model=JobInfo)
async def start_job(request: Request) -> JobInfo:
    """Start a job of a public kind."""
    manager = get_jobs(request)
    spec, parts = await read_multipart(request, JobRequest)
    return await run_in_threadpool(
        manager.submit, spec.kind, spec.inputs, spec.params, parts, public_only=True
    )


@router.get("/jobs/{job_id}", response_model=JobInfo)
def job_info(job_id: str, request: Request) -> JobInfo:
    """A job's state and progress."""
    return get_jobs(request).info(job_id)


@router.get("/jobs/{job_id}/result", response_model=JobResult)
def job_result(job_id: str, request: Request) -> JobResult:
    """A finished job's result."""
    return get_jobs(request).result(job_id)


@router.delete("/jobs/{job_id}", response_model=JobInfo)
def cancel_job(job_id: str, request: Request) -> JobInfo:
    """Cancel a queued or running job."""
    return get_jobs(request).cancel(job_id)


# --------------------------------------------------------------------------
# Built-in operations
# --------------------------------------------------------------------------


@operation(JobKind.SLEEP)
def sleep_operation(ctx: JobContext) -> dict[str, Any]:
    """Test operation. Params: ``seconds`` (1), ``steps`` (10), ``fail``
    (false; raise an error at the end)."""
    seconds = float(ctx.params.get("seconds", 1.0))
    steps = max(1, int(ctx.params.get("steps", 10)))
    deadline = time.monotonic()
    for step in range(steps):
        deadline += seconds / steps
        while (left := deadline - time.monotonic()) > 0:
            ctx.check_cancelled()
            time.sleep(min(left, 0.05))
        ctx.progress((step + 1) / steps, f"Step {step + 1} of {steps}")
    if ctx.params.get("fail"):
        raise RuntimeError("The sleep job failed on request.")
    return {"seconds": seconds, "steps": steps}


@operation("echo", debug=True)
def echo_operation(ctx: JobContext) -> dict[str, Any]:
    """Debug operation: stores every pickle part as an object, returns params."""
    for name, obj in ctx.parts.items():
        ctx.add_object(name, obj, ObjectKind.OTHER, description=f"Part '{name}'")
    return dict(ctx.params)


@operation("crash_worker", debug=True)
def crash_worker_operation(ctx: JobContext) -> dict[str, Any]:
    """Debug operation: crashes the worker process with a signal (``signal``,
    default ``SIGSEGV``), or ends it with ``exit_code``."""
    if "exit_code" in ctx.params:
        os._exit(int(ctx.params["exit_code"]))
    signum = signal.Signals[str(ctx.params.get("signal", "SIGSEGV"))]
    signal.pthread_kill(threading.get_ident(), signum)
    time.sleep(10)  # the signal arrives meanwhile
    return {}


@operation("memory_hog", debug=True)
def memory_hog_operation(ctx: JobContext) -> dict[str, Any]:
    """Debug operation: allocates ``bytes`` of memory in ``chunk_bytes`` steps."""
    import numpy as np

    total = int(ctx.params.get("bytes", 1024**3))
    chunk = int(ctx.params.get("chunk_bytes", 32 * 1024**2))
    rng = np.random.default_rng(0)
    held = []
    for i in range(math.ceil(total / chunk)):
        ctx.check_cancelled()
        # Random numbers: memory compression (macOS) can't shrink them.
        held.append(rng.random(chunk // 8))
        ctx.progress(min(1.0, (i + 1) * chunk / total), "Allocating")
        time.sleep(0.01)
    return {"allocated_bytes": sum(a.nbytes for a in held), "pid": os.getpid()}
