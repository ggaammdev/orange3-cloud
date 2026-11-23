"""Load CSV and Parquet files into Orange tables on the machine.

Sources
-------
A table is loaded from a file uploaded with ``POST /v1/uploads`` (kept in
``<data_dir>/uploads``), a ``gs://`` URI (``gcsfs``), an ``http(s)://`` URL
(``fsspec``), or a path on the machine's own disk. CSV files are read with
pyarrow's streaming reader (:mod:`.csv_file`), Parquet files a row group at
a time (:mod:`.parquet_file`); ``.gz`` files are decompressed while reading
(:mod:`.sources`). An ``http(s)://`` Parquet file (read with many requests
over the whole load) is first copied to the machine's disk with one request,
just before it is read. When the request asks for it (``copy_first``: signed
links, which may expire before a long load ends), every ``http(s)://`` file
is copied at the start of the load, before any is read, and the copies stay
until the load ends.

Several files
-------------
Several files with the same columns (all CSV or all Parquet) are loaded one
after the other into one table, e.g. the months of a year. The first file's
columns are the table's, with their roles and types; the other files'
columns are matched to them by name, or, when no name is the same, by the
same name in other letter case (``Airport_fee`` and ``airport_fee``). Their
types may differ as long as they are read the same way (numbers of any width,
text, dates and times in any unit). A column that a later file doesn't have
is missing in its rows, and a column that only later files have is skipped,
each with a warning. A later file with none of the table's columns, or a
Parquet column that holds numbers in one file and text in another (unless
its type is set), is an error.

Domain inference
----------------
The rules follow Orange's File widget:

* numbers become ``ContinuousVariable``, except columns whose values are all
  in {0, 1} or all in {1, 2}, which become ``DiscreteVariable``;
* text (and true/false) becomes ``DiscreteVariable`` when it has at most
  :data:`DISCRETE_MAX_VALUES` (100) distinct values and at most
  ``round(n_rows ** 0.7)`` (so an ID column of a small table stays text);
  other text becomes a ``StringVariable`` meta column;
* dates and timestamps become ``TimeVariable`` (in seconds since 1970, UTC);
* a column with no values at all becomes ``ContinuousVariable``;
* missing values (empty, ``?``, ``NA``, ``NaN``, ``null``, ...) become NaN
  (``""`` in text columns); spaces around text values are dropped.

A CSV file that starts with Orange's three header rows (names, types, flags)
takes its types and roles from them (:mod:`.orange_header`). A CSV file
whose first line holds only numbers has no header row, as in Orange: its
columns are named ``Feature 1``, ``Feature 2``, ...
``ColumnSpec`` overrides in the request change a column's role (feature,
target, meta, skip) and type (numeric, categorical, text, datetime), also
over the header rows. A text column that the request makes the target is
always categorical.

Memory
------
The files are read in batches. Each batch is converted column by column into
float64 arrays (numbers, category codes, times) and object arrays (text),
and the batch's Arrow data is dropped. At the end, the table's X, Y and
metas arrays are allocated (untouched pages cost no memory) and filled batch
by batch, freeing each batch as soon as it is copied. So loading needs about
the final table's size plus a batch, not 2-3 times the table. Peak memory is
measured (process RSS, sampled in a thread) and stored in the table's
``TableInfo.peak_load_memory_bytes``.
"""

from __future__ import annotations

import mmap
import os
import time
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pyarrow as pa

from orangecontrib.cloud.protocol.errors import ErrorCode
from orangecontrib.cloud.protocol.tables import (
    ColumnRole,
    ColumnSpec,
    ColumnType,
    TableFormat,
    without_extensions,
)
from orangecontrib.cloud.server.columns import (
    DISCRETE_MAX_VALUES,
    NUMERIC,
    TEXT,
    TIME,
    Column,
    arrow_kind,
)
from orangecontrib.cloud.server.csv_file import CsvFile, ReadAsText
from orangecontrib.cloud.server.orange_header import HeaderColumn, OrangeHeader
from orangecontrib.cloud.server.parquet_file import ParquetFile
from orangecontrib.cloud.server.resources import PeakMemory
from orangecontrib.cloud.server.sources import (
    check_disk_space,
    copy_to_disk,
    detect_format,
    display_location,
    file_name,
    file_size,
    is_http,
)
from orangecontrib.cloud.server.worker import OperationError

