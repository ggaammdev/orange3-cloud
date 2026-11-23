"""Cloud tables as Orange ``Table`` objects that hold no data.

A :class:`RemoteTable` is what Orange's widgets receive for a table that lives
on the cloud machine. It has the cloud table's domain (its columns and their
types) and **one placeholder row in which every value is missing**, so nothing
on this computer can compute anything from the data. Orange's learners, Test
& Score and viewers see no values and compute nothing; the Cloud widgets
compute on all rows on the machine.

Orange's data-changing widgets (Select Columns, Select Rows, Feature
Constructor, Continuize, ...) still work on it. Their steps are recorded in
the table's *lineage* instead of being applied to the data:

* each change of domain (``Table.transform`` / ``from_table``) adds
  ``("transform", Domain)``,
* each row filter (the ``Storage._filter_*`` hooks that
  ``Orange.data.filter`` calls) adds ``("filter", Filter)``, deep-copied,
  because Select Rows changes its filter after using it. Filters aren't
  applied locally, so the placeholder row stays: Orange's widgets treat a
  table with 0 rows as "no data".

The machine replays the steps on all rows when a Cloud widget needs the data
(:meth:`RemoteTable.resolve`).

Steps that learn their settings from the data come from Cloud Preprocess
(:func:`preprocessed`): ``("preprocess", recipe)``, fitted on all rows on the
machine. Its output has the fitted table's domain, as the machine sent it
(``ref.fitted``). The steps after it go in a separate request that starts
from the fitted table and refers to that domain's columns instead of
sending copies, so the machine sees its own fitted columns.

Computing distributions or contingencies of the placeholder row (the
``Storage`` hooks that ``Orange.statistics`` calls) raises
:class:`PlaceholderStatisticsError`: from no data, Orange's Rank, Discretize
and Purge Domain would choose arbitrary columns. The hooks can't tell who
asks, so this also stops Orange's Preprocess with a counting step when a
cloud table is connected to it (it sends its recipe only after applying it
to its data; Cloud Preprocess needs the recipe built without data), and
Orange's Rank stays busy after the error (its ``on_exception`` re-raises,
and a ``ValueError`` would give empty scores instead of an error).

Anything that depends on which local rows were picked (a selection in a
table, Data Sampler, an annotated output with a "Selected" column filled in
afterwards) or on values written into the table here (Randomize: any
``Table.unlocked``) gives a plain local ``Table`` of the placeholder row,
which Cloud widgets reject as local data.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import numpy as np
from Orange.data import Domain, Table, Variable
from Orange.data import filter as data_filter

from orangecontrib.cloud.client.api import CloudError, CloudNotFoundError

if TYPE_CHECKING:
    from orangecontrib.cloud.client.api import CloudClient
    from orangecontrib.cloud.protocol.jobs import JobInfo, JobResult
    from orangecontrib.cloud.protocol.tables import TableInfo

    #: Waits for a job and returns its result, e.g. ``CloudClient.wait_for_job``.
    WaitForJob = Callable[[JobInfo], JobResult]

__all__ = [
    "CloudRef",
    "LocalDataError",
    "PlaceholderStatisticsError",
    "RemoteTable",
    "RemoteTableError",
    "TableUnavailableError",
    "is_cloud_table",
    "preprocessed",
    "resolve",
]

#: A column as Orange's ``Storage`` hooks take it: an index, a name or a variable.
ColumnRef = int | str | Variable

LOCAL_DATA = "This is local data, not a cloud table."
UNAVAILABLE = "This cloud table is no longer available. Run Cloud File again."
NO_STATISTICS = (
    "Orange's widgets can't compute statistics of a cloud table on this computer: "
    "the data is on the cloud machine, and here there is only an empty placeholder row. "
    "Use the Cloud widget instead: Cloud Rank for Rank, Cloud Discretize for Discretize, "
    "Cloud Purge Domain for Purge Domain, and Cloud Preprocess for steps that learn "
    "from the data. For Cloud Preprocess, build the recipe in Orange's Preprocess "
    "without data connected to it: with data, Preprocess doesn't send a changed recipe."
)


class RemoteTableError(CloudError):
    """A cloud table can't be used; the message says why and what to do."""

    def __init__(self, message: str):
        super().__init__(None, message)


