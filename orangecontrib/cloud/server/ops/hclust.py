"""Hierarchical clustering on the machine: the ``hclust`` job.

``hclust`` clusters the items (rows or columns) of a distance matrix kept on
the machine (input ``distances``, from a ``distances`` job) the way Orange's
Hierarchical Clustering widget does: SciPy's ``linkage`` on the matrix's
upper triangle (``Orange.clustering.hierarchical.dist_matrix_linkage``),
Orange's tree (``tree_from_linkage``) and its cuts: the top N clusters
(``top_clusters``) or the clusters below a share of the root's height (the
widget's "Height ratio"). Clusters are numbered C1, C2, ... in the
dendrogram's order, as in the widget.

**Memory.** The linkage needs the matrix's upper triangle (n (n - 1) / 2 x 8
bytes, half the matrix) and, except for single linkage, a working copy of
it (:func:`memory_bytes`). Before computing, the job compares that with the
memory the worker may still use and refuses with ``payload_too_large`` if
it doesn't fit.

**Result.** Object ``linkage`` (SciPy's linkage matrix, (n - 1) x 4, small):
a later job with the same linkage passes it as input ``linkage`` and only
cuts again. Between rows, table ``table``: the input ``data`` (the table the
distances were computed from) with a "Cluster" meta column, which has no
``compute_value``, so steps recorded on it replay. Object ``outputs`` (kind
``other``, small): ``domain`` (the table's, ``None`` between columns),
``labels`` (each item's cluster index, between columns), ``sizes`` (rows per
cluster), ``cut`` (the clusters' leaf ranges) and ``tree``: the top of
Orange's tree (its top clusters as leaves), to draw the dendrogram.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from orangecontrib.cloud.protocol.errors import ErrorCode
from orangecontrib.cloud.protocol.jobs import JobKind, ObjectKind
from orangecontrib.cloud.server.resources import memory_budget
from orangecontrib.cloud.server.worker import JobCancelled, JobContext, OperationError, operation

__all__ = [
    "LINKAGES",
    "clusters_at_height",
    "cut",
    "hclust_operation",
    "memory_bytes",
]

#: SciPy's linkage methods, in the order of Orange's widget.
LINKAGES = ("single", "average", "weighted", "complete", "ward")
CUTS = ("clusters", "height")
#: Most clusters of a "clusters" cut (Orange's widget allows 1,000).
MAX_CLUSTERS = 1000
#: Most leaves of the dendrogram's tree.
MAX_TOP = 200
#: Copies of the upper triangle the linkage makes (measured peak memory:
#: single linkage reads it, the others work on a copy), plus a margin.
LINKAGE_COPIES = {"single": 1.2}
LINKAGE_COPIES_DEFAULT = 2.2
#: Side of the tiles checked at a time for symmetry and infinite values.
CHECK_BLOCK = 1024

# The messages of Orange's widget.
EMPTY_MATRIX = "Distance matrix is empty."
NOT_FINITE = "Some distances are infinite"
NOT_SYMMETRIC = "Distance matrix is not symmetric."


@dataclass
class Options:
    """The job's options, checked."""

    linkage: str = "ward"
    cut: str = "clusters"
    clusters: int = 3
    height_ratio: float = 75.0  #: percent of the root's height
    top: int = 50


def _int(params: dict[str, Any], name: str, default: int, high: int) -> int:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= high:
        raise OperationError(
            ErrorCode.BAD_REQUEST, f"'{name}' must be an integer from 1 to {high}."
        )
    return value


def options(params: dict[str, Any]) -> Options:
    """The job's options from its params; a bad one fails the job."""
    linkage = params.get("linkage", "ward")
    if linkage not in LINKAGES:
        raise OperationError(ErrorCode.BAD_REQUEST, f"'linkage' must be one of {LINKAGES}.")
    how = params.get("cut", "clusters")
    if how not in CUTS:
        raise OperationError(ErrorCode.BAD_REQUEST, f"'cut' must be one of {CUTS}.")
    ratio = params.get("height_ratio", 75.0)
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 <= ratio <= 100:
        raise OperationError(ErrorCode.BAD_REQUEST, "'height_ratio' must be from 0 to 100.")
    return Options(
        linkage=linkage,
        cut=how,
        clusters=_int(params, "clusters", 3, MAX_CLUSTERS),
        height_ratio=float(ratio),
        top=_int(params, "top", 50, MAX_TOP),
    )


def memory_bytes(n_items: int, linkage: str) -> int:
    """Memory the linkage of ``n_items`` items needs: the upper triangle of
    the matrix and SciPy's working copy."""
    triangle = n_items * (n_items - 1) // 2 * 8
    return int(LINKAGE_COPIES.get(linkage, LINKAGE_COPIES_DEFAULT) * triangle)


