"""HTTP client for the cloud machine's API v1.

:class:`CloudClient` is the only place in the add-on that talks to the
machine. It runs on the user's computer, so it imports nothing from the
server side: just ``httpx``, the shared :mod:`orangecontrib.cloud.protocol`
models and, for unpickling, Orange.

* **TLS** trusts only the machine's own certificate: the client's SSL context
  has no default CAs, just the PEM it was given, and hostnames are checked.
  When the certificate is issued for a name other than the URL's host (e.g. a
  fixed name, before the machine's IP is known), pass it as
  ``server_hostname``. Plain ``http://`` needs ``allow_http=True``.
* **Errors** become :class:`CloudError` (or a subclass) with the protocol's
  error code, the message and the machine's traceback. Network failures are
  :class:`CloudConnectionError`.
* **Retries:** ``GET`` and ``DELETE`` are retried with backoff after
  connection errors and ``502``/``503``/``504`` answers. ``POST`` calls
  (uploads, jobs, shutdown) are never retried. Object downloads continue
  where they broke off, with ``Range`` requests. A thread's calls aren't
  retried once its task has given up (:meth:`CloudClient.giving_up_when`).
* **Jobs:** :meth:`CloudClient.wait_for_job` polls a job, reports progress,
  cancels it on request and returns its :class:`JobResult`.

The client is thread-safe (``httpx.Client`` is), so a session can run calls
on a background thread.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import ssl
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import IO, Any, BinaryIO

import httpx
from pydantic import BaseModel, ValidationError

from orangecontrib.cloud.protocol.errors import ErrorCode, ErrorResponse
from orangecontrib.cloud.protocol.jobs import JobInfo, JobRequest, JobResult, JobState
from orangecontrib.cloud.protocol.machine import DeleteResponse, InfoResponse, ShutdownResponse
from orangecontrib.cloud.protocol.pickles import digest, dumps, loads
from orangecontrib.cloud.protocol.tables import (
    ColumnSpec,
    DeriveRequest,
    StatsRequest,
    TableCreateRequest,
    TableFormat,
    TableInfo,
    TableList,
    UploadInfo,
)
from orangecontrib.cloud.protocol.wire import (
    API_PREFIX,
    JSON_CONTENT_TYPE,
    PAYLOAD_HEADER,
    PAYLOAD_PICKLE,
    PICKLE_CONTENT_TYPE,
    SPEC_PART,
)

__all__ = [
    "CANCEL_TIMEOUT",
    "DEFAULT_TIMEOUT",
    "CloudAuthError",
    "CloudCancelled",
    "CloudClient",
    "CloudConnectionError",
    "CloudError",
    "CloudNotFoundError",
    "CloudOutOfMemoryError",
    "CloudUnsupportedStepError",
    "CloudWorkerCrashedError",
    "DownloadProgress",
    "ProgressCallback",
    "pinned_ssl_context",
]

#: Default timeouts: 10 s to connect, 60 s between bytes otherwise.
DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

#: Timeout of ``POST /v1/tables/{id}/stats``, which answers when it's done.
STATS_TIMEOUT = httpx.Timeout(600.0, connect=10.0)

#: Timeout of the cancel that :meth:`CloudClient.wait_for_job` sends, e.g.
#: when a widget closes or Orange quits: it doesn't wait long for the machine.
CANCEL_TIMEOUT = httpx.Timeout(5.0, connect=3.0)

#: ``on_progress(fraction, text)`` for jobs, with fraction from 0 to 1.
ProgressCallback = Callable[[float, str], None]

#: ``on_progress(received, total)`` for downloads, in bytes (total: ``None``
#: when unknown).
DownloadProgress = Callable[[int, "int | None"], None]

#: Methods that are safe to repeat after a network failure.
_RETRY_METHODS = frozenset({"GET", "DELETE"})
#: Statuses after which safe calls are retried (proxies, restarts).
_RETRY_STATUSES = frozenset({502, 503, 504})
#: Network errors after which safe calls are retried. A read timeout isn't
#: one: the machine is busy, and repeating the call would only add to it.
_RETRY_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.PoolTimeout,
)

_UPLOAD_CHUNK = 1024**2


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class CloudError(Exception):
    """An error from the machine, or from talking to it.

    ``code`` is the protocol's :class:`ErrorCode`, or ``None`` when the
    machine didn't send one (network failures, unexpected answers).
    ``remote_traceback`` is the formatted Python traceback from the machine,
    when there is one. ``status_code`` is the HTTP status, if any.
    """

    def __init__(
        self,
        code: ErrorCode | None,
        message: str,
        remote_traceback: str | None = None,
        status_code: int | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.remote_traceback = remote_traceback
        self.status_code = status_code

    def __str__(self) -> str:
        return self.message

    @classmethod
    def from_response(cls, error: ErrorResponse, status_code: int | None = None) -> CloudError:
        """The right subclass for a protocol error."""
        subclass = _ERROR_CLASSES.get(error.code, CloudError)
        return subclass(error.code, error.message, error.remote_traceback, status_code)


class CloudConnectionError(CloudError):
    """The machine couldn't be reached, or the connection broke."""

    def __init__(self, message: str):
        super().__init__(None, message)


