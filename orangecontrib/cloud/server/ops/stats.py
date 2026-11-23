"""Statistics on all rows of a table: ``POST /v1/tables/{id}/stats``.

The Cloud viewers (Feature Statistics, Distributions, Box Plot, Scatter Plot)
show these numbers instead of computing anything from the placeholder row.
Each kind computes what Orange's own widget computes, the same way, on all
rows of the table on the machine:

* ``basic``: per column, Orange's Feature Statistics: missing values, min,
  max, mean, median, mode and dispersion (coefficient of variation of
  numeric columns, entropy of categorical ones);
* ``distribution``: per column, counts per value (categorical) or per bin
  (numeric), optionally per value of a categorical ``group_by`` column. The
  bins are one of the binnings Orange's Distributions widget offers
  (``binning``), or 10 of equal width. Optionally a fitted curve per group;
* ``quantiles``: per column and group, Orange's Box Plot numbers;
* ``histogram2d``: counts on a grid of two numeric columns, optionally per
  group (a scatter plot's density);
* ``values``: per column, its values and how many rows have each, as Edit
  Domain and Create Class count them (Cloud Categories, Cloud Create
  Class): a categorical column's values, or the distinct values of a numeric
  or text column (at most :data:`MAX_VALUES`).

Columns are indices into the table's Orange ``Domain``. The answer is a
pickled ``dict`` (``docs/api-v1.md`` lists its keys). The worker keeps the
last answers per table and request, so asking again costs nothing.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

import numpy as np
from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from orangecontrib.cloud.protocol.errors import ErrorCode
from orangecontrib.cloud.protocol.pickles import dumps
from orangecontrib.cloud.protocol.recipes import value_counts
from orangecontrib.cloud.protocol.tables import StatsKind, StatsRequest
from orangecontrib.cloud.protocol.wire import PAYLOAD_HEADER, PAYLOAD_PICKLE, PICKLE_CONTENT_TYPE
from orangecontrib.cloud.server.errors import ApiError
from orangecontrib.cloud.server.jobs import get_jobs
from orangecontrib.cloud.server.worker import worker_store

__all__ = ["StatsError", "compute_stats", "router"]

#: Bins of the ``distribution`` of a numeric column without a ``binning``.
EQUAL_BINS = 10

#: Points at which a fitted curve is given, as in Orange's Distributions.
CURVE_POINTS = 100

#: ``values``: a column with more distinct values isn't listed.
MAX_VALUES = 100_000


class StatsError(ValueError):
    """A request that doesn't fit the table; the message says why."""


# --------------------------------------------------------------------------
# Columns
# --------------------------------------------------------------------------


def _variable(table: Any, index: int) -> Any:
    domain = table.domain
    if not -len(domain.metas) <= index < len(domain.variables):
        raise StatsError(f"The table has no column {index}.")
    return domain[index]


def _values(table: Any, var: Any) -> np.ndarray:
    """The column's values: floats for numeric and categorical columns."""
    values = table.get_column(var)
    return values.astype(float) if var.is_primitive() else values


def _group(table: Any, index: int | None) -> tuple[Any, np.ndarray] | tuple[None, None]:
    """The ``group_by`` column and its codes (NaN where missing)."""
    if index is None:
        return None, None
    var = _variable(table, index)
    if not var.is_discrete:
        raise StatsError(f"'{var.name}' isn't categorical, so it can't split the data.")
    return var, _values(table, var)


def _numeric(var: Any) -> None:
    if not var.is_continuous:
        raise StatsError(f"'{var.name}' isn't numeric.")


def _float(value: Any) -> float:
    return float(np.asarray(value).reshape(-1)[0])


# --------------------------------------------------------------------------
# basic: Orange's Feature Statistics
# --------------------------------------------------------------------------


def _entropy(x: np.ndarray) -> float:
    """Entropy of the values of a categorical column, as Feature Statistics."""
    import Orange.statistics.util as ut
    import scipy.stats as ss

    counts = ut.bincount(x[:, 0])[0]
    return float(ss.entropy(counts / np.sum(counts)))


def _coefficient_of_variation(x: np.ndarray) -> float:
    import Orange.statistics.util as ut

    mean = _float(ut.nanmean(x, axis=0))
    if np.isclose(mean, 0, atol=1e-12):
        return float("inf")
    return float(np.sqrt(_float(ut.nanvar(x, axis=0))) / mean)


