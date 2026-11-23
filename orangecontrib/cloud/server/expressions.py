"""Feature Constructor's expressions and Create Class's rules on the machine,
without Qt.

Feature Constructor keeps each new column's expression in a ``FeatureFunc``
(with ``MappingTransformCast`` or ``DateTimeCast`` to convert the results),
and Create Class its substring rules in ``ValueFromStringSubstring`` or
``ValueFromDiscreteSubstring``. These classes live in Orange's widget modules
``Orange.widgets.data.owfeatureconstructor`` and
``Orange.widgets.data.owcreateclass``, which import Qt, and the machine has
no Qt. So :mod:`orangecontrib.cloud.protocol` pickles them by name, and on
the machine these stand-ins take their place: they compute the same values
as Orange's own classes (adapted from Orange 3.41, GPL-3.0-or-later; Create
Class's are the same in 3.39), and they pickle as Orange's classes again, so
the add-on gets Orange's own objects back.
"""

from __future__ import annotations

import ast
import builtins
import math
import random
import re
import types
from collections.abc import Callable, Mapping
from itertools import starmap
from typing import Any

import numpy as np
from Orange.data import Table, Value, Variable
from Orange.data import TimeVariable as _TimeVariable
from Orange.preprocess.transformation import Lookup, MappingTransform, Transformation
from Orange.util import frompyfunc

from orangecontrib.cloud.protocol.pickles import SUBSTITUTES

__all__ = [
    "DateTimeCast",
    "FeatureFunc",
    "MappingTransformCast",
    "ValueFromDiscreteSubstring",
    "ValueFromStringSubstring",
    "map_by_substring",
    "register",
]

_MODULE = "Orange.widgets.data.owfeatureconstructor"
_CREATE_CLASS = "Orange.widgets.data.owcreateclass"

_ALLOWED = [
    "Ellipsis", "False", "None", "True", "abs", "all", "any", "acsii",
    "bin", "bool", "bytearray", "bytes", "chr", "complex", "dict",
    "divmod", "enumerate", "filter", "float", "format", "frozenset",
    "getattr", "hasattr", "hash", "hex", "id", "int", "iter", "len",
    "list", "map", "max", "memoryview", "min", "next", "object",
    "oct", "ord", "pow", "range", "repr", "reversed", "round",
    "set", "slice", "sorted", "str", "sum", "tuple", "type",
    "zip",
]  # fmt: skip

#: The names an expression can use, as in Feature Constructor.
_GLOBALS: dict[str, Any] = {
    name: getattr(builtins, name) for name in _ALLOWED if hasattr(builtins, name)
}
_GLOBALS.update({name: getattr(math, name) for name in dir(math) if not name.startswith("_")})
_GLOBALS.update({
    "normalvariate": random.normalvariate,
    "gauss": random.gauss,
    "expovariate": random.expovariate,
    "gammavariate": random.gammavariate,
    "betavariate": random.betavariate,
    "lognormvariate": random.lognormvariate,
    "paretovariate": random.paretovariate,
    "vonmisesvariate": random.vonmisesvariate,
    "weibullvariate": random.weibullvariate,
    "triangular": random.triangular,
    "uniform": random.uniform,
    "nanmean": lambda *args: np.nanmean(args),
    "nanmin": lambda *args: np.nanmin(args),
    "nanmax": lambda *args: np.nanmax(args),
    "nansum": lambda *args: np.nansum(args),
    "nanstd": lambda *args: np.nanstd(args),
    "nanmedian": lambda *args: np.nanmedian(args),
    "nancumsum": lambda *args: np.nancumsum(args),
    "nancumprod": lambda *args: np.nancumprod(args),
    "nanargmax": lambda *args: np.nanargmax(args),
    "nanargmin": lambda *args: np.nanargmin(args),
    "nanvar": lambda *args: np.nanvar(args),
    "mean": lambda *args: np.mean(args),
    "std": lambda *args: np.std(args),
    "median": lambda *args: np.median(args),
    "cumsum": lambda *args: np.cumsum(args),
    "cumprod": lambda *args: np.cumprod(args),
    "argmax": lambda *args: np.argmax(args),
    "argmin": lambda *args: np.argmin(args),
    "var": lambda *args: np.var(args),
})  # fmt: skip


def _arguments(names) -> ast.arguments:
    return ast.arguments(
        posonlyargs=[],
        args=[ast.arg(arg=name, annotation=None) for name in names],
        vararg=None,
        kwonlyargs=[],
        kwarg=None,
        defaults=[],
        kw_defaults=[],
    )


