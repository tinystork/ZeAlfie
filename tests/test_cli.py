from __future__ import annotations

import hashlib
import shutil
import textwrap
from io import StringIO
from pathlib import Path

import pytest


import zealfie.cli as cli
from zealfie import get_version
from zealfie.components.model import ComponentDefinition, ComponentStatus, EntryPointContract, ReasonCode
from zealfie.components.registry import ComponentRegistry
from zealfie.cli import _format_deployment_plan, _format_deployment_result, main, run
from zealfie.releases.model import VerifiedArtifact
from zealfie.runtime.layout import RuntimeLayout
from zealfie.runtime.manager import SharedRuntime
from zealfie.runtime.model import (
    DeploymentResult,
    RuntimeReasonCode,
    RuntimeState,
    RuntimeStatus,
)
from zealfie.runtime.planning import (
    DeploymentAction,
    DeploymentPlan,
    DeploymentReasonCode,
    DeploymentStep,
    DesiredComponent,
    DesiredRuntimeState,
)
from zealfie.runtime.probe import probe_runtime_distribution


# ===========================================================================
# Fake registry for component status tests
# ===========================================================================


class FakeRegistry:
    def __init__(self, status: ComponentStatus) -> None:
        self.status = status

    def inspect_all(self) -> tuple[ComponentStatus, ...]:
        return (self.status,)

    def inspect(self, component_id: str) -> ComponentStatus:
        if component_id != self.status.component_id:
            from zealfie.components import UnknownComponentError

            raise UnknownComponentError(component_id)
        return self.status

    def available_ids(self) -> tuple[str, ...]:
        return (self.status.component_id,)


ABSENT_ZESOLVER = ComponentStatus(
    component_id="zesolver",
    display_name="ZeSolver",
    installed=False,
    version=None,
    launch_contract_available=False,
    matched_entry_point=None,
    reason_code=ReasonCode.DISTRIBUTION_NOT_INSTALLED,
    reason='distribution "ZeSolver" is not installed',
)

PRESENT_ZESOLVER = ComponentStatus(
    component_id="zesolver",
    display_name="ZeSolver",
    installed=True,
    version="1.0.0",
    launch_contract_available=False,
    matched_entry_point=None,
    reason_code=ReasonCode.PUBLIC_ENTRY_POINT_NOT_FOUND,
    reason='expected public entry point "gui_scripts:zesolver" was not found',
)

AVAILABLE_WITNESS = ComponentStatus(
    component_id="witness",
    display_name="ZeWitness",
    installed=True,
    version="0.0.1",
    launch_contract_available=True,
    matched_entry_point=None,
    reason_code=None,
    reason=None,
)


# ===========================================================================
# Existing CLI tests (unchanged)
# ===========================================================================


def test_main_returns_success() -> None:
    assert main([]) == 0


def test_version_option_outputs_package_version() -> None:
    stdout = StringIO()
    code = run(["--version"], stdout=stdout)
    assert code == 0
    assert stdout.getvalue().strip() == f"ZeAlfie {get_version()}"


def test_status_command_outputs_absent_component(monkeypatch) -> None:
    monkeypatch.setattr(cli, "default_registry", lambda: FakeRegistry(ABSENT_ZESOLVER))
    stdout = StringIO()
    code = run(["status"], stdout=stdout)
    output = stdout.getvalue()
    assert code == 0
    assert f"ZeAlfie {get_version()}" in output
    assert "Platform:" in output
    assert "Python:" in output
    assert "Components:" in output
    assert "ZeSolver" in output
    assert "Installed: no" in output
    assert "Version: unavailable" in output
    assert "Launch contract: unavailable" in output
    assert 'Reason: distribution "ZeSolver" is not installed' in output


def test_status_command_outputs_present_component(monkeypatch) -> None:
    monkeypatch.setattr(cli, "default_registry", lambda: FakeRegistry(PRESENT_ZESOLVER))
    stdout = StringIO()
    code = run(["status"], stdout=stdout)
    output = stdout.getvalue()
    assert code == 0
    assert "ZeSolver" in output
    assert "Installed: yes" in output
    assert "Version: 1.0.0" in output
    assert "Launch contract: unavailable" in output
    assert 'Reason: expected public entry point "gui_scripts:zesolver" was not found' in output


def test_status_command_outputs_available_contract(monkeypatch) -> None:
    monkeypatch.setattr(cli, "default_registry", lambda: FakeRegistry(AVAILABLE_WITNESS))
    stdout = StringIO()
    code = run(["status"], stdout=stdout)
    output = stdout.getvalue()
    assert code == 0
    assert "ZeWitness" in output
    assert "Installed: yes" in output
    assert "Version: 0.0.1" in output
    assert "Launch contract: available" in output
    assert "Reason: none" in output


def test_unknown_command_returns_error_code() -> None:
    assert main(["unknown-command"]) == 2


def test_cli_does_not_import_or_require_zesolver(monkeypatch) -> None:
    monkeypatch.setattr(cli, "default_registry", lambda: FakeRegistry(ABSENT_ZESOLVER))
    stdout = StringIO()
    code = run(["status"], stdout=stdout)
    assert code == 0
    assert "ZeSolver" in stdout.getvalue()


def test_status_specific_unknown_component_returns_error_without_traceback(monkeypatch) -> None:
    monkeypatch.setattr(cli, "default_registry", lambda: FakeRegistry(ABSENT_ZESOLVER))
    stdout = StringIO()
    code = run(["status", "missing"], stdout=stdout)
    output = stdout.getvalue()
    assert code == 2
    assert "Unknown component: missing" in output
    assert "zesolver" in output
    assert "Traceback" not in output


def test_status_specific_zesolver_displays_reason(monkeypatch) -> None:
    monkeypatch.setattr(cli, "default_registry", lambda: FakeRegistry(PRESENT_ZESOLVER))
    stdout = StringIO()
    code = run(["status", "zesolver"], stdout=stdout)
    output = stdout.getvalue()
    assert code == 0
    assert "ZeSolver" in output
    assert "Installed: yes" in output
    assert "Version: 1.0.0" in output
    assert "Launch contract: unavailable" in output
    assert 'Reason: expected public entry point "gui_scripts:zesolver" was not found' in output
    assert ReasonCode.PUBLIC_ENTRY_POINT_NOT_FOUND.value not in output


# ===========================================================================
# CLI unit tests — plan format output
# ===========================================================================

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

_FAKE_ARTIFACT = VerifiedArtifact(
    component_id="zewitness",
    version="0.0.1",
    path=Path("/fake/witness.whl"),
    size=100,
    sha256="a" * 64,
    distribution_name="zealfie-witness",
    wheel_version="0.0.1",
)

_FAKE_ARTIFACT2 = VerifiedArtifact(
    component_id="zewitness2",
    version="0.1.0",
    path=Path("/fake/witness2.whl"),
    size=200,
    sha256="b" * 64,
    distribution_name="zealfie-witness2",
    wheel_version="0.1.0",
)


