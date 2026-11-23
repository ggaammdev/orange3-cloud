"""Recipes that the add-on sends for the machine to fit on all rows.

A recipe is an Orange ``Preprocess``. A Cloud widget records it on a cloud
table as the step ``("preprocess", recipe)``, and the machine fits it on all
rows of the table with Orange's own code (the ``derive`` job). Cloud
Preprocess sends recipes from Orange's Preprocess widget, and Cloud Purge
Domain sends Orange's own ``Remove``. Orange has no recipe for the settings
of its Discretize widget (a method for each column), of Edit Domain's "as
categorical" and "merge" (which need the values and counts of all rows) or
of Create Class, so Cloud Discretize, Cloud Categories and Cloud Create
Class send the recipes defined here, because both sides use them:

* :class:`DiscretizeColumns` does what Orange's Discretize widget does with
  its settings, with Orange's own discretizers
  (``Orange.preprocess.discretize``);
* :class:`ColumnCategories` makes a numeric or text column categorical with
  the values found on all rows, and merges values by their counts on all
  rows, as Edit Domain does on the whole table;
* :class:`CreateClass` makes Create Class's class column, keeping its rules
  as Create Class does when it sees all rows.

Orange's widget modules need Qt, which the machine doesn't have, so the few
functions with which the widgets do this are repeated here, with their
numbers and messages (adapted from Orange 3.41, GPL-3.0-or-later:
``Orange/widgets/data/owdiscretize.py``, ``oweditdomain.py`` and
``owcreateclass.py``). Create Class's own classes
(``ValueFromStringSubstring``, ``ValueFromDiscreteSubstring``) come from
Orange where its widget module can be imported, and from the machine's
stand-ins otherwise
(:func:`~orangecontrib.cloud.protocol.pickles.class_by_name`).

Unlike the rest of :mod:`orangecontrib.cloud.protocol`, this module needs
Orange (both sides have it), so the package doesn't import it. Like the
rest, it imports nothing from the add-on and no Qt.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping, Sequence
from enum import IntEnum
from itertools import count
from typing import Any, NamedTuple

import numpy as np
import Orange.preprocess.discretize as disc
from Orange.data import (
    ContinuousVariable,
    DiscreteVariable,
    Domain,
    Table,
    TimeVariable,
    Variable,
)
from Orange.preprocess.preprocess import Preprocess
from Orange.preprocess.transformation import Lookup, MappingTransform
from Orange.version import short_version

from orangecontrib.cloud.protocol.pickles import class_by_name

__all__ = [
    "MERGED_NAME",
    "ColumnCategories",
    "CreateClass",
    "DiscretizeColumns",
    "Hint",
    "Key",
    "Merge",
    "Method",
    "Rule",
    "ValueCounts",
    "as_categorical",
    "category_names",
    "class_labels",
    "discretize_variable",
    "infrequent",
    "is_discretizable",
    "known_outcome",
    "merge_categories",
    "value_counts",
    "variable_key",
]


class Method(IntEnum):
    """The methods of Orange's Discretize widget, with its numbers (``Methods``)."""

    DEFAULT = 0  #: the general preset (only for a column)
    KEEP = 1
    MDL = 2
    EQUAL_FREQ = 3
    EQUAL_WIDTH = 4
    REMOVE = 5
    CUSTOM = 6
    BINNING = 7
    FIXED_WIDTH = 8
    FIXED_WIDTH_TIME = 9


#: A method and its arguments as Orange's widget keeps them (``VarHint``):
#: e.g. ``(3, (4,))`` for equal frequency with 4 intervals, ``(6, ("1, 2",))``
#: for custom cut points, ``(9, ("2", 3))`` for a time interval of 2 weeks.
Hint = tuple[int, tuple]
#: A column as Orange's widget keys its settings: its name and whether it is
#: a time column.
Key = tuple[str, bool]
#: What a column becomes: a discretized column, the column itself (kept), or
#: ``None`` (removed).
Outcome = Variable | None

