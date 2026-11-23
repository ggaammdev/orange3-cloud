"""Evaluating learners on the machine: the ``evaluate`` job.

``evaluate`` gets pickled Orange learners (part ``learners``, a list) and a
table (input ``data``; with ``params["method"] == "test_on_test_data"`` also
input ``test_data``). It runs cross-validation (``folds``, ``stratified``)
or tests on the test data, the way Orange's Test & Score does (the same
splits: ``CrossValidation(k, stratified, random_state=42)``), on all rows.

**Parallelism.** Every learner × fold is a part, its own Dask task, in the
worker that holds the table (:mod:`.parts`): scikit-learn learners in the
worker's threads, and on Linux learners that hold Python's GIL (e.g.
Orange's own tree) in processes forked from the worker, which share the
table copy-on-write. Parts run side by side as far as the worker's threads
and memory allow. The values ``parallel``, ``threads_per_part``,
``part_memory_bytes``, ``processes`` and ``parts_run_again`` report how.

**Result.** An Orange ``Results`` object is assembled on the machine and
stored as object ``results`` (kind ``results``; its ``size_bytes`` is the
download size). The values hold the scores, computed with
``Orange.evaluation.scoring`` on all rows like Orange's Test & Score: per
learner for the average over classes (key ``""``) and, for a categorical
target, for each target class (one against the rest).
"""

from __future__ import annotations

import copy
import logging
import math
from collections.abc import Sequence
from operator import attrgetter
from typing import Any

import numpy as np

from orangecontrib.cloud.protocol.errors import ErrorCode
from orangecontrib.cloud.protocol.jobs import JobKind, ObjectKind
from orangecontrib.cloud.protocol.pickles import dumps
from orangecontrib.cloud.server.ops.learn import learner_name
from orangecontrib.cloud.server.ops.parts import (
    MIN_PART_MEMORY_FACTOR,
    RUNS,
    RUNS_LOCK,
    Run,
    gil_bound,
    run_parts,
)
from orangecontrib.cloud.server.worker import (
    JobCancelled,
    JobContext,
    OperationError,
    operation,
    table_memory,
)

__all__ = [
    "CROSS_VALIDATION",
    "RANDOM_STATE",
    "TEST_ON_TEST_DATA",
    "evaluate_operation",
    "results_one_vs_rest",
    "score_results",
    "usable_scorers",
]

log = logging.getLogger(__name__)

CROSS_VALIDATION = "cross_validation"
TEST_ON_TEST_DATA = "test_on_test_data"

#: The seed of Orange's Test & Score widget, so the folds are the same.
RANDOM_STATE = 42

#: Scores per target class only for targets with at most this many values.
MAX_TARGET_SCORES = 50


# --------------------------------------------------------------------------
# Scores (as Orange's Test & Score computes them, without its Qt code)


def usable_scorers(domain: Any) -> list[type]:
    """Orange's scorers for the domain's target, in Test & Score's order."""
    from Orange.evaluation import scoring

    if domain is None or domain.class_var is None:
        return []
    candidates = [
        s
        for s in scoring.Score.registry.values()
        if s.is_scalar
        and not s.__dict__.get("abstract")
        and s.is_compatible(domain)
        and s.class_types
    ]
    return sorted(candidates, key=attrgetter("priority"))


def results_one_vs_rest(results: Any, pos_index: int) -> Any:
    """``results`` as a binary problem: class ``pos_index`` against the rest
    (Orange's Test & Score does the same for a chosen target class)."""
    from Orange.data import DiscreteVariable, Domain
    from Orange.evaluation import Results
    from Orange.preprocess.transformation import Indicator

    res = Results()
    res.actual = results.actual == pos_index
    res.predicted = results.predicted == pos_index
    if results.probabilities is not None:
        c = results.probabilities.shape[2]
        neg = [i for i in range(c) if i != pos_index]
        pos_prob = results.probabilities[:, :, [pos_index]]
        neg_prob = np.sum(results.probabilities[:, :, neg], axis=2, keepdims=True)
        res.probabilities = np.dstack((neg_prob, pos_prob))
    else:
        res.probabilities = None
    res.folds = results.folds
    res.row_indices = results.row_indices
    class_var = results.domain.class_var
    indicator = DiscreteVariable(
        f"I({class_var.name}=={class_var.values[pos_index]})",
        values=("False", "True"),
        compute_value=Indicator(class_var, pos_index),
    )
    res.data = None
    res.domain = Domain(results.domain.attributes, [indicator], results.domain.metas)
    return res


def _score(scorer: type, results: Any, target: int | None) -> float | None:
    import warnings

    from sklearn.exceptions import UndefinedMetricWarning

    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore", "((F-score|Precision)) is ill-defined.*", UndefinedMetricWarning
            )
            if scorer.is_binary:
                value = scorer(results, target=target, average="weighted")
            else:
                value = scorer(results)
        value = float(np.asarray(value).reshape(-1)[0])
    except Exception:  # noqa: BLE001 - job runner: reported as an empty score, as in Orange
        return None
    return value if math.isfinite(value) else None


