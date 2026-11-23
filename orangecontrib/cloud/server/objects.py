"""Downloading and deleting stored objects (``/v1/objects/{id}``).

An object (a model, evaluation results, a distance matrix, ...) lives in the
worker's memory. For a download, the worker pickles it once into a file in
``<data dir>/objects``, and the file is streamed in chunks, which waits for
the client. (A pickle sent as one response body broke large downloads over
slow connections: uvicorn closed the seemingly idle connection after its
keep-alive timeout, and asyncio dropped the unsent rest of the body after its
TLS shutdown timeout, about 35 s in all.) The file is served with ``Range``
support, so a download that broke off continues where it stopped, with the
same bytes, instead of starting again and pickling the object again. The newest
:data:`KEEP_FILES` files are kept; an object's file goes when it is deleted.
"""

from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from orangecontrib.cloud.protocol.errors import ErrorCode
from orangecontrib.cloud.protocol.machine import DeleteResponse
from orangecontrib.cloud.protocol.pickles import dump
from orangecontrib.cloud.protocol.wire import PAYLOAD_HEADER, PAYLOAD_PICKLE, PICKLE_CONTENT_TYPE
from orangecontrib.cloud.server.app import get_config
from orangecontrib.cloud.server.errors import ApiError
from orangecontrib.cloud.server.jobs import JobManager, get_jobs
from orangecontrib.cloud.server.worker import worker_store

__all__ = ["KEEP_FILES", "ObjectFiles", "router"]

#: Pickle files kept for downloads, newest first.
KEEP_FILES = 4

_MISSING = object()

#: Makes the app's :class:`ObjectFiles` once, also for parallel first requests.
_CREATE_LOCK = threading.Lock()


def _write_object_pickle(object_id: str, path: str) -> bool:
    """In the worker: pickle a stored object into ``path``; False if it's gone."""
    obj = worker_store().objects.get(object_id, _MISSING)
    if obj is _MISSING:
        return False
    with open(path, "wb") as f:
        dump(obj, f)
    return True


@dataclass
class _ObjectLock:
    """The lock of one object's file, and how many requests hold or wait for it."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    users: int = 0


class ObjectFiles:
    """The pickle files of stored objects, made on the first download."""

    def __init__(self, folder: Path):
        self.folder = folder
        self._lock = threading.Lock()
        self._object_locks: dict[str, _ObjectLock] = {}

    def path(self, object_id: str) -> Path:
        """Where the pickle of ``object_id`` is kept."""
        return self.folder / f"{object_id}.pickle"

    def get(self, jobs: JobManager, object_id: str) -> Path:
        """The object's pickle file, written by the worker if needed."""
        jobs.object_info(object_id)  # not_found for unknown or lost objects
        with self._locked(object_id):  # one pickle per object, also for parallel requests
            path = self.path(object_id)
            if not path.exists():
                self._write(jobs, object_id, path)
        return path

    @contextmanager
    def _locked(self, object_id: str) -> Iterator[None]:
        """Hold the object's own lock. It exists only while requests hold or
        wait for it, so no lock outlives the object."""
        with self._lock:
            entry = self._object_locks.setdefault(object_id, _ObjectLock())
            entry.users += 1
        try:
            with entry.lock:
                yield
        finally:
            with self._lock:
                entry.users -= 1
                if not entry.users:
                    del self._object_locks[object_id]

    def _write(self, jobs: JobManager, object_id: str, path: Path) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=f".{object_id}-", suffix=".tmp")
        os.close(fd)
        try:
            if not jobs.run_on_worker(_write_object_pickle, object_id, tmp):
                raise ApiError(ErrorCode.NOT_FOUND, f"No object with ID '{object_id}'.")
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        self._prune(keep=path)

    def _prune(self, keep: Path) -> None:
        """Delete all but the newest :data:`KEEP_FILES` files."""
        files = sorted(self.folder.glob("*.pickle"), key=_mtime, reverse=True)
        for old in files[KEEP_FILES:]:
            if old != keep:
                old.unlink(missing_ok=True)

    def forget(self, object_id: str) -> None:
        """Delete the object's file, if there is one (after a download that
        is writing it)."""
        with self._locked(object_id):
            self.path(object_id).unlink(missing_ok=True)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return 0.0


def get_object_files(request: Request) -> ObjectFiles:
    """The app's :class:`ObjectFiles`, made on first use."""
    state = request.app.state
    with _CREATE_LOCK:
        files = getattr(state, "object_files", None)
        if files is None:
            data_dir = get_config(request).data_dir or Path(tempfile.gettempdir()) / "orange3-cloud"
            files = state.object_files = ObjectFiles(Path(data_dir) / "objects")
    return files


router = APIRouter()


@router.get("/objects/{object_id}")
def get_object(object_id: str, request: Request) -> FileResponse:
    """The object as a pickle; ``Range`` requests continue a download."""
    path = get_object_files(request).get(get_jobs(request), object_id)
    return FileResponse(
        path,
        media_type=PICKLE_CONTENT_TYPE,
        headers={PAYLOAD_HEADER: PAYLOAD_PICKLE},
    )


@router.delete("/objects/{object_id}", response_model=DeleteResponse)
def delete_object(object_id: str, request: Request) -> DeleteResponse:
    """Delete the object from the worker, and its pickle file."""
    get_jobs(request).delete_object(object_id)
    get_object_files(request).forget(object_id)
    return DeleteResponse(id=object_id)
