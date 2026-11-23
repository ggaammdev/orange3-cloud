"""Feature scores on the machine: the ``rank`` job.

``rank`` scores the features of a table (input ``data``) on all its rows,
the way Orange's Rank widget does: with the scoring methods of
``Orange.preprocess.score`` named in ``params["methods"]`` (information
gain, gain ratio, Gini, ANOVA, χ², ReliefF, FCBF, univariate regression,
RReliefF) and with learner-based scorers (part ``scorers``: pickled Orange
learners that can score features, such as random forests or logistic
regression). Each method and scorer is called as the widget calls it, with
its fallbacks: a method that refuses the whole table is called for each
feature, and one that still fails gives no scores (the widget's empty
column). ReliefF and RReliefF sample rows as in Orange (50 iterations,
``random_state=0``), so they cost about the same on any table size per row.

**Memory.** The scorers copy and discretize the data: measured on 1M rows,
up to 10 times the memory of the features and target for the methods, and
14 times for a random forest's scores. Before scoring, the job compares
:data:`DATA_COPIES` times that with the memory the worker may still use and
refuses with ``payload_too_large`` if it doesn't fit.

**Result.** Values only: ``methods`` (per method, a score per feature),
``scorers`` (per scorer, rows of scores per feature: one row, or one per
class value for e.g. logistic regression), ``failed`` (the methods and
scorers that gave no scores), ``has_missing``, ``rows`` and ``seconds``.
Scores are JSON numbers; a missing score is ``null`` and an infinite one
the string ``"inf"`` or ``"-inf"``.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Sequence
from typing import Any

import numpy as np

from orangecontrib.cloud.protocol.errors import ErrorCode
from orangecontrib.cloud.protocol.jobs import JobKind
from orangecontrib.cloud.server.ops.learn import learner_name, use_all_cores
from orangecontrib.cloud.server.resources import memory_budget
from orangecontrib.cloud.server.worker import JobContext, OperationError, operation

__all__ = [
    "DATA_COPIES",
    "METHODS",
    "check_memory",
    "json_scores",
    "method_scores",
    "rank_operation",
    "scorer_scores",
    "scoring_data",
]

log = logging.getLogger(__name__)

#: The scoring methods of Orange's Rank widget: classes of
#: ``Orange.preprocess.score``, by name.
METHODS = (
    "InfoGain",
    "GainRatio",
    "Gini",
    "ANOVA",
    "Chi2",
    "ReliefF",
    "FCBF",
    "UnivariateLinearRegression",
    "RReliefF",
)

#: Memory the scorers need, in copies of the features and target (measured
#: up to 10 for the methods and 14 for a random forest, on 1M rows).
DATA_COPIES = 12

TOO_LARGE = (
    "Scoring {rows:,} rows needs about {needed:.1f} GB of memory, but the machine "
    "has {free:.1f} GB free. Choose a larger machine size in Cloud settings."
)


def json_scores(scores: Any) -> list[Any]:
    """Scores as JSON values: numbers, ``None`` for a missing score and
    ``"inf"``/``"-inf"`` for infinite ones (nested lists stay nested)."""
    out: list[Any] = []
    for value in np.asarray(scores, dtype=float).tolist():
        if isinstance(value, list):
            out.append(json_scores(value))
        elif math.isnan(value):
            out.append(None)
        elif math.isinf(value):
            out.append("inf" if value > 0 else "-inf")
        else:
            out.append(value)
    return out


def method_scores(data: Any, method: str) -> np.ndarray | None:
    """Scores of ``method`` (a name in :data:`METHODS`) for each feature, as
    Orange's Rank computes them; ``None`` when the method fails."""
    from Orange.preprocess import score

    estimator = getattr(score, method)()
    with np.errstate(all="ignore"):
        try:
            return np.asarray(estimator(data), dtype=float)
        except ValueError:
            try:
                return np.array(
                    [estimator(data, attr) for attr in data.domain.attributes], dtype=float
                )
            except ValueError as exc:
                log.warning("%s doesn't work on this data: %s", method, exc)
                return None


