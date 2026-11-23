"""The add-on's settings, kept in Orange's own settings store (QSettings).

They are what the user chooses in the Cloud settings dialog: where the machine
runs (the provider), its time limits, and for each provider its account (e.g.
a Google Cloud project), region, zone, machine size and boot disk size
(:class:`ProviderSettings`). They live in the ``orange3-cloud`` group of
Orange's settings file (``~/.config/biolab.si/Orange.ini`` on macOS and
Linux), **never in workflow (``.ows``) files**, and they hold no secrets: the
token and certificate of a running machine are in the add-on's private folder
(see :func:`~orangecontrib.cloud.client.providers.base.state_dir`).

``ORANGE3_CLOUD_SETTINGS_FILE`` (an ``.ini`` path) uses another file instead,
for tests and scripts that shouldn't touch the user's settings.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from AnyQt.QtCore import QSettings

__all__ = [
    "DEFAULT_IDLE_TIMEOUT",
    "DEFAULT_MAX_LIFETIME",
    "DISK_DEFAULT_GB",
    "DISK_MAX_GB",
    "DISK_MIN_GB",
    "GROUP",
    "HOSTED_INTEREST_URL",
    "CloudSettings",
    "ProviderSettings",
    "hosted_interest_url",
    "settings_store",
]

#: The group of the add-on's keys in Orange's settings.
GROUP = "orange3-cloud"

#: Orange's settings store (``Orange.canvas.config.Config``).
_ORANGE_ORGANIZATION = "biolab.si"
_ORANGE_APPLICATION = "Orange"

#: Seconds without requests or jobs before the machine stops by itself.
DEFAULT_IDLE_TIMEOUT = 30 * 60
#: Seconds after which the machine stops, even with jobs running.
DEFAULT_MAX_LIFETIME = 12 * 60 * 60

#: Boot disk size in GB: the default and the range the user can choose.
DISK_DEFAULT_GB = 100
DISK_MIN_GB = 50
DISK_MAX_GB = 2000

#: The page where users can ask to hear about a hosted version, opened by the
#: Cloud settings dialog's "Notify me about the hosted version" button: the
#: roadmap issue for a hosted service, where people upvote it and subscribe to
#: its updates. Empty disables the button. The key ``hosted_interest_url`` in
#: the add-on's settings group overrides it.
HOSTED_INTEREST_URL = "https://github.com/ggaammdev/orange3-cloud/issues/13"


def settings_store() -> QSettings:
    """Orange's settings store.

    In Orange, ``QSettings()`` is Orange's settings file (the canvas sets the
    organization, application name and INI format at startup). Elsewhere
    (tests, a Python shell) the same file is opened by name.
    """
    from AnyQt.QtCore import QCoreApplication, QSettings

    path = os.environ.get("ORANGE3_CLOUD_SETTINGS_FILE")
    if path:
        return QSettings(path, QSettings.Format.IniFormat)
    if QCoreApplication.applicationName() == _ORANGE_APPLICATION:
        return QSettings()
    return QSettings(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        _ORANGE_ORGANIZATION,
        _ORANGE_APPLICATION,
    )


def hosted_interest_url(store: QSettings | None = None) -> str:
    """The hosted-interest page's URL (http or https), or "" when none is set."""
    store = store if store is not None else settings_store()
    url = _str(store.value(f"{GROUP}/hosted_interest_url")) or HOSTED_INTEREST_URL
    return url if url.startswith(("https://", "http://")) else ""


def _int(value: object, default: int) -> int:
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _disk_gb(value: object) -> int:
    gb = _int(value, DISK_DEFAULT_GB)
    return gb if DISK_MIN_GB <= gb <= DISK_MAX_GB else DISK_DEFAULT_GB


