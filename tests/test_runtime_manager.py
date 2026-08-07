"""Tests for SharedRuntime manager operations."""

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
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def witness_wheel(tmp_path_factory) -> Path:
    witness_dir = Path(__file__).resolve().parent / "fixtures" / "witness_component"
    tmp = tmp_path_factory.mktemp("mgr-witness-wheel")
    return build_wheel(witness_dir, output_dir=tmp)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_absent_becomes_ready(tmp_path: Path) -> None:
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    status = rt.create()

    assert status.state == RuntimeState.READY
    assert status.python_executable is not None
    assert status.python_executable.is_file()


def test_create_idempotent(tmp_path: Path) -> None:
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))

    s1 = rt.create()
    s2 = rt.create()

    assert s1.state == RuntimeState.READY
    assert s2.state == RuntimeState.READY
    assert s2.python_executable == s1.python_executable


def test_create_broken_raises(tmp_path: Path) -> None:
    root = tmp_path / "rt"
    current = root / "current"
    current.mkdir(parents=True)

    rt = SharedRuntime(layout=RuntimeLayout(root=root))
    assert rt.status().state == RuntimeState.BROKEN

    with pytest.raises(SharedRuntimeError, match="BROKEN"):
        rt.create()


# ---------------------------------------------------------------------------
# install_local_wheel
# ---------------------------------------------------------------------------


def test_install_witness(tmp_path: Path, witness_wheel: Path) -> None:
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()

    result = rt.install_local_wheel(witness_wheel)
    assert result.outcome == InstallOutcome.INSTALLED
    assert result.distribution_name == "zealfie-witness"
    assert result.version == "0.0.1"


def test_install_already_installed_same_version(tmp_path: Path, witness_wheel: Path) -> None:
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()

    r1 = rt.install_local_wheel(witness_wheel)
    assert r1.outcome == InstallOutcome.INSTALLED

    r2 = rt.install_local_wheel(witness_wheel)
    assert r2.outcome == InstallOutcome.ALREADY_INSTALLED
    assert r2.version == "0.0.1"


def test_install_missing_wheel(tmp_path: Path) -> None:
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()

    result = rt.install_local_wheel("/nonexistent/wheel.whl")
    assert result.outcome == InstallOutcome.FAILED
    assert "not found" in (result.detail or "")


def test_install_on_absent_runtime_fails(tmp_path: Path, witness_wheel: Path) -> None:
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "nonexistent"))
    result = rt.install_local_wheel(witness_wheel)
    assert result.outcome == InstallOutcome.FAILED


def test_install_on_broken_runtime_fails(tmp_path: Path, witness_wheel: Path) -> None:
    root = tmp_path / "rt"
    current = root / "current"
    current.mkdir(parents=True)

    rt = SharedRuntime(layout=RuntimeLayout(root=root))
    result = rt.install_local_wheel(witness_wheel)
    assert result.outcome == InstallOutcome.FAILED


def test_python_returns_none_when_not_ready(tmp_path: Path) -> None:
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "nonexistent"))
    assert rt.python() is None


def test_python_returns_path_when_ready(tmp_path: Path) -> None:
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()
    p = rt.python()
    assert p is not None
    assert p.is_file()


# ---------------------------------------------------------------------------
# Hardening: contract validation
# ---------------------------------------------------------------------------

WITNESS_DEF = ComponentDefinition(
    component_id="zewitness",
    display_name="ZeWitness",
    distribution_name="zealfie-witness",
    launch_entry_points=(EntryPointContract("console_scripts", "zewitness"),),
)


def test_install_with_valid_contract_succeeds(tmp_path: Path, witness_wheel: Path) -> None:
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()

    result = rt.install_local_wheel(witness_wheel, component_definition=WITNESS_DEF)
    assert result.outcome == InstallOutcome.INSTALLED


def test_already_installed_with_valid_contract(tmp_path: Path, witness_wheel: Path) -> None:
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()
    rt.install_local_wheel(witness_wheel)

    r2 = rt.install_local_wheel(witness_wheel, component_definition=WITNESS_DEF)
    assert r2.outcome == InstallOutcome.ALREADY_INSTALLED