__all__ = [
    "LoadOptions",
    "LoadedTable",
    "default_name",
    "load_table",
]

#: Share of a file's part of the progress bar for copying it to the disk
#: before reading it.
COPY_SHARE = 0.25

#: Largest part in which loaded values wait for the table to be built.
STORE_BLOCK_BYTES = 16 * 1024**2

#: Files named in one warning about columns that differ between files.
FILES_NAMED = 3

#: Arrow type of the missing values of a column that a file doesn't have.
_NULL_TYPES = {NUMERIC: pa.float64(), TEXT: pa.string(), TIME: pa.timestamp("s")}

#: How the errors name what a column holds.
_KIND_TEXTS = {NUMERIC: "numbers", TEXT: "text", TIME: "dates or times"}


def _float_block(n_columns: int, n_rows: int) -> np.ndarray:
    """An uninitialized (n_columns, n_rows) float64 array in its own memory
    mapping, so its memory goes back to the system as soon as it is freed
    (``malloc`` may keep freed blocks of this size)."""
    nbytes = n_columns * n_rows * 8
    if nbytes == 0:
        return np.empty((n_columns, n_rows))
    return np.frombuffer(mmap.mmap(-1, nbytes), dtype=np.float64).reshape(n_columns, n_rows)


def _unique_names(names: Sequence[str], keep_spaces: bool) -> list[str]:
    """Column names as the table has them: without spaces around them (but
    Orange keeps those at the end in its header rows), a name for unnamed
    columns, and unique."""
    from Orange.data.util import get_unique_names_duplicates

    names = [(n if keep_spaces else n.strip()) or f"Column {i + 1}" for i, n in enumerate(names)]
    return get_unique_names_duplicates(names)


def _files_text(files: list[str]) -> str:
    """E.g. "a.csv, b.csv and 3 more files"."""
    if len(files) <= FILES_NAMED:
        return ", ".join(files[:-1]) + " and " + files[-1] if len(files) > 1 else files[0]
    return ", ".join(files[:FILES_NAMED]) + f" and {len(files) - FILES_NAMED} more files"


@dataclass
class _Batch:
    n_rows: int
    #: Converted values by column index: float64 or object arrays.
    arrays: dict[int, np.ndarray]


@dataclass
class LoadedTable:
    """A loaded table, the memory and time loading it took, and warnings."""

    table: Any  #: Orange.data.Table
    peak_memory_bytes: int
    seconds: float
    #: Columns that couldn't be read (unsupported types), and other notes.
    warnings: list[str] = field(default_factory=list)


@dataclass
class LoadOptions:
    """How :func:`load_table` reads a file."""

    gzip: bool | None = None  #: ``None``: from each file's name
    columns: list[ColumnSpec] = field(default_factory=list)  #: roles and types of columns
    name: str | None = None  #: the table's name; ``None``: from the (first) file's name
    tmp_dir: str | None = None  #: where to put temporary files
    #: Copy an ``http(s)://`` file to the disk with one request before reading it.
    copy_first: bool = False


@dataclass
class _File:
    """One file of the load, and what it is read as."""

    location: str
    gzip: bool
    #: Where it is read from: the location, or its copy on the disk.
    read_from: str = ""


class _Differences:
    """Columns that differ between the files, for one warning per column."""

    def __init__(self) -> None:
        self.missing: dict[str, list[str]] = {}  #: column -> files without it
        self.extra: dict[str, list[str]] = {}  #: column -> files that only have it

    def warnings(self) -> list[str]:
        """One warning per column."""
        texts = [
            f"Column '{name}' isn't in {_files_text(files)}: their rows have missing values in it."
            for name, files in self.missing.items()
        ]
        texts += [
            f"Column '{name}' of {_files_text(files)} isn't in the first file; skipped."
            for name, files in self.extra.items()
        ]
        return texts


