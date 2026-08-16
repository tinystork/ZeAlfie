"""Service-layer transactional accelerated deployment tests (M1-2I).

A synthetic FULL witness of ``ZeAlfieService.install_accelerated_runtime``:

1. a real shared runtime is built through the real service install path
   (witness product, selection + provenance + installed lock persisted);
2. a synthetic ``PLAN_READY`` accelerated plan adds the synthetic
   ``fake-accel`` distribution (built from ``tests/fixtures/fake_accel``);
3. the base full-state plan is materialized through the injectable
   ``full_state_provider`` (local verified artifacts — hermetic,
   offline) and the accelerated artifacts through a synthetic
   directory-based acquirer;
4. the service runs acquire -> resolve -> build -> validate -> gate ->
   persist -> activate and the assertions verify: success, phase
   COMPLETED, old runtime preserved and still usable, active slot
   switched, ``fake-accel`` installed at the planned version in the new
   slot, accelerated-metadata.json recorded with backend + variants,
   product provenance / installed-lock / selection UNCHANGED, and KEEP
   exactness (no version/commit/wheel drift).

Then failure injections, each leaving the previously active runtime
unchanged: non-``PLAN_READY`` plans, the fail-closed default acquirer,
injected acquisition failures, specifier violations (RESOLVE), gate
failures (byte-compare proves no in-place mutation), and cooperative
cancellation.

``fake-accel`` is a synthetic pure-Python fixture name — ZeAlfie never
selects a concrete GPU framework.  Tests that create real venvs are
marked ``zealfie_slow``; the rest are hermetic FAST tests.
"""

from __future__ import annotations

import hashlib
import io
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

from zealfie.acceleration import (
    AcceleratedAcquisitionError,
    AcceleratedDeploymentPhase,
    AcceleratedDeploymentPlan,
    AcceleratedPlanStatus,
    AcceleratedVariant,
    AcceleratedVariantCatalog,
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
    ZeAlfieService,
)
from zealfie.building import inspect_wheel
from zealfie.components.model import EntryPointContract
from zealfie.host import recommend
from zealfie.host.models import (
    AccelerationRecommendation,
    CapabilityStatus,
    GpuInfo,
    GpuKind,
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
from zealfie.runtime.state import save_active_state
from zealfie.runtime.probe import probe_runtime_distribution
from zealfie.runtime.provenance import (
    ProductProvenance,
    ProductProvenanceStore,
)
from zealfie.sources import RemoteSource, ResolvedSource
from zealfie.sources.acquisition import AcquisitionError

WITNESS_SHA = "d4a0f1e2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8"
_EP = (EntryPointContract("console_scripts", "zewitness"),)


@pytest.fixture(scope="session")
def fake_accel_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    # Build fake-accel 1.0.0 once per session.
    from zealfie.building import build_wheel

    fixture_dir = Path(__file__).resolve().parent / "fixtures" / "fake_accel"
    output = tmp_path_factory.mktemp("shared-fake-accel-service")
    return build_wheel(fixture_dir, output_dir=output)


# ---------------------------------------------------------------------------
# Synthetic catalog / host / plan helpers
# ---------------------------------------------------------------------------


def _catalog(*, acceleration: bool = True) -> ProductCatalog:
    accel = None
    if acceleration:
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
            launch_entry_points=_EP,
            required_extras=(),
            remote_source=RemoteSource(
                owner="tinystork",
                repo="ZeWitness",
                ref="main",
            ),
            acceleration=accel,
        ),
    ))


def _host() -> HostTarget:
    return HostTarget(
        python_tag="py312",
        abi_tag="cp312",
        platform_tag="linux_x86_64",
    )


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


def _recommendation() -> AccelerationRecommendation:
    return AccelerationRecommendation(
        status=RecommendationStatus.OFFER_SETUP,
        backend="NVIDIA_CUDA",
        reason_code=HostReasonCode.ACCELERATION_OFFER_SETUP,
        reason="supported accelerator detected; setup offered",
    )


def _variant_catalog() -> AcceleratedVariantCatalog:
    """Synthetic variant catalog: fake-accel 1.0.0 for NVIDIA_CUDA on
    the service host platform tag."""
    return AcceleratedVariantCatalog((
        AcceleratedVariant(
            distribution="fake-accel",
            version="1.0.0",
            backend="NVIDIA_CUDA",
            platform="linux_x86_64",
        ),
    ))


def _driver_unavailable_caps() -> HostCapabilities:
    """SUPPORTED-looking host with an NVIDIA GPU whose driver is gone."""
    return _caps(gpus=(
        GpuInfo(
            vendor="NVIDIA",
            model="Tesla T4",
            kind=GpuKind.DISCRETE,
            hardware_present=True,
            driver_status=CapabilityStatus.UNAVAILABLE,
            driver_version=None,
            driver_reason_code=HostReasonCode.NVIDIA_DRIVER_UNAVAILABLE,
            driver_reason="nvidia driver not available",
        ),
    ))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _witness_ppa(
    wheel_path: Path,
    *,
    version: str = "0.0.1",
    commit_sha: str = WITNESS_SHA,
) -> PreparedProductArtifact:
    info = inspect_wheel(wheel_path)
    size = wheel_path.stat().st_size
    resolved = ResolvedSource(
        source=RemoteSource(owner="tinystork", repo="ZeWitness", ref="main"),
        commit_sha=commit_sha,
    )
    verified = VerifiedArtifact(
        component_id="zewitness",
        version=version,
        path=wheel_path,
        size=size,
        sha256=_sha256(wheel_path),
        distribution_name=info.distribution_name,
        wheel_version=info.version,
    )
    return PreparedProductArtifact(
        product_id="zewitness",
        component_id="zewitness",
        resolved_source=resolved,
        wheel_path=wheel_path,
        verified_artifact=verified,
    )


