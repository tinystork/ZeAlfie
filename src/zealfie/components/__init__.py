"""Component inspection primitives for ZeAlfie."""

from __future__ import annotations

from .manifest import (
    InvalidComponentManifestError,
    ManifestError,
    UnsupportedManifestSchemaError,
    load_default_component_definitions,
)
from .metadata import inspect_component
from .model import (
    ComponentDefinition,
    ComponentStatus,
    EntryPointContract,
    EntryPointInfo,
    ReasonCode,
)
from .registry import ComponentRegistry, UnknownComponentError, default_registry

__all__ = [
    "ComponentDefinition",
    "ComponentRegistry",
    "ComponentStatus",
    "EntryPointContract",
    "EntryPointInfo",
    "InvalidComponentManifestError",
    "ManifestError",
    "ReasonCode",
    "UnsupportedManifestSchemaError",
    "UnknownComponentError",
    "default_registry",
    "inspect_component",
    "load_default_component_definitions",
]
