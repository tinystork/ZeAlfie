"""M0-6 integration: staged runtime transaction with witness upgrade.

Validates:
1. Create slot A → install witness 0.0.1 → activate A
2. Begin transaction B → install witness 0.0.2 in B
3. Active = A, candidate = B
4. Validate B
5. Activate B → active = B, previous = A
6. Active witness = 0.0.2
7. Rollback → active = A, previous = B
8. Active witness = 0.0.1
9. Candidate failure → active unchanged
10. Stale transaction detection
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zealfie.building import build_wheel
from zealfie.components.model import ComponentDefinition, EntryPointContract
from zealfie.runtime import (
    CandidateState,
    InstallOutcome,
    RuntimeLayout,
    RuntimeReasonCode,
    RuntimeState,
    SharedRuntime,
    probe_runtime_distribution,
)

WITNESS_DEF = ComponentDefinition(
    component_id="zewitness",
    display_name="ZeWitness",
    distribution_name="zealfie-witness",
    launch_entry_points=(EntryPointContract("console_scripts", "zewitness"),),
)


@pytest.fixture(scope="session")
def witness_v1(tmp_path_factory) -> Path:
    d = Path(__file__).resolve().parents[1] / "fixtures" / "witness_component"
    t = tmp_path_factory.mktemp("v1-wheel")
    return build_wheel(d, output_dir=t)


@pytest.fixture(scope="session")
def witness_v2(tmp_path_factory) -> Path:
    d = Path(__file__).resolve().parents[1] / "fixtures" / "witness_component_v2"
    t = tmp_path_factory.mktemp("v2-wheel")
    return build_wheel(d, output_dir=t)


# ---------------------------------------------------------------------------
# Full upgrade + rollback cycle
# ---------------------------------------------------------------------------


def test_full_upgrade_and_rollback(
    tmp_path: Path, witness_v1: Path, witness_v2: Path
) -> None:
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)

    # 1. Create initial runtime with witness v1.
    s = rt.create()
    assert s.state == RuntimeState.READY

    r = rt.install_local_wheel(witness_v1, component_definition=WITNESS_DEF)
    assert r.outcome == InstallOutcome.INSTALLED
    assert r.version == "0.0.1"

    # Verify active witness version.
    active_python = rt.python()
    probe = probe_runtime_distribution(active_python, "zealfie-witness")
    assert probe["version"] == "0.0.1"

    # 2. Begin transaction for v2.
    txn = rt.begin_transaction()
    slot_b = txn.candidate_slot_id
    assert slot_b.startswith("rt-")

    # Create the candidate slot venv.
    import venv
    slot_path = layout.slot_path(slot_b)
    slot_path.parent.mkdir(parents=True, exist_ok=True)
    venv.create(slot_path, with_pip=True, clear=True)

    # 3. Install v2 in candidate.
    r2 = rt.install_local_wheel(
        witness_v2, slot_id=slot_b, component_definition=WITNESS_DEF
    )
    assert r2.outcome == InstallOutcome.INSTALLED
    assert r2.version == "0.0.2"

    # 4. Active still v1.
    probe_a = probe_runtime_distribution(active_python, "zealfie-witness")
    assert probe_a["version"] == "0.0.1"

    # 5. Validate candidate.
    st = rt.validate_candidate(txn, component_definition=WITNESS_DEF)
    assert st.reason_code == RuntimeReasonCode.RUNTIME_READY
    assert txn.state == CandidateState.VALID

    # 6. Activate B.
    act = rt.activate(txn)
    assert act.active_slot_id == slot_b
    assert act.previous_slot_id == s.active_slot_id

    # 7. Active witness now v2.
    st2 = rt.status()
    active_py2 = _python_of(st2)
    probe2 = probe_runtime_distribution(active_py2, "zealfie-witness")
    assert probe2["version"] == "0.0.2"

    # 8. Rollback.
    rb = rt.rollback()
    assert rb.active_slot_id == s.active_slot_id
    assert rb.previous_slot_id == slot_b

    # 9. Active witness back to v1.
    st3 = rt.status()
    probe3 = probe_runtime_distribution(_python_of(st3), "zealfie-witness")
    assert probe3["version"] == "0.0.1"


# ---------------------------------------------------------------------------
# Candidate failure
# ---------------------------------------------------------------------------


def test_bad_candidate_does_not_affect_active(
    tmp_path: Path, witness_v1: Path, witness_v2: Path
) -> None:
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    rt.install_local_wheel(witness_v1, component_definition=WITNESS_DEF)
    active_before = rt.status().active_slot_id

    # Create a candidate but don't install anything.
    txn = rt.begin_transaction()
    import venv
    slot_path = layout.slot_path(txn.candidate_slot_id)
    slot_path.parent.mkdir(parents=True, exist_ok=True)
    venv.create(slot_path, with_pip=True, clear=True)

    # Validation fails — no witness installed.
    st = rt.validate_candidate(txn, component_definition=WITNESS_DEF)
    assert st.state == RuntimeState.BROKEN

    # Activation must refuse.
    act = rt.activate(txn)
    assert act.reason_code == RuntimeReasonCode.CANDIDATE_VALIDATION_FAILED

    # Active must be unchanged.
    assert rt.status().active_slot_id == active_before


# ---------------------------------------------------------------------------
# Stale transaction
# ---------------------------------------------------------------------------


def test_stale_transaction_rejected(tmp_path: Path, witness_v1: Path) -> None:
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    rt.install_local_wheel(witness_v1, component_definition=WITNESS_DEF)

    # Transaction based on current active.
    txn = rt.begin_transaction()
    import venv
    slot_path = layout.slot_path(txn.candidate_slot_id)
    slot_path.parent.mkdir(parents=True, exist_ok=True)
    venv.create(slot_path, with_pip=True, clear=True)
    rt.install_local_wheel(witness_v1, slot_id=txn.candidate_slot_id,
                           component_definition=WITNESS_DEF)
    rt.validate_candidate(txn, component_definition=WITNESS_DEF)

    # Meanwhile, another activation changes the active slot.
    txn2 = rt.begin_transaction()
    slot2 = layout.slot_path(txn2.candidate_slot_id)
    slot2.parent.mkdir(parents=True, exist_ok=True)
    venv.create(slot2, with_pip=True, clear=True)
    rt.install_local_wheel(witness_v1, slot_id=txn2.candidate_slot_id,
                           component_definition=WITNESS_DEF)
    rt.validate_candidate(txn2, component_definition=WITNESS_DEF)
    rt.activate(txn2)

    # Old transaction's activate must be refused.
    result = rt.activate(txn)
    assert result.reason_code == RuntimeReasonCode.STALE_TRANSACTION


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _python_of(st: "RuntimeStatus") -> "Path":
    assert st.active_path is not None
    import sys
    slot = st.active_path
    if sys.platform == "win32":
        return slot / "Scripts" / "python.exe"
    return slot / "bin" / "python"


# ===========================================================================
# M0-9 Closure A — ABSENT rollback invariant
# ===========================================================================


def test_rollback_absent_returns_absent(tmp_path) -> None:
    """Rollback on an ABSENT runtime returns state ABSENT (not READY).

    This preserves the invariant: READY implies a valid active slot.
    """
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)

    # Verify runtime is ABSENT.
    assert rt.status().state == RuntimeState.ABSENT

    # Rollback on ABSENT must return ABSENT.
    result = rt.rollback()
    assert result.state == RuntimeState.ABSENT, (
        f"expected ABSENT, got {result.state.value}"
    )
    assert result.active_slot_id is None
    assert result.reason_code == RuntimeReasonCode.ROLLBACK_TARGET_NOT_FOUND


def test_rollback_absent_no_filesystem_mutation(tmp_path) -> None:
    """Rollback on ABSENT must not create any files or directories."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)

    # Capture initial filesystem state.
    root = tmp_path / "rt"
    assert not root.exists() or list(root.iterdir()) == []

    # Rollback on ABSENT.
    result = rt.rollback()
    assert result.state == RuntimeState.ABSENT

    # Filesystem must be unchanged.
    # The runtime root may not even exist, or if it does (from parent mkdir)
    # it should be empty.
    if root.exists():
        contents = list(root.iterdir())
        assert len(contents) == 0, f"unexpected filesystem changes: {contents}"


def test_rollback_absent_state_unchanged(tmp_path) -> None:
    """Rollback on ABSENT must not change the runtime state to READY.
    After rollback, status() must still report ABSENT."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)

    # Initial status is ABSENT.
    s1 = rt.status()
    assert s1.state == RuntimeState.ABSENT

    # Rollback.
    rt.rollback()

    # Status must still be ABSENT.
    s2 = rt.status()
    assert s2.state == RuntimeState.ABSENT, (
        f"status changed from ABSENT to {s2.state.value} after rollback"
    )
