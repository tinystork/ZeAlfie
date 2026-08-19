"""Tests for ZA-M1-4.2 CORR-3 — Windows GUI self-update install interpreter.

Regression for the Windows GUI self-update root cause: when the update is
initiated from ``zealfie-gui.exe``, ``sys.executable`` is ``pythonw.exe`` and
pip/distlib regenerates the console/gui launchers with windowed shebangs
(``zealfie.exe → pythonw.exe``, ``zealfie-gui.exe → pythonww.exe``), silently
breaking the console entry point.

These tests are hermetic and run on Linux: Windows is simulated via the
injectable ``_is_windows`` seam (for the pure resolver) and via a global
``sys`` monkeypatch + real temp files (for the handoff/pip wiring).  No
network, no build, no pip, no mutation.
"""

from __future__ import annotations

import pytest

from zealfie.selfupdate.interpreter import (
    InterpreterResolutionError,
    resolve_install_interpreter,
)


# ---------------------------------------------------------------------------
# 1. Windows GUI (pythonw.exe) resolves the same-venv console sibling
# ---------------------------------------------------------------------------


def test_windows_pythonw_resolves_sibling_console_interpreter(tmp_path) -> None:
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    sibling = scripts / "python.exe"
    sibling.write_bytes(b"")
    (scripts / "pythonw.exe").write_bytes(b"")

    resolved = resolve_install_interpreter(
        sys_executable=scripts / "pythonw.exe",
        sys_prefix=tmp_path / "venv",
        _is_windows=True,
    )
    assert resolved == str(sibling)


# ---------------------------------------------------------------------------
# 2. spawn_windows_helper hands off with the resolved console interpreter
# ---------------------------------------------------------------------------


def test_spawn_windows_helper_uses_resolved_console_interpreter(
    monkeypatch, tmp_path
) -> None:
    import zealfie.selfupdate.handoff as handoff

    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    console = scripts / "python.exe"
    console.write_bytes(b"")
    (scripts / "pythonw.exe").write_bytes(b"")

    monkeypatch.setattr(handoff.sys, "platform", "win32")
    monkeypatch.setattr(handoff.sys, "executable", str(scripts / "pythonw.exe"))
    monkeypatch.setattr(handoff.sys, "prefix", str(tmp_path / "venv"))

    captured: dict = {}

    def _fake_popen(argv, **kwargs):
        captured["argv"] = argv

        class _P:
            pass

        return _P()

    monkeypatch.setattr(handoff.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(handoff.subprocess, "DETACHED_PROCESS", 0x8, raising=False)
    monkeypatch.setattr(
        handoff.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False
    )
    monkeypatch.setattr(
        handoff.subprocess, "CREATE_NO_WINDOW", 0x8000000, raising=False
    )

    ok = handoff.spawn_windows_helper(runtime_root=tmp_path, caller_pid=1234)
    assert ok is True
    assert captured["argv"][0] == str(console)
    assert not captured["argv"][0].endswith("pythonw.exe")


# ---------------------------------------------------------------------------
# 3. The update path never produces a pythonw.exe interpreter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("windowed_name", ["pythonw.exe", "pythonww.exe"])
def test_resolver_never_returns_pythonw(tmp_path, windowed_name) -> None:
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    sibling = scripts / "python.exe"
    sibling.write_bytes(b"")
    (scripts / windowed_name).write_bytes(b"")

    resolved = resolve_install_interpreter(
        sys_executable=scripts / windowed_name,
        sys_prefix=tmp_path / "venv",
        _is_windows=True,
    )
    assert resolved == str(sibling)
    assert not resolved.lower().endswith("pythonw.exe")


@pytest.mark.parametrize("windowed_name", ["pythonw.exe", "pythonww.exe"])
def test_pip_install_uses_resolved_interpreter_never_pythonw(
    monkeypatch, tmp_path, windowed_name
) -> None:
    import zealfie.selfupdate.activator as activator_mod

    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    console = scripts / "python.exe"
    console.write_bytes(b"")
    (scripts / windowed_name).write_bytes(b"")

    monkeypatch.setattr(activator_mod.sys, "platform", "win32")
    monkeypatch.setattr(
        activator_mod.sys, "executable", str(scripts / windowed_name)
    )
    monkeypatch.setattr(activator_mod.sys, "prefix", str(tmp_path / "venv"))

    captured: dict = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv

        class _P:
            returncode = 0
            stderr = ""

        return _P()

    monkeypatch.setattr(activator_mod.subprocess, "run", _fake_run)

    wheel = tmp_path / "zealfie-0.0.7-py3-none-any.whl"
    wheel.write_bytes(b"x")
    activator_mod._run_pip_install(wheel)

    assert captured["argv"][0] == str(console)
    assert not captured["argv"][0].lower().endswith("pythonw.exe")


def test_verify_installed_version_uses_resolved_interpreter(
    monkeypatch, tmp_path
) -> None:
    import zealfie.selfupdate.activator as activator_mod

    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    console = scripts / "python.exe"
    console.write_bytes(b"")
    (scripts / "pythonw.exe").write_bytes(b"")

    monkeypatch.setattr(activator_mod.sys, "platform", "win32")
    monkeypatch.setattr(
        activator_mod.sys, "executable", str(scripts / "pythonw.exe")
    )
    monkeypatch.setattr(activator_mod.sys, "prefix", str(tmp_path / "venv"))

    captured: dict = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv

        class _P:
            returncode = 0
            stdout = "0.0.7\n"
            stderr = ""

        return _P()

    monkeypatch.setattr(activator_mod.subprocess, "run", _fake_run)

    assert activator_mod._verify_installed_version("0.0.7") is None
    assert captured["argv"][0] == str(console)
    assert not captured["argv"][0].lower().endswith("pythonw.exe")


# ---------------------------------------------------------------------------
# 4. Idempotent when sys.executable is already python.exe
# ---------------------------------------------------------------------------


def test_windows_python_exe_returned_as_is(tmp_path) -> None:
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    console = scripts / "python.exe"
    console.write_bytes(b"")

    resolved = resolve_install_interpreter(
        sys_executable=console,
        sys_prefix=tmp_path / "venv",
        _is_windows=True,
    )
    assert resolved == str(console)


def test_windows_unexpected_name_returned_as_is(tmp_path) -> None:
    # An unexpected (non-windowed) name is never rewritten — idempotent.
    exe = tmp_path / "venv" / "Scripts" / "python3.12.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")

    resolved = resolve_install_interpreter(
        sys_executable=exe,
        sys_prefix=tmp_path / "venv",
        _is_windows=True,
    )
    assert resolved == str(exe)