class CloudAuthError(CloudError):
    """The machine refused the token (``unauthorized``)."""


class CloudNotFoundError(CloudError):
    """Unknown table, job, object or upload (``not_found``), e.g. after the
    machine restarted."""


class CloudOutOfMemoryError(CloudError):
    """The machine ran out of memory (``out_of_memory``)."""


class CloudWorkerCrashedError(CloudError):
    """The job's worker process on the machine crashed (``worker_crashed``);
    tables and objects in its memory are lost."""


class CloudCancelled(CloudError):
    """The job was cancelled (``cancelled``), by this client or another."""


class CloudUnsupportedStepError(CloudError):
    """A recorded step can't be replayed on all rows (``unsupported_step``);
    the message names the column and what to do."""


_ERROR_CLASSES: dict[ErrorCode, type[CloudError]] = {
    ErrorCode.UNAUTHORIZED: CloudAuthError,
    ErrorCode.NOT_FOUND: CloudNotFoundError,
    ErrorCode.OUT_OF_MEMORY: CloudOutOfMemoryError,
    ErrorCode.WORKER_CRASHED: CloudWorkerCrashedError,
    ErrorCode.CANCELLED: CloudCancelled,
    ErrorCode.UNSUPPORTED_STEP: CloudUnsupportedStepError,
}


# --------------------------------------------------------------------------
# TLS
# --------------------------------------------------------------------------


def _read_pem(cert: str | bytes | os.PathLike) -> str:
    if isinstance(cert, bytes):
        return cert.decode("ascii")
    if isinstance(cert, str) and "-----BEGIN" in cert:
        return cert
    return Path(cert).read_text(encoding="ascii")