def _hardware() -> HardwareCompatibility:
    return HardwareCompatibility(
        status=HardwareCompatibilityStatus.SUPPORTED,
        reason_code=HardwareCompatibilityReasonCode.COMPATIBLE.value,
        reason="compatible",
        products_concerned=("zewitness",),
    )


def _accel_plan(
    source_active_slot_id: str | None,
    *,
    status: AcceleratedPlanStatus = AcceleratedPlanStatus.PLAN_READY,
    keep: tuple[PlannedKeepProduct, ...] = (),
    specifier: str = "==1.0.0",
):
    """Synthetic accelerated plan for fake-accel."""
    entry = PlannedAcceleratedDependency(
        distribution="fake-accel",
        specifier=specifier,
        extras=(),
        declaring_products=("zewitness",),
        variant=AcceleratedVariant(
            distribution="fake-accel",
            version="1.0.0",
            backend="NVIDIA_CUDA",
        ),
        variant_status=VariantStatus.SELECTED,
    )
    ready = status is AcceleratedPlanStatus.PLAN_READY
    return AcceleratedDeploymentPlan(
        status=status,
        hardware=_hardware(),
        backend="NVIDIA_CUDA" if ready else None,
        products_concerned=("zewitness",),
        keep_products=keep,
        added_requirements=(entry,) if ready else (),
        source_runtime_state="READY" if source_active_slot_id else "ABSENT",
        source_active_slot_id=source_active_slot_id,
        source_previous_slot_id=None,
        target_runtime=(
            "new shared runtime slot with accelerated NVIDIA_CUDA closure"
            if ready
            else "no new runtime required"
        ),
        blocked=not ready,
        blocked_reason=None if ready else "synthetic blocked plan",
        closure_impact=(),
    )


class _SingleWheelAcquirer:
    """Synthetic acquirer: copies the fake wheel into ``work_root`` and
    verifies size + sha256.  Never touches the network."""

    def __init__(self, wheel_path: Path, *, version: str = "1.0.0",
                 honour_cancel: bool = True) -> None:
        self._wheel_path = wheel_path
        self._version = version
        self._honour_cancel = honour_cancel
        self.calls = 0

    def acquire(self, plan, work_root, *, cancel_check=None):
        self.calls += 1
        acquired = []
        for entry in plan.added_requirements:
            if self._honour_cancel and cancel_check is not None:
                cancel_check()
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
    """Acquirer that always raises (synthetic acquisition failure)."""

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls = 0

    def acquire(self, plan, work_root, *, cancel_check=None):
        self.calls += 1
        raise self._error


class _SpyAcquirer:
    """Acquirer that records invocation and returns nothing (must never
    be called in no-acquisition tests)."""

    def __init__(self) -> None:
        self.calls = 0

    def acquire(self, plan, work_root, *, cancel_check=None):
        self.calls += 1
        raise AssertionError("acquirer must not be called")


def _slot_python(slot_dir: Path) -> Path:
    if sys.platform == "win32":
        return slot_dir / "Scripts" / "python.exe"
    return slot_dir / "bin" / "python"


def _assert_slot_usable(layout: RuntimeLayout, slot_id: str, dist: str) -> None:
    slot_path = layout.slot_path(slot_id)
    assert slot_path.is_dir()
    python = _slot_python(slot_path)
    assert python.is_file()
    probe = probe_runtime_distribution(str(python), dist)
    assert probe["installed"] is True


def _snapshot_dir(root: Path) -> dict[str, str]:
    """Byte-level snapshot: relative path -> sha256 of every file."""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = _sha256(path)
    return out


def _empty_wheelhouse(tmp_path: Path) -> Path:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir(parents=True, exist_ok=True)
    return wheelhouse


# ---------------------------------------------------------------------------
# Slow-path base setup: a real READY runtime built through the service
# ---------------------------------------------------------------------------


def _make_ready_service(
    tmp_path: Path, witness_v1: Path,
) -> tuple[ZeAlfieService, SharedRuntime, RuntimeLayout, SelectionStore, Path]:
    """Install the witness product through the real service path and
    return ``(service, runtime, layout, selection_store, active_slot_id)``."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    runtime = SharedRuntime(layout=layout)
    store = SelectionStore(path=tmp_path / "desired-products.toml")
    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=runtime,
        selection_store=store,
        host=_host(),
    )
    ppa = _witness_ppa(witness_v1)
    result = service.install_prepared_product_deployment(
        [ppa],
        dependency_wheelhouse=_empty_wheelhouse(tmp_path),
    )
    assert result.success is True, f"base install failed: {result.reason}"
    active_id = runtime.status().active_slot_id
    assert active_id is not None
    _assert_slot_usable(layout, active_id, "zealfie-witness")
    return service, runtime, layout, store, active_id


# =============================================================================
# NOT_PLAN_READY — honest refusal, zero side effects (FAST)
# =============================================================================


def test_service_plan_none_no_accelerated_requirements(tmp_path):
    """TINYDEBIAN default: no product declares GPU requirements -> the
    service-built plan is NO_ACCELERATED_REQUIREMENTS and the method
    refuses without any acquisition or runtime work."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    runtime = SharedRuntime(layout=layout)
    service = ZeAlfieService(
        catalog=_catalog(acceleration=False),
        runtime=runtime,
        selection_store=SelectionStore(path=tmp_path / "desired-products.toml"),
        host=_host(),
        capability_collector=lambda: _caps(),
        recommender=lambda caps: _recommendation(),
    )
    acquirer = _SpyAcquirer()

    result = service.install_accelerated_runtime(
        acquirer=acquirer,
        work_root=tmp_path / "work",
    )

    assert result.success is False
    assert result.cancelled is False
    assert result.phase is AcceleratedDeploymentPhase.PREPARE
    assert result.old_runtime_preserved is True
    assert "NO_ACCELERATED_REQUIREMENTS" in (result.reason or "")
    assert "no product declares accelerated requirements" in (
        result.reason or ""
    )
    assert acquirer.calls == 0
    assert not layout.slots.exists()
    assert runtime.status().state.value == "ABSENT"


