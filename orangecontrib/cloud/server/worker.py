"""The worker side of jobs: operations, their context and the store.

Tables and stored objects (models, distance matrices, ...) live in the job
worker's memory, in :class:`WorkerStore`; :mod:`.jobs` has the server side.

Writing an operation
--------------------
::

    @operation("fit")
    def fit(ctx: JobContext):
        data = ctx.table("data")              # a table from the job's inputs
        learner = ctx.parts["learner"]         # an unpickled part
        ctx.progress(0.1, "Training")
        ctx.check_cancelled()
        model = learner(data)
        ctx.add_object("model", model, ObjectKind.MODEL)
        return {"rows": len(data)}             # small JSON values

Operations run in a worker thread. They report progress with
:meth:`JobContext.progress`, call :meth:`JobContext.check_cancelled` now and
then, and raise :class:`OperationError` to fail with a specific error code.
Any other exception fails the job with ``job_failed`` and the traceback;
``MemoryError`` fails it with ``out_of_memory``.
"""

from __future__ import annotations

import asyncio
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from orangecontrib.cloud.protocol.errors import OUT_OF_MEMORY_MESSAGE, ErrorCode, ErrorResponse
from orangecontrib.cloud.protocol.jobs import JobKind, ObjectInfo, ObjectKind
from orangecontrib.cloud.protocol.pickles import dump, loads
from orangecontrib.cloud.protocol.tables import TableFormat, TableInfo

# Registers the stand-ins for Feature Constructor's and Preprocess's classes,
# which need Qt, before the worker unpickles any part.
from orangecontrib.cloud.server import expressions, preprocessors  # noqa: F401

__all__ = [
    "EVENT_TOPIC",
    "JobCancelled",
    "JobContext",
    "LoadedFrom",
    "Operation",
    "OperationError",
    "OPERATIONS",
    "WorkerStore",
    "current_worker",
    "operation",
    "pickled_size",
    "table_memory",
    "worker_store",
]

#: Dask event topic for job events (started, progress) sent by the worker.
EVENT_TOPIC = "orange3-cloud-job"


def utc_now() -> datetime:
    """The current time, in UTC."""
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    """A new ID for a job, table or object, such as ``t-0123456789ab``."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------
# Operation registry
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Operation:
    """A registered job kind and the function that runs it."""

    kind: str
    func: Callable[[JobContext], dict[str, Any] | None]
    #: May be started with ``POST /v1/jobs``; other kinds only by other
    #: endpoints (e.g. ``load_table`` by ``POST /v1/tables``).
    public: bool = True
    #: Only available when ``ServerConfig.debug_operations`` is set (tests).
    debug: bool = False


#: Registered operations by kind.
OPERATIONS: dict[str, Operation] = {}


def operation(
    kind: str | JobKind, *, public: bool = True, debug: bool = False
) -> Callable[[Callable], Callable]:
    """Register ``func(ctx: JobContext) -> dict | None`` as job kind ``kind``.

    The function's return value becomes the job result's ``values`` and must
    be JSON. The function must be importable by name (defined at module
    level), because the worker process imports it.
    """
    kind = str(getattr(kind, "value", kind))

    def decorator(func):
        old = OPERATIONS.get(kind)
        if old is not None and (old.func.__module__, old.func.__qualname__) != (
            func.__module__,
            func.__qualname__,
        ):
            raise ValueError(f"operation {kind!r} is already registered")
        OPERATIONS[kind] = Operation(kind, func, public=public, debug=debug)
        return func

    return decorator


# --------------------------------------------------------------------------
# Worker side: store, context, runner
# --------------------------------------------------------------------------


class JobCancelled(Exception):
    """Raised by :meth:`JobContext.check_cancelled` when the job is cancelled."""


class OperationError(Exception):
    """Raise in an operation to fail the job with a specific error code."""

    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class WorkerStore:
    """Tables and objects held in the worker process's memory.

    There is one per process (:func:`worker_store`). Operations reach it as
    ``ctx.store``; the server reaches it with :meth:`JobManager.run_on_worker`.
    """

    def __init__(self):
        self.tables: dict[str, Any] = {}
        self.objects: dict[str, Any] = {}
        #: IDs of jobs cancelled while they run.
        self.cancelled: set[str] = set()


_store: WorkerStore | None = None
_store_lock = threading.Lock()


def worker_store() -> WorkerStore:
    """The :class:`WorkerStore` of this process."""
    global _store
    with _store_lock:
        if _store is None:
            _store = WorkerStore()
        return _store


class _CountingWriter:
    def __init__(self):
        self.size = 0

    def write(self, data) -> int:
        n = memoryview(data).nbytes
        self.size += n
        return n


def pickled_size(obj: Any) -> int:
    """Size of ``obj``'s pickle, without holding the pickle in memory."""
    writer = _CountingWriter()
    dump(obj, writer)
    return writer.size


def _qualified_name(obj: Any) -> str:
    cls = type(obj)
    return f"{cls.__module__}.{cls.__qualname__}"


