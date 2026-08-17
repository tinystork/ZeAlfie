"""Pure hardware acceleration compatibility evaluation (M1-2H).

Deterministic, fail-closed evaluation of the acceleration requirements
declared by products (see :mod:`zealfie.acceleration.models`) against
the host observation and its interpretation produced by M1-2G.

This module is pure: no I/O, no subprocess, no network, no Qt, and no
mutation of its inputs.  It never selects a concrete accelerated
framework — it only checks declared backends and cross-product
requirement consistency.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from zealfie.acceleration.models import (
    HardwareCompatibility,
    HardwareCompatibilityReasonCode,
    HardwareCompatibilityStatus,
    KNOWN_BACKENDS,
    ProductAccelerationRequirements,
)
from zealfie.host.models import (
    AccelerationRecommendation,
    CapabilityStatus,
    HostCapabilities,
    RecommendationStatus,
)


def evaluate_acceleration_compatibility(
    requirements_map: Mapping[str, ProductAccelerationRequirements],
    capabilities: HostCapabilities,
    recommendation: AccelerationRecommendation,
) -> HardwareCompatibility:
    """Evaluate declared acceleration requirements against the host.

    Rules are applied in a fixed order; the first decisive rule wins
    (fail-closed, deterministic):

    1. empty *requirements_map* → ``BLOCKED`` — there is nothing to
       evaluate and silence must never mean "supported";
    2. ``capabilities.partial`` → ``UNKNOWN``;
    3. any declared ``backend`` outside :data:`KNOWN_BACKENDS` →
       ``BLOCKED``;
    4. recommendation ``NOT_APPLICABLE`` → ``BLOCKED``;
    5. recommendation ``BLOCKED`` → ``BLOCKED`` (reason passthrough);
    6. recommendation ``UNKNOWN`` → ``UNKNOWN`` (reason passthrough);
    7. recommendation ``ALREADY_READY`` / ``OFFER_SETUP`` → host side
       is fine; proceed to cross-product conflict checks;
    8. cross-product conflicts (exact-pin disagreements, pin vs
       excluding range, obviously disjoint simple ranges, requirement
       vs declared incompatibility) → ``BLOCKED`` with conflict
       details; otherwise ``SUPPORTED``.

    Cross-product checks run over ALL declared requirements in a
    deterministic order: sorted product ids, then declaration order
    within each product.
    """
    product_ids = sorted(requirements_map)

    if not product_ids:
        return HardwareCompatibility(
            status=HardwareCompatibilityStatus.BLOCKED,
            reason_code=(
                HardwareCompatibilityReasonCode.NO_ACCELERATION_REQUIREMENTS.value
            ),
            reason=(
                "no product declares accelerated requirements; "
                "nothing to evaluate"
            ),
            products_concerned=(),
        )

    if capabilities.partial:
        return HardwareCompatibility(
            status=HardwareCompatibilityStatus.UNKNOWN,
            reason_code=HardwareCompatibilityReasonCode.HOST_CAPABILITIES_PARTIAL.value,
            reason=(
                "host capability observation is partial; acceleration "
                "compatibility is unknown"
            ),
            products_concerned=tuple(product_ids),
        )

    unknown_backends = sorted(
        product_id
        for product_id in product_ids
        if requirements_map[product_id].backend not in KNOWN_BACKENDS
    )
    if unknown_backends:
        backend = requirements_map[unknown_backends[0]].backend
        return HardwareCompatibility(
            status=HardwareCompatibilityStatus.BLOCKED,
            reason_code=(
                HardwareCompatibilityReasonCode.UNSUPPORTED_ACCELERATION_BACKEND.value
            ),
            reason=f"unsupported acceleration backend {backend}",
            products_concerned=tuple(unknown_backends),
        )

    status = recommendation.status
    if status is RecommendationStatus.NOT_APPLICABLE:
        return HardwareCompatibility(
            status=HardwareCompatibilityStatus.BLOCKED,
            reason_code=HardwareCompatibilityReasonCode.ACCELERATION_NOT_APPLICABLE.value,
            reason="no supported accelerator hardware detected",
            products_concerned=tuple(product_ids),
        )
    if status is RecommendationStatus.BLOCKED:
        return HardwareCompatibility(
            status=HardwareCompatibilityStatus.BLOCKED,
            reason_code=HardwareCompatibilityReasonCode.ACCELERATION_BLOCKED.value,
            reason=recommendation.reason,
            products_concerned=tuple(product_ids),
        )
    if status is RecommendationStatus.UNKNOWN:
        return HardwareCompatibility(
            status=HardwareCompatibilityStatus.UNKNOWN,
            reason_code=HardwareCompatibilityReasonCode.ACCELERATION_UNKNOWN.value,
            reason=recommendation.reason,
            products_concerned=tuple(product_ids),
        )

    # Host side is OK (ALREADY_READY or OFFER_SETUP): cross-product checks.
    conflicts, involved = _find_conflicts(requirements_map, product_ids)
    if conflicts:
        return HardwareCompatibility(
            status=HardwareCompatibilityStatus.BLOCKED,
            reason_code=HardwareCompatibilityReasonCode.REQUIREMENT_CONFLICT.value,
            reason="; ".join(conflicts),
            products_concerned=tuple(sorted(involved)),
            conflicts=tuple(conflicts),
        )

    return HardwareCompatibility(
        status=HardwareCompatibilityStatus.SUPPORTED,
        reason_code=HardwareCompatibilityReasonCode.COMPATIBLE.value,
        reason=(
            "host acceleration is compatible with all declared product "
            "requirements"
        ),
        products_concerned=(),
    )


# ---------------------------------------------------------------------------
# Host prerequisites classification (ZA-M1-2J.2 Phase F)
# ---------------------------------------------------------------------------


class HostPrerequisitesStatus(str, Enum):
    """Overall verdict of the host prerequisites evaluation."""

    OK = "OK"
    """Every checkable host prerequisite is satisfied (or none exists)."""

    BLOCKED = "BLOCKED"
    """A checkable host prerequisite is missing/insufficient."""


class HostPrerequisiteStatus(str, Enum):
    """Per-entry status inside a host prerequisites classification."""

    OK = "OK"
    """Observed and satisfied."""

    BELOW_MINIMUM = "BELOW_MINIMUM"
    """Observed and below the required minimum (blocks)."""

    NOT_OBSERVED = "NOT_OBSERVED"
    """Not observable by this ZeAlfie release — documented, never
    fabricated into a verdict."""

    MANAGED = "MANAGED"
    """Managed by the runtime closure, not required from the host."""

    @property
    def display(self) -> str:
        """User-facing label (never the raw enum name in UI output)."""
        return {
            HostPrerequisiteStatus.OK: "ok",
            HostPrerequisiteStatus.BELOW_MINIMUM: "below minimum",
            HostPrerequisiteStatus.NOT_OBSERVED: "not observed",
            HostPrerequisiteStatus.MANAGED: "managed",
        }[self]


@dataclass(frozen=True, slots=True)
class HostPrerequisiteEntry:
    """One classified prerequisite line of a backend closure.

    ``entry`` names the host-side condition (e.g. ``nvidia-driver``) or
    a managed distribution; ``requirement`` is the exact requirement
    text; ``status`` is the honest per-entry verdict; ``observed`` is
    the observed value (``None`` when not observed).
    """

    entry: str
    requirement: str
    status: HostPrerequisiteStatus
    observed: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.entry, str) or not self.entry.strip():
            raise ValueError("entry must be a non-empty string")
        object.__setattr__(self, "entry", self.entry.strip())
        if not isinstance(self.requirement, str) or not self.requirement.strip():
            raise ValueError("requirement must be a non-empty string")
        object.__setattr__(self, "requirement", self.requirement.strip())
        if not isinstance(self.status, HostPrerequisiteStatus):
            raise ValueError(
                "status must be a HostPrerequisiteStatus, "
                f"got {type(self.status).__qualname__}"
            )
        observed = self.observed
        if observed is not None:
            if not isinstance(observed, str) or not observed.strip():
                raise ValueError(
                    "observed must be None or a non-empty string"
                )
            object.__setattr__(self, "observed", observed.strip())


@dataclass(frozen=True, slots=True)
class HostPrerequisites:
    """Host prerequisites classification for one accelerator backend.

    ``required_host`` lists the host-side conditions that must already
    hold — ZeAlfie NEVER installs host drivers or toolkits.
    ``managed_runtime`` lists what the runtime closure manages
    (distribution name + exact pinned version), assembled by the
    planner from the selected variants.  ``status`` is the honest
    verdict of the checkable checks; ``reason`` explains a BLOCKED
    verdict.
    """

    status: HostPrerequisitesStatus
    required_host: tuple[HostPrerequisiteEntry, ...]
    managed_runtime: tuple[HostPrerequisiteEntry, ...]
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, HostPrerequisitesStatus):
            raise ValueError(
                "status must be a HostPrerequisitesStatus, "
                f"got {type(self.status).__qualname__}"
            )
        required = tuple(self.required_host)
        for entry in required:
            if not isinstance(entry, HostPrerequisiteEntry):
                raise ValueError(
                    "required_host must contain HostPrerequisiteEntry "
                    f"values, got {type(entry).__qualname__}"
                )
        object.__setattr__(self, "required_host", required)
        managed = tuple(self.managed_runtime)
        for entry in managed:
            if not isinstance(entry, HostPrerequisiteEntry):
                raise ValueError(
                    "managed_runtime must contain HostPrerequisiteEntry "
                    f"values, got {type(entry).__qualname__}"
                )
        object.__setattr__(self, "managed_runtime", managed)
        reason = self.reason
        if reason is not None:
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError(
                    "reason must be None or a non-empty string"
                )
            object.__setattr__(self, "reason", reason.strip())


#: Curated per-backend REQUIRED_HOST knowledge (2026-08-16, Phase F).
#: Source of truth: the machine-readable closure spec
#: ``AGENT/tmp/m1_2j_2_cuda_closure/closure_nvidia_cuda_20260816.toml``
#: and the Phase C/D sandbox reports.  Host-side conditions only —
#: ZeAlfie never installs drivers or toolkits.
BACKEND_REQUIRED_HOST: dict[str, tuple[tuple[str, str], ...]] = {
    "NVIDIA_CUDA": (
        (
            "nvidia-driver",
            ">= 550.54.14 (minimum officiel CUDA 12.4)",
        ),
        (
            "nvidia-gpu-cc",
            "NVIDIA GPU Compute Capability >= 6.0 (Pascal+)",
        ),
    ),
}

#: NOTE on the driver floor: this is a SINGLE curated value (derived
#: from Linux observations) shared by every platform — the real Windows
#: witness driver 576.80 satisfied the same check, so user-facing text
#: must never claim a specific platform for it.  Per-platform floors
#: are a documented FOLLOW-UP, not implemented here.
#: Checkable driver version floor per backend (parsed against the
#: observed ``GpuInfo.driver_version``).
BACKEND_DRIVER_FLOORS: dict[str, str] = {
    "NVIDIA_CUDA": "550.54.14",
}

#: Managed-runtime cost note per backend (curated sandbox measurement,
#: 2026-08-16).  Approximate on purpose — the exact bytes are enforced
#: by the artifact manifest, not by this note.
BACKEND_MANAGED_RUNTIME_COST: dict[str, str] = {
    "NVIDIA_CUDA": "~1.16 Go download / ~1.7 Go installed",
}


def evaluate_host_prerequisites(
    backend: str,
    capabilities: HostCapabilities,
) -> HostPrerequisites:
    """Evaluate the checkable host prerequisites for *backend* (pure).

    Only observable, checkable violations decide: an NVIDIA driver
    version that parses and is below the curated floor BLOCKs the
    evaluation; missing or unparseable observations are recorded as
    ``NOT_OBSERVED`` and never fabricate a verdict (absence of usable
    drivers is already gated upstream by the recommendation).  The
    Compute Capability floor has no observation channel in this ZeAlfie
    release — it is documented as REQUIRED_HOST ``NOT_OBSERVED`` at plan
    time and verified for real at activation time by the backend
    compute probe (NVRTC compile fails on unsupported architectures).

    ``managed_runtime`` is assembled by the planner (this function
    knows nothing about the variant catalog); a backend without curated
    knowledge returns an empty ``OK`` classification — generic backends
    keep the previous behaviour.
    """
    if not isinstance(backend, str) or not backend.strip():
        raise ValueError("backend must be a non-empty string")
    backend = backend.strip()

    required = BACKEND_REQUIRED_HOST.get(backend)
    if not required:
        return HostPrerequisites(
            status=HostPrerequisitesStatus.OK,
            required_host=(),
            managed_runtime=(),
            reason=None,
        )

    driver_floor = BACKEND_DRIVER_FLOORS.get(backend)
    entries: list[HostPrerequisiteEntry] = []
    status = HostPrerequisitesStatus.OK
    reason: str | None = None

    for entry_name, requirement in required:
        if entry_name == "nvidia-driver" and driver_floor is not None:
            entry, below = _evaluate_driver_prerequisite(
                entry_name, requirement, driver_floor, capabilities
            )
            entries.append(entry)
            if below:
                status = HostPrerequisitesStatus.BLOCKED
                reason = (
                    f"host prerequisite not satisfied for {backend}: "
                    f"nvidia-driver {entry.observed} does not meet "
                    f">= {driver_floor} (minimum officiel CUDA 12.4)"
                )
        else:
            entries.append(
                HostPrerequisiteEntry(
                    entry=entry_name,
                    requirement=requirement,
                    status=HostPrerequisiteStatus.NOT_OBSERVED,
                )
            )

    return HostPrerequisites(
        status=status,
        required_host=tuple(entries),
        managed_runtime=(),
        reason=reason,
    )


def _evaluate_driver_prerequisite(
    entry: str,
    requirement: str,
    floor: str,
    capabilities: HostCapabilities,
) -> tuple[HostPrerequisiteEntry, bool]:
    """Check observed NVIDIA driver versions against *floor*."""
    try:
        floor_version = Version(floor)
    except InvalidVersion:
        return (
            HostPrerequisiteEntry(
                entry=entry,
                requirement=requirement,
                status=HostPrerequisiteStatus.NOT_OBSERVED,
            ),
            False,
        )

    observed: list[tuple[str, Version]] = []
    for gpu in capabilities.nvidia_gpus:
        if gpu.driver_status is not CapabilityStatus.AVAILABLE:
            continue
        raw = gpu.driver_version
        if not raw:
            continue
        try:
            observed.append((raw, Version(raw)))
        except InvalidVersion:
            continue

    if not observed:
        return (
            HostPrerequisiteEntry(
                entry=entry,
                requirement=requirement,
                status=HostPrerequisiteStatus.NOT_OBSERVED,
            ),
            False,
        )

    below = sorted(
        (raw, version) for raw, version in observed if version < floor_version
    )
    if below:
        return (
            HostPrerequisiteEntry(
                entry=entry,
                requirement=requirement,
                status=HostPrerequisiteStatus.BELOW_MINIMUM,
                observed=below[0][0],
            ),
            True,
        )
    _, best_version = max(observed, key=lambda pair: pair[1])
    best_raw = next(
        raw for raw, version in observed if version == best_version
    )
    return (
        HostPrerequisiteEntry(
            entry=entry,
            requirement=requirement,
            status=HostPrerequisiteStatus.OK,
            observed=best_raw,
        ),
        False,
    )


def _exact_pin(specifier: str | None) -> Version | None:
    """Return the exact version of a ``==x`` specifier, else ``None``.

    Only a specifier set consisting of a single ``==`` specifier counts
    as an exact pin.  ``None`` (any version) and ranges never do.
    """
    if specifier is None:
        return None
    spec_set = SpecifierSet(specifier)
    if len(spec_set) == 1:
        spec = next(iter(spec_set))
        if spec.operator == "==":
            return spec.version
    return None


def _merge_lower_bound(
    current: tuple[Version, bool] | None,
    candidate: tuple[Version, bool],
) -> tuple[Version, bool]:
    """Merge lower constraints, keeping the maximum version.

    When two lower constraints name the same version, the bound is
    inclusive only if both are inclusive (``>v`` and ``>=v`` merge to
    an exclusive bound at ``v``).
    """
    if current is None:
        return candidate
    version, inclusive = current
    candidate_version, candidate_inclusive = candidate
    if candidate_version > version:
        return candidate
    if candidate_version == version:
        return (version, inclusive and candidate_inclusive)
    return current


def _merge_upper_bound(
    current: tuple[Version, bool] | None,
    candidate: tuple[Version, bool],
) -> tuple[Version, bool]:
    """Merge upper constraints, keeping the minimum version.

    When two upper constraints name the same version, the bound is
    inclusive only if both are inclusive (``<v`` and ``<=v`` merge to
    an exclusive bound at ``v``).
    """
    if current is None:
        return candidate
    version, inclusive = current
    candidate_version, candidate_inclusive = candidate
    if candidate_version < version:
        return candidate
    if candidate_version == version:
        return (version, inclusive and candidate_inclusive)
    return current


def _simple_bounds(
    specifier: str | None,
) -> tuple[tuple[Version, bool] | None, tuple[Version, bool] | None]:
    """Reduce a specifier to ``(lower, upper)`` simple bound constraints.

    Only the single-bound comparisons ``>=``, ``>``, ``<=``, ``<`` and
    ``==`` (as the exact interval ``[v, v]``) participate.  ``!=``,
    ``~=``, ``===`` and wildcard ``==x.*`` forms are ignored: they fall
    through to the variant-level fail-closed check during planning.
    Each bound is ``(version, inclusive)``; ``None`` means "no
    constraint on that side".
    """
    if specifier is None:
        return None, None
    lower: tuple[Version, bool] | None = None
    upper: tuple[Version, bool] | None = None
    for spec in SpecifierSet(specifier):
        operator = spec.operator
        if operator in ("!=", "~=", "==="):
            continue
        if operator == "==" and "*" in str(spec.version):
            continue  # wildcard form — not a simple bound
        if operator == ">=":
            lower = _merge_lower_bound(lower, (spec.version, True))
        elif operator == ">":
            lower = _merge_lower_bound(lower, (spec.version, False))
        elif operator == "<=":
            upper = _merge_upper_bound(upper, (spec.version, True))
        elif operator == "<":
            upper = _merge_upper_bound(upper, (spec.version, False))
        elif operator == "==":
            lower = _merge_lower_bound(lower, (spec.version, True))
            upper = _merge_upper_bound(upper, (spec.version, True))
    return lower, upper


def _disjoint_simple_ranges(
    specifier_a: str | None,
    specifier_b: str | None,
) -> bool:
    """Return True when two specifiers are obviously disjoint ranges.

    Conservative by design: only obvious disjointness of simple
    single-bound constraints counts.  Anything subtler (ignored
    operators, wildcards, prerelease corners) falls through to the
    variant-level fail-closed check during planning.
    """
    lower: tuple[Version, bool] | None = None
    upper: tuple[Version, bool] | None = None
    for specifier in (specifier_a, specifier_b):
        spec_lower, spec_upper = _simple_bounds(specifier)
        if spec_lower is not None:
            lower = _merge_lower_bound(lower, spec_lower)
        if spec_upper is not None:
            upper = _merge_upper_bound(upper, spec_upper)
    if lower is None or upper is None:
        return False
    lower_version, lower_inclusive = lower
    upper_version, upper_inclusive = upper
    if lower_version > upper_version:
        return True
    if lower_version == upper_version:
        return not (lower_inclusive and upper_inclusive)
    return False


def _find_conflicts(
    requirements_map: Mapping[str, ProductAccelerationRequirements],
    product_ids: list[str],
) -> tuple[list[str], set[str]]:
    """Find cross-product conflicts deterministically.

    Products are visited in sorted-id order; requirements within a
    product keep their declaration order.  Two products are in conflict
    when:

    * they require the same distribution with two different exact pins
      (``==x`` vs ``==y``, ``x != y``);
    * one pins ``==x`` and the other declares a specifier set that does
      not contain ``x``;
    * a requirement distribution of one product appears in the other
      product's incompatibilities;
    * neither requirement is an exact pin and their specifiers are
      obviously disjoint simple ranges (``>=``/``>``/``<=``/``<``/``==``
      bounds; ``!=``, ``~=``, ``===`` and wildcard forms are ignored
      here — they fall through to the variant-level fail-closed check
      during planning).
    """
    conflicts: list[str] = []
    involved: set[str] = set()

    for a_index, product_a in enumerate(product_ids):
        reqs_a = requirements_map[product_a]
        for product_b in product_ids[a_index + 1:]:
            reqs_b = requirements_map[product_b]

            for req_a in reqs_a.requirements:
                for req_b in reqs_b.requirements:
                    if req_a.distribution != req_b.distribution:
                        continue
                    pin_a = _exact_pin(req_a.specifier)
                    pin_b = _exact_pin(req_b.specifier)
                    if pin_a is not None and pin_b is not None:
                        if pin_a != pin_b:
                            conflicts.append(
                                f"distribution {req_a.distribution!r} is pinned "
                                f"to conflicting versions by {product_a!r} "
                                f"(=={pin_a}) and {product_b!r} (=={pin_b})"
                            )
                            involved.update((product_a, product_b))
                    elif pin_a is not None and req_b.specifier is not None:
                        if not SpecifierSet(req_b.specifier).contains(pin_a):
                            conflicts.append(
                                f"distribution {req_a.distribution!r} pinned to "
                                f"=={pin_a} by {product_a!r} is not satisfied "
                                f"by {product_b!r}'s requirement "
                                f"{req_b.specifier!r}"
                            )
                            involved.update((product_a, product_b))
                    elif pin_b is not None and req_a.specifier is not None:
                        if not SpecifierSet(req_a.specifier).contains(pin_b):
                            conflicts.append(
                                f"distribution {req_b.distribution!r} pinned to "
                                f"=={pin_b} by {product_b!r} is not satisfied "
                                f"by {product_a!r}'s requirement "
                                f"{req_a.specifier!r}"
                            )
                            involved.update((product_a, product_b))
                    elif (
                        pin_a is None
                        and pin_b is None
                        and req_a.specifier is not None
                        and req_b.specifier is not None
                        and _disjoint_simple_ranges(
                            req_a.specifier, req_b.specifier
                        )
                    ):
                        conflicts.append(
                            f"distribution {req_a.distribution!r} required by "
                            f"{product_a!r} ({req_a.specifier!r}) and "
                            f"{product_b!r} ({req_b.specifier!r}) has "
                            "disjoint version ranges"
                        )
                        involved.update((product_a, product_b))

            incompat_b = {inc.distribution for inc in reqs_b.incompatibilities}
            for req_a in reqs_a.requirements:
                if req_a.distribution in incompat_b:
                    conflicts.append(
                        f"distribution {req_a.distribution!r} required by "
                        f"{product_a!r} is listed as incompatible by "
                        f"{product_b!r}"
                    )
                    involved.update((product_a, product_b))

            incompat_a = {inc.distribution for inc in reqs_a.incompatibilities}
            for req_b in reqs_b.requirements:
                if req_b.distribution in incompat_a:
                    conflicts.append(
                        f"distribution {req_b.distribution!r} required by "
                        f"{product_b!r} is listed as incompatible by "
                        f"{product_a!r}"
                    )
                    involved.update((product_a, product_b))

    return conflicts, involved
