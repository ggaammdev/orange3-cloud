"""The endpoints of API v1 (:data:`ENDPOINTS`): for each, its method and path,
and the models of its request and answer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from orangecontrib.cloud.protocol.jobs import JobInfo, JobRequest, JobResult
from orangecontrib.cloud.protocol.machine import DeleteResponse, InfoResponse, ShutdownResponse
from orangecontrib.cloud.protocol.tables import (
    DeriveRequest,
    RowsParams,
    StatsRequest,
    TableCreateRequest,
    TableInfo,
    TableList,
    UploadInfo,
    UploadParams,
)
from orangecontrib.cloud.protocol.wire import API_PREFIX

__all__ = ["ENDPOINTS", "Endpoint", "RequestBody", "ResponseBody"]


#: How a request sends its body.
RequestBody = Literal["none", "json", "multipart", "raw"]
#: How a response returns its body.
ResponseBody = Literal["json", "pickle"]


@dataclass(frozen=True)
class Endpoint:
    """One endpoint of API v1."""

    method: str
    path: str  #: full path, with ``{id}`` placeholders
    summary: str
    #: ``json``: the model is the body. ``multipart``: the model is the
    #: ``spec`` part and ``pickle_parts`` are the other parts. ``raw``: the
    #: body is a file stream. ``none``: no body.
    request_body: RequestBody = "none"
    request_model: type[BaseModel] | None = None
    #: Query parameters, if any.
    query_model: type[BaseModel] | None = None
    pickle_parts: tuple[str, ...] = ()
    response_body: ResponseBody = "json"
    response_model: type[BaseModel] | None = None
    #: True if the endpoint starts a job and answers ``202`` with a JobInfo.
    job: bool = False

    @property
    def key(self) -> str:
        """The endpoint as ``METHOD /path``."""
        return f"{self.method} {self.path}"


_P = API_PREFIX

ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint(
        "GET",
        f"{_P}/info",
        "Machine and version information",
        response_model=InfoResponse,
    ),
    Endpoint(
        "POST",
        f"{_P}/shutdown",
        "Shut the machine down",
        response_model=ShutdownResponse,
    ),
    Endpoint(
        "POST",
        f"{_P}/uploads",
        "Upload a file",
        request_body="raw",
        query_model=UploadParams,
        response_model=UploadInfo,
    ),
    Endpoint(
        "POST",
        f"{_P}/tables",
        "Load a file into a table",
        request_body="json",
        request_model=TableCreateRequest,
        response_model=JobInfo,
        job=True,
    ),
    Endpoint("GET", f"{_P}/tables", "List tables", response_model=TableList),
    Endpoint("GET", f"{_P}/tables/{{id}}", "Table metadata", response_model=TableInfo),
    Endpoint(
        "GET",
        f"{_P}/tables/{{id}}/domain",
        "Pickled Orange Domain of a table",
        response_body="pickle",
    ),
    Endpoint(
        "GET",
        f"{_P}/tables/{{id}}/rows",
        "Pickled page of a table's rows, optionally sorted",
        query_model=RowsParams,
        response_body="pickle",
    ),
    Endpoint(
        "POST",
        f"{_P}/tables/{{id}}/derive",
        "Replay recorded steps on all rows",
        request_body="multipart",
        request_model=DeriveRequest,
        pickle_parts=("steps",),
        response_model=JobInfo,
        job=True,
    ),
    Endpoint(
        "POST",
        f"{_P}/tables/{{id}}/stats",
        "Statistics on all rows",
        request_body="json",
        request_model=StatsRequest,
        response_body="pickle",
    ),
    Endpoint("DELETE", f"{_P}/tables/{{id}}", "Delete a table", response_model=DeleteResponse),
    Endpoint(
        "POST",
        f"{_P}/jobs",
        "Start a job",
        request_body="multipart",
        request_model=JobRequest,
        response_model=JobInfo,
        job=True,
    ),
    Endpoint("GET", f"{_P}/jobs/{{id}}", "Job state and progress", response_model=JobInfo),
    Endpoint(
        "GET",
        f"{_P}/jobs/{{id}}/result",
        "Result of a finished job",
        response_model=JobResult,
    ),
    Endpoint("DELETE", f"{_P}/jobs/{{id}}", "Cancel a job", response_model=JobInfo),
    Endpoint(
        "GET",
        f"{_P}/objects/{{id}}",
        "Download a stored object as a pickle",
        response_body="pickle",
    ),
    Endpoint(
        "DELETE",
        f"{_P}/objects/{{id}}",
        "Delete a stored object",
        response_model=DeleteResponse,
    ),
)