def test_service_plan_none_blocked_by_empty_variant_catalog(tmp_path):
    """With a declared accelerated requirement but the fail-closed empty
    default variant catalog, the service-built plan is BLOCKED and the
    method refuses without acquisition or runtime work."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    runtime = SharedRuntime(layout=layout)
    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=runtime,
        selection_store=SelectionStore(path=tmp_path / "desired-products.toml"),
        host=_host(),
        capability_collector=lambda: _caps(),
        recommender=lambda caps: _recommendation(),
    )
    acquirer = _SpyAcquirer()

    result = service.install_accelerated_runtime(
        acquirer=acquirer,
        work_root=tmp_path / "work",
    )

    assert result.success is False
    assert result.phase is AcceleratedDeploymentPhase.PREPARE
    assert "BLOCKED" in (result.reason or "")
    assert "no accelerated variant available" in (result.reason or "")
    assert acquirer.calls == 0
    assert not layout.slots.exists()


def test_service_blocked_plan_no_side_effects(tmp_path):
    """An explicit BLOCKED plan is refused before the base-state
    provider, acquisition, and any candidate slot creation."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    runtime = SharedRuntime(layout=layout)
    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=runtime,
        selection_store=SelectionStore(path=tmp_path / "desired-products.toml"),
        host=_host(),
    )
    blocked = _accel_plan(
        None, status=AcceleratedPlanStatus.BLOCKED,
    )
    acquirer = _SpyAcquirer()
    provider_calls: list[int] = []

    def provider():
        provider_calls.append(1)
        raise AssertionError("full_state_provider must not be called")

    result = service.install_accelerated_runtime(
        plan=blocked,
        acquirer=acquirer,
        full_state_provider=provider,
        work_root=tmp_path / "work",
    )

    assert result.success is False
    assert result.cancelled is False
    assert result.phase is AcceleratedDeploymentPhase.PREPARE
    assert "BLOCKED" in (result.reason or "")
    assert "synthetic blocked plan" in (result.reason or "")
    assert acquirer.calls == 0
    assert provider_calls == []
    assert not layout.slots.exists()
    assert runtime.status().state.value == "ABSENT"


# =============================================================================
# ACQUIRE failures (FAST — no candidate slot is ever created)
# =============================================================================


def test_service_default_acquirer_fail_closed(tmp_path, witness_v1):
    """The production default acquirer is the manifest-backed acquirer
    (real source, M1-2J Phase D): the synthetic fake-accel distribution
    has no manifest entry -> fail-closed MissingArtifact at ACQUIRE, no
    candidate slot, runtime untouched."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    runtime = SharedRuntime(layout=layout)
    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=runtime,
        selection_store=SelectionStore(path=tmp_path / "desired-products.toml"),
        host=_host(),
    )
    keep = PlannedKeepProduct(
        product_id="zewitness",
        version="0.0.1",
        commit_sha=WITNESS_SHA,
        wheel_sha256=_sha256(witness_v1),
        source="provenance",
    )
    plan = _accel_plan(None, keep=(keep,))

    result = service.install_accelerated_runtime(
        plan=plan,
        capabilities=_caps(),
        recommendation=_recommendation(),
        acquirer=None,  # -> manifest-backed service default
        full_state_provider=lambda: [_witness_ppa(witness_v1)],
        dependency_wheelhouse=_empty_wheelhouse(tmp_path),
        work_root=tmp_path / "work",
    )

    assert result.success is False
    assert result.cancelled is False
    assert result.phase is AcceleratedDeploymentPhase.ACQUIRE
    assert result.old_runtime_preserved is True
    # The manifest-backed default refuses the unknown synthetic
    # distribution fail-closed (MissingArtifact) — no network, no slot.
    assert "no accelerated artifact for distribution 'fake-accel'" in (
        result.reason or ""
    )
    assert not layout.slots.exists()
    assert runtime.status().state.value == "ABSENT"


def _keep_for_witness(witness_v1: Path) -> PlannedKeepProduct:
    return PlannedKeepProduct(
        product_id="zewitness",
        version="0.0.1",
        commit_sha=WITNESS_SHA,
        wheel_sha256=_sha256(witness_v1),
    )


def _service_with_active_provenance(tmp_path, *, witness_v1):
    """Service with active provenance for zewitness and no runtime work
    performed (PREPARE-phase failures never touch the runtime)."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    save_active_state(layout.active_pointer, "rt-abc123", None)
    provenance_store = ProductProvenanceStore(layout)
    provenance_store.record(
        "rt-abc123",
        [
            ProductProvenance(
                product_id="zewitness",
                version="0.0.1",
                source_owner="tinystork",
                source_repo="ZeWitness",
                requested_ref="main",
                commit_sha=WITNESS_SHA,
                wheel_sha256=_sha256(witness_v1),
            )
        ],
    )
    service = ZeAlfieService(
        catalog=_catalog(),
        selection_store=SelectionStore(path=tmp_path / "desired-products.toml"),
        provenance_store=provenance_store,
        host=_host(),
    )
    return service, layout


