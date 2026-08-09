"""Shared session-scoped fixtures for ZeAlfie tests.

Witness component wheels are built once per session and shared across
all test modules.  Tests that need to mutate/copy artifacts MUST copy
them into tmp_path first and MUST NOT mutate the shared source wheels.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zealfie.building import build_wheel

# ---------------------------------------------------------------------------
# Shared witness wheels — built once per session.
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def witness_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build zealfie-witness v0.0.1 wheel once per session."""
    d = Path(__file__).resolve().parent / "fixtures" / "witness_component"
    t = tmp_path_factory.mktemp("shared-witness")
    return build_wheel(d, output_dir=t)


@pytest.fixture(scope="session")
def witness_v2_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build zealfie-witness v0.0.2 wheel once per session."""
    d = Path(__file__).resolve().parent / "fixtures" / "witness_component_v2"
    t = tmp_path_factory.mktemp("shared-witness-v2")
    return build_wheel(d, output_dir=t)


@pytest.fixture(scope="session")
def witness_second_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build zealfie-witness2 wheel once per session."""
    d = Path(__file__).resolve().parent / "fixtures" / "witness_second"
    t = tmp_path_factory.mktemp("shared-witness-second")
    return build_wheel(d, output_dir=t)


# ---------------------------------------------------------------------------
# Aliases so existing test parameter names continue to work.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def witness_v1(witness_wheel: Path) -> Path:
    """Alias: witness_component v0.0.1 (same as witness_wheel)."""
    return witness_wheel


@pytest.fixture(scope="session")
def witness_v2(witness_v2_wheel: Path) -> Path:
    """Alias: witness_component_v2 v0.0.2 (same as witness_v2_wheel)."""
    return witness_v2_wheel


@pytest.fixture(scope="session")
def witness_second(witness_second_wheel: Path) -> Path:
    """Alias: witness_second (same as witness_second_wheel)."""
    return witness_second_wheel


@pytest.fixture(scope="session")
def witness2_wheel(witness_second_wheel: Path) -> Path:
    """Alias: witness_second used in test_runtime_service.py."""
    return witness_second_wheel


# ---------------------------------------------------------------------------
# CLI-specific aliases (replaces duplicate session-scoped build fixtures in
# test_cli.py).  Same wheels, different names to avoid parameter renames.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def witness_wheel_cli(witness_wheel: Path) -> Path:
    """Alias: shared witness_wheel for CLI tests."""
    return witness_wheel


@pytest.fixture(scope="session")
def witness_v2_wheel_cli(witness_v2_wheel: Path) -> Path:
    """Alias: shared witness_v2_wheel for CLI tests."""
    return witness_v2_wheel


@pytest.fixture(scope="session")
def witness2_wheel_cli(witness_second_wheel: Path) -> Path:
    """Alias: shared witness_second_wheel for CLI tests."""
    return witness_second_wheel