def pinned_ssl_context(cert_pem: str | bytes | os.PathLike) -> ssl.SSLContext:
    """An SSL context that trusts only ``cert_pem`` (PEM text or a file path).

    No system or default CAs are loaded, the peer's certificate is required
    and its hostname is checked.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # CERT_REQUIRED, check_hostname
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_verify_locations(cadata=_read_pem(cert_pem))
    return context


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


class CloudClient:
    """Typed client for one machine.

    :param base_url: e.g. ``https://203.0.113.5:8443`` (without ``/v1``).
    :param token: the machine's bearer token.
    :param cert_pem: the machine's self-signed certificate, as PEM text,
        bytes or a file path. Required: the URL must be ``https://``.
    :param server_hostname: name to check the certificate against (and to
        send as SNI) instead of the URL's host.
    :param timeout: seconds, or an ``httpx.Timeout``.
    :param retries: how often safe calls are repeated after a failure.
    """

    #: Seconds before the first repeat of a failed call; doubled each time.
    retry_backoff = 0.5

    def __init__(
        self,
        base_url: str,
        token: str,
        cert_pem: str | bytes | os.PathLike | None = None,
        *,
        server_hostname: str | None = None,
        timeout: float | httpx.Timeout = DEFAULT_TIMEOUT,
        retries: int = 3,
    ):
        if not token:
            raise ValueError("the client needs the machine's token")
        url = httpx.URL(base_url)
        if url.scheme != "https":
            raise ValueError(f"unsupported URL scheme (only https://): {base_url!r}")
        if cert_pem is None:
            raise ValueError("https:// needs the machine's certificate (cert_pem)")

        self.base_url = str(url).rstrip("/")
        self.server_hostname = server_hostname
        self.retries = max(0, retries)
        self._local = threading.local()  # each thread's giving_up_when()
        self._extensions = {"sni_hostname": server_hostname} if server_hostname else None
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            verify=pinned_ssl_context(cert_pem),
            follow_redirects=False,
        )

    def __repr__(self) -> str:  # never shows the token
        return f"{type(self).__name__}({self.base_url!r})"

    # Lifetime

    def close(self) -> None:
        """Close the HTTP connections."""
        self._http.close()

    def __enter__(self) -> CloudClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextlib.contextmanager
    def giving_up_when(self, stop: Callable[[], bool]) -> Iterator[None]:
        """Within the block, this thread's failed calls aren't repeated once
        ``stop()`` is true (e.g. its task was cancelled because Orange quits)."""
        previous = getattr(self._local, "stop", None)
        self._local.stop = stop
        try:
            yield
        finally:
            self._local.stop = previous

    def _gave_up(self) -> bool:
        stop = getattr(self._local, "stop", None)
        return stop is not None and stop()

    # Low level

    def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        """One request, with retries for safe methods; raises on errors."""
        url = API_PREFIX + path
        attempts = 1 + (self.retries if method in _RETRY_METHODS else 0)
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                response = self._http.request(method, url, extensions=self._extensions, **kwargs)
            except _RETRY_ERRORS as exc:
                if last or _is_certificate_error(exc) or self._gave_up():
                    raise self._connection_error(exc) from exc
            except httpx.HTTPError as exc:
                raise self._connection_error(exc) from exc
            else:
                if response.is_success:
                    return response
                if last or response.status_code not in _RETRY_STATUSES or self._gave_up():
                    raise self._error(response)
            time.sleep(self.retry_backoff * 2**attempt)
        raise AssertionError("unreachable")

    def _connection_error(self, exc: httpx.HTTPError) -> CloudConnectionError:
        if isinstance(exc, httpx.TimeoutException):
            what = "didn't answer in time"
        elif _is_certificate_error(exc):
            what = "doesn't present the certificate this connection trusts"
        elif isinstance(exc, httpx.ConnectError):
            what = "can't be reached"
        else:
            what = "closed the connection"
        return CloudConnectionError(f"The cloud machine at {self.base_url} {what} ({exc}).")

    @staticmethod
    def _error(response: httpx.Response) -> CloudError:
        status = response.status_code
        try:
            error = ErrorResponse.model_validate_json(response.content)
        except (ValidationError, ValueError):
            # Not one of the machine's answers, e.g. from a proxy.
            text = response.text.strip()[:200] or response.reason_phrase
            code = ErrorCode.UNAUTHORIZED if status == 401 else None
            subclass = _ERROR_CLASSES.get(code, CloudError) if code else CloudError
            return subclass(code, f"HTTP {status}: {text}", None, status)
        return CloudError.from_response(error, status)

    def _json(self, method: str, path: str, model: type[BaseModel], **kwargs) -> Any:
        response = self._send(method, path, **kwargs)
        try:
            return model.model_validate_json(response.content)
        except ValidationError as exc:
            raise CloudError(
                None,
                f"Unexpected answer from {method} {path}: {exc}",
                None,
                response.status_code,
            ) from exc

    def _pickle(self, method: str, path: str, **kwargs) -> Any:
        response = self._send(method, path, **kwargs)
        content_type = response.headers.get("content-type", "").split(";")[0].strip()
        if (
            response.headers.get(PAYLOAD_HEADER) != PAYLOAD_PICKLE
            or content_type != PICKLE_CONTENT_TYPE
        ):
            raise CloudError(
                None,
                f"{method} {path} didn't answer with a pickle.",
                None,
                response.status_code,
            )
        return loads(response.content)

    @staticmethod
    def _multipart(spec: BaseModel, objects: Mapping[str, Any]) -> dict[str, tuple]:
        files: dict[str, tuple] = {
            SPEC_PART: (None, spec.model_dump_json(), JSON_CONTENT_TYPE),
        }
        for name, obj in objects.items():
            if name == SPEC_PART:
                raise ValueError(f"'{SPEC_PART}' can't be the name of an object")
            files[name] = _pickle_part(name, dumps(obj))
        return files

    # Machine

    def info(self) -> InfoResponse:
        """``GET /v1/info``. Doesn't count as activity on the machine."""
        return self._json("GET", "/info", InfoResponse)

    def shutdown(self) -> ShutdownResponse:
        """``POST /v1/shutdown``: the machine stops after answering."""
        return self._json("POST", "/shutdown", ShutdownResponse)

    # Uploads

    def upload(
        self,
        source: str | os.PathLike | BinaryIO,
        filename: str | None = None,
        on_progress: Callable[[int, int | None], None] | None = None,
        chunk_size: int = _UPLOAD_CHUNK,
    ) -> UploadInfo:
        """``POST /v1/uploads``: stream a local file to the machine.

        ``source`` is a path or a binary file object (read from its current
        position). ``filename`` defaults to the path's name; its extension
        tells the format. ``on_progress(bytes_sent, total)`` is called after
        each chunk; ``total`` is ``None`` when the size is unknown.
        """
        if isinstance(source, (str, os.PathLike)):
            path = Path(source)
            with path.open("rb") as f:
                return self.upload(f, filename or path.name, on_progress, chunk_size)
        if filename is None:
            name = getattr(source, "name", None)
            if not isinstance(name, str):
                raise ValueError("pass a filename for a file object without a name")
            filename = Path(name).name
        total = _remaining_size(source)
        headers = {"Content-Type": PICKLE_CONTENT_TYPE}
        if total is not None:
            headers["Content-Length"] = str(total)

        def chunks() -> Iterator[bytes]:
            sent = 0
            if on_progress is not None:
                on_progress(0, total)
            while chunk := source.read(chunk_size):
                yield chunk
                sent += len(chunk)
                if on_progress is not None:
                    on_progress(sent, total)

        return self._json(
            "POST",
            "/uploads",
            UploadInfo,
            params={"filename": filename},
            content=chunks(),
            headers=headers,
        )

    # Tables

    def create_table(
        self,
        request: TableCreateRequest | None = None,
        *,
        uri: str | None = None,
        upload_id: str | None = None,
        format: TableFormat | None = None,
        name: str | None = None,
        columns: list[ColumnSpec] | None = None,
    ) -> JobInfo:
        """``POST /v1/tables``: start a ``load_table`` job.

        Pass a :class:`TableCreateRequest`, or its fields as keywords.
        """
        if request is None:
            request = TableCreateRequest(
                uri=uri, upload_id=upload_id, format=format, name=name, columns=columns or []
            )
        return self._json(
            "POST",
            "/tables",
            JobInfo,
            content=request.model_dump_json(),
            headers={"Content-Type": JSON_CONTENT_TYPE},
        )

    def list_tables(self) -> list[TableInfo]:
        """``GET /v1/tables``."""
        return self._json("GET", "/tables", TableList).tables

    def table_info(self, table_id: str) -> TableInfo:
        """``GET /v1/tables/{id}``."""
        return self._json("GET", f"/tables/{table_id}", TableInfo)

    def table_domain(self, table_id: str) -> Any:
        """``GET /v1/tables/{id}/domain``: the table's Orange ``Domain``."""
        return self._pickle("GET", f"/tables/{table_id}/domain")

    def table_rows(
        self,
        table_id: str,
        start: int = 0,
        stop: int | None = None,
        sort: str | None = None,
        descending: bool = False,
    ) -> Any:
        """``GET /v1/tables/{id}/rows``: rows ``start`` to ``stop`` (exclusive;
        ``None``: 1000 rows) as an Orange ``Table``, in the table's order or
        sorted on the machine by the column named ``sort`` (missing values
        last)."""
        params: dict[str, Any] = {"start": start}
        if stop is not None:
            params["stop"] = stop
        if sort is not None:
            params["sort"] = sort
            params["descending"] = descending
        return self._pickle("GET", f"/tables/{table_id}/rows", params=params)

    def delete_table(self, table_id: str) -> DeleteResponse:
        """``DELETE /v1/tables/{id}``."""
        return self._json("DELETE", f"/tables/{table_id}", DeleteResponse)

    def derive(
        self,
        table_id: str,
        steps: Sequence[tuple[str, Any]],
        name: str | None = None,
        columns: Sequence[Any] | None = None,
    ) -> JobInfo:
        """``POST /v1/tables/{id}/derive``: replay recorded steps on all rows (a job).

        ``steps`` are ``("transform", Domain)``, ``("filter", Filter)`` and
        ``("preprocess", recipe)`` pairs, in order. ``columns``: the table's
        ``domain.variables + domain.metas`` as this side got them from the
        machine; the steps refer to these by position instead of sending
        copies. The machine answers with a finished job when it has already
        replayed the same steps on the table, and starts from the table it
        derived with the first steps when it has one (``prefix_keys``).
        """
        refs = {"data": columns} if columns else None
        data = dumps(tuple(steps), refs)
        # The keys leave out what widgets cache on the columns as they draw them.
        prefix_keys = [digest(tuple(steps[:k]), refs) for k in range(1, len(steps))]
        spec = DeriveRequest(
            name=name, steps_key=digest(tuple(steps), refs), prefix_keys=prefix_keys
        )
        files = {
            SPEC_PART: (None, spec.model_dump_json(), JSON_CONTENT_TYPE),
            "steps": _pickle_part("steps", data),
        }
        return self._json("POST", f"/tables/{table_id}/derive", JobInfo, files=files)

    def stats(self, table_id: str, request: StatsRequest) -> dict[str, Any]:
        """``POST /v1/tables/{id}/stats``: statistics on all rows, as the
        ``dict`` that ``docs/api-v1.md`` describes for the request's kind."""
        return self._pickle(
            "POST",
            f"/tables/{table_id}/stats",
            # Fitting some curves (e.g. beta) to millions of rows takes minutes.
            timeout=STATS_TIMEOUT,
            content=request.model_dump_json(),
            headers={"Content-Type": JSON_CONTENT_TYPE},
        )

    # Jobs

    def submit_job(
        self,
        kind: str,
        inputs: Mapping[str, str | list[str]] | None = None,
        params: Mapping[str, Any] | None = None,
        objects: Mapping[str, Any] | None = None,
    ) -> JobInfo:
        """``POST /v1/jobs``: start a job.

        ``inputs`` reference tables and objects on the machine by ID;
        ``params`` are JSON options; ``objects`` are pickled and sent as
        parts with the same names (e.g. ``{"learners": [...]}``).
        """
        objects = dict(objects or {})
        spec = JobRequest(
            kind=str(getattr(kind, "value", kind)),
            inputs=dict(inputs or {}),
            params=json.loads(json.dumps(dict(params or {}))),
            parts=list(objects),
        )
        return self._json("POST", "/jobs", JobInfo, files=self._multipart(spec, objects))

    def job_info(self, job_id: str) -> JobInfo:
        """``GET /v1/jobs/{id}``."""
        return self._json("GET", f"/jobs/{job_id}", JobInfo)

    def job_result(self, job_id: str) -> JobResult:
        """``GET /v1/jobs/{id}/result`` of a finished job; raises the job's
        error if it failed or was cancelled."""
        return self._json("GET", f"/jobs/{job_id}/result", JobResult)

    def cancel_job(self, job_id: str, timeout: httpx.Timeout | None = None) -> JobInfo:
        """``DELETE /v1/jobs/{id}``: the answer is already ``cancelled``; the
        operation stops at its next check. ``timeout`` replaces the client's."""
        options = {} if timeout is None else {"timeout": timeout}
        return self._json("DELETE", f"/jobs/{job_id}", JobInfo, **options)

    def wait_for_job(
        self,
        job: str | JobInfo,
        on_progress: ProgressCallback | None = None,
        should_cancel: Callable[[], bool] | None = None,
        poll_interval: float = 0.2,
        max_poll_interval: float = 1.0,
        timeout: float | None = None,
    ) -> JobResult:
        """Wait until a job finishes and return its result.

        Polls ``GET /v1/jobs/{id}``, starting every ``poll_interval`` seconds
        and slowing down gently to ``max_poll_interval``.

        * ``on_progress(fraction, text)`` is called whenever the progress or
          the status text changes.
        * ``should_cancel()`` is checked often (also between polls); when it
          returns True, the job is cancelled on the machine (waiting at most
          :data:`CANCEL_TIMEOUT`) and :class:`CloudCancelled` is raised.
        * A failed job raises its error: :class:`CloudError` (or a subclass,
          e.g. :class:`CloudOutOfMemoryError`) with the remote traceback. A
          job cancelled elsewhere raises :class:`CloudCancelled`.
        * After ``timeout`` seconds (``None``: no limit) a :class:`CloudError`
          is raised; the job keeps running.
        """
        job_id = job.id if isinstance(job, JobInfo) else job
        deadline = None if timeout is None else time.monotonic() + timeout
        interval = poll_interval
        last: tuple[float, str] | None = None

        def cancel_requested() -> bool:
            if should_cancel is not None and should_cancel():
                self.cancel_job(job_id, CANCEL_TIMEOUT)
                raise CloudCancelled(ErrorCode.CANCELLED, "The job was cancelled.")
            return False

        while True:
            cancel_requested()
            info = self.job_info(job_id)
            if on_progress is not None and (info.progress, info.status) != last:
                last = (info.progress, info.status)
                on_progress(info.progress, info.status)
            if info.state == JobState.DONE:
                return self.job_result(job_id)
            if info.state.finished:
                error = info.error or ErrorResponse(
                    code=ErrorCode.CANCELLED
                    if info.state == JobState.CANCELLED
                    else ErrorCode.JOB_FAILED,
                    message=f"The job {info.state.value}.",
                )
                raise CloudError.from_response(error)
            if deadline is not None and time.monotonic() >= deadline:
                raise CloudError(None, f"The job {job_id} didn't finish in {timeout} s.")
            # Sleep in short slices, so a cancel request is noticed quickly.
            wake = time.monotonic() + interval
            while (left := wake - time.monotonic()) > 0:
                cancel_requested()
                time.sleep(min(left, 0.05))
            interval = min(max_poll_interval, interval * 1.5)

    # Stored objects

    def get_object(self, object_id: str, on_progress: DownloadProgress | None = None) -> Any:
        """``GET /v1/objects/{id}``: download and unpickle a stored object.

        A download that breaks off continues where it stopped (a ``Range``
        request for the rest of the same file), as long as each attempt gets
        more bytes; ``on_progress(received, total)`` is called as bytes come.
        """
        return loads(self._download(f"/objects/{object_id}", on_progress))

    def _download(self, path: str, on_progress: DownloadProgress | None) -> bytearray:
        """The body of a large ``GET`` of a pickle, continued after breaks."""
        download = _Download(API_PREFIX + path, on_progress)
        failures = 0
        while True:
            received = len(download.data)
            try:
                return self._download_part(download)
            except (*_RETRY_ERRORS, httpx.ReadTimeout) as exc:
                # A read timeout counts only once the body is coming: before
                # that the machine is busy, and asking again won't help.
                body_broke = download.started and not isinstance(exc, httpx.PoolTimeout)
                retry = body_broke or not isinstance(exc, httpx.ReadTimeout)
                failures = 0 if len(download.data) > received else failures + 1
                gave_up = failures > self.retries or self._gave_up()
                if not retry or gave_up or _is_certificate_error(exc):
                    raise self._connection_error(exc) from exc
            except _RetryStatus:
                failures += 1
                if failures > self.retries or self._gave_up():
                    raise
            time.sleep(self.retry_backoff * 2 ** max(0, failures - 1))

    def _download_part(self, download: _Download) -> bytearray:
        """One request: the whole file, or the rest of it."""
        download.started = False
        headers = download.resume_headers()
        with self._http.stream(
            "GET", download.url, headers=headers, extensions=self._extensions
        ) as response:
            if not response.is_success:
                response.read()
                if response.status_code in _RETRY_STATUSES:
                    raise _RetryStatus(self._error(response))
                raise self._error(response)
            download.begin(response)
            download.started = True
            for chunk in response.iter_bytes():
                download.add(chunk)
        return download.finish()

    def delete_object(self, object_id: str) -> DeleteResponse:
        """``DELETE /v1/objects/{id}``."""
        return self._json("DELETE", f"/objects/{object_id}", DeleteResponse)


