"""Tests for SharedRuntime manager operations — M0-6 slot edition."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.zealfie_slow

from zealfie.building import build_wheel
from zealfie.components.model import ComponentDefinition, EntryPointContract
from zealfie.runtime import (
    InstallOutcome,
    RuntimeLayout,
    RuntimeReasonCode,
    RuntimeState,
    SharedRuntime,
    SharedRuntimeError,
    probe_runtime_distribution,
)

WITNESS_DEF = ComponentDefinition(
    component_id="zewitness",
    display_name="ZeWitness",
    distribution_name="zealfie-witness",
    launch_entry_points=(EntryPointContract("console_scripts", "zewitness"),),
)

WRONG_DIST_DEF = ComponentDefinition(
    component_id="zesolver",
    display_name="ZeSolver",
    distribution_name="ZeSolver",
    launch_entry_points=(EntryPointContract("gui_scripts", "zesolver"),),
)

WRONG_GROUP_DEF = ComponentDefinition(
    component_id="zewitness",
    display_name="ZeWitness",
    distribution_name="zealfie-witness",
    launch_entry_points=(EntryPointContract("gui_scripts", "zewitness"),),
)


@pytest.fixture(scope="session")
def witness_wheel(tmp_path_factory) -> Path:
    d = Path(__file__).resolve().parent / "fixtures" / "witness_component"
    t = tmp_path_factory.mktemp("mgr-wheel")
    return build_wheel(d, output_dir=t)

# -- create --------------------------------------------------------------


def test_create_absent_becomes_ready(tmp_path: Path) -> None:
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    s = rt.create()
    assert s.state == RuntimeState.READY
    assert s.active_slot_id is not None


def test_create_idempotent(tmp_path: Path) -> None:
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    s1 = rt.create()
    s2 = rt.create()
    assert s1.active_slot_id == s2.active_slot_id


def test_create_broken_raises(tmp_path: Path) -> None:
    layout = RuntimeLayout(root=tmp_path / "rt")
    layout.state_dir.mkdir(parents=True)
    layout.active_pointer.write_text("bad json")
    rt = SharedRuntime(layout=layout)
    with pytest.raises(SharedRuntimeError, match="BROKEN"):
        rt.create()


# -- install --------------------------------------------------------------


def test_install_witness(tmp_path: Path, witness_wheel: Path) -> None:
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()
    r = rt.install_local_wheel(witness_wheel, component_definition=WITNESS_DEF)
    assert r.outcome == InstallOutcome.INSTALLED
    assert r.distribution_name == "zealfie-witness"
    assert r.version == "0.0.1"


def test_install_already_installed(tmp_path: Path, witness_wheel: Path) -> None:
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()
    rt.install_local_wheel(witness_wheel)
    r = rt.install_local_wheel(witness_wheel, component_definition=WITNESS_DEF)
    assert r.outcome == InstallOutcome.ALREADY_INSTALLED


def test_preinstall_wrong_distribution(tmp_path: Path, witness_wheel: Path) -> None:
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()
    r = rt.install_local_wheel(witness_wheel, component_definition=WRONG_DIST_DEF)
    assert r.outcome == InstallOutcome.CONTRACT_MISMATCH


def test_preinstall_wrong_group(tmp_path: Path, witness_wheel: Path) -> None:
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()
    r = rt.install_local_wheel(witness_wheel, component_definition=WRONG_GROUP_DEF)
    assert r.outcome == InstallOutcome.CONTRACT_MISMATCH


# -- probe fail-closed ----------------------------------------------------


def test_probe_error_blocks_install(monkeypatch, tmp_path: Path, witness_wheel: Path) -> None:
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()

    def fake_probe(python, dist_name, **kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr("zealfie.runtime.manager.probe_runtime_distribution", fake_probe)
    r = rt.install_local_wheel(witness_wheel)
    assert r.outcome == InstallOutcome.FAILED


# -- VERSION_MISMATCH -----------------------------------------------------


def test_version_mismatch(monkeypatch, tmp_path: Path, witness_wheel: Path) -> None:
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()

    def fake_probe(python, dist_name, **kwargs):
        return {"python_version": "3.13", "installed": True, "version": "9.9.9",
                "entry_points": [{"group": "console_scripts", "name": "zewitness", "value": "..."}]}

    monkeypatch.setattr("zealfie.runtime.manager.probe_runtime_distribution", fake_probe)
    r = rt.install_local_wheel(witness_wheel)
    assert r.outcome == InstallOutcome.VERSION_MISMATCH


# -- normalisation --------------------------------------------------------


def test_normalisation() -> None:
    from zealfie.runtime.manager import _normalise_distribution_name as n
    assert n("zealfie-witness") == "zealfie-witness"
    assert n("ZeAlfie-Witness") == "zealfie-witness"
    assert n("zealfie.witness") == "zealfie-witness"
    assert n("zealfie--witness") == "zealfie-witness"


# -- transaction ----------------------------------------------------------


def test_begin_transaction_generates_slot_id(tmp_path: Path) -> None:
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()
    txn = rt.begin_transaction()
    assert txn.candidate_slot_id.startswith("rt-")
    assert txn.base_active_slot_id == rt.status().active_slot_id


def test_discard_orphan_slot(tmp_path: Path, witness_wheel: Path) -> None:
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()
    txn = rt.begin_transaction()
    import venv
    slot_path = rt.layout.slot_path(txn.candidate_slot_id)
    slot_path.parent.mkdir(parents=True, exist_ok=True)
    venv.create(slot_path, with_pip=True, clear=True)
    rt.install_local_wheel(witness_wheel, slot_id=txn.candidate_slot_id)
    rt.validate_candidate(txn)
    rt.activate(txn)

    # Now there's a previous slot.
    prev = rt.status().previous_slot_id
    assert prev is not None

    # Discard an orphan (not active, not previous).
    txn2 = rt.begin_transaction()
    orphan_path = rt.layout.slot_path(txn2.candidate_slot_id)
    orphan_path.parent.mkdir(parents=True, exist_ok=True)
    orphan_path.mkdir()
    result = rt.discard_slot(txn2.candidate_slot_id)
    assert result.reason_code == RuntimeReasonCode.RUNTIME_READY
    assert not orphan_path.is_dir()


def test_python_returns_path(tmp_path: Path) -> None:
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()
    p = rt.python()
    assert p is not None
    assert p.is_file()


# ===========================================================================
# M0-9 Closure A — ABSENT rollback invariant (manager level)
# ===========================================================================


def test_rollback_on_absent_manager(tmp_path: Path) -> None:
    """SharedRuntime.rollback() on ABSENT returns ABSENT with
    ROLLBACK_TARGET_NOT_FOUND and None active_slot_id."""
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))

    # Pre-condition: runtime is ABSENT.
    s = rt.status()
    assert s.state == RuntimeState.ABSENT

    # Rollback.
    rb = rt.rollback()
    assert rb.state == RuntimeState.ABSENT
    assert rb.active_slot_id is None
    assert rb.reason_code == RuntimeReasonCode.ROLLBACK_TARGET_NOT_FOUND
    assert rb.reason is not None
