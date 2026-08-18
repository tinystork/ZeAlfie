"""ZA-M1-3A.2 — accelerated slot state continuity regression tests.

Real witness (Windows, Tristan): after ``install_accelerated_runtime``
the new active slot (e.g. rt-9a0854fc3f72) carried the accelerated
closure (ZeSolver + ZeMosaic + CUDA) but NO product provenance and NO
installed-lock.  Observed consequences, all covered here:

* update checks reported ``PROVENANCE_UNKNOWN`` for every product;
* installing a third product refused full-state reconstruction
  (selected products had no active provenance);
* the GPU panel kept offering setup although the accelerated runtime
  was installed (readiness was never derived from the active slot).

These tests lock the corrected contract:

1. products A+B installed with exact provenance identities/SHAs;
2. GPU deployment (PLAN_READY, synthetic acquirer/gate);
3. the new active slot is fully described: provenance = exact KEEP
   identities (same versions / commit SHAs / wheel digests — never
   invented) and an installed-runtime lock including the acquired
   accelerated closure actually deployed;
4. update checks read the new provenance (never PROVENANCE_UNKNOWN);
5. a third product install rebuilds the full state from the new
   provenance (keep machinery sees A+B at their exact SHAs);
6. failure / cancellation never promotes a partial new-slot write to
   authority: the old slot keeps its provenance and lock, and no other
   slot carries records.

Hermetic: synthetic catalog, fixture-built wheels, injected acquirer —
the same harness style as ``tests/test_accelerated_service.py``.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

import pytest

from zealfie.acceleration import (
    AcceleratedAcquisitionError,
    AcceleratedDeploymentPhase,
    AcceleratedDeploymentPlan,
    AcceleratedPlanStatus,
    AcceleratedVariant,
    CooperativeCancellationError,
    HardwareCompatibility,
    HardwareCompatibilityReasonCode,
    HardwareCompatibilityStatus,
    PlannedAcceleratedDependency,
    PlannedKeepProduct,
    VariantStatus,
)
from zealfie.acceleration.deployment import (
    AcceleratedSlotMetadataStore,
    AcquiredAcceleratedVariant,
)
from zealfie.acceleration.models import (
    AcceleratedRequirement,
    ProductAccelerationRequirements,
)
from zealfie.app import (
    InstallPhase,
    PreparedProductArtifact,
    ProductCatalog,
    ProductDescriptor,
    SelectionStore,
    UpdateStatus,
    ZeAlfieService,
)
from zealfie.products.policy import ProductPolicyStore
from zealfie.building import inspect_wheel
from zealfie.components.model import EntryPointContract
from zealfie.host.models import (
    AccelerationRecommendation,
    CapabilityStatus,
    HostCapabilities,
    HostReasonCode,
    RecommendationStatus,
)
from zealfie.releases.model import HostTarget, VerifiedArtifact
from zealfie.runtime import (
    InstalledLockStore,
    RuntimeLayout,
    SharedRuntime,
)
from zealfie.runtime.probe import probe_runtime_distribution
from zealfie.runtime.provenance import (
    ProductProvenance,
    ProductProvenanceStore,
)
from zealfie.sources import RemoteSource, ResolvedSource

SHA_A = "d4a0f1e2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8"
SHA_B = "e5b1f2a3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9"
_EP_A = (EntryPointContract("console_scripts", "zewitness"),)
_EP_B = (EntryPointContract("console_scripts", "zewitness2"),)


# ---------------------------------------------------------------------------
# Synthetic catalog / host / plan helpers (mirror test_accelerated_service)
# ---------------------------------------------------------------------------


def _catalog() -> ProductCatalog:
    accel = ProductAccelerationRequirements(
        product_id="zewitness",
        backend="NVIDIA_CUDA",
        optional=True,
        requirements=(
            AcceleratedRequirement(
                distribution="fake-accel",
                specifier="==1.0.0",
            ),
        ),
    )
    return ProductCatalog((
        ProductDescriptor(
            product_id="zewitness",
            display_name="ZeWitness",
            distribution_name="zealfie-witness",
            launch_entry_points=_EP_A,
            required_extras=(),
            remote_source=RemoteSource(
                owner="tinystork",
                repo="ZeWitness",
                ref="main",
            ),
            acceleration=accel,
        ),
        ProductDescriptor(
            product_id="zewitness2",
            display_name="ZeWitnessTwo",
            distribution_name="zealfie-witness2",
            launch_entry_points=_EP_B,
            required_extras=(),
            remote_source=RemoteSource(
                owner="tinystork",
                repo="ZeWitness2",
                ref="main",
            ),
        ),
        ProductDescriptor(
            product_id="zethird",
            display_name="ZeThird",
            distribution_name="zealfie-third",
            launch_entry_points=(),
            required_extras=(),
            remote_source=RemoteSource(
                owner="tinystork",
                repo="ZeThird",
                ref="main",
            ),
        ),
    ))


def _host() -> HostTarget:
    return HostTarget(
        python_tag="py312",
        abi_tag="cp312",
        platform_tag="linux_x86_64",
    )


def _caps() -> HostCapabilities:
    return HostCapabilities(
        os_name="Linux",
        cpu_arch="x86_64",
        platform_status=CapabilityStatus.AVAILABLE,
        platform_reason_code=HostReasonCode.OS_DETECTED,
        platform_reason="os detected",
        gpus=(),
        partial=False,
    )


def _recommendation() -> AccelerationRecommendation:
    return AccelerationRecommendation(
        status=RecommendationStatus.OFFER_SETUP,
        backend="NVIDIA_CUDA",
        reason_code=HostReasonCode.ACCELERATION_OFFER_SETUP,
        reason="supported accelerator detected; setup offered",
    )


def _hardware() -> HardwareCompatibility:
    return HardwareCompatibility(
        status=HardwareCompatibilityStatus.SUPPORTED,
        reason_code=HardwareCompatibilityReasonCode.COMPATIBLE.value,
        reason="compatible",
        products_concerned=("zewitness",),
    )


def _accel_plan(
    source_active_slot_id: str,
    keep: tuple[PlannedKeepProduct, ...],
) -> AcceleratedDeploymentPlan:
    entry = PlannedAcceleratedDependency(
        distribution="fake-accel",
        specifier="==1.0.0",
        extras=(),
        declaring_products=("zewitness",),
        variant=AcceleratedVariant(
            distribution="fake-accel",
            version="1.0.0",
            backend="NVIDIA_CUDA",
        ),
        variant_status=VariantStatus.SELECTED,
    )
    return AcceleratedDeploymentPlan(
        status=AcceleratedPlanStatus.PLAN_READY,
        hardware=_hardware(),
        backend="NVIDIA_CUDA",
        products_concerned=("zewitness",),
        keep_products=keep,
        added_requirements=(entry,),
        source_runtime_state="READY",
        source_active_slot_id=source_active_slot_id,
        source_previous_slot_id=None,
        target_runtime=(
            "new shared runtime slot with accelerated NVIDIA_CUDA closure"
        ),
        blocked=False,
        blocked_reason=None,
        closure_impact=(),
    )


class _SingleWheelAcquirer:
    """Synthetic acquirer: copies the fake wheel into ``work_root`` and
    verifies size + sha256 (never the network)."""

    def __init__(self, wheel_path: Path, *, version: str = "1.0.0") -> None:
        self._wheel_path = wheel_path
        self._version = version
        self.calls = 0

    def acquire(self, plan, work_root, *, cancel_check=None):
        self.calls += 1
        acquired = []
        for entry in plan.added_requirements:
            dest = Path(work_root) / self._wheel_path.name
            shutil.copyfile(self._wheel_path, dest)
            size = dest.stat().st_size
            acquired.append(
                AcquiredAcceleratedVariant(
                    distribution=entry.distribution,
                    version=self._version,
                    wheel_path=dest,
                    size=size,
                    sha256=_sha256(dest),
                )
            )
        return tuple(acquired)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _ppa(
    product_id: str,
    wheel_path: Path,
    *,
    repo: str,
    version: str,
    commit_sha: str,
) -> PreparedProductArtifact:
    info = inspect_wheel(wheel_path)
    size = wheel_path.stat().st_size
    resolved = ResolvedSource(
        source=RemoteSource(owner="tinystork", repo=repo, ref="main"),
        commit_sha=commit_sha,
    )
    verified = VerifiedArtifact(
        component_id=product_id,
        version=version,
        path=wheel_path,
        size=size,
        sha256=_sha256(wheel_path),
        distribution_name=info.distribution_name,
        wheel_version=info.version,
    )
    return PreparedProductArtifact(
        product_id=product_id,
        component_id=product_id,
        resolved_source=resolved,
        wheel_path=wheel_path,
        verified_artifact=verified,
    )


def _keep_from_provenance(prov: ProductProvenance) -> PlannedKeepProduct:
    return PlannedKeepProduct(
        product_id=prov.product_id,
        version=prov.version,
        commit_sha=prov.commit_sha,
        wheel_sha256=prov.wheel_sha256,
        source="provenance",
    )


def _empty_wheelhouse(tmp_path: Path) -> Path:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir(parents=True, exist_ok=True)
    return wheelhouse


def _slot_python(slot_dir: Path) -> Path:
    if sys.platform == "win32":
        return slot_dir / "Scripts" / "python.exe"
    return slot_dir / "bin" / "python"


def _make_ready_service(
    tmp_path: Path, witness_v1: Path, witness_second: Path,
) -> tuple[ZeAlfieService, SharedRuntime, RuntimeLayout, SelectionStore, str]:
    """Install A (zewitness) + B (zewitness2) through the REAL service
    path; return service/runtime/layout/selection/active_slot_id."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    runtime = SharedRuntime(layout=layout)
    store = SelectionStore(path=tmp_path / "desired-products.toml")
    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=runtime,
        selection_store=store,
        policy_store=ProductPolicyStore(path=tmp_path / "policy.toml"),
        host=_host(),
        recommender=lambda caps: _recommendation(),
    )
    ppa_a = _ppa("zewitness", witness_v1, repo="ZeWitness",
                 version="0.0.1", commit_sha=SHA_A)
    ppa_b = _ppa("zewitness2", witness_second, repo="ZeWitness2",
                 version="0.1.0", commit_sha=SHA_B)
    result = service.install_prepared_product_deployment(
        [ppa_a, ppa_b],
        dependency_wheelhouse=_empty_wheelhouse(tmp_path),
    )
    assert result.success is True, f"base install failed: {result.reason}"
    active_id = runtime.status().active_slot_id
    assert active_id is not None
    return service, runtime, layout, store, active_id


