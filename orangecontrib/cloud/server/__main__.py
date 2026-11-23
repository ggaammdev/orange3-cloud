"""Run the API server: ``python -m orangecontrib.cloud.server``.

The token can come from ``--token`` or, so that it doesn't show in the
process list, from the ``ORANGE3_CLOUD_TOKEN`` environment variable. Give
``--tls-cert`` and ``--tls-key`` together to serve HTTPS.

On the machine, ``docker/bootstrap.py`` starts the server after installing the
user's packages, and passes the best-effort packages it couldn't install with
``--install-failures`` (a JSON list of ``{name, version, error}``), and the
user's Python version in ``ORANGE3_CLOUD_PYTHON_REQUESTED`` (``/v1/info`` shows
it next to the installed one, which differs when uv has no build of it).

The server stops by itself after ``--idle-timeout`` seconds without requests or
jobs (default 30 minutes) and after ``--max-lifetime`` seconds of uptime
(default 12 hours); 0 turns a limit off. After the idle timeout without any
request, it also cancels its jobs (Orange crashed or is out of touch). The
exit code says why it stopped, so that the bootstrap can tell a planned stop
from a crash: 0 after ``POST /v1/shutdown``, ``EXIT_IDLE_TIMEOUT`` (3) and
``EXIT_MAX_LIFETIME`` (4) after the limits, 1 after an error. After SIGTERM
or SIGINT, uvicorn stops cleanly and then ends the process with that signal.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import uvicorn

from orangecontrib.cloud.protocol.machine import InstallFailure
from orangecontrib.cloud.server.app import (
    IDLE_TIMEOUT,
    MAX_LIFETIME,
    ServerConfig,
    create_machine_app,
)

TOKEN_ENV = "ORANGE3_CLOUD_TOKEN"
PYTHON_REQUESTED_ENV = "ORANGE3_CLOUD_PYTHON_REQUESTED"

DEFAULT_IDLE_TIMEOUT = 30 * 60
DEFAULT_MAX_LIFETIME = 12 * 3600

#: Exit codes after the server stopped by itself (``docker/bootstrap.py``
#: knows them too).
EXIT_IDLE_TIMEOUT = 3
EXIT_MAX_LIFETIME = 4
EXIT_CODES = {IDLE_TIMEOUT: EXIT_IDLE_TIMEOUT, MAX_LIFETIME: EXIT_MAX_LIFETIME}

#: Seconds to let open requests finish when the server stops.
GRACEFUL_SHUTDOWN_SECONDS = 30


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m orangecontrib.cloud.server",
        description="Run the orange3-cloud API server.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on")
    parser.add_argument("--port", type=int, default=8443, help="port (default: 8443)")
    parser.add_argument(
        "--token",
        default=os.environ.get(TOKEN_ENV),
        help=f"bearer token; better set {TOKEN_ENV}, which isn't visible in the process list",
    )
    parser.add_argument("--tls-cert", type=Path, help="TLS certificate (PEM)")
    parser.add_argument("--tls-key", type=Path, help="TLS private key (PEM)")
    parser.add_argument("--machine", help="machine description shown in /v1/info")
    parser.add_argument("--data-dir", type=Path, help="folder for uploads and other files")
    parser.add_argument(
        "--worker-threads", type=int, help="threads for jobs (default: one per CPU)"
    )
    parser.add_argument(
        "--worker-memory",
        help="memory limit for jobs, e.g. 12GB (default: the machine's memory minus a reserve)",
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=DEFAULT_IDLE_TIMEOUT,
        help="stop after this many seconds without requests or jobs, and cancel jobs "
        f"after this many seconds without any request (default: {DEFAULT_IDLE_TIMEOUT}; 0: never)",
    )
    parser.add_argument(
        "--max-lifetime",
        type=float,
        default=DEFAULT_MAX_LIFETIME,
        help="stop after this many seconds of uptime, even with jobs running "
        f"(default: {DEFAULT_MAX_LIFETIME}; 0: never)",
    )
    parser.add_argument(
        "--install-failures",
        type=Path,
        help="JSON file listing the packages the bootstrap couldn't install",
    )
    parser.add_argument(
        "--python-requested",
        default=os.environ.get(PYTHON_REQUESTED_ENV),
        help="the Python version the user asked for, shown in /v1/info "
        f"(default: ${PYTHON_REQUESTED_ENV})",
    )
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)
    if not args.token:
        parser.error(f"give --token or set {TOKEN_ENV}")
    if (args.tls_cert is None) != (args.tls_key is None):
        parser.error("give both --tls-cert and --tls-key, or neither")
    return args


def read_install_failures(path: Path | None) -> list[InstallFailure]:
    if path is None:
        return []
    return [InstallFailure.model_validate(item) for item in json.loads(path.read_text())]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level.upper())

    server: uvicorn.Server | None = None

    def stop() -> None:
        if server is not None:
            server.should_exit = True  # graceful: finish open requests, then exit

    worker_memory = None
    if args.worker_memory:
        from dask.utils import parse_bytes

        worker_memory = parse_bytes(args.worker_memory)
    config = ServerConfig(
        token=args.token,
        machine=args.machine,
        data_dir=args.data_dir,
        idle_timeout_seconds=args.idle_timeout,
        max_lifetime_seconds=args.max_lifetime,
        install_failures=read_install_failures(args.install_failures),
        python_version_requested=args.python_requested or None,
        on_shutdown=stop,
        worker_threads=args.worker_threads,
        worker_memory_bytes=worker_memory,
    )
    app = create_machine_app(config)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=args.host,
            port=args.port,
            ssl_certfile=args.tls_cert,
            ssl_keyfile=args.tls_key,
            log_level=args.log_level,
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
        )
    )
    server.run()
    if not server.started:
        return 1
    return EXIT_CODES.get(app.state.server.shutdown_reason, 0)


if __name__ == "__main__":
    sys.exit(main())
