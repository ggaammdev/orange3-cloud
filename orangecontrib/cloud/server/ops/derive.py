"""Replaying recorded steps on all rows: ``POST /v1/tables/{id}/derive``.

Orange's own data-changing widgets (Select Columns, Select Rows, Feature
Constructor, Continuize, ...) work on the add-on's placeholder table, which
records their steps instead of applying them: ``("transform", Domain)`` and
``("filter", Filter)``. The ``derive`` job replays the steps on all rows of a
table with Orange's own ``Table.transform`` and filters, keeps the result as
a new table, and drops the tables in between as it goes.

Steps that learned something from the data learned it from the placeholder's
single row of missing values, so the machine refuses them with the error
``unsupported_step`` instead of computing wrong results:

* every new column must come from a computation that holds no numbers taken
  from the data (:data:`ALLOWED_COMPUTE_VALUES`); Orange's steps that learn
  from data, such as ``Normalizer`` or ``ReplaceUnknowns``, belong in Cloud
  Preprocess, discretizing (``Discretizer``) in Cloud Discretize, and Edit
  Domain's "as categorical" (a ``MappingTransform`` of the values it saw) in
  Cloud Categories; Create Class keeps only its first rule on the
  placeholder and belongs in Cloud Create Class; anything else isn't
  supported;
* a mapping of categories (``Lookup``, e.g. Edit Domain's renaming or
  merging) must keep some of them, unlike Purge Domain's "remove unused
  values" on the placeholder (Cloud Purge Domain decides it on all rows);
* a categorical column made by Feature Constructor must give, on all rows,
  only values in its list of values.

Steps that learn their settings from the data come as ``("preprocess",
recipe)``: from Cloud Preprocess, a recipe from Orange's Preprocess widget;
from Cloud Purge Domain, Orange's ``Remove``; from Cloud Discretize, Cloud
Categories and Cloud Create Class, the add-on's recipes
(:mod:`orangecontrib.cloud.protocol.recipes`: ``DiscretizeColumns``,
``ColumnCategories``, ``CreateClass``).
The machine fits it on all rows with Orange's own code, after checking that
the fitted table can fit in memory (``payload_too_large`` otherwise; a
recipe's result is a new table, at least as large as the arrays of the
table it starts from, see :func:`check_fit_memory`). Its result has columns
computed with numbers learned from all rows (``Normalizer``, ...), so the
add-on sends the steps after it in a separate request that starts from the
fitted table and refers to its columns by position (``refs`` in
:func:`~orangecontrib.cloud.protocol.pickles.dumps`): they are this table's own
columns, which the checks accept, and not copies learned from the placeholder.

The result is kept per parent table and ``steps_key``, so the same steps on
the same table are replayed once. Widgets downstream of the same steps share
their first steps (e.g. Feature Constructor and Select Rows, then a second
Select Rows in one branch and Select Columns in another), so ``derive`` jobs
run one at a time, and each starts from the longest of its first steps
already replayed on the same table (the request's ``prefix_keys`` name
them); while replaying, a job keeps the table after a step as an extra
output when a waiting job starts with the same steps and then goes on with
other ones. So a long first step (Feature Constructor on many rows) runs
once, and the machine's memory holds one replay at a time. A table kept so
is dropped when a ``derive`` job ends and no other waiting or running one
starts with its steps, unless a request got it as its result meanwhile
(:func:`claim_kept`); derived tables that were deleted are forgotten.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import numpy as np
from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool

from orangecontrib.cloud.protocol.errors import ErrorCode
from orangecontrib.cloud.protocol.jobs import JobInfo, JobKind
from orangecontrib.cloud.protocol.recipes import DiscretizeColumns
from orangecontrib.cloud.protocol.tables import DeriveRequest
from orangecontrib.cloud.server.jobs import get_jobs, read_multipart
from orangecontrib.cloud.server.resources import memory_budget
from orangecontrib.cloud.server.worker import JobContext, OperationError, operation, worker_store

__all__ = [
    "ALLOWED_COMPUTE_VALUES",
    "AS_CATEGORICAL_MESSAGE",
    "CREATE_CLASS_MESSAGE",
    "DISCRETIZED_MESSAGE",
    "LEARNED_MESSAGE",
    "NO_VALUES_MESSAGE",
    "UNSUPPORTED_MESSAGE",
    "check_categorical",
    "check_fit_memory",
    "check_transform",
    "claim_kept",
    "derive_operation",
    "replay",
    "router",
]

_FEATURE_FUNC = "Orange.widgets.data.owfeatureconstructor.FeatureFunc"
_MAPPING_CAST = "Orange.widgets.data.owfeatureconstructor.MappingTransformCast"
_LOOKUP = "Orange.preprocess.transformation.Lookup"
_MAPPING_TRANSFORM = "Orange.preprocess.transformation.MappingTransform"
_DISCRETIZER = "Orange.preprocess.discretize.Discretizer"
_CREATE_CLASS = "Orange.widgets.data.owcreateclass"

#: Kinds of ``compute_value`` that carry no numbers learned from data: copies,
#: one-hot coding from a column's own categories, Feature Constructor's
#: expressions, and mappings of a column's own categories (Edit Domain's
#: renaming, merging and turning categories into numbers or text; checked
#: further by :func:`_check_lookup`). Add a kind only after checking that it
#: learns nothing.
ALLOWED_COMPUTE_VALUES = frozenset({
    "Orange.preprocess.transformation.Identity",
    "Orange.preprocess.transformation.Indicator",
    "Orange.preprocess.transformation.Indicator1",
    _FEATURE_FUNC,
    _LOOKUP,
})  # fmt: skip

LEARNED_MESSAGE = (
    "'{name}' was made by a step that learns its settings from the data. "
    "On cloud data, use Cloud Preprocess for this step."
)
UNSUPPORTED_MESSAGE = "'{name}' was made by a step that isn't supported on cloud data ({kind})."
CREATE_CLASS_MESSAGE = (
    "'{name}' was made by Create Class, which isn't supported on cloud data: "
    "it counts matches on the laptop's empty placeholder row and so keeps "
    "only its first rule. On cloud data, use Cloud Create Class for this step."
)
AS_CATEGORICAL_MESSAGE = (
    "'{name}' took its categories from the laptop's empty placeholder row, "
    "not from the data. On cloud data, use Cloud Categories to make a "
    "column categorical."
)
NO_VALUES_MESSAGE = (
    "'{name}' keeps none of the values of '{source}': the step chose them "
    "from the laptop's empty placeholder row, which has no values. On cloud "
    "data, use Cloud Purge Domain to remove unused values."
)
DISCRETIZED_MESSAGE = (
    "'{name}' was discretized by a widget that sees only the laptop's empty "
    "placeholder row. On cloud data, use Cloud Discretize, or Discretize in "
    "Cloud Preprocess."
)
VALUES_MESSAGE = (
    "'{name}' gives values that aren't in its list ({values}). "
    "List all values in Feature Constructor."
)
#: Rows whose Feature Constructor values are checked at a time.
CHECK_ROWS = 1_000_000

TOO_LARGE_MESSAGE = (
    "Fitting {recipe} on {rows:,} rows needs at least {needed:.1f} GB of memory, but "
    "the machine has {free:.1f} GB free. Choose a larger machine size in Cloud settings."
)


def _class_name(obj: Any) -> str:
    cls = type(obj)
    return getattr(cls, "orange_class", None) or f"{cls.__module__}.{cls.__qualname__}"


def _refuse(message: str) -> OperationError:
    return OperationError(ErrorCode.UNSUPPORTED_STEP, message)


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


def _new_variables(domain: Any, source: Any) -> Iterator[Any]:
    """Variables of ``domain`` that ``source`` (a Domain) doesn't have."""
    for var in domain.variables + domain.metas:
        if var not in source:
            yield var


