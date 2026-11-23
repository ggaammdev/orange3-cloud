"""A rough estimate of the memory a file needs once it's loaded on the machine.

Cloud File uses it to warn before loading a file that is too big for the
machine, and to suggest a machine size that fits. It reads only the file's
size and its first rows (about 1 MB), on this computer:

* the rows: the file's size divided by the size of the first rows (``.gz``
  files are assumed to be compressed about 4 times),
* the table: rows x columns x 8 bytes, with more for text columns with many
  different values (which stay Python strings on the machine),
* working memory for algorithms: 3 to 5 times the table, on top of it.

The estimate is rough, and the widget says so.
"""

from __future__ import annotations

import csv
import gzip as gzip_module
import io
import math
import os
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from orangecontrib.cloud.client.providers.base import MachineType
from orangecontrib.cloud.client.providers.sizes import (
    DISK_DEFAULT_GB,
    DISK_MAX_GB,
    suggest_machine_type,
)
from orangecontrib.cloud.protocol.tables import FORMAT_EXTENSIONS, GZIP_EXTENSIONS

__all__ = [
    "GZIP_RATIO",
    "MemoryEstimate",
    "combine_estimates",
    "estimate_csv",
    "estimate_local_file",
    "estimate_stored",
    "estimate_url",
    "file_format",
    "format_bytes",
]

#: Bytes of (uncompressed) text read from the start of a CSV file.
HEAD_BYTES = 1024**2
#: Assumed compression of ``.gz`` files.
GZIP_RATIO = 4
#: Assumed in-memory size of a Parquet file per byte of file, when its
#: metadata can't be read on this computer (pyarrow isn't installed).
PARQUET_RATIO = 3
#: Working memory for algorithms, as a multiple of the table's size.
WORKING_LOW, WORKING_HIGH = 3, 5
#: Memory of one text value kept as a Python string, without its characters.
STRING_BYTES = 57
#: Text columns with at most this many distinct values become categorical.
CATEGORICAL_MAX = 100
#: Space on the boot disk for the system, Python and packages.
DISK_SYSTEM_GB = 30

_NULLS = {"", "?", "NA", "N/A", "n/a", "NaN", "nan", "NULL", "null", "#N/A", "-nan", "-NaN"}
_DELIMITERS = ",\t;|"


def file_format(name: str) -> tuple[str, bool]:
    """``("csv" | "parquet", gzip)`` from a file name or URL; raises
    ``ValueError`` for other files."""
    base = name.lower().split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    gz = base.endswith(GZIP_EXTENSIONS)
    if gz:
        base = base.rsplit(".", 1)[0]
    for fmt, extensions in FORMAT_EXTENSIONS.items():
        if base.endswith(extensions):
            return fmt.value, gz
    raise ValueError(f"'{name}' isn't a CSV or Parquet file (.csv, .parquet, optionally .gz).")


def format_bytes(n: float) -> str:
    """E.g. "1.5 GB" (binary units, as machine memory is given)."""
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            break
        n /= 1024
    if unit == "bytes":
        return f"{int(n)} bytes"
    return f"{n:.1f} {unit}" if n < 10 else f"{n:.0f} {unit}"


