"""Columns of a file being loaded: how Arrow values become Orange values."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from orangecontrib.cloud.protocol.tables import ColumnRole, ColumnType

__all__ = ["DISCRETE_MAX_VALUES", "NUMERIC", "TEXT", "TIME", "Column", "arrow_kind", "text_values"]

#: Most distinct values of an inferred categorical column (as in Orange).
DISCRETE_MAX_VALUES = 100

NUMERIC, TEXT, TIME = "numeric", "text", "time"


def arrow_kind(t: pa.DataType) -> str | None:
    """How a column of Arrow type ``t`` is read; ``None``: unsupported."""
    if pa.types.is_dictionary(t):
        t = t.value_type
    if pa.types.is_integer(t) or pa.types.is_floating(t) or pa.types.is_decimal(t):
        return NUMERIC
    if (
        pa.types.is_string(t)
        or pa.types.is_large_string(t)
        or pa.types.is_boolean(t)
        or pa.types.is_null(t)
        or getattr(pa.types, "is_string_view", lambda _: False)(t)
    ):
        return TEXT
    if pa.types.is_timestamp(t) or pa.types.is_date(t) or pa.types.is_time(t):
        return TIME
    return None


_TIME_UNITS = {"s": 1.0, "ms": 1e3, "us": 1e6, "ns": 1e9}


@dataclass
class Column:
    """One column of the file and what is known about it while reading."""

    index: int  #: position in the file
    name: str
    role: ColumnRole  #: requested role (feature unless overridden)
    type: ColumnType | None  #: requested type, if any
    kind: str = TEXT  #: how values are read: numeric, text or time
    #: Text: distinct values in order of appearance, their codes, and whether
    #: the column has turned into a string column (too many values).
    values: list[str] = field(default_factory=list)
    codes: dict[str, int] = field(default_factory=dict)
    is_string: bool = False
    #: Numbers: the distinct values while they are all in {0, 1, 2}.
    small_ints: set[int] | None = field(default_factory=set)
    have_date: bool = False
    have_time: bool = False
    #: The values listed in the file's header (Orange's ``a b c`` type), in
    #: their order; other values are missing.
    fixed_values: list[str] | None = None
    #: Attributes of the variable (Orange's ``key=value`` flags).
    attributes: dict[str, Any] = field(default_factory=dict)
    #: The role comes from the file's header, so Orange's rules apply: a
    #: class column of text is categorical only if it has few values.
    file_role: bool = False

    def reset(self, kind: str) -> None:
        """Start reading the column's values again, as ``kind``."""
        self.kind = kind
        fixed = self.fixed_values or []
        self.values, self.codes = list(fixed), {v: k for k, v in enumerate(fixed)}
        self.small_ints = set()
        self.have_date = self.have_time = False
        # Text that the user wants as text is never categorical.
        self.is_string = kind == TEXT and self.type == ColumnType.TEXT

    @property
    def forced_categorical(self) -> bool:
        """Text values are categorical however many there are."""
        target = self.role == ColumnRole.TARGET and not self.file_role
        return self.type == ColumnType.CATEGORICAL or target

    @property
    def max_values(self) -> float:
        """Most distinct values of a categorical column."""
        return math.inf if self.forced_categorical else DISCRETE_MAX_VALUES

    @property
    def float_valued(self) -> bool:
        """Values of a batch are float64 (else Python objects)."""
        return not (self.kind == TEXT and self.is_string)

    # Converting one batch of Arrow values

    def convert(self, arr: pa.Array, out: np.ndarray | None) -> np.ndarray | None:
        """Convert ``arr`` into ``out`` (float columns) or a new object array.

        Returns ``None`` if a categorical column just got too many values;
        it is a string column from then on.
        """
        if self.kind == NUMERIC:
            self._numbers(arr, out)
        elif self.kind == TIME:
            self._times(arr, out)
        elif self.is_string:
            return text_values(arr)
        elif not self._codes(arr, out):
            self.is_string = True
            return None
        return out

    def _numbers(self, arr: pa.Array, out: np.ndarray) -> None:
        if pa.types.is_dictionary(arr.type):
            arr = arr.dictionary_decode()
        if arrow_kind(arr.type) == TEXT and not pa.types.is_boolean(arr.type):
            # text the user wants as numbers
            import pandas as pd

            values = pd.to_numeric(pd.Series(text_values(arr)), errors="coerce")
            out[:] = values.to_numpy(dtype=np.float64, na_value=np.nan)
        else:
            out[:] = pc.fill_null(arr.cast(pa.float64()), np.nan).to_numpy()
        if self.small_ints is not None and self.type is None:
            valid = out[~np.isnan(out)]
            if valid.size and (valid.min() < 0 or valid.max() > 2):
                self.small_ints = None
                return
            uniques = np.unique(valid)
            if not np.isin(uniques, (0.0, 1.0, 2.0)).all():
                self.small_ints = None
            else:
                self.small_ints.update(int(u) for u in uniques)

    def _times(self, arr: pa.Array, out: np.ndarray) -> None:
        if pa.types.is_dictionary(arr.type):
            arr = arr.dictionary_decode()
        t = arr.type
        if pa.types.is_timestamp(t) or pa.types.is_date64(t):
            unit = "ms" if pa.types.is_date64(t) else t.unit
            # Nanoseconds since 1970 are beyond float64's exact integers;
            # the rounding is far below a microsecond.
            raw = arr.cast(pa.int64()).cast(pa.float64(), safe=False)
            np.divide(pc.fill_null(raw, np.nan).to_numpy(), _TIME_UNITS[unit], out=out)
            self.have_date = True
        elif pa.types.is_date32(t):
            raw = pc.fill_null(arr.cast(pa.int32()).cast(pa.float64()), np.nan).to_numpy()
            np.multiply(raw, 86400.0, out=out)
            self.have_date = True
        elif pa.types.is_time(t):
            ints = arr.cast(pa.int64() if t.bit_width == 64 else pa.int32())
            raw = pc.fill_null(ints.cast(pa.float64(), safe=False), np.nan).to_numpy()
            np.divide(raw, _TIME_UNITS[t.unit], out=out)
            self.have_time = True
            return
        else:  # text the user wants as dates
            import pandas as pd

            stamps = pd.to_datetime(
                pd.Series(text_values(arr)).replace("", None), errors="coerce", utc=True,
                format="mixed",
            )  # fmt: skip
            epoch = pd.Timestamp(0, tz="UTC")
            out[:] = (stamps - epoch).dt.total_seconds().to_numpy(dtype=np.float64)
            self.have_date = True
        if not self.have_time:
            self.have_time = bool(np.any(np.fmod(out[~np.isnan(out)], 86400.0) != 0))

    def _codes(self, arr: pa.Array, out: np.ndarray) -> bool:
        if not pa.types.is_dictionary(arr.type):
            if not (pa.types.is_string(arr.type) or pa.types.is_large_string(arr.type)):
                arr = arr.cast(pa.string())
            arr = arr.dictionary_encode()
        dictionary = arr.dictionary
        if not pa.types.is_string(dictionary.type):
            dictionary = dictionary.cast(pa.string())
        dictionary = pc.utf8_trim_whitespace(dictionary)  # as Orange's reader
        lookup = np.empty(len(dictionary) + 1)
        lookup[-1] = np.nan  # index -1: missing
        for k, value in enumerate(dictionary.to_pylist()):
            code = self.codes.get(value)
            if code is None:
                if value is None or value == "" or self.fixed_values is not None:
                    lookup[k] = np.nan
                    continue
                if len(self.values) >= self.max_values:
                    return False
                code = self.codes[value] = len(self.values)
                self.values.append(value)
            lookup[k] = code
        indices = pc.fill_null(arr.indices, -1).to_numpy()
        np.take(lookup, indices, out=out)
        return True

    def codes_to_strings(self, codes: np.ndarray) -> np.ndarray:
        """Category codes (NaN: missing) to text values (``""``: missing)."""
        values = np.array([*self.values, ""], dtype=object)
        index = np.where(np.isnan(codes), -1, codes).astype(np.intp)
        return values[index]


def text_values(arr: pa.Array) -> np.ndarray:
    """Text values as an object array, with ``""`` for missing values."""
    if not (pa.types.is_string(arr.type) or pa.types.is_large_string(arr.type)):
        arr = arr.cast(pa.string())
    # Spaces around values are dropped, as by Orange's reader.
    return pc.fill_null(pc.utf8_trim_whitespace(arr), "").to_numpy(zero_copy_only=False)
