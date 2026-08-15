"""Service-layer tests for M1-2H accelerated deployment plan preview.

Verifies that ``ZeAlfieService.build_accelerated_deployment_plan`` wires
the pure planner to the service state: host observation/interpretation
(injectable), runtime status readback, KEEP products documented
verbatim from provenance and the installed-runtime lock, and the
fail-closed variant catalog.  Every test injects fake collectors and
synthetic catalogs — never real hardware.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zealfie.acceleration import (
    AcceleratedPlanStatus,
    AcceleratedVariant,
    AcceleratedVariantCatalog,
    PlannedKeepProduct,
    VariantStatus,
)
from zealfie.acceleration.models import (
    AcceleratedRequirement,
    ProductAccelerationRequirements,
)
from zealfie.app import (
    ProductCatalog,
    ProductDescriptor,
    ProductPolicyStore,
    ProductProvenance,
    ProductProvenanceStore,
    SelectionStore,
    ZeAlfieService,
)
from zealfie.components.model import EntryPointContract
from zealfie.host.models import (
    AccelerationRecommendation,
    CapabilityStatus,
    HostCapabilities,
    HostReasonCode,
    RecommendationStatus,
)
from zealfie.products.catalog import default_catalog
from zealfie.releases.model import HostTarget
from zealfie.runtime.installed_lock import (
    InstalledDependency,
    InstalledLockStore,
    InstalledRuntimeLock,
)
from zealfie.runtime.layout import RuntimeLayout
from zealfie.runtime.model import RuntimeState, RuntimeStatus
from zealfie.runtime.state import save_active_state

_EP = (EntryPointContract("console_scripts", "zewitness"),)
SHA_A = "a" * 40
WHEEL_A = "f" * 64


# ---------------------------------------------------------------------------
# Helpers — synthetic, hermetic
# ---------------------------------------------------------------------------


def _caps(**kwargs) -> HostCapabilities:
    defaults = dict(
        os_name="Linux",
        cpu_arch="x86_64",
        platform_status=CapabilityStatus.AVAILABLE,
        platform_reason_code=HostReasonCode.OS_DETECTED,
        platform_reason="os detected",
        gpus=(),
        partial=False,
    )
    defaults.update(kwargs)
    return HostCapabilities(**defaults)


def _recommendation(
    status: RecommendationStatus = RecommendationStatus.OFFER_SETUP,
    reason: str = "supported accelerator detected; setup offered",
    backend: str = "NVIDIA_CUDA",
) -> AccelerationRecommendation:
    reason_code = {
        RecommendationStatus.OFFER_SETUP: HostReasonCode.ACCELERATION_OFFER_SETUP,
        RecommendationStatus.ALREADY_READY: HostReasonCode.ACCELERATION_ALREADY_READY,
        RecommendationStatus.NOT_APPLICABLE: HostReasonCode.ACCELERATION_NOT_APPLICABLE,
        RecommendationStatus.BLOCKED: HostReasonCode.ACCELERATION_BLOCKED,
        RecommendationStatus.UNKNOWN: HostReasonCode.ACCELERATION_UNKNOWN,
    }[status]
    return AccelerationRecommendation(
        status=status,
        backend=backend,
        reason_code=reason_code,
        reason=reason,
    )


def _host() -> HostTarget:
    return HostTarget(
        python_tag="py312",
        abi_tag="cp312",
        platform_tag="linux_x86_64",
    )


class _FakeRt:
    """Minimal runtime exposing only ``status()`` (no layout attribute)."""

    def __init__(self, status: RuntimeStatus) -> None:
        self._status = status

    def status(self) -> RuntimeStatus:
        return self._status


def _absent_runtime(root: Path) -> _FakeRt:
    return _FakeRt(
        RuntimeStatus(state=RuntimeState.ABSENT, runtime_root=root)
    )


def _descriptor_with_acceleration() -> ProductDescriptor:
    acceleration = ProductAccelerationRequirements(
        product_id="zebench",
        backend="NVIDIA_CUDA",
        optional=True,
        requirements=(
            AcceleratedRequirement(
                distribution="accelerated-lib",
                specifier=">=1.0",
            ),
        ),
    )
    return ProductDescriptor(
        product_id="zebench",
        display_name="ZeBench",
        distribution_name="zebench",
        launch_entry_points=_EP,
        acceleration=acceleration,
    )


def _descriptor_plain() -> ProductDescriptor:
    return ProductDescriptor(
        product_id="zebench",
        display_name="ZeBench",
        distribution_name="zebench",
        launch_entry_points=_EP,
    )


def _variant_catalog() -> AcceleratedVariantCatalog:
    return AcceleratedVariantCatalog(
        variants=(
            AcceleratedVariant(
                distribution="accelerated-lib",
                version="1.2.0",
                backend="NVIDIA_CUDA",
                platform="linux_x86_64",
            ),
        )
    )


def _snapshot(root: Path) -> dict[str, bytes]:
    """Full byte snapshot of every file under *root* (deterministic)."""
    snapshot: dict[str, bytes] = {}
    if not root.exists():
        return snapshot
    for path in sorted(root.rglob("*")):
        if path.is_file():
            snapshot[str(path.relative_to(root))] = path.read_bytes()
    return snapshot


# ---------------------------------------------------------------------------
# Default catalog: honest no-requirements plan, fully read-only
# ---------------------------------------------------------------------------


def test_default_catalog_no_accelerated_requirements_plan_read_only(
    tmp_path,
):
    """Default catalog (no acceleration declared) -> honest
    NO_ACCELERATED_REQUIREMENTS plan; the runtime (status + files,
    including active.json bytes) is unchanged after planning."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    save_active_state(layout.active_pointer, "rt-abc123", None)
    runtime = _FakeRt(
        RuntimeStatus(
            state=RuntimeState.READY,
            runtime_root=layout.root,
            active_slot_id="rt-abc123",
        )
    )

    collector_calls = 0

    def collector():
        nonlocal collector_calls
        collector_calls += 1
        return _caps()

    def recommender(caps):
        return _recommendation(RecommendationStatus.NOT_APPLICABLE)

    service = ZeAlfieService(
        catalog=default_catalog(),
        runtime=runtime,
        host=_host(),
        capability_collector=collector,
        recommender=recommender,
    )

    status_before = runtime.status()
    snapshot_before = _snapshot(tmp_path)

    plan = service.build_accelerated_deployment_plan()

    status_after = runtime.status()
    snapshot_after = _snapshot(tmp_path)

    assert plan.status is AcceleratedPlanStatus.NO_ACCELERATED_REQUIREMENTS
    assert plan.blocked is True
    assert plan.backend is None
    assert plan.products_concerned == ()
    assert plan.added_requirements == ()
    assert plan.keep_products == ()
    assert plan.target_runtime == "no new runtime required"
    assert "no product declares accelerated requirements" in (
        plan.blocked_reason or ""
    )
    assert any(
        "preserved" in line.lower() for line in plan.closure_impact
    )

    # 100% read-only: identical status and identical file bytes,
    # including the active.json pointer.
    assert status_after == status_before
    assert snapshot_after == snapshot_before
    assert any(key.endswith("active.json") for key in snapshot_before)
    assert collector_calls == 1


