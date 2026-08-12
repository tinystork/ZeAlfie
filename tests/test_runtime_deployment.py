"""Tests for M0-8B transactional offline deployment engine.

Covers:
- BLOCKED plan refusal (no mutation)
- Stale plan detection (READY→READY, ABSENT→READY, READY→BROKEN)
- Full-state materialization (KEEP+INSTALL both installed)
- Active slot unchanged during preparation
- Atomic activation (active=B, previous=A)
- Artifact TOCTOU revalidation
- Partial pip failure leaves active unchanged
- M0-8B.2: apply-time conflict hardening (registry change after planning)
- M0-8B.2: multi-component pre-activation TOCTOU (candidate corruption)
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.zealfie_slow

from zealfie.building import build_wheel
from zealfie.components.model import ComponentDefinition, EntryPointContract
from zealfie.components.registry import ComponentRegistry
from zealfie.releases.model import VerifiedArtifact
from zealfie.runtime import (
    DeploymentAction,
    DeploymentPlan,
    DesiredComponent,
    DesiredRuntimeState,
    InstallOutcome,
    RuntimeLayout,
    RuntimeState,
    RuntimeStatus,
    SharedRuntime,
    apply_deployment_plan,
    build_deployment_plan,
    check_desired_state_conflicts,
    probe_runtime_distribution,
)

# ---------------------------------------------------------------------------
# Witness component definitions
# ---------------------------------------------------------------------------

WITNESS_DEF = ComponentDefinition(
    component_id="zewitness",
    display_name="ZeWitness",
    distribution_name="zealfie-witness",
    launch_entry_points=(EntryPointContract("console_scripts", "zewitness"),),
)

WITNESS2_DEF = ComponentDefinition(
    component_id="zewitness2",
    display_name="ZeWitness2",
    distribution_name="zealfie-witness2",
    launch_entry_points=(EntryPointContract("console_scripts", "zewitness2"),),
)


# ---------------------------------------------------------------------------
# Session-scoped wheel fixtures
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _registry() -> ComponentRegistry:
    return ComponentRegistry((WITNESS_DEF, WITNESS2_DEF))


def _registry_single() -> ComponentRegistry:
    return ComponentRegistry((WITNESS_DEF,))


def _va_from_wheel(wheel_path: Path, component_id: str, version: str) -> VerifiedArtifact:
    """Build a VerifiedArtifact from a wheel file."""
    from zealfie.building import inspect_wheel
    info = inspect_wheel(wheel_path)
    actual_size = wheel_path.stat().st_size
    h = hashlib.sha256()
    with open(wheel_path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
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


def _plan_absent(components: tuple[DesiredComponent, ...]) -> DeploymentPlan:
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
        state=RuntimeState.ABSENT,
        runtime_root=Path("/fake"),
    )
    plan = build_deployment_plan(desired, registry, status)
    return plan


def _plan_ready(
    components: tuple[DesiredComponent, ...],
    active_slot_id: str = "rt-source",
) -> DeploymentPlan:
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

    # Need a probe callable for READY runtimes.
    def probe(runtime_python: str, dist_name: str) -> dict:
        return {
            "python_version": "3.14.0",
            "installed": False,
            "version": None,
            "entry_points": [],
        }

    return build_deployment_plan(desired, registry, status, probe_distribution=probe)


# ---------------------------------------------------------------------------
# 1) BLOCKED plan refuses before transaction/venv/pip
# ---------------------------------------------------------------------------

def test_blocked_plan_refused_no_mutation(
    tmp_path: Path, witness_v1: Path,
) -> None:
    """BLOCKED plan must be refused before any candidate slot or venv creation."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()

    # Build a blocked plan by creating a BROKEN runtime status.
    registry = _registry_single()
    desired = DesiredRuntimeState(components=(_dc("zewitness", "0.0.1", witness_v1),))
    broken_status = RuntimeStatus(
        state=RuntimeState.BROKEN,
        runtime_root=layout.root,
        reason="test BROKEN status",
    )
    plan = build_deployment_plan(desired, registry, broken_status)
    assert plan.blocked is True

    active_before = rt.status().active_slot_id

    result = apply_deployment_plan(plan, registry=registry, runtime=rt)
    assert result.success is False
    assert "blocked" in (result.reason or "").lower()

    # Active unchanged.
    assert rt.status().active_slot_id == active_before

    # No candidate slot directories created.
    slots_dir = layout.slots
    slot_dirs = []
    if slots_dir.is_dir():
        slot_dirs = [d for d in slots_dir.iterdir() if d.is_dir()]
    assert len(slot_dirs) <= 1  # At most the active slot