def _sources(compute_value: Any) -> list[Any]:
    """The variables an allowed ``compute_value`` reads."""
    if _class_name(compute_value) == _FEATURE_FUNC:
        return [var for _, var in compute_value.args]
    return [compute_value.variable]


def _not_allowed(var: Any, compute_value: Any) -> OperationError:
    """The refusal for a column made by a kind that isn't allowed."""
    kind = _class_name(compute_value)
    if kind == _MAPPING_TRANSFORM:
        return _refuse(AS_CATEGORICAL_MESSAGE.format(name=var.name))
    if kind == _DISCRETIZER:
        return _refuse(DISCRETIZED_MESSAGE.format(name=var.name))
    if kind.startswith(f"{_CREATE_CLASS}."):
        return _refuse(CREATE_CLASS_MESSAGE.format(name=var.name))
    if kind.startswith("Orange.") and not kind.startswith("Orange.widgets."):
        # Orange's own transformations that aren't allowed hold numbers from
        # data (normalizing, imputing, discretizing, projections, models).
        return _refuse(LEARNED_MESSAGE.format(name=var.name))
    return _refuse(UNSUPPORTED_MESSAGE.format(name=var.name, kind=kind.rpartition(".")[2]))


def _check_lookup(var: Any, lookup: Any) -> None:
    """Refuse a mapping of categories that keeps none of them, as steps that
    chose the used values on the placeholder row do."""
    source = lookup.variable
    if var.is_discrete and len(var.values) == 0 and len(getattr(source, "values", ())) > 0:
        raise _refuse(NO_VALUES_MESSAGE.format(name=var.name, source=source.name))