def _fake_absent_plan() -> DeploymentPlan:
    desired = DesiredRuntimeState(
        components=(
            DesiredComponent("zewitness", "0.0.1", _FAKE_ARTIFACT),
        )
    )
    steps = (
        DeploymentStep(
            component_id="zewitness",
            desired_version="0.0.1",
            artifact=_FAKE_ARTIFACT,
            action=DeploymentAction.INSTALL,
            reason_code=DeploymentReasonCode.RUNTIME_ABSENT,
            reason="shared runtime is absent — install planned",
        ),
    )
    return DeploymentPlan(
        desired_state=desired,
        runtime_state=RuntimeState.ABSENT,
        steps=steps,
        blocked=False,
    )


def _fake_keep_plan() -> DeploymentPlan:
    desired = DesiredRuntimeState(
        components=(
            DesiredComponent("zewitness", "0.0.1", _FAKE_ARTIFACT),
        )
    )
    steps = (
        DeploymentStep(
            component_id="zewitness",
            desired_version="0.0.1",
            artifact=_FAKE_ARTIFACT,
            action=DeploymentAction.KEEP,
            current_version="0.0.1",
            reason_code=DeploymentReasonCode.ALREADY_SATISFIED,
            reason="installed version and launch contract are correct",
        ),
    )
    return DeploymentPlan(
        desired_state=desired,
        runtime_state=RuntimeState.READY,
        steps=steps,
        blocked=False,
        source_active_slot_id="rt-deadbeef0000",
        source_previous_slot_id="rt-cafebabe0000",
    )


def _fake_blocked_plan() -> DeploymentPlan:
    desired = DesiredRuntimeState(
        components=(
            DesiredComponent("zewitness", "0.0.1", _FAKE_ARTIFACT),
        )
    )
    steps = (
        DeploymentStep(
            component_id="zewitness",
            desired_version="0.0.1",
            artifact=_FAKE_ARTIFACT,
            action=DeploymentAction.BLOCKED,
            reason_code=DeploymentReasonCode.RUNTIME_BROKEN,
            reason="shared runtime is BROKEN",
        ),
    )
    return DeploymentPlan(
        desired_state=desired,
        runtime_state=RuntimeState.BROKEN,
        steps=steps,
        blocked=True,
        blocked_reason="shared runtime is BROKEN",
    )


def test_format_absent_plan():
    """Format output for an ABSENT runtime plan."""
    plan = _fake_absent_plan()
    output = _format_deployment_plan(plan)
    assert "Runtime state: ABSENT" in output
    assert "zewitness:" in output
    assert "Action: INSTALL" in output
    assert "Desired version: 0.0.1" in output
    assert "Reason code: RUNTIME_ABSENT" in output
    assert "shared runtime is absent — install planned" in output


def test_format_keep_plan():
    """Format output for a READY runtime KEEP plan."""
    plan = _fake_keep_plan()
    output = _format_deployment_plan(plan)
    assert "Runtime state: READY" in output
    assert "Source active slot: rt-deadbeef0000" in output
    assert "Source previous slot: rt-cafebabe0000" in output
    assert "Action: KEEP" in output
    assert "Current version: 0.0.1" in output
    assert "ALREADY_SATISFIED" in output


def test_format_blocked_plan():
    """Format output for a blocked plan."""
    plan = _fake_blocked_plan()
    output = _format_deployment_plan(plan)
    assert "Runtime state: BROKEN" in output
    assert "Blocked: shared runtime is BROKEN" in output
    assert "Action: BLOCKED" in output
    assert "RUNTIME_BROKEN" in output


def test_format_deployment_result_success():
    """Format output for a successful deployment."""
    result = DeploymentResult(
        success=True,
        active_slot_id="rt-newslot0000",
        previous_slot_id="rt-oldslot0000",
    )
    output = _format_deployment_result(result)
    assert "Success: yes" in output
    assert "Active slot: rt-newslot0000" in output
    assert "Previous slot: rt-oldslot0000" in output


def test_format_deployment_result_failure():
    """Format output for a failed deployment."""
    result = DeploymentResult(
        success=False,
        reason="deployment plan is blocked",
    )
    output = _format_deployment_result(result)
    assert "Success: no" in output
    assert "Reason: deployment plan is blocked" in output


# ===========================================================================
# CLI unit tests — runtime plan with fake service
# ===========================================================================


class _FakePlanService:
    """Fake ZeAlfieService for CLI plan tests."""

    def __init__(self, plan: DeploymentPlan) -> None:
        self._plan = plan

    def plan_offline_deployment(self, release_dir: Path) -> DeploymentPlan:
        return self._plan


def test_runtime_plan_absent_success(monkeypatch):
    """CLI runtime plan returns 0 for a successful non-blocked plan."""
    monkeypatch.setattr(cli, "_make_service", lambda: _FakePlanService(_fake_absent_plan()))
    stdout = StringIO()
    code = run(["runtime", "plan", "--release-dir", "/fake/rd"], stdout=stdout)
    assert code == 0
    output = stdout.getvalue()
    assert "Deployment plan:" in output
    assert "Runtime state: ABSENT" in output
    assert "Action: INSTALL" in output


def test_runtime_plan_blocked_returns_1(monkeypatch):
    """CLI runtime plan returns 1 for blocked plans."""
    monkeypatch.setattr(cli, "_make_service", lambda: _FakePlanService(_fake_blocked_plan()))
    stdout = StringIO()
    code = run(["runtime", "plan", "--release-dir", "/fake/rd"], stdout=stdout)
    assert code == 1
    output = stdout.getvalue()
    assert "Blocked:" in output
    assert "Action: BLOCKED" in output


def test_runtime_plan_offline_error_stderr(monkeypatch):
    """OfflineReleaseError returns code 4 and does not print a plan."""
    from zealfie.app import OfflineReleaseError

    class _ErrorService:
        def plan_offline_deployment(self, release_dir):
            raise OfflineReleaseError("missing manifest 'missing.toml'")

    monkeypatch.setattr(cli, "_make_service", lambda: _ErrorService())
    stdout = StringIO()
    code = run(
        ["runtime", "plan", "--release-dir", "/fake/rd"],
        stdout=stdout,
    )
    assert code == 4
    assert "Deployment plan:" not in stdout.getvalue()


def test_runtime_plan_offline_error_to_stderr(monkeypatch):
    """OfflineReleaseError goes to stderr with message and no traceback."""
    from zealfie.app import OfflineReleaseError
    import sys

    class _ErrorService:
        def plan_offline_deployment(self, release_dir):
            raise OfflineReleaseError("bad release dir")

    monkeypatch.setattr(cli, "_make_service", lambda: _ErrorService())

    backup = sys.stderr
    try:
        sys.stderr = stderr = StringIO()
        stdout = StringIO()
        code = run(
            ["runtime", "plan", "--release-dir", "/fake/rd"],
            stdout=stdout,
        )
        assert code == 4
        assert "plan failed: bad release dir" in stderr.getvalue()
        assert "Traceback" not in stderr.getvalue()
    finally:
        sys.stderr = backup