def score_results(
    results: Any, scorers: Sequence[type], *, one_vs_rest: bool = True
) -> dict[str, list[list[float | None]]]:
    """Scores per learner (rows) and scorer (columns), for the average over
    classes (key ``""``) and, for a categorical target with at most
    :data:`MAX_TARGET_SCORES` values, for each class value: one against the
    rest (Orange's Test & Score) or, with ``one_vs_rest=False``, with that
    value as the scorers' target (Orange's Predictions). Failed learners
    get ``None``."""
    class_var = results.domain.class_var
    targets: list[tuple[str, int | None]] = [("", None)]
    if class_var.is_discrete and len(class_var.values) <= MAX_TARGET_SCORES:
        targets += [(value, i) for i, value in enumerate(class_var.values)]
    scores: dict[str, list[list[float | None]]] = {key: [] for key, _ in targets}
    for single in results.split_by_model():
        failed = bool(single.failed[0])
        for key, target in targets:
            if failed:
                row = [None] * len(scorers)
            elif target is None:
                row = [_score(s, single, None) for s in scorers]
            elif not one_vs_rest:
                row = [_score(s, single, target) for s in scorers]
            else:
                ovr = results_one_vs_rest(single, target)
                row = [_score(s, ovr, 1) for s in scorers]
            scores[key].append(row)
    return scores


# --------------------------------------------------------------------------
# The operation


def _check_learners(learners: Any) -> list[Any]:
    from Orange.base import Learner

    if not isinstance(learners, (list, tuple)) or not learners:
        raise OperationError(ErrorCode.BAD_REQUEST, "The job needs a list of learners, 'learners'.")
    if not all(isinstance(learner, Learner) for learner in learners):
        raise OperationError(ErrorCode.BAD_REQUEST, "'learners' must be Orange learners.")
    return list(learners)


def _results(run: Run, data: Any, learners: list[Any], n_folds: int) -> tuple[Any, list]:
    """Assemble Orange's ``Results`` from the parts (as ``Validation`` does)."""
    from Orange.evaluation import Results

    if run.indices is None:
        test = run.test_data
        row_indices = np.arange(len(test))
        folds: list[Any] = [Ellipsis]
        actual = test.Y
        domain = test.domain
    else:
        tests = [test_i for _, test_i in run.indices]
        folds, ptr = [], 0
        for test_i in tests:
            folds.append(slice(ptr, ptr + len(test_i)))
            ptr += len(test_i)
        row_indices = np.concatenate(tests, axis=0)
        y = data.Y
        actual = y[row_indices]  # not data[row_indices].Y: no copy of the table
        domain = data.domain
    n = len(learners)
    results = Results(
        domain=domain,
        nrows=len(row_indices),
        learners=learners,
        row_indices=row_indices,
        folds=folds,
        actual=actual,
        score_by_folds=False,
        train_time=np.zeros(n),
        test_time=np.zeros(n),
    )
    failures: list[str | None] = [None] * n
    for (fold, learner_i), part in sorted(run.parts.items()):
        if failures[learner_i] is not None:
            continue
        if part.failed is not None:
            failures[learner_i] = part.failed
            results.failed[learner_i] = RuntimeError(part.failed)
            continue
        values = np.asarray(part.predicted)
        if values.ndim > 1 and values.shape[1] > 1:
            failures[learner_i] = "Multiple targets are not supported."
            results.failed[learner_i] = ValueError(failures[learner_i])
            continue
        where = folds[fold]
        results.predicted[learner_i][where] = values
        results.train_time[learner_i] += part.train_time
        results.test_time[learner_i] += part.test_time
        if part.probabilities is not None:
            results.probabilities[learner_i][where, :] = part.probabilities
    return results, failures


def _successful(results: Any, keep: list[int], names: list[str]) -> Any:
    """The results of the learners ``keep`` only, named ``names`` (Orange's
    evaluation widgets read the names from ``learner_names``). A shallow
    copy: ``results`` stays as it was."""
    results = copy.copy(results)
    if len(keep) < len(results.learners):
        results.learners = [results.learners[i] for i in keep]
        results.predicted = results.predicted[keep]
        if results.probabilities is not None:
            results.probabilities = results.probabilities[keep]
        results.failed = [results.failed[i] for i in keep]
        results.train_time = results.train_time[keep]
        results.test_time = results.test_time[keep]
    results.learner_names = names
    return results


def _max_parallel(params: dict[str, Any]) -> int | None:
    value = params.get("max_parallel")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise OperationError(ErrorCode.BAD_REQUEST, "'max_parallel' must be a positive integer.")
    return value