# ---------------------------------------------------------------------------
# 2) READY → BROKEN: refused before candidate creation
# ---------------------------------------------------------------------------

def test_ready_to_broken_refused(
    tmp_path: Path, witness_v1: Path,
) -> None:
    """BROKEN current runtime must be refused before candidate creation."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()

    slots_dir = layout.slots
    # Count existing slot dirs.
    existing_before = len([d for d in slots_dir.iterdir() if d.is_dir()]) if slots_dir.exists() else 0

    # Build an ABSENT plan so plan.blocked is False.
    registry = _registry_single()
    desired = DesiredRuntimeState(components=(_dc("zewitness", "0.0.1", witness_v1),))
    absent_status = RuntimeStatus(
        state=RuntimeState.ABSENT,
        runtime_root=layout.root,
    )
    plan = build_deployment_plan(desired, registry, absent_status)

    # Corrupt the state file to make runtime BROKEN.
    layout.active_pointer.write_text("bad json {{{")

    result = apply_deployment_plan(plan, registry=registry, runtime=rt)
    assert result.success is False
    assert "BROKEN" in (result.reason or "")

    # No new candidate slot directories.
    existing_after = len([d for d in slots_dir.iterdir() if d.is_dir()]) if slots_dir.exists() else 0
    assert existing_after == existing_before


# ---------------------------------------------------------------------------
# 3) Stale plan: READY A → READY C
# ---------------------------------------------------------------------------

def test_stale_plan_ready_to_ready_refused(
    tmp_path: Path, witness_v1: Path,
) -> None:
    """Plan built from slot A, but active is now C → refuse before mutation."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    original_active = rt.status().active_slot_id

    # Build plan with a different source_active_slot_id.
    registry = _registry_single()
    desired = DesiredRuntimeState(components=(_dc("zewitness", "0.0.1", witness_v1),))
    ready_status = RuntimeStatus(
        state=RuntimeState.READY,
        runtime_root=layout.root,
        active_slot_id="rt-different-slot",
        active_path=Path("/fake/slots/rt-different-slot"),
        python_executable=Path("/fake/slots/rt-different-slot/bin/python"),
        python_version="3.14.0",
    )

    def probe(runtime_python: str, dist_name: str) -> dict:
        return {"python_version": "3.14.0", "installed": False,
                "version": None, "entry_points": []}

    plan = build_deployment_plan(desired, registry, ready_status, probe_distribution=probe)
    assert plan.source_active_slot_id == "rt-different-slot"
    assert plan.blocked is False

    result = apply_deployment_plan(plan, registry=registry, runtime=rt)
    assert result.success is False
    assert "stale" in (result.reason or "").lower()

    # Active unchanged.
    assert rt.status().active_slot_id == original_active


# ---------------------------------------------------------------------------
# 4) Stale plan: ABSENT → READY
# ---------------------------------------------------------------------------