@pytest.mark.zealfie_slow
def test_runtime_plan_invalid_utf8_manifest_to_stderr_no_traceback(
    tmp_path, monkeypatch, witness_wheel_cli
):
    """A real invalid UTF-8 release manifest is reported cleanly by the CLI."""
    from zealfie.app import ZeAlfieService
    import sys

    rd = tmp_path / "release"
    rd.mkdir()
    (rd / "zewitness.toml").write_bytes(b"\xff\xfe\x00\x00not utf-8")

    # Artifact contents are irrelevant because manifest parsing fails first,
    # but the release directory still looks like a local release bundle.
    shutil.copy2(
        witness_wheel_cli,
        rd / "zealfie_witness-0.0.1-py3-none-any.whl",
    )

    runtime = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    registry = _registry_for_ids("zewitness")
    monkeypatch.setattr(
        cli,
        "_make_service",
        lambda: ZeAlfieService(registry=registry, runtime=runtime),
    )

    backup = sys.stderr
    try:
        sys.stderr = stderr = StringIO()
        stdout = StringIO()
        code = run(["runtime", "plan", "--release-dir", str(rd)], stdout=stdout)
        assert code == 4
        assert stdout.getvalue() == ""
        err = stderr.getvalue()
        assert "plan failed:" in err
        assert "cannot read release manifest" in err
        assert "Traceback" not in err
    finally:
        sys.stderr = backup


def test_runtime_plan_keep_returns_0(monkeypatch):
    """CLI runtime plan returns 0 for a READY KEEP plan."""
    monkeypatch.setattr(cli, "_make_service", lambda: _FakePlanService(_fake_keep_plan()))
    stdout = StringIO()
    code = run(["runtime", "plan", "--release-dir", "/fake/rd"], stdout=stdout)
    assert code == 0
    output = stdout.getvalue()
    assert "Action: KEEP" in output
    assert "ALREADY_SATISFIED" in output


# ===========================================================================
# CLI unit tests — runtime apply with fake service
# ===========================================================================


class _FakeApplyService:
    """Fake ZeAlfieService for CLI apply tests."""

    def __init__(self, result: DeploymentResult) -> None:
        self._result = result
        self.apply_called_with: list[Path] = []

    def apply_offline_deployment(self, release_dir: Path) -> DeploymentResult:
        self.apply_called_with.append(release_dir)
        return self._result


def test_runtime_apply_success(monkeypatch):
    """CLI runtime apply returns 0 on success."""
    result = DeploymentResult(
        success=True,
        active_slot_id="rt-slot12340000",
        previous_slot_id="rt-slotabcd0000",
    )
    service = _FakeApplyService(result)
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    stdout = StringIO()
    code = run(["runtime", "apply", "--release-dir", "/fake/rd"], stdout=stdout)
    assert code == 0
    output = stdout.getvalue()
    assert "Success: yes" in output
    assert "Active slot: rt-slot12340000" in output
    assert "Previous slot: rt-slotabcd0000" in output


def test_runtime_apply_failure_returns_3(monkeypatch):
    """CLI runtime apply returns 3 on DeploymentResult(success=False)."""
    result = DeploymentResult(success=False, reason="stale plan")
    service = _FakeApplyService(result)
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    stdout = StringIO()
    code = run(["runtime", "apply", "--release-dir", "/fake/rd"], stdout=stdout)
    assert code == 3
    output = stdout.getvalue()
    assert "Success: no" in output
    assert "Reason: stale plan" in output


def test_runtime_apply_offline_error_to_stderr(monkeypatch):
    """OfflineReleaseError from apply → stderr, exit 4, no traceback."""
    from zealfie.app import OfflineReleaseError
    import sys

    class _ErrorService:
        def apply_offline_deployment(self, release_dir):
            raise OfflineReleaseError("bad sha256")

    monkeypatch.setattr(cli, "_make_service", lambda: _ErrorService())

    backup = sys.stderr
    try:
        sys.stderr = stderr = StringIO()
        stdout = StringIO()
        code = run(
            ["runtime", "apply", "--release-dir", "/fake/rd"],
            stdout=stdout,
        )
        assert code == 4
        assert "apply failed: bad sha256" in stderr.getvalue()
        assert "Traceback" not in stderr.getvalue()
    finally:
        sys.stderr = backup


def test_runtime_apply_calls_service_with_release_dir(monkeypatch):
    """CLI runtime apply passes release_dir to service.apply_offline_deployment."""
    result = DeploymentResult(success=True, active_slot_id="rt-0000")
    service = _FakeApplyService(result)
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    stdout = StringIO()
    code = run(["runtime", "apply", "--release-dir", "/my/release"], stdout=stdout)
    assert code == 0
    assert len(service.apply_called_with) == 1
    assert service.apply_called_with[0] == Path("/my/release")


# ===========================================================================
# CLI unit tests — runtime rollback with fake service
# ===========================================================================


class _FakeRollbackService:
    """Fake ZeAlfieService for CLI rollback tests."""

    def __init__(self, status: RuntimeStatus) -> None:
        self._status = status

    def rollback_runtime(self) -> RuntimeStatus:
        return self._status


def test_runtime_rollback_success(monkeypatch):
    """CLI runtime rollback returns 0 when resulting state is READY."""
    status = RuntimeStatus(
        state=RuntimeState.READY,
        runtime_root=Path("/fake/rt"),
        active_slot_id="rt-rolled0000",
        previous_slot_id="rt-prevslot0000",
        reason_code=RuntimeReasonCode.RUNTIME_READY,
    )
    monkeypatch.setattr(cli, "_make_service", lambda: _FakeRollbackService(status))
    stdout = StringIO()
    code = run(["runtime", "rollback"], stdout=stdout)
    assert code == 0
    output = stdout.getvalue()
    assert "State: READY" in output
    assert "Active slot: rt-rolled0000" in output
    assert "Previous slot: rt-prevslot0000" in output


def test_runtime_rollback_broken_returns_3(monkeypatch):
    """CLI runtime rollback returns 3 when resulting state is BROKEN."""
    status = RuntimeStatus(
        state=RuntimeState.BROKEN,
        runtime_root=Path("/fake/rt"),
        reason_code=RuntimeReasonCode.ROLLBACK_TARGET_NOT_FOUND,
        reason="no previous slot to rollback to",
    )
    monkeypatch.setattr(cli, "_make_service", lambda: _FakeRollbackService(status))
    stdout = StringIO()
    code = run(["runtime", "rollback"], stdout=stdout)
    assert code == 3
    output = stdout.getvalue()
    assert "State: BROKEN" in output
    assert "ROLLBACK_TARGET_NOT_FOUND" in output


# ===========================================================================
# CLI does not consume/persist a previous plan (apply is fresh)
# ===========================================================================


def test_cli_has_no_plan_persistence_path(monkeypatch):
    """CLI has no mechanism to consume/persist a previous plan.

    plan and apply are independent code paths that each create a fresh
    service and call the corresponding method.  The CLI never stores a
    plan object between commands and has no persistence path for it.
    """
    # Verify _make_service creates a fresh service each call.
    # Since _make_service is isolated, tests can verify no plan reuse.
    call_count = 0

    class _CountingService:
        def plan_offline_deployment(self, release_dir):
            nonlocal call_count
            call_count += 1
            return _fake_absent_plan()

    monkeypatch.setattr(cli, "_make_service", lambda: _CountingService())

    # Call plan twice — each call creates fresh service.
    run(["runtime", "plan", "--release-dir", "/a"], stdout=StringIO())
    assert call_count == 1

    # Plan again — new service, count increments.
    run(["runtime", "plan", "--release-dir", "/b"], stdout=StringIO())
    assert call_count == 2

    # Plan has no way to store/retrieve a plan. No CLI path for it.
    # The _make_service factory is the only entry point.


