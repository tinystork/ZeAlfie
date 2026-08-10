"""Product catalog and product-shell read model.

The product catalog describes **what ZeAlfie knows**.
The selection store persists **which products the user wants** (the desired product set).
The component registry materialized from catalog + selection is the **derived technical
representation** of that desired set — not the primary source of the user's choice.

These layers are kept intentionally separate so that:

* ``4 known + 0 selected + 0 installed`` is a valid state;
* ``4 known + only ZeSolver selected`` is a valid state;
* adding a known product to the catalog never forces it into the selection or deployment planning.

See :mod:`zealfie.products.catalog` for definitions and loading.
See :mod:`zealfie.products.state` for the runtime product-shell read model.
See :mod:`zealfie.products.selection` for the user selection store and materialization.
See :mod:`zealfie.sources` for remote source models and resolution.
"""

from __future__ import annotations

from .catalog import (
    ProductCatalog,
    ProductDescriptor,
    UnknownProductError,
    default_catalog,
)
from .selection import (
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
from .state import (
    ManagedStatus,
    ProductShellState,
    ProductState,
    ProductStateReasonCode,
)

# M1-2D.1: Re-export remote source types so consumers import from the
# products package, keeping the sources module an implementation detail.
from zealfie.sources import (
    InvalidRemoteSourceError,
    RemoteSource,
    ResolvedSource,
    SourceError,
    SourceRefResolver,
    SourceResolutionError,
    resolve_source,
)

__all__ = [
    "bootstrap_selection_from_legacy_registry",
    "CorruptSelectionError",
    "DesiredProductSelection",
    "InvalidRemoteSourceError",
    "ManagedStatus",
    "ProductCatalog",
    "ProductDescriptor",
    "ProductShellState",
    "ProductState",
    "ProductStateReasonCode",
    "RemoteSource",
    "ResolvedSource",
    "SelectionStore",
    "SelectionStoreError",
    "SourceError",
    "SourceRefResolver",
    "SourceResolutionError",
    "UnknownProductError",
    "default_catalog",
    "default_selection_path",
    "desired_component_registry",
    "materialize_desired_components",
    "resolve_source",
    "validate_selection_against_catalog",
]