def test_planning_never_writes_files_with_absent_runtime(tmp_path):
    """With a fully absent runtime, planning creates no files at all."""
    service = ZeAlfieService(
        catalog=default_catalog(),
        runtime=_absent_runtime(tmp_path / "fake"),
        host=_host(),
        capability_collector=_caps,
        recommender=lambda caps: _recommendation(
            RecommendationStatus.NOT_APPLICABLE
        ),
    )
    plan = service.build_accelerated_deployment_plan()
    assert plan.status is AcceleratedPlanStatus.NO_ACCELERATED_REQUIREMENTS
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Synthetic accelerated catalog: PLAN_READY with injected inputs
# ---------------------------------------------------------------------------


def test_plan_ready_with_synthetic_catalog_and_injected_inputs():
    """A synthetic catalog declaring acceleration + injected
    capabilities/recommendation + synthetic variant catalog ->
    PLAN_READY with the correct backend and selected variant, and no
    double probing (injected inputs are used as-is)."""
    collector_calls = 0
    recommender_calls = 0

    def collector():
        nonlocal collector_calls
        collector_calls += 1
        raise AssertionError("injected capabilities must avoid probing")

    def recommender(caps):
        nonlocal recommender_calls
        recommender_calls += 1
        raise AssertionError("injected recommendation must avoid re-deriving")

    service = ZeAlfieService(
        catalog=ProductCatalog((_descriptor_with_acceleration(),)),
        runtime=_absent_runtime(Path("/fake")),
        host=_host(),
        capability_collector=collector,
        recommender=recommender,
    )

    plan = service.build_accelerated_deployment_plan(
        capabilities=_caps(),
        recommendation=_recommendation(),
        variant_catalog=_variant_catalog(),
    )

    assert plan.status is AcceleratedPlanStatus.PLAN_READY
    assert plan.blocked is False
    assert plan.backend == "NVIDIA_CUDA"
    assert plan.products_concerned == ("zebench",)
    assert plan.target_runtime == (
        "new shared runtime slot with accelerated NVIDIA_CUDA closure"
    )
    assert len(plan.added_requirements) == 1
    entry = plan.added_requirements[0]
    assert entry.distribution == "accelerated-lib"
    assert entry.specifier == ">=1.0"
    assert entry.variant_status is VariantStatus.SELECTED
    assert entry.variant is not None
    assert entry.variant.version == "1.2.0"
    assert collector_calls == 0
    assert recommender_calls == 0


