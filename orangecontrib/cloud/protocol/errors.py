"""Errors: the body of every error response and of a failed job, its codes,
and the HTTP status of each code."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

__all__ = ["ERROR_HTTP_STATUS", "OUT_OF_MEMORY_MESSAGE", "ErrorCode", "ErrorResponse"]


class ErrorCode(str, Enum):
    """Machine-readable error codes in :class:`ErrorResponse`."""

    BAD_REQUEST = "bad_request"  #: malformed request or invalid parameters
    UNAUTHORIZED = "unauthorized"  #: missing or wrong bearer token
    NOT_FOUND = "not_found"  #: unknown endpoint, table, job, object or upload
    CONFLICT = "conflict"  #: e.g. the result of a job that hasn't finished
    PAYLOAD_TOO_LARGE = "payload_too_large"  #: e.g. a distance matrix too big for memory
    VERSION_MISMATCH = "version_mismatch"  #: API or package versions don't match
    OUT_OF_MEMORY = "out_of_memory"  #: the machine ran out of memory
    WORKER_CRASHED = "worker_crashed"  #: the job worker process crashed; tables in memory are lost
    JOB_FAILED = "job_failed"  #: the operation raised an exception
    CANCELLED = "cancelled"  #: the job was cancelled
    SHUTTING_DOWN = "shutting_down"  #: the machine is shutting down
    #: a recorded step can't be replayed on all rows (e.g. it learned from the
    #: placeholder row); the message names the column and the reason
    UNSUPPORTED_STEP = "unsupported_step"
    INTERNAL = "internal"  #: anything else that went wrong on the server


#: HTTP status the server uses for each error code.
ERROR_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.BAD_REQUEST: 400,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.PAYLOAD_TOO_LARGE: 413,
    ErrorCode.VERSION_MISMATCH: 409,
    ErrorCode.OUT_OF_MEMORY: 507,
    ErrorCode.WORKER_CRASHED: 500,
    ErrorCode.JOB_FAILED: 500,
    ErrorCode.CANCELLED: 409,
    ErrorCode.SHUTTING_DOWN: 503,
    ErrorCode.UNSUPPORTED_STEP: 422,
    ErrorCode.INTERNAL: 500,
}

#: Message for :attr:`ErrorCode.OUT_OF_MEMORY`.
OUT_OF_MEMORY_MESSAGE = "The machine ran out of memory. Choose a larger machine size."


class ErrorResponse(BaseModel):
    """Body of every error response, and the error of a failed job."""

    code: ErrorCode
    message: str
    #: Formatted Python traceback from the machine, when there is one.
    remote_traceback: str | None = None
