"""ZA-M1-3A.3a — GPU continuity across product transactions.

Targeted regression tests for the preservation path: when the ACTIVE
slot carries a validated accelerated runtime, an ordinary product
install/update must rebuild the candidate runtime with the SAME
accelerated closure (never a CPU-only downgrade).  These tests exercise
the REAL ``install_product`` / ``install_prepared_product_deployment``
path (no fake ``DeploymentResult``), with a synthetic accelerated
acquirer injected through the new ``accelerated_acquirer`` kwarg.

Scenarios:

1. accelerated -> UPDATE of an already-active product (version bump)
   -> the accelerated variant is preserved (same variant, real new slot).
2. accelerated -> install of a third product -> continuity (real new
   slot; the lock carries the GPU closure; metadata keyed on the NEW slot).
3. accelerated metadata corrupt/missing -> product install falls back to
   the plain CPU path (no GPU invented; the old slot stays intact).
4. product transaction failure (accelerated acquirer raises) -> the old
   GPU-active slot stays authoritative.

Hermetic: synthetic catalog, fixture-built wheels, injected acquirer —
the same harness style as ``tests/test_accelerated_slot_continuity.py``
(the full-flow scenario lives there; these are the focused variants).
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

import pytest

from zealfie.acceleration import (
    AcceleratedDeploymentPhase,
    AcceleratedDeploymentPlan,
    AcceleratedPlanStatus,
    AcceleratedVariant,
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
    PreparedProductArtifact,
    ProductCatalog,
    ProductDescriptor,
    SelectionStore,
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
SHA_C = "c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d0"
_EP_A = (EntryPointContract("console_scripts", "zewitness"),)
_EP_B = (EntryPointContract("console_scripts", "zewitness2"),)


# ---------------------------------------------------------------------------
# Synthetic catalog / host / plan helpers (mirror test_accelerated_slot_continuity)
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


class _FailingAcquirer:
    """Synthetic acquirer that always raises (transaction failure)."""

    def acquire(self, plan, work_root, *, cancel_check=None):
        raise RuntimeError("synthetic accelerated acquire failure")


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


def _accelerated_install(
    service: ZeAlfieService,
    tmp_path: Path,
    fake_accel_wheel: Path,
    witness_v1: Path,
    witness_second: Path,
    active_before: str,
) -> AcceleratedDeploymentPlan:
    """Run the accelerated GPU install and return the used plan."""
    prov_before = dict(service.active_provenance())
    keeps = tuple(
        _keep_from_provenance(prov_before[pid]) for pid in sorted(prov_before)
    )
    plan = _accel_plan(active_before, keep=keeps)
    result = service.install_accelerated_runtime(
        plan=plan,
        capabilities=_caps(),
        recommendation=_recommendation(),
        acquirer=_SingleWheelAcquirer(fake_accel_wheel),
        gate=None,
        metadata_store=None,
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
    return plan


# ---------------------------------------------------------------------------
# 1. Accelerated -> UPDATE of an already-active product (version bump)
# ---------------------------------------------------------------------------


@pytest.mark.zealfie_slow
def test_gpu_continuity_update_product(
    tmp_path, witness_v1, witness_second, witness_v2, fake_accel_wheel,
    monkeypatch,
):
    monkeypatch.setattr(
        "zealfie.acceleration.deployment.get_backend_compute_probe",
        lambda backend: None,
    )
    service, runtime, layout, store, active_before = _make_ready_service(
        tmp_path, witness_v1, witness_second
    )
    _accelerated_install(
        service, tmp_path, fake_accel_wheel, witness_v1, witness_second,
        active_before,
    )
    gpu_slot = runtime.status().active_slot_id
    assert gpu_slot is not None and gpu_slot != active_before

    metadata_store = AcceleratedSlotMetadataStore(layout)
    provenance_store = ProductProvenanceStore(layout)
    installed_store = InstalledLockStore(layout)
    prov_gpu = provenance_store.load_slot(gpu_slot)
    lock_gpu = installed_store.load_slot(gpu_slot)

    keep_calls: list[tuple[str, str]] = []

    def _fake_keep(product_id, provenance, *, fetcher, work_root,
                   progress_callback=None):
        keep_calls.append((product_id, provenance.commit_sha))
        return _ppa(
            product_id,
            witness_second,  # only zewitness2 is KEEP here
            repo="ZeWitness2",
            version=provenance.version,
            commit_sha=provenance.commit_sha,
        )

    def _fake_target(product_id, policy, *, resolver, fetcher, work_root,
                     progress_callback=None):
        return _ppa(
            product_id, witness_v2,
            repo="ZeWitness", version="0.0.2", commit_sha=SHA_C,
        )

    monkeypatch.setattr(service, "_prepare_keep_product_artifact", _fake_keep)
    monkeypatch.setattr(
        service, "_prepare_target_product_artifact", _fake_target
    )

    update_result = service.install_product(
        "zewitness",
        resolver=lambda owner, repo, ref: SHA_C,
        fetcher=lambda owner, repo, sha: b"",
        work_root=tmp_path / "update-work",
        dependency_wheelhouse=_empty_wheelhouse(tmp_path),
        accelerated_acquirer=_SingleWheelAcquirer(fake_accel_wheel),
    )
    assert update_result.success is True, (
        f"update failed: {update_result.reason}"
    )

    rt_c = update_result.active_slot_id
    assert rt_c is not None
    assert runtime.status().active_slot_id == rt_c
    assert rt_c != gpu_slot

    # KEEP = the OTHER product (zewitness2) at its exact SHA; the target
    # (zewitness) is the update.
    assert keep_calls == [("zewitness2", SHA_B)]

    # Provenance: zewitness bumped to v0.0.2/SHA_C, zewitness2 unchanged.
    prov_c = dict(service.active_provenance())
    assert set(prov_c) == {"zewitness", "zewitness2"}
    assert prov_c["zewitness"].version == "0.0.2"
    assert prov_c["zewitness"].commit_sha == SHA_C
    assert prov_c["zewitness2"].commit_sha == SHA_B

    # The accelerated closure is preserved: same variant, keyed on rt-C.
    meta_c = metadata_store.load_slot(rt_c)
    assert meta_c is not None
    assert meta_c.backend == "NVIDIA_CUDA"
    assert meta_c.variants == (
        ("fake-accel", "1.0.0", _sha256(fake_accel_wheel)),
    )

    # The lock for rt-C carries the GPU closure + products.
    lock_c = installed_store.load_slot(rt_c)
    assert lock_c is not None
    assert "fake-accel" in lock_c.dependencies
    assert lock_c.dependencies["fake-accel"].version == "1.0.0"
    assert lock_c.dependencies["fake-accel"].primary is False
    for dist in ("zealfie-witness", "zealfie-witness2"):
        assert dist in lock_c.dependencies, dist

    assert service.acceleration_runtime_ready() is True
    new_python = _slot_python(layout.slot_path(rt_c))
    probe = probe_runtime_distribution(str(new_python), "fake-accel")
    assert probe["installed"] is True
    assert probe["version"] == "1.0.0"

    # The old GPU slot's records stay intact (rollback target).
    assert provenance_store.load_slot(gpu_slot) == prov_gpu
    assert installed_store.load_slot(gpu_slot) == lock_gpu


# ---------------------------------------------------------------------------
# 2. Accelerated -> install a third product -> continuity (focused)
# ---------------------------------------------------------------------------


@pytest.mark.zealfie_slow
def test_gpu_continuity_install_third_product(
    tmp_path, witness_v1, witness_second, witness_third, fake_accel_wheel,
    monkeypatch,
):
    monkeypatch.setattr(
        "zealfie.acceleration.deployment.get_backend_compute_probe",
        lambda backend: None,
    )
    service, runtime, layout, store, active_before = _make_ready_service(
        tmp_path, witness_v1, witness_second
    )
    _accelerated_install(
        service, tmp_path, fake_accel_wheel, witness_v1, witness_second,
        active_before,
    )
    gpu_slot = runtime.status().active_slot_id
    assert gpu_slot is not None and gpu_slot != active_before

    metadata_store = AcceleratedSlotMetadataStore(layout)
    provenance_store = ProductProvenanceStore(layout)
    installed_store = InstalledLockStore(layout)

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

    rt_c = third_result.active_slot_id
    assert rt_c is not None
    assert runtime.status().active_slot_id == rt_c
    assert rt_c != gpu_slot
    assert rt_c != active_before

    assert keep_calls == [("zewitness", SHA_A), ("zewitness2", SHA_B)]

    # metadata keyed on the NEW slot (scenario 6), same validated variant.
    meta_c = metadata_store.load_slot(rt_c)
    assert meta_c is not None
    assert meta_c.backend == "NVIDIA_CUDA"
    assert meta_c.variants == (
        ("fake-accel", "1.0.0", _sha256(fake_accel_wheel)),
    )
    # The OLD GPU slot's metadata is not overwritten.
    meta_gpu = metadata_store.load_slot(gpu_slot)
    assert meta_gpu is not None
    assert meta_gpu.backend == "NVIDIA_CUDA"

    # lock for rt-C carries the GPU closure + A/B/third (scenario 5).
    lock_c = installed_store.load_slot(rt_c)
    assert lock_c is not None
    assert "fake-accel" in lock_c.dependencies
    accel_dep = lock_c.dependencies["fake-accel"]
    assert accel_dep.version == "1.0.0"
    assert accel_dep.primary is False
    assert "zealfie-witness" in accel_dep.required_by
    for dist in ("zealfie-witness", "zealfie-witness2", "zealfie-third"):
        assert dist in lock_c.dependencies, dist

    assert service.acceleration_runtime_ready() is True
    new_python = _slot_python(layout.slot_path(rt_c))
    probe = probe_runtime_distribution(str(new_python), "fake-accel")
    assert probe["installed"] is True
    assert probe["version"] == "1.0.0"


# ---------------------------------------------------------------------------
# 3. Corrupt/missing accelerated metadata -> CPU path (no GPU invented)
# ---------------------------------------------------------------------------


@pytest.mark.zealfie_slow
def test_gpu_continuity_missing_metadata_cpu_path(
    tmp_path, witness_v1, witness_second, witness_third, fake_accel_wheel,
    monkeypatch,
):
    monkeypatch.setattr(
        "zealfie.acceleration.deployment.get_backend_compute_probe",
        lambda backend: None,
    )
    service, runtime, layout, store, active_before = _make_ready_service(
        tmp_path, witness_v1, witness_second
    )
    _accelerated_install(
        service, tmp_path, fake_accel_wheel, witness_v1, witness_second,
        active_before,
    )
    gpu_slot = runtime.status().active_slot_id
    assert gpu_slot is not None and gpu_slot != active_before
    assert service.acceleration_runtime_ready() is True

    metadata_store = AcceleratedSlotMetadataStore(layout)
    provenance_store = ProductProvenanceStore(layout)
    installed_store = InstalledLockStore(layout)
    prov_gpu = provenance_store.load_slot(gpu_slot)
    lock_gpu = installed_store.load_slot(gpu_slot)

    # Corrupt the accelerated metadata: readiness must fail closed.
    metadata_store.path.write_text("{ not valid json", encoding="utf-8")
    assert service.acceleration_runtime_ready() is False

    def _fake_keep(product_id, provenance, *, fetcher, work_root,
                   progress_callback=None):
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

    third_result = service.install_product(
        "zethird",
        resolver=lambda owner, repo, ref: SHA_B,
        fetcher=lambda owner, repo, sha: b"",
        work_root=tmp_path / "third-work",
        dependency_wheelhouse=_empty_wheelhouse(tmp_path),
    )
    assert third_result.success is True, (
        f"CPU third install failed: {third_result.reason}"
    )

    rt_c = third_result.active_slot_id
    assert rt_c is not None
    assert runtime.status().active_slot_id == rt_c
    assert rt_c != gpu_slot

    # No GPU invented: readiness stays False, no fake-accel in the lock.
    assert service.acceleration_runtime_ready() is False
    assert metadata_store.load_slot(rt_c) is None
    lock_c = installed_store.load_slot(rt_c)
    assert lock_c is not None
    assert "fake-accel" not in lock_c.dependencies
    for dist in ("zealfie-witness", "zealfie-witness2", "zealfie-third"):
        assert dist in lock_c.dependencies, dist

    # The old GPU slot's provenance/lock stay intact.
    assert provenance_store.load_slot(gpu_slot) == prov_gpu
    assert installed_store.load_slot(gpu_slot) == lock_gpu


# ---------------------------------------------------------------------------
# 4. Product transaction failure -> the old GPU-active slot stays authority
# ---------------------------------------------------------------------------


@pytest.mark.zealfie_slow
def test_gpu_continuity_acquire_failure_keeps_old_authority(
    tmp_path, witness_v1, witness_second, witness_third, fake_accel_wheel,
    monkeypatch,
):
    monkeypatch.setattr(
        "zealfie.acceleration.deployment.get_backend_compute_probe",
        lambda backend: None,
    )
    service, runtime, layout, store, active_before = _make_ready_service(
        tmp_path, witness_v1, witness_second
    )
    _accelerated_install(
        service, tmp_path, fake_accel_wheel, witness_v1, witness_second,
        active_before,
    )
    gpu_slot = runtime.status().active_slot_id
    assert gpu_slot is not None and gpu_slot != active_before
    assert service.acceleration_runtime_ready() is True

    metadata_store = AcceleratedSlotMetadataStore(layout)
    provenance_store = ProductProvenanceStore(layout)
    installed_store = InstalledLockStore(layout)
    prov_gpu = provenance_store.load_slot(gpu_slot)
    lock_gpu = installed_store.load_slot(gpu_slot)
    meta_gpu = metadata_store.load_slot(gpu_slot)

    def _fake_keep(product_id, provenance, *, fetcher, work_root,
                   progress_callback=None):
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

    with pytest.raises(RuntimeError):
        service.install_product(
            "zethird",
            resolver=lambda owner, repo, ref: SHA_B,
            fetcher=lambda owner, repo, sha: b"",
            work_root=tmp_path / "third-work",
            dependency_wheelhouse=_empty_wheelhouse(tmp_path),
            accelerated_acquirer=_FailingAcquirer(),
        )

    # The old GPU-active slot stays authoritative; nothing changed.
    assert runtime.status().active_slot_id == gpu_slot
    assert provenance_store.load_slot(gpu_slot) == prov_gpu
    assert installed_store.load_slot(gpu_slot) == lock_gpu
    assert metadata_store.load_slot(gpu_slot) == meta_gpu
    assert service.acceleration_runtime_ready() is True