def check_memory(n_items: int, linkage: str, axis: str, budget: int) -> int:
    """The memory the linkage needs; fails with ``payload_too_large`` above ``budget``."""
    needed = memory_bytes(n_items, linkage)
    if needed > budget:
        gb = 1024**3
        raise OperationError(
            ErrorCode.PAYLOAD_TOO_LARGE,
            f"Hierarchical clustering ({linkage} linkage) of {n_items:,} {axis} needs about "
            f"{needed / gb:.1f} GB of memory besides the distances, but the cloud machine can "
            f"give it {budget / gb:.1f} GB. Use a machine with more memory (Cloud settings…) "
            f"or fewer {axis}.",
        )
    return needed


def check_matrix(matrix: np.ndarray, ctx: JobContext | None = None) -> None:
    """Fail where Orange's widget refuses the matrix: fewer than 2 items, not
    symmetric (``np.allclose``, as Orange's ``DistMatrix.is_symmetric``) or
    infinite distances. Compares square tiles of the upper triangle with the
    lower one, so it needs little memory and reads memory in order."""
    n = len(matrix)
    if matrix.ndim != 2 or n < 2 or matrix.shape[1] != n:
        raise OperationError(ErrorCode.BAD_REQUEST, EMPTY_MATRIX)
    for i in range(0, n, CHECK_BLOCK):
        if ctx is not None:
            ctx.check_cancelled()
            ctx.progress(0.1 * (1 - ((n - i) / n) ** 2), "Checking the distances")
        for j in range(i, n, CHECK_BLOCK):
            upper = matrix[i : i + CHECK_BLOCK, j : j + CHECK_BLOCK]
            if not np.allclose(upper, matrix[j : j + CHECK_BLOCK, i : i + CHECK_BLOCK].T):
                raise OperationError(ErrorCode.BAD_REQUEST, NOT_SYMMETRIC)
            if not np.isfinite(upper).all():
                raise OperationError(ErrorCode.BAD_REQUEST, NOT_FINITE)


def linkage_matrix(matrix: np.ndarray, method: str) -> np.ndarray:
    """SciPy's linkage of the matrix's upper triangle, as Orange's
    ``dist_matrix_linkage`` computes it (without its index arrays)."""
    from scipy.cluster.hierarchy import linkage
    from scipy.spatial.distance import squareform

    condensed = squareform(np.asarray(matrix), checks=False)
    return linkage(condensed, method=method)


def clusters_at_height(root: Any, height: float) -> list:
    """The clusters of Orange's tree below ``height``, as Orange's widget cuts it."""
    from Orange.clustering.hierarchical import preorder

    lower: set = set()
    clusters = []
    for node in preorder(root):
        if node in lower:
            continue
        if node.value.height < height:
            clusters.append(node)
            lower.update(preorder(node))
    return clusters


def cut(root: Any, opts: Options) -> list:
    """The clusters of the cut, in the dendrogram's order (Orange's C1, C2, ...)."""
    from Orange.clustering.hierarchical import top_clusters

    if opts.cut == "clusters":
        clusters = top_clusters(root, opts.clusters)
    else:
        clusters = clusters_at_height(root, opts.height_ratio * root.value.height / 100)
    return sorted(clusters, key=lambda c: c.value.first)


def labels_of(root: Any, clusters: list) -> np.ndarray:
    """Each item's cluster index (in ``clusters``)."""
    from Orange.clustering.hierarchical import leaves

    order = np.array([leaf.value.index for leaf in leaves(root)])
    labels = np.empty(len(order), dtype=float)
    for i, cluster in enumerate(clusters):
        labels[order[cluster.value.first : cluster.value.last]] = i
    return labels


def top_tree(root: Any, n_leaves: int) -> Any:
    """The top of Orange's tree: its ``n_leaves`` top clusters become leaves."""
    from Orange.clustering.hierarchical import prune, top_clusters

    top = {id(node) for node in top_clusters(root, n_leaves)}
    return prune(root, condition=lambda node: id(node) in top)


def clustered_table(data: Any, labels: np.ndarray, n_clusters: int) -> Any:
    """``data`` with a "Cluster" meta column (sharing the data's arrays)."""
    from Orange.data import DiscreteVariable, Domain
    from Orange.data.util import get_unique_names

    domain = data.domain
    cluster_var = DiscreteVariable(
        get_unique_names(domain, "Cluster"), values=[f"C{i + 1}" for i in range(n_clusters)]
    )
    out_domain = Domain(domain.attributes, domain.class_vars, domain.metas + (cluster_var,))
    table = data.transform(out_domain)
    with table.unlocked(table.metas):
        table.metas[:, -1] = labels
    return table


