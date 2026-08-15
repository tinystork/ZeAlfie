"""Host capability discovery and acceleration recommendation (M1-2G).

Exposes the observation/interpretation models, the read-only probe runner,
and the pure recommender.  No Qt, no subprocess calls at import time.
"""

from __future__ import annotations

from .models import (
    AccelerationRecommendation,
    CapabilityStatus,
    GpuInfo,
    GpuKind,
    GpuSetupIntent,
    HostCapabilities,
    HostReasonCode,
    RecommendationStatus,
)
from .probes import HostProber
from .recommendation import (
    NVIDIA_CUDA_BACKEND,
    build_gpu_setup_intent,
    recommend,
)

__all__ = [
    "AccelerationRecommendation",
    "CapabilityStatus",
    "GpuInfo",
    "GpuKind",
    "GpuSetupIntent",
    "HostCapabilities",
    "HostProber",
    "HostReasonCode",
    "NVIDIA_CUDA_BACKEND",
    "RecommendationStatus",
    "build_gpu_setup_intent",
    "recommend",
]
