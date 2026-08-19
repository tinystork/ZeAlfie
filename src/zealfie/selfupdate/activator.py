"""ZeAlfie self-update activator (ZA-M1-4 LOT D §E; ZA-M1-4.1 Windows handoff).

The *standalone* path that actually replaces the installed ZeAlfie wheel.
The running GUI/CLI process NEVER calls this — it is invoked as a separate
``zealfie self-update apply`` command when the GUI is not active.

Fail-closed behaviour:

* loads the pending marker leniently (corrupt/absent → refuse);
* re-verifies the staged wheel byte-for-byte against the recorded SHA-256 +
  size (never trusts the marker alone);
* refuses while another ZeAlfie mutation holds the runtime mutation lease;
* on Linux performs the replacement in-process via a list-argv
  ``python -m pip install --no-deps --no-index <wheel>`` subprocess (no
  shell);
* on Windows hands off to a detached helper process
  (:mod:`zealfie.selfupdate.handoff` → :mod:`zealfie.selfupdate.windows_helper`)
  that waits for this process to exit before replacing — the running process
  never installs over its own environment;
* macOS and other platforms return ``NOT_SUPPORTED_ON_PLATFORM`` honestly;
* after a successful pip install, verifies the freshly-installed ZeAlfie
  version equals the staged target (a fresh subprocess, no shell) before
  clearing the pending marker;
* clears the pending marker only on verified success; on failure leaves it
  in place (a failed pip install of a pure-Python wheel leaves the current
  install usable).
"""

from __future__ import annotations

import importlib.metadata
import os
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from packaging.version import InvalidVersion, Version

from zealfie.runtime.layout import RuntimeLayout
from zealfie.runtime.mutation_lock import (
    OPERATION_RUNTIME_APPLY,
    RuntimeMutationBusyError,
    RuntimeMutationLock,
    RuntimeMutationLockError,
)

from .handoff import spawn_windows_helper
from .interpreter import (
    InterpreterResolutionError,
    resolve_install_interpreter,
)
from .state import (
    PendingMarkerError,
    PendingSelfUpdate,
    clear_pending_marker,
    load_pending_marker,
)
from .verify import compute_sha256

__all__ = [
    "ApplyStatus",
    "SelfUpdateApplyError",
    "SelfUpdateApplyResult",
    "apply_pending_update",
]


class ApplyStatus(StrEnum):
    APPLIED = "APPLIED"
    NO_PENDING = "NO_PENDING"
    NOT_SUPPORTED_ON_PLATFORM = "NOT_SUPPORTED_ON_PLATFORM"
    REFUSE_DOWNGRADE = "REFUSE_DOWNGRADE"
    BUSY = "BUSY"
    HANDOFF_STARTED = "HANDOFF_STARTED"
    FAILED = "FAILED"


class SelfUpdateApplyError(RuntimeError):
    """Raised when a staged wheel fails re-verification before install."""


@dataclass(frozen=True, slots=True)
class SelfUpdateApplyResult:
    """Outcome of an apply attempt (success or an honest refusal/failure)."""

    status: ApplyStatus
    message: str


