"""Component inspection primitives for ZeAlfie."""

from __future__ import annotations

from .definitions import KNOWN_COMPONENTS
from .metadata import inspect_component
from .model import ComponentDefinition, ComponentStatus, ReasonCode
from .registry import ComponentRegistry, UnknownComponentError, default_registry

__all__ = [
    "ComponentDefinition",
    "ComponentRegistry",
    "ComponentStatus",
    "KNOWN_COMPONENTS",
    "ReasonCode",
    "UnknownComponentError",
    "default_registry",
    "inspect_component",
]
