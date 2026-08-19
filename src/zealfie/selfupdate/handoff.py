"""Detached helper spawn for the Windows self-update handoff (ZA-M1-4.1).

The running ZeAlfie process never installs over its own environment.  On
Windows it therefore hands the pending self-update to a detached helper
process (:mod:`zealfie.selfupdate.windows_helper`) that waits for the caller
to exit, then performs the verified replacement.

All subprocess arguments are passed as a list (never ``shell=True`` and
never a string-concatenated command).  The module imports cleanly on every
platform: the Windows creation flags are accessed through ``getattr`` so a
POSIX interpreter never raises at import or call time.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .interpreter import InterpreterResolutionError, resolve_install_interpreter

__all__ = ["spawn_windows_helper"]


def _detached_creationflags() -> int:
    """Return the Windows detached-process creation flags (0 on POSIX).

    Attribute access is guarded so this module imports and runs on Linux
    (for tests) where ``subprocess.DETACHED_PROCESS`` and friends do not
    exist.
    """
    return (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )


def spawn_windows_helper(
    *,
    runtime_root: Path | str,
    caller_pid: int,
    python: str | Path | None = None,
) -> bool:
    """Spawn a detached helper that applies the pending self-update.

    ``python`` defaults to the resolved install interpreter — the venv
    *console* interpreter, never ``pythonw.exe`` (see
    :func:`zealfie.selfupdate.interpreter.resolve_install_interpreter`).  The
    helper is spawned with list argv (no shell) and is fully detached from
    this process so it can outlive it and install after the caller exits.

    Returns ``True`` iff the helper process was spawned successfully
    (``OSError`` / ``ValueError`` → ``False``).  A same-venv console
    interpreter that cannot be proven also fails closed (``False``) so the
    pending update is never handed off with a windowed interpreter.
    """
    try:
        interpreter = resolve_install_interpreter(python=python)
    except InterpreterResolutionError:
        return False
    argv = [
        interpreter,
        "-m",
        "zealfie.selfupdate.windows_helper",
        "--caller-pid",
        str(caller_pid),
        "--runtime-root",
        str(runtime_root),
    ]
    try:
        if sys.platform == "win32":
            subprocess.Popen(
                argv,
                creationflags=_detached_creationflags(),
                close_fds=True,
            )
        else:
            subprocess.Popen(
                argv,
                start_new_session=True,
                close_fds=True,
            )
    except (OSError, ValueError):
        return False
    return True
