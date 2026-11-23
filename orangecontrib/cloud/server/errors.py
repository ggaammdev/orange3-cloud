"""Errors the server returns as protocol :class:`ErrorResponse` bodies."""

from __future__ import annotations

from orangecontrib.cloud.protocol.errors import ERROR_HTTP_STATUS, ErrorCode, ErrorResponse

__all__ = ["ApiError"]


class ApiError(Exception):
    """Raise in a route to answer with an :class:`ErrorResponse`.

    The HTTP status comes from :data:`ERROR_HTTP_STATUS` unless given.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        remote_traceback: str | None = None,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.remote_traceback = remote_traceback
        self.status_code = status_code or ERROR_HTTP_STATUS[code]
        self.headers = headers

    def to_response(self) -> ErrorResponse:
        return ErrorResponse(
            code=self.code, message=self.message, remote_traceback=self.remote_traceback
        )
