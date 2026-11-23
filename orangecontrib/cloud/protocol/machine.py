"""The machine: what it is, runs and has left (``GET /v1/info``), shutting it
down, and the answer to deleting a table or object it stores."""

from __future__ import annotations

from pydantic import BaseModel, Field

from orangecontrib.cloud.protocol.wire import API_VERSION

__all__ = ["DeleteResponse", "InfoResponse", "InstallFailure", "ShutdownResponse"]


class InstallFailure(BaseModel):
    """A best-effort package the machine couldn't install at boot."""

    name: str
    version: str
    error: str


class InfoResponse(BaseModel):
    """``GET /v1/info``: what the machine is, runs and has left."""

    api_version: int = API_VERSION
    server_version: str  #: ``orange3-cloud`` version on the machine
    #: Python version installed on the machine.
    python_version: str
    #: Python version the user's environment asked for. When uv has no build
    #: of it, the machine installs the newest release of the same minor version,
    #: so ``python_version`` can differ in the last part (3.11.2 -> 3.11.15).
    #: ``None``: unknown (the server wasn't started by the machine's bootstrap).
    python_version_requested: str | None = None
    #: Installed distributions on the machine: PEP 503 name -> version.
    packages: dict[str, str] = Field(default_factory=dict)
    #: Best-effort packages that failed to install at boot.
    install_failures: list[InstallFailure] = Field(default_factory=list)
    #: Description of the machine, e.g. ``n2-standard-16`` or ``local``.
    machine: str | None = None
    cpu_count: int
    memory_total_bytes: int
    #: Memory available for new work (like ``psutil``'s "available").
    memory_free_bytes: int
    #: Memory held by stored tables.
    memory_tables_bytes: int = 0
    disk_total_bytes: int | None = None
    disk_free_bytes: int | None = None
    uptime_seconds: float
    #: Seconds since the last API request or running job (``GET /v1/info``
    #: itself doesn't count, so polling it doesn't keep the machine alive).
    idle_seconds: float = 0.0
    #: Idle shutdown after this many seconds without requests or running jobs.
    #: Queued and running jobs are cancelled after this many seconds without
    #: any request (``GET /v1/info`` included), e.g. after Orange crashed.
    #: ``None``: neither.
    idle_timeout_seconds: float | None = None
    #: Shutdown after this many seconds of uptime, whatever is running.
    #: ``None``: no maximum lifetime.
    max_lifetime_seconds: float | None = None
    #: Seconds until the idle shutdown, if nothing happens meanwhile. ``None``
    #: while jobs are queued or running, or without an idle timeout.
    idle_time_left_seconds: float | None = None
    #: Seconds until the maximum lifetime ends. ``None``: no maximum lifetime.
    lifetime_left_seconds: float | None = None
    #: Seconds until the machine shuts down by itself, if nothing changes: the
    #: smaller of the two above. ``None``: no shutdown is due.
    time_left_seconds: float | None = None
    jobs_running: int = 0
    jobs_queued: int = 0


class ShutdownResponse(BaseModel):
    """``POST /v1/shutdown``."""

    shutting_down: bool = True
    message: str = ""


class DeleteResponse(BaseModel):
    """Answer to ``DELETE`` on a table or object."""

    id: str
    deleted: bool = True
