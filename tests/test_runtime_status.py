"""Tests for runtime status detection (ABSENT / READY / BROKEN)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from zealfie.runtime import (
    RuntimeLayout,
    RuntimeReasonCode,
    RuntimeState,
    RuntimeStatus,
    SharedRuntime,
    SharedRuntimeError,
)


# ---------------------------------------------------------------------------
# ABSENT
# ---------------------------------------------------------------------------


def test_status_absent_on_nonexistent_root(tmp_path: Path) -> None:
    root = tmp_path / "nonexistent"
    rt = SharedRuntime(layout=RuntimeLayout(root=root))
    status = rt.status()

    assert status.state == RuntimeState.ABSENT
    assert status.reason_code == RuntimeReasonCode.RUNTIME_NOT_FOUND
    assert status.python_executable is None
    assert "does not exist" in (status.reason or "")


def test_status_absent_on_empty_parent(tmp_path: Path) -> None:
    """Status is ABSENT even if the parent directory exists."""
    root = tmp_path / "rt-empty"
    root.mkdir()
    rt = SharedRuntime(layout=RuntimeLayout(root=root))
    status = rt.status()

    assert status.state == RuntimeState.ABSENT


# ---------------------------------------------------------------------------
# BROKEN — Python missing
# ---------------------------------------------------------------------------


def test_status_broken_python_missing(tmp_path: Path) -> None:
    """If the current directory exists but has no Python, status is BROKEN."""
    root = tmp_path / "rt-broken"
    current = root / "current"
    current.mkdir(parents=True)

    rt = SharedRuntime(layout=RuntimeLayout(root=root))
    status = rt.status()

    assert status.state == RuntimeState.BROKEN
    assert status.reason_code == RuntimeReasonCode.RUNTIME_PYTHON_NOT_FOUND
    assert status.python_executable is None


def test_status_broken_python_not_executable(tmp_path: Path) -> None:
    """A directory in place of Python also yields BROKEN."""
    root = tmp_path / "rt-broken2"
    current = root / "current"
    fake_python_dir = current / "bin" / "python"
    fake_python_dir.mkdir(parents=True)

    rt = SharedRuntime(layout=RuntimeLayout(root=root))
    status = rt.status()

    assert status.state == RuntimeState.BROKEN
    assert status.reason_code == RuntimeReasonCode.RUNTIME_PYTHON_NOT_FOUND


def test_status_broken_python_unusable(tmp_path: Path) -> None:
    """If Python exists but crashes, status is BROKEN."""
    root = tmp_path / "rt-broken3"
    current = root / "current"
    current.mkdir(parents=True)
    if sys.platform == "win32":
        python = current / "Scripts" / "python.exe"
    else:
        python = current / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 1\n")
    python.chmod(0o755)

    rt = SharedRuntime(layout=RuntimeLayout(root=root))
    status = rt.status()

    assert status.state == RuntimeState.BROKEN
    assert status.reason_code == RuntimeReasonCode.RUNTIME_PYTHON_UNUSABLE


# ---------------------------------------------------------------------------
# READY
# ---------------------------------------------------------------------------


def test_status_ready_after_create(tmp_path: Path) -> None:
    root = tmp_path / "rt"
    rt = SharedRuntime(layout=RuntimeLayout(root=root))

    rt.create()
    status = rt.status()

    assert status.state == RuntimeState.READY
    assert status.reason_code == RuntimeReasonCode.RUNTIME_READY
    assert status.python_executable is not None
    assert status.python_executable.is_file()
    assert status.python_version is not None
    assert status.reason is None


# ---------------------------------------------------------------------------
# Idempotent status / create
# ---------------------------------------------------------------------------


def test_status_ready_after_second_create(tmp_path: Path) -> None:
    root = tmp_path / "rt"
    rt = SharedRuntime(layout=RuntimeLayout(root=root))

    rt.create()
    s1 = rt.status()
    assert s1.state == RuntimeState.READY

    rt.create()
    s2 = rt.status()
    assert s2.state == RuntimeState.READY


def test_create_on_broken_raises(tmp_path: Path) -> None:
    """create() on a BROKEN runtime must raise, not destroy."""
    root = tmp_path / "rt"
    current = root / "current"
    current.mkdir(parents=True)
    # No Python inside → BROKEN.

    rt = SharedRuntime(layout=RuntimeLayout(root=root))
    assert rt.status().state == RuntimeState.BROKEN

    with pytest.raises(SharedRuntimeError, match="BROKEN"):
        rt.create()

    # Runtime directory must still exist (not destroyed).
    assert current.is_dir()
