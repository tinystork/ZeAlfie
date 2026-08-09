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

__all__ = [
    "ComponentNotInstalledError",
    "FULL_NAME",
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
    "RuntimeStatus",
    "UnknownProductError",
    "ZeAlfieService",
    "collect_status",
    "format_component_status",
    "format_status",
    "startup_message",
]