@dataclass
class MemoryEstimate:
    """How much memory a file probably needs on the machine."""

    file_bytes: int  #: size of the file
    format: str  #: ``csv`` or ``parquet``
    gzip: bool
    rows: int | None  #: estimated rows (``None``: unknown)
    columns: int | None
    table_bytes: int  #: estimated size of the loaded table
    exact_rows: bool = False  #: ``rows`` is exact (a small file, or Parquet metadata)
    n_files: int = 1  #: several files with the same columns, loaded as one table

    @property
    def needed_low(self) -> int:
        """The table plus the least working memory."""
        return self.table_bytes * (1 + WORKING_LOW)

    @property
    def needed_high(self) -> int:
        """The table plus the most working memory."""
        return self.table_bytes * (1 + WORKING_HIGH)

    def fits(self, memory_bytes: int) -> bool:
        """Whether a machine with ``memory_bytes`` probably has enough memory."""
        return memory_bytes >= self.needed_low

    def suggested_machine_type(self, types: Sequence[MachineType]) -> MachineType | None:
        """The smallest of ``types`` with memory for the table and the most
        working memory, or else the least; ``None`` if even the largest is too
        small."""
        return suggest_machine_type(self.needed_low, self.needed_high, types)

    @property
    def disk_gb(self) -> int:
        """A boot disk big enough for the file (in GB, at least the default)."""
        gb = math.ceil((self.file_bytes / 1024**3 + DISK_SYSTEM_GB) / 10) * 10
        return min(max(gb, DISK_DEFAULT_GB), DISK_MAX_GB)

    def describe(self) -> str:
        """E.g. "File: 1.2 GB (gzip). About 30,000,000 rows and 12 columns:
        about 2.7 GB in memory, 11–16 GB with working memory for algorithms."""
        files = f"{self.n_files} files" if self.n_files > 1 else "File"
        text = f"{files}: {format_bytes(self.file_bytes)}{' (gzip)' if self.gzip else ''}."
        if self.rows is not None:
            about = "" if self.exact_rows else "about "
            text += f" {about.capitalize()}{self.rows:,} rows"
            if self.columns is not None:
                text += f" and {self.columns} columns"
            text += ":"
        text += (
            f" about {format_bytes(self.table_bytes)} in memory, "
            f"{format_bytes(self.needed_low)}–{format_bytes(self.needed_high)} "
            "with working memory for algorithms."
        )
        return text


def combine_estimates(estimates: Sequence[MemoryEstimate | None]) -> MemoryEstimate | None:
    """The estimate for several files loaded as one table (``None`` if one
    of them has none)."""
    if not estimates or any(e is None for e in estimates):
        return None
    if len(estimates) == 1:
        return estimates[0]
    rows = [e.rows for e in estimates]
    first = estimates[0]
    return MemoryEstimate(
        file_bytes=sum(e.file_bytes for e in estimates),
        format=first.format,
        gzip=any(e.gzip for e in estimates),
        rows=None if None in rows else sum(rows),
        columns=first.columns,
        table_bytes=sum(e.table_bytes for e in estimates),
        exact_rows=all(e.exact_rows for e in estimates),
        n_files=sum(e.n_files for e in estimates),
    )


# --------------------------------------------------------------------------
# CSV


def _is_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def _column_bytes(values: list[str]) -> int:
    """Memory per row of one column, from its first values."""
    present = [v for v in values if v.strip() not in _NULLS]
    if not present or all(_is_number(v) for v in present):
        return 8
    if len(set(present)) <= CATEGORICAL_MAX:
        return 8
    average = sum(len(v) for v in present) / len(present)
    return int(8 + STRING_BYTES + average)


def _orange_header(names: list[str], rows: list[list[str]]) -> bool:
    """The file starts with Orange's three header rows (names, types, flags)."""
    from Orange.data.io_base import DataTableMixin

    header, _ = DataTableMixin.parse_headers([names, *rows[:2]])
    return len(header) == 3


def estimate_csv(head: bytes, file_bytes: int, gzip: bool, complete: bool) -> MemoryEstimate:
    """Estimate from the first (uncompressed) bytes of a CSV file.

    ``complete``: ``head`` is the whole file, so the row count is exact.
    """
    text = head.decode("utf-8", errors="replace")
    if not complete and "\n" in text:
        text = text[: text.rindex("\n") + 1]  # drop the incomplete last line
    first_line = text.split("\n", 1)[0]
    delimiter = max(_DELIMITERS, key=first_line.count) if first_line else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header = next(reader, [])
    rows = [r for r in reader if r]
    n_columns = len(header)
    header_bytes = len(first_line.encode()) + 1
    if _orange_header(header, rows):  # its type and flag rows aren't data
        header_bytes += sum(len(delimiter.join(r).encode()) + 1 for r in rows[:2])
        rows = rows[2:]
    body_bytes = max(len(text.encode()) - header_bytes, 1)
    if complete:
        n_rows, exact = len(rows), True
    elif rows:
        uncompressed = file_bytes * (GZIP_RATIO if gzip else 1)
        n_rows = max(len(rows), round((uncompressed - header_bytes) * len(rows) / body_bytes))
        exact = False
    else:
        n_rows, exact = None, False
    if n_rows is None:
        # Not even one whole row: assume 8 bytes per byte of text.
        table_bytes = file_bytes * (GZIP_RATIO if gzip else 1)
    else:
        per_row = sum(_column_bytes([r[j] for r in rows if len(r) > j]) for j in range(n_columns))
        table_bytes = n_rows * per_row
    return MemoryEstimate(file_bytes, "csv", gzip, n_rows, n_columns, table_bytes, exact)


