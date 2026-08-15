"""Tests for M1-2H hardware acceleration compatibility evaluation.

Covers the pure, deterministic, fail-closed evaluator
``evaluate_acceleration_compatibility`` using synthetic
``HostCapabilities`` and ``AccelerationRecommendation`` built directly —
never real hardware.

Synthetic distribution names (``accelerated-lib``, ``fake-cuda``) are
used everywhere — ZeAlfie never selects a concrete accelerated
framework.
"""

from __future__ import annotations

import pytest

from zealfie.acceleration.compatibility import (
    evaluate_acceleration_compatibility,
)
from zealfie.acceleration.models import (
    AcceleratedRequirement,
    AccelerationIncompatibility,
    HardwareCompatibilityReasonCode,
    HardwareCompatibilityStatus,
    ProductAccelerationRequirements,
)
from zealfie.host.models import (
    AccelerationRecommendation,
    CapabilityStatus,
    HostCapabilities,
    HostReasonCode,
    RecommendationStatus,
)

# ===========================================================================
# Synthetic fixtures — built directly, never real hardware
# ===========================================================================


def make_capabilities(partial: bool = False) -> HostCapabilities:
    """A confident, complete host observation (no GPUs needed)."""
    return HostCapabilities(
        os_name="linux",
        cpu_arch="x86_64",
        platform_status=CapabilityStatus.AVAILABLE,
        platform_reason_code=HostReasonCode.OS_DETECTED,
        platform_reason="os detected",
        gpus=(),
        partial=partial,
    )


def make_recommendation(
    status: RecommendationStatus = RecommendationStatus.OFFER_SETUP,
    reason: str = "supported accelerator detected; setup offered",
) -> AccelerationRecommendation:
    """A synthetic interpretation for the requested status."""
    reason_code = {
        RecommendationStatus.OFFER_SETUP: HostReasonCode.ACCELERATION_OFFER_SETUP,
        RecommendationStatus.ALREADY_READY: HostReasonCode.ACCELERATION_ALREADY_READY,
        RecommendationStatus.NOT_APPLICABLE: HostReasonCode.ACCELERATION_NOT_APPLICABLE,
        RecommendationStatus.BLOCKED: HostReasonCode.ACCELERATION_BLOCKED,
        RecommendationStatus.UNKNOWN: HostReasonCode.ACCELERATION_UNKNOWN,
    }[status]
    return AccelerationRecommendation(
        status=status,
        backend="NVIDIA_CUDA",
        reason_code=reason_code,
        reason=reason,
    )


def make_product(
    product_id: str,
    backend: str = "NVIDIA_CUDA",
    requirements: tuple[AcceleratedRequirement, ...] = (),
    incompatibilities: tuple[AccelerationIncompatibility, ...] = (),
) -> ProductAccelerationRequirements:
    return ProductAccelerationRequirements(
        product_id=product_id,
        backend=backend,
        requirements=requirements,
        incompatibilities=incompatibilities,
    )


def unsupported_backend_product(
    product_id: str, backend: str = "AMD_ROCM"
) -> ProductAccelerationRequirements:
    """Build a ProductAccelerationRequirements carrying an unsupported
    backend.

    The model refuses such declarations by design (fail-closed at
    construction), so the evaluator's defensive rule is exercised with
    a manually assembled instance — exactly what a corrupted or
    hand-rolled requirements map could contain.
    """
    obj = object.__new__(ProductAccelerationRequirements)
    object.__setattr__(obj, "product_id", product_id)
    object.__setattr__(obj, "backend", backend)
    object.__setattr__(obj, "optional", True)
    object.__setattr__(obj, "requirements", ())
    object.__setattr__(obj, "incompatibilities", ())
    object.__setattr__(obj, "source", "test@0")
    return obj


def requirement(distribution: str, specifier: str | None = None):
    return AcceleratedRequirement(distribution=distribution, specifier=specifier)


def evaluate(products, capabilities=None, recommendation=None):
    return evaluate_acceleration_compatibility(
        requirements_map={p.product_id: p for p in products},
        capabilities=capabilities or make_capabilities(),
        recommendation=recommendation or make_recommendation(),
    )


