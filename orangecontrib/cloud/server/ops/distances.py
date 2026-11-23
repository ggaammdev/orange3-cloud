"""Distance matrices on the machine: the ``distances`` job.

``distances`` computes the distances between the rows or the columns of a
table (input ``data``) the way Orange's Distances widget does, with the
metrics of ``Orange.distance``: it refuses and fixes the data as the widget
does (categorical columns ignored where the metric can't use them, missing
values imputed, Mahalanobis only up to 1,000 columns or rows), then calls
the metric with ``impute=True`` and, for Euclidean and Manhattan,
optionally ``normalize=True``. Unlike the widget, it has no limit of 20,000
rows: the limit is the machine's memory.

**Memory.** Before computing, the job compares what the metric needs
(:func:`~orangecontrib.cloud.protocol.memory.distances_memory_bytes`: n x n x 8
bytes times the metric's working matrices, plus copies of the data) with
the memory the worker may still use, and refuses with
``payload_too_large`` if it doesn't fit, so a large request can't make the
worker run out of memory.

**Result.** Object ``distances`` (kind ``distances``): Orange's
``DistMatrix``, as the widget outputs it (its ``row_items`` the data, its
``axis`` 1 for rows and 0 for columns). It stays on the machine; later jobs
use it by its object ID.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from orangecontrib.cloud.protocol.errors import ErrorCode
from orangecontrib.cloud.protocol.jobs import JobKind, ObjectKind
from orangecontrib.cloud.protocol.memory import distances_memory_bytes, distances_too_large_message
from orangecontrib.cloud.server.resources import memory_budget
from orangecontrib.cloud.server.worker import JobContext, OperationError, operation

__all__ = ["AXES", "METRIC_CLASSES", "distances_operation", "prepare"]

#: The ``axis`` param: what is compared.
AXES = ("rows", "columns")
#: ``Orange.distance`` class of each metric, and its name in Orange's widget.
METRIC_CLASSES = {
    "euclidean": ("Euclidean", "Euclidean"),
    "manhattan": ("Manhattan", "Manhattan"),
    "cosine": ("Cosine", "Cosine"),
    "mahalanobis": ("Mahalanobis", "Mahalanobis"),
    "hamming": ("Hamming", "Hamming"),
    "pearson": ("PearsonR", "Pearson"),
    "pearson_absolute": ("PearsonRAbsolute", "Pearson (absolute)"),
    "spearman": ("SpearmanR", "Spearman"),
    "spearman_absolute": ("SpearmanRAbsolute", "Spearman (absolute)"),
    "jaccard": ("Jaccard", "Jaccard"),
}
#: Mahalanobis handles up to this many columns (rows, between columns).
MAHALANOBIS_ITEMS = 1000
#: Share of the progress bar for preparing the data.
PREPARE = 0.05

# The messages of Orange's Distances widget.
DENSE_ONLY = "{} requires dense data."
MAHALANOBIS_LIMIT = "Mahalanobis handles up to 1000 {}."
NO_CONTINUOUS = "No numeric features"
NO_BINARY = "No binary features"
IGNORING_DISCRETE = "Ignoring categorical features"
IGNORING_NONBINARY = "Ignoring non-binary features"
IMPUTING = "Missing values were imputed"
NO_FEATURES = "Data has no features"


@dataclass
class Options:
    """The job's options, checked."""

    metric: str = "euclidean"
    normalize: bool = False
    axis: str = "rows"

    @property
    def by_columns(self) -> bool:
        """Whether the job compares columns."""
        return self.axis == "columns"


def options(params: dict[str, Any]) -> Options:
    """The job's options from its params; a bad one fails the job."""
    metric = params.get("metric", "euclidean")
    if metric not in METRIC_CLASSES:
        raise OperationError(
            ErrorCode.BAD_REQUEST, f"'metric' must be one of {tuple(METRIC_CLASSES)}."
        )
    axis = params.get("axis", "rows")
    if axis not in AXES:
        raise OperationError(ErrorCode.BAD_REQUEST, f"'axis' must be one of {AXES}.")
    return Options(metric, bool(params.get("normalize", False)), axis)


def metric_class(metric: str) -> Any:
    """The ``Orange.distance`` class of ``metric``."""
    from Orange import distance

    return getattr(distance, METRIC_CLASSES[metric][0])


def metric_name(opts: Options) -> str:
    """The metric's name as Orange's widget shows it, e.g. "Euclidean (normalized)"."""
    name = METRIC_CLASSES[opts.metric][1]
    return f"{name} (normalized)" if normalizes(opts) else name


def normalizes(opts: Options) -> bool:
    """Whether the metric normalizes (only those that support it)."""
    return opts.normalize and bool(getattr(metric_class(opts.metric), "supports_normalization", 0))


def n_items(data: Any, opts: Options) -> int:
    """How many rows or columns the job compares."""
    return len(data.domain.attributes) if opts.by_columns else len(data)


def data_bytes(data: Any) -> int:
    """Memory of the data's values (``X``)."""
    x = data.X
    if hasattr(x, "indptr"):  # scipy.sparse
        return int(x.data.nbytes + x.indices.nbytes + x.indptr.nbytes)
    return int(x.nbytes)


def check_memory(data: Any, opts: Options, budget: int) -> int:
    """The memory the job needs; fails with ``payload_too_large`` above ``budget``."""
    items = n_items(data, opts)
    needed = distances_memory_bytes(items, opts.metric, data_bytes(data))
    if needed > budget:
        raise OperationError(
            ErrorCode.PAYLOAD_TOO_LARGE,
            distances_too_large_message(items, opts.axis, needed, budget),
        )
    return needed


