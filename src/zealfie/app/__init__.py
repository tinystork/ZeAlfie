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
    ProductDependencyAcquisitionError,
    ProductDeploymentPlanningError,
    ProductInstallPreparationError,
    RemoteSourceUnavailableError,
    ZeAlfieService,
)

# M1-2E E.1: Installed-product provenance re-exported from the runtime layer
# for consistency with existing re-export patterns.
from zealfie.runtime.provenance import (
    ProductProvenance,
    ProductProvenanceStore,
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

# M1-2B: SpawnedLaunch re-exported so that GUI consumers import from the
# application layer.
from zealfie.launching import SpawnedLaunch

__all__ = [
    "bootstrap_selection_from_legacy_registry",
    "ComponentNotInstalledError",
    "CorruptSelectionError",
    "DesiredProductSelection",
    "FULL_NAME",
    "InstallPhase",
    "InstallProgress",
    "InvalidRemoteSourceError",
    "LaunchContractNotSatisfiedError",
    "LaunchPreparationError",
    "LaunchScriptNotFoundError",
    "ManagedStatus",
    "OfflineReleaseError",
    "PHASE_PERCENT",
    "PreparedProductArtifact",
    "ProductProvenance",
    "ProductProvenanceStore",
    "ProductDeploymentPlanningError",
    "ProductDependencyAcquisitionError",
    "ProductCatalog",
    "ProductDescriptor",
    "ProductInstallPreparationError",
    "ProductShellState",
    "ProductState",
    "ProductStateReasonCode",
    "RemoteSource",
    "RemoteSourceUnavailableError",
    "ResolvedSource",
    "RuntimeStatus",
    "SelectionStore",
    "SelectionStoreError",
    "SourceError",
    "SourceRefResolver",
    "SourceResolutionError",
    "SpawnedLaunch",
    "UnknownProductError",
    "ZeAlfieService",
    "collect_status",
    "default_selection_path",
    "desired_component_registry",
    "format_component_status",
    "format_status",
    "interpolate_percent",
    "materialize_desired_components",
    "resolve_source",
    "startup_message",
    "validate_selection_against_catalog",
]
