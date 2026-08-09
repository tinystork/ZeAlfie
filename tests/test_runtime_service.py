"""Tests for ZeAlfieService — offline deployment planning (M0-9.1)
and apply/rollback orchestration (M0-9.2).

All read-only tests use synthetic/fake runtimes.
Witness apply/rollback tests use temp runtime roots and session-scoped
witness wheels.
"""

from __future__ import annotations

import hashlib
import shutil
import textwrap
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.zealfie_slow

from zealfie.app import OfflineReleaseError, ZeAlfieService
from zealfie.components.model import ComponentDefinition, EntryPointContract
from zealfie.components.registry import ComponentRegistry
from zealfie.releases.model import HostTarget
from zealfie.runtime.deployment import apply_deployment_plan
from zealfie.runtime.layout import RuntimeLayout
from zealfie.runtime.manager import SharedRuntime
from zealfie.runtime.model import (
    RuntimeReasonCode,
    DeploymentResult,
    RuntimeState,
    RuntimeStatus,
)
from zealfie.runtime.planning import (
    DeploymentAction,
    DeploymentPlan,
    DeploymentReasonCode,
    DesiredRuntimeState,
)
from zealfie.runtime.probe import probe_runtime_distribution


# ---------------------------------------------------------------------------
# Synthetic runtime that never pays the real-machine cost.
# ---------------------------------------------------------------------------


class _FakeSharedRuntime:
    """A SharedRuntime replacement that returns a canned status.

    Tests that need to inject a specific runtime status use this.
    """

    def __init__(self, status: RuntimeStatus) -> None:
        self._status = status

    def status(self) -> RuntimeStatus:
        return self._status

    def rollback(self) -> RuntimeStatus:
        """Simulate successful rollback."""
        return RuntimeStatus(
            state=RuntimeState.READY,
            runtime_root=Path("/fake/runtime"),
            active_slot_id="rt-rollback0000",
            reason_code=RuntimeReasonCode.RUNTIME_READY,
        )


