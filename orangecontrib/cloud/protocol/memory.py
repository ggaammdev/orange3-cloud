"""Memory estimates that both sides check before a job that may not fit:
the ``distances`` and ``mds`` jobs, whose matrices grow with the square of
the number of items."""

from __future__ import annotations

__all__ = [
    "DISTANCE_DATA_COPIES",
    "DISTANCE_METRICS",
    "MDS_MATRICES",
    "distances_memory_bytes",
    "distances_too_large_message",
    "mds_memory_bytes",
    "mds_too_large_message",
]

#: The metrics of the ``distances`` job (Orange's ``Orange.distance``
#: classes), with the memory each needs while computing, in matrices of
#: n x n x 8 bytes (measured peaks of Orange's code, rounded up).
DISTANCE_METRICS: dict[str, float] = {
    "euclidean": 2.1,
    "manhattan": 1.2,
    "cosine": 2.1,
    "mahalanobis": 1.6,
    "hamming": 1.6,
    "pearson": 3.1,
    "pearson_absolute": 3.1,
    "spearman": 5.1,
    "spearman_absolute": 5.1,
    "jaccard": 2.1,
}
#: Copies of the data (imputed, normalized, ...) while computing.
DISTANCE_DATA_COPIES = 4


def distances_memory_bytes(n_items: int, metric: str, data_bytes: int = 0) -> int:
    """Memory the ``distances`` job needs for ``n_items`` rows or columns:
    the metric's working matrices and copies of the data (``data_bytes``)."""
    factor = DISTANCE_METRICS.get(metric, max(DISTANCE_METRICS.values()))
    return int(factor * n_items * n_items * 8 + DISTANCE_DATA_COPIES * data_bytes)


def distances_too_large_message(n_items: int, axis: str, needed: int, free: int) -> str:
    """Why the machine won't compute distances between ``n_items`` rows or
    columns (``axis``): they need ``needed`` bytes, it can give them ``free``."""
    gb = 1024**3
    return (
        f"Distances between {n_items:,} {axis} need about {needed / gb:.1f} GB of memory "
        f"(the matrix itself is {n_items * n_items * 8 / gb:.1f} GB), but the cloud machine "
        f"can give it {free / gb:.1f} GB. Use a machine with more memory (Cloud settings…) "
        f"or fewer {axis}."
    )


#: Working memory of the ``mds`` job (scikit-learn's SMACOF), in matrices of
#: n x n x 8 bytes besides the distances (measured peak 5.5, rounded up).
MDS_MATRICES = 6.0


def mds_memory_bytes(n_items: int, data_bytes: int | None = None) -> int:
    """Memory the ``mds`` job needs for ``n_items`` items: SMACOF's working
    matrices and, when it computes the distances from data of ``data_bytes``,
    the distances and copies of the data."""
    if data_bytes is None:
        return int(MDS_MATRICES * n_items * n_items * 8)
    return int((MDS_MATRICES + 1) * n_items * n_items * 8 + DISTANCE_DATA_COPIES * data_bytes)


def mds_too_large_message(n_items: int, axis: str, needed: int, free: int) -> str:
    """Why the machine won't run MDS on ``n_items`` rows or columns (``axis``)."""
    gb = 1024**3
    return (
        f"MDS of {n_items:,} {axis} needs about {needed / gb:.1f} GB of memory, but the cloud "
        f"machine can give it {free / gb:.1f} GB. Use a machine with more memory "
        f"(Cloud settings…) or fewer {axis}."
    )