# Orange's Discretize widget's messages for columns it can't discretize.
INVALID_WIDTH = "invalid width"
TOO_MANY_INTERVALS = "too many intervals"
NO_DISCRETE_CLASS = "no discrete class"
INVALID_CUTS = "invalid cuts"
KEEP_TIME = ": <keep, time var>"
KEEP_NOT_TIME = ": <keep, not time>"

_CUT_SEPARATOR = re.compile(r"\s*,\s*")


def variable_key(var: Variable) -> Key:
    """The key of ``var`` in the widget's settings, as Orange's ``variable_key``."""
    return var.name, isinstance(var, TimeVariable)


def is_discretizable(var: Variable) -> bool:
    """Whether Orange's Discretize widget lists ``var`` (numeric and time columns)."""
    return isinstance(var, ContinuousVariable)


def _width(text: str) -> float | None:
    """A fixed width typed by the user, or ``None`` if it isn't a positive number."""
    try:
        width = float(text)
    except ValueError:
        return None
    return width if width > 0 else None


def _time_width(text: str) -> int | None:
    try:
        width = int(text)
    except ValueError:
        return None
    return width if width > 0 else None


def _cuts(text: str) -> list[float] | None:
    """Custom cut points typed by the user, or ``None`` unless they increase."""
    try:
        cuts = [float(x) for x in _CUT_SEPARATOR.split(text.strip())]
    except ValueError:
        return None
    if not cuts or any(x >= y for x, y in zip(cuts, cuts[1:], strict=False)):
        return None
    return cuts


# The discretization of each method, as Orange's widget calls it: a
# discretized variable, ``var`` itself, ``None``, or a message. They are only
# called when :func:`known_outcome` can't tell the outcome without the data.


def _fixed_width(data: Table, var: Variable, text: str) -> Outcome | str:
    digits = len(text) - text.index(".") - 1 if "." in text else 0
    try:
        return disc.FixedWidth(_width(text), digits)(data, var)
    except disc.TooManyIntervals:
        return TOO_MANY_INTERVALS


def _fixed_time_width(data: Table, var: Variable, text: str, unit: int) -> Outcome | str:
    width = _time_width(text)
    if unit == 3:  # weeks: days in Orange's discretizer
        width *= 7
    unit -= unit >= 3
    try:
        return disc.FixedTimeWidth(width, unit)(data, var)
    except disc.TooManyIntervals:
        return TOO_MANY_INTERVALS


def _custom(data: Table, var: Variable, text: str) -> Outcome:
    return disc.Discretizer.create_discretized_var(var, _cuts(text))


_DISCRETIZE: dict[int, Callable[..., Outcome | str]] = {
    Method.MDL: lambda data, var: disc.EntropyMDL()(data, var),
    Method.EQUAL_FREQ: lambda data, var, k: disc.EqualFreq(k)(data, var),
    Method.EQUAL_WIDTH: lambda data, var, k: disc.EqualWidth(k)(data, var),
    Method.BINNING: lambda data, var, n: disc.Binning(n)(data, var),
    Method.FIXED_WIDTH: _fixed_width,
    Method.FIXED_WIDTH_TIME: _fixed_time_width,
    Method.CUSTOM: _custom,
}


def _known_error(method: int, args: tuple, domain: Domain) -> str | None:
    """Orange's message for settings that can't work on any data."""
    if method == Method.MDL and not domain.has_discrete_class:
        return NO_DISCRETE_CLASS
    if method == Method.FIXED_WIDTH and _width(args[0]) is None:
        return INVALID_WIDTH
    if method == Method.FIXED_WIDTH_TIME and _time_width(args[0]) is None:
        return INVALID_WIDTH
    if method == Method.CUSTOM and _cuts(args[0]) is None:
        return INVALID_CUTS
    return None


