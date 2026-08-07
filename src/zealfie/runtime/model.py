"""Structured models for the ZeAlfie shared runtime."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class RuntimeState(StrEnum):
    """Coarse-grained lifecycle state of the shared runtime."""

    ABSENT = "ABSENT"
    READY = "READY"
    BROKEN = "BROKEN"


class RuntimeReasonCode(StrEnum):
    """Stable reason codes produced by runtime status checks."""

    RUNTIME_NOT_FOUND = "RUNTIME_NOT_FOUND"
    RUNTIME_PYTHON_NOT_FOUND = "RUNTIME_PYTHON_NOT_FOUND"
    RUNTIME_PYTHON_UNUSABLE = "RUNTIME_PYTHON_UNUSABLE"
    RUNTIME_METADATA_CHECK_FAILED = "RUNTIME_METADATA_CHECK_FAILED"
    RUNTIME_READY = "RUNTIME_READY"


class InstallOutcome(StrEnum):
    """Result of attempting to install a local wheel into the runtime."""

    INSTALLED = "INSTALLED"
    ALREADY_INSTALLED = "ALREADY_INSTALLED"
    VERSION_MISMATCH = "VERSION_MISMATCH"
    CONTRACT_MISMATCH = "CONTRACT_MISMATCH"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    """Immutable snapshot of the shared runtime state.

    ``runtime_root`` is the top-level runtime directory (e.g.
    ``~/.local/share/zealfie/runtime``).  ``current`` is the active
    venv directory inside it (``runtime_root/current``).
    """

    state: RuntimeState
    runtime_root: Path
    current: Path
    python_executable: Path | None = None
    python_version: str | None = None
    reason_code: RuntimeReasonCode | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class InstallResult:
    """Result of a local wheel installation operation."""

    outcome: InstallOutcome
    distribution_name: str
    version: str | None = None
    detail: str | None = None