def test_plan_uses_default_collection_once_when_not_injected():
    """Without injected inputs the plan collects once and derives the
    recommendation from that exact same observation."""
    calls = []
    caps_obj = _caps()

    def collector():
        calls.append("collect")
        return caps_obj

    def recommender(caps):
        calls.append(("recommend", caps))
        return _recommendation()

    service = ZeAlfieService(
        catalog=ProductCatalog((_descriptor_with_acceleration(),)),
        runtime=_absent_runtime(Path("/fake")),
        host=_host(),
        capability_collector=collector,
        recommender=recommender,
    )
    plan = service.build_accelerated_deployment_plan()
    assert calls == ["collect", ("recommend", caps_obj)]
    assert plan.hardware.status.value == "SUPPORTED"


def test_missing_variant_blocks_plan_honestly():
    """Default (empty) variant catalog -> BLOCKED, fail-closed, no
    partial fallback — the plan is still a successful preview."""
    service = ZeAlfieService(
        catalog=ProductCatalog((_descriptor_with_acceleration(),)),
        runtime=_absent_runtime(Path("/fake")),
        host=_host(),
        capability_collector=_caps,
        recommender=lambda caps: _recommendation(),
    )
    plan = service.build_accelerated_deployment_plan()
    assert plan.status is AcceleratedPlanStatus.BLOCKED
    assert plan.blocked is True
    assert plan.blocked_reason == (
        "no accelerated variant available for: accelerated-lib"
    )
    entry = plan.added_requirements[0]
    assert entry.variant_status is VariantStatus.NOT_AVAILABLE
    assert entry.variant is None


# ---------------------------------------------------------------------------
# KEEP products: provenance verbatim, lock degradation
# ---------------------------------------------------------------------------


def test_provenance_keep_products_carried_verbatim(tmp_path):
    """Active provenance -> PlannedKeepProduct carries the exact
    commit_sha/wheel_sha256 verbatim (never re-resolved), even in a
    no-accelerated-requirements plan."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    store = ProductProvenanceStore(layout)
    provenance = ProductProvenance(
        product_id="zebench",
        version="2.0.0",
        source_owner="tinystork",
        source_repo="ZeBench",
        requested_ref="main",
        commit_sha=SHA_A,
        wheel_sha256=WHEEL_A,
    )
    store.record("rt-abc123", [provenance])
    save_active_state(layout.active_pointer, "rt-abc123", None)

    service = ZeAlfieService(
        catalog=default_catalog(),
        runtime=_FakeRt(
            RuntimeStatus(
                state=RuntimeState.READY,
                runtime_root=layout.root,
                active_slot_id="rt-abc123",
            )
        ),
        host=_host(),
        capability_collector=_caps,
        recommender=lambda caps: _recommendation(
            RecommendationStatus.NOT_APPLICABLE
        ),
        provenance_store=store,
    )
    plan = service.build_accelerated_deployment_plan()

    expected = PlannedKeepProduct(
        product_id="zebench",
        version="2.0.0",
        commit_sha=SHA_A,
        wheel_sha256=WHEEL_A,
    )
    assert plan.keep_products == (expected,)
    assert plan.keep_products[0].commit_sha == SHA_A
    assert plan.keep_products[0].wheel_sha256 == WHEEL_A
    # Provenance bytes are still on disk, unchanged.
    assert store.load_active() == {"zebench": provenance}


def test_installed_lock_only_keep_degrades_shas_to_none(tmp_path):
    """A primary installed distribution without provenance -> KEEP
    product with version from the lock and commit/wheel SHAs honestly
    degraded to None (never fabricated)."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    lock = InstalledRuntimeLock(
        primary_names=frozenset({"zebench"}),
        dependencies={
            "zebench": InstalledDependency(
                name="zebench",
                version="3.1.0",
                primary=True,
            )
        },
    )
    store = InstalledLockStore(layout)
    store.record("rt-abc123", lock)
    save_active_state(layout.active_pointer, "rt-abc123", None)

    service = ZeAlfieService(
        catalog=ProductCatalog((_descriptor_plain(),)),
        runtime=_FakeRt(
            RuntimeStatus(
                state=RuntimeState.READY,
                runtime_root=layout.root,
                active_slot_id="rt-abc123",
            )
        ),
        host=_host(),
        capability_collector=_caps,
        recommender=lambda caps: _recommendation(
            RecommendationStatus.NOT_APPLICABLE
        ),
        installed_lock_store=store,
    )
    plan = service.build_accelerated_deployment_plan()

    assert plan.keep_products == (
        PlannedKeepProduct(
            product_id="zebench",
            version="3.1.0",
            commit_sha=None,
            wheel_sha256=None,
            source="installed_lock",
        ),
    )


