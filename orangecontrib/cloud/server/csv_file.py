"""Reading one CSV file of a load (:mod:`.loader`) in batches.

The delimiter (comma, tab, semicolon or ``|``) comes from the first line.
A file may start with Orange's three header rows (:mod:`.orange_header`), or
have no header row at all when its first line holds only numbers; then its
columns are named ``Feature 1``, ``Feature 2``, ... as in Orange. The types
are inferred from the first :data:`CSV_INFER_BYTES` of text; a column whose
values further down don't fit its type makes the loader read the files again
with that column as text (:class:`ReadAsText`).
"""

from __future__ import annotations

import csv
import re
from collections.abc import Callable
from typing import Any

import pyarrow as pa

from orangecontrib.cloud.server.columns import NUMERIC, TEXT, Column, arrow_kind
from orangecontrib.cloud.server.orange_header import (
    HEADER_ROWS,
    ORANGE_NULL_VALUES,
    OrangeHeader,
    read_header,
)
from orangecontrib.cloud.server.sources import base_name, open_stream, read_fully

__all__ = ["CSV_INFER_BYTES", "NULL_VALUES", "CsvFile", "ReadAsText"]

#: Bytes of CSV text whose values decide the inferred column types.
CSV_INFER_BYTES = 8 * 1024**2

#: Bytes read at a time to find the delimiter and Orange's header rows.
CSV_HEAD_BYTES = 64 * 1024

#: Bytes of CSV text parsed per batch: the file's size divided by
#: ``CSV_TARGET_BATCHES``, within these limits. The parser keeps a few dozen
#: blocks in memory at once, so small blocks keep loading memory low for
#: small tables, and the target keeps the number of batches (each held in its
#: own memory mapping) moderate for large files.
CSV_BLOCK_MIN = 1024**2
CSV_BLOCK_MAX = 64 * 1024**2
CSV_TARGET_BATCHES = 2000

#: Assumed compression ratio of ``.gz`` files.
GZIP_RATIO = 4

#: Strings read as missing values (pyarrow's defaults plus Orange's ``?``).
NULL_VALUES = [
    "", "?", "#N/A", "#N/A N/A", "#NA", "-1.#IND", "-1.#QNAN", "-NaN", "-nan",
    "1.#IND", "1.#QNAN", "N/A", "NA", "NULL", "NaN", "n/a", "nan", "null",
]  # fmt: skip

_CSV_COLUMN_ERROR = re.compile(r"In CSV column #(\d+): CSV conversion error")


class ReadAsText(Exception):  # noqa: N818 - a signal to the loader, not an error
    """A column has values that don't fit its type: the loader reads the
    files again with ``column`` (an index into its columns) as text."""

    def __init__(self, column: int):
        super().__init__(column)
        self.column = column


def _lines(head: bytes) -> int:
    """Lines started in ``head``, also with old Mac line ends (``\\r``)."""
    return max(head.count(b"\n"), head.count(b"\r"))


def _generated_names(line: str, delimiter: str) -> list[str] | None:
    """Orange's names for the columns of a file without a header row (its
    first line holds only numbers), else ``None``."""
    values = next(csv.reader([line], delimiter=delimiter), [])
    try:
        for value in values:
            float(value)
    except ValueError:
        return None
    return [f"Feature {i + 1}" for i in range(len(values))] or None


