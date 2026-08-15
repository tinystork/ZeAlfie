"""Application-layer services for ZeAlfie."""

from __future__ import annotations

from ._status import (
    FULL_NAME,
    RuntimeStatus,
    collect_status,
    format_component_status,
    format_status,
    startup_message,
)
from .progress import (
    InstallPhase,
    InstallProgress,
    PHASE_PERCENT,
    interpolate_percent,
)
from .service import (
    ComponentNotInstalledError,
    LaunchContractNotSatisfiedError,
    LaunchPreparationError,
    LaunchScriptNotFoundError,
    OfflineReleaseError,
    PreparedProductArtifact,
    ProductChannelUnavailableError,
    ProductCompatibilityBlockedError,
    ProductDependencyAcquisitionError,
    ProductDeploymentPlanningError,
    ProductInstallPreparationError,
    ProductUpdateNotApplicableError,
    RemoteSourceUnavailableError,
    ZeAlfieService,
)
from .updates import (
    ProductUpdateResult,
    UpdateStatus,
    check_product_update,
)
from .update_checks import (
    UpdateCheckCoordinator,
)

# M1-2E E.1: Installed-product provenance re-exported from the runtime layer
# for consistency with existing re-export patterns.
from zealfie.runtime.provenance import (
    ProductProvenance,
    ProductProvenanceStore,
)
# M1-2F Phase 4 corrective: Installed-runtime lock read model re-exported
# from the runtime layer for consistency with the provenance re-export.
from zealfie.runtime.installed_lock import (
    InstalledDependency,
    InstalledLockStore,
    InstalledRuntimeLock,
)

# M1-2G: Host capability / acceleration recommendation API re-exported from
# the application layer so CLI and GUI consumers import from ``zealfie.app``
# rather than from ``zealfie.host`` internals directly.
from zealfie.host import (
    AccelerationRecommendation,
    CapabilityStatus,
    GpuInfo,
    GpuKind,
    GpuSetupIntent,
    HostCapabilities,
    HostReasonCode,
    RecommendationStatus,
    build_gpu_setup_intent,
    recommend,
)

# M1-2A: Product-shell API re-exported from the application layer so that
# CLI and future GUI consumers import from the application layer, not from
# zealfie.products internals directly.
from zealfie.products.catalog import (
    ProductCatalog,
    ProductDescriptor,
    UnknownProductError,
)
from zealfie.products.state import (
    ManagedStatus,
    ProductShellState,
    ProductState,
    ProductStateReasonCode,
)

# M1-2D.1: Remote source models and resolution re-exported from the
# application layer for consistency with existing patterns.
from zealfie.sources import (
    InvalidRemoteSourceError,
    RemoteSource,
    ResolvedSource,
    SourceError,
    SourceRefResolver,
    SourceResolutionError,
    resolve_source,
)

# M1-2D.3: Selection store and materialization re-exported.
from zealfie.products.selection import (
    bootstrap_selection_from_legacy_registry,
    CorruptSelectionError,
    DesiredProductSelection,
    SelectionStore,
    SelectionStoreError,
    default_selection_path,
    desired_component_registry,
    materialize_desired_components,
    validate_selection_against_catalog,
)
from zealfie.products.policy import (
    CorruptProductPolicyError,
    DEFAULT_CHANNEL_REFS,
    ProductPolicy,
    ProductPolicyError,
    ProductPolicyStore,
    default_product_policy,
    default_product_policy_path,
    effective_ref,
)

# M1-2B: SpawnedLaunch re-exported so that GUI consumers import from the
# application layer.
from zealfie.launching import SpawnedLaunch

__all__ = [
    "AccelerationRecommendation",
    "bootstrap_selection_from_legacy_registry",
    "build_gpu_setup_intent",
    "CapabilityStatus",
    "ComponentNotInstalledError",
    "CorruptSelectionError",
    "CorruptProductPolicyError",
    "DEFAULT_CHANNEL_REFS",
    "DesiredProductSelection",
    "FULL_NAME",
    "GpuInfo",
    "GpuKind",
    "GpuSetupIntent",
    "HostCapabilities",
    "HostReasonCode",
    "InstallPhase",
    "InstallProgress",
    "InstalledDependency",
    "InstalledLockStore",
    "InstalledRuntimeLock",
    "InvalidRemoteSourceError",
    "LaunchContractNotSatisfiedError",
    "LaunchPreparationError",
    "LaunchScriptNotFoundError",
    "ManagedStatus",
    "OfflineReleaseError",
    "PHASE_PERCENT",
    "PreparedProductArtifact",
    "ProductChannelUnavailableError",
    "ProductCompatibilityBlockedError",
    "ProductUpdateResult",
    "ProductProvenance",
    "ProductProvenanceStore",
    "ProductDeploymentPlanningError",
    "ProductDependencyAcquisitionError",
    "ProductCatalog",
    "ProductDescriptor",
    "ProductInstallPreparationError",
    "ProductPolicy",
    "ProductPolicyError",
    "ProductPolicyStore",
    "ProductUpdateNotApplicableError",
    "ProductShellState",
    "ProductState",
    "ProductStateReasonCode",
    "RemoteSource",
    "RemoteSourceUnavailableError",
    "ResolvedSource",
    "RuntimeStatus",
    "RecommendationStatus",
    "SelectionStore",
    "SelectionStoreError",
    "SourceError",
    "SourceRefResolver",
    "SourceResolutionError",
    "SpawnedLaunch",
    "UnknownProductError",
    "UpdateStatus",
    "UpdateCheckCoordinator",
    "ZeAlfieService",
    "check_product_update",
    "collect_status",
    "default_selection_path",
    "default_product_policy",
    "default_product_policy_path",
    "desired_component_registry",
    "effective_ref",
    "format_component_status",
    "format_status",
    "interpolate_percent",
    "materialize_desired_components",
    "resolve_source",
    "recommend",
    "startup_message",
    "validate_selection_against_catalog",
]
