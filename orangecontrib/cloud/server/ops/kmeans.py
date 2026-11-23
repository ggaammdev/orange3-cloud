"""k-Means clustering on the machine: the ``kmeans`` job.

``kmeans`` clusters a table (input ``data``) for one or more numbers of
clusters (``params["ks"]``) the way Orange's k-Means widget does: the data
is normalized (``normalize``), continuized and imputed as the widget does,
and ``Orange.clustering.KMeans`` runs with the widget's seed.

**Silhouettes**, always on all rows:

* up to ``silhouette_rows`` rows (default :data:`SILHOUETTE_ROWS`): the
  exact silhouette, the numbers of scikit-learn's ``silhouette_samples``
  that Orange's widget uses. The distances between rows don't depend on k,
  so they are computed once, in chunks, for all k (:func:`exact_silhouettes`),
  which equals ``silhouette_samples`` for each k;
* above it: the **simplified silhouette** (:func:`simplified_silhouette`):
  each row's distance to its own cluster's centre against the nearest other
  centre. Its numbers differ from the exact silhouette, so its column is
  named "Simplified silhouette".

**Result.** For each k that didn't fail, a table ``k<k>`` (e.g. ``k3``): the
data with a cluster column and a silhouette column as meta attributes, like
the annotated output of Orange's widget (the silhouette scaled as there,
``arctan(s) / pi + 0.5``). The cluster column has no ``compute_value`` (in
Orange's widget it holds the model), so it stays equal to itself after a
round trip and the add-on's later steps replay on it. Object ``outputs``
(kind ``other``): ``{k: {"domain": the table's domain, "centroids": Orange's
centroids table}}``, small, one download for every k.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from orangecontrib.cloud.protocol.errors import ErrorCode
from orangecontrib.cloud.protocol.jobs import JobKind, ObjectKind
from orangecontrib.cloud.server.worker import JobCancelled, JobContext, OperationError, operation

__all__ = [
    "EXACT",
    "RANDOM_STATE",
    "SILHOUETTE_ROWS",
    "SIMPLIFIED",
    "exact_silhouettes",
    "kmeans_operation",
    "simplified_silhouette",
]

#: The seed of Orange's k-Means widget.
RANDOM_STATE = 0
#: Default row limit for the exact silhouette.
SILHOUETTE_ROWS = 100_000
#: The silhouette's kinds, as the values' ``silhouette`` says.
EXACT, SIMPLIFIED = "exact", "simplified"
#: Column names of the silhouette, per kind.
SILHOUETTE_NAMES = {EXACT: "Silhouette", SIMPLIFIED: "Simplified silhouette"}
INIT_METHODS = ("k-means++", "random")
#: Most numbers of clusters in one job.
MAX_KS = 50
#: Memory for one chunk of distances in the exact silhouette.
WORKING_MEMORY_MB = 256
#: Rows per block in the simplified silhouette.
BLOCK_ROWS = 65_536

NOT_ENOUGH_DATA = "Too few ({}) unique data instances for {} clusters"
NO_FEATURES = "Data is missing features."


@dataclass
class Options:
    """The job's options, checked."""

    ks: list[int]
    init: str = "k-means++"
    n_init: int = 10
    max_iter: int = 300
    normalize: bool = True
    silhouette_rows: int = SILHOUETTE_ROWS


def _int(params: dict[str, Any], name: str, default: int, low: int) -> int:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < low:
        raise OperationError(ErrorCode.BAD_REQUEST, f"'{name}' must be an integer >= {low}.")
    return value


def options(params: dict[str, Any]) -> Options:
    """The job's options from its params; a bad one fails the job."""
    ks = params.get("ks")
    if (
        not isinstance(ks, list)
        or not 0 < len(ks) <= MAX_KS
        or not all(isinstance(k, int) and not isinstance(k, bool) and k >= 2 for k in ks)
    ):
        raise OperationError(
            ErrorCode.BAD_REQUEST, f"'ks' must be a list of 1 to {MAX_KS} integers >= 2."
        )
    init = params.get("init", "k-means++")
    if init not in INIT_METHODS:
        raise OperationError(ErrorCode.BAD_REQUEST, f"'init' must be one of {INIT_METHODS}.")
    return Options(
        ks=sorted(set(ks)),
        init=init,
        n_init=_int(params, "n_init", 10, 1),
        max_iter=_int(params, "max_iter", 300, 1),
        normalize=bool(params.get("normalize", True)),
        silhouette_rows=_int(params, "silhouette_rows", SILHOUETTE_ROWS, 0),
    )