def test_keep_preparation_fetches_exact_40hex_sha_never_mutable_ref(
    tmp_path,
):
    """ZA-M1-2J.1: ``_prepare_keep_product_artifact`` re-acquires the
    product at the provenance's exact 40-hex commit SHA — the recording
    fetcher receives the SHA, NEVER the mutable ``requested_ref``
    (main/beta/any branch name).  No fallback to mutable refs exists."""
    fixtures = Path(__file__).resolve().parent / "fixtures"

    def _zip_fixture_source(fixture_name: str) -> bytes:
        source_dir = fixtures / fixture_name
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in sorted(source_dir.rglob("*")):
                if file_path.is_file():
                    rel = file_path.relative_to(source_dir)
                    if str(rel).startswith("build/"):
                        continue
                    zf.write(str(file_path), str(rel))
        return buf.getvalue()

    class _RecordingFetcher:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str]] = []

        def __call__(self, owner: str, repo: str, commit_sha: str) -> bytes:
            self.calls.append((owner, repo, commit_sha))
            return _zip_fixture_source("witness_component")

    service = ZeAlfieService(catalog=_catalog())
    fetcher = _RecordingFetcher()
    provenance = ProductProvenance(
        product_id="zewitness",
        version="0.0.1",
        source_owner="tinystork",
        source_repo="ZeWitness",
        requested_ref="main",
        commit_sha=WITNESS_SHA,
        wheel_sha256="f" * 64,
    )

    prepared = service._prepare_keep_product_artifact(
        "zewitness",
        provenance,
        fetcher=fetcher,
        work_root=tmp_path / "keep-work",
    )

    assert len(fetcher.calls) == 1
    owner, repo, sha = fetcher.calls[0]
    assert (owner, repo) == ("tinystork", "ZeWitness")
    assert sha == WITNESS_SHA
    assert sha == provenance.commit_sha
    assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)
    # NEVER the mutable ref: the requested_ref is provenance metadata only.
    assert sha != provenance.requested_ref
    assert provenance.requested_ref == "main"
    assert prepared.resolved_source.commit_sha == WITNESS_SHA
    assert prepared.resolved_source.source.ref == "main"
    assert prepared.verified_artifact.version == "0.0.1"


def test_service_no_fetcher_fails_closed_with_exact_error(
    tmp_path, witness_v1,
):
    """The exact production failure of the first real gpu-install
    (d29e758): with active provenance and no full_state_provider, a
    missing fetcher fails closed at PREPARE with "no artifact fetcher
    configured" and the active runtime stays byte-identical.  The
    CLI/GUI wiring now always supplies the fetcher; this guards the
    fail-closed contract itself."""
    service, layout = _service_with_active_provenance(
        tmp_path, witness_v1=witness_v1
    )
    pointer_before = layout.active_pointer.read_bytes()
    plan = _accel_plan("rt-abc123", keep=(_keep_for_witness(witness_v1),))

    result = service.install_accelerated_runtime(
        plan=plan,
        capabilities=_caps(),
        recommendation=_recommendation(),
        acquirer=_SpyAcquirer(),
        fetcher=None,  # the d29e758 wiring bug — no fetcher transmitted
        full_state_provider=None,
        dependency_wheelhouse=_empty_wheelhouse(tmp_path),
        work_root=tmp_path / "work",
    )

    assert result.success is False
    assert result.cancelled is False
    assert result.phase is AcceleratedDeploymentPhase.PREPARE
    assert "base runtime preparation failed" in (result.reason or "")
    assert "no artifact fetcher configured" in (result.reason or "")
    assert result.old_runtime_preserved is True
    # Zero mutation: pointer unchanged, no slots, no acquisition.
    assert layout.active_pointer.read_bytes() == pointer_before
    assert not (layout.slots.exists() and any(layout.slots.iterdir()))


def test_service_fetcher_failure_preserves_active_runtime(
    tmp_path, witness_v1,
):
    """A raising fetcher (KEEP re-acquisition transport failure) fails
    closed at PREPARE: honest result, active runtime untouched, and the
    fetcher was asked for the exact provenance SHA (never a branch)."""
    service, layout = _service_with_active_provenance(
        tmp_path, witness_v1=witness_v1
    )
    pointer_before = layout.active_pointer.read_bytes()

    class _RaisingFetcher:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str]] = []

        def __call__(self, owner: str, repo: str, commit_sha: str) -> bytes:
            self.calls.append((owner, repo, commit_sha))
            raise AcquisitionError("synthetic fetcher failure")

    fetcher = _RaisingFetcher()
    plan = _accel_plan("rt-abc123", keep=(_keep_for_witness(witness_v1),))

    result = service.install_accelerated_runtime(
        plan=plan,
        capabilities=_caps(),
        recommendation=_recommendation(),
        acquirer=_SpyAcquirer(),
        fetcher=fetcher,
        full_state_provider=None,
        dependency_wheelhouse=_empty_wheelhouse(tmp_path),
        work_root=tmp_path / "work",
    )

    assert result.success is False
    assert result.cancelled is False
    assert result.phase is AcceleratedDeploymentPhase.PREPARE
    assert "base runtime preparation failed" in (result.reason or "")
    assert "synthetic fetcher failure" in (result.reason or "")
    assert result.old_runtime_preserved is True
    # The KEEP path asked for the exact 40-hex provenance SHA.
    assert len(fetcher.calls) == 1
    owner, repo, sha = fetcher.calls[0]
    assert (owner, repo) == ("tinystork", "ZeWitness")
    assert sha == WITNESS_SHA
    assert len(sha) == 40
    # Zero mutation: pointer unchanged, no slots, acquisition never ran.
    assert layout.active_pointer.read_bytes() == pointer_before
    assert not (layout.slots.exists() and any(layout.slots.iterdir()))