def _objects_memory(array: Any) -> int:
    """Estimated memory of the Python objects (e.g. strings) in an object
    array, from up to 1000 evenly spaced elements."""
    import sys

    flat = array.reshape(-1) if array.flags.c_contiguous else array.ravel()
    if not flat.size:
        return 0
    step = max(1, flat.size // 1000)
    sample = flat[::step]
    return int(sum(sys.getsizeof(v) for v in sample) / len(sample) * flat.size)


def table_memory(table: Any) -> int:
    """Memory of a table's arrays, including the text in object arrays."""
    total = 0
    for name in ("X", "Y", "metas", "W"):
        array = getattr(table, name, None)
        if array is None:
            continue
        if hasattr(array, "data") and hasattr(array, "indices"):  # scipy.sparse
            total += array.data.nbytes + array.indices.nbytes + array.indptr.nbytes
        else:
            total += getattr(array, "nbytes", 0)
            if getattr(getattr(array, "dtype", None), "kind", "") == "O":
                total += _objects_memory(array)
    return total


def current_worker() -> Any:
    """The Dask worker running this code, or ``None`` outside a worker."""
    try:
        from distributed import get_worker

        return get_worker()
    except (ImportError, ValueError):
        return None


def _emit(msg: dict[str, Any]) -> None:
    """Send a job event to the server; does nothing outside a Dask worker."""
    worker = current_worker()
    if worker is not None:
        worker.log_event(EVENT_TOPIC, msg)


@dataclass(frozen=True)
class LoadedFrom:
    """Where a table loaded from a file came from (for its :class:`TableInfo`)."""

    source: str  #: the file, as the user gave it
    format: TableFormat
    peak_memory_bytes: int | None = None  #: memory the load needed on top of the table


class JobContext:
    """What an operation gets: its inputs, options and ways to report back."""

    #: Smallest time between two progress events with the same status text.
    PROGRESS_INTERVAL = 0.1

    def __init__(
        self,
        job_id: str,
        kind: str,
        inputs: dict[str, str | list[str]],
        params: dict[str, Any],
        parts: dict[str, Any],
        store: WorkerStore,
    ):
        self.job_id = job_id
        self.kind = kind
        #: References from the request: names -> table/object IDs.
        self.inputs = inputs
        #: The operation's options (JSON).
        self.params = params
        #: Unpickled parts of the request, by name.
        self.parts = parts
        #: Tables and objects in this worker.
        self.store = store
        #: Outputs added with :meth:`add_table` and :meth:`add_object`.
        self.tables: dict[str, TableInfo] = {}
        self.objects: dict[str, ObjectInfo] = {}
        #: IDs of earlier jobs' tables dropped with :meth:`release_table`.
        self.released_tables: list[str] = []
        self._last_progress = (0.0, None)
        self._last_sent = 0.0

    # Progress and cancel

    def progress(self, fraction: float, status: str | None = None) -> None:
        """Report progress (0 to 1) and, optionally, a short status text."""
        fraction = min(1.0, max(0.0, float(fraction)))
        now = time.monotonic()
        if status == self._last_progress[1] and now - self._last_sent < self.PROGRESS_INTERVAL:
            self._last_progress = (fraction, status)
            return
        self._last_progress = (fraction, status)
        self._last_sent = now
        msg: dict[str, Any] = {"job": self.job_id, "progress": fraction}
        if status is not None:
            msg["status"] = status
        _emit(msg)

    @property
    def cancelled(self) -> bool:
        """True once the job was cancelled."""
        return self.job_id in self.store.cancelled

    def check_cancelled(self) -> None:
        """Raise :class:`JobCancelled` if the job was cancelled."""
        if self.cancelled:
            raise JobCancelled()

    # Inputs

    def _resolve(self, name: str, store: dict[str, Any], what: str) -> Any:
        if name not in self.inputs:
            raise OperationError(ErrorCode.BAD_REQUEST, f"The job needs the input '{name}'.")
        ref = self.inputs[name]
        ids = ref if isinstance(ref, list) else [ref]
        missing = [i for i in ids if i not in store]
        if missing:
            raise OperationError(ErrorCode.NOT_FOUND, f"No {what} with ID '{missing[0]}'.")
        values = [store[i] for i in ids]
        return values if isinstance(ref, list) else values[0]

    def table(self, name: str) -> Any:
        """The table (or list of tables) named ``name`` in the inputs."""
        return self._resolve(name, self.store.tables, "table")

    def object(self, name: str) -> Any:
        """The stored object (or list of them) named ``name`` in the inputs."""
        return self._resolve(name, self.store.objects, "object")

    # Outputs

    def add_table(
        self,
        name: str,
        table: Any,
        *,
        table_name: str | None = None,
        parent_id: str | None = None,
        loaded_from: LoadedFrom | None = None,
    ) -> TableInfo:
        """Store an Orange table as output ``name`` of the job."""
        table_id = new_id("t")
        domain = table.domain
        source = loaded_from.source if loaded_from else None
        info = TableInfo(
            id=table_id,
            name=table_name or getattr(table, "name", None) or name,
            n_rows=len(table),
            n_attributes=len(domain.attributes),
            n_class_vars=len(domain.class_vars),
            n_metas=len(domain.metas),
            memory_bytes=table_memory(table),
            peak_load_memory_bytes=loaded_from.peak_memory_bytes if loaded_from else None,
            source=source or (f"table:{parent_id}" if parent_id else f"job:{self.job_id}"),
            format=loaded_from.format if loaded_from else None,
            parent_id=parent_id,
            created_at=utc_now(),
        )
        self.store.tables[table_id] = table
        self.tables[name] = info
        return info

    def release_table(self, table_id: str) -> None:
        """Drop an earlier job's table that nothing needs any more; the
        server forgets it when this job is done."""
        if self.store.tables.pop(table_id, _MISSING) is not _MISSING:
            self.released_tables.append(table_id)

    def add_object(
        self,
        name: str,
        obj: Any,
        kind: ObjectKind = ObjectKind.OTHER,
        description: str = "",
    ) -> ObjectInfo:
        """Store a Python object (model, distances, ...) as output ``name``."""
        shape = getattr(obj, "shape", None)
        if not (isinstance(shape, tuple) and all(isinstance(s, int) for s in shape)):
            shape = None
        info = ObjectInfo(
            id=new_id("o"),
            kind=kind,
            type=_qualified_name(obj),
            size_bytes=pickled_size(obj),
            shape=list(shape) if shape is not None else None,
            description=description,
            created_at=utc_now(),
        )
        self.store.objects[info.id] = obj
        self.objects[name] = info
        return info

    def _discard_outputs(self) -> None:
        for info in self.tables.values():
            self.store.tables.pop(info.id, None)
        for info in self.objects.values():
            self.store.objects.pop(info.id, None)


def error_dict(code: ErrorCode, message: str, exc: BaseException | None = None) -> dict:
    """An :class:`ErrorResponse` as JSON, with the traceback of ``exc``."""
    tb = None
    if exc is not None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return ErrorResponse(code=code, message=message, remote_traceback=tb).model_dump(mode="json")


def _input_columns(inputs: dict[str, str | list[str]], store: WorkerStore) -> dict[str, list]:
    """The columns (``domain.variables + domain.metas``) of the job's input
    tables by input name: pickled parts may refer to them by position."""
    columns = {}
    for name, table_id in inputs.items():
        table = store.tables.get(table_id) if isinstance(table_id, str) else None
        if table is not None:
            columns[name] = [*table.domain.variables, *table.domain.metas]
    return columns


def run_operation(
    job_id: str,
    kind: str,
    func: Callable[[JobContext], dict[str, Any] | None],
    inputs: dict[str, str | list[str]],
    params: dict[str, Any],
    parts: dict[str, bytes],
) -> dict[str, Any]:
    """Run one job in a worker thread. Never raises: failures are returned."""
    store = worker_store()
    _emit({"job": job_id, "event": "started"})
    started = time.monotonic()
    ctx: JobContext | None = None
    try:
        if job_id in store.cancelled:
            raise JobCancelled()
        refs = _input_columns(inputs, store)
        unpickled = {name: loads(data, refs) for name, data in parts.items()}
        del parts
        ctx = JobContext(job_id, kind, inputs, params, unpickled, store)
        values = func(ctx) or {}
        return {
            "ok": True,
            "tables": {n: i.model_dump(mode="json") for n, i in ctx.tables.items()},
            "objects": {n: i.model_dump(mode="json") for n, i in ctx.objects.items()},
            "released_tables": ctx.released_tables,
            "values": values,
            "runtime_seconds": time.monotonic() - started,
            "worker": getattr(current_worker(), "address", None),
        }
    except JobCancelled:
        error = error_dict(ErrorCode.CANCELLED, "The job was cancelled.")
    except MemoryError as exc:
        error = error_dict(ErrorCode.OUT_OF_MEMORY, OUT_OF_MEMORY_MESSAGE, exc)
    except OperationError as exc:
        error = error_dict(exc.code, exc.message, exc)
    except Exception as exc:  # noqa: BLE001 - job runner: reported as JOB_FAILED
        message = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        error = error_dict(ErrorCode.JOB_FAILED, message, exc)
    finally:
        store.cancelled.discard(job_id)
    if ctx is not None:
        ctx._discard_outputs()
    return {"ok": False, "error": error}


# Small functions the server runs on the worker with ``client.run``.


def mark_cancelled(job_id: str) -> None:
    """Mark a job as cancelled, so its operation stops at its next check."""
    worker_store().cancelled.add(job_id)


def delete_object(object_id: str) -> bool:
    """Drop a stored object; False if there was none."""
    return worker_store().objects.pop(object_id, _MISSING) is not _MISSING


def delete_table(table_id: str) -> bool:
    """Drop a stored table; False if there was none."""
    return worker_store().tables.pop(table_id, _MISSING) is not _MISSING


_MISSING = object()


async def in_worker_thread(func: Callable, *args: Any) -> Any:
    """Run ``func(*args)`` in a thread of the worker (with ``client.run``)."""
    # Runs on the worker's event loop (client.run), so it starts at once
    # even when all task threads are busy, and moves the work to a thread
    # so it doesn't block the worker's communication.
    return await asyncio.to_thread(func, *args)