def known_outcome(var: Variable, hint: Hint, domain: Domain) -> tuple[Outcome, str] | None:
    """What ``var`` becomes with ``hint`` when that doesn't depend on the
    data (kept, removed, or settings that can't work), and Orange's note for
    the list of columns; ``None`` when only the data can tell."""
    method, args = hint
    is_time = isinstance(var, TimeVariable)
    if is_time and method in (Method.FIXED_WIDTH, Method.CUSTOM):
        return var, KEEP_TIME
    if not is_time and method == Method.FIXED_WIDTH_TIME:
        return var, KEEP_NOT_TIME
    if method == Method.KEEP:
        return var, ""
    if method == Method.REMOVE:
        return None, ""
    error = _known_error(method, args, domain)
    if error is not None:
        return None, f" <{error}>"
    if method not in _DISCRETIZE:
        raise ValueError(f"Unknown discretization method {method!r}.")
    return None


def discretize_variable(data: Table, var: Variable, hint: Hint) -> tuple[Outcome, str]:
    """``var`` discretized on all rows of ``data`` as Orange's Discretize
    widget does it (its ``_discretize_var``), and its note: the cut points
    (as newer Orange shows them), or why the column was kept or removed."""
    known = known_outcome(var, hint, data.domain)
    if known is not None:
        return known
    method, args = hint
    dvar = _DISCRETIZE[method](data, var, *args)
    if isinstance(dvar, str):
        return None, f" <{dvar}>"
    thresholds = dvar.compute_value.points
    if len(thresholds) == 0:
        return None, " <removed>"
    return dvar, "(" + ", ".join(map(var.repr_val, thresholds)) + ")"


class DiscretizeColumns(Preprocess):
    """Orange's Discretize widget's settings as a recipe: every numeric or
    time column (features, target and metas) discretized on all rows with
    its own method, or with the general preset."""

    def __init__(self, default: Hint = (Method.KEEP, ()), hints: Mapping[Key, Hint] | None = None):
        self.default: Hint = (int(default[0]), tuple(default[1]))
        #: The columns' own methods, in sorted order, so equal settings pickle the same.
        self.hints: dict[Key, Hint] = dict(
            sorted(
                (tuple(key), (int(method), tuple(args)))
                for key, (method, args) in (hints or {}).items()
            )
        )

    def hint(self, var: Variable) -> Hint:
        """The method for ``var``: its own, or the general preset."""
        return self.hints.get(variable_key(var), self.default)

    def changes(self, domain: Domain) -> bool:
        """Whether the recipe changes any column of ``domain`` (False when
        every numeric column is kept)."""
        for var in domain.variables + domain.metas:
            if is_discretizable(var):
                known = known_outcome(var, self.hint(var), domain)
                if known is None or known[0] is not var:
                    return True
        return False

    def __call__(self, data: Table, progress: Callable[[int, int], Any] | None = None) -> Table:
        """``data`` with its columns discretized; ``progress(i, n)`` is called
        before each of the ``n`` numeric columns (e.g. to cancel)."""
        domain = data.domain
        columns = [var for var in domain.variables + domain.metas if is_discretizable(var)]
        outcomes: dict[Key, Outcome] = {}
        for i, var in enumerate(columns):
            if progress is not None:
                progress(i, len(columns))
            outcomes[variable_key(var)] = discretize_variable(data, var, self.hint(var))[0]

        def part(variables: tuple[Variable, ...]) -> list[Variable]:
            kept = (outcomes.get(variable_key(var), var) for var in variables)
            return [var for var in kept if var is not None]

        new = Domain(part(domain.attributes), part(domain.class_vars), part(domain.metas))
        return data.transform(new)


# --------------------------------------------------------------------------
# A column's values, counted on all rows (Cloud Categories, Cloud Create Class)


