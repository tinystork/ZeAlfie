"""Tests for runtime layout paths and platform resolution."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from zealfie.runtime.layout import (
    RuntimeLayout,
    default_runtime_layout,
    default_runtime_root,
)


# ---------------------------------------------------------------------------
# RuntimeLayout
# ---------------------------------------------------------------------------


def test_runtime_layout_resolves_root() -> None:
    """The layout always resolves the root to an absolute path."""
    layout = RuntimeLayout(root=Path("/tmp/zealfie-test"))
    assert layout.root.is_absolute()


def test_runtime_layout_current_is_under_root() -> None:
    layout = RuntimeLayout(root=Path("/tmp/rt"))
    assert layout.current == Path("/tmp/rt/current").resolve()


def test_runtime_layout_staging_is_under_root() -> None:
    layout = RuntimeLayout(root=Path("/tmp/rt"))
    assert layout.staging == Path("/tmp/rt/staging").resolve()


def test_runtime_layout_active_aliases_current() -> None:
    layout = RuntimeLayout(root=Path("/tmp/rt"))
    assert layout.active == layout.current


def test_runtime_layout_immutable() -> None:
    from dataclasses import FrozenInstanceError

    layout = RuntimeLayout(root=Path("/tmp/rt"))
    with pytest.raises(FrozenInstanceError):
        layout.root = Path("/other")  # type: ignore[misc]


def test_runtime_layout_normalises_dot(tmp_path: Path) -> None:
    """A relative path is resolved to absolute."""
    layout = RuntimeLayout(root=tmp_path / "sub" / ".." / "rt")
    assert layout.root == (tmp_path / "rt").resolve()


# ---------------------------------------------------------------------------
# Platform defaults
# ---------------------------------------------------------------------------


def test_default_runtime_root_is_absolute() -> None:
    root = default_runtime_root()
    assert root.is_absolute()


def test_default_runtime_root_separated_from_dev_venv() -> None:
    """The runtime root must not be inside the project dev venv or repo."""
    root = default_runtime_root()
    project = Path(__file__).resolve().parents[2]
    # The runtime should be under the user's home or XDG dir, not in the repo.
    assert str(project) not in str(root)


def test_default_runtime_root_linux_like() -> None:
    """On Linux the path should be under ~/.local/share or XDG_DATA_HOME."""
    root = default_runtime_root()
    assert "zealfie" in root.parts
    assert "runtime" in root.parts or root.name == "runtime"


# ---------------------------------------------------------------------------
# Override
# ---------------------------------------------------------------------------


def test_default_runtime_layout_override_root(tmp_path: Path) -> None:
    layout = default_runtime_layout(root=tmp_path)
    assert layout.root == tmp_path.resolve()


def test_default_runtime_layout_env_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ZEALFIE_RUNTIME_ROOT", str(tmp_path))
    layout = default_runtime_layout()
    assert layout.root == tmp_path.resolve()


def test_default_runtime_layout_no_override_uses_default(monkeypatch) -> None:
    monkeypatch.delenv("ZEALFIE_RUNTIME_ROOT", raising=False)
    layout = default_runtime_layout()
    assert layout.root.is_absolute()
    # Must not be inside the project repo.
    project = Path(__file__).resolve().parents[2]
    assert str(project) not in str(layout.root)


# ---------------------------------------------------------------------------
# Hardening: cross-platform path logic
# ---------------------------------------------------------------------------


def test_linux_xdg_data_home_set(monkeypatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setenv("XDG_DATA_HOME", "/custom/data")
    monkeypatch.delenv("ZEALFIE_RUNTIME_ROOT", raising=False)

    root = default_runtime_root()
    assert root.parts[0] == "/"
    assert root == Path("/custom/data/zealfie/runtime")


def test_linux_xdg_data_home_unset(monkeypatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("ZEALFIE_RUNTIME_ROOT", raising=False)

    root = default_runtime_root()
    assert ".local" in root.parts
    assert "share" in root.parts
    assert "zealfie" in root.parts


def test_macos_runtime_root(monkeypatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.delenv("ZEALFIE_RUNTIME_ROOT", raising=False)

    root = default_runtime_root()
    assert "Library" in root.parts
    assert "Application Support" in root.parts
    assert "zealfie" in root.parts


def test_windows_localappdata_set(monkeypatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Windows")
    monkeypatch.setenv("LOCALAPPDATA", "C:\\Users\\Test\\AppData\\Local")
    monkeypatch.delenv("ZEALFIE_RUNTIME_ROOT", raising=False)

    root = default_runtime_root()
    # On Windows the root starts with drive letter.
    assert "zealfie" in root.parts
    assert "runtime" in root.parts or root.name == "runtime"


def test_windows_localappdata_unset(monkeypatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Windows")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("ZEALFIE_RUNTIME_ROOT", raising=False)

    root = default_runtime_root()
    # Falls back to home/AppData/Local
    assert "AppData" in root.parts
    assert "zealfie" in root.parts
