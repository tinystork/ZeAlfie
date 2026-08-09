"""M0-6B.1: non-object JSON state, rollback fail-closed, unified create validation."""

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
    RuntimeStatus,
    SharedRuntime,
    load_active_state,
)

WITNESS_DEF = ComponentDefinition(
    "zewitness", "ZeWitness", "zealfie-witness",
    (EntryPointContract("console_scripts", "zewitness"),),
)


@pytest.fixture(scope="session")
def witness_wheel(tmp_path_factory) -> Path:
    d = Path(__file__).resolve().parent / "fixtures" / "witness_component"
    t = tmp_path_factory.mktemp("6b1-wheel")
    return build_wheel(d, output_dir=t)

# ===================================================================
# 1. Non-object JSON roots → BROKEN
# ===================================================================


def test_state_json_array_root_is_broken(tmp_path):
    layout = RuntimeLayout(root=tmp_path / "rt")
    layout.state_dir.mkdir(parents=True)
    layout.active_pointer.write_text("[]")
    st = load_active_state(layout.active_pointer, layout_root=layout.root)
    assert st.state == RuntimeState.BROKEN
    assert st.reason_code == RuntimeReasonCode.RUNTIME_STATE_FILE_INVALID
    assert "object" in (st.reason or "")


def test_state_json_null_root_is_broken(tmp_path):
    layout = RuntimeLayout(root=tmp_path / "rt")
    layout.state_dir.mkdir(parents=True)
    layout.active_pointer.write_text("null")
    st = load_active_state(layout.active_pointer, layout_root=layout.root)
    assert st.state == RuntimeState.BROKEN


def test_state_json_string_root_is_broken(tmp_path):
    layout = RuntimeLayout(root=tmp_path / "rt")
    layout.state_dir.mkdir(parents=True)
    layout.active_pointer.write_text('"x"')
    st = load_active_state(layout.active_pointer, layout_root=layout.root)
    assert st.state == RuntimeState.BROKEN


def test_state_json_number_root_is_broken(tmp_path):
    layout = RuntimeLayout(root=tmp_path / "rt")
    layout.state_dir.mkdir(parents=True)
    layout.active_pointer.write_text("123")
    st = load_active_state(layout.active_pointer, layout_root=layout.root)
    assert st.state == RuntimeState.BROKEN


def test_state_json_bool_root_is_broken(tmp_path):
    layout = RuntimeLayout(root=tmp_path / "rt")
    layout.state_dir.mkdir(parents=True)
    layout.active_pointer.write_text("true")
    st = load_active_state(layout.active_pointer, layout_root=layout.root)
    assert st.state == RuntimeState.BROKEN


# ===================================================================
# 2. Rollback fail-closed on BROKEN
# ===================================================================


@pytest.mark.zealfie_slow
def test_rollback_on_corrupted_state_is_broken(tmp_path, witness_wheel):
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    rt.install_local_wheel(witness_wheel, component_definition=WITNESS_DEF)

    pointer_before = layout.active_pointer.read_bytes()
    # Corrupt state.
    layout.active_pointer.write_text('{"schema_version":1,"active_slot":["bad"]}')
    corrupted_bytes = layout.active_pointer.read_bytes()

    rb = rt.rollback()
    assert rb.state == RuntimeState.BROKEN
    # Pointer must remain as-is (corrupted, not further changed).
    assert layout.active_pointer.read_bytes() == corrupted_bytes


def test_rollback_on_absent_state_is_absent_not_broken(tmp_path):
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rb = rt.rollback()
    # No previous slot → ROLLBACK_TARGET_NOT_FOUND.
    assert rb.reason_code == RuntimeReasonCode.ROLLBACK_TARGET_NOT_FOUND


# ===================================================================
# 3. create() uses validation path
# ===================================================================


@pytest.mark.zealfie_slow
def test_create_passes_validation(tmp_path):
    """create() must succeed via the validate_candidate path."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    s = rt.create()
    assert s.state == RuntimeState.READY
    assert s.active_slot_id is not None
    assert s.python_executable is not None
    assert layout.active_pointer.is_file()


@pytest.mark.zealfie_slow
def test_create_validation_failure_no_active_pointer(tmp_path, monkeypatch):
    """If validation fails during create(), no active pointer is written."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)

    # Force validate_candidate to fail.
    original = rt.validate_candidate

    def fake_validate(txn, **kw):
        return RuntimeStatus(
            state=RuntimeState.BROKEN,
            runtime_root=layout.root,
            reason_code=RuntimeReasonCode.CANDIDATE_VALIDATION_FAILED,
            reason="simulated validation failure",
        )

    monkeypatch.setattr(rt, "validate_candidate", fake_validate)

    s = rt.create()
    assert s.state == RuntimeState.BROKEN
    assert not layout.active_pointer.is_file()


# ===================================================================
# 4. All existing slot validation preserved
# ===================================================================


def test_active_slot_dotdot_still_broken(tmp_path):
    layout = RuntimeLayout(root=tmp_path / "rt")
    layout.state_dir.mkdir(parents=True)
    layout.active_pointer.write_text(
        json.dumps({"schema_version": 1, "active_slot": ".."})
    )
    st = load_active_state(layout.active_pointer, layout_root=layout.root)
    assert st.state == RuntimeState.BROKEN


def test_active_equals_previous_still_broken(tmp_path):
    layout = RuntimeLayout(root=tmp_path / "rt")
    layout.state_dir.mkdir(parents=True)
    layout.active_pointer.write_text(
        json.dumps({"schema_version": 1, "active_slot": "rt-x", "previous_slot": "rt-x"})
    )
    st = load_active_state(layout.active_pointer, layout_root=layout.root)
    assert st.state == RuntimeState.BROKEN