class ValueCounts(NamedTuple):
    """A column's values and how many rows have each, counted on all rows."""

    #: A categorical column's values; the distinct values of a numeric or
    #: text column, sorted, in the order Edit Domain's "as categorical" gives.
    values: Sequence[Any]
    counts: np.ndarray  #: the number of rows with each value
    #: Indices into ``values``, in the order the values first appear in the
    #: rows (Edit Domain counts with a ``Counter``, which keeps that order
    #: for equal counts). Values that no row has aren't in it.
    first_seen: np.ndarray
    missing: int  #: the number of rows where the column is missing

    @classmethod
    def from_stats(cls, column: Mapping[str, Any]) -> ValueCounts | None:
        """The counts in a column of the ``values`` statistics; ``None`` when
        the column has too many values to list."""
        if column.get("values") is None:
            return None
        return cls(*(column[name] for name in cls._fields))


def value_counts(table: Table, var: Variable) -> ValueCounts:
    """The values of ``var`` and their counts on all rows of ``table``."""
    column = table.get_column(var)
    if var.is_string:
        column = np.asarray(column, dtype=object)
        defined = column != var.Unknown
    else:
        column = np.asarray(column, dtype=float)
        defined = ~np.isnan(column)
    found = column[defined]
    if var.is_discrete:
        codes = found.astype(int)
        values: Sequence[Any] = tuple(var.values)
        counts = np.bincount(codes, minlength=len(values))
        seen, first = np.unique(codes, return_index=True)
    else:
        values, first, counts = np.unique(found, return_index=True, return_counts=True)
        seen = np.arange(len(values))
    order = np.argsort(first, kind="stable")
    return ValueCounts(values, counts, seen[order], int(len(column) - np.sum(defined)))


# --------------------------------------------------------------------------
# Cloud Categories: Edit Domain's "as categorical" and "merge" on all rows


class Merge(IntEnum):
    """How Edit Domain's "merge" dialog chooses the values to merge."""

    NONE = 0  #: merge nothing
    SELECTED = 1  #: "Group selected values"
    FEWER_THAN = 2  #: "Group values with less than N occurrences"
    PERCENT = 3  #: "Group values with less than p % occurrences"
    ALL_BUT = 4  #: "Group all except N most frequent values"


#: Edit Domain's name for the merged value.
MERGED_NAME = "other"

#: Orange 3.41 names the categories of a numeric column in the column's
#: format (``repr_val``: "2" for a column without decimals); older versions
#: with ``str`` ("2.0").
_FORMATTED_NUMBERS = tuple(int(x) for x in re.findall(r"\d+", short_version)[:2]) >= (3, 41)


def category_names(var: Variable, values: Sequence[Any]) -> tuple[str, ...]:
    """The categories that Edit Domain's "as categorical" gives the
    distinct ``values`` of the numeric or text column ``var``."""
    if var.is_continuous and _FORMATTED_NUMBERS:
        return tuple(var.repr_val(value) for value in values)
    return tuple(str(value) for value in values)


def as_categorical(var: Variable, values: Sequence[Any]) -> DiscreteVariable:
    """Edit Domain's "as categorical" of the numeric or text column ``var``,
    whose rows have the distinct ``values`` (sorted)."""
    if not (var.is_string or var.is_continuous) or var.is_time:
        raise ValueError(f"'{var.name}' isn't a numeric or text column.")
    names = category_names(var, values)
    if var.is_string:
        transform = MappingTransform(var, {name: float(i) for i, name in enumerate(names)})
    else:
        mapping = {value: i for i, value in enumerate(values)}
        transform = MappingTransform(var, mapping, dtype=np.float64, unknown=np.nan)
    new = DiscreteVariable(var.name, values=names, compute_value=transform)
    new.attributes = dict(var.attributes)
    new.sparse = var.sparse
    return new


