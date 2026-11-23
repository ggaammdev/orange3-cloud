"""Work in forked processes (Linux only): results, errors, out of memory,
crashes and stopping a child."""

import os
import signal
import threading
import time

import numpy as np
import pytest

from orangecontrib.cloud.server import forked
from orangecontrib.cloud.server.forked import (
    ForkedParts,
    PartCrashed,
    PartFailed,
    PartKilled,
    PartStopped,
)

pytestmark = pytest.mark.skipif(not forked.available(), reason="forked processes: Linux only")

#: Shared with the children copy-on-write (set before the zygote forks).
TABLE = np.arange(1_000_000, dtype=float)


def work(args):
    """What the children run: ``args`` is (what, value)."""
    what, value = args
    if what == "sum":
        return float(TABLE[: int(value)].sum()), os.getpid()
    if what == "fail":
        raise ValueError(value)
    if what == "memory":
        raise MemoryError()
    if what == "signal":
        os.kill(os.getpid(), value)
    if what == "sleep":
        time.sleep(value)
    return None


@pytest.fixture
def parts():
    parts = ForkedParts(work)
    parts.start()
    yield parts
    parts.close()


def test_results_and_errors(parts):
    total, pid = parts.run("a", ("sum", 10))
    assert total == 45.0 and pid not in (os.getpid(), parts.zygote)
    with pytest.raises(PartFailed, match="ValueError: bad"):
        parts.run("b", ("fail", "bad"))
    with pytest.raises(MemoryError):
        parts.run("c", ("memory", None))
    with pytest.raises(PartKilled):  # SIGKILL from outside: the OOM killer
        parts.run("d", ("signal", signal.SIGKILL))
    with pytest.raises(PartCrashed, match=r"SIGSEGV(.|\n)*Segmentation fault"):
        parts.run("e", ("signal", signal.SIGSEGV))
    # Many at once, from threads.
    results = {}
    threads = [
        threading.Thread(target=lambda i=i: results.update({i: parts.run(f"t{i}", ("sum", i))}))
        for i in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert {i: r[0] for i, r in results.items()} == {i: float(sum(range(i))) for i in range(8)}
    assert not os.listdir(parts.folder)  # result files are deleted


def _in_thread(parts, name, errors):
    def wait():
        try:
            parts.run(name, ("sleep", 60))
        except PartStopped as exc:
            errors.append(exc)

    thread = threading.Thread(target=wait)
    thread.start()
    time.sleep(0.5)
    return thread


def test_stop_and_close(parts):
    stop = threading.Event()
    started = time.monotonic()
    threading.Timer(0.5, stop.set).start()
    with pytest.raises(PartStopped):  # as for a cancelled job
        parts.run("a", ("sleep", 60), should_stop=stop.is_set)
    assert time.monotonic() - started < 10
    errors = []
    thread = _in_thread(parts, "b", errors)
    assert parts.memory("b") > 0 and parts.memory() >= parts.memory("b")
    parts.stop("b")  # as when the processes use too much memory
    thread.join(10)
    assert len(errors) == 1 and not thread.is_alive()
    thread = _in_thread(parts, "c", errors)
    zygote = parts.zygote
    parts.close()
    thread.join(10)
    assert len(errors) == 2 and not thread.is_alive()
    with pytest.raises(ProcessLookupError):
        os.kill(zygote, 0)
