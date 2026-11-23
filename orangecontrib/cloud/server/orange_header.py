"""Orange's three header rows (names, types, flags) at the top of a CSV or .tab file.

Orange's File widget reads a file as having this header when the second row
holds only type names (``continuous``/``c``/``numeric``/``n``,
``discrete``/``d``/``categorical`` or a list of values, ``string``/``s``/
``text``, ``time``/``t``, or nothing) and the third row only flags
(``class``/``c``, ``meta``/``m``, ``ignore``/``i``, ``weight``/``w``,
``key=value`` attributes, or nothing). The test is Orange's own
(``DataTableMixin.parse_headers``), and the types and flags mean what they
mean in Orange:

* an empty type is inferred from the values, as for files without the header;
* text columns are metas, even when flagged as the class;
* ignored columns are skipped; weight columns too (cloud tables have no
  weights), with a warning;
* ``key=value`` flags become the variable's ``attributes``.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Any

from orangecontrib.cloud.protocol.tables import ColumnRole, ColumnType

__all__ = ["HeaderColumn", "OrangeHeader", "read_header"]

#: Number of rows of Orange's header.
HEADER_ROWS = 3

#: Strings Orange reads as missing values besides the loader's own.
ORANGE_NULL_VALUES = [".", "~"]

#: A type that lists the values: words separated by single spaces (as Orange).
_VALUE_LIST = re.compile(r"^\s*[^\s]+(\s[^\s]+)+\s*$")


@dataclass
class HeaderColumn:
    """The role, type, values and attributes of one column from the header."""

    role: ColumnRole | None = None  #: ``None``: a feature
    type: ColumnType | None = None  #: ``None``: inferred from the values
    values: list[str] | None = None  #: the listed values of a categorical column
    attributes: dict[str, Any] = field(default_factory=dict)
    weight: bool = False  #: a weight column (skipped)


@dataclass
class OrangeHeader:
    """Column names and what the type and flag rows say about each column."""

    names: list[str]
    columns: list[HeaderColumn]


def read_header(head: str, delimiter: str) -> OrangeHeader | None:
    """Orange's three-row header from the start of a file's text, or ``None``
    if the file doesn't start with one."""
    from Orange.data.io_base import DataTableMixin

    reader = csv.reader(io.StringIO(head, newline=""), delimiter=delimiter, skipinitialspace=True)
    rows = []
    for row in reader:
        rows.append(row)
        if len(rows) == HEADER_ROWS + 1:  # the last one may be cut short
            break
    if len(rows) < HEADER_ROWS:
        return None
    header, _ = DataTableMixin.parse_headers(rows[:HEADER_ROWS])
    if len(header) != HEADER_ROWS:
        return None
    names, types, flags = header
    n = len(names)
    types = (types + [""] * n)[:n]
    flags = (flags + [""] * n)[:n]
    return OrangeHeader(names, [_column(t, f) for t, f in zip(types, flags, strict=True)])


def _column(type_: str, flags: str) -> HeaderColumn:
    """One column's role, type and values from its type and flags as Orange
    reads them."""
    from Orange.data.io_base import Flags

    col = _typed(type_.strip())
    flag = Flags(Flags.split(flags))
    col.attributes = dict(flag.attributes)
    if flag.i:
        col.role = ColumnRole.SKIP
    elif flag.m or col.type == ColumnType.TEXT:
        col.role = ColumnRole.META
    elif flag.w:
        col.role, col.weight = ColumnRole.SKIP, True
    elif flag.c:
        col.role = ColumnRole.TARGET
    return col


def _typed(type_: str) -> HeaderColumn:
    from Orange.data import ContinuousVariable, DiscreteVariable, StringVariable, TimeVariable
    from Orange.data.io_base import Flags

    if type_ in StringVariable.TYPE_HEADERS:
        return HeaderColumn(type=ColumnType.TEXT)
    if type_ in ContinuousVariable.TYPE_HEADERS:
        return HeaderColumn(type=ColumnType.NUMERIC)
    if type_ in TimeVariable.TYPE_HEADERS:
        return HeaderColumn(type=ColumnType.DATETIME)
    if _VALUE_LIST.match(type_):
        return HeaderColumn(type=ColumnType.CATEGORICAL, values=Flags.split(type_))
    if type_ in DiscreteVariable.TYPE_HEADERS:
        return HeaderColumn(type=ColumnType.CATEGORICAL)
    return HeaderColumn()
