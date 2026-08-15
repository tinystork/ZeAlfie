"""Shared runtime management for ZeAlfie (M0-6 slot architecture).

The ``runtime`` package provides the persistent, shared Python
environment that hosts ZeSoftware components via immutable slots
and atomic activation.

M0-8A adds the pure, read-only deployment planning layer.
M0-8B adds the transactional offline deployment engine.
"""

from __future__ import annotations

from .deployment import DeploymentCancelledError, apply_deployment_plan
from .installed_lock import (
    INSTALLED_LOCK_SCHEMA_VERSION,
    InstalledDependency,
    InstalledLockStore,
    InstalledRuntimeLock,
    installed_lock_from_runtime_lock,
)
from .layout import RuntimeLayout, default_runtime_layout, default_runtime_root, validate_slot_id
from .manager import SharedRuntime, SharedRuntimeError
from .model import (
    ActiveRuntimeState,
    CandidateState,
    DeploymentResult,
    InstallOutcome,
    InstallResult,
    RuntimeReasonCode,
    RuntimeSlot,
    RuntimeState,
    RuntimeStatus,
)
from .planning import (
    DeploymentAction,
    DeploymentPlan,
    DeploymentReasonCode,
    DeploymentStep,
    DesiredComponent,
    DesiredRuntimeState,
    check_desired_state_conflicts,
    PlanningError,
    build_deployment_plan,
)
from .probe import probe_runtime_distribution, probe_runtime_python_version
from .provenance import (
    PROVENANCE_SCHEMA_VERSION,
    ProductProvenance,
    ProductProvenanceStore,
)
from .state import load_active_state, save_active_state
from .transaction import RuntimeTransaction, generate_slot_id

__all__ = [
    "ActiveRuntimeState",
    "CandidateState",
    "DeploymentAction",
    "DeploymentCancelledError",
    "DeploymentPlan",
    "DeploymentReasonCode",
    "DeploymentResult",
    "DeploymentStep",
    "DesiredComponent",
    "DesiredRuntimeState",
    "InstallOutcome",
    "InstallResult",
    "INSTALLED_LOCK_SCHEMA_VERSION",
    "InstalledDependency",
    "InstalledLockStore",
    "InstalledRuntimeLock",
    "PlanningError",
    "PROVENANCE_SCHEMA_VERSION",
    "ProductProvenance",
    "ProductProvenanceStore",
    "RuntimeLayout",
    "RuntimeReasonCode",
    "RuntimeSlot",
    "RuntimeState",
    "RuntimeStatus",
    "validate_slot_id",
    "RuntimeTransaction",
    "SharedRuntime",
    "SharedRuntimeError",
    "apply_deployment_plan",
    "check_desired_state_conflicts",
    "build_deployment_plan",
    "default_runtime_layout",
    "default_runtime_root",
    "generate_slot_id",
    "load_active_state",
    "installed_lock_from_runtime_lock",
    "probe_runtime_distribution",
    "probe_runtime_python_version",
    "save_active_state",
]