def test_provenance_wins_over_installed_lock(tmp_path):
    """When both stores describe the same product, provenance is
    authoritative and its SHAs are never overwritten by lock data."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    prov_store = ProductProvenanceStore(layout)
    prov_store.record(
        "rt-abc123",
        [
            ProductProvenance(
                product_id="zebench",
                version="2.0.0",
                source_owner="tinystork",
                source_repo="ZeBench",
                requested_ref="main",
                commit_sha=SHA_A,
                wheel_sha256=WHEEL_A,
            )
        ],
    )
    lock_store = InstalledLockStore(layout)
    lock_store.record(
        "rt-abc123",
        InstalledRuntimeLock(
            primary_names=frozenset({"zebench"}),
            dependencies={
                "zebench": InstalledDependency(
                    name="zebench", version="9.9.9", primary=True
                )
            },
        ),
    )
    save_active_state(layout.active_pointer, "rt-abc123", None)

    service = ZeAlfieService(
        catalog=ProductCatalog((_descriptor_plain(),)),
        runtime=_FakeRt(
            RuntimeStatus(
                state=RuntimeState.READY,
                runtime_root=layout.root,
                active_slot_id="rt-abc123",
            )
        ),
        host=_host(),
        capability_collector=_caps,
        recommender=lambda caps: _recommendation(
            RecommendationStatus.NOT_APPLICABLE
        ),
        provenance_store=prov_store,
        installed_lock_store=lock_store,
    )
    plan = service.build_accelerated_deployment_plan()

    assert plan.keep_products == (
        PlannedKeepProduct(
            product_id="zebench",
            version="2.0.0",
            commit_sha=SHA_A,
            wheel_sha256=WHEEL_A,
        ),
    )


def test_keep_products_carry_source_tags(tmp_path):
    """Provenance KEEP entries carry source='provenance'; installed-lock
    fallback entries carry source='installed_lock'."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    prov_store = ProductProvenanceStore(layout)
    prov_store.record(
        "rt-abc123",
        [
            ProductProvenance(
                product_id="zebench",
                version="2.0.0",
                source_owner="tinystork",
                source_repo="ZeBench",
                requested_ref="main",
                commit_sha=SHA_A,
                wheel_sha256=WHEEL_A,
            )
        ],
    )
    lock_store = InstalledLockStore(layout)
    lock_store.record(
        "rt-abc123",
        InstalledRuntimeLock(
            primary_names=frozenset({"zeother"}),
            dependencies={
                "zeother": InstalledDependency(
                    name="zeother", version="1.5.0", primary=True
                )
            },
        ),
    )
    save_active_state(layout.active_pointer, "rt-abc123", None)

    other = ProductDescriptor(
        product_id="zeother",
        display_name="ZeOther",
        distribution_name="zeother",
        launch_entry_points=_EP,
    )
    service = ZeAlfieService(
        catalog=ProductCatalog((_descriptor_plain(), other)),
        runtime=_FakeRt(
            RuntimeStatus(
                state=RuntimeState.READY,
                runtime_root=layout.root,
                active_slot_id="rt-abc123",
            )
        ),
        host=_host(),
        capability_collector=_caps,
        recommender=lambda caps: _recommendation(
            RecommendationStatus.NOT_APPLICABLE
        ),
        provenance_store=prov_store,
        installed_lock_store=lock_store,
    )
    plan = service.build_accelerated_deployment_plan()

    by_id = {keep.product_id: keep for keep in plan.keep_products}
    assert set(by_id) == {"zebench", "zeother"}
    assert by_id["zebench"].source == "provenance"
    assert by_id["zebench"].commit_sha == SHA_A
    assert by_id["zebench"].wheel_sha256 == WHEEL_A
    assert by_id["zeother"].source == "installed_lock"
    assert by_id["zeother"].commit_sha is None
    assert by_id["zeother"].wheel_sha256 is None
    assert by_id["zeother"].version == "1.5.0"
    # Deterministic order by product id.
    assert tuple(keep.product_id for keep in plan.keep_products) == (
        "zebench",
        "zeother",
    )


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_plan_is_deterministic():
    """Two calls with identical injected inputs produce equal plans."""
    service = ZeAlfieService(
        catalog=ProductCatalog((_descriptor_with_acceleration(),)),
        runtime=_absent_runtime(Path("/fake")),
        host=_host(),
        capability_collector=_caps,
        recommender=lambda caps: _recommendation(),
    )
    first = service.build_accelerated_deployment_plan(
        capabilities=_caps(),
        recommendation=_recommendation(),
        variant_catalog=_variant_catalog(),
    )
    second = service.build_accelerated_deployment_plan(
        capabilities=_caps(),
        recommendation=_recommendation(),
        variant_catalog=_variant_catalog(),
    )
    assert first == second
    assert first.status is AcceleratedPlanStatus.PLAN_READY
