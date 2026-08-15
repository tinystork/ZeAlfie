"""Acceleration requirement models (M1-2H).

Pure frozen value objects describing what products require from the
host's hardware acceleration, plus the result of evaluating those
requirements against the host.

Architectural invariant — ZeAlfie NEVER selects a concrete accelerated
framework.  A product declares its accelerated needs as Python
*distribution names* (for example a wheel that ships accelerated
kernels); ZeAlfie only checks that the declared backend is known and
that cross-product requirements do not conflict.  The runtime never
decides which accelerated implementation to install.

These models know nothing about Qt, subprocess, ``nvidia-smi``, or any
concrete GPU framework.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name

#: Acceleration backends this ZeAlfie release knows how to reason about.
#: Anything else is rejected fail-closed — never guessed at.
KNOWN_BACKENDS = frozenset({"NVIDIA_CUDA"})

#: Default provenance marker for requirements declared in the product
#: catalog.  Future sources (release manifests, runtime witnesses) may
#: declare their own provenance strings.
DEFAULT_ACCELERATION_SOURCE = "zealfie-catalog@1"


def _first_duplicate(values: list[str]) -> str:
    """Return the first duplicated value, or ``""`` if none."""
    seen: set[str] = set()
    for value in values:
        if value in seen:
            return value
        seen.add(value)
    return ""


@dataclass(frozen=True, slots=True)
class AcceleratedRequirement:
    """One accelerated distribution a product requires.

    ``distribution`` is canonicalized (PEP 503) and is the only linkage
    key.  ``specifier`` is an optional PEP 440 version range string that
    must parse as :class:`~packaging.specifiers.SpecifierSet`; ``None``
    means "any version".  ``extras`` are canonicalized, sorted, and
    must not contain duplicates.
    """

    distribution: str
    specifier: str | None = None
    extras: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.distribution, str) or not self.distribution.strip():
            raise ValueError("distribution must be a non-empty string")
        object.__setattr__(
            self, "distribution", canonicalize_name(self.distribution.strip())
        )

        specifier = self.specifier
        if specifier is not None:
            if not isinstance(specifier, str) or not specifier.strip():
                raise ValueError(
                    "specifier must be None or a non-empty string"
                )
            try:
                SpecifierSet(specifier)
            except InvalidSpecifier as exc:
                raise ValueError(
                    f"invalid specifier {specifier!r}: {exc}"
                ) from exc
            object.__setattr__(self, "specifier", specifier.strip())

        canon_extras: list[str] = []
        seen: set[str] = set()
        for raw_extra in self.extras:
            if not isinstance(raw_extra, str) or not raw_extra.strip():
                raise ValueError("extras must not contain empty values")
            extra = canonicalize_name(raw_extra.strip())
            if extra in seen:
                raise ValueError(f"duplicate extra: {extra}")
            seen.add(extra)
            canon_extras.append(extra)
        object.__setattr__(self, "extras", tuple(sorted(canon_extras)))


@dataclass(frozen=True, slots=True)
class AccelerationIncompatibility:
    """A distribution this product must NOT share a runtime with.

    ``distribution`` is canonicalized (PEP 503).  ``reason`` is a
    human-readable explanation of why the distribution is incompatible.
    """

    distribution: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.distribution, str) or not self.distribution.strip():
            raise ValueError("distribution must be a non-empty string")
        object.__setattr__(
            self, "distribution", canonicalize_name(self.distribution.strip())
        )
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason must be a non-empty string")
        object.__setattr__(self, "reason", self.reason.strip())


@dataclass(frozen=True, slots=True)
class ProductAccelerationRequirements:
    """Accelerated requirements declared by one product (M1-2H).

    ``backend`` must be a member of :data:`KNOWN_BACKENDS` — ZeAlfie
    refuses to reason about unknown acceleration backends (fail-closed).

    ``optional`` marks whether the requirements are hard needs or nice
    to haves; it is carried for future planning decisions and does not
    currently change the compatibility verdict (all declared
    requirements participate in conflict checks).

    A distribution must never appear in both ``requirements`` and
    ``incompatibilities``, and must not be duplicated within either.
    """

    product_id: str
    backend: str
    optional: bool = True
    requirements: tuple[AcceleratedRequirement, ...] = ()
    incompatibilities: tuple[AccelerationIncompatibility, ...] = ()
    source: str = DEFAULT_ACCELERATION_SOURCE

    def __post_init__(self) -> None:
        if not isinstance(self.product_id, str) or not self.product_id.strip():
            raise ValueError("product_id must be a non-empty string")
        object.__setattr__(self, "product_id", self.product_id.strip())

        backend = self.backend
        if not isinstance(backend, str) or not backend.strip():
            raise ValueError("backend must be a non-empty string")
        backend = backend.strip()
        if backend not in KNOWN_BACKENDS:
            raise ValueError(f"unsupported acceleration backend {backend!r}")
        object.__setattr__(self, "backend", backend)

        if not isinstance(self.optional, bool):
            raise ValueError("optional must be a bool")

        requirements = tuple(self.requirements)
        for req in requirements:
            if not isinstance(req, AcceleratedRequirement):
                raise ValueError(
                    "requirements must contain AcceleratedRequirement values, "
                    f"got {type(req).__qualname__}"
                )
        incompatibilities = tuple(self.incompatibilities)
        for inc in incompatibilities:
            if not isinstance(inc, AccelerationIncompatibility):
                raise ValueError(
                    "incompatibilities must contain AccelerationIncompatibility "
                    f"values, got {type(inc).__qualname__}"
                )

        req_dists = [req.distribution for req in requirements]
        duplicate = _first_duplicate(req_dists)
        if duplicate:
            raise ValueError(f"duplicate requirement distribution: {duplicate}")

        inc_dists = [inc.distribution for inc in incompatibilities]
        duplicate = _first_duplicate(inc_dists)
        if duplicate:
            raise ValueError(
                f"duplicate incompatibility distribution: {duplicate}"
            )

        overlap = set(req_dists) & set(inc_dists)
        if overlap:
            raise ValueError(
                "distribution declared both as requirement and as "
                f"incompatibility: {sorted(overlap)[0]}"
            )

        object.__setattr__(self, "requirements", requirements)
        object.__setattr__(self, "incompatibilities", incompatibilities)

        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source must be a non-empty string")
        object.__setattr__(self, "source", self.source.strip())


class HardwareCompatibilityStatus(str, Enum):
    """Overall verdict of the acceleration compatibility evaluation."""

    SUPPORTED = "SUPPORTED"
    """Host can satisfy all declared requirements with no conflicts."""

    BLOCKED = "BLOCKED"
    """Evaluation found a blocking incompatibility."""

    UNKNOWN = "UNKNOWN"
    """Host evidence is insufficient to decide."""


class HardwareCompatibilityReasonCode(str, Enum):
    """Stable machine-readable reason codes for compatibility verdicts.

    Deliberately stable strings so CLI/API consumers can rely on them
    without parsing prose.
    """

    # No requirements to evaluate.
    NO_ACCELERATION_REQUIREMENTS = "NO_ACCELERATION_REQUIREMENTS"

    # Host observation / interpretation outcomes.
    HOST_CAPABILITIES_PARTIAL = "HOST_CAPABILITIES_PARTIAL"
    UNSUPPORTED_ACCELERATION_BACKEND = "UNSUPPORTED_ACCELERATION_BACKEND"
    ACCELERATION_NOT_APPLICABLE = "ACCELERATION_NOT_APPLICABLE"
    ACCELERATION_BLOCKED = "ACCELERATION_BLOCKED"
    ACCELERATION_UNKNOWN = "ACCELERATION_UNKNOWN"

    # Cross-product requirement conflicts.
    REQUIREMENT_CONFLICT = "REQUIREMENT_CONFLICT"

    # Host and requirements agree.
    COMPATIBLE = "COMPATIBLE"


@dataclass(frozen=True, slots=True)
class HardwareCompatibility:
    """Result of evaluating acceleration requirements against the host.

    ``reason_code`` is a stable machine-readable string (values of
    :class:`HardwareCompatibilityReasonCode`); ``reason`` is
    human-readable prose.  ``products_concerned`` lists the sorted
    product ids affected by the verdict.  ``conflicts`` carries the
    deterministic, human-readable conflict details when the verdict is
    ``BLOCKED`` for cross-product reasons.
    """

    status: HardwareCompatibilityStatus
    reason_code: str
    reason: str
    products_concerned: tuple[str, ...]
    conflicts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.status, HardwareCompatibilityStatus):
            raise ValueError(
                "status must be a HardwareCompatibilityStatus, "
                f"got {type(self.status).__qualname__}"
            )
        if not isinstance(self.reason_code, str) or not self.reason_code.strip():
            raise ValueError("reason_code must be a non-empty string")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason must be a non-empty string")
        object.__setattr__(
            self, "products_concerned", tuple(self.products_concerned)
        )
        object.__setattr__(self, "conflicts", tuple(self.conflicts))
