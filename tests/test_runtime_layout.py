"""Tests for runtime layout — M0-6 slot edition."""

from __future__ import annotations

from pathlib import Path

import pytest

from zealfie.runtime.layout import (
    RuntimeLayout,
    default_runtime_layout,
    default_runtime_root,
)


def test_layout_root_absolute() -> None:
    layout = RuntimeLayout(root=Path("/tmp/rt"))
    assert layout.root.is_absolute()


def test_layout_slots_dir() -> None:
    layout = RuntimeLayout(root=Path("/tmp/rt"))
    assert layout.slots == Path("/tmp/rt/slots").resolve()


def test_layout_state_dir() -> None:
    layout = RuntimeLayout(root=Path("/tmp/rt"))
    assert layout.state_dir == Path("/tmp/rt/state").resolve()


def test_layout_active_pointer() -> None:
    layout = RuntimeLayout(root=Path("/tmp/rt"))
    assert layout.active_pointer.name == "active.json"


def test_slot_path() -> None:
    layout = RuntimeLayout(root=Path("/tmp/rt"))
    assert layout.slot_path("rt-abc") == (Path("/tmp/rt/slots/rt-abc").resolve())


def test_layout_immutable() -> None:
    from dataclasses import FrozenInstanceError
    layout = RuntimeLayout(root=Path("/tmp/rt"))
    with pytest.raises(FrozenInstanceError):
        layout.root = Path("/other")  # type: ignore[misc]


def test_default_root_is_absolute() -> None:
    assert default_runtime_root().is_absolute()


def test_override_root(tmp_path: Path) -> None:
    layout = default_runtime_layout(root=tmp_path)
    assert layout.root == tmp_path.resolve()


def test_env_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ZEALFIE_RUNTIME_ROOT", str(tmp_path))
    layout = default_runtime_layout()
    assert layout.root == tmp_path.resolve()


# ---------------------------------------------------------------------------
# Cross-platform paths
# ---------------------------------------------------------------------------


def test_linux_xdg_set(monkeypatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setenv("XDG_DATA_HOME", "/custom/data")
    monkeypatch.delenv("ZEALFIE_RUNTIME_ROOT", raising=False)
    assert default_runtime_root() == Path("/custom/data/zealfie/runtime")


def test_macos_path(monkeypatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.delenv("ZEALFIE_RUNTIME_ROOT", raising=False)
    root = default_runtime_root()
    assert "Library" in root.parts
    assert "Application Support" in root.parts


def test_windows_localappdata(monkeypatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Windows")
    monkeypatch.setenv("LOCALAPPDATA", "C:\\Users\\T\\AppData\\Local")
    monkeypatch.delenv("ZEALFIE_RUNTIME_ROOT", raising=False)
    root = default_runtime_root()
    assert "zealfie" in root.parts
    assert "runtime" in root.parts or root.name == "runtime"


def test_windows_fallback(monkeypatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Windows")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("ZEALFIE_RUNTIME_ROOT", raising=False)
    root = default_runtime_root()
    assert "AppData" in root.parts