def _basic(var: Any, values: np.ndarray) -> dict[str, float | int]:
    """Missing values, min, max, mean, median, mode and dispersion of a
    column; NaN where Feature Statistics shows nothing."""
    import Orange.statistics.util as ut

    nan = float("nan")
    stats: dict[str, float | int] = dict.fromkeys(
        ("min", "max", "mean", "median", "mode", "dispersion"), nan
    )
    if var.is_string:
        stats["missing"] = int(np.sum(values == var.Unknown))
        return stats
    x = values.reshape(-1, 1)
    stats["missing"] = int(ut.countnans(x))
    if not np.isfinite(x).any():
        return stats
    stats["min"] = _float(ut.nanmin(x, axis=0))
    stats["max"] = _float(ut.nanmax(x, axis=0))
    stats["mode"] = _float(ut.nanmode(x, axis=0)[0])
    if var.is_discrete:
        stats["dispersion"] = _entropy(x)
    else:
        stats["mean"] = _float(ut.nanmean(x, axis=0))
        stats["median"] = _float(ut.nanmedian(x, axis=0))
        if not var.is_time:
            stats["dispersion"] = _coefficient_of_variation(x)
    return stats


def _basic_stats(table: Any, request: StatsRequest) -> dict[str, Any]:
    columns = {}
    for index in _indices(table, request.columns):
        var = _variable(table, index)
        columns[index] = _basic(var, _values(table, var))
    return {"n_rows": len(table), "columns": columns}


def _indices(table: Any, columns: list[int] | None) -> list[int]:
    if columns is not None:
        return list(columns)
    domain = table.domain
    return list(range(len(domain.variables))) + [-1 - i for i in range(len(domain.metas))]


# --------------------------------------------------------------------------
# distribution: Orange's Distributions
# --------------------------------------------------------------------------


def _binnings(var: Any, values: np.ndarray) -> list[Any]:
    """The binnings that Orange's Distributions widget offers for a column."""
    from Orange.data import ContinuousVariable
    from Orange.preprocess.discretize import decimal_binnings, time_binnings

    if var.is_time:
        return time_binnings(values, min_unique=5)
    exact = type(var) is ContinuousVariable  # Distributions' min_var_resolution
    min_width = 10**-var.number_of_decimals if exact else 0
    return decimal_binnings(values, min_width=min_width, add_unique=10, min_unique=5)


def _equal_bins(values: np.ndarray) -> np.ndarray:
    low, high = float(np.min(values)), float(np.max(values))
    if low == high:
        low, high = low - 0.5, high + 0.5
    return np.linspace(low, high, EQUAL_BINS + 1)


def _counts(codes: np.ndarray, groups: np.ndarray | None, n_bins: int, n_groups: int) -> np.ndarray:
    """Counts per bin (``codes``), and per group: shape (bins,) or (bins, groups)."""
    if groups is None:
        return np.bincount(codes, minlength=n_bins)[:n_bins].astype(float)
    flat = np.bincount(codes * n_groups + groups.astype(int), minlength=n_bins * n_groups)
    return flat[: n_bins * n_groups].reshape(n_bins, n_groups).astype(float)


def _ash(values: np.ndarray, x: np.ndarray, sigma: float) -> np.ndarray:
    """Orange's "Kernel density" curve (Distributions' ``AshCurve.pdf``)."""
    hist, _ = np.histogram(values, x)
    kernel_x = np.arange(len(x)) - len(hist) / 2
    kernel = 1 / (np.sqrt(2 * np.pi)) * np.exp(-((kernel_x * sigma) ** 2) / 2)
    ash = np.convolve(hist, kernel, mode="same")
    return ash / ash.sum()


def _curve(values: np.ndarray, x: np.ndarray, request: StatsRequest) -> dict[str, Any] | None:
    """A fitted curve at the points ``x``, as Distributions draws it."""
    if not values.size:
        return None
    if request.fit == "kde":
        return {"params": (), "y": _ash(values, x, (22 - request.smoothing) / 40)}
    import scipy.stats

    dist = getattr(scipy.stats, str(request.fit))
    params = tuple(float(p) for p in dist.fit(values))
    return {"params": params, "y": dist.pdf(x, *params)}


