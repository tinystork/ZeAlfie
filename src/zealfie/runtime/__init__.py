"""Shared runtime management for ZeAlfie (M0-6 slot architecture).

The ``runtime`` package provides the persistent, shared Python
environment that hosts ZeSoftware components via immutable slots
and atomic activation.
"""

from __future__ import annotations

from .layout import RuntimeLayout, default_runtime_layout, default_runtime_root, validate_slot_id
from .manager import SharedRuntime, SharedRuntimeError
from .model import (
    ActiveRuntimeState,
    CandidateState,
    InstallOutcome,
    InstallResult,
    RuntimeReasonCode,
    RuntimeSlot,
    RuntimeState,
    RuntimeStatus,
)
from .probe import probe_runtime_distribution, probe_runtime_python_version
from .state import load_active_state, save_active_state
from .transaction import RuntimeTransaction, generate_slot_id

__all__ = [
    "ActiveRuntimeState",
    "CandidateState",
    "InstallOutcome",
    "InstallResult",
    "RuntimeLayout",
    "RuntimeReasonCode",
    "RuntimeSlot",
    "RuntimeState",
    "RuntimeStatus",
    "validate_slot_id",
    "RuntimeTransaction",
    "SharedRuntime",
    "SharedRuntimeError",
    "default_runtime_layout",
    "default_runtime_root",
    "generate_slot_id",
    "load_active_state",
    "probe_runtime_distribution",
    "probe_runtime_python_version",
    "save_active_state",
]
