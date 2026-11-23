"""Where the machine reads table files from.

A file uploaded with ``POST /v1/uploads`` (a path on the machine's disk), a
URL with one of :data:`~orangecontrib.cloud.protocol.tables.URI_SCHEMES`: a
``gs://`` URI (``gcsfs``) or an ``http(s)://`` URL (``fsspec``), or any other
path on the machine's own disk. ``.gz`` files are decompressed while reading.
An ``http(s)://`` file that is read more than once over a long time is first
copied to the machine's disk with one request (:func:`copy_to_disk`): a
signed link may stop working before a long load ends.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable

import pyarrow as pa

from orangecontrib.cloud.protocol.errors import ErrorCode
from orangecontrib.cloud.protocol.tables import (
    FORMAT_EXTENSIONS,
    GZIP_EXTENSIONS,
    URI_SCHEMES,
    TableFormat,
)
from orangecontrib.cloud.server.worker import OperationError

__all__ = [
    "base_name",
    "check_disk_space",
    "copy_to_disk",
    "detect_format",
    "display_location",
    "file_name",
    "file_size",
    "is_http",
    "is_remote",
    "local_path",
    "open_stream",
    "read_fully",
    "remote_options",
    "unreadable_location_message",
]

#: Bytes read and written at a time by :func:`copy_to_disk`.
COPY_CHUNK_BYTES = 16 * 1024**2
#: Disk space :func:`copy_to_disk` leaves free, for the machine's other work.
DISK_RESERVE_BYTES = 1024**3


def file_name(location: str) -> str:
    """The file's name in a location (an ``http(s)://`` URL's query, e.g. a
    signature with ``/`` in it, isn't part of it)."""
    return display_location(location).rstrip("/").rsplit("/", 1)[-1]


def base_name(location: str) -> str:
    """A file's name in lower case, without ``.gz``."""
    name = file_name(location).lower()
    return name.rsplit(".", 1)[0] if name.endswith(GZIP_EXTENSIONS) else name


def detect_format(filename: str, fmt: TableFormat | None = None) -> tuple[TableFormat, bool]:
    """Format and gzip compression of a file, from its name unless given."""
    gzip = file_name(filename).lower().endswith(GZIP_EXTENSIONS)
    name = base_name(filename)
    if fmt is not None:
        return fmt, gzip
    for found, extensions in FORMAT_EXTENSIONS.items():
        if name.endswith(extensions):
            return found, gzip
    raise ValueError(
        f"Can't tell the format of '{display_location(filename)}' from its name. "
        "Set the format (csv or parquet)."
    )


def is_remote(location: str) -> bool:
    """The location is a URL the machine reads (:data:`URI_SCHEMES`)."""
    return location.lower().startswith(URI_SCHEMES)


def is_http(location: str) -> bool:
    """The location is an ``http://`` or ``https://`` URL."""
    return location.lower().startswith(("http://", "https://"))


def display_location(location: str) -> str:
    """The location as the machine shows and keeps it: an ``http(s)://`` URL
    without its query, which in a signed link holds the signature."""
    return location.split("?", 1)[0] if is_http(location) else location


def unreadable_location_message() -> str:
    """What a location must be, for the error about one the machine can't read."""
    schemes = ", ".join(URI_SCHEMES[:-1]) + " or " + URI_SCHEMES[-1]
    return f"The URI must start with {schemes}, or be an absolute path on the machine."


def local_path(location: str) -> str:
    """A local path without its ``file://`` prefix."""
    return location[len("file://") :] if location.startswith("file://") else location


def remote_options(location: str) -> dict:
    """fsspec's options for ``location``. An HTTP download may take as long as
    it needs (aiohttp stops any request after 5 minutes by default); only a
    connection that stalls fails."""
    if not is_http(location):
        return {}
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=None, sock_connect=60, sock_read=300)
    return {"client_kwargs": {"timeout": timeout}}


