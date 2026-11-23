"""The clouds' command-line tools: finding them, and running their browser
sign-in.

Each cloud signs in with its own tool (``gcloud``, ``aws``, ``az``). Its sign-in
command opens the cloud's sign-in page in the browser and waits until the
user is done there, so :func:`run_sign_in` runs it without a console and can
be cancelled. Orange started from the Dock or the Start menu doesn't get the
PATH that the user's shell profile sets up, so :func:`find_tool` also looks
in the usual install folders and on the PATH of the user's login shell.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from orangecontrib.cloud.client.providers.base import ProviderError, SignInCancelledError

__all__ = ["find_tool", "run_sign_in", "tool_from_login_shell"]

log = logging.getLogger(__name__)


def tool_from_login_shell(name: str, timeout: float = 10) -> str | None:
    """The path of the command ``name`` on the PATH of the user's login
    shell, or ``None`` (always ``None`` on Windows)."""
    if sys.platform == "win32":
        return None
    shell = os.environ.get("SHELL") or "/bin/sh"
    try:
        result = subprocess.run(
            [shell, "-l", "-i", "-c", f"command -v {name}"],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in reversed(result.stdout.splitlines()):
        line = line.strip()
        if line.startswith("/") and os.access(line, os.X_OK):
            return line
    return None


def find_tool(
    names: Sequence[str], known_paths: Iterable[Path], *, login_shell: bool = True
) -> str | None:
    """The path of a command-line tool, or ``None`` if it isn't installed.

    Looks for each of ``names`` on ``PATH``, then in ``known_paths`` and,
    unless ``login_shell`` is false, on the PATH of the user's login shell
    (for the last of ``names``).
    """
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    for path in known_paths:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return tool_from_login_shell(names[-1]) if login_shell else None


def run_sign_in(
    command: Sequence[str],
    cancel: threading.Event | None = None,
    *,
    tool: str,
    install_hint: str,
    poll: float = 0.2,
    env: Mapping[str, str] | None = None,
) -> str:
    """Run a tool's sign-in ``command`` and wait until it ends; return its
    output.

    The user signs in in the browser, which can take minutes, so call it off
    the GUI thread. Setting ``cancel`` kills the process (and what it
    started) and raises :class:`SignInCancelledError`. Raises
    :class:`ProviderError` if the tool (``tool``, e.g. ``gcloud``) can't
    start, with ``install_hint``, or doesn't finish, with its last lines.
    ``env``: the tool's environment (default: this process's).
    """
    posix = sys.platform != "win32"
    try:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,  # the tool must never wait for an answer on the console
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=posix,  # its own process group, killed as a whole
            env=dict(env) if env is not None else None,
        )
    except OSError as exc:
        raise ProviderError(f"Couldn't run {tool} to sign in ({exc}). {install_hint}") from exc
    while True:
        try:
            output, _ = process.communicate(timeout=poll)
            break
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                _kill(process, posix, tool)
                raise SignInCancelledError("Sign-in cancelled.") from None
    if process.returncode != 0:
        last = "\n".join(output.strip().splitlines()[-4:])
        raise ProviderError(
            f"Signing in didn't finish ({tool} exit code {process.returncode}).\n{last}".strip()
        )
    return output


def _kill(process: subprocess.Popen, posix: bool, tool: str) -> None:
    """Kill the sign-in process and what it started, and wait for it."""
    if posix:
        with contextlib.suppress(ProcessLookupError):  # it ended in the meantime
            os.killpg(process.pid, signal.SIGKILL)
    else:
        process.kill()
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        # A child outside the process group still holds the output open:
        # stop reading; the process itself is gone.
        log.warning("%s's sign-in left a process running after it was cancelled", tool)
