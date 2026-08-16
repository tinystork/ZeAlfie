"""M0-6A hardening: contract propagation, slot safety, rollback health, BROKEN state."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from zealfie.building import build_wheel
from zealfie.components.model import ComponentDefinition, EntryPointContract
from zealfie.runtime import (
    InstallOutcome,
    OPERATION_RUNTIME_APPLY,
    RuntimeLayout,
    RuntimeMutationLock,
    RuntimeReasonCode,
    RuntimeState,
    SharedRuntime,
    SharedRuntimeError,
    load_active_state,
    save_active_state,
    probe_runtime_distribution,
)


WITNESS_DEF = ComponentDefinition(
    "zewitness", "ZeWitness", "zealfie-witness",
    (EntryPointContract("console_scripts", "zewitness"),),
)
WRONG_DIST_DEF = ComponentDefinition(
    "zesolver", "ZeSolver", "ZeSolver",
    (EntryPointContract("gui_scripts", "zesolver"),),
)
WRONG_GROUP_DEF = ComponentDefinition(
    "zewitness", "ZeWitness", "zealfie-witness",
    (EntryPointContract("gui_scripts", "zewitness"),),
)
WRONG_NAME_DEF = ComponentDefinition(
    "zewitness", "ZeWitness", "zealfie-witness",
    (EntryPointContract("console_scripts", "other"),),
)
MULTI_DEF = ComponentDefinition(
    "zewitness", "ZeWitness", "zealfie-witness",
    (
        EntryPointContract("gui_scripts", "zesolver"),
        EntryPointContract("console_scripts", "zewitness"),
    ),
)



# ===================================================================
# 1. Contract propagation (M0-5B regression)
# ===================================================================


@pytest.mark.zealfie_slow
def test_wrong_distribution_rejected_before_pip(tmp_path, witness_wheel, monkeypatch):
    """Wheel with wrong distribution name -> CONTRACT_MISMATCH, pip not called."""
    import subprocess as sp_mod
    pip_called = False
    orig = sp_mod.run
    def fake(cmd, **kw):
        nonlocal pip_called
        if "pip" in str(cmd):
            pip_called = True
        return orig(cmd, **kw)
    monkeypatch.setattr(sp_mod, "run", fake)

    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()
    pip_called = False

    r = rt.install_local_wheel(witness_wheel, component_definition=WRONG_DIST_DEF)
    assert r.outcome == InstallOutcome.CONTRACT_MISMATCH
    assert not pip_called


@pytest.mark.zealfie_slow
def test_wrong_entry_point_group_rejected(tmp_path, witness_wheel, monkeypatch):
    import subprocess as sp_mod
    pip_called = False
    orig = sp_mod.run
    def fake(cmd, **kw):
        nonlocal pip_called
        if "pip" in str(cmd):
            pip_called = True
        return orig(cmd, **kw)
    monkeypatch.setattr(sp_mod, "run", fake)

    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()
    pip_called = False

    r = rt.install_local_wheel(witness_wheel, component_definition=WRONG_GROUP_DEF)
    assert r.outcome == InstallOutcome.CONTRACT_MISMATCH
    assert not pip_called


@pytest.mark.zealfie_slow
def test_wrong_entry_point_name_rejected(tmp_path, witness_wheel, monkeypatch):
    import subprocess as sp_mod
    pip_called = False
    orig = sp_mod.run
    def fake(cmd, **kw):
        nonlocal pip_called
        if "pip" in str(cmd):
            pip_called = True
        return orig(cmd, **kw)
    monkeypatch.setattr(sp_mod, "run", fake)

    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()
    pip_called = False

    r = rt.install_local_wheel(witness_wheel, component_definition=WRONG_NAME_DEF)
    assert r.outcome == InstallOutcome.CONTRACT_MISMATCH
    assert not pip_called


@pytest.mark.zealfie_slow
def test_multi_contract_at_least_one_match(tmp_path, witness_wheel):
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()
    r = rt.install_local_wheel(witness_wheel, component_definition=MULTI_DEF)
    assert r.outcome == InstallOutcome.INSTALLED


@pytest.mark.zealfie_slow
def test_probe_error_fail_closed(tmp_path, witness_wheel, monkeypatch):
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()

    def fake_probe(python, dist_name, **kwargs):
        raise RuntimeError("crash")
    monkeypatch.setattr("zealfie.runtime.manager.probe_runtime_distribution", fake_probe)
    r = rt.install_local_wheel(witness_wheel)
    assert r.outcome == InstallOutcome.FAILED


@pytest.mark.zealfie_slow
def test_probe_timeout_fail_closed(tmp_path, witness_wheel, monkeypatch):
    import subprocess as sp
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()
    def fake(python, dist_name, **kwargs):
        raise sp.TimeoutExpired(cmd=[], timeout=1)
    monkeypatch.setattr("zealfie.runtime.manager.probe_runtime_distribution", fake)
    r = rt.install_local_wheel(witness_wheel)
    assert r.outcome == InstallOutcome.FAILED


@pytest.mark.zealfie_slow
def test_version_mismatch_reported(tmp_path, witness_wheel, monkeypatch):
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()
    def fake(python, dist_name, **kwargs):
        return {"python_version": "3.13", "installed": True, "version": "9.9.9",
                "entry_points": [{"group": "console_scripts", "name": "zewitness", "value": "..."}]}
    monkeypatch.setattr("zealfie.runtime.manager.probe_runtime_distribution", fake)
    r = rt.install_local_wheel(witness_wheel)
    assert r.outcome == InstallOutcome.VERSION_MISMATCH


# ===================================================================
# 2. Slot path safety
# ===================================================================


def test_slot_path_rejects_empty():
    with pytest.raises(ValueError):
        RuntimeLayout(root=Path("/tmp")).slot_path("")


def test_slot_path_rejects_whitespace():
    with pytest.raises(ValueError):
        RuntimeLayout(root=Path("/tmp")).slot_path("   ")


def test_slot_path_rejects_parent_traversal():
    with pytest.raises(ValueError):
        RuntimeLayout(root=Path("/tmp")).slot_path("../outside")


def test_slot_path_rejects_slash():
    with pytest.raises(ValueError):
        RuntimeLayout(root=Path("/tmp")).slot_path("foo/bar")


def test_slot_path_rejects_backslash():
    with pytest.raises(ValueError):
        RuntimeLayout(root=Path("/tmp")).slot_path("foo\\bar")


def test_slot_path_rejects_absolute():
    with pytest.raises(ValueError):
        RuntimeLayout(root=Path("/tmp")).slot_path("/etc/passwd")


def test_slot_path_rejects_windows_abs():
    with pytest.raises(ValueError):
        RuntimeLayout(root=Path("/tmp")).slot_path("C:\\Windows")


def test_slot_path_accepts_valid():
    layout = RuntimeLayout(root=Path("/tmp/rt"))
    p = layout.slot_path("rt-abc")
    assert p == (Path("/tmp/rt/slots/rt-abc").resolve())


def test_slot_path_symlink_escape(tmp_path):
    layout = RuntimeLayout(root=tmp_path / "rt")
    layout.slots.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (layout.slots / "escape").symlink_to(outside)
    except OSError:
        pytest.skip("symlink not allowed")
    with pytest.raises(ValueError, match="escapes"):
        layout.slot_path("escape")


# ===================================================================
# 3. Rollback target health
# ===================================================================


@pytest.mark.zealfie_slow
def test_rollback_no_previous(tmp_path, witness_wheel):
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()
    rt.install_local_wheel(witness_wheel, component_definition=WITNESS_DEF)
    rb = rt.rollback()
    assert rb.reason_code == RuntimeReasonCode.ROLLBACK_TARGET_NOT_FOUND


@pytest.mark.zealfie_slow
def test_rollback_target_missing(tmp_path, witness_wheel):
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    slot_a = rt.status().active_slot_id
    rt.install_local_wheel(witness_wheel, component_definition=WITNESS_DEF)

    # Activate B → A becomes previous.
    txn = rt.begin_transaction()
    import venv
    slot_b = txn.candidate_slot_id
    layout.slot_path(slot_b).parent.mkdir(parents=True, exist_ok=True)
    venv.create(layout.slot_path(slot_b), with_pip=True, clear=True)
    rt.install_local_wheel(witness_wheel, slot_id=slot_b, component_definition=WITNESS_DEF)
    rt.validate_candidate(txn, component_definition=WITNESS_DEF)
    with RuntimeMutationLock(layout.root).acquire(
        OPERATION_RUNTIME_APPLY
    ):
        rt.activate(txn)

    # Delete previous slot.
    import shutil
    shutil.rmtree(layout.slot_path(slot_a))

    rb = rt.rollback()
    assert rb.reason_code == RuntimeReasonCode.ACTIVE_SLOT_NOT_FOUND
    assert rt.status().active_slot_id == slot_b  # unchanged


@pytest.mark.zealfie_slow
def test_rollback_target_broken(tmp_path, witness_wheel):
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    slot_a = rt.status().active_slot_id
    rt.install_local_wheel(witness_wheel, component_definition=WITNESS_DEF)

    txn = rt.begin_transaction()
    import venv
    slot_b = txn.candidate_slot_id
    layout.slot_path(slot_b).parent.mkdir(parents=True, exist_ok=True)
    venv.create(layout.slot_path(slot_b), with_pip=True, clear=True)
    rt.install_local_wheel(witness_wheel, slot_id=slot_b, component_definition=WITNESS_DEF)
    rt.validate_candidate(txn, component_definition=WITNESS_DEF)
    with RuntimeMutationLock(layout.root).acquire(
        OPERATION_RUNTIME_APPLY
    ):
        rt.activate(txn)

    # Corrupt previous slot's Python.
    a_python = layout.slot_path(slot_a) / "bin" / "python"
    a_python.unlink()

    rb = rt.rollback()
    assert rb.state == RuntimeState.BROKEN
    assert rt.status().active_slot_id == slot_b  # unchanged


# ===================================================================
# 4. BROKEN state protection
# ===================================================================


@pytest.mark.zealfie_slow
def test_broken_state_blocks_activation(tmp_path, witness_wheel):
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    rt.install_local_wheel(witness_wheel, component_definition=WITNESS_DEF)

    txn = rt.begin_transaction()
    import venv
    slot_b = txn.candidate_slot_id
    layout.slot_path(slot_b).parent.mkdir(parents=True, exist_ok=True)
    venv.create(layout.slot_path(slot_b), with_pip=True, clear=True)
    rt.install_local_wheel(witness_wheel, slot_id=slot_b, component_definition=WITNESS_DEF)
    rt.validate_candidate(txn, component_definition=WITNESS_DEF)

    # Corrupt active.json
    layout.active_pointer.write_text("not json")

    with RuntimeMutationLock(layout.root).acquire(
        OPERATION_RUNTIME_APPLY
    ):
        result = rt.activate(txn)
    assert result.state == RuntimeState.BROKEN
    # Pointer must still be corrupted (not overwritten).
    assert layout.active_pointer.read_text() == "not json"


@pytest.mark.zealfie_slow
def test_corrupt_state_during_transaction_rejected(tmp_path, witness_wheel):
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    rt.install_local_wheel(witness_wheel, component_definition=WITNESS_DEF)

    txn = rt.begin_transaction()
    import venv
    slot_b = txn.candidate_slot_id
    layout.slot_path(slot_b).parent.mkdir(parents=True, exist_ok=True)
    venv.create(layout.slot_path(slot_b), with_pip=True, clear=True)
    rt.install_local_wheel(witness_wheel, slot_id=slot_b, component_definition=WITNESS_DEF)
    rt.validate_candidate(txn, component_definition=WITNESS_DEF)

    pointer_before = layout.active_pointer.read_bytes()
    # Corrupt
    layout.active_pointer.write_text("garbage")
    with RuntimeMutationLock(layout.root).acquire(
        OPERATION_RUNTIME_APPLY
    ):
        result = rt.activate(txn)
    assert result.state == RuntimeState.BROKEN
    assert layout.active_pointer.read_text() == "garbage"  # unchanged


# ===================================================================
# 5. Active pointer invariance on failure
# ===================================================================


@pytest.mark.zealfie_slow
def test_pointer_unchanged_on_contract_mismatch(tmp_path, witness_wheel):
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    pointer_before = layout.active_pointer.read_bytes()

    rt.install_local_wheel(witness_wheel, component_definition=WRONG_DIST_DEF)
    assert layout.active_pointer.read_bytes() == pointer_before


@pytest.mark.zealfie_slow
def test_pointer_unchanged_on_discard_active(tmp_path, witness_wheel):
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_id = rt.status().active_slot_id
    pointer_before = layout.active_pointer.read_bytes()

    rt.discard_slot(active_id)
    assert layout.active_pointer.read_bytes() == pointer_before


@pytest.mark.zealfie_slow
def test_pointer_unchanged_on_bad_slot_id(tmp_path):
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    pointer_before = layout.active_pointer.read_bytes()

    with pytest.raises(ValueError):
        layout.slot_path("../escape")
    assert layout.active_pointer.read_bytes() == pointer_before


# ===================================================================
# 6. Integration: failed candidate preserves active
# ===================================================================


@pytest.mark.zealfie_slow
def test_bad_candidate_preserves_active(tmp_path, witness_wheel):
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    rt.install_local_wheel(witness_wheel, component_definition=WITNESS_DEF)
    active_before = rt.status().active_slot_id
    pointer_before = layout.active_pointer.read_bytes()

    txn = rt.begin_transaction()
    import venv
    slot_b = txn.candidate_slot_id
    layout.slot_path(slot_b).parent.mkdir(parents=True, exist_ok=True)
    venv.create(layout.slot_path(slot_b), with_pip=True, clear=True)
    # No witness installed → validation fails.
    st = rt.validate_candidate(txn, component_definition=WITNESS_DEF)
    assert st.state == RuntimeState.BROKEN

    with RuntimeMutationLock(layout.root).acquire(
        OPERATION_RUNTIME_APPLY
    ):
        result = rt.activate(txn)
    assert result.reason_code == RuntimeReasonCode.CANDIDATE_VALIDATION_FAILED
    assert rt.status().active_slot_id == active_before
    assert layout.active_pointer.read_bytes() == pointer_before
