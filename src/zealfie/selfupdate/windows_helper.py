"""Detached Windows self-update helper (ZA-M1-4.1).

Runs as a separate process (``python -m zealfie.selfupdate.windows_helper``),
spawned by :func:`zealfie.selfupdate.handoff.spawn_windows_helper`.  It waits
for the caller (the ``zealfie self-update apply`` process) to exit — thereby
releasing its file locks — then applies the verified pending self-update via
the shared :func:`~zealfie.selfupdate.activator._apply_verified_wheel`.

The helper depends only on ``sys.executable`` (the venv python) and the
``--runtime-root`` path; it never depends on a source checkout or CWD.  All
subprocess argv is a list (never ``shell=True``).
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from zealfie.runtime.layout import RuntimeLayout

from .activator import (
    ApplyStatus,
    _apply_verified_wheel,
    _installed_zealfie_version,
    _refuse_downgrade,
)
from .state import PendingMarkerError, load_pending_marker

__all__ = ["main", "wait_for_caller_exit"]

_DEFAULT_CALLER_WAIT_TIMEOUT_S = 300.0


def wait_for_caller_exit(
    pid: int,
    *,
    timeout_s: float = _DEFAULT_CALLER_WAIT_TIMEOUT_S,
    _wait_impl=None,
) -> None:
    """Block until *pid* exits (or *timeout_s* elapses).  Never raises.

    ``_wait_impl`` is an injectable test seam — a callable
    ``(pid, timeout_s) -> None`` that replaces the platform wait.
    """
    if _wait_impl is not None:
        _wait_impl(pid, timeout_s)
        return
    if sys.platform == "win32":
        _wait_for_caller_exit_windows(pid, timeout_s)
    else:
        _wait_for_caller_exit_posix(pid, timeout_s)


def _wait_for_caller_exit_windows(pid: int, timeout_s: float) -> None:
    """Wait on the caller process handle (Windows).  Never raises.

    ``OpenProcess(SYNCHRONIZE, …)`` returning a null handle means the caller
    is already gone (or is not waitable) — return immediately.
    """
    SYNCHRONIZE = 0x00100000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return
    try:
        kernel32.WaitForSingleObject(handle, int(timeout_s * 1000))
    finally:
        kernel32.CloseHandle(handle)


def _wait_for_caller_exit_posix(pid: int, timeout_s: float) -> None:
    """Poll ``os.kill(pid, 0)`` until the caller exits or timeout.  Never raises."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except OSError:
            return
        if time.monotonic() >= deadline:
            return
        time.sleep(0.05)


def main(argv: Sequence[str] | None = None) -> int:
    """Helper entry point: wait for the caller, then apply the update."""
    args = _parse_args(argv)
    wait_for_caller_exit(args.caller_pid)
    layout = RuntimeLayout(root=args.runtime_root)
    return _apply_after_caller_exit(layout)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="zealfie.selfupdate.windows_helper",
        description="detached ZeAlfie self-update helper",
    )
    parser.add_argument("--caller-pid", required=True, type=int)
    parser.add_argument("--runtime-root", required=True, type=Path)
    return parser.parse_args(argv)


def _apply_after_caller_exit(layout: RuntimeLayout) -> int:
    """Load + downgrade-guard + apply the pending marker (post caller exit)."""
    try:
        pending = load_pending_marker(layout)
    except PendingMarkerError as exc:
        print(
            f"windows self-update helper: pending marker is corrupt: {exc}",
            file=sys.stderr,
        )
        return 1
    if pending is None:
        print(
            "windows self-update helper: no pending self-update is staged",
            file=sys.stderr,
        )
        return 1

    refusal = _refuse_downgrade(pending, _installed_zealfie_version())
    if refusal is not None:
        print(f"windows self-update helper: {refusal.message}", file=sys.stderr)
        return 1

    wheel_path = Path(pending.wheel_path)
    result = _apply_verified_wheel(pending, wheel_path, layout.root, layout)
    if result.status is ApplyStatus.APPLIED:
        print(result.message, file=sys.stdout)
        return 0
    print(f"windows self-update helper: {result.message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
