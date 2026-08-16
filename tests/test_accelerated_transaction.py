"""Full-witness transactional accelerated deployment tests (M1-2I).

A synthetic FULL witness of the accelerated deployment flow:

1. a real shared runtime A is created and a base product deployment
   (witness component + RuntimeLock) is applied by the existing M0-8B
   engine;
2. a synthetic ``PLAN_READY`` accelerated plan adds the synthetic
   ``fake-accel`` distribution (built from ``tests/fixtures/fake_accel``);
3. artifacts are acquired from a synthetic directory-based acquirer;
4. :func:`apply_accelerated_deployment` runs the whole engine and the
   assertions verify: active slot change, old slot still usable,
   ``fake-accel`` installed at the planned version in the new slot,
   non-primary installed-lock recording, observational metadata
   (backend + variant sha), spy-gate invocation, and untouched KEEP
   provenance inputs.

Then a battery of failure injections, each ending with the old active
slot unchanged and still usable (probed through its own Python).

``fake-accel`` is a synthetic pure-Python fixture name — ZeAlfie never
selects a concrete GPU framework.  All tests are ``zealfie_slow`` (real
venvs + pip installs).
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.zealfie_slow

from zealfie.acceleration import (
    AcceleratedAcquisitionUnavailable,
    AcceleratedDeploymentPlan,
    AcceleratedDeploymentPhase,
    AcceleratedPlanStatus,
    AcceleratedVariant,
    CooperativeCancellationError,
    HardwareCompatibility,
    HardwareCompatibilityReasonCode,
    HardwareCompatibilityStatus,
    PlannedAcceleratedDependency,
    PlannedKeepProduct,
    VariantStatus,
    default_accelerated_artifact_acquirer,
)
from zealfie.acceleration.deployment import (
    AcceleratedSlotMetadataStore,
    AcquiredAcceleratedVariant,
    apply_accelerated_deployment,
    extend_runtime_lock_with_acceleration,
)
from zealfie.building import build_wheel, inspect_wheel
from zealfie.common import normalise_distribution_name
from zealfie.components.model import ComponentDefinition, EntryPointContract
from zealfie.components.registry import ComponentRegistry
from zealfie.dependencies.models import LockedDependency, RuntimeLock
from zealfie.releases.model import VerifiedArtifact
from zealfie.runtime import (
    DeploymentPlan,
    DesiredComponent,
    DesiredRuntimeState,
    InstalledLockStore,
    RuntimeLayout,
    RuntimeReasonCode,
    RuntimeState,
    RuntimeStatus,
    SharedRuntime,
    apply_deployment_plan,
    build_deployment_plan,
    installed_lock_from_runtime_lock,
)
from zealfie.runtime.probe import probe_runtime_distribution

# ---------------------------------------------------------------------------
# Witness component definition
# ---------------------------------------------------------------------------

WITNESS_DEF = ComponentDefinition(
    component_id="zewitness",
    display_name="ZeWitness",
    distribution_name="zealfie-witness",
    launch_entry_points=(EntryPointContract("console_scripts", "zewitness"),),
)


def _registry() -> ComponentRegistry:
    return ComponentRegistry((WITNESS_DEF,))


# ---------------------------------------------------------------------------
# Session-scoped fake-accel wheel
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def fake_accel_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build fake-accel 1.0.0 once per session."""
    d = Path(__file__).resolve().parent / "fixtures" / "fake_accel"
    t = tmp_path_factory.mktemp("shared-fake-accel-txn")
    return build_wheel(d, output_dir=t)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _va_from_wheel(
    wheel_path: Path, component_id: str, version: str
) -> VerifiedArtifact:
    info = inspect_wheel(wheel_path)
    actual_size = wheel_path.stat().st_size
    h = hashlib.sha256()
    with open(wheel_path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return VerifiedArtifact(
        component_id=component_id,
        version=version,
        path=wheel_path,
        size=actual_size,
        sha256=h.hexdigest(),
        distribution_name=info.distribution_name,
        wheel_version=info.version,
    )


def _dc(component_id: str, version: str, wheel_path: Path) -> DesiredComponent:
    return DesiredComponent(
        component_id=component_id,
        version=version,
        artifact=_va_from_wheel(wheel_path, component_id, version),
    )


def _lock_dep(wheel_path: Path) -> LockedDependency:
    info = inspect_wheel(wheel_path)
    size = wheel_path.stat().st_size
    sha = hashlib.sha256()
    with open(wheel_path, "rb") as f:
        while chunk := f.read(65536):
            sha.update(chunk)
    return LockedDependency(
        name=normalise_distribution_name(info.distribution_name),
        version=info.version,
        wheel_path=wheel_path,
        size=size,
        sha256=sha.hexdigest(),
    )


def _plan_ready(
    components: tuple[DesiredComponent, ...],
    active_slot_id: str,
    dependency_lock: RuntimeLock,
) -> DeploymentPlan:
    """Build a READY base deployment plan with a dependency lock."""
    desired = DesiredRuntimeState(components=components)
    registry = ComponentRegistry(
        tuple(
            ComponentDefinition(
                component_id=dc.component_id,
                display_name=dc.component_id.title(),
                distribution_name=dc.artifact.distribution_name,
                launch_entry_points=(
                    EntryPointContract("console_scripts", dc.component_id),
                ),
            )
            for dc in components
        )
    )
    status = RuntimeStatus(
        state=RuntimeState.READY,
        runtime_root=Path("/fake"),
        active_slot_id=active_slot_id,
        active_path=Path("/fake/slots") / active_slot_id,
        python_executable=Path("/fake/slots") / active_slot_id / "bin" / "python",
        python_version="3.14.0",
    )

    def probe(runtime_python: str, dist_name: str) -> dict:
        return {
            "python_version": "3.14.0",
            "installed": False,
            "version": None,
            "entry_points": [],
        }

    return build_deployment_plan(
        desired, registry, status,
        probe_distribution=probe,
        dependency_lock=dependency_lock,
    )


def _slot_python(slot_dir: Path) -> Path:
    if sys.platform == "win32":
        return slot_dir / "Scripts" / "python.exe"
    return slot_dir / "bin" / "python"


def _assert_slot_usable(layout: RuntimeLayout, slot_id: str, dist: str) -> None:
    """Probe *dist* through the slot's own Python interpreter."""
    slot_path = layout.slot_path(slot_id)
    assert slot_path.is_dir()
    python = _slot_python(slot_path)
    assert python.is_file()
    probe = probe_runtime_distribution(str(python), dist)
    assert probe["installed"] is True


def _hardware() -> HardwareCompatibility:
    return HardwareCompatibility(
        status=HardwareCompatibilityStatus.SUPPORTED,
        reason_code=HardwareCompatibilityReasonCode.COMPATIBLE.value,
        reason="compatible",
        products_concerned=("zewitness",),
    )


def _accel_plan(
    source_active_slot_id: str,
    *,
    status: AcceleratedPlanStatus = AcceleratedPlanStatus.PLAN_READY,
    keep: tuple[PlannedKeepProduct, ...] = (),
    distribution: str = "fake-accel",
    specifier: str = "==1.0.0",
) -> AcceleratedDeploymentPlan:
    """Synthetic accelerated plan for fake-accel."""
    entry = PlannedAcceleratedDependency(
        distribution=distribution,
        specifier=specifier,
        extras=(),
        declaring_products=("zewitness",),
        variant=AcceleratedVariant(
            distribution=distribution,
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
        source_runtime_state="READY",
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
    """Synthetic acquirer: copies one fake wheel per planned dependency
    into ``work_root`` and verifies size + sha256."""

    def __init__(self, wheel_path: Path) -> None:
        self._wheel_path = wheel_path

    def acquire(self, plan, work_root, *, cancel_check=None):
        acquired = []
        for entry in plan.added_requirements:
            if cancel_check is not None:
                cancel_check()
            # Preserve the source wheel's valid PEP 427 filename.
            dest = work_root / self._wheel_path.name
            shutil.copyfile(self._wheel_path, dest)
            size = dest.stat().st_size
            sha = hashlib.sha256()
            with open(dest, "rb") as fh:
                while chunk := fh.read(65536):
                    sha.update(chunk)
            acquired.append(
                AcquiredAcceleratedVariant(
                    distribution=entry.distribution,
                    version=entry.variant.version,
                    wheel_path=dest,
                    size=size,
                    sha256=sha.hexdigest(),
                )
            )
        return tuple(acquired)


def _prepare(
    tmp_path: Path, witness_v1: Path,
) -> tuple[
    SharedRuntime,
    RuntimeLayout,
    tuple[DesiredComponent, ...],
    RuntimeLock,
    DeploymentPlan,
    str,
]:
    """Create runtime, deploy the witness product via the M0-8B engine,
    and return everything needed to run an accelerated deployment.

    The returned base deployment plan is built FROM the slot that the
    initial deployment activated (so it is coherent with the current
    runtime and can be re-applied as the base of an accelerated
    deployment).
    """
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()

    witness_dep = _lock_dep(witness_v1)
    base_lock = RuntimeLock(locked={witness_dep.name: witness_dep})
    components = (_dc("zewitness", "0.0.1", witness_v1),)

    first_plan = _plan_ready(components, rt.status().active_slot_id, base_lock)
    result = apply_deployment_plan(
        first_plan, registry=_registry(), runtime=rt
    )
    assert result.success is True, f"base deployment failed: {result.reason}"
    active_id = rt.status().active_slot_id
    assert active_id is not None
    _assert_slot_usable(layout, active_id, "zealfie-witness")

    # Rebuild the plan from the current active slot: this is the plan
    # the accelerated deployment will extend and re-apply.
    plan = _plan_ready(components, active_id, base_lock)
    return rt, layout, components, base_lock, plan, active_id


# =============================================================================
# Happy path — full accelerated deployment
# =============================================================================


def test_accelerated_deployment_full_flow(
    tmp_path: Path, witness_v1: Path, fake_accel_wheel: Path,
) -> None:
    rt, layout, components, base_lock, dep_plan, active_before = _prepare(
        tmp_path, witness_v1
    )

    # Synthetic KEEP provenance — passed in, never re-resolved.
    keep = PlannedKeepProduct(
        product_id="zewitness",
        version="0.0.1",
        commit_sha=None,
        wheel_sha256=None,
        source="installed_lock",
    )
    accel_plan = _accel_plan(active_before, keep=(keep,))
    declaring = {"zewitness": "zealfie-witness"}

    work_root = tmp_path / "accel-work"
    work_root.mkdir()
    acquired = _SingleWheelAcquirer(fake_accel_wheel).acquire(
        accel_plan, work_root
    )
    assert len(acquired) == 1

    gate_calls: list[tuple[str, AcceleratedDeploymentPlan]] = []

    class _SpyGate:
        def check(
            self, candidate_python: str, plan: AcceleratedDeploymentPlan
        ):
            gate_calls.append((candidate_python, plan))
            return None

    spy_gate = _SpyGate()

    metadata_store = AcceleratedSlotMetadataStore(layout)

    result = apply_accelerated_deployment(
        accelerated_plan=accel_plan,
        deployment_plan=dep_plan,
        registry=_registry(),
        runtime=rt,
        acquired=acquired,
        declaring_distributions=declaring,
        accelerated_gate=spy_gate,
        metadata_store=metadata_store,
    )

    assert result.success is True, f"accelerated deployment failed: {result.reason}"
    assert result.cancelled is False
    assert result.phase is AcceleratedDeploymentPhase.COMPLETED
    assert result.reason is None
    assert result.old_runtime_preserved is True

    # Active slot changed; previous slot is the old runtime.
    final = rt.status()
    assert final.active_slot_id is not None
    assert final.active_slot_id != active_before
    assert result.active_slot_id == final.active_slot_id
    assert final.previous_slot_id == active_before
    assert result.previous_slot_id == active_before

    # Old slot STILL USABLE (probe its python + product distribution),
    # and does NOT carry the accelerated distribution.
    _assert_slot_usable(layout, active_before, "zealfie-witness")
    old_python = _slot_python(layout.slot_path(active_before))
    old_accel_probe = probe_runtime_distribution(str(old_python), "fake-accel")
    assert old_accel_probe["installed"] is False

    # fake-accel installed at 1.0.0 in the new slot.
    new_python = _slot_python(layout.slot_path(final.active_slot_id))
    accel_probe = probe_runtime_distribution(str(new_python), "fake-accel")
    assert accel_probe["installed"] is True
    assert accel_probe["version"] == "1.0.0"

    # The extended lock records fake-accel as NON-PRIMARY with the
    # correct required_by edge.
    extended = extend_runtime_lock_with_acceleration(
        base_lock, accel_plan, acquired, declaring
    )
    assert extended.primary_names == base_lock.primary_names
    assert "fake-accel" in extended.dependency_names
    assert "fake-accel" not in extended.primary_names
    assert extended.locked["fake-accel"].required_by == frozenset(
        {"zealfie-witness"}
    )
    assert extended.locked["zealfie-witness"] is base_lock.locked[
        "zealfie-witness"
    ]

    # Installed-lock persistence lives in the service layer; here we
    # reduce the deployed lock exactly the way the service does and
    # verify the non-primary recording.
    installed_store = InstalledLockStore(layout)
    installed_store.record(
        final.active_slot_id,
        installed_lock_from_runtime_lock(extended),
    )
    recorded = installed_store.load_slot(final.active_slot_id)
    assert recorded is not None
    assert recorded["fake-accel"].primary is False
    assert recorded["fake-accel"].version == "1.0.0"
    assert recorded["zealfie-witness"].primary is True

    # Accelerated metadata records backend + variant sha under the
    # FINAL slot id.
    meta = metadata_store.load_slot(final.active_slot_id)
    assert meta is not None
    assert meta.backend == "NVIDIA_CUDA"
    assert meta.variants == (
        ("fake-accel", "1.0.0", acquired[0].sha256),
    )

    # The gate was invoked with the candidate python and the plan.
    assert len(gate_calls) == 1
    gate_python, gate_plan = gate_calls[0]
    assert gate_plan is accel_plan
    gate_python_path = Path(gate_python)
    assert gate_python_path.is_file()
    assert gate_python_path.parent.parent == layout.slot_path(
        final.active_slot_id
    )

    # KEEP provenance inputs unchanged — no re-resolution, stores untouched.
    assert accel_plan.keep_products == (keep,)
    assert accel_plan.keep_products[0] is keep
    assert keep.product_id == "zewitness"
    assert keep.version == "0.0.1"
    assert keep.source == "installed_lock"


# =============================================================================
# ZA-M1-2J.2 Phase F — backend compute probe inside the default gate
# =============================================================================


def test_compute_probe_failure_blocks_before_activation(
    tmp_path: Path, witness_v1: Path, fake_accel_wheel: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a) Distribution/version gate OK but the backend compute probe
    FAILS (synthetic script exit 1): the candidate is rejected BEFORE
    activation — phase GATE, the old runtime stays the active pointer
    and remains usable."""
    rt, layout, _, _, dep_plan, active_before = _prepare(tmp_path, witness_v1)
    accel_plan = _accel_plan(active_before)

    work_root = tmp_path / "work"
    work_root.mkdir()
    acquired = _SingleWheelAcquirer(fake_accel_wheel).acquire(
        accel_plan, work_root
    )

    monkeypatch.setattr(
        "zealfie.acceleration.deployment.get_backend_compute_probe",
        lambda backend: {
            "label": "synthetic failing probe",
            "script": (
                'import sys\n'
                'print("BACKEND_COMPUTE_PROBE_FAIL: '
                'RuntimeError: synthetic compute boom")\n'
                "sys.exit(1)\n"
            ),
        },
    )

    result = apply_accelerated_deployment(
        accelerated_plan=accel_plan,
        deployment_plan=dep_plan,
        registry=_registry(),
        runtime=rt,
        acquired=acquired,
        declaring_distributions={"zewitness": "zealfie-witness"},
        # accelerated_gate defaults to the REAL default gate — the
        # compute probe failure must be produced by it, not injected.
    )

    assert result.success is False
    assert result.cancelled is False
    assert result.phase is AcceleratedDeploymentPhase.GATE
    reason = result.reason or ""
    assert "pre-activation gate failed:" in reason
    assert "backend compute probe failed for NVIDIA_CUDA" in reason
    assert "synthetic compute boom" in reason
    assert result.old_runtime_preserved is True

    # The active pointer was never switched and the old runtime is
    # still fully usable.
    assert rt.status().active_slot_id == active_before
    _assert_slot_usable(layout, active_before, "zealfie-witness")


def test_compute_probe_ok_activation_succeeds(
    tmp_path: Path, witness_v1: Path, fake_accel_wheel: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) Compute probe OK => the activation goes through the REAL
    default gate and completes."""
    rt, layout, _, _, dep_plan, active_before = _prepare(tmp_path, witness_v1)
    accel_plan = _accel_plan(active_before)

    work_root = tmp_path / "work"
    work_root.mkdir()
    acquired = _SingleWheelAcquirer(fake_accel_wheel).acquire(
        accel_plan, work_root
    )

    monkeypatch.setattr(
        "zealfie.acceleration.deployment.get_backend_compute_probe",
        lambda backend: {
            "label": "synthetic passing probe",
            "script": 'print("BACKEND_COMPUTE_PROBE_OK")\n',
        },
    )

    result = apply_accelerated_deployment(
        accelerated_plan=accel_plan,
        deployment_plan=dep_plan,
        registry=_registry(),
        runtime=rt,
        acquired=acquired,
        declaring_distributions={"zewitness": "zealfie-witness"},
    )

    assert result.success is True, f"accelerated deployment failed: {result.reason}"
    assert result.phase is AcceleratedDeploymentPhase.COMPLETED
    final = rt.status()
    assert final.active_slot_id is not None
    assert final.active_slot_id != active_before
    new_python = _slot_python(layout.slot_path(final.active_slot_id))
    accel_probe = probe_runtime_distribution(str(new_python), "fake-accel")
    assert accel_probe["installed"] is True
    assert accel_probe["version"] == "1.0.0"
    _assert_slot_usable(layout, active_before, "zealfie-witness")


def test_backend_without_probe_keeps_previous_gate_behaviour(
    tmp_path: Path, witness_v1: Path, fake_accel_wheel: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(c) A backend without a registered compute probe keeps the
    distribution/version-only gate — genericity preserved, activation
    completes."""
    rt, layout, _, _, dep_plan, active_before = _prepare(tmp_path, witness_v1)
    accel_plan = _accel_plan(active_before)

    work_root = tmp_path / "work"
    work_root.mkdir()
    acquired = _SingleWheelAcquirer(fake_accel_wheel).acquire(
        accel_plan, work_root
    )

    monkeypatch.setattr(
        "zealfie.acceleration.deployment.get_backend_compute_probe",
        lambda backend: None,
    )

    result = apply_accelerated_deployment(
        accelerated_plan=accel_plan,
        deployment_plan=dep_plan,
        registry=_registry(),
        runtime=rt,
        acquired=acquired,
        declaring_distributions={"zewitness": "zealfie-witness"},
    )

    assert result.success is True, f"accelerated deployment failed: {result.reason}"
    assert result.phase is AcceleratedDeploymentPhase.COMPLETED
    final = rt.status()
    assert final.active_slot_id is not None
    assert final.active_slot_id != active_before
    _assert_slot_usable(layout, active_before, "zealfie-witness")


# =============================================================================
# Failure injections — every one leaves the old active slot usable
# =============================================================================


def test_acquisition_failure_preserves_active_runtime(
    tmp_path: Path, witness_v1: Path,
) -> None:
    rt, layout, _, _, _, active_before = _prepare(tmp_path, witness_v1)
    accel_plan = _accel_plan(active_before)
    work_root = tmp_path / "work"
    work_root.mkdir()

    # The fail-closed default acquirer refuses before anything happens.
    with pytest.raises(AcceleratedAcquisitionUnavailable):
        default_accelerated_artifact_acquirer().acquire(accel_plan, work_root)

    assert rt.status().active_slot_id == active_before
    _assert_slot_usable(layout, active_before, "zealfie-witness")


def test_lock_extension_error_unknown_declaring_product(
    tmp_path: Path, witness_v1: Path, fake_accel_wheel: Path,
) -> None:
    rt, layout, _, _, dep_plan, active_before = _prepare(tmp_path, witness_v1)
    accel_plan = _accel_plan(active_before)

    work_root = tmp_path / "work"
    work_root.mkdir()
    acquired = _SingleWheelAcquirer(fake_accel_wheel).acquire(
        accel_plan, work_root
    )

    result = apply_accelerated_deployment(
        accelerated_plan=accel_plan,
        deployment_plan=dep_plan,
        registry=_registry(),
        runtime=rt,
        acquired=acquired,
        declaring_distributions={},  # unknown declaring product
    )

    assert result.success is False
    assert result.cancelled is False
    assert result.phase is AcceleratedDeploymentPhase.RESOLVE
    assert "lock extension failed" in (result.reason or "")
    assert result.old_runtime_preserved is True
    assert rt.status().active_slot_id == active_before
    _assert_slot_usable(layout, active_before, "zealfie-witness")


def test_build_failure_corrupt_acquired_wheel(
    tmp_path: Path, witness_v1: Path, fake_accel_wheel: Path,
) -> None:
    rt, layout, _, _, dep_plan, active_before = _prepare(tmp_path, witness_v1)
    accel_plan = _accel_plan(active_before)

    work_root = tmp_path / "work"
    work_root.mkdir()
    acquired = _SingleWheelAcquirer(fake_accel_wheel).acquire(
        accel_plan, work_root
    )

    # Corrupt the acquired wheel after acquisition: TOCTOU must block.
    with open(acquired[0].wheel_path, "ab") as fh:
        fh.write(b"tampered-after-acquire")

    result = apply_accelerated_deployment(
        accelerated_plan=accel_plan,
        deployment_plan=dep_plan,
        registry=_registry(),
        runtime=rt,
        acquired=acquired,
        declaring_distributions={"zewitness": "zealfie-witness"},
    )

    assert result.success is False
    assert result.cancelled is False
    assert result.phase is AcceleratedDeploymentPhase.BUILD
    assert "TOCTOU" in (result.reason or "")
    assert result.old_runtime_preserved is True
    assert rt.status().active_slot_id == active_before
    _assert_slot_usable(layout, active_before, "zealfie-witness")


def test_gate_failure_blocks_before_activation(
    tmp_path: Path, witness_v1: Path, fake_accel_wheel: Path,
) -> None:
    rt, layout, _, _, dep_plan, active_before = _prepare(tmp_path, witness_v1)
    accel_plan = _accel_plan(active_before)

    work_root = tmp_path / "work"
    work_root.mkdir()
    acquired = _SingleWheelAcquirer(fake_accel_wheel).acquire(
        accel_plan, work_root
    )

    gate_calls: list[str] = []

    class _FailingGate:
        def check(
            self, candidate_python: str, plan: AcceleratedDeploymentPlan
        ):
            gate_calls.append(candidate_python)
            return "synthetic backend refused"

    failing_gate = _FailingGate()

    metadata_store = AcceleratedSlotMetadataStore(layout)

    result = apply_accelerated_deployment(
        accelerated_plan=accel_plan,
        deployment_plan=dep_plan,
        registry=_registry(),
        runtime=rt,
        acquired=acquired,
        declaring_distributions={"zewitness": "zealfie-witness"},
        accelerated_gate=failing_gate,
        metadata_store=metadata_store,
    )

    assert result.success is False
    assert result.cancelled is False
    assert result.phase is AcceleratedDeploymentPhase.GATE
    assert result.reason == "pre-activation gate failed: synthetic backend refused"
    assert result.old_runtime_preserved is True
    assert len(gate_calls) == 1

    # No metadata was recorded (gate ran first and failed).
    assert not metadata_store.path.exists()

    assert rt.status().active_slot_id == active_before
    _assert_slot_usable(layout, active_before, "zealfie-witness")


def test_metadata_write_failure_blocks_before_activation(
    tmp_path: Path, witness_v1: Path, fake_accel_wheel: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The default gate must PASS here (the failure under test is the
    # metadata write): the NVIDIA_CUDA compute probe cannot run in this
    # synthetic candidate (covered separately by the Phase F probe
    # tests).
    monkeypatch.setattr(
        "zealfie.acceleration.deployment.get_backend_compute_probe",
        lambda backend: None,
    )
    rt, layout, _, _, dep_plan, active_before = _prepare(tmp_path, witness_v1)
    accel_plan = _accel_plan(active_before)

    work_root = tmp_path / "work"
    work_root.mkdir()
    acquired = _SingleWheelAcquirer(fake_accel_wheel).acquire(
        accel_plan, work_root
    )

    class _FailingMetadataStore:
        """Metadata store whose write path always fails."""

        def __init__(self, layout: RuntimeLayout) -> None:
            self.calls: list[str] = []
            self._real = AcceleratedSlotMetadataStore(layout)

        def record(self, slot_id, metadata):
            self.calls.append(slot_id)
            raise OSError("synthetic disk full")

        @property
        def path(self) -> Path:
            return self._real.path

    failing_store = _FailingMetadataStore(layout)

    result = apply_accelerated_deployment(
        accelerated_plan=accel_plan,
        deployment_plan=dep_plan,
        registry=_registry(),
        runtime=rt,
        acquired=acquired,
        declaring_distributions={"zewitness": "zealfie-witness"},
        metadata_store=failing_store,
    )

    assert result.success is False
    assert result.cancelled is False
    assert result.phase is AcceleratedDeploymentPhase.PERSIST
    assert "accelerated metadata persistence failed" in (result.reason or "")
    assert result.old_runtime_preserved is True
    assert len(failing_store.calls) == 1
    assert not failing_store.path.exists()

    assert rt.status().active_slot_id == active_before
    _assert_slot_usable(layout, active_before, "zealfie-witness")


def test_cancellation_before_apply(
    tmp_path: Path, witness_v1: Path, fake_accel_wheel: Path,
) -> None:
    rt, layout, _, _, dep_plan, active_before = _prepare(tmp_path, witness_v1)
    accel_plan = _accel_plan(active_before)

    work_root = tmp_path / "work"
    work_root.mkdir()
    acquired = _SingleWheelAcquirer(fake_accel_wheel).acquire(
        accel_plan, work_root
    )

    calls: list[int] = []

    def cancel():
        calls.append(1)
        raise CooperativeCancellationError("user cancelled")

    result = apply_accelerated_deployment(
        accelerated_plan=accel_plan,
        deployment_plan=dep_plan,
        registry=_registry(),
        runtime=rt,
        acquired=acquired,
        declaring_distributions={"zewitness": "zealfie-witness"},
        cancel_check=cancel,
    )

    assert result.success is False
    assert result.cancelled is True
    assert result.phase is AcceleratedDeploymentPhase.ACQUIRE
    assert result.old_runtime_preserved is True
    assert len(calls) == 1
    assert rt.status().active_slot_id == active_before
    _assert_slot_usable(layout, active_before, "zealfie-witness")


def test_cancellation_during_apply(
    tmp_path: Path, witness_v1: Path, fake_accel_wheel: Path,
) -> None:
    rt, layout, _, _, dep_plan, active_before = _prepare(tmp_path, witness_v1)
    accel_plan = _accel_plan(active_before)

    work_root = tmp_path / "work"
    work_root.mkdir()
    acquired = _SingleWheelAcquirer(fake_accel_wheel).acquire(
        accel_plan, work_root
    )

    calls = {"n": 0}

    def cancel_nth():
        calls["n"] += 1
        if calls["n"] >= 2:
            raise CooperativeCancellationError("cancelled mid-flight")

    result = apply_accelerated_deployment(
        accelerated_plan=accel_plan,
        deployment_plan=dep_plan,
        registry=_registry(),
        runtime=rt,
        acquired=acquired,
        declaring_distributions={"zewitness": "zealfie-witness"},
        cancel_check=cancel_nth,
    )

    assert result.success is False
    assert result.cancelled is True
    assert result.phase is AcceleratedDeploymentPhase.BUILD
    assert result.old_runtime_preserved is True
    assert calls["n"] == 2
    assert rt.status().active_slot_id == active_before
    _assert_slot_usable(layout, active_before, "zealfie-witness")


def test_stale_plan_detected_at_apply(
    tmp_path: Path, witness_v1: Path, fake_accel_wheel: Path,
) -> None:
    rt, layout, _, _, dep_plan, active_before = _prepare(tmp_path, witness_v1)
    accel_plan = _accel_plan(active_before)

    # Move the active pointer after the plans were built.
    second = apply_deployment_plan(dep_plan, registry=_registry(), runtime=rt)
    assert second.success is True
    active_after = rt.status().active_slot_id
    assert active_after is not None
    assert active_after != active_before

    work_root = tmp_path / "work"
    work_root.mkdir()
    acquired = _SingleWheelAcquirer(fake_accel_wheel).acquire(
        accel_plan, work_root
    )

    result = apply_accelerated_deployment(
        accelerated_plan=accel_plan,
        deployment_plan=dep_plan,
        registry=_registry(),
        runtime=rt,
        acquired=acquired,
        declaring_distributions={"zewitness": "zealfie-witness"},
    )

    assert result.success is False
    assert result.cancelled is False
    assert result.phase is AcceleratedDeploymentPhase.PREPARE
    assert "stale deployment plan" in (result.reason or "")
    assert result.old_runtime_preserved is True
    assert rt.status().active_slot_id == active_after
    _assert_slot_usable(layout, active_after, "zealfie-witness")


def test_plan_not_ready_refused(
    tmp_path: Path, witness_v1: Path, fake_accel_wheel: Path,
) -> None:
    rt, layout, _, _, dep_plan, active_before = _prepare(tmp_path, witness_v1)
    blocked_plan = _accel_plan(
        active_before, status=AcceleratedPlanStatus.BLOCKED
    )

    work_root = tmp_path / "work"
    work_root.mkdir()
    acquired = _SingleWheelAcquirer(fake_accel_wheel).acquire(
        _accel_plan(active_before), work_root
    )

    result = apply_accelerated_deployment(
        accelerated_plan=blocked_plan,
        deployment_plan=dep_plan,
        registry=_registry(),
        runtime=rt,
        acquired=acquired,
        declaring_distributions={"zewitness": "zealfie-witness"},
    )

    assert result.success is False
    assert result.cancelled is False
    assert result.phase is AcceleratedDeploymentPhase.PREPARE
    assert "accelerated plan is not ready" in (result.reason or "")
    assert result.old_runtime_preserved is True
    assert rt.status().active_slot_id == active_before
    _assert_slot_usable(layout, active_before, "zealfie-witness")


def test_incoherent_source_slots_refused(
    tmp_path: Path, witness_v1: Path, fake_accel_wheel: Path,
) -> None:
    rt, layout, _, _, dep_plan, active_before = _prepare(tmp_path, witness_v1)
    incoherent = _accel_plan("rt-somewhere-else")

    work_root = tmp_path / "work"
    work_root.mkdir()
    acquired = _SingleWheelAcquirer(fake_accel_wheel).acquire(
        incoherent, work_root
    )

    result = apply_accelerated_deployment(
        accelerated_plan=incoherent,
        deployment_plan=dep_plan,
        registry=_registry(),
        runtime=rt,
        acquired=acquired,
        declaring_distributions={"zewitness": "zealfie-witness"},
    )

    assert result.success is False
    assert result.cancelled is False
    assert result.phase is AcceleratedDeploymentPhase.PREPARE
    assert "incoherent" in (result.reason or "")
    assert result.old_runtime_preserved is True
    assert rt.status().active_slot_id == active_before
    _assert_slot_usable(layout, active_before, "zealfie-witness")


def test_accelerated_plan_without_backend_refused(
    tmp_path: Path, witness_v1: Path, fake_accel_wheel: Path,
) -> None:
    rt, layout, _, _, dep_plan, active_before = _prepare(tmp_path, witness_v1)
    no_backend = _accel_plan(active_before)
    import dataclasses

    no_backend = dataclasses.replace(no_backend, backend=None)

    work_root = tmp_path / "work"
    work_root.mkdir()
    acquired = _SingleWheelAcquirer(fake_accel_wheel).acquire(
        no_backend, work_root
    )

    result = apply_accelerated_deployment(
        accelerated_plan=no_backend,
        deployment_plan=dep_plan,
        registry=_registry(),
        runtime=rt,
        acquired=acquired,
        declaring_distributions={"zewitness": "zealfie-witness"},
    )

    assert result.success is False
    assert result.cancelled is False
    assert result.phase is AcceleratedDeploymentPhase.PREPARE
    assert "has no backend" in (result.reason or "")
    assert rt.status().active_slot_id == active_before
    _assert_slot_usable(layout, active_before, "zealfie-witness")

class _ActivationFailingRuntime:
    """Thin proxy over a real ``SharedRuntime``: every method
    delegates unchanged except ``activate``, which deterministically
    reports a failed activation.

    A non-READY activation status is the runtime layer's own failure
    channel — the M0-8B engine maps it to ``reason="activation
    failed: ..."`` without ever touching the active pointer, which the
    accelerated orchestrator classifies as phase ACTIVATE.
    """

    def __init__(self, real: SharedRuntime) -> None:
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def activate(self, txn) -> RuntimeStatus:
        return RuntimeStatus(
            state=RuntimeState.BROKEN,
            runtime_root=self._real.layout.root,
            reason_code=RuntimeReasonCode.ACTIVATION_FAILED,
            reason="synthetic activation failure injection",
        )


def test_activation_failure_preserves_active_runtime(
    tmp_path: Path, witness_v1: Path, fake_accel_wheel: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Activation failure (I.7 case 7): the orchestrator reports
    success=False at phase ACTIVATE and the previously active slot
    stays the active pointer — untouched and still usable."""
    # Default gate must reach ACTIVATE: disable the NVIDIA_CUDA compute
    # probe (covered separately by the Phase F probe tests).
    monkeypatch.setattr(
        "zealfie.acceleration.deployment.get_backend_compute_probe",
        lambda backend: None,
    )
    rt, layout, _, _, dep_plan, active_before = _prepare(tmp_path, witness_v1)
    accel_plan = _accel_plan(active_before)

    work_root = tmp_path / "work"
    work_root.mkdir()
    acquired = _SingleWheelAcquirer(fake_accel_wheel).acquire(
        accel_plan, work_root
    )

    result = apply_accelerated_deployment(
        accelerated_plan=accel_plan,
        deployment_plan=dep_plan,
        registry=_registry(),
        runtime=_ActivationFailingRuntime(rt),
        acquired=acquired,
        declaring_distributions={"zewitness": "zealfie-witness"},
    )

    assert result.success is False
    assert result.cancelled is False
    assert result.phase is AcceleratedDeploymentPhase.ACTIVATE
    assert "activation failed" in (result.reason or "")
    assert result.old_runtime_preserved is True

    # The active pointer was never switched: the old runtime is still
    # the active slot and remains fully usable.
    assert rt.status().active_slot_id == active_before
    _assert_slot_usable(layout, active_before, "zealfie-witness")
