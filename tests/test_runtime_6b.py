"""M0-6B: TOCTOU revalidation, canonical slot validation, BROKEN discard safety."""

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
    RuntimeMutationLock,
    RuntimeLayout,
    RuntimeReasonCode,
    RuntimeState,
    SharedRuntime,
    validate_slot_id,
    load_active_state,
)

WITNESS_DEF = ComponentDefinition(
    "zewitness", "ZeWitness", "zealfie-witness",
    (EntryPointContract("console_scripts", "zewitness"),),
)


@pytest.fixture(scope="session")
def witness_wheel(tmp_path_factory) -> Path:
    d = Path(__file__).resolve().parent / "fixtures" / "witness_component"
    t = tmp_path_factory.mktemp("6b-wheel")
    return build_wheel(d, output_dir=t)

# ===================================================================
# 1. TOCTOU: revalidate at activation time
# ===================================================================


@pytest.mark.zealfie_slow
def test_candidate_python_removed_after_validation_blocks_activation(
    tmp_path, witness_wheel
):
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

    # Corrupt candidate: remove Python binary.
    python = layout.slot_path(slot_b) / "bin" / "python"
    python.unlink()

    pointer_before = layout.active_pointer.read_bytes()
    with RuntimeMutationLock(layout.root).acquire(
        OPERATION_RUNTIME_APPLY
    ):
        result = rt.activate(txn)
    assert result.state == RuntimeState.BROKEN
    assert layout.active_pointer.read_bytes() == pointer_before


@pytest.mark.zealfie_slow
def test_candidate_component_removed_after_validation_blocks_activation(
    tmp_path, witness_wheel
):
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

    # Corrupt: remove the installed witness dist-info.
    import shutil
    dist_dirs = list(layout.slot_path(slot_b).glob("lib/python*/site-packages/zealfie_witness*"))
    for d in dist_dirs:
        if d.is_dir():
            shutil.rmtree(d)

    pointer_before = layout.active_pointer.read_bytes()
    with RuntimeMutationLock(layout.root).acquire(
        OPERATION_RUNTIME_APPLY
    ):
        result = rt.activate(txn)
    assert result.state == RuntimeState.BROKEN
    assert layout.active_pointer.read_bytes() == pointer_before


@pytest.mark.zealfie_slow
def test_create_uses_validation_path(tmp_path):
    """create() must use the same prepare → validate → activate flow."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    s = rt.create()
    assert s.state == RuntimeState.READY
    assert s.active_slot_id is not None
    assert s.python_executable is not None
    assert layout.active_pointer.is_file()


# ===================================================================
# 2. Canonical slot validation in state + layout
# ===================================================================


def test_active_slot_dotdot_in_state_is_broken(tmp_path):
    layout = RuntimeLayout(root=tmp_path / "rt")
    layout.state_dir.mkdir(parents=True)
    layout.active_pointer.write_text(
        json.dumps({"schema_version": 1, "active_slot": "..", "previous_slot": None})
    )
    st = load_active_state(layout.active_pointer, layout_root=layout.root)
    assert st.state == RuntimeState.BROKEN
    assert st.reason_code == RuntimeReasonCode.RUNTIME_STATE_FILE_INVALID


def test_active_slot_slash_in_state_is_broken(tmp_path):
    layout = RuntimeLayout(root=tmp_path / "rt")
    layout.state_dir.mkdir(parents=True)
    layout.active_pointer.write_text(
        json.dumps({"schema_version": 1, "active_slot": "A/B", "previous_slot": None})
    )
    st = load_active_state(layout.active_pointer, layout_root=layout.root)
    assert st.state == RuntimeState.BROKEN


def test_active_slot_empty_in_state_is_broken(tmp_path):
    layout = RuntimeLayout(root=tmp_path / "rt")
    layout.state_dir.mkdir(parents=True)
    layout.active_pointer.write_text(
        json.dumps({"schema_version": 1, "active_slot": "", "previous_slot": None})
    )
    st = load_active_state(layout.active_pointer, layout_root=layout.root)
    assert st.state == RuntimeState.BROKEN


def test_active_slot_dot_in_state_is_broken(tmp_path):
    layout = RuntimeLayout(root=tmp_path / "rt")
    layout.state_dir.mkdir(parents=True)
    layout.active_pointer.write_text(
        json.dumps({"schema_version": 1, "active_slot": ".", "previous_slot": None})
    )
    st = load_active_state(layout.active_pointer, layout_root=layout.root)
    assert st.state == RuntimeState.BROKEN


def test_previous_slot_invalid_makes_state_broken(tmp_path):
    layout = RuntimeLayout(root=tmp_path / "rt")
    layout.state_dir.mkdir(parents=True)
    layout.active_pointer.write_text(
        json.dumps({"schema_version": 1, "active_slot": "rt-ok", "previous_slot": ".."}),
    )
    st = load_active_state(layout.active_pointer, layout_root=layout.root)
    assert st.state == RuntimeState.BROKEN


def test_active_equals_previous_is_broken(tmp_path):
    layout = RuntimeLayout(root=tmp_path / "rt")
    layout.state_dir.mkdir(parents=True)
    layout.active_pointer.write_text(
        json.dumps({"schema_version": 1, "active_slot": "rt-x", "previous_slot": "rt-x"}),
    )
    st = load_active_state(layout.active_pointer, layout_root=layout.root)
    assert st.state == RuntimeState.BROKEN


def test_validate_slot_id_rejects_all_invalid():
    for bad in ("", " ", ".", "..", "../A", "A/B", "A\\B", "/tmp/A", "C:\\A"):
        with pytest.raises(ValueError):
            validate_slot_id(bad)


def test_validate_slot_id_accepts_valid():
    validate_slot_id("rt-abc123")


# ===================================================================
# 3. BROKEN state forbids discard
# ===================================================================


@pytest.mark.zealfie_slow
def test_broken_state_forbids_discard(tmp_path, witness_wheel):
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    slot_a = rt.status().active_slot_id

    # Corrupt state.
    layout.active_pointer.write_text("not json")

    result = rt.discard_slot(slot_a)
    assert result.state == RuntimeState.BROKEN
    assert result.reason_code == RuntimeReasonCode.SLOT_DISCARD_REFUSED
    assert layout.slot_path(slot_a).is_dir()


@pytest.mark.zealfie_slow
def test_broken_state_forbids_discard_orphan(tmp_path):
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()

    # Create an orphan slot.
    orphan = layout.slot_path("rt-orphan")
    orphan.mkdir(parents=True)

    # Corrupt state.
    layout.active_pointer.write_text("garbage")

    result = rt.discard_slot("rt-orphan")
    assert result.state == RuntimeState.BROKEN
    assert result.reason_code == RuntimeReasonCode.SLOT_DISCARD_REFUSED


# ===================================================================
# 4. Pointer invariance on all failure paths
# ===================================================================


@pytest.mark.zealfie_slow
def test_pointer_unchanged_on_toctou_python_removed(tmp_path, witness_wheel):
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

    (layout.slot_path(slot_b) / "bin" / "python").unlink()
    pointer_before = layout.active_pointer.read_bytes()
    with RuntimeMutationLock(layout.root).acquire(
        OPERATION_RUNTIME_APPLY
    ):
        rt.activate(txn)
    assert layout.active_pointer.read_bytes() == pointer_before
