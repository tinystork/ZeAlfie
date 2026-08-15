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

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from zealfie.acceleration.models import (
    HardwareCompatibility,
    HardwareCompatibilityReasonCode,
    HardwareCompatibilityStatus,
    KNOWN_BACKENDS,
    ProductAccelerationRequirements,
)
from zealfie.host.models import (
    AccelerationRecommendation,
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
