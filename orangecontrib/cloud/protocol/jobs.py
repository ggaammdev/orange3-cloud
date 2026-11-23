"""Jobs: starting one (``POST /v1/jobs``), its state and its result, and the
objects (models, distance matrices, ...) that jobs store on the machine."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, JsonValue

from orangecontrib.cloud.protocol.errors import ErrorResponse
from orangecontrib.cloud.protocol.tables import TableInfo

__all__ = [
    "JobInfo",
    "JobKind",
    "JobRequest",
    "JobResult",
    "JobState",
    "ObjectInfo",
    "ObjectKind",
]


class JobState(str, Enum):
    """Where a job is in its life."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def finished(self) -> bool:
        """True once the job is done, failed or cancelled."""
        return self in (JobState.DONE, JobState.FAILED, JobState.CANCELLED)


class JobKind(str, Enum):
    """Known job kinds.

    ``POST /v1/jobs`` accepts the kinds from ``fit`` to ``sleep``; the table
    endpoints create the ``load_table`` and ``derive`` jobs.
    The machine may register more kinds, so job models hold the kind as a
    plain string.
    """

    FIT = "fit"
    EVALUATE = "evaluate"
    PREDICT = "predict"
    KMEANS = "kmeans"
    TSNE = "tsne"
    DISTANCES = "distances"
    HCLUST = "hclust"
    MDS = "mds"
    RANK = "rank"
    SLEEP = "sleep"  #: test operation: sleeps, reports progress, can fail
    LOAD_TABLE = "load_table"
    DERIVE = "derive"


class ObjectKind(str, Enum):
    """What a stored object is."""

    MODEL = "model"
    DISTANCES = "distances"
    RESULTS = "results"  #: Orange evaluation ``Results``
    TABLE = "table"  #: a small, local Orange ``Table`` (e.g. centroids)
    STATS = "stats"
    OTHER = "other"


class ObjectInfo(BaseModel):
    """A Python object stored on the machine, downloadable as a pickle."""

    id: str
    kind: ObjectKind
    #: Qualified class name, e.g. ``Orange.classification.RandomForestModel``.
    type: str
    #: Size of the pickle, so the add-on can warn before large downloads.
    size_bytes: int | None = None
    #: Array shape, for distance matrices.
    shape: list[int] | None = None
    description: str = ""
    created_at: datetime


class JobRequest(BaseModel):
    """``spec`` part of ``POST /v1/jobs``.

    Other parts of the multipart body are pickles, named in ``parts``.
    """

    kind: str
    #: Named references to things already on the machine: table IDs, object
    #: IDs, or lists of them (e.g. several models).
    inputs: dict[str, str | list[str]] = Field(default_factory=dict)
    #: Options of the operation, as JSON.
    params: dict[str, JsonValue] = Field(default_factory=dict)
    #: Names of the pickle parts sent with the request, e.g. ``["learners"]``.
    parts: list[str] = Field(default_factory=list)


class JobInfo(BaseModel):
    """State of a job: ``GET /v1/jobs/{id}`` and the answer to every call
    that starts or cancels one."""

    id: str
    kind: str
    state: JobState
    #: From 0 to 1.
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    #: Short human-readable status, e.g. ``"Fold 3 of 10"``.
    status: str = ""
    #: Set when the job failed or was cancelled; its message says why, e.g.
    #: that the machine cancelled it because Orange had been silent too long.
    error: ErrorResponse | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class JobResult(BaseModel):
    """``GET /v1/jobs/{id}/result`` of a finished job.

    Large outputs stay on the machine and are referenced here: tables (fetch
    rows or the domain from the table endpoints) and objects (download
    the pickle from ``GET /v1/objects/{id}``).
    """

    job_id: str
    kind: str
    tables: dict[str, TableInfo] = Field(default_factory=dict)
    objects: dict[str, ObjectInfo] = Field(default_factory=dict)
    #: Small JSON results, e.g. scores or silhouette values.
    values: dict[str, JsonValue] = Field(default_factory=dict)
    runtime_seconds: float | None = None
