"""Tests for ZA-M1-4 LOT D — ZeAlfie self-update.

Hermetic: resolver, tags lister, fetcher, wheel build/inspect, and pip
subprocess are all mocked.  No real network, no real pip install of zealfie.

Covers (mission §5):
1.  no update available (current == latest -> UP_TO_DATE);
2.  update available (commit_sha 40-hex immutable, requested_ref is the tag);
3.  immutable source identity (never a branch/abbrev);
4.  corrupt artifact rejected (version/sha256 mismatch -> stage/apply fails,
    nothing persisted);
5.  failed replacement leaves current install intact (pip fails -> error,
    marker preserved);
6.  editable/source checkout detected honestly -> NOT_SUPPORTED;
7.  no self-update performed silently (plan/check never stage/apply);
8.  restart handoff (activator refuses while a mutation lease is held; the
    apply path re-verifies sha256 before install).
"""

from __future__ import annotations

import json
import threading
from io import StringIO
from pathlib import Path

import pytest

import zealfie.cli as cli
import zealfie.selfupdate.activator as activator_mod
import zealfie.selfupdate.identity as identity_mod
import zealfie.selfupdate.verify as verify_mod
from zealfie.building import InspectedWheel
from zealfie.runtime.layout import RuntimeLayout
from zealfie.runtime.mutation_lock import RuntimeMutationLock
from zealfie.selfupdate import (
    ApplyStatus,
    InstallMode,
    SelfUpdateStatus,
    ZeAlfieIdentity,
    apply_pending_update,
    build_self_update_plan,
    compute_sha256,
    detect_identity,
    resolve_available_update,
    self_update_supported,
    stage_and_persist,
    stage_update,
)
from zealfie.selfupdate.resolver import SelfUpdateResolutionError
from zealfie.selfupdate.state import (
    PendingSelfUpdate,
    load_pending_marker,
    pending_marker_path,
    write_pending_marker,
)
from zealfie.selfupdate.verify import SelfUpdateStagingError

SHA_A = "a" * 40
SHA_B = "b" * 40


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _identity(version="0.0.6", mode=InstallMode.INSTALLED) -> ZeAlfieIdentity:
    return ZeAlfieIdentity(
        version=version,
        install_mode=mode,
        location="/site-packages/zealfie",
    )


INSTALLED = _identity()
EDITABLE = ZeAlfieIdentity("0.0.6", InstallMode.EDITABLE, "/src/zealfie")
SOURCE = ZeAlfieIdentity("0.0.6", InstallMode.SOURCE, "/repo/src/zealfie")
UNKNOWN = ZeAlfieIdentity("0.0.6", InstallMode.UNKNOWN, "/nowhere/zealfie")


def _resolver(sha):
    calls: list[tuple[str, str, str]] = []

    def resolve(owner, repo, ref):
        calls.append((owner, repo, ref))
        return sha

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


def _tags(names):
    calls: list[tuple[str, str]] = []

    def list_tags(owner, repo):
        calls.append((owner, repo))
        return list(names)

    list_tags.calls = calls  # type: ignore[attr-defined]
    return list_tags


def _noop_fetcher(owner, repo, commit_sha):
    return b"fake zip"


def _resolution(available_version="0.0.7", commit_sha=SHA_A) -> "UpdateResolution":
    from zealfie.selfupdate import UpdateResolution

    return UpdateResolution(
        current_version="0.0.6",
        available_version=available_version,
        channel="stable",
        source_owner="tinystork",
        source_repo="ZeAlfie",
        requested_ref=f"v{available_version}",
        commit_sha=commit_sha,
        up_to_date=False,
    )


def _inspected(wheel_path, *, version, distribution_name="zealfie"):
    return InspectedWheel(
        wheel_path=Path(wheel_path),
        top_level_packages=("zealfie",),
        dist_info_dir=f"{distribution_name}-{version}.dist-info",
        distribution_name=distribution_name,
        version=version,
        entry_points=(),
    )


class _FakeStaged:
    def __init__(self, stage_dir):
        self.stage_dir = Path(stage_dir)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _fake_acquire(stage_dir=Path(".")):
    def _acquire(resolved, *, fetcher, stage_root):
        return _FakeStaged(Path(stage_dir))

    return _acquire


class _Proc:
    def __init__(self, returncode, stderr=""):
        self.returncode = returncode
        self.stderr = stderr


def _write_valid_marker(layout: RuntimeLayout, tmp_path: Path):
    wheel = tmp_path / "staged.whl"
    wheel.write_bytes(b"staged wheel bytes")
    pending = PendingSelfUpdate(
        target_version="0.0.7",
        channel="stable",
        commit_sha=SHA_A,
        wheel_path=str(wheel),
        wheel_sha256=compute_sha256(wheel),
        size=wheel.stat().st_size,
        created_at="2026-01-01T00:00:00+00:00",
    )
    write_pending_marker(layout, pending)
    return pending, wheel


def _boom(label, calls):
    def _fn(*args, **kwargs):
        calls.append(label)
        raise AssertionError(f"{label} must not be called")

    return _fn


# ---------------------------------------------------------------------------
# 1. no update available
# ---------------------------------------------------------------------------


def test_no_update_available_is_up_to_date() -> None:
    plan = build_self_update_plan(
        INSTALLED, resolver=_resolver(SHA_A), tags_lister=_tags(["v0.0.6"])
    )
    assert plan.status is SelfUpdateStatus.UP_TO_DATE
    assert plan.resolution is not None
    assert plan.resolution.up_to_date is True


# ---------------------------------------------------------------------------
# 2. update available
# ---------------------------------------------------------------------------