def test_already_installed_but_wrong_contract_is_mismatch(tmp_path: Path, witness_wheel: Path) -> None:
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()
    rt.install_local_wheel(witness_wheel)

    wrong_def = ComponentDefinition(
        component_id="zewitness",
        display_name="ZeWitness",
        distribution_name="zealfie-witness",
        launch_entry_points=(EntryPointContract("gui_scripts", "zesolver"),),
    )
    r2 = rt.install_local_wheel(witness_wheel, component_definition=wrong_def)
    assert r2.outcome == InstallOutcome.CONTRACT_MISMATCH


def test_post_install_missing_contract_is_reported(tmp_path: Path, witness_wheel: Path) -> None:
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()

    wrong_def = ComponentDefinition(
        component_id="zewitness",
        display_name="ZeWitness",
        distribution_name="zealfie-witness",
        launch_entry_points=(EntryPointContract("gui_scripts", "zesolver"),),
    )
    result = rt.install_local_wheel(witness_wheel, component_definition=wrong_def)
    # The wheel IS installed, but the contract does not match.
    assert result.outcome == InstallOutcome.CONTRACT_MISMATCH


# ---------------------------------------------------------------------------
# Hardening: probe fail-closed
# ---------------------------------------------------------------------------


def test_probe_error_blocks_install(monkeypatch, tmp_path: Path, witness_wheel: Path) -> None:
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()

    def fake_probe(python, dist_name, **kwargs):
        raise RuntimeError("simulated probe crash")

    monkeypatch.setattr(
        "zealfie.runtime.manager.probe_runtime_distribution", fake_probe
    )

    result = rt.install_local_wheel(witness_wheel)
    assert result.outcome == InstallOutcome.FAILED
    assert "probe failed" in (result.detail or "")


def test_probe_timeout_blocks_install(monkeypatch, tmp_path: Path, witness_wheel: Path) -> None:
    import subprocess

    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()

    def fake_probe(python, dist_name, **kwargs):
        raise subprocess.TimeoutExpired(cmd=[], timeout=1)

    monkeypatch.setattr(
        "zealfie.runtime.manager.probe_runtime_distribution", fake_probe
    )

    result = rt.install_local_wheel(witness_wheel)
    assert result.outcome == InstallOutcome.FAILED


# ---------------------------------------------------------------------------
# Hardening: VERSION_MISMATCH
# ---------------------------------------------------------------------------


def test_version_mismatch_detected(monkeypatch, tmp_path: Path, witness_wheel: Path) -> None:
    """When the runtime has a different version, install is blocked."""
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()

    def fake_probe(python, dist_name, **kwargs):
        return {
            "python_version": "3.13",
            "installed": True,
            "version": "9.9.9",
            "entry_points": [
                {"group": "console_scripts", "name": "zewitness", "value": "zewitness.__main__:main"}
            ],
        }

    monkeypatch.setattr(
        "zealfie.runtime.manager.probe_runtime_distribution", fake_probe
    )

    result = rt.install_local_wheel(witness_wheel)
    assert result.outcome == InstallOutcome.VERSION_MISMATCH
    assert result.version == "9.9.9"


# ---------------------------------------------------------------------------
# M0-5B: Pre-install contract validation
# ---------------------------------------------------------------------------

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

WRONG_NAME_DEF = ComponentDefinition(
    component_id="zewitness",
    display_name="ZeWitness",
    distribution_name="zealfie-witness",
    launch_entry_points=(EntryPointContract("console_scripts", "other-name"),),
)

MULTI_CONTRACT_DEF = ComponentDefinition(
    component_id="zewitness",
    display_name="ZeWitness",
    distribution_name="zealfie-witness",
    launch_entry_points=(
        EntryPointContract("gui_scripts", "zesolver"),
        EntryPointContract("console_scripts", "zewitness"),
    ),
)


def test_preinstall_wrong_distribution_name(tmp_path: Path, witness_wheel: Path) -> None:
    """A wheel whose dist name doesn't match the definition is rejected before pip."""
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()

    result = rt.install_local_wheel(witness_wheel, component_definition=WRONG_DIST_DEF)
    assert result.outcome == InstallOutcome.CONTRACT_MISMATCH
    assert "zealfie-witness" in (result.detail or "")
    assert "zesolver" in (result.detail or "")


