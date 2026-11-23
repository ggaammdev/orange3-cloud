"""Distance matrices that stay on the cloud machine.

A :class:`CloudDistances` is what Cloud Distances outputs: a reference to
Orange's ``DistMatrix`` computed and kept on the machine (an object ID),
with small metadata for the laptop: its shape, metric and axis, the table
it was computed from and, between columns, the column names. It holds no
values and no secrets, so it can be copied and pickled safely. Cloud
widgets pass its object ID to their jobs; :meth:`CloudDistances.download`
brings the matrix to this computer, for small ones.

:func:`check_memory` refuses, before the job runs, distances that don't fit
in the machine's free memory; the machine checks again itself.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from orangecontrib.cloud.client.api import CloudNotFoundError
from orangecontrib.cloud.client.providers.base import MachineType
from orangecontrib.cloud.client.providers.sizes import largest_memory_gb, smallest_machine_type
from orangecontrib.cloud.data.remote_table import RemoteTableError
from orangecontrib.cloud.protocol.machine import InfoResponse
from orangecontrib.cloud.protocol.memory import (
    distances_memory_bytes,
    distances_too_large_message,
    mds_memory_bytes,
    mds_too_large_message,
)

if TYPE_CHECKING:
    from Orange.data import Domain

    from orangecontrib.cloud.client.api import CloudClient

__all__ = [
    "CloudDistances",
    "DistancesTooLargeError",
    "DistancesUnavailableError",
    "check_mds_memory",
    "check_memory",
]

DISTANCES_UNAVAILABLE = (
    "These cloud distances are no longer available. Compute them again (Cloud Distances)."
)
#: Share of the machine's free memory that a job may use (the machine stops
#: its worker at 95% of the worker's memory limit and lets jobs fill 90%).
FREE_SHARE = 0.9
#: Share of a machine size's memory that work can use (the rest: the system,
#: the API server, the worker's own memory and the table).
USABLE_SHARE = 0.85


class DistancesUnavailableError(RemoteTableError):
    """The machine that held the distances is gone, or it no longer has them."""

    def __init__(self, message: str = DISTANCES_UNAVAILABLE):
        super().__init__(message)


class DistancesTooLargeError(RemoteTableError):
    """The distances need more memory than the machine has free."""

    def __init__(self, message: str, needed_bytes: int):
        super().__init__(message)
        self.needed_bytes = needed_bytes


@dataclass(frozen=True)
class CloudDistances:
    """A distance matrix stored on the machine (see the module's documentation)."""

    object_id: str  #: the ``DistMatrix``'s object ID on the machine
    n_items: int  #: rows (and columns) of the matrix
    metric: str  #: the metric's name as Orange shows it, e.g. "Euclidean (normalized)"
    axis: str  #: what was compared: "rows" or "columns"
    table_id: str  #: the table the distances were computed from
    domain: Domain | None = None  #: that table's domain
    labels: tuple[str, ...] | None = None  #: the column names, between columns
    machine_id: str | None = None  #: the machine's ``MachineHandle.machine_id``
    size_bytes: int | None = None  #: size of the download (the pickle)

    def __str__(self) -> str:
        return f"{self.metric} distances between {self.n_items:,} {self.axis} (in the cloud)"

    @property
    def shape(self) -> tuple[int, int]:
        """The matrix's shape."""
        return self.n_items, self.n_items

    def resolve(self, machine_id: str | None = None) -> str:
        """The matrix's object ID, for jobs on the machine ``machine_id``.
        Raises :class:`DistancesUnavailableError` if it's on another machine."""
        if machine_id is not None and self.machine_id is not None and machine_id != self.machine_id:
            raise DistancesUnavailableError()
        return self.object_id

    def download(self, client: CloudClient, machine_id: str | None = None) -> Any:
        """Download Orange's ``DistMatrix`` (``GET /v1/objects/{id}``); only
        for small matrices."""
        object_id = self.resolve(machine_id)
        try:
            return client.get_object(object_id)
        except CloudNotFoundError as exc:
            raise DistancesUnavailableError() from exc


def check_memory(
    n_items: int,
    axis: str,
    metric: str,
    data_bytes: int,
    info: InfoResponse,
    sizes: Sequence[MachineType] = (),
) -> int:
    """The memory the distances need; raises :class:`DistancesTooLargeError`
    if that's more than the machine (``info``) can give a job. The message
    names the smallest of ``sizes`` (the provider's machine sizes) that would
    fit."""
    needed = distances_memory_bytes(n_items, metric, data_bytes)
    free = int(FREE_SHARE * info.memory_free_bytes)
    if needed > free:
        message = distances_too_large_message(n_items, axis, needed, free)
        _too_large(message, needed, info, sizes)
    return needed


def check_mds_memory(
    n_items: int,
    axis: str,
    data_bytes: int | None,
    info: InfoResponse,
    sizes: Sequence[MachineType] = (),
) -> int:
    """The memory MDS of ``n_items`` needs (from data of ``data_bytes``, or
    from distances if ``None``); raises :class:`DistancesTooLargeError` as
    :func:`check_memory` does."""
    needed = mds_memory_bytes(n_items, data_bytes)
    free = int(FREE_SHARE * info.memory_free_bytes)
    if needed > free:
        _too_large(mds_too_large_message(n_items, axis, needed, free), needed, info, sizes)
    return needed


def _too_large(message: str, needed: int, info: InfoResponse, sizes: Sequence[MachineType]) -> None:
    """Raise :class:`DistancesTooLargeError`, naming one of ``sizes`` that fits."""
    size = smallest_machine_type(needed / USABLE_SHARE + info.memory_tables_bytes, sizes)
    if size is not None:
        message += f" The smallest that fits: {size.label} ({size.memory_gb:g} GB)."
    elif sizes:
        message += f" The largest machine has {largest_memory_gb(sizes):g} GB."
    raise DistancesTooLargeError(message, needed)