def apply_pending_update(
    *,
    layout: RuntimeLayout,
    runtime_root: Path | str | None = None,
    installed_version: str | None = None,
) -> SelfUpdateApplyResult:
    """Apply a previously staged self-update (standalone activator path).

    ``runtime_root`` is the directory the mutation lock guards; it defaults
    to ``layout.root``.  ``installed_version`` overrides the detected
    installed ZeAlfie version (used by tests); when ``None`` it is detected
    via :func:`_installed_zealfie_version`.
    """
    try:
        pending = load_pending_marker(layout)
    except PendingMarkerError as exc:
        return SelfUpdateApplyResult(
            ApplyStatus.FAILED, f"pending self-update marker is corrupt: {exc}"
        )
    if pending is None:
        return SelfUpdateApplyResult(
            ApplyStatus.NO_PENDING,
            "no pending self-update is staged; run "
            "`zealfie self-update stage` first",
        )

    # Downgrade guard (MINOR-3): a stale marker must never silently
    # downgrade the installed ZeAlfie.  Fail closed on unparseable
    # versions (refuse, never proceed).
    installed = (
        installed_version
        if installed_version is not None
        else _installed_zealfie_version()
    )
    refusal = _refuse_downgrade(pending, installed)
    if refusal is not None:
        return refusal

    root = Path(runtime_root) if runtime_root is not None else layout.root

    if sys.platform != "linux" and sys.platform != "win32":
        return SelfUpdateApplyResult(
            ApplyStatus.NOT_SUPPORTED_ON_PLATFORM,
            "self-update replacement is not supported on this platform; "
            "Linux applies in-process and Windows hands off to a detached "
            "helper",
        )

    # Early refusal while another ZeAlfie mutation is in progress (MINOR-4
    # check-then-act serialization): a held lease refuses before any work.
    busy = RuntimeMutationLock(root).probe_busy()
    if busy is not None:
        operation = busy.get("operation")
        pid = busy.get("pid")
        return SelfUpdateApplyResult(
            ApplyStatus.BUSY,
            "refusing to apply self-update: another ZeAlfie mutation is in "
            f"progress (operation={operation}, pid={pid})",
        )

    if sys.platform == "win32":
        # External handoff (ZA-M1-4.1): the running process never installs
        # over its own environment.  Spawn a detached helper that waits for
        # this process to exit, then applies the verified update.
        if spawn_windows_helper(runtime_root=root, caller_pid=os.getpid()):
            return SelfUpdateApplyResult(
                ApplyStatus.HANDOFF_STARTED,
                "self-update handoff started; ZeAlfie will finish the update "
                "after this process exits",
            )
        return SelfUpdateApplyResult(
            ApplyStatus.FAILED,
            "failed to spawn the Windows self-update helper; the pending "
            "self-update was not applied",
        )

    # Linux: in-process verified replacement.
    wheel_path = Path(pending.wheel_path)
    return _apply_verified_wheel(pending, wheel_path, root, layout)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _installed_zealfie_version() -> str:
    """Detect the installed ZeAlfie version (fallback ``"0.0.0"``).

    Isolated so tests can monkeypatch version detection without touching
    ``importlib.metadata``.
    """
    try:
        return importlib.metadata.version("zealfie")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0"


def _refuse_downgrade(
    pending: PendingSelfUpdate, installed: str
) -> SelfUpdateApplyResult | None:
    """Return a REFUSE_DOWNGRADE result when the target is a downgrade.

    Returns ``None`` to signal "proceed".  A stale marker must never
    silently downgrade the installed ZeAlfie; unparseable versions fail
    closed (refuse, never proceed).  Shared by the Linux in-process path
    and the Windows helper.
    """
    try:
        target_v = Version(pending.target_version)
        installed_v = Version(installed)
    except InvalidVersion as exc:
        return SelfUpdateApplyResult(
            ApplyStatus.REFUSE_DOWNGRADE,
            "refusing to apply self-update: cannot compare versions "
            f"(target {pending.target_version!r}, installed "
            f"{installed!r}): {exc}",
        )
    if target_v < installed_v:
        return SelfUpdateApplyResult(
            ApplyStatus.REFUSE_DOWNGRADE,
            "refusing to apply self-update: target version "
            f"{pending.target_version} is lower than the installed "
            f"version {installed}; a stale marker must never silently "
            "downgrade",
        )
    return None