# ===========================================================================
# Host-side verdicts
# ===========================================================================


def test_supported_offer_setup():
    """OFFER_SETUP + no conflicts -> SUPPORTED."""
    result = evaluate(
        [make_product("prod-a", requirements=(requirement("accelerated-lib", ">=1.0"),))]
    )
    assert result.status is HardwareCompatibilityStatus.SUPPORTED
    assert result.reason_code == HardwareCompatibilityReasonCode.COMPATIBLE.value
    assert result.products_concerned == ()
    assert result.conflicts == ()


def test_supported_already_ready():
    """ALREADY_READY + no conflicts -> SUPPORTED."""
    result = evaluate(
        [make_product("prod-a", requirements=(requirement("accelerated-lib"),))],
        recommendation=make_recommendation(RecommendationStatus.ALREADY_READY),
    )
    assert result.status is HardwareCompatibilityStatus.SUPPORTED


def test_blocked_driver():
    """A BLOCKED recommendation blocks with the recommendation reason."""
    result = evaluate(
        [make_product("prod-a", requirements=(requirement("accelerated-lib"),))],
        recommendation=make_recommendation(
            RecommendationStatus.BLOCKED, reason="nvidia driver too old"
        ),
    )
    assert result.status is HardwareCompatibilityStatus.BLOCKED
    assert result.reason_code == HardwareCompatibilityReasonCode.ACCELERATION_BLOCKED.value
    assert result.reason == "nvidia driver too old"
    assert result.products_concerned == ("prod-a",)


def test_unknown_partial_capabilities():
    """Partial host capabilities -> UNKNOWN (fail-closed, never assumed)."""
    result = evaluate(
        [make_product("prod-a", requirements=(requirement("accelerated-lib"),))],
        capabilities=make_capabilities(partial=True),
    )
    assert result.status is HardwareCompatibilityStatus.UNKNOWN
    assert (
        result.reason_code
        == HardwareCompatibilityReasonCode.HOST_CAPABILITIES_PARTIAL.value
    )
    assert result.products_concerned == ("prod-a",)


def test_blocked_not_applicable():
    """NOT_APPLICABLE recommendation -> BLOCKED with the fixed reason."""
    result = evaluate(
        [make_product("prod-a", requirements=(requirement("accelerated-lib"),))],
        recommendation=make_recommendation(RecommendationStatus.NOT_APPLICABLE),
    )
    assert result.status is HardwareCompatibilityStatus.BLOCKED
    assert (
        result.reason_code
        == HardwareCompatibilityReasonCode.ACCELERATION_NOT_APPLICABLE.value
    )
    assert result.reason == "no supported accelerator hardware detected"


def test_unknown_recommendation():
    """UNKNOWN recommendation -> UNKNOWN with the recommendation reason."""
    result = evaluate(
        [make_product("prod-a", requirements=(requirement("accelerated-lib"),))],
        recommendation=make_recommendation(
            RecommendationStatus.UNKNOWN, reason="driver probe failed"
        ),
    )
    assert result.status is HardwareCompatibilityStatus.UNKNOWN
    assert result.reason_code == HardwareCompatibilityReasonCode.ACCELERATION_UNKNOWN.value
    assert result.reason == "driver probe failed"


def test_blocked_unknown_backend():
    """A product declaring an unknown backend -> BLOCKED."""
    result = evaluate(
        [
            make_product("prod-a", requirements=(requirement("accelerated-lib"),)),
            unsupported_backend_product("prod-b", backend="AMD_ROCM"),
        ]
    )
    assert result.status is HardwareCompatibilityStatus.BLOCKED
    assert (
        result.reason_code
        == HardwareCompatibilityReasonCode.UNSUPPORTED_ACCELERATION_BACKEND.value
    )
    assert result.reason == "unsupported acceleration backend AMD_ROCM"
    assert result.products_concerned == ("prod-b",)