# ===========================================================================
# CLI plan is read-only — test that it doesn't mutate runtime
# ===========================================================================


def test_plan_does_not_create_runtime(tmp_path, monkeypatch):
    """CLI runtime plan does not create a runtime or mutate filesystem.

    When we inject a fake service that returns a plan, no runtime
    directories are created.
    """
    monkeypatch.setattr(cli, "_make_service", lambda: _FakePlanService(_fake_absent_plan()))
    stdout = StringIO()
    code = run(
        ["runtime", "plan", "--release-dir", str(tmp_path / "noexist")],
        stdout=stdout,
    )
    assert code == 0
    assert "INSTALL" in stdout.getvalue()
    assert not (tmp_path / "noexist").exists()


# ===========================================================================
# Witness end-to-end via CLI layer
# ===========================================================================



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


def _make_release_dir(
    root: Path,
    witness_wheel: Path,
    witness2_wheel: Path | None = None,
    *,
    version: str = "0.0.1",
) -> Path:
    """Prepare a release directory with witness(es)."""
    rd = root / "release"
    rd.mkdir(parents=True)

    fn1 = f"zealfie_witness-{version}-py3-none-any.whl"
    w1 = _copy_wheel_as(witness_wheel, rd, fn1)
    _write_manifest(rd, "zewitness", version, fn1, _sha256(w1), w1.stat().st_size)

    if witness2_wheel is not None:
        fn2 = "zealfie_witness2-0.1.0-py3-none-any.whl"
        w2 = _copy_wheel_as(witness2_wheel, rd, fn2)
        _write_manifest(rd, "zewitness2", "0.1.0", fn2, _sha256(w2), w2.stat().st_size)

    return rd


def _registry_for_ids(*component_ids: str) -> ComponentRegistry:
    """Build a ComponentRegistry containing only the requested witness defs."""
    defs = []
    for cid in component_ids:
        if cid == "zewitness":
            defs.append(WITNESS_DEF)
        elif cid == "zewitness2":
            defs.append(WITNESS2_DEF)
        else:
            raise ValueError(f"unknown component id: {cid}")
    return ComponentRegistry(defs)


@pytest.mark.zealfie_slow
def test_witness_e2e_plan_apply_rollback_via_cli(
    tmp_path, witness_wheel_cli, witness_v2_wheel_cli, monkeypatch
):
    """End-to-end witness cycle through CLI: plan, apply v1, apply v2, rollback.

    Uses monkeypatched _make_service to inject a controlled registry and
    a temp runtime layout — no production runtime is touched.
    """
    rt_root = tmp_path / "rt"
    layout = RuntimeLayout(root=rt_root)
    runtime = SharedRuntime(layout=layout)

    # Registry with just zewitness.
    registry = _registry_for_ids("zewitness")

    # -- Build release dirs --
    rd_v1 = _make_release_dir(tmp_path / "v1", witness_wheel_cli, version="0.0.1")
    rd_v2 = _make_release_dir(tmp_path / "v2", witness_v2_wheel_cli, version="0.0.2")

    # -- Patch _make_service to use controlled registry and runtime --
    from zealfie.app import ZeAlfieService

    def _controlled_service():
        return ZeAlfieService(registry=registry, runtime=runtime)

    monkeypatch.setattr(cli, "_make_service", _controlled_service)

    # -- Step 1: plan v1 (read-only, ABSENT runtime) --
    stdout = StringIO()
    code = run(["runtime", "plan", "--release-dir", str(rd_v1)], stdout=stdout)
    assert code == 0, f"plan v1 failed with code {code}"
    output = stdout.getvalue()
    assert "Action: INSTALL" in output
    assert "RUNTIME_ABSENT" in output

    # Verify runtime is still ABSENT (plan is read-only).
    rt_status = runtime.status()
    assert rt_status.state == RuntimeState.ABSENT, (
        f"plan should not mutate runtime, but state is {rt_status.state}"
    )

    # -- Step 2: apply v1 --
    stdout = StringIO()
    code = run(["runtime", "apply", "--release-dir", str(rd_v1)], stdout=stdout)
    assert code == 0, f"apply v1 failed with code {code}: {stdout.getvalue()}"
    output = stdout.getvalue()
    assert "Success: yes" in output
    assert "Active slot:" in output
    # First apply on ABSENT runtime has no previous slot.

    # Verify runtime is READY with v1.
    rt_status = runtime.status()
    assert rt_status.state == RuntimeState.READY
    active_python = runtime.python()
    assert active_python is not None
    probe = probe_runtime_distribution(active_python, "zealfie-witness")
    assert probe["version"] == "0.0.1"

    # Record slot for rollback reference.
    slot_v1 = rt_status.active_slot_id

    # -- Step 3: plan v2 (should see version mismatch → INSTALL) --
    stdout = StringIO()
    code = run(["runtime", "plan", "--release-dir", str(rd_v2)], stdout=stdout)
    assert code == 0, f"plan v2 failed with code {code}"
    output = stdout.getvalue()
    assert "Action: INSTALL" in output
    assert "VERSION_MISMATCH" in output
    assert "0.0.1" in output  # current version
    assert "0.0.2" in output  # desired version

    # -- Step 4: apply v2 --
    stdout = StringIO()
    code = run(["runtime", "apply", "--release-dir", str(rd_v2)], stdout=stdout)
    assert code == 0, f"apply v2 failed with code {code}: {stdout.getvalue()}"
    output = stdout.getvalue()
    assert "Success: yes" in output

    # Verify runtime has v2.
    active_python = runtime.python()
    probe = probe_runtime_distribution(active_python, "zealfie-witness")
    assert probe["version"] == "0.0.2"

    slot_v2 = runtime.status().active_slot_id
    assert slot_v2 != slot_v1

    # -- Step 5: rollback --
    stdout = StringIO()
    code = run(["runtime", "rollback"], stdout=stdout)
    assert code == 0, f"rollback failed with code {code}: {stdout.getvalue()}"
    output = stdout.getvalue()
    assert "State: READY" in output

    # Verify runtime is back to v1.
    rt_status = runtime.status()
    assert rt_status.active_slot_id == slot_v1
    assert rt_status.previous_slot_id == slot_v2

    active_python = runtime.python()
    probe = probe_runtime_distribution(active_python, "zealfie-witness")
    assert probe["version"] == "0.0.1"


