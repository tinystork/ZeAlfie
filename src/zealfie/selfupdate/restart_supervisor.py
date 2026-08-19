"""Detached restart supervisor for GUI self-update (ZA-M1-4.2).

Spawned by :func:`zealfie.selfupdate.restart.spawn_restart_supervisor` after
a successful apply/handoff.  It waits (bounded) for the pending self-update
marker to be cleared — the standalone activator / Windows helper clears it
only after a *verified* successful install — then launches a fresh
``zealfie-gui``.

Fail-closed and restart-loop-free:

* it never applies an update and never re-spawns itself;
* on timeout it still launches the GUI: the currently-installed version is
  always safe to launch (a failed apply leaves the previous install intact)
  and the GUI's own startup check self-corrects (re-detects / re-proposes).
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from zealfie.runtime.layout import RuntimeLayout

from .state import pending_marker_path

__all__ = ["main", "wait_for_marker_clear"]

DEFAULT_TIMEOUT_S = 600.0
_POLL_INTERVAL_S = 0.5


def wait_for_marker_clear(
    layout: RuntimeLayout,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    _poll_interval: float = _POLL_INTERVAL_S,
    _sleep=time.sleep,
) -> bool:
    """Poll until the pending marker is gone (or *timeout_s* elapses).

    Returns ``True`` when the marker was observed cleared (verified success);
    ``False`` on timeout.  Never raises.
    """
    marker = pending_marker_path(layout)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not marker.is_file():
            return True
        _sleep(_poll_interval)
    return not marker.is_file()


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="zealfie.selfupdate.restart_supervisor",
        description="wait for a self-update to finish, then relaunch the GUI",
    )
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--python", required=False, type=str, default=None)
    parser.add_argument("--timeout-s", required=False, type=float, default=DEFAULT_TIMEOUT_S)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Supervisor entry point: wait for marker clear, then launch the GUI."""
    from .restart import spawn_gui_process

    args = _parse_args(argv)
    layout = RuntimeLayout(root=args.runtime_root)
    wait_for_marker_clear(layout, timeout_s=args.timeout_s)
    # Always relaunch (see module docstring); a failed launch is not fatal.
    spawn_gui_process(python=args.python)
    return 0


if __name__ == "__main__":
    sys.exit(main())