def test_empty_map_blocked():
    """An empty requirements map -> BLOCKED, never silently supported."""
    result = evaluate_acceleration_compatibility(
        requirements_map={},
        capabilities=make_capabilities(),
        recommendation=make_recommendation(),
    )
    assert result.status is HardwareCompatibilityStatus.BLOCKED
    assert (
        result.reason_code
        == HardwareCompatibilityReasonCode.NO_ACCELERATION_REQUIREMENTS.value
    )
    assert (
        result.reason
        == "no product declares accelerated requirements; nothing to evaluate"
    )
    assert result.products_concerned == ()
    assert result.conflicts == ()


# ===========================================================================
# Cross-product conflict checks
# ===========================================================================


def test_blocked_exact_pin_conflict():
    """Two exact pins on the same distribution that differ -> BLOCKED."""
    result = evaluate(
        [
            make_product("prod-a", requirements=(requirement("accelerated-lib", "==1.0.0"),)),
            make_product("prod-b", requirements=(requirement("accelerated-lib", "==2.0.0"),)),
        ]
    )
    assert result.status is HardwareCompatibilityStatus.BLOCKED
    assert result.reason_code == HardwareCompatibilityReasonCode.REQUIREMENT_CONFLICT.value
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert "accelerated-lib" in conflict
    assert "prod-a" in conflict
    assert "prod-b" in conflict
    assert result.products_concerned == ("prod-a", "prod-b")


def test_blocked_pin_vs_excluding_range():
    """An exact pin not contained in another product's range -> BLOCKED."""
    result = evaluate(
        [
            make_product("prod-a", requirements=(requirement("accelerated-lib", "==1.0.0"),)),
            make_product("prod-b", requirements=(requirement("accelerated-lib", ">=2.0"),)),
        ]
    )
    assert result.status is HardwareCompatibilityStatus.BLOCKED
    assert len(result.conflicts) == 1
    assert "accelerated-lib" in result.conflicts[0]
    assert result.products_concerned == ("prod-a", "prod-b")


def test_supported_identical_pins():
    """Two identical exact pins are not a conflict."""
    result = evaluate(
        [
            make_product("prod-a", requirements=(requirement("accelerated-lib", "==1.0.0"),)),
            make_product("prod-b", requirements=(requirement("accelerated-lib", "==1.0.0"),)),
        ]
    )
    assert result.status is HardwareCompatibilityStatus.SUPPORTED
    assert result.conflicts == ()


def test_supported_pin_inside_range():
    """A pin contained in another product's range is not a conflict."""
    result = evaluate(
        [
            make_product("prod-a", requirements=(requirement("accelerated-lib", "==1.5.0"),)),
            make_product("prod-b", requirements=(requirement("accelerated-lib", ">=1.0,<2"),)),
        ]
    )
    assert result.status is HardwareCompatibilityStatus.SUPPORTED


def test_supported_pin_and_any():
    """A pin plus an unconstrained requirement is not a conflict."""
    result = evaluate(
        [
            make_product("prod-a", requirements=(requirement("accelerated-lib", "==1.0.0"),)),
            make_product("prod-b", requirements=(requirement("accelerated-lib"),)),
        ]
    )
    assert result.status is HardwareCompatibilityStatus.SUPPORTED


def test_supported_disjoint_distributions():
    """Different distributions across products are not conflicts."""
    result = evaluate(
        [
            make_product("prod-a", requirements=(requirement("accelerated-lib", "==1.0.0"),)),
            make_product("prod-b", requirements=(requirement("kernel-common", "==9.0"),)),
        ]
    )
    assert result.status is HardwareCompatibilityStatus.SUPPORTED


def test_blocked_incompatibility_cross_product():
    """A's requirement listed in B's incompatibilities -> BLOCKED."""
    result = evaluate(
        [
            make_product("prod-a", requirements=(requirement("accelerated-lib"),)),
            make_product(
                "prod-b",
                incompatibilities=(
                    AccelerationIncompatibility(
                        distribution="accelerated-lib", reason="kernel clash"
                    ),
                ),
            ),
        ]
    )
    assert result.status is HardwareCompatibilityStatus.BLOCKED
    assert result.reason_code == HardwareCompatibilityReasonCode.REQUIREMENT_CONFLICT.value
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert "accelerated-lib" in conflict
    assert "prod-a" in conflict
    assert "prod-b" in conflict
    assert result.products_concerned == ("prod-a", "prod-b")


