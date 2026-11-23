"""Training models on the machine: the ``fit`` job.

``fit`` gets a pickled Orange learner (part ``learner``, with any attached
preprocessors) and a table (input ``data``), trains the learner on all rows
of the table and keeps the model on the machine as object ``model``. The
add-on downloads it only when the user asks (``GET /v1/objects/{id}``); the
object's ``size_bytes`` says how big the download is.
"""

from __future__ import annotations

import time
from typing import Any

from orangecontrib.cloud.protocol.errors import ErrorCode
from orangecontrib.cloud.protocol.jobs import JobKind, ObjectKind
from orangecontrib.cloud.server.worker import JobCancelled, JobContext, OperationError, operation

__all__ = ["fit_operation", "learner_name", "use_all_cores"]


def learner_name(learner: Any) -> str:
    """The learner's name as Orange shows it, e.g. "random forest"."""
    return str(getattr(learner, "name", None) or type(learner).__name__)


def use_all_cores(learner: Any, data: Any) -> None:
    """Let a scikit-learn learner that runs on one core by default (Orange's
    random forests have ``n_jobs=1``) use all cores. The results don't change:
    scikit-learn gives the same model for any ``n_jobs``."""
    from Orange.base import SklLearner
    from Orange.modelling import Fitter

    if isinstance(learner, Fitter):
        try:
            learner = learner.get_learner(data)  # cached, so the fit uses it
        except TypeError:
            return  # no learner for this kind of target: the fit says so
    params = getattr(learner, "params", None)
    if (
        isinstance(learner, SklLearner)
        and isinstance(params, dict)
        and params.get("n_jobs", 0) in (1, None)
    ):
        params["n_jobs"] = -1


@operation(JobKind.FIT)
def fit_operation(ctx: JobContext) -> dict[str, Any]:
    """Train ``parts["learner"]`` on ``inputs["data"]``.

    Result: object ``model`` and values ``rows``, ``learner`` (its name),
    ``model_type`` and ``training_seconds``.
    """
    from Orange.base import Learner

    data = ctx.table("data")
    learner = ctx.parts.get("learner")
    if not isinstance(learner, Learner):
        raise OperationError(ErrorCode.BAD_REQUEST, "The job needs an Orange learner, 'learner'.")
    name = learner_name(learner)
    rows = len(data)
    ctx.progress(0.0, f"Training {name} on {rows:,} rows")
    ctx.check_cancelled()
    use_all_cores(learner, data)
    started = time.monotonic()
    model = learner(data)
    seconds = time.monotonic() - started
    if ctx.cancelled:  # scikit-learn can't be stopped midway; drop the model
        raise JobCancelled()
    ctx.progress(0.99, "Measuring the model")
    info = ctx.add_object(
        "model", model, ObjectKind.MODEL, description=f"{name} trained on {rows:,} rows"
    )
    ctx.progress(1.0, "Trained")
    return {
        "rows": rows,
        "learner": name,
        "model_type": info.type,
        "training_seconds": seconds,
    }