class LocalDataError(RemoteTableError):
    """A Cloud widget got a local table instead of a cloud table."""

    def __init__(self, message: str = LOCAL_DATA):
        super().__init__(message)


class TableUnavailableError(RemoteTableError):
    """The machine that held the table is gone, or it no longer has the table."""

    def __init__(self, message: str = UNAVAILABLE):
        super().__init__(message)


class PlaceholderStatisticsError(RemoteTableError):
    """Orange tried to compute distributions or contingencies from a cloud
    table's placeholder row."""

    def __init__(self, message: str = NO_STATISTICS):
        super().__init__(message)


@dataclass(frozen=True)
class CloudRef:
    """Where a cloud table lives and which steps were recorded on it.

    It holds only plain values and the recorded steps, never a client or a
    secret, so a table can be copied and pickled safely.
    """

    table_id: str  #: the table on the machine that the steps start from
    name: str  #: the table's name, without the size
    n_rows: int | None  #: rows after the steps; ``None`` once a filter is recorded
    machine_id: str | None = None  #: the machine's ``MachineHandle.machine_id``
    #: Recorded steps, in order: ``("transform", Domain)``, ``("filter",
    #: Filter)`` or ``("preprocess", recipe)``.
    steps: tuple[tuple[str, Any], ...] = ()
    #: The domain the machine fitted for each ``"preprocess"`` step, in order.
    fitted: tuple[Domain, ...] = ()

    def add(self, kind: str, value: Any) -> CloudRef:
        """A reference with one more step. A filter or a recipe makes the row
        count unknown."""
        n_rows = self.n_rows if kind == "transform" else None
        return replace(self, steps=self.steps + ((kind, value),), n_rows=n_rows)

    def segments(self) -> list[tuple[tuple[tuple[str, Any], ...], list | None]]:
        """The steps as requests to the machine, with the columns each one's
        steps may refer to: every ``"preprocess"`` step ends a request, and
        the next one starts from its fitted table (see the module's
        documentation)."""
        segments: list[tuple[tuple[tuple[str, Any], ...], list | None]] = []
        current: list[tuple[str, Any]] = []
        columns, fitted = None, iter(self.fitted)
        for step in self.steps:
            current.append(step)
            if step[0] == "preprocess":
                segments.append((tuple(current), columns))
                current, domain = [], next(fitted, None)
                columns = None if domain is None else [*domain.variables, *domain.metas]
        if current:
            segments.append((tuple(current), columns))
        return segments

    @property
    def display_name(self) -> str:
        """E.g. "data (1,000,000 rows, in the cloud)"."""
        if self.n_rows is None:
            return f"{self.name} (in the cloud)"
        rows = "1 row" if self.n_rows == 1 else f"{self.n_rows:,} rows"
        return f"{self.name} ({rows}, in the cloud)"


def _all_rows(row_indices: Any) -> bool:
    return row_indices is ... or (isinstance(row_indices, slice) and row_indices == slice(None))