def _numeric_distribution(
    var: Any, values: np.ndarray, groups: np.ndarray | None, n_groups: int, request: StatsRequest
) -> dict[str, Any]:
    defined = ~np.isnan(values)
    result: dict[str, Any] = {}
    if request.binning is None:
        thresholds = _equal_bins(values[defined])
    else:
        binnings = _binnings(var, values)
        result["binnings"] = binnings
        result["binning"] = index = min(request.binning, len(binnings) - 1)
        thresholds = np.asarray(binnings[index].thresholds, dtype=float)
    result["thresholds"] = thresholds
    valid = defined if groups is None else defined & ~np.isnan(groups)
    data = values[valid]
    if groups is None:
        result["counts"] = np.histogram(data, thresholds)[0].astype(float)
        parts = [data]
    else:
        codes = groups[valid]
        parts = [data[codes == i] for i in range(n_groups)]
        result["counts"] = np.column_stack(
            [np.histogram(part, thresholds)[0] for part in parts]
        ).astype(float)
    result["n_valid"] = int(data.size)
    if request.fit is not None:
        x = np.linspace(thresholds[0], thresholds[-1], CURVE_POINTS)
        result["curve_x"] = x
        result["curves"] = [_curve(part, x, request) for part in parts]
    return result


def _categorical_distribution(
    var: Any, values: np.ndarray, groups: np.ndarray | None, n_groups: int
) -> dict[str, Any]:
    n_values = len(var.values)
    defined = ~np.isnan(values)
    if groups is None:
        counts = _counts(values[defined].astype(int), None, n_values, 0)
        return {"counts": counts, "n_valid": int(defined.sum())}
    grouped = ~np.isnan(groups)
    both = defined & grouped
    counts = _counts(values[both].astype(int), groups[both], n_values, n_groups)
    ungrouped = values[defined & ~grouped].astype(int)
    return {
        "counts": counts,
        "n_valid": int(both.sum()),
        # Rows whose group is missing (Box Plot shows them as a group).
        "group_missing": _counts(ungrouped, None, n_values, 0),
    }


def _distribution_stats(table: Any, request: StatsRequest) -> dict[str, Any]:
    group_var, groups = _group(table, request.group_by)
    n_groups = len(group_var.values) if group_var is not None else 0
    columns = {}
    for index in _indices(table, request.columns):
        var = _variable(table, index)
        values = _values(table, var)
        if var.is_string:
            if request.columns is None:
                continue
            raise StatsError(f"'{var.name}' is a text column.")
        missing = int(np.isnan(values).sum())
        if var.is_discrete:
            column = _categorical_distribution(var, values, groups, n_groups)
        elif missing == len(values):
            column = {"counts": None, "n_valid": 0}
        else:
            column = _numeric_distribution(var, values, groups, n_groups, request)
        columns[index] = {**column, "missing": missing}
    return {"n_rows": len(table), "columns": columns}


# --------------------------------------------------------------------------
# quantiles: Orange's Box Plot
# --------------------------------------------------------------------------


def _box(values: np.ndarray) -> dict[str, float | int]:
    """Count, min, max, mean, variance and quartiles (midpoint) of the
    values that aren't missing, as Box Plot's ``BoxData``."""
    values = values[~np.isnan(values)]
    if not values.size:
        return {"n": 0}
    q25, median, q75 = np.quantile(values, [0.25, 0.5, 0.75], method="midpoint")
    return {
        "n": int(values.size),
        "min": float(np.min(values)),
        "max": float(np.max(values)),
        "mean": float(np.mean(values)),
        "var": float(np.var(values)),
        "q25": float(q25),
        "median": float(median),
        "q75": float(q75),
    }


def _quantile_stats(table: Any, request: StatsRequest) -> dict[str, Any]:
    group_var, groups = _group(table, request.group_by)
    columns = {}
    for index in _indices(table, request.columns):
        var = _variable(table, index)
        _numeric(var)
        values = _values(table, var)
        if group_var is None:
            columns[index] = {"groups": [_box(values)]}
        else:
            parts = [values[groups == i] for i in range(len(group_var.values))]
            columns[index] = {"groups": [_box(part) for part in parts]}
    return {"n_rows": len(table), "columns": columns}


# --------------------------------------------------------------------------
# histogram2d: a scatter plot's density
# --------------------------------------------------------------------------


def _edges(values: np.ndarray, n: int) -> np.ndarray:
    low, high = float(np.min(values)), float(np.max(values))
    if low == high:
        low, high = low - 0.5, high + 0.5
    return np.linspace(low, high, n + 1)


