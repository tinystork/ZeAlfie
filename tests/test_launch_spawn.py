"""Tests for non-blocking spawn launch (M1-2B).

Covers :func:`spawn_launch_plan` and
:meth:`ZeAlfieService.spawn_component`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from zealfie.launching import (
    LaunchError,
    LaunchPlan,
    SpawnedLaunch,
    execute_launch_plan,
    spawn_launch_plan,
)

# For service-level tests
from zealfie.app import (
    LaunchPreparationError,
    ZeAlfieService,
    SpawnedLaunch as AppSpawnedLaunch,
)
from zealfie.components.model import ComponentDefinition, EntryPointContract
from zealfie.components.registry import ComponentRegistry
from zealfie.runtime.model import RuntimeReasonCode, RuntimeState, RuntimeStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wait_until(predicate, *, timeout_s=5.0, interval_s=0.02, message=""):
    """Poll *predicate* until it returns truthy, or raise AssertionError.

    Polls at *interval_s* intervals for up to *timeout_s* seconds.
    Uses small intervals so total wall time is only noticeably affected
    when something is genuinely wrong.  Cross-platform: synchronization
    is based on observable conditions, not platform-specific process APIs.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise AssertionError(message or f"Condition not met within {timeout_s}s")


WITNESS_DEF = ComponentDefinition(
    "zewitness",
    "ZeWitness",
    "zealfie-witness",
    (EntryPointContract("console_scripts", "zewitness"),),
)

ZESOLVER_DEF = ComponentDefinition(
    "zesolver",
    "ZeSolver",
    "ZeSolver",
    (EntryPointContract("gui_scripts", "zesolver"),),
    required_extras=("gui",),
)

OTHER_DEF = ComponentDefinition(
    "other",
    "Other",
    "other-dist",
    (EntryPointContract("console_scripts", "other"),),
)


class _FakeSharedRuntime:
    def __init__(
        self,
        status: RuntimeStatus,
        *,
        probe_result: dict | None = None,
    ) -> None:
        self._status = status
        self._probe_result = probe_result or {}

    def status(self) -> RuntimeStatus:
        return self._status


def _ready_status(active_path: Path, python: Path | None = None) -> RuntimeStatus:
    if python is None:
        python = active_path / "bin" / "python"
    return RuntimeStatus(
        state=RuntimeState.READY,
        runtime_root=active_path.parent,
        active_slot_id="rt-test00000000",
        active_path=active_path,
        python_executable=python,
        python_version="3.13.0",
        reason_code=RuntimeReasonCode.RUNTIME_READY,
    )


# ===========================================================================
# spawn_launch_plan — unit tests
# ===========================================================================