def test_stale_plan_absent_to_ready_refused(
    tmp_path: Path, witness_v1: Path,
) -> None:
    """Plan built from ABSENT, but runtime is now READY → refuse."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    original_active = rt.status().active_slot_id

    registry = _registry_single()
    desired = DesiredRuntimeState(components=(_dc("zewitness", "0.0.1", witness_v1),))
    absent_status = RuntimeStatus(
        state=RuntimeState.ABSENT,
        runtime_root=layout.root,
    )
    plan = build_deployment_plan(desired, registry, absent_status)
    assert plan.source_active_slot_id is None
    assert plan.blocked is False

    # Runtime is now READY (after rt.create()).
    result = apply_deployment_plan(plan, registry=registry, runtime=rt)
    assert result.success is False
    assert "stale" in (result.reason or "").lower()
    assert rt.status().active_slot_id == original_active


# ---------------------------------------------------------------------------
# 5) Full-state materialization: KEEP+INSTALL both installed
# ---------------------------------------------------------------------------

def test_full_state_materialization_keep_and_install(
    tmp_path: Path, witness_v1: Path, witness_second: Path,
) -> None:
    """KEEP and INSTALL steps are both materialized in the new candidate.

    The plan describes a diff (component1 KEEP, component2 INSTALL),
    but the apply engine installs every desired component regardless.
    """
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_id_before = rt.status().active_slot_id

    # Build a plan with KEEP for zewitness and INSTALL for zewitness2.
    components = (
        _dc("zewitness", "0.0.1", witness_v1),
        _dc("zewitness2", "0.1.0", witness_second),
    )

    registry = _registry()
    desired = DesiredRuntimeState(components=components)
    ready_status = RuntimeStatus(
        state=RuntimeState.READY,
        runtime_root=layout.root,
        active_slot_id=active_id_before,
        active_path=layout.slot_path(active_id_before),
        python_executable=_slot_python(layout.slot_path(active_id_before)),
        python_version="3.14.0",
    )

    # Install witness_v1 into the active slot so probe shows installed=True.
    rt.install_local_wheel(witness_v1, component_definition=WITNESS_DEF)

    def probe(runtime_python: str, dist_name: str) -> dict:
        if dist_name == "zealfie-witness":
            return {
                "python_version": "3.14.0",
                "installed": True,
                "version": "0.0.1",
                "entry_points": [
                    {"group": "console_scripts", "name": "zewitness",
                     "value": "zewitness.__main__:main"},
                ],
            }
        else:
            return {
                "python_version": "3.14.0",
                "installed": False,
                "version": None,
                "entry_points": [],
            }

    plan = build_deployment_plan(desired, registry, ready_status,
                                 probe_distribution=probe)
    assert plan.blocked is False
    # Verify KEEP + INSTALL distinction in plan.
    actions = {s.component_id: s.action for s in plan.steps}
    assert actions.get("zewitness") == DeploymentAction.KEEP
    assert actions.get("zewitness2") == DeploymentAction.INSTALL

    # Apply — this should install BOTH components in the new candidate.
    result = apply_deployment_plan(plan, registry=registry, runtime=rt)
    assert result.success is True, f"deployment failed: {result.reason}"

    # Verify new active has both components.
    new_status = rt.status()
    assert new_status.active_slot_id != active_id_before
    assert new_status.previous_slot_id == active_id_before

    # Probe both distributions.
    active_python = _slot_python(new_status.active_path)
    probe1 = probe_runtime_distribution(active_python, "zealfie-witness")
    assert probe1["installed"] is True
    assert probe1["version"] == "0.0.1"

    probe2 = probe_runtime_distribution(active_python, "zealfie-witness2")
    assert probe2["installed"] is True
    assert probe2["version"] == "0.1.0"


# ---------------------------------------------------------------------------
# 6) Active unchanged during preparation
# ---------------------------------------------------------------------------

def test_active_unchanged_during_preparation(
    tmp_path: Path, witness_v1: Path, witness_second: Path,
) -> None:
    """Active slot must remain A throughout preparation, until final activation."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_id_before = rt.status().active_slot_id
    rt.install_local_wheel(witness_v1, component_definition=WITNESS_DEF)

    components = (
        _dc("zewitness", "0.0.1", witness_v1),
        _dc("zewitness2", "0.1.0", witness_second),
    )
    registry = _registry()

    # Build plan that matches current active.
    plan = _plan_ready(components, active_slot_id=active_id_before)

    # Instrument: patch install_local_wheel to verify active hasn't changed.
    original_install = rt.install_local_wheel

    def tracked_install(*args, **kwargs):
        current_active = rt.status().active_slot_id
        assert current_active == active_id_before, (
            f"Active changed to {current_active} during install"
        )
        return original_install(*args, **kwargs)

    rt.install_local_wheel = tracked_install  # type: ignore[method-assign]

    # Also verify active unchanged after venv creation.
    import venv as _venv
    original_create = _venv.create

    def tracked_create(*args, **kwargs):
        current_active = rt.status().active_slot_id
        assert current_active == active_id_before, (
            f"Active changed to {current_active} during venv creation"
        )
        return original_create(*args, **kwargs)

    import zealfie.runtime.deployment as dep
    dep.venv.create = tracked_create  # type: ignore[attr-defined]

    try:
        result = apply_deployment_plan(plan, registry=registry, runtime=rt)
        assert result.success is True, f"deployment failed: {result.reason}"
    finally:
        dep.venv.create = original_create  # type: ignore[attr-defined]

    # After activation, active must be the new candidate.
    assert rt.status().active_slot_id != active_id_before


