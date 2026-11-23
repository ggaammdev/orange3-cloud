"""t-SNE on the machine: the ``tsne`` job.

``tsne`` embeds a table (input ``data``) in two dimensions the way Orange's
t-SNE widget does, step by step with ``Orange.projection.TSNE``: its
preprocessors (continuize, impute), then optionally normalizing and PCA
(``PCA(n_components, random_state=0)``, then the preprocessors again), the
affinities (nearest neighbours, by perplexity, or two perplexities for
"Preserve global structure"), the initial positions (PCA or spectral), and
the optimization in steps of 25 iterations: 250 with early exaggeration 12,
then 500 with the chosen exaggeration. The seed is the widget's
(``random_state=0``).

openTSNE uses all of the worker's threads (``n_jobs``; Orange's widget uses
one). Up to about 1,000 rows (exact nearest neighbours) the embedding is the
same as with one thread; above that, the approximate nearest neighbours
depend on the number of threads, so the embedding is an equally good one
but not the same numbers.

**Result.** Table ``table``: the data plus meta columns "t-SNE-x" and
"t-SNE-y", like the Data output of Orange's widget. The columns have no
``compute_value`` (in Orange's widget they hold the t-SNE model), so they
stay equal to themselves after a round trip and the add-on's later steps
replay on them.
"""

from __future__ import annotations

import time
import warnings
from dataclasses import dataclass
from typing import Any

import numpy as np

from orangecontrib.cloud.protocol.errors import ErrorCode
from orangecontrib.cloud.protocol.jobs import JobKind
from orangecontrib.cloud.server.resources import worker_threads
from orangecontrib.cloud.server.worker import JobContext, OperationError, operation

__all__ = ["RANDOM_STATE", "STEP_ITERATIONS", "Options", "embed", "options", "tsne_operation"]

#: The seed of Orange's t-SNE widget.
RANDOM_STATE = 0
#: Iterations between two progress reports (and cancellation checks), as in Orange's widget.
STEP_ITERATIONS = 25
INITIALIZATIONS = ("pca", "spectral")
METRICS = ("l2", "l1", "cosine")
MAX_PCA_COMPONENTS = 50
#: Share of the progress bar for each phase (as the weights of Orange's widget).
PREPARE, AFFINITIES, OPTIMIZE = 0.05, 0.3, 0.65

NOT_ENOUGH_ROWS = "Input data needs at least 2 rows"
NOT_ENOUGH_COLS = "Input data needs at least 2 attributes"
CONSTANT_DATA = "Input data is constant"
NO_VALID_DATA = "No projection due to no valid data"


@dataclass
class Options:
    """The job's options, checked (the settings of Orange's t-SNE widget)."""

    perplexity: float = 30
    multiscale: bool = False
    exaggeration: float = 1.0
    initialization: str = "pca"
    metric: str = "l2"
    normalize: bool = True
    pca: bool = True
    pca_components: int = 20


def _number(params: dict[str, Any], name: str, default: float, bounds: tuple[float, float]) -> Any:
    value = params.get(name, default)
    low, high = bounds
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise OperationError(
            ErrorCode.BAD_REQUEST, f"'{name}' must be a number from {low} to {high}."
        )
    return value


def _choice(params: dict[str, Any], name: str, choices: tuple[str, ...]) -> str:
    value = params.get(name, choices[0])
    if value not in choices:
        raise OperationError(ErrorCode.BAD_REQUEST, f"'{name}' must be one of {choices}.")
    return value


def options(params: dict[str, Any]) -> Options:
    """The job's options from its params; a bad one fails the job."""
    components = _number(params, "pca_components", 20, (2, MAX_PCA_COMPONENTS))
    if not isinstance(components, int):
        raise OperationError(ErrorCode.BAD_REQUEST, "'pca_components' must be an integer.")
    return Options(
        perplexity=_number(params, "perplexity", 30, (1, 500)),
        multiscale=bool(params.get("multiscale", False)),
        exaggeration=_number(params, "exaggeration", 1.0, (1, 4)),
        initialization=_choice(params, "initialization", INITIALIZATIONS),
        metric=_choice(params, "metric", METRICS),
        normalize=bool(params.get("normalize", True)),
        pca=bool(params.get("pca", True)),
        pca_components=components,
    )


def check_data(data: Any) -> None:
    """Fail as Orange's t-SNE widget refuses its input (on all rows)."""
    if len(data) < 2:
        raise OperationError(ErrorCode.BAD_REQUEST, NOT_ENOUGH_ROWS)
    if len(data.domain.attributes) < 2:
        raise OperationError(ErrorCode.BAD_REQUEST, NOT_ENOUGH_COLS)
    if data.is_sparse():
        return
    if np.all(~np.isfinite(data.X)):
        raise OperationError(ErrorCode.BAD_REQUEST, NO_VALID_DATA)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", "Degrees of freedom .*", RuntimeWarning)
        if np.nan_to_num(np.nanstd(data.X, axis=0)).sum() == 0:
            raise OperationError(ErrorCode.BAD_REQUEST, CONSTANT_DATA)


def _keep_going(iteration: int, error: float, embedding: Any) -> bool:
    """An openTSNE callback that never stops the optimization."""
    return False