def test_service_late_conflict_specifier_violation(
    tmp_path, witness_v1, fake_accel_wheel,
):
    """An acquired variant whose version violates the plan specifier is
    rejected at RESOLVE — before any candidate slot creation."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    runtime = SharedRuntime(layout=layout)
    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=runtime,
        selection_store=SelectionStore(path=tmp_path / "desired-products.toml"),
        host=_host(),
    )
    keep = PlannedKeepProduct(
        product_id="zewitness",
        version="0.0.1",
        commit_sha=WITNESS_SHA,
        wheel_sha256=_sha256(witness_v1),
        source="provenance",
    )
    plan = _accel_plan(None, keep=(keep,), specifier="==1.0.0")
    # The wheel on disk IS fake-accel 1.0.0 (sha/size valid), but the
    # acquired variant CLAIMS 9.9.9 — the specifier must reject it.
    wrong_version_acquirer = _SingleWheelAcquirer(
        fake_accel_wheel, version="9.9.9",
    )

    result = service.install_accelerated_runtime(
        plan=plan,
        capabilities=_caps(),
        recommendation=_recommendation(),
        acquirer=wrong_version_acquirer,
        full_state_provider=lambda: [_witness_ppa(witness_v1)],
        dependency_wheelhouse=_empty_wheelhouse(tmp_path),
        work_root=tmp_path / "work",
    )

    assert result.success is False
    assert result.cancelled is False
    assert result.phase is AcceleratedDeploymentPhase.RESOLVE
    assert "does not satisfy declared specifier" in (result.reason or "")
    assert result.old_runtime_preserved is True
    assert not layout.slots.exists()
    assert runtime.status().state.value == "ABSENT"


def test_service_cancellation_at_acquire(tmp_path, witness_v1):
    """Cooperative cancellation before acquisition aborts cleanly with
    cancelled=True and zero runtime mutation."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    runtime = SharedRuntime(layout=layout)
    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=runtime,
        selection_store=SelectionStore(path=tmp_path / "desired-products.toml"),
        host=_host(),
    )
    keep = PlannedKeepProduct(
        product_id="zewitness",
        version="0.0.1",
        commit_sha=WITNESS_SHA,
        wheel_sha256=_sha256(witness_v1),
        source="provenance",
    )
    plan = _accel_plan(None, keep=(keep,))
    acquirer = _SpyAcquirer()
    calls: list[int] = []

    def cancel():
        calls.append(1)
        raise CooperativeCancellationError("user cancelled")

    result = service.install_accelerated_runtime(
        plan=plan,
        capabilities=_caps(),
        recommendation=_recommendation(),
        acquirer=acquirer,
        full_state_provider=lambda: [_witness_ppa(witness_v1)],
        dependency_wheelhouse=_empty_wheelhouse(tmp_path),
        cancel_check=cancel,
        work_root=tmp_path / "work",
    )

    assert result.success is False
    assert result.cancelled is True
    assert result.phase is AcceleratedDeploymentPhase.ACQUIRE
    assert result.old_runtime_preserved is True
    assert calls == [1]
    assert acquirer.calls == 0
    assert not layout.slots.exists()


# =============================================================================
# Deploy-time hardware re-verification (late GPU conflict, FAST)
# =============================================================================