def test_update_available_resolves_immutable_sha() -> None:
    resolver = _resolver(SHA_B)
    plan = build_self_update_plan(
        INSTALLED, resolver=resolver, tags_lister=_tags(["v0.0.6", "v0.0.7"])
    )
    assert plan.status is SelfUpdateStatus.UPDATE_AVAILABLE
    res = plan.resolution
    assert res is not None
    assert res.available_version == "0.0.7"
    assert res.requested_ref == "v0.0.7"
    assert res.commit_sha == SHA_B
    assert len(res.commit_sha) == 40
    # The resolver was asked to resolve the tag, never a branch.
    assert resolver.calls == [("tinystork", "ZeAlfie", "v0.0.7")]


# ---------------------------------------------------------------------------
# 3. immutable source identity
# ---------------------------------------------------------------------------


def test_resolution_rejects_branch_ref() -> None:
    resolver = _resolver("main")
    with pytest.raises(SelfUpdateResolutionError):
        resolve_available_update(
            INSTALLED, resolver=resolver, tags_lister=_tags(["v0.0.7"])
        )


def test_resolution_rejects_abbreviated_sha() -> None:
    resolver = _resolver("abc123")
    with pytest.raises(SelfUpdateResolutionError):
        resolve_available_update(
            INSTALLED, resolver=resolver, tags_lister=_tags(["v0.0.7"])
        )


def test_resolution_ignores_non_version_tags() -> None:
    resolver = _resolver(SHA_A)
    plan = build_self_update_plan(
        INSTALLED,
        resolver=resolver,
        tags_lister=_tags(["latest", "nightly", "v0.0.7"]),
    )
    assert plan.status is SelfUpdateStatus.UPDATE_AVAILABLE
    assert plan.resolution.requested_ref == "v0.0.7"


def test_stable_channel_ignores_beta_and_rc_tags() -> None:
    resolver = _resolver(SHA_A)
    plan = build_self_update_plan(
        INSTALLED,
        resolver=resolver,
        tags_lister=_tags(["v0.0.6", "v0.0.7-beta.1", "v0.0.8-rc.1"]),
    )
    # Highest stable is v0.0.6 (== current) -> up to date.
    assert plan.status is SelfUpdateStatus.UP_TO_DATE


def test_beta_channel_picks_beta_tags_only() -> None:
    resolver = _resolver(SHA_B)
    plan = build_self_update_plan(
        INSTALLED,
        channel="beta",
        resolver=resolver,
        tags_lister=_tags(["v0.0.6", "v0.0.7-beta.1", "v0.0.7-beta.2"]),
    )
    assert plan.status is SelfUpdateStatus.UPDATE_AVAILABLE
    assert plan.resolution.requested_ref == "v0.0.7-beta.2"


# ---------------------------------------------------------------------------
# 4. corrupt artifact rejected
# ---------------------------------------------------------------------------


def test_stage_rejects_wheel_version_mismatch(monkeypatch, tmp_path) -> None:
    resolution = _resolution("0.0.7")
    wheel = tmp_path / "zealfie-0.0.7-py3-none-any.whl"
    wheel.write_bytes(b"fake wheel bytes")
    monkeypatch.setattr(verify_mod, "acquire_source", _fake_acquire())
    monkeypatch.setattr(
        verify_mod, "build_wheel", lambda src, output_dir=None: wheel
    )
    monkeypatch.setattr(
        verify_mod, "inspect_wheel", lambda wp: _inspected(wp, version="0.0.8")
    )
    with pytest.raises(SelfUpdateStagingError, match="version mismatch"):
        stage_update(resolution, fetcher=_noop_fetcher, work_root=tmp_path / "work")


def test_stage_and_persist_nothing_written_on_mismatch(monkeypatch, tmp_path) -> None:
    layout = RuntimeLayout(root=tmp_path / "rt")
    resolution = _resolution("0.0.7")
    wheel = tmp_path / "w.whl"
    wheel.write_bytes(b"bytes")
    monkeypatch.setattr(verify_mod, "acquire_source", _fake_acquire())
    monkeypatch.setattr(
        verify_mod, "build_wheel", lambda src, output_dir=None: wheel
    )
    monkeypatch.setattr(
        verify_mod, "inspect_wheel", lambda wp: _inspected(wp, version="0.0.8")
    )
    with pytest.raises(SelfUpdateStagingError):
        stage_and_persist(
            resolution,
            fetcher=_noop_fetcher,
            work_root=tmp_path / "work",
            layout=layout,
        )
    assert not pending_marker_path(layout).exists()


def test_stage_rejects_wrong_distribution(monkeypatch, tmp_path) -> None:
    resolution = _resolution("0.0.7")
    wheel = tmp_path / "w.whl"
    wheel.write_bytes(b"bytes")
    monkeypatch.setattr(verify_mod, "acquire_source", _fake_acquire())
    monkeypatch.setattr(
        verify_mod, "build_wheel", lambda src, output_dir=None: wheel
    )
    monkeypatch.setattr(
        verify_mod,
        "inspect_wheel",
        lambda wp: _inspected(wp, version="0.0.7", distribution_name="ze-solver"),
    )
    with pytest.raises(SelfUpdateStagingError, match="distribution mismatch"):
        stage_update(resolution, fetcher=_noop_fetcher, work_root=tmp_path / "work")