@dataclass
class ProviderSettings:
    """The user's choices for one machine provider."""

    #: The account machines run in, e.g. a Google Cloud project's ID ("": not
    #: chosen yet).
    account: str = ""
    region: str = ""
    zone: str = ""
    #: The machine size's name ("": the provider's default).
    machine_type: str = ""
    #: Boot disk size in GB.
    disk_gb: int = DISK_DEFAULT_GB

    @classmethod
    def from_values(cls, values: dict[str, object]) -> ProviderSettings:
        """Settings from stored values (defaults for missing or invalid ones)."""
        return cls(
            account=_str(values.get("account")),
            region=_str(values.get("region")),
            zone=_str(values.get("zone")),
            machine_type=_str(values.get("machine_type")),
            disk_gb=_disk_gb(values.get("disk_gb")),
        )


#: The keys of :class:`ProviderSettings`.
_PROVIDER_KEYS = tuple(f.name for f in fields(ProviderSettings))
#: Keys saved by versions with only Google Cloud: key -> (provider, key).
#: They are read when the provider's own keys don't exist yet, and removed
#: when the settings are saved.
_OLD_KEYS = {
    "gcp_project": ("gcp", "account"),
    "gcp_region": ("gcp", "region"),
    "gcp_zone": ("gcp", "zone"),
    "gcp_machine_type": ("gcp", "machine_type"),
    "gcp_disk_gb": ("gcp", "disk_gb"),
}


def _load_providers(store: QSettings) -> dict[str, ProviderSettings]:
    """Each provider's settings from the group ``providers/<id>`` (in the
    add-on's group), else from the old keys."""
    values: dict[str, dict[str, object]] = {}
    for old, (provider, key) in _OLD_KEYS.items():
        if store.contains(old):
            values.setdefault(provider, {})[key] = store.value(old)
    store.beginGroup("providers")
    try:
        for provider in store.childGroups():
            store.beginGroup(provider)
            values[provider] = {key: store.value(key) for key in _PROVIDER_KEYS}
            store.endGroup()
    finally:
        store.endGroup()
    return {provider: ProviderSettings.from_values(v) for provider, v in values.items()}


@dataclass
class CloudSettings:
    """The user's choices. Time limits are seconds; 0 turns a limit off."""

    #: Provider id (see :mod:`~orangecontrib.cloud.client.providers.registry`).
    provider: str = "local"
    #: Seconds without requests or jobs before the machine stops by itself.
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT
    #: Seconds after which the machine stops, even with jobs running.
    max_lifetime: int = DEFAULT_MAX_LIFETIME
    #: Each provider's choices, by provider id (see :meth:`of`).
    providers: dict[str, ProviderSettings] = field(default_factory=dict)

    def of(self, provider_id: str) -> ProviderSettings:
        """The choices for provider ``provider_id`` (defaults if none yet)."""
        return self.providers.setdefault(provider_id, ProviderSettings())

    @classmethod
    def load(cls, store: QSettings | None = None) -> CloudSettings:
        """The settings from Orange's settings store (defaults for missing or invalid values)."""
        store = store if store is not None else settings_store()
        default = cls()
        store.beginGroup(GROUP)
        try:
            return cls(
                provider=_str(store.value("provider")) or default.provider,
                idle_timeout=_int(store.value("idle_timeout"), default.idle_timeout),
                max_lifetime=_int(store.value("max_lifetime"), default.max_lifetime),
                providers=_load_providers(store),
            )
        finally:
            store.endGroup()

    def save(self, store: QSettings | None = None) -> None:
        """Write the settings to Orange's settings store."""
        store = store if store is not None else settings_store()
        store.beginGroup(GROUP)
        try:
            store.setValue("provider", self.provider)
            store.setValue("idle_timeout", int(self.idle_timeout))
            store.setValue("max_lifetime", int(self.max_lifetime))
            for provider, chosen in self.providers.items():
                for key in _PROVIDER_KEYS:
                    store.setValue(f"providers/{provider}/{key}", getattr(chosen, key))
            for old in _OLD_KEYS:
                store.remove(old)
        finally:
            store.endGroup()
        store.sync()