def _linkage(ctx: JobContext, matrix: np.ndarray, opts: Options, axis: str) -> tuple:
    """The linkage matrix: the input ``linkage`` if given, else computed and
    stored. Also returns the memory needed and the seconds it took."""
    n = len(matrix)
    if "linkage" in ctx.inputs:
        z = ctx.object("linkage")
        if not isinstance(z, np.ndarray) or z.shape != (n - 1, 4):
            raise OperationError(ErrorCode.BAD_REQUEST, "'linkage' isn't this matrix's linkage.")
        return z, 0, 0.0
    needed = check_memory(n, opts.linkage, axis, memory_budget())
    check_matrix(matrix, ctx)
    ctx.progress(0.1, f"{opts.linkage.capitalize()} linkage of {n:,} {axis}")
    started = time.perf_counter()
    z = linkage_matrix(matrix, opts.linkage)
    seconds = time.perf_counter() - started
    if ctx.cancelled:  # SciPy can't be stopped midway
        raise JobCancelled()
    ctx.add_object(
        "linkage", z, ObjectKind.OTHER, description=f"{opts.linkage} linkage of {n:,} {axis}"
    )
    return z, needed, seconds


def _store(ctx: JobContext, matrix: Any, labels: np.ndarray, n_clusters: int) -> Any:
    """Store the clustered table (between rows); returns its domain or ``None``."""
    if getattr(matrix, "axis", 1) == 0:
        return None
    data = ctx.table("data")
    if len(data) != len(matrix):
        raise OperationError(
            ErrorCode.BAD_REQUEST,
            f"The data has {len(data):,} rows but the distances {len(matrix):,}.",
        )
    ctx.progress(0.9, "Storing the clusters")
    table = clustered_table(data, labels, n_clusters)
    name = getattr(data, "name", "")
    label = f"{n_clusters} clusters"
    ctx.add_table(
        "table",
        table,
        table_name=f"{name} {label}" if name else label,
        parent_id=ctx.inputs["data"],
    )
    return table.domain


@operation(JobKind.HCLUST)
def hclust_operation(ctx: JobContext) -> dict[str, Any]:
    """Hierarchical clustering of ``inputs["distances"]`` (see the module's
    documentation).

    Inputs: ``distances``, ``data`` (between rows) and, optionally,
    ``linkage`` (from an earlier job on the same distances and linkage).
    Params: ``linkage`` (a :data:`LINKAGES`), ``cut`` ("clusters" or
    "height"), ``clusters``, ``height_ratio`` (percent) and ``top``.
    Result: object ``linkage`` (unless given), table ``table`` (between
    rows), object ``outputs`` and values ``items``, ``axis``, ``clusters``,
    ``height`` (the root's), ``cut_height``, ``memory_needed_bytes``,
    ``linkage_reused``, ``linkage_seconds`` and ``seconds``.
    """
    from Orange.clustering.hierarchical import tree_from_linkage

    started = time.perf_counter()
    opts = options(ctx.params)
    matrix = ctx.object("distances")
    axis = "columns" if getattr(matrix, "axis", 1) == 0 else "rows"
    z, needed, linkage_seconds = _linkage(ctx, matrix, opts, axis)
    ctx.progress(0.8, "Cutting the tree")
    root = tree_from_linkage(z)
    clusters = cut(root, opts)
    labels = labels_of(root, clusters)
    domain = _store(ctx, matrix, labels, len(clusters)) if clusters else None
    outputs = {
        "domain": domain,
        "labels": labels if axis == "columns" else None,
        "sizes": [c.value.last - c.value.first for c in clusters],
        "cut": [c.value.range for c in clusters],
        "tree": top_tree(root, max(opts.top, min(len(clusters), MAX_TOP))),
    }
    ctx.add_object("outputs", outputs, ObjectKind.OTHER, description="Hierarchical clustering")
    ctx.progress(1.0, "Clustered")
    height = float(root.value.height)
    return {
        "items": len(matrix),
        "axis": axis,
        "linkage": opts.linkage,
        "clusters": len(clusters),
        "height": height,
        "cut_height": height * opts.height_ratio / 100 if opts.cut == "height" else None,
        "memory_needed_bytes": needed,
        "linkage_reused": "linkage" in ctx.inputs,
        "linkage_seconds": linkage_seconds,
        "seconds": time.perf_counter() - started,
    }
