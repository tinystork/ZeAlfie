"""Host capability and acceleration recommendation models (M1-2G).

These are pure value objects.  They encode the architectural invariant:

    OBSERVATION HostCapabilities
        -> INTERPRETATION AccelerationRecommendation
        -> SERVICE
        -> GUI / CLI

* :class:`HostCapabilities` is a read-only *observation* of the host
  (OS name, CPU architecture, and zero/one/many GPUs).
* :class:`AccelerationRecommendation` is the *interpretation* derived from
  that observation by a pure recommender.

Neither model knows about Qt, subprocess, ``nvidia-smi``, ``/sys``,
``/proc``, or any specific probe.  Tri-state statuses are used instead of
booleans so that "we could not tell" is always distinguishable from
"available" and "unavailable".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CapabilityStatus(str, Enum):
    """Tri-state capability status — never collapses to a boolean.

    * ``AVAILABLE``   — the capability was observed and is usable.
    * ``UNAVAILABLE`` — the capability was observed to be absent.
    * ``UNKNOWN``     — the capability could not be determined (probe
      failed, insufficient evidence, malformed output, etc.).
    """

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class GpuKind(str, Enum):
    """Whether a GPU is integrated, discrete, or not determinable."""

    INTEGRATED = "INTEGRATED"
    DISCRETE = "DISCRETE"
    UNKNOWN = "UNKNOWN"


class RecommendationStatus(str, Enum):
    """Interpreted acceleration recommendation status (M1-2G)."""

    OFFER_SETUP = "OFFER_SETUP"
    ALREADY_READY = "ALREADY_READY"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


class HostReasonCode(str, Enum):
    """Stable machine-readable reason codes for host observations.

    These are deliberately stable strings so CLI/API consumers can rely on
    them without parsing prose.  They are observations, not conclusions.
    """

    # Platform / OS
    OS_DETECTED = "OS_DETECTED"
    OS_PROBE_FAILED = "OS_PROBE_FAILED"
    CPU_ARCH_DETECTED = "CPU_ARCH_DETECTED"
    CPU_ARCH_PROBE_FAILED = "CPU_ARCH_PROBE_FAILED"

    # GPU hardware presence
    GPU_HARDWARE_DETECTED = "GPU_HARDWARE_DETECTED"
    NO_ACCELERATOR_HARDWARE = "NO_ACCELERATOR_HARDWARE"
    GPU_HARDWARE_UNKNOWN = "GPU_HARDWARE_UNKNOWN"

    # NVIDIA driver
    NVIDIA_DRIVER_AVAILABLE = "NVIDIA_DRIVER_AVAILABLE"
    NVIDIA_DRIVER_UNAVAILABLE = "NVIDIA_DRIVER_UNAVAILABLE"
    NVIDIA_DRIVER_UNKNOWN = "NVIDIA_DRIVER_UNKNOWN"
    NVIDIA_SMI_UNAVAILABLE = "NVIDIA_SMI_UNAVAILABLE"
    NVIDIA_SMI_MALFORMED = "NVIDIA_SMI_MALFORMED"

    # Evidence quality
    PARTIAL_EVIDENCE = "PARTIAL_EVIDENCE"

    # Recommendation conclusions
    SUPPORTED_ACCELERATOR_DETECTED = "SUPPORTED_ACCELERATOR_DETECTED"
    ACCELERATION_NOT_APPLICABLE = "ACCELERATION_NOT_APPLICABLE"
    ACCELERATION_BLOCKED = "ACCELERATION_BLOCKED"
    ACCELERATION_UNKNOWN = "ACCELERATION_UNKNOWN"
    ACCELERATION_OFFER_SETUP = "ACCELERATION_OFFER_SETUP"
    ACCELERATION_ALREADY_READY = "ACCELERATION_ALREADY_READY"


@dataclass(frozen=True, slots=True)
class GpuInfo:
    """Observation of a single GPU.

    ``hardware_present`` records whether the GPU hardware was actually
    observed.  Driver availability is a separate tri-state
    :class:`CapabilityStatus` so a present-but-driverless GPU is never
    collapsed into a boolean.
    """

    vendor: str
    model: str | None
    kind: GpuKind
    hardware_present: bool
    driver_status: CapabilityStatus
    driver_version: str | None
    driver_reason_code: HostReasonCode | None
    driver_reason: str | None
    nvidia_smi_available: bool = False
    cuda_driver_present: bool = False

    @property
    def is_nvidia(self) -> bool:
        """True when the GPU vendor is NVIDIA (case-sensitive exact match)."""
        return self.vendor == "NVIDIA"


@dataclass(frozen=True, slots=True)
class HostCapabilities:
    """Read-only observation of the host platform and GPUs.

    ``partial`` is ``True`` when the probes could not produce a complete,
    confident observation (for example the OS probe failed, or the GPU
    hardware probes all errored).  ``runtime_hints`` are informational
    hints for *future* runtime selection — never authoritative.
    """

    os_name: str | None
    cpu_arch: str | None
    platform_status: CapabilityStatus
    platform_reason_code: HostReasonCode | None
    platform_reason: str | None
    gpus: tuple[GpuInfo, ...]
    partial: bool
    reason_codes: tuple[HostReasonCode, ...] = ()
    runtime_hints: tuple[str, ...] = ()

    @property
    def gpu_count(self) -> int:
        return len(self.gpus)

    @property
    def nvidia_gpus(self) -> tuple[GpuInfo, ...]:
        return tuple(g for g in self.gpus if g.is_nvidia)

    @property
    def has_gpu(self) -> bool:
        return bool(self.gpus)


@dataclass(frozen=True, slots=True)
class AccelerationRecommendation:
    """Interpreted acceleration recommendation, separated from observation.

    ``backend`` names the accelerator backend this recommendation is about
    (only ``NVIDIA_CUDA`` is implemented in M1-2G).  ``gpus`` carries the
    supporting observations for diagnostics and display.
    """

    status: RecommendationStatus
    backend: str
    reason_code: HostReasonCode
    reason: str
    gpus: tuple[GpuInfo, ...] = ()

    @property
    def offer_setup(self) -> bool:
        return self.status is RecommendationStatus.OFFER_SETUP

    @property
    def blocked(self) -> bool:
        return self.status is RecommendationStatus.BLOCKED

    @property
    def applicable(self) -> bool:
        return self.status in (
            RecommendationStatus.OFFER_SETUP,
            RecommendationStatus.ALREADY_READY,
            RecommendationStatus.BLOCKED,
        )


@dataclass(frozen=True, slots=True)
class GpuSetupIntent:
    """A preparatory, no-mutation result for the GUI configure action.

    ``actionable`` is ``True`` only when a configure action makes sense.
    ``performed_any_mutation`` is always ``False`` in M1-2G: the intent
    never installs or changes the system, and the message must never claim
    installation success.
    """

    recommendation: AccelerationRecommendation
    actionable: bool
    message: str
    performed_any_mutation: bool = False