def _absent_status() -> RuntimeStatus:
    return RuntimeStatus(
        state=RuntimeState.ABSENT,
        runtime_root=Path("/fake/runtime"),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------



WITNESS_DEF = ComponentDefinition(
    "zewitness",
    "ZeWitness",
    "zealfie-witness",
    (EntryPointContract("console_scripts", "zewitness"),),
)

WITNESS2_DEF = ComponentDefinition(
    "zewitness2",
    "ZeWitness2",
    "zealfie-witness2",
    (EntryPointContract("console_scripts", "zewitness2"),),
)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _copy_wheel_as(wheel_path: Path, root: Path, filename: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    copied = root / filename
    shutil.copy2(wheel_path, copied)
    return copied


def _write_manifest(
    release_dir: Path,
    component_id: str,
    version: str,
    filename: str,
    sha256_val: str,
    size_val: int,
    *,
    python_tag: str = "py3",
    abi_tag: str = "none",
    platform_tag: str = "any",
) -> None:
    toml_text = textwrap.dedent(f"""\
        schema_version = 1
        component_id = "{component_id}"
        version = "{version}"

        [[artifacts]]
        filename = "{filename}"
        size = {size_val}
        sha256 = "{sha256_val}"
        python_tag = "{python_tag}"
        abi_tag = "{abi_tag}"
        platform_tag = "{platform_tag}"
    """)
    (release_dir / f"{component_id}.toml").write_text(toml_text)


# ===========================================================================
# M0-9.1 Tests — read-only planning
# ===========================================================================


# 1) Complete release set succeeds — plan generated
# ---------------------------------------------------------------------------


def test_complete_release_set_succeeds(
    tmp_path, witness_wheel, witness2_wheel
) -> None:
    """A release_dir with manifests for every registry component produces a plan."""
    rd = tmp_path / "release"
    rd.mkdir()

    # Copy wheels
    fn1 = "zealfie_witness-0.0.1-py3-none-any.whl"
    fn2 = "zealfie_witness2-0.1.0-py3-none-any.whl"
    w1 = _copy_wheel_as(witness_wheel, rd, fn1)
    w2 = _copy_wheel_as(witness2_wheel, rd, fn2)

    # Write manifests
    _write_manifest(
        rd, "zewitness", "0.0.1", fn1,
        _sha256(w1), w1.stat().st_size,
    )
    _write_manifest(
        rd, "zewitness2", "0.1.0", fn2,
        _sha256(w2), w2.stat().st_size,
    )

    registry = ComponentRegistry([WITNESS_DEF, WITNESS2_DEF])
    service = ZeAlfieService(
        registry=registry,
        runtime=_FakeSharedRuntime(_absent_status()),
    )

    desired = service.resolve_offline_release_set(rd)
    assert isinstance(desired, DesiredRuntimeState)
    assert len(desired.components) == 2
    ids = [c.component_id for c in desired.components]
    assert ids == ["zewitness", "zewitness2"]

    # Plan generation — read-only, ABSENT → INSTALL for all.
    plan = service.plan_offline_deployment(rd)
    assert isinstance(plan, DeploymentPlan)
    assert plan.runtime_state == RuntimeState.ABSENT
    assert plan.blocked is False
    assert len(plan.steps) == 2
    for step in plan.steps:
        assert step.action == DeploymentAction.INSTALL
        assert step.reason_code == DeploymentReasonCode.RUNTIME_ABSENT
        assert step.artifact is not None


# 2) Missing component manifest fails closed
# ---------------------------------------------------------------------------


def test_missing_component_manifest_fails_closed(
    tmp_path, witness_wheel, witness2_wheel
) -> None:
    """When a registry component has no manifest, fail closed."""
    rd = tmp_path / "release"
    rd.mkdir()

    fn1 = "zealfie_witness-0.0.1-py3-none-any.whl"
    w1 = _copy_wheel_as(witness_wheel, rd, fn1)
    _write_manifest(rd, "zewitness", "0.0.1", fn1, _sha256(w1), w1.stat().st_size)
    # zewitness2 manifest is missing.

    registry = ComponentRegistry([WITNESS_DEF, WITNESS2_DEF])
    service = ZeAlfieService(
        registry=registry,
        runtime=_FakeSharedRuntime(_absent_status()),
    )

    with pytest.raises(OfflineReleaseError, match="missing release manifest"):
        service.resolve_offline_release_set(rd)


# 3) Unknown/extra manifest fails closed
# ---------------------------------------------------------------------------


def test_extra_unknown_manifest_fails_closed(
    tmp_path, witness_wheel
) -> None:
    """A .toml file whose stem is not a known component → fail closed."""
    rd = tmp_path / "release"
    rd.mkdir()

    fn1 = "zealfie_witness-0.0.1-py3-none-any.whl"
    w1 = _copy_wheel_as(witness_wheel, rd, fn1)
    _write_manifest(rd, "zewitness", "0.0.1", fn1, _sha256(w1), w1.stat().st_size)

    # Write an extra manifest not in the registry.
    (rd / "zeintruder.toml").write_text(textwrap.dedent("""\
        schema_version = 1
        component_id = "zeintruder"
        version = "1.0.0"

        [[artifacts]]
        filename = "doesnotexist.whl"
        size = 0
        sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    """))

    registry = ComponentRegistry([WITNESS_DEF])
    service = ZeAlfieService(
        registry=registry,
        runtime=_FakeSharedRuntime(_absent_status()),
    )

    with pytest.raises(OfflineReleaseError, match="unknown release manifest"):
        service.resolve_offline_release_set(rd)


# 4) Bad manifest — parse error propagates
# ---------------------------------------------------------------------------


def test_bad_manifest_parse_error_propagates(
    tmp_path, witness_wheel
) -> None:
    """A syntactically invalid manifest → OfflineReleaseError."""
    rd = tmp_path / "release"
    rd.mkdir()

    fn1 = "zealfie_witness-0.0.1-py3-none-any.whl"
    w1 = _copy_wheel_as(witness_wheel, rd, fn1)

    # Broken TOML
    (rd / "zewitness.toml").write_text("this is not valid toml {{{")

    registry = ComponentRegistry([WITNESS_DEF])
    service = ZeAlfieService(
        registry=registry,
        runtime=_FakeSharedRuntime(_absent_status()),
    )

    with pytest.raises(OfflineReleaseError, match="invalid release manifest"):
        service.resolve_offline_release_set(rd)


# 5) Bad artifact resolution propagates clear failure
# ---------------------------------------------------------------------------


def test_missing_wheel_artifact_propagates_failure(
    tmp_path, witness_wheel
) -> None:
    """Manifest references a wheel not present → OfflineReleaseError."""
    rd = tmp_path / "release"
    rd.mkdir()

    fn = "zealfie_witness-0.0.1-py3-none-any.whl"
    w1 = _copy_wheel_as(witness_wheel, rd, fn)
    sha = _sha256(w1)
    size = w1.stat().st_size

    # Manifest references a different (nonexistent) filename.
    _write_manifest(rd, "zewitness", "0.0.1", "nonexistent.whl", sha, size)

    registry = ComponentRegistry([WITNESS_DEF])
    service = ZeAlfieService(
        registry=registry,
        runtime=_FakeSharedRuntime(_absent_status()),
    )

    with pytest.raises(OfflineReleaseError, match="cannot resolve release"):
        service.resolve_offline_release_set(rd)


def test_tampered_sha256_propagates_failure(
    tmp_path, witness_wheel
) -> None:
    """Manifest with wrong SHA256 → OfflineReleaseError."""
    rd = tmp_path / "release"
    rd.mkdir()

    fn = "zealfie_witness-0.0.1-py3-none-any.whl"
    w1 = _copy_wheel_as(witness_wheel, rd, fn)
    size = w1.stat().st_size
    bad_sha = "a" * 64

    _write_manifest(rd, "zewitness", "0.0.1", fn, bad_sha, size)

    registry = ComponentRegistry([WITNESS_DEF])
    service = ZeAlfieService(
        registry=registry,
        runtime=_FakeSharedRuntime(_absent_status()),
    )

    with pytest.raises(OfflineReleaseError, match="cannot resolve release"):
        service.resolve_offline_release_set(rd)


def test_manifest_component_id_mismatch_fails(
    tmp_path, witness_wheel
) -> None:
    """Manifest filename matches but declared component_id differs."""
    rd = tmp_path / "release"
    rd.mkdir()

    fn = "zealfie_witness-0.0.1-py3-none-any.whl"
    w1 = _copy_wheel_as(witness_wheel, rd, fn)
    sha = _sha256(w1)
    size = w1.stat().st_size

    # Manifest for "zewitness" but declared component_id is different.
    toml_text = textwrap.dedent(f"""\
        schema_version = 1
        component_id = "zeother"
        version = "0.0.1"

        [[artifacts]]
        filename = "{fn}"
        size = {size}
        sha256 = "{sha}"
        python_tag = "py3"
        abi_tag = "none"
        platform_tag = "any"
    """)
    (rd / "zewitness.toml").write_text(toml_text)

    registry = ComponentRegistry([WITNESS_DEF])
    service = ZeAlfieService(
        registry=registry,
        runtime=_FakeSharedRuntime(_absent_status()),
    )

    with pytest.raises(
        OfflineReleaseError, match="manifest component_id mismatch"
    ):
        service.resolve_offline_release_set(rd)


# 6) Plan generation: INSTALL for ABSENT runtime with witness registry
# ---------------------------------------------------------------------------


def test_plan_absent_runtime_install_all(
    tmp_path, witness_wheel, witness2_wheel
) -> None:
    """ABSENT runtime → every component gets INSTALL."""
    rd = tmp_path / "release"
    rd.mkdir()

    fn1 = "zealfie_witness-0.0.1-py3-none-any.whl"
    fn2 = "zealfie_witness2-0.1.0-py3-none-any.whl"
    w1 = _copy_wheel_as(witness_wheel, rd, fn1)
    w2 = _copy_wheel_as(witness2_wheel, rd, fn2)

    _write_manifest(rd, "zewitness", "0.0.1", fn1, _sha256(w1), w1.stat().st_size)
    _write_manifest(rd, "zewitness2", "0.1.0", fn2, _sha256(w2), w2.stat().st_size)

    registry = ComponentRegistry([WITNESS_DEF, WITNESS2_DEF])
    service = ZeAlfieService(
        registry=registry,
        runtime=_FakeSharedRuntime(_absent_status()),
    )

    plan = service.plan_offline_deployment(rd)

    assert plan.runtime_state == RuntimeState.ABSENT
    assert plan.blocked is False
    assert len(plan.steps) == 2
    assert [s.component_id for s in plan.steps] == ["zewitness", "zewitness2"]
    for step in plan.steps:
        assert step.action == DeploymentAction.INSTALL
        assert step.reason_code == DeploymentReasonCode.RUNTIME_ABSENT
        assert step.artifact is not None
        assert step.artifact.path.exists()


# 7) Service dependency injection — tests don't touch real user runtime
# ---------------------------------------------------------------------------


def test_service_uses_injected_runtime(
    tmp_path, witness_wheel
) -> None:
    """Service with fake runtime doesn't touch real filesystem."""
    rd = tmp_path / "release"
    rd.mkdir()

    fn = "zealfie_witness-0.0.1-py3-none-any.whl"
    w1 = _copy_wheel_as(witness_wheel, rd, fn)
    _write_manifest(rd, "zewitness", "0.0.1", fn, _sha256(w1), w1.stat().st_size)

    registry = ComponentRegistry([WITNESS_DEF])
    fake_rt = _FakeSharedRuntime(_absent_status())

    service = ZeAlfieService(registry=registry, runtime=fake_rt)
    plan = service.plan_offline_deployment(rd)

    assert plan.runtime_state == RuntimeState.ABSENT
    assert plan.steps[0].action == DeploymentAction.INSTALL


def test_service_uses_injected_host(tmp_path, witness_wheel) -> None:
    """Service with an explicit host target still resolves correctly."""
    rd = tmp_path / "release"
    rd.mkdir()

    fn = "zealfie_witness-0.0.1-py3-none-any.whl"
    w1 = _copy_wheel_as(witness_wheel, rd, fn)
    _write_manifest(rd, "zewitness", "0.0.1", fn, _sha256(w1), w1.stat().st_size)

    registry = ComponentRegistry([WITNESS_DEF])
    host = HostTarget("py312", "cp312", "linux_x86_64")
    service = ZeAlfieService(
        registry=registry,
        runtime=_FakeSharedRuntime(_absent_status()),
        host=host,
    )

    plan = service.plan_offline_deployment(rd)
    assert plan.steps[0].action == DeploymentAction.INSTALL


# 8) Empty registry fails early
# ---------------------------------------------------------------------------


def test_empty_registry_fails_early() -> None:
    """An empty component registry → OfflineReleaseError."""
    registry = ComponentRegistry([])
    service = ZeAlfieService(
        registry=registry,
        runtime=_FakeSharedRuntime(_absent_status()),
    )
    with pytest.raises(OfflineReleaseError, match="registry is empty"):
        service.resolve_offline_release_set(Path("/nonexistent"))


# 9) Non-directory release_dir fails early
# ---------------------------------------------------------------------------


def test_nonexistent_release_dir_fails(tmp_path) -> None:
    """A nonexistent release_dir → OfflineReleaseError."""
    registry = ComponentRegistry([WITNESS_DEF])
    service = ZeAlfieService(
        registry=registry,
        runtime=_FakeSharedRuntime(_absent_status()),
    )
    with pytest.raises(OfflineReleaseError, match="does not exist"):
        service.resolve_offline_release_set(tmp_path / "no_such_dir")


# 10) Plan is deterministic — same release_dir, same plan
# ---------------------------------------------------------------------------


def test_plan_is_deterministic(tmp_path, witness_wheel, witness2_wheel) -> None:
    """Two calls with the same inputs produce identical plans."""
    rd = tmp_path / "release"
    rd.mkdir()

    fn1 = "zealfie_witness-0.0.1-py3-none-any.whl"
    fn2 = "zealfie_witness2-0.1.0-py3-none-any.whl"
    w1 = _copy_wheel_as(witness_wheel, rd, fn1)
    w2 = _copy_wheel_as(witness2_wheel, rd, fn2)

    _write_manifest(rd, "zewitness", "0.0.1", fn1, _sha256(w1), w1.stat().st_size)
    _write_manifest(rd, "zewitness2", "0.1.0", fn2, _sha256(w2), w2.stat().st_size)

    registry = ComponentRegistry([WITNESS_DEF, WITNESS2_DEF])
    service = ZeAlfieService(
        registry=registry,
        runtime=_FakeSharedRuntime(_absent_status()),
    )

    plan1 = service.plan_offline_deployment(rd)
    plan2 = service.plan_offline_deployment(rd)

    assert plan1.runtime_state == plan2.runtime_state
    assert plan1.blocked == plan2.blocked
    assert [s.component_id for s in plan1.steps] == [
        s.component_id for s in plan2.steps
    ]
    assert [s.action for s in plan1.steps] == [s.action for s in plan2.steps]


# 11) verify plan_offline_deployment returns correct type
# ---------------------------------------------------------------------------


def test_plan_offline_deployment_returns_deployment_plan(
    tmp_path, witness_wheel
) -> None:
    """The return type is DeploymentPlan."""
    rd = tmp_path / "release"
    rd.mkdir()

    fn = "zealfie_witness-0.0.1-py3-none-any.whl"
    w1 = _copy_wheel_as(witness_wheel, rd, fn)
    _write_manifest(rd, "zewitness", "0.0.1", fn, _sha256(w1), w1.stat().st_size)

    registry = ComponentRegistry([WITNESS_DEF])
    service = ZeAlfieService(
        registry=registry,
        runtime=_FakeSharedRuntime(_absent_status()),
    )

    plan = service.plan_offline_deployment(rd)
    assert isinstance(plan, DeploymentPlan)
    assert plan.desired_state is not None
    assert len(plan.steps) == 1


# 12) Non-toml files in release_dir are ignored
# ---------------------------------------------------------------------------


def test_non_toml_files_ignored(tmp_path, witness_wheel) -> None:
    """Non-.toml files in release_dir don't cause failures."""
    rd = tmp_path / "release"
    rd.mkdir()

    fn = "zealfie_witness-0.0.1-py3-none-any.whl"
    w1 = _copy_wheel_as(witness_wheel, rd, fn)
    _write_manifest(rd, "zewitness", "0.0.1", fn, _sha256(w1), w1.stat().st_size)

    # Add some extra non-toml files
    (rd / "README.txt").write_text("hello")
    (rd / "notes.md").write_text("notes")

    registry = ComponentRegistry([WITNESS_DEF])
    service = ZeAlfieService(
        registry=registry,
        runtime=_FakeSharedRuntime(_absent_status()),
    )

    plan = service.plan_offline_deployment(rd)
    assert plan.runtime_state == RuntimeState.ABSENT
    assert len(plan.steps) == 1


# ===========================================================================
# M0-9.2 Tests — apply + rollback orchestration
# ===========================================================================


# ---------------------------------------------------------------------------
# M0-9.2.1 — apply re-plans fresh at call time (no persisted plan)
# ---------------------------------------------------------------------------

def test_apply_replans_fresh_at_call_time(
    tmp_path, witness_wheel, witness_v2_wheel, monkeypatch
) -> None:
    """apply_offline_deployment re-plans fresh; a stale plan is NOT reused.

    Strategy: create two release directories (v1→0.0.1, v2→0.0.2).
    Call plan_offline_deployment on v1 to prove a plan can be built.
    Then call apply_offline_deployment on v2.  Assert the plan passed
    to apply_deployment_plan carries the v2 desired version (0.0.2).
    """
    # -- v1 release dir (0.0.1) --
    rd_v1 = tmp_path / "release_v1"
    rd_v1.mkdir()
    fn1 = "zealfie_witness-0.0.1-py3-none-any.whl"
    w1 = _copy_wheel_as(witness_wheel, rd_v1, fn1)
    _write_manifest(rd_v1, "zewitness", "0.0.1", fn1, _sha256(w1), w1.stat().st_size)

    # -- v2 release dir (0.0.2) --
    rd_v2 = tmp_path / "release_v2"
    rd_v2.mkdir()
    fn2 = "zealfie_witness-0.0.2-py3-none-any.whl"
    w2 = _copy_wheel_as(witness_v2_wheel, rd_v2, fn2)
    _write_manifest(rd_v2, "zewitness", "0.0.2", fn2, _sha256(w2), w2.stat().st_size)

    registry = ComponentRegistry([WITNESS_DEF])

    # Build a service with a real SharedRuntime so apply can proceed.
    layout = RuntimeLayout(root=tmp_path / "rt")
    runtime = SharedRuntime(layout=layout)
    runtime.create()

    service = ZeAlfieService(registry=registry, runtime=runtime)

    # Step 1: plan v1 to prove planning works (this is NOT used by apply).
    plan_v1 = service.plan_offline_deployment(rd_v1)
    assert plan_v1.desired_state.components[0].version == "0.0.1"

    # Step 2: wrap apply_deployment_plan to capture the plan it receives.
    captured_plans: list[DeploymentPlan] = []
    original_apply = apply_deployment_plan

    def _capturing_apply(plan, *, registry, runtime):
        captured_plans.append(plan)
        return original_apply(plan, registry=registry, runtime=runtime)

    monkeypatch.setattr(
        "zealfie.app.service.apply_deployment_plan", _capturing_apply
    )

    # Step 3: apply v2. The version in the captured plan must be 0.0.2.
    result = service.apply_offline_deployment(rd_v2)
    assert result.success is True, f"apply failed: {result.reason}"

    assert len(captured_plans) == 1
    applied_plan = captured_plans[0]
    assert applied_plan.desired_state.components[0].version == "0.0.2", (
        f"apply used a stale plan with version "
        f"{applied_plan.desired_state.components[0].version!r} "
        f"instead of fresh 0.0.2"
    )

    # Verify the runtime actually has v0.0.2 installed.
    active_python = runtime.python()
    assert active_python is not None
    probe = probe_runtime_distribution(active_python, "zealfie-witness")
    assert probe["version"] == "0.0.2"


# ---------------------------------------------------------------------------
# M0-9.2.2 — apply delegates to existing engine and returns DeploymentResult
# ---------------------------------------------------------------------------


def test_apply_delegates_to_engine_and_returns_deployment_result(
    tmp_path, witness_wheel
) -> None:
    """apply_offline_deployment delegates to apply_deployment_plan.

    Uses a real SharedRuntime so the real engine is exercised.
    """
    rd = tmp_path / "release"
    rd.mkdir()
    fn = "zealfie_witness-0.0.1-py3-none-any.whl"
    w1 = _copy_wheel_as(witness_wheel, rd, fn)
    _write_manifest(rd, "zewitness", "0.0.1", fn, _sha256(w1), w1.stat().st_size)

    registry = ComponentRegistry([WITNESS_DEF])
    layout = RuntimeLayout(root=tmp_path / "rt")
    runtime = SharedRuntime(layout=layout)
    runtime.create()

    service = ZeAlfieService(registry=registry, runtime=runtime)
    result = service.apply_offline_deployment(rd)

    assert isinstance(result, DeploymentResult)
    assert result.success is True
    assert result.active_slot_id is not None
    assert result.previous_slot_id is not None

    # Verify witness was actually installed.
    active_python = runtime.python()
    assert active_python is not None
    probe = probe_runtime_distribution(active_python, "zealfie-witness")
    assert probe["installed"] is True
    assert probe["version"] == "0.0.1"


# ---------------------------------------------------------------------------
# M0-9.2.3 — actual witness apply success using temp runtime root
# ---------------------------------------------------------------------------


def test_apply_offline_deployment_witness_success(
    tmp_path, witness_wheel
) -> None:
    """apply_offline_deployment succeeds with a temp runtime and witness wheel."""
    rd = tmp_path / "release"
    rd.mkdir()
    fn = "zealfie_witness-0.0.1-py3-none-any.whl"
    w1 = _copy_wheel_as(witness_wheel, rd, fn)
    _write_manifest(rd, "zewitness", "0.0.1", fn, _sha256(w1), w1.stat().st_size)

    registry = ComponentRegistry([WITNESS_DEF])
    layout = RuntimeLayout(root=tmp_path / "rt")
    runtime = SharedRuntime(layout=layout)

    service = ZeAlfieService(registry=registry, runtime=runtime)

    # Initially ABSENT → apply creates and installs.
    result = service.apply_offline_deployment(rd)
    assert result.success is True, f"apply failed: {result.reason}"
    assert result.active_slot_id is not None

    # Runtime should be READY with witness installed.
    status = runtime.status()
    assert status.state == RuntimeState.READY
    assert status.active_slot_id == result.active_slot_id

    active_python = runtime.python()
    assert active_python is not None
    probe = probe_runtime_distribution(active_python, "zealfie-witness")
    assert probe["installed"] is True
    assert probe["version"] == "0.0.1"


# ---------------------------------------------------------------------------
# M0-9.2.4 — rollback delegation
# ---------------------------------------------------------------------------


def test_rollback_runtime_delegation() -> None:
    """rollback_runtime delegates to the injected SharedRuntime.rollback()."""
    fake_rt = _FakeSharedRuntime(_absent_status())
    service = ZeAlfieService(runtime=fake_rt)

    result = service.rollback_runtime()
    assert isinstance(result, RuntimeStatus)
    assert result.state == RuntimeState.READY
    assert result.active_slot_id == "rt-rollback0000"


# ---------------------------------------------------------------------------
# M0-9.2.5 — rollback after two applies (witness-based)
# ---------------------------------------------------------------------------


def test_rollback_after_two_applies(
    tmp_path, witness_wheel, witness_v2_wheel
) -> None:
    """Apply v1, then apply v2, then rollback → active is v1 again."""
    # -- v1 release dir (0.0.1) --
    rd_v1 = tmp_path / "release_v1"
    rd_v1.mkdir()
    fn1 = "zealfie_witness-0.0.1-py3-none-any.whl"
    w1 = _copy_wheel_as(witness_wheel, rd_v1, fn1)
    _write_manifest(rd_v1, "zewitness", "0.0.1", fn1, _sha256(w1), w1.stat().st_size)

    # -- v2 release dir (0.0.2) --
    rd_v2 = tmp_path / "release_v2"
    rd_v2.mkdir()
    fn2 = "zealfie_witness-0.0.2-py3-none-any.whl"
    w2 = _copy_wheel_as(witness_v2_wheel, rd_v2, fn2)
    _write_manifest(rd_v2, "zewitness", "0.0.2", fn2, _sha256(w2), w2.stat().st_size)

    registry = ComponentRegistry([WITNESS_DEF])
    layout = RuntimeLayout(root=tmp_path / "rt")
    runtime = SharedRuntime(layout=layout)

    service = ZeAlfieService(registry=registry, runtime=runtime)

    # Apply v1.
    r1 = service.apply_offline_deployment(rd_v1)
    assert r1.success is True
    slot_v1 = r1.active_slot_id

    # Verify v1 is active.
    active_python = runtime.python()
    probe = probe_runtime_distribution(active_python, "zealfie-witness")
    assert probe["version"] == "0.0.1"

    # Apply v2.
    r2 = service.apply_offline_deployment(rd_v2)
    assert r2.success is True
    slot_v2 = r2.active_slot_id
    assert slot_v2 != slot_v1

    # Verify v2 is active.
    active_python = runtime.python()
    probe = probe_runtime_distribution(active_python, "zealfie-witness")
    assert probe["version"] == "0.0.2"

    # Rollback.
    rollback_status = service.rollback_runtime()
    assert rollback_status.state == RuntimeState.READY
    assert rollback_status.active_slot_id == slot_v1
    assert rollback_status.previous_slot_id == slot_v2

    # Verify v1 is active again after rollback.
    active_python = runtime.python()
    probe = probe_runtime_distribution(active_python, "zealfie-witness")
    assert probe["version"] == "0.0.1"


# ---------------------------------------------------------------------------
# M0-9.2.6 — apply on blocked plan returns failure, no mutation
# ---------------------------------------------------------------------------


def test_apply_blocked_plan_returns_failure(
    tmp_path, witness_wheel
) -> None:
    """When the plan resolves as blocked (e.g., BROKEN runtime), apply fails.

    Uses a fake BROKEN runtime to trigger a blocked plan from the service.
    """
    rd = tmp_path / "release"
    rd.mkdir()
    fn = "zealfie_witness-0.0.1-py3-none-any.whl"
    w1 = _copy_wheel_as(witness_wheel, rd, fn)
    _write_manifest(rd, "zewitness", "0.0.1", fn, _sha256(w1), w1.stat().st_size)

    registry = ComponentRegistry([WITNESS_DEF])
    broken_status = RuntimeStatus(
        state=RuntimeState.BROKEN,
        runtime_root=Path("/fake/rt"),
        reason="simulated broken for test",
    )

    # Fake runtime with BROKEN status and no-op rollback.
    class _BrokenFakeRuntime:
        def status(self):
            return broken_status

        def rollback(self):
            return RuntimeStatus(
                state=RuntimeState.BROKEN,
                runtime_root=Path("/fake/rt"),
                reason="cannot rollback BROKEN runtime",
            )

    service = ZeAlfieService(
        registry=registry,
        runtime=_BrokenFakeRuntime(),
    )

    result = service.apply_offline_deployment(rd)
    assert isinstance(result, DeploymentResult)
    assert result.success is False
    assert result.reason is not None
    assert "blocked" in result.reason.lower()


# ===========================================================================
# M0-9 Closure C — User-controlled release-dir read failures
# ===========================================================================


def test_invalid_utf8_manifest_wraps_error(tmp_path, witness_wheel) -> None:
    """A manifest file with invalid UTF-8 raises OfflineReleaseError, not
    a raw UnicodeDecodeError, with a clear message."""
    rd = tmp_path / "release"
    rd.mkdir()

    fn = "zealfie_witness-0.0.1-py3-none-any.whl"
    w1 = _copy_wheel_as(witness_wheel, rd, fn)
    # Write a valid manifest alongside so the directory isn't empty.
    _write_manifest(rd, "zewitness", "0.0.1", fn, _sha256(w1), w1.stat().st_size)

    # Write a second manifest (for a multi-component registry) with invalid
    # bytes that are not valid UTF-8.
    bad_bytes = b'\xff\xfe\x00\x00invalid bytes here'
    (rd / "zewitness2.toml").write_bytes(bad_bytes)

    registry = ComponentRegistry([WITNESS_DEF, WITNESS2_DEF])
    service = ZeAlfieService(
        registry=registry,
        runtime=_FakeSharedRuntime(_absent_status()),
    )

    with pytest.raises(OfflineReleaseError, match="cannot read release manifest"):
        service.resolve_offline_release_set(rd)


def test_unreadable_manifest_file_wraps_error(
    tmp_path, witness_wheel, monkeypatch
) -> None:
    """A manifest file that cannot be read (e.g., permissions) raises
    OfflineReleaseError, not a raw OSError."""
    rd = tmp_path / "release"
    rd.mkdir()

    fn = "zealfie_witness-0.0.1-py3-none-any.whl"
    w1 = _copy_wheel_as(witness_wheel, rd, fn)
    _write_manifest(rd, "zewitness", "0.0.1", fn, _sha256(w1), w1.stat().st_size)

    # Create a manifest file whose read fails deterministically.
    bad_path = rd / "zewitness2.toml"
    bad_path.write_text("valid toml but unreadable in test")

    original_read_text = Path.read_text

    def failing_read_text(self, *args, **kwargs):
        if self == bad_path:
            raise OSError("simulated manifest read failure")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", failing_read_text)

    registry = ComponentRegistry([WITNESS_DEF, WITNESS2_DEF])
    service = ZeAlfieService(
        registry=registry,
        runtime=_FakeSharedRuntime(_absent_status()),
    )

    with pytest.raises(OfflineReleaseError, match="cannot read release manifest"):
        service.resolve_offline_release_set(rd)


def test_unicode_decode_error_not_leaked_from_parse(tmp_path, witness_wheel) -> None:
    """Direct call to parse_release_manifest_file with invalid UTF-8 raises
    ReleaseManifestError, not UnicodeDecodeError."""
    from zealfie.releases.manifest import (
        ReleaseManifestError,
        parse_release_manifest_file,
    )

    bad_path = tmp_path / "bad.toml"
    bad_path.write_bytes(b'\xff\xfe\x00\x00garbage')

    with pytest.raises(ReleaseManifestError, match="cannot read release manifest"):
        parse_release_manifest_file(bad_path)

# ===========================================================================
# M1-0A: prepare_launch_plan / launch_component
# ===========================================================================

from zealfie.app.service import (
    ComponentNotInstalledError,
    LaunchContractNotSatisfiedError,
    LaunchPreparationError,
    LaunchScriptNotFoundError,
)
from zealfie.components import UnknownComponentError
from zealfie.launching import LaunchPlan, LaunchResult


class _SyntheticSharedRuntime:
    """A SharedRuntime replacement that returns canned status + python."""

    def __init__(
        self,
        status: RuntimeStatus,
        *,
        probe_result: dict | None = None,
        scripts_dir: Path | None = None,
    ) -> None:
        self._status = status
        self._probe_result = probe_result or {}
        self._scripts_dir = scripts_dir

    def status(self) -> RuntimeStatus:
        return self._status

    def python(self) -> Path | None:
        return self._status.python_executable


def _ready_status(active_path: Path, python: Path | None = None) -> RuntimeStatus:
    if python is None:
        python = active_path / "bin" / "python"
    return RuntimeStatus(
        state=RuntimeState.READY,
        runtime_root=active_path.parent,
        active_slot_id="rt-test00000000",
        active_path=active_path,
        python_executable=python,
        python_version="3.13.0",
        reason_code=RuntimeReasonCode.RUNTIME_READY,
    )


def _absent_status_result() -> RuntimeStatus:
    return RuntimeStatus(
        state=RuntimeState.ABSENT,
        runtime_root=Path("/fake/runtime"),
    )


def _broken_status_result() -> RuntimeStatus:
    return RuntimeStatus(
        state=RuntimeState.BROKEN,
        runtime_root=Path("/fake/runtime"),
        reason="active slot missing",
    )


# -- Unknown component ---------------------------------------------------------


def test_prepare_launch_plan_unknown_component_raises():
    """Unknown component raises UnknownComponentError directly."""
    service = ZeAlfieService(
        registry=ComponentRegistry([WITNESS_DEF]),
        runtime=_FakeSharedRuntime(_ready_status(Path("/fake/rt"))),
    )
    with pytest.raises(UnknownComponentError):
        service.prepare_launch_plan("nonexistent")


# -- ABSENT / BROKEN runtime ---------------------------------------------------


def test_prepare_launch_plan_absent_runtime_raises():
    """ABSENT runtime raises LaunchPreparationError."""
    service = ZeAlfieService(
        registry=ComponentRegistry([WITNESS_DEF]),
        runtime=_FakeSharedRuntime(_absent_status_result()),
    )
    with pytest.raises(LaunchPreparationError, match="absent"):
        service.prepare_launch_plan("zewitness")


def test_prepare_launch_plan_broken_runtime_raises():
    """BROKEN runtime raises LaunchPreparationError."""
    service = ZeAlfieService(
        registry=ComponentRegistry([WITNESS_DEF]),
        runtime=_FakeSharedRuntime(_broken_status_result()),
    )
    with pytest.raises(LaunchPreparationError, match="broken"):
        service.prepare_launch_plan("zewitness")


# -- Missing distribution ------------------------------------------------------


def test_prepare_launch_plan_not_installed_raises(monkeypatch, tmp_path):
    """ComponentNotInstalledError when probe says not installed."""
    active = tmp_path / "rt" / "slots" / "test"
    python = active / "bin" / "python"

    from zealfie.app import service as svc_mod

    def fake_probe(runtime_python, dist_name):
        assert str(runtime_python) == str(python)
        return {"installed": False, "version": None, "entry_points": []}

    monkeypatch.setattr(svc_mod, "probe_runtime_distribution", fake_probe)

    # Create scripts dir but NOT the script file so we can verify
    # we don't reach script resolution.
    scripts = active / "bin"
    scripts.mkdir(parents=True)

    service = ZeAlfieService(
        registry=ComponentRegistry([WITNESS_DEF]),
        runtime=_FakeSharedRuntime(_ready_status(active, python=python)),
    )
    with pytest.raises(ComponentNotInstalledError, match="not installed"):
        service.prepare_launch_plan("zewitness")


# -- Missing contract ----------------------------------------------------------


def test_prepare_launch_plan_contract_not_satisfied_raises(monkeypatch):
    """LaunchContractNotSatisfiedError when no entry-point contract matches."""
    active = Path("/fake/rt/slots/test")
    python = active / "bin" / "python"

    from zealfie.app import service as svc_mod

    def fake_probe(runtime_python, dist_name):
        return {
            "installed": True,
            "version": "0.0.1",
            "entry_points": [
                {"group": "console_scripts", "name": "other_tool"},
            ],
        }

    monkeypatch.setattr(svc_mod, "probe_runtime_distribution", fake_probe)

    service = ZeAlfieService(
        registry=ComponentRegistry([WITNESS_DEF]),
        runtime=_FakeSharedRuntime(_ready_status(active, python=python)),
    )
    with pytest.raises(LaunchContractNotSatisfiedError):
        service.prepare_launch_plan("zewitness")


# -- Script not found ----------------------------------------------------------


def test_prepare_launch_plan_script_not_found_raises(monkeypatch, tmp_path):
    """LaunchScriptNotFoundError when the entry-point wrapper is missing."""
    active = tmp_path / "rt" / "slots" / "test"
    python = active / "bin" / "python"

    from zealfie.app import service as svc_mod

    def fake_probe(runtime_python, dist_name):
        return {
            "installed": True,
            "version": "0.0.1",
            "entry_points": [
                {"group": "console_scripts", "name": "zewitness", "value": "zewitness.__main__:main"},
            ],
        }

    monkeypatch.setattr(svc_mod, "probe_runtime_distribution", fake_probe)

    # Create scripts dir but NOT the script file.
    scripts = active / "bin"
    scripts.mkdir(parents=True)

    service = ZeAlfieService(
        registry=ComponentRegistry([WITNESS_DEF]),
        runtime=_FakeSharedRuntime(_ready_status(active, python=python)),
    )
    with pytest.raises(LaunchScriptNotFoundError):
        service.prepare_launch_plan("zewitness")


# -- Successful plan preparation -----------------------------------------------


def test_prepare_launch_plan_success(monkeypatch, tmp_path):
    """Successful plan preparation with all conditions met."""
    active = tmp_path / "rt" / "slots" / "test"
    python = active / "bin" / "python"
    scripts = active / "bin"
    scripts.mkdir(parents=True)
    # Create the script wrapper.
    script = scripts / "zewitness"
    script.write_text("#!/bin/sh\necho ok")
    script.chmod(0o755)

    from zealfie.app import service as svc_mod

    def fake_probe(runtime_python, dist_name):
        assert str(runtime_python) == str(python)
        return {
            "installed": True,
            "version": "0.0.1",
            "entry_points": [
                {"group": "console_scripts", "name": "zewitness", "value": "zewitness.__main__:main"},
            ],
        }

    monkeypatch.setattr(svc_mod, "probe_runtime_distribution", fake_probe)

    service = ZeAlfieService(
        registry=ComponentRegistry([WITNESS_DEF]),
        runtime=_FakeSharedRuntime(_ready_status(active, python=python)),
    )

    plan = service.prepare_launch_plan("zewitness")
    assert isinstance(plan, LaunchPlan)
    assert plan.component_id == "zewitness"
    assert plan.executable == script
    assert plan.arguments == ()
    # The script is inside the runtime, NOT the dev venv.
    assert str(active) in str(plan.executable)


# -- launch_component success --------------------------------------------------


def test_launch_component_success(monkeypatch, tmp_path):
    """launch_component prepares and executes a LaunchPlan successfully."""
    active = tmp_path / "rt" / "slots" / "test"
    python = active / "bin" / "python"
    scripts = active / "bin"
    scripts.mkdir(parents=True)
    script = scripts / "zewitness"
    script.write_text("#!/bin/sh\necho ok")
    script.chmod(0o755)

    from zealfie.app import service as svc_mod

    def fake_probe(runtime_python, dist_name):
        return {
            "installed": True,
            "version": "0.0.1",
            "entry_points": [
                {"group": "console_scripts", "name": "zewitness", "value": "zewitness.__main__:main"},
            ],
        }

    monkeypatch.setattr(svc_mod, "probe_runtime_distribution", fake_probe)

    service = ZeAlfieService(
        registry=ComponentRegistry([WITNESS_DEF]),
        runtime=_FakeSharedRuntime(_ready_status(active, python=python)),
    )

    result = service.launch_component("zewitness", timeout_seconds=5)
    assert isinstance(result, LaunchResult)
    assert result.return_code == 0
    assert "ok" in result.stdout
    assert result.timed_out is False


# -- Multi-entry-point contract: picks first match in registry order -----------


def test_prepare_launch_plan_picks_first_matching_contract(monkeypatch, tmp_path):
    """With multiple launch_entry_points in registry order, the first
    matching probe entry point is selected."""
    active = tmp_path / "rt" / "slots" / "test"
    python = active / "bin" / "python"
    scripts = active / "bin"
    scripts.mkdir(parents=True)
    first_script = scripts / "first_app"
    first_script.write_text("#!/bin/sh\necho first")
    first_script.chmod(0o755)
    second_script = scripts / "second_app"
    second_script.write_text("#!/bin/sh\necho second")
    second_script.chmod(0o755)

    from zealfie.app import service as svc_mod

    def fake_probe(runtime_python, dist_name):
        return {
            "installed": True,
            "version": "0.0.1",
            "entry_points": [
                {"group": "console_scripts", "name": "first_app"},
                {"group": "console_scripts", "name": "second_app"},
            ],
        }

    monkeypatch.setattr(svc_mod, "probe_runtime_distribution", fake_probe)

    multi_def = ComponentDefinition(
        "multi",
        "Multi",
        "multi-dist",
        (
            EntryPointContract("console_scripts", "first_app"),
            EntryPointContract("console_scripts", "second_app"),
        ),
    )

    service = ZeAlfieService(
        registry=ComponentRegistry([multi_def]),
        runtime=_FakeSharedRuntime(_ready_status(active, python=python)),
    )

    plan = service.prepare_launch_plan("multi")
    assert plan.executable == first_script



# ===========================================================================
# M1-0A integration: witness deploy + launch via service
# ===========================================================================


@pytest.mark.zealfie_slow
def test_witness_deploy_and_launch_via_service(tmp_path, witness_wheel):
    """Deploy witness to a temp shared runtime, then launch via
    ZeAlfieService.launch_component and verify output."""
    rt_root = tmp_path / "rt"
    layout = RuntimeLayout(root=rt_root)
    rt = SharedRuntime(layout=layout)

    # Create the runtime (ABSENT → READY).
    rt.create()

    # Install the witness wheel into the runtime.
    install_result = rt.install_local_wheel(
        witness_wheel, component_definition=WITNESS_DEF
    )
    assert install_result.outcome.value == 'INSTALLED', (
        f"install failed: {install_result.detail}"
    )

    # Verify the runtime is READY with the witness installed.
    rt_status = rt.status()
    assert rt_status.state == RuntimeState.READY
    assert rt_status.python_executable is not None

    # Launch via service.
    service = ZeAlfieService(
        registry=ComponentRegistry([WITNESS_DEF]),
        runtime=rt,
    )

    launch_result = service.launch_component("zewitness", timeout_seconds=10)
    assert launch_result.return_code == 0, (
        f"witness launch failed: rc={launch_result.return_code} "
        f"stderr={launch_result.stderr}"
    )
    assert launch_result.stdout.strip() == "ZeWitness is present."
    assert launch_result.stderr == ""
    assert launch_result.timed_out is False

    # Also verify prepare_launch_plan produces the correct plan.
    plan_obj = service.prepare_launch_plan("zewitness")
    assert plan_obj.component_id == "zewitness"
    # The executable is inside the runtime scripts dir, not the dev venv.
    active_path = rt_status.active_path
    assert active_path is not None
    assert str(active_path) in str(plan_obj.executable)
    assert plan_obj.executable.is_file()


@pytest.mark.zealfie_slow
def test_witness_deploy_and_launch_from_absent_fails_clean(tmp_path):
    """Launch from an ABSENT runtime fails with a clean error."""
    rt_root = tmp_path / "rt"
    layout = RuntimeLayout(root=rt_root)
    rt = SharedRuntime(layout=layout)

    # Runtime is ABSENT (never created).
    assert rt.status().state == RuntimeState.ABSENT

    service = ZeAlfieService(
        registry=ComponentRegistry([WITNESS_DEF]),
        runtime=rt,
    )

    with pytest.raises(LaunchPreparationError, match="absent"):
        service.launch_component("zewitness")


# ===========================================================================
# M1-0A C1 — malformed probe payloads fail closed
# ===========================================================================


def test_prepare_launch_plan_malformed_entry_point_element_raises(monkeypatch):
    """A probe payload with a non-dict entry point element raises
    LaunchPreparationError, not AttributeError."""
    active = Path("/fake/rt/slots/test")
    python = active / "bin" / "python"

    from zealfie.app import service as svc_mod

    def fake_probe(runtime_python, dist_name):
        return {
            "installed": True,
            "version": "0.0.1",
            "entry_points": ["not-a-dict"],
        }

    monkeypatch.setattr(svc_mod, "probe_runtime_distribution", fake_probe)

    service = ZeAlfieService(
        registry=ComponentRegistry([WITNESS_DEF]),
        runtime=_FakeSharedRuntime(_ready_status(active, python=python)),
    )

    with pytest.raises(LaunchPreparationError) as exc_info:
        service.prepare_launch_plan("zewitness")

    # Must be LaunchPreparationError, NOT AttributeError.
    assert not isinstance(exc_info.value, AttributeError)
    assert "entry_points[0] is not a dict" in str(exc_info.value)


def test_prepare_launch_plan_non_bool_installed_raises(monkeypatch):
    """A probe payload with a non-bool 'installed' field raises
    LaunchPreparationError."""
    active = Path("/fake/rt/slots/test")
    python = active / "bin" / "python"

    from zealfie.app import service as svc_mod

    def fake_probe(runtime_python, dist_name):
        return {
            "installed": "yes",  # string, not bool
            "version": "0.0.1",
            "entry_points": [],
        }

    monkeypatch.setattr(svc_mod, "probe_runtime_distribution", fake_probe)

    service = ZeAlfieService(
        registry=ComponentRegistry([WITNESS_DEF]),
        runtime=_FakeSharedRuntime(_ready_status(active, python=python)),
    )

    with pytest.raises(LaunchPreparationError, match="non-bool 'installed'"):
        service.prepare_launch_plan("zewitness")


def test_prepare_launch_plan_non_list_entry_points_raises(monkeypatch):
    """A probe payload with a non-list 'entry_points' field raises
    LaunchPreparationError."""
    active = Path("/fake/rt/slots/test")
    python = active / "bin" / "python"

    from zealfie.app import service as svc_mod

    def fake_probe(runtime_python, dist_name):
        return {
            "installed": True,
            "version": "0.0.1",
            "entry_points": "not-a-list",
        }

    monkeypatch.setattr(svc_mod, "probe_runtime_distribution", fake_probe)

    service = ZeAlfieService(
        registry=ComponentRegistry([WITNESS_DEF]),
        runtime=_FakeSharedRuntime(_ready_status(active, python=python)),
    )

    with pytest.raises(LaunchPreparationError, match="non-list 'entry_points'"):
        service.prepare_launch_plan("zewitness")


# -- M1-0A D: probe validation alignment (installed=False branch) --------------


def test_prepare_launch_plan_installed_false_missing_version_raises(monkeypatch):
    """installed=False with missing 'version' key raises LaunchPreparationError."""
    active = Path("/fake/rt/slots/test")
    python = active / "bin" / "python"

    from zealfie.app import service as svc_mod

    def fake_probe(runtime_python, dist_name):
        return {
            "installed": False,
            # version key missing
            "entry_points": [],
        }

    monkeypatch.setattr(svc_mod, "probe_runtime_distribution", fake_probe)

    service = ZeAlfieService(
        registry=ComponentRegistry([WITNESS_DEF]),
        runtime=_FakeSharedRuntime(_ready_status(active, python=python)),
    )

    with pytest.raises(LaunchPreparationError, match="missing 'version' key when installed=False"):
        service.prepare_launch_plan("zewitness")


def test_prepare_launch_plan_installed_false_version_not_none_raises(monkeypatch):
    """installed=False with non-None version raises LaunchPreparationError."""
    active = Path("/fake/rt/slots/test")
    python = active / "bin" / "python"

    from zealfie.app import service as svc_mod

    def fake_probe(runtime_python, dist_name):
        return {
            "installed": False,
            "version": "0.0.1",  # should be None
            "entry_points": [],
        }

    monkeypatch.setattr(svc_mod, "probe_runtime_distribution", fake_probe)

    service = ZeAlfieService(
        registry=ComponentRegistry([WITNESS_DEF]),
        runtime=_FakeSharedRuntime(_ready_status(active, python=python)),
    )

    with pytest.raises(LaunchPreparationError, match="version must be None when installed=False"):
        service.prepare_launch_plan("zewitness")


def test_prepare_launch_plan_installed_false_missing_entry_points_raises(monkeypatch):
    """installed=False with missing 'entry_points' key raises LaunchPreparationError."""
    active = Path("/fake/rt/slots/test")
    python = active / "bin" / "python"

    from zealfie.app import service as svc_mod

    def fake_probe(runtime_python, dist_name):
        return {
            "installed": False,
            "version": None,
            # entry_points key missing
        }

    monkeypatch.setattr(svc_mod, "probe_runtime_distribution", fake_probe)

    service = ZeAlfieService(
        registry=ComponentRegistry([WITNESS_DEF]),
        runtime=_FakeSharedRuntime(_ready_status(active, python=python)),
    )

    with pytest.raises(LaunchPreparationError, match="missing 'entry_points' key when installed=False"):
        service.prepare_launch_plan("zewitness")


def test_prepare_launch_plan_installed_false_entry_points_not_empty_raises(monkeypatch):
    """installed=False with non-empty entry_points raises LaunchPreparationError."""
    active = Path("/fake/rt/slots/test")
    python = active / "bin" / "python"

    from zealfie.app import service as svc_mod

    def fake_probe(runtime_python, dist_name):
        return {
            "installed": False,
            "version": None,
            "entry_points": [{"group": "console_scripts", "name": "foo"}],
        }

    monkeypatch.setattr(svc_mod, "probe_runtime_distribution", fake_probe)

    service = ZeAlfieService(
        registry=ComponentRegistry([WITNESS_DEF]),
        runtime=_FakeSharedRuntime(_ready_status(active, python=python)),
    )

    with pytest.raises(LaunchPreparationError, match="entry_points must be empty when installed=False"):
        service.prepare_launch_plan("zewitness")


def test_prepare_launch_plan_installed_false_entry_points_not_list_raises(monkeypatch):
    """installed=False with non-list entry_points raises LaunchPreparationError."""
    active = Path("/fake/rt/slots/test")
    python = active / "bin" / "python"

    from zealfie.app import service as svc_mod

    def fake_probe(runtime_python, dist_name):
        return {
            "installed": False,
            "version": None,
            "entry_points": "not-a-list",
        }

    monkeypatch.setattr(svc_mod, "probe_runtime_distribution", fake_probe)

    service = ZeAlfieService(
        registry=ComponentRegistry([WITNESS_DEF]),
        runtime=_FakeSharedRuntime(_ready_status(active, python=python)),
    )

    with pytest.raises(LaunchPreparationError, match="entry_points must be a list when installed=False"):
        service.prepare_launch_plan("zewitness")


def test_prepare_launch_plan_installed_true_empty_version_raises(monkeypatch):
    """installed=True with empty version string raises LaunchPreparationError."""
    active = Path("/fake/rt/slots/test")
    python = active / "bin" / "python"

    from zealfie.app import service as svc_mod

    def fake_probe(runtime_python, dist_name):
        return {
            "installed": True,
            "version": "",  # empty string
            "entry_points": [
                {"group": "console_scripts", "name": "zewitness"},
            ],
        }

    monkeypatch.setattr(svc_mod, "probe_runtime_distribution", fake_probe)

    service = ZeAlfieService(
        registry=ComponentRegistry([WITNESS_DEF]),
        runtime=_FakeSharedRuntime(_ready_status(active, python=python)),
    )

    with pytest.raises(LaunchPreparationError, match="version must be non-empty str when installed=True"):
        service.prepare_launch_plan("zewitness")


def test_prepare_launch_plan_installed_true_valid_probe_passes(monkeypatch, tmp_path):
    """installed=True with a well-formed probe succeeds validation."""
    active = tmp_path / "rt" / "slots" / "test"
    python = active / "bin" / "python"
    scripts = active / "bin"
    scripts.mkdir(parents=True)
    script = scripts / "zewitness"
    script.write_text("#!/bin/sh\necho ok")
    script.chmod(0o755)

    from zealfie.app import service as svc_mod

    def fake_probe(runtime_python, dist_name):
        return {
            "installed": True,
            "version": "0.0.1",
            "entry_points": [
                {"group": "console_scripts", "name": "zewitness"},
            ],
        }

    monkeypatch.setattr(svc_mod, "probe_runtime_distribution", fake_probe)

    service = ZeAlfieService(
        registry=ComponentRegistry([WITNESS_DEF]),
        runtime=_FakeSharedRuntime(_ready_status(active, python=python)),
    )

    plan = service.prepare_launch_plan("zewitness")
    assert plan.component_id == "zewitness"


# ===========================================================================
# M1-1C: Shared runtime dependency resolution in offline release flow
# ===========================================================================


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_test_wheel(
    output: Path,
    name: str,
    version: str,
    *,
    requires_dist: list[str] | None = None,
    provides_extra: list[str] | None = None,
) -> Path:
    """Build a minimal test wheel with optional Requires-Dist and Provides-Extra."""
    safe_name = name.replace("-", "_").replace(".", "_")
    wheel_name = f"{safe_name}-{version}-py3-none-any.whl"
    wheel_path = output / wheel_name
    dist_info = f"{safe_name}-{version}.dist-info"

    wheelfile = (
        "Wheel-Version: 1.0\n"
        "Generator: test\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    )
    metadata = f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n"
    if requires_dist:
        for req in requires_dist:
            metadata += f"Requires-Dist: {req}\n"
    if provides_extra:
        for extra in provides_extra:
            metadata += f"Provides-Extra: {extra}\n"
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


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _write_manifest_entry(
    release_dir: Path,
    component_id: str,
    version: str,
    filename: str,
    sha256_val: str,
    size_val: int,
    *,
    python_tag: str = "py3",
    abi_tag: str = "none",
    platform_tag: str = "any",
) -> None:
    """Write a single-artifact component release manifest."""
    toml_text = textwrap.dedent(f"""\
        schema_version = 1
        component_id = "{component_id}"
        version = "{version}"

        [[artifacts]]
        filename = "{filename}"
        size = {size_val}
        sha256 = "{sha256_val}"
        python_tag = "{python_tag}"
        abi_tag = "{abi_tag}"
        platform_tag = "{platform_tag}"
    """)
    (release_dir / f"{component_id}.toml").write_text(toml_text)


def _make_component_def(
    component_id: str,
    distribution_name: str,
    *,
    launch_entry_points: tuple[EntryPointContract, ...] = (),
    required_extras: tuple[str, ...] = (),
) -> ComponentDefinition:
    """Create a ComponentDefinition with optional entry points and extras."""
    return ComponentDefinition(
        component_id=component_id,
        display_name=component_id.title(),
        distribution_name=distribution_name,
        launch_entry_points=launch_entry_points,
        required_extras=required_extras,
    )


# ---------------------------------------------------------------------------
# M1-1C Test 1: Plan with dependency lock
# ---------------------------------------------------------------------------

@pytest.mark.zealfie_slow
def test_plan_with_dependency_lock_resolves_primary_and_dependency(
    tmp_path,
) -> None:
    """Planning a release dir with a component wheel declaring Requires-Dist
    and a local dependency wheel produces a DeploymentPlan with a non-None
    dependency_lock containing both primary and dependency entries."""
    rd = tmp_path / "release"
    rd.mkdir()

    # Build a dependency wheel
    dep_wheel = _build_test_wheel(rd, "py-lib", "2.0.0")

    # Build a component wheel that depends on py-lib
    comp_wheel = _build_test_wheel(
        rd, "test-comp", "1.0.0", requires_dist=["py-lib>=2.0"],
    )

    # Write manifest for the component
    sha = _sha256_of(comp_wheel)
    size = comp_wheel.stat().st_size
    _write_manifest_entry(
        rd, "testcomp", "1.0.0", comp_wheel.name, sha, size,
    )

    comp_def = _make_component_def("testcomp", "test-comp")
    registry = ComponentRegistry([comp_def])
    service = ZeAlfieService(
        registry=registry,
        runtime=_FakeSharedRuntime(_absent_status()),
    )

    plan = service.plan_offline_deployment(rd)

    # The plan must carry a dependency_lock.
    assert plan.dependency_lock is not None, (
        "Expected a non-None dependency_lock"
    )
    lock = plan.dependency_lock

    # The lock must contain both the primary component and its dependency.
    assert len(lock) == 2, f"Expected 2 locked entries, got {len(lock)}"

    # Primary names: entries with no required_by
    primaries = lock.primary_names
    assert "test-comp" in primaries, f"Expected test-comp in primaries, got {primaries}"

    # Dependency names
    deps = lock.dependency_names
    assert "py-lib" in deps, f"Expected py-lib in dependencies, got {deps}"

    # The primary entry has the right version and wheel path.
    primary_dep = lock["test-comp"]
    assert primary_dep.version == "1.0.0"
    assert primary_dep.wheel_path.resolve() == comp_wheel.resolve()

    # The dependency entry has the right version and references the primary.
    dep_dep = lock["py-lib"]
    assert dep_dep.version == "2.0.0"
    assert dep_dep.wheel_path.resolve() == dep_wheel.resolve()
    assert "test-comp" in dep_dep.required_by


# ---------------------------------------------------------------------------
# M1-1C Test 2: Apply with dependency lock installs both
# ---------------------------------------------------------------------------

@pytest.mark.zealfie_slow
def test_apply_with_dependency_lock_installs_both(tmp_path) -> None:
    """Applying a release dir with a component + dependency wheel installs
    both the component and the dependency into the active shared runtime."""
    rd = tmp_path / "release"
    rd.mkdir()

    # Build wheels
    dep_wheel = _build_test_wheel(rd, "py-lib", "2.0.0")
    comp_wheel = _build_test_wheel(
        rd, "test-comp", "1.0.0", requires_dist=["py-lib>=2.0"],
    )
    sha = _sha256_of(comp_wheel)
    size = comp_wheel.stat().st_size
    _write_manifest_entry(
        rd, "testcomp", "1.0.0", comp_wheel.name, sha, size,
    )

    comp_def = _make_component_def("testcomp", "test-comp")
    registry = ComponentRegistry([comp_def])

    layout = RuntimeLayout(root=tmp_path / "rt")
    runtime = SharedRuntime(layout=layout)
    runtime.create()

    service = ZeAlfieService(registry=registry, runtime=runtime)
    result = service.apply_offline_deployment(rd)

    assert result.success is True, f"Apply failed: {result.reason}"
    assert result.active_slot_id is not None

    # Verify both component and dependency are installed.
    active_python = runtime.python()
    assert active_python is not None

    probe_comp = probe_runtime_distribution(active_python, "test-comp")
    assert probe_comp["installed"] is True
    assert probe_comp["version"] == "1.0.0"

    probe_dep = probe_runtime_distribution(active_python, "py-lib")
    assert probe_dep["installed"] is True
    assert probe_dep["version"] == "2.0.0"


# ---------------------------------------------------------------------------
# M1-1C Test 3: Missing dependency wheel → OfflineReleaseError
# ---------------------------------------------------------------------------

def test_missing_dependency_wheel_raises_offline_release_error(
    tmp_path,
) -> None:
    """When a component wheel Requires-Dist a package not present in the
    release directory, plan_offline_deployment raises OfflineReleaseError
    before any runtime mutation."""
    rd = tmp_path / "release"
    rd.mkdir()

    # Component wheel depends on py-lib, but only the component wheel is present
    comp_wheel = _build_test_wheel(
        rd, "test-comp", "1.0.0", requires_dist=["py-lib>=2.0"],
    )
    sha = _sha256_of(comp_wheel)
    size = comp_wheel.stat().st_size
    _write_manifest_entry(
        rd, "testcomp", "1.0.0", comp_wheel.name, sha, size,
    )

    comp_def = _make_component_def("testcomp", "test-comp")
    registry = ComponentRegistry([comp_def])

    # Use a real runtime to verify active state is unchanged
    layout = RuntimeLayout(root=tmp_path / "rt")
    runtime = SharedRuntime(layout=layout)
    runtime.create()
    active_before = runtime.status().active_slot_id

    service = ZeAlfieService(registry=registry, runtime=runtime)

    # Plan should fail
    with pytest.raises(
        OfflineReleaseError,
        match="shared runtime dependency resolution failed",
    ):
        service.plan_offline_deployment(rd)

    # Apply should also fail (it re-plans internally)
    with pytest.raises(
        OfflineReleaseError,
        match="shared runtime dependency resolution failed",
    ):
        service.apply_offline_deployment(rd)

    # Active slot must be unchanged — no mutation occurred.
    assert runtime.status().active_slot_id == active_before


# ---------------------------------------------------------------------------
# M1-1C Test 4: Required extras flow to resolver
# ---------------------------------------------------------------------------

def test_required_extras_passed_to_resolver(monkeypatch) -> None:
    """Required extras from ComponentDefinition.required_extras are passed
    to resolve_runtime_dependencies as part of the primary_wheels extras."""
    from zealfie.app import service as svc_mod
    from zealfie.dependencies import RuntimeLock

    # Build a synthetic empty lock to return from the monkeypatched resolver.
    fake_lock = RuntimeLock(locked={})

    captured_primary_wheels: list[tuple[Path, frozenset[str]]] = []

    def fake_resolve(primary_wheels, wheelhouse, **kwargs):
        nonlocal captured_primary_wheels
        captured_primary_wheels = list(primary_wheels)
        return fake_lock

    monkeypatch.setattr(svc_mod, "resolve_runtime_dependencies", fake_resolve)

    # Create component with required_extras.
    comp_def = ComponentDefinition(
        component_id="testcomp",
        display_name="TestComp",
        distribution_name="test-comp",
        launch_entry_points=(),
        required_extras=("feature", "gpu"),
    )

    # Monkeypatch resolve_offline_release_set below so this focused test
    # avoids real release-dir filesystem setup.
    rd = Path("/fake/rd")
    service = ZeAlfieService(
        registry=ComponentRegistry([comp_def]),
        runtime=_FakeSharedRuntime(_absent_status()),
    )

    # Monkeypatch resolve_offline_release_set to return a synthetic state
    # with the extras flowing from the definition.  This avoids needing a real
    # release directory with manifests/wheels for this focused test.
    from zealfie.releases.model import VerifiedArtifact
    from zealfie.runtime.planning import DesiredComponent, DesiredRuntimeState

    fake_artifact = VerifiedArtifact(
        component_id="testcomp",
        version="1.0.0",
        path=Path("/fake/rd/test_comp-1.0.0-py3-none-any.whl"),
        size=1234,
        sha256="a" * 64,
        distribution_name="test-comp",
        wheel_version="1.0.0",
    )
    fake_state = DesiredRuntimeState(
        components=(DesiredComponent("testcomp", "1.0.0", fake_artifact),)
    )

    monkeypatch.setattr(
        svc_mod.ZeAlfieService,
        "resolve_offline_release_set",
        lambda self, rd: fake_state,
    )

    plan = service.plan_offline_deployment(rd)
    assert plan.dependency_lock is fake_lock

    # Verify the primary_wheels extras match required_extras
    assert len(captured_primary_wheels) == 1
    _, extras = captured_primary_wheels[0]
    assert extras == frozenset({"feature", "gpu"}), (
        f"Expected extras {{'feature','gpu'}}, got {extras}"
    )


# ---------------------------------------------------------------------------
# M1-1C Test 5: Component with no dependencies (backward compatibility)
# ---------------------------------------------------------------------------

@pytest.mark.zealfie_slow
def test_component_with_no_dependencies_plans_and_applies_successfully(
    tmp_path, witness_wheel,
) -> None:
    """A component with no external dependencies still plans/applies
    successfully, and the plan has a RuntimeLock with only primary entries."""
    rd = tmp_path / "release"
    rd.mkdir()

    fn = "zealfie_witness-0.0.1-py3-none-any.whl"
    w1 = _copy_wheel_as(witness_wheel, rd, fn)
    _write_manifest(rd, "zewitness", "0.0.1", fn, _sha256(w1), w1.stat().st_size)

    registry = ComponentRegistry([WITNESS_DEF])
    layout = RuntimeLayout(root=tmp_path / "rt")
    runtime = SharedRuntime(layout=layout)
    runtime.create()

    service = ZeAlfieService(registry=registry, runtime=runtime)

    # Plan: must produce a non-None dependency_lock.
    plan = service.plan_offline_deployment(rd)
    assert plan.dependency_lock is not None, (
        "Expected a non-None dependency_lock even with no dependencies"
    )
    lock = plan.dependency_lock
    assert len(lock) >= 1
    # No dependency entries — only primary entries.
    assert lock.dependency_names == frozenset(), (
        f"Expected no dependency entries, got {lock.dependency_names}"
    )
    # The witness component must be a primary.
    assert "zealfie-witness" in lock.primary_names

    # Apply: must succeed.
    result = service.apply_offline_deployment(rd)
    assert result.success is True, f"Apply failed: {result.reason}"

    active_python = runtime.python()
    assert active_python is not None
    probe = probe_runtime_distribution(active_python, "zealfie-witness")
    assert probe["installed"] is True
    assert probe["version"] == "0.0.1"


# ---------------------------------------------------------------------------
# M1-1C Test 6: Plan with dependency_lock is present for ABSENT runtime
# ---------------------------------------------------------------------------

@pytest.mark.zealfie_slow
def test_plan_with_lock_for_absent_runtime_carries_lock(tmp_path) -> None:
    """When the runtime is ABSENT, the plan still carries a dependency_lock
    with resolved dependencies ready for materialization."""
    rd = tmp_path / "release"
    rd.mkdir()

    dep_wheel = _build_test_wheel(rd, "py-lib", "2.0.0")
    comp_wheel = _build_test_wheel(
        rd, "test-comp", "1.0.0", requires_dist=["py-lib"],
    )
    sha = _sha256_of(comp_wheel)
    size = comp_wheel.stat().st_size
    _write_manifest_entry(
        rd, "testcomp", "1.0.0", comp_wheel.name, sha, size,
    )

    comp_def = _make_component_def("testcomp", "test-comp")
    registry = ComponentRegistry([comp_def])
    service = ZeAlfieService(
        registry=registry,
        runtime=_FakeSharedRuntime(_absent_status()),
    )

    plan = service.plan_offline_deployment(rd)
    assert plan.runtime_state == RuntimeState.ABSENT
    assert plan.dependency_lock is not None
    lock = plan.dependency_lock
    assert "test-comp" in lock.primary_names
    assert "py-lib" in lock.dependency_names


# ---------------------------------------------------------------------------
# M1-1C Test 7: Dependency resolution error includes original resolver detail
# ---------------------------------------------------------------------------

def test_dependency_resolution_error_preserves_original_detail(
    tmp_path,
) -> None:
    """When dependency resolution fails, the OfflineReleaseError message
    includes the original error detail."""
    rd = tmp_path / "release"
    rd.mkdir()

    comp_wheel = _build_test_wheel(
        rd, "test-comp", "1.0.0", requires_dist=["unobtainium>=99"],
    )
    sha = _sha256_of(comp_wheel)
    size = comp_wheel.stat().st_size
    _write_manifest_entry(
        rd, "testcomp", "1.0.0", comp_wheel.name, sha, size,
    )

    comp_def = _make_component_def("testcomp", "test-comp")
    registry = ComponentRegistry([comp_def])
    service = ZeAlfieService(
        registry=registry,
        runtime=_FakeSharedRuntime(_absent_status()),
    )

    with pytest.raises(OfflineReleaseError) as exc_info:
        service.plan_offline_deployment(rd)

    msg = str(exc_info.value)
    assert "shared runtime dependency resolution failed" in msg
    assert "unobtainium" in msg