# --------------------------------------------------------------------------
# Parquet


def _estimate_parquet_metadata(path: str, file_bytes: int, gzip: bool) -> MemoryEstimate:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        return MemoryEstimate(file_bytes, "parquet", gzip, None, None, file_bytes * PARQUET_RATIO)
    meta = pq.ParquetFile(path).metadata
    schema = meta.schema.to_arrow_schema()
    n_rows = meta.num_rows
    per_row = 0
    for j, field in enumerate(schema):
        if pa.types.is_string(field.type) or pa.types.is_large_string(field.type):
            size = sum(meta.row_group(g).column(j).total_uncompressed_size
                       for g in range(meta.num_row_groups))  # fmt: skip
            average = size / max(n_rows, 1)
            # Dictionary-encoded columns with few values store tiny codes.
            per_row += 8 if average < 4 else int(8 + STRING_BYTES + average)
        else:
            per_row += 8
    return MemoryEstimate(
        file_bytes, "parquet", gzip, n_rows, len(schema), n_rows * per_row, exact_rows=True
    )


# --------------------------------------------------------------------------
# Sources


def estimate_local_file(path: str | os.PathLike) -> MemoryEstimate:
    """Estimate for a file on this computer."""
    path = os.fspath(path)
    fmt, gz = file_format(path)
    file_bytes = os.path.getsize(path)
    if fmt == "parquet":
        if gz:
            return MemoryEstimate(file_bytes, fmt, gz, None, None, file_bytes * PARQUET_RATIO)
        return _estimate_parquet_metadata(path, file_bytes, gz)
    opener = gzip_module.open if gz else open
    with opener(path, "rb") as f:
        head = f.read(HEAD_BYTES + 1)
    complete = len(head) <= HEAD_BYTES
    return estimate_csv(head[:HEAD_BYTES], file_bytes, gz, complete)


def estimate_url(url: str, timeout: float = 15.0) -> MemoryEstimate | None:
    """Estimate for an ``http(s)://`` file, from its size and its first bytes
    (one request for a range of bytes). ``None`` if the server doesn't say
    how big the file is."""
    from orangecontrib.cloud.client.urls import read_url_head

    fmt, gz = file_format(url)
    size, raw = read_url_head(url, _head_bytes(fmt, gz), timeout)
    if size is None:
        return None
    return _estimate_head(raw, size, fmt, gz)


def _head_bytes(fmt: str, gz: bool) -> int:
    """Bytes to read from the start of a remote file (none for Parquet)."""
    if fmt != "csv":
        return 0
    return HEAD_BYTES // (GZIP_RATIO if gz else 1)


def _estimate_head(raw: bytes, size: int, fmt: str, gz: bool) -> MemoryEstimate:
    """Estimate from a remote file's size and first (raw) bytes."""
    if fmt == "parquet":
        return MemoryEstimate(size, fmt, gz, None, None, size * PARQUET_RATIO)
    complete = len(raw) >= size
    head = raw
    if gz:
        head = zlib.decompressobj(wbits=31).decompress(raw, HEAD_BYTES)
    return estimate_csv(head, size, gz, complete)


def estimate_stored(uri: str, read_head: Callable[[str, int], tuple[int, bytes]]) -> MemoryEstimate:
    """Estimate for a file in a provider's storage (e.g. ``gs://``), from its
    size and its first bytes: ``read_head(uri, length)`` gives both, read with
    the user's sign-in (:meth:`~orangecontrib.cloud.client.providers.base.
    ClusterProvider.read_stored_head`)."""
    fmt, gz = file_format(uri)
    size, raw = read_head(uri, _head_bytes(fmt, gz))
    return _estimate_head(raw, size, fmt, gz)