def _check_variable(var: Any, source: Any, seen: set[int]) -> None:
    if var in source or id(var) in seen:
        return
    seen.add(id(var))
    compute_value = var.compute_value
    if compute_value is None:
        return  # Orange gives missing values for it, here as on the laptop
    if _class_name(compute_value) not in ALLOWED_COMPUTE_VALUES:
        raise _not_allowed(var, compute_value)
    if _class_name(compute_value) == _LOOKUP:
        _check_lookup(var, compute_value)
    for source_var in _sources(compute_value):
        _check_variable(source_var, source, seen)


def check_transform(domain: Any, source: Any) -> None:
    """Refuse a transform to ``domain`` from the ``source`` domain if a new
    column, or a column it is computed from, learned from data."""
    seen: set[int] = set()
    for var in _new_variables(domain, source):
        _check_variable(var, source, seen)


def _outside_values(var: Any, func: Any, table: Any) -> bool:
    """Whether Feature Constructor's ``func`` gives values outside ``var``'s
    list on any row of ``table``."""
    if _class_name(func.cast) == _MAPPING_CAST:
        # No list was given, so Feature Constructor took the values that the
        # expression gave on the placeholder row. Orange alone takes them from
        # all rows: as text, the same way.
        as_text = type(func)(func.expression, func.args, use_values=func.use_values)
        found = {str(value) for value in as_text(table)}
        return not found <= set(var.values)
    for start in range(0, len(table), CHECK_ROWS):
        # In parts: an expression makes Python objects for every row.
        codes = np.asarray(func(table[start : start + CHECK_ROWS]), dtype=float)
        codes = codes[np.isfinite(codes)]
        valid = (codes >= 0) & (codes < len(var.values)) & (codes == np.round(codes))
        if not valid.all():
            return True
    return False


def check_categorical(domain: Any, table: Any) -> None:
    """Refuse a transform of ``table`` to ``domain`` if a categorical column
    made by Feature Constructor gives values that aren't in its list."""
    for var in _new_variables(domain, table.domain):
        func = var.compute_value
        if not var.is_discrete or _class_name(func) != _FEATURE_FUNC:
            continue
        if _outside_values(var, func, table):
            values = ", ".join(var.values)
            raise _refuse(VALUES_MESSAGE.format(name=var.name, values=values))


