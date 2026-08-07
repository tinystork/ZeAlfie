"""M0-6 integration: witness cycle using slot architecture."""

from __future__ import annotations

from pathlib import Path

import pytest

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


@pytest.fixture(scope="session")
def witness_wheel(tmp_path_factory) -> Path:
    d = Path(__file__).resolve().parents[1] / "fixtures" / "witness_component"
    t = tmp_path_factory.mktemp("wcycle-wheel")
    return build_wheel(d, output_dir=t)


def test_full_slot_cycle(tmp_path: Path, witness_wheel: Path) -> None:
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)

    # ABSENT
    assert rt.status().state == RuntimeState.ABSENT

    # Create → READY
    s = rt.create()
    assert s.state == RuntimeState.READY
    assert s.active_slot_id is not None

    # Install witness
    r = rt.install_local_wheel(witness_wheel, component_definition=WITNESS_DEF)
    assert r.outcome == InstallOutcome.INSTALLED

    # Probe
    probe = probe_runtime_distribution(rt.python(), "zealfie-witness")
    assert probe["version"] == "0.0.1"
    assert any(ep["group"] == "console_scripts" and ep["name"] == "zewitness"
               for ep in probe["entry_points"])

    # ALREADY_INSTALLED
    r2 = rt.install_local_wheel(witness_wheel, component_definition=WITNESS_DEF)
    assert r2.outcome == InstallOutcome.ALREADY_INSTALLED

    # Create again → idempotent
    s2 = rt.create()
    assert s2.active_slot_id == s.active_slot_id
    assert s2.python_executable == s.python_executable


def test_broken_runtime_not_auto_repaired(tmp_path: Path, witness_wheel: Path) -> None:
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()

    # Corrupt: delete the active slot.
    active_path = rt.status().active_path
    import shutil
    shutil.rmtree(active_path)

    # Must be BROKEN.
    assert rt.status().state == RuntimeState.BROKEN

    # create() must raise.
    with pytest.raises(SharedRuntimeError, match="BROKEN"):
        rt.create()
