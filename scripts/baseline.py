#!/usr/bin/env python3
"""Baseline benchmark: where Orange on one computer hits its limits.

Generates synthetic CSV files (30 numeric columns and a discrete class) of
growing size and runs two operations on each with Orange's own library:

- ``load``: read the CSV the way Orange's File widget does (``CSVReader``),
- ``rf_cv``: Random Forest (Orange's defaults) with 5-fold cross-validation.

With the ``local`` backend, every operation runs in a child process, so its
time and peak memory are measured on their own. A child is stopped after
``--timeout`` seconds, or when it runs out of memory: a ``MemoryError``, a
kill by the operating system, resident memory above ``--max-rss``, or the
system swap growing by more than ``--max-swap-growth``. Once an operation
fails at one size, it is not tried at bigger sizes. Generated files are
deleted afterwards, also on errors and Ctrl-C.

With the ``cloud`` backend, the same operations run on a cloud machine
through the add-on: the machine starts at the first operation and is deleted
at the end. The file goes to the provider's storage first (a bucket on
Google Cloud) or straight to the machine (local Docker). ``load`` loads it
with the machine's loader, and ``rf_cv`` evaluates the same learner on the
loaded table with an ``evaluate`` job (the folds run in parallel). The time
is the job's run time on the machine, the peak memory is the most memory in
use on the whole machine during the job (from ``GET /v1/info``), and the
cost is the run time times the machine's price.

Backends live in ``BACKENDS``: a backend runs one operation on one CSV file
and returns a result dict, so other backends can be added without changing
the rest of the script.

Examples::

    python scripts/baseline.py                          # 1M, 5M, 20M rows
    python scripts/baseline.py --sizes 10k --timeout 60 --out results.json
    python scripts/baseline.py --backend cloud --provider gcp \\
        --machine-type n2-standard-16 --data-dir ~/baseline-data
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback

import numpy as np
import psutil

N_FEATURES = 30
CHUNK_ROWS = 200_000
BYTES_PER_ROW = 240  # a little above what the generator writes, for the disk check
OPERATIONS = ("load", "rf_cv")
POLL_SECONDS = 0.2


# --- Data ------------------------------------------------------------------


def parse_size(text: str) -> int:
    text = text.strip().lower()
    mult = {"k": 1_000, "m": 1_000_000, "g": 1_000_000_000}.get(text[-1:], 1)
    return int(float(text.rstrip("kmg")) * mult)


def format_size(rows: int) -> str:
    for suffix, mult in (("M", 1_000_000), ("k", 1_000)):
        if rows >= mult and rows % mult == 0:
            return f"{rows // mult}{suffix}"
    return str(rows)


def generate_csv(path: str, rows: int, seed: int, orange_header: bool = True) -> None:
    """Write a CSV in chunks, to keep memory low: with Orange's three header
    rows, or with only the names (the same data rows)."""
    free = shutil.disk_usage(os.path.dirname(path)).free
    if free < rows * BYTES_PER_ROW * 1.1:
        raise RuntimeError(
            f"not enough disk space for {format_size(rows)} rows in {os.path.dirname(path)}"
        )
    rng = np.random.default_rng(seed)
    weights = rng.standard_normal(N_FEATURES)
    names = [f"x{i}" for i in range(N_FEATURES)] + ["y"]
    with open(path, "w") as f:
        f.write(",".join(names) + "\n")
        if orange_header:
            f.write(",".join(["continuous"] * N_FEATURES + ["discrete"]) + "\n")
            f.write(",".join([""] * N_FEATURES + ["class"]) + "\n")
        for start in range(0, rows, CHUNK_ROWS):
            n = min(CHUNK_ROWS, rows - start)
            x = rng.standard_normal((n, N_FEATURES))
            noise = rng.standard_normal(n) * 2
            y = (x @ weights + noise > 0).astype(float)
            np.savetxt(
                f, np.column_stack([x, y]), fmt=["%.4f"] * N_FEATURES + ["%d"], delimiter=","
            )


# --- Operations (run inside the child process of the local backend) ---------


def _load(path):
    from Orange.data.io import CSVReader

    return CSVReader(path).read()


def op_load(path: str) -> dict:
    import Orange.data.io  # noqa: F401 -- import before timing

    t0 = time.perf_counter()
    data = _load(path)
    return {"seconds": time.perf_counter() - t0, "rows": len(data)}


def op_rf_cv(path: str) -> dict:
    from Orange.classification import RandomForestLearner
    from Orange.evaluation import CA, CrossValidation

    data = _load(path)
    t0 = time.perf_counter()
    results = CrossValidation(k=5)(data, [RandomForestLearner()])
    seconds = time.perf_counter() - t0
    if results.failed[0]:
        raise results.failed[0]
    return {"seconds": seconds, "rows": len(data), "ca": float(CA(results)[0])}


LOCAL_OPERATIONS = {"load": op_load, "rf_cv": op_rf_cv}


def _is_memory_error(exc: BaseException | None) -> bool:
    while exc is not None:
        if isinstance(exc, MemoryError):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def worker(op: str, path: str, result_path: str) -> int:
    try:
        result = {"status": "ok", **LOCAL_OPERATIONS[op](path)}
    except BaseException as e:  # noqa: BLE001 - the child's boundary: reported to the parent
        status = "oom" if _is_memory_error(e) else "error"
        result = {"status": status, "error": f"{type(e).__name__}: {e}"}
        traceback.print_exc()
    with open(result_path, "w") as f:
        json.dump(result, f)
    return 0


# --- Backends ----------------------------------------------------------------


def _tree_rss(proc: psutil.Process) -> int:
    total = 0
    for p in [proc, *proc.children(recursive=True)]:
        with contextlib.suppress(psutil.Error):
            total += p.memory_info().rss
    return total


def _kill_group(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def _over_limit(seconds: float, peak: int, swap_growth: int, args: argparse.Namespace) -> tuple:
    """(status, reason) when a child went over a limit, else (None, None)."""
    if seconds > args.timeout:
        return "timeout", "over --timeout"
    if peak > args.max_rss:
        return "oom", "resident memory over --max-rss"
    if swap_growth > args.max_swap_growth:
        return "oom", "system swap grew over --max-swap-growth"
    return None, None


def run_local(op: str, path: str, args: argparse.Namespace) -> dict:
    """Run one operation in a child process and measure its time and peak memory."""
    result_path = path + f".{op}.json"
    cmd = [sys.executable, os.path.abspath(__file__), "--_worker", op, path, result_path]
    swap0 = psutil.swap_memory().used
    t0 = time.perf_counter()
    log_path = path + f".{op}.log"
    with open(log_path, "wb") as log_file:
        child = subprocess.Popen(
            cmd, start_new_session=True, stdout=log_file, stderr=subprocess.STDOUT
        )
    proc = psutil.Process(child.pid)
    peak = swap_growth = 0
    killed = reason = None
    rusage = None
    try:
        while True:
            pid, status, rusage = os.wait4(child.pid, os.WNOHANG)
            if pid:
                child.returncode = os.waitstatus_to_exitcode(status)
                break
            with contextlib.suppress(psutil.Error):
                peak = max(peak, _tree_rss(proc))
            swap_growth = max(swap_growth, psutil.swap_memory().used - swap0)
            killed, reason = _over_limit(time.perf_counter() - t0, peak, swap_growth, args)
            if killed:
                _kill_group(child.pid)
                _, status, rusage = os.wait4(child.pid, 0)
                child.returncode = os.waitstatus_to_exitcode(status)
                break
            time.sleep(POLL_SECONDS)
    finally:
        if child.returncode is None:
            _kill_group(child.pid)
            child.wait()
    wall = time.perf_counter() - t0
    if rusage is not None:
        # ru_maxrss is in bytes on macOS and in kilobytes on Linux.
        scale = 1 if sys.platform == "darwin" else 1024
        peak = max(peak, rusage.ru_maxrss * scale)

    result: dict = {"wall_seconds": wall, "peak_rss_bytes": peak, "swap_growth_bytes": swap_growth}
    if killed:
        result.update(status=killed, reason=reason)
    elif os.path.exists(result_path):
        with open(result_path) as f:
            result.update(json.load(f))
        os.remove(result_path)
    elif child.returncode == -signal.SIGKILL:
        result["status"] = "oom"  # killed by the operating system
    else:
        result.update(status="error", error=f"exit code {child.returncode}")
    if result["status"] == "error":
        with open(log_path, errors="replace") as f:
            result["log_tail"] = f.read()[-1000:]
    os.remove(log_path)
    return result


class LocalBackend:
    """Runs each operation in a child process on this computer."""

    orange_header = True

    def __init__(self, args: argparse.Namespace):
        self.args = args

    def run(self, op: str, path: str) -> dict:
        """Run ``op`` on the file at ``path``."""
        return run_local(op, path, self.args)

    def done(self, path: str) -> None:
        """All operations on ``path`` have run."""

    def close(self) -> None:
        """Release what the backend holds."""


class MachineMemory:
    """The most memory in use on the cloud machine while a job runs, sampled
    from ``GET /v1/info`` in a thread (``with MachineMemory(client) as m:``)."""

    def __init__(self, client, interval: float = 1.0):
        self.client = client
        self.interval = interval
        self.peak = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="machine-memory", daemon=True)

    def _run(self) -> None:
        from orangecontrib.cloud.client.api import CloudError

        while True:
            try:
                info = self.client.info()
            except CloudError as exc:  # a missed sample isn't fatal; say so
                log(f"  (memory sample failed: {exc})")
            else:
                self.peak = max(self.peak, info.memory_total_bytes - info.memory_free_bytes)
            if self._stop.wait(self.interval):
                return

    def __enter__(self) -> MachineMemory:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join()


class CloudBackend:
    """Runs the operations on a cloud machine through the add-on. Its files
    have plain CSV headers (only the names), and the class column is set
    when loading."""

    orange_header = False

    def __init__(self, args: argparse.Namespace):
        from orangecontrib.cloud.client.providers.gcp import OwnGCPProvider
        from orangecontrib.cloud.client.providers.registry import get_provider

        self.args = args
        if args.provider == "gcp":
            self.provider = OwnGCPProvider(
                project=os.environ.get("ORANGE3_CLOUD_GCP_PROJECT"),
                zone=os.environ.get("ORANGE3_CLOUD_GCP_ZONE"),
            )
            self.provider.disk_gb = 100
        else:
            self.provider = get_provider(args.provider)
        self.handle = None
        self.client = None
        self.price: float | None = None
        self.started = 0.0
        self.idle_bytes = 0  # memory in use on the machine before any work
        self.tables: dict[str, str] = {}  # file -> table ID on the machine
        self.staged: dict[str, str] = {}  # file -> URI in the provider's storage

    def _start(self) -> None:
        from orangecontrib.cloud.client.api import CloudClient
        from orangecontrib.cloud.env import collect_environment

        log(f"starting a machine ({self.provider.label}, {self.args.machine_type or 'default'})")
        self.started = time.monotonic()
        self.handle = h = self.provider.start(
            collect_environment(), self.args.machine_type, idle_timeout=3600,
            max_lifetime=4 * 3600, on_status=lambda m: log(f"  {m}"),
        )  # fmt: skip
        self.client = CloudClient(h.url, h.token, h.cert_pem, server_hostname=h.server_hostname)
        self.price = self.provider.machine_price_per_hour(h)
        info = self.client.info()
        self.idle_bytes = info.memory_total_bytes - info.memory_free_bytes
        log(f"  started in {time.monotonic() - self.started:.0f} s: {info.machine}, "
            f"{info.cpu_count} CPUs, {info.memory_total_bytes / 2**30:.1f} GiB, "
            f"${self.price or 0:.2f}/h")  # fmt: skip

    def _source(self, path: str) -> tuple[dict, float]:
        """Where the machine loads ``path`` from, and the seconds the upload took."""
        t0 = time.perf_counter()
        if path not in self.staged:
            uri = self.provider.stage_file(self.handle, path)
            if uri is None:
                return {"upload_id": self.client.upload(path).id}, time.perf_counter() - t0
            self.staged[path] = uri
        return {"uri": self.staged[path]}, time.perf_counter() - t0

    def _wait(self, job):
        deadline = time.monotonic() + self.args.timeout
        return self.client.wait_for_job(
            job, should_cancel=lambda: time.monotonic() > deadline, max_poll_interval=2.0
        )

    def _load(self, path: str) -> dict:
        from orangecontrib.cloud.protocol.tables import ColumnRole, ColumnSpec, ColumnType

        source, upload_seconds = self._source(path)
        log(f"  file ready in {upload_seconds:.0f} s")
        with MachineMemory(self.client) as memory:
            target = ColumnSpec(name="y", role=ColumnRole.TARGET, type=ColumnType.CATEGORICAL)
            result = self._wait(self.client.create_table(columns=[target], **source))
        table = result.tables["table"]
        self.tables[path] = table.id
        return {
            "status": "ok", "seconds": result.runtime_seconds, "rows": table.n_rows,
            "upload_seconds": upload_seconds, "peak_rss_bytes": memory.peak,
            "table_bytes": table.memory_bytes,
        }  # fmt: skip

    def _rf_cv(self, path: str) -> dict:
        from Orange.classification import RandomForestLearner

        if path not in self.tables:
            self._load(path)
        params = {"method": "cross_validation", "folds": 5, "stratified": True}
        with MachineMemory(self.client) as memory:
            job = self.client.submit_job(
                "evaluate", inputs={"data": self.tables[path]}, params=params,
                objects={"learners": [RandomForestLearner()]},
            )  # fmt: skip
            result = self._wait(job)
        for obj in result.objects.values():  # the Results stay unused on the machine
            self.client.delete_object(obj.id)
        values = result.values
        if values["learners"][0]["failed"]:
            return {"status": "error", "error": "the learner failed"}
        ca = [s["id"] for s in values["scorers"]].index("CA")
        return {
            "status": "ok", "seconds": result.runtime_seconds, "rows": values["rows"],
            "ca": values["scores"][""][0][ca], "peak_rss_bytes": memory.peak,
            "parallel": values["parallel"], "threads_per_part": values["threads_per_part"],
        }  # fmt: skip

    def run(self, op: str, path: str) -> dict:
        """Run ``op`` on the file at ``path`` on the machine (started if needed)."""
        from orangecontrib.cloud.client.api import (
            CloudCancelled,
            CloudError,
            CloudOutOfMemoryError,
        )
        from orangecontrib.cloud.client.costs import cost_of

        if self.client is None:
            self._start()
        t0 = time.perf_counter()
        try:
            result = self._load(path) if op == "load" else self._rf_cv(path)
        except CloudCancelled:
            result = {"status": "timeout", "reason": "over --timeout"}
        except CloudOutOfMemoryError as exc:
            result = {"status": "oom", "error": str(exc)}
        except CloudError as exc:
            result = {"status": "error", "error": str(exc)}
        result["wall_seconds"] = time.perf_counter() - t0
        result["machine"] = self.handle.machine_type or self.handle.name
        result["machine_idle_bytes"] = self.idle_bytes
        result["cost_usd"] = cost_of(result.get("seconds"), self.price)
        return result

    def done(self, path: str) -> None:
        """Delete the table of ``path`` from the machine, to free its memory."""
        if self.client is not None and path in self.tables:
            self.client.delete_table(self.tables.pop(path))

    def close(self) -> None:
        """Delete the machine and, with ``--delete-uploads``, the uploaded files."""
        if self.handle is not None:
            from orangecontrib.cloud.client.costs import cost_of

            seconds = time.monotonic() - self.started
            self.client.close()
            log("deleting the machine")
            self.provider.delete(self.handle)
            cost = cost_of(seconds, self.price) or 0
            log(f"machine {self.handle.machine_type}: {seconds:.0f} s from start to delete, "
                f"about ${cost:.2f}")  # fmt: skip
        if self.args.delete_uploads and self.staged:
            log(f"deleting {len(self.staged)} uploaded files")
            self.provider.delete_stored_files(list(self.staged.values()))


BACKENDS = {"local": LocalBackend, "cloud": CloudBackend}


# --- Main ----------------------------------------------------------------------


def _data_file(workdir: str, rows: int, args: argparse.Namespace, orange_header: bool) -> str:
    """The CSV file of ``rows`` rows, generated unless ``--data-dir`` has it."""
    kind = "" if orange_header else "-plain"
    path = os.path.join(workdir, f"data-{format_size(rows)}-seed{args.seed}{kind}.csv")
    if args.data_dir and os.path.exists(path):
        log(f"{format_size(rows)} rows: reusing {path}")
    else:
        log(f"{format_size(rows)} rows: generating {path}")
        generate_csv(path, rows, args.seed, orange_header)
    return path


def run(args: argparse.Namespace) -> list[dict]:
    if args.data_dir:
        os.makedirs(args.data_dir, exist_ok=True)
        workdir = args.data_dir
    else:
        workdir = tempfile.mkdtemp(prefix="orange3-cloud-baseline-", dir=args.workdir)
    backend = BACKENDS[args.backend](args)
    failed: set[str] = set()
    results = []
    try:
        for rows in args.sizes:
            ops = [op for op in args.operations if op not in failed]
            if not ops:
                break
            path = _data_file(workdir, rows, args, backend.orange_header)
            for op in ops:
                if op == "rf_cv" and "load" in failed:
                    res = {"status": "skipped", "error": "loading failed"}
                else:
                    log(f"{format_size(rows)} rows: {op} ...")
                    res = backend.run(op, path)
                res = {"backend": args.backend, "rows": rows, "operation": op, **res}
                log(f"  -> {res['status']}, {fmt_seconds(res)}, {fmt_bytes(res)}")
                results.append(res)
                if res["status"] != "ok":
                    failed.add(op)
            backend.done(path)
            if not args.data_dir:
                os.remove(path)
    finally:
        backend.close()
        if not args.data_dir:
            shutil.rmtree(workdir, ignore_errors=True)
    return results


def fmt_seconds(res: dict) -> str:
    s = res.get("seconds", res.get("wall_seconds"))
    return "-" if s is None else f"{s:.1f} s"


def fmt_bytes(res: dict) -> str:
    b = res.get("peak_rss_bytes")
    return "-" if b is None else f"{b / 2**30:.2f} GiB"


def fmt_cost(res: dict) -> str:
    c = res.get("cost_usd")
    return "-" if c is None else f"${c:.3f}"


def markdown_table(results: list[dict]) -> str:
    cloud = any("machine" in r for r in results)
    head = "| Rows | Operation | Time | Peak memory | Status |"
    rule = "|---:|---|---:|---:|---|"
    if cloud:
        head, rule = "| Machine " + head + " Cost |", "|---" + rule + "---:|"
    lines = [head, rule]
    for r in results:
        line = (
            f"| {format_size(r['rows'])} | {r['operation']} | {fmt_seconds(r)} "
            f"| {fmt_bytes(r)} | {r['status']} |"
        )
        if cloud:
            line = f"| {r.get('machine', '-')} {line} {fmt_cost(r)} |"
        lines.append(line)
    return "\n".join(lines)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def parse_args(argv=None) -> argparse.Namespace:
    total = psutil.virtual_memory().total
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--backend", choices=sorted(BACKENDS), default="local")
    p.add_argument(
        "--sizes",
        default="1M,5M,20M",
        help="comma-separated row counts, e.g. 10k,1M (default: %(default)s)",
    )
    p.add_argument(
        "--operations", default=",".join(OPERATIONS), help="comma-separated, from: %(default)s"
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=30 * 60,
        help="seconds per operation (default: %(default)s)",
    )
    p.add_argument(
        "--max-rss",
        type=float,
        default=0.9 * total / 2**30,
        help="GiB; stop an operation above this resident memory (default: 90%% of RAM)",
    )
    p.add_argument(
        "--max-swap-growth",
        type=float,
        default=8.0,
        help="GiB; stop an operation when system swap grows by more (default: 8)",
    )
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--workdir", help="where to put the generated files (default: system temp)")
    p.add_argument(
        "--data-dir",
        help="keep the generated files here and reuse them in later runs "
        "(on Google Cloud, so are their uploads)",
    )
    cloud = p.add_argument_group("cloud backend")
    cloud.add_argument(
        "--provider",
        choices=("gcp", "local"),
        default="gcp",
        help="machine provider (default: %(default)s; Google Cloud reads "
        "ORANGE3_CLOUD_GCP_PROJECT and _ZONE, else the Cloud settings)",
    )
    cloud.add_argument("--machine-type", help="e.g. n2-standard-16 (default: the Cloud settings)")
    cloud.add_argument(
        "--delete-uploads",
        action="store_true",
        help="delete the files uploaded to the provider's storage at the end",
    )
    p.add_argument("--out", help="write results to this file: .md for a table, otherwise JSON")
    args = p.parse_args(argv)
    args.sizes = [parse_size(s) for s in args.sizes.split(",") if s.strip()]
    args.operations = [op.strip() for op in args.operations.split(",") if op.strip()]
    if unknown := set(args.operations) - set(OPERATIONS):
        p.error(f"unknown operations: {', '.join(sorted(unknown))}")
    args.max_rss *= 2**30
    args.max_swap_growth *= 2**30
    return args


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--_worker"]:
        return worker(*argv[1:4])
    args = parse_args(argv)
    # Turn SIGTERM into SystemExit, so the generated files are still deleted.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(128 + signal.SIGTERM))
    results = run(args)
    table = markdown_table(results)
    print(table)
    if args.out:
        with open(args.out, "w") as f:
            if args.out.endswith(".md"):
                f.write(table + "\n")
            else:
                json.dump(results, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
