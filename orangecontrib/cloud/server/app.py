"""The API server that runs on the cloud machine (API v1, FastAPI).

:func:`create_app` builds the app from a :class:`ServerConfig`. Every route,
including those of the routers passed to it, requires the machine's bearer
token. Errors are returned as protocol :class:`ErrorResponse` bodies; an
unexpected exception becomes ``internal`` (or ``out_of_memory``) with the
formatted traceback in ``remote_traceback``.

A watchdog stops the server by itself after the idle timeout (no requests and
no jobs) or at the end of the maximum lifetime, through the same path as
``POST /v1/shutdown``: :attr:`ServerConfig.on_shutdown` is called and
:attr:`ServerState.shutdown_reason` says why. It also cancels the queued and
running jobs when the add-on hasn't been in touch (no request at all, not
even ``GET /v1/info``) for the idle timeout, e.g. after Orange crashed: nobody
would collect their results, and they would keep the machine from idling
until the end of its lifetime. The idle shutdown then follows as usual.

Other server modules add endpoints as :class:`fastapi.APIRouter` objects with
paths relative to ``/v1`` (e.g. ``/jobs``), passed in ``routers``. They reach
the config and the shared state through :func:`get_config` and
:func:`get_state`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import math
import os
import platform
import shutil
import tempfile
import time
import traceback
from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from importlib.metadata import distributions, version
from pathlib import Path
from typing import TYPE_CHECKING

import psutil
from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from packaging.utils import canonicalize_name
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from orangecontrib.cloud.protocol.errors import OUT_OF_MEMORY_MESSAGE, ErrorCode, ErrorResponse
from orangecontrib.cloud.protocol.machine import InfoResponse, InstallFailure, ShutdownResponse
from orangecontrib.cloud.protocol.wire import API_PREFIX
from orangecontrib.cloud.server.errors import ApiError

if TYPE_CHECKING:
    from orangecontrib.cloud.server.jobs import JobManager

__all__ = [
    "IDLE_TIMEOUT",
    "MAX_LIFETIME",
    "SHUTDOWN_REQUESTED",
    "Lifespan",
    "ServerConfig",
    "ServerState",
    "cancel_unattended_jobs",
    "create_app",
    "create_machine_app",
    "get_config",
    "get_state",
    "machine_cpu_count",
    "machine_memory",
    "unattended_message",
]

log = logging.getLogger(__name__)

DIST_NAME = "orange3-cloud"

#: Values of :attr:`ServerState.shutdown_reason`.
SHUTDOWN_REQUESTED = "requested"  #: ``POST /v1/shutdown``
IDLE_TIMEOUT = "idle_timeout"
MAX_LIFETIME = "max_lifetime"

_REASON_TEXT = {
    SHUTDOWN_REQUESTED: "requested through the API",
    IDLE_TIMEOUT: "idle for longer than the idle timeout",
    MAX_LIFETIME: "the maximum lifetime has ended",
}

#: The watchdog checks the limits this often (or more often for short limits).
WATCHDOG_INTERVAL = 5.0


@dataclass
class ServerConfig:
    """Settings of one server: everything that differs between machines."""

    #: The machine's bearer token.
    token: str
    #: Description of the machine, e.g. ``n2-standard-16`` or ``local``.
    machine: str | None = None
    #: Where uploads and other files go; ``None``: the system temp folder.
    data_dir: Path | None = None
    #: Idle shutdown after this many seconds without requests or jobs, and
    #: jobs are cancelled after this many seconds without any request;
    #: ``None`` or 0: never.
    idle_timeout_seconds: float | None = None
    #: Shutdown after this many seconds of uptime, even with jobs running;
    #: ``None`` or 0: never.
    max_lifetime_seconds: float | None = None
    #: Best-effort packages the bootstrap couldn't install.
    install_failures: list[InstallFailure] = field(default_factory=list)
    #: The Python version the user asked for (their EnvironmentSpec's), when
    #: the bootstrap started the server; ``None``: unknown.
    python_version_requested: str | None = None
    #: Called to stop the server (``__main__`` stops uvicorn): after
    #: ``POST /v1/shutdown`` has answered, or by the watchdog. ``None``:
    #: nothing happens, as in tests.
    on_shutdown: Callable[[], None] | None = None
    #: Threads of the Dask worker; ``None``: one per CPU.
    worker_threads: int | None = None
    #: Memory limit of the Dask worker; ``None``: the machine's memory minus
    #: a reserve for the API server.
    worker_memory_bytes: int | None = None
    #: Also offer the test-only job kinds (``echo``, ``memory_hog``).
    debug_operations: bool = False

    def __post_init__(self):
        if not self.token:
            raise ValueError("the server needs a non-empty token")
        # 0 means "off", like None.
        self.idle_timeout_seconds = self.idle_timeout_seconds or None
        self.max_lifetime_seconds = self.max_lifetime_seconds or None


@dataclass
class ServerState:
    """Mutable state shared by all routes of one app."""

    started_at: float = field(default_factory=time.monotonic)
    #: ``time.monotonic()`` of the last authenticated request (except
    #: ``GET /v1/info``) or of the last time a job was seen running.
    last_activity: float = field(default_factory=time.monotonic)
    #: ``time.monotonic()`` of the last authenticated request, ``GET /v1/info``
    #: (the add-on's heartbeat) included: when the add-on was last in touch.
    last_contact: float = field(default_factory=time.monotonic)
    shutting_down: bool = False
    #: Why the server is shutting down: ``SHUTDOWN_REQUESTED``,
    #: ``IDLE_TIMEOUT`` or ``MAX_LIFETIME``; ``None`` while it runs.
    shutdown_reason: str | None = None
    #: The job system, while it runs (see ``server/jobs.py``).
    job_manager: JobManager | None = None

    def touch(self) -> None:
        """Record activity now (for the idle shutdown)."""
        self.last_activity = time.monotonic()

    def record_contact(self) -> None:
        """Record that the add-on is in touch now (for unattended jobs)."""
        self.last_contact = time.monotonic()

    @property
    def uptime_seconds(self) -> float:
        """Seconds since the server started."""
        return time.monotonic() - self.started_at

    @property
    def idle_seconds(self) -> float:
        """Seconds since the last activity."""
        return time.monotonic() - self.last_activity

    @property
    def silent_seconds(self) -> float:
        """Seconds since the add-on was last in touch."""
        return time.monotonic() - self.last_contact

    def job_counts(self) -> tuple[int, int]:
        """Numbers of running and queued jobs."""
        if self.job_manager is None:
            return 0, 0
        return self.job_manager.counts()

    @property
    def jobs_active(self) -> bool:
        """True while any job is running or queued (no idle shutdown then)."""
        return any(self.job_counts())

    def time_left(self, config: ServerConfig) -> tuple[float | None, float | None]:
        """Seconds until the idle shutdown and until the end of the lifetime.

        The first is ``None`` while jobs are queued or running (they count as
        activity, so this also resets the idle time) or without an idle
        timeout; the second is ``None`` without a maximum lifetime.
        """
        idle_left = lifetime_left = None
        if self.jobs_active:
            self.touch()
        elif config.idle_timeout_seconds:
            idle_left = max(0.0, config.idle_timeout_seconds - self.idle_seconds)
        if config.max_lifetime_seconds:
            lifetime_left = max(0.0, config.max_lifetime_seconds - self.uptime_seconds)
        return idle_left, lifetime_left

    def due_shutdown(self, config: ServerConfig) -> str | None:
        """``IDLE_TIMEOUT`` or ``MAX_LIFETIME`` once that limit is reached."""
        idle_left, lifetime_left = self.time_left(config)
        if lifetime_left == 0:
            return MAX_LIFETIME
        if idle_left == 0:
            return IDLE_TIMEOUT
        return None


def unattended_message(idle_timeout: float) -> str:
    """The error message of jobs cancelled because the add-on was silent."""
    return f"The machine cancelled the job: Orange hadn't been in touch for {_span(idle_timeout)}."


def _span(seconds: float) -> str:
    """``seconds`` in words, e.g. "30 minutes", "1 hour" or "90 seconds"."""
    for unit, size in (("hour", 3600), ("minute", 60), ("second", 1)):
        if seconds >= size and seconds % size == 0:
            count = int(seconds // size)
            return f"{count} {unit}{'s' if count != 1 else ''}"
    return f"{seconds:g} seconds"


async def cancel_unattended_jobs(config: ServerConfig, state: ServerState) -> list[str]:
    """Cancel the queued and running jobs if the add-on hasn't been in touch
    for the idle timeout (e.g. Orange crashed; never without an idle
    timeout); returns their IDs."""
    manager, timeout = state.job_manager, config.idle_timeout_seconds
    if manager is None or not timeout:
        return []
    # The jobs first: a request is in touch before it starts a job, so a job
    # started after this isn't cancelled, and one started before it means
    # that the silence is shorter.
    job_ids = manager.unfinished()
    if not job_ids or state.silent_seconds < timeout:
        return []
    message = unattended_message(timeout)

    def cancel() -> None:
        for job_id in job_ids:
            manager.cancel(job_id, message)

    await asyncio.to_thread(cancel)
    log.warning(
        "Cancelled %d job(s) (%s): Orange hadn't been in touch for %.0f s.",
        len(job_ids),
        ", ".join(job_ids),
        state.silent_seconds,
    )
    return job_ids


def begin_shutdown(config: ServerConfig, state: ServerState, reason: str) -> bool:
    """Mark the server as shutting down; False if it already is.

    The caller then calls ``config.on_shutdown``.
    """
    if state.shutting_down:
        return False
    state.shutting_down = True
    state.shutdown_reason = reason
    log.warning("Shutting down: %s.", _REASON_TEXT.get(reason, reason))
    return True


async def _watchdog(config: ServerConfig, state: ServerState) -> None:
    """Stop the server when the idle timeout or the maximum lifetime is reached."""
    limits = [t for t in (config.idle_timeout_seconds, config.max_lifetime_seconds) if t]
    if not limits:
        return
    interval = min(WATCHDOG_INTERVAL, min(limits) / 10)
    log.info(
        "Idle shutdown after %s s, maximum lifetime %s s.",
        config.idle_timeout_seconds or "-",
        config.max_lifetime_seconds or "-",
    )
    while not state.shutting_down:
        await asyncio.sleep(interval)
        try:
            await cancel_unattended_jobs(config, state)
            reason = state.due_shutdown(config)
        except Exception:  # noqa: BLE001 - watchdog: logged; keep watching, a forgotten machine costs money
            log.exception("The watchdog couldn't check the limits")
            continue
        if reason is not None and begin_shutdown(config, state, reason):
            if config.on_shutdown is not None:
                config.on_shutdown()
            return


def get_config(request: Request) -> ServerConfig:
    """The app's :class:`ServerConfig`."""
    return request.app.state.config


