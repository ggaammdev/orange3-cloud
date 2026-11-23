"""Describe the user's Python environment so the cloud machine can copy it.

:func:`collect_environment` lists the installed distributions and sorts them
into three groups:

* **required**: Orange, the core scientific stack, this add-on, every other
  Orange add-on, and everything they depend on. The machine must install
  exactly these versions.
* **best effort**: everything else that is installed. The machine tries to
  install these, and reports the ones that fail.
* **excluded**: packages that can't be copied to a Linux machine: Qt bindings,
  operating-system-specific packages, and editable or local installs.

Dependencies are followed through ``Requires-Dist`` with environment markers
evaluated for the target machine (Linux, the user's Python version), not for
the user's computer.
"""

from __future__ import annotations

import contextlib
import json
import platform
import re
import sys
from collections import deque
from importlib import metadata
from typing import Any

from packaging.markers import (
    InvalidMarker,
    UndefinedComparison,
    UndefinedEnvironmentName,
)
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from pydantic import BaseModel, Field

__all__ = [
    "EnvironmentSpec",
    "ExcludedPackage",
    "Package",
    "collect_environment",
]

SCHEMA_VERSION = 1

#: This add-on's own distribution name.
SELF_NAME = "orange3-cloud"

#: Packages that are always required (when installed), besides add-ons.
CORE_PACKAGES = ("orange3", "numpy", "scipy", "scikit-learn", "pandas", SELF_NAME)

#: Entry point group that Orange add-ons register.
ADDON_ENTRY_POINT_GROUP = "orange3.addon"

_QT_REASON = (
    "Qt binding: the cloud machine has no GUI, and Orange's interface stays on your computer"
)
_OS_REASON = "specific to the operating system of your computer"

#: (pattern on the PEP 503 name, reason) for packages that are never copied.
EXCLUDE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^pyqt\d.*$"), _QT_REASON),  # PyQt5/6, -sip, -Qt6, -WebEngine...
    (re.compile(r"^pyqt-builder$"), _QT_REASON),
    (re.compile(r"^pyside\d*(-.*)?$"), _QT_REASON),
    (re.compile(r"^shiboken\d*(-.*)?$"), _QT_REASON),
    (re.compile(r"^pyobjc(-.*)?$"), _OS_REASON + " (macOS)"),
    (re.compile(r"^appnope$"), _OS_REASON + " (macOS)"),
    (re.compile(r"^macholib$"), _OS_REASON + " (macOS)"),
    (re.compile(r"^pywin32(-ctypes)?$"), _OS_REASON + " (Windows)"),
    (re.compile(r"^pywinpty$"), _OS_REASON + " (Windows)"),
    (re.compile(r"^comtypes$"), _OS_REASON + " (Windows)"),
    (re.compile(r"^wmi$"), _OS_REASON + " (Windows)"),
    (re.compile(r"^pyreadline3?$"), _OS_REASON + " (Windows)"),
)

#: Marker environment of the cloud machine. The Python version is filled in
#: from the running interpreter; everything else describes the Linux image.
_TARGET_MARKERS = {
    "os_name": "posix",
    "sys_platform": "linux",
    "platform_system": "Linux",
    "platform_machine": "x86_64",
    "platform_release": "",
    "platform_version": "",
    "platform_python_implementation": "CPython",
    "implementation_name": "cpython",
}


class Package(BaseModel):
    """An installed distribution the machine should install."""

    name: str  #: PEP 503 normalized name, e.g. ``scikit-learn``
    version: str
    #: True when the user's copy is an editable or local install. Only used for
    #: this add-on itself, which the machine installs from its own source.
    local: bool = False

    @property
    def pin(self) -> str:
        """Requirement string pinning the exact version, e.g. ``numpy==2.1.0``."""
        return f"{self.name}=={self.version}"


class ExcludedPackage(BaseModel):
    """An installed distribution that can't be copied to the machine."""

    name: str
    version: str
    reason: str


class EnvironmentSpec(BaseModel):
    """What the cloud machine must install to match the user's environment."""

    schema_version: int = SCHEMA_VERSION
    python_version: str  #: full version, e.g. ``3.12.7``
    python_implementation: str = "CPython"
    platform: str  #: ``sys.platform`` of the user's computer, e.g. ``darwin``
    machine: str  #: ``platform.machine()``, e.g. ``arm64``
    required: list[Package] = Field(default_factory=list)
    best_effort: list[Package] = Field(default_factory=list)
    excluded: list[ExcludedPackage] = Field(default_factory=list)

    def to_json(self, **kwargs: Any) -> str:
        """The spec as JSON (``model_dump_json``'s options)."""
        return self.model_dump_json(**kwargs)

    @classmethod
    def from_json(cls, data: str | bytes) -> EnvironmentSpec:
        """A spec from its JSON."""
        return cls.model_validate_json(data)

    def get(self, name: str) -> Package | ExcludedPackage | None:
        """Find a package by name in any of the three groups."""
        key = canonicalize_name(name)
        for pkg in (*self.required, *self.best_effort, *self.excluded):
            if pkg.name == key:
                return pkg
        return None


