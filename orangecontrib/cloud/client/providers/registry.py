"""Machine providers by their id: ``local`` (Docker on this computer),
``gcp`` (the user's Google Cloud project), ``aws`` (the user's AWS
account) and ``azure`` (the user's Azure subscription), and any registered
later.

The Cloud settings dialog offers the providers of :func:`registered_providers`
in their order, each with its page (see :mod:`~orangecontrib.cloud.gui.
provider_page`). The built-in ones are imported when first needed; another
cloud adds its module to :data:`BUILTIN`, or calls :func:`register_provider`.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass

from orangecontrib.cloud.client.providers.base import ClusterProvider, ProviderError

__all__ = [
    "BUILTIN",
    "ProviderEntry",
    "get_provider",
    "register_provider",
    "registered_providers",
    "unregister_provider",
]

#: The add-on's own providers, in the dialog's order: (id, module, class).
BUILTIN = (
    ("local", "orangecontrib.cloud.client.providers.local_docker", "LocalDockerProvider"),
    ("gcp", "orangecontrib.cloud.client.providers.gcp", "OwnGCPProvider"),
    ("aws", "orangecontrib.cloud.client.providers.aws", "AWSProvider"),
    ("azure", "orangecontrib.cloud.client.providers.azure", "AzureProvider"),
)


@dataclass(frozen=True)
class ProviderEntry:
    """A registered provider."""

    id: str
    label: str  #: for the user, e.g. "Google Cloud"
    factory: Callable[[], ClusterProvider]


_ENTRIES: dict[str, ProviderEntry] = {}


def register_provider(
    provider_id: str, factory: Callable[[], ClusterProvider], label: str = ""
) -> None:
    """Make ``get_provider(provider_id)`` create providers with ``factory``.
    ``label``: for the user (default: the class's ``label`` if ``factory``
    is a provider class, else the id)."""
    label = label or getattr(factory, "label", "") or provider_id
    _ENTRIES[provider_id] = ProviderEntry(provider_id, label, factory)


def unregister_provider(provider_id: str) -> None:
    """Forget a registered provider (e.g. one a test added)."""
    _ENTRIES.pop(provider_id, None)


def _register_builtin(provider_id: str) -> None:
    for builtin_id, module, name in BUILTIN:
        if builtin_id == provider_id and builtin_id not in _ENTRIES:
            register_provider(builtin_id, getattr(importlib.import_module(module), name))


def registered_providers() -> list[ProviderEntry]:
    """The providers the user can choose: the built-in ones, then the others
    in the order they were registered."""
    for provider_id, _, _ in BUILTIN:
        _register_builtin(provider_id)
    builtin = [provider_id for provider_id, _, _ in BUILTIN]
    order = builtin + [p for p in _ENTRIES if p not in builtin]
    return [_ENTRIES[p] for p in order if p in _ENTRIES]


def get_provider(provider_id: str) -> ClusterProvider:
    """A provider by its id (as stored in a :class:`MachineHandle`)."""
    _register_builtin(provider_id)
    try:
        entry = _ENTRIES[provider_id]
    except KeyError:
        raise ProviderError(f"Unknown machine provider '{provider_id}'.") from None
    return entry.factory()
