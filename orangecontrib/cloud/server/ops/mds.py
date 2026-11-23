"""Multidimensional scaling on the machine: the ``mds`` job.

``mds`` places the items of a distance matrix in two dimensions the way
Orange's MDS widget does. The matrix is either kept on the machine (input
``distances``, from a ``distances`` job) or computed from a table (input
``data`` alone): Euclidean distances of the data after
``Orange.projection.MDS``'s preprocessors (continuize, impute), as the
widget computes them. The initial positions are Torgerson's classical MDS
("PCA", ``Orange.projection.manifold.torgerson``) or random. Then, as the
widget does, ``Orange.projection.MDS`` (scikit-learn's SMACOF) runs in steps
of 25 iterations, each starting from the last positions, until the maximum
number of iterations or until the stress (divided by the sum of the points'
distances from the origin) improves by less than ``MDS``'s ``eps``. The
steps give the progress and the chances to cancel.

**Memory.** SMACOF works with several n x n matrices besides the distances
(:func:`~orangecontrib.cloud.protocol.memory.mds_memory_bytes`). Before computing,
the job compares that with the memory the worker may still use and refuses
with ``payload_too_large`` if it doesn't fit.

**Result.** With input ``data``, table ``table``: the data plus meta
columns "mds-x" and "mds-y", as in the widget's Data output. The columns
have no ``compute_value``, so steps recorded on the table replay. Without
it (distances between columns), object ``embedding``: the positions, an
n x 2 array.
"""

from __future__ import annotations

import time
import warnings
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from orangecontrib.cloud.protocol.errors import ErrorCode
from orangecontrib.cloud.protocol.jobs import JobKind, ObjectKind
from orangecontrib.cloud.protocol.memory import mds_memory_bytes, mds_too_large_message
from orangecontrib.cloud.server.ops.distances import data_bytes
from orangecontrib.cloud.server.ops.hclust import check_matrix
from orangecontrib.cloud.server.resources import memory_budget
from orangecontrib.cloud.server.worker import JobContext, OperationError, operation

__all__ = [
    "INITIALIZATIONS",
    "STEP_ITERATIONS",
    "Options",
    "euclidean",
    "initial_positions",
    "kruskal_stress",
    "mds_operation",
    "optimize",
]

#: "PCA" (Torgerson) and "random", the initializations of Orange's widget.
INITIALIZATIONS = ("pca", "random")
#: Iterations per step: the widget's default refresh rate ("Every 25 steps").
STEP_ITERATIONS = 25
MAX_ITERATIONS = 10_000
#: Seed of the random initial positions (Orange's widget uses no seed).
RANDOM_STATE = 0
#: Rows of the matrix compared at a time when computing the stress.
STRESS_BLOCK = 2048
#: Share of the progress bar for the distances and the initial positions.
PREPARE = 0.1

# The messages of Orange's widget.
NOT_ENOUGH_ROWS = "Input data needs at least 2 rows"
MATRIX_TOO_SMALL = "Input matrix must be at least 2x2"
NO_ATTRIBUTES = "Data has no attributes"
MISMATCHING = "Data and distances dimensions do not match."


@dataclass
class Options:
    """The job's options, checked (Orange's MDS widget's settings)."""

    initialization: str = "pca"
    max_iter: int = 300
    step: int = STEP_ITERATIONS


def _int(params: dict[str, Any], name: str, default: int) -> int:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_ITERATIONS:
        raise OperationError(
            ErrorCode.BAD_REQUEST, f"'{name}' must be an integer from 1 to {MAX_ITERATIONS}."
        )
    return value


def options(params: dict[str, Any]) -> Options:
    """The job's options from its params; a bad one fails the job."""
    initialization = params.get("initialization", "pca")
    if initialization not in INITIALIZATIONS:
        raise OperationError(
            ErrorCode.BAD_REQUEST, f"'initialization' must be one of {INITIALIZATIONS}."
        )
    return Options(
        initialization=initialization,
        max_iter=_int(params, "max_iter", 300),
        step=_int(params, "step", STEP_ITERATIONS),
    )