def test_stage_success_records_sha_and_size(monkeypatch, tmp_path) -> None:
    resolution = _resolution("0.0.7")
    wheel = tmp_path / "w.whl"
    wheel.write_bytes(b"hello wheel bytes")
    monkeypatch.setattr(verify_mod, "acquire_source", _fake_acquire())
    monkeypatch.setattr(
        verify_mod, "build_wheel", lambda src, output_dir=None: wheel
    )
    monkeypatch.setattr(
        verify_mod, "inspect_wheel", lambda wp: _inspected(wp, version="0.0.7")
    )
    staged = stage_update(
        resolution, fetcher=_noop_fetcher, work_root=tmp_path / "work"
    )
    assert staged.wheel_version == "0.0.7"
    assert staged.wheel_sha256 == compute_sha256(wheel)
    assert staged.size == wheel.stat().st_size


# ---------------------------------------------------------------------------
# 5. failed replacement leaves current install intact
# ---------------------------------------------------------------------------


def test_apply_failed_pip_preserves_marker(monkeypatch, tmp_path) -> None:
    layout = RuntimeLayout(root=tmp_path / "rt")
    _write_valid_marker(layout, tmp_path)

    monkeypatch.setattr(
        activator_mod, "_run_pip_install", lambda wp: _Proc(1, stderr="boom")
    )
    result = apply_pending_update(
        layout=layout, runtime_root=tmp_path / "rtroot"
    )
    assert result.status is ApplyStatus.FAILED
    assert "left untouched" in result.message
    # Marker preserved; the current install is still usable.
    assert pending_marker_path(layout).exists()
    assert load_pending_marker(layout) is not None


def test_apply_success_clears_marker(monkeypatch, tmp_path) -> None:
    layout = RuntimeLayout(root=tmp_path / "rt")
    pending, wheel = _write_valid_marker(layout, tmp_path)

    calls: list[Path] = []

    def _fake_pip(wp):
        calls.append(Path(wp))
        return _Proc(0)

    monkeypatch.setattr(activator_mod, "_run_pip_install", _fake_pip)
    # The freshly-installed version is verified to equal the target before
    # the marker is cleared (ZA-M1-4.1).  Mock the fresh-subprocess check.
    monkeypatch.setattr(activator_mod, "_verify_installed_version", lambda tv: None)
    result = apply_pending_update(
        layout=layout, runtime_root=tmp_path / "rtroot"
    )
    assert result.status is ApplyStatus.APPLIED
    assert calls == [wheel]
    assert not pending_marker_path(layout).exists()


# ---------------------------------------------------------------------------
# 6. editable / source checkout detected honestly
# ---------------------------------------------------------------------------


def test_editable_and_source_and_unknown_are_not_supported() -> None:
    for identity in (EDITABLE, SOURCE, UNKNOWN):
        supported, reason = self_update_supported(identity)
        assert supported is False
        assert reason

    supported, reason = self_update_supported(INSTALLED)
    assert supported is True
    assert reason is None


def test_plan_not_supported_for_editable() -> None:
    plan = build_self_update_plan(
        EDITABLE, resolver=_resolver(SHA_A), tags_lister=_tags(["v0.0.7"])
    )
    assert plan.status is SelfUpdateStatus.NOT_SUPPORTED
    assert "editable" in (plan.reason or "").lower()


