"""Tests for M1-2D.4.2C — Service integration for dependency acquisition.

Tests cover:

1. Success path: fake acquisition → wheelhouse passed to plan/apply;
   selection persisted only after success.
2. Acquisition failure: no runtime apply, no selection mutation.
3. Missing product wheel around acquisition: clean service-level
   error with __cause__ preserved; no apply/selection.
4. Apply failure: auto-acquired staging cleaned; selection not persisted.
5. Success: auto-acquired staging cleaned after apply; selection
   persisted only after success.
6. Explicit caller-supplied dependency_wheelhouse bypasses acquirer.
7. Sentinel: required extras from product catalog passed to
   build_acquisition_request / fake acquirer request.
8-11: Error wrapping, hierarchy, and propagation.
12: Staging-under-work-root contract: private unique staging dir
    under work_root, zealfie-acq- prefix, cleaned after success.
13: Raw OSError wrapping and cleanup.
14: Wheelhouse exists and contains expected content during
    install_prepared_product_deployment callback.
15: Two sequential auto-acquires use different staging dirs.

All tests are FAST — no real network, no pip, no venv, no subprocess.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest

from zealfie.app import (
    PreparedProductArtifact,
    ProductCatalog,
    ProductDescriptor,
    ProductDependencyAcquisitionError,
    RemoteSourceUnavailableError,
    SelectionStore,
    ZeAlfieService,
)
from zealfie.components.model import EntryPointContract
from zealfie.dependencies.acquisition import (
    AcquisitionTransportError,
    DependencyAcquisitionRequest,
    DependencyAcquisitionResult,
)
from zealfie.dependencies.models import ExtraNotFound, MetadataError
from zealfie.releases.model import VerifiedArtifact
from zealfie.sources import RemoteSource, ResolvedSource


# ===========================================================================
# Constants
# ===========================================================================

VALID_SHA = "d4a0f1e2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8"
BRANCH_REF = "main"


# ===========================================================================
# Helpers
# ===========================================================================


def _planning_catalog(
    product_id="zesolver",
    dist_name="zealfie-solver",
    required_extras=(),
    **kwargs,
) -> ProductCatalog:
    """Create a single-product catalog for service integration tests."""
    desc = ProductDescriptor(
        product_id=product_id,
        display_name=product_id.capitalize(),
        distribution_name=dist_name,
        launch_entry_points=(
            EntryPointContract("gui_scripts", "zesolver"),
        ),
        required_extras=required_extras,
        remote_source=RemoteSource(
            owner="tinystork",
            repo="ZeSolver",
            ref=BRANCH_REF,
        ),
        **kwargs,
    )
    return ProductCatalog((desc,))


def _make_ppa(product_id, component_id, wheel_path, version="1.0",
              dist_name=None):
    """Create a PreparedProductArtifact for testing."""
    if dist_name is None:
        dist_name = f"zealfie-{product_id}"

    remote = RemoteSource(owner="tinystork", repo=f"Ze{product_id.capitalize()}", ref="main")
    resolved = ResolvedSource(source=remote, commit_sha=VALID_SHA)

    return PreparedProductArtifact(
        product_id=product_id,
        component_id=component_id,
        resolved_source=resolved,
        wheel_path=wheel_path,
        verified_artifact=VerifiedArtifact(
            component_id=component_id,
            version=version,
            path=wheel_path,
            size=wheel_path.stat().st_size if wheel_path.exists() else 100,
            sha256="e" * 64,
            distribution_name=dist_name,
            wheel_version=version,
        ),
    )


# ===========================================================================
# Fake acquirer for test injection
# ===========================================================================


@dataclass
class _FakeAcquirer:
    """Injectable acquirer that returns a predetermined result or raises.

    When *staging_dir* is provided (non-None), the fake honours it by
    creating the directory, placing a sentinel inside, and returning a
    :class:`DependencyAcquisitionResult` whose ``staging_wheelhouse`` is
    the exact provided directory so that service cleanup targets the
    correct private directory.
    """

    result: DependencyAcquisitionResult | None = None
    error: Exception | None = None
    requests: list[DependencyAcquisitionRequest] | None = None
    staging_dirs: list[Path | None] | None = None

    def __post_init__(self):
        if self.requests is None:
            self.requests = []
        if self.staging_dirs is None:
            self.staging_dirs = []

    def acquire(
        self,
        request: DependencyAcquisitionRequest,
        *,
        staging_dir=None,
        timeout_seconds=300,
    ) -> DependencyAcquisitionResult:
        self.requests.append(request)
        self.staging_dirs.append(
            Path(staging_dir) if staging_dir is not None else None
        )
        if self.error is not None:
            raise self.error
        if staging_dir is not None:
            # Honour the caller-provided staging directory.
            _staging = Path(staging_dir)
            _staging.mkdir(parents=True, exist_ok=True)
            (_staging / ".sentinel").touch()
            return DependencyAcquisitionResult(
                staging_wheelhouse=_staging,
                acquired=(),
            )
        if self.result is not None:
            return self.result
        # Default: success with empty acquired wheels
        return DependencyAcquisitionResult(
            staging_wheelhouse=Path("/fake/staging-wheelhouse"),
            acquired=(),
        )


# ===========================================================================
# Fake resolver / fetcher for prepare_product_artifact
# ===========================================================================


def _fake_resolver(owner: str, repo: str, ref: str) -> str:
    return VALID_SHA


def _fake_fetcher(owner: str, repo: str, commit_sha: str) -> bytes:
    return b"fake-archive"


# ===========================================================================
# Fake runtime
# ===========================================================================


class _FakeAbsentRt:
    def status(self):
        from zealfie.runtime.model import RuntimeState, RuntimeStatus
        return RuntimeStatus(
            state=RuntimeState.ABSENT,
            runtime_root=Path("/fake"),
        )


# ===========================================================================
# Test 1: Success path — fake acquisition → wheelhouse passed through
# ===========================================================================


def test_c1_success_acquisition_wheelhouse_passed_to_plan_apply(
    tmp_path, witness_wheel, monkeypatch,
):
    """Full success path: prepare → acquire → plan/apply receives
    staging_wheelhouse; selection persisted only after success."""
    from zealfie.runtime.model import DeploymentResult

    # --- Catalog: ZeSolver with gui extra ---
    catalog = _planning_catalog(
        product_id="zesolver",
        dist_name="zealfie-solver",
    )

    # --- Fake acquirer — service creates its own private staging ---
    fake_acquirer = _FakeAcquirer()

    # --- Selection store ---
    sel_path = tmp_path / "desired-products.toml"
    store = SelectionStore(path=sel_path)

    # --- Service with fake acquirer ---
    service = ZeAlfieService(
        catalog=catalog,
        runtime=_FakeAbsentRt(),
        selection_store=store,
        acquirer=fake_acquirer,
    )

    # --- Monkeypatch prepare_product_artifact on the instance ---
    ppa = _make_ppa("zesolver", "zesolver", witness_wheel, dist_name="zealfie-solver")
    prepare_calls = []

    def _fake_prepare(product_id, *, resolver, fetcher, work_root):
        prepare_calls.append((product_id, resolver, fetcher, work_root))
        return ppa

    monkeypatch.setattr(service, "prepare_product_artifact", _fake_prepare)

    # --- Monkeypatch install_prepared_product_deployment on the instance ---
    fake_deploy_result = DeploymentResult(success=True, active_slot_id="rt-abc123")
    install_calls = []

    def _fake_install_prepared(prepared_artifacts, *,
                               dependency_wheelhouse=None,
                               probe_distribution=None, accelerated_acquirer=None):
        install_calls.append({
            "prepared_artifacts": prepared_artifacts,
            "dependency_wheelhouse": dependency_wheelhouse,
        })
        # Simulate selection persistence
        store.select(ppa.product_id, catalog=catalog)
        return fake_deploy_result

    monkeypatch.setattr(service, "install_prepared_product_deployment", _fake_install_prepared)

    work_root = tmp_path / "work"

    # --- Execute install_product ---
    result = service.install_product(
        "zesolver",
        resolver=_fake_resolver,
        fetcher=_fake_fetcher,
        work_root=work_root,
        dependency_wheelhouse=None,  # trigger auto-acquire
    )

    # --- Assertions ---
    assert result is fake_deploy_result
    assert result.success is True

    # prepare was called
    assert len(prepare_calls) == 1

    # acquirer was called with a non-None staging_dir
    assert len(fake_acquirer.staging_dirs) == 1
    svc_staging = fake_acquirer.staging_dirs[0]
    assert svc_staging is not None
    assert svc_staging.is_relative_to(work_root), (
        f"staging {svc_staging} must be under work_root {work_root}"
    )
    assert svc_staging.name.startswith("zealfie-acq-"), (
        f"staging dir must have zealfie-acq- prefix, got {svc_staging.name}"
    )

    # install_prepared was called with the service-created staging wheelhouse
    assert len(install_calls) == 1
    assert install_calls[0]["dependency_wheelhouse"] == svc_staging

    # acquirer was called
    assert len(fake_acquirer.requests) == 1

    # selection persisted
    store.reload()
    assert "zesolver" in store.selected_product_ids

    # Verify staging is cleaned after success
    assert not svc_staging.exists(), "auto-acquired staging must be cleaned after success"

    # No leftover zealfie-acq-* dirs under work_root
    leftovers = list(work_root.glob("zealfie-acq-*"))
    assert len(leftovers) == 0, f"expected no leftover staging dirs, got {leftovers}"


# ===========================================================================
# Test 2: Acquisition failure → no apply, no selection mutation
# ===========================================================================


def test_c2_acquisition_failure_no_apply_no_selection(
    tmp_path, witness_wheel, monkeypatch,
):
    """When acquisition raises AcquisitionTransportError, no apply occurs
    and selection is not mutated."""
    catalog = _planning_catalog(product_id="zesolver")

    # Fake acquirer that fails
    fake_acquirer = _FakeAcquirer(
        error=AcquisitionTransportError("pip-download", "No matching distribution"),
    )

    sel_path = tmp_path / "desired-products.toml"
    sel_path.parent.mkdir(parents=True, exist_ok=True)
    sel_path.write_text(
        'schema_version = 1\n'
        'selected_product_ids = ["zesolver"]\n'
    )
    store = SelectionStore(path=sel_path)
    original_content = sel_path.read_text()

    service = ZeAlfieService(
        catalog=catalog,
        runtime=_FakeAbsentRt(),
        selection_store=store,
        acquirer=fake_acquirer,
    )

    # Monkeypatch prepare on instance
    ppa = _make_ppa("zesolver", "zesolver", witness_wheel, dist_name="zealfie-solver")

    def _fake_prepare(product_id, *, resolver, fetcher, work_root):
        return ppa

    monkeypatch.setattr(service, "prepare_product_artifact", _fake_prepare)

    # Block install_prepared — must not be called
    install_called = False

    def _explosive_install(*args, **kwargs):
        nonlocal install_called
        install_called = True
        raise AssertionError("install_prepared must not be called on acquisition failure")

    monkeypatch.setattr(service, "install_prepared_product_deployment", _explosive_install)

    work_root = tmp_path / "work"

    # Execute — should raise ProductDependencyAcquisitionError
    with pytest.raises(ProductDependencyAcquisitionError) as exc_info:
        service.install_product(
            "zesolver",
            resolver=_fake_resolver,
            fetcher=_fake_fetcher,
            work_root=work_root,
            dependency_wheelhouse=None,
        )

    # Error carries __cause__
    assert exc_info.value.__cause__ is not None
    assert isinstance(exc_info.value.__cause__, AcquisitionTransportError)
    assert "zesolver" in str(exc_info.value)

    # install_prepared was NOT called
    assert not install_called

    # Selection NOT mutated
    assert sel_path.read_text() == original_content

    # acquirer was called
    assert len(fake_acquirer.requests) == 1

    # Staging was created by service before acquisition raised — must be cleaned
    assert len(fake_acquirer.staging_dirs) == 1
    svc_staging = fake_acquirer.staging_dirs[0]
    assert svc_staging is not None
    assert not svc_staging.exists(), (
        "staging must be cleaned even when acquisition fails"
    )

    # No leftover zealfie-acq-* dirs under work_root
    leftovers = list(work_root.glob("zealfie-acq-*"))
    assert len(leftovers) == 0, f"expected no leftover staging dirs, got {leftovers}"


# ===========================================================================
# Test 3: Missing product wheel → clean service-level error, cause preserved
# ===========================================================================


def test_c3_missing_wheel_clean_error_cause_preserved(
    tmp_path, witness_wheel, monkeypatch,
):
    """Product wheel disappeared after prepare but before acquire →
    raises ProductDependencyAcquisitionError with FileNotFoundError cause."""
    catalog = _planning_catalog(product_id="zesolver")

    # Fake acquirer that raises FileNotFoundError (wheel gone)
    fnf = FileNotFoundError("No such file: /tmp/gone.whl")
    fake_acquirer = _FakeAcquirer(error=fnf)

    sel_path = tmp_path / "desired-products.toml"
    store = SelectionStore(path=sel_path)

    service = ZeAlfieService(
        catalog=catalog,
        runtime=_FakeAbsentRt(),
        selection_store=store,
        acquirer=fake_acquirer,
    )

    ppa = _make_ppa("zesolver", "zesolver", witness_wheel, dist_name="zealfie-solver")

    def _fake_prepare(product_id, *, resolver, fetcher, work_root):
        return ppa

    monkeypatch.setattr(service, "prepare_product_artifact", _fake_prepare)

    install_called = False

    def _explosive_install(*args, **kwargs):
        nonlocal install_called
        install_called = True
        raise AssertionError("must not be called")

    monkeypatch.setattr(service, "install_prepared_product_deployment", _explosive_install)

    with pytest.raises(ProductDependencyAcquisitionError) as exc_info:
        service.install_product(
            "zesolver",
            resolver=_fake_resolver,
            fetcher=_fake_fetcher,
            work_root=tmp_path / "work",
            dependency_wheelhouse=None,
        )

    # Error details
    assert exc_info.value.__cause__ is fnf
    assert "zesolver" in str(exc_info.value)

    # No apply
    assert not install_called

    # No selection mutation
    store.reload()
    assert "zesolver" not in store.selected_product_ids


# ===========================================================================
# Test 4: Apply failure → staging cleaned, selection not persisted
# ===========================================================================


def test_c4_apply_failure_staging_cleaned_no_selection(
    tmp_path, witness_wheel, monkeypatch,
):
    """When apply returns success=False, auto-acquired staging is cleaned
    and selection is not persisted."""
    from zealfie.runtime.model import DeploymentResult

    catalog = _planning_catalog(product_id="zesolver")

    # Fake acquirer — service creates its own private staging
    fake_acquirer = _FakeAcquirer()

    sel_path = tmp_path / "desired-products.toml"
    store = SelectionStore(path=sel_path)

    service = ZeAlfieService(
        catalog=catalog,
        runtime=_FakeAbsentRt(),
        selection_store=store,
        acquirer=fake_acquirer,
    )

    ppa = _make_ppa("zesolver", "zesolver", witness_wheel, dist_name="zealfie-solver")

    def _fake_prepare(product_id, *, resolver, fetcher, work_root):
        return ppa

    monkeypatch.setattr(service, "prepare_product_artifact", _fake_prepare)

    # install_prepared returns failure
    fake_deploy_result = DeploymentResult(success=False, reason="apply exploded")

    def _fake_install(*args, **kwargs):
        return fake_deploy_result

    monkeypatch.setattr(service, "install_prepared_product_deployment", _fake_install)

    work_root = tmp_path / "work"

    result = service.install_product(
        "zesolver",
        resolver=_fake_resolver,
        fetcher=_fake_fetcher,
        work_root=work_root,
        dependency_wheelhouse=None,
    )

    # Apply result returned as-is
    assert result is fake_deploy_result
    assert result.success is False

    # Staging cleaned after apply failure
    assert len(fake_acquirer.staging_dirs) == 1
    svc_staging = fake_acquirer.staging_dirs[0]
    assert svc_staging is not None
    assert not svc_staging.exists(), (
        "auto-acquired staging must be cleaned after apply failure"
    )

    # No leftover zealfie-acq-* dirs under work_root
    leftovers = list(work_root.glob("zealfie-acq-*"))
    assert len(leftovers) == 0, f"expected no leftover staging dirs, got {leftovers}"

    # Selection NOT persisted
    store.reload()
    assert "zesolver" not in store.selected_product_ids


# ===========================================================================
# Test 5: Success — staging cleaned after apply; selection persisted
# ===========================================================================


def test_c5_success_staging_cleaned_selection_persisted(
    tmp_path, witness_wheel, monkeypatch,
):
    """After successful apply, auto-acquired staging is cleaned and
    selection is persisted."""
    from zealfie.runtime.model import DeploymentResult

    catalog = _planning_catalog(product_id="zesolver")

    # Fake acquirer — service creates its own private staging
    fake_acquirer = _FakeAcquirer()

    sel_path = tmp_path / "desired-products.toml"
    store = SelectionStore(path=sel_path)

    service = ZeAlfieService(
        catalog=catalog,
        runtime=_FakeAbsentRt(),
        selection_store=store,
        acquirer=fake_acquirer,
    )

    ppa = _make_ppa("zesolver", "zesolver", witness_wheel, dist_name="zealfie-solver")

    def _fake_prepare(product_id, *, resolver, fetcher, work_root):
        return ppa

    monkeypatch.setattr(service, "prepare_product_artifact", _fake_prepare)

    fake_deploy_result = DeploymentResult(success=True, active_slot_id="rt-success1")

    def _fake_install(prepared_artifacts, *, dependency_wheelhouse=None, probe_distribution=None, accelerated_acquirer=None):
        store.select(ppa.product_id, catalog=catalog)
        return fake_deploy_result

    monkeypatch.setattr(service, "install_prepared_product_deployment", _fake_install)

    work_root = tmp_path / "work"

    result = service.install_product(
        "zesolver",
        resolver=_fake_resolver,
        fetcher=_fake_fetcher,
        work_root=work_root,
        dependency_wheelhouse=None,
    )

    assert result.success is True
    # Staging cleaned
    assert len(fake_acquirer.staging_dirs) == 1
    svc_staging = fake_acquirer.staging_dirs[0]
    assert svc_staging is not None
    assert not svc_staging.exists(), (
        "auto-acquired staging must be cleaned after success"
    )

    # No leftover zealfie-acq-* dirs under work_root
    leftovers = list(work_root.glob("zealfie-acq-*"))
    assert len(leftovers) == 0, f"expected no leftover staging dirs, got {leftovers}"

    # Selection persisted
    store.reload()
    assert "zesolver" in store.selected_product_ids


# ===========================================================================
# Test 6: Explicit dependency_wheelhouse bypasses acquirer
# ===========================================================================


def test_c6_explicit_wheelhouse_bypasses_acquirer(
    tmp_path, witness_wheel, monkeypatch,
):
    """When caller supplies dependency_wheelhouse, the acquirer is never
    called and staging is never cleaned."""
    from zealfie.runtime.model import DeploymentResult

    catalog = _planning_catalog(product_id="zesolver")

    # Fake acquirer that would fail if called
    fake_acquirer = _FakeAcquirer(
        error=AssertionError("acquirer must not be called when wheelhouse supplied"),
    )

    sel_path = tmp_path / "desired-products.toml"
    store = SelectionStore(path=sel_path)

    service = ZeAlfieService(
        catalog=catalog,
        runtime=_FakeAbsentRt(),
        selection_store=store,
        acquirer=fake_acquirer,
    )

    ppa = _make_ppa("zesolver", "zesolver", witness_wheel, dist_name="zealfie-solver")

    def _fake_prepare(product_id, *, resolver, fetcher, work_root):
        return ppa

    monkeypatch.setattr(service, "prepare_product_artifact", _fake_prepare)

    # Capture what install_prepared receives
    captured_wheelhouse = []

    fake_deploy_result = DeploymentResult(success=True, active_slot_id="rt-explicit")

    def _fake_install(prepared_artifacts, *, dependency_wheelhouse=None, probe_distribution=None, accelerated_acquirer=None):
        captured_wheelhouse.append(dependency_wheelhouse)
        store.select(ppa.product_id, catalog=catalog)
        return fake_deploy_result

    monkeypatch.setattr(service, "install_prepared_product_deployment", _fake_install)

    # Caller-supplied wheelhouse directory
    caller_wheelhouse = tmp_path / "caller-wheelhouse"
    caller_wheelhouse.mkdir()

    result = service.install_product(
        "zesolver",
        resolver=_fake_resolver,
        fetcher=_fake_fetcher,
        work_root=tmp_path / "work",
        dependency_wheelhouse=caller_wheelhouse,
    )

    assert result.success is True

    # Acquirer was never called
    assert len(fake_acquirer.requests) == 0, "acquirer must not be called"

    # install_prepared received the caller's wheelhouse
    assert len(captured_wheelhouse) == 1
    assert captured_wheelhouse[0] == caller_wheelhouse

    # Caller's wheelhouse is NOT cleaned
    assert caller_wheelhouse.exists(), "caller-supplied wheelhouse must not be cleaned"

    # Selection persisted
    store.reload()
    assert "zesolver" in store.selected_product_ids


# ===========================================================================
# Test 7: Sentinel — required extras passed to build_acquisition_request
# ===========================================================================


def test_c7_required_extras_passed_to_acquisition_request(
    tmp_path, witness_wheel, monkeypatch,
):
    """Product catalog required_extras (e.g., gui for ZeSolver) are
    passed to build_acquisition_request → fake acquirer receives the
    correct active_extras in the request."""
    import zealfie.app.service as svc_mod
    from zealfie.runtime.model import DeploymentResult

    # ZeSolver with gui extra
    catalog = _planning_catalog(
        product_id="zesolver",
        dist_name="zealfie-solver",
        required_extras=("gui",),
    )

    # Fake acquirer — service creates its own private staging
    fake_acquirer = _FakeAcquirer()

    sel_path = tmp_path / "desired-products.toml"
    store = SelectionStore(path=sel_path)

    service = ZeAlfieService(
        catalog=catalog,
        runtime=_FakeAbsentRt(),
        selection_store=store,
        acquirer=fake_acquirer,
    )

    ppa = _make_ppa("zesolver", "zesolver", witness_wheel, dist_name="zealfie-solver")

    def _fake_prepare(product_id, *, resolver, fetcher, work_root):
        return ppa

    monkeypatch.setattr(service, "prepare_product_artifact", _fake_prepare)

    # Mock build_acquisition_request to capture the active_extras
    # passed to it, and return a request that points at the ppa wheel.
    build_call_args = []

    def _fake_build(product_wheel_path, active_extras=None):
        build_call_args.append((product_wheel_path, active_extras))
        # Return a valid request that doesn't go through METADATA validation
        return DependencyAcquisitionRequest(
            product_wheel_path=product_wheel_path,
            active_extras=active_extras if active_extras is not None else frozenset(),
        )

    monkeypatch.setattr(svc_mod, "build_acquisition_request", _fake_build)

    fake_deploy_result = DeploymentResult(success=True, active_slot_id="rt-gui")

    def _fake_install(prepared_artifacts, *, dependency_wheelhouse=None, probe_distribution=None, accelerated_acquirer=None):
        store.select(ppa.product_id, catalog=catalog)
        return fake_deploy_result

    monkeypatch.setattr(service, "install_prepared_product_deployment", _fake_install)

    work_root = tmp_path / "work"

    result = service.install_product(
        "zesolver",
        resolver=_fake_resolver,
        fetcher=_fake_fetcher,
        work_root=work_root,
        dependency_wheelhouse=None,
    )

    assert result.success is True

    # build_acquisition_request was called with the catalog extras
    assert len(build_call_args) == 1
    called_path, called_extras = build_call_args[0]
    assert called_path == ppa.wheel_path.resolve()
    assert called_extras == frozenset({"gui"})

    # Acquirer received the request
    assert len(fake_acquirer.requests) == 1
    req = fake_acquirer.requests[0]
    assert req.active_extras == frozenset({"gui"})
    assert req.product_wheel_path == ppa.wheel_path.resolve()

    # Staging cleaned
    assert len(fake_acquirer.staging_dirs) == 1
    svc_staging = fake_acquirer.staging_dirs[0]
    assert svc_staging is not None
    assert not svc_staging.exists()

    # No leftover zealfie-acq-* dirs under work_root
    leftovers = list(work_root.glob("zealfie-acq-*"))
    assert len(leftovers) == 0, f"expected no leftover staging dirs, got {leftovers}"


# ===========================================================================
# Test 8: MetadataError wrapped in ProductDependencyAcquisitionError
# ===========================================================================


def test_c8_metadata_error_wrapped(
    tmp_path, witness_wheel, monkeypatch,
):
    """MetadataError during build_acquisition_request is wrapped in
    ProductDependencyAcquisitionError with __cause__ preserved."""
    catalog = _planning_catalog(product_id="zesolver")

    # Use a real MetadataError as the cause
    cause = MetadataError(Path("/fake.whl"), "cannot read METADATA")
    fake_acquirer = _FakeAcquirer(error=cause)

    sel_path = tmp_path / "desired-products.toml"
    store = SelectionStore(path=sel_path)

    service = ZeAlfieService(
        catalog=catalog,
        runtime=_FakeAbsentRt(),
        selection_store=store,
        acquirer=fake_acquirer,
    )

    ppa = _make_ppa("zesolver", "zesolver", witness_wheel, dist_name="zealfie-solver")

    def _fake_prepare(product_id, *, resolver, fetcher, work_root):
        return ppa

    monkeypatch.setattr(service, "prepare_product_artifact", _fake_prepare)

    install_called = False

    def _explosive_install(*args, **kwargs):
        nonlocal install_called
        install_called = True
        raise AssertionError("must not be called")

    monkeypatch.setattr(service, "install_prepared_product_deployment", _explosive_install)

    with pytest.raises(ProductDependencyAcquisitionError) as exc_info:
        service.install_product(
            "zesolver",
            resolver=_fake_resolver,
            fetcher=_fake_fetcher,
            work_root=tmp_path / "work",
            dependency_wheelhouse=None,
        )

    assert exc_info.value.__cause__ is cause
    assert not install_called


# ===========================================================================
# Test 9: ExtraNotFound wrapped (catalog misconfiguration guard)
# ===========================================================================


def test_c9_extra_not_found_wrapped(
    tmp_path, witness_wheel, monkeypatch,
):
    """ExtraNotFound from build_acquisition_request → clean service error.

    The catalog declares a required_extra that the real wheel does not
    provide.  build_acquisition_request raises ExtraNotFound, which
    install_product wraps in ProductDependencyAcquisitionError."""
    catalog = _planning_catalog(product_id="zesolver", required_extras=("nonexistent",))

    # Acquirer with no error — it should never be called because
    # build_acquisition_request fails first.
    fake_acquirer = _FakeAcquirer(
        error=AssertionError("acquirer must not be called on ExtraNotFound"),
    )

    sel_path = tmp_path / "desired-products.toml"
    store = SelectionStore(path=sel_path)

    service = ZeAlfieService(
        catalog=catalog,
        runtime=_FakeAbsentRt(),
        selection_store=store,
        acquirer=fake_acquirer,
    )

    ppa = _make_ppa("zesolver", "zesolver", witness_wheel, dist_name="zealfie-solver")

    def _fake_prepare(product_id, *, resolver, fetcher, work_root):
        return ppa

    monkeypatch.setattr(service, "prepare_product_artifact", _fake_prepare)

    install_called = False

    def _explosive_install(*args, **kwargs):
        nonlocal install_called
        install_called = True
        raise AssertionError("must not be called")

    monkeypatch.setattr(service, "install_prepared_product_deployment", _explosive_install)

    with pytest.raises(ProductDependencyAcquisitionError) as exc_info:
        service.install_product(
            "zesolver",
            resolver=_fake_resolver,
            fetcher=_fake_fetcher,
            work_root=tmp_path / "work",
            dependency_wheelhouse=None,
        )

    # Cause is an ExtraNotFound (from build_acquisition_request)
    assert exc_info.value.__cause__ is not None
    assert isinstance(exc_info.value.__cause__, ExtraNotFound)
    assert "nonexistent" in str(exc_info.value.__cause__)
    assert not install_called


# ===========================================================================
# Test 10: Prepare failure propagates without wrapping
# ===========================================================================


def test_c10_prepare_failure_propagates_unchanged(
    tmp_path, monkeypatch,
):
    """Errors from prepare_product_artifact (e.g., UnknownProductError,
    RemoteSourceUnavailableError) propagate without wrapping and without
    calling the acquirer or install_prepared."""
    catalog = _planning_catalog(product_id="zesolver")

    fake_acquirer = _FakeAcquirer(
        error=AssertionError("acquirer must not be called on prepare failure"),
    )

    sel_path = tmp_path / "desired-products.toml"
    store = SelectionStore(path=sel_path)

    service = ZeAlfieService(
        catalog=catalog,
        runtime=_FakeAbsentRt(),
        selection_store=store,
        acquirer=fake_acquirer,
    )

    from zealfie.products.catalog import UnknownProductError

    with pytest.raises(UnknownProductError, match="nonexistent"):
        service.install_product(
            "nonexistent",
            resolver=_fake_resolver,
            fetcher=_fake_fetcher,
            work_root=tmp_path / "work",
            dependency_wheelhouse=None,
        )

    # Acquirer never called
    assert len(fake_acquirer.requests) == 0


# ===========================================================================
# Test 11: Error hierarchy — ProductDependencyAcquisitionError is RuntimeError
# ===========================================================================


def test_c11_error_hierarchy():
    """ProductDependencyAcquisitionError is a RuntimeError and supports
    `raise ... from cause`."""
    err = ProductDependencyAcquisitionError("test")
    assert isinstance(err, RuntimeError)

    # __cause__ via raise-from
    cause = FileNotFoundError("gone")
    try:
        raise ProductDependencyAcquisitionError("wrapped") from cause
    except ProductDependencyAcquisitionError as wrapped:
        assert wrapped.__cause__ is cause


# ===========================================================================
# Test 12: Staging-under-work-root contract — private unique staging dir
# ===========================================================================


def test_c12_staging_under_work_root_private_and_cleaned(
    tmp_path, witness_wheel, monkeypatch,
):
    """install_product creates a private unique staging directory under
    work_root, passes it to acquirer (non-None), and cleans it after
    success.  The staging dir must have the zealfie-acq- prefix and
    must not exist after install_product returns."""
    from zealfie.runtime.model import DeploymentResult

    catalog = _planning_catalog(product_id="zesolver")

    # Capture the staging_dir argument
    captured_staging_dirs = []

    class _CapturingAcquirer:
        def acquire(self, request, *, staging_dir=None, timeout_seconds=300):
            captured_staging_dirs.append(
                Path(staging_dir) if staging_dir is not None else None
            )
            # Honour the provided staging dir
            if staging_dir is not None:
                _staging = Path(staging_dir)
                _staging.mkdir(parents=True, exist_ok=True)
                (_staging / ".sentinel").touch()
                return DependencyAcquisitionResult(
                    staging_wheelhouse=_staging,
                    acquired=(),
                )
            # Fallback for legacy tests
            import tempfile
            _staging = Path(tempfile.mkdtemp(prefix="test-acq-"))
            return DependencyAcquisitionResult(
                staging_wheelhouse=_staging,
                acquired=(),
            )

    fake_acquirer = _CapturingAcquirer()

    sel_path = tmp_path / "desired-products.toml"
    store = SelectionStore(path=sel_path)

    service = ZeAlfieService(
        catalog=catalog,
        runtime=_FakeAbsentRt(),
        selection_store=store,
        acquirer=fake_acquirer,
    )

    ppa = _make_ppa("zesolver", "zesolver", witness_wheel, dist_name="zealfie-solver")

    def _fake_prepare(product_id, *, resolver, fetcher, work_root):
        return ppa

    monkeypatch.setattr(service, "prepare_product_artifact", _fake_prepare)

    fake_deploy_result = DeploymentResult(success=True, active_slot_id="rt-staging")

    def _fake_install(prepared_artifacts, *, dependency_wheelhouse=None, probe_distribution=None, accelerated_acquirer=None):
        store.select(ppa.product_id, catalog=catalog)
        return fake_deploy_result

    monkeypatch.setattr(service, "install_prepared_product_deployment", _fake_install)

    work_root = tmp_path / "work"

    result = service.install_product(
        "zesolver",
        resolver=_fake_resolver,
        fetcher=_fake_fetcher,
        work_root=work_root,
        dependency_wheelhouse=None,
    )

    assert result.success is True

    # Acquirer received a non-None staging_dir under work_root
    assert len(captured_staging_dirs) == 1
    staging = captured_staging_dirs[0]
    assert staging is not None, (
        "install_product must pass a non-None staging_dir to acquirer"
    )
    assert staging.is_relative_to(work_root), (
        f"staging {staging} must be under work_root {work_root}"
    )
    assert staging.name.startswith("zealfie-acq-"), (
        f"staging dir must have zealfie-acq- prefix, got {staging.name}"
    )

    # Cleaned after success
    assert not staging.exists(), (
        "auto-acquired staging must be cleaned after success"
    )

    # No leftover zealfie-acq-* dirs under work_root
    leftovers = list(work_root.glob("zealfie-acq-*"))
    assert len(leftovers) == 0, f"expected no leftover staging dirs, got {leftovers}"

    # Selection persisted
    store.reload()
    assert "zesolver" in store.selected_product_ids


# ===========================================================================
# Test 13: Raw OSError from acquirer wrapped as ProductDependencyAcquisitionError
# ===========================================================================


def test_c13_raw_oserror_wrapped_cause_preserved_no_apply_no_selection(
    tmp_path, witness_wheel, monkeypatch,
):
    """Raw OSError (not FileNotFoundError) raised by acquirer.acquire is
    wrapped into ProductDependencyAcquisitionError with __cause__ preserved;
    no apply occurs, no selection mutation.  The service-created staging
    is cleaned in finally."""
    catalog = _planning_catalog(product_id="zesolver")

    # Raw OSError simulating a staging/acquirer disk failure
    cause = OSError("disk full during pip download")
    fake_acquirer = _FakeAcquirer(error=cause)

    sel_path = tmp_path / "desired-products.toml"
    sel_path.parent.mkdir(parents=True, exist_ok=True)
    store = SelectionStore(path=sel_path)
    original_content = sel_path.read_text() if sel_path.exists() else ""

    service = ZeAlfieService(
        catalog=catalog,
        runtime=_FakeAbsentRt(),
        selection_store=store,
        acquirer=fake_acquirer,
    )

    ppa = _make_ppa("zesolver", "zesolver", witness_wheel, dist_name="zealfie-solver")

    def _fake_prepare(product_id, *, resolver, fetcher, work_root):
        return ppa

    monkeypatch.setattr(service, "prepare_product_artifact", _fake_prepare)

    install_called = False

    def _explosive_install(*args, **kwargs):
        nonlocal install_called
        install_called = True
        raise AssertionError("install_prepared must not be called on OSError")

    monkeypatch.setattr(service, "install_prepared_product_deployment", _explosive_install)

    work_root = tmp_path / "work"

    with pytest.raises(ProductDependencyAcquisitionError) as exc_info:
        service.install_product(
            "zesolver",
            resolver=_fake_resolver,
            fetcher=_fake_fetcher,
            work_root=work_root,
            dependency_wheelhouse=None,
        )

    # Error carries __cause__
    assert exc_info.value.__cause__ is cause
    assert isinstance(exc_info.value.__cause__, OSError)
    assert "disk full" in str(exc_info.value)
    assert "zesolver" in str(exc_info.value)

    # install_prepared was NOT called — no apply, no runtime mutation
    assert not install_called

    # Selection NOT mutated
    store.reload()
    if original_content:
        assert sel_path.read_text() == original_content
    else:
        assert "zesolver" not in store.selected_product_ids

    # acquirer was called exactly once
    assert len(fake_acquirer.requests) == 1

    # Staging created by service must be cleaned even on failure
    assert len(fake_acquirer.staging_dirs) == 1
    svc_staging = fake_acquirer.staging_dirs[0]
    assert svc_staging is not None
    assert not svc_staging.exists(), (
        "staging must be cleaned even when acquisition raises OSError"
    )

    # No leftover zealfie-acq-* dirs under work_root
    leftovers = list(work_root.glob("zealfie-acq-*"))
    assert len(leftovers) == 0, (
        f"expected no leftover staging dirs after OSError, got {leftovers}"
    )


# ===========================================================================
# Test 14: Wheelhouse exists and contains expected content during
#          install_prepared_product_deployment callback
# ===========================================================================


def test_c14_wheelhouse_present_and_contains_content_during_apply(
    tmp_path, witness_wheel, monkeypatch,
):
    """The auto-acquired dependency wheelhouse exists and contains the
    expected content (acquirer-written sentinel) when
    ``install_prepared_product_deployment`` is called, i.e. the
    wheelhouse is present through planning/apply before cleanup."""
    from zealfie.runtime.model import DeploymentResult

    catalog = _planning_catalog(product_id="zesolver")

    fake_acquirer = _FakeAcquirer()

    sel_path = tmp_path / "desired-products.toml"
    store = SelectionStore(path=sel_path)

    service = ZeAlfieService(
        catalog=catalog,
        runtime=_FakeAbsentRt(),
        selection_store=store,
        acquirer=fake_acquirer,
    )

    ppa = _make_ppa("zesolver", "zesolver", witness_wheel, dist_name="zealfie-solver")

    def _fake_prepare(product_id, *, resolver, fetcher, work_root):
        return ppa

    monkeypatch.setattr(service, "prepare_product_artifact", _fake_prepare)

    # Capture wheelhouse state at install_prepared time
    wheelhouse_snapshot: list[dict] = []

    fake_deploy_result = DeploymentResult(success=True, active_slot_id="rt-c14")

    def _fake_install(prepared_artifacts, *, dependency_wheelhouse=None, probe_distribution=None, accelerated_acquirer=None):
        # Snapshot wheelhouse state while it should still exist
        wh = Path(dependency_wheelhouse) if dependency_wheelhouse else None
        if wh:
            sentinel_path = wh / ".sentinel"
            wheelhouse_snapshot.append({
                "exists": wh.exists(),
                "is_dir": wh.is_dir(),
                "sentinel_exists": sentinel_path.exists(),
                "contents": sorted(p.name for p in wh.iterdir()) if wh.is_dir() else [],
            })
        else:
            wheelhouse_snapshot.append(None)
        store.select(ppa.product_id, catalog=catalog)
        return fake_deploy_result

    monkeypatch.setattr(service, "install_prepared_product_deployment", _fake_install)

    result = service.install_product(
        "zesolver",
        resolver=_fake_resolver,
        fetcher=_fake_fetcher,
        work_root=tmp_path / "work",
        dependency_wheelhouse=None,
    )

    assert result.success is True

    # Wheelhouse snapshot captured during apply
    assert len(wheelhouse_snapshot) == 1
    snap = wheelhouse_snapshot[0]
    assert snap is not None, "dependency_wheelhouse must not be None during apply"
    assert snap["exists"], "wheelhouse must exist during apply"
    assert snap["is_dir"], "wheelhouse must be a directory during apply"
    assert snap["sentinel_exists"], (
        "sentinel must exist in wheelhouse during apply"
    )
    assert ".sentinel" in snap["contents"], (
        f"wheelhouse contents during apply: {snap['contents']}"
    )

    # After install_product returns, wheelhouse is cleaned
    staging_dir = fake_acquirer.staging_dirs[0]
    assert not staging_dir.exists(), "staging must be cleaned after service returns"


# ===========================================================================
# Test 15: Two sequential auto-acquires use different staging dirs
# ===========================================================================


def test_c15_two_sequential_acquires_use_different_staging_dirs(
    tmp_path, witness_wheel, monkeypatch,
):
    """Two sequential ``install_product`` calls with auto-acquire each
    create a different unique staging directory, and both are
    cleaned after their respective calls."""
    from zealfie.runtime.model import DeploymentResult

    catalog = _planning_catalog(product_id="zesolver")

    fake_acquirer = _FakeAcquirer()

    sel_path = tmp_path / "desired-products.toml"
    store = SelectionStore(path=sel_path)

    service = ZeAlfieService(
        catalog=catalog,
        runtime=_FakeAbsentRt(),
        selection_store=store,
        acquirer=fake_acquirer,
    )

    ppa = _make_ppa("zesolver", "zesolver", witness_wheel, dist_name="zealfie-solver")

    def _fake_prepare(product_id, *, resolver, fetcher, work_root):
        return ppa

    monkeypatch.setattr(service, "prepare_product_artifact", _fake_prepare)

    fake_deploy_result = DeploymentResult(success=True, active_slot_id="rt-c15")

    def _fake_install(prepared_artifacts, *, dependency_wheelhouse=None, probe_distribution=None, accelerated_acquirer=None):
        store.select(ppa.product_id, catalog=catalog)
        return fake_deploy_result

    monkeypatch.setattr(service, "install_prepared_product_deployment", _fake_install)

    work_root = tmp_path / "work"

    # First install
    result1 = service.install_product(
        "zesolver",
        resolver=_fake_resolver,
        fetcher=_fake_fetcher,
        work_root=work_root,
        dependency_wheelhouse=None,
    )
    assert result1.success is True

    # Second install
    result2 = service.install_product(
        "zesolver",
        resolver=_fake_resolver,
        fetcher=_fake_fetcher,
        work_root=work_root,
        dependency_wheelhouse=None,
    )
    assert result2.success is True

    # Two staging dirs were passed
    assert len(fake_acquirer.staging_dirs) == 2
    staging1 = fake_acquirer.staging_dirs[0]
    staging2 = fake_acquirer.staging_dirs[1]

    # Both non-None
    assert staging1 is not None
    assert staging2 is not None

    # Different directories
    assert staging1 != staging2, (
        f"sequential auto-acquires must use different staging dirs: "
        f"{staging1} == {staging2}"
    )

    # Both under work_root
    assert staging1.is_relative_to(work_root)
    assert staging2.is_relative_to(work_root)

    # Both have zealfie-acq- prefix
    assert staging1.name.startswith("zealfie-acq-")
    assert staging2.name.startswith("zealfie-acq-")

    # Both cleaned
    assert not staging1.exists(), "first staging must be cleaned"
    assert not staging2.exists(), "second staging must be cleaned"

    # No leftover zealfie-acq-* dirs under work_root
    leftovers = list(work_root.glob("zealfie-acq-*"))
    assert len(leftovers) == 0, f"expected no leftover staging dirs, got {leftovers}"