def check_memory(n_items: int, axis: str, from_data: Any, budget: int) -> int:
    """The memory the job needs (from the table ``from_data``, or from
    distances if ``None``); fails with ``payload_too_large`` above ``budget``."""
    needed = mds_memory_bytes(n_items, None if from_data is None else data_bytes(from_data))
    if needed > budget:
        raise OperationError(
            ErrorCode.PAYLOAD_TOO_LARGE, mds_too_large_message(n_items, axis, needed, budget)
        )
    return needed


def euclidean(data: Any) -> Any:
    """The distances Orange's widget computes from data: Euclidean, after
    ``MDS``'s preprocessors. Fails where the widget refuses the data."""
    from Orange.distance import Euclidean
    from Orange.projection import MDS

    if len(data) < 2:
        raise OperationError(ErrorCode.BAD_REQUEST, NOT_ENOUGH_ROWS)
    if not data.domain.attributes:
        raise OperationError(ErrorCode.BAD_REQUEST, NO_ATTRIBUTES)
    return Euclidean(MDS().preprocess(data))


def initial_positions(matrix: Any, initialization: str) -> np.ndarray:
    """The starting positions: Torgerson's (the widget's "PCA") or random."""
    from Orange.projection.manifold import torgerson

    if initialization == "pca":
        return torgerson(matrix)
    return np.random.RandomState(RANDOM_STATE).rand(len(matrix), 2)


@dataclass
class Optimized:
    """Positions and how they were found."""

    embedding: np.ndarray
    iterations: int = 0  #: SMACOF's iterations
    steps: int = 0
    converged: bool = False  #: stopped before the maximum number of iterations


def _smacof_step(matrix: Any, init: np.ndarray, iterations: int) -> tuple[Any, float]:
    """One step of the widget: ``MDS`` for ``iterations`` from ``init``."""
    from Orange.projection import MDS

    mds = MDS(
        dissimilarity="precomputed",
        n_components=2,
        n_init=1,
        max_iter=iterations,
        init_type="PCA",
        init_data=init,
    )
    with warnings.catch_warnings():
        # scikit-learn's notices about its future parameter names
        warnings.simplefilter("ignore", FutureWarning)
        return mds(matrix), mds.params["eps"]


def optimize(
    matrix: Any, init: np.ndarray, opts: Options, ctx: JobContext | None = None
) -> Optimized:
    """SMACOF from ``init`` in steps, stopping as Orange's widget stops
    (``run_mds``); ``ctx`` (optional) gets progress and is checked for
    cancelling between steps."""
    result = Optimized(init)
    done, old_stress = 0, np.finfo(float).max
    while True:
        if ctx is not None:
            ctx.check_cancelled()
            text = f"Optimizing: iteration {done} of at most {opts.max_iter}"
            ctx.progress(PREPARE + (1 - 2 * PREPARE) * done / opts.max_iter, text)
        step = min(opts.max_iter - done, opts.step)
        fit, eps = _smacof_step(matrix, result.embedding, step)
        done += step
        result.embedding, stress = fit.embedding_, fit.stress_
        result.iterations += int(fit.n_iter_)
        result.steps += 1
        norm = np.sqrt(np.sum(result.embedding**2, axis=1)).sum()
        if norm > 0:
            stress /= norm
        if done >= opts.max_iter or stress == 0 or (old_stress - stress) < eps:
            result.converged = done < opts.max_iter
            return result
        old_stress = stress


def kruskal_stress(embedding: np.ndarray, matrix: Any) -> float:
    """Kruskal's stress, as Orange's widget shows it, computed in blocks of
    rows so it needs little memory."""
    from scipy.spatial.distance import cdist

    total = squares = 0.0
    for i in range(0, len(embedding), STRESS_BLOCK):
        rows = np.asarray(matrix[i : i + STRESS_BLOCK])
        total += float(np.square(cdist(embedding[i : i + STRESS_BLOCK], embedding) - rows).sum())
        squares += float(np.square(rows).sum())
    return float(np.sqrt(total / (squares or 1)))


def output_table(data: Any, embedding: np.ndarray) -> Any:
    """The data with "mds-x" and "mds-y" meta columns (named as Orange's
    widget names them), sharing the data's arrays."""
    from Orange.data import ContinuousVariable, Domain
    from Orange.data.util import get_unique_names

    domain = data.domain
    names = get_unique_names(domain, ["mds-x", "mds-y"])
    columns = tuple(ContinuousVariable(name) for name in names)
    table = data.transform(Domain(domain.attributes, domain.class_vars, domain.metas + columns))
    with table.unlocked(table.metas):
        table.metas[:, -2:] = embedding
    return table


