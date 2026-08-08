"""Structured models for the ZeAlfie shared runtime (M0-6 slot architecture)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class RuntimeState(StrEnum):
    ABSENT = "ABSENT"
    READY = "READY"
    BROKEN = "BROKEN"


class RuntimeReasonCode(StrEnum):
    RUNTIME_NOT_FOUND = "RUNTIME_NOT_FOUND"
    RUNTIME_PYTHON_NOT_FOUND = "RUNTIME_PYTHON_NOT_FOUND"
    RUNTIME_PYTHON_UNUSABLE = "RUNTIME_PYTHON_UNUSABLE"
    RUNTIME_METADATA_CHECK_FAILED = "RUNTIME_METADATA_CHECK_FAILED"
    RUNTIME_READY = "RUNTIME_READY"
    # M0-6 additions
    RUNTIME_STATE_FILE_INVALID = "RUNTIME_STATE_FILE_INVALID"
    ACTIVE_SLOT_NOT_FOUND = "ACTIVE_SLOT_NOT_FOUND"
    ACTIVE_SLOT_BROKEN = "ACTIVE_SLOT_BROKEN"
    CANDIDATE_VALIDATION_FAILED = "CANDIDATE_VALIDATION_FAILED"
    ACTIVATION_FAILED = "ACTIVATION_FAILED"
    ROLLBACK_TARGET_NOT_FOUND = "ROLLBACK_TARGET_NOT_FOUND"
    STALE_TRANSACTION = "STALE_TRANSACTION"
    SLOT_DISCARD_REFUSED = "SLOT_DISCARD_REFUSED"


class InstallOutcome(StrEnum):
    INSTALLED = "INSTALLED"
    ALREADY_INSTALLED = "ALREADY_INSTALLED"
    VERSION_MISMATCH = "VERSION_MISMATCH"
    CONTRACT_MISMATCH = "CONTRACT_MISMATCH"
    FAILED = "FAILED"


class CandidateState(StrEnum):
    PREPARED = "PREPARED"
    VALID = "VALID"
    INVALID = "INVALID"


# ---------------------------------------------------------------------------
# Runtime slot
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuntimeSlot:
    """A named runtime slot with a stable, immutable path."""

    slot_id: str
    path: Path


# ---------------------------------------------------------------------------
# Active pointer state (persisted as JSON)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActiveRuntimeState:
    """The serialisable active/previous slot pointers.

    ``schema_version`` is always ``1`` for M0-6.
    """

    schema_version: int = 1
    active_slot: str | None = None
    previous_slot: str | None = None


# ---------------------------------------------------------------------------
# Runtime status (evolved)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    state: RuntimeState
    runtime_root: Path
    active_slot_id: str | None = None
    active_path: Path | None = None
    previous_slot_id: str | None = None
    python_executable: Path | None = None
    python_version: str | None = None
    reason_code: RuntimeReasonCode | None = None
    reason: str | None = None


# ---------------------------------------------------------------------------
# Install result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstallResult:
    outcome: InstallOutcome
    distribution_name: str
    version: str | None = None
    detail: str | None = None


# ---------------------------------------------------------------------------
# Deployment result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeploymentResult:
    """The result of applying a deployment plan to the shared runtime.

    When *success* is ``True``, *active_slot_id* carries the newly
    activated candidate slot id.

    When *success* is ``False``, *reason* explains the failure.
    """

    success: bool
    active_slot_id: str | None = None
    previous_slot_id: str | None = None
    reason: str | None = None
