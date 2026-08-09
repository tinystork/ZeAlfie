"""Shared runtime dependency resolution (M1-1A).

Pure planning: ``Requires-Dist`` / extras / wheelhouse
→ exact ``RuntimeLock`` → NO MUTATION.

M1-1B will consume ``RuntimeLock`` for materialization.
"""

from __future__ import annotations

from .host_tags import (
    HostTagProvider,
    SysTagProvider,
    default_compatible_tags,
    default_marker_env,
)
from .models import (
    AmbiguousDependency,
    ConstraintConflict,
    DependencyResolutionError,
    ExtraNotFound,
    IncompatibleWheelTag,
    LockedDependency,
    MetadataError,
    MissingDependency,
    RuntimeLock,
    WheelIdentityMismatch,
)
from .resolver import resolve_runtime_dependencies

__all__ = [
    "AmbiguousDependency",
    "ConstraintConflict",
    "DependencyResolutionError",
    "ExtraNotFound",
    "HostTagProvider",
    "IncompatibleWheelTag",
    "LockedDependency",
    "MetadataError",
    "MissingDependency",
    "RuntimeLock",
    "SysTagProvider",
    "WheelIdentityMismatch",
    "default_compatible_tags",
    "default_marker_env",
    "resolve_runtime_dependencies",
]