def test_preinstall_wrong_entry_point_group(tmp_path: Path, witness_wheel: Path) -> None:
    """Wheel with correct dist name but wrong group is rejected before pip."""
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()

    result = rt.install_local_wheel(witness_wheel, component_definition=WRONG_GROUP_DEF)
    assert result.outcome == InstallOutcome.CONTRACT_MISMATCH
    assert "launch contract" in (result.detail or "").lower()


def test_preinstall_wrong_entry_point_name(tmp_path: Path, witness_wheel: Path) -> None:
    """Wheel with correct dist name but wrong entry point name is rejected."""
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()

    result = rt.install_local_wheel(witness_wheel, component_definition=WRONG_NAME_DEF)
    assert result.outcome == InstallOutcome.CONTRACT_MISMATCH


def test_preinstall_multi_contract_at_least_one_matches(tmp_path: Path, witness_wheel: Path) -> None:
    """When multiple contracts are defined, matching at least one is sufficient."""
    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()

    result = rt.install_local_wheel(witness_wheel, component_definition=MULTI_CONTRACT_DEF)
    assert result.outcome == InstallOutcome.INSTALLED


def test_preinstall_wrong_dist_does_not_call_pip(
    monkeypatch, tmp_path: Path, witness_wheel: Path
) -> None:
    """Statistically incompatible wheels never reach pip."""
    import subprocess as sp_mod

    pip_called = False
    original_run = sp_mod.run

    def fake_run(cmd, **kwargs):
        nonlocal pip_called
        if "pip" in str(cmd):
            pip_called = True
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(sp_mod, "run", fake_run)

    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()

    # Reset — venv creation also runs pip internally.
    pip_called = False

    result = rt.install_local_wheel(witness_wheel, component_definition=WRONG_DIST_DEF)
    assert result.outcome == InstallOutcome.CONTRACT_MISMATCH
    assert not pip_called, "pip must not be called for wrong distribution"


def test_preinstall_wrong_group_does_not_call_pip(
    monkeypatch, tmp_path: Path, witness_wheel: Path
) -> None:
    """Wrong entry point group also never reaches pip."""
    import subprocess as sp_mod

    pip_called = False
    original_run = sp_mod.run

    def fake_run(cmd, **kwargs):
        nonlocal pip_called
        if "pip" in str(cmd):
            pip_called = True
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(sp_mod, "run", fake_run)

    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()
    pip_called = False

    result = rt.install_local_wheel(witness_wheel, component_definition=WRONG_GROUP_DEF)
    assert result.outcome == InstallOutcome.CONTRACT_MISMATCH
    assert not pip_called, "pip must not be called for wrong group"


def test_preinstall_wrong_name_does_not_call_pip(
    monkeypatch, tmp_path: Path, witness_wheel: Path
) -> None:
    """Wrong entry point name also never reaches pip."""
    import subprocess as sp_mod

    pip_called = False
    original_run = sp_mod.run

    def fake_run(cmd, **kwargs):
        nonlocal pip_called
        if "pip" in str(cmd):
            pip_called = True
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(sp_mod, "run", fake_run)

    rt = SharedRuntime(layout=RuntimeLayout(root=tmp_path / "rt"))
    rt.create()
    pip_called = False

    result = rt.install_local_wheel(witness_wheel, component_definition=WRONG_NAME_DEF)
    assert result.outcome == InstallOutcome.CONTRACT_MISMATCH
    assert not pip_called, "pip must not be called for wrong name"


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def test_distribution_name_normalisation() -> None:
    from zealfie.runtime.manager import _normalise_distribution_name

    assert _normalise_distribution_name("zealfie-witness") == "zealfie-witness"
    assert _normalise_distribution_name("ZeAlfie-Witness") == "zealfie-witness"
    assert _normalise_distribution_name("zealfie_witness") == "zealfie-witness"
    assert _normalise_distribution_name("zealfie.witness") == "zealfie-witness"
    assert _normalise_distribution_name("zealfie--witness") == "zealfie-witness"
    assert _normalise_distribution_name("ZeAlfie-._-Witness") == "zealfie-witness"