@operation(JobKind.EVALUATE)
def evaluate_operation(ctx: JobContext) -> dict[str, Any]:
    """Cross-validate ``parts["learners"]`` on ``inputs["data"]``, or test
    them on ``inputs["test_data"]`` (see the module's documentation).

    Params: ``method`` (``cross_validation`` or ``test_on_test_data``),
    ``folds`` (default 10), ``stratified`` (default true), ``max_parallel``
    (most parts at a time; default: as many as fit).
    Result: object ``results`` (without failed learners; none if all
    failed) and values ``rows``, ``method``, ``folds``,
    ``learners`` (name, failed, train and test seconds per learner),
    ``scorers`` (id, name, long name), ``scores``, ``warnings``,
    ``parallel``, ``threads_per_part``, ``part_memory_bytes`` (per
    learner), ``processes`` (per learner: its parts ran in forked processes)
    and ``parts_run_again``.
    """
    from Orange.evaluation import CrossValidation

    data = ctx.table("data")
    learners = _check_learners(ctx.parts.get("learners"))
    method = ctx.params.get("method", CROSS_VALIDATION)
    if data.domain.class_var is None:
        raise OperationError(ErrorCode.BAD_REQUEST, "The data has no target variable.")
    warnings: list[str] = []
    test_data = None
    if method == CROSS_VALIDATION:
        k = int(ctx.params.get("folds", 10))
        if not 2 <= k <= len(data):
            message = f"Can't make {k} folds of {len(data):,} rows."
            raise OperationError(ErrorCode.BAD_REQUEST, message)
        stratified = bool(ctx.params.get("stratified", True))
        ctx.progress(0.0, f"Splitting {len(data):,} rows into {k} folds")
        validation = CrossValidation(k=k, stratified=stratified, random_state=RANDOM_STATE)
        indices = validation.get_indices(data)
        warnings += list(dict.fromkeys(validation.warnings))
        n_folds = len(indices)
        rows = len(data)
    elif method == TEST_ON_TEST_DATA:
        test_data = ctx.table("test_data")
        if test_data.domain.class_var != data.domain.class_var:
            raise OperationError(
                ErrorCode.BAD_REQUEST, "The test data's target differs from the data's."
            )
        indices = None
        n_folds = 1
        rows = len(test_data)
    else:
        raise OperationError(ErrorCode.BAD_REQUEST, f"Unknown method '{method}'.")

    names = [learner_name(learner) for learner in learners]
    keys = [(fold, i) for fold in range(n_folds) for i in range(len(learners))]
    run = Run(
        data=data,
        test_data=test_data,
        indices=indices,
        learners=[dumps(learner) for learner in learners],
        names=names,
        gil_bound=[gil_bound(learner, data) for learner in learners],
    )
    min_part_bytes = MIN_PART_MEMORY_FACTOR * (
        table_memory(data) + (table_memory(test_data) if test_data is not None else 0)
    )
    with RUNS_LOCK:
        RUNS[ctx.job_id] = run
    try:
        schedule = run_parts(ctx, run, keys, min_part_bytes, _max_parallel(ctx.params))
    finally:
        with RUNS_LOCK:
            RUNS.pop(ctx.job_id, None)
    log.info(
        "evaluate %s: %d parts (%d learners x %d folds), up to %d at a time with %d "
        "cores each; memory per part (MB): %s",
        ctx.job_id,
        len(keys),
        len(learners),
        n_folds,
        schedule.parallel,
        schedule.threads_per_part,
        {names[i]: b // 2**20 for i, b in schedule.part_memory.items()},
    )
    if ctx.cancelled:
        raise JobCancelled()

    ctx.progress(0.98, "Computing the scores")
    results, failures = _results(run, data, learners, n_folds)
    scorers = usable_scorers(results.domain)
    scores = score_results(results, scorers)
    ctx.check_cancelled()
    ctx.progress(0.99, "Measuring the results")
    what = f"{n_folds}-fold cross-validation" if indices is not None else "test on test data"
    valid = [i for i, failed in enumerate(failures) if failed is None]
    if valid:  # like Orange's Test & Score, the output leaves out failed learners
        output = _successful(results, valid, [names[i] for i in valid])
        ctx.add_object(
            "results",
            output,
            ObjectKind.RESULTS,
            description=f"{what} of {len(valid)} learners on {rows:,} rows",
        )
    ctx.progress(1.0, "Evaluated")
    return {
        "rows": rows,
        "method": method,
        "folds": n_folds,
        "learners": [
            {
                "name": names[i],
                "failed": failures[i],
                "train_time": float(results.train_time[i]),
                "test_time": float(results.test_time[i]),
            }
            for i in range(len(learners))
        ],
        "scorers": [{"id": s.__name__, "name": s.name, "long_name": s.long_name} for s in scorers],
        "scores": scores,
        "warnings": warnings,
        "parallel": schedule.parallel,
        "threads_per_part": schedule.threads_per_part,
        "part_memory_bytes": [schedule.part_memory.get(i) for i in range(len(learners))],
        "processes": [bool(p) for p in run.in_process] or [False] * len(learners),
        "parts_run_again": schedule.run_again,
    }
