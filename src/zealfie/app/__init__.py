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
from .service import (
    ComponentNotInstalledError,
    LaunchContractNotSatisfiedError,
    LaunchPreparationError,
    LaunchScriptNotFoundError,
    OfflineReleaseError,
    ZeAlfieService,
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

# M1-2B: SpawnedLaunch re-exported so that GUI consumers import from the
# application layer.
from zealfie.launching import SpawnedLaunch

__all__ = [
    "ComponentNotInstalledError",
    "FULL_NAME",
    "InvalidRemoteSourceError",
    "LaunchContractNotSatisfiedError",
    "LaunchPreparationError",
    "LaunchScriptNotFoundError",
    "ManagedStatus",
    "OfflineReleaseError",
    "ProductCatalog",
    "ProductDescriptor",
    "ProductShellState",
    "ProductState",
    "ProductStateReasonCode",
    "RemoteSource",
    "ResolvedSource",
    "RuntimeStatus",
    "SourceError",
    "SourceRefResolver",
    "SourceResolutionError",
    "SpawnedLaunch",
    "UnknownProductError",
    "ZeAlfieService",
    "collect_status",
    "format_component_status",
    "format_status",
    "resolve_source",
    "startup_message",
]