# ---------------------------------------------------------------------------
# D.1-D.7 — the full continuity scenario
# ---------------------------------------------------------------------------


@pytest.mark.zealfie_slow
def test_accelerated_slot_continuity_full_flow(
    tmp_path, witness_v1, witness_second, witness_third, fake_accel_wheel,
    monkeypatch,
):
    """The complete ZA-M1-3A.2 regression scenario.

    A+B installed with exact provenance -> PLAN_READY GPU deployment ->
    new active slot -> the new slot is FULLY described (provenance with
    the same identities/SHAs + installed lock with the accelerated
    closure) -> update check is not PROVENANCE_UNKNOWN -> a third
    product install rebuilds the full state from the new provenance ->
    the service reports the active slot's accelerated runtime as
    validated (ALREADY_READY, never from a bare GPU probe).
    """
    monkeypatch.setattr(
        "zealfie.acceleration.deployment.get_backend_compute_probe",
        lambda backend: None,
    )
    service, runtime, layout, store, active_before = _make_ready_service(
        tmp_path, witness_v1, witness_second
    )

    # ---- 1. A+B installed with exact provenance identities/SHAs --------
    prov_before = dict(service.active_provenance())
    assert set(prov_before) == {"zewitness", "zewitness2"}
    assert prov_before["zewitness"].commit_sha == SHA_A
    assert prov_before["zewitness"].wheel_sha256 == _sha256(witness_v1)
    assert prov_before["zewitness2"].commit_sha == SHA_B
    assert prov_before["zewitness2"].wheel_sha256 == _sha256(witness_second)

    metadata_store = AcceleratedSlotMetadataStore(layout)
    provenance_store = ProductProvenanceStore(layout)
    installed_store = InstalledLockStore(layout)
    prov_slot_before = provenance_store.load_slot(active_before)
    lock_slot_before = installed_store.load_slot(active_before)
    assert prov_slot_before is not None
    assert lock_slot_before is not None
    selection_before = store.path.read_bytes()

    # ---- C (precondition): before any accelerated runtime exists, a GPU
    # probe alone never concludes readiness -----------------------------
    assert service.acceleration_runtime_ready() is False
    assert (
        service.get_acceleration_recommendation(_caps()).status
        is RecommendationStatus.OFFER_SETUP
    )

    # ---- 2/3. PLAN_READY GPU deployment -> new active slot -------------
    keeps = tuple(
        _keep_from_provenance(prov_before[pid]) for pid in sorted(prov_before)
    )
    plan = _accel_plan(active_before, keep=keeps)
    acquirer = _SingleWheelAcquirer(fake_accel_wheel)

    result = service.install_accelerated_runtime(
        plan=plan,
        capabilities=_caps(),
        recommendation=_recommendation(),
        acquirer=acquirer,
        gate=None,  # -> default gate (real probe inside candidate venv)
        metadata_store=None,  # -> derived from the runtime layout
        full_state_provider=lambda: [
            _ppa("zewitness", witness_v1, repo="ZeWitness",
                 version="0.0.1", commit_sha=SHA_A),
            _ppa("zewitness2", witness_second, repo="ZeWitness2",
                 version="0.1.0", commit_sha=SHA_B),
        ],
        dependency_wheelhouse=_empty_wheelhouse(tmp_path),
        work_root=tmp_path / "accel-work",
    )

    assert result.success is True, f"accelerated deploy failed: {result.reason}"
    assert result.phase is AcceleratedDeploymentPhase.COMPLETED
    assert result.old_runtime_preserved is True

    final = runtime.status()
    assert final.active_slot_id is not None
    assert final.active_slot_id != active_before
    assert result.active_slot_id == final.active_slot_id
    assert result.previous_slot_id == active_before
    # Selection is untouched by the accelerated mutation (products unchanged).
    assert store.path.read_bytes() == selection_before

    # ---- 4. The NEW slot carries provenance: A+B with the SAME SHAs ----
    active_prov = dict(service.active_provenance())
    assert set(active_prov) == {"zewitness", "zewitness2"}
    assert active_prov["zewitness"] == prov_before["zewitness"]
    assert active_prov["zewitness2"] == prov_before["zewitness2"]
    assert active_prov["zewitness"].wheel_sha256 == _sha256(witness_v1)
    assert active_prov["zewitness2"].wheel_sha256 == _sha256(witness_second)
    # The old slot's records stay intact (rollback target).
    assert provenance_store.load_slot(active_before) == prov_slot_before

    # ---- 5. The NEW slot carries an installed-runtime lock including
    # the accelerated closure actually deployed --------------------------
    new_lock = installed_store.load_slot(final.active_slot_id)
    assert new_lock is not None
    for dist, prim in (("zealfie-witness", True), ("zealfie-witness2", True)):
        assert dist in new_lock.dependencies, dist
        assert new_lock.dependencies[dist].primary is prim
    assert "fake-accel" in new_lock.dependencies
    accel_dep = new_lock.dependencies["fake-accel"]
    assert accel_dep.version == "1.0.0"
    assert accel_dep.primary is False
    assert "zealfie-witness" in accel_dep.required_by
    # Active readback resolves to the new slot's lock.
    assert installed_store.load_active() == new_lock
    # Old slot lock unchanged.
    assert installed_store.load_slot(active_before) == lock_slot_before

    # ---- Accelerated metadata still under the final slot ----------------
    meta = metadata_store.load_slot(final.active_slot_id)
    assert meta is not None
    assert meta.backend == "NVIDIA_CUDA"
    assert meta.variants == (
        ("fake-accel", "1.0.0", _sha256(fake_accel_wheel)),
    )

    # ---- 6. Update checks read the new provenance: never
    # PROVENANCE_UNKNOWN --------------------------------------------------
    update = service.check_product_update(
        "zewitness",
        resolver=lambda owner, repo, ref: SHA_A,
    )
    assert update.status is not UpdateStatus.PROVENANCE_UNKNOWN
    assert update.status is UpdateStatus.UP_TO_DATE
    assert update.installed_commit_sha == SHA_A

    # ---- 7. A THIRD product install rebuilds the full state from the
    # NEW provenance AND preserves the accelerated closure (ZA-M1-3A.3a).
    # The REAL service path runs (no fake DeploymentResult): the active
    # slot carries a validated accelerated runtime, so the candidate is
    # rebuilt with the SAME accelerated variant — never a CPU downgrade.
    keep_calls: list[tuple[str, str]] = []

    def _fake_keep(product_id, provenance, *, fetcher, work_root,
                   progress_callback=None):
        keep_calls.append((product_id, provenance.commit_sha))
        return _ppa(
            product_id,
            witness_v1 if product_id == "zewitness" else witness_second,
            repo="ZeWitness" if product_id == "zewitness" else "ZeWitness2",
            version=provenance.version,
            commit_sha=provenance.commit_sha,
        )

    def _fake_target(product_id, policy, *, resolver, fetcher, work_root,
                     progress_callback=None):
        return _ppa(
            product_id, witness_third,
            repo="ZeThird", version="1.0.0", commit_sha=SHA_B,
        )

    monkeypatch.setattr(service, "_prepare_keep_product_artifact", _fake_keep)
    monkeypatch.setattr(
        service, "_prepare_target_product_artifact", _fake_target
    )

    active_before_third = runtime.status().active_slot_id
    assert active_before_third == final.active_slot_id  # GPU slot from step 3

    third_result = service.install_product(
        "zethird",
        resolver=lambda owner, repo, ref: SHA_B,
        fetcher=lambda owner, repo, sha: b"",
        work_root=tmp_path / "third-work",
        dependency_wheelhouse=_empty_wheelhouse(tmp_path),
        accelerated_acquirer=_SingleWheelAcquirer(fake_accel_wheel),
    )
    assert third_result.success is True, (
        f"third install failed: {third_result.reason}"
    )

    # KEEP = A+B re-materialized at the EXACT SHAs carried by the NEW
    # slot's provenance (never re-resolved, never invented).
    assert keep_calls == [("zewitness", SHA_A), ("zewitness2", SHA_B)]
    assert keep_calls[0][1] == active_prov["zewitness"].commit_sha
    assert keep_calls[1][1] == active_prov["zewitness2"].commit_sha

    # ---- rt-C is REAL: a genuinely new active slot ---------------------
    rt_c = third_result.active_slot_id
    assert rt_c is not None
    assert runtime.status().active_slot_id == rt_c
    assert rt_c != active_before_third
    assert rt_c != active_before

    # ---- provenance active = {A, B, third} with SHAs A/B preserved -----
    third_prov = dict(service.active_provenance())
    assert set(third_prov) == {"zewitness", "zewitness2", "zethird"}
    assert third_prov["zewitness"].commit_sha == SHA_A
    assert third_prov["zewitness2"].commit_sha == SHA_B

    # ---- accelerated metadata keyed on the NEW slot, same variant ------
    meta_c = metadata_store.load_slot(rt_c)
    assert meta_c is not None
    assert meta_c.backend == "NVIDIA_CUDA"
    assert meta_c.variants == (
        ("fake-accel", "1.0.0", _sha256(fake_accel_wheel)),
    )

    # ---- installed lock for rt-C contains fake-accel + A/B/third -------
    lock_c = installed_store.load_slot(rt_c)
    assert lock_c is not None
    assert "fake-accel" in lock_c.dependencies
    accel_dep_c = lock_c.dependencies["fake-accel"]
    assert accel_dep_c.version == "1.0.0"
    assert accel_dep_c.primary is False
    assert "zealfie-witness" in accel_dep_c.required_by
    for dist in ("zealfie-witness", "zealfie-witness2", "zealfie-third"):
        assert dist in lock_c.dependencies, dist

    # ---- C: the active slot's accelerated runtime is still validated --
    assert service.acceleration_runtime_ready() is True
    ready_rec = service.get_acceleration_recommendation(_caps())
    assert ready_rec.status is RecommendationStatus.ALREADY_READY
    assert ready_rec.backend == "NVIDIA_CUDA"

    # ---- selection now includes the third product ----------------------
    assert store.selected_product_ids == (
        "zethird", "zewitness", "zewitness2",
    )

    # ---- the NEW slot really carries the accelerated wheel -------------
    new_python = _slot_python(layout.slot_path(rt_c))
    probe = probe_runtime_distribution(str(new_python), "fake-accel")
    assert probe["installed"] is True
    assert probe["version"] == "1.0.0"