def _apply_verified_wheel(
    pending: PendingSelfUpdate,
    wheel_path: Path,
    root: Path,
    layout: RuntimeLayout,
) -> SelfUpdateApplyResult:
    """Re-verify, lock, install, and confirm a staged wheel (shared core).

    Used by both the Linux in-process path and the Windows helper.  The
    pending marker is cleared only after the installed version is verified
    to equal ``pending.target_version``.
    """
    try:
        # Fail-closed gate: prove the same-venv console interpreter BEFORE any
        # mutation.  _run_pip_install / _verify_installed_version resolve the
        # same interpreter internally (idempotent).
        resolve_install_interpreter()
    except InterpreterResolutionError as exc:
        return SelfUpdateApplyResult(ApplyStatus.FAILED, str(exc))

    try:
        _verify_staged_wheel(pending, wheel_path)
    except SelfUpdateApplyError as exc:
        return SelfUpdateApplyResult(ApplyStatus.FAILED, str(exc))

    # Hold the runtime mutation lease across the actual replacement so
    # the check-then-act is serialized with other ZeAlfie mutations
    # (MINOR-4).  A held lease refuses with BUSY; a primitive failure
    # refuses with FAILED (fail closed).
    try:
        with RuntimeMutationLock(root).acquire(OPERATION_RUNTIME_APPLY):
            result = _run_pip_install(wheel_path)
    except RuntimeMutationBusyError:
        return SelfUpdateApplyResult(
            ApplyStatus.BUSY,
            "refusing to apply self-update: another ZeAlfie mutation "
            "acquired the runtime mutation lease before the replacement "
            "could start; no changes were applied",
        )
    except RuntimeMutationLockError as exc:
        return SelfUpdateApplyResult(
            ApplyStatus.FAILED,
            "cannot acquire the runtime mutation lease to apply the "
            f"self-update: {exc}",
        )

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        detail = f": {stderr}" if stderr else ""
        return SelfUpdateApplyResult(
            ApplyStatus.FAILED,
            f"pip install failed (exit {result.returncode}){detail}; "
            "the current install was left untouched",
        )

    # Verify the freshly-installed version equals the staged target using a
    # FRESH subprocess (so the result reflects the on-disk install, not the
    # currently-imported module).  On mismatch/unparseable, fail closed and
    # DO NOT clear the marker.
    verification = _verify_installed_version(pending.target_version)
    if verification is not None:
        return verification

    clear_pending_marker(layout)
    return SelfUpdateApplyResult(
        ApplyStatus.APPLIED, f"ZeAlfie updated to {pending.target_version}"
    )


def _verify_installed_version(target_version: str) -> SelfUpdateApplyResult | None:
    """Verify the installed ZeAlfie version equals *target_version*.

    Runs a FRESH subprocess (list argv, no shell) with the resolved
    install interpreter (never ``pythonw.exe``).  Returns ``None`` on
    success; a FAILED result on mismatch/unparseable output (fail closed).
    The subprocess is bounded by a timeout so a hung verifier never blocks
    the apply; a timeout fails closed (marker left in place).
    """
    interpreter = resolve_install_interpreter()
    try:
        proc = subprocess.run(
            [
                interpreter,
                "-c",
                "import importlib.metadata; "
                "print(importlib.metadata.version('zealfie'))",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return SelfUpdateApplyResult(
            ApplyStatus.FAILED,
            "version verification timed out; marker left in place",
        )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        detail = f": {stderr}" if stderr else ""
        return SelfUpdateApplyResult(
            ApplyStatus.FAILED,
            "version verification failed: cannot read the installed ZeAlfie "
            f"version (exit {proc.returncode}){detail}; the pending marker "
            "was left in place",
        )
    parsed = (proc.stdout or "").strip()
    if parsed != target_version:
        return SelfUpdateApplyResult(
            ApplyStatus.FAILED,
            "version verification failed: installed version "
            f"{parsed!r} does not match the staged target "
            f"{target_version!r}; the pending marker was left in place",
        )
    return None


def _verify_staged_wheel(pending: PendingSelfUpdate, wheel_path: Path) -> None:
    """Re-verify the staged wheel byte-for-byte against the marker.

    Checks existence, size, then SHA-256.  Never trusts the marker alone.
    """
    if not wheel_path.is_file():
        raise SelfUpdateApplyError(
            f"staged wheel is missing: {wheel_path}"
        )
    actual_size = wheel_path.stat().st_size
    if actual_size != pending.size:
        raise SelfUpdateApplyError(
            f"staged wheel size mismatch: marker says {pending.size} bytes, "
            f"actual {actual_size}"
        )
    actual_sha256 = compute_sha256(wheel_path)
    if actual_sha256 != pending.wheel_sha256:
        raise SelfUpdateApplyError(
            "staged wheel SHA-256 mismatch: the staged artifact does not "
            "match the recorded integrity proof; refusing to install"
        )


def _run_pip_install(wheel_path: Path) -> subprocess.CompletedProcess:
    """Run ``python -m pip install --no-deps --no-index <wheel>`` (list argv).

    Runs with the resolved install interpreter (never ``pythonw.exe``).
    No shell; argv is passed as a list so no quoting/expansion can occur.
    """
    interpreter = resolve_install_interpreter()
    return subprocess.run(
        [
            interpreter,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-index",
            str(wheel_path),
        ],
        capture_output=True,
        text=True,
    )