def scorer_scores(data: Any, scorer: Any) -> np.ndarray | None:
    """A learner-based scorer's scores, one row per score (e.g. per class
    value) and a column per feature, as Orange's Rank computes them;
    ``None`` when the scorer fails."""
    use_all_cores(scorer, data)
    try:
        return np.atleast_2d(np.asarray(scorer.score_data(data), dtype=float))
    except (ValueError, TypeError) as exc:
        log.warning("%s doesn't work on this data: %s", learner_name(scorer), exc)
        return None


def _methods(params: dict[str, Any]) -> list[str]:
    methods = params.get("methods", [])
    if not isinstance(methods, list) or any(m not in METHODS for m in methods):
        raise OperationError(
            ErrorCode.BAD_REQUEST, f"'methods' must be a list of names from {METHODS}."
        )
    return methods


def _scorers(scorers: Any) -> list[Any]:
    from Orange.preprocess.score import Scorer

    if scorers is None:
        return []
    if not isinstance(scorers, (list, tuple)) or not all(isinstance(s, Scorer) for s in scorers):
        raise OperationError(
            ErrorCode.BAD_REQUEST, "'scorers' must be a list of Orange scorers (learners)."
        )
    return list(scorers)


def scoring_data(data: Any) -> Any:
    """The table without its meta columns, which no scorer uses, so their
    copies (text, often) don't cost memory."""
    from Orange.data import Domain

    return data.transform(Domain(data.domain.attributes, data.domain.class_vars))


def check_memory(data: Any, budget: int) -> int:
    """The memory scoring ``data`` needs; fails with ``payload_too_large``
    above ``budget``."""
    columns = len(data.domain.attributes) + len(data.domain.class_vars)
    needed = DATA_COPIES * len(data) * columns * 8
    if needed > budget:
        raise OperationError(
            ErrorCode.PAYLOAD_TOO_LARGE,
            TOO_LARGE.format(rows=len(data), needed=needed / 1e9, free=budget / 1e9),
        )
    return needed


def _has_missing(data: Any) -> bool:
    import bottleneck as bn

    return bool(bn.anynan(data.X) or bn.anynan(data.Y))


def _score_all(
    ctx: JobContext, data: Any, methods: Sequence[str], scorers: Sequence[Any]
) -> dict[str, Any]:
    total = max(1, len(methods) + len(scorers))
    by_method: dict[str, Any] = {}
    by_scorer: list[Any] = []
    failed: list[str] = []
    for i, method in enumerate(methods):
        ctx.check_cancelled()
        ctx.progress(i / total, f"Scoring with {method}")
        scores = method_scores(data, method)
        by_method[method] = None if scores is None else json_scores(scores)
        if scores is None:
            failed.append(method)
    for i, scorer in enumerate(scorers, start=len(methods)):
        ctx.check_cancelled()
        name = learner_name(scorer)
        ctx.progress(i / total, f"Scoring with {name}")
        scores = scorer_scores(data, scorer)
        by_scorer.append(None if scores is None else json_scores(scores))
        if scores is None:
            failed.append(name)
    return {"methods": by_method, "scorers": by_scorer, "failed": failed}


@operation(JobKind.RANK)
def rank_operation(ctx: JobContext) -> dict[str, Any]:
    """Score the features of ``inputs["data"]`` (see the module's documentation).

    Params: ``methods`` (names from :data:`METHODS`). Part ``scorers``
    (optional): a list of learner-based scorers.
    """
    methods = _methods(ctx.params)
    scorers = _scorers(ctx.parts.get("scorers"))
    data = ctx.table("data")
    check_memory(data, memory_budget())
    started = time.perf_counter()
    ctx.progress(0.0, f"Preparing {len(data):,} rows")
    data = scoring_data(data)
    values = _score_all(ctx, data, methods, scorers)
    ctx.check_cancelled()
    ctx.progress(1.0, "Scored")
    values.update(
        has_missing=_has_missing(data), rows=len(data), seconds=time.perf_counter() - started
    )
    return values