def get_state(request: Request) -> ServerState:
    """The app's :class:`ServerState`."""
    return request.app.state.server


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


def require_token(request: Request) -> None:
    """Dependency of every route: check ``Authorization: Bearer <token>``."""
    scheme, _, given = request.headers.get("authorization", "").partition(" ")
    expected = get_config(request).token
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        given.strip().encode(), expected.encode()
    ):
        raise ApiError(
            ErrorCode.UNAUTHORIZED,
            "Missing or wrong token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Any request shows that the add-on is in touch (for unattended jobs),
    # but reading the status doesn't count as activity, so that a client
    # showing the time left doesn't keep the machine alive.
    state = get_state(request)
    state.record_contact()
    if request.url.path != f"{API_PREFIX}/info":
        state.touch()


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


def _error_json(
    status_code: int, error: ErrorResponse, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(error.model_dump(mode="json"), status_code=status_code, headers=headers)


_STATUS_CODES = {
    400: ErrorCode.BAD_REQUEST,
    401: ErrorCode.UNAUTHORIZED,
    404: ErrorCode.NOT_FOUND,
    409: ErrorCode.CONFLICT,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
    503: ErrorCode.SHUTTING_DOWN,
    507: ErrorCode.OUT_OF_MEMORY,
}


async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return _error_json(exc.status_code, exc.to_response(), exc.headers)


async def _http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = _STATUS_CODES.get(exc.status_code)
    if code is None:
        code = ErrorCode.INTERNAL if exc.status_code >= 500 else ErrorCode.BAD_REQUEST
    return _error_json(
        exc.status_code,
        ErrorResponse(code=code, message=str(exc.detail)),
        getattr(exc, "headers", None),
    )


async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    details = "; ".join(
        f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg', '')}"
        for e in jsonable_encoder(exc.errors())
    )
    return _error_json(
        400, ErrorResponse(code=ErrorCode.BAD_REQUEST, message=f"Invalid request: {details}")
    )


def error_response_for(exc: BaseException) -> tuple[int, ErrorResponse]:
    """Status and protocol error for an unexpected exception."""
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    if isinstance(exc, MemoryError):
        return 507, ErrorResponse(
            code=ErrorCode.OUT_OF_MEMORY, message=OUT_OF_MEMORY_MESSAGE, remote_traceback=tb
        )
    message = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return 500, ErrorResponse(code=ErrorCode.INTERNAL, message=message, remote_traceback=tb)


class _UnexpectedErrorMiddleware:
    """Turn exceptions no handler caught into an ``ErrorResponse``.

    Pure ASGI (not ``BaseHTTPMiddleware``), so streamed request and response
    bodies pass through untouched. Unlike a handler for ``Exception``, which
    Starlette re-raises after answering, the exception ends here, logged.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            log.exception("Unhandled error in %s %s", scope.get("method"), scope.get("path"))
            if started:  # too late to send an error body
                raise
            status_code, error = error_response_for(exc)
            await _error_json(status_code, error)(scope, receive, send)


# --------------------------------------------------------------------------
# Machine info
# --------------------------------------------------------------------------


def _installed_packages() -> dict[str, str]:
    packages: dict[str, str] = {}
    for dist in distributions():
        name = dist.metadata["Name"]
        if name:
            # The first one on sys.path wins, as with importlib.metadata.version.
            packages.setdefault(canonicalize_name(name), dist.version)
    return dict(sorted(packages.items()))


def machine_cpu_count() -> int:
    """CPUs this process may use, within a cgroup v2 CPU quota if there is one."""
    if hasattr(os, "sched_getaffinity"):
        count = len(os.sched_getaffinity(0))
    else:
        count = os.cpu_count() or 1
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        if quota != "max":
            count = min(count, max(1, math.ceil(int(quota) / int(period))))
    except (OSError, ValueError):  # no cgroup v2 CPU quota
        return count
    return count


def machine_memory() -> tuple[int, int]:
    """Total and available memory, within a cgroup v2 limit if there is one."""
    vm = psutil.virtual_memory()
    total, free = vm.total, vm.available
    try:
        limit = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        if limit != "max" and int(limit) < total:
            used = int(Path("/sys/fs/cgroup/memory.current").read_text())
            total = int(limit)
            free = max(0, min(free, total - used))
    except (OSError, ValueError):  # no cgroup v2 memory limit
        return vm.total, vm.available
    return total, free


def _existing_folder(path: Path) -> Path:
    """``path``, or its nearest parent that exists (for the disk usage)."""
    path = Path(path).absolute()
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def _info(config: ServerConfig, state: ServerState, packages: dict[str, str]) -> InfoResponse:
    total, free = machine_memory()
    running, queued = state.job_counts()
    manager = state.job_manager
    disk = shutil.disk_usage(_existing_folder(config.data_dir or Path(tempfile.gettempdir())))
    idle_left, lifetime_left = state.time_left(config)
    due = [t for t in (idle_left, lifetime_left) if t is not None]
    return InfoResponse(
        server_version=version(DIST_NAME),
        python_version=platform.python_version(),
        python_version_requested=config.python_version_requested,
        packages=packages,
        install_failures=config.install_failures,
        machine=config.machine,
        cpu_count=machine_cpu_count(),
        memory_total_bytes=total,
        memory_free_bytes=free,
        memory_tables_bytes=manager.tables_memory if manager is not None else 0,
        disk_total_bytes=disk.total,
        disk_free_bytes=disk.free,
        uptime_seconds=state.uptime_seconds,
        idle_seconds=state.idle_seconds,
        idle_timeout_seconds=config.idle_timeout_seconds,
        max_lifetime_seconds=config.max_lifetime_seconds,
        idle_time_left_seconds=idle_left,
        lifetime_left_seconds=lifetime_left,
        time_left_seconds=min(due) if due else None,
        jobs_running=running,
        jobs_queued=queued,
    )


# --------------------------------------------------------------------------
# Core routes
# --------------------------------------------------------------------------

core_router = APIRouter()


@core_router.get("/info", response_model=InfoResponse)
def info(request: Request) -> InfoResponse:
    """The machine's versions, resources and limits."""
    return _info(get_config(request), get_state(request), request.app.state.packages)


@core_router.post("/shutdown", response_model=ShutdownResponse)
def shutdown(request: Request, background: BackgroundTasks) -> ShutdownResponse:
    """Shut the machine down after the response."""
    config = get_config(request)
    if begin_shutdown(config, get_state(request), SHUTDOWN_REQUESTED) and config.on_shutdown:
        # Runs after the response has been sent.
        background.add_task(config.on_shutdown)
    return ShutdownResponse(shutting_down=True, message="The machine is shutting down.")


# --------------------------------------------------------------------------
# App factory
# --------------------------------------------------------------------------


#: Starts a resource with the server and stops it after; gets the app.
Lifespan = Callable[[FastAPI], AbstractAsyncContextManager]


def create_app(
    config: ServerConfig,
    routers: Iterable[APIRouter] = (),
    lifespans: Iterable[Lifespan] = (),
) -> FastAPI:
    """Build the API app.

    ``routers`` add endpoints; their paths are relative to ``/v1``, and the
    token check applies to them like to every other route. ``lifespans``
    start shared resources (e.g. the job system) when the server starts, in
    order, and stop them in reverse order when it stops. The watchdog for the
    idle timeout and the maximum lifetime runs once they have started.
    """
    lifespans = list(lifespans)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Uptime counts from when the server starts serving.
        app.state.server = state = ServerState()
        config: ServerConfig = app.state.config
        if config.data_dir is not None:
            Path(config.data_dir).mkdir(parents=True, exist_ok=True)
        async with AsyncExitStack() as stack:
            for start in lifespans:
                await stack.enter_async_context(start(app))
            # The idle time counts from when the server is ready.
            state.touch()
            state.record_contact()
            watchdog = asyncio.create_task(_watchdog(app.state.config, state))
            try:
                yield
            finally:
                watchdog.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watchdog

    app = FastAPI(
        title="orange3-cloud machine",
        version=version(DIST_NAME),
        dependencies=[Depends(require_token)],
        lifespan=lifespan,
        # No public docs: every route needs the token.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.config = config
    app.state.server = ServerState()
    app.state.packages = _installed_packages()

    app.add_exception_handler(ApiError, _api_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_middleware(_UnexpectedErrorMiddleware)

    app.include_router(core_router, prefix=API_PREFIX)
    for router in routers:
        app.include_router(router, prefix=API_PREFIX)
    return app


def create_machine_app(config: ServerConfig) -> FastAPI:
    """The app the machine runs: the core routes, the job system and tables."""
    from orangecontrib.cloud.server import jobs, objects, tables
    from orangecontrib.cloud.server.ops import derive, stats

    return create_app(
        config,
        routers=[jobs.router, objects.router, tables.router, derive.router, stats.router],
        lifespans=[jobs.job_system],
    )
