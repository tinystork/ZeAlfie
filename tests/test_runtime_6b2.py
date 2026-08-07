"""M0-6B.2: active_slot mandatory, internal mark_valid, writer validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zealfie.building import build_wheel
from zealfie.components.model import ComponentDefinition, EntryPointContract
from zealfie.runtime import (
    RuntimeLayout,
    RuntimeReasonCode,
    RuntimeState,
    SharedRuntime,
    load_active_state,
    save_active_state,
)

WITNESS_DEF = ComponentDefinition(
    "zewitness", "ZeWitness", "zealfie-witness",
    (EntryPointContract("console_scripts", "zewitness"),),
)


@pytest.fixture(scope="session")
def witness_wheel(tmp_path_factory) -> Path:
    d = Path(__file__).resolve().parent / "fixtures" / "witness_component"
    t = tmp_path_factory.mktemp("6b2-wheel")
    return build_wheel(d, output_dir=t)


# ===================================================================
# 1. active_slot mandatory when file exists
# ===================================================================


def test_active_slot_missing_is_broken(tmp_path):
    layout = RuntimeLayout(root=tmp_path / "rt")
    layout.state_dir.mkdir(parents=True)
    layout.active_pointer.write_text(
        json.dumps({"schema_version": 1})
    )
    st = load_active_state(layout.active_pointer, layout_root=layout.root)
    assert st.state == RuntimeState.BROKEN
    assert st.reason_code == RuntimeReasonCode.RUNTIME_STATE_FILE_INVALID


def test_active_slot_null_is_broken(tmp_path):
    layout = RuntimeLayout(root=tmp_path / "rt")
    layout.state_dir.mkdir(parents=True)
    layout.active_pointer.write_text(
        json.dumps({"schema_version": 1, "active_slot": None})
    )
    st = load_active_state(layout.active_pointer, layout_root=layout.root)
    assert st.state == RuntimeState.BROKEN


def test_active_slot_valid_is_readable(tmp_path):
    layout = RuntimeLayout(root=tmp_path / "rt")
    layout.state_dir.mkdir(parents=True)
    layout.active_pointer.write_text(
        json.dumps({"schema_version": 1, "active_slot": "rt-abc"})
    )
    st = load_active_state(layout.active_pointer, layout_root=layout.root)
    assert st.state == RuntimeState.READY
    assert st.active_slot_id == "rt-abc"


def test_transaction_with_null_active_slot_rejected(tmp_path, witness_wheel):
    """Activation must refuse when active.json has active_slot=null."""
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

    # Corrupt state: write null active_slot.
    layout.active_pointer.write_text(
        json.dumps({"schema_version": 1, "active_slot": None})
    )
    corrupted = layout.active_pointer.read_bytes()

    result = rt.activate(txn)
    assert result.state == RuntimeState.BROKEN
    assert layout.active_pointer.read_bytes() == corrupted


# ===================================================================
# 2. Writer validation
# ===================================================================


def test_save_rejects_invalid_active_slot(tmp_path):
    layout = RuntimeLayout(root=tmp_path / "rt")
    with pytest.raises(ValueError):
        save_active_state(layout.active_pointer, "..", None)
    assert not layout.active_pointer.is_file()


def test_save_rejects_invalid_previous_slot(tmp_path):
    layout = RuntimeLayout(root=tmp_path / "rt")
    with pytest.raises(ValueError):
        save_active_state(layout.active_pointer, "rt-ok", "../bad")
    assert not layout.active_pointer.is_file()


def test_save_rejects_active_equals_previous(tmp_path):
    layout = RuntimeLayout(root=tmp_path / "rt")
    with pytest.raises(ValueError):
        save_active_state(layout.active_pointer, "rt-x", "rt-x")


def test_save_valid_active_no_previous(tmp_path):
    layout = RuntimeLayout(root=tmp_path / "rt")
    save_active_state(layout.active_pointer, "rt-a", None)
    assert layout.active_pointer.is_file()
    data = json.loads(layout.active_pointer.read_text())
    assert data["active_slot"] == "rt-a"
    assert data.get("previous_slot") is None


def test_save_valid_active_with_previous(tmp_path):
    layout = RuntimeLayout(root=tmp_path / "rt")
    save_active_state(layout.active_pointer, "rt-b", "rt-a")
    data = json.loads(layout.active_pointer.read_text())
    assert data["active_slot"] == "rt-b"
    assert data["previous_slot"] == "rt-a"


# ===================================================================
# 3. mark_valid not in public API
# ===================================================================


def test_mark_valid_is_internal():
    from zealfie.runtime.transaction import RuntimeTransaction
    assert hasattr(RuntimeTransaction, "_mark_valid")
    assert hasattr(RuntimeTransaction, "_mark_invalid")
    assert not hasattr(RuntimeTransaction, "mark_valid")
    assert not hasattr(RuntimeTransaction, "mark_invalid")