# ---------------------------------------------------------------------------
# D.8 — failure / cancellation: the OLD state stays authoritative
# ---------------------------------------------------------------------------


def _other_slot_ids(layout: RuntimeLayout, active: str) -> list[str]:
    if not layout.slots.exists():
        return []
    return sorted(p.name for p in layout.slots.iterdir() if p.name != active)


def _assert_no_new_slot_records(
    layout: RuntimeLayout,
    provenance_store: ProductProvenanceStore,
    installed_store: InstalledLockStore,
    active: str,
) -> None:
    for sid in _other_slot_ids(layout, active):
        assert provenance_store.load_slot(sid) == {}, sid
        assert installed_store.load_slot(sid) is None, sid


@pytest.mark.zealfie_slow
def test_accelerated_slot_continuity_gate_failure_keeps_old_authority(
    tmp_path, witness_v1, witness_second, fake_accel_wheel,
):
    """A gate failure AFTER the candidate slot was created must leave the
    old slot's provenance/lock authoritative and must never record
    provenance/lock for any other slot."""
    service, runtime, layout, store, active_before = _make_ready_service(
        tmp_path, witness_v1, witness_second
    )
    prov_before = dict(service.active_provenance())
    keeps = tuple(
        _keep_from_provenance(prov_before[pid]) for pid in sorted(prov_before)
    )
    plan = _accel_plan(active_before, keep=keeps)

    metadata_store = AcceleratedSlotMetadataStore(layout)
    provenance_store = ProductProvenanceStore(layout)
    installed_store = InstalledLockStore(layout)
    prov_slot_before = provenance_store.load_slot(active_before)
    lock_slot_before = installed_store.load_slot(active_before)

    class _FailingGate:
        def check(self, candidate_python: str, plan):
            return "synthetic backend refused"

    result = service.install_accelerated_runtime(
        plan=plan,
        capabilities=_caps(),
        recommendation=_recommendation(),
        acquirer=_SingleWheelAcquirer(fake_accel_wheel),
        gate=_FailingGate(),
        metadata_store=metadata_store,
        full_state_provider=lambda: [
            _ppa("zewitness", witness_v1, repo="ZeWitness",
                 version="0.0.1", commit_sha=SHA_A),
            _ppa("zewitness2", witness_second, repo="ZeWitness2",
                 version="0.1.0", commit_sha=SHA_B),
        ],
        dependency_wheelhouse=_empty_wheelhouse(tmp_path),
        work_root=tmp_path / "accel-work",
    )

    assert result.success is False
    assert result.cancelled is False
    assert result.phase is AcceleratedDeploymentPhase.GATE
    assert result.old_runtime_preserved is True
    assert runtime.status().active_slot_id == active_before
    # The old slot keeps its records; no other slot gained any.
    assert provenance_store.load_slot(active_before) == prov_slot_before
    assert installed_store.load_slot(active_before) == lock_slot_before
    _assert_no_new_slot_records(
        layout, provenance_store, installed_store, active_before
    )
    # The metadata record runs strictly after the gate: none written.
    assert not metadata_store.path.exists()