@dataclass
class _Clustering:
    """One k's clustering and its silhouettes."""

    k: int
    model: Any = None  #: Orange's ``KMeansModel``
    failed: str | None = None
    seconds: float = 0.0
    silhouettes: np.ndarray | None = None  #: per row, unscaled

    @property
    def score(self) -> float | None:
        """The mean silhouette (what Orange's widget shows for k)."""
        return None if self.silhouettes is None else float(np.mean(self.silhouettes))


class NotEnoughData(ValueError):
    """More clusters than rows."""


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, NotEnoughData):
        return str(exc)
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def preprocess(data: Any, normalize: bool) -> tuple[Any, bool]:
    """The data as Orange's k-Means widget clusters it: normalized (unless
    sparse), then with ``KMeans.preprocessors`` (continuize, impute). Also
    returns whether it was normalized."""
    import scipy.sparse as sp
    from Orange.clustering import KMeans
    from Orange.preprocess import Normalize

    normalized = normalize and not sp.issparse(data.X)
    if normalized:
        data = Normalize()(data)
    for preprocessor in KMeans.preprocessors:
        data = preprocessor(data)
    return data, normalized


def _fit(prepared: Any, k: int, opts: Options, original_domain: Any) -> Any:
    """Orange's ``KMeansModel`` for ``k``, as Orange's widget fits it."""
    from Orange.clustering import KMeans

    if k > len(prepared):
        raise NotEnoughData(NOT_ENOUGH_DATA.format(len(prepared), k))
    model = KMeans(
        n_clusters=k,
        init=opts.init,
        n_init=opts.n_init,
        max_iter=opts.max_iter,
        random_state=RANDOM_STATE,
        preprocessors=[],
    ).get_model(prepared)
    model.original_domain = original_domain
    return model


def _cluster(ctx: JobContext, prepared: Any, opts: Options, original_domain: Any) -> list:
    """Fit every k (progress 0 to 0.7); a k that fails says why."""
    clusterings = []
    for i, k in enumerate(opts.ks):
        ctx.check_cancelled()
        ctx.progress(0.7 * i / len(opts.ks), f"Clustering {len(prepared):,} rows, k = {k}")
        clustering = _Clustering(k)
        started = time.perf_counter()
        try:
            clustering.model = _fit(prepared, k, opts, original_domain)
        except MemoryError:
            raise
        except Exception as exc:  # noqa: BLE001 - job runner: reported as this k's error
            clustering.failed = _error_text(exc)
        clustering.seconds = time.perf_counter() - started
        clusterings.append(clustering)
    if ctx.cancelled:  # scikit-learn can't be stopped midway
        raise JobCancelled()
    return clusterings


# --------------------------------------------------------------------------
# Silhouettes


def _encoded(labels: np.ndarray, n_rows: int) -> tuple[np.ndarray, int]:
    """Labels as 0..m-1 (as scikit-learn encodes them) and m; fails, as
    ``silhouette_samples`` does, unless 2 <= m <= n_rows - 1."""
    _, codes = np.unique(labels, return_inverse=True)
    m = int(codes.max()) + 1 if len(codes) else 0
    if not 2 <= m <= n_rows - 1:
        raise ValueError(
            f"Number of labels is {m}. Valid values are 2 to n_samples - 1 (inclusive)"
        )
    return codes.reshape(-1), m


def _from_cluster_sums(sums: np.ndarray, codes: np.ndarray, m: int) -> np.ndarray:
    """Silhouettes from each row's summed distances to each cluster, as
    scikit-learn's ``silhouette_samples`` computes them."""
    n = len(codes)
    freqs = np.bincount(codes, minlength=m)
    rows = np.arange(n)
    intra = sums[rows, codes].copy()
    sums[rows, codes] = np.inf
    sums /= freqs
    inter = sums.min(axis=1)
    denom = (freqs - 1).take(codes, mode="clip")
    with np.errstate(divide="ignore", invalid="ignore"):
        intra /= denom
    silhouettes = inter - intra
    with np.errstate(divide="ignore", invalid="ignore"):
        silhouettes /= np.maximum(intra, inter)
    return np.nan_to_num(silhouettes)


