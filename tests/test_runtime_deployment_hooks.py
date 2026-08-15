"""Tests for the M1-2I ``apply_deployment_plan`` hooks.

Covers the two minimal backward-compatible keyword hooks:

* default behaviour with ``cancel_check=None`` / ``pre_activate=None``
  is EXACTLY the pre-hook behaviour (existing-style deployment
  succeeds; identity hooks change nothing);
* ``pre_activate`` returning an error string blocks activation with
  ``reason="pre-activation gate failed: <err>"``, no activation, active
  pointer unchanged, and the hook receives the transaction with the
  candidate path;
* ``cancel_check`` raising ``DeploymentCancelledError`` propagates (an
  interruption, not a failure — no result object);
* ``cancel_check`` raising any other exception becomes a
  ``DeploymentResult(success=False)`` failure;
* the cancellation checkpoints fire exactly once per documented site;
* the hooks are keyword-only parameters.

All tests are ``zealfie_slow`` (real venv + pip installs).
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.zealfie_slow

from zealfie.components.model import ComponentDefinition, EntryPointContract
from zealfie.components.registry import ComponentRegistry
from zealfie.runtime import (
    CandidateState,
    DeploymentCancelledError,
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
from zealfie.runtime.probe import probe_runtime_distribution
from zealfie.runtime.transaction import RuntimeTransaction

WITNESS_DEF = ComponentDefinition(
    component_id="zewitness",
    display_name="ZeWitness",
    distribution_name="zealfie-witness",
    launch_entry_points=(EntryPointContract("console_scripts", "zewitness"),),
)


def _registry() -> ComponentRegistry:
    return ComponentRegistry((WITNESS_DEF,))


def _plan_ready(witness_v1: Path, active_slot_id: str) -> DeploymentPlan:
    """A READY single-component plan with no dependency lock."""
    import hashlib

    from zealfie.building import inspect_wheel
    from zealfie.releases.model import VerifiedArtifact

    info = inspect_wheel(witness_v1)
    size = witness_v1.stat().st_size
    h = hashlib.sha256()
    with open(witness_v1, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    artifact = VerifiedArtifact(
        component_id="zewitness",
        version="0.0.1",
        path=witness_v1,
        size=size,
        sha256=h.hexdigest(),
        distribution_name=info.distribution_name,
        wheel_version=info.version,
    )
    desired = DesiredRuntimeState(
        components=(
            DesiredComponent(
                component_id="zewitness",
                version="0.0.1",
                artifact=artifact,
            ),
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
        desired, _registry(), status, probe_distribution=probe
    )


def _slot_python(slot_dir: Path) -> Path:
    if sys.platform == "win32":
        return slot_dir / "Scripts" / "python.exe"
    return slot_dir / "bin" / "python"


def _active_pointer_text(layout: RuntimeLayout) -> str:
    if layout.active_pointer.is_file():
        return layout.active_pointer.read_text(encoding="utf-8")
    return "<absent>"


# =============================================================================
# Hooks are keyword-only with None defaults
# =============================================================================


def test_hooks_are_keyword_only_with_none_defaults() -> None:
    signature = inspect.signature(apply_deployment_plan)
    for name in ("cancel_check", "pre_activate"):
        parameter = signature.parameters[name]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is None


# =============================================================================
# Defaults preserve existing behaviour exactly
# =============================================================================


def test_no_hooks_existing_style_deployment_succeeds(
    tmp_path: Path, witness_v1: Path,
) -> None:
    """An existing-style deployment with hooks omitted behaves exactly
    as before."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_before = rt.status().active_slot_id

    plan = _plan_ready(witness_v1, active_before)
    result = apply_deployment_plan(plan, registry=_registry(), runtime=rt)
    assert result.success is True, f"deployment failed: {result.reason}"

    final = rt.status()
    assert final.active_slot_id != active_before
    probe = probe_runtime_distribution(
        str(_slot_python(final.active_path)), "zealfie-witness"
    )
    assert probe["installed"] is True
    assert probe["version"] == "0.0.1"


def test_identity_hooks_change_nothing(tmp_path: Path, witness_v1: Path) -> None:
    """Passing no-op hooks changes nothing about the deployment outcome."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_before = rt.status().active_slot_id

    plan = _plan_ready(witness_v1, active_before)
    result = apply_deployment_plan(
        plan,
        registry=_registry(),
        runtime=rt,
        cancel_check=lambda: None,
        pre_activate=lambda txn: None,
    )
    assert result.success is True, f"deployment failed: {result.reason}"

    final = rt.status()
    assert final.active_slot_id != active_before
    assert final.previous_slot_id == active_before
    probe = probe_runtime_distribution(
        str(_slot_python(final.active_path)), "zealfie-witness"
    )
    assert probe["installed"] is True
    assert probe["version"] == "0.0.1"


# =============================================================================
# pre_activate gate hook
# =============================================================================


def test_pre_activate_error_blocks_activation(
    tmp_path: Path, witness_v1: Path,
) -> None:
    """A non-None error string from ``pre_activate`` fails the deployment
    before activation; the active pointer is untouched and the candidate
    (already created) is not activated."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_before = rt.status().active_slot_id
    pointer_before = _active_pointer_text(layout)

    seen: list[RuntimeTransaction] = []

    def refusing_hook(txn: RuntimeTransaction) -> str | None:
        seen.append(txn)
        return "gate refused"

    plan = _plan_ready(witness_v1, active_before)
    result = apply_deployment_plan(
        plan,
        registry=_registry(),
        runtime=rt,
        pre_activate=refusing_hook,
    )

    assert result.success is False
    assert result.reason == "pre-activation gate failed: gate refused"
    assert result.active_slot_id is None

    # The hook received the transaction with the candidate path.
    assert len(seen) == 1
    txn = seen[0]
    assert txn.candidate_slot_id.startswith("rt-")
    assert txn.candidate_path.is_dir()
    assert _slot_python(txn.candidate_path).is_file()

    # No activation: pointer byte-identical, old runtime still active.
    assert _active_pointer_text(layout) == pointer_before
    assert rt.status().active_slot_id == active_before
    probe = probe_runtime_distribution(
        str(_slot_python(layout.slot_path(active_before))), "zealfie-witness"
    )
    assert probe["installed"] is False  # old slot never touched


