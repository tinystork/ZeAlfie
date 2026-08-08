"""Tests for the M0-8A deployment planning layer.

All tests are synthetic — no real venvs, no wheel installations, no
filesystem mutation.  Probes are faked with callables.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zealfie.components.model import ComponentDefinition, EntryPointContract
from zealfie.components.registry import ComponentRegistry
from zealfie.releases.model import VerifiedArtifact
from zealfie.runtime.model import RuntimeReasonCode, RuntimeState, RuntimeStatus
from zealfie.runtime.planning import (
    DeploymentAction,
    DeploymentPlan,
    DeploymentReasonCode,
    DeploymentStep,
    DesiredComponent,
    DesiredRuntimeState,
    PlanningError,
    build_deployment_plan,
)


# ---------------------------------------------------------------------------
# Synthetic helpers
# ---------------------------------------------------------------------------


def _va(
    component_id: str = "zesolver",
    version: str = "1.0.0",
    path: Path | None = None,
    size: int = 12345,
    sha256: str = "a" * 64,
    distribution_name: str = "ZeSolver",
    wheel_version: str = "1.0.0",
) -> VerifiedArtifact:
    return VerifiedArtifact(
        component_id=component_id,
        version=version,
        path=path or Path(f"/fake/{component_id}.whl"),
        size=size,
        sha256=sha256,
        distribution_name=distribution_name,
        wheel_version=wheel_version,
    )


def _dc(
    component_id: str = "zesolver",
    version: str = "1.0.0",
    distribution_name: str | None = None,
    wheel_version: str | None = None,
) -> DesiredComponent:
    """Build a synthetic DesiredComponent.

    When *distribution_name* is None, it defaults to the *component_id*
    so that it matches the default registry definitions.
    """
    dn = distribution_name if distribution_name is not None else component_id
    wv = wheel_version if wheel_version is not None else version

    return DesiredComponent(
        component_id=component_id,
        version=version,
        artifact=_va(
            component_id=component_id,
            version=version,
            distribution_name=dn,
            wheel_version=wv,
        ),
    )


def _registry(*ids: str) -> ComponentRegistry:
    """Build a synthetic registry.

    Component ids are used for all required fields; a default
    launch entry point is provided.
    """
    defs = tuple(
        ComponentDefinition(
            component_id=cid,
            display_name=cid.title(),
            distribution_name=cid,
            launch_entry_points=(EntryPointContract("console_scripts", cid),),
        )
        for cid in ids
    )
    return ComponentRegistry(defs)


def _status(state: RuntimeState, **kwargs: object) -> RuntimeStatus:
    defaults: dict[str, object] = {
        "runtime_root": Path("/fake/runtime"),
    }
    if state == RuntimeState.READY:
        defaults.setdefault("python_executable", Path("/fake/runtime/bin/python"))
        defaults.setdefault("python_version", "3.13.5")
        defaults.setdefault("active_slot_id", "rt-test")
        defaults.setdefault("active_path", Path("/fake/runtime/slots/rt-test"))
    elif state == RuntimeState.BROKEN:
        defaults.setdefault("reason", "shared runtime is BROKEN")
    defaults.update(kwargs)
    return RuntimeStatus(state=state, **defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# DesiredComponent validation
# ---------------------------------------------------------------------------


def test_desired_component_id_must_match_artifact() -> None:
    with pytest.raises(ValueError, match="component_id"):
        DesiredComponent(
            component_id="wrong",
            version="1.0.0",
            artifact=_va(component_id="zesolver", version="1.0.0"),
        )


def test_desired_component_version_must_match_artifact() -> None:
    with pytest.raises(ValueError, match="version"):
        DesiredComponent(
            component_id="zesolver",
            version="2.0.0",
            artifact=_va(component_id="zesolver", version="1.0.0"),
        )


def test_desired_component_version_must_match_wheel_version() -> None:
    with pytest.raises(ValueError, match="wheel_version"):
        DesiredComponent(
            component_id="zesolver",
            version="1.0.0",
            artifact=_va(
                component_id="zesolver",
                version="1.0.0",
                wheel_version="1.0.1",
            ),
        )


def test_desired_component_rejects_empty_id() -> None:
    with pytest.raises(ValueError, match="component_id"):
        DesiredComponent(
            component_id="",
            version="1.0.0",
            artifact=_va(component_id="", version="1.0.0"),
        )


def test_desired_component_rejects_empty_version() -> None:
    with pytest.raises(ValueError, match="version"):
        DesiredComponent(
            component_id="zesolver",
            version="",
            artifact=_va(component_id="zesolver", version=""),
        )


# ---------------------------------------------------------------------------
# DesiredRuntimeState validation
# ---------------------------------------------------------------------------


def test_desired_state_rejects_empty() -> None:
    with pytest.raises(ValueError, match="at least one component"):
        DesiredRuntimeState(components=())


def test_desired_state_rejects_duplicates() -> None:
    dc = _dc("zesolver")
    with pytest.raises(ValueError, match="duplicate"):
        DesiredRuntimeState(components=(dc, dc))


def test_desired_state_is_sorted() -> None:
    a = _dc("b")
    b = _dc("a")
    state = DesiredRuntimeState(components=(a, b))
    assert [c.component_id for c in state.components] == ["a", "b"]


# ---------------------------------------------------------------------------
# 1) Desired state missing a registry component -> PlanningError
# ---------------------------------------------------------------------------


def test_missing_registry_component_raises_planning_error() -> None:
    desired = DesiredRuntimeState(components=(_dc("zesolver"),))
    registry = _registry("zesolver", "zemosaic")
    status = _status(RuntimeState.READY)
    with pytest.raises(PlanningError, match="missing component ids"):
        build_deployment_plan(desired, registry, status,
                              probe_distribution=_default_probe)


# ---------------------------------------------------------------------------
# 2) Desired state includes unknown/extra component -> PlanningError
# ---------------------------------------------------------------------------


def test_extra_unknown_component_raises_planning_error() -> None:
    desired = DesiredRuntimeState(
        components=(_dc("zesolver"), _dc("zefake"))
    )
    registry = _registry("zesolver")
    status = _status(RuntimeState.READY)
    with pytest.raises(PlanningError, match="unknown component ids"):
        build_deployment_plan(desired, registry, status,
                              probe_distribution=_default_probe)


# ---------------------------------------------------------------------------
# 3) ABSENT -> deterministic INSTALL for all, no probe calls
# ---------------------------------------------------------------------------


def test_absent_plans_install_for_all_no_probe() -> None:
    desired = DesiredRuntimeState(components=(_dc("zesolver"), _dc("zemosaic")))
    registry = _registry("zesolver", "zemosaic")
    status = _status(RuntimeState.ABSENT)

    def must_not_be_called(*args, **kwargs):
        raise AssertionError("probe must not be called for ABSENT runtime")

    plan = build_deployment_plan(desired, registry, status,
                                 probe_distribution=must_not_be_called)

    assert plan.runtime_state == RuntimeState.ABSENT
    assert plan.blocked is False
    assert len(plan.steps) == 2
    # Deterministic order by component_id.
    assert [s.component_id for s in plan.steps] == ["zemosaic", "zesolver"]
    for step in plan.steps:
        assert step.action == DeploymentAction.INSTALL
        assert step.reason_code == DeploymentReasonCode.RUNTIME_ABSENT
        assert step.artifact is not None
        assert step.current_version is None


# ---------------------------------------------------------------------------
# 4) BROKEN -> blocked plan, no probe calls
# ---------------------------------------------------------------------------


def test_broken_plans_blocked_for_all() -> None:
    desired = DesiredRuntimeState(components=(_dc("zesolver"),))
    registry = _registry("zesolver")
    status = _status(RuntimeState.BROKEN)

    def must_not_be_called(*args, **kwargs):
        raise AssertionError("probe must not be called for BROKEN runtime")

    plan = build_deployment_plan(desired, registry, status,
                                 probe_distribution=must_not_be_called)

    assert plan.blocked is True
    assert plan.blocked_reason is not None
    assert "BROKEN" in (plan.blocked_reason or "")
    for step in plan.steps:
        assert step.action == DeploymentAction.BLOCKED
        assert step.reason_code == DeploymentReasonCode.RUNTIME_BROKEN
        assert step.artifact is not None


# ---------------------------------------------------------------------------
# 5) READY matching version+contract -> KEEP, artifact still present
# ---------------------------------------------------------------------------


def test_ready_matching_keep() -> None:
    desired = DesiredRuntimeState(components=(_dc("zesolver"),))
    registry = _registry("zesolver")
    status = _status(RuntimeState.READY)

    def probe(runtime_python: str, dist_name: str) -> dict:
        assert dist_name == "zesolver"
        return {
            "python_version": "3.13.5",
            "installed": True,
            "version": "1.0.0",
            "entry_points": [
                {"group": "console_scripts", "name": "zesolver",
                 "value": "zesolver:main"},
            ],
        }

    plan = build_deployment_plan(desired, registry, status,
                                 probe_distribution=probe)

    assert plan.blocked is False
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.action == DeploymentAction.KEEP
    assert step.reason_code == DeploymentReasonCode.ALREADY_SATISFIED
    assert step.artifact is not None
    assert step.current_version == "1.0.0"


# ---------------------------------------------------------------------------
# 6) READY installed false -> INSTALL
# ---------------------------------------------------------------------------


def test_ready_not_installed_install() -> None:
    desired = DesiredRuntimeState(components=(_dc("zesolver"),))
    registry = _registry("zesolver")
    status = _status(RuntimeState.READY)

    def probe(runtime_python: str, dist_name: str) -> dict:
        return {"python_version": "3.13.5", "installed": False,
                "version": None, "entry_points": []}

    plan = build_deployment_plan(desired, registry, status,
                                 probe_distribution=probe)

    assert plan.blocked is False
    step = plan.steps[0]
    assert step.action == DeploymentAction.INSTALL
    assert step.reason_code == DeploymentReasonCode.DISTRIBUTION_MISSING


# ---------------------------------------------------------------------------
# 7) READY version mismatch -> INSTALL
# ---------------------------------------------------------------------------


def test_ready_version_mismatch_install() -> None:
    desired = DesiredRuntimeState(components=(_dc("zesolver"),))
    registry = _registry("zesolver")
    status = _status(RuntimeState.READY)

    def probe(runtime_python: str, dist_name: str) -> dict:
        return {
            "python_version": "3.13.5",
            "installed": True,
            "version": "0.9.0",  # older than desired 1.0.0
            "entry_points": [
                {"group": "console_scripts", "name": "zesolver",
                 "value": "zesolver:main"},
            ],
        }

    plan = build_deployment_plan(desired, registry, status,
                                 probe_distribution=probe)

    step = plan.steps[0]
    assert step.action == DeploymentAction.INSTALL
    assert step.reason_code == DeploymentReasonCode.VERSION_MISMATCH
    assert step.current_version == "0.9.0"
    assert step.artifact is not None


# ---------------------------------------------------------------------------
# 8) READY missing launch contract -> INSTALL/repair
# ---------------------------------------------------------------------------


def test_ready_missing_launch_contract_install() -> None:
    desired = DesiredRuntimeState(components=(_dc("zesolver"),))
    registry = _registry("zesolver")
    status = _status(RuntimeState.READY)

    def probe(runtime_python: str, dist_name: str) -> dict:
        return {
            "python_version": "3.13.5",
            "installed": True,
            "version": "1.0.0",
            "entry_points": [],  # No matching entry point
        }

    plan = build_deployment_plan(desired, registry, status,
                                 probe_distribution=probe)

    step = plan.steps[0]
    assert step.action == DeploymentAction.INSTALL
    assert step.reason_code == DeploymentReasonCode.LAUNCH_CONTRACT_MISMATCH
    assert "launch contract" in (step.reason or "").lower()


# ---------------------------------------------------------------------------
# 9) READY probe exception or malformed payload -> blocked/fail closed
# ---------------------------------------------------------------------------


def test_ready_probe_exception_blocks_plan() -> None:
    desired = DesiredRuntimeState(components=(_dc("zesolver"),))
    registry = _registry("zesolver")
    status = _status(RuntimeState.READY)

    def probe(runtime_python: str, dist_name: str) -> dict:
        raise RuntimeError("probe process crashed")

    plan = build_deployment_plan(desired, registry, status,
                                 probe_distribution=probe)

    assert plan.blocked is True
    assert plan.runtime_state == RuntimeState.READY
    for step in plan.steps:
        assert step.action == DeploymentAction.BLOCKED
        assert step.reason_code == DeploymentReasonCode.PROBE_FAILED
    assert "probe process crashed" in (plan.blocked_reason or "")


def test_ready_probe_returns_non_dict_blocked() -> None:
    desired = DesiredRuntimeState(components=(_dc("zesolver"),))
    registry = _registry("zesolver")
    status = _status(RuntimeState.READY)

    def probe(runtime_python: str, dist_name: str) -> str:
        return "not a dict"  # type: ignore[return-value]

    plan = build_deployment_plan(desired, registry, status,
                                 probe_distribution=probe)  # type: ignore[arg-type]

    assert plan.blocked is True
    assert plan.steps[0].reason_code == DeploymentReasonCode.PROBE_FAILED
    assert "non-dict" in (plan.steps[0].reason or "")


# ---------------------------------------------------------------------------
# 10) Strict payload validation — malformed entries -> BLOCKED/PROBE_FAILED
# ---------------------------------------------------------------------------


def test_installed_string_false_is_malformed_blocks() -> None:
    """installed as string "false" is truthy -> must be caught as malformed."""
    desired = DesiredRuntimeState(components=(_dc("zesolver"),))
    registry = _registry("zesolver")
    status = _status(RuntimeState.READY)

    def probe(runtime_python: str, dist_name: str) -> dict:
        return {
            "python_version": "3.13.5",
            "installed": "false",  # string, not bool — was truthy bypass
            "version": "1.0",
            "entry_points": [],
        }

    plan = build_deployment_plan(desired, registry, status,
                                 probe_distribution=probe)

    assert plan.blocked is True
    assert plan.steps[0].action == DeploymentAction.BLOCKED
    assert plan.steps[0].reason_code == DeploymentReasonCode.PROBE_FAILED
    assert "installed must be bool" in (plan.steps[0].reason or "")


def test_installed_missing_is_malformed_blocks() -> None:
    """Missing installed key -> probe.get returns None, not bool -> blocked."""
    desired = DesiredRuntimeState(components=(_dc("zesolver"),))
    registry = _registry("zesolver")
    status = _status(RuntimeState.READY)

    def probe(runtime_python: str, dist_name: str) -> dict:
        return {
            "python_version": "3.13.5",
            # installed key intentionally missing
            "version": "1.0.0",
            "entry_points": [],
        }

    plan = build_deployment_plan(desired, registry, status,
                                 probe_distribution=probe)

    assert plan.blocked is True
    assert plan.steps[0].action == DeploymentAction.BLOCKED
    assert plan.steps[0].reason_code == DeploymentReasonCode.PROBE_FAILED
    assert "installed must be bool" in (plan.steps[0].reason or "")


def test_installed_true_version_none_blocks() -> None:
    """installed=True with version=None is malformed -> blocked."""
    desired = DesiredRuntimeState(components=(_dc("zesolver"),))
    registry = _registry("zesolver")
    status = _status(RuntimeState.READY)

    def probe(runtime_python: str, dist_name: str) -> dict:
        return {
            "python_version": "3.13.5",
            "installed": True,
            "version": None,
            "entry_points": [
                {"group": "console_scripts", "name": "zesolver", "value": "..."},
            ],
        }

    plan = build_deployment_plan(desired, registry, status,
                                 probe_distribution=probe)

    assert plan.blocked is True
    assert plan.steps[0].action == DeploymentAction.BLOCKED
    assert plan.steps[0].reason_code == DeploymentReasonCode.PROBE_FAILED
    assert "version must be non-empty str" in (plan.steps[0].reason or "")


def test_installed_true_version_non_string_blocks() -> None:
    """installed=True with version=int -> was stringified; now blocked."""
    desired = DesiredRuntimeState(components=(_dc("zesolver"),))
    registry = _registry("zesolver")
    status = _status(RuntimeState.READY)

    def probe(runtime_python: str, dist_name: str) -> dict:
        return {
            "python_version": "3.13.5",
            "installed": True,
            "version": {"major": 1, "minor": 0},  # non-string object
            "entry_points": [
                {"group": "console_scripts", "name": "zesolver", "value": "..."},
            ],
        }

    plan = build_deployment_plan(desired, registry, status,
                                 probe_distribution=probe)

    assert plan.blocked is True
    assert plan.steps[0].action == DeploymentAction.BLOCKED
    assert plan.steps[0].reason_code == DeploymentReasonCode.PROBE_FAILED
    assert "version must be non-empty str" in (plan.steps[0].reason or "")


def test_entry_points_not_list_blocks() -> None:
    """entry_points: "not-a-list" with matching installed/version -> blocked."""
    desired = DesiredRuntimeState(components=(_dc("zesolver"),))
    registry = _registry("zesolver")
    status = _status(RuntimeState.READY)

    def probe(runtime_python: str, dist_name: str) -> dict:
        return {
            "python_version": "3.13.5",
            "installed": True,
            "version": "1.0.0",
            "entry_points": "not-a-list",
        }

    plan = build_deployment_plan(desired, registry, status,
                                 probe_distribution=probe)

    assert plan.blocked is True
    assert plan.steps[0].action == DeploymentAction.BLOCKED
    assert plan.steps[0].reason_code == DeploymentReasonCode.PROBE_FAILED
    assert "entry_points must be a list" in (plan.steps[0].reason or "")


def test_entry_points_contains_non_dict_blocks() -> None:
    """entry_points list with a string element -> blocked."""
    desired = DesiredRuntimeState(components=(_dc("zesolver"),))
    registry = _registry("zesolver")
    status = _status(RuntimeState.READY)

    def probe(runtime_python: str, dist_name: str) -> dict:
        return {
            "python_version": "3.13.5",
            "installed": True,
            "version": "1.0.0",
            "entry_points": [
                {"group": "console_scripts", "name": "zesolver", "value": "..."},
                "not-a-dict",
            ],
        }

    plan = build_deployment_plan(desired, registry, status,
                                 probe_distribution=probe)

    assert plan.blocked is True
    assert plan.steps[0].action == DeploymentAction.BLOCKED
    assert plan.steps[0].reason_code == DeploymentReasonCode.PROBE_FAILED
    assert "must be dict" in (plan.steps[0].reason or "")


def test_entry_points_non_string_group_name_blocks() -> None:
    """entry_points dict with int group/name -> blocked."""
    desired = DesiredRuntimeState(components=(_dc("zesolver"),))
    registry = _registry("zesolver")
    status = _status(RuntimeState.READY)

    def probe(runtime_python: str, dist_name: str) -> dict:
        return {
            "python_version": "3.13.5",
            "installed": True,
            "version": "1.0.0",
            "entry_points": [
                {"group": "console_scripts", "name": "zesolver", "value": "..."},
                {"group": 123, "name": "bad-ep"},
            ],
        }

    plan = build_deployment_plan(desired, registry, status,
                                 probe_distribution=probe)

    assert plan.blocked is True
    assert plan.steps[0].action == DeploymentAction.BLOCKED
    assert plan.steps[0].reason_code == DeploymentReasonCode.PROBE_FAILED
    assert "group/name must be str" in (plan.steps[0].reason or "")


# ---------------------------------------------------------------------------
# 10-B) M0-8A external closure — absent-distribution payload hardening
# ---------------------------------------------------------------------------


def test_installed_false_non_none_version_blocks() -> None:
    """installed=False with version='1.0' must block, not INSTALL."""
    desired = DesiredRuntimeState(components=(_dc("zesolver"),))
    registry = _registry("zesolver")
    status = _status(RuntimeState.READY)

    def probe(runtime_python: str, dist_name: str) -> dict:
        return {"python_version": "3.13.5", "installed": False,
                "version": "1.0", "entry_points": []}

    plan = build_deployment_plan(desired, registry, status,
                                 probe_distribution=probe)

    assert plan.blocked is True
    assert plan.steps[0].action == DeploymentAction.BLOCKED
    assert plan.steps[0].reason_code == DeploymentReasonCode.PROBE_FAILED
    assert "version must be None" in (plan.steps[0].reason or "")


def test_installed_false_entry_points_none_blocks() -> None:
    """installed=False with entry_points=None must block, not INSTALL."""
    desired = DesiredRuntimeState(components=(_dc("zesolver"),))
    registry = _registry("zesolver")
    status = _status(RuntimeState.READY)

    def probe(runtime_python: str, dist_name: str) -> dict:
        return {"python_version": "3.13.5", "installed": False,
                "version": None, "entry_points": None}

    plan = build_deployment_plan(desired, registry, status,
                                 probe_distribution=probe)

    assert plan.blocked is True
    assert plan.steps[0].action == DeploymentAction.BLOCKED
    assert plan.steps[0].reason_code == DeploymentReasonCode.PROBE_FAILED
    assert "entry_points must be a list" in (plan.steps[0].reason or "")


def test_installed_false_entry_points_non_list_blocks() -> None:
    """installed=False with entry_points='not-a-list' must block."""
    desired = DesiredRuntimeState(components=(_dc("zesolver"),))
    registry = _registry("zesolver")
    status = _status(RuntimeState.READY)

    def probe(runtime_python: str, dist_name: str) -> dict:
        return {"python_version": "3.13.5", "installed": False,
                "version": None, "entry_points": "not-a-list"}

    plan = build_deployment_plan(desired, registry, status,
                                 probe_distribution=probe)

    assert plan.blocked is True
    assert plan.steps[0].action == DeploymentAction.BLOCKED
    assert plan.steps[0].reason_code == DeploymentReasonCode.PROBE_FAILED
    assert "entry_points must be a list" in (plan.steps[0].reason or "")


def test_installed_false_entry_points_non_empty_blocks() -> None:
    """installed=False with non-empty entry_points must block."""
    desired = DesiredRuntimeState(components=(_dc("zesolver"),))
    registry = _registry("zesolver")
    status = _status(RuntimeState.READY)

    def probe(runtime_python: str, dist_name: str) -> dict:
        return {"python_version": "3.13.5", "installed": False,
                "version": None,
                "entry_points": [
                    {"group": "console_scripts", "name": "zesolver", "value": "..."},
                ]}

    plan = build_deployment_plan(desired, registry, status,
                                 probe_distribution=probe)

    assert plan.blocked is True
    assert plan.steps[0].action == DeploymentAction.BLOCKED
    assert plan.steps[0].reason_code == DeploymentReasonCode.PROBE_FAILED
    assert "entry_points must be empty" in (plan.steps[0].reason or "")


def test_installed_false_missing_version_key_blocks() -> None:
    """installed=False with missing 'version' key must block."""
    desired = DesiredRuntimeState(components=(_dc("zesolver"),))
    registry = _registry("zesolver")
    status = _status(RuntimeState.READY)

    def probe(runtime_python: str, dist_name: str) -> dict:
        return {"python_version": "3.13.5", "installed": False,
                "entry_points": []}

    plan = build_deployment_plan(desired, registry, status,
                                 probe_distribution=probe)

    assert plan.blocked is True
    assert plan.steps[0].action == DeploymentAction.BLOCKED
    assert plan.steps[0].reason_code == DeploymentReasonCode.PROBE_FAILED
    assert "missing 'version' key" in (plan.steps[0].reason or "")


def test_installed_false_missing_entry_points_key_blocks() -> None:
    """installed=False with missing 'entry_points' key must block."""
    desired = DesiredRuntimeState(components=(_dc("zesolver"),))
    registry = _registry("zesolver")
    status = _status(RuntimeState.READY)

    def probe(runtime_python: str, dist_name: str) -> dict:
        return {"python_version": "3.13.5", "installed": False,
                "version": None}

    plan = build_deployment_plan(desired, registry, status,
                                 probe_distribution=probe)

    assert plan.blocked is True
    assert plan.steps[0].action == DeploymentAction.BLOCKED
    assert plan.steps[0].reason_code == DeploymentReasonCode.PROBE_FAILED
    assert "missing 'entry_points' key" in (plan.steps[0].reason or "")


def test_installed_false_exact_reproduced_payload_blocks() -> None:
    """Exact reproduced payload: {installed:False, version:'1.0', entry_points:[123]}."""
    desired = DesiredRuntimeState(components=(_dc("zesolver"),))
    registry = _registry("zesolver")
    status = _status(RuntimeState.READY)

    def probe(runtime_python: str, dist_name: str) -> dict:
        return {"python_version": "3.13.5", "installed": False,
                "version": "1.0", "entry_points": [123]}

    plan = build_deployment_plan(desired, registry, status,
                                 probe_distribution=probe)

    assert plan.blocked is True
    assert plan.steps[0].action == DeploymentAction.BLOCKED
    assert plan.steps[0].reason_code == DeploymentReasonCode.PROBE_FAILED
    # The first validation failure is version must be None, so that's
    # what the error message should contain.
    assert "version must be None" in (plan.steps[0].reason or "")


# 11) Default probe — READY without explicit probe_distribution
# ---------------------------------------------------------------------------


def test_ready_uses_default_probe_distribution(monkeypatch) -> None:
    """Without explicit probe_distribution, defaults to real probe_runtime_distribution.

    Monkeypatches the module-level import so no real subprocess runs.
    """
    desired = DesiredRuntimeState(components=(_dc("zesolver"),))
    registry = _registry("zesolver")
    status = _status(RuntimeState.READY)

    def fake_real_probe(runtime_python: str, dist_name: str) -> dict:
        return {
            "python_version": "3.13.5",
            "installed": True,
            "version": "1.0.0",
            "entry_points": [
                {"group": "console_scripts", "name": "zesolver",
                 "value": "zesolver:main"},
            ],
        }

    monkeypatch.setattr(
        "zealfie.runtime.planning.probe_runtime_distribution",
        fake_real_probe,
    )

    # No probe_distribution= passed — should use the patched default.
    plan = build_deployment_plan(desired, registry, status)

    assert plan.blocked is False
    assert plan.steps[0].action == DeploymentAction.KEEP
    assert plan.steps[0].reason_code == DeploymentReasonCode.ALREADY_SATISFIED


# ---------------------------------------------------------------------------
# 12) Multi-component deterministic order + completeness guard
# ---------------------------------------------------------------------------


def test_multi_component_deterministic_order() -> None:
    desired = DesiredRuntimeState(
        components=(_dc("zemosaic"), _dc("zesolver"), _dc("zeanalyser"))
    )
    registry = _registry("zesolver", "zemosaic", "zeanalyser")
    status = _status(RuntimeState.ABSENT)

    plan = build_deployment_plan(desired, registry, status)

    assert [s.component_id for s in plan.steps] == [
        "zeanalyser", "zemosaic", "zesolver"
    ]
    assert len(plan.steps) == 3


def test_single_component_update_impossible_when_registry_has_two() -> None:
    """Cannot express a partial update for one component when registry has two."""
    desired = DesiredRuntimeState(components=(_dc("zesolver"),))
    registry = _registry("zesolver", "zemosaic")
    status = _status(RuntimeState.READY)

    with pytest.raises(PlanningError, match="missing component ids"):
        build_deployment_plan(desired, registry, status,
                              probe_distribution=_default_probe)


# ---------------------------------------------------------------------------
# 13) Distribution name mismatch -> PlanningError
# ---------------------------------------------------------------------------


def test_artifact_distribution_name_mismatch_rejected() -> None:
    desired = DesiredRuntimeState(
        components=(
            DesiredComponent(
                component_id="zesolver",
                version="1.0.0",
                artifact=_va(
                    component_id="zesolver",
                    version="1.0.0",
                    distribution_name="SomethingElse",
                ),
            ),
        )
    )
    registry = _registry("zesolver")
    status = _status(RuntimeState.READY)

    with pytest.raises(PlanningError, match="distribution_name mismatch"):
        build_deployment_plan(desired, registry, status,
                              probe_distribution=_default_probe)


def test_distribution_name_normalisation_accepted() -> None:
    """Normalised names must match (e.g. Foo_Bar vs foo-bar)."""
    desired = DesiredRuntimeState(
        components=(
            DesiredComponent(
                component_id="zesolver",
                version="1.0.0",
                artifact=_va(
                    component_id="zesolver",
                    version="1.0.0",
                    distribution_name="Ze_Solver",  # normalises to "ze-solver"
                ),
            ),
        )
    )
    # Use a definition whose distribution_name normalises to the same value.
    registry = ComponentRegistry(
        (
            ComponentDefinition(
                component_id="zesolver",
                display_name="ZeSolver",
                distribution_name="Ze-Solver",  # normalises to "ze-solver"
                launch_entry_points=(EntryPointContract("console_scripts", "zesolver"),),
            ),
        )
    )
    status = _status(RuntimeState.READY)

    def probe(runtime_python: str, dist_name: str) -> dict:
        assert dist_name == "Ze-Solver"  # raw registry name
        return {
            "python_version": "3.13.5",
            "installed": True,
            "version": "1.0.0",
            "entry_points": [
                {"group": "console_scripts", "name": "zesolver",
                 "value": "zesolver:main"},
            ],
        }

    plan = build_deployment_plan(desired, registry, status,
                                 probe_distribution=probe)
    assert plan.steps[0].action == DeploymentAction.KEEP


# ---------------------------------------------------------------------------
# READY with no python_executable -> PlanningError
# ---------------------------------------------------------------------------


def test_ready_without_python_executable_raises() -> None:
    desired = DesiredRuntimeState(components=(_dc("zesolver"),))
    registry = _registry("zesolver")
    status = _status(RuntimeState.READY, python_executable=None)

    with pytest.raises(PlanningError, match="python_executable"):
        build_deployment_plan(desired, registry, status,
                              probe_distribution=_default_probe)


# ---------------------------------------------------------------------------
# DeploymentPlan immutability
# ---------------------------------------------------------------------------


def test_deployment_plan_is_frozen() -> None:
    plan = DeploymentPlan(
        desired_state=DesiredRuntimeState(components=(_dc("zesolver"),)),
        runtime_state=RuntimeState.ABSENT,
        steps=(),
    )
    with pytest.raises(Exception):
        plan.steps = ()  # type: ignore[misc]


def test_deployment_step_artifact_present_even_for_keep() -> None:
    """Every DeploymentStep must carry the artifact, even for KEEP."""
    desired = DesiredRuntimeState(components=(_dc("zesolver"),))
    registry = _registry("zesolver")
    status = _status(RuntimeState.READY)

    def probe(runtime_python: str, dist_name: str) -> dict:
        return {
            "python_version": "3.13.5",
            "installed": True,
            "version": "1.0.0",
            "entry_points": [
                {"group": "console_scripts", "name": "zesolver",
                 "value": "zesolver:main"},
            ],
        }

    plan = build_deployment_plan(desired, registry, status,
                                 probe_distribution=probe)
    step = plan.steps[0]
    assert step.artifact is not None
    assert step.artifact.component_id == "zesolver"
    assert step.artifact.version == "1.0.0"


# ---------------------------------------------------------------------------
# No mutation: smoke test that no filesystem side-effects occur
# ---------------------------------------------------------------------------


def test_build_plan_is_pure_no_fs_side_effects(tmp_path: Path) -> None:
    """Ensure that build_deployment_plan doesn't create any files/dirs."""
    before = set(tmp_path.iterdir())

    desired = DesiredRuntimeState(components=(_dc("zesolver"),))
    registry = _registry("zesolver")
    status = _status(RuntimeState.ABSENT)

    build_deployment_plan(desired, registry, status)

    after = set(tmp_path.iterdir())
    assert before == after


# ---------------------------------------------------------------------------
# Default probe for tests that need one but don't assert on results
# ---------------------------------------------------------------------------


def _default_probe(runtime_python: str, dist_name: str) -> dict[str, object]:
    return {
        "python_version": "3.13.5",
        "installed": False,
        "version": None,
        "entry_points": [],
    }


# ---------------------------------------------------------------------------
# Verify imports from zealfie.runtime
# ---------------------------------------------------------------------------


def test_planning_types_exported_from_runtime_package() -> None:
    from zealfie import runtime as rt

    assert rt.DeploymentAction is DeploymentAction
    assert rt.DeploymentPlan is DeploymentPlan
    assert rt.DeploymentReasonCode is DeploymentReasonCode
    assert rt.DeploymentStep is DeploymentStep
    assert rt.DesiredComponent is DesiredComponent
    assert rt.DesiredRuntimeState is DesiredRuntimeState
    assert rt.PlanningError is PlanningError
    assert rt.build_deployment_plan is build_deployment_plan