# ---------------------------------------------------------------------------
# 7) Atomic success: after apply, active=B and previous=A
# ---------------------------------------------------------------------------

def test_atomic_success_active_is_b_previous_is_a(
    tmp_path: Path, witness_v1: Path, witness_v2: Path,
) -> None:
    """After successful apply, active=B (candidate) and previous=A (old active)."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_a = rt.status().active_slot_id
    rt.install_local_wheel(witness_v1, component_definition=WITNESS_DEF)

    # Build plan for upgrade: zewitness v2.
    registry = _registry_single()
    desired = DesiredRuntimeState(components=(_dc("zewitness", "0.0.2", witness_v2),))
    ready_status = RuntimeStatus(
        state=RuntimeState.READY,
        runtime_root=layout.root,
        active_slot_id=active_a,
        active_path=layout.slot_path(active_a),
        python_executable=_slot_python(layout.slot_path(active_a)),
        python_version="3.14.0",
    )

    def probe(runtime_python: str, dist_name: str) -> dict:
        return {
            "python_version": "3.14.0",
            "installed": True,
            "version": "0.0.1",
            "entry_points": [
                {"group": "console_scripts", "name": "zewitness",
                 "value": "zewitness.__main__:main"},
            ],
        }

    plan = build_deployment_plan(desired, registry, ready_status,
                                 probe_distribution=probe)

    result = apply_deployment_plan(plan, registry=registry, runtime=rt)
    assert result.success is True

    final = rt.status()
    assert final.active_slot_id != active_a
    assert final.previous_slot_id == active_a
    assert result.active_slot_id == final.active_slot_id
    assert result.previous_slot_id == active_a

    # Verify the new active has v2.
    python = _slot_python(final.active_path)
    p = probe_runtime_distribution(python, "zealfie-witness")
    assert p["version"] == "0.0.2"


def test_raising_progress_callback_does_not_abort_apply(
    tmp_path: Path, witness_v1: Path, witness_v2: Path,
) -> None:
    """A progress callback that raises must not alter apply results.

    Progress is observational only: a raising callback is swallowed and
    the successful deployment still completes and activates atomically.
    """
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_a = rt.status().active_slot_id
    rt.install_local_wheel(witness_v1, component_definition=WITNESS_DEF)

    registry = _registry_single()
    desired = DesiredRuntimeState(components=(_dc("zewitness", "0.0.2", witness_v2),))
    ready_status = RuntimeStatus(
        state=RuntimeState.READY,
        runtime_root=layout.root,
        active_slot_id=active_a,
        active_path=layout.slot_path(active_a),
        python_executable=_slot_python(layout.slot_path(active_a)),
        python_version="3.14.0",
    )

    def probe(runtime_python: str, dist_name: str) -> dict:
        return {
            "python_version": "3.14.0",
            "installed": True,
            "version": "0.0.1",
            "entry_points": [
                {"group": "console_scripts", "name": "zewitness",
                 "value": "zewitness.__main__:main"},
            ],
        }

    plan = build_deployment_plan(desired, registry, ready_status,
                                 probe_distribution=probe)

    def _boom(progress):
        raise RuntimeError("callback exploded")

    result = apply_deployment_plan(
        plan, registry=registry, runtime=rt, progress_callback=_boom,
    )
    assert result.success is True, f"deployment failed: {result.reason}"

    final = rt.status()
    assert final.active_slot_id != active_a
    assert final.previous_slot_id == active_a


# ---------------------------------------------------------------------------
# 8) Artifact TOCTOU: corrupted wheel rejected, active unchanged
# ---------------------------------------------------------------------------

def test_artifact_toc_tou_corrupted_wheel_rejected(
    tmp_path: Path, witness_v1: Path,
) -> None:
    """Corrupted wheel after plan build → revalidation fails → active unchanged."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_before = rt.status().active_slot_id

    registry = _registry_single()

    # Corrupt the wheel file on disk (append garbage).
    import shutil
    corrupted = tmp_path / "corrupted.whl"
    shutil.copy2(witness_v1, corrupted)
    with open(corrupted, "ab") as f:
        f.write(b"garbage")

    # Build a VerifiedArtifact with correct metadata but wrong path/size/hash.
    good_va = _va_from_wheel(witness_v1, "zewitness", "0.0.1")
    corrupted_va = VerifiedArtifact(
        component_id=good_va.component_id,
        version=good_va.version,
        path=corrupted,
        size=good_va.size,       # Wrong — file is larger now
        sha256=good_va.sha256,   # Wrong — hash changed
        distribution_name=good_va.distribution_name,
        wheel_version=good_va.wheel_version,
    )

    corrupted_dc = DesiredComponent(
        component_id="zewitness",
        version="0.0.1",
        artifact=corrupted_va,
    )

    # Build plan matching current runtime identity so it's not stale.
    corrupted_plan = _plan_ready((corrupted_dc,), active_slot_id=active_before)

    result = apply_deployment_plan(corrupted_plan, registry=registry, runtime=rt)
    assert result.success is False
    assert (
        "revalidation" in (result.reason or "").lower()
        or "size" in (result.reason or "").lower()
        or "sha" in (result.reason or "").lower()
    ), f"unexpected reason: {result.reason}"

    # Active unchanged.
    assert rt.status().active_slot_id == active_before


