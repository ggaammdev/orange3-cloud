"""The results of a cloud's setup checker, the same for every cloud.

A cloud's checker (e.g. :mod:`~orangecontrib.cloud.client.providers.
gcp_setup`) returns a list of :class:`Check`: whether each requirement is
met, what was found, how to fix it and where. Its page in the Cloud settings
dialog shows them before Start.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

__all__ = ["Check", "Status", "all_passed", "not_checked"]


class Status(Enum):
    """The outcome of a setup check."""

    PASSED = "passed"
    FAILED = "failed"
    #: Couldn't be checked (an earlier check failed, or the call failed).
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Check:
    """One check's result."""

    #: Stable identifier within a cloud's checker, e.g. ``credentials``.
    key: str
    title: str
    status: Status
    #: What was found.
    detail: str = ""
    #: How to fix it (empty when passed).
    fix: str = ""
    #: Where to fix it.
    link: str = ""
    #: The missing items (APIs, permissions), if any.
    missing: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        """True if the check passed."""
        return self.status is Status.PASSED


def all_passed(checks: Iterable[Check]) -> bool:
    """True if there are checks and all of them passed."""
    checks = list(checks)
    return bool(checks) and all(c.passed for c in checks)


def not_checked(key: str, title: str, error: Exception | str, link: str = "") -> Check:
    """A check that couldn't be made because ``error`` happened."""
    return Check(key, title, Status.UNKNOWN, detail=f"Couldn't check: {error}", link=link)