class RemoteTable(Table):
    """An Orange ``Table`` for a cloud table: the domain, one all-missing row,
    and a :class:`CloudRef` (see the module's documentation).

    Create it with :meth:`open` or :meth:`from_info`. ``len()`` is 1 (the
    placeholder row); the real row count is ``ref.n_rows``.
    """

    #: ``None`` only for instances Orange creates internally (e.g. through
    #: ``from_numpy``); those hold ordinary local data.
    ref: CloudRef | None = None

    # ------------------------------------------------------------------
    # Construction

    @classmethod
    def placeholder(cls, domain: Domain, ref: CloudRef) -> RemoteTable:
        """A table with ``domain`` and one row in which every value is missing."""
        X = np.full((1, len(domain.attributes)), np.nan)
        Y = np.full((1, len(domain.class_vars)), np.nan)
        metas = np.array([[var.Unknown for var in domain.metas]], dtype=object)
        metas = metas.reshape(1, len(domain.metas))
        table = cls.from_numpy(domain, X, Y, metas)
        return table._with_ref(ref)

    @classmethod
    def from_info(
        cls, info: TableInfo, domain: Domain, machine_id: str | None = None
    ) -> RemoteTable:
        """The table for the machine's ``TableInfo`` and the table's domain."""
        ref = CloudRef(info.id, info.name, info.n_rows, machine_id)
        return cls.placeholder(domain, ref)

    @classmethod
    def open(cls, client: CloudClient, table_id: str, machine_id: str | None = None) -> RemoteTable:
        """Fetch the table's metadata and domain from the machine (two small
        calls) and return its ``RemoteTable``."""
        try:
            info = client.table_info(table_id)
            domain = client.table_domain(table_id)
        except CloudNotFoundError as exc:
            raise TableUnavailableError() from exc
        return cls.from_info(info, domain, machine_id)

    def _with_ref(self, ref: CloudRef) -> RemoteTable:
        self.ref = ref
        with self.unlocked_reference():
            self.name = ref.display_name
        return self

    def _derived(self, source: Table, ref: CloudRef) -> RemoteTable:
        """A new ``RemoteTable`` with a copy of ``source``'s placeholder row and ``ref``."""
        table = type(self)()
        table.domain = source.domain
        with table.unlocked_reference():
            table.X, table.Y = source.X.copy(), source.Y.copy()
            table.metas, table.W = source.metas.copy(), source.W.copy()
            table.ids = source.ids.copy()
            table.attributes = getattr(source, "attributes", {})
        return table._with_ref(ref)

    # ------------------------------------------------------------------
    # Recorded transforms: Table.transform, Table(table), copy() and
    # Orange's widgets all go through from_table.

    @classmethod
    def from_table(cls, domain: Domain, source: Table, row_indices: Any = ...) -> Table:
        """As ``Table.from_table``; on all rows of a cloud table, a cloud table
        with the transform recorded."""
        local = Table.from_table(domain, source, row_indices)
        ref = getattr(source, "ref", None) if isinstance(source, RemoteTable) else None
        if ref is None or not _all_rows(row_indices):
            # A subset of the placeholder rows (e.g. a selection) means
            # nothing on the machine: plain local data.
            return local
        if domain is source.domain or domain == source.domain:
            return source._derived(local, ref)
        known = source.domain
        if any(
            var not in known and var.compute_value is None
            for var in domain.variables + domain.metas
        ):
            # A new column whose values are filled in afterwards (e.g. the
            # "Selected" column of an annotated output): the machine can't
            # compute it.
            return local
        return source._derived(local, ref.add("transform", domain))

    @classmethod
    def from_table_rows(cls, source: Table, row_indices: Any) -> Table:
        """As ``Table.from_table_rows``: rows of the placeholder, as local data."""
        # Rows picked from the placeholder: plain local data.
        return Table.from_table_rows(source, row_indices)

    # ------------------------------------------------------------------
    # Recorded filters (the Storage hooks that Orange.data.filter calls).
    # The placeholder row is kept, because Orange's widgets treat a table
    # with no rows as no data.

    def _filtered(self, row_filter: data_filter.Filter) -> RemoteTable:
        # Deep copy: Select Rows sets ``negate`` on its filter after using it.
        return self._derived(self, self.ref.add("filter", copy.deepcopy(row_filter)))

    def _filter_is_defined(
        self, columns: Sequence[ColumnRef] | None = None, negate: bool = False
    ) -> Table:
        if self.ref is None:
            return super()._filter_is_defined(columns, negate)
        return self._filtered(data_filter.IsDefined(columns, negate))

    def _filter_has_class(self, negate: bool = False) -> Table:
        if self.ref is None:
            return super()._filter_has_class(negate)
        return self._filtered(data_filter.HasClass(negate))

    def _filter_same_value(self, column: ColumnRef, value: Any, negate: bool = False) -> Table:
        if self.ref is None:
            return super()._filter_same_value(column, value, negate)
        return self._filtered(data_filter.SameValue(column, value, negate))

    def _filter_values(self, filter: data_filter.Values) -> Table:  # noqa: A002 (Orange's name)
        if self.ref is None:
            return super()._filter_values(filter)
        return self._filtered(filter)

    # ``_filter_random`` (Data Sampler and similar) isn't overridden: Orange
    # then picks rows of the placeholder, which gives plain local data.

    # ------------------------------------------------------------------
    # Statistics: the Storage hooks behind Orange.statistics' distributions
    # and contingencies. Computed from the placeholder row, they would choose
    # columns from no data at all (Rank's scores, Discretize's cut points,
    # Purge Domain's constant columns), so they raise instead. Basic
    # statistics (``_compute_basic_stats``) stay as Orange computes them:
    # Orange's Data Table uses them, and Normalize's steps are refused by the
    # machine with a clearer message.

    def _compute_distributions(
        self, columns: Sequence[ColumnRef] | None = None
    ) -> list[tuple[np.ndarray, float]]:
        if self.ref is None:
            return super()._compute_distributions(columns)
        raise PlaceholderStatisticsError()

    def _compute_contingency(
        self, col_vars: Sequence[ColumnRef] | None = None, row_var: ColumnRef | None = None
    ) -> list[tuple[Any, np.ndarray]]:
        if self.ref is None:
            return super()._compute_contingency(col_vars, row_var)
        raise PlaceholderStatisticsError()

    def unlocked(self, *parts: Any) -> Any:
        """Unlock the arrays for writing, as ``Table.unlocked``. Values written
        here (e.g. Randomize's shuffled rows) can't be replayed on the machine,
        so the table becomes plain local data."""
        self.ref = None
        return super().unlocked(*parts)

    # ------------------------------------------------------------------
    # For Cloud widgets

    @property
    def steps(self) -> tuple[tuple[str, Any], ...]:
        """The recorded steps, in order."""
        return self.ref.steps if self.ref is not None else ()

    def resolve(
        self,
        client: CloudClient,
        machine_id: str | None = None,
        wait: WaitForJob | None = None,
    ) -> str:
        """The ID of the table on the machine to compute on.

        With recorded steps, the machine replays them on all rows first
        (``derive``; it keeps the result, so the same steps run once) and
        this is the ID of the result. ``wait`` waits for each ``derive`` job
        (default: ``client.wait_for_job``).

        Raises :class:`LocalDataError` for local data,
        :class:`TableUnavailableError` if the machine (``machine_id``, when
        both it and the table know it) or the table is gone, and
        ``CloudUnsupportedStepError`` for a step the machine refuses, e.g.
        one that learned its settings from the placeholder row.
        """
        ref = self.ref
        if ref is None:
            raise LocalDataError()
        if machine_id is not None and ref.machine_id is not None and machine_id != ref.machine_id:
            raise TableUnavailableError()
        table_id = ref.table_id
        try:
            client.table_info(table_id)
            for steps, columns in ref.segments():
                job = client.derive(table_id, steps, name=ref.name, columns=columns)
                table_id = (wait or client.wait_for_job)(job).tables["table"].id
        except CloudNotFoundError as exc:
            raise TableUnavailableError() from exc
        return table_id