# ---------------------------------------------------------------------------
# 9) Partial pip failure: first OK, second fails; B never active
# ---------------------------------------------------------------------------

def test_partial_pip_failure_active_unchanged(
    tmp_path: Path, witness_v1: Path, witness_second: Path,
) -> None:
    """First component installs OK, second fails → B never active, A remains."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_before = rt.status().active_slot_id

    components = (
        _dc("zewitness", "0.0.1", witness_v1),
        _dc("zewitness2", "0.1.0", witness_second),
    )
    # Build plan matching current runtime identity so it's not stale.
    plan = _plan_ready(components, active_slot_id=active_before)
    registry = _registry()

    # Patch install_local_wheel to make the second install fail.
    original_install = rt.install_local_wheel
    call_count = [0]

    def failing_install(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 2:
            # Second call (zewitness2) → simulate failure
            from zealfie.runtime.model import InstallResult, InstallOutcome
            return InstallResult(
                outcome=InstallOutcome.FAILED,
                distribution_name="zealfie-witness2",
                detail="simulated pip failure",
            )
        return original_install(*args, **kwargs)

    rt.install_local_wheel = failing_install  # type: ignore[method-assign]

    try:
        result = apply_deployment_plan(plan, registry=registry, runtime=rt)
        assert result.success is False
        assert "simulated pip failure" in (result.reason or "")
    finally:
        rt.install_local_wheel = original_install  # type: ignore[method-assign]

    # Active must still be the original slot.
    final_status = rt.status()
    assert final_status.active_slot_id == active_before

    # Previous should be None (only one activation happened).
    assert final_status.previous_slot_id is None


# ---------------------------------------------------------------------------
# 10) M0-8B.2: Apply-time conflict hardening — registry changed after planning
# ---------------------------------------------------------------------------

def test_apply_conflict_registry_detected_before_mutation_dup_dist(
    tmp_path: Path, witness_v1: Path, witness_second: Path,
) -> None:
    """A plan built with a coherent registry is refused at apply time when
    the registry definitions change to introduce a conflict, before candidate
    creation and with the active pointer unchanged.

    Conflict type: duplicate normalised distribution name.
    """
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)

    # Build plan with coherent registry R:
    #   zewitness  → zealfie-witness
    #   zewitness2 → zealfie-witness2
    components = (
        _dc("zewitness", "0.0.1", witness_v1),
        _dc("zewitness2", "0.1.0", witness_second),
    )

    coherent_registry = ComponentRegistry((
        ComponentDefinition(
            component_id="zewitness",
            display_name="ZeWitness",
            distribution_name="zealfie-witness",
            launch_entry_points=(EntryPointContract("console_scripts", "zewitness"),),
        ),
        ComponentDefinition(
            component_id="zewitness2",
            display_name="ZeWitness2",
            distribution_name="zealfie-witness2",
            launch_entry_points=(EntryPointContract("console_scripts", "zewitness2"),),
        ),
    ))

    desired = DesiredRuntimeState(components=components)
    absent_status = RuntimeStatus(
        state=RuntimeState.ABSENT,
        runtime_root=layout.root,
    )
    plan = build_deployment_plan(desired, coherent_registry, absent_status)
    assert plan.blocked is False
    assert plan.source_active_slot_id is None

    # Conflicting registry: same component IDs, but both now map to the SAME
    # normalised distribution name.
    conflicting_registry = ComponentRegistry((
        ComponentDefinition(
            component_id="zewitness",
            display_name="ZeWitness",
            distribution_name="zealfie-shared",
            launch_entry_points=(EntryPointContract("console_scripts", "zewitness"),),
        ),
        ComponentDefinition(
            component_id="zewitness2",
            display_name="ZeWitness2",
            distribution_name="zealfie-shared",  # CONFLICT: same normalised dist name
            launch_entry_points=(EntryPointContract("console_scripts", "zewitness2"),),
        ),
    ))

    # Count slot dirs before apply (should be empty — ABSENT).
    slots_dir = layout.slots
    slot_dirs_before = []
    if slots_dir.exists():
        slot_dirs_before = [d for d in slots_dir.iterdir() if d.is_dir()]

    result = apply_deployment_plan(plan, registry=conflicting_registry, runtime=rt)
    assert result.success is False
    assert "conflict" in (result.reason or "").lower(), f"unexpected reason: {result.reason}"

    # No candidate slot directories were created.
    slot_dirs_after = []
    if slots_dir.exists():
        slot_dirs_after = [d for d in slots_dir.iterdir() if d.is_dir()]
    assert len(slot_dirs_after) == len(slot_dirs_before), (
        f"Expected no new slot dirs, but got {len(slot_dirs_after)} "
        f"(was {len(slot_dirs_before)})"
    )

    # Runtime is still ABSENT — no active pointer was written.
    final_status = rt.status()
    assert final_status.state == RuntimeState.ABSENT


def test_apply_conflict_registry_detected_before_mutation_dup_entry_point(
    tmp_path: Path, witness_v1: Path, witness_second: Path,
) -> None:
    """A plan built with a coherent registry is refused at apply time when
    the registry definitions introduce a duplicate entry-point contract.

    Conflict type: duplicate launch entry-point group:name.
    """
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)

    components = (
        _dc("zewitness", "0.0.1", witness_v1),
        _dc("zewitness2", "0.1.0", witness_second),
    )

    coherent_registry = ComponentRegistry((
        ComponentDefinition(
            component_id="zewitness",
            display_name="ZeWitness",
            distribution_name="zealfie-witness",
            launch_entry_points=(EntryPointContract("console_scripts", "zewitness"),),
        ),
        ComponentDefinition(
            component_id="zewitness2",
            display_name="ZeWitness2",
            distribution_name="zealfie-witness2",
            launch_entry_points=(EntryPointContract("console_scripts", "zewitness2"),),
        ),
    ))

    desired = DesiredRuntimeState(components=components)
    absent_status = RuntimeStatus(
        state=RuntimeState.ABSENT,
        runtime_root=layout.root,
    )
    plan = build_deployment_plan(desired, coherent_registry, absent_status)
    assert plan.blocked is False

    # Conflicting registry: both components declare the SAME launch entry point.
    conflicting_registry = ComponentRegistry((
        ComponentDefinition(
            component_id="zewitness",
            display_name="ZeWitness",
            distribution_name="zealfie-witness",
            launch_entry_points=(EntryPointContract("console_scripts", "zeshared"),),
        ),
        ComponentDefinition(
            component_id="zewitness2",
            display_name="ZeWitness2",
            distribution_name="zealfie-witness2",
            launch_entry_points=(EntryPointContract("console_scripts", "zeshared"),),
        ),
    ))

    slots_dir = layout.slots
    slot_dirs_before = []
    if slots_dir.exists():
        slot_dirs_before = [d for d in slots_dir.iterdir() if d.is_dir()]

    result = apply_deployment_plan(plan, registry=conflicting_registry, runtime=rt)
    assert result.success is False
    assert "conflict" in (result.reason or "").lower(), f"unexpected reason: {result.reason}"

    # No candidate slot directories were created.
    slot_dirs_after = []
    if slots_dir.exists():
        slot_dirs_after = [d for d in slots_dir.iterdir() if d.is_dir()]
    assert len(slot_dirs_after) == len(slot_dirs_before)

    # Still ABSENT.
    assert rt.status().state == RuntimeState.ABSENT


# ---------------------------------------------------------------------------
# 11) M0-8B.2: Multi-component pre-activation TOCTOU — candidate corruption
#    between validate_candidate() and activate()
# ---------------------------------------------------------------------------

def test_multi_component_pre_activation_toc_tou(
    tmp_path: Path, witness_v1: Path, witness_second: Path,
) -> None:
    """Corrupt the candidate after validate_candidate() but before activate()
    in a multi-component deployment.  The pre-activation TOCTOU revalidation
    in RuntimeTransaction.activate must catch the corruption, refuse
    activation, and leave the active pointer unchanged.
    """
    import shutil
    import subprocess

    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_before = rt.status().active_slot_id

    components = (
        _dc("zewitness", "0.0.1", witness_v1),
        _dc("zewitness2", "0.1.0", witness_second),
    )
    registry = _registry()

    # Build a plan matching the current runtime so it's not stale.
    plan = _plan_ready(components, active_slot_id=active_before)

    # Monkeypatch rt.activate to corrupt the candidate after validation
    # but before calling the real activation.
    original_activate = rt.activate

    def corrupt_then_activate(txn):
        # --- CORRUPT: uninstall one component from the candidate venv. ------
        candidate_path = txn.candidate_path
        python = _slot_python(candidate_path)
        # Uninstall zealfie-witness2 so the multi-component TOCTOU recheck
        # will find it missing at activation time.
        result = subprocess.run(
            [str(python), "-m", "pip", "uninstall", "-y", "zealfie-witness2"],
            capture_output=True, text=True, timeout=60,
            cwd=str(candidate_path),
        )
        assert result.returncode == 0, (
            f"pip uninstall failed: {result.stderr}"
        )
        # --- Now call real activation — TOCTOU revalidation should fail. ---
        return original_activate(txn)

    rt.activate = corrupt_then_activate  # type: ignore[method-assign]

    try:
        result = apply_deployment_plan(plan, registry=registry, runtime=rt)
        assert result.success is False, (
            f"Expected apply to fail due to pre-activation TOCTOU "
            f"corruption, but got success"
        )
        assert (
            "candidate" in (result.reason or "").lower()
            or "activation" in (result.reason or "").lower()
            or "not found" in (result.reason or "").lower()
        ), f"unexpected reason: {result.reason}"
    finally:
        rt.activate = original_activate  # type: ignore[method-assign]

    # Active slot must still be the original (A), not the candidate (B).
    final_status = rt.status()
    assert final_status.active_slot_id == active_before, (
        f"Active slot changed to {final_status.active_slot_id}, "
        f"expected {active_before}"
    )

    # Previous should still be None (no successful activation).
    assert final_status.previous_slot_id is None


# ---------------------------------------------------------------------------
# 12) M0-8B.2: Candidate version mismatch — v2 desired, v1 installed in candidate
# ---------------------------------------------------------------------------

def test_candidate_version_mismatch_vs_desired_state(
    tmp_path: Path, witness_v1: Path, witness_v2: Path,
) -> None:
    """When the desired state requires v2 but the candidate actually contains
    v1 (e.g. because the wheel was swapped), the apply path must detect the
    version mismatch after candidate validation, refuse activation, and leave
    the active pointer unchanged.
    """
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_before = rt.status().active_slot_id

    # Install v1 into the active slot as the starting state.
    rt.install_local_wheel(witness_v1, component_definition=WITNESS_DEF)

    # Build a plan for upgrade to v2.
    registry = _registry_single()
    desired = DesiredRuntimeState(components=(_dc("zewitness", "0.0.2", witness_v2),))
    ready_status = RuntimeStatus(
        state=RuntimeState.READY,
        runtime_root=layout.root,
        active_slot_id=active_before,
        active_path=layout.slot_path(active_before),
        python_executable=_slot_python(layout.slot_path(active_before)),
        python_version="3.14.0",
    )

    def probe(runtime_python: str, dist_name: str) -> dict:
        return {
            "python_version": "3.14.0",
            "installed": True,
            "version": "0.0.1",
            "entry_points": [
                {"group": "console_scripts", "name": "zewitness",
                 "value": "zewitness.__main__:main"},
            ],
        }

    plan = build_deployment_plan(desired, registry, ready_status,
                                 probe_distribution=probe)
    assert plan.desired_state.components[0].version == "0.0.2"

    # Monkeypatch install_local_wheel: install v1 into the candidate even
    # though the plan carries v2.  Returns success so the apply path
    # proceeds past installation into validation.
    original_install = rt.install_local_wheel

    def wrong_version_install(*args, **kwargs):
        # Install v1 instead of whatever wheel the plan carries.
        return original_install(
            str(witness_v1),
            slot_id=kwargs.get("slot_id"),
            component_definition=kwargs.get("component_definition"),
        )

    rt.install_local_wheel = wrong_version_install  # type: ignore[method-assign]

    try:
        result = apply_deployment_plan(plan, registry=registry, runtime=rt)
        assert result.success is False, (
            f"Expected apply to fail due to version mismatch, got success"
        )
        assert (
            "version" in (result.reason or "").lower()
            or "mismatch" in (result.reason or "").lower()
        ), f"unexpected reason: {result.reason}"
        # Specifically check the expected vs got versions.
        assert "0.0.2" in (result.reason or ""), (
            f"reason should mention expected version 0.0.2: {result.reason}"
        )
        assert "0.0.1" in (result.reason or ""), (
            f"reason should mention observed version 0.0.1: {result.reason}"
        )
    finally:
        rt.install_local_wheel = original_install  # type: ignore[method-assign]

    # Active slot must still be the original (A), not the candidate (B).
    final_status = rt.status()
    assert final_status.active_slot_id == active_before, (
        f"Active slot changed to {final_status.active_slot_id}, "
        f"expected {active_before}"
    )

    # Previous should still be None (no successful activation).
    assert final_status.previous_slot_id is None


# ---------------------------------------------------------------------------
# 13) M0-8B.2: Candidate venv creation must NOT use clear=True
# ---------------------------------------------------------------------------

def test_candidate_venv_creation_no_clear_true(
    tmp_path: Path, witness_v1: Path,
) -> None:
    """Apply must call venv.create with clear=False (or at minimum NOT
    with clear=True).  The candidate is a fresh, non-pre-existing path;
    clear=True would violate the immutable-slot semantic.
    """
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()

    registry = _registry_single()
    components = (_dc("zewitness", "0.0.1", witness_v1),)
    plan = _plan_ready(components, active_slot_id=rt.status().active_slot_id)

    import venv as _venv
    original_create = _venv.create

    clear_kwargs_seen: list[bool] = []

    def tracked_create(*args, **kwargs):
        clear_val = kwargs.get("clear", None)
        clear_kwargs_seen.append(clear_val)
        return original_create(*args, **kwargs)

    import zealfie.runtime.deployment as dep
    dep.venv.create = tracked_create  # type: ignore[attr-defined]

    try:
        result = apply_deployment_plan(plan, registry=registry, runtime=rt)
        assert result.success is True, f"deployment failed: {result.reason}"
    finally:
        dep.venv.create = original_create  # type: ignore[attr-defined]

    # Must have been called at least once.
    assert len(clear_kwargs_seen) > 0, "venv.create was never called"

    # clear=True must never have been passed.
    for clear_val in clear_kwargs_seen:
        assert clear_val is not True, (
            f"venv.create was called with clear=True (got clear={clear_val!r}); "
            f"immutable candidate semantics require clear=False"
        )



# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _slot_python(slot_dir: Path) -> Path:
    if sys.platform == "win32":
        return slot_dir / "Scripts" / "python.exe"
    return slot_dir / "bin" / "python"