@pytest.mark.zealfie_slow
def test_witness_e2e_plan_then_apply_fresh_not_persisted(
    tmp_path, witness_wheel_cli, monkeypatch
):
    """apply re-plans fresh internally; a previous plan call is not consumed.

    Plan v1, then apply v1.  The plan output from the first call is
    not passed to apply — apply re-plans fresh internally.
    """
    rt_root = tmp_path / "rt"
    layout = RuntimeLayout(root=rt_root)
    runtime = SharedRuntime(layout=layout)
    registry = _registry_for_ids("zewitness")

    rd = _make_release_dir(tmp_path / "r", witness_wheel_cli, version="0.0.1")

    from zealfie.app import ZeAlfieService

    def _controlled_service():
        return ZeAlfieService(registry=registry, runtime=runtime)

    monkeypatch.setattr(cli, "_make_service", _controlled_service)

    # Plan
    stdout = StringIO()
    code = run(["runtime", "plan", "--release-dir", str(rd)], stdout=stdout)
    assert code == 0
    plan_output = stdout.getvalue()

    # Plan output contains plan information but no result ID to reuse.
    assert "INSTALL" in plan_output

    # Apply (fresh — no plan passed)
    stdout = StringIO()
    code = run(["runtime", "apply", "--release-dir", str(rd)], stdout=stdout)
    assert code == 0
    assert "Success: yes" in stdout.getvalue()

    # Verify witness installed
    active_python = runtime.python()
    probe = probe_runtime_distribution(active_python, "zealfie-witness")
    assert probe["version"] == "0.0.1"


@pytest.mark.zealfie_slow
def test_witness_e2e_multi_component_plan(
    tmp_path, witness_wheel_cli, witness2_wheel_cli, monkeypatch
):
    """CLI plan for multi-component release shows both components."""
    rt_root = tmp_path / "rt"
    layout = RuntimeLayout(root=rt_root)
    runtime = SharedRuntime(layout=layout)
    registry = _registry_for_ids("zewitness", "zewitness2")

    rd = _make_release_dir(
        tmp_path / "r",
        witness_wheel_cli,
        witness2_wheel=witness2_wheel_cli,
    )

    from zealfie.app import ZeAlfieService

    monkeypatch.setattr(
        cli, "_make_service",
        lambda: ZeAlfieService(registry=registry, runtime=runtime),
    )

    stdout = StringIO()
    code = run(["runtime", "plan", "--release-dir", str(rd)], stdout=stdout)
    assert code == 0
    output = stdout.getvalue()
    assert "zewitness:" in output
    assert "zewitness2:" in output
    assert output.count("Action: INSTALL") == 2


# ===========================================================================
# Existing tests still pass — runtime status/create unchanged
# ===========================================================================


def test_runtime_status_absent(tmp_path, monkeypatch):
    """CLI runtime status on an ABSENT runtime."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    monkeypatch.setattr(cli, "default_runtime_layout", lambda: layout)
    stdout = StringIO()
    code = run(["runtime", "status"], stdout=stdout)
    assert code == 0
    output = stdout.getvalue()
    assert "State: ABSENT" in output
    assert str(layout.root) in output


@pytest.mark.zealfie_slow
def test_runtime_status_ready(tmp_path, monkeypatch):
    """CLI runtime status on a READY runtime."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    monkeypatch.setattr(cli, "default_runtime_layout", lambda: layout)
    stdout = StringIO()
    code = run(["runtime", "status"], stdout=stdout)
    assert code == 0
    output = stdout.getvalue()
    assert "State: READY" in output


@pytest.mark.zealfie_slow
def test_runtime_create_idempotent(tmp_path, monkeypatch):
    """CLI runtime create is idempotent."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    rh = SharedRuntime(layout=layout)
    rh.create()
    monkeypatch.setattr(cli, "default_runtime_layout", lambda: layout)
    stdout = StringIO()
    code = run(["runtime", "create"], stdout=stdout)
    assert code == 0
    assert "State: READY" in stdout.getvalue()


# ===========================================================================
# M0-9 Closure B — CLI rollback exit-code hardening
# ===========================================================================


def test_runtime_rollback_absent_returns_nonzero(monkeypatch):
    """ABSENT runtime rollback returns non-zero.
    After fix A, rollback on ABSENT returns state=ABSENT with
    reason_code=ROLLBACK_TARGET_NOT_FOUND, and the CLI returns 3."""
    status = RuntimeStatus(
        state=RuntimeState.ABSENT,
        runtime_root=Path("/fake/rt"),
        reason_code=RuntimeReasonCode.ROLLBACK_TARGET_NOT_FOUND,
        reason="shared runtime is absent — nothing to roll back",
    )
    monkeypatch.setattr(cli, "_make_service", lambda: _FakeRollbackService(status))
    stdout = StringIO()
    code = run(["runtime", "rollback"], stdout=stdout)
    assert code == 3, f"expected exit 3 for ABSENT rollback, got {code}"
    output = stdout.getvalue()
    assert "State: ABSENT" in output
    assert "ROLLBACK_TARGET_NOT_FOUND" in output


def test_runtime_rollback_ready_no_previous_returns_nonzero(monkeypatch):
    """READY runtime with no previous slot returns non-zero.

    This happens when the very first deployment is active and hasn't
    been upgraded — there's no previous slot to roll back to."""
    status = RuntimeStatus(
        state=RuntimeState.READY,
        runtime_root=Path("/fake/rt"),
        active_slot_id="rt-someslot0000",
        reason_code=RuntimeReasonCode.ROLLBACK_TARGET_NOT_FOUND,
        reason="no previous slot to roll back to",
    )
    monkeypatch.setattr(cli, "_make_service", lambda: _FakeRollbackService(status))
    stdout = StringIO()
    code = run(["runtime", "rollback"], stdout=stdout)
    assert code == 3, (
        f"expected exit 3 for READY+ROLLBACK_TARGET_NOT_FOUND, got {code}"
    )
    output = stdout.getvalue()
    assert "State: READY" in output
    assert "ROLLBACK_TARGET_NOT_FOUND" in output


def test_runtime_rollback_success_returns_zero(monkeypatch):
    """Successful rollback (READY + RUNTIME_READY) returns exit 0."""
    status = RuntimeStatus(
        state=RuntimeState.READY,
        runtime_root=Path("/fake/rt"),
        active_slot_id="rt-rolled0000",
        previous_slot_id="rt-prevslot0000",
        reason_code=RuntimeReasonCode.RUNTIME_READY,
    )
    monkeypatch.setattr(cli, "_make_service", lambda: _FakeRollbackService(status))
    stdout = StringIO()
    code = run(["runtime", "rollback"], stdout=stdout)
    assert code == 0, f"expected exit 0 for successful rollback, got {code}"
    output = stdout.getvalue()
    assert "State: READY" in output
    assert "RUNTIME_READY" in output


def test_runtime_rollback_broken_returns_nonzero(monkeypatch):
    """BROKEN runtime rollback returns non-zero."""
    status = RuntimeStatus(
        state=RuntimeState.BROKEN,
        runtime_root=Path("/fake/rt"),
        reason_code=RuntimeReasonCode.ROLLBACK_TARGET_NOT_FOUND,
        reason="cannot rollback: global state is BROKEN",
    )
    monkeypatch.setattr(cli, "_make_service", lambda: _FakeRollbackService(status))
    stdout = StringIO()
    code = run(["runtime", "rollback"], stdout=stdout)
    assert code == 3, f"expected exit 3 for BROKEN rollback, got {code}"
    output = stdout.getvalue()
    assert "State: BROKEN" in output

# ===========================================================================
# M1-0A: CLI launch command
# ===========================================================================

from zealfie.app.service import (
    ComponentNotInstalledError,
    LaunchContractNotSatisfiedError,
    LaunchPreparationError,
    LaunchScriptNotFoundError,
)
from zealfie.components import UnknownComponentError
from zealfie.launching import LaunchError, LaunchResult


