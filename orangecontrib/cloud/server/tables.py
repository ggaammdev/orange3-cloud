"""Table store: the ``load_table`` job, uploads, and the ``/v1/tables`` routes.

Loading itself (sources, domain inference, Orange's header rows, memory) is
in :mod:`orangecontrib.cloud.server.loader`.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

import numpy as np
import pyarrow as pa
from fastapi import APIRouter, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from orangecontrib.cloud.protocol.errors import ErrorCode
from orangecontrib.cloud.protocol.jobs import JobInfo, JobKind
from orangecontrib.cloud.protocol.machine import DeleteResponse
from orangecontrib.cloud.protocol.pickles import dumps
from orangecontrib.cloud.protocol.tables import (
    DEFAULT_PAGE_ROWS,
    MAX_PAGE_ROWS,
    ColumnSpec,
    RowsParams,
    TableCreateRequest,
    TableFormat,
    TableInfo,
    TableList,
    UploadInfo,
    UploadParams,
)
from orangecontrib.cloud.protocol.wire import PAYLOAD_HEADER, PAYLOAD_PICKLE, PICKLE_CONTENT_TYPE
from orangecontrib.cloud.server.app import get_config
from orangecontrib.cloud.server.errors import ApiError
from orangecontrib.cloud.server.jobs import get_jobs
from orangecontrib.cloud.server.loader import LoadOptions, default_name, load_table
from orangecontrib.cloud.server.sources import (
    detect_format,
    display_location,
    is_remote,
    local_path,
    unreadable_location_message,
)
from orangecontrib.cloud.server.worker import (
    JobContext,
    LoadedFrom,
    OperationError,
    operation,
    worker_store,
)

__all__ = ["load_table_operation", "router"]

_UPLOAD_ID = re.compile(r"^u-[0-9a-f]{12}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


@operation(JobKind.LOAD_TABLE, public=False)
def load_table_operation(ctx: JobContext) -> dict[str, Any]:
    """Load a file, or several files with the same columns, into a table.
    Params: ``locations``, ``format``, ``columns`` (ColumnSpec dicts),
    ``name``, ``source``, ``tmp_dir``, ``copy_first``."""
    p = ctx.params
    try:
        loaded = load_table(
            p["locations"],
            p["format"],
            LoadOptions(
                columns=[ColumnSpec.model_validate(c) for c in p.get("columns", [])],
                name=p.get("name"),
                tmp_dir=p.get("tmp_dir"),
                copy_first=p.get("copy_first", False),
            ),
            progress=ctx.progress,
            check_cancelled=ctx.check_cancelled,
        )
    except FileNotFoundError as exc:
        raise OperationError(ErrorCode.NOT_FOUND, f"File not found: {p['source']}") from exc
    except pa.ArrowInvalid as exc:
        raise OperationError(ErrorCode.BAD_REQUEST, f"Can't read {p['source']}: {exc}") from exc
    ctx.add_table(
        "table",
        loaded.table,
        table_name=loaded.table.name,
        loaded_from=LoadedFrom(p["source"], TableFormat(p["format"]), loaded.peak_memory_bytes),
    )
    return {"load_seconds": round(loaded.seconds, 3), "warnings": loaded.warnings}


# --------------------------------------------------------------------------
# Functions the server runs in the worker
# --------------------------------------------------------------------------


def _domain_pickle(table_id: str) -> bytes | None:
    table = worker_store().tables.get(table_id)
    if table is None:
        return None
    return dumps(table.domain)


#: Sort orders kept in the worker, so paging through a sorted table doesn't
#: sort it again: ``(table_id, column, descending)`` -> row indices.
_SORT_ORDERS: dict[tuple[str, str, bool], np.ndarray] = {}
_SORT_ORDERS_KEPT = 4


def _sort_order(table_id: str, table, column: str, descending: bool) -> np.ndarray:
    """Row indices of ``table`` sorted by ``column``; missing values last.
    Categorical columns sort by the order of their values, like Orange."""
    store = worker_store()
    for key in [k for k in _SORT_ORDERS if k[0] not in store.tables]:
        del _SORT_ORDERS[key]
    key = (table_id, column, descending)
    order = _SORT_ORDERS.get(key)
    if order is not None and len(order) == len(table):
        return order
    var = table.domain[column]
    values = table.get_column(var)
    if var.is_primitive():
        values = values.astype(float)
        # argsort puts NaN last, also for the negated values.
        order = np.argsort(-values if descending else values, kind="stable")
    else:
        values = values.astype(str)
        order = np.argsort(values, kind="stable")
        if descending:
            order = order[::-1]
        missing = values[order] == ""
        order = np.concatenate([order[~missing], order[missing]])
    while len(_SORT_ORDERS) >= _SORT_ORDERS_KEPT:
        del _SORT_ORDERS[next(iter(_SORT_ORDERS))]
    _SORT_ORDERS[key] = order
    return order


def _rows_pickle(
    table_id: str, start: int, stop: int, sort: str | None, descending: bool
) -> bytes | str | None:
    """A page of rows as a pickled ``Table``; ``None`` for an unknown table,
    a message for an unknown column."""
    table = worker_store().tables.get(table_id)
    if table is None:
        return None
    n = len(table)
    start, stop = min(start, n), min(stop, n)
    if sort is None:
        page = table[start:stop]
    else:
        domain = table.domain
        if sort not in [v.name for v in domain.variables + domain.metas]:
            return f"The table has no column '{sort}'."
        page = table[_sort_order(table_id, table, sort, descending)[start:stop]]
    return dumps(page)


# --------------------------------------------------------------------------
# Uploads
# --------------------------------------------------------------------------


def _data_dir(request: Request) -> Path:
    config = get_config(request)
    return config.data_dir or Path(tempfile.gettempdir()) / "orange3-cloud"


def _uploads_dir(request: Request) -> Path:
    return _data_dir(request) / "uploads"


def _upload_path(request: Request, upload_id: str) -> Path:
    folder = _uploads_dir(request) / upload_id
    files = sorted(folder.iterdir()) if _UPLOAD_ID.match(upload_id) and folder.is_dir() else []
    if not files:
        raise ApiError(ErrorCode.NOT_FOUND, f"No upload with ID '{upload_id}'.")
    return files[0]


def _safe_filename(filename: str) -> str:
    name = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = re.sub(r"[\x00-\x1f]", "_", name)
    if name in ("", ".", ".."):
        raise ApiError(ErrorCode.BAD_REQUEST, "The upload needs a file name.")
    return name


#: Bytes collected before each write of an upload to disk.
_UPLOAD_WRITE_BYTES = 4 * 1024**2


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

router = APIRouter()


@router.post("/uploads", response_model=UploadInfo)
async def upload_file(request: Request, params: Annotated[UploadParams, Query()]) -> UploadInfo:
    """Store the request body as a file for ``POST /v1/tables``."""
    filename = _safe_filename(params.filename)
    folder = _uploads_dir(request) / f"u-{uuid.uuid4().hex[:12]}"
    length = request.headers.get("content-length")
    await asyncio.to_thread(folder.mkdir, parents=True)
    if length and length.isdigit() and int(length) > shutil.disk_usage(folder).free:
        await asyncio.to_thread(shutil.rmtree, folder, True)
        raise ApiError(ErrorCode.PAYLOAD_TOO_LARGE, "The file is bigger than the free disk space.")
    path = folder / filename
    size = 0
    try:
        with open(path, "wb") as f:
            buffer = bytearray()
            async for chunk in request.stream():
                buffer += chunk
                if len(buffer) >= _UPLOAD_WRITE_BYTES:
                    await asyncio.to_thread(f.write, buffer)
                    size += len(buffer)
                    buffer = bytearray()
            await asyncio.to_thread(f.write, buffer)
            size += len(buffer)
    except BaseException:
        await asyncio.to_thread(shutil.rmtree, folder, True)
        raise
    return UploadInfo(id=folder.name, filename=filename, size_bytes=size, created_at=_now())


def _check_location(location: str) -> None:
    """Refuse a location the machine can't read, and a missing local file."""
    if is_remote(location):
        return
    path = local_path(location)
    if not os.path.isabs(path):
        raise ApiError(ErrorCode.BAD_REQUEST, unreadable_location_message())
    if not os.path.isfile(path):
        raise ApiError(ErrorCode.NOT_FOUND, f"No file at '{path}' on the machine.")