def infrequent(counts: ValueCounts, merge: Merge, amount: float) -> list[int]:
    """Indices of the values that Edit Domain's "merge" dialog merges in the
    mode ``merge`` (other than SELECTED), with the counts of all rows: as its
    ``Counter``, only values that some row has, the most frequent first and
    those with equal counts in the order they first appear."""
    numbers = [int(n) for n in counts.counts]
    common = sorted(counts.first_seen.tolist(), key=numbers.__getitem__, reverse=True)
    if merge == Merge.ALL_BUT:
        return common[int(amount) :]
    if merge == Merge.FEWER_THAN:
        return [i for i in common if numbers[i] < amount]
    if merge == Merge.PERCENT:
        total = sum(numbers[i] for i in common)
        return [i for i in common if numbers[i] / total * 100 < amount]
    raise ValueError(f"Can't choose values to merge in mode {merge!r}.")


def merge_categories(var: DiscreteVariable, merged: Collection[str], name: str) -> Variable:
    """The categorical column ``var`` with its values ``merged`` renamed to
    ``name``, as Edit Domain maps categories; ``var`` itself if nothing changes."""
    mapping = [(value, name if value in merged else value) for value in var.values]
    if all(old == new for old, new in mapping):
        return var
    values = list(dict.fromkeys(new for _, new in mapping))
    codes = {value: i for i, value in enumerate(values)}
    lookup = np.array([codes[new] for _, new in mapping], dtype=float)
    new_var = DiscreteVariable(var.name, values=values, compute_value=Lookup(var, lookup))
    new_var.attributes.update(var.attributes)
    return new_var


def _replaced(domain: Domain, old: Variable, new: Variable) -> Domain:
    """``domain`` with ``new`` in the place of ``old``."""

    def part(variables: Sequence[Variable]) -> list[Variable]:
        return [new if var is old else var for var in variables]

    return Domain(part(domain.attributes), part(domain.class_vars), part(domain.metas))


class ColumnCategories(Preprocess):
    """Cloud Categories' settings as a recipe: a numeric or text column made
    categorical with the values that all rows have (Edit Domain's "as
    categorical"), and the categories merged by their counts on all rows
    (Edit Domain's "merge"), as Edit Domain does it on the whole table."""

    def __init__(
        self,
        column: str,
        merge: int = Merge.NONE,
        amount: float = 0,
        selected: Sequence[str] = (),
        name: str = MERGED_NAME,
    ):
        self.column = column
        self.merge = Merge(merge)
        #: N or p of the merge mode (``Merge``).
        self.amount = amount
        #: The values to merge in the mode SELECTED.
        self.selected = tuple(selected)
        #: The name of the merged value.
        self.name = name or MERGED_NAME

    def variable(self, var: Variable, counts: ValueCounts) -> Variable:
        """``var`` as the recipe changes it, from the ``counts`` of its values
        on all rows; ``var`` itself when nothing changes."""
        if not var.is_discrete:
            var = as_categorical(var, counts.values)
        if self.merge == Merge.NONE:
            return var
        if self.merge == Merge.SELECTED:
            merged = [value for value in var.values if value in self.selected]
        else:
            merged = [var.values[i] for i in infrequent(counts, self.merge, self.amount)]
        return merge_categories(var, merged, self.name)

    def __call__(self, data: Table) -> Table:
        """``data`` with the column changed as Edit Domain changes it."""
        var = data.domain[self.column]
        new = self.variable(var, value_counts(data, var))
        return data.transform(_replaced(data.domain, var, new))


# --------------------------------------------------------------------------
# Cloud Create Class: Create Class's rules, kept as it keeps them on all rows

_CREATE_CLASS = "Orange.widgets.data.owcreateclass"

#: A rule of Create Class: the class's name (empty: C1, C2, ...) and the
#: substring (empty: every row that no rule above matched).
Rule = tuple[str, str]


def class_labels(rules: Sequence[Rule]) -> list[str]:
    """Create Class's class names for ``rules``: the given names, and C1,
    C2, ... for empty ones, after the largest C<n> given."""
    numbers = [int(m.group(1)) for label, _ in rules if (m := re.match(r"C(\d+)", label))]
    following = count(max(numbers, default=0) + 1)
    return [label or f"C{next(following)}" for label, _ in rules]