def make_lambda(expression: ast.Expression, args: list[str], env: dict[str, Any]) -> Callable:
    """A function of ``args`` that evaluates ``expression``, with ``env``'s
    names and :data:`_GLOBALS` available, as Feature Constructor makes it."""
    inner = ast.copy_location(
        ast.Lambda(args=_arguments(args), body=expression.body), expression.body
    )
    outer = ast.Expression(body=ast.Lambda(args=_arguments(env), body=inner))
    ast.fix_missing_locations(outer)
    namespace = dict(_GLOBALS, __builtins__={})
    make = eval(compile(outer, "<lambda>", "eval"), namespace)  # noqa: S307 - as in Orange
    func = make(**env)
    if not isinstance(func, types.FunctionType):
        raise TypeError("The expression didn't compile to a function.")
    return func


def _masked(func: Callable) -> Callable:
    """``func`` that gives NaN where it raises, like Orange's ``ftry`` (which
    older Orange versions keep in a widget module)."""

    def masked(*args: Any) -> Any:
        try:
            return func(*args)
        except Exception:  # noqa: BLE001 - Feature Constructor's rule: an error is a missing value
            return np.nan

    return masked


class FeatureFunc:
    """Stand-in for Feature Constructor's ``FeatureFunc``: computes one
    column from an expression over other columns."""

    orange_class = f"{_MODULE}.FeatureFunc"
    dtype: Any = None

    def __init__(  # noqa: PLR0913 - the same arguments as Orange's FeatureFunc
        self,
        expression: str,
        args: list[tuple[str, Variable]],
        extra_env: Mapping[str, Any] | None = None,
        cast: Callable | None = None,
        use_values: bool = False,
        dtype: Any = None,
    ):
        self.expression = expression
        self.args = args
        self.extra_env = dict(extra_env or {})
        self.func = make_lambda(
            ast.parse(expression, mode="eval"), [name for name, _ in args], self.extra_env
        )
        self.cast = cast
        self.mask_exceptions = True
        self.use_values = use_values
        self.dtype = dtype

    def __call__(self, table: Any, *_) -> Any:
        """The column's values for a table, or the value for one row."""
        if isinstance(table, Table):
            return self._call_table(table)
        row = table
        one = Table.from_numpy(
            row.domain, np.array([row.x]), np.array([row.y]), np.array([row.metas])
        )
        return self._call_table(one)[0]

    def _call_table(self, table: Table) -> np.ndarray:
        try:
            cols = [self.extract_column(table, var) for _, var in self.args]
        except ValueError:
            if self.mask_exceptions:
                return np.full(len(table), np.nan)
            raise
        rows = zip(*cols, strict=True) if cols else [()] * len(table)
        func = _masked(self.func) if self.mask_exceptions else self.func
        values = list(starmap(func, rows))
        if self.cast is not None:
            values = self.cast(values)
        return np.asarray(values, dtype=self.dtype)

    def extract_column(self, table: Table, var: Variable) -> Any:
        """The values of ``var`` as the expression sees them."""
        data = table.get_column(var)
        if var.is_string:
            return data
        if var.is_discrete and not self.use_values:
            values = np.array([*var.values, None], dtype=object)
            idx = data.astype(int)
            idx[~np.isfinite(data)] = len(values) - 1
            return values[idx].tolist()
        if var.is_time or self.use_values:
            return Value._as_values(var, data.tolist())
        return data.tolist()

    def __reduce__(self):
        return type(self), (
            self.expression,
            self.args,
            self.extra_env,
            self.cast,
            self.use_values,
            self.dtype,
        )

    def __hash__(self) -> int:
        return hash(
            (self.expression, tuple(self.args), tuple(sorted(self.extra_env.items())), self.cast)
        )

    def __eq__(self, other: object) -> bool:
        return (
            _same_class(self, other)
            and self.expression == other.expression
            and self.args == other.args
            and self.extra_env == other.extra_env
            and self.cast == other.cast
        )


class MappingTransformCast:
    """Stand-in for Feature Constructor's ``MappingTransformCast``: turns
    the expression's results into the codes of a categorical column."""

    orange_class = f"{_MODULE}.MappingTransformCast"

    def __init__(self, mapping: Mapping):
        self.t = MappingTransform(None, mapping)

    def __reduce_ex__(self, protocol):
        return type(self), (self.t.mapping,)

    def __call__(self, values: Any) -> Any:
        """The codes of ``values``; values not in the mapping are missing."""
        return self.t.transform(values)

    def __eq__(self, other: object) -> bool:
        return _same_class(self, other) and self.t == other.t

    def __hash__(self) -> int:
        return hash(self.t)


_parse_datetime = _TimeVariable("_").parse


def _cast_datetime(value: Any) -> float:
    if isinstance(value, (int, float)):
        return value
    if value == "" or value is None:
        return np.nan
    return _parse_datetime(value)


_cast_datetimes = frompyfunc(_cast_datetime, 1, 1, dtype=float)


class DateTimeCast:
    """Stand-in for Feature Constructor's ``DateTimeCast``: parses dates."""

    orange_class = f"{_MODULE}.DateTimeCast"

    def __call__(self, values: Any) -> Any:
        """The values as seconds since 1970."""
        return _cast_datetimes(values)

    def __eq__(self, other: object) -> bool:
        return _same_class(self, other)

    def __hash__(self) -> int:
        return hash(_cast_datetime)


