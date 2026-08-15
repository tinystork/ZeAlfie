"""Acceleration requirement contract (M1-2H).

Products declare their accelerated needs as distribution-level
requirements against a known accelerator *backend*; ZeAlfie evaluates
those declarations against the host observation produced by M1-2G and
plans the accelerated runtime closure from an explicit variant
catalog.

Architectural invariant: ZeAlfie NEVER selects a concrete accelerated
framework (no PyTorch/CuPy/TensorFlow/Numba choice anywhere in
production code).  It only checks declared backends and cross-product
requirement consistency, and resolves concrete variants through a
declared catalog — fail-closed: a missing or ambiguous variant blocks
the plan, it is never guessed.
"""

from zealfie.acceleration.compatibility import (
    evaluate_acceleration_compatibility,
)
from zealfie.acceleration.models import (
    AcceleratedRequirement,
    AccelerationIncompatibility,
    HardwareCompatibility,
    HardwareCompatibilityReasonCode,
    HardwareCompatibilityStatus,
    KNOWN_BACKENDS,
    ProductAccelerationRequirements,
)
from zealfie.acceleration.deployment import (
    AcceleratedAcquisitionError,
    AcceleratedAcquisitionUnavailable,
    AcceleratedArtifactAcquirer,
    AcceleratedDeploymentPhase,
    AcceleratedDeploymentResult,
    AcceleratedGate,
    AcceleratedSlotMetadata,
    AcceleratedSlotMetadataStore,
    AcquiredAcceleratedVariant,
    CooperativeCancellationError,
    apply_accelerated_deployment,
    default_accelerated_artifact_acquirer,
    default_accelerated_gate,
    extend_runtime_lock_with_acceleration,
)
from zealfie.acceleration.planning import (
    AcceleratedDeploymentPlan,
    AcceleratedPlanStatus,
    PlannedAcceleratedDependency,
    PlannedKeepProduct,
    VariantStatus,
    build_accelerated_deployment_plan,
)
from zealfie.acceleration.variants import (
    AcceleratedVariant,
    AcceleratedVariantCatalog,
    AmbiguousVariantError,
    default_variant_catalog,
)

__all__ = [
    "AcceleratedAcquisitionError",
    "AcceleratedAcquisitionUnavailable",
    "AcceleratedArtifactAcquirer",
    "AcceleratedDeploymentPhase",
    "AcceleratedDeploymentPlan",
    "AcceleratedDeploymentResult",
    "AcceleratedGate",
    "AcceleratedPlanStatus",
    "AcceleratedRequirement",
    "AcceleratedSlotMetadata",
    "AcceleratedSlotMetadataStore",
    "AcceleratedVariant",
    "AcceleratedVariantCatalog",
    "AccelerationIncompatibility",
    "AcquiredAcceleratedVariant",
    "AmbiguousVariantError",
    "CooperativeCancellationError",
    "HardwareCompatibility",
    "HardwareCompatibilityReasonCode",
    "HardwareCompatibilityStatus",
    "KNOWN_BACKENDS",
    "PlannedAcceleratedDependency",
    "PlannedKeepProduct",
    "ProductAccelerationRequirements",
    "VariantStatus",
    "apply_accelerated_deployment",
    "build_accelerated_deployment_plan",
    "default_accelerated_artifact_acquirer",
    "default_accelerated_gate",
    "default_variant_catalog",
    "evaluate_acceleration_compatibility",
    "extend_runtime_lock_with_acceleration",
]