class TestSpawnLaunchPlan:
    """Tests for the low-level spawn_launch_plan function."""

    def test_uses_popen_with_shell_false_and_structured_cmd(self, monkeypatch):
        """spawn_launch_plan calls Popen with shell=False and a list cmd."""
        captured_cmd = None
        captured_kwargs = {}

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                nonlocal captured_cmd, captured_kwargs
                captured_cmd = cmd
                captured_kwargs = kwargs
                self.pid = 12345

        monkeypatch.setattr(subprocess, "Popen", FakePopen)

        plan = LaunchPlan(
            component_id="test",
            executable=Path("/usr/bin/python3"),
            arguments=("-c", "print('hello')"),
        )
        result = spawn_launch_plan(plan)

        assert captured_cmd == ["/usr/bin/python3", "-c", "print('hello')"]
        assert captured_kwargs["shell"] is False
        assert isinstance(result, SpawnedLaunch)
        assert result.pid == 12345
        assert result.component_id == "test"
        assert result.executable == Path("/usr/bin/python3")
        assert result.command == ("/usr/bin/python3", "-c", "print('hello')")

    def test_returns_immediately_and_does_not_call_subprocess_run(self, monkeypatch):
        """spawn_launch_plan does not call subprocess.run."""
        run_called = False
        original_run = subprocess.run

        def fake_run(*args, **kwargs):
            nonlocal run_called
            run_called = True
            return original_run(*args, **kwargs)

        monkeypatch.setattr(subprocess, "run", fake_run)

        plan = LaunchPlan(
            component_id="test",
            executable=Path(sys.executable),
            arguments=("-c", "import time; time.sleep(0.1)"),
        )
        result = spawn_launch_plan(plan)

        assert run_called is False, "subprocess.run should NOT be called"
        assert result.pid is not None
        assert result.pid > 0

    def test_no_stdout_stderr_pipe_by_default(self, monkeypatch):
        """Default stdin/stdout/stderr are None (inherit), not PIPE."""
        captured_kwargs = {}

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                nonlocal captured_kwargs
                captured_kwargs = kwargs
                self.pid = 42

        monkeypatch.setattr(subprocess, "Popen", FakePopen)

        plan = LaunchPlan(component_id="test", executable=Path(sys.executable))
        spawn_launch_plan(plan)

        assert captured_kwargs.get("stdin") is None
        assert captured_kwargs.get("stdout") is None
        assert captured_kwargs.get("stderr") is None

    def test_cwd_from_plan_passed_through(self, monkeypatch, tmp_path):
        """cwd from LaunchPlan is passed to Popen."""
        captured_cwd = None

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                nonlocal captured_cwd
                captured_cwd = kwargs.get("cwd")
                self.pid = 1

        monkeypatch.setattr(subprocess, "Popen", FakePopen)

        plan = LaunchPlan(
            component_id="test",
            executable=Path(sys.executable),
            working_directory=tmp_path,
        )
        spawn_launch_plan(plan)

        assert captured_cwd == str(tmp_path)

    def test_env_overrides_merge_without_mutating_os_environ(self, monkeypatch):
        """env_overrides are merged into os.environ.copy(), not os.environ."""
        captured_env = None

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                nonlocal captured_env
                captured_env = kwargs.get("env")
                self.pid = 1

        monkeypatch.setattr(subprocess, "Popen", FakePopen)

        # Record original os.environ to verify no mutation.
        original_keys = set(os.environ.keys())
        original_home = os.environ.get("HOME")

        plan = LaunchPlan(component_id="test", executable=Path(sys.executable))
        spawn_launch_plan(plan, env_overrides={"CUSTOM_KEY": "custom_value"})

        # Verify child env has the override.
        assert captured_env is not None
        assert captured_env["CUSTOM_KEY"] == "custom_value"

        # Verify os.environ was NOT mutated.
        assert "CUSTOM_KEY" not in os.environ, (
            "os.environ was mutated — CUSTOM_KEY leaked into parent"
        )
        assert set(os.environ.keys()) == original_keys, (
            "os.environ keys changed after spawn_launch_plan"
        )

    def test_env_overrides_none_means_no_copy(self, monkeypatch):
        """When env_overrides is None, child inherits env directly."""
        captured_env = None

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                nonlocal captured_env
                captured_env = kwargs.get("env")
                self.pid = 1

        monkeypatch.setattr(subprocess, "Popen", FakePopen)

        plan = LaunchPlan(component_id="test", executable=Path(sys.executable))
        spawn_launch_plan(plan)  # no env_overrides

        assert captured_env is None, (
            "Popen should receive env=None when no overrides"
        )

    def test_empty_env_overrides_is_treated_as_none(self, monkeypatch):
        """An empty dict passed as env_overrides should pass env=None."""
        captured_env = None

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                nonlocal captured_env
                captured_env = kwargs.get("env")
                self.pid = 1

        monkeypatch.setattr(subprocess, "Popen", FakePopen)

        plan = LaunchPlan(component_id="test", executable=Path(sys.executable))
        spawn_launch_plan(plan, env_overrides={})

        assert captured_env is None

    def test_explicit_stdin_devnull(self, monkeypatch):
        """Explicit stdin=DEVNULL is forwarded to Popen."""
        captured_stdin = object()

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                nonlocal captured_stdin
                captured_stdin = kwargs.get("stdin")
                self.pid = 1

        monkeypatch.setattr(subprocess, "Popen", FakePopen)

        plan = LaunchPlan(component_id="test", executable=Path(sys.executable))
        spawn_launch_plan(plan, stdin=subprocess.DEVNULL)

        assert captured_stdin is subprocess.DEVNULL

    def test_explicit_stdout_stderr_passed_through(self, monkeypatch):
        """Explicit stdout/stderr are forwarded."""
        captured_kwargs = {}

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                nonlocal captured_kwargs
                captured_kwargs = kwargs
                self.pid = 1

        monkeypatch.setattr(subprocess, "Popen", FakePopen)

        plan = LaunchPlan(component_id="test", executable=Path(sys.executable))
        spawn_launch_plan(
            plan,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        assert captured_kwargs["stdout"] is subprocess.DEVNULL
        assert captured_kwargs["stderr"] is subprocess.DEVNULL

    def test_no_abandoned_pipes(self, monkeypatch):
        """Verify PIPE is never used as a default — no abandoned pipes."""
        captured_kwargs = {}

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                nonlocal captured_kwargs
                captured_kwargs = kwargs
                self.pid = 1

        monkeypatch.setattr(subprocess, "Popen", FakePopen)

        plan = LaunchPlan(component_id="test", executable=Path(sys.executable))
        spawn_launch_plan(plan)

        assert captured_kwargs.get("stdout") is not subprocess.PIPE, (
            "stdout must not be PIPE by default"
        )
        assert captured_kwargs.get("stderr") is not subprocess.PIPE, (
            "stderr must not be PIPE by default"
        )

    def test_error_path_popen_oserror_becomes_launch_error(self, monkeypatch):
        """Popen OSError is wrapped as LaunchError."""

        def failing_popen(*args, **kwargs):
            raise OSError("simulated fork failure")

        monkeypatch.setattr(subprocess, "Popen", failing_popen)

        plan = LaunchPlan(component_id="test", executable=Path("/nonexistent/binary"))
        with pytest.raises(LaunchError, match="could not spawn"):
            spawn_launch_plan(plan)


# ===========================================================================
# spawn_launch_plan — real-process tests
# ===========================================================================


class TestSpawnLaunchPlanRealProcess:
    """Tests that actually spawn real processes (short-lived)."""

    def test_spawn_real_process_and_get_pid(self, tmp_path):
        """A real short-lived process is spawned and we get a valid pid."""
        sentinel = tmp_path / "pid_ok.txt"
        plan = LaunchPlan(
            component_id="test",
            executable=Path(sys.executable),
            arguments=("-c", f"import pathlib; pathlib.Path({str(sentinel)!r}).write_text('ok')"),
        )
        result = spawn_launch_plan(plan)
        assert result.pid > 0
        assert result.component_id == "test"
        _wait_until(lambda: sentinel.exists() and sentinel.read_text() == "ok",
                    message=f"Sentinel {sentinel} not observed")

    def test_spawn_with_cwd(self, tmp_path):
        """Spawn a process that confirms cwd is set correctly."""
        plan = LaunchPlan(
            component_id="test",
            executable=Path(sys.executable),
            arguments=("-c", "import os; print(os.getcwd())"),
            working_directory=tmp_path,
        )
        # Redirect stdout to a temp file so we can read it.
        out_file = tmp_path / "out.txt"
        with open(out_file, "w") as f:
            result = spawn_launch_plan(plan, stdout=f.fileno())
        _wait_until(lambda: out_file.read_text().strip() == str(tmp_path),
                    message="cwd output not observed")

    def test_spawn_with_env_overrides(self, tmp_path):
        """Spawn a process with env_overrides and verify child sees them."""
        plan = LaunchPlan(
            component_id="test",
            executable=Path(sys.executable),
            arguments=("-c", "import os; print(os.environ.get('ZEALFIE_TEST_KEY', 'MISSING'))"),
            working_directory=tmp_path,
        )
        out_file = tmp_path / "env_out.txt"
        with open(out_file, "w") as f:
            result = spawn_launch_plan(
                plan,
                env_overrides={"ZEALFIE_TEST_KEY": "hello_world"},
                stdin=subprocess.DEVNULL,
                stdout=f.fileno(),
            )
        _wait_until(lambda: out_file.read_text().strip() == "hello_world",
                    message="env override output not observed")

    def test_real_process_survives_parent_call(self, tmp_path):
        """The spawned process runs independently (does not block parent)."""
        start_sentinel = tmp_path / "started.txt"
        done_sentinel = tmp_path / "done.txt"
        start = time.monotonic()
        plan = LaunchPlan(
            component_id="test",
            executable=Path(sys.executable),
            arguments=("-c", (
                f"import pathlib, time; "
                f"pathlib.Path({str(start_sentinel)!r}).write_text('started'); "
                f"time.sleep(2); "
                f"pathlib.Path({str(done_sentinel)!r}).write_text('done')"
            )),
        )
        result = spawn_launch_plan(plan, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elapsed = time.monotonic() - start

        assert elapsed < 0.5, f"spawn took {elapsed:.2f}s, expected <0.5s (non-blocking)"
        assert result.pid > 0

        # Wait for child to complete naturally via sentinel observation.
        _wait_until(lambda: done_sentinel.exists(),
                    timeout_s=10.0,
                    message="Child did not finish within deadline")

    def test_spawn_uses_shell_false_with_real_process(self, tmp_path):
        """Verify a real child is spawned with shell=False (no shell injection)."""
        sentinel = tmp_path / "shell_false_ok.txt"
        plan = LaunchPlan(
            component_id="test",
            executable=Path(sys.executable),
            arguments=("-c", f"import pathlib; pathlib.Path({str(sentinel)!r}).write_text('ok')"),
        )
        result = spawn_launch_plan(
            plan,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait_until(lambda: sentinel.exists(),
                    message="Child did not exit")

    def test_spawn_with_devnull_stdin(self, tmp_path):
        """Spawn with stdin=DEVNULL — process does not hang waiting for input."""
        sentinel = tmp_path / "devnull_ok.txt"
        plan = LaunchPlan(
            component_id="test",
            executable=Path(sys.executable),
            arguments=("-c", (
                f"import pathlib, sys; "
                f"_ = sys.stdin.readline(); "  # reads '' from DEVNULL immediately
                f"pathlib.Path({str(sentinel)!r}).write_text('ok')"
            )),
        )
        result = spawn_launch_plan(
            plan,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait_until(lambda: sentinel.exists(),
                    message="DEVNULL stdin child did not finish")


# ===========================================================================
# ZeAlfieService.spawn_component — service-level tests
# ===========================================================================


class TestServiceSpawnComponent:
    """Tests for ZeAlfieService.spawn_component using fake runtime."""

    def test_spawn_component_calls_prepare_and_spawn(self, monkeypatch, tmp_path):
        """spawn_component calls prepare_launch_plan then spawn_launch_plan."""
        active = tmp_path / "rt" / "slots" / "test"
        python = active / "bin" / "python"
        scripts = active / "bin"
        scripts.mkdir(parents=True)
        script = scripts / "zewitness"
        script.write_text("#!/bin/sh\necho ok")
        script.chmod(0o755)

        from zealfie.app import service as svc_mod

        def fake_probe(runtime_python, dist_name):
            return {
                "installed": True,
                "version": "0.0.1",
                "entry_points": [
                    {"group": "console_scripts", "name": "zewitness"},
                ],
            }

        monkeypatch.setattr(svc_mod, "probe_runtime_distribution", fake_probe)

        # Capture spawn_launch_plan call.
        captured_plan = None
        captured_kwargs = {}

        def fake_spawn(plan, **kwargs):
            nonlocal captured_plan, captured_kwargs
            captured_plan = plan
            captured_kwargs = kwargs
            return SpawnedLaunch(
                component_id=plan.component_id,
                pid=99999,
                executable=plan.executable,
            )

        monkeypatch.setattr(svc_mod, "spawn_launch_plan", fake_spawn)

        service = ZeAlfieService(
            registry=ComponentRegistry([WITNESS_DEF]),
            runtime=_FakeSharedRuntime(_ready_status(active, python=python)),
        )

        result = service.spawn_component("zewitness")
        assert result.component_id == "zewitness"
        assert result.pid == 99999
        assert captured_plan is not None
        assert captured_plan.component_id == "zewitness"

    def test_spawn_component_unknown_component_raises(self):
        """Unknown component raises UnknownComponentError."""
        from zealfie.components import UnknownComponentError

        active = Path("/fake/rt/slots/test")
        service = ZeAlfieService(
            registry=ComponentRegistry([WITNESS_DEF]),
            runtime=_FakeSharedRuntime(_ready_status(active)),
        )
        with pytest.raises(UnknownComponentError):
            service.spawn_component("nonexistent")

    def test_spawn_component_absent_runtime_raises(self):
        """ABSENT runtime raises LaunchPreparationError."""
        service = ZeAlfieService(
            registry=ComponentRegistry([WITNESS_DEF]),
            runtime=_FakeSharedRuntime(
                RuntimeStatus(
                    state=RuntimeState.ABSENT,
                    runtime_root=Path("/fake/runtime"),
                )
            ),
        )
        with pytest.raises(LaunchPreparationError, match="absent"):
            service.spawn_component("zewitness")

    def test_spawn_component_env_overrides_still_pass_through(self, monkeypatch, tmp_path):
        """Caller env_overrides are forwarded through spawn_component."""
        active = tmp_path / "rt" / "slots" / "test"
        python = active / "bin" / "python"
        scripts = active / "bin"
        scripts.mkdir(parents=True)
        script = scripts / "zewitness"
        script.write_text("#!/bin/sh\necho ok")
        script.chmod(0o755)

        from zealfie.app import service as svc_mod

        def fake_probe(runtime_python, dist_name):
            return {
                "installed": True,
                "version": "0.0.1",
                "entry_points": [
                    {"group": "console_scripts", "name": "zewitness"},
                ],
            }

        monkeypatch.setattr(svc_mod, "probe_runtime_distribution", fake_probe)

        captured_env_overrides = None

        def fake_spawn(plan, *, env_overrides=None, **kwargs):
            nonlocal captured_env_overrides
            captured_env_overrides = env_overrides
            return SpawnedLaunch(component_id=plan.component_id, pid=1)

        monkeypatch.setattr(svc_mod, "spawn_launch_plan", fake_spawn)

        service = ZeAlfieService(
            registry=ComponentRegistry([WITNESS_DEF]),
            runtime=_FakeSharedRuntime(_ready_status(active, python=python)),
        )

        service.spawn_component(
            "zewitness",
            env_overrides={"MY_CUSTOM": "val"},
        )

        assert captured_env_overrides == {"MY_CUSTOM": "val"}

    def test_spawn_component_zesolver_gets_embedded_host(self, monkeypatch, tmp_path):
        """ZeSolver spawn includes ZESOLVER_EMBEDDED_HOST=1 in child env."""
        active = tmp_path / "rt" / "slots" / "test"
        python = active / "bin" / "python"
        scripts = active / "bin"
        scripts.mkdir(parents=True)
        script = scripts / "zesolver"
        script.write_text("#!/bin/sh\necho ok")
        script.chmod(0o755)

        from zealfie.app import service as svc_mod

        def fake_probe(runtime_python, dist_name):
            return {
                "installed": True,
                "version": "0.0.1",
                "entry_points": [
                    {"group": "gui_scripts", "name": "zesolver"},
                ],
            }

        monkeypatch.setattr(svc_mod, "probe_runtime_distribution", fake_probe)

        captured_env_overrides = None

        def fake_spawn(plan, *, env_overrides=None, **kwargs):
            nonlocal captured_env_overrides
            captured_env_overrides = env_overrides
            return SpawnedLaunch(component_id=plan.component_id, pid=1)

        monkeypatch.setattr(svc_mod, "spawn_launch_plan", fake_spawn)

        service = ZeAlfieService(
            registry=ComponentRegistry([ZESOLVER_DEF]),
            runtime=_FakeSharedRuntime(_ready_status(active, python=python)),
        )

        service.spawn_component("zesolver")

        assert captured_env_overrides is not None
        assert captured_env_overrides["ZESOLVER_EMBEDDED_HOST"] == "1"

    def test_spawn_component_zesolver_env_not_pollute_parent(self, monkeypatch, tmp_path):
        """ZeSolver embedded-host is ONLY in child env, not os.environ."""
        active = tmp_path / "rt" / "slots" / "test"
        python = active / "bin" / "python"
        scripts = active / "bin"
        scripts.mkdir(parents=True)
        script = scripts / "zesolver"
        script.write_text("#!/bin/sh\necho ok")
        script.chmod(0o755)

        from zealfie.app import service as svc_mod

        def fake_probe(runtime_python, dist_name):
            return {
                "installed": True,
                "version": "0.0.1",
                "entry_points": [
                    {"group": "gui_scripts", "name": "zesolver"},
                ],
            }

        monkeypatch.setattr(svc_mod, "probe_runtime_distribution", fake_probe)

        # Use real spawn so we can check os.environ post-call
        monkeypatch.setattr(
            svc_mod,
            "spawn_launch_plan",
            lambda plan, *, env_overrides=None, **kwargs: SpawnedLaunch(
                component_id=plan.component_id, pid=1
            ),
        )

        service = ZeAlfieService(
            registry=ComponentRegistry([ZESOLVER_DEF]),
            runtime=_FakeSharedRuntime(_ready_status(active, python=python)),
        )

        assert "ZESOLVER_EMBEDDED_HOST" not in os.environ, (
            "ZESOLVER_EMBEDDED_HOST leaked into os.environ before spawn"
        )

        service.spawn_component("zesolver")

        assert "ZESOLVER_EMBEDDED_HOST" not in os.environ, (
            "os.environ was mutated — ZESOLVER_EMBEDDED_HOST leaked into parent"
        )

    def test_spawn_component_non_zesolver_no_embedded_host(self, monkeypatch, tmp_path):
        """Non-ZeSolver component does NOT get embedded-host override."""
        active = tmp_path / "rt" / "slots" / "test"
        python = active / "bin" / "python"
        scripts = active / "bin"
        scripts.mkdir(parents=True)
        script = scripts / "other"
        script.write_text("#!/bin/sh\necho ok")
        script.chmod(0o755)

        from zealfie.app import service as svc_mod

        def fake_probe(runtime_python, dist_name):
            return {
                "installed": True,
                "version": "0.0.1",
                "entry_points": [
                    {"group": "console_scripts", "name": "other"},
                ],
            }

        monkeypatch.setattr(svc_mod, "probe_runtime_distribution", fake_probe)

        captured_env_overrides = None

        def fake_spawn(plan, *, env_overrides=None, **kwargs):
            nonlocal captured_env_overrides
            captured_env_overrides = env_overrides
            return SpawnedLaunch(component_id=plan.component_id, pid=1)

        monkeypatch.setattr(svc_mod, "spawn_launch_plan", fake_spawn)

        service = ZeAlfieService(
            registry=ComponentRegistry([OTHER_DEF]),
            runtime=_FakeSharedRuntime(_ready_status(active, python=python)),
        )

        service.spawn_component("other")

        assert captured_env_overrides is None, (
            "non-ZeSolver component should not get env_overrides"
        )

    def test_spawn_component_caller_overrides_embedded_host(self, monkeypatch, tmp_path):
        """Caller can override ZeSolver's embedded-host setting."""
        active = tmp_path / "rt" / "slots" / "test"
        python = active / "bin" / "python"
        scripts = active / "bin"
        scripts.mkdir(parents=True)
        script = scripts / "zesolver"
        script.write_text("#!/bin/sh\necho ok")
        script.chmod(0o755)

        from zealfie.app import service as svc_mod

        def fake_probe(runtime_python, dist_name):
            return {
                "installed": True,
                "version": "0.0.1",
                "entry_points": [
                    {"group": "gui_scripts", "name": "zesolver"},
                ],
            }

        monkeypatch.setattr(svc_mod, "probe_runtime_distribution", fake_probe)

        captured_env_overrides = None

        def fake_spawn(plan, *, env_overrides=None, **kwargs):
            nonlocal captured_env_overrides
            captured_env_overrides = env_overrides
            return SpawnedLaunch(component_id=plan.component_id, pid=1)

        monkeypatch.setattr(svc_mod, "spawn_launch_plan", fake_spawn)

        service = ZeAlfieService(
            registry=ComponentRegistry([ZESOLVER_DEF]),
            runtime=_FakeSharedRuntime(_ready_status(active, python=python)),
        )

        service.spawn_component(
            "zesolver",
            env_overrides={"ZESOLVER_EMBEDDED_HOST": "0"},
        )

        assert captured_env_overrides is not None
        assert captured_env_overrides["ZESOLVER_EMBEDDED_HOST"] == "0", (
            "Caller override should win"
        )

    def test_spawn_component_zesolver_with_additional_env(self, monkeypatch, tmp_path):
        """ZeSolver gets both embedded-host AND caller's extra vars."""
        active = tmp_path / "rt" / "slots" / "test"
        python = active / "bin" / "python"
        scripts = active / "bin"
        scripts.mkdir(parents=True)
        script = scripts / "zesolver"
        script.write_text("#!/bin/sh\necho ok")
        script.chmod(0o755)

        from zealfie.app import service as svc_mod

        def fake_probe(runtime_python, dist_name):
            return {
                "installed": True,
                "version": "0.0.1",
                "entry_points": [
                    {"group": "gui_scripts", "name": "zesolver"},
                ],
            }

        monkeypatch.setattr(svc_mod, "probe_runtime_distribution", fake_probe)

        captured_env_overrides = None

        def fake_spawn(plan, *, env_overrides=None, **kwargs):
            nonlocal captured_env_overrides
            captured_env_overrides = env_overrides
            return SpawnedLaunch(component_id=plan.component_id, pid=1)

        monkeypatch.setattr(svc_mod, "spawn_launch_plan", fake_spawn)

        service = ZeAlfieService(
            registry=ComponentRegistry([ZESOLVER_DEF]),
            runtime=_FakeSharedRuntime(_ready_status(active, python=python)),
        )

        service.spawn_component(
            "zesolver",
            env_overrides={"DISPLAY": ":1"},
        )

        assert captured_env_overrides is not None
        assert captured_env_overrides["ZESOLVER_EMBEDDED_HOST"] == "1"
        assert captured_env_overrides["DISPLAY"] == ":1"

    def test_spawn_component_returns_spawned_launch_type(self, monkeypatch, tmp_path):
        """Return type is SpawnedLaunch from app layer."""
        active = tmp_path / "rt" / "slots" / "test"
        python = active / "bin" / "python"
        scripts = active / "bin"
        scripts.mkdir(parents=True)
        script = scripts / "zewitness"
        script.write_text("#!/bin/sh\necho ok")
        script.chmod(0o755)

        from zealfie.app import service as svc_mod

        def fake_probe(runtime_python, dist_name):
            return {
                "installed": True,
                "version": "0.0.1",
                "entry_points": [
                    {"group": "console_scripts", "name": "zewitness"},
                ],
            }

        monkeypatch.setattr(svc_mod, "probe_runtime_distribution", fake_probe)

        # Use real spawn so we exercise the full path.
        sentinel = tmp_path / "svc_spawn_ok.txt"
        plan = LaunchPlan(
            component_id="test_x",
            executable=Path(sys.executable),
            arguments=("-c", f"import pathlib; pathlib.Path({str(sentinel)!r}).write_text('ok')"),
        )

        def fake_prepare(self, cid):
            # shortcut: return a known-safe plan instead of probing
            return plan

        monkeypatch.setattr(
            ZeAlfieService, "prepare_launch_plan", fake_prepare
        )

        service = ZeAlfieService(
            registry=ComponentRegistry([WITNESS_DEF]),
            runtime=_FakeSharedRuntime(_ready_status(active, python=python)),
        )

        result = service.spawn_component("zewitness")
        assert isinstance(result, SpawnedLaunch)
        assert isinstance(result, AppSpawnedLaunch)
        assert result.pid > 0

        _wait_until(lambda: sentinel.exists() and sentinel.read_text() == "ok",
                    message="Service spawn child did not complete")


# ===========================================================================
# launch_component remains synchronous
# ===========================================================================


def test_launch_component_still_synchronous():
    """Existing launch_component still uses execute_launch_plan (blocking)."""
    # This test verifies launch_component hasn't been modified.
    plan = LaunchPlan(
        component_id="test",
        executable=Path(sys.executable),
        arguments=("-c", "pass"),
    )

    from zealfie.launching import execute_launch_plan as exec_fn

    result = exec_fn(plan)
    assert result.return_code == 0
    assert result.timed_out is False
    assert result.stdout == ""
    assert result.stderr == ""


# ===========================================================================
# execute_launch_plan unchanged
# ===========================================================================


def test_execute_launch_plan_unchanged():
    """Existing execute_launch_plan still works as before."""
    plan = LaunchPlan(
        component_id="test",
        executable=Path(sys.executable),
        arguments=("-c", "print('hello-old')"),
    )
    result = execute_launch_plan(plan)
    assert result.return_code == 0
    assert result.stdout.strip() == "hello-old"
    assert result.timed_out is False