def test_optional_requirement_still_checked():
    """optional=True does not exempt a product from conflict checks
    (rules run over ALL declared requirements)."""
    a = make_product("prod-a", requirements=(requirement("accelerated-lib", "==1.0.0"),))
    b = ProductAccelerationRequirements(
        product_id="prod-b",
        backend="NVIDIA_CUDA",
        optional=True,
        requirements=(requirement("accelerated-lib", "==2.0.0"),),
    )
    result = evaluate_acceleration_compatibility(
        requirements_map={"prod-a": a, "prod-b": b},
        capabilities=make_capabilities(),
        recommendation=make_recommendation(),
    )
    assert result.status is HardwareCompatibilityStatus.BLOCKED


# ===========================================================================
# Range-vs-range structural conflict detection (M1-2H corrective)
# ===========================================================================


def test_blocked_disjoint_simple_ranges():
    """Two obviously disjoint simple ranges -> BLOCKED; the conflict
    names the distribution, both products and both specifiers."""
    result = evaluate(
        [
            make_product(
                "prod-a",
                requirements=(requirement("accelerated-lib", ">=1.0,<2.0"),),
            ),
            make_product(
                "prod-b",
                requirements=(requirement("accelerated-lib", ">=2.0,<3.0"),),
            ),
        ]
    )
    assert result.status is HardwareCompatibilityStatus.BLOCKED
    assert (
        result.reason_code
        == HardwareCompatibilityReasonCode.REQUIREMENT_CONFLICT.value
    )
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert "accelerated-lib" in conflict
    assert "prod-a" in conflict
    assert "prod-b" in conflict
    assert ">=1.0,<2.0" in conflict
    assert ">=2.0,<3.0" in conflict
    assert result.products_concerned == ("prod-a", "prod-b")


def test_supported_equal_bound_inclusive_ranges():
    """``>=2.0`` vs ``<=2.0`` share version 2.0 with inclusive bounds on
    both sides -> SUPPORTED."""
    result = evaluate(
        [
            make_product(
                "prod-a", requirements=(requirement("accelerated-lib", ">=2.0"),)
            ),
            make_product(
                "prod-b", requirements=(requirement("accelerated-lib", "<=2.0"),)
            ),
        ]
    )
    assert result.status is HardwareCompatibilityStatus.SUPPORTED
    assert result.conflicts == ()


def test_blocked_equal_bound_exclusive_ranges():
    """``>2.0`` vs ``<=2.0`` meet at 2.0 with an exclusive side ->
    BLOCKED."""
    result = evaluate(
        [
            make_product(
                "prod-a", requirements=(requirement("accelerated-lib", ">2.0"),)
            ),
            make_product(
                "prod-b", requirements=(requirement("accelerated-lib", "<=2.0"),)
            ),
        ]
    )
    assert result.status is HardwareCompatibilityStatus.BLOCKED
    assert len(result.conflicts) == 1


def test_supported_overlapping_ranges():
    """Overlapping ranges -> SUPPORTED."""
    result = evaluate(
        [
            make_product(
                "prod-a",
                requirements=(requirement("accelerated-lib", ">=1.0,<3.0"),),
            ),
            make_product(
                "prod-b",
                requirements=(requirement("accelerated-lib", ">=2.0,<4.0"),),
            ),
        ]
    )
    assert result.status is HardwareCompatibilityStatus.SUPPORTED
    assert result.conflicts == ()


def test_blocked_upper_bound_vs_lower_bound():
    """``<2.0`` vs ``>=2.0`` -> BLOCKED."""
    result = evaluate(
        [
            make_product(
                "prod-a", requirements=(requirement("accelerated-lib", "<2.0"),)
            ),
            make_product(
                "prod-b", requirements=(requirement("accelerated-lib", ">=2.0"),)
            ),
        ]
    )
    assert result.status is HardwareCompatibilityStatus.BLOCKED
    assert len(result.conflicts) == 1
    assert "disjoint" in result.conflicts[0]