def _csv_block_size(size: int | None, gzip: bool) -> int:
    if not size:
        return 8 * 1024**2
    text_bytes = size * (GZIP_RATIO if gzip else 1)
    return int(min(max(text_bytes // CSV_TARGET_BATCHES, CSV_BLOCK_MIN), CSV_BLOCK_MAX))


def _read_type(col: Column, inferred: pa.DataType, as_text: set[int]) -> pa.DataType:
    """The Arrow type a column of a file is read as: its inferred type, or
    text when it is wanted as text, or as numbers or dates that the file's
    values aren't (converted after reading). Raises :class:`ReadAsText` when
    a later file's values don't fit the column's inferred type (e.g. text in
    a column of numbers)."""
    kind = arrow_kind(inferred)
    if col.kind == TEXT or col.index in as_text:
        return pa.string()
    if pa.types.is_null(inferred):  # no values at the start of this file
        return pa.float64() if col.kind == NUMERIC else pa.string()
    if kind != col.kind:
        if col.type is None:
            raise ReadAsText(col.index)
        return pa.string()  # converted to the requested type
    return pa.float64() if col.kind == NUMERIC else inferred


class CsvFile:
    """One CSV file: its delimiter, header rows, inferred schema and batches."""

    def __init__(self, location: str, read_from: str, gzip: bool):
        #: The file as given, and where it is read from (a copy on the disk).
        self.location = location
        self.read_from = read_from
        self.gzip = gzip
        self.delimiter = ","
        self.header: OrangeHeader | None = None  #: Orange's header rows, if any
        self.header_names: list[str] | None = None  #: generated names: no header row
        self.size: int | None = None

    # The start of the file

    def sniff(self) -> None:
        """The delimiter, from the first line; also reads Orange's header
        rows into ``self.header`` if the file starts with them."""
        text = self._head()
        line = text.splitlines()[0] if text else ""
        counts = {d: line.count(d) for d in (",", "\t", ";", "|")}
        best = max(counts, key=counts.get)
        self.delimiter = best if counts[best] else ","
        if counts["\t"] and base_name(self.location).endswith((".tab", ".tsv")):
            self.delimiter = "\t"  # as Orange's reader of these files
        self.header = read_header(text, self.delimiter)
        if self.header is None:
            self.header_names = _generated_names(line, self.delimiter)

    def _head(self) -> str:
        """The first lines of the file: enough for Orange's header rows,
        also when lines are long (wide files)."""
        stream, raw, _ = open_stream(self.read_from, self.gzip)
        try:
            head = stream.read(CSV_HEAD_BYTES)
            while len(head) < CSV_INFER_BYTES and _lines(head) <= HEADER_ROWS:
                more = stream.read(CSV_HEAD_BYTES)
                if not more:
                    break
                head += more
        finally:
            stream.close()
            raw.close()
        return head.decode("utf-8-sig", errors="replace")

    @property
    def null_values(self) -> list[str]:
        """The strings that are missing values in this file."""
        return NULL_VALUES + ORANGE_NULL_VALUES if self.header else NULL_VALUES

    def _header_options(self) -> dict[str, Any]:
        """``ReadOptions`` that skip Orange's header rows, if the file has
        them, or that name the columns of a file without a header row."""
        if self.header is not None:
            return {"column_names": self.header.names, "skip_rows": HEADER_ROWS}
        if self.header_names is not None:
            return {"column_names": self.header_names}
        return {}

    @property
    def header_rows(self) -> int:
        """Rows before the data."""
        if self.header is not None:
            return HEADER_ROWS
        return 0 if self.header_names is not None else 1

    def infer_schema(self) -> pa.Schema:
        """Column names and types inferred from the start of the file (call
        :meth:`sniff` first); also sets ``size``."""
        import pyarrow.csv as pv

        parse = pv.ParseOptions(delimiter=self.delimiter)
        convert = pv.ConvertOptions(null_values=self.null_values, strings_can_be_null=True)
        stream, raw, self.size = open_stream(self.read_from, self.gzip)
        try:
            head = read_fully(stream, CSV_INFER_BYTES)
            complete = len(head) < CSV_INFER_BYTES
        finally:
            stream.close()
            raw.close()
        if not complete:  # parse whole lines only
            head = head[: head.rfind(b"\n") + 1] or head
        read = pv.ReadOptions(block_size=len(head) + 1, use_threads=False, **self._header_options())
        try:
            schema = pv.read_csv(pa.py_buffer(head), read, parse, convert).schema
        except pa.ArrowInvalid:
            if complete:
                raise
            # e.g. the cut fell inside a quoted value: let the reader find
            # its own first block.
            stream, raw, _ = open_stream(self.read_from, self.gzip)
            try:
                read = pv.ReadOptions(
                    block_size=CSV_INFER_BYTES, use_threads=False, **self._header_options()
                )
                schema = pv.open_csv(stream, read, parse, convert).schema
            finally:
                stream.close()
                raw.close()
        del head
        pa.default_memory_pool().release_unused()
        return schema

    # Reading

    def read(  # noqa: PLR0913 - what the loader decided about each column
        self,
        schema: pa.Schema,
        names: list[str],
        columns: list[tuple[Column, int | None]],
        as_text: set[int],
        add_batch: Callable[[int, Callable[[Column], pa.Array]], None],
        report: Callable[[float], None],
        check_cancelled: Callable[[], None],
    ) -> None:
        """Read the file's rows in batches into ``add_batch``.

        ``names``: the file's column names, made unique as the loader made
        them. ``columns``: the wanted columns and their positions in this
        file (``None``: the file doesn't have it; its values are missing).
        ``as_text``: columns read as text. ``report(fraction)`` tells how
        much of the file has been read. Raises :class:`ReadAsText` when a
        column has values further down that don't fit its type.
        """
        import pyarrow.csv as pv

        present = [(col, pos) for col, pos in columns if pos is not None]
        types = {names[pos]: _read_type(col, schema.types[pos], as_text) for col, pos in present}
        read = pv.ReadOptions(
            block_size=_csv_block_size(self.size, self.gzip),
            column_names=names,
            skip_rows=self.header_rows,
        )
        parse = pv.ParseOptions(delimiter=self.delimiter)
        convert = pv.ConvertOptions(
            null_values=self.null_values,
            strings_can_be_null=True,
            column_types=types,
            include_columns=[names[pos] for _, pos in present],
        )
        try:
            self._read_batches(pv, (read, parse, convert), columns, add_batch, report,
                               check_cancelled)  # fmt: skip
        except pa.ArrowInvalid as exc:
            match = _CSV_COLUMN_ERROR.search(str(exc))
            by_position = {pos: col for col, pos in present}
            if match is None or int(match.group(1)) not in by_position:
                raise
            raise ReadAsText(by_position[int(match.group(1))].index) from exc

    def _read_batches(  # noqa: PLR0913 - the options and the loader's callbacks
        self,
        pv,
        options: tuple,
        columns: list[tuple[Column, int | None]],
        add_batch: Callable[[int, Callable[[Column], pa.Array]], None],
        report: Callable[[float], None],
        check_cancelled: Callable[[], None],
    ) -> None:
        stream, raw, size = open_stream(self.read_from, self.gzip)
        try:
            reader = pv.open_csv(stream, *options)
            # A batch holds the included columns in the order they were listed.
            included = [pos for _, pos in columns if pos is not None]
            in_batch = {pos: k for k, pos in enumerate(included)}
            positions = {col.index: pos for col, pos in columns}
            for batch in reader:
                check_cancelled()

                def column(col: Column, b=batch) -> pa.Array | None:
                    pos = positions[col.index]
                    return None if pos is None else b.column(in_batch[pos])

                add_batch(batch.num_rows, column)
                del batch
                if size:
                    try:
                        done = raw.tell() / size
                    except (OSError, ValueError, NotImplementedError):  # can't tell
                        done = 0.0
                    report(min(done, 1.0))
        finally:
            stream.close()
            raw.close()