@dataclass
class _Input:
    """The matrix to embed, the table for the output, and the memory check."""

    matrix: Any
    data: Any
    axis: str
    seconds: dict[str, float] = field(default_factory=dict)
    memory_needed: int = 0
    memory_budget: int = 0


def _input(ctx: JobContext) -> _Input:
    """The distances (given, or computed from ``data``) and the output table."""
    budget = memory_budget()
    data = ctx.table("data") if "data" in ctx.inputs else None
    if "distances" not in ctx.inputs:
        if data is None:
            raise OperationError(ErrorCode.BAD_REQUEST, "The job needs 'distances' or 'data'.")
        needed = check_memory(len(data), "rows", data, budget)
        started = time.perf_counter()
        ctx.progress(0.0, f"Computing the distances between {len(data):,} rows")
        matrix = euclidean(data)
        seconds = {"distances": time.perf_counter() - started}
        return _Input(matrix, data, "rows", seconds, needed, budget)
    matrix = ctx.object("distances")
    axis = "columns" if getattr(matrix, "axis", 1) == 0 else "rows"
    if matrix.ndim == 2 and len(matrix) < 2:
        raise OperationError(ErrorCode.BAD_REQUEST, MATRIX_TOO_SMALL)
    if data is not None and len(data) != len(matrix):
        raise OperationError(ErrorCode.BAD_REQUEST, MISMATCHING)
    needed = check_memory(len(matrix), axis, None, budget)
    check_matrix(matrix)
    return _Input(matrix, data, axis, {}, needed, budget)


def _store(ctx: JobContext, given: _Input, embedding: np.ndarray) -> None:
    """Store the output: the table with the positions, or the positions."""
    if given.data is None:
        ctx.add_object(
            "embedding", embedding, ObjectKind.OTHER, description=f"MDS of {len(embedding)} items"
        )
        return
    name = getattr(given.data, "name", "")
    ctx.add_table(
        "table",
        output_table(given.data, embedding),
        table_name=f"{name} MDS" if name else "MDS",
        parent_id=ctx.inputs["data"],
    )


@operation(JobKind.MDS)
def mds_operation(ctx: JobContext) -> dict[str, Any]:
    """MDS of ``inputs["distances"]`` or of ``inputs["data"]`` (see the
    module's documentation).

    Inputs: ``distances`` and/or ``data`` (the output's table; without
    ``distances``, the data to compute Euclidean distances from). Params:
    ``initialization`` ("pca" or "random"), ``max_iter`` and ``step``
    (iterations per step, default 25). Result: table ``table`` (with
    ``data``) or object ``embedding``, and values ``items``, ``axis``,
    ``iterations``, ``steps``, ``converged``, ``stress`` (Kruskal's),
    ``memory_needed_bytes``, ``memory_budget_bytes`` and ``seconds``
    (``distances``, ``initialization``, ``optimize``).
    """
    opts = options(ctx.params)
    given = _input(ctx)
    n = len(given.matrix)
    started = time.perf_counter()
    ctx.progress(PREPARE / 2, f"Initial positions of {n:,} {given.axis}")
    init = initial_positions(given.matrix, opts.initialization)
    given.seconds["initialization"] = time.perf_counter() - started
    started = time.perf_counter()
    optimized = optimize(given.matrix, init, opts, ctx)
    given.seconds["optimize"] = time.perf_counter() - started
    ctx.progress(1 - PREPARE, "Computing the stress")
    stress = kruskal_stress(optimized.embedding, given.matrix)
    ctx.progress(1 - PREPARE / 2, "Storing the positions")
    _store(ctx, given, optimized.embedding)
    ctx.progress(1.0, "Embedded")
    return {
        "items": n,
        "axis": given.axis,
        "iterations": optimized.iterations,
        "steps": optimized.steps,
        "converged": optimized.converged,
        "stress": stress,
        "memory_needed_bytes": given.memory_needed,
        "memory_budget_bytes": given.memory_budget,
        "seconds": given.seconds,
    }
