"""Tests for ZA-M1-4.2 — Qt-free GUI self-update orchestration + restart.

Hermetic: identity, plan builder, stage, pending loader, apply, and the
restart spawn seams are all injected fakes.  No network, no build, no pip,
no Qt.  Covers the mission's fail-closed behaviour matrix:

* SOURCE / EDITABLE / UNKNOWN → NOT_SUPPORTED (nothing attempted);
* UP_TO_DATE → silent;
* UPDATE_AVAILABLE → stage once, or reuse a valid pending marker;
* stage / build / verify / check failure → FAILED (silent, no mutation);
* apply is a separate, single-shot path (never invoked here);
* restart spawns are one-shot, list-argv, no shell, no restart loop.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from zealfie.runtime.layout import RuntimeLayout
from zealfie.selfupdate import (
    ApplyStatus,
    InstallMode,
    SelfUpdateStatus,
    ZeAlfieIdentity,
)
from zealfie.selfupdate.orchestration import (
    GuiSelfUpdateResult,
    GuiSelfUpdateStatus,
    make_self_update_apply_fn,
    make_self_update_check_fn,
    run_self_update_check,
)
from zealfie.selfupdate.state import (
    PendingMarkerError,
    PendingSelfUpdate,
    write_pending_marker,
)
from zealfie.selfupdate.verify import compute_sha256

SHA_A = "a" * 40
SHA_B = "b" * 40

INSTALLED = ZeAlfieIdentity("0.0.6", InstallMode.INSTALLED, "/site-packages/zealfie")
EDITABLE = ZeAlfieIdentity("0.0.6", InstallMode.EDITABLE, "/src/zealfie")
SOURCE = ZeAlfieIdentity("0.0.6", InstallMode.SOURCE, "/repo/src/zealfie")
UNKNOWN = ZeAlfieIdentity("0.0.6", InstallMode.UNKNOWN, "/nowhere/zealfie")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _plan(status, resolution=None, reason=None):
    return SimpleNamespace(status=status, resolution=resolution, reason=reason)


def _resolution(available_version="0.0.7"):
    return SimpleNamespace(
        channel="stable",
        available_version=available_version,
    )


def _staged(wheel_version="0.0.7"):
    return SimpleNamespace(wheel_version=wheel_version)


def _resolver(sha=SHA_A):
    return lambda owner, repo, ref: sha


def _tags(names=("v0.0.7",)):
    return lambda owner, repo: list(names)


def _noop_fetcher(owner, repo, commit_sha):
    return b"fake"


def _write_marker(layout: RuntimeLayout, tmp_path: Path, target="0.0.7"):
    wheel = tmp_path / "staged.whl"
    wheel.write_bytes(b"staged wheel bytes")
    pending = PendingSelfUpdate(
        target_version=target,
        channel="stable",
        commit_sha=SHA_A,
        wheel_path=str(wheel),
        wheel_sha256=compute_sha256(wheel),
        size=wheel.stat().st_size,
        created_at="2026-01-01T00:00:00+00:00",
    )
    write_pending_marker(layout, pending)
    return pending


# ---------------------------------------------------------------------------
# 1. Not supported (SOURCE / EDITABLE / UNKNOWN) → silent, nothing staged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("identity", [SOURCE, EDITABLE, UNKNOWN])
def test_not_supported_is_silent_and_never_stages(identity, tmp_path):
    stage_calls: list = []

    result = run_self_update_check(
        resolver=None,
        tags_lister=None,
        fetcher=None,
        work_root=tmp_path / "work",
        layout=RuntimeLayout(root=tmp_path / "rt"),
        identity=identity,
        _stage=lambda *a, **k: stage_calls.append(1) or _staged(),
    )

    assert result.status is GuiSelfUpdateStatus.NOT_SUPPORTED
    assert result.version is None
    assert stage_calls == []


# ---------------------------------------------------------------------------
# 2. UP_TO_DATE → silent
# ---------------------------------------------------------------------------


def test_up_to_date_is_silent(tmp_path):
    result = run_self_update_check(
        resolver=None,
        tags_lister=None,
        fetcher=None,
        work_root=tmp_path / "work",
        layout=RuntimeLayout(root=tmp_path / "rt"),
        identity=INSTALLED,
        _plan=lambda identity, channel, resolver, tags_lister: _plan(
            SelfUpdateStatus.UP_TO_DATE
        ),
    )
    assert result.status is GuiSelfUpdateStatus.UP_TO_DATE
    assert result.version is None


# ---------------------------------------------------------------------------
# 3. CHECK_FAILED → FAILED, silent
# ---------------------------------------------------------------------------


def test_check_failed_is_silent_and_never_stages(tmp_path):
    stage_calls: list = []

    result = run_self_update_check(
        resolver=None,
        tags_lister=None,
        fetcher=None,
        work_root=tmp_path / "work",
        layout=RuntimeLayout(root=tmp_path / "rt"),
        identity=INSTALLED,
        _plan=lambda identity, channel, resolver, tags_lister: _plan(
            SelfUpdateStatus.CHECK_FAILED, reason="network down"
        ),
        _stage=lambda *a, **k: stage_calls.append(1) or _staged(),
    )
    assert result.status is GuiSelfUpdateStatus.FAILED
    assert "network down" in (result.reason or "")
    assert stage_calls == []


# ---------------------------------------------------------------------------
# 4. UPDATE_AVAILABLE → stage once, then UPDATE_READY
# ---------------------------------------------------------------------------


def test_update_available_stages_and_reports_ready(tmp_path):
    stage_calls: list = []

    def _stage(resolution, *, fetcher, work_root, layout):
        stage_calls.append(resolution)
        return _staged("0.0.7")

    result = run_self_update_check(
        resolver=None,
        tags_lister=None,
        fetcher=None,
        work_root=tmp_path / "work",
        layout=RuntimeLayout(root=tmp_path / "rt"),
        identity=INSTALLED,
        _plan=lambda identity, channel, resolver, tags_lister: _plan(
            SelfUpdateStatus.UPDATE_AVAILABLE, resolution=_resolution("0.0.7")
        ),
        _stage=_stage,
    )
    assert result.status is GuiSelfUpdateStatus.UPDATE_READY
    assert result.version == "0.0.7"
    assert len(stage_calls) == 1


# ---------------------------------------------------------------------------
# 5. stage failure → FAILED, silent, no crash
# ---------------------------------------------------------------------------


def test_stage_failure_is_silent(tmp_path):
    result = run_self_update_check(
        resolver=None,
        tags_lister=None,
        fetcher=None,
        work_root=tmp_path / "work",
        layout=RuntimeLayout(root=tmp_path / "rt"),
        identity=INSTALLED,
        _plan=lambda identity, channel, resolver, tags_lister: _plan(
            SelfUpdateStatus.UPDATE_AVAILABLE, resolution=_resolution("0.0.7")
        ),
        _stage=lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("build failed")
        ),
    )
    assert result.status is GuiSelfUpdateStatus.FAILED
    assert "build failed" in (result.reason or "")


# ---------------------------------------------------------------------------
# 6. Pending reuse / staleness / corrupt
# ---------------------------------------------------------------------------


def test_valid_matching_pending_is_reused_without_restaging(tmp_path):
    layout = RuntimeLayout(root=tmp_path / "rt")
    _write_marker(layout, tmp_path, target="0.0.7")

    stage_calls: list = []

    result = run_self_update_check(
        resolver=None,
        tags_lister=None,
        fetcher=None,
        work_root=tmp_path / "work",
        layout=layout,
        identity=INSTALLED,
        _plan=lambda identity, channel, resolver, tags_lister: _plan(
            SelfUpdateStatus.UPDATE_AVAILABLE, resolution=_resolution("0.0.7")
        ),
        _stage=lambda *a, **k: stage_calls.append(1) or _staged(),
    )
    assert result.status is GuiSelfUpdateStatus.UPDATE_READY
    assert result.version == "0.0.7"
    assert stage_calls == [], "a valid matching pending must not re-stage"


def test_stale_pending_is_restaged(tmp_path):
    layout = RuntimeLayout(root=tmp_path / "rt")
    _write_marker(layout, tmp_path, target="0.0.5")  # stale (differs from available)

    stage_calls: list = []

    result = run_self_update_check(
        resolver=None,
        tags_lister=None,
        fetcher=None,
        work_root=tmp_path / "work",
        layout=layout,
        identity=INSTALLED,
        _plan=lambda identity, channel, resolver, tags_lister: _plan(
            SelfUpdateStatus.UPDATE_AVAILABLE, resolution=_resolution("0.0.7")
        ),
        _stage=lambda *a, **k: stage_calls.append(1) or _staged("0.0.7"),
    )
    assert result.status is GuiSelfUpdateStatus.UPDATE_READY
    assert len(stage_calls) == 1


def test_corrupt_pending_is_treated_as_absent(tmp_path):
    layout = RuntimeLayout(root=tmp_path / "rt")

    def _boom(layout_):
        raise PendingMarkerError("corrupt")

    stage_calls: list = []

    result = run_self_update_check(
        resolver=None,
        tags_lister=None,
        fetcher=None,
        work_root=tmp_path / "work",
        layout=layout,
        identity=INSTALLED,
        _plan=lambda identity, channel, resolver, tags_lister: _plan(
            SelfUpdateStatus.UPDATE_AVAILABLE, resolution=_resolution("0.0.7")
        ),
        _stage=lambda *a, **k: stage_calls.append(1) or _staged("0.0.7"),
        _load_pending=_boom,
    )
    assert result.status is GuiSelfUpdateStatus.UPDATE_READY
    assert len(stage_calls) == 1


# ---------------------------------------------------------------------------
# 7. Real plan-builder composition (resolver + tags_lister fakes)
# ---------------------------------------------------------------------------


def test_composes_with_real_plan_builder(tmp_path):
    """run_self_update_check reuses the real build_self_update_plan."""
    stage_calls: list = []

    result = run_self_update_check(
        resolver=_resolver(SHA_B),
        tags_lister=_tags(["v0.0.6", "v0.0.7"]),
        fetcher=_noop_fetcher,
        work_root=tmp_path / "work",
        layout=RuntimeLayout(root=tmp_path / "rt"),
        identity=INSTALLED,
        _stage=lambda *a, **k: stage_calls.append(1) or _staged("0.0.7"),
    )
    assert result.status is GuiSelfUpdateStatus.UPDATE_READY
    assert result.version == "0.0.7"
    assert len(stage_calls) == 1


def test_real_plan_builder_up_to_date(tmp_path):
    result = run_self_update_check(
        resolver=_resolver(SHA_A),
        tags_lister=_tags(["v0.0.6"]),
        fetcher=_noop_fetcher,
        work_root=tmp_path / "work",
        layout=RuntimeLayout(root=tmp_path / "rt"),
        identity=INSTALLED,
    )
    assert result.status is GuiSelfUpdateStatus.UP_TO_DATE


# ---------------------------------------------------------------------------
# 8. Factory binding
# ---------------------------------------------------------------------------


def test_make_check_fn_detects_identity_and_short_circuits(monkeypatch, tmp_path):
    import zealfie.selfupdate.orchestration as orch

    monkeypatch.setattr(orch, "detect_identity", lambda: SOURCE)

    fn = make_self_update_check_fn(
        resolver=_resolver(SHA_B),
        tags_lister=_tags(["v0.0.7"]),
        fetcher=_noop_fetcher,
        work_root=tmp_path / "work",
        layout=RuntimeLayout(root=tmp_path / "rt"),
    )
    result = fn()
    assert result.status is GuiSelfUpdateStatus.NOT_SUPPORTED


def test_make_apply_fn_delegates_to_activator(monkeypatch, tmp_path):
    import zealfie.selfupdate.orchestration as orch
    from zealfie.selfupdate import SelfUpdateApplyResult

    layout = RuntimeLayout(root=tmp_path / "rt")
    captured: dict = {}

    def _fake_apply(*, layout, runtime_root=None):
        captured["layout"] = layout
        captured["runtime_root"] = runtime_root
        return SelfUpdateApplyResult(ApplyStatus.APPLIED, "updated")

    monkeypatch.setattr(orch, "apply_pending_update", _fake_apply)

    fn = make_self_update_apply_fn(layout=layout)
    result = fn()
    assert result.status is ApplyStatus.APPLIED
    assert captured["layout"] is layout
    assert captured["runtime_root"] == layout.root


# ---------------------------------------------------------------------------
# 9. Restart spawns (list argv, no shell, one-shot, no loop)
# ---------------------------------------------------------------------------


def test_spawn_gui_process_list_argv_no_shell():
    import zealfie.selfupdate.restart as restart

    captured: dict = {}

    def _fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs

        class _P:
            pass

        return _P()

    assert (
        restart.spawn_gui_process(python="/py with spaces", _popen=_fake_popen)
        is True
    )

    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[0] == "/py with spaces"
    assert argv[1] == "-c"
    assert "from zealfie.gui import main" in argv[2]
    assert "shell" not in captured["kwargs"]
    assert captured["kwargs"].get("shell") is not True
    # POSIX detached via start_new_session.
    assert captured["kwargs"].get("start_new_session") is True


def test_spawn_restart_supervisor_list_argv():
    import zealfie.selfupdate.restart as restart

    captured: dict = {}

    def _fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs

        class _P:
            pass

        return _P()

    root = "/tmp/runtime root with spaces"
    assert (
        restart.spawn_restart_supervisor(
            runtime_root=root, python="/py", _popen=_fake_popen
        )
        is True
    )

    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[0] == "/py"
    assert argv[1:3] == ["-m", "zealfie.selfupdate.restart_supervisor"]
    assert argv[3:5] == ["--runtime-root", root]
    assert "shell" not in captured["kwargs"]


def test_restart_gui_after_update_prefers_supervisor():
    calls: list[str] = []

    def _supervisor(**kwargs):
        calls.append("supervisor")
        return True

    def _gui(**kwargs):
        calls.append("gui")
        return True

    from zealfie.selfupdate.restart import restart_gui_after_update

    restart_gui_after_update(
        runtime_root="/rt",
        _spawn_supervisor=_supervisor,
        _spawn_gui=_gui,
    )
    assert calls == ["supervisor"]


def test_restart_gui_after_update_falls_back_to_gui():
    calls: list[str] = []

    def _supervisor(**kwargs):
        calls.append("supervisor")
        return False

    def _gui(**kwargs):
        calls.append("gui")
        return True

    from zealfie.selfupdate.restart import restart_gui_after_update

    restart_gui_after_update(
        runtime_root="/rt",
        _spawn_supervisor=_supervisor,
        _spawn_gui=_gui,
    )
    assert calls == ["supervisor", "gui"]


def test_supervisor_wait_for_marker_clear(tmp_path, monkeypatch):
    from zealfie.selfupdate.restart_supervisor import wait_for_marker_clear

    layout = RuntimeLayout(root=tmp_path / "rt")
    _write_marker(layout, tmp_path, target="0.0.7")

    # Marker present → not cleared (immediate check, tiny timeout).
    assert wait_for_marker_clear(layout, timeout_s=0.0) is False

    # Remove marker → cleared.
    from zealfie.selfupdate.state import clear_pending_marker

    clear_pending_marker(layout)
    assert wait_for_marker_clear(layout, timeout_s=0.0) is True


def test_supervisor_main_spawns_gui_once(monkeypatch, tmp_path):
    import zealfie.selfupdate.restart_supervisor as sup
    import zealfie.selfupdate.restart as restart

    layout = RuntimeLayout(root=tmp_path / "rt")
    _write_marker(layout, tmp_path, target="0.0.7")

    spawns: list[tuple] = []

    def _fake_wait(layout_, *, timeout_s):
        return True

    def _fake_spawn(*, python=None):
        spawns.append(python)
        return True

    monkeypatch.setattr(sup, "wait_for_marker_clear", _fake_wait)
    monkeypatch.setattr(restart, "spawn_gui_process", _fake_spawn)

    code = sup.main(
        ["--runtime-root", str(tmp_path / "rt"), "--python", "/py"]
    )
    assert code == 0
    # The supervisor launches the GUI exactly once (no restart loop).
    assert spawns == ["/py"]
