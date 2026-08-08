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
from pathlib import Path

import pytest

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


@pytest.fixture(scope="session")
def witness_wheel(tmp_path_factory) -> Path:
    """Build the zealfie-witness wheel once per session."""
    d = Path(__file__).resolve().parent / "fixtures" / "witness_component"
    t = tmp_path_factory.mktemp("svc-wheel")
    from zealfie.building import build_wheel
    return build_wheel(d, output_dir=t)


@pytest.fixture(scope="session")
def witness2_wheel(tmp_path_factory) -> Path:
    """Build the zealfie-witness2 wheel once per session."""
    d = Path(__file__).resolve().parent / "fixtures" / "witness_second"
    t = tmp_path_factory.mktemp("svc-wheel2")
    from zealfie.building import build_wheel
    return build_wheel(d, output_dir=t)


@pytest.fixture(scope="session")
def witness_v2_wheel(tmp_path_factory) -> Path:
    """Build the zealfie-witness v0.0.2 wheel once per session."""
    d = Path(__file__).resolve().parent / "fixtures" / "witness_component_v2"
    t = tmp_path_factory.mktemp("svc-wheel-v2")
    from zealfie.building import build_wheel
    return build_wheel(d, output_dir=t)


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