class _FakeLaunchService:
    """Fake ZeAlfieService for CLI launch tests."""

    def __init__(self, result_or_error):
        self._result_or_error = result_or_error
        self.launch_called_with: list[tuple] = []

    def launch_component(self, component_id, *, timeout_seconds=None):
        self.launch_called_with.append((component_id, timeout_seconds))
        if isinstance(self._result_or_error, Exception):
            raise self._result_or_error
        return self._result_or_error


# -- Success -------------------------------------------------------------------


def test_launch_success_returns_component_rc(monkeypatch):
    """CLI launch prints output and returns component's return code."""
    result = LaunchResult(return_code=0, stdout="hello world\n", stderr="")
    service = _FakeLaunchService(result)
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    stdout = StringIO()
    code = run(["launch", "zewitness"], stdout=stdout)
    assert code == 0
    assert "hello world" in stdout.getvalue()
    assert service.launch_called_with == [("zewitness", None)]


def test_launch_success_nonzero_rc(monkeypatch):
    """CLI launch returns component's non-zero return code."""
    result = LaunchResult(return_code=42, stdout="", stderr="error detail")
    service = _FakeLaunchService(result)
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    stdout = StringIO()
    code = run(["launch", "zewitness"], stdout=stdout)
    assert code == 42


def test_launch_passes_timeout(monkeypatch):
    """CLI launch passes --timeout to service.launch_component."""
    result = LaunchResult(return_code=0, stdout="ok", stderr="")
    service = _FakeLaunchService(result)
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    stdout = StringIO()
    code = run(["launch", "zewitness", "--timeout", "10"], stdout=stdout)
    assert code == 0
    assert service.launch_called_with == [("zewitness", 10.0)]


# -- Unknown component ---------------------------------------------------------


def test_launch_unknown_component_clean_error(monkeypatch):
    """Unknown component → exit code 5, clean stderr, no traceback."""
    service = _FakeLaunchService(UnknownComponentError("zewitness"))
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    monkeypatch.setattr(
        cli, "default_registry",
        lambda: ComponentRegistry(
            [ComponentDefinition("zesolver", "ZeSolver", "ZeSolver",
                                 (EntryPointContract("gui_scripts", "zesolver"),))]
        )
    )
    import sys
    backup = sys.stderr
    try:
        sys.stderr = stderr = StringIO()
        stdout = StringIO()
        code = run(["launch", "nonexistent"], stdout=stdout)
        assert code == 5
        assert "Unknown component: nonexistent" in stderr.getvalue()
        assert "zesolver" in stderr.getvalue()
        assert "Traceback" not in stderr.getvalue()
    finally:
        sys.stderr = backup


# -- LaunchPreparationError (runtime absent/broken) ----------------------------


def test_launch_preparation_error_clean(monkeypatch):
    """LaunchPreparationError → exit code 6, clean stderr, no traceback."""
    service = _FakeLaunchService(
        LaunchPreparationError("shared runtime is absent")
    )
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    import sys
    backup = sys.stderr
    try:
        sys.stderr = stderr = StringIO()
        stdout = StringIO()
        code = run(["launch", "zewitness"], stdout=stdout)
        assert code == 6
        assert "cannot launch 'zewitness': shared runtime is absent" in stderr.getvalue()
        assert "Traceback" not in stderr.getvalue()
    finally:
        sys.stderr = backup


def test_launch_component_not_installed_clean(monkeypatch):
    """ComponentNotInstalledError → exit code 6, clean stderr, no traceback."""
    service = _FakeLaunchService(
        ComponentNotInstalledError("not installed")
    )
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    import sys
    backup = sys.stderr
    try:
        sys.stderr = stderr = StringIO()
        stdout = StringIO()
        code = run(["launch", "zewitness"], stdout=stdout)
        assert code == 6
        assert "cannot launch 'zewitness': not installed" in stderr.getvalue()
        assert "Traceback" not in stderr.getvalue()
    finally:
        sys.stderr = backup


def test_launch_contract_not_satisfied_clean(monkeypatch):
    """LaunchContractNotSatisfiedError → exit code 6, clean stderr."""
    service = _FakeLaunchService(
        LaunchContractNotSatisfiedError("no matching entry points")
    )
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    import sys
    backup = sys.stderr
    try:
        sys.stderr = stderr = StringIO()
        stdout = StringIO()
        code = run(["launch", "zewitness"], stdout=stdout)
        assert code == 6
        assert "cannot launch 'zewitness': no matching entry points" in stderr.getvalue()
        assert "Traceback" not in stderr.getvalue()
    finally:
        sys.stderr = backup


def test_launch_script_not_found_clean(monkeypatch):
    """LaunchScriptNotFoundError → exit code 6, clean stderr."""
    service = _FakeLaunchService(
        LaunchScriptNotFoundError("script not found")
    )
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    import sys
    backup = sys.stderr
    try:
        sys.stderr = stderr = StringIO()
        stdout = StringIO()
        code = run(["launch", "zewitness"], stdout=stdout)
        assert code == 6
        assert "cannot launch 'zewitness': script not found" in stderr.getvalue()
        assert "Traceback" not in stderr.getvalue()
    finally:
        sys.stderr = backup


# -- Timeout -------------------------------------------------------------------


def test_launch_timed_out_returns_10(monkeypatch):
    """CLI launch returns exit code 10 on timeout."""
    result = LaunchResult(
        return_code=-1, stdout="partial", stderr="", timed_out=True
    )
    service = _FakeLaunchService(result)
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    import sys
    backup = sys.stderr
    try:
        sys.stderr = stderr = StringIO()
        stdout = StringIO()
        code = run(["launch", "zewitness"], stdout=stdout)
        assert code == 10
        assert "launch timed out" in stderr.getvalue()
        assert "partial" in stdout.getvalue()
    finally:
        sys.stderr = backup


# -- Parser tests --------------------------------------------------------------


def test_launch_in_parser():
    """launch subcommand is present in the argument parser."""
    p = cli.build_parser()
    # Parse standalone launch with component.
    args = p.parse_args(["launch", "zewitness"])
    assert args.command == "launch"
    assert args.component_id == "zewitness"
    assert args.timeout_seconds is None


def test_launch_parser_timeout():
    """launch subcommand accepts --timeout."""
    p = cli.build_parser()
    args = p.parse_args(["launch", "zewitness", "--timeout", "60"])
    assert args.timeout_seconds == 60.0


# -- LaunchError (execution boundary failure) ----------------------------------


def test_launch_execution_error_clean(monkeypatch):
    """LaunchError from execute_launch_plan → exit code 6, clean stderr,
    no traceback."""
    service = _FakeLaunchService(
        LaunchError("could not execute /bin/nonexistent: Permission denied")
    )
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    import sys
    backup = sys.stderr
    try:
        sys.stderr = stderr = StringIO()
        stdout = StringIO()
        code = run(["launch", "zewitness"], stdout=stdout)
        assert code == 6
        assert "cannot launch 'zewitness': could not execute" in stderr.getvalue()
        assert "Permission denied" in stderr.getvalue()
        assert "Traceback" not in stderr.getvalue()
    finally:
        sys.stderr = backup


