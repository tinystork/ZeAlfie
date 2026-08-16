"""Tests for runtime status detection (ABSENT / READY / BROKEN) — M0-6 slot edition."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


from zealfie.runtime import (
    RuntimeLayout,OPERATION_RUNTIME_APPLY,
    RuntimeLayout,RuntimeMutationLock,
    RuntimeReasonCode,
    RuntimeState,
    SharedRuntime,
    SharedRuntimeError,
    save_active_state,
)


def test_status_absent_no_pointer(tmp_path: Path) -> None:
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    s = rt.status()
    assert s.state == RuntimeState.ABSENT


@pytest.mark.zealfie_slow
def test_status_ready_after_create(tmp_path: Path) -> None:
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()
    s = rt.status()
    assert s.state == RuntimeState.READY
    assert s.active_slot_id is not None
    assert s.active_slot_id.startswith("rt-")
    assert s.python_executable is not None
    assert s.python_executable.is_file()
    assert s.python_version is not None


@pytest.mark.zealfie_slow
def test_create_idempotent(tmp_path: Path) -> None:
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    s1 = rt.create()
    s2 = rt.create()
    assert s1.active_slot_id == s2.active_slot_id


def test_broken_pointer_invalid_json(tmp_path: Path) -> None:
    layout = RuntimeLayout(root=tmp_path / "rt")
    layout.state_dir.mkdir(parents=True)
    layout.active_pointer.write_text("not json")

    rt = SharedRuntime(layout=layout)
    s = rt.status()
    assert s.state == RuntimeState.BROKEN
    assert s.reason_code == RuntimeReasonCode.RUNTIME_STATE_FILE_INVALID


def test_broken_pointer_active_slot_missing(tmp_path: Path) -> None:
    layout = RuntimeLayout(root=tmp_path / "rt")
    layout.state_dir.mkdir(parents=True)
    save_active_state(layout.active_pointer, "nonexistent-slot", None)

    rt = SharedRuntime(layout=layout)
    s = rt.status()
    assert s.state == RuntimeState.BROKEN
    assert s.reason_code == RuntimeReasonCode.ACTIVE_SLOT_NOT_FOUND


def test_broken_pointer_python_missing(tmp_path: Path) -> None:
    layout = RuntimeLayout(root=tmp_path / "rt")
    slot_path = layout.slot_path("rt-broken")
    slot_path.mkdir(parents=True)
    layout.state_dir.mkdir(parents=True)
    save_active_state(layout.active_pointer, "rt-broken", None)

    rt = SharedRuntime(layout=layout)
    s = rt.status()
    assert s.state == RuntimeState.BROKEN
    assert s.reason_code == RuntimeReasonCode.RUNTIME_PYTHON_NOT_FOUND


def test_create_on_broken_raises(tmp_path: Path) -> None:
    layout = RuntimeLayout(root=tmp_path / "rt")
    layout.state_dir.mkdir(parents=True)
    layout.active_pointer.write_text("not json")
    rt = SharedRuntime(layout=layout)
    with pytest.raises(SharedRuntimeError, match="BROKEN"):
        rt.create()


@pytest.mark.zealfie_slow
def test_rollback_no_target(tmp_path: Path) -> None:
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    rb = rt.rollback()
    assert rb.reason_code == RuntimeReasonCode.ROLLBACK_TARGET_NOT_FOUND


@pytest.mark.zealfie_slow
def test_discard_active_refused(tmp_path: Path) -> None:
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_id = rt.status().active_slot_id
    result = rt.discard_slot(active_id)
    assert result.reason_code == RuntimeReasonCode.SLOT_DISCARD_REFUSED


@pytest.mark.zealfie_slow
def test_discard_previous_refused(tmp_path: Path) -> None:
    """After a rollback, previous slot cannot be discarded."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active_id = rt.status().active_slot_id

    # Create a rollback target.
    txn = rt.begin_transaction()
    import venv
    slot_b = txn.candidate_slot_id
    layout.slot_path(slot_b).parent.mkdir(parents=True, exist_ok=True)
    venv.create(layout.slot_path(slot_b), with_pip=True, clear=True)
    txn._mark_valid()
    with RuntimeMutationLock(layout.root).acquire(
        OPERATION_RUNTIME_APPLY
    ):
        rt.activate(txn)  # B now active, A is previous

    # Discard previous (A) must be refused.
    result = rt.discard_slot(active_id)
    assert result.reason_code == RuntimeReasonCode.SLOT_DISCARD_REFUSED
