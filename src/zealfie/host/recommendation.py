"""Acceleration recommendation interpretation (M1-2G).

Pure, deterministic, and dependency-free:

    HostCapabilities -> AccelerationRecommendation

No probes, no subprocess, no Qt, no package/framework selection, and no
system mutation.  The recommender decides *capability and intention* only —
it never picks a concrete CUDA/PyTorch/CuPy/TensorFlow/Numba package.
"""

from __future__ import annotations

from .models import (
    AccelerationRecommendation,
    CapabilityStatus,
    GpuInfo,
    GpuSetupIntent,
    HostCapabilities,
    HostReasonCode,
    RecommendationStatus,
)

# M1-2G supports exactly one accelerator backend for now.
NVIDIA_CUDA_BACKEND = "NVIDIA_CUDA"


def _recommendation(
    status: RecommendationStatus,
    reason_code: HostReasonCode,
    reason: str,
    capabilities: HostCapabilities,
) -> AccelerationRecommendation:
    return AccelerationRecommendation(
        status=status,
        backend=NVIDIA_CUDA_BACKEND,
        reason_code=reason_code,
        reason=reason,
        gpus=capabilities.nvidia_gpus,
    )


def recommend(capabilities: HostCapabilities) -> AccelerationRecommendation:
    """Interpret host capabilities into an acceleration recommendation.

    Rules (in order):

    1. OS/platform probe failed  -> UNKNOWN.
    2. Evidence is partial       -> UNKNOWN.
    3. No GPUs at all            -> NOT_APPLICABLE.
    4. GPUs but none NVIDIA      -> NOT_APPLICABLE (only NVIDIA_CUDA backend).
    5. NVIDIA present, driver available for at least one GPU -> OFFER_SETUP
       (no runtime-readiness concept exists in M1-2G, so OFFER_SETUP is the
       honest recommendation even when a driver is usable).
    6. NVIDIA present, driver status UNKNOWN -> UNKNOWN (probe error).
    7. NVIDIA present, driver UNAVAILABLE     -> BLOCKED.
    """
    if capabilities.platform_status is CapabilityStatus.UNKNOWN:
        return _recommendation(
            RecommendationStatus.UNKNOWN,
            HostReasonCode.ACCELERATION_UNKNOWN,
            "host platform could not be determined; acceleration status is unknown",
            capabilities,
        )

    if capabilities.partial:
        return _recommendation(
            RecommendationStatus.UNKNOWN,
            HostReasonCode.ACCELERATION_UNKNOWN,
            "host acceleration evidence is incomplete; acceleration status is unknown",
            capabilities,
        )

    if not capabilities.gpus:
        return _recommendation(
            RecommendationStatus.NOT_APPLICABLE,
            HostReasonCode.ACCELERATION_NOT_APPLICABLE,
            "no supported accelerator hardware detected; running in CPU mode",
            capabilities,
        )

    nvidia = capabilities.nvidia_gpus
    if not nvidia:
        return _recommendation(
            RecommendationStatus.NOT_APPLICABLE,
            HostReasonCode.ACCELERATION_NOT_APPLICABLE,
            "no supported NVIDIA accelerator detected; running in CPU mode",
            capabilities,
        )

    # NVIDIA hardware present — evaluate the driver (a supported accelerator
    # with a usable driver is what enables the CUDA path).
    available = [g for g in nvidia if g.driver_status is CapabilityStatus.AVAILABLE]
    unknown = [g for g in nvidia if g.driver_status is CapabilityStatus.UNKNOWN]

    if available:
        model = _primary_model(available)
        detail = f" ({model})" if model else ""
        return _recommendation(
            RecommendationStatus.OFFER_SETUP,
            HostReasonCode.ACCELERATION_OFFER_SETUP,
            f"supported NVIDIA accelerator detected with usable driver{detail}; "
            "GPU setup can be offered",
            capabilities,
        )

    if unknown:
        return _recommendation(
            RecommendationStatus.UNKNOWN,
            HostReasonCode.ACCELERATION_UNKNOWN,
            "NVIDIA accelerator detected but driver status could not be determined",
            capabilities,
        )

    return _recommendation(
        RecommendationStatus.BLOCKED,
        HostReasonCode.ACCELERATION_BLOCKED,
        "NVIDIA accelerator detected but no compatible/usable driver is available",
        capabilities,
    )


def _primary_model(gpus: list[GpuInfo]) -> str | None:
    """Return the model of the first GPU that has one, or ``None``."""
    for gpu in gpus:
        if gpu.model:
            return gpu.model
    return None


# ---------------------------------------------------------------------------
# GpuSetupIntent — preparatory, no-mutation result for the GUI button
# ---------------------------------------------------------------------------


def build_gpu_setup_intent(
    recommendation: AccelerationRecommendation,
) -> GpuSetupIntent:
    """Build a preparatory, no-mutation GPU setup intent from a recommendation.

    The intent never installs or mutates anything.  Its ``message`` is
    honest: it never claims that a CUDA toolkit or accelerated runtime was
    installed, and never claims this version cannot install one.
    """
    status = recommendation.status

    if status is RecommendationStatus.OFFER_SETUP:
        return GpuSetupIntent(
            recommendation=recommendation,
            actionable=True,
            message=(
                "NVIDIA GPU detected with a usable driver. GPU acceleration "
                "can be configured for compatible installed products."
            ),
        )
    if status is RecommendationStatus.ALREADY_READY:
        return GpuSetupIntent(
            recommendation=recommendation,
            actionable=False,
            message="GPU acceleration is already ready.",
        )
    if status is RecommendationStatus.BLOCKED:
        return GpuSetupIntent(
            recommendation=recommendation,
            actionable=False,
            message=(
                "A compatible NVIDIA driver is unavailable. Install the "
                "NVIDIA driver before configuring GPU support."
            ),
        )
    if status is RecommendationStatus.NOT_APPLICABLE:
        return GpuSetupIntent(
            recommendation=recommendation,
            actionable=False,
            message="No supported GPU detected — running in CPU mode.",
        )
    return GpuSetupIntent(
        recommendation=recommendation,
        actionable=False,
        message="GPU acceleration status is unknown.",
    )
