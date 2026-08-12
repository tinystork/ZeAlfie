"""Tests for M1-2D.6 — structured install progress emitted by the service.

Covers the backend contract end to end at the service layer, without any
Qt, network, pip, or venv:

1. ``install_product`` without a callback behaves exactly as before.
2. With a callback, milestones arrive in logical order with monotone
   0..100 percents and 100 only on success.
3. Failures (result or exception) never emit COMPLETED / 100 and the
   original result/exception is preserved.
4. ``prepare_product_artifact`` emits resolve → download → build.

All tests are FAST.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from zealfie.app import (
    InstallPhase,
    InstallProgress,
    PreparedProductArtifact,
    ProductCatalog,
    ProductDescriptor,
    SelectionStore,
    ZeAlfieService,
)
from zealfie.components.model import EntryPointContract
from zealfie.releases.model import VerifiedArtifact
from zealfie.runtime.model import DeploymentResult, RuntimeState, RuntimeStatus
from zealfie.sources import RemoteSource, ResolvedSource


VALID_SHA = "d4a0f1e2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8"
BRANCH_REF = "main"

_PHASE_ORDER = list(InstallPhase)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _planning_catalog(product_id="zesolver", dist_name="zealfie-solver") -> ProductCatalog:
    return ProductCatalog((
        ProductDescriptor(
            product_id=product_id,
            display_name=product_id.capitalize(),
            distribution_name=dist_name,
            launch_entry_points=(EntryPointContract("gui_scripts", "zesolver"),),
            required_extras=(),
            remote_source=RemoteSource(
                owner="tinystork", repo="ZeSolver", ref=BRANCH_REF,
            ),
        ),
    ))


def _make_ppa(wheel_path: Path, product_id="zesolver", version="0.0.1") -> PreparedProductArtifact:
    remote = RemoteSource(owner="tinystork", repo="ZeSolver", ref=BRANCH_REF)
    resolved = ResolvedSource(source=remote, commit_sha=VALID_SHA)
    return PreparedProductArtifact(
        product_id=product_id,
        component_id=product_id,
        resolved_source=resolved,
        wheel_path=wheel_path,
        verified_artifact=VerifiedArtifact(
            component_id=product_id,
            version=version,
            path=wheel_path,
            size=wheel_path.stat().st_size if wheel_path.exists() else 100,
            sha256="e" * 64,
            distribution_name="zealfie-solver",
            wheel_version=version,
        ),
    )


def _fake_resolver(owner: str, repo: str, ref: str) -> str:
    return VALID_SHA


def _fake_fetcher(owner: str, repo: str, commit_sha: str) -> bytes:
    return b"fake-archive"


class _FakeAbsentRt:
    def status(self):
        return RuntimeStatus(state=RuntimeState.ABSENT, runtime_root=Path("/fake"))


class _FakeAcquirer:
    def __init__(self):
        self.requests: list = []

    def acquire(self, request, *, staging_dir=None, timeout_seconds=300):
        self.requests.append(request)
        # Honour the caller-supplied staging dir (create + sentinel).
        staging = Path(staging_dir) if staging_dir is not None else None
        if staging is not None:
            staging.mkdir(parents=True, exist_ok=True)
            (staging / ".sentinel").touch()
        return None  # service ignores the return value


def _build_service(tmp_path, monkeypatch, *, ppa, fake_apply):
    """Build a service with fake prepare + fake apply, and record apply kwargs."""
    import zealfie.app.service as svc_mod

    catalog = _planning_catalog()
    store = SelectionStore(path=tmp_path / "desired-products.toml")
    service = ZeAlfieService(
        catalog=catalog,
        runtime=_FakeAbsentRt(),
        selection_store=store,
        acquirer=_FakeAcquirer(),
    )

    def _fake_prepare(product_id, *, resolver, fetcher, work_root, progress_callback=None):
        return ppa

    monkeypatch.setattr(service, "prepare_product_artifact", _fake_prepare)

    apply_calls: list[dict] = []

    def _recording_apply(plan, *, registry, runtime, progress_callback=None):
        apply_calls.append({"progress_callback": progress_callback})
        return fake_apply(plan, registry=registry, runtime=runtime,
                          progress_callback=progress_callback)

    monkeypatch.setattr(svc_mod, "apply_deployment_plan", _recording_apply)

    return service, apply_calls


# ---------------------------------------------------------------------------
# 1) Callback-absent backward compatibility
# ---------------------------------------------------------------------------


def test_install_product_without_callback_is_backcompat(tmp_path, witness_wheel, monkeypatch):
    """Without a callback, install_product behaves exactly as before: the
    fake apply receives no progress_callback kwarg and the result is the
    exact fake DeploymentResult."""
    ppa = _make_ppa(witness_wheel)
    fake_result = DeploymentResult(success=True, active_slot_id="rt-abc123")

    def _fake_apply(plan, *, registry, runtime, progress_callback=None):
        return fake_result

    service, apply_calls = _build_service(
        tmp_path, monkeypatch, ppa=ppa, fake_apply=_fake_apply,
    )

    result = service.install_product(
        "zesolver",
        resolver=_fake_resolver,
        fetcher=_fake_fetcher,
        work_root=tmp_path / "work",
        dependency_wheelhouse=Path("/nonexistent-wheelhouse"),
    )

    assert result is fake_result
    assert len(apply_calls) == 1
    assert apply_calls[0]["progress_callback"] is None, (
        "progress_callback must not be forwarded when absent"
    )


# ---------------------------------------------------------------------------
# 2) Monotone / ordered / success
# ---------------------------------------------------------------------------


def test_progress_monotone_order_success(tmp_path, witness_wheel, monkeypatch):
    """With a callback, milestones arrive in logical order, percents are
    monotone 0..100, and 100 is emitted exactly once on COMPLETED."""
    ppa = _make_ppa(witness_wheel)
    fake_result = DeploymentResult(success=True, active_slot_id="rt-abc123")

    def _fake_apply(plan, *, registry, runtime, progress_callback=None):
        if progress_callback is not None:
            progress_callback(InstallProgress(InstallPhase.VALIDATING, 90, "Validating\u2026"))
            progress_callback(InstallProgress(InstallPhase.ACTIVATING, 95, "Activating\u2026"))
        return fake_result

    service, _ = _build_service(
        tmp_path, monkeypatch, ppa=ppa, fake_apply=_fake_apply,
    )

    events: list[InstallProgress] = []
    result = service.install_product(
        "zesolver",
        resolver=_fake_resolver,
        fetcher=_fake_fetcher,
        work_root=tmp_path / "work",
        dependency_wheelhouse=None,  # auto-acquire → ACQUIRING_DEPENDENCIES
        progress_callback=events.append,
    )

    assert result.success is True
    assert len(events) >= 4, f"expected several milestones, got {events}"

    phases = [e.phase for e in events]
    percents = [e.percent for e in events]

    # Logical order: phases strictly follow enum order (may skip).
    for i in range(1, len(events)):
        assert _PHASE_ORDER.index(phases[i]) >= _PHASE_ORDER.index(phases[i - 1]), (
            f"phases out of order: {phases}"
        )

    # Monotone, bounded percents.
    for i in range(len(percents)):
        assert 0 <= percents[i] <= 100
        if i > 0:
            assert percents[i] >= percents[i - 1], f"non-monotone: {percents}"

    # 100 appears exactly once, on COMPLETED.
    assert percents.count(100) == 1, f"100 must appear once: {percents}"
    assert phases[-1] is InstallPhase.COMPLETED
    assert percents[-1] == 100

    # Required milestone phases present.
    assert InstallPhase.PREPARING in phases
    assert InstallPhase.ACQUIRING_DEPENDENCIES in phases
    assert InstallPhase.PLANNING_RUNTIME in phases
    assert InstallPhase.INSTALLING_RUNTIME in phases


# ---------------------------------------------------------------------------
# 3) Failure: no COMPLETED / no 100
# ---------------------------------------------------------------------------


def test_progress_no_completed_on_failure_result(tmp_path, witness_wheel, monkeypatch):
    """A failed DeploymentResult never emits COMPLETED or 100, and the
    original result is preserved."""
    ppa = _make_ppa(witness_wheel)
    fake_result = DeploymentResult(success=False, reason="deployment plan blocked")

    def _fake_apply(plan, *, registry, runtime, progress_callback=None):
        return fake_result

    service, _ = _build_service(
        tmp_path, monkeypatch, ppa=ppa, fake_apply=_fake_apply,
    )

    events: list[InstallProgress] = []
    result = service.install_product(
        "zesolver",
        resolver=_fake_resolver,
        fetcher=_fake_fetcher,
        work_root=tmp_path / "work",
        dependency_wheelhouse=Path("/nonexistent-wheelhouse"),
        progress_callback=events.append,
    )

    assert result is fake_result
    assert result.success is False
    assert InstallPhase.COMPLETED not in [e.phase for e in events]
    assert all(e.percent < 100 for e in events), f"no 100 on failure: {events}"


def test_progress_no_completed_on_exception(tmp_path, witness_wheel, monkeypatch):
    """An exception during apply propagates unchanged and never emits
    COMPLETED / 100."""
    ppa = _make_ppa(witness_wheel)
    boom = RuntimeError("simulated apply explosion")

    def _fake_apply(plan, *, registry, runtime, progress_callback=None):
        raise boom

    service, _ = _build_service(
        tmp_path, monkeypatch, ppa=ppa, fake_apply=_fake_apply,
    )

    events: list[InstallProgress] = []
    with pytest.raises(RuntimeError) as exc_info:
        service.install_product(
            "zesolver",
            resolver=_fake_resolver,
            fetcher=_fake_fetcher,
            work_root=tmp_path / "work",
            dependency_wheelhouse=Path("/nonexistent-wheelhouse"),
            progress_callback=events.append,
        )

    assert exc_info.value is boom
    assert InstallPhase.COMPLETED not in [e.phase for e in events]
    assert all(e.percent < 100 for e in events)


# ---------------------------------------------------------------------------
# 4) prepare_product_artifact emits resolve → download → build
# ---------------------------------------------------------------------------


def test_prepare_product_artifact_emits_resolve_download_build(tmp_path, monkeypatch):
    """The real prepare_product_artifact emits RESOLVING_SOURCE then
    DOWNLOADING_SOURCE then BUILDING_PRODUCT at the real boundaries."""
    import zealfie.app.service as svc_mod
    from zealfie.building import inspect_wheel as _real_inspect_wheel

    catalog = _planning_catalog()
    service = ZeAlfieService(catalog=catalog, runtime=_FakeAbsentRt())

    remote = RemoteSource(owner="tinystork", repo="ZeSolver", ref=BRANCH_REF)
    resolved = ResolvedSource(source=remote, commit_sha=VALID_SHA)

    monkeypatch.setattr(svc_mod, "resolve_source", lambda src, *, resolver: resolved)

    @contextmanager
    def _fake_acquire(resolved, *, fetcher, stage_root):
        stage = Path(stage_root) / "staged"
        stage.mkdir(parents=True, exist_ok=True)
        yield stage

    monkeypatch.setattr(svc_mod, "acquire_source", _fake_acquire)

    wheel_path = tmp_path / "out" / "prod-0.0.1-py3-none-any.whl"
    wheel_path.parent.mkdir(parents=True, exist_ok=True)
    wheel_path.write_bytes(b"dummy-wheel")
    monkeypatch.setattr(
        svc_mod, "build_wheel_from_staged",
        lambda staged, *, output_dir: wheel_path,
    )

    class _Info:
        version = "0.0.1"

    monkeypatch.setattr("zealfie.building.inspect_wheel", lambda p: _Info())

    verified = VerifiedArtifact(
        component_id="zesolver",
        version="0.0.1",
        path=wheel_path,
        size=wheel_path.stat().st_size,
        sha256="e" * 64,
        distribution_name="zealfie-solver",
        wheel_version="0.0.1",
    )
    monkeypatch.setattr(
        svc_mod, "verify_artifact",
        lambda manifest, *, registry, artifact_root: verified,
    )

    events: list[InstallProgress] = []
    ppa = service.prepare_product_artifact(
        "zesolver",
        resolver=_fake_resolver,
        fetcher=_fake_fetcher,
        work_root=tmp_path / "work",
        progress_callback=events.append,
    )

    assert ppa.product_id == "zesolver"
    phases = [e.phase for e in events]
    assert InstallPhase.RESOLVING_SOURCE in phases
    assert InstallPhase.DOWNLOADING_SOURCE in phases
    assert InstallPhase.BUILDING_PRODUCT in phases
    # Ordered and monotone.
    for i in range(1, len(events)):
        assert _PHASE_ORDER.index(phases[i]) >= _PHASE_ORDER.index(phases[i - 1])
        assert events[i].percent >= events[i - 1].percent


# ---------------------------------------------------------------------------
# 5) Raising progress callbacks are observational only
# ---------------------------------------------------------------------------


def test_raising_callback_does_not_abort_success(tmp_path, witness_wheel, monkeypatch):
    """A progress callback that raises must not abort a successful install."""
    ppa = _make_ppa(witness_wheel)
    fake_result = DeploymentResult(success=True, active_slot_id="rt-abc123")

    def _fake_apply(plan, *, registry, runtime, progress_callback=None):
        return fake_result

    service, _ = _build_service(
        tmp_path, monkeypatch, ppa=ppa, fake_apply=_fake_apply,
    )

    def _boom(progress):
        raise RuntimeError("callback exploded")

    result = service.install_product(
        "zesolver",
        resolver=_fake_resolver,
        fetcher=_fake_fetcher,
        work_root=tmp_path / "work",
        dependency_wheelhouse=Path("/nonexistent-wheelhouse"),
        progress_callback=_boom,
    )

    assert result is fake_result
    assert result.success is True


def test_raising_callback_does_not_mask_failure(tmp_path, witness_wheel, monkeypatch):
    """A raising callback must not mask or alter a failed install result."""
    ppa = _make_ppa(witness_wheel)
    fake_result = DeploymentResult(success=False, reason="deployment plan blocked")

    def _fake_apply(plan, *, registry, runtime, progress_callback=None):
        return fake_result

    service, _ = _build_service(
        tmp_path, monkeypatch, ppa=ppa, fake_apply=_fake_apply,
    )

    def _boom(progress):
        raise RuntimeError("callback exploded")

    result = service.install_product(
        "zesolver",
        resolver=_fake_resolver,
        fetcher=_fake_fetcher,
        work_root=tmp_path / "work",
        dependency_wheelhouse=Path("/nonexistent-wheelhouse"),
        progress_callback=_boom,
    )

    assert result is fake_result
    assert result.success is False
    assert result.reason == "deployment plan blocked"
