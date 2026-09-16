"""Focused lifecycle tests for the ZA-TMPFS-CLEANUP-GATE tooling cleanup.

These tests cover DEVELOPMENT/TEST scratch ownership only:

* ``packaging/windows/gui_smoke_offscreen.py`` and
  ``packaging/macos/gui_smoke_offscreen.py`` are executed offscreen on
  Linux with the interpreter running the suite (PySide6 is available in the
  test venv) to prove the real bounded GUI smoke passes, leaves no owned
  scratch behind, and preserves the caller-owned ``--work-root`` files;
* the same scripts' scratch context manager is shown to clean up on both the
  success and the exceptional path;
* ``packaging/macos/witnesses.py`` disposable allocations (Qt smoke root,
  child venv, relocation copy, PATH shims, negative-control app copy) are
  scoped and success-cleaned, the first allocation survives a failure of a
  later one, and the caller-owned work directory and input app are preserved;
* ``tests/witness/posix_lock_ci_witness.py`` registers its first scratch
  allocation before attempting the second.

The macOS native witness functions are exercised here with their native
operations controlled (mocked subprocess + a fake ``macpack``); this is NOT
a native macOS witness and proves nothing about a real ``.app`` bundle.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MACOS_PKG = _REPO_ROOT / "packaging" / "macos"
_WIN_GUI = _REPO_ROOT / "packaging" / "windows" / "gui_smoke_offscreen.py"
_MAC_GUI = _MACOS_PKG / "gui_smoke_offscreen.py"
_POSIX_WITNESS = _REPO_ROOT / "tests" / "witness" / "posix_lock_ci_witness.py"

_SENTINEL = "keep.txt"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_script_module(path: Path, name: str):
    """Load a GUI smoke script, restoring QT_QPA_PLATFORM to its prior state."""
    had = "QT_QPA_PLATFORM" in os.environ
    prior = os.environ.get("QT_QPA_PLATFORM")
    module = _load_module(path, name)
    if had:
        os.environ["QT_QPA_PLATFORM"] = prior  # type: ignore[assignment]
    else:
        os.environ.pop("QT_QPA_PLATFORM", None)
    return module


@pytest.fixture(scope="module")
def windows_gui_module():
    return _load_script_module(_WIN_GUI, "zealfie_win_gui_smoke_test")


@pytest.fixture(scope="module")
def macos_gui_module():
    return _load_script_module(_MAC_GUI, "zealfie_macos_gui_smoke_test")


@pytest.fixture(scope="module")
def witnesses_module():
    sys.path.insert(0, str(_MACOS_PKG))
    try:
        return _load_module(_MACOS_PKG / "witnesses.py", "zealfie_macos_witnesses_test")
    finally:
        sys.path.pop(0)


class _FakeMacpack:
    """Minimal stand-in for the macOS bundle layout helpers."""

    APP_NAME = "ZeAlfie.app"

    def bundle_app_dir(self, app: Path) -> Path:
        return app / "Contents" / "Resources" / "app"

    def bundled_interpreter(self, app: Path) -> Path:
        return app / "Contents" / "Resources" / "python" / "bin" / "python3.13"

    def bundle_python_dir(self, app: Path) -> Path:
        return app / "Contents" / "Resources" / "python"

    def bundle_launcher(self, app: Path) -> Path:
        return app / "Contents" / "MacOS" / "ZeAlfie"


def _make_fake_app(base: Path) -> Path:
    app = base / "ZeAlfie.app"
    launcher = app / "Contents" / "MacOS" / "ZeAlfie"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    interpreter = app / "Contents" / "Resources" / "python" / "bin" / "python3.13"
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (app / "Contents" / "Resources" / "app").mkdir(parents=True, exist_ok=True)
    return app


def _prepare_work(base: Path) -> Path:
    work = base / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / _SENTINEL).write_text("keep", encoding="utf-8")
    return work


def _assert_only_sentinel(work: Path) -> None:
    entries = sorted(p.name for p in work.iterdir())
    assert entries == [_SENTINEL], f"owned scratch left behind in {work}: {entries}"
    assert (work / _SENTINEL).read_text(encoding="utf-8") == "keep"


# ---------------------------------------------------------------------------
# GUI smoke scripts — real offscreen execution on Linux
# ---------------------------------------------------------------------------


def _run_gui_smoke(script: Path, work: Path, scratch: Path) -> subprocess.CompletedProcess:
    home = scratch / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["TMPDIR"] = str(scratch)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(scratch / "xdg-config")
    env["XDG_CACHE_HOME"] = str(scratch / "xdg-cache")
    env["XDG_DATA_HOME"] = str(scratch / "xdg-data")
    return subprocess.run(
        [sys.executable, str(script), "--work-root", str(work)],
        env=env,
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


@pytest.mark.parametrize(
    ("script", "marker"),
    [
        (_WIN_GUI, "GUI SMOKE PASS"),
        (_MAC_GUI, "QT_SMOKE=PASS"),
    ],
    ids=["windows-script", "macos-script"],
)
def test_gui_smoke_offscreen_success_cleans_scratch(
    script: Path, marker: str, tmp_path: Path
) -> None:
    pytest.importorskip("PySide6")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    work = _prepare_work(tmp_path)

    proc = _run_gui_smoke(script, work, scratch)

    assert proc.returncode == 0, f"{script.name} failed:\n{proc.stdout}\n{proc.stderr}"
    assert marker in proc.stdout, proc.stdout
    _assert_only_sentinel(work)
    # No owned runtime scratch escaped under TMPDIR (the pre-existing global
    # leak allocated a global mkdtemp, not under the caller's work_root).
    leftovers = [p for p in scratch.rglob("*smoke-runtime-*")]
    assert leftovers == [], f"global scratch leak: {leftovers}"


@pytest.mark.parametrize(
    "module_fixture",
    ["windows_gui_module", "macos_gui_module"],
)
def test_gui_smoke_helper_cleans_on_success_and_failure(
    module_fixture: str, request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    module = request.getfixturevalue(module_fixture)
    work = _prepare_work(tmp_path)

    allocated = {}
    with module._isolated_runtime_root(work) as root:
        allocated["path"] = root
        assert root.is_dir()
        assert root.parent == work
    assert not allocated["path"].exists()
    _assert_only_sentinel(work)

    caught = {}
    with pytest.raises(RuntimeError):
        with module._isolated_runtime_root(work) as root:
            caught["path"] = root
            assert root.is_dir()
            raise RuntimeError("simulated failure inside the smoke")
    assert not caught["path"].exists()
    _assert_only_sentinel(work)


# ---------------------------------------------------------------------------
# packaging/macos/witnesses.py — scoped disposable scratch
# ---------------------------------------------------------------------------


def test_witness_qt_smoke_cleans_scratch_on_success(
    witnesses_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _prepare_work(tmp_path)
    app = _make_fake_app(tmp_path)
    monkeypatch.setattr(witnesses_module, "macpack", _FakeMacpack())
    monkeypatch.setattr(witnesses_module, "_bundled_python", lambda app: app / "python")

    captured = {}

    def fake_run(argv, **kwargs):
        wr = Path(argv[argv.index("--work-root") + 1])
        captured["work_root"] = wr
        assert wr.is_dir() and wr.parent == work
        return subprocess.CompletedProcess(argv, 0, stdout="QT_SMOKE=PASS\n", stderr="")

    monkeypatch.setattr(witnesses_module, "_run", fake_run)

    out = witnesses_module.witness_qt_smoke(app, work)

    assert out == {"stdout": "QT_SMOKE=PASS"}
    assert captured["work_root"].parent == work
    assert not captured["work_root"].exists()
    _assert_only_sentinel(work)


def test_witness_qt_smoke_cleans_scratch_on_failure(
    witnesses_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _prepare_work(tmp_path)
    app = _make_fake_app(tmp_path)
    monkeypatch.setattr(witnesses_module, "macpack", _FakeMacpack())
    monkeypatch.setattr(witnesses_module, "_bundled_python", lambda app: app / "python")

    captured = {}

    def fake_run(argv, **kwargs):
        captured["work_root"] = Path(argv[argv.index("--work-root") + 1])
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")

    monkeypatch.setattr(witnesses_module, "_run", fake_run)

    with pytest.raises(witnesses_module.WitnessError):
        witnesses_module.witness_qt_smoke(app, work)

    assert not captured["work_root"].exists()
    _assert_only_sentinel(work)


def test_witness_child_venv_cleans_scratch(
    witnesses_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _prepare_work(tmp_path)
    app = _make_fake_app(tmp_path)
    fake_macpack = _FakeMacpack()
    monkeypatch.setattr(witnesses_module, "macpack", fake_macpack)
    monkeypatch.setattr(witnesses_module, "_bundled_python", lambda app: app / "python")

    captured = {}

    def fake_run_json(label, argv, **kwargs):
        driver = Path(argv[1])
        candidate = Path(argv[2])
        assert driver.is_file(), "driver must exist while the witness runs"
        assert candidate.parent.parent == work
        captured["driver"] = driver
        captured["scratch"] = candidate.parent
        return {
            "child": {
                "executable": str(candidate / "bin" / "python3.13"),
                "base_prefix": str(fake_macpack.bundle_python_dir(app)),
                "prefix": str(fake_macpack.bundle_python_dir(app)),
                "version": "3.13.15",
            },
            "pip_version": "pip 26.3",
            "install_tail": "Successfully installed packaging-26.3",
            "import": "CHILD_IMPORT=26.3",
        }

    monkeypatch.setattr(witnesses_module, "_run_json", fake_run_json)

    payload = witnesses_module.witness_child_venv(app, work, tmp_path / "wheelhouse")

    assert payload["import"] == "CHILD_IMPORT=26.3"
    assert not captured["driver"].exists()
    assert not captured["scratch"].exists()
    _assert_only_sentinel(work)


def test_witness_relocation_cleans_copy_and_qt_scratch(
    witnesses_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _prepare_work(tmp_path)
    app = _make_fake_app(tmp_path)
    monkeypatch.setattr(witnesses_module, "macpack", _FakeMacpack())
    monkeypatch.setattr(witnesses_module, "_bundled_python", lambda app: app / "python")

    seen = []

    def fake_run(argv, **kwargs):
        seen.append(Path(argv[0]))
        return subprocess.CompletedProcess(
            argv, 0, stdout="QT_SMOKE=PASS " + str(argv[0]) + "\n", stderr=""
        )

    monkeypatch.setattr(witnesses_module, "_run", fake_run)

    out = witnesses_module.witness_relocation(app, work, [])

    relocated = Path(out["relocated"])
    assert relocated.parent.parent == work
    assert not relocated.exists()
    assert all(not path.exists() for path in seen)
    assert app.is_dir(), "input app must be preserved"
    _assert_only_sentinel(work)


def test_witness_relocation_cleans_first_scratch_when_second_fails(
    witnesses_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _prepare_work(tmp_path)
    app = _make_fake_app(tmp_path)
    monkeypatch.setattr(witnesses_module, "macpack", _FakeMacpack())
    monkeypatch.setattr(witnesses_module, "_bundled_python", lambda app: app / "python")
    monkeypatch.setattr(
        witnesses_module,
        "_run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv, 0, stdout="QT_SMOKE=PASS " + str(argv[0]) + "\n", stderr=""
        ),
    )

    real_temporary_directory = tempfile.TemporaryDirectory
    state = {"calls": 0}

    def failing_temporary_directory(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 2:
            raise OSError("simulated second allocation failure")
        return real_temporary_directory(*args, **kwargs)

    monkeypatch.setattr(
        witnesses_module,
        "tempfile",
        types.SimpleNamespace(TemporaryDirectory=failing_temporary_directory),
    )

    with pytest.raises(OSError):
        witnesses_module.witness_relocation(app, work, [])

    assert state["calls"] == 2
    _assert_only_sentinel(work)


def test_witness_no_host_python_cleans_shim_and_negative_control(
    witnesses_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _prepare_work(tmp_path)
    app = _make_fake_app(tmp_path)
    fake_macpack = _FakeMacpack()
    monkeypatch.setattr(witnesses_module, "macpack", fake_macpack)
    monkeypatch.setattr(
        witnesses_module, "_bundled_python", lambda app: fake_macpack.bundled_interpreter(app)
    )
    monkeypatch.setattr(
        witnesses_module, "_ps_args", lambda pid: str(fake_macpack.bundled_interpreter(app))
    )

    class FakeProc:
        def __init__(self, *args, **kwargs):
            self.pid = 4242

        def poll(self):
            return None

        def terminate(self):
            return None

        def kill(self):
            return None

        def wait(self, timeout=None):
            return 0

        def communicate(self):
            return ("", "")

    monkeypatch.setattr(
        witnesses_module,
        "subprocess",
        types.SimpleNamespace(
            Popen=FakeProc,
            TimeoutExpired=subprocess.TimeoutExpired,
            PIPE=subprocess.PIPE,
        ),
    )
    monkeypatch.setattr(
        witnesses_module,
        "_run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="bundled interpreter missing\n"
        ),
    )

    out = witnesses_module.witness_no_host_python(app, work)

    assert out["negative_rc"] == 1
    assert out["started_args"] == str(fake_macpack.bundled_interpreter(app))
    assert app.is_dir(), "input app must be preserved"
    _assert_only_sentinel(work)


# ---------------------------------------------------------------------------
# tests/witness/posix_lock_ci_witness.py — allocation registration order
# ---------------------------------------------------------------------------


def test_posix_witness_registers_first_allocation_before_second(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    posix_module = _load_module(_POSIX_WITNESS, "zealfie_posix_lock_witness_test")
    base = tmp_path / "posix-scratch"
    base.mkdir()
    real_mkdtemp = tempfile.mkdtemp
    created: list[str] = []
    state = {"calls": 0}

    def fake_mkdtemp(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 2:
            raise OSError("simulated second allocation failure")
        path = real_mkdtemp(dir=str(base), prefix=kwargs.get("prefix", "tmp"))
        created.append(path)
        return path

    monkeypatch.setattr(
        posix_module, "tempfile", types.SimpleNamespace(mkdtemp=fake_mkdtemp)
    )

    with pytest.raises(OSError):
        posix_module._drive()

    assert state["calls"] == 2
    assert created, "the first allocation must have happened"
    assert all(not Path(path).exists() for path in created), (
        "the first allocation must be cleaned when the second one fails"
    )
