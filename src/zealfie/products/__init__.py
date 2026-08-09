"""Product catalog and product-shell read model.

The product catalog describes **what ZeAlfie knows**.
The component registry (deployment) describes **what the user chose to manage/install**.

These two are kept intentionally separate so that:

* ``4 known + 0 installed`` is a valid state;
* ``4 known + only ZeSolver installed`` is a valid state;
* adding a known product to the catalog never forces it into deployment planning.

See :mod:`zealfie.products.catalog` for definitions and loading.
See :mod:`zealfie.products.state` for the runtime product-shell read model.
See :mod:`zealfie.sources` for remote source models and resolution.
"""

from __future__ import annotations

from .catalog import (
    ProductCatalog,
    ProductDescriptor,
    UnknownProductError,
    default_catalog,
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
    "InvalidRemoteSourceError",
    "ManagedStatus",
    "ProductCatalog",
    "ProductDescriptor",
    "ProductShellState",
    "ProductState",
    "ProductStateReasonCode",
    "RemoteSource",
    "ResolvedSource",
    "SourceError",
    "SourceRefResolver",
    "SourceResolutionError",
    "UnknownProductError",
    "default_catalog",
    "resolve_source",
]