def test_detect_identity_source_when_git_ancestor(monkeypatch, tmp_path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    pkg = repo / "src" / "zealfie"
    pkg.mkdir(parents=True)
    monkeypatch.setattr(identity_mod, "_package_root", lambda: pkg)
    monkeypatch.setattr(identity_mod, "_has_editable_marker", lambda: False)
    monkeypatch.setattr(identity_mod, "_under_site_packages", lambda loc: False)
    assert detect_identity().install_mode is InstallMode.SOURCE


def test_detect_identity_editable_marker(monkeypatch, tmp_path) -> None:
    pkg = tmp_path / "src" / "zealfie"
    pkg.mkdir(parents=True)
    monkeypatch.setattr(identity_mod, "_package_root", lambda: pkg)
    monkeypatch.setattr(identity_mod, "_has_git_ancestor", lambda loc: False)
    monkeypatch.setattr(identity_mod, "_has_editable_marker", lambda: True)
    monkeypatch.setattr(identity_mod, "_under_site_packages", lambda loc: False)
    assert detect_identity().install_mode is InstallMode.EDITABLE


class _FakeDist:
    def __init__(self, direct_url=None, files=None):
        self._direct_url = direct_url
        self._files = files

    def read_text(self, name):
        if name == "direct_url.json":
            return self._direct_url
        return None

    @property
    def files(self):
        return self._files


def test_editable_marker_from_direct_url(monkeypatch) -> None:
    dist = _FakeDist(
        direct_url=json.dumps(
            {"url": "file:///src/zealfie", "dir_info": {"editable": True}}
        )
    )
    monkeypatch.setattr(identity_mod._metadata, "distribution", lambda name: dist)
    assert identity_mod._has_editable_marker() is True


def test_no_editable_marker_for_plain_dist(monkeypatch) -> None:
    dist = _FakeDist(direct_url=None, files=[])
    monkeypatch.setattr(identity_mod._metadata, "distribution", lambda name: dist)
    assert identity_mod._has_editable_marker() is False


# ---------------------------------------------------------------------------
# 7. no self-update performed silently
# ---------------------------------------------------------------------------


def test_build_plan_never_stages_or_applies(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        verify_mod, "stage_update", _boom("stage_update", calls)
    )
    monkeypatch.setattr(
        activator_mod, "apply_pending_update", _boom("apply_pending_update", calls)
    )
    plan = build_self_update_plan(
        INSTALLED, resolver=_resolver(SHA_A), tags_lister=_tags(["v0.0.7"])
    )
    assert plan.status is SelfUpdateStatus.UPDATE_AVAILABLE
    assert calls == []


def test_cli_check_never_stages_or_applies(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        cli, "detect_identity", lambda: INSTALLED
    )
    monkeypatch.setattr(
        cli,
        "_make_self_update_deps",
        lambda: (
            _resolver(SHA_A),
            _tags(["v0.0.7"]),
            _noop_fetcher,
            tmp_path / "work",
        ),
    )
    calls: list[str] = []
    monkeypatch.setattr(cli, "stage_and_persist", _boom("stage", calls))
    monkeypatch.setattr(cli, "apply_pending_update", _boom("apply", calls))
    out = StringIO()
    code = cli.run(["self-update", "check"], stdout=out)
    assert code == 0
    assert "UPDATE_AVAILABLE" in out.getvalue()
    assert "Available version: 0.0.7" in out.getvalue()
    assert calls == []


def test_cli_check_up_to_date_returns_zero(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cli, "detect_identity", lambda: INSTALLED)
    monkeypatch.setattr(
        cli,
        "_make_self_update_deps",
        lambda: (
            _resolver(SHA_A),
            _tags(["v0.0.6"]),
            _noop_fetcher,
            tmp_path / "work",
        ),
    )
    out = StringIO()
    code = cli.run(["self-update", "check"], stdout=out)
    assert code == 0
    assert "UP_TO_DATE" in out.getvalue()


def test_cli_check_not_supported_returns_zero(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cli, "detect_identity", lambda: SOURCE)
    monkeypatch.setattr(
        cli,
        "_make_self_update_deps",
        lambda: (
            _resolver(SHA_A),
            _tags(["v0.0.7"]),
            _noop_fetcher,
            tmp_path / "work",
        ),
    )
    out = StringIO()
    code = cli.run(["self-update", "check"], stdout=out)
    assert code == 0
    assert "NOT_SUPPORTED" in out.getvalue()


def test_cli_check_failed_returns_nonzero(monkeypatch, tmp_path) -> None:
    from zealfie.sources import SourceResolutionError

    def _failing_tags(owner, repo):
        raise SourceResolutionError("network down")

    monkeypatch.setattr(cli, "detect_identity", lambda: INSTALLED)
    monkeypatch.setattr(
        cli,
        "_make_self_update_deps",
        lambda: (
            _resolver(SHA_A),
            _failing_tags,
            _noop_fetcher,
            tmp_path / "work",
        ),
    )
    import sys

    backup = sys.stderr
    try:
        sys.stderr = err = StringIO()
        out = StringIO()
        code = cli.run(["self-update", "check"], stdout=out)
        assert code != 0
        assert "CHECK_FAILED" in err.getvalue()
    finally:
        sys.stderr = backup


# ---------------------------------------------------------------------------
# 8. restart handoff
# ---------------------------------------------------------------------------


class _FakeBusyLock:
    def __init__(self, root):
        self.root = root

    def probe_busy(self):
        return {"operation": "product-install", "pid": 1234}


def test_apply_refuses_while_mutation_lease_held(monkeypatch, tmp_path) -> None:
    layout = RuntimeLayout(root=tmp_path / "rt")
    _write_valid_marker(layout, tmp_path)
    monkeypatch.setattr(activator_mod, "RuntimeMutationLock", _FakeBusyLock)
    result = apply_pending_update(
        layout=layout, runtime_root=tmp_path / "rtroot"
    )
    assert result.status is ApplyStatus.BUSY
    assert "another ZeAlfie mutation" in result.message


def test_apply_reverifies_sha256_before_install(monkeypatch, tmp_path) -> None:
    layout = RuntimeLayout(root=tmp_path / "rt")
    pending, wheel = _write_valid_marker(layout, tmp_path)
    # Tamper with the staged wheel after the marker was recorded.
    data = wheel.read_bytes()
    wheel.write_bytes(b"\x00" + data[1:])  # same size, different content

    calls: list[Path] = []
    monkeypatch.setattr(
        activator_mod,
        "_run_pip_install",
        lambda wp: (calls.append(Path(wp)) or _Proc(0)),
    )
    result = apply_pending_update(
        layout=layout, runtime_root=tmp_path / "rtroot"
    )
    assert result.status is ApplyStatus.FAILED
    assert "SHA-256 mismatch" in result.message
    # pip was never invoked, and the marker is preserved.
    assert calls == []
    assert load_pending_marker(layout) is not None


def test_apply_reverifies_size_before_install(monkeypatch, tmp_path) -> None:
    layout = RuntimeLayout(root=tmp_path / "rt")
    pending, wheel = _write_valid_marker(layout, tmp_path)
    wheel.write_bytes(b"short")  # different size than recorded

    monkeypatch.setattr(
        activator_mod, "_run_pip_install", lambda wp: _Proc(0)
    )
    result = apply_pending_update(
        layout=layout, runtime_root=tmp_path / "rtroot"
    )
    assert result.status is ApplyStatus.FAILED
    assert "size mismatch" in result.message


def test_apply_no_pending_returns_no_pending(tmp_path) -> None:
    layout = RuntimeLayout(root=tmp_path / "rt")
    result = apply_pending_update(
        layout=layout, runtime_root=tmp_path / "rtroot"
    )
    assert result.status is ApplyStatus.NO_PENDING


def test_apply_corrupt_marker_returns_failed(tmp_path) -> None:
    layout = RuntimeLayout(root=tmp_path / "rt")
    pending_marker_path(layout).parent.mkdir(parents=True, exist_ok=True)
    pending_marker_path(layout).write_text("{not valid json", encoding="utf-8")
    result = apply_pending_update(
        layout=layout, runtime_root=tmp_path / "rtroot"
    )
    assert result.status is ApplyStatus.FAILED
    assert "corrupt" in result.message


def test_apply_not_supported_on_non_linux(monkeypatch, tmp_path) -> None:
    layout = RuntimeLayout(root=tmp_path / "rt")
    _write_valid_marker(layout, tmp_path)
    monkeypatch.setattr(activator_mod.sys, "platform", "darwin")
    result = apply_pending_update(
        layout=layout, runtime_root=tmp_path / "rtroot"
    )
    assert result.status is ApplyStatus.NOT_SUPPORTED_ON_PLATFORM


def _write_marker_with_target(layout: RuntimeLayout, tmp_path: Path, target: str):
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


def test_apply_refuses_downgrade(monkeypatch, tmp_path) -> None:
    """MINOR-3: a stale marker whose target_version is lower than the
    installed version must never silently downgrade; pip is not run."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    _write_marker_with_target(layout, tmp_path, "0.0.5")

    pip_called: list[Path] = []
    monkeypatch.setattr(
        activator_mod,
        "_run_pip_install",
        lambda wp: (pip_called.append(Path(wp)) or _Proc(0)),
    )
    result = apply_pending_update(
        layout=layout,
        runtime_root=tmp_path / "rtroot",
        installed_version="0.0.9",
    )
    assert result.status is ApplyStatus.REFUSE_DOWNGRADE
    assert "lower than the installed version" in result.message
    assert pip_called == []
    # Refused, not applied: marker preserved.
    assert load_pending_marker(layout) is not None


def test_apply_refuses_unparseable_version(monkeypatch, tmp_path) -> None:
    """MINOR-3: unparseable versions fail closed (refuse, never proceed)."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    _write_marker_with_target(layout, tmp_path, "not-a-version")

    pip_called: list[Path] = []
    monkeypatch.setattr(
        activator_mod,
        "_run_pip_install",
        lambda wp: (pip_called.append(Path(wp)) or _Proc(0)),
    )
    result = apply_pending_update(
        layout=layout,
        runtime_root=tmp_path / "rtroot",
        installed_version="0.0.9",
    )
    assert result.status is ApplyStatus.REFUSE_DOWNGRADE
    assert "cannot compare versions" in result.message
    assert pip_called == []


# ---------------------------------------------------------------------------
# CLI apply wiring
# ---------------------------------------------------------------------------


def test_cli_apply_delegates_to_activator(monkeypatch, tmp_path) -> None:
    from zealfie.selfupdate import SelfUpdateApplyResult

    layout = RuntimeLayout(root=tmp_path / "rt")
    monkeypatch.setattr(cli, "default_runtime_layout", lambda: layout)
    monkeypatch.setattr(
        cli,
        "apply_pending_update",
        lambda *, layout, runtime_root=None: SelfUpdateApplyResult(
            ApplyStatus.APPLIED, "ZeAlfie updated to 0.0.7"
        ),
    )
    out = StringIO()
    code = cli.run(["self-update", "apply"], stdout=out)
    assert code == 0
    assert "ZeAlfie updated to 0.0.7" in out.getvalue()


def test_cli_apply_busy_returns_four(monkeypatch, tmp_path) -> None:
    from zealfie.selfupdate import SelfUpdateApplyResult

    layout = RuntimeLayout(root=tmp_path / "rt")
    monkeypatch.setattr(cli, "default_runtime_layout", lambda: layout)
    monkeypatch.setattr(
        cli,
        "apply_pending_update",
        lambda *, layout, runtime_root=None: SelfUpdateApplyResult(
            ApplyStatus.BUSY, "busy"
        ),
    )
    import sys

    backup = sys.stderr
    try:
        sys.stderr = err = StringIO()
        code = cli.run(["self-update", "apply"], stdout=StringIO())
        assert code == 4
        assert "busy" in err.getvalue()
    finally:
        sys.stderr = backup


def test_cli_self_update_requires_subcommand(monkeypatch) -> None:
    import sys

    backup = sys.stderr
    try:
        sys.stderr = err = StringIO()
        code = cli.run(["self-update"], stdout=StringIO())
        assert code == 2
        assert "requires a subcommand" in err.getvalue()
    finally:
        sys.stderr = backup


# ---------------------------------------------------------------------------
# ZA-M1-4.1: verified-replace core, Windows handoff, helper, no-secret
# ---------------------------------------------------------------------------


def test_apply_missing_wheel_fails_closed(monkeypatch, tmp_path) -> None:
    """Fail closed when the staged wheel is absent: no install, marker kept."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    _pending, wheel = _write_valid_marker(layout, tmp_path)
    wheel.unlink()

    pip_called: list[Path] = []
    monkeypatch.setattr(
        activator_mod,
        "_run_pip_install",
        lambda wp: (pip_called.append(Path(wp)) or _Proc(0)),
    )
    result = apply_pending_update(
        layout=layout, runtime_root=tmp_path / "rtroot"
    )
    assert result.status is ApplyStatus.FAILED
    assert "missing" in result.message
    assert pip_called == []
    assert pending_marker_path(layout).exists()


def test_verify_installed_version_returns_none_on_match(monkeypatch) -> None:
    class _P:
        returncode = 0
        stdout = "0.0.7\n"
        stderr = ""

    monkeypatch.setattr(
        activator_mod.subprocess, "run", lambda argv, **kw: _P()
    )
    assert activator_mod._verify_installed_version("0.0.7") is None


def test_verify_installed_version_returns_failed_on_mismatch(monkeypatch) -> None:
    class _P:
        returncode = 0
        stdout = "0.0.6\n"
        stderr = ""

    monkeypatch.setattr(
        activator_mod.subprocess, "run", lambda argv, **kw: _P()
    )
    result = activator_mod._verify_installed_version("0.0.7")
    assert result is not None
    assert result.status is ApplyStatus.FAILED
    assert "does not match" in result.message


def test_verify_installed_version_returns_failed_on_subprocess_error(
    monkeypatch,
) -> None:
    class _P:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(
        activator_mod.subprocess, "run", lambda argv, **kw: _P()
    )
    result = activator_mod._verify_installed_version("0.0.7")
    assert result.status is ApplyStatus.FAILED
    assert "cannot read the installed ZeAlfie version" in result.message


def test_apply_keeps_marker_when_version_verification_fails(
    monkeypatch, tmp_path
) -> None:
    """A version mismatch after install is fail-closed: marker NOT cleared."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    _write_valid_marker(layout, tmp_path)

    monkeypatch.setattr(activator_mod, "_run_pip_install", lambda wp: _Proc(0))
    monkeypatch.setattr(
        activator_mod,
        "_verify_installed_version",
        lambda tv: activator_mod.SelfUpdateApplyResult(
            ApplyStatus.FAILED,
            "version verification failed: installed version '0.0.6' does "
            "not match the staged target '0.0.7'",
        ),
    )
    result = apply_pending_update(
        layout=layout, runtime_root=tmp_path / "rtroot"
    )
    assert result.status is ApplyStatus.FAILED
    assert "version verification failed" in result.message
    assert pending_marker_path(layout).exists()


def test_apply_verified_wheel_busy_when_lease_held(monkeypatch, tmp_path) -> None:
    """The helper core refuses (BUSY) when the mutation lease is held."""
    from zealfie.runtime.mutation_lock import RuntimeMutationBusyError

    layout = RuntimeLayout(root=tmp_path / "rt")
    pending, wheel = _write_valid_marker(layout, tmp_path)

    class _BusyLock:
        def __init__(self, root):
            pass

        def acquire(self, operation):
            raise RuntimeMutationBusyError(lock_path=Path("/whatever"))

    monkeypatch.setattr(activator_mod, "RuntimeMutationLock", _BusyLock)
    pip_called: list[Path] = []
    monkeypatch.setattr(
        activator_mod,
        "_run_pip_install",
        lambda wp: (pip_called.append(Path(wp)) or _Proc(0)),
    )

    result = activator_mod._apply_verified_wheel(
        pending, wheel, tmp_path / "rtroot", layout
    )
    assert result.status is ApplyStatus.BUSY
    assert pip_called == []
    assert pending_marker_path(layout).exists()


def test_run_pip_install_uses_list_argv_no_shell(monkeypatch, tmp_path) -> None:
    """pip install is invoked with list argv (no shell); space path intact."""
    import sys

    wheel = tmp_path / "dir with spaces" / "zealfie wheel.whl"
    wheel.parent.mkdir(parents=True)
    wheel.write_bytes(b"x")
    captured: dict = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _Proc(0)

    monkeypatch.setattr(activator_mod.subprocess, "run", _fake_run)
    activator_mod._run_pip_install(wheel)

    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[0] == sys.executable
    assert argv[1:6] == ["-m", "pip", "install", "--no-deps", "--no-index"]
    assert len(argv) == 7
    # The space-containing path is a single argv element.
    assert argv[6] == str(wheel)
    assert " " in argv[6]
    # No shell anywhere.
    assert "shell" not in captured["kwargs"]
    assert captured["kwargs"].get("shell") is not True


def _assert_no_secret(msg: str) -> None:
    assert "GITHUB_TOKEN" not in msg
    assert "ghp_" not in msg
    assert "user:pass" not in msg
    assert "proxy=" not in msg.lower()


def test_no_secret_in_diagnostics(monkeypatch, tmp_path) -> None:
    """Apply/handoff/helper messages never leak GITHUB_TOKEN / proxy creds."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    _write_valid_marker(layout, tmp_path)

    # Handoff message.
    monkeypatch.setattr(activator_mod.sys, "platform", "win32")
    monkeypatch.setattr(
        activator_mod, "spawn_windows_helper", lambda **kwargs: True
    )
    result = apply_pending_update(
        layout=layout, runtime_root=tmp_path / "rtroot"
    )
    assert result.status is ApplyStatus.HANDOFF_STARTED
    _assert_no_secret(result.message)

    # Spawn-failure message.
    monkeypatch.setattr(
        activator_mod, "spawn_windows_helper", lambda **kwargs: False
    )
    result = apply_pending_update(
        layout=layout, runtime_root=tmp_path / "rtroot"
    )
    assert result.status is ApplyStatus.FAILED
    _assert_no_secret(result.message)

    # Version-verification mismatch message (helper/apply diagnostic).
    monkeypatch.setattr(activator_mod.sys, "platform", "linux")
    mismatch = activator_mod.SelfUpdateApplyResult(
        ApplyStatus.FAILED,
        "version verification failed: installed version '0.0.6' does not "
        "match the staged target '0.0.7'",
    )
    _assert_no_secret(mismatch.message)


def test_apply_win32_handoff_spawns_helper(monkeypatch, tmp_path) -> None:
    """On Windows the activator hands off instead of pip-installing."""
    import os

    layout = RuntimeLayout(root=tmp_path / "rt")
    _write_valid_marker(layout, tmp_path)

    spawn_calls: list[tuple] = []
    pip_called: list[Path] = []

    def _fake_spawn(*, runtime_root, caller_pid, python=None):
        spawn_calls.append((runtime_root, caller_pid))
        return True

    monkeypatch.setattr(activator_mod.sys, "platform", "win32")
    monkeypatch.setattr(activator_mod, "spawn_windows_helper", _fake_spawn)
    monkeypatch.setattr(
        activator_mod,
        "_run_pip_install",
        lambda wp: (pip_called.append(Path(wp)) or _Proc(0)),
    )

    result = apply_pending_update(
        layout=layout, runtime_root=tmp_path / "rtroot"
    )
    assert result.status is ApplyStatus.HANDOFF_STARTED
    assert "handoff started" in result.message
    assert spawn_calls and spawn_calls[0][0] == Path(tmp_path / "rtroot")
    assert spawn_calls[0][1] == os.getpid()
    # The caller did NOT pip-install in-process.
    assert pip_called == []
    # The marker is preserved; the helper clears it after the caller exits.
    assert pending_marker_path(layout).exists()


def test_apply_win32_spawn_failure_returns_failed(monkeypatch, tmp_path) -> None:
    layout = RuntimeLayout(root=tmp_path / "rt")
    _write_valid_marker(layout, tmp_path)

    monkeypatch.setattr(activator_mod.sys, "platform", "win32")
    monkeypatch.setattr(
        activator_mod, "spawn_windows_helper", lambda **kwargs: False
    )
    result = apply_pending_update(
        layout=layout, runtime_root=tmp_path / "rtroot"
    )
    assert result.status is ApplyStatus.FAILED
    assert "failed to spawn" in result.message
    assert pending_marker_path(layout).exists()


def test_helper_waits_for_caller_before_install(monkeypatch, tmp_path) -> None:
    """The helper calls the injected wait seam BEFORE applying the update.

    This proves call ordering and the fail-closed return-value contract: the
    seam must return True (caller confirmed exited) for the apply to proceed.
    It does not exercise real Windows handle waiting.
    """
    import zealfie.selfupdate.windows_helper as wh

    layout = RuntimeLayout(root=tmp_path / "rt")
    _write_valid_marker(layout, tmp_path)

    events: list[str] = []

    def _wait(*args, **kwargs):
        events.append("wait")
        return True

    def _fake_apply(pending, wheel_path, root, layout_):
        events.append("apply")
        return activator_mod.SelfUpdateApplyResult(
            ApplyStatus.APPLIED, "ZeAlfie updated to 0.0.7"
        )

    monkeypatch.setattr(wh, "wait_for_caller_exit", _wait)
    monkeypatch.setattr(wh, "_apply_verified_wheel", _fake_apply)
    monkeypatch.setattr(wh, "_installed_zealfie_version", lambda: "0.0.6")

    code = wh.main(
        ["--caller-pid", "12345", "--runtime-root", str(tmp_path / "rt")]
    )
    assert events == ["wait", "apply"]
    assert code == 0


def test_wait_for_caller_exit_uses_wait_impl() -> None:
    import zealfie.selfupdate.windows_helper as wh

    seen: list[tuple] = []
    result = wh.wait_for_caller_exit(
        42,
        timeout_s=1.5,
        _wait_impl=lambda pid, ts: seen.append((pid, ts)) or True,
    )
    assert seen == [(42, 1.5)]
    assert result is True


def test_wait_for_caller_exit_posix_returns_on_missing_pid() -> None:
    import zealfie.selfupdate.windows_helper as wh

    # A very large pid cannot exist; polling must return without raising and
    # report the caller as confirmed exited.
    assert wh._wait_for_caller_exit_posix(999999999, 0.2) is True


def test_spawn_windows_helper_list_argv_no_shell(monkeypatch, tmp_path) -> None:
    import zealfie.selfupdate.handoff as handoff

    captured: dict = {}

    def _fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs

        class _P:
            pass

        return _P()

    monkeypatch.setattr(handoff.subprocess, "Popen", _fake_popen)
    root = tmp_path / "runtime root with spaces"
    ok = handoff.spawn_windows_helper(
        runtime_root=root,
        caller_pid=777,
        python="/path/to/python with spaces",
    )
    assert ok is True

    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[0] == "/path/to/python with spaces"
    assert argv[1:3] == ["-m", "zealfie.selfupdate.windows_helper"]
    assert argv[3:5] == ["--caller-pid", "777"]
    assert argv[5:] == ["--runtime-root", str(root)]
    # Space-containing path is a single argv element.
    assert argv.index(str(root)) == 6
    assert " " in argv[6]

    kwargs = captured["kwargs"]
    assert "shell" not in kwargs
    assert kwargs.get("shell") is not True
    # POSIX branch: detached via start_new_session, no creationflags.
    assert kwargs.get("start_new_session") is True
    assert "creationflags" not in kwargs


def test_spawn_windows_helper_win32_creationflags_guarded(
    monkeypatch, tmp_path
) -> None:
    import zealfie.selfupdate.handoff as handoff

    captured: dict = {}

    def _fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs

        class _P:
            pass

        return _P()

    monkeypatch.setattr(handoff.sys, "platform", "win32")
    monkeypatch.setattr(handoff.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(
        handoff.subprocess, "DETACHED_PROCESS", 0x00000008, raising=False
    )
    monkeypatch.setattr(
        handoff.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False
    )
    monkeypatch.setattr(
        handoff.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False
    )

    ok = handoff.spawn_windows_helper(runtime_root=tmp_path, caller_pid=1)
    assert ok is True

    kwargs = captured["kwargs"]
    assert kwargs.get("creationflags") == (
        0x00000008 | 0x00000200 | 0x08000000
    )
    assert kwargs.get("close_fds") is True
    assert "shell" not in kwargs


# ---------------------------------------------------------------------------
# ZA-M1-4.1 corrective: fail-closed Windows caller wait
# ---------------------------------------------------------------------------


class _FakeKernel32Fn:
    """Minimal ctypes-function stand-in: callable + settable restype/argtypes."""

    def __init__(self, fn):
        self._fn = fn

    def __call__(self, *args, **kwargs):
        return self._fn(*args, **kwargs)


def _fake_kernel32(handle, wait_rc, last_error=0):
    class _K:
        pass

    k = _K()
    k.OpenProcess = _FakeKernel32Fn(lambda access, inherit, pid: handle)
    k.WaitForSingleObject = _FakeKernel32Fn(lambda h, ms: wait_rc)
    k.CloseHandle = _FakeKernel32Fn(lambda h: None)
    k.GetLastError = _FakeKernel32Fn(lambda: last_error)
    return k


def test_wait_windows_return_code_semantics() -> None:
    import ctypes

    import zealfie.selfupdate.windows_helper as wh

    sentinel = 0x1234

    # WAIT_OBJECT_0 (0) -> confirmed exited.
    k = _fake_kernel32(handle=sentinel, wait_rc=0x00000000)
    assert wh._wait_for_caller_exit_windows(123, 1.0, _kernel32=k) is True
    # 64-bit HANDLE-safe signature was set on the (fake) kernel32 functions.
    assert k.OpenProcess.restype is ctypes.c_void_p
    assert k.WaitForSingleObject.restype is ctypes.c_uint32
    assert k.CloseHandle.argtypes == [ctypes.c_void_p]
    assert k.GetLastError.restype is ctypes.c_uint32

    # WAIT_TIMEOUT (0x102) -> not confirmed.
    assert (
        wh._wait_for_caller_exit_windows(
            123, 1.0, _kernel32=_fake_kernel32(handle=sentinel, wait_rc=0x00000102)
        )
        is False
    )

    # WAIT_FAILED (0xFFFFFFFF) -> not confirmed.
    assert (
        wh._wait_for_caller_exit_windows(
            123, 1.0, _kernel32=_fake_kernel32(handle=sentinel, wait_rc=0xFFFFFFFF)
        )
        is False
    )

    # Null handle + ERROR_INVALID_PARAMETER (0x57) -> caller gone -> confirmed.
    assert (
        wh._wait_for_caller_exit_windows(
            123, 1.0, _kernel32=_fake_kernel32(handle=None, wait_rc=0, last_error=0x57)
        )
        is True
    )

    # Null handle + any other error -> cannot confirm -> fail closed.
    assert (
        wh._wait_for_caller_exit_windows(
            123, 1.0, _kernel32=_fake_kernel32(handle=None, wait_rc=0, last_error=5)
        )
        is False
    )


def test_helper_aborts_when_wait_not_confirmed(monkeypatch, tmp_path) -> None:
    """Fail-closed: an unconfirmed caller exit must NOT apply (marker kept)."""
    import sys

    import zealfie.selfupdate.windows_helper as wh

    layout = RuntimeLayout(root=tmp_path / "rt")
    _write_valid_marker(layout, tmp_path)

    applied: list[object] = []

    def _fake_apply(pending, wheel_path, root, layout_):
        applied.append(pending)
        return activator_mod.SelfUpdateApplyResult(
            ApplyStatus.APPLIED, "should not happen"
        )

    monkeypatch.setattr(wh, "wait_for_caller_exit", lambda *a, **k: False)
    monkeypatch.setattr(wh, "_apply_verified_wheel", _fake_apply)

    backup = sys.stderr
    try:
        sys.stderr = err = StringIO()
        code = wh.main(
            ["--caller-pid", "12345", "--runtime-root", str(tmp_path / "rt")]
        )
    finally:
        sys.stderr = backup

    assert code != 0
    assert applied == []
    assert "did not confirm exit" in err.getvalue()
    assert pending_marker_path(layout).exists()


def test_helper_proceeds_when_caller_confirmed_exited(monkeypatch, tmp_path) -> None:
    """Fail-closed happy path: confirmed exit -> helper applies and returns 0."""
    import zealfie.selfupdate.windows_helper as wh

    layout = RuntimeLayout(root=tmp_path / "rt")
    _write_valid_marker(layout, tmp_path)

    events: list[str] = []

    def _fake_apply(pending, wheel_path, root, layout_):
        events.append("apply")
        return activator_mod.SelfUpdateApplyResult(
            ApplyStatus.APPLIED, "ZeAlfie updated to 0.0.7"
        )

    monkeypatch.setattr(wh, "wait_for_caller_exit", lambda *a, **k: True)
    monkeypatch.setattr(wh, "_apply_verified_wheel", _fake_apply)
    monkeypatch.setattr(wh, "_installed_zealfie_version", lambda: "0.0.6")

    code = wh.main(
        ["--caller-pid", "12345", "--runtime-root", str(tmp_path / "rt")]
    )
    assert code == 0
    assert events == ["apply"]


def test_verify_installed_version_timeout_fails_closed(monkeypatch) -> None:
    """A hung verifier subprocess must not block the apply: timeout -> FAILED."""
    import subprocess as sp

    def _timeout(argv, **kwargs):
        raise sp.TimeoutExpired(cmd=argv, timeout=60)

    monkeypatch.setattr(activator_mod.subprocess, "run", _timeout)
    result = activator_mod._verify_installed_version("0.0.7")
    assert result is not None
    assert result.status is ApplyStatus.FAILED
    assert "timed out" in result.message
    assert "marker left in place" in result.message