@dataclass
class Prepared:
    """The data as the metric gets it, and the widget's warnings."""

    data: Any
    warnings: list[str] = field(default_factory=list)


def _check(data: Any, opts: Options, metric: Any) -> None:
    """Fail where Orange's widget refuses the data."""
    if data.is_sparse() and not metric.supports_sparse:
        raise OperationError(ErrorCode.BAD_REQUEST, DENSE_ONLY.format(metric_name(opts)))
    if opts.metric == "mahalanobis":
        if opts.by_columns and len(data) > MAHALANOBIS_ITEMS:
            raise OperationError(ErrorCode.BAD_REQUEST, MAHALANOBIS_LIMIT.format("rows"))
        if not opts.by_columns and len(data.domain.attributes) > MAHALANOBIS_ITEMS:
            raise OperationError(ErrorCode.BAD_REQUEST, MAHALANOBIS_LIMIT.format("columns"))


def _fix_discrete(prepared: Prepared, opts: Options, metric: Any) -> None:
    from Orange import distance

    data, domain = prepared.data, prepared.data.domain
    if not domain.has_discrete_attributes() or metric is distance.Jaccard:
        return
    if (
        (data.is_sparse() and getattr(metric, "fallback", None))
        or not metric.supports_discrete
        or opts.by_columns
    ):
        if not domain.has_continuous_attributes():
            raise OperationError(ErrorCode.BAD_REQUEST, NO_CONTINUOUS)
        prepared.warnings.append(IGNORING_DISCRETE)
        prepared.data = distance.remove_discrete_features(data, to_metas=True)


def _fix_missing(prepared: Prepared, metric: Any) -> None:
    import bottleneck as bn
    from Orange import distance

    if not metric.supports_missing and bn.anynan(prepared.data.X):
        prepared.warnings.append(IMPUTING)
        prepared.data = distance.impute(prepared.data)


def _fix_nonbinary(prepared: Prepared, metric: Any) -> None:
    from Orange import distance

    data = prepared.data
    if metric is not distance.Jaccard or data.is_sparse():
        return
    attributes = data.domain.attributes
    n_binary = sum(a.is_discrete and len(a.values) == 2 for a in attributes)
    if not n_binary:
        raise OperationError(ErrorCode.BAD_REQUEST, NO_BINARY)
    if n_binary < len(attributes):
        prepared.warnings.append(IGNORING_NONBINARY)
        prepared.data = distance.remove_nonbinary_features(data, to_metas=True)


def prepare(data: Any, opts: Options) -> Prepared:
    """Check and fix the data as Orange's Distances widget does, in its order."""
    metric = metric_class(opts.metric)
    _check(data, opts, metric)
    prepared = Prepared(data)
    if not data.domain.attributes:
        prepared.warnings.append(NO_FEATURES)
    _fix_discrete(prepared, opts, metric)
    _fix_missing(prepared, metric)
    _fix_nonbinary(prepared, metric)
    return prepared


def compute(data: Any, opts: Options, ctx: JobContext | None = None) -> Any:
    """Orange's ``DistMatrix`` of the prepared ``data``, as the widget computes it."""

    def callback(percent: float) -> None:
        if ctx is not None:
            ctx.check_cancelled()
            ctx.progress(PREPARE + (1 - 2 * PREPARE) * min(percent, 100) / 100)

    kwargs: dict[str, Any] = {"axis": 0 if opts.by_columns else 1, "impute": True}
    if normalizes(opts):
        kwargs["normalize"] = True
    return metric_class(opts.metric)(data, callback=callback, **kwargs)


@operation(JobKind.DISTANCES)
def distances_operation(ctx: JobContext) -> dict[str, Any]:
    """Distances between the rows or columns of ``inputs["data"]`` (see the
    module's documentation).

    Params: ``metric`` (a key of ``DISTANCE_METRICS``), ``normalize``
    (Euclidean and Manhattan only) and ``axis`` ("rows" or "columns").
    Result: object ``distances`` and values ``items``, ``axis``,
    ``metric`` (Orange's name), ``warnings``, ``labels`` (the column
    names, between columns), ``memory_needed_bytes``,
    ``memory_budget_bytes`` and ``seconds``.
    """
    opts = options(ctx.params)
    data = ctx.table("data")
    budget = memory_budget()
    needed = check_memory(data, opts, budget)
    started = time.perf_counter()
    items = n_items(data, opts)
    ctx.progress(0.0, f"Preparing {len(data):,} rows")
    prepared = prepare(data, opts)
    ctx.progress(PREPARE, f"Computing distances between {items:,} {opts.axis}")
    matrix = compute(prepared.data, opts, ctx)
    ctx.progress(1 - PREPARE, "Storing the distances")
    name = metric_name(opts)
    ctx.add_object(
        "distances",
        matrix,
        ObjectKind.DISTANCES,
        description=f"{name} distances between {len(matrix):,} {opts.axis}",
    )
    labels = [a.name for a in prepared.data.domain.attributes] if opts.by_columns else None
    ctx.progress(1.0, "Computed")
    return {
        "items": len(matrix),
        "axis": opts.axis,
        "metric": name,
        "warnings": prepared.warnings,
        "labels": labels,
        "memory_needed_bytes": needed,
        "memory_budget_bytes": budget,
        "seconds": time.perf_counter() - started,
    }
