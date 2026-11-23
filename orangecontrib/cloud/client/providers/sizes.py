"""Machine sizes: choosing one that fits, and the boot disk limits.

Each provider has its own sizes (:attr:`~orangecontrib.cloud.client.
providers.base.ClusterProvider.machine_sizes`, fixed for the add-on: x86-64
machines only, no GPUs). Cloud File, Cloud Distances and the size picker of
the Cloud settings use these functions to suggest the smallest that fits.
The boot disk limits are the same everywhere and defined with the settings
(:mod:`orangecontrib.cloud.settings`), where the user's choice is checked.
"""

from __future__ import annotations

from collections.abc import Sequence

from orangecontrib.cloud.client.providers.base import MachineHandle, MachineType
from orangecontrib.cloud.settings import DISK_DEFAULT_GB, DISK_MAX_GB, DISK_MIN_GB

__all__ = [
    "DISK_DEFAULT_GB",
    "DISK_MAX_GB",
    "DISK_MIN_GB",
    "GIB",
    "MachineType",
    "handle_disk_gb",
    "largest_memory_gb",
    "smallest_machine_type",
    "suggest_machine_type",
]

GIB = 1024**3


def smallest_machine_type(memory_bytes: float, types: Sequence[MachineType]) -> MachineType | None:
    """The machine type with the least memory (then the fewest CPUs) that has
    at least ``memory_bytes``; ``None`` if none is big enough."""
    fitting = [t for t in types if t.memory_gb is not None and t.memory_gb * GIB >= memory_bytes]
    return min(fitting, key=lambda t: (t.memory_gb, t.cpus or 0), default=None)


def suggest_machine_type(
    needed_low: float, needed_high: float, types: Sequence[MachineType]
) -> MachineType | None:
    """The smallest size with ``needed_high`` bytes of memory, or else the
    smallest with ``needed_low``; ``None`` if even the largest is too small."""
    return smallest_machine_type(needed_high, types) or smallest_machine_type(needed_low, types)


def largest_memory_gb(types: Sequence[MachineType]) -> float:
    """The memory of the largest of ``types``, in GB (0 if none is known)."""
    return max((t.memory_gb or 0 for t in types), default=0)


def handle_disk_gb(handle: MachineHandle) -> int:
    """The boot disk size of the machine of ``handle`` in GB, from its
    ``disk_gb`` (the default size if it has none)."""
    disk = str(handle.extra.get("disk_gb", ""))
    return int(disk) if disk.isdigit() else DISK_DEFAULT_GB
