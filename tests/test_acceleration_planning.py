"""Tests for M1-2H pure accelerated deployment planning.

Covers :func:`build_accelerated_deployment_plan` and its plan models
using synthetic ``HostCapabilities`` / ``AccelerationRecommendation`` /
``RuntimeStatus`` built directly (never real hardware) and synthetic
product catalogs parsed from TOML text with ``[products.acceleration]``
tables.

Synthetic distribution names (``accelerated-lib``, ``fake-cuda``) are
used everywhere — ZeAlfie never selects a concrete accelerated
framework.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zealfie.acceleration import (
    AcceleratedDeploymentPlan,
    AcceleratedPlanStatus,
    AcceleratedVariant,
    AcceleratedVariantCatalog,
    HardwareCompatibility,
    HardwareCompatibilityReasonCode,
    HardwareCompatibilityStatus,
    HostPrerequisiteEntry,
    HostPrerequisitesStatus,
    HostPrerequisiteStatus,
    PlannedAcceleratedDependency,
    PlannedKeepProduct,
    VariantStatus,
    build_accelerated_deployment_plan,
)
from zealfie.host.models import (
    AccelerationRecommendation,
    CapabilityStatus,
    GpuInfo,
    GpuKind,
    HostCapabilities,
    HostReasonCode,
    RecommendationStatus,
)
from zealfie.products.catalog import ProductCatalog, load_catalog_from_text
from zealfie.runtime.model import RuntimeState, RuntimeStatus

PLATFORM_TAG = "linux_x86_64"
SHA_A = "a" * 40
SHA_B = "b" * 40
WHEEL_A = "f" * 64
WHEEL_B = "e" * 64


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


def make_nvidia_capabilities(driver_version: str | None) -> HostCapabilities:
    """A confident host observation with one NVIDIA GPU (synthetic)."""
    return HostCapabilities(
        os_name="linux",
        cpu_arch="x86_64",
        platform_status=CapabilityStatus.AVAILABLE,
        platform_reason_code=HostReasonCode.OS_DETECTED,
        platform_reason="os detected",
        gpus=(
            GpuInfo(
                vendor="NVIDIA",
                model="GeForce MX150",
                kind=GpuKind.DISCRETE,
                hardware_present=True,
                driver_status=CapabilityStatus.AVAILABLE,
                driver_version=driver_version,
                driver_reason_code=None,
                driver_reason=None,
                nvidia_smi_available=True,
                cuda_driver_present=True,
            ),
        ),
        partial=False,
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


def make_runtime_status(
    state: RuntimeState = RuntimeState.READY,
    active_slot_id: str | None = "slot-active",
    previous_slot_id: str | None = "slot-previous",
) -> RuntimeStatus:
    return RuntimeStatus(
        state=state,
        runtime_root=Path("/fake/runtime"),
        active_slot_id=active_slot_id,
        previous_slot_id=previous_slot_id,
    )


def keep_product(
    product_id: str,
    version: str = "1.0.0",
    commit_sha: str | None = SHA_A,
    wheel_sha256: str | None = WHEEL_A,
) -> PlannedKeepProduct:
    return PlannedKeepProduct(
        product_id=product_id,
        version=version,
        commit_sha=commit_sha,
        wheel_sha256=wheel_sha256,
    )


def variant(
    distribution: str,
    version: str = "1.0.0",
    backend: str = "NVIDIA_CUDA",
    platform: str | None = None,
) -> AcceleratedVariant:
    return AcceleratedVariant(
        distribution=distribution,
        version=version,
        backend=backend,
        platform=platform,
    )


def _requirement(
    distribution: str,
    specifier: str | None = None,
    extras: tuple[str, ...] = (),
) -> str:
    """TOML for one [[products.acceleration.requirements]] entry."""
    lines = [
        "[[products.acceleration.requirements]]",
        f'distribution = "{distribution}"',
    ]
    if specifier is not None:
        lines.append(f'specifier = "{specifier}"')
    if extras:
        quoted = ", ".join(f'"{extra}"' for extra in extras)
        lines.append(f"extras = [{quoted}]")
    return "\n".join(lines)


def _acc_block(*requirements: str) -> str:
    """TOML body for a [products.acceleration] table."""
    return "\n".join(('backend = "NVIDIA_CUDA"', *requirements))


def make_catalog(*products: tuple[str, str | None]) -> ProductCatalog:
    """Build a catalog from ``(product_id, acceleration_block)`` pairs."""
    parts = ["schema_version = 1", ""]
    for product_id, acc_block in products:
        parts.append("[[products]]")
        parts.append(f'id = "{product_id}"')
        parts.append(f'display_name = "{product_id}"')
        parts.append(f'distribution_name = "{product_id}"')
        parts.append("[products.launch]")
        parts.append(
            f'entry_points = [{{group = "gui_scripts", name = "{product_id}"}}]'
        )
        if acc_block is not None:
            parts.append("")
            parts.append("[products.acceleration]")
            parts.append(acc_block)
        parts.append("")
    return load_catalog_from_text("\n".join(parts))


def build(
    *products: tuple[str, str | None],
    capabilities: HostCapabilities | None = None,
    recommendation: AccelerationRecommendation | None = None,
    runtime_status: RuntimeStatus | None = None,
    variant_catalog: AcceleratedVariantCatalog | None = None,
    keep_products: dict[str, PlannedKeepProduct] | None = None,
    platform_tag: str = PLATFORM_TAG,
) -> AcceleratedDeploymentPlan:
    return build_accelerated_deployment_plan(
        catalog=make_catalog(*products),
        capabilities=capabilities if capabilities is not None else make_capabilities(),
        recommendation=(
            recommendation
            if recommendation is not None
            else make_recommendation()
        ),
        runtime_status=(
            runtime_status if runtime_status is not None else make_runtime_status()
        ),
        variant_catalog=(
            variant_catalog
            if variant_catalog is not None
            else AcceleratedVariantCatalog(variants=())
        ),
        keep_products=keep_products if keep_products is not None else {},
        platform_tag=platform_tag,
    )


# ===========================================================================
# Host-side verdicts
# ===========================================================================


def test_cpu_only_blocked_not_applicable_keeps_preserved():
    """NOT_APPLICABLE recommendation with declared requirements -> BLOCKED,
    KEEP products preserved, no added requirements."""
    kp = keep_product("zebench", version="2.0.0", commit_sha=SHA_B, wheel_sha256=WHEEL_B)
    plan = build(
        ("zebench", _acc_block(_requirement("accelerated-lib"))),
        recommendation=make_recommendation(RecommendationStatus.NOT_APPLICABLE),
        keep_products={"zebench": kp},
    )
    assert plan.status is AcceleratedPlanStatus.BLOCKED
    assert plan.blocked is True
    assert plan.hardware.status is HardwareCompatibilityStatus.BLOCKED
    assert plan.blocked_reason == "no supported accelerator hardware detected"
    assert plan.backend is None
    assert plan.added_requirements == ()
    assert plan.products_concerned == ("zebench",)
    assert plan.keep_products == (kp,)
    assert plan.target_runtime == "no new runtime required"
    assert plan.closure_impact == ()


def test_blocked_recommendation_driver_reason():
    """A BLOCKED recommendation (no driver) -> BLOCKED with the driver
    reason passthrough."""
    plan = build(
        ("zebench", _acc_block(_requirement("accelerated-lib"))),
        recommendation=make_recommendation(
            RecommendationStatus.BLOCKED, reason="nvidia driver too old"
        ),
    )
    assert plan.status is AcceleratedPlanStatus.BLOCKED
    assert plan.blocked is True
    assert plan.blocked_reason == "nvidia driver too old"
    assert plan.hardware.reason_code == HardwareCompatibilityReasonCode.ACCELERATION_BLOCKED.value
    assert plan.added_requirements == ()
    assert plan.target_runtime == "no new runtime required"


def test_partial_capabilities_unknown_fail_closed():
    """Partial host capabilities -> UNKNOWN, fail-closed (blocked True)."""
    plan = build(
        ("zebench", _acc_block(_requirement("accelerated-lib"))),
        capabilities=make_capabilities(partial=True),
    )
    assert plan.status is AcceleratedPlanStatus.UNKNOWN
    assert plan.blocked is True
    assert plan.hardware.status is HardwareCompatibilityStatus.UNKNOWN
    assert plan.hardware.reason_code == HardwareCompatibilityReasonCode.HOST_CAPABILITIES_PARTIAL.value
    assert plan.added_requirements == ()
    assert plan.target_runtime == "no new runtime required"


def test_unknown_recommendation_unknown_fail_closed():
    """An UNKNOWN recommendation -> UNKNOWN, fail-closed (blocked True)."""
    plan = build(
        ("zebench", _acc_block(_requirement("accelerated-lib"))),
        recommendation=make_recommendation(
            RecommendationStatus.UNKNOWN, reason="driver probe failed"
        ),
    )
    assert plan.status is AcceleratedPlanStatus.UNKNOWN
    assert plan.blocked is True
    assert plan.blocked_reason == "driver probe failed"
    assert plan.hardware.status is HardwareCompatibilityStatus.UNKNOWN
    assert plan.added_requirements == ()


# ===========================================================================
# Successful planning
# ===========================================================================


def test_plan_ready_with_matching_variant():
    """OFFER_SETUP + matching variant -> PLAN_READY, all SELECTED, correct
    backend and deterministic target_runtime."""
    expected = variant("accelerated-lib")
    plan = build(
        ("zebench", _acc_block(_requirement("accelerated-lib", ">=1.0"))),
        variant_catalog=AcceleratedVariantCatalog(variants=(expected,)),
    )
    assert plan.status is AcceleratedPlanStatus.PLAN_READY
    assert plan.blocked is False
    assert plan.blocked_reason is None
    assert plan.backend == "NVIDIA_CUDA"
    assert plan.target_runtime == (
        "new shared runtime slot with accelerated NVIDIA_CUDA closure"
    )
    assert len(plan.added_requirements) == 1
    entry = plan.added_requirements[0]
    assert entry.distribution == "accelerated-lib"
    assert entry.specifier == ">=1.0"
    assert entry.extras == ()
    assert entry.declaring_products == ("zebench",)
    assert entry.variant is expected
    assert entry.variant_status is VariantStatus.SELECTED
    assert plan.closure_impact == ("Add accelerated-lib (>=1.0) [variant 1.0.0]",)


def test_plan_ready_closure_impact_with_keeps():
    """PLAN_READY closure impact lists preserved products and added
    requirements deterministically."""
    kp_a = keep_product("prod-a")
    kp_b = keep_product("prod-b", version="3.0.0", commit_sha=SHA_B, wheel_sha256=WHEEL_B)
    plan = build(
        ("zebench", _acc_block(_requirement("accelerated-lib", ">=1.0"))),
        variant_catalog=AcceleratedVariantCatalog(variants=(variant("accelerated-lib"),)),
        keep_products={"prod-b": kp_b, "prod-a": kp_a},
    )
    assert plan.closure_impact == (
        "Preserve 2 installed product(s): prod-a, prod-b",
        "Add accelerated-lib (>=1.0) [variant 1.0.0]",
    )


def test_single_product_concerned_singleton():
    """Exactly one accelerated product -> products_concerned singleton."""
    plan = build(
        ("zebench", _acc_block(_requirement("accelerated-lib"))),
        variant_catalog=AcceleratedVariantCatalog(variants=(variant("accelerated-lib"),)),
    )
    assert plan.products_concerned == ("zebench",)


def test_multiple_keep_products_exact_preservation():
    """KEEP products are preserved byte-identical, sorted by product id —
    even in a PLAN_READY plan."""
    kp_zebra = keep_product("zebra", version="2.0.0", commit_sha=SHA_B, wheel_sha256=WHEEL_B)
    kp_aardvark = keep_product("aardvark", version="1.0.0", commit_sha=SHA_A, wheel_sha256=WHEEL_A)
    plan = build(
        ("zebench", _acc_block(_requirement("accelerated-lib"))),
        variant_catalog=AcceleratedVariantCatalog(variants=(variant("accelerated-lib"),)),
        keep_products={"zebra": kp_zebra, "aardvark": kp_aardvark},
    )
    assert plan.keep_products == (kp_aardvark, kp_zebra)
    assert plan.keep_products[0] is kp_aardvark
    assert plan.keep_products[1] is kp_zebra
    assert plan.keep_products[0].commit_sha == SHA_A
    assert plan.keep_products[0].wheel_sha256 == WHEEL_A
    assert plan.keep_products[1].version == "2.0.0"
    assert plan.keep_products[1].commit_sha == SHA_B
    assert plan.keep_products[1].wheel_sha256 == WHEEL_B


def test_shared_distribution_merged_single_entry():
    """Two products sharing one distribution with compatible specifiers ->
    one merged entry listing both products sorted."""
    plan = build(
        ("prod-a", _acc_block(_requirement("accelerated-lib", ">=1.0"))),
        ("prod-b", _acc_block(_requirement("accelerated-lib", ">=1.0,<3"))),
        variant_catalog=AcceleratedVariantCatalog(variants=(variant("accelerated-lib"),)),
    )
    assert plan.status is AcceleratedPlanStatus.PLAN_READY
    assert len(plan.added_requirements) == 1
    entry = plan.added_requirements[0]
    assert entry.distribution == "accelerated-lib"
    assert entry.specifier == ">=1.0, >=1.0,<3"
    assert entry.declaring_products == ("prod-a", "prod-b")
    assert entry.variant_status is VariantStatus.SELECTED


def test_merged_extras_sorted_union():
    """Extras from several products merge into one sorted canonicalized
    union."""
    plan = build(
        (
            "prod-a",
            _acc_block(_requirement("accelerated-lib", extras=("Gui", "cuda12"))),
        ),
        (
            "prod-b",
            _acc_block(_requirement("accelerated-lib", extras=("cuda12", "fast"))),
        ),
        variant_catalog=AcceleratedVariantCatalog(variants=(variant("accelerated-lib"),)),
    )
    entry = plan.added_requirements[0]
    assert entry.extras == ("cuda12", "fast", "gui")


def test_conflicting_exact_pins_blocked():
    """Two products with conflicting exact pins -> BLOCKED via hardware
    REQUIREMENT_CONFLICT, no added requirements."""
    plan = build(
        ("prod-a", _acc_block(_requirement("accelerated-lib", "==1.0.0"))),
        ("prod-b", _acc_block(_requirement("accelerated-lib", "==2.0.0"))),
    )
    assert plan.status is AcceleratedPlanStatus.BLOCKED
    assert plan.blocked is True
    assert plan.hardware.status is HardwareCompatibilityStatus.BLOCKED
    assert plan.hardware.reason_code == HardwareCompatibilityReasonCode.REQUIREMENT_CONFLICT.value
    assert plan.products_concerned == ("prod-a", "prod-b")
    assert plan.added_requirements == ()


# ===========================================================================
# Variant availability
# ===========================================================================


def test_missing_variant_blocked_no_fallback():
    """A required distribution with no variant -> BLOCKED, NOT_AVAILABLE,
    no partial fallback."""
    plan = build(
        ("zebench", _acc_block(_requirement("accelerated-lib"))),
        ("prod-b", _acc_block(_requirement("kernel-common"))),
        variant_catalog=AcceleratedVariantCatalog(variants=(variant("kernel-common"),)),
    )
    assert plan.status is AcceleratedPlanStatus.BLOCKED
    assert plan.blocked is True
    assert plan.blocked_reason == "no accelerated variant available for: accelerated-lib"
    assert plan.backend == "NVIDIA_CUDA"
    assert plan.target_runtime == "no new runtime required"
    assert len(plan.added_requirements) == 2
    statuses = {
        entry.distribution: entry.variant_status for entry in plan.added_requirements
    }
    assert statuses == {
        "accelerated-lib": VariantStatus.NOT_AVAILABLE,
        "kernel-common": VariantStatus.SELECTED,
    }
    missing_entry = plan.added_requirements[0]
    assert missing_entry.variant is None
    assert missing_entry.variant_status is VariantStatus.NOT_AVAILABLE
    assert plan.closure_impact == ()


def test_platform_mismatch_variant_not_available():
    """A variant tagged for another platform -> NOT_AVAILABLE -> BLOCKED."""
    plan = build(
        ("zebench", _acc_block(_requirement("accelerated-lib"))),
        variant_catalog=AcceleratedVariantCatalog(
            variants=(variant("accelerated-lib", platform="windows_x86_64"),)
        ),
        platform_tag=PLATFORM_TAG,
    )
    assert plan.status is AcceleratedPlanStatus.BLOCKED
    assert plan.blocked_reason == "no accelerated variant available for: accelerated-lib"
    entry = plan.added_requirements[0]
    assert entry.variant_status is VariantStatus.NOT_AVAILABLE
    assert entry.variant is None


def test_platform_independent_variant_matches_any_platform():
    """A platform=None variant matches any platform tag -> PLAN_READY."""
    plan = build(
        ("zebench", _acc_block(_requirement("accelerated-lib"))),
        variant_catalog=AcceleratedVariantCatalog(
            variants=(variant("accelerated-lib", platform=None),)
        ),
        platform_tag="windows_x86_64",
    )
    assert plan.status is AcceleratedPlanStatus.PLAN_READY
    assert plan.added_requirements[0].variant_status is VariantStatus.SELECTED


def test_no_acceleration_requirements_status():
    """A catalog without accelerated requirements -> NO_ACCELERATED_REQUIREMENTS."""
    plan = build(("zebench", None), ("prod-b", None))
    assert plan.status is AcceleratedPlanStatus.NO_ACCELERATED_REQUIREMENTS
    assert plan.blocked is True
    assert plan.backend is None
    assert plan.products_concerned == ()
    assert plan.added_requirements == ()
    assert plan.blocked_reason == (
        "no product declares accelerated requirements; the active CPU "
        "closure is preserved unchanged"
    )
    assert plan.target_runtime == "no new runtime required"
    assert plan.closure_impact == (
        "No accelerated requirements declared — active shared runtime "
        "is preserved as-is.",
    )


# ===========================================================================
# Variant must satisfy the merged specifier (M1-2H corrective)
# ===========================================================================


def test_exact_pin_satisfied_by_variant_plan_ready():
    """A variant exactly matching the merged pin -> PLAN_READY."""
    plan = build(
        ("zebench", _acc_block(_requirement("accelerated-lib", "==1.2.0"))),
        variant_catalog=AcceleratedVariantCatalog(
            variants=(variant("accelerated-lib", version="1.2.0"),)
        ),
    )
    assert plan.status is AcceleratedPlanStatus.PLAN_READY
    assert plan.blocked is False
    entry = plan.added_requirements[0]
    assert entry.variant_status is VariantStatus.SELECTED
    assert entry.variant is not None
    assert entry.variant.version == "1.2.0"


def test_exact_pin_violated_by_variant_blocked():
    """A variant that does not match the merged pin -> BLOCKED with a
    deterministic detail; the entry stays NOT_AVAILABLE."""
    plan = build(
        ("zebench", _acc_block(_requirement("accelerated-lib", "==2.0.0"))),
        variant_catalog=AcceleratedVariantCatalog(
            variants=(variant("accelerated-lib", version="1.2.0"),)
        ),
    )
    assert plan.status is AcceleratedPlanStatus.BLOCKED
    assert plan.blocked is True
    assert plan.blocked_reason == (
        "no accelerated variant available for: accelerated-lib "
        "(declared ==2.0.0 not satisfied by available variant 1.2.0)"
    )
    entry = plan.added_requirements[0]
    assert entry.variant is None
    assert entry.variant_status is VariantStatus.NOT_AVAILABLE
    assert plan.closure_impact == ()


def test_range_violated_by_variant_blocked():
    """A variant outside the merged range -> BLOCKED with a deterministic
    detail naming the specifier and the variant version."""
    plan = build(
        ("zebench", _acc_block(_requirement("accelerated-lib", ">=2.0,<3.0"))),
        variant_catalog=AcceleratedVariantCatalog(
            variants=(variant("accelerated-lib", version="1.2.0"),)
        ),
    )
    assert plan.status is AcceleratedPlanStatus.BLOCKED
    assert plan.blocked_reason == (
        "no accelerated variant available for: accelerated-lib "
        "(declared >=2.0,<3.0 not satisfied by available variant 1.2.0)"
    )
    entry = plan.added_requirements[0]
    assert entry.variant is None
    assert entry.variant_status is VariantStatus.NOT_AVAILABLE


def test_merged_specifier_violated_by_variant_blocked():
    """The *merged* specifier across products is the contract: a variant
    satisfying one product's range but not the other -> BLOCKED (the
    ranges overlap structurally, so only the variant-level check
    catches it)."""
    plan = build(
        ("prod-a", _acc_block(_requirement("accelerated-lib", ">=1.0,<2.0"))),
        ("prod-b", _acc_block(_requirement("accelerated-lib", ">=1.8"))),
        variant_catalog=AcceleratedVariantCatalog(
            variants=(variant("accelerated-lib", version="2.5.0"),)
        ),
    )
    assert plan.status is AcceleratedPlanStatus.BLOCKED
    assert plan.blocked_reason == (
        "no accelerated variant available for: accelerated-lib "
        "(declared >=1.0,<2.0, >=1.8 not satisfied by available "
        "variant 2.5.0)"
    )
    entry = plan.added_requirements[0]
    assert entry.specifier == ">=1.0,<2.0, >=1.8"
    assert entry.variant_status is VariantStatus.NOT_AVAILABLE


def test_prerelease_variant_satisfying_prerelease_bound_plan_ready():
    """A prerelease variant numerically satisfying the specifier is
    accepted because the check allows prereleases (the same check
    without prereleases would reject every prerelease variant)."""
    plan = build(
        ("zebench", _acc_block(_requirement("accelerated-lib", ">=2.0rc1"))),
        variant_catalog=AcceleratedVariantCatalog(
            variants=(variant("accelerated-lib", version="2.0.0rc1"),)
        ),
    )
    assert plan.status is AcceleratedPlanStatus.PLAN_READY
    entry = plan.added_requirements[0]
    assert entry.variant_status is VariantStatus.SELECTED
    assert entry.variant is not None
    assert entry.variant.version == "2.0.0rc1"


def test_prerelease_variant_above_release_bound_plan_ready():
    """A prerelease variant above a plain release bound (``>=2.0``) is
    accepted: ``prereleases=True`` removes the blanket prerelease
    exclusion PEP 440 would otherwise apply."""
    plan = build(
        ("zebench", _acc_block(_requirement("accelerated-lib", ">=2.0"))),
        variant_catalog=AcceleratedVariantCatalog(
            variants=(variant("accelerated-lib", version="2.1.0rc1"),)
        ),
    )
    assert plan.status is AcceleratedPlanStatus.PLAN_READY
    entry = plan.added_requirements[0]
    assert entry.variant_status is VariantStatus.SELECTED
    assert entry.variant.version == "2.1.0rc1"


def test_prerelease_variant_below_release_bound_blocked():
    """PEP 440 orders ``2.0.0rc1`` *below* ``2.0``, so ``>=2.0`` does
    not contain it even with prereleases allowed — fail-closed BLOCKED
    with a deterministic detail."""
    plan = build(
        ("zebench", _acc_block(_requirement("accelerated-lib", ">=2.0"))),
        variant_catalog=AcceleratedVariantCatalog(
            variants=(variant("accelerated-lib", version="2.0.0rc1"),)
        ),
    )
    assert plan.status is AcceleratedPlanStatus.BLOCKED
    assert plan.blocked_reason == (
        "no accelerated variant available for: accelerated-lib "
        "(declared >=2.0 not satisfied by available variant 2.0.0rc1)"
    )


def test_specifier_unsatisfied_entry_marked_not_available():
    """A found-but-unsatisfying variant is treated as unavailable while
    other SELECTED entries survive — the blocked reason carries the
    detail alongside plain missing distributions."""
    plan = build(
        ("prod-a", _acc_block(_requirement("accelerated-lib", ">=2.0,<3.0"))),
        ("prod-b", _acc_block(_requirement("kernel-common"))),
        variant_catalog=AcceleratedVariantCatalog(
            variants=(
                variant("accelerated-lib", version="1.2.0"),
                variant("kernel-common"),
            )
        ),
    )
    assert plan.status is AcceleratedPlanStatus.BLOCKED
    statuses = {
        entry.distribution: entry.variant_status
        for entry in plan.added_requirements
    }
    assert statuses == {
        "accelerated-lib": VariantStatus.NOT_AVAILABLE,
        "kernel-common": VariantStatus.SELECTED,
    }
    assert plan.blocked_reason == (
        "no accelerated variant available for: accelerated-lib "
        "(declared >=2.0,<3.0 not satisfied by available variant 1.2.0)"
    )


# ===========================================================================
# Source runtime snapshot, determinism and purity
# ===========================================================================


def test_source_runtime_state_copied():
    """source_runtime_state and slot ids are copied from the runtime status."""
    plan = build(
        ("zebench", _acc_block(_requirement("accelerated-lib"))),
        variant_catalog=AcceleratedVariantCatalog(variants=(variant("accelerated-lib"),)),
        runtime_status=make_runtime_status(
            state=RuntimeState.BROKEN,
            active_slot_id=None,
            previous_slot_id=None,
        ),
    )
    assert plan.source_runtime_state == "BROKEN"
    assert plan.source_active_slot_id is None
    assert plan.source_previous_slot_id is None


def test_deterministic_equal_inputs_equal_plans():
    """Two builds from equal inputs produce equal plans (including
    keep-product insertion order independence)."""
    variant_catalog = AcceleratedVariantCatalog(
        variants=(
            variant("accelerated-lib"),
            variant("kernel-common", version="9.0"),
        )
    )
    kp_zebra = keep_product("zebra", version="2.0.0", commit_sha=SHA_B, wheel_sha256=WHEEL_B)
    kp_aardvark = keep_product("aardvark", version="1.0.0", commit_sha=SHA_A, wheel_sha256=WHEEL_A)

    def make() -> AcceleratedDeploymentPlan:
        return build(
            ("prod-b", _acc_block(_requirement("kernel-common", "==9.0"))),
            ("prod-a", _acc_block(_requirement("accelerated-lib", ">=1.0"))),
            variant_catalog=variant_catalog,
            keep_products={"zebra": kp_zebra, "aardvark": kp_aardvark},
        )

    def make_reversed_keeps() -> AcceleratedDeploymentPlan:
        return build(
            ("prod-b", _acc_block(_requirement("kernel-common", "==9.0"))),
            ("prod-a", _acc_block(_requirement("accelerated-lib", ">=1.0"))),
            variant_catalog=variant_catalog,
            keep_products={"aardvark": kp_aardvark, "zebra": kp_zebra},
        )

    first = make()
    second = make()
    assert first == second
    assert make_reversed_keeps() == first
    assert first.status is AcceleratedPlanStatus.PLAN_READY


def test_planning_has_no_side_effects(tmp_path):
    """The planner is pure: no files written, inputs untouched."""
    runtime_status = make_runtime_status()
    variant_catalog = AcceleratedVariantCatalog(variants=(variant("accelerated-lib"),))
    build(
        ("zebench", _acc_block(_requirement("accelerated-lib"))),
        variant_catalog=variant_catalog,
        runtime_status=runtime_status,
    )
    assert list(tmp_path.iterdir()) == []
    assert runtime_status.state is RuntimeState.READY
    assert runtime_status.active_slot_id == "slot-active"
    assert runtime_status.previous_slot_id == "slot-previous"
    assert variant_catalog.variants == (variant("accelerated-lib"),)


# ===========================================================================
# Plan model validation
# ===========================================================================


def test_planned_keep_product_validation():
    """PlannedKeepProduct rejects empty product ids and versions."""
    with pytest.raises(ValueError, match="product_id"):
        PlannedKeepProduct(product_id="  ", version="1.0.0")
    with pytest.raises(ValueError, match="version"):
        PlannedKeepProduct(product_id="prod-a", version=" ")


def test_planned_keep_product_source_validation():
    """source defaults to provenance and must be one of the two known
    tags."""
    assert (
        PlannedKeepProduct(product_id="prod-a", version="1.0.0").source
        == "provenance"
    )
    keep = PlannedKeepProduct(
        product_id="prod-a", version="1.0.0", source="installed_lock"
    )
    assert keep.source == "installed_lock"
    with pytest.raises(ValueError, match="source"):
        PlannedKeepProduct(product_id="prod-a", version="1.0.0", source="lock")


def test_planned_dependency_variant_status_invariant():
    """variant_status must be SELECTED iff variant is not None."""
    v = variant("accelerated-lib")
    with pytest.raises(ValueError, match="SELECTED iff variant"):
        PlannedAcceleratedDependency(
            distribution="accelerated-lib",
            specifier=None,
            extras=(),
            declaring_products=("prod-a",),
            variant=v,
            variant_status=VariantStatus.NOT_AVAILABLE,
        )
    with pytest.raises(ValueError, match="SELECTED iff variant"):
        PlannedAcceleratedDependency(
            distribution="accelerated-lib",
            specifier=None,
            extras=(),
            declaring_products=("prod-a",),
            variant=None,
            variant_status=VariantStatus.SELECTED,
        )


def test_planned_dependency_rejects_empty_declaring_products():
    """declaring_products must not be empty."""
    with pytest.raises(ValueError, match="declaring_products"):
        PlannedAcceleratedDependency(
            distribution="accelerated-lib",
            specifier=None,
            extras=(),
            declaring_products=(),
            variant=None,
            variant_status=VariantStatus.NOT_AVAILABLE,
        )


def test_plan_status_enum_values():
    """AcceleratedPlanStatus exposes the four documented statuses."""
    assert {s.value for s in AcceleratedPlanStatus} == {
        "NO_ACCELERATED_REQUIREMENTS",
        "PLAN_READY",
        "BLOCKED",
        "UNKNOWN",
    }


# ===========================================================================
# ZA-M1-2J.2 Phase F — host prerequisites classification in the plan
# ===========================================================================


def test_plan_ready_attaches_host_prerequisites():
    """A PLAN_READY plan attaches the host prerequisites classification:
    driver OK (observed version), CC NOT_OBSERVED (documentary), and
    MANAGED_RUNTIME entries for each planned distribution + cost."""
    plan = build(
        ("zebench", _acc_block(_requirement("accelerated-lib", ">=1.0"))),
        capabilities=make_nvidia_capabilities("550.163.01"),
        variant_catalog=AcceleratedVariantCatalog(
            variants=(variant("accelerated-lib"),)
        ),
    )
    assert plan.status is AcceleratedPlanStatus.PLAN_READY
    prereqs = plan.host_prerequisites
    assert prereqs is not None
    assert prereqs.status is HostPrerequisitesStatus.OK
    assert prereqs.reason is None

    required = {entry.entry: entry for entry in prereqs.required_host}
    driver = required["nvidia-driver"]
    assert driver.status is HostPrerequisiteStatus.OK
    assert driver.observed == "550.163.01"
    assert "550.54.14" in driver.requirement
    cc = required["nvidia-gpu-cc"]
    assert cc.status is HostPrerequisiteStatus.NOT_OBSERVED

    managed = {
        entry.entry: entry
        for entry in prereqs.managed_runtime
        if entry.entry != "total"
    }
    assert managed == {
        "accelerated-lib": HostPrerequisiteEntry(
            entry="accelerated-lib",
            requirement="==1.0.0",
            status=HostPrerequisiteStatus.MANAGED,
        ),
    }
    totals = [
        entry for entry in prereqs.managed_runtime if entry.entry == "total"
    ]
    assert len(totals) == 1
    assert "download" in totals[0].requirement


def test_driver_below_floor_blocks_plan():
    """A checkable missing host precondition (driver below floor) =>
    BLOCKED with the honest reason, no added requirements, no partial
    fallback."""
    plan = build(
        ("zebench", _acc_block(_requirement("accelerated-lib", ">=1.0"))),
        capabilities=make_nvidia_capabilities("550.54.13"),
        variant_catalog=AcceleratedVariantCatalog(
            variants=(variant("accelerated-lib"),)
        ),
    )
    assert plan.status is AcceleratedPlanStatus.BLOCKED
    assert plan.blocked is True
    assert plan.backend == "NVIDIA_CUDA"
    assert plan.added_requirements == ()
    assert plan.target_runtime == "no new runtime required"
    reason = plan.blocked_reason or ""
    assert "host prerequisite not satisfied" in reason
    assert "nvidia-driver 550.54.13" in reason
    assert ">= 550.54.14" in reason
    assert plan.host_prerequisites is None


def test_driver_at_exact_floor_is_accepted():
    """The curated floor itself (550.54.14) satisfies the check."""
    plan = build(
        ("zebench", _acc_block(_requirement("accelerated-lib", ">=1.0"))),
        capabilities=make_nvidia_capabilities("550.54.14"),
        variant_catalog=AcceleratedVariantCatalog(
            variants=(variant("accelerated-lib"),)
        ),
    )
    assert plan.status is AcceleratedPlanStatus.PLAN_READY
    prereqs = plan.host_prerequisites
    assert prereqs is not None
    driver = {
        entry.entry: entry for entry in prereqs.required_host
    }["nvidia-driver"]
    assert driver.status is HostPrerequisiteStatus.OK
    assert driver.observed == "550.54.14"


def test_no_gpus_observed_driver_entry_not_observed_non_blocking():
    """Synthetic hosts without GPU observation (the recommendation gates
    real absence) get a NOT_OBSERVED driver entry — never a fabricated
    verdict, never a spurious BLOCKED."""
    plan = build(
        ("zebench", _acc_block(_requirement("accelerated-lib", ">=1.0"))),
        variant_catalog=AcceleratedVariantCatalog(
            variants=(variant("accelerated-lib"),)
        ),
    )
    assert plan.status is AcceleratedPlanStatus.PLAN_READY
    prereqs = plan.host_prerequisites
    assert prereqs is not None
    driver = {
        entry.entry: entry for entry in prereqs.required_host
    }["nvidia-driver"]
    assert driver.status is HostPrerequisiteStatus.NOT_OBSERVED


def test_driver_floor_violation_beats_variant_lookup():
    """Host problems win over variant problems: a driver below floor
    BLOCKs with the host reason even when the variant catalog is empty."""
    plan = build(
        ("zebench", _acc_block(_requirement("accelerated-lib", ">=1.0"))),
        capabilities=make_nvidia_capabilities("470.161.03"),
    )
    assert plan.status is AcceleratedPlanStatus.BLOCKED
    assert plan.added_requirements == ()
    assert "nvidia-driver 470.161.03" in (plan.blocked_reason or "")


def test_plan_with_host_prerequisites_validation():
    """The plan model validates its new optional field (soft migration:
    default None stays valid)."""
    assert _make_none_prereq_plan().host_prerequisites is None
    with pytest.raises(ValueError, match="host_prerequisites"):
        AcceleratedDeploymentPlan(
            status=AcceleratedPlanStatus.BLOCKED,
            hardware=HardwareCompatibility(
                status=HardwareCompatibilityStatus.BLOCKED,
                reason_code="ACCELERATION_BLOCKED",
                reason="blocked",
                products_concerned=(),
            ),
            backend=None,
            products_concerned=(),
            keep_products=(),
            added_requirements=(),
            source_runtime_state="READY",
            source_active_slot_id=None,
            source_previous_slot_id=None,
            target_runtime="no new runtime required",
            blocked=True,
            blocked_reason="blocked",
            closure_impact=(),
            host_prerequisites="not-a-HostPrerequisites",  # type: ignore[arg-type]
        )


def _make_none_prereq_plan() -> AcceleratedDeploymentPlan:
    return AcceleratedDeploymentPlan(
        status=AcceleratedPlanStatus.BLOCKED,
        hardware=HardwareCompatibility(
            status=HardwareCompatibilityStatus.BLOCKED,
            reason_code="ACCELERATION_BLOCKED",
            reason="blocked",
            products_concerned=(),
        ),
        backend=None,
        products_concerned=(),
        keep_products=(),
        added_requirements=(),
        source_runtime_state="READY",
        source_active_slot_id=None,
        source_previous_slot_id=None,
        target_runtime="no new runtime required",
        blocked=True,
        blocked_reason="blocked",
        closure_impact=(),
    )