# -- Timeout validation (adversarial) ------------------------------------------


def test_launch_parser_timeout_nan_rejected():
    """--timeout nan → argparse SystemExit, clean error."""
    p = cli.build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["launch", "zewitness", "--timeout", "nan"])


def test_launch_parser_timeout_inf_rejected():
    """--timeout inf → argparse SystemExit, clean error."""
    p = cli.build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["launch", "zewitness", "--timeout", "inf"])


def test_launch_parser_timeout_negative_rejected():
    """--timeout negative → argparse SystemExit, clean error."""
    p = cli.build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["launch", "zewitness", "--timeout", "-1"])


def test_launch_parser_timeout_zero_rejected():
    """--timeout 0 → argparse SystemExit, clean error."""
    p = cli.build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["launch", "zewitness", "--timeout", "0"])


def test_launch_parser_timeout_positive_accepted():
    """--timeout with a positive float is accepted."""
    p = cli.build_parser()
    args = p.parse_args(["launch", "zewitness", "--timeout", "0.5"])
    assert args.timeout_seconds == 0.5

# ===========================================================================
# D.4.1G: CLI install command
# ===========================================================================


class _FakeInstallService:
    """Fake ZeAlfieService for CLI install tests."""

    def __init__(self, result_or_error, *, catalog_ids=("zesolver",)):
        self._result_or_error = result_or_error
        self._catalog_ids = catalog_ids
        self.install_called_with: list[dict] = []

    @property
    def catalog(self):
        from zealfie.products.catalog import ProductCatalog

        return _FakeInstallCatalog(self._catalog_ids)

    def install_product(self, product_id, *, resolver, fetcher, work_root,
                        dependency_wheelhouse=None, probe_distribution=None):
        self.install_called_with.append({
            "product_id": product_id,
            "resolver": resolver,
            "fetcher": fetcher,
            "work_root": work_root,
        })
        if isinstance(self._result_or_error, Exception):
            raise self._result_or_error
        return self._result_or_error


class _FakeInstallCatalog:
    """Fake ProductCatalog for install tests."""

    def __init__(self, available_ids=("zesolver",)):
        self._ids = available_ids

    def available_ids(self):
        return self._ids


def _fake_resolver(owner, repo, ref):
    return "a" * 40


def _fake_fetcher(owner, repo, commit_sha):
    return b"fake zip content"


# -- Success -------------------------------------------------------------------


def test_install_success_returns_zero(monkeypatch, tmp_path):
    """zealfie install success → exit 0, prints DeploymentResult."""
    result = DeploymentResult(success=True, active_slot_id="rt-new0000", previous_slot_id="rt-old0000")
    service = _FakeInstallService(result)
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    monkeypatch.setattr(cli, "_make_install_deps", lambda: (_fake_resolver, _fake_fetcher, tmp_path / "work"))
    stdout = StringIO()
    code = run(["install", "zesolver"], stdout=stdout)
    assert code == 0
    output = stdout.getvalue()
    assert "Success: yes" in output
    assert "Active slot: rt-new0000" in output
    assert len(service.install_called_with) == 1
    assert service.install_called_with[0]["product_id"] == "zesolver"


def test_install_failure_returns_3(monkeypatch, tmp_path):
    """zealfie install DeploymentResult(success=False) → exit 3."""
    result = DeploymentResult(success=False, reason="plan was blocked")
    service = _FakeInstallService(result)
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    monkeypatch.setattr(cli, "_make_install_deps", lambda: (_fake_resolver, _fake_fetcher, tmp_path / "work"))
    stdout = StringIO()
    code = run(["install", "zesolver"], stdout=stdout)
    assert code == 3
    output = stdout.getvalue()
    assert "Success: no" in output
    assert "plan was blocked" in output


def test_install_passes_deps_to_service(monkeypatch, tmp_path):
    """zealfie install forwards resolver, fetcher, work_root."""
    result = DeploymentResult(success=True, active_slot_id="rt-x")
    service = _FakeInstallService(result)
    work = tmp_path / "my-work"
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    monkeypatch.setattr(cli, "_make_install_deps", lambda: (_fake_resolver, _fake_fetcher, work))
    stdout = StringIO()
    code = run(["install", "zesolver"], stdout=stdout)
    assert code == 0
    assert len(service.install_called_with) == 1
    call = service.install_called_with[0]
    assert call["resolver"] is _fake_resolver
    assert call["fetcher"] is _fake_fetcher
    assert call["work_root"] == work
    # work_root should exist after handler runs
    assert work.is_dir()


# -- Unknown product -----------------------------------------------------------


def test_install_unknown_product_returns_2(monkeypatch):
    """zealfie install unknown product → exit 2, clean stderr."""
    from zealfie.app import UnknownProductError

    service = _FakeInstallService(UnknownProductError("zemosaic"), catalog_ids=("zesolver",))
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    monkeypatch.setattr(cli, "_make_install_deps", lambda: (_fake_resolver, _fake_fetcher, Path("/tmp")))
    import sys
    backup = sys.stderr
    try:
        sys.stderr = stderr = StringIO()
        stdout = StringIO()
        code = run(["install", "zemosaic"], stdout=stdout)
        assert code == 2
        assert "Unknown product: zemosaic" in stderr.getvalue()
        assert "zesolver" in stderr.getvalue()
        assert "Traceback" not in stderr.getvalue()
    finally:
        sys.stderr = backup


# -- RemoteSourceUnavailableError ----------------------------------------------


def test_install_no_remote_source_returns_7(monkeypatch, tmp_path):
    """Product has no remote_source → exit 7."""
    from zealfie.app import RemoteSourceUnavailableError

    service = _FakeInstallService(
        RemoteSourceUnavailableError("product 'zesolver' has no remote source")
    )
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    monkeypatch.setattr(cli, "_make_install_deps", lambda: (_fake_resolver, _fake_fetcher, tmp_path / "work"))
    import sys
    backup = sys.stderr
    try:
        sys.stderr = stderr = StringIO()
        stdout = StringIO()
        code = run(["install", "zesolver"], stdout=stdout)
        assert code == 7
        assert "cannot install 'zesolver'" in stderr.getvalue()
        assert "no remote source" in stderr.getvalue()
        assert "Traceback" not in stderr.getvalue()
    finally:
        sys.stderr = backup


# -- SourceResolutionError -----------------------------------------------------


def test_install_resolver_failure_returns_8(monkeypatch, tmp_path):
    """SourceResolutionError → exit 8."""
    from zealfie.sources import SourceResolutionError

    service = _FakeInstallService(
        SourceResolutionError("ref not-found not found")
    )
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    monkeypatch.setattr(cli, "_make_install_deps", lambda: (_fake_resolver, _fake_fetcher, tmp_path / "work"))
    import sys
    backup = sys.stderr
    try:
        sys.stderr = stderr = StringIO()
        stdout = StringIO()
        code = run(["install", "zesolver"], stdout=stdout)
        assert code == 8
        assert "cannot resolve source for 'zesolver'" in stderr.getvalue()
        assert "not found" in stderr.getvalue()
        assert "Traceback" not in stderr.getvalue()
    finally:
        sys.stderr = backup