# ---------------------------------------------------------------------------
# 5. Fail-closed: no same-venv console sibling provable -> clear error
# ---------------------------------------------------------------------------


def test_windows_fail_closed_when_sibling_missing(tmp_path) -> None:
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "pythonw.exe").write_bytes(b"")

    with pytest.raises(InterpreterResolutionError):
        resolve_install_interpreter(
            sys_executable=scripts / "pythonw.exe",
            sys_prefix=tmp_path / "venv",
            _is_windows=True,
        )


def test_windows_fail_closed_when_sibling_is_not_a_file(tmp_path) -> None:
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "pythonw.exe").write_bytes(b"")
    (scripts / "python.exe").mkdir()  # directory, not a file

    with pytest.raises(InterpreterResolutionError):
        resolve_install_interpreter(
            sys_executable=scripts / "pythonw.exe",
            sys_prefix=tmp_path / "venv",
            _is_windows=True,
        )


def test_windows_fail_closed_when_executable_outside_venv_scripts(tmp_path) -> None:
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_bytes(b"")
    # The executable lives directly under the venv root, not under Scripts.
    exe = tmp_path / "venv" / "pythonw.exe"
    exe.write_bytes(b"")

    with pytest.raises(InterpreterResolutionError):
        resolve_install_interpreter(
            sys_executable=exe,
            sys_prefix=tmp_path / "venv",
            _is_windows=True,
        )


def test_windows_fail_closed_when_prefix_unexpected(tmp_path) -> None:
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_bytes(b"")
    (scripts / "pythonw.exe").write_bytes(b"")

    with pytest.raises(InterpreterResolutionError):
        resolve_install_interpreter(
            sys_executable=scripts / "pythonw.exe",
            sys_prefix=tmp_path / "other_venv",
            _is_windows=True,
        )


def test_apply_verified_wheel_fails_closed_when_sibling_unprovable(
    monkeypatch, tmp_path
) -> None:
    """The install core refuses (FAILED, no mutation) when the same-venv
    console interpreter cannot be proven (e.g. pythonw.exe without sibling)."""
    import zealfie.selfupdate.activator as activator_mod
    from zealfie.runtime.layout import RuntimeLayout
    from zealfie.selfupdate import ApplyStatus
    from zealfie.selfupdate.state import (
        PendingSelfUpdate,
        pending_marker_path,
        write_pending_marker,
    )
    from zealfie.selfupdate.verify import compute_sha256

    layout = RuntimeLayout(root=tmp_path / "rt")
    wheel = tmp_path / "staged.whl"
    wheel.write_bytes(b"staged wheel bytes")
    pending = PendingSelfUpdate(
        target_version="0.0.7",
        channel="stable",
        commit_sha="a" * 40,
        wheel_path=str(wheel),
        wheel_sha256=compute_sha256(wheel),
        size=wheel.stat().st_size,
        created_at="2026-01-01T00:00:00+00:00",
    )
    write_pending_marker(layout, pending)

    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "pythonw.exe").write_bytes(b"")  # no python.exe sibling

    monkeypatch.setattr(activator_mod.sys, "platform", "win32")
    monkeypatch.setattr(
        activator_mod.sys, "executable", str(scripts / "pythonw.exe")
    )
    monkeypatch.setattr(activator_mod.sys, "prefix", str(tmp_path / "venv"))

    result = activator_mod._apply_verified_wheel(
        pending, wheel, tmp_path / "rtroot", layout
    )
    assert result.status is ApplyStatus.FAILED
    assert "console interpreter" in result.message
    # No mutation: the pending marker is left in place.
    assert pending_marker_path(layout).exists()


# ---------------------------------------------------------------------------
# 6. POSIX unchanged: sys.executable returned as-is
# ---------------------------------------------------------------------------


def test_posix_returns_sys_executable_unchanged() -> None:
    exe = "/usr/bin/python3"
    assert (
        resolve_install_interpreter(
            sys_executable=exe, sys_prefix="/usr", _is_windows=False
        )
        == exe
    )


def test_posix_ignores_windowed_name() -> None:
    # Even a pythonw.exe-named executable is returned unchanged off Windows.
    exe = "/venv/Scripts/pythonw.exe"
    assert (
        resolve_install_interpreter(
            sys_executable=exe, sys_prefix="/venv", _is_windows=False
        )
        == exe
    )


# ---------------------------------------------------------------------------
# Explicit python= injection is preserved unchanged
# ---------------------------------------------------------------------------


def test_explicit_python_injection_wins_unchanged() -> None:
    assert (
        resolve_install_interpreter(python="/custom/python", _is_windows=True)
        == "/custom/python"
    )