# --------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------


def _recipe_names(recipe: Any) -> str:
    """E.g. "Impute, Normalize, Discretize" for a Preprocess recipe."""
    items = getattr(recipe, "preprocessors", None)
    items = [recipe] if items is None else items
    names = [type(item).__name__.lstrip("_") for item in items if item is not None]
    return ", ".join(names) or "nothing"


def describe(kind: str, value: Any) -> str:
    """One short line for a step, e.g. "transform: 4 columns"."""
    if kind == "transform":
        n = len(value.variables) + len(value.metas)
        return f"transform: {n} column{'s' if n != 1 else ''}"
    if kind == "preprocess":
        return f"preprocess: {_recipe_names(value)}"
    return f"filter: {type(value).__name__}"


def _validate(steps: Any) -> list[tuple[str, Any]]:
    from Orange.data import Domain
    from Orange.data.filter import Filter
    from Orange.preprocess.preprocess import Preprocess

    expected = {"transform": Domain, "filter": Filter, "preprocess": Preprocess}
    if not isinstance(steps, (tuple, list)):
        raise OperationError(ErrorCode.BAD_REQUEST, "'steps' must be a list of steps.")
    for step in steps:
        if not (
            isinstance(step, tuple)
            and len(step) == 2
            and step[0] in expected
            and isinstance(step[1], expected[step[0]])
        ):
            raise OperationError(
                ErrorCode.BAD_REQUEST,
                "Each step must be ('transform', Domain), ('filter', Filter) "
                "or ('preprocess', Preprocess).",
            )
    return list(steps)


def _array_bytes(table: Any) -> int:
    """Memory of a table's arrays, without the text that meta columns hold."""
    total = 0
    for array in (table.X, table.Y, table.metas, table.W):
        if hasattr(array, "indices"):  # scipy.sparse
            total += array.data.nbytes + array.indices.nbytes + array.indptr.nbytes
        else:
            total += array.nbytes
    return total


def check_fit_memory(table: Any, recipe: Any, budget: int) -> int:
    """The memory fitting ``recipe`` on ``table`` needs at least: its result,
    a new table, holds new arrays as large as ``table``'s (the text in meta
    columns is shared). Fails with ``payload_too_large`` above ``budget``."""
    needed = _array_bytes(table)
    if needed > budget:
        message = TOO_LARGE_MESSAGE.format(
            recipe=_recipe_names(recipe), rows=len(table), needed=needed / 1e9, free=budget / 1e9
        )
        raise OperationError(ErrorCode.PAYLOAD_TOO_LARGE, message)
    return needed


def _fit(recipe: Any, table: Any, ctx: JobContext | None, done: tuple[int, int]) -> Any:
    """``recipe`` fitted on all rows of ``table``, with Orange's own code.
    Cloud Discretize's recipe reports each column and can be cancelled
    between them; ``done`` is (this step, number of steps)."""
    check_fit_memory(table, recipe, memory_budget())
    if ctx is None or not isinstance(recipe, DiscretizeColumns):
        return recipe(table)
    step, steps = done

    def progress(column: int, columns: int) -> None:
        ctx.check_cancelled()
        ctx.progress(
            (step + column / columns) / steps, f"Discretizing column {column + 1} of {columns}"
        )

    return recipe(table, progress=progress)


