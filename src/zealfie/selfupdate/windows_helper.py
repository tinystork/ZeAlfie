"""Detached Windows self-update helper (ZA-M1-4.1).

Runs as a separate process (``python -m zealfie.selfupdate.windows_helper``),
spawned by :func:`zealfie.selfupdate.handoff.spawn_windows_helper`.  It waits
for the caller (the ``zealfie self-update apply`` process) to exit — thereby
releasing its file locks — then applies the verified pending self-update via
the shared :func:`~zealfie.selfupdate.activator._apply_verified_wheel`.

The helper depends only on ``sys.executable`` (the venv python) and the
``--runtime-root`` path; it never depends on a source checkout or CWD.  All
subprocess argv is a list (never ``shell=True``).

Fail-closed: the update is applied ONLY when the caller is *confirmed* to have
exited.  A timeout, wait failure, or unconfirmable caller leaves the pending
marker in place and returns a non-zero exit code.
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

# Windows wait constants.
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_WAIT_FAILED = 0xFFFFFFFF
_SYNCHRONIZE = 0x00100000
_ERROR_INVALID_PARAMETER = 0x57


def wait_for_caller_exit(
    pid: int,
    *,
    timeout_s: float = _DEFAULT_CALLER_WAIT_TIMEOUT_S,
    _wait_impl=None,
) -> bool:
    """Block until *pid* exits (or *timeout_s* elapses).  Never raises.

    Returns ``True`` only when the caller is *confirmed* to have exited;
    ``False`` on timeout, wait failure, or an unconfirmable caller (fail
    closed).  ``_wait_impl`` is an injectable test seam — a callable
    ``(pid, timeout_s) -> bool`` that replaces the platform wait.
    """
    if _wait_impl is not None:
        return bool(_wait_impl(pid, timeout_s))
    if sys.platform == "win32":
        return _wait_for_caller_exit_windows(pid, timeout_s)
    return _wait_for_caller_exit_posix(pid, timeout_s)


def _wait_for_caller_exit_windows(
    pid: int, timeout_s: float, *, _kernel32=None
) -> bool:
    """Wait on the caller process handle (Windows).  Never raises.

    Returns ``True`` only when the caller is confirmed exited:

    * ``WaitForSingleObject`` returns ``WAIT_OBJECT_0`` (0) → exited;
    * ``WAIT_TIMEOUT`` / ``WAIT_FAILED`` / anything else → not confirmed;
    * ``OpenProcess`` returns a null handle with ``ERROR_INVALID_PARAMETER``
      (0x57) → the caller process does not exist → confirmed exited;
    * ``OpenProcess`` returns a null handle with any other error (e.g. access
      denied) → not confirmable → fail closed (do NOT install).

    ``_kernel32`` is an injectable seam for tests (no real ``ctypes.windll``).
    """
    kernel32 = _kernel32 if _kernel32 is not None else ctypes.windll.kernel32

    # Set signatures explicitly so a 64-bit HANDLE is never truncated and the
    # return codes are read back as the correct width.
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [
        ctypes.c_uint32,
        ctypes.c_bool,
        ctypes.c_uint32,
    ]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.GetLastError.restype = ctypes.c_uint32
    kernel32.GetLastError.argtypes = []

    handle = kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
    if handle is None:
        # A null handle means either the caller is gone (ERROR_INVALID_PARAMETER)
        # or we could not open it at all (access denied etc.).  Only the former
        # confirms exit; everything else fails closed.
        return kernel32.GetLastError() == _ERROR_INVALID_PARAMETER
    try:
        result = kernel32.WaitForSingleObject(handle, int(timeout_s * 1000))
    finally:
        kernel32.CloseHandle(handle)
    return result == _WAIT_OBJECT_0


def _wait_for_caller_exit_posix(pid: int, timeout_s: float) -> bool:
    """Poll ``os.kill(pid, 0)`` until the caller exits or timeout.  Never raises.

    Returns ``True`` when the caller is confirmed gone (``ProcessLookupError``
    / ``OSError``); ``False`` on timeout.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def main(argv: Sequence[str] | None = None) -> int:
    """Helper entry point: wait for the caller, then apply the update."""
    args = _parse_args(argv)
    if not wait_for_caller_exit(args.caller_pid):
        print(
            "windows self-update helper: caller process did not confirm exit "
            "before the wait elapsed; refusing to apply the pending "
            "self-update (marker left in place)",
            file=sys.stderr,
        )
        return 1
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
