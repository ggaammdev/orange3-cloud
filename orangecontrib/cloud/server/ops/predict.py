"""Predicting on the machine: the ``predict`` job.

``predict`` gets a table (input ``data``) and models stored on the machine
(input ``models``, a list of object IDs, e.g. from ``fit``). Every model
predicts every row, the way Orange's Predictions widget calls it (on the
data without its target), in blocks of rows so that memory stays bounded
and progress and cancelling work on big tables.

**Result.** A new table, output ``table``: the data with, for each model
that didn't fail, a prediction column and, for a categorical target, a
probability column per class value ("name (value)"), added as meta
attributes like the output of Orange's Predictions widget. It stays on the
machine. The values hold, when the data has the target that a model
predicts, the scores of the predictions on all rows with a known target,
computed with ``Orange.evaluation.scoring`` as Orange's Predictions widget
computes them.
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np

from orangecontrib.cloud.protocol.errors import ErrorCode
from orangecontrib.cloud.protocol.jobs import JobKind
from orangecontrib.cloud.server.ops.evaluate import score_results, usable_scorers
from orangecontrib.cloud.server.ops.learn import learner_name
from orangecontrib.cloud.server.worker import JobCancelled, JobContext, OperationError, operation

__all__ = ["BLOCK_BYTES", "predict_operation"]

#: Rows are predicted in blocks of about this many bytes of input data.
BLOCK_BYTES = 256 * 1024**2

MULTIPLE_TARGETS = "Multiple targets are not supported."


def _block_rows(data: Any) -> int:
    width = max(1, len(data.domain.attributes) + len(data.domain.metas))
    return max(1000, BLOCK_BYTES // (8 * width))


class _Prediction:
    """One model's predictions on all rows."""

    def __init__(self, model: Any, name: str):
        self.model = model
        self.name = name
        self.failed: str | None = None
        self.values: np.ndarray | None = None  # in the model's class values
        self.probabilities: np.ndarray | None = None
        self.seconds = 0.0
        target = model.domain.class_var
        self.discrete = target is not None and target.is_discrete


def _predict_block(pred: _Prediction, block: Any) -> tuple[np.ndarray, np.ndarray | None]:
    from Orange.base import Model

    if pred.discrete:
        values, probs = pred.model(block, Model.ValueProbs)
        return np.asarray(values, dtype=float), np.asarray(probs, dtype=float)
    return np.asarray(pred.model(block, Model.Value), dtype=float), None


def _predict(ctx: JobContext, data: Any, preds: list[_Prediction]) -> None:
    """Fill in each model's predictions, block by block of rows."""
    from Orange.data import Domain

    n = len(data)
    step = _block_rows(data)
    starts = list(range(0, n, step)) or [0]
    domain = data.domain
    classless = Domain(domain.attributes, None, domain.metas)
    total = len(preds) * len(starts)
    done = 0
    for pred in preds:
        values, probs = [], []
        for start in starts:
            ctx.check_cancelled()
            if pred.failed is None:
                # A slice of a table shares its arrays; transform() copies
                # only the block's class-less view.
                block = data[start : start + step].transform(classless)
                started = time.perf_counter()
                try:
                    v, p = _predict_block(pred, block)
                    if v.ndim > 1 and v.shape[1] > 1:
                        raise ValueError(MULTIPLE_TARGETS)
                    values.append(v.reshape(-1))
                    if p is not None:
                        probs.append(p)
                except MemoryError:
                    raise
                except Exception as exc:  # noqa: BLE001 - job runner: reported as the model's error
                    pred.failed = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                pred.seconds += time.perf_counter() - started
            done += 1
            status = f"Predicting with {pred.name}: {done} of {total} blocks"
            ctx.progress(0.9 * done / total, status)
        if pred.failed is None:
            pred.values = np.concatenate(values) if values else np.empty(0)
            if pred.discrete:
                width = len(pred.model.domain.class_var.values)
                pred.probabilities = np.vstack(probs) if probs else np.empty((0, width))


def _output_table(data: Any, preds: list[_Prediction]) -> Any:
    """The data with the prediction (and probability) columns as metas, as
    Orange's Predictions widget outputs it. Shares the data's attributes."""
    from Orange.data import ContinuousVariable, DiscreteVariable, Domain
    from Orange.data.util import get_unique_names

    new_vars, columns = [], []
    for pred in preds:
        if pred.failed is not None:
            continue
        target = pred.model.domain.class_var
        if pred.discrete:
            values = list(target.values)
            new_vars.append(DiscreteVariable(pred.name, values=values))
            columns.append(pred.values)
            for i, value in enumerate(values):
                new_vars.append(ContinuousVariable(f"{pred.name} ({value})"))
                columns.append(pred.probabilities[:, i])
        else:
            new_vars.append(ContinuousVariable(pred.name))
            columns.append(pred.values)
    domain = data.domain
    names = [var.name for var in domain.variables + domain.metas]
    unique = []
    for var in new_vars:
        name = get_unique_names(names, var.name)
        unique.append(var if name == var.name else var.copy(name=name))
        names.append(name)
    out_domain = Domain(domain.attributes, domain.class_vars, domain.metas + tuple(unique))
    table = data.transform(out_domain)  # shares X; the new metas are empty
    if columns:
        block = np.column_stack(columns)
        with table.unlocked(table.metas):
            table.metas[:, -block.shape[1] :] = block
    return table


