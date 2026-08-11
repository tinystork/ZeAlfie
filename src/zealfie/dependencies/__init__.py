"""Shared runtime dependency resolution (M1-1A).

Pure planning: ``Requires-Dist`` / extras / wheelhouse
→ exact ``RuntimeLock`` → NO MUTATION.

M1-1B will consume ``RuntimeLock`` for materialization.

M1-2D.4.2A adds dependency acquisition contract models
(``DependencyAcquisitionRequest``, ``DependencyAcquisitionResult``,
``AcquiredWheel``) and pre-flight validation
(``build_acquisition_request``).

M1-2D.4.2B adds the minimal ``PipWheelhouseAcquirer`` transport.
"""

from __future__ import annotations

from .acquisition import (
    AcquiredWheel,
    AcquisitionTransportError,
    DependencyAcquisitionError,
    DependencyAcquisitionRequest,
    DependencyAcquisitionResult,
    build_acquisition_request,
)
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
from .pip_acquirer import PipWheelhouseAcquirer
from .resolver import resolve_runtime_dependencies

__all__ = [
    "AcquiredWheel",
    "AcquisitionTransportError",
    "AmbiguousDependency",
    "ConstraintConflict",
    "DependencyAcquisitionError",
    "DependencyAcquisitionRequest",
    "DependencyAcquisitionResult",
    "DependencyResolutionError",
    "ExtraNotFound",
    "HostTagProvider",
    "IncompatibleWheelTag",
    "LockedDependency",
    "MetadataError",
    "MissingDependency",
    "PipWheelhouseAcquirer",
    "RuntimeLock",
    "SysTagProvider",
    "WheelIdentityMismatch",
    "build_acquisition_request",
    "default_compatible_tags",
    "default_marker_env",
    "resolve_runtime_dependencies",
]
