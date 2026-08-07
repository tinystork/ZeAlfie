"""Integration test: persistent shared runtime with witness component.

Validates the complete M0-5 shared runtime cycle:

1. Runtime is ABSENT.
2. Create → READY.
3. Idempotent create → still READY.
4. Build and install witness wheel → INSTALLED.
5. Probe confirms distribution, version, entry points.
6. Install same wheel → ALREADY_INSTALLED.
7. Broken runtime is detected and not auto-repaired.
8. All operations are offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zealfie.building import build_wheel
from zealfie.runtime import (
    InstallOutcome,
    RuntimeLayout,
    RuntimeReasonCode,
    RuntimeState,
    SharedRuntime,
    SharedRuntimeError,
    probe_runtime_distribution,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def witness_wheel(tmp_path_factory) -> Path:
    witness_dir = Path(__file__).resolve().parents[1] / "fixtures" / "witness_component"
    tmp = tmp_path_factory.mktemp("int-v2-witness-wheel")
    return build_wheel(witness_dir, output_dir=tmp)


# ---------------------------------------------------------------------------
# Full cycle
# ---------------------------------------------------------------------------


def test_full_shared_runtime_cycle(tmp_path: Path, witness_wheel: Path) -> None:
    """End-to-end persistent runtime cycle with the witness component."""
    root = tmp_path / "runtime-root"

    # 1. Runtime is ABSENT
    rt = SharedRuntime(layout=RuntimeLayout(root=root))
    s0 = rt.status()
    assert s0.state == RuntimeState.ABSENT
    assert s0.reason_code == RuntimeReasonCode.RUNTIME_NOT_FOUND

    # 2. Create → READY
    s1 = rt.create()
    assert s1.state == RuntimeState.READY
    assert s1.reason_code == RuntimeReasonCode.RUNTIME_READY
    assert s1.python_executable is not None
    assert s1.python_executable.is_file()
    assert s1.python_version is not None

    # 3. Status reflects READY
    s2 = rt.status()
    assert s2.state == RuntimeState.READY

    # 4. Create again → idempotent, still READY
    s3 = rt.create()
    assert s3.state == RuntimeState.READY
    assert s3.python_executable == s2.python_executable  # same interpreter

    # 5. Install witness wheel
    r1 = rt.install_local_wheel(witness_wheel)
    assert r1.outcome == InstallOutcome.INSTALLED
    assert r1.distribution_name == "zealfie-witness"
    assert r1.version == "0.0.1"

    # 6. Probe confirms the installed distribution
    probe = probe_runtime_distribution(s2.python_executable, "zealfie-witness")
    assert probe["installed"] is True
    assert probe["version"] == "0.0.1"
    eps = probe["entry_points"]
    assert any(
        ep["group"] == "console_scripts" and ep["name"] == "zewitness"
        for ep in eps
    ), f"expected console_scripts:zewitness in {eps}"

    # 7. Install same wheel again → ALREADY_INSTALLED
    r2 = rt.install_local_wheel(witness_wheel)
    assert r2.outcome == InstallOutcome.ALREADY_INSTALLED
    assert r2.version == "0.0.1"

    # 8. Create again → still READY, nothing destroyed
    s4 = rt.create()
    assert s4.state == RuntimeState.READY
    assert s4.python_executable == s2.python_executable

    # 9. Probe still sees the witness after idempotent create
    probe2 = probe_runtime_distribution(s4.python_executable, "zealfie-witness")
    assert probe2["installed"] is True
    assert probe2["version"] == "0.0.1"


# ---------------------------------------------------------------------------
# Broken runtime is NOT auto-repaired
# ---------------------------------------------------------------------------


def test_broken_runtime_not_auto_repaired(tmp_path: Path) -> None:
    root = tmp_path / "rt-broken"

    # Create a healthy runtime first.
    rt = SharedRuntime(layout=RuntimeLayout(root=root))
    rt.create()
    python = rt.python()
    assert python is not None
    assert python.is_file()

    # Corrupt it: remove the Python binary.
    import shutil
    shutil.rmtree(python.parent)

    # Status → BROKEN
    s = rt.status()
    assert s.state == RuntimeState.BROKEN

    # create() must raise, not destroy.
    with pytest.raises(SharedRuntimeError, match="BROKEN"):
        rt.create()

    # Current directory must still exist.
    current = root / "current"
    assert current.is_dir()