def exact_silhouettes(
    X: Any, labelings: list[np.ndarray], on_chunk: Any = None
) -> list[np.ndarray | str]:
    """The exact silhouette of each labeling of the rows of ``X`` (Euclidean,
    the numbers of scikit-learn's ``silhouette_samples``), from one pass over
    the distances between rows; an error text for a labeling it can't score.

    ``on_chunk(done_rows)`` is called after each chunk of rows (for progress
    and cancelling)."""
    from sklearn.metrics import pairwise_distances_chunked

    n = X.shape[0]
    results: list[np.ndarray | str] = []
    encoded = []  # (index in results, codes, m, first column)
    columns = 0
    for labels in labelings:
        try:
            codes, m = _encoded(np.asarray(labels), n)
        except ValueError as exc:
            results.append(str(exc))
            continue
        encoded.append((len(results), codes, m, columns))
        results.append("")
        columns += m
    if not encoded:
        return results
    onehot = np.zeros((n, columns))
    for _, codes, _, first in encoded:
        onehot[np.arange(n), first + codes] = 1
    sums = np.empty((n, columns))
    start = 0
    chunks = pairwise_distances_chunked(
        X,
        reduce_func=lambda D, _start: D @ onehot,
        metric="euclidean",
        working_memory=WORKING_MEMORY_MB,
    )
    for chunk in chunks:
        sums[start : start + len(chunk)] = chunk
        start += len(chunk)
        if on_chunk is not None:
            on_chunk(start)
    for i, codes, m, first in encoded:
        results[i] = _from_cluster_sums(sums[:, first : first + m].copy(), codes, m)
    return results


