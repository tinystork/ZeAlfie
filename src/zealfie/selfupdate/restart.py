"""Restart spawn helpers for GUI self-update (ZA-M1-4.2).

Two detached-process spawns, both list-argv (never ``shell=True``) and
platform-guarded so the module imports and runs on every OS:

* :func:`spawn_gui_process` — launch a fresh ``zealfie-gui`` process;
* :func:`spawn_restart_supervisor` — launch the detached
  :mod:`zealfie.selfupdate.restart_supervisor`, which waits for the pending
  self-update marker to be cleared (the standalone activator/helper clears it
  only after a verified success) before launching the fresh GUI;

* :func:`restart_gui_after_update` — the single restart entry point the GUI
  calls once after a successful apply/handoff: it prefers the supervisor
  (correct on Windows, where the detached helper installs only after the GUI
  exits) and falls back to a direct GUI spawn when the supervisor cannot be
  launched.

None of these functions apply an update or re-spawn themselves: the restart
supervisor is one-shot and never calls back into the GUI, so there is no
restart loop.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

__all__ = [
    "restart_gui_after_update",
    "spawn_gui_process",
    "spawn_restart_supervisor",
]

#: The GUI entry point invoked in the fresh process (a single statement so a
#: fresh interpreter runs the newest installed ``zealfie`` code).
_GUI_RUN_CMD = "from zealfie.gui import main; main()"


def _detached_kwargs() -> dict:
    """Detached-process kwargs, platform-guarded (empty extras on POSIX)."""
    if sys.platform == "win32":
        return {
            "creationflags": (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            ),
            "close_fds": True,
        }
    return {"start_new_session": True, "close_fds": True}


def spawn_gui_process(
    *,
    python: str | Path | None = None,
    _popen=subprocess.Popen,
) -> bool:
    """Spawn a fresh, detached ``zealfie-gui`` process.  Returns success.

    List argv, no shell, no CWD dependence.  ``python`` defaults to the
    current interpreter (the venv python whose ``zealfie`` was just updated).
    """
    interpreter = str(python) if python is not None else sys.executable
    argv = [interpreter, "-c", _GUI_RUN_CMD]
    try:
        _popen(argv, **_detached_kwargs())
    except (OSError, ValueError):
        return False
    return True


def spawn_restart_supervisor(
    *,
    runtime_root: str | Path,
    python: str | Path | None = None,
    _popen=subprocess.Popen,
) -> bool:
    """Spawn the detached restart supervisor for *runtime_root*.  Returns success.

    The supervisor waits for the pending self-update marker to be cleared,
    then launches the fresh GUI.  List argv, no shell.
    """
    interpreter = str(python) if python is not None else sys.executable
    argv = [
        interpreter,
        "-m",
        "zealfie.selfupdate.restart_supervisor",
        "--runtime-root",
        str(runtime_root),
        "--python",
        interpreter,
    ]
    try:
        _popen(argv, **_detached_kwargs())
    except (OSError, ValueError):
        return False
    return True


def restart_gui_after_update(
    *,
    runtime_root: str | Path,
    python: str | Path | None = None,
    _spawn_supervisor=spawn_restart_supervisor,
    _spawn_gui=spawn_gui_process,
) -> None:
    """Launch the post-update restart once, preferring the supervisor.

    Called by the GUI exactly once after a successful apply/handoff result.
    On Linux (in-process apply) the marker is already cleared and the
    supervisor launches the GUI immediately; on Windows the supervisor waits
    for the detached helper to clear the marker.  Falls back to a direct GUI
    spawn when the supervisor cannot be launched.  Never raises.
    """
    if not _spawn_supervisor(runtime_root=runtime_root, python=python):
        _spawn_gui(python=python)