def replay(
    table: Any,
    steps: list[tuple[str, Any]],
    ctx: JobContext | None = None,
    after_step: Callable[[int, Any], None] | None = None,
) -> Any:
    """``table`` after the steps, checked as the module describes.
    ``after_step(i, table)`` gets the table after each step but the last."""
    for i, (kind, value) in enumerate(steps):
        if ctx is not None:
            ctx.check_cancelled()
            ctx.progress(i / len(steps), f"Step {i + 1} of {len(steps)}: {describe(kind, value)}")
        if kind == "preprocess":
            table = _fit(value, table, ctx, (i, len(steps)))
        elif kind == "filter":
            table = value(table)
        elif value != table.domain:
            check_transform(value, table.domain)
            check_categorical(value, table)
            table = table.transform(value)
        if after_step is not None and i < len(steps) - 1:
            after_step(i, table)
    return table


# --------------------------------------------------------------------------
# One replay at a time, from the longest replayed beginning
# --------------------------------------------------------------------------

#: Held by the ``derive`` job that is replaying (see the module's documentation).
_REPLAYING = threading.Lock()
#: Guards :data:`_DERIVED` and :data:`_WAITING`.
_STATE = threading.Lock()
#: Tables this worker derived: (parent table ID, key of the steps) -> TableInfo.
_DERIVED: dict[tuple[str, str], Any] = {}
#: ``derive`` jobs in this worker: job ID -> (parent table ID, keys of their
#: first 1, 2, ... n steps).
_WAITING: dict[str, tuple[str, list[str]]] = {}
#: Tables kept after a step for waiting jobs (``steps-<k>`` outputs), which no
#: widget uses: table ID -> (parent table ID, key of the steps).
_KEPT: dict[str, tuple[str, str]] = {}


@contextmanager
def _turn(ctx: JobContext, parent_id: str, keys: list[str]) -> Iterator[None]:
    """Wait (cancellably) for the other ``derive`` jobs, then replay."""
    with _STATE:
        _WAITING[ctx.job_id] = (parent_id, keys)
    try:
        if not _REPLAYING.acquire(blocking=False):
            ctx.progress(0.0, "Waiting for another table to be derived")
            while not _REPLAYING.acquire(timeout=0.5):
                ctx.check_cancelled()
        try:
            yield
        finally:
            _REPLAYING.release()
    finally:
        with _STATE:
            _WAITING.pop(ctx.job_id, None)


def _derived(ctx: JobContext, parent_id: str, key: str) -> Any:
    """The TableInfo of a table derived from ``parent_id`` with the steps of
    ``key`` that the worker still has, else ``None``."""
    with _STATE:
        info = _DERIVED.get((parent_id, key))
    return info if info is not None and info.id in ctx.store.tables else None


def _forget_deleted(ctx: JobContext) -> None:
    """Forget the derived tables that were deleted."""
    with _STATE:
        for where, info in list(_DERIVED.items()):
            if info.id not in ctx.store.tables:
                del _DERIVED[where]
                _KEPT.pop(info.id, None)


def _release_unwanted(ctx: JobContext) -> None:
    """Drop the tables kept after a step that no other waiting or running
    ``derive`` job starts with."""
    with _STATE:
        for table_id, (parent_id, key) in list(_KEPT.items()):
            wanted = any(
                job_id != ctx.job_id and parent == parent_id and key in keys
                for job_id, (parent, keys) in _WAITING.items()
            )
            if not wanted:
                del _KEPT[table_id]
                _DERIVED.pop((parent_id, key), None)
                ctx.release_table(table_id)


def claim_kept(table_id: str) -> bool:
    """Keep a derived table that a request now gets as its result (a table
    kept after a step is otherwise dropped once no job wants it). False if
    the worker no longer has it. Runs in the worker (``run_on_worker``)."""
    with _STATE:
        _KEPT.pop(table_id, None)
        return table_id in worker_store().tables


def _start(ctx: JobContext, parent_id: str, keys: list[str]) -> tuple[Any, int]:
    """The longest beginning of the steps already replayed on the table
    (its TableInfo, or ``None``) and how many steps it has."""
    for k in range(len(keys), 0, -1):
        if keys[k - 1] and (info := _derived(ctx, parent_id, keys[k - 1])) is not None:
            return info, k
    return None, 0