def _histogram2d_stats(table: Any, request: StatsRequest) -> dict[str, Any]:
    if request.columns is None or len(request.columns) != 2:
        raise StatsError("'histogram2d' needs exactly two columns.")
    variables = [_variable(table, index) for index in request.columns]
    for var in variables:
        _numeric(var)
    x, y = (_values(table, var) for var in variables)
    valid = ~np.isnan(x) & ~np.isnan(y)
    x, y = x[valid], y[valid]
    result: dict[str, Any] = {"n_rows": len(table), "n_valid": int(valid.sum())}
    if not x.size:
        return {**result, "counts": None}
    x_edges, y_edges = _edges(x, request.grid), _edges(y, request.grid)
    result.update(x_edges=x_edges, y_edges=y_edges)
    result["counts"] = np.histogram2d(x, y, [x_edges, y_edges])[0]
    group_var, groups = _group(table, request.group_by)
    if group_var is not None:
        codes = groups[valid]
        result["group_counts"] = np.stack(
            [
                np.histogram2d(x[codes == i], y[codes == i], [x_edges, y_edges])[0]
                for i in range(len(group_var.values))
            ]
        )
    return result


# --------------------------------------------------------------------------
# values: Edit Domain's and Create Class's counts
# --------------------------------------------------------------------------


def _value_stats(table: Any, request: StatsRequest) -> dict[str, Any]:
    columns = {}
    for index in _indices(table, request.columns):
        counts = value_counts(table, _variable(table, index))
        column = {**counts._asdict(), "n_values": len(counts.values)}
        if len(counts.values) > MAX_VALUES:
            column.update(values=None, counts=None, first_seen=None)
        columns[index] = column
    return {"n_rows": len(table), "columns": columns}


# --------------------------------------------------------------------------
# In the worker
# --------------------------------------------------------------------------

_KINDS: dict[StatsKind, Callable[[Any, StatsRequest], dict[str, Any]]] = {
    StatsKind.BASIC: _basic_stats,
    StatsKind.DISTRIBUTION: _distribution_stats,
    StatsKind.QUANTILES: _quantile_stats,
    StatsKind.HISTOGRAM2D: _histogram2d_stats,
    StatsKind.VALUES: _value_stats,
}


def compute_stats(table: Any, request: StatsRequest) -> dict[str, Any]:
    """The statistics of ``table`` that ``request`` asks for (a ``dict``).
    Raises :class:`StatsError` for a request that doesn't fit the table."""
    return _KINDS[request.kind](table, request)


#: Pickled answers kept in the worker: ``(table_id, request JSON)`` -> pickle.
_CACHE: OrderedDict[tuple[str, str], bytes] = OrderedDict()
_CACHE_KEPT = 64
_cache_lock = threading.Lock()


def _stats_pickle(table_id: str, request_json: str) -> bytes | str | None:
    """The pickled answer; ``None`` for an unknown table, a message for a
    request that doesn't fit it."""
    store = worker_store()
    table = store.tables.get(table_id)
    if table is None:
        return None
    key = (table_id, request_json)
    with _cache_lock:
        for old in [k for k in _CACHE if k[0] not in store.tables]:
            del _CACHE[old]
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    try:
        data = dumps(compute_stats(table, StatsRequest.model_validate_json(request_json)))
    except StatsError as exc:
        return str(exc)
    with _cache_lock:
        _CACHE[key] = data
        while len(_CACHE) > _CACHE_KEPT:
            _CACHE.popitem(last=False)
    return data


# --------------------------------------------------------------------------
# Route
# --------------------------------------------------------------------------

router = APIRouter()


@router.post("/tables/{table_id}/stats")
async def table_stats(table_id: str, body: StatsRequest, request: Request) -> Response:
    """Statistics on all rows of a table, as a pickled ``dict``."""
    manager = get_jobs(request)
    manager.table_info(table_id)
    data = await run_in_threadpool(
        manager.run_on_worker, _stats_pickle, table_id, body.model_dump_json()
    )
    if data is None:
        raise ApiError(ErrorCode.NOT_FOUND, f"No table with ID '{table_id}'.")
    if isinstance(data, str):
        raise ApiError(ErrorCode.BAD_REQUEST, data)
    return Response(
        content=data, media_type=PICKLE_CONTENT_TYPE, headers={PAYLOAD_HEADER: PAYLOAD_PICKLE}
    )