def tsne_projector(n_rows: int, opts: Options, n_jobs: int) -> Any:
    """Orange's ``TSNE``, set up as Orange's widget sets it up."""
    from Orange.projection import manifold

    perplexity: Any = opts.perplexity
    if opts.multiscale:
        perplexity = min((n_rows - 1) / 3, 50), min((n_rows - 1) / 3, 500)
    return manifold.TSNE(
        n_components=2,
        # Only so that openTSNE computes the KL divergence at the end of each
        # step; it doesn't change the embedding.
        callbacks=_keep_going,
        callbacks_every_iters=STEP_ITERATIONS,
        initialization=opts.initialization,
        metric=opts.metric,
        perplexity=perplexity,
        multiscale=opts.multiscale,
        exaggeration=opts.exaggeration,
        n_jobs=n_jobs,
        random_state=RANDOM_STATE,
    )


def prepare(data: Any, tsne: Any, opts: Options) -> Any:
    """The data as Orange's widget embeds it: t-SNE's preprocessors, then
    normalized (unless sparse) and PCA, if chosen."""
    from Orange.preprocess import Normalize
    from Orange.projection import PCA

    effective = tsne.preprocess(data)
    if opts.normalize and not data.is_sparse():
        effective = Normalize()(effective)
    if opts.pca:
        components = min(opts.pca_components, len(data.domain.attributes))
        projection = PCA(n_components=components, random_state=RANDOM_STATE)(effective)
        effective = tsne.preprocess(projection(effective))
    return effective


@dataclass
class Embedded:
    """An embedding and how it was made."""

    embedding: np.ndarray
    kl_divergence: float | None
    iterations: int
    seconds: dict[str, float]


def _kl(embedding: Any) -> float | None:
    kl = getattr(embedding, "kl_divergence", None)
    return None if kl is None else float(kl)


def _status(done: int, total: int, embedding: Any) -> str:
    kl = _kl(embedding) if done else None
    text = f"Optimizing: iteration {done} of {total}"
    return text if kl is None else f"{text}, KL divergence {kl:.3f}"


def embed(data: Any, opts: Options, n_jobs: int, ctx: JobContext | None = None) -> Embedded:
    """Embed ``data`` as Orange's t-SNE widget does; ``ctx`` (optional) gets
    progress and is checked for cancelling every 25 iterations."""
    seconds: dict[str, float] = {}
    started = time.perf_counter()

    def report(fraction: float, text: str) -> None:
        if ctx is not None:
            ctx.check_cancelled()
            ctx.progress(fraction, text)

    rows = len(data)
    tsne = tsne_projector(rows, opts, n_jobs)
    report(0.0, f"Preparing {rows:,} rows")
    effective = prepare(data, tsne, opts)
    seconds["prepare"] = time.perf_counter() - started
    report(PREPARE, f"Finding the nearest neighbours of {rows:,} rows")
    started = time.perf_counter()
    affinities = tsne.compute_affinities(effective.X)
    source = effective.X if opts.initialization == "pca" else affinities.P
    embedding = tsne.prepare_embedding(affinities, tsne.compute_initialization(source))
    seconds["affinities"] = time.perf_counter() - started
    started = time.perf_counter()
    total = tsne.early_exaggeration_iter + tsne.n_iter
    done = 0
    for exaggeration, until in (
        (tsne.early_exaggeration, tsne.early_exaggeration_iter),
        (tsne.exaggeration, total),
    ):
        while done < until:
            report(PREPARE + AFFINITIES + OPTIMIZE * done / total, _status(done, total, embedding))
            step = min(STEP_ITERATIONS, until - done)
            embedding = embedding.optimize(
                step, exaggeration=exaggeration, momentum=0.8, inplace=False
            )
            done += step
    seconds["optimize"] = time.perf_counter() - started
    return Embedded(np.asarray(embedding), _kl(embedding), total, seconds)


def output_table(data: Any, embedding: np.ndarray) -> Any:
    """The data with "t-SNE-x" and "t-SNE-y" meta columns (named as Orange's
    widget names them), sharing the data's arrays."""
    from Orange.data import ContinuousVariable, Domain
    from Orange.data.util import get_unique_names

    domain = data.domain
    names = get_unique_names(
        [v.name for v in domain.class_vars + domain.metas], ["t-SNE-x", "t-SNE-y"]
    )
    names = get_unique_names(domain, names)
    columns = tuple(ContinuousVariable(name) for name in names)
    table = data.transform(Domain(domain.attributes, domain.class_vars, domain.metas + columns))
    with table.unlocked(table.metas):
        table.metas[:, -2:] = embedding
    return table


@operation(JobKind.TSNE)
def tsne_operation(ctx: JobContext) -> dict[str, Any]:
    """Embed ``inputs["data"]`` with t-SNE (see the module's documentation).

    Params: ``perplexity`` (1 to 500), ``multiscale``, ``exaggeration`` (1
    to 4), ``initialization`` ("pca" or "spectral"), ``metric`` ("l2", "l1"
    or "cosine"), ``normalize``, ``pca`` and ``pca_components`` (2 to 50).
    Result: table ``table`` and values ``rows``, ``n_jobs``, ``iterations``,
    ``kl_divergence`` and ``seconds`` (``prepare``, ``affinities``,
    ``optimize``).
    """
    opts = options(ctx.params)
    data = ctx.table("data")
    check_data(data)
    n_jobs = worker_threads()
    embedded = embed(data, opts, n_jobs, ctx)
    ctx.progress(0.98, "Storing the embedding")
    name = getattr(data, "name", "")
    ctx.add_table(
        "table",
        output_table(data, embedded.embedding),
        table_name=f"{name} t-SNE" if name else "t-SNE",
        parent_id=ctx.inputs["data"],
    )
    ctx.progress(1.0, "Embedded")
    return {
        "rows": len(data),
        "n_jobs": n_jobs,
        "iterations": embedded.iterations,
        "kl_divergence": embedded.kl_divergence,
        "seconds": embedded.seconds,
    }
