"""What the providers' storage of uploaded files has in common.

Each cloud keeps the files uploaded from this computer in one bucket (or
storage account) of the user's own, under the same names
(:func:`object_name`). Uploads go up in parts and continue where they
stopped: a broken connection is retried (:func:`with_retries`), and the
upload's session is kept in the add-on's private folder
(:class:`UploadSessions`), so that the next upload of the same file
continues after an interruption or a restart of Orange.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypeVar

from orangecontrib.cloud.client.providers.base import ProviderError, state_dir, write_private

__all__ = [
    "RETRY_DELAYS",
    "UPLOAD_FOLDER",
    "TransientError",
    "UploadSessions",
    "object_name",
    "with_retries",
]

log = logging.getLogger(__name__)
T = TypeVar("T")

#: Folder of uploaded files in the bucket.
UPLOAD_FOLDER = "uploads/"
#: Seconds to wait before each retry after a broken connection.
RETRY_DELAYS = (1, 2, 4, 8, 16, 30)


class TransientError(ProviderError):
    """The connection broke or the cloud was briefly unavailable: try again."""


def object_name(path: str | os.PathLike, size: int, mtime_ns: int) -> str:
    """The bucket's name for a local file: the same file (path, size and
    modification time) always gets the same name."""
    path = os.path.abspath(path)
    key = hashlib.sha256(f"{path}\0{size}\0{mtime_ns}".encode()).hexdigest()[:16]
    return f"{UPLOAD_FOLDER}{key}/{os.path.basename(path)}"


def with_retries(
    call: Callable[[], T],
    delays: Sequence[float],
    cloud: str,
    check: Callable[[], T] | None = None,
) -> T:
    """``call()``, retried after a broken connection (:class:`TransientError`)
    after each of ``delays`` seconds; with ``check``, each retry asks it
    first (e.g. how much of an upload arrived). If the connection doesn't
    come back, raises :class:`ProviderError` that names ``cloud``."""
    for delay in (*delays, None):
        try:
            return call()
        except TransientError as exc:
            if delay is None:
                raise ProviderError(
                    f"The upload stopped: the connection to {cloud} broke and didn't "
                    "come back. Load again to continue where it stopped."
                ) from exc
            log.info("%s Retrying in %s s.", exc, delay)
        time.sleep(delay)
        if check is not None:
            try:
                return check()
            except TransientError as exc:
                log.info("%s", exc)
    raise AssertionError("unreachable")  # pragma: no cover


class UploadSessions:
    """Uploads in progress, kept in the add-on's private folder in the file
    ``file_name`` (an upload's session lets anyone finish it), so that an
    upload continues after an interruption or a restart of Orange.

    :param path: the file (default: ``file_name`` in :func:`state_dir`).
    """

    def __init__(self, path: Path | None = None, *, file_name: str = "uploads.json"):
        self._path = path
        self.file_name = file_name

    @property
    def path(self) -> Path:
        """The file that keeps the upload sessions."""
        return self._path or state_dir() / self.file_name

    def _read(self) -> dict[str, dict]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("Ignoring the unreadable upload sessions in %s: %s", self.path, exc)
            return {}

    def get(self, uri: str, size: int, mtime_ns: int) -> str | None:
        """The session (e.g. a URL or an upload ID) of an unfinished upload
        of this file, if any."""
        entry = self._read().get(uri)
        if entry and entry.get("size") == size and entry.get("mtime_ns") == mtime_ns:
            return entry.get("session")
        return None

    def put(self, uri: str, size: int, mtime_ns: int, session: str) -> None:
        """Remember an upload's session."""
        sessions = self._read()
        sessions[uri] = {"session": session, "size": size, "mtime_ns": mtime_ns}
        write_private(self.path, json.dumps(sessions, indent=1))

    def drop(self, uri: str) -> None:
        """Forget an upload's session (it finished or expired)."""
        sessions = self._read()
        if sessions.pop(uri, None) is not None:
            write_private(self.path, json.dumps(sessions, indent=1))