@pytest.mark.parametrize(
    "changed_caps",
    [
        pytest.param(_caps(partial=True), id="partial-evidence"),
        pytest.param(_driver_unavailable_caps(), id="driver-unavailable"),
    ],
)
def test_service_deploy_time_late_conflict_no_mutation(
    tmp_path, changed_caps,
):
    """A plan built with SUPPORTED capabilities is refused when the
    deploy-time re-verification observes changed hardware evidence:
    success=False, phase=PREPARE, honest late-conflict reason, and zero
    mutation (no candidate slot, no acquisition, no base preparation)."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    runtime = SharedRuntime(layout=layout)
    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=runtime,
        selection_store=SelectionStore(path=tmp_path / "desired-products.toml"),
        host=_host(),
        capability_collector=lambda: _caps(),
        recommender=recommend,
    )
    plan = service.build_accelerated_deployment_plan(
        capabilities=_caps(),
        recommendation=_recommendation(),
        variant_catalog=_variant_catalog(),
    )
    assert plan.status is AcceleratedPlanStatus.PLAN_READY
    assert plan.backend == "NVIDIA_CUDA"

    acquirer = _SpyAcquirer()
    provider_calls: list[int] = []

    def provider():
        provider_calls.append(1)
        raise AssertionError("full_state_provider must not be called")

    slots_before = (
        sorted(p.name for p in layout.slots.iterdir())
        if layout.slots.exists()
        else []
    )

    result = service.install_accelerated_runtime(
        plan=plan,
        capabilities=changed_caps,
        acquirer=acquirer,
        full_state_provider=provider,
        work_root=tmp_path / "work",
    )

    assert result.success is False
    assert result.cancelled is False
    assert result.phase is AcceleratedDeploymentPhase.PREPARE
    assert "late GPU compatibility conflict detected at deployment time" in (
        result.reason or ""
    )
    assert result.old_runtime_preserved is True
    assert acquirer.calls == 0
    assert provider_calls == []
    slots_after = (
        sorted(p.name for p in layout.slots.iterdir())
        if layout.slots.exists()
        else []
    )
    assert slots_after == slots_before
    assert runtime.status().state.value == "ABSENT"
    assert runtime.status().active_slot_id is None


def test_service_deploy_time_check_probe_counting(tmp_path, witness_v1):
    """A counting capability collector proves the deploy-time
    re-verification collects exactly once when capabilities are omitted
    and never when capabilities + recommendation are both provided."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    runtime = SharedRuntime(layout=layout)

    class _CountingCollector:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self):
            self.calls += 1
            return _caps()

    collector = _CountingCollector()
    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=runtime,
        selection_store=SelectionStore(path=tmp_path / "desired-products.toml"),
        host=_host(),
        capability_collector=collector,
        recommender=lambda caps: _recommendation(),
    )
    keep = PlannedKeepProduct(
        product_id="zewitness",
        version="0.0.1",
        commit_sha=WITNESS_SHA,
        wheel_sha256=_sha256(witness_v1),
        source="provenance",
    )
    plan = _accel_plan(None, keep=(keep,))

    # (1) Both provided -> no collection at all; the deploy-time check
    # passes and the flow reaches ACQUIRE (manifest-backed default
    # acquirer refuses the unknown synthetic distribution).
    result = service.install_accelerated_runtime(
        plan=plan,
        capabilities=_caps(),
        recommendation=_recommendation(),
        acquirer=None,
        full_state_provider=lambda: [_witness_ppa(witness_v1)],
        dependency_wheelhouse=_empty_wheelhouse(tmp_path),
        work_root=tmp_path / "work-provided",
    )
    assert collector.calls == 0
    assert result.success is False
    assert result.phase is AcceleratedDeploymentPhase.ACQUIRE
    assert "no accelerated artifact for distribution 'fake-accel'" in (
        result.reason or ""
    )

    # (2) Both omitted -> exactly one collection at the deploy-time
    # check, then the same ACQUIRE outcome.
    result = service.install_accelerated_runtime(
        plan=plan,
        acquirer=None,
        full_state_provider=lambda: [_witness_ppa(witness_v1)],
        dependency_wheelhouse=_empty_wheelhouse(tmp_path),
        work_root=tmp_path / "work-collected",
    )
    assert collector.calls == 1
    assert result.success is False
    assert result.phase is AcceleratedDeploymentPhase.ACQUIRE
    assert "no accelerated artifact for distribution 'fake-accel'" in (
        result.reason or ""
    )


@pytest.mark.parametrize(
    ("fresh_recommendation", "expected_fragment"),
    [
        pytest.param(
            lambda: AccelerationRecommendation(
                status=RecommendationStatus.NOT_APPLICABLE,
                backend="NVIDIA_CUDA",
                reason_code=HostReasonCode.ACCELERATION_NOT_APPLICABLE,
                reason="no supported accelerator hardware detected",
            ),
            "no supported accelerator hardware detected",
            id="not-applicable",
        ),
        pytest.param(
            lambda: AccelerationRecommendation(
                status=RecommendationStatus.OFFER_SETUP,
                backend="OTHER_BACKEND",
                reason_code=HostReasonCode.ACCELERATION_OFFER_SETUP,
                reason="synthetic backend drift",
            ),
            (
                "backend changed from 'NVIDIA_CUDA' at planning to "
                "'OTHER_BACKEND' at deployment"
            ),
            id="backend-mismatch",
        ),
    ],
)
def test_service_deploy_time_backend_coherence(
    tmp_path, fresh_recommendation, expected_fragment,
):
    """A fresh recommendation that no longer matches the plan (backend
    changed, or acceleration no longer applicable) fails closed at
    PREPARE before any base preparation or acquisition."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    runtime = SharedRuntime(layout=layout)
    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=runtime,
        selection_store=SelectionStore(path=tmp_path / "desired-products.toml"),
        host=_host(),
        capability_collector=lambda: _caps(),
        recommender=lambda caps: _recommendation(),
    )
    plan = _accel_plan(None)
    acquirer = _SpyAcquirer()
    provider_calls: list[int] = []

    def provider():
        provider_calls.append(1)
        raise AssertionError("full_state_provider must not be called")

    result = service.install_accelerated_runtime(
        plan=plan,
        capabilities=_caps(),
        recommendation=fresh_recommendation(),
        acquirer=acquirer,
        full_state_provider=provider,
        work_root=tmp_path / "work",
    )

    assert result.success is False
    assert result.cancelled is False
    assert result.phase is AcceleratedDeploymentPhase.PREPARE
    assert "late GPU compatibility conflict detected at deployment time" in (
        result.reason or ""
    )
    assert expected_fragment in (result.reason or "")
    assert result.old_runtime_preserved is True
    assert acquirer.calls == 0
    assert provider_calls == []
    assert not layout.slots.exists()
    assert runtime.status().state.value == "ABSENT"


# =============================================================================
# zealfie_slow witnesses — real venvs through the real service path
# =============================================================================


def _keep_from_provenance(
    prov: ProductProvenance,
) -> PlannedKeepProduct:
    """Build the plan's KEEP entry verbatim from active provenance."""
    return PlannedKeepProduct(
        product_id=prov.product_id,
        version=prov.version,
        commit_sha=prov.commit_sha,
        wheel_sha256=prov.wheel_sha256,
        source="provenance",
    )


class _ProgressCollector:
    def __init__(self) -> None:
        self.phases: list[InstallPhase] = []

    def __call__(self, progress) -> None:
        self.phases.append(progress.phase)