def _unique_in_order(names: Sequence[str]) -> tuple[tuple[str, ...], tuple[int, ...]]:
    """The distinct names in the order they appear, and each name's index
    among them (Create Class's ``unique_in_order_mapping``)."""
    unique = list(dict.fromkeys(names))
    return tuple(str(name) for name in unique), tuple(unique.index(name) for name in names)


class CreateClass(Preprocess):
    """Create Class's settings as a recipe: a class column from substrings
    of a text or categorical column's values. Create Class keeps its rules
    until the rows it sees are all matched; this keeps them as Create Class
    does when it sees all rows."""

    def __init__(
        self,
        column: str,
        class_name: str,
        rules: Sequence[Rule],
        *,
        case_sensitive: bool = False,
        match_beginning: bool = False,
        regular_expressions: bool = False,
    ):
        self.column = column
        self.class_name = class_name
        self.rules: tuple[Rule, ...] = tuple((str(name), str(text)) for name, text in rules)
        self.case_sensitive = case_sensitive
        self.match_beginning = match_beginning
        self.regular_expressions = regular_expressions

    def _matches(self, strings: np.ndarray, pattern: str) -> np.ndarray:
        """Which ``strings`` match ``pattern``, as Create Class counts them."""
        if self.regular_expressions:
            compiled = re.compile(pattern, 0 if self.case_sensitive else re.IGNORECASE)
            return np.array([bool(compiled.search(s)) for s in strings], dtype=bool)
        if not self.case_sensitive:
            pattern = pattern.lower()
        found = np.char.find(strings, pattern.strip())
        return found == 0 if self.match_beginning else found != -1

    def match_counts(self, values: Sequence[Any], weights: Sequence[int]) -> list[tuple[int, int]]:
        """Create Class's counts for each rule, until no rows are left: the
        rows it matches that no rule above matched, and all rows it matches.
        ``values`` are the column's distinct texts or its categories, and
        ``weights`` their numbers of rows."""
        strings = np.array([str(value) for value in values], dtype=str)
        if not (self.case_sensitive or self.regular_expressions):
            strings = np.char.lower(strings)
        weights = np.asarray(weights, dtype=int)
        remaining = weights.copy()
        counts = []
        for _, pattern in self.rules:
            found = self._matches(strings, pattern)
            counts.append((int(remaining[found].sum()), int(weights[found].sum())))
            remaining[found] = 0
            if not remaining.any():
                break
        return counts

    def variable(self, var: Variable, counts: ValueCounts) -> DiscreteVariable:
        """The class column made from ``var``, from the ``counts`` of its
        values on all rows, as Create Class makes it."""
        if not (var.is_string or var.is_discrete):
            raise ValueError(f"'{var.name}' isn't a text or categorical column.")
        kept = len(self.match_counts(counts.values, counts.counts))
        patterns = tuple(pattern for _, pattern in self.rules[:kept])
        names, map_values = _unique_in_order(class_labels(self.rules)[:kept])
        kind = "ValueFromStringSubstring" if var.is_string else "ValueFromDiscreteSubstring"
        compute_value = class_by_name(f"{_CREATE_CLASS}.{kind}")(
            var,
            patterns,
            self.case_sensitive,
            self.match_beginning and not self.regular_expressions,
            map_values,
            self.regular_expressions,
        )
        return DiscreteVariable(self.class_name, names, compute_value=compute_value)

    def __call__(self, data: Table) -> Table:
        """``data`` with the new class column; the old class columns become metas."""
        domain = data.domain
        var = domain[self.column]
        new_class = self.variable(var, value_counts(data, var))
        new = Domain(domain.attributes, new_class, domain.metas + domain.class_vars)
        return data.transform(new)
