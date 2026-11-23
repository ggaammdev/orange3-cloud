"""Cloud File's URLs: several files in one URL, the checks before loading,
the memory estimate of files on the web or in the cloud's storage, and what
the machine is asked to load.

A URL may name several files with the same columns, loaded as one table, with
a numbered pattern in braces as in a shell: ``…/trips_2025-{01..12}.parquet``
names the twelve files ``…/trips_2025-01.parquet`` to ``…_2025-12.parquet``
(numbers keep the width of the first one), and ``…/{north,south}.csv`` names
two. A URL may have several patterns: ``…/{2024,2025}-{01..12}.parquet`` names
24 files.
"""

from __future__ import annotations

import itertools
import logging
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlsplit

from orangecontrib.cloud.client.api import CloudCancelled
from orangecontrib.cloud.client.providers.base import ClusterProvider, MachineHandle
from orangecontrib.cloud.data.estimate import (
    MemoryEstimate,
    combine_estimates,
    estimate_stored,
    estimate_url,
    file_format,
)
from orangecontrib.cloud.protocol.errors import ErrorCode
from orangecontrib.cloud.protocol.tables import MAX_FILES, without_extensions
from orangecontrib.cloud.widgets.base import CloudTask

__all__ = [
    "WEB_SCHEMES",
    "estimate_remote",
    "expand_pattern",
    "machine_source",
    "or_list",
    "pattern_name",
    "upload_progress",
    "url_problem",
]

log = logging.getLogger(__name__)

#: URL schemes every machine loads.
WEB_SCHEMES = ("https://", "http://")

#: A pattern: ``{a..b}`` (numbers) or ``{x,y,...}`` (any text without braces).
_PATTERN = re.compile(r"\{(-?\d+)\.\.(-?\d+)\}|\{([^{}]*,[^{}]*)\}")

#: Files whose memory is estimated at the same time.
ESTIMATE_THREADS = 8


def or_list(items: list[str]) -> str:
    """E.g. "a, b or c"."""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1]


def _choices(match: re.Match) -> list[str]:
    """The texts one pattern stands for."""
    if match.group(3) is not None:
        return match.group(3).split(",")
    first, last = match.group(1), match.group(2)
    width = len(first) if first.lstrip("-").startswith("0") else 0
    a, b = int(first), int(last)
    step = 1 if b >= a else -1
    if abs(b - a) >= MAX_FILES:
        raise ValueError(f"{match.group(0)} names more than {MAX_FILES:,} files.")
    return [str(k).zfill(width) for k in range(a, b + step, step)]


def expand_pattern(text: str) -> list[str]:
    """The files a URL names: itself, or each one its patterns stand for, in
    order (the first pattern changes slowest). Raises ``ValueError`` for
    unpaired braces and more than :data:`MAX_FILES` files."""
    matches = list(_PATTERN.finditer(text))
    rest = _PATTERN.sub("", text)
    if "{" in rest or "}" in rest:
        raise ValueError(
            "Use {01..12} for numbered files or {a,b} for a list in the URL, with matching braces."
        )
    if not matches:
        return [text]
    choices = [_choices(m) for m in matches]
    count = 1
    for options in choices:
        count *= len(options)
    if count > MAX_FILES:
        raise ValueError(f"The URL names {count:,} files; at most {MAX_FILES:,} load as one table.")
    parts = _PATTERN.split(text)[:: _PATTERN.groups + 1]  # the text around the patterns
    files = []
    for chosen in itertools.product(*choices):
        pieces = [parts[0]]
        for value, after in zip(chosen, parts[1:], strict=True):
            pieces += [value, after]
        files.append("".join(pieces))
    return files


def pattern_name(text: str) -> str:
    """The name of the table loaded from a URL: its file name without the
    extensions, e.g. "trips_2025-{01..12}"."""
    return without_extensions(urlsplit(text).path.rsplit("/", 1)[-1] or text) or "table"


def url_problem(url: str, schemes: tuple[str, ...]) -> str | None:
    """Why ``url`` (a file, or several in a pattern) can't be loaded
    (``None``: it can). ``schemes``: those the machine's provider accepts."""
    if not url.lower().startswith(schemes):
        return f"The URL must start with {or_list(list(schemes))}."
    parts = urlsplit(url)
    if parts.username or parts.password:
        return "Don't put a user name or password in the URL; workflows save it."
    try:
        formats = {file_format(name)[0] for name in expand_pattern(url)}
    except ValueError as exc:
        return str(exc)
    if len(formats) > 1:
        return "The files must all be CSV files or all Parquet files."
    return None


def _estimate_one(location: str, provider: ClusterProvider | None) -> MemoryEstimate | None:
    if provider is not None and location.lower().startswith(provider.storage_schemes):
        return estimate_stored(location, provider.read_stored_head)
    return estimate_url(location)


def estimate_remote(
    task: CloudTask, location: str, provider: ClusterProvider | None
) -> MemoryEstimate | None:
    """In the worker thread: estimate the memory of a file (or the files of a
    pattern) at a URL or in the provider's storage."""
    files = expand_pattern(location)
    task.set_status(
        "Estimating the memory the files need" if len(files) > 1 else
        "Estimating the memory the file needs"
    )  # fmt: skip
    try:
        with ThreadPoolExecutor(min(ESTIMATE_THREADS, len(files))) as pool:
            estimates = list(pool.map(lambda f: _estimate_one(f, provider), files))
    except Exception as exc:  # noqa: BLE001 - the machine may still read it; it says why if not
        log.info("No memory estimate for %s: %s", location, exc)
        return None
    return combine_estimates(estimates)


def machine_source(
    provider: ClusterProvider | None, location: str, handle: MachineHandle | None
) -> dict[str, Any]:
    """``create_table``'s source for a location (or the files of a pattern)
    in the provider's storage or on the web. A link the provider made for the
    machine (e.g. a signed one, which may expire before a long load ends) is
    copied to the machine's disk first."""
    files = expand_pattern(location)
    links = files if provider is None else [provider.machine_location(f, handle) for f in files]
    copy_first = links != files
    if len(files) == 1:
        return {"uri": links[0], "copy_first": copy_first}
    return {"uris": links, "copy_first": copy_first, "name": pattern_name(location)}


def upload_progress(task: CloudTask, share: float) -> Callable[[int, int | None], None]:
    """An upload's ``on_progress``: fills ``share`` of the progress bar and
    stops the upload when the task is cancelled."""

    def on_progress(sent: int, total: int | None) -> None:
        if task.is_cancelled():
            raise CloudCancelled(ErrorCode.CANCELLED, "The upload was cancelled.")
        if total:
            task.set_progress(share * sent / total)

    return on_progress