def simplified_silhouette(X: Any, labels: np.ndarray, centers: np.ndarray) -> np.ndarray:
    """The simplified silhouette of each row: ``(b - a) / max(a, b)``, where
    ``a`` is the row's distance to its own cluster's centre and ``b`` to the
    nearest other centre; 0 in clusters of one row, as in the exact one."""
    from sklearn.metrics.pairwise import euclidean_distances

    labels = np.asarray(labels).reshape(-1)
    n = X.shape[0]
    out = np.empty(n)
    for start in range(0, n, BLOCK_ROWS):
        own = labels[start : start + BLOCK_ROWS]
        dist = euclidean_distances(X[start : start + BLOCK_ROWS], centers)
        rows = np.arange(len(own))
        a = dist[rows, own].copy()
        dist[rows, own] = np.inf
        b = dist.min(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            out[start : start + len(own)] = np.nan_to_num((b - a) / np.maximum(a, b))
    sizes = np.bincount(labels, minlength=len(centers))
    out[sizes[labels] <= 1] = 0
    return out


def _silhouettes(ctx: JobContext, prepared: Any, clusterings: list, kind: str) -> None:
    """Fill in each fitted clustering's silhouettes (progress 0.7 to 0.9); a
    clustering that can't be scored fails, as in Orange's widget."""
    fitted = [c for c in clusterings if c.failed is None]
    n = len(prepared)
    if kind == EXACT:

        def on_chunk(done: int) -> None:
            ctx.check_cancelled()
            ctx.progress(0.7 + 0.2 * done / n, f"Exact silhouette: {done:,} of {n:,} rows")

        ctx.progress(0.7, "Exact silhouette")
        scores = exact_silhouettes(prepared.X, [c.model.labels for c in fitted], on_chunk)
    else:
        scores = []
        for i, c in enumerate(fitted):
            ctx.check_cancelled()
            ctx.progress(0.7 + 0.2 * i / len(fitted), f"Simplified silhouette, k = {c.k}")
            scores.append(simplified_silhouette(prepared.X, c.model.labels, c.model.centroids))
    for clustering, score in zip(fitted, scores, strict=True):
        if isinstance(score, str):
            clustering.failed = score
        else:
            clustering.silhouettes = score


# --------------------------------------------------------------------------
# Outputs


def _scaled(silhouettes: np.ndarray, labels: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    """Per row and per cluster, the silhouettes scaled as Orange's widget
    shows them (``arctan(s) / pi + 0.5``; a cluster's is its rows' mean)."""
    scores = np.arctan(silhouettes) / np.pi + 0.5
    per_cluster = np.zeros(k)
    for i in range(k):
        in_cluster = labels == i
        if in_cluster.any():
            per_cluster[i] = np.mean(scores[in_cluster])
    return scores, per_cluster


def _centroid_attributes(model: Any, domain: Any) -> list:
    """The centroids' columns, as Orange's widget names them."""
    from Orange.preprocess.impute import ReplaceUnknowns

    attributes = set(domain.attributes)
    return [
        attr.compute_value.variable
        if isinstance(attr.compute_value, ReplaceUnknowns)
        and attr.compute_value.variable in attributes
        else attr
        for attr in model.domain.attributes
    ]


def outputs(data: Any, clustering: _Clustering, kind: str) -> tuple[Any, Any]:
    """The data with the cluster and silhouette columns (sharing the data's
    arrays) and the centroids table, as Orange's k-Means widget makes them."""
    from Orange.data import ContinuousVariable, DiscreteVariable, Domain, Table
    from Orange.data.util import get_unique_names

    model, k = clustering.model, clustering.k
    labels = np.asarray(model.labels).reshape(-1)
    domain = data.domain
    cluster_var = DiscreteVariable(
        get_unique_names(domain, "Cluster"), values=[f"C{i + 1}" for i in range(k)]
    )
    silhouette_var = ContinuousVariable(get_unique_names(domain, SILHOUETTE_NAMES[kind]))
    scores, per_cluster = _scaled(clustering.silhouettes, labels, k)
    out_domain = Domain(
        domain.attributes, domain.class_vars, domain.metas + (cluster_var, silhouette_var)
    )
    table = data.transform(out_domain)  # shares X; the new metas are empty
    with table.unlocked(table.metas):
        table.metas[:, -2] = labels.astype(float)
        table.metas[:, -1] = scores
    centroid_domain = Domain(
        _centroid_attributes(model, domain), [], domain.metas + (cluster_var, silhouette_var)
    )
    centroid_metas = np.hstack(
        (np.full((k, len(domain.metas)), np.nan), np.arange(k).reshape(k, 1), per_cluster[:, None])
    )
    centroids = Table.from_numpy(centroid_domain, model.centroids.copy(), None, centroid_metas)
    name = getattr(data, "name", "")
    centroids.name = f"{name} centroids" if name else "centroids"
    return table, centroids


def _store(ctx: JobContext, data: Any, clusterings: list, kind: str) -> None:
    """Store each clustering's table and the ``outputs`` object."""
    stored = {}
    name = getattr(data, "name", "")
    done = [c for c in clusterings if c.failed is None]
    for i, clustering in enumerate(done):
        ctx.check_cancelled()
        ctx.progress(0.9 + 0.1 * i / len(done), f"Storing the clusters, k = {clustering.k}")
        table, centroids = outputs(data, clustering, kind)
        label = f"clusters, k={clustering.k}"
        ctx.add_table(
            f"k{clustering.k}",
            table,
            table_name=f"{name} {label}" if name else label,
            parent_id=ctx.inputs["data"],
        )
        stored[clustering.k] = {"domain": table.domain, "centroids": centroids}
    if stored:
        ctx.add_object(
            "outputs",
            stored,
            ObjectKind.OTHER,
            description=f"k-Means domains and centroids for k = {', '.join(map(str, stored))}",
        )


@operation(JobKind.KMEANS)
def kmeans_operation(ctx: JobContext) -> dict[str, Any]:
    """Cluster ``inputs["data"]`` for each k in ``params["ks"]`` (see the
    module's documentation).

    Params: ``ks``, ``init`` ("k-means++" or "random"), ``n_init``,
    ``max_iter``, ``normalize`` and ``silhouette_rows``.
    Result: tables ``k<k>``, object ``outputs`` and values ``rows``,
    ``silhouette`` ("exact" or "simplified"), ``silhouette_rows``,
    ``normalized``, ``clusterings`` (``k``, ``score``, ``failed``,
    ``iterations``, ``seconds`` per k), ``fit_seconds`` and
    ``silhouette_seconds``.
    """
    opts = options(ctx.params)
    data = ctx.table("data")
    if not data.domain.attributes:
        raise OperationError(ErrorCode.BAD_REQUEST, NO_FEATURES)
    rows = len(data)
    kind = EXACT if rows <= opts.silhouette_rows else SIMPLIFIED
    ctx.progress(0.0, f"Preparing {rows:,} rows")
    prepared, normalized = preprocess(data, opts.normalize)
    started = time.monotonic()
    clusterings = _cluster(ctx, prepared, opts, data.domain)
    fit_seconds = time.monotonic() - started
    started = time.monotonic()
    _silhouettes(ctx, prepared, clusterings, kind)
    silhouette_seconds = time.monotonic() - started
    del prepared
    _store(ctx, data, clusterings, kind)
    ctx.progress(1.0, "Clustered")
    return {
        "rows": rows,
        "silhouette": kind,
        "silhouette_rows": opts.silhouette_rows,
        "normalized": normalized,
        "clusterings": [
            {
                "k": c.k,
                "score": c.score,
                "failed": c.failed,
                "iterations": int(c.model.projector.n_iter_) if c.failed is None else None,
                "seconds": c.seconds,
            }
            for c in clusterings
        ],
        "fit_seconds": fit_seconds,
        "silhouette_seconds": silhouette_seconds,
    }