class _Loader:
    def __init__(
        self,
        files: list[_File],
        fmt: TableFormat,
        options: LoadOptions,
        progress: Callable[[float, str], None],
        check_cancelled: Callable[[], None],
    ):
        self.files = files
        self.copy_first = options.copy_first
        self.fmt = fmt
        self.specs = {spec.name: spec for spec in options.columns}
        self.name = options.name or default_name(files[0].location)
        self.tmp_dir = options.tmp_dir
        self.progress = progress
        self.check_cancelled = check_cancelled
        self.columns: list[Column] = []
        self.header: OrangeHeader | None = None  #: the first file's header rows, if any
        self.batches: deque[_Batch] = deque()
        self.n_rows = 0
        self.warnings: list[str] = []
        self.differences = _Differences()
        #: Columns read as text because some values didn't fit their type.
        self.as_text: set[int] = set()
        #: Share of the bar's reading part that the copies made first had.
        self.copied_share = 0.0

    # Columns

    def _set_columns(self, names: list[str], kinds: list[str | None]) -> None:
        unknown = [n for n in self.specs if n not in names]
        if unknown:
            raise OperationError(ErrorCode.BAD_REQUEST, f"The file has no column '{unknown[0]}'.")
        self.columns = [
            self._new_column(i, name, kind)
            for i, (name, kind) in enumerate(zip(names, kinds, strict=True))
        ]
        if not self.wanted:
            raise OperationError(ErrorCode.BAD_REQUEST, "Every column is skipped.")

    def _new_column(self, index: int, name: str, kind: str | None) -> Column:
        """A column with its role and type: from the request's ``ColumnSpec``
        if it has them, else from Orange's header rows, if the file has them."""
        spec = self.specs.get(name)
        head = self.header.columns[index] if self.header else HeaderColumn()
        col = Column(index, name, ColumnRole.FEATURE, None, attributes=dict(head.attributes))
        if spec is not None and spec.role:
            col.role = spec.role
        elif head.role:
            col.role, col.file_role = head.role, True
            if head.weight:
                self.warnings.append(
                    f"Column '{name}' holds weights, which cloud tables don't keep; skipped."
                )
        if spec is not None and spec.type:
            col.type = spec.type
        else:
            col.type, col.fixed_values = head.type, head.values
        if kind is None and col.role != ColumnRole.SKIP:
            self.warnings.append(f"Column '{name}' has an unsupported type; skipped.")
            col.role = ColumnRole.SKIP
        col.reset(self._read_kind(kind, col.type))
        return col

    @staticmethod
    def _read_kind(kind: str | None, wanted: ColumnType | None) -> str:
        if wanted == ColumnType.NUMERIC:
            return NUMERIC
        if wanted in (ColumnType.CATEGORICAL, ColumnType.TEXT):
            return TEXT
        if wanted == ColumnType.DATETIME:
            return TIME
        return kind or TEXT

    @property
    def wanted(self) -> list[Column]:
        return [c for c in self.columns if c.role != ColumnRole.SKIP]

    def _match(self, file: _File, names: list[str]) -> list[tuple[Column, int | None]]:
        """The wanted columns and their positions in a later file's
        ``names`` (``None``: not in the file), by name or else by the same
        name in other letter case."""
        exact = {name: pos for pos, name in enumerate(names)}
        folded: dict[str, list[int]] = {}
        for pos, name in enumerate(names):
            folded.setdefault(name.casefold(), []).append(pos)
        where = file_name(file.location)
        matched: list[tuple[Column, int | None]] = []
        for col in self.columns:
            pos = exact.get(col.name)
            if pos is None and len(candidates := folded.get(col.name.casefold(), [])) == 1:
                pos = candidates[0]
            if col.role == ColumnRole.SKIP:
                continue
            if pos is None:
                self.differences.missing.setdefault(col.name, []).append(where)
            matched.append((col, pos))
        if all(pos is None for _, pos in matched):
            raise OperationError(
                ErrorCode.BAD_REQUEST,
                f"{where} has none of the columns of the first file. Load files with the "
                "same columns.",
            )
        known = {n.casefold() for n in (c.name for c in self.columns)}
        for name in names:
            if name.casefold() not in known:
                self.differences.extra.setdefault(name, []).append(where)
        return matched

    def _check_kinds(
        self, file: _File, matched: list[tuple[Column, int | None]], types: list[pa.DataType]
    ) -> None:
        """Refuse a later Parquet file whose column is read differently than
        in the first file (unless its type is set, which says how to read it).
        A column without values (Arrow's null type) fits any."""
        for col, pos in matched:
            if pos is None or col.type is not None or pa.types.is_null(types[pos]):
                continue
            kind = arrow_kind(types[pos])
            if kind == col.kind:
                continue
            found = _KIND_TEXTS.get(kind, "values of an unsupported type")
            first = _KIND_TEXTS[col.kind]
            raise OperationError(
                ErrorCode.BAD_REQUEST,
                f"Column '{col.name}' holds {first} in the first file but {found} in "
                f"{file_name(file.location)}. "
                "Set its type in Cloud File to read all the files the same way.",
            )

    # Batches

    def _add_batch(self, n: int, get_column: Callable[[Column], pa.Array | None]) -> None:
        """Convert a batch of ``n`` rows, one column at a time.

        ``get_column`` gives ``None`` for a column the file doesn't have:
        its values are missing. The values are stored in parts of at most
        about :data:`STORE_BLOCK_BYTES`, so that building the table can free
        them a part at a time even when a batch is big (a Parquet row group).
        """
        wanted = self.wanted
        float_cols = [c for c in wanted if c.float_valued]
        step = max(1024, STORE_BLOCK_BYTES // (8 * max(1, len(float_cols))))
        bounds = [(a, min(n, a + step)) for a in range(0, n, step)] or [(0, 0)]
        blocks = [_float_block(len(float_cols), b - a) for a, b in bounds]
        slot = {c.index: k for k, c in enumerate(float_cols)}
        parts = [_Batch(b - a, {}) for a, b in bounds]
        for col in wanted:
            arr = get_column(col)
            if arr is None:
                arr = pa.nulls(n, _NULL_TYPES[col.kind])
            k = slot.get(col.index)
            out = None
            if k is not None:
                # Convert straight into the block if there is only one.
                out = blocks[0][k] if len(blocks) == 1 else np.empty(n)
            values = col.convert(arr, out)
            if values is None:  # too many categories: now a string column
                for old in self.batches:
                    old.arrays[col.index] = col.codes_to_strings(old.arrays[col.index])
                values = col.convert(arr, None)
                k = None
            del arr
            for (a, b), part, block in zip(bounds, parts, blocks, strict=True):
                if k is None:
                    part.arrays[col.index] = values[a:b] if len(parts) > 1 else values
                else:
                    if len(blocks) > 1:
                        block[k] = values[a:b]
                    part.arrays[col.index] = block[k]
            del values, out
            # Give Arrow's freed buffers back to the system now.
            pa.default_memory_pool().release_unused()
        del blocks
        self.batches.extend(parts)
        self.n_rows += n

    def _restart(self) -> None:
        self.batches.clear()
        self.n_rows = 0
        self.differences = _Differences()
        for col in self.columns:
            col.reset(col.kind)

    # Progress

    def _file_progress(self, i: int, start: float, share: float) -> Callable[[float, str], None]:
        """Progress of reading the ``i``-th file: ``fraction`` of its work,
        from ``start`` and ``share`` of its part of the bar's reading part
        (after the copies made first, before building the table)."""
        n = len(self.files)
        prefix = f"File {i + 1} of {n}: " if n > 1 else ""
        first = self.copied_share

        def report(fraction: float, text: str) -> None:
            done = (i + start + share * min(fraction, 1.0)) / n
            self.progress(0.9 * (first + (1.0 - first) * done), prefix + text)

        return report

    def _copy_progress(self, k: int, n: int) -> Callable[[float, str], None]:
        """Progress of the ``k``-th of the ``n`` copies made first."""
        prefix = f"Copying file {k + 1} of {n}: " if n > 1 else ""

        def report(fraction: float, text: str) -> None:
            self.progress(0.9 * COPY_SHARE * (k + min(fraction, 1.0)) / n, prefix + text)

        return report

    # Files

    def _copied_first(self, file: _File) -> bool:
        """An ``http(s)://`` file that the request asks to copy first (a
        signed link, which may expire before the files after it are read)."""
        return self.copy_first and is_http(file.location)

    def _copy_files(self) -> None:
        """Copy the files the request asks to copy first to the disk, all
        before reading any, so that each link is used soon after it was
        made. They stay until the load ends, also when CSV files are read
        again (:class:`ReadAsText`)."""
        to_copy = [f for f in self.files if self._copied_first(f)]
        if not to_copy:
            return
        self.copied_share = COPY_SHARE
        if len(to_copy) > 1:
            self.progress(0.0, f"Checking the sizes of {len(to_copy)} files")
            sizes = []
            for file in to_copy:
                self.check_cancelled()
                with self._naming_errors(file):
                    sizes.append(file_size(file.location))
            check_disk_space(sizes, self.tmp_dir)
        for k, file in enumerate(to_copy):
            self.check_cancelled()
            with self._naming_errors(file):
                file.read_from = copy_to_disk(
                    file.location, self.tmp_dir, self._copy_progress(k, len(to_copy)),
                    self.check_cancelled,
                )  # fmt: skip

    def _delete_copies(self) -> None:
        """Delete the copies on the disk."""
        for file in self.files:
            if file.read_from and file.read_from != file.location:
                os.unlink(file.read_from)
            file.read_from = ""

    def _copy_if_needed(self, i: int, file: _File) -> float:
        """Copy an ``http(s)://`` Parquet file (read with many requests over
        the whole load) to the disk with one request just before reading it
        (``.parquet.gz`` is decompressed to the disk anyway). Returns the
        share of the file's progress that the copy had."""
        if self._copied_first(file):
            return 0.0  # copied at the start
        file.read_from = file.location
        if not is_http(file.location) or self.fmt != TableFormat.PARQUET or file.gzip:
            return 0.0
        file.read_from = copy_to_disk(
            file.location,
            self.tmp_dir,
            self._file_progress(i, 0.0, COPY_SHARE),
            self.check_cancelled,
        )
        return COPY_SHARE

    def _rows_reporter(self, i: int, start: float) -> Callable[[float], None]:
        report = self._file_progress(i, start, 1.0 - start)
        return lambda fraction: report(fraction, f"Read {self.n_rows:,} rows")

    def _read_csv(self, i: int, file: _File, start: float) -> None:
        f = CsvFile(file.location, file.read_from, file.gzip)
        f.sniff()
        schema = f.infer_schema()
        names = _unique_names(schema.names, keep_spaces=f.header is not None)
        kinds = [arrow_kind(t) for t in schema.types]
        if i == 0:
            if not self.columns:  # not when reading again (some columns as text)
                self.header = f.header
                self._set_columns(names, kinds)
            matched = [(col, col.index) for col in self.wanted]
        else:
            matched = self._match(file, names)
        f.read(schema, names, matched, self.as_text, self._add_batch,
               self._rows_reporter(i, start), self.check_cancelled)  # fmt: skip

    def _read_parquet(self, i: int, file: _File, start: float) -> None:
        if file.gzip:
            self._file_progress(i, start, 0.0)(0.0, "Decompressing")
        with ParquetFile(file.read_from, file.gzip, self.tmp_dir, self.check_cancelled) as f:
            schema = f.schema
            names = _unique_names(schema.names, keep_spaces=False)
            kinds = [arrow_kind(t) for t in schema.types]
            if i == 0:
                self._set_columns(names, kinds)
                matched = [(col, col.index) for col in self.wanted]
            else:
                matched = self._match(file, names)
                self._check_kinds(file, matched, schema.types)
            f.read(matched, self._add_batch, self._rows_reporter(i, start))

    @contextmanager
    def _naming_errors(self, file: _File) -> Iterator[None]:
        """Name the file in the errors about it when there are several."""
        try:
            yield
        except (FileNotFoundError, pa.ArrowInvalid) as exc:
            if len(self.files) == 1:
                raise  # the job names the file
            where = display_location(file.location)
            if isinstance(exc, FileNotFoundError):
                raise OperationError(ErrorCode.NOT_FOUND, f"File not found: {where}") from exc
            raise OperationError(ErrorCode.BAD_REQUEST, f"Can't read {where}: {exc}") from exc

    def _read_file(self, i: int, file: _File) -> None:
        with self._naming_errors(file):
            start = self._copy_if_needed(i, file)
            try:
                if self.fmt == TableFormat.PARQUET:
                    self._read_parquet(i, file, start)
                else:
                    self._read_csv(i, file, start)
            finally:
                if not self._copied_first(file) and file.read_from != file.location:
                    os.unlink(file.read_from)
                    file.read_from = ""

    def _read_files(self) -> None:
        """Read every file. A CSV column with values that don't fit its type
        is read as text, from the first file again (from the copies made
        first, if any)."""
        while True:
            try:
                for i, file in enumerate(self.files):
                    self.check_cancelled()
                    self._read_file(i, file)
            except ReadAsText as signal:
                col = self.columns[signal.column]
                if signal.column in self.as_text:
                    raise OperationError(
                        ErrorCode.BAD_REQUEST, f"Can't read column '{col.name}' as text."
                    ) from signal
                self.as_text.add(signal.column)
                if col.type is None:
                    self.warnings.append(
                        f"Column '{col.name}' has values that aren't {col.kind}; read as text."
                    )
                    col.reset(TEXT)
                self._restart()
            else:
                self.warnings += self.differences.warnings()
                return

    # The table

    def _variable(self, col: Column):
        """The Orange variable for a column, and a function that converts a
        batch's values to the variable's values."""
        from Orange.data import ContinuousVariable, DiscreteVariable, StringVariable, TimeVariable

        if col.kind == NUMERIC:
            small = col.small_ints
            if col.type is None and small and (small <= {0, 1} or small <= {1, 2}):
                values = sorted(small)
                lookup = np.full(4, np.nan)
                for code, value in enumerate(values):
                    lookup[value] = code

                def to_codes(x):
                    return lookup[np.where(np.isnan(x), 3, x).astype(np.intp)]

                return DiscreteVariable(col.name, [str(v) for v in values]), to_codes
            return ContinuousVariable(col.name), None
        if col.kind == TIME:
            have_date, have_time = col.have_date, col.have_time
            if not (have_date or have_time):
                have_date = True
            return TimeVariable(col.name, have_date=have_date, have_time=have_time), None
        if not col.is_string:
            return self._text_variable(col)
        if col.role == ColumnRole.TARGET and not col.file_role:  # Orange: a meta
            raise OperationError(
                ErrorCode.BAD_REQUEST,
                f"Column '{col.name}' is text and can't be the target; make it categorical.",
            )
        return StringVariable(col.name), None

    def _text_variable(self, col: Column):
        """A categorical variable for text with few values, else a text one;
        numeric when every value is missing (as in Orange)."""
        from Orange.data import ContinuousVariable, DiscreteVariable, StringVariable
        from Orange.misc.collections import natural_sorted

        n_values = len(col.values)
        if n_values == 0 and not col.forced_categorical:
            return ContinuousVariable(col.name), None  # codes: all NaN
        limit = min(DISCRETE_MAX_VALUES, round(self.n_rows**0.7))
        if not (col.forced_categorical or n_values <= limit):
            return StringVariable(col.name), col.codes_to_strings
        fixed = col.fixed_values is not None
        order = list(col.values) if fixed else natural_sorted(col.values)
        new_code = {v: k for k, v in enumerate(order)}
        lookup = np.array([new_code[v] for v in col.values] + [np.nan])

        def to_sorted_codes(x):
            return lookup[np.where(np.isnan(x), -1, x).astype(np.intp)]

        return DiscreteVariable(col.name, order), to_sorted_codes

    def _build_table(self):
        from Orange.data import Domain, StringVariable, Table

        self.progress(0.9, "Building the table")
        parts: dict[str, list] = {"X": [], "Y": [], "metas": []}
        placement = []  # (column, part, position, convert)
        for col in self.wanted:
            var, convert = self._variable(col)
            var.attributes.update(col.attributes)
            if col.role == ColumnRole.META or isinstance(var, StringVariable):
                part = "metas"
            elif col.role == ColumnRole.TARGET:
                part = "Y"
            else:
                part = "X"
            placement.append((col, part, len(parts[part]), convert))
            parts[part].append(var)
        attributes, class_vars, metas = parts["X"], parts["Y"], parts["metas"]
        domain = Domain(attributes, class_vars, metas)

        n = self.n_rows
        # np.empty doesn't touch the memory: pages are used only once the
        # loop below fills them, while it frees the batches.
        X = np.empty((n, len(attributes)))
        Y = np.empty((n, len(class_vars))) if len(class_vars) != 1 else np.empty(n)
        M = np.empty((n, len(metas)), dtype=object)
        arrays = {"X": X, "Y": Y, "metas": M}
        start = 0
        while self.batches:
            self.check_cancelled()
            batch = self.batches.popleft()
            stop = start + batch.n_rows
            for col, part, pos, convert in placement:
                values = batch.arrays.pop(col.index)
                if convert is not None:
                    values = convert(values)
                array = arrays[part]
                if array.ndim == 1:
                    array[start:stop] = values
                else:
                    array[start:stop, pos] = values
                del values
            del batch
            start = stop
            self.progress(0.9 + 0.1 * start / max(n, 1), "Building the table")
        table = Table.from_numpy(domain, X, Y if class_vars else None, M if metas else None)
        table.name = self.name
        return table

    def run(self) -> LoadedTable:
        # Import first, so that importing isn't counted as loading memory.
        import Orange.data  # noqa: F401
        import Orange.misc.collections  # noqa: F401
        import pandas  # noqa: F401
        import pyarrow.csv  # noqa: F401
        import pyarrow.parquet  # noqa: F401

        started = time.monotonic()
        with PeakMemory() as peak:
            try:
                self._copy_files()
                self._read_files()
            finally:
                self._delete_copies()
            table = self._build_table()
        return LoadedTable(table, peak.extra_bytes, time.monotonic() - started, self.warnings)


def load_table(
    location: str | Sequence[str],
    fmt: TableFormat | str | None = None,
    options: LoadOptions | None = None,
    *,
    progress: Callable[[float, str], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> LoadedTable:
    """Load a CSV or Parquet file (local path, ``gs://`` or ``http(s)://``),
    or several files with the same columns, into an Orange table."""
    options = options or LoadOptions()
    locations = [location] if isinstance(location, str) else list(location)
    files, formats = [], set()
    for loc in locations:
        detected, detected_gzip = detect_format(loc, TableFormat(fmt) if fmt else None)
        formats.add(detected)
        files.append(_File(loc, detected_gzip if options.gzip is None else options.gzip))
    if len(formats) > 1:
        raise OperationError(
            ErrorCode.BAD_REQUEST, "The files must all be CSV files or all Parquet files."
        )
    loader = _Loader(
        files,
        formats.pop(),
        options,
        progress or (lambda *_: None),
        check_cancelled or (lambda: None),
    )
    return loader.run()


def default_name(location: str) -> str:
    """The table's name from its file's name, without the extensions."""
    return without_extensions(file_name(location)) or "table"