def test_pre_activate_exception_becomes_failure(
    tmp_path: Path, witness_v1: Path,
) -> None:
    """An exception raised by ``pre_activate`` is caught and converted to
    a failure result (no-throw contract)."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_before = rt.status().active_slot_id

    def exploding_hook(txn: RuntimeTransaction) -> str | None:
        raise RuntimeError("hook exploded")

    plan = _plan_ready(witness_v1, active_before)
    result = apply_deployment_plan(
        plan,
        registry=_registry(),
        runtime=rt,
        pre_activate=exploding_hook,
    )

    assert result.success is False
    assert result.reason == (
        "pre-activation gate failed: RuntimeError: hook exploded"
    )
    assert rt.status().active_slot_id == active_before


def test_pre_activate_runs_after_validation_before_activation(
    tmp_path: Path, witness_v1: Path,
) -> None:
    """The hook observes the validated transaction: candidate state is
    VALID, expected versions are recorded, and the witness distribution
    is already installed in the candidate."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_before = rt.status().active_slot_id

    observations: dict[str, object] = {}

    def observing_hook(txn: RuntimeTransaction) -> str | None:
        observations["state"] = txn.state
        observations["expected_versions"] = txn.expected_versions
        observations["candidate_slot_id"] = txn.candidate_slot_id
        probe = probe_runtime_distribution(
            str(_slot_python(txn.candidate_path)), "zealfie-witness"
        )
        observations["installed_in_candidate"] = probe.get("installed")
        return None

    plan = _plan_ready(witness_v1, active_before)
    result = apply_deployment_plan(
        plan,
        registry=_registry(),
        runtime=rt,
        pre_activate=observing_hook,
    )

    assert result.success is True, f"deployment failed: {result.reason}"
    assert observations["state"] is CandidateState.VALID
    assert observations["expected_versions"] == {"zewitness": "0.0.1"}
    assert observations["installed_in_candidate"] is True
    assert observations["candidate_slot_id"] == rt.status().active_slot_id


# =============================================================================
# cancel_check hook
# =============================================================================


def test_cancel_check_deployment_cancelled_propagates(
    tmp_path: Path, witness_v1: Path,
) -> None:
    """``DeploymentCancelledError`` from ``cancel_check`` is re-raised:
    an interruption, not a failure — no result object, active pointer
    untouched."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_before = rt.status().active_slot_id
    pointer_before = _active_pointer_text(layout)

    calls: list[int] = []

    def cancel():
        calls.append(1)
        raise DeploymentCancelledError("cancelled by user")

    plan = _plan_ready(witness_v1, active_before)
    with pytest.raises(DeploymentCancelledError):
        apply_deployment_plan(
            plan,
            registry=_registry(),
            runtime=rt,
            cancel_check=cancel,
        )

    assert calls == [1]
    assert _active_pointer_text(layout) == pointer_before
    assert rt.status().active_slot_id == active_before


def test_cancel_check_other_exception_becomes_failure(
    tmp_path: Path, witness_v1: Path,
) -> None:
    """Any other exception from ``cancel_check`` becomes a failure result
    (no-throw contract), active pointer untouched."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_before = rt.status().active_slot_id

    def broken_cancel():
        raise RuntimeError("cancel machinery broken")

    plan = _plan_ready(witness_v1, active_before)
    result = apply_deployment_plan(
        plan,
        registry=_registry(),
        runtime=rt,
        cancel_check=broken_cancel,
    )

    assert result.success is False
    assert result.reason == "cancel check failed: cancel machinery broken"
    assert rt.status().active_slot_id == active_before


def test_cancel_check_fires_at_every_checkpoint(
    tmp_path: Path, witness_v1: Path,
) -> None:
    """With a single-component no-lock plan, ``cancel_check`` fires
    exactly five times: before begin-transaction, before candidate venv
    creation, before the component install, before validation, and
    before activation."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_before = rt.status().active_slot_id

    calls: list[int] = []

    def counting_cancel():
        calls.append(1)

    plan = _plan_ready(witness_v1, active_before)
    result = apply_deployment_plan(
        plan,
        registry=_registry(),
        runtime=rt,
        cancel_check=counting_cancel,
    )

    assert result.success is True, f"deployment failed: {result.reason}"
    assert len(calls) == 5


def test_cancel_check_late_cancellation_leaves_active_untouched(
    tmp_path: Path, witness_v1: Path,
) -> None:
    """Cancellation raised on the LAST checkpoint (before activation)
    still leaves the active pointer untouched — the candidate exists but
    is never activated."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_before = rt.status().active_slot_id
    pointer_before = _active_pointer_text(layout)

    state = {"n": 0}

    def cancel_on_fifth():
        state["n"] += 1
        if state["n"] >= 5:
            raise DeploymentCancelledError("cancelled at the last checkpoint")

    plan = _plan_ready(witness_v1, active_before)
    with pytest.raises(DeploymentCancelledError):
        apply_deployment_plan(
            plan,
            registry=_registry(),
            runtime=rt,
            cancel_check=cancel_on_fifth,
        )

    assert state["n"] == 5
    assert _active_pointer_text(layout) == pointer_before
    assert rt.status().active_slot_id == active_before