# -- AcquisitionError ----------------------------------------------------------


def test_install_fetch_failure_returns_9(monkeypatch, tmp_path):
    """AcquisitionError → exit 9."""
    from zealfie.sources.acquisition import AcquisitionError

    service = _FakeInstallService(
        AcquisitionError("network error fetching archive")
    )
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    monkeypatch.setattr(cli, "_make_install_deps", lambda: (_fake_resolver, _fake_fetcher, tmp_path / "work"))
    import sys
    backup = sys.stderr
    try:
        sys.stderr = stderr = StringIO()
        stdout = StringIO()
        code = run(["install", "zesolver"], stdout=stdout)
        assert code == 9
        assert "cannot fetch source for 'zesolver'" in stderr.getvalue()
        assert "network error" in stderr.getvalue()
        assert "Traceback" not in stderr.getvalue()
    finally:
        sys.stderr = backup


# -- ArtifactRejectionError ----------------------------------------------------


def test_install_artifact_rejection_returns_3(monkeypatch, tmp_path):
    """ArtifactRejectionError → exit 3, clean stderr."""
    from zealfie.releases import ArtifactRejectionError

    service = _FakeInstallService(
        ArtifactRejectionError("invalid artifact filename")
    )
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    monkeypatch.setattr(cli, "_make_install_deps", lambda: (_fake_resolver, _fake_fetcher, tmp_path / "work"))
    import sys
    backup = sys.stderr
    try:
        sys.stderr = stderr = StringIO()
        stdout = StringIO()
        code = run(["install", "zesolver"], stdout=stdout)
        assert code == 3
        assert "install failed for 'zesolver'" in stderr.getvalue()
        assert "Traceback" not in stderr.getvalue()
    finally:
        sys.stderr = backup


# -- ProductInstallPreparationError --------------------------------------------


def test_install_prep_error_returns_3(monkeypatch, tmp_path):
    """ProductInstallPreparationError → exit 3."""
    from zealfie.app import ProductInstallPreparationError

    service = _FakeInstallService(
        ProductInstallPreparationError("build failed")
    )
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    monkeypatch.setattr(cli, "_make_install_deps", lambda: (_fake_resolver, _fake_fetcher, tmp_path / "work"))
    import sys
    backup = sys.stderr
    try:
        sys.stderr = stderr = StringIO()
        stdout = StringIO()
        code = run(["install", "zesolver"], stdout=stdout)
        assert code == 3
        assert "install failed for 'zesolver'" in stderr.getvalue()
        assert "Traceback" not in stderr.getvalue()
    finally:
        sys.stderr = backup


# -- CorruptSelectionError -----------------------------------------------------


def test_install_corrupt_selection_returns_3(monkeypatch, tmp_path):
    """CorruptSelectionError → exit 3, clean stderr."""
    from zealfie.products.selection import CorruptSelectionError

    service = _FakeInstallService(
        CorruptSelectionError("selection file corrupted")
    )
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    monkeypatch.setattr(cli, "_make_install_deps", lambda: (_fake_resolver, _fake_fetcher, tmp_path / "work"))
    import sys
    backup = sys.stderr
    try:
        sys.stderr = stderr = StringIO()
        stdout = StringIO()
        code = run(["install", "zesolver"], stdout=stdout)
        assert code == 3
        assert "install failed for 'zesolver'" in stderr.getvalue()
        assert "Traceback" not in stderr.getvalue()
    finally:
        sys.stderr = backup


# -- Parser tests --------------------------------------------------------------


def test_install_in_parser():
    """install subcommand is present in the argument parser."""
    p = cli.build_parser()
    args = p.parse_args(["install", "zesolver"])
    assert args.command == "install"
    assert args.product_id == "zesolver"


def test_install_requires_product_id():
    """install requires a product_id argument."""
    p = cli.build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["install"])


# -- Default work root ---------------------------------------------------------


def test_default_install_work_root_exists():
    """default_install_work_root returns a Path."""
    from zealfie.app.install_defaults import default_install_work_root

    root = default_install_work_root()
    assert isinstance(root, Path)
    assert "zealfie" in str(root)
    assert "work" in str(root).split("/")[-1] or root.name == "work"


def test_make_install_deps_returns_triple():
    """_make_install_deps returns (resolver, fetcher, work_root)."""
    resolver, fetcher, work_root = cli._make_install_deps()
    assert callable(resolver)
    assert callable(fetcher)
    assert isinstance(work_root, Path)


# -- No real network -----------------------------------------------------------


def test_install_never_touches_real_network(monkeypatch, tmp_path):
    """Install tests use only fake transports — no real GitHub."""
    result = DeploymentResult(success=True, active_slot_id="rt-x")
    service = _FakeInstallService(result)
    monkeypatch.setattr(cli, "_make_service", lambda: service)

    # Use callable mocks that record calls
    resolver_calls = []
    fetcher_calls = []

    def recording_resolver(owner, repo, ref):
        resolver_calls.append((owner, repo, ref))
        return "a" * 40

    def recording_fetcher(owner, repo, commit_sha):
        fetcher_calls.append((owner, repo, commit_sha))
        return b"zip"

    monkeypatch.setattr(
        cli, "_make_install_deps",
        lambda: (recording_resolver, recording_fetcher, tmp_path / "work"),
    )
    stdout = StringIO()
    code = run(["install", "zesolver"], stdout=stdout)
    assert code == 0
    # Resolver and fetcher are passed to service, not called directly by CLI.
    # The service call was recorded — no real network by definition.
    assert len(service.install_called_with) == 1

# -- ProductDeploymentPlanningError --------------------------------------------


def test_install_planning_error_returns_3(monkeypatch, tmp_path):
    """ProductDeploymentPlanningError → exit 3, clean stderr."""
    from zealfie.app import ProductDeploymentPlanningError

    service = _FakeInstallService(
        ProductDeploymentPlanningError("deployment plan blocked")
    )
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    monkeypatch.setattr(cli, "_make_install_deps", lambda: (_fake_resolver, _fake_fetcher, tmp_path / "work"))
    import sys
    backup = sys.stderr
    try:
        sys.stderr = stderr = StringIO()
        stdout = StringIO()
        code = run(["install", "zesolver"], stdout=stdout)
        assert code == 3
        assert "install failed for 'zesolver'" in stderr.getvalue()
        assert "Traceback" not in stderr.getvalue()
    finally:
        sys.stderr = backup


# -- PlanningError -------------------------------------------------------------


def test_install_planning_error_runtime_returns_3(monkeypatch, tmp_path):
    """PlanningError (runtime) → exit 3, clean stderr."""
    from zealfie.runtime import PlanningError

    service = _FakeInstallService(
        PlanningError("planning constraints violated")
    )
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    monkeypatch.setattr(cli, "_make_install_deps", lambda: (_fake_resolver, _fake_fetcher, tmp_path / "work"))
    import sys
    backup = sys.stderr
    try:
        sys.stderr = stderr = StringIO()
        stdout = StringIO()
        code = run(["install", "zesolver"], stdout=stdout)
        assert code == 3
        assert "install failed for 'zesolver'" in stderr.getvalue()
        assert "Traceback" not in stderr.getvalue()
    finally:
        sys.stderr = backup
