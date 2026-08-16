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
    HostPrerequisiteEntry,
    HostPrerequisites,
    HostPrerequisitesStatus,
    HostPrerequisiteStatus,
    evaluate_acceleration_compatibility,
    evaluate_host_prerequisites,
)
from zealfie.acceleration.backend_probe import (
    BACKEND_COMPUTE_PROBES,
    get_backend_compute_probe,
)
from zealfie.acceleration.acquisition import (
    AcceleratedArtifactEntry,
    AcceleratedArtifactManifest,
    InvalidArtifactManifestError,
    ManifestAcceleratedArtifactAcquirer,
    MissingArtifact,
    PlatformMismatch,
    Sha256Mismatch,
    TransportError,
    VersionMismatch,
    default_accelerated_artifact_manifest,
    default_manifest_artifact_acquirer,
    default_manifest_variant_catalog,
    load_accelerated_artifact_manifest,
    load_accelerated_artifact_manifest_file,
    variant_catalog_from_artifact_manifest,
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
    "AcceleratedArtifactEntry",
    "AcceleratedArtifactManifest",
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
    "InvalidArtifactManifestError",
    "AcquiredAcceleratedVariant",
    "AmbiguousVariantError",
    "BACKEND_COMPUTE_PROBES",
    "CooperativeCancellationError",
    "HardwareCompatibility",
    "HardwareCompatibilityReasonCode",
    "HardwareCompatibilityStatus",
    "HostPrerequisiteEntry",
    "HostPrerequisites",
    "HostPrerequisitesStatus",
    "HostPrerequisiteStatus",
    "KNOWN_BACKENDS",
    "ManifestAcceleratedArtifactAcquirer",
    "MissingArtifact",
    "PlatformMismatch",
    "PlannedAcceleratedDependency",
    "PlannedKeepProduct",
    "ProductAccelerationRequirements",
    "Sha256Mismatch",
    "TransportError",
    "VariantStatus",
    "VersionMismatch",
    "apply_accelerated_deployment",
    "build_accelerated_deployment_plan",
    "default_accelerated_artifact_acquirer",
    "default_accelerated_artifact_manifest",
    "default_accelerated_gate",
    "default_manifest_artifact_acquirer",
    "default_manifest_variant_catalog",
    "default_variant_catalog",
    "evaluate_acceleration_compatibility",
    "evaluate_host_prerequisites",
    "extend_runtime_lock_with_acceleration",
    "get_backend_compute_probe",
    "load_accelerated_artifact_manifest",
    "load_accelerated_artifact_manifest_file",
    "variant_catalog_from_artifact_manifest",
]