class _RetryStatus(CloudError):
    """A ``502``/``503``/``504`` answer to a download, worth asking again."""

    def __init__(self, error: CloudError):
        super().__init__(error.code, error.message, error.remote_traceback, error.status_code)


class _Download:
    """The bytes of a download so far, and what continuing it needs."""

    def __init__(self, url: str, on_progress: DownloadProgress | None):
        self.url = url
        self.on_progress = on_progress
        self.data = bytearray()
        self.total: int | None = None
        self.etag: str | None = None  #: the file's version, from the first answer
        self.started = False  #: the current answer's body is coming

    def resume_headers(self) -> dict[str, str]:
        """Ask for the rest of the same file (else the machine sends all of it)."""
        if not self.data or not self.etag:
            self.data.clear()
            return {}
        return {"Range": f"bytes={len(self.data)}-", "If-Range": self.etag}

    def begin(self, response: httpx.Response) -> None:
        """Check an answer's headers; a full answer starts from the beginning."""
        content_type = response.headers.get("content-type", "").split(";")[0].strip()
        if (
            response.headers.get(PAYLOAD_HEADER) != PAYLOAD_PICKLE
            or content_type != PICKLE_CONTENT_TYPE
        ):
            raise CloudError(None, f"GET {self.url} didn't answer with a pickle.", None,
                             response.status_code)  # fmt: skip
        if response.status_code != 206:
            self.data.clear()
        self.etag = response.headers.get("etag")
        length = response.headers.get("content-length", "")
        self.total = len(self.data) + int(length) if length.isdigit() else None
        self._report()

    def add(self, chunk: bytes) -> None:
        """Keep a chunk of the body."""
        self.data += chunk
        self._report()

    def finish(self) -> bytearray:
        """All the bytes, after checking that none are missing."""
        if self.total is not None and len(self.data) != self.total:
            raise CloudConnectionError(
                f"GET {self.url} ended after {len(self.data)} of {self.total} bytes."
            )
        return self.data

    def _report(self) -> None:
        if self.on_progress is not None:
            self.on_progress(len(self.data), self.total)


def _pickle_part(name: str, data: bytes) -> tuple:
    """A multipart file part that carries a pickle."""
    return (name, data, PICKLE_CONTENT_TYPE, {PAYLOAD_HEADER: PAYLOAD_PICKLE})


def _is_certificate_error(exc: BaseException) -> bool:
    """True for TLS verification failures (wrong certificate or name)."""
    return isinstance(exc, httpx.ConnectError) and (
        "CERTIFICATE_VERIFY_FAILED" in str(exc)
        or isinstance(exc.__cause__ or exc.__context__, ssl.SSLCertVerificationError)
    )


def _remaining_size(f: IO[bytes]) -> int | None:
    """Bytes left to read in ``f``, or ``None`` if it can't tell."""
    try:
        return os.fstat(f.fileno()).st_size - f.tell()
    except (AttributeError, OSError, io.UnsupportedOperation):  # not a file on disk
        return _seek_remaining(f)


def _seek_remaining(f: IO[bytes]) -> int | None:
    """Bytes left to read in ``f``, by seeking to its end and back."""
    try:
        here = f.tell()
        end = f.seek(0, io.SEEK_END)
        f.seek(here)
        return end - here
    except (AttributeError, OSError, io.UnsupportedOperation):
        return None