def test_no_structural_conflict_for_ignored_operators():
    """A pair involving ``!=`` produces no structural range conflict
    (documented conservative): unsatisfiability of the merged specifier
    is still blocked at variant selection during planning."""
    result = evaluate(
        [
            make_product(
                "prod-a", requirements=(requirement("accelerated-lib", "!=1.0"),)
            ),
            make_product(
                "prod-b", requirements=(requirement("accelerated-lib", ">=2.0"),)
            ),
        ]
    )
    assert result.status is HardwareCompatibilityStatus.SUPPORTED
    assert result.conflicts == ()


def test_disjoint_simple_ranges_ignores_wildcard_forms():
    """Wildcard ``==1.0.*`` forms contribute no simple bounds to the
    structural check (conservative); genuine unsatisfiability falls
    through to the variant-level fail-closed check at planning."""
    from zealfie.acceleration.compatibility import _disjoint_simple_ranges

    assert _disjoint_simple_ranges("==1.0.*", ">=1.1") is False


def test_range_conflict_detection_is_order_independent():
    """Range conflicts are found regardless of mapping insertion order."""
    products = [
        make_product(
            "prod-a", requirements=(requirement("accelerated-lib", ">=1.0,<2.0"),)
        ),
        make_product(
            "prod-b", requirements=(requirement("accelerated-lib", ">=2.0,<3.0"),)
        ),
    ]
    forward = evaluate_acceleration_compatibility(
        requirements_map={p.product_id: p for p in products},
        capabilities=make_capabilities(),
        recommendation=make_recommendation(),
    )
    backward = evaluate_acceleration_compatibility(
        requirements_map={p.product_id: p for p in reversed(products)},
        capabilities=make_capabilities(),
        recommendation=make_recommendation(),
    )
    assert forward == backward
    assert forward.status is HardwareCompatibilityStatus.BLOCKED


# ===========================================================================
# Determinism and purity
# ===========================================================================


def test_two_identical_calls_return_equal_results():
    """Two identical evaluations return equal (deterministic) results."""
    products = [
        make_product("prod-a", requirements=(requirement("accelerated-lib", ">=1.0"),)),
        make_product("prod-b", requirements=(requirement("kernel-common", "==2.0"),)),
    ]
    first = evaluate(products)
    second = evaluate(products)
    assert first == second
    assert first.status == second.status
    assert first.reason == second.reason
    assert first.products_concerned == second.products_concerned
    assert first.conflicts == second.conflicts


def test_conflict_detection_is_order_independent():
    """Conflicts are found regardless of mapping insertion order."""
    products = [
        make_product("prod-a", requirements=(requirement("accelerated-lib", "==1.0.0"),)),
        make_product("prod-b", requirements=(requirement("accelerated-lib", "==2.0.0"),)),
    ]
    forward = evaluate_acceleration_compatibility(
        requirements_map={p.product_id: p for p in products},
        capabilities=make_capabilities(),
        recommendation=make_recommendation(),
    )
    backward = evaluate_acceleration_compatibility(
        requirements_map={p.product_id: p for p in reversed(products)},
        capabilities=make_capabilities(),
        recommendation=make_recommendation(),
    )
    assert forward == backward
    assert forward.status is HardwareCompatibilityStatus.BLOCKED
    assert forward.products_concerned == ("prod-a", "prod-b")


def test_evaluation_does_not_mutate_inputs():
    """The evaluator is pure: its inputs are untouched."""
    products = [
        make_product("prod-a", requirements=(requirement("accelerated-lib", "==1.0.0"),)),
        make_product("prod-b", requirements=(requirement("accelerated-lib", "==2.0.0"),)),
    ]
    requirements_map = {p.product_id: p for p in products}
    before = dict(requirements_map)
    capabilities = make_capabilities()
    recommendation = make_recommendation()
    evaluate_acceleration_compatibility(
        requirements_map=requirements_map,
        capabilities=capabilities,
        recommendation=recommendation,
    )
    assert list(requirements_map) == list(before)
    assert capabilities.partial is False
    assert recommendation.status is RecommendationStatus.OFFER_SETUP