def _format(locations: list[str], fmt: TableFormat | None) -> TableFormat:
    """The files' format: from their names unless given; the same for all."""
    try:
        formats = {detect_format(location, fmt)[0] for location in locations}
    except ValueError as exc:
        raise ApiError(ErrorCode.BAD_REQUEST, str(exc)) from None
    if len(formats) > 1:
        raise ApiError(
            ErrorCode.BAD_REQUEST, "The files must all be CSV files or all Parquet files."
        )
    return formats.pop()


def _load_params(body: TableCreateRequest, request: Request) -> dict[str, Any]:
    locations = body.locations
    if locations is None:
        path = _upload_path(request, body.upload_id)
        locations, source = [str(path)], f"upload:{body.upload_id}"
    else:
        for location in locations:
            _check_location(location)
        source = display_location(locations[0])
        if len(locations) > 1:
            source += f" (+{len(locations) - 1} more files)"
    fmt = _format(locations, body.format)
    name = body.name or default_name(locations[0])
    if not body.name and len(locations) > 1:
        name += f" and {len(locations) - 1} more"
    tmp_dir = _data_dir(request) / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    return {
        "locations": locations,
        "format": fmt.value,
        "columns": [c.model_dump(mode="json") for c in body.columns],
        "name": name,
        "source": source,
        "tmp_dir": str(tmp_dir),
        "copy_first": body.copy_first,
    }


