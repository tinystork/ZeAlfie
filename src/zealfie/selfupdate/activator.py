"""ZeAlfie self-update activator (ZA-M1-4 LOT D §E).

The *standalone* path that actually replaces the installed ZeAlfie wheel.
The running GUI/CLI process NEVER calls this — it is invoked as a separate
``zealfie self-update apply`` command when the GUI is not active.

Fail-closed behaviour:

* loads the pending marker leniently (corrupt/absent → refuse);
* re-verifies the staged wheel byte-for-byte against the recorded SHA-256 +
  size (never trusts the marker alone);
* refuses while another ZeAlfie mutation holds the runtime mutation lease;
* performs the replacement only on Linux, via a list-argv
  ``python -m pip install --no-deps --no-index <wheel>`` subprocess (no
  shell).  Windows/macOS activators are a documented follow-up and return
  ``NOT_SUPPORTED_ON_PLATFORM`` honestly;
* clears the pending marker only on success; on failure leaves it in place
  (a failed pip install of a pure-Python wheel leaves the current install
  usable).
"""

from __future__ import annotations

import importlib.metadata
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

    if sys.platform != "linux":
        return SelfUpdateApplyResult(
            ApplyStatus.NOT_SUPPORTED_ON_PLATFORM,
            "self-update replacement is only supported on Linux; "
            "Windows/macOS activators are not yet implemented",
        )

    root = runtime_root if runtime_root is not None else layout.root
    busy = RuntimeMutationLock(root).probe_busy()
    if busy is not None:
        operation = busy.get("operation")
        pid = busy.get("pid")
        return SelfUpdateApplyResult(
            ApplyStatus.BUSY,
            "refusing to apply self-update: another ZeAlfie mutation is in "
            f"progress (operation={operation}, pid={pid})",
        )

    wheel_path = Path(pending.wheel_path)
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

    clear_pending_marker(layout)
    return SelfUpdateApplyResult(
        ApplyStatus.APPLIED, f"ZeAlfie updated to {pending.target_version}"
    )


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

    No shell; argv is passed as a list so no quoting/expansion can occur.
    """
    return subprocess.run(
        [
            sys.executable,
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