def is_cloud_table(data: Any) -> bool:
    """Whether ``data`` is a cloud table (and not local data)."""
    return isinstance(data, RemoteTable) and data.ref is not None


def resolve(
    data: Any, client: CloudClient, machine_id: str | None = None, wait: WaitForJob | None = None
) -> str:
    """:meth:`RemoteTable.resolve` for any input a Cloud widget gets; local
    data raises :class:`LocalDataError`."""
    if not is_cloud_table(data):
        raise LocalDataError()
    return data.resolve(client, machine_id, wait)


def preprocessed(
    data: Any,
    recipe: Any,
    client: CloudClient,
    machine_id: str | None = None,
    wait: WaitForJob | None = None,
) -> RemoteTable:
    """``data`` with Orange's Preprocess ``recipe`` fitted on all its rows on
    the machine: a :class:`RemoteTable` with the fitted domain and
    ``("preprocess", recipe)`` added to its steps. Raises as :func:`resolve`."""
    if not is_cloud_table(data):
        raise LocalDataError()
    pending = data._derived(data, data.ref.add("preprocess", recipe))
    table_id = pending.resolve(client, machine_id, wait)
    try:
        info = client.table_info(table_id)
        domain = client.table_domain(table_id)
    except CloudNotFoundError as exc:
        raise TableUnavailableError() from exc
    ref = replace(pending.ref, n_rows=info.n_rows, fitted=(*pending.ref.fitted, domain))
    return RemoteTable.placeholder(domain, ref)