@pytest.mark.zealfie_slow
def test_accelerated_slot_continuity_cancellation_keeps_old_authority(
    tmp_path, witness_v1, witness_second, fake_accel_wheel,
):
    """Cancellation raised mid-deployment leaves the old slot's
    provenance/lock authoritative; no partial new-slot write exists."""
    service, runtime, layout, store, active_before = _make_ready_service(
        tmp_path, witness_v1, witness_second
    )
    prov_before = dict(service.active_provenance())
    keeps = tuple(
        _keep_from_provenance(prov_before[pid]) for pid in sorted(prov_before)
    )
    plan = _accel_plan(active_before, keep=keeps)

    provenance_store = ProductProvenanceStore(layout)
    installed_store = InstalledLockStore(layout)
    prov_slot_before = provenance_store.load_slot(active_before)
    lock_slot_before = installed_store.load_slot(active_before)

    calls = {"n": 0}

    def cancel_nth():
        calls["n"] += 1
        if calls["n"] >= 3:  # 1: service, 2: engine ACQUIRE, 3: BUILD
            raise CooperativeCancellationError("cancelled mid-flight")

    result = service.install_accelerated_runtime(
        plan=plan,
        capabilities=_caps(),
        recommendation=_recommendation(),
        acquirer=_SingleWheelAcquirer(fake_accel_wheel),
        full_state_provider=lambda: [
            _ppa("zewitness", witness_v1, repo="ZeWitness",
                 version="0.0.1", commit_sha=SHA_A),
            _ppa("zewitness2", witness_second, repo="ZeWitness2",
                 version="0.1.0", commit_sha=SHA_B),
        ],
        dependency_wheelhouse=_empty_wheelhouse(tmp_path),
        cancel_check=cancel_nth,
        work_root=tmp_path / "accel-work",
    )

    assert result.success is False
    assert result.cancelled is True
    assert result.old_runtime_preserved is True
    assert runtime.status().active_slot_id == active_before
    assert provenance_store.load_slot(active_before) == prov_slot_before
    assert installed_store.load_slot(active_before) == lock_slot_before
    _assert_no_new_slot_records(
        layout, provenance_store, installed_store, active_before
    )


