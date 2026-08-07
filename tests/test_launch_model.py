"""Tests for LaunchPlan, LaunchResult, and script resolution."""

from __future__ import annotations

import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from zealfie.launching import (
    EntryPointScriptNotFoundError,
    LaunchPlan,
    LaunchResult,
    resolve_script,
)


# ---------------------------------------------------------------------------
# LaunchPlan
# ---------------------------------------------------------------------------


def test_launch_plan_is_immutable() -> None:
    plan = LaunchPlan(component_id="test", executable=Path("/usr/bin/true"))
    with pytest.raises(FrozenInstanceError):
        plan.component_id = "other"  # type: ignore[misc]


def test_launch_plan_arguments_are_normalised_to_tuple() -> None:
    plan = LaunchPlan(
        component_id="test",
        executable=Path("/usr/bin/python"),
        arguments=["-c", "print(1)"],
    )
    assert isinstance(plan.arguments, tuple)
    assert plan.arguments == ("-c", "print(1)")


def test_launch_plan_executable_is_required() -> None:
    plan = LaunchPlan(component_id="test", executable=Path("/bin/ls"))
    assert plan.executable == Path("/bin/ls")


def test_launch_plan_working_directory_is_optional() -> None:
    plan = LaunchPlan(component_id="test", executable=Path("/bin/ls"))
    assert plan.working_directory is None


def test_launch_plan_with_working_directory() -> None:
    plan = LaunchPlan(
        component_id="test",
        executable=Path("/bin/ls"),
        working_directory=Path("/tmp"),
    )
    assert plan.working_directory == Path("/tmp")


def test_launch_plan_component_id_is_required() -> None:
    with pytest.raises(ValueError, match="component_id is required"):
        LaunchPlan(component_id="", executable=Path("/bin/true"))


def test_launch_plan_default_arguments_is_empty() -> None:
    plan = LaunchPlan(component_id="test", executable=Path("/bin/true"))
    assert plan.arguments == ()


def test_launch_plan_no_shell_string() -> None:
    """The plan is always a structured command, never a shell string."""
    plan = LaunchPlan(component_id="test", executable=Path("/bin/echo"), arguments=("hello",))
    assert isinstance(plan.executable, Path)
    assert all(isinstance(a, str) for a in plan.arguments)


# ---------------------------------------------------------------------------
# LaunchResult
# ---------------------------------------------------------------------------


def test_launch_result_success() -> None:
    result = LaunchResult(return_code=0, stdout="ok", stderr="")
    assert result.return_code == 0
    assert result.stdout == "ok"
    assert result.stderr == ""
    assert result.timed_out is False


def test_launch_result_with_stderr() -> None:
    result = LaunchResult(return_code=1, stdout="", stderr="error message")
    assert result.stderr == "error message"


def test_launch_result_timed_out() -> None:
    result = LaunchResult(
        return_code=-1, stdout="partial", stderr="", timed_out=True
    )
    assert result.timed_out is True


def test_launch_result_nonzero_code() -> None:
    result = LaunchResult(return_code=42, stdout="", stderr="failed")
    assert result.return_code == 42


def test_launch_result_default_timed_out_is_false() -> None:
    result = LaunchResult(return_code=0, stdout="x", stderr="")
    assert result.timed_out is False


# ---------------------------------------------------------------------------
# resolve_script
# ---------------------------------------------------------------------------


def test_resolve_script_present(tmp_path: Path) -> None:
    scripts = tmp_path / "bin"
    scripts.mkdir()
    script = scripts / "zewitness"
    script.write_text("#!/bin/sh\necho ok")
    script.chmod(0o755)

    resolved = resolve_script(scripts, "zewitness")
    assert resolved == script


def test_resolve_script_not_present_raises(tmp_path: Path) -> None:
    scripts = tmp_path / "bin"
    scripts.mkdir()

    with pytest.raises(EntryPointScriptNotFoundError):
        resolve_script(scripts, "nonexistent")


def test_resolve_script_not_a_file_raises(tmp_path: Path) -> None:
    scripts = tmp_path / "bin"
    scripts.mkdir()
    sub_dir = scripts / "subdir"
    sub_dir.mkdir()

    with pytest.raises(EntryPointScriptNotFoundError):
        resolve_script(scripts, "subdir")


def test_resolve_script_windows_suffix_simulated(tmp_path: Path) -> None:
    """On any platform, passing a name with .exe should work."""
    script = tmp_path / "test.exe"
    script.write_text("")
    script.chmod(0o755)

    resolved = resolve_script(tmp_path, "test.exe")
    assert resolved == script


