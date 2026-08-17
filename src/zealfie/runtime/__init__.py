"""Shared runtime management for ZeAlfie (M0-6 slot architecture).

The ``runtime`` package provides the persistent, shared Python
environment that hosts ZeSoftware components via immutable slots
and atomic activation.

M0-8A adds the pure, read-only deployment planning layer.
M0-8B adds the transactional offline deployment engine.
"""

from __future__ import annotations

from .artifact_cache import (
    ArtifactCacheStore,
    CacheGcPlan,
    CacheGcResult,
    apply_cache_gc_plan,
    build_cache_gc_plan,
    materialize_cached,
    runtime_cache_gc,
)
from .deployment import DeploymentCancelledError, apply_deployment_plan
from .gc import (
    GcPlan,
    GcResult,
    GcSlotEntry,
    GcStatus,
    SlotCategory,
    apply_gc_plan,
    build_gc_plan,
)
from .installed_lock import (
    INSTALLED_LOCK_SCHEMA_VERSION,
    InstalledDependency,
    InstalledLockStore,
    InstalledRuntimeLock,
    installed_lock_from_runtime_lock,
)
from .layout import RuntimeLayout, default_runtime_layout, default_runtime_root, validate_slot_id
from .manager import SharedRuntime, SharedRuntimeError
from .mutation_lock import (
    OPERATION_GPU_INSTALL,
    OPERATION_PRODUCT_INSTALL,
    OPERATION_PRODUCT_UPDATE,
    OPERATION_RUNTIME_APPLY,
    OPERATION_RUNTIME_CREATE,
    OPERATION_RUNTIME_DISCARD,
    OPERATION_RUNTIME_GC,
    OPERATION_RUNTIME_ROLLBACK,
    RuntimeMutationBusyError,
    RuntimeMutationLease,
    RuntimeMutationLeaseRequired,
    RuntimeMutationLock,
    RuntimeMutationLockError,
)
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
    "ArtifactCacheStore",
    "CacheGcPlan",
    "CacheGcResult",
    "CandidateState",
    "DeploymentAction",
    "DeploymentCancelledError",
    "DeploymentPlan",
    "DeploymentReasonCode",
    "DeploymentResult",
    "DeploymentStep",
    "DesiredComponent",
    "DesiredRuntimeState",
    "GcPlan",
    "GcResult",
    "GcSlotEntry",
    "GcStatus",
    "InstallOutcome",
    "InstallResult",
    "INSTALLED_LOCK_SCHEMA_VERSION",
    "InstalledDependency",
    "InstalledLockStore",
    "InstalledRuntimeLock",
    "OPERATION_GPU_INSTALL",
    "OPERATION_PRODUCT_INSTALL",
    "OPERATION_PRODUCT_UPDATE",
    "OPERATION_RUNTIME_APPLY",
    "OPERATION_RUNTIME_CREATE",
    "OPERATION_RUNTIME_DISCARD",
    "OPERATION_RUNTIME_GC",
    "OPERATION_RUNTIME_ROLLBACK",
    "PlanningError",
    "PROVENANCE_SCHEMA_VERSION",
    "ProductProvenance",
    "ProductProvenanceStore",
    "RuntimeLayout",
    "RuntimeMutationBusyError",
    "RuntimeMutationLease",
    "RuntimeMutationLeaseRequired",
    "RuntimeMutationLock",
    "RuntimeMutationLockError",
    "RuntimeReasonCode",
    "RuntimeSlot",
    "RuntimeState",
    "RuntimeStatus",
    "SlotCategory",
    "validate_slot_id",
    "RuntimeTransaction",
    "SharedRuntime",
    "SharedRuntimeError",
    "apply_cache_gc_plan",
    "apply_deployment_plan",
    "apply_gc_plan",
    "build_cache_gc_plan",
    "build_gc_plan",
    "check_desired_state_conflicts",
    "build_deployment_plan",
    "default_runtime_layout",
    "default_runtime_root",
    "generate_slot_id",
    "load_active_state",
    "installed_lock_from_runtime_lock",
    "materialize_cached",
    "probe_runtime_distribution",
    "probe_runtime_python_version",
    "runtime_cache_gc",
    "save_active_state",
]