# --------------------------------------------------------------------------
# Create Class


def _matches(strings: np.ndarray, pattern: str, regular_expression: bool, case: bool) -> Any:
    if regular_expression:
        compiled = re.compile(pattern, 0 if case else re.IGNORECASE)
        return np.array([bool(compiled.search(s)) for s in strings], dtype=bool)
    return np.char.find(strings, pattern)


def map_by_substring(
    a: np.ndarray,
    patterns: list[str],
    case_sensitive: bool,
    match_beginning: bool,
    regular_expressions: bool,
    map_values: Any = None,
) -> np.ndarray:
    """The index of the first pattern that each string matches (``map_values``
    of it, if given), or NaN, as Create Class's ``map_by_substring``."""
    map_values = np.arange(len(patterns)) if map_values is None else np.array(map_values, int)
    result = np.full(len(a), np.nan)
    if not case_sensitive and not regular_expressions:
        a = np.char.lower(a)
        patterns = [pattern.lower() for pattern in patterns]
    for index, pattern in reversed(list(enumerate(patterns))):
        found = _matches(a, pattern, regular_expressions, case_sensitive)
        if not regular_expressions:
            found = found == 0 if match_beginning else found != -1
        result[found] = map_values[index]
    return result


class _SubstringRules:
    """Equality and hashing of Create Class's rules, as Orange's ``_EqHashMixin``."""

    variable: Any
    patterns: Any
    case_sensitive: bool
    match_beginning: bool
    regular_expressions: bool
    map_values: Any

    def _key(self) -> tuple:
        values = None if self.map_values is None else tuple(self.map_values)
        flags = (self.case_sensitive, self.match_beginning, self.regular_expressions)
        return (self.variable, tuple(self.patterns), *flags, values)

    def __eq__(self, other: object) -> bool:
        return _same_class(self, other) and self._key() == other._key()

    def __hash__(self) -> int:
        return hash((type(self).orange_class, *self._key()))


class ValueFromStringSubstring(_SubstringRules, Transformation):
    """Stand-in for Create Class's ``ValueFromStringSubstring``: the class of
    each row from substrings of a text column."""

    orange_class = f"{_CREATE_CLASS}.ValueFromStringSubstring"
    InheritEq = True  # Orange's check for compute values: _SubstringRules has them

    def __init__(
        self,
        variable: Variable,
        patterns: list[str],
        case_sensitive: bool = False,
        match_beginning: bool = False,
        map_values: Any = None,
        regular_expressions: bool = False,
    ):
        super().__init__(variable)
        self.patterns = patterns
        self.case_sensitive = case_sensitive
        self.match_beginning = match_beginning
        self.regular_expressions = regular_expressions
        self.map_values = map_values

    def transform(self, c: np.ndarray) -> np.ndarray:
        """The class codes of the strings ``c``; missing stays missing."""
        nans = np.equal(c, None)
        c = c.astype(str)
        c[nans] = ""
        flags = (self.case_sensitive, self.match_beginning, self.regular_expressions)
        result = map_by_substring(c, self.patterns, *flags, self.map_values)
        result[nans] = np.nan
        return result


class ValueFromDiscreteSubstring(_SubstringRules, Lookup):
    """Stand-in for Create Class's ``ValueFromDiscreteSubstring``: the class
    of each category of a categorical column, from substrings of its name."""

    orange_class = f"{_CREATE_CLASS}.ValueFromDiscreteSubstring"
    InheritEq = True  # Orange's check for compute values: _SubstringRules has them

    def __init__(
        self,
        variable: Variable,
        patterns: list[str],
        case_sensitive: bool = False,
        match_beginning: bool = False,
        map_values: Any = None,
        regular_expressions: bool = False,
    ):
        super().__init__(variable, [])
        self.patterns = patterns
        self.case_sensitive = case_sensitive
        self.match_beginning = match_beginning
        self.regular_expressions = regular_expressions
        self.map_values = map_values
        flags = (case_sensitive, match_beginning, regular_expressions)
        self.lookup_table = map_by_substring(variable.values, patterns, *flags, map_values)


# --------------------------------------------------------------------------


def _same_class(a: Any, b: Any) -> bool:
    """Whether ``b`` is ``a``'s class or the Orange class it stands for."""
    name = getattr(type(a), "orange_class", None)
    other = getattr(type(b), "orange_class", None) or f"{type(b).__module__}.{type(b).__qualname__}"
    return name == other


def register() -> None:
    """Make :mod:`orangecontrib.cloud.protocol` use these stand-ins where
    Orange's widget module can't be imported."""
    stand_ins = (
        FeatureFunc,
        MappingTransformCast,
        DateTimeCast,
        ValueFromStringSubstring,
        ValueFromDiscreteSubstring,
    )
    for cls in stand_ins:
        SUBSTITUTES[cls.orange_class] = cls


register()