def _direct_url(dist: metadata.Distribution) -> dict | None:
    try:
        text = dist.read_text("direct_url.json")
        return json.loads(text) if text else None
    except (OSError, ValueError):
        return None


def _local_install_reason(dists: list[metadata.Distribution]) -> str | None:
    """Return why a distribution is a local install (PEP 610), or None."""
    for dist in dists:
        info = _direct_url(dist)
        if not info:
            continue
        if info.get("dir_info", {}).get("editable"):
            return "installed in editable mode from a local folder"
        if str(info.get("url", "")).startswith("file:"):
            return "installed from a local file or folder"
    return None


def _exclude_reason(name: str) -> str | None:
    for pattern, reason in EXCLUDE_RULES:
        if pattern.match(name):
            return reason
    return None


def _target_environment(extra: str = "") -> dict[str, str]:
    env = dict(_TARGET_MARKERS)
    env["python_version"] = "{}.{}".format(*sys.version_info[:2])
    env["python_full_version"] = platform.python_version()
    env["implementation_version"] = platform.python_version()
    env["extra"] = extra
    return env


def _applies(req: Requirement, extras: frozenset[str]) -> bool:
    """Does ``req`` apply on the target machine, given the requested extras?"""
    if req.marker is None:
        return True
    try:
        return any(req.marker.evaluate(_target_environment(e)) for e in ("", *extras))
    except (InvalidMarker, UndefinedComparison, UndefinedEnvironmentName):
        return True  # can't tell: follow it, so nothing is missed


def _requirements(dist: metadata.Distribution) -> list[Requirement]:
    reqs = []
    for line in dist.requires or ():
        with contextlib.suppress(InvalidRequirement):
            reqs.append(Requirement(line))
    return reqs


def _installed() -> dict[str, list[metadata.Distribution]]:
    """Installed distributions by normalized name, in ``sys.path`` order.

    The first one is the one Python imports (an extra copy can show up, e.g.
    an ``.egg-info`` folder in the current directory).
    """
    found: dict[str, list[metadata.Distribution]] = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if not name or not dist.version:
            continue
        found.setdefault(canonicalize_name(name), []).append(dist)
    return found


def _addon_names() -> set[str]:
    names = set()
    for ep in metadata.entry_points(group=ADDON_ENTRY_POINT_GROUP):
        if ep.dist is not None and ep.dist.metadata["Name"]:
            names.add(canonicalize_name(ep.dist.metadata["Name"]))
    return names


def _required_closure(
    roots: set[str], installed: dict[str, list[metadata.Distribution]]
) -> set[str]:
    """Names of the roots and everything they depend on (installed only)."""
    seen_extras: dict[str, frozenset[str]] = {}
    queue = deque((name, frozenset()) for name in roots)
    while queue:
        name, extras = queue.popleft()
        if name not in installed:
            continue  # not installed here; the machine resolves it itself
        known = seen_extras.get(name)
        if known is not None and extras <= known:
            continue
        extras = extras | (known or frozenset())
        seen_extras[name] = extras
        for req in _requirements(installed[name][0]):
            if _applies(req, extras):
                queue.append((canonicalize_name(req.name), frozenset(req.extras)))
    return set(seen_extras)


def collect_environment() -> EnvironmentSpec:
    """Describe the running Python environment as an :class:`EnvironmentSpec`."""
    installed = _installed()
    roots = {canonicalize_name(n) for n in CORE_PACKAGES}
    roots |= {n for n in installed if n.startswith("orange3-")}
    roots |= _addon_names()
    required_names = _required_closure(roots, installed)

    required, best_effort, excluded = [], [], []
    for name in sorted(installed):
        dists = installed[name]
        version = dists[0].version
        reason = _exclude_reason(name)
        local_reason = _local_install_reason(dists)
        if reason is None and local_reason is not None:
            if name == SELF_NAME:
                # The machine installs this add-on from its own source, wheel
                # or the package index, so it stays required.
                required.append(Package(name=name, version=version, local=True))
                continue
            reason = local_reason
        if reason is not None:
            excluded.append(ExcludedPackage(name=name, version=version, reason=reason))
        elif name in required_names:
            required.append(Package(name=name, version=version))
        else:
            best_effort.append(Package(name=name, version=version))

    return EnvironmentSpec(
        python_version=platform.python_version(),
        python_implementation=platform.python_implementation(),
        platform=sys.platform,
        machine=platform.machine(),
        required=required,
        best_effort=best_effort,
        excluded=excluded,
    )