@pytest.mark.zealfie_slow
def test_service_accelerated_full_flow(
    tmp_path, witness_v1, fake_accel_wheel, monkeypatch,
):
    """Synthetic end-to-end witness through the SERVICE.

    Success, phase COMPLETED, old runtime preserved and still usable,
    active slot switched, fake-accel installed at the planned version,
    accelerated-metadata.json recorded, product provenance/installed
    lock/selection UNCHANGED, and KEEP exactness (no drift)."""
    # The default gate's backend compute probe (NVIDIA_CUDA) imports
    # cupy, which this synthetic candidate does not carry.  The compute
    # probe behaviour is covered by dedicated Phase F tests
    # (tests/test_acceleration_backend_probe.py and the transaction
    # tests); this witness stays focused on the service orchestration.
    monkeypatch.setattr(
        "zealfie.acceleration.deployment.get_backend_compute_probe",
        lambda backend: None,
    )
    service, runtime, layout, store, active_before = _make_ready_service(
        tmp_path, witness_v1
    )

    # ---- KEEP documentation verbatim from active provenance -------------
    prov_before = dict(service.active_provenance())
    assert set(prov_before) == {"zewitness"}
    keep = _keep_from_provenance(prov_before["zewitness"])
    assert keep.version == "0.0.1"
    assert keep.commit_sha == WITNESS_SHA
    assert keep.wheel_sha256 == _sha256(witness_v1)

    accel_plan = _accel_plan(active_before, keep=(keep,))
    provider_artifact = _witness_ppa(witness_v1)
    acquirer = _SingleWheelAcquirer(fake_accel_wheel)

    metadata_store = AcceleratedSlotMetadataStore(layout)
    provenance_store = ProductProvenanceStore(layout)
    installed_store = InstalledLockStore(layout)
    selection_before = store.path.read_bytes()
    prov_slot_before = provenance_store.load_slot(active_before)
    lock_slot_before = installed_store.load_slot(active_before)
    assert prov_slot_before is not None
    assert lock_slot_before is not None

    old_slot_path = layout.slot_path(active_before)
    old_snapshot = _snapshot_dir(old_slot_path)

    progress = _ProgressCollector()

    result = service.install_accelerated_runtime(
        plan=accel_plan,
        capabilities=_caps(),
        recommendation=_recommendation(),
        acquirer=acquirer,
        gate=None,  # -> default gate (real probe inside candidate venv)
        metadata_store=None,  # -> derived from the runtime layout
        full_state_provider=lambda: [provider_artifact],
        dependency_wheelhouse=_empty_wheelhouse(tmp_path),
        progress_callback=progress,
        work_root=tmp_path / "accel-work",
    )

    assert result.success is True, f"accelerated deploy failed: {result.reason}"
    assert result.cancelled is False
    assert result.phase is AcceleratedDeploymentPhase.COMPLETED
    assert result.reason is None
    assert result.old_runtime_preserved is True

    # ---- Active slot switched; old slot is the previous slot ------------
    final = runtime.status()
    assert final.active_slot_id is not None
    assert final.active_slot_id != active_before
    assert result.active_slot_id == final.active_slot_id
    assert result.previous_slot_id == active_before
    assert final.previous_slot_id == active_before

    # ---- Old slot still usable, unchanged byte-for-byte, no fake-accel --
    _assert_slot_usable(layout, active_before, "zealfie-witness")
    assert _snapshot_dir(old_slot_path) == old_snapshot
    old_python = _slot_python(old_slot_path)
    old_probe = probe_runtime_distribution(str(old_python), "fake-accel")
    assert old_probe["installed"] is False

    # ---- New slot: product version unchanged + fake-accel 1.0.0 ---------
    new_python = _slot_python(layout.slot_path(final.active_slot_id))
    witness_probe = probe_runtime_distribution(
        str(new_python), "zealfie-witness"
    )
    assert witness_probe["installed"] is True
    assert witness_probe["version"] == keep.version
    accel_probe = probe_runtime_distribution(str(new_python), "fake-accel")
    assert accel_probe["installed"] is True
    assert accel_probe["version"] == "1.0.0"

    # ---- Accelerated metadata: backend + variants under the final slot --
    meta = metadata_store.load_slot(final.active_slot_id)
    assert meta is not None
    assert meta.backend == "NVIDIA_CUDA"
    assert meta.variants == (
        ("fake-accel", "1.0.0", _sha256(fake_accel_wheel)),
    )

    # ---- Products unchanged: no provenance / lock / selection writes ----
    assert provenance_store.load_slot(final.active_slot_id) == {}
    assert provenance_store.load_slot(active_before) == prov_slot_before
    assert installed_store.load_slot(final.active_slot_id) is None
    assert installed_store.load_slot(active_before) == lock_slot_before
    assert store.path.read_bytes() == selection_before

    # ---- Progress: completed only, and only at the end ------------------
    assert progress.phases[-1] is InstallPhase.COMPLETED
    assert progress.phases.count(InstallPhase.COMPLETED) == 1


@pytest.mark.zealfie_slow
def test_service_acquisition_failure_preserves_active_runtime(
    tmp_path, witness_v1,
):
    """An injected acquisition failure through the SERVICE leaves the
    previously active runtime untouched and usable."""
    service, runtime, layout, store, active_before = _make_ready_service(
        tmp_path, witness_v1
    )
    keep = _keep_from_provenance(service.active_provenance()["zewitness"])
    accel_plan = _accel_plan(active_before, keep=(keep,))
    failing = _FailingAcquirer(
        AcceleratedAcquisitionError("synthetic acquisition failure")
    )

    result = service.install_accelerated_runtime(
        plan=accel_plan,
        capabilities=_caps(),
        recommendation=_recommendation(),
        acquirer=failing,
        full_state_provider=lambda: [_witness_ppa(witness_v1)],
        dependency_wheelhouse=_empty_wheelhouse(tmp_path),
        work_root=tmp_path / "accel-work",
    )

    assert result.success is False
    assert result.cancelled is False
    assert result.phase is AcceleratedDeploymentPhase.ACQUIRE
    assert "synthetic acquisition failure" in (result.reason or "")
    assert result.old_runtime_preserved is True
    assert failing.calls == 1
    assert runtime.status().active_slot_id == active_before
    _assert_slot_usable(layout, active_before, "zealfie-witness")


