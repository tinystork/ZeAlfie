"""Tests for M1-1B shared runtime dependency materialization.

Covers:
- Plan with RuntimeLock installs dependencies and activates
- Dependency wheel TOCTOU (corrupt wheel after lock, block before pip)
- Missing dependency wheel from lock
- Exact dependency version validation (wrong version blocks activation)
- RuntimeLock primary/component mismatch (fail before candidate creation)
- RuntimeLock primary path mismatch (fail before candidate creation)
- RuntimeLock primary size mismatch (fail before candidate creation)
- RuntimeLock primary sha256 mismatch (fail before candidate creation)
- Dependency install order (dependencies installed before components)
- pip path remains --no-index --no-deps for dependency installs
"""

from __future__ import annotations

import hashlib
import importlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

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
    RuntimeLayout,
    RuntimeState,
    RuntimeStatus,
    SharedRuntime,
    apply_deployment_plan,
    build_deployment_plan,
)


# ---------------------------------------------------------------------------
# Component definition for the witness wheel used in these tests
# ---------------------------------------------------------------------------

WITNESS_DEF = ComponentDefinition(
    component_id="zewitness",
    display_name="ZeWitness",
    distribution_name="zealfie-witness",
    launch_entry_points=(EntryPointContract("console_scripts", "zewitness"),),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _registry(*defs: ComponentDefinition) -> ComponentRegistry:
    return ComponentRegistry(defs)


def _va_from_wheel(
    wheel_path: Path, component_id: str, version: str
) -> VerifiedArtifact:
    """Build a VerifiedArtifact from a wheel file."""
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


def _build_minimal_wheel(
    output: Path, name: str, version: str, requires_dist: list[str] | None = None,
) -> Path:
    """Create a minimal wheel with given name, version, and Requires-Dist."""
    import zipfile

    # Per PEP 491, the .dist-info directory uses _ for the distribution name
    # (normalised: runs of -_. replaced by _).
    safe_name = name.replace("-", "_").replace(".", "_")
    wheel_name = f"{safe_name}-{version}-py3-none-any.whl"
    wheel_path = output / wheel_name
    dist_info = f"{safe_name}-{version}.dist-info"
    wheelfile = (
        f"Wheel-Version: 1.0\n"
        f"Generator: test\n"
        f"Root-Is-Purelib: true\n"
        f"Tag: py3-none-any\n"
    )
    metadata = f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n"
    if requires_dist:
        for req in requires_dist:
            metadata += f"Requires-Dist: {req}\n"
    record = (
        f"{dist_info}/WHEEL,,\n"
        f"{dist_info}/METADATA,,\n"
        f"{dist_info}/RECORD,,\n"
    )
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{dist_info}/WHEEL", wheelfile)
        zf.writestr(f"{dist_info}/METADATA", metadata)
        zf.writestr(f"{dist_info}/RECORD", record)
    return wheel_path


def _lock_dep(wheel_path: Path) -> LockedDependency:
    """Build a LockedDependency from a wheel file."""
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


def _make_dependency_lock(
    component_dep: LockedDependency,
    dep_deps: list[LockedDependency],
) -> RuntimeLock:
    """Build a RuntimeLock where component_dep is primary and dep_deps are dependencies."""
    locked: dict[str, LockedDependency] = {
        component_dep.name: component_dep,
    }

    # Add each dependency with required_by pointing to the component
    for dd in dep_deps:
        dep_with_rb = LockedDependency(
            name=dd.name,
            version=dd.version,
            wheel_path=dd.wheel_path,
            size=dd.size,
            sha256=dd.sha256,
            extras=dd.extras,
            required_by=frozenset({component_dep.name}),
        )
        locked[dd.name] = dep_with_rb

    return RuntimeLock(locked=locked)


def _plan_ready(
    components: tuple[DesiredComponent, ...],
    active_slot_id: str = "rt-source",
    dependency_lock: RuntimeLock | None = None,
) -> DeploymentPlan:
    """Build a READY plan with optional dependency lock."""
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


# =============================================================================
# Test 1: Plan with RuntimeLock installs dependency wheels and activates
# =============================================================================


@pytest.mark.zealfie_slow
def test_plan_with_lock_installs_dependencies_and_activates(
    tmp_path: Path, witness_v1: Path,
) -> None:
    """A plan carrying a RuntimeLock installs the dependency wheels before
    components and activates successfully."""
    # Build a simple dependency wheel ("pylib").
    dep_dir = tmp_path / "deps"
    dep_dir.mkdir()
    py_lib = _build_minimal_wheel(dep_dir, "py-lib", "2.0.0")

    # Build RuntimeLock: witness is primary, py-lib is a dependency.
    witness_dep = _lock_dep(witness_v1)
    py_dep = _lock_dep(py_lib)
    lock = _make_dependency_lock(witness_dep, [py_dep])

    # Verify lock structure
    assert lock.primary_names == frozenset({witness_dep.name})
    assert lock.dependency_names == frozenset({py_dep.name})

    # Create runtime and apply plan
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_before = rt.status().active_slot_id

    components = (_dc("zewitness", "0.0.1", witness_v1),)
    registry = _registry(WITNESS_DEF)

    plan = _plan_ready(
        components,
        active_slot_id=active_before,
        dependency_lock=lock,
    )

    result = apply_deployment_plan(plan, registry=registry, runtime=rt)
    assert result.success is True, f"deployment failed: {result.reason}"

    # Verify activation
    final = rt.status()
    assert final.active_slot_id != active_before
    assert final.previous_slot_id == active_before

    # Verify both the component AND the dependency are installed
    from zealfie.runtime.probe import probe_runtime_distribution
    python = _slot_python(final.active_path)
    probe_witness = probe_runtime_distribution(python, "zealfie-witness")
    assert probe_witness["installed"] is True
    assert probe_witness["version"] == "0.0.1"

    probe_dep = probe_runtime_distribution(python, "py-lib")
    assert probe_dep["installed"] is True
    assert probe_dep["version"] == "2.0.0"


# =============================================================================
# Test 2: Dependency wheel TOCTOU -- mutate after lock creation -> block
# =============================================================================


@pytest.mark.zealfie_slow
def test_dependency_wheel_toc_tou_mutate_blocks(
    tmp_path: Path, witness_v1: Path,
) -> None:
    """Mutate a dependency wheel after the lock is created: TOCTOU revalidation
    must block before pip, active slot unchanged."""
    dep_dir = tmp_path / "deps"
    dep_dir.mkdir()
    py_lib = _build_minimal_wheel(dep_dir, "py-lib", "2.0.0")

    witness_dep = _lock_dep(witness_v1)
    py_dep = _lock_dep(py_lib)
    lock = _make_dependency_lock(witness_dep, [py_dep])

    # Corrupt the dependency wheel on disk (truncate it)
    with open(py_lib, "ab") as f:
        f.write(b"tampered-data")

    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_before = rt.status().active_slot_id

    components = (_dc("zewitness", "0.0.1", witness_v1),)
    registry = _registry(WITNESS_DEF)

    plan = _plan_ready(
        components,
        active_slot_id=active_before,
        dependency_lock=lock,
    )

    result = apply_deployment_plan(plan, registry=registry, runtime=rt)
    assert result.success is False, (
        f"Expected TOCTOU rejection, got success"
    )
    assert "TOCTOU" in (result.reason or ""), f"unexpected reason: {result.reason}"
    assert "size" in (result.reason or "").lower(), f"unexpected reason: {result.reason}"

    # Active slot unchanged
    final = rt.status()
    assert final.active_slot_id == active_before


# =============================================================================
# Test 3: Missing dependency wheel from lock -> block
# =============================================================================


@pytest.mark.zealfie_slow
def test_missing_dependency_wheel_blocks(
    tmp_path: Path, witness_v1: Path,
) -> None:
    """A RuntimeLock that references a non-existent dependency wheel path
    must block before activation, active unchanged."""
    witness_dep = _lock_dep(witness_v1)

    # Create a LockedDependency pointing to a non-existent file
    ghost_path = tmp_path / "nonexistent" / "ghost-1.0.0-py3-none-any.whl"
    ghost_dep = LockedDependency(
        name="ghost",
        version="1.0.0",
        wheel_path=ghost_path,
        size=12345,
        sha256="a" * 64,
        required_by=frozenset({witness_dep.name}),
    )

    lock = RuntimeLock(locked={
        witness_dep.name: witness_dep,
        ghost_dep.name: ghost_dep,
    })

    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_before = rt.status().active_slot_id

    components = (_dc("zewitness", "0.0.1", witness_v1),)
    registry = _registry(WITNESS_DEF)

    plan = _plan_ready(
        components,
        active_slot_id=active_before,
        dependency_lock=lock,
    )

    result = apply_deployment_plan(plan, registry=registry, runtime=rt)
    assert result.success is False
    assert "not found" in (result.reason or "").lower(), f"unexpected reason: {result.reason}"

    # Active unchanged
    assert rt.status().active_slot_id == active_before


# =============================================================================
# Test 4: Exact dependency version validation -- wrong version blocks
# =============================================================================


@pytest.mark.zealfie_slow
def test_exact_dependency_validation_wrong_version_blocks(
    tmp_path: Path, witness_v1: Path, monkeypatch,
) -> None:
    """After dependency install, if exact version validation finds a mismatch,
    activation must be blocked and active pointer unchanged.

    Monkeypatches _validate_exact_dependency_versions to simulate a
    version mismatch without corrupting the venv.
    """
    dep_dir = tmp_path / "deps"
    dep_dir.mkdir()
    py_lib = _build_minimal_wheel(dep_dir, "py-lib", "2.0.0")

    witness_dep = _lock_dep(witness_v1)
    py_dep = _lock_dep(py_lib)
    lock = _make_dependency_lock(witness_dep, [py_dep])

    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_before = rt.status().active_slot_id

    components = (_dc("zewitness", "0.0.1", witness_v1),)
    registry = _registry(WITNESS_DEF)

    plan = _plan_ready(
        components,
        active_slot_id=active_before,
        dependency_lock=lock,
    )

    # Monkeypatch _validate_exact_dependency_versions to simulate
    # a version mismatch (cheaper than corrupting the actual venv).
    import zealfie.runtime.deployment as dep_mod

    def fake_validate(txn, lock):
        return "dependency version mismatch for 'py-lib': expected '2.0.0', got '9.9.9'"

    monkeypatch.setattr(
        dep_mod, "_validate_exact_dependency_versions",
        fake_validate,
    )

    result = apply_deployment_plan(plan, registry=registry, runtime=rt)
    assert result.success is False, (
        f"Expected version mismatch rejection, got success"
    )
    assert "version" in (result.reason or "").lower(), f"unexpected reason: {result.reason}"

    # Active unchanged
    assert rt.status().active_slot_id == active_before


@pytest.mark.zealfie_slow
def test_activation_revalidates_dependencies_before_pointer_switch(
    tmp_path: Path, witness_v1: Path, monkeypatch,
) -> None:
    """Activation must revalidate dependency distributions from the lock
    immediately before moving the active pointer.

    This exercises the transaction wiring separately from the lower-level
    probe helper tests: dependency install and candidate validation succeed,
    then the activation-time dependency revalidation hook rejects the
    candidate and the active slot remains unchanged.
    """
    dep_dir = tmp_path / "deps"
    dep_dir.mkdir()
    py_lib = _build_minimal_wheel(dep_dir, "py-lib", "2.0.0")

    witness_dep = _lock_dep(witness_v1)
    py_dep = _lock_dep(py_lib)
    lock = _make_dependency_lock(witness_dep, [py_dep])

    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_before = rt.status().active_slot_id

    components = (_dc("zewitness", "0.0.1", witness_v1),)
    registry = _registry(WITNESS_DEF)
    plan = _plan_ready(
        components,
        active_slot_id=active_before,
        dependency_lock=lock,
    )

    import zealfie.runtime.transaction as txn_mod

    calls: list[RuntimeLock] = []

    def fake_activation_revalidation(_python: Path, dependency_lock: RuntimeLock) -> str:
        calls.append(dependency_lock)
        return "dependency 'py-lib' not installed in candidate at activation time"

    monkeypatch.setattr(
        txn_mod,
        "_revalidate_dependency_distributions",
        fake_activation_revalidation,
    )

    result = apply_deployment_plan(plan, registry=registry, runtime=rt)
    assert result.success is False
    assert "activation failed" in (result.reason or "")
    assert "py-lib" in (result.reason or "")
    assert calls == [lock]
    assert rt.status().active_slot_id == active_before


# =============================================================================
# Test 5: RuntimeLock primary/component mismatch -> fail before candidate
# =============================================================================


@pytest.mark.zealfie_slow
def test_lock_primary_component_mismatch_fails_before_candidate_creation(
    tmp_path: Path, witness_v1: Path, witness_second: Path,
) -> None:
    """RuntimeLock primary entry does not match any DesiredComponent distribution
    name -> fail before candidate creation (no slots created)."""
    # Build a lock where the primary is witness_second, but the plan
    # only has zewitness.
    wrong_dep = _lock_dep(witness_second)
    lock = RuntimeLock(locked={wrong_dep.name: wrong_dep})

    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_before = rt.status().active_slot_id

    # Plan only has zewitness
    components = (_dc("zewitness", "0.0.1", witness_v1),)
    registry = _registry(WITNESS_DEF)

    plan = _plan_ready(
        components,
        active_slot_id=active_before,
        dependency_lock=lock,
    )

    # Count slot dirs before
    slots_dir = layout.slots
    slot_dirs_before = []
    if slots_dir.exists():
        slot_dirs_before = [d for d in slots_dir.iterdir() if d.is_dir()]

    result = apply_deployment_plan(plan, registry=registry, runtime=rt)
    assert result.success is False
    assert (
        "does not have an entry" in (result.reason or "")
    ), f"unexpected reason: {result.reason}"

    # No new slots created (fail before candidate creation)
    slot_dirs_after = []
    if slots_dir.exists():
        slot_dirs_after = [d for d in slots_dir.iterdir() if d.is_dir()]
    assert len(slot_dirs_after) == len(slot_dirs_before), (
        f"Expected no new slots, got {len(slot_dirs_after)} (was {len(slot_dirs_before)})"
    )

    # Active unchanged
    assert rt.status().active_slot_id == active_before


@pytest.mark.zealfie_slow
def test_lock_primary_version_mismatch_fails_before_candidate(
    tmp_path: Path, witness_v1: Path,
) -> None:
    """RuntimeLock primary version differs from DesiredComponent version,
    with all other fields matching -> coherence fails before candidate."""
    # Build a LockedDependency from the SAME witness_v1 wheel as the
    # DesiredComponent, but with a different version string.
    # This ensures only version differs so the coherence check hits
    # the version comparison.
    dc_artifact = _va_from_wheel(witness_v1, "zewitness", "0.0.1")

    # Construct a LockedDependency with same path/size/sha256 but wrong version.
    wrong_version_dep = LockedDependency(
        name=normalise_distribution_name(dc_artifact.distribution_name),
        version="9.9.9",
        wheel_path=witness_v1,
        size=dc_artifact.size,
        sha256=dc_artifact.sha256,
    )
    lock = RuntimeLock(locked={wrong_version_dep.name: wrong_version_dep})

    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_before = rt.status().active_slot_id

    # Plan has v0.0.1
    components = (_dc("zewitness", "0.0.1", witness_v1),)
    registry = _registry(WITNESS_DEF)

    plan = _plan_ready(
        components,
        active_slot_id=active_before,
        dependency_lock=lock,
    )

    # Count slot dirs before
    slots_dir = layout.slots
    slot_dirs_before = []
    if slots_dir.exists():
        slot_dirs_before = [d for d in slots_dir.iterdir() if d.is_dir()]

    result = apply_deployment_plan(plan, registry=registry, runtime=rt)
    assert result.success is False
    assert "version" in (result.reason or "").lower(), f"unexpected reason: {result.reason}"

    # No new slots
    slot_dirs_after = []
    if slots_dir.exists():
        slot_dirs_after = [d for d in slots_dir.iterdir() if d.is_dir()]
    assert len(slot_dirs_after) == len(slot_dirs_before)

    assert rt.status().active_slot_id == active_before


# =============================================================================
# Test 6: RuntimeLock primary path mismatch -> fail before candidate
# =============================================================================


@pytest.mark.zealfie_slow
def test_lock_primary_path_mismatch_fails_before_candidate(
    tmp_path: Path, witness_v1: Path, witness_second: Path,
) -> None:
    """RuntimeLock primary wheel_path differs from DesiredComponent artifact
    path -> fail before candidate creation (no new slot dir)."""
    # Build a locked dep from witness_second but with the same normalised
    # distribution name as witness so it maps to the same component.
    # Then the path will differ.
    artifact = _va_from_wheel(witness_v1, "zewitness", "0.0.1")
    wrong_path_dep = LockedDependency(
        name=normalise_distribution_name(artifact.distribution_name),
        version=artifact.wheel_version,
        wheel_path=witness_second,  # DIFFERENT path
        size=artifact.size,
        sha256=artifact.sha256,
    )
    lock = RuntimeLock(locked={wrong_path_dep.name: wrong_path_dep})

    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_before = rt.status().active_slot_id

    components = (_dc("zewitness", "0.0.1", witness_v1),)
    registry = _registry(WITNESS_DEF)

    plan = _plan_ready(
        components,
        active_slot_id=active_before,
        dependency_lock=lock,
    )

    slots_dir = layout.slots
    slot_dirs_before = []
    if slots_dir.exists():
        slot_dirs_before = [d for d in slots_dir.iterdir() if d.is_dir()]

    result = apply_deployment_plan(plan, registry=registry, runtime=rt)
    assert result.success is False
    assert "wheel_path" in (result.reason or ""), f"unexpected reason: {result.reason}"

    # No new slots created
    slot_dirs_after = []
    if slots_dir.exists():
        slot_dirs_after = [d for d in slots_dir.iterdir() if d.is_dir()]
    assert len(slot_dirs_after) == len(slot_dirs_before), (
        f"Expected no new slots, got {len(slot_dirs_after)} (was {len(slot_dirs_before)})"
    )

    assert rt.status().active_slot_id == active_before


# =============================================================================
# Test 7: RuntimeLock primary size mismatch -> fail before candidate
# =============================================================================


@pytest.mark.zealfie_slow
def test_lock_primary_size_mismatch_fails_before_candidate(
    tmp_path: Path, witness_v1: Path,
) -> None:
    """RuntimeLock primary size differs from DesiredComponent artifact
    size -> fail before candidate creation (no new slot dir)."""
    artifact = _va_from_wheel(witness_v1, "zewitness", "0.0.1")
    wrong_size_dep = LockedDependency(
        name=normalise_distribution_name(artifact.distribution_name),
        version=artifact.wheel_version,
        wheel_path=witness_v1,
        size=99999,  # WRONG size
        sha256=artifact.sha256,
    )
    lock = RuntimeLock(locked={wrong_size_dep.name: wrong_size_dep})

    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_before = rt.status().active_slot_id

    components = (_dc("zewitness", "0.0.1", witness_v1),)
    registry = _registry(WITNESS_DEF)

    plan = _plan_ready(
        components,
        active_slot_id=active_before,
        dependency_lock=lock,
    )

    slots_dir = layout.slots
    slot_dirs_before = []
    if slots_dir.exists():
        slot_dirs_before = [d for d in slots_dir.iterdir() if d.is_dir()]

    result = apply_deployment_plan(plan, registry=registry, runtime=rt)
    assert result.success is False
    assert "size" in (result.reason or ""), f"unexpected reason: {result.reason}"

    # No new slots created
    slot_dirs_after = []
    if slots_dir.exists():
        slot_dirs_after = [d for d in slots_dir.iterdir() if d.is_dir()]
    assert len(slot_dirs_after) == len(slot_dirs_before), (
        f"Expected no new slots, got {len(slot_dirs_after)} (was {len(slot_dirs_before)})"
    )

    assert rt.status().active_slot_id == active_before


# =============================================================================
# Test 8: RuntimeLock primary sha256 mismatch -> fail before candidate
# =============================================================================


@pytest.mark.zealfie_slow
def test_lock_primary_sha256_mismatch_fails_before_candidate(
    tmp_path: Path, witness_v1: Path,
) -> None:
    """RuntimeLock primary sha256 differs from DesiredComponent artifact
    sha256 -> fail before candidate creation (no new slot dir)."""
    artifact = _va_from_wheel(witness_v1, "zewitness", "0.0.1")
    wrong_sha_dep = LockedDependency(
        name=normalise_distribution_name(artifact.distribution_name),
        version=artifact.wheel_version,
        wheel_path=witness_v1,
        size=artifact.size,
        sha256="b" * 64,  # WRONG sha256
    )
    lock = RuntimeLock(locked={wrong_sha_dep.name: wrong_sha_dep})

    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_before = rt.status().active_slot_id

    components = (_dc("zewitness", "0.0.1", witness_v1),)
    registry = _registry(WITNESS_DEF)

    plan = _plan_ready(
        components,
        active_slot_id=active_before,
        dependency_lock=lock,
    )

    slots_dir = layout.slots
    slot_dirs_before = []
    if slots_dir.exists():
        slot_dirs_before = [d for d in slots_dir.iterdir() if d.is_dir()]

    result = apply_deployment_plan(plan, registry=registry, runtime=rt)
    assert result.success is False
    assert "sha256" in (result.reason or ""), f"unexpected reason: {result.reason}"

    # No new slots created
    slot_dirs_after = []
    if slots_dir.exists():
        slot_dirs_after = [d for d in slots_dir.iterdir() if d.is_dir()]
    assert len(slot_dirs_after) == len(slot_dirs_before), (
        f"Expected no new slots, got {len(slot_dirs_after)} (was {len(slot_dirs_before)})"
    )

    assert rt.status().active_slot_id == active_before


# =============================================================================
# Test 9: Dependency install order — dependencies before components
# =============================================================================


@pytest.mark.zealfie_slow
def test_dependency_installed_before_components(
    tmp_path: Path, witness_v1: Path, monkeypatch,
) -> None:
    """Dependencies from the RuntimeLock are installed BEFORE any component
    wheel.  Probes install order by recording distribution names during
    install_local_wheel calls."""
    dep_dir = tmp_path / "deps"
    dep_dir.mkdir()
    py_lib = _build_minimal_wheel(dep_dir, "py-lib", "2.0.0")

    witness_dep = _lock_dep(witness_v1)
    py_dep = _lock_dep(py_lib)
    lock = _make_dependency_lock(witness_dep, [py_dep])

    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_before = rt.status().active_slot_id

    components = (_dc("zewitness", "0.0.1", witness_v1),)
    registry = _registry(WITNESS_DEF)

    plan = _plan_ready(
        components,
        active_slot_id=active_before,
        dependency_lock=lock,
    )

    # Record install order via monkeypatch.
    install_order: list[str] = []
    original_install = rt.install_local_wheel

    def tracking_install(wheel_path, slot_id, component_definition=None):
        info = inspect_wheel(wheel_path)
        install_order.append(normalise_distribution_name(info.distribution_name))
        return original_install(wheel_path, slot_id=slot_id,
                                component_definition=component_definition)

    monkeypatch.setattr(rt, "install_local_wheel", tracking_install)

    result = apply_deployment_plan(plan, registry=registry, runtime=rt)
    assert result.success is True, f"deployment failed: {result.reason}"

    # py-lib (dependency) must appear BEFORE zealfie-witness (component)
    dep_idx = install_order.index("py-lib") if "py-lib" in install_order else -1
    comp_idx = install_order.index("zealfie-witness") if "zealfie-witness" in install_order else -1
    assert dep_idx >= 0, f"dependency py-lib not found in install order: {install_order}"
    assert comp_idx >= 0, f"component zealfie-witness not found in install order: {install_order}"
    assert dep_idx < comp_idx, (
        f"Dependency py-lib (idx {dep_idx}) must be installed before "
        f"component zealfie-witness (idx {comp_idx}): {install_order}"
    )


# =============================================================================
# Test 10: pip path remains --no-index --no-deps for dependency installs
# =============================================================================


@pytest.mark.zealfie_slow
def test_dependency_install_uses_no_index_no_deps(
    tmp_path: Path, witness_v1: Path, monkeypatch,
) -> None:
    """Dependency wheels are installed with --no-index --no-deps."""
    dep_dir = tmp_path / "deps"
    dep_dir.mkdir()
    py_lib = _build_minimal_wheel(dep_dir, "py-lib", "2.0.0")

    witness_dep = _lock_dep(witness_v1)
    py_dep = _lock_dep(py_lib)
    lock = _make_dependency_lock(witness_dep, [py_dep])

    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_before = rt.status().active_slot_id

    components = (_dc("zewitness", "0.0.1", witness_v1),)
    registry = _registry(WITNESS_DEF)

    plan = _plan_ready(
        components,
        active_slot_id=active_before,
        dependency_lock=lock,
    )

    # Capture all subprocess.run calls globally.
    original_run = subprocess.run
    captured_calls: list[list[str]] = []

    def tracking_run(cmd, **kwargs):
        captured_calls.append(list(cmd) if isinstance(cmd, list) else str(cmd))
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", tracking_run)

    result = apply_deployment_plan(plan, registry=registry, runtime=rt)
    assert result.success is True, f"deployment failed: {result.reason}"

    # Find pip install calls
    pip_calls = [c for c in captured_calls if "-m" in c and "pip" in c and "install" in c]
    assert len(pip_calls) >= 1, f"No pip install calls found in {captured_calls}"

    for call in pip_calls:
        # --no-index must be present
        assert "--no-index" in call, f"pip call missing --no-index: {call}"
        # --no-deps must be present
        assert "--no-deps" in call, f"pip call missing --no-deps: {call}"


# =============================================================================
# Test 11: Plan without dependency lock still works (existing behavior)
# =============================================================================


@pytest.mark.zealfie_slow
def test_plan_without_lock_still_works(
    tmp_path: Path, witness_v1: Path,
) -> None:
    """A plan with dependency_lock=None behaves identically to before M1-1B."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_before = rt.status().active_slot_id

    components = (_dc("zewitness", "0.0.1", witness_v1),)
    registry = _registry(WITNESS_DEF)

    plan = _plan_ready(
        components,
        active_slot_id=active_before,
        dependency_lock=None,  # Explicitly None
    )

    result = apply_deployment_plan(plan, registry=registry, runtime=rt)
    assert result.success is True, f"deployment failed: {result.reason}"

    final = rt.status()
    assert final.active_slot_id != active_before

    from zealfie.runtime.probe import probe_runtime_distribution
    probe = probe_runtime_distribution(_slot_python(final.active_path), "zealfie-witness")
    assert probe["installed"] is True
    assert probe["version"] == "0.0.1"


# =============================================================================
# M1-1B edge: multiple components, dependency lock with shared deps
# =============================================================================


@pytest.mark.zealfie_slow
def test_multi_component_with_shared_dependency(
    tmp_path: Path, witness_v1: Path, witness_second: Path,
) -> None:
    """Two components with a shared dependency: dependency is installed once,
    both components activate successfully."""
    dep_dir = tmp_path / "deps"
    dep_dir.mkdir()
    py_lib = _build_minimal_wheel(dep_dir, "py-lib", "2.0.0")

    w1_dep = _lock_dep(witness_v1)
    w2_dep = _lock_dep(witness_second)
    py_dep = _lock_dep(py_lib)

    # Both components depend on py-lib
    locked: dict[str, LockedDependency] = {
        w1_dep.name: w1_dep,
        w2_dep.name: w2_dep,
        py_dep.name: LockedDependency(
            name=py_dep.name, version=py_dep.version,
            wheel_path=py_dep.wheel_path, size=py_dep.size,
            sha256=py_dep.sha256,
            required_by=frozenset({w1_dep.name, w2_dep.name}),
        ),
    }
    lock = RuntimeLock(locked=locked)

    assert lock.primary_names == frozenset({w1_dep.name, w2_dep.name})
    assert lock.dependency_names == frozenset({py_dep.name})

    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_before = rt.status().active_slot_id

    components = (
        _dc("zewitness", "0.0.1", witness_v1),
        _dc("zewitness2", "0.1.0", witness_second),
    )
    registry = _registry(WITNESS_DEF, ComponentDefinition(
        component_id="zewitness2",
        display_name="ZeWitness2",
        distribution_name="zealfie-witness2",
        launch_entry_points=(EntryPointContract("console_scripts", "zewitness2"),),
    ))

    plan = _plan_ready(
        components,
        active_slot_id=active_before,
        dependency_lock=lock,
    )

    result = apply_deployment_plan(plan, registry=registry, runtime=rt)
    assert result.success is True, f"deployment failed: {result.reason}"

    final = rt.status()
    from zealfie.runtime.probe import probe_runtime_distribution
    python = _slot_python(final.active_path)

    for dist_name in ("zealfie-witness", "zealfie-witness2", "py-lib"):
        probe = probe_runtime_distribution(python, dist_name)
        assert probe["installed"] is True, f"{dist_name} not installed"

    assert final.previous_slot_id == active_before


# =============================================================================
# Verify DeploymentPlan.dependency_lock is None by default
# =============================================================================


def test_deployment_plan_dependency_lock_defaults_to_none() -> None:
    """DeploymentPlan constructed without dependency_lock has it as None."""
    from zealfie.releases.model import VerifiedArtifact as VA

    plan = DeploymentPlan(
        desired_state=DesiredRuntimeState(
            components=(
                DesiredComponent(
                    component_id="test",
                    version="1.0.0",
                    artifact=VA(
                        component_id="test", version="1.0.0",
                        path=Path("/fake/test.whl"), size=100,
                        sha256="a" * 64, distribution_name="test",
                        wheel_version="1.0.0",
                    ),
                ),
            )
        ),
        runtime_state=RuntimeState.ABSENT,
        steps=(),
    )
    assert plan.dependency_lock is None


def test_deployment_plan_sets_dependency_lock() -> None:
    """DeploymentPlan can be constructed with a dependency_lock."""
    from zealfie.releases.model import VerifiedArtifact as VA

    lock = RuntimeLock(locked={})
    plan = DeploymentPlan(
        desired_state=DesiredRuntimeState(
            components=(
                DesiredComponent(
                    component_id="test",
                    version="1.0.0",
                    artifact=VA(
                        component_id="test", version="1.0.0",
                        path=Path("/fake/test.whl"), size=100,
                        sha256="a" * 64, distribution_name="test",
                        wheel_version="1.0.0",
                    ),
                ),
            )
        ),
        runtime_state=RuntimeState.ABSENT,
        steps=(),
        dependency_lock=lock,
    )
    assert plan.dependency_lock is lock