def _scorable(pred: _Prediction, data: Any) -> tuple[np.ndarray, np.ndarray | None] | None:
    """The model's predictions in the data's class values, if it predicts
    the data's target (as Orange's Predictions widget maps them); else None."""
    class_var = data.domain.class_var
    target = pred.model.domain.class_var
    if pred.failed is not None or class_var is None or target != class_var:
        return None
    values, probs = pred.values, pred.probabilities
    if target is not class_var and target.is_discrete:
        backmappers, n_values = pred.model.get_backmappers(data)
        if backmappers is not None:
            probs = pred.model.backmap_probs(probs, n_values, backmappers)
            values = pred.model.backmap_value(values, probs, n_values, backmappers)
    return np.asarray(values, dtype=float).reshape(-1), probs


def _scores(data: Any, preds: list[_Prediction]) -> dict[str, Any]:
    """Scores of the models that predict the data's target, on all rows
    with a known target (keys of :func:`score_results`; ``None`` rows for
    models without scores)."""
    from Orange.evaluation import Results

    class_var = data.domain.class_var
    scorers = usable_scorers(data.domain)
    empty = {"scorers": [], "scores": {}, "scored_rows": 0, "missing_targets": 0}
    if class_var is None or not scorers:
        return empty
    actual = np.asarray(data.Y, dtype=float).reshape(-1)
    known = ~np.isnan(actual)
    mapped = [_scorable(pred, data) for pred in preds]
    scored = [i for i, m in enumerate(mapped) if m is not None]
    result = {
        "scorers": [{"id": s.__name__, "name": s.name, "long_name": s.long_name} for s in scorers],
        "scores": {},
        "scored_rows": int(known.sum()),
        "missing_targets": int((~known).sum()),
        "scored": [m is not None for m in mapped],
    }
    if not scored or not known.any():
        return result
    m = int(known.sum())
    results = Results(
        domain=data.domain,
        nrows=m,
        nmethods=len(scored),
        learners=[preds[i].name for i in scored],
        train_time=np.zeros(len(scored)),
        test_time=np.zeros(len(scored)),
        row_indices=np.arange(m),
        folds=[Ellipsis],
        actual=actual[known],
        predicted=np.vstack([mapped[i][0][known] for i in scored]),
        probabilities=(
            np.array([mapped[i][1][known] for i in scored]) if class_var.is_discrete else None
        ),
    )
    # Orange's Predictions scores a target class directly, not one against the rest.
    by_scored = score_results(results, scorers, one_vs_rest=False)
    for key, rows in by_scored.items():
        full: list[Any] = [None] * len(preds)
        for row, i in zip(rows, scored, strict=True):
            full[i] = row
        result["scores"][key] = full
    return result


@operation(JobKind.PREDICT)
def predict_operation(ctx: JobContext) -> dict[str, Any]:
    """Predict ``inputs["data"]`` with the models ``inputs["models"]`` (see
    the module's documentation).

    Params: ``names`` (optional, a name per model for its columns; default
    the model's name).
    Result: table ``table`` (none if every model failed) and values
    ``rows``, ``models`` (name, failed, target, scored, seconds per model),
    ``scorers``, ``scores`` (per target, rows are models, ``null`` for
    models without scores), ``scored_rows``, ``missing_targets`` and
    ``predict_seconds``.
    """
    from Orange.base import Model

    data = ctx.table("data")
    if not isinstance(ctx.inputs.get("models"), list) or not ctx.inputs["models"]:
        raise OperationError(ErrorCode.BAD_REQUEST, "The job needs a list of models, 'models'.")
    models = ctx.object("models")
    if not all(isinstance(model, Model) for model in models):
        raise OperationError(ErrorCode.BAD_REQUEST, "'models' must be Orange models.")
    names = ctx.params.get("names") or []
    preds = [
        _Prediction(
            model,
            str(names[i]) if i < len(names) and names[i] else learner_name(model),
        )
        for i, model in enumerate(models)
    ]
    rows = len(data)
    ctx.progress(0.0, f"Predicting {rows:,} rows with {len(preds)} models")
    started = time.monotonic()
    _predict(ctx, data, preds)
    if ctx.cancelled:
        raise JobCancelled()
    ctx.progress(0.9, "Computing the scores")
    scores = _scores(data, preds)
    ctx.check_cancelled()
    if any(pred.failed is None for pred in preds):
        ctx.progress(0.95, "Storing the predictions")
        table = _output_table(data, preds)
        name = f"{data.name} predictions" if getattr(data, "name", "") else "predictions"
        ctx.add_table("table", table, table_name=name, parent_id=ctx.inputs["data"])
    ctx.progress(1.0, "Predicted")
    scored = scores.pop("scored", [False] * len(preds))
    return {
        "rows": rows,
        "models": [
            {
                "name": pred.name,
                "failed": pred.failed,
                "target": (
                    pred.model.domain.class_var.name if pred.model.domain.class_var else None
                ),
                "scored": scored[i],
                "seconds": pred.seconds,
            }
            for i, pred in enumerate(preds)
        ],
        **scores,
        "predict_seconds": time.monotonic() - started,
    }
