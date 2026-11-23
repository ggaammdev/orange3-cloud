"""Models that stay on the cloud machine.

A :class:`CloudModel` is what Cloud Learner outputs: a reference to a model
trained and kept on the machine, with what the laptop needs to show it and
to use it in other Cloud widgets (its name, the domain it was trained on,
the learner, the training rows). It holds no data and no secrets, so it can
be copied and pickled safely. :meth:`CloudModel.download` brings the real
Orange model to this computer; ``size_bytes`` says how big that is first.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from orangecontrib.cloud.client.api import CloudNotFoundError
from orangecontrib.cloud.data.remote_table import RemoteTableError

if TYPE_CHECKING:
    from Orange.data import Domain

    from orangecontrib.cloud.client.api import CloudClient

__all__ = ["CloudModel", "ModelUnavailableError"]

MODEL_UNAVAILABLE = "This cloud model is no longer available. Train it again (Cloud Learner)."


class ModelUnavailableError(RemoteTableError):
    """The machine that held the model is gone, or it no longer has the model."""

    def __init__(self, message: str = MODEL_UNAVAILABLE):
        super().__init__(message)


@dataclass(frozen=True)
class CloudModel:
    """A model stored on the machine (see the module's documentation)."""

    object_id: str  #: the model's object ID on the machine
    name: str  #: the model's name, as Orange names models: the learner's name
    domain: Domain  #: the domain of the data it was trained on
    learner_name: str  #: the learner's class, e.g. ``RandomForestLearner``
    model_type: str  #: the model's qualified class name
    table_id: str  #: the table it was trained on
    n_rows: int  #: rows it was trained on
    machine_id: str | None = None  #: the machine's ``MachineHandle.machine_id``
    size_bytes: int | None = None  #: size of the download (the pickle)
    training_seconds: float | None = None

    def __str__(self) -> str:
        return f"{self.name} (in the cloud, trained on {self.n_rows:,} rows)"

    def resolve(self, machine_id: str | None = None) -> str:
        """The model's object ID, for jobs on the machine ``machine_id``.
        Raises :class:`ModelUnavailableError` if the model is on another machine."""
        if machine_id is not None and self.machine_id is not None and machine_id != self.machine_id:
            raise ModelUnavailableError()
        return self.object_id

    def download(self, client: CloudClient, machine_id: str | None = None) -> Any:
        """Download the Orange model (``GET /v1/objects/{id}``)."""
        object_id = self.resolve(machine_id)
        try:
            return client.get_object(object_id)
        except CloudNotFoundError as exc:
            raise ModelUnavailableError() from exc