@router.post("/tables", status_code=202, response_model=JobInfo)
def create_table(body: TableCreateRequest, request: Request) -> JobInfo:
    """Start a job that loads a file into a table."""
    manager = get_jobs(request)
    params = _load_params(body, request)
    return manager.submit(JobKind.LOAD_TABLE.value, params=params)


@router.get("/tables", response_model=TableList)
def list_tables(request: Request) -> TableList:
    """All tables on the machine."""
    return TableList(tables=get_jobs(request).list_tables())


@router.get("/tables/{table_id}", response_model=TableInfo)
def table_info(table_id: str, request: Request) -> TableInfo:
    """A table's metadata."""
    return get_jobs(request).table_info(table_id)


def _pickle_response(data: bytes | None, table_id: str) -> Response:
    if data is None:
        raise ApiError(ErrorCode.NOT_FOUND, f"No table with ID '{table_id}'.")
    return Response(
        content=data, media_type=PICKLE_CONTENT_TYPE, headers={PAYLOAD_HEADER: PAYLOAD_PICKLE}
    )


@router.get("/tables/{table_id}/domain")
async def table_domain(table_id: str, request: Request) -> Response:
    """A table's Orange ``Domain``, pickled."""
    manager = get_jobs(request)
    manager.table_info(table_id)
    data = await run_in_threadpool(manager.run_on_worker, _domain_pickle, table_id)
    return _pickle_response(data, table_id)


@router.get("/tables/{table_id}/rows")
async def table_rows(
    table_id: str, request: Request, params: Annotated[RowsParams, Query()]
) -> Response:
    """A page of a table's rows as an Orange ``Table``, pickled."""
    manager = get_jobs(request)
    manager.table_info(table_id)
    stop = params.start + DEFAULT_PAGE_ROWS if params.stop is None else params.stop
    if stop < params.start:
        raise ApiError(ErrorCode.BAD_REQUEST, "'stop' must not be smaller than 'start'.")
    if stop - params.start > MAX_PAGE_ROWS:
        raise ApiError(ErrorCode.BAD_REQUEST, f"At most {MAX_PAGE_ROWS:,} rows per request.")
    data = await run_in_threadpool(
        manager.run_on_worker,
        _rows_pickle,
        table_id,
        params.start,
        stop,
        params.sort,
        params.descending,
    )
    if isinstance(data, str):
        raise ApiError(ErrorCode.BAD_REQUEST, data)
    return _pickle_response(data, table_id)


@router.delete("/tables/{table_id}", response_model=DeleteResponse)
def delete_table(table_id: str, request: Request) -> DeleteResponse:
    """Delete a table."""
    get_jobs(request).delete_table(table_id)
    return DeleteResponse(id=table_id)
