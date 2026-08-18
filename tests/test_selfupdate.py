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
