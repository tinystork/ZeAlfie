"""Tests for technical subprocess console-window hiding (W-UX-01).

ZeAlfie-owned technical helper subprocesses (host probes, pip, builds,
backend compute probes) must never flash a foreground console window on
Windows, while product application launches must stay untouched.

Two groups of tests:

1. Helper unit tests + per-site wiring tests: every technical
   ``subprocess.run`` call site passes
   ``**technical_subprocess_platform_kwargs()`` so ``creationflags`` is
   present on Windows and absent elsewhere.
2. Product-launch protection tests: ``execute_launch_plan`` /
   ``spawn_launch_plan`` never receive ``creationflags`` / ``startupinfo``
   on any platform.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from zealfie.common.subprocess_platform import technical_subprocess_platform_kwargs

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PLATFORM_VALUES = ("win32", "linux", "darwin")

# Actual value on Windows.  Injected into the subprocess module in win32
# simulation tests because CREATE_NO_WINDOW does not exist on POSIX Python.
_WINDOWS_CREATE_NO_WINDOW = 0x08000000


def _patch_platform(monkeypatch: pytest.MonkeyPatch, platform_name: str) -> None:
    """Simulate *platform_name*, injecting CREATE_NO_WINDOW on win32."""
    monkeypatch.setattr(sys, "platform", platform_name)
    if platform_name == "win32":
        monkeypatch.setattr(
            subprocess, "CREATE_NO_WINDOW", _WINDOWS_CREATE_NO_WINDOW, raising=False
        )


def _assert_kwargs_match_platform(kwargs: dict, platform_name: str) -> None:
    """On Windows creationflags must be CREATE_NO_WINDOW; elsewhere absent."""
    if platform_name == "win32":
        assert kwargs.get("creationflags") == subprocess.CREATE_NO_WINDOW
    else:
        assert "creationflags" not in kwargs


def _recorded_run(record: list, *, stdout: str = "", stderr: str = ""):
    """Return a fake ``subprocess.run`` recording args/kwargs."""

    def recorder(*args, **kwargs):
        argv = list(args[0]) if args else []
        record.append((argv, dict(kwargs)))
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=stdout, stderr=stderr
        )

    return recorder


# ---------------------------------------------------------------------------
# 1. Helper unit tests
# ---------------------------------------------------------------------------


def test_helper_returns_create_no_window_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_platform(monkeypatch, "win32")
    assert technical_subprocess_platform_kwargs() == {
        "creationflags": subprocess.CREATE_NO_WINDOW
    }


@pytest.mark.parametrize("platform_name", ["linux", "darwin"])
def test_helper_returns_empty_elsewhere(
    monkeypatch: pytest.MonkeyPatch, platform_name: str
) -> None:
    _patch_platform(monkeypatch, platform_name)
    assert technical_subprocess_platform_kwargs() == {}


# ---------------------------------------------------------------------------
# 2. Wiring tests — one per technical call site
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform_name", PLATFORM_VALUES)
def test_host_probes_default_command_runner(
    monkeypatch: pytest.MonkeyPatch, platform_name: str
) -> None:
    from zealfie.host import probes

    _patch_platform(monkeypatch, platform_name)
    recorded: list = []
    monkeypatch.setattr(
        probes.subprocess, "run", _recorded_run(recorded, stdout="ok\n")
    )

    assert probes.default_command_runner(["true"]) == "ok\n"

    assert len(recorded) == 1
    argv, kwargs = recorded[0]
    assert argv == ["true"]
    _assert_kwargs_match_platform(kwargs, platform_name)


@pytest.mark.parametrize("platform_name", PLATFORM_VALUES)
def test_acceleration_deployment_backend_compute_probe(
    monkeypatch: pytest.MonkeyPatch, platform_name: str
) -> None:
    from zealfie.acceleration import deployment

    _patch_platform(monkeypatch, platform_name)
    recorded: list = []
    monkeypatch.setattr(
        deployment.subprocess,
        "run",
        _recorded_run(recorded, stdout="BACKEND_COMPUTE_PROBE_OK"),
    )

    result = deployment._run_backend_compute_probe(
        sys.executable,
        "TEST",
        {"label": "x", "script": "print('BACKEND_COMPUTE_PROBE_OK')"},
    )

    assert result is None
    assert len(recorded) == 1
    argv, kwargs = recorded[0]
    assert argv == [sys.executable, "-"]
    _assert_kwargs_match_platform(kwargs, platform_name)


@pytest.mark.parametrize("platform_name", PLATFORM_VALUES)
def test_building_build_wheel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform_name: str
) -> None:
    from zealfie import building

    _patch_platform(monkeypatch, platform_name)

    src = tmp_path / "src"
    src.mkdir()
    (src / "pyproject.toml").write_text("[build-system]\nrequires = []\n")

    recorded: list = []

    def recorder(*args, **kwargs):
        argv = list(args[0]) if args else []
        recorded.append((argv, dict(kwargs)))
        # The real build would drop exactly one wheel into --outdir (last arg).
        outdir = Path(argv[-1])
        (outdir / "dummy_pkg-1.0-py3-none-any.whl").write_bytes(b"dummy")
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(building.subprocess, "run", recorder)

    wheel = building.build_wheel(src, output_dir=tmp_path / "dist")

    assert wheel.is_file()
    assert wheel.suffix == ".whl"
    assert len(recorded) == 1
    _, kwargs = recorded[0]
    _assert_kwargs_match_platform(kwargs, platform_name)


@pytest.mark.parametrize("platform_name", PLATFORM_VALUES)
def test_runtime_manager_install_wheel_pip_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform_name: str
) -> None:
    from zealfie.runtime import manager

    _patch_platform(monkeypatch, platform_name)

    recorded: list = []
    monkeypatch.setattr(manager.subprocess, "run", _recorded_run(recorded))
    monkeypatch.setattr(
        manager,
        "_inspect_or_fail",
        lambda wp: SimpleNamespace(
            distribution_name="dummy-pkg", version="1.0", entry_points=()
        ),
    )
    monkeypatch.setattr(manager, "_runtime_python", lambda venv_dir: Path(sys.executable))

    probe_calls = {"n": 0}

    def fake_probe(python, dist_name):
        probe_calls["n"] += 1
        if probe_calls["n"] == 1:
            return {"installed": False}
        return {"installed": True, "version": "1.0", "entry_points": []}

    monkeypatch.setattr(manager, "probe_runtime_distribution", fake_probe)

    wheel = tmp_path / "dummy_pkg-1.0-py3-none-any.whl"
    wheel.write_bytes(b"x")
    slot = tmp_path / "slots" / "slot1"
    slot.mkdir(parents=True)

    runtime = manager.SharedRuntime(layout=manager.RuntimeLayout(root=tmp_path))
    result = runtime.install_local_wheel(wheel, slot_id="slot1")

    assert result.outcome == manager.InstallOutcome.INSTALLED
    assert probe_calls["n"] == 2
    assert len(recorded) == 1
    _, kwargs = recorded[0]
    _assert_kwargs_match_platform(kwargs, platform_name)


@pytest.mark.parametrize("platform_name", PLATFORM_VALUES)
def test_runtime_probe_distribution(
    monkeypatch: pytest.MonkeyPatch, platform_name: str
) -> None:
    from zealfie.runtime import probe

    _patch_platform(monkeypatch, platform_name)
    recorded: list = []
    monkeypatch.setattr(
        probe.subprocess,
        "run",
        _recorded_run(
            recorded,
            stdout='{"installed": true, "version": "1.0", "entry_points": {}}',
        ),
    )

    result = probe.probe_runtime_distribution(sys.executable, "dummy-pkg")

    assert result["installed"] is True
    assert result["version"] == "1.0"
    assert len(recorded) == 1
    _, kwargs = recorded[0]
    _assert_kwargs_match_platform(kwargs, platform_name)


@pytest.mark.parametrize("platform_name", PLATFORM_VALUES)
def test_runtime_probe_python_version(
    monkeypatch: pytest.MonkeyPatch, platform_name: str
) -> None:
    from zealfie.runtime import probe

    _patch_platform(monkeypatch, platform_name)
    recorded: list = []
    monkeypatch.setattr(
        probe.subprocess, "run", _recorded_run(recorded, stdout="3.12.5 (main)\n")
    )

    result = probe.probe_runtime_python_version(sys.executable)

    assert result == "3.12.5 (main)"
    assert len(recorded) == 1
    _, kwargs = recorded[0]
    _assert_kwargs_match_platform(kwargs, platform_name)


@pytest.mark.parametrize("platform_name", PLATFORM_VALUES)
def test_pip_acquirer_acquire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform_name: str
) -> None:
    from zealfie.dependencies import pip_acquirer

    _patch_platform(monkeypatch, platform_name)
    recorded: list = []
    monkeypatch.setattr(pip_acquirer.subprocess, "run", _recorded_run(recorded))
    # Post-run seams: wheel staging scan + product-wheel removal are
    # separate helpers — bypass them so no real wheel files are needed.
    monkeypatch.setattr(
        pip_acquirer,
        "_remove_product_wheel_from_staging",
        lambda staging, product: None,
    )
    monkeypatch.setattr(pip_acquirer, "_collect_acquired", lambda staging: ())

    request = pip_acquirer.DependencyAcquisitionRequest(
        product_wheel_path=tmp_path / "dummy_pkg-1.0-py3-none-any.whl",
        active_extras=frozenset(),
    )
    staging = tmp_path / "staging"
    result = pip_acquirer.PipWheelhouseAcquirer().acquire(
        request, staging_dir=staging, timeout_seconds=30
    )

    assert result.staging_wheelhouse == staging.resolve()
    assert result.acquired == ()
    assert len(recorded) == 1
    _, kwargs = recorded[0]
    _assert_kwargs_match_platform(kwargs, platform_name)


@pytest.mark.parametrize("platform_name", PLATFORM_VALUES)
def test_temporary_venv_install_and_run_python(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform_name: str
) -> None:
    from zealfie import environment

    _patch_platform(monkeypatch, platform_name)
    recorded: list = []
    monkeypatch.setattr(environment.subprocess, "run", _recorded_run(recorded))
    # Never build a real venv in this test.
    monkeypatch.setattr(environment.TemporaryVenv, "create", lambda self: None)

    venv = environment.TemporaryVenv()
    try:
        venv.install_wheel(tmp_path / "dummy.whl")
        venv.run_python(["-c", "pass"])
    finally:
        venv.cleanup()

    assert len(recorded) == 2
    for _, kwargs in recorded:
        _assert_kwargs_match_platform(kwargs, platform_name)


# ---------------------------------------------------------------------------
# 3. Product-launch protection tests — must NEVER get hidden technical kwargs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform_name", PLATFORM_VALUES)
def test_execute_launch_plan_never_gets_hidden_kwargs(
    monkeypatch: pytest.MonkeyPatch, platform_name: str
) -> None:
    import zealfie.launching.executor as executor

    _patch_platform(monkeypatch, platform_name)
    recorded: list = []
    monkeypatch.setattr(executor.subprocess, "run", _recorded_run(recorded))

    plan = executor.LaunchPlan(
        component_id="x",
        executable=Path(sys.executable),
        arguments=("-c", "pass"),
    )
    result = executor.execute_launch_plan(plan)

    assert result.return_code == 0
    assert len(recorded) == 1
    _, kwargs = recorded[0]
    assert "creationflags" not in kwargs
    assert "startupinfo" not in kwargs


@pytest.mark.parametrize("platform_name", PLATFORM_VALUES)
def test_spawn_launch_plan_never_gets_hidden_kwargs(
    monkeypatch: pytest.MonkeyPatch, platform_name: str
) -> None:
    import zealfie.launching.executor as executor

    _patch_platform(monkeypatch, platform_name)

    class FakePopen:
        pid = 1234

    recorded: list = []

    def fake_popen(*args, **kwargs):
        recorded.append((list(args[0]) if args else [], dict(kwargs)))
        return FakePopen()

    monkeypatch.setattr(executor.subprocess, "Popen", fake_popen)

    plan = executor.LaunchPlan(
        component_id="x",
        executable=Path(sys.executable),
        arguments=("-c", "pass"),
    )
    spawned = executor.spawn_launch_plan(plan)

    assert spawned.pid == 1234
    assert len(recorded) == 1
    _, kwargs = recorded[0]
    assert "creationflags" not in kwargs
    assert "startupinfo" not in kwargs