# ---------------------------------------------------------------------------
# D.6 (fast, non-slow): update status never PROVENANCE_UNKNOWN after
# continuity — covered in the full flow; here the pure check uses the
# service against a real provenance store (no runtime work).
# ---------------------------------------------------------------------------


def test_update_check_reads_new_slot_provenance(tmp_path):
    """A product whose provenance lives under the ACTIVE slot never
    reports PROVENANCE_UNKNOWN (core check, no GPU work)."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    from zealfie.runtime.state import save_active_state

    save_active_state(layout.active_pointer, "rt-new", None)
    provenance_store = ProductProvenanceStore(layout)
    provenance_store.record(
        "rt-new",
        [
            ProductProvenance(
                product_id="zewitness",
                version="0.0.1",
                source_owner="tinystork",
                source_repo="ZeWitness",
                requested_ref="main",
                commit_sha=SHA_A,
                wheel_sha256="f" * 64,
            )
        ],
    )
    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=SharedRuntime(layout=layout),
        selection_store=SelectionStore(path=tmp_path / "desired-products.toml"),
        policy_store=ProductPolicyStore(path=tmp_path / "policy.toml"),
        host=_host(),
    )
    update = service.check_product_update(
        "zewitness",
        resolver=lambda owner, repo, ref: SHA_A,
    )
    assert update.status is UpdateStatus.UP_TO_DATE
    assert update.installed_commit_sha == SHA_A