def _remote_size(fs, path: str, missing_ok: bool = True) -> int | None:
    """A remote file's size, ``None`` if the server doesn't tell (or, with
    ``missing_ok``, if there is no such file: opening it then says so)."""
    try:
        return fs.size(path)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    except (OSError, ValueError):  # e.g. a web server that doesn't tell
        return None


def file_size(location: str) -> int | None:
    """A file's size in bytes, ``None`` if a web server doesn't tell."""
    if not is_remote(location):
        return os.path.getsize(local_path(location))
    import fsspec

    fs, path = fsspec.core.url_to_fs(location, **remote_options(location))
    return _remote_size(fs, path, missing_ok=False)


def check_disk_space(sizes: list[int | None], folder: str | None) -> None:
    """Refuse files to be copied to ``folder`` together that don't fit on
    its disk (files of unknown size are left out)."""
    total = sum(size or 0 for size in sizes)
    free = shutil.disk_usage(folder or tempfile.gettempdir()).free
    if total > free - DISK_RESERVE_BYTES:
        raise OperationError(
            ErrorCode.PAYLOAD_TOO_LARGE,
            f"The {len(sizes)} files ({total / 1024**3:.1f} GB together) don't fit on the "
            f"machine's disk ({free / 1024**3:.1f} GB free). Choose a larger disk in the "
            "Cloud settings.",
        )


def _open_raw(location: str) -> tuple[pa.NativeFile, int | None]:
    """The file as an Arrow input stream, and its size if known."""
    if is_remote(location):
        import fsspec

        fs, path = fsspec.core.url_to_fs(location, **remote_options(location))
        size = _remote_size(fs, path)
        # Sequential reads only: plain HTTP streaming needs no range requests
        # (and, with the size known, no second request for it).
        f = fs.open(path, "rb", **({"block_size": 0, "size": size} if is_http(location) else {}))
        return pa.PythonFile(f, mode="r"), size
    path = local_path(location)
    return pa.OSFile(path, "rb"), os.path.getsize(path)


def read_fully(stream: pa.NativeFile, length: int) -> bytes:
    """``length`` bytes of ``stream``, fewer only at its end (an HTTP stream
    returns what has arrived)."""
    parts = []
    while length > 0 and (chunk := stream.read(length)):
        parts.append(chunk)
        length -= len(chunk)
    return b"".join(parts)


def open_stream(location: str, gzip: bool) -> tuple[pa.NativeFile, pa.NativeFile, int | None]:
    """A file as (stream to read, raw file for progress, raw size)."""
    raw, size = _open_raw(location)
    stream = pa.CompressedInputStream(raw, "gzip") if gzip else raw
    return stream, raw, size


def copy_to_disk(
    location: str,
    folder: str | None,
    progress: Callable[[float, str], None],
    check_cancelled: Callable[[], None],
) -> str:
    """Copy a remote file to a new file in ``folder`` with one request and
    return its path (the caller deletes it). A signed link is checked only
    when the request starts, so the copy may take longer than the link
    lives. Refuses a file that doesn't fit on the disk."""
    raw, size = _open_raw(location)
    try:
        free = shutil.disk_usage(folder or tempfile.gettempdir()).free
        if size is not None and size > free - DISK_RESERVE_BYTES:
            raise OperationError(
                ErrorCode.PAYLOAD_TOO_LARGE,
                f"The file ({size / 1024**3:.1f} GB) doesn't fit on the machine's disk "
                f"({free / 1024**3:.1f} GB free). Choose a larger disk in the Cloud settings.",
            )
        fd, path = tempfile.mkstemp(prefix="download-", dir=folder)
        try:
            with os.fdopen(fd, "wb") as f:
                copied = 0
                while chunk := raw.read(COPY_CHUNK_BYTES):
                    check_cancelled()
                    f.write(chunk)
                    copied += len(chunk)
                    total = f" of {size / 1024**2:,.0f}" if size else ""
                    progress(
                        copied / size if size else 0.0,
                        f"Downloaded {copied / 1024**2:,.0f}{total} MB to the machine's disk",
                    )
        except BaseException:
            os.unlink(path)
            raise
    finally:
        raw.close()
    return path