def _keep_wanted(ctx: JobContext, parent_id: str, keys: list[str], done: int) -> Callable:
    """``after_step`` for :func:`replay`: keep the table after a step when a
    waiting job starts with the same steps and goes on with other ones (a job
    that shares the next step too starts from a later table)."""

    def after_step(i: int, table: Any) -> None:
        key, next_key = keys[done + i], keys[done + i + 1]
        with _STATE:
            wanted = any(
                job_id != ctx.job_id
                and parent == parent_id
                and key in their_keys
                and next_key not in their_keys
                for job_id, (parent, their_keys) in _WAITING.items()
            )
        if wanted and key and _derived(ctx, parent_id, key) is None:
            info = _remember(ctx, parent_id, key, f"steps-{done + i + 1}", table)
            with _STATE:
                _KEPT[info.id] = (parent_id, key)

    return after_step


def _remember(ctx: JobContext, parent_id: str, key: str, name: str, table: Any) -> Any:
    """Keep ``table`` as output ``name``, derived from ``parent_id`` with the
    steps of ``key``."""
    info = ctx.add_table(name, table, table_name=ctx.params.get("name"), parent_id=parent_id)
    info.steps_key = key
    with _STATE:
        _DERIVED[(parent_id, key)] = info
    return info


@operation(JobKind.DERIVE, public=False)
def derive_operation(ctx: JobContext) -> dict[str, Any]:
    """Replay ``parts["steps"]`` on ``inputs["data"]``. Params: ``name``,
    ``steps_key``, ``prefix_keys``. Result: table ``table`` (and tables after
    some steps that waiting jobs start with) and values ``rows`` and
    ``replayed_steps``."""
    steps = _validate(ctx.parts.get("steps"))
    parent_id = str(ctx.inputs["data"])
    prefix_keys = list(ctx.params.get("prefix_keys") or [])[: len(steps) - 1]
    keys = prefix_keys + [""] * (len(steps) - 1 - len(prefix_keys))
    keys.append(ctx.params.get("steps_key") or "")
    with _turn(ctx, parent_id, keys):
        _forget_deleted(ctx)
        start, done = _start(ctx, parent_id, keys)
        if done == len(steps):  # another job replayed the same steps meanwhile
            claim_kept(start.id)
            ctx.tables["table"] = start
            _release_unwanted(ctx)
            return {"rows": start.n_rows, "replayed_steps": 0}
        table = ctx.table("data") if start is None else ctx.store.tables[start.id]
        after_step = _keep_wanted(ctx, parent_id, keys, done)
        table = replay(table, steps[done:], ctx, after_step)
        info = _remember(ctx, parent_id, keys[-1], "table", table)
        _release_unwanted(ctx)
    info.steps = [describe(kind, value) for kind, value in steps]
    ctx.progress(1.0, "Done")
    return {"rows": len(table), "replayed_steps": len(steps) - done}


# --------------------------------------------------------------------------
# Route
# --------------------------------------------------------------------------

router = APIRouter()


@router.post("/tables/{table_id}/derive", status_code=202, response_model=JobInfo)
async def derive_table(table_id: str, request: Request) -> JobInfo:
    """Start a ``derive`` job, or answer with a finished one when the same
    steps were already replayed on this table."""
    manager = get_jobs(request)
    parent = manager.table_info(table_id)
    spec, parts = await read_multipart(request, DeriveRequest, ("steps",))
    cached = manager.find_derived(table_id, spec.steps_key)
    if cached is not None and await run_in_threadpool(manager.run_on_worker, claim_kept, cached.id):
        return manager.finished_job(JobKind.DERIVE.value, {"table": cached})
    params = {
        "name": spec.name or parent.name,
        "steps_key": spec.steps_key,
        "prefix_keys": spec.prefix_keys,
    }
    return await run_in_threadpool(
        manager.submit, JobKind.DERIVE.value, {"data": table_id}, params, parts
    )
