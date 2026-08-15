"""Acceleration requirement contract (M1-2H).

Products declare their accelerated needs as distribution-level
requirements against a known accelerator *backend*; ZeAlfie evaluates
those declarations against the host observation produced by M1-2G.

Architectural invariant: ZeAlfie NEVER selects a concrete accelerated
framework (no PyTorch/CuPy/TensorFlow/Numba choice anywhere in
production code).  It only checks declared backends and cross-product
requirement consistency, fail-closed.
"""

from zealfie.acceleration.compatibility import (
    evaluate_acceleration_compatibility,
)
from zealfie.acceleration.models import (
    AcceleratedRequirement,
    AccelerationIncompatibility,
    HardwareCompatibility,
    HardwareCompatibilityReasonCode,
    HardwareCompatibilityStatus,
    KNOWN_BACKENDS,
    ProductAccelerationRequirements,
)

__all__ = [
    "AcceleratedRequirement",
    "AccelerationIncompatibility",
    "HardwareCompatibility",
    "HardwareCompatibilityReasonCode",
    "HardwareCompatibilityStatus",
    "KNOWN_BACKENDS",
    "ProductAccelerationRequirements",
    "evaluate_acceleration_compatibility",
]