@pytest.mark.zealfie_slow
def test_service_gate_failure_blocks_and_never_mutates_active(
    tmp_path, witness_v1, fake_accel_wheel,
):
    """A failing gate blocks the deployment before activation; the
    active slot is byte-identical afterwards (no in-place mutation)."""
    service, runtime, layout, store, active_before = _make_ready_service(
        tmp_path, witness_v1
    )
    keep = _keep_from_provenance(service.active_provenance()["zewitness"])
    accel_plan = _accel_plan(active_before, keep=(keep,))

    gate_calls: list[str] = []

    class _FailingGate:
        def check(self, candidate_python: str, plan):
            gate_calls.append(candidate_python)
            return "synthetic backend refused"

    metadata_store = AcceleratedSlotMetadataStore(layout)
    active_slot_path = layout.slot_path(active_before)
    before = _snapshot_dir(active_slot_path)
    progress = _ProgressCollector()

    result = service.install_accelerated_runtime(
        plan=accel_plan,
        capabilities=_caps(),
        recommendation=_recommendation(),
        acquirer=_SingleWheelAcquirer(fake_accel_wheel),
        gate=_FailingGate(),
        metadata_store=metadata_store,
        full_state_provider=lambda: [_witness_ppa(witness_v1)],
        dependency_wheelhouse=_empty_wheelhouse(tmp_path),
        progress_callback=progress,
        work_root=tmp_path / "accel-work",
    )

    assert result.success is False
    assert result.cancelled is False
    assert result.phase is AcceleratedDeploymentPhase.GATE
    assert result.reason == "pre-activation gate failed: synthetic backend refused"
    assert result.old_runtime_preserved is True
    assert len(gate_calls) == 1

    # No metadata recorded (the gate runs before the record) and the
    # active slot is byte-identical (never mutated in place).
    assert not metadata_store.path.exists()
    assert _snapshot_dir(active_slot_path) == before
    assert runtime.status().active_slot_id == active_before
    _assert_slot_usable(layout, active_before, "zealfie-witness")
    assert InstallPhase.COMPLETED not in progress.phases


@pytest.mark.zealfie_slow
def test_service_cancellation_during_apply(
    tmp_path, witness_v1, fake_accel_wheel,
):
    """Cancellation raised mid-deployment (inside the BUILD window)
    aborts with cancelled=True and the old runtime intact."""
    service, runtime, layout, store, active_before = _make_ready_service(
        tmp_path, witness_v1
    )
    keep = _keep_from_provenance(service.active_provenance()["zewitness"])
    accel_plan = _accel_plan(active_before, keep=(keep,))

    calls = {"n": 0}

    def cancel_nth():
        calls["n"] += 1
        if calls["n"] >= 3:  # 1: service, 2: engine ACQUIRE, 3: BUILD
            raise CooperativeCancellationError("cancelled mid-flight")

    result = service.install_accelerated_runtime(
        plan=accel_plan,
        capabilities=_caps(),
        recommendation=_recommendation(),
        acquirer=_SingleWheelAcquirer(
            fake_accel_wheel, honour_cancel=False,
        ),
        full_state_provider=lambda: [_witness_ppa(witness_v1)],
        dependency_wheelhouse=_empty_wheelhouse(tmp_path),
        cancel_check=cancel_nth,
        work_root=tmp_path / "accel-work",
    )

    assert result.success is False
    assert result.cancelled is True
    assert result.phase is AcceleratedDeploymentPhase.BUILD
    assert result.old_runtime_preserved is True
    assert calls["n"] == 3
    assert runtime.status().active_slot_id == active_before
    _assert_slot_usable(layout, active_before, "zealfie-witness")


@pytest.mark.zealfie_slow
def test_service_keep_exactness_no_drift(
    tmp_path, witness_v1, fake_accel_wheel,
):
    """KEEP exactness enforced by the service: a provider whose artifact
    version drifts from the plan's KEEP documentation is refused at
    PREPARE, before any candidate slot creation."""
    service, runtime, layout, store, active_before = _make_ready_service(
        tmp_path, witness_v1
    )
    prov = service.active_provenance()["zewitness"]
    # The plan documents the exact installed identity...
    keep = _keep_from_provenance(prov)
    accel_plan = _accel_plan(active_before, keep=(keep,))
    # ...but the provider supplies a DRIFTED artifact (wrong version).
    drifted = _witness_ppa(witness_v1, version="9.9.9")
    slots_before = sorted(
        p.name for p in layout.slots.iterdir()
    ) if layout.slots.exists() else []

    result = service.install_accelerated_runtime(
        plan=accel_plan,
        capabilities=_caps(),
        recommendation=_recommendation(),
        acquirer=_SpyAcquirer(),
        full_state_provider=lambda: [drifted],
        dependency_wheelhouse=_empty_wheelhouse(tmp_path),
        work_root=tmp_path / "accel-work",
    )

    assert result.success is False
    assert result.phase is AcceleratedDeploymentPhase.PREPARE
    assert "version drift" in (result.reason or "")
    assert result.old_runtime_preserved is True
    slots_after = sorted(
        p.name for p in layout.slots.iterdir()
    ) if layout.slots.exists() else []
    assert slots_after == slots_before
    assert runtime.status().active_slot_id == active_before