def test_resolve_script_absolute_scripts_dir(tmp_path: Path) -> None:
    script = tmp_path / "mycmd"
    script.write_text("")
    script.chmod(0o755)

    resolved = resolve_script(str(tmp_path), "mycmd")
    assert resolved.is_absolute()


# ---------------------------------------------------------------------------
# Hardening: resolve_script boundary checks
# ---------------------------------------------------------------------------


def test_resolve_script_rejects_parent_directory_traversal(tmp_path: Path) -> None:
    from zealfie.launching import InvalidEntryPointScriptNameError

    with pytest.raises(InvalidEntryPointScriptNameError):
        resolve_script(tmp_path, "../zewitness")


def test_resolve_script_rejects_dot_dot_only(tmp_path: Path) -> None:
    from zealfie.launching import InvalidEntryPointScriptNameError

    with pytest.raises(InvalidEntryPointScriptNameError):
        resolve_script(tmp_path, "..")


def test_resolve_script_rejects_posix_path(tmp_path: Path) -> None:
    from zealfie.launching import InvalidEntryPointScriptNameError

    with pytest.raises(InvalidEntryPointScriptNameError):
        resolve_script(tmp_path, "foo/bar")


def test_resolve_script_rejects_backslash_path(tmp_path: Path) -> None:
    from zealfie.launching import InvalidEntryPointScriptNameError

    with pytest.raises(InvalidEntryPointScriptNameError):
        resolve_script(tmp_path, "foo\\bar")


def test_resolve_script_rejects_windows_style_path(tmp_path: Path) -> None:
    from zealfie.launching import InvalidEntryPointScriptNameError

    with pytest.raises(InvalidEntryPointScriptNameError):
        resolve_script(tmp_path, "foo\\bar")


def test_resolve_script_rejects_absolute_path(tmp_path: Path) -> None:
    from zealfie.launching import InvalidEntryPointScriptNameError

    with pytest.raises(InvalidEntryPointScriptNameError):
        resolve_script(tmp_path, "/absolute/path")


def test_resolve_script_rejects_windows_absolute_path(tmp_path: Path) -> None:
    from zealfie.launching import InvalidEntryPointScriptNameError

    with pytest.raises(InvalidEntryPointScriptNameError):
        resolve_script(tmp_path, "C:\\absolute\\path")


def test_resolve_script_rejects_empty_name(tmp_path: Path) -> None:
    from zealfie.launching import InvalidEntryPointScriptNameError

    with pytest.raises(InvalidEntryPointScriptNameError):
        resolve_script(tmp_path, "")


def test_resolve_script_rejects_whitespace_only(tmp_path: Path) -> None:
    from zealfie.launching import InvalidEntryPointScriptNameError

    with pytest.raises(InvalidEntryPointScriptNameError):
        resolve_script(tmp_path, "   ")


def test_resolve_script_rejects_leading_whitespace(tmp_path: Path) -> None:
    from zealfie.launching import InvalidEntryPointScriptNameError

    with pytest.raises(InvalidEntryPointScriptNameError):
        resolve_script(tmp_path, " zewitness")


def test_resolve_script_accepts_plain_name(tmp_path: Path) -> None:
    script = tmp_path / "zewitness"
    script.write_text("")
    script.chmod(0o755)

    resolved = resolve_script(tmp_path, "zewitness")
    assert resolved == script


def test_resolve_script_resolved_path_must_stay_in_scripts_dir(tmp_path: Path) -> None:
    """Even if a valid name resolves outside scripts_dir (symlink, etc.),
    it must be rejected."""
    from zealfie.launching import InvalidEntryPointScriptNameError

    outside = tmp_path / "outside"
    outside.mkdir()
    script = outside / "escapist"
    script.write_text("")
    script.chmod(0o755)

    # Create a symlink inside scripts_dir pointing outside.
    scripts = tmp_path / "bin"
    scripts.mkdir()
    symlink = scripts / "escapist"
    try:
        symlink.symlink_to(script)
    except OSError:
        pytest.skip("symlink creation not allowed on this platform")

    with pytest.raises(InvalidEntryPointScriptNameError):
        resolve_script(scripts, "escapist")


def test_resolve_script_symlink_inside_scripts_dir_ok(tmp_path: Path) -> None:
    """A symlink that resolves to a target still inside scripts_dir is fine."""
    scripts = tmp_path / "bin"
    scripts.mkdir()
    target = scripts / "real-script"
    target.write_text("")
    target.chmod(0o755)
    symlink = scripts / "myapp"
    try:
        symlink.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation not allowed on this platform")

    resolved = resolve_script(scripts, "myapp")
    assert resolved.is_file()
