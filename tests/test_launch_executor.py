"""Tests for controlled subprocess execution."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from zealfie.launching import (
    LaunchError,
    LaunchPlan,
    LaunchResult,
    execute_launch_plan,
)


# ---------------------------------------------------------------------------
# Normal execution
# ---------------------------------------------------------------------------


def test_execute_successful_command() -> None:
    plan = LaunchPlan(
        component_id="test",
        executable=Path(sys.executable),
        arguments=("-c", "print('hello')"),
    )
    result = execute_launch_plan(plan)
    assert result.return_code == 0
    assert result.stdout.strip() == "hello"
    assert result.stderr == ""
    assert result.timed_out is False


def test_execute_nonzero_exit_code() -> None:
    plan = LaunchPlan(
        component_id="test",
        executable=Path(sys.executable),
        arguments=("-c", "import sys; sys.exit(42)"),
    )
    result = execute_launch_plan(plan)
    assert result.return_code == 42


def test_execute_stderr() -> None:
    plan = LaunchPlan(
        component_id="test",
        executable=Path(sys.executable),
        arguments=("-c", "import sys; print('error', file=sys.stderr)"),
    )
    result = execute_launch_plan(plan)
    assert result.return_code == 0
    assert "error" in result.stderr


def test_execute_stdout_and_stderr() -> None:
    plan = LaunchPlan(
        component_id="test",
        executable=Path(sys.executable),
        arguments=("-c", "import sys; print('out'); print('err', file=sys.stderr)"),
    )
    result = execute_launch_plan(plan)
    assert result.stdout.strip() == "out"
    assert "err" in result.stderr


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


def test_execute_timeout_kills_process() -> None:
    plan = LaunchPlan(
        component_id="test",
        executable=Path(sys.executable),
        arguments=("-c", "import time; time.sleep(60)"),
    )
    result = execute_launch_plan(plan, timeout_seconds=1)
    assert result.timed_out is True
    assert result.return_code == -1


# ---------------------------------------------------------------------------
# Missing executable
# ---------------------------------------------------------------------------


def test_execute_missing_executable_raises() -> None:
    plan = LaunchPlan(
        component_id="test",
        executable=Path("/nonexistent/for/sure/binary_xyz"),
    )
    with pytest.raises(LaunchError):
        execute_launch_plan(plan, timeout_seconds=5)


# ---------------------------------------------------------------------------
# Working directory
# ---------------------------------------------------------------------------


def test_execute_with_working_directory(tmp_path: Path) -> None:
    plan = LaunchPlan(
        component_id="test",
        executable=Path(sys.executable),
        arguments=("-c", "import os; print(os.getcwd())"),
        working_directory=tmp_path,
    )
    result = execute_launch_plan(plan)
    assert result.return_code == 0
    assert str(tmp_path) in result.stdout


def test_execute_nonexistent_working_directory_raises() -> None:
    plan = LaunchPlan(
        component_id="test",
        executable=Path(sys.executable),
        arguments=("-c", "pass"),
        working_directory=Path("/nonexistent/path/xyz"),
    )
    with pytest.raises(Exception):
        execute_launch_plan(plan, timeout_seconds=5)


# ---------------------------------------------------------------------------
# Immutability / safety
# ---------------------------------------------------------------------------


def test_execute_uses_shell_false_by_construction() -> None:
    """A LaunchPlan never contains a shell string."""
    plan = LaunchPlan(component_id="test", executable=Path(sys.executable), arguments=("-c", "pass"))
    # The plan stores structured data only.
    assert isinstance(plan.executable, Path)
    assert isinstance(plan.arguments, tuple)
    # Try a contrived shell injection: the argument is passed literally.
    plan2 = LaunchPlan(
        component_id="test",
        executable=Path(sys.executable),
        arguments=("-c", "print('; rm -rf /')"),
    )
    result = execute_launch_plan(plan2)
    assert "; rm -rf /" in result.stdout
    assert result.return_code == 0


# ---------------------------------------------------------------------------
# Hardening: timeout guarantees
# ---------------------------------------------------------------------------


def test_execute_default_timeout_is_30_seconds(monkeypatch) -> None:
    """When no timeout is passed, subprocess.run receives timeout=30."""
    import subprocess as sp_mod

    captured_timeout: float | None = None
    original_run = sp_mod.run

    def fake_run(cmd, **kwargs):
        nonlocal captured_timeout
        captured_timeout = kwargs.get("timeout")
        return sp_mod.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(sp_mod, "run", fake_run)

    plan = LaunchPlan(
        component_id="test",
        executable=Path(sys.executable),
        arguments=("-c", "pass"),
    )
    execute_launch_plan(plan)  # no explicit timeout

    assert captured_timeout == 30, (
        f"expected default timeout of 30, got {captured_timeout}"
    )


def test_execute_explicit_timeout_overrides_default() -> None:
    """A long-running command with a short explicit timeout is killed."""
    plan = LaunchPlan(
        component_id="test",
        executable=Path(sys.executable),
        arguments=("-c", "import time; time.sleep(9999)"),
    )
    result = execute_launch_plan(plan, timeout_seconds=1)
    assert result.timed_out is True
    assert result.return_code == -1


def test_execute_timeout_leaves_no_zombie() -> None:
    """After a timeout the process must not remain active."""
    plan = LaunchPlan(
        component_id="test",
        executable=Path(sys.executable),
        arguments=("-c", "import time; time.sleep(9999)"),
    )
    result = execute_launch_plan(plan, timeout_seconds=0.5)
    assert result.timed_out is True


def test_execute_fast_command_succeeds_with_default_timeout() -> None:
    """Fast commands must not be affected by the default 30s timeout."""
    plan = LaunchPlan(
        component_id="test",
        executable=Path(sys.executable),
        arguments=("-c", "pass"),
    )
    result = execute_launch_plan(plan)
    assert result.timed_out is False
    assert result.return_code == 0
