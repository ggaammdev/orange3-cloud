"""The job kinds of this package: :func:`register_operations`, which the job
system calls when it starts."""

from __future__ import annotations

import importlib

__all__ = ["OPERATION_MODULES", "register_operations"]

#: The modules of this package whose ``@operation`` functions are job kinds
#: (the other modules hold parts that these share).
OPERATION_MODULES = (
    "derive",
    "distances",
    "evaluate",
    "hclust",
    "kmeans",
    "learn",
    "mds",
    "predict",
    "rank",
    "tsne",
)


def register_operations() -> None:
    """Register the job kinds of :data:`OPERATION_MODULES`: importing a module
    registers its ``@operation`` functions."""
    for name in OPERATION_MODULES:
        importlib.import_module(f"{__package__}.{name}")
