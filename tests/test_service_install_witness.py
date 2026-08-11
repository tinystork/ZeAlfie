"""Witness integration tests for M1-2D.4.2D — real service install
path through ``install_product()`` with the production
``PipWheelhouseAcquirer`` and ``install_prepared_product_deployment``.

These tests exercise the full service install/acquire pipeline end to
end using a real witness wheel (no transitive dependencies) and a real
shared runtime.  They are marked ``zealfie_slow`` because they create a
real venv and invoke pip.

FAST: deselected by ``not zealfie_slow``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.zealfie_slow

from zealfie.app import (
    PreparedProductArtifact,
    ProductCatalog,
    ProductDescriptor,
    SelectionStore,
    ZeAlfieService,
)
from zealfie.app.service import (
    ProductDependencyAcquisitionError,
)
from zealfie.components.model import EntryPointContract
from zealfie.dependencies.acquisition import (
    AcquisitionTransportError,
)
from zealfie.dependencies.pip_acquirer import (
    PipWheelhouseAcquirer,
)
from zealfie.releases.model import VerifiedArtifact
from zealfie.runtime.layout import RuntimeLayout
from zealfie.runtime.manager import SharedRuntime
from zealfie.runtime.model import (
    DeploymentResult,
    RuntimeState,
)
from zealfie.runtime.probe import probe_runtime_distribution
from zealfie.sources import RemoteSource, ResolvedSource


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────


WITNESS_SHA = "d4a0f1e2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8"


def _fa_sha256(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _make_witness_ppa(witness_wheel: Path) -> PreparedProductArtifact:
    """Build a ``PreparedProductArtifact`` from the real witness wheel.

    The witness wheel (zealfie-witness v0.0.1) has no Requires-Dist
    and no Provides-Extra — ideal for a no-dependency install path.
    """
    assert witness_wheel.exists(), f"witness wheel not found: {witness_wheel}"
    remote = RemoteSource(owner="tinystork", repo="ZeWitness", ref="main")
    resolved = ResolvedSource(source=remote, commit_sha=WITNESS_SHA)
    verified = VerifiedArtifact(
        component_id="zewitness",
        version="0.0.1",
        path=witness_wheel,
        size=witness_wheel.stat().st_size,
        sha256=_fa_sha256(witness_wheel),
        distribution_name="zealfie-witness",
        wheel_version="0.0.1",
    )
    return PreparedProductArtifact(
        product_id="zewitness",
        component_id="zewitness",
        resolved_source=resolved,
        wheel_path=witness_wheel,
        verified_artifact=verified,
    )


def _witness_catalog() -> ProductCatalog:
    """Catalog containing only the witness product (no required_extras)."""
    return ProductCatalog((
        ProductDescriptor(
            product_id="zewitness",
            display_name="ZeWitness",
            distribution_name="zealfie-witness",
            launch_entry_points=(
                EntryPointContract("console_scripts", "zewitness"),
            ),
            required_extras=(),
            remote_source=RemoteSource(
                owner="tinystork",
                repo="ZeWitness",
                ref="main",
            ),
        ),
    ))


def _fake_resolver(owner: str, repo: str, ref: str) -> str:
    return WITNESS_SHA


def _fake_fetcher(owner: str, repo: str, commit_sha: str) -> bytes:
    return b"fake-archive"


# ─────────────────────────────────────────────────────────────────────────
# Acceptance test 1 — full witness install cycle (production acquirer)
# ─────────────────────────────────────────────────────────────────────────


def test_witness_install_product_real_acquirer_real_apply(
    tmp_path, witness_wheel, monkeypatch,
):
    """Full ``install_product()`` cycle through the production
    ``PipWheelhouseAcquirer`` + real transactional
    ``install_prepared_product_deployment``.

    Verifies:
    - ``DeploymentResult.success == True``
    - Active runtime is ``READY``
    - Witness distribution is installed (``zealfie-witness`` v0.0.1)
    - Launch contract / entrypoint is detectable
    - Selection persisted only after success
    - Acquired dependency staging is empty for no-deps witness
    - Acquired staging is cleaned after service returns
    """
    # --- Catalog, selection store, runtime ---
    catalog = _witness_catalog()
    sel_path = tmp_path / "desired-products.toml"
    store = SelectionStore(path=sel_path)

    layout = RuntimeLayout(root=tmp_path / "rt")
    runtime = SharedRuntime(layout=layout)
    # Runtime starts ABSENT (never created).
    assert runtime.status().state == RuntimeState.ABSENT

    # --- Service with PRODUCTION acquirer ---
    acquirer = PipWheelhouseAcquirer()
    service = ZeAlfieService(
        catalog=catalog,
        runtime=runtime,
        selection_store=store,
        acquirer=acquirer,
    )

    # --- Monkeypatch prepare_product_artifact ---
    ppa = _make_witness_ppa(witness_wheel)
    prepare_calls: list[str] = []

    def _fake_prepare(product_id, *, resolver, fetcher, work_root):
        prepare_calls.append(product_id)
        return ppa

    monkeypatch.setattr(service, "prepare_product_artifact", _fake_prepare)

    # --- Track staging lifecycle ---
    captured_staging: list[Path] = []

    original_acquire = acquirer.acquire

    def _tracking_acquire(request, *, staging_dir=None, timeout_seconds=300):
        result = original_acquire(request, staging_dir=staging_dir,
                                  timeout_seconds=timeout_seconds)
        captured_staging.append(result.staging_wheelhouse)
        return result

    monkeypatch.setattr(acquirer, "acquire", _tracking_acquire)

    # --- Install product (auto-acquire) ---
    result = service.install_product(
        "zewitness",
        resolver=_fake_resolver,
        fetcher=_fake_fetcher,
        work_root=tmp_path / "work",
        dependency_wheelhouse=None,  # triggers auto-acquire
    )

    # ── Verify DeploymentResult ──────────────────────────────────────
    assert isinstance(result, DeploymentResult)
    assert result.success is True, f"install_product failed: {result.reason}"
    assert result.active_slot_id is not None

    # ── Verify prepare was called ────────────────────────────────────
    assert len(prepare_calls) == 1
    assert prepare_calls[0] == "zewitness"

    # ── Verify runtime is READY ──────────────────────────────────────
    status = runtime.status()
    assert status.state == RuntimeState.READY, (
        f"expected READY, got {status.state}: {status.reason}"
    )
    assert status.active_slot_id == result.active_slot_id

    # ── Verify witness distribution installed ────────────────────────
    active_python = runtime.python()
    assert active_python is not None
    probe = probe_runtime_distribution(active_python, "zealfie-witness")
    assert probe["installed"] is True, f"witness not installed: {probe}"
    assert probe["version"] == "0.0.1", f"wrong version: {probe}"

    # ── Verify launch contract / entrypoint detectable ───────────────
    plan = service.prepare_launch_plan("zewitness")
    assert plan.component_id == "zewitness"
    assert plan.executable.is_file(), f"entrypoint missing: {plan.executable}"
    assert "zewitness" in str(plan.executable)

    # ── Verify selection persisted only after success ────────────────
    store.reload()
    assert "zewitness" in store.selected_product_ids, (
        "selection must be persisted after success"
    )

    # ── Verify acquired staging was cleaned ──────────────────────────
    assert len(captured_staging) == 1, "acquirer should have been called once"
    assert not captured_staging[0].exists(), (
        "auto-acquired staging must be cleaned after install_product returns"
    )


# ─────────────────────────────────────────────────────────────────────────
# Acceptance test 2 — staging content for no-deps witness
# ─────────────────────────────────────────────────────────────────────────


def test_witness_acquired_staging_empty_no_product_wheel(
    tmp_path, witness_wheel, monkeypatch,
):
    """For a no-dependency witness, the acquired staging wheelhouse is
    empty after product-wheel removal and contains no ``.whl`` files.

    This test isolates the acquirer step to verify the staging contains
    no wheels (the product wheel copy is stripped).  Uses the production
    ``PipWheelhouseAcquirer`` directly.
    """
    import zealfie.dependencies.acquisition as acq_mod

    req = acq_mod.build_acquisition_request(witness_wheel, active_extras=frozenset())
    acquirer = PipWheelhouseAcquirer()

    result = acquirer.acquire(req, staging_dir=tmp_path / "staging")

    # Staging directory exists and is the one we supplied.
    assert result.staging_wheelhouse.exists()
    assert result.staging_wheelhouse == (tmp_path / "staging").resolve()

    # No wheels remain in staging (product wheel copy was removed).
    remaining = list(result.staging_wheelhouse.glob("*.whl"))
    assert len(remaining) == 0, (
        f"expected empty staging, found {[r.name for r in remaining]}"
    )
    assert result.acquired == ()


# ─────────────────────────────────────────────────────────────────────────
# Acceptance test 3 — staging cleaned after service returns
# ─────────────────────────────────────────────────────────────────────────


def test_witness_staging_cleaned_after_service_returns(
    tmp_path, witness_wheel, monkeypatch,
):
    """The auto-acquired staging directory is always cleaned after
    ``install_product`` returns, regardless of success or failure.

    We verify this for the success path by checking the staging path
    captured during acquisition is gone when ``install_product`` raises
    or returns.  Uses the production ``PipWheelhouseAcquirer``.
    """
    catalog = _witness_catalog()
    store = SelectionStore(path=tmp_path / "sel.toml")

    layout = RuntimeLayout(root=tmp_path / "rt")
    runtime = SharedRuntime(layout=layout)

    acquirer = PipWheelhouseAcquirer()
    service = ZeAlfieService(
        catalog=catalog,
        runtime=runtime,
        selection_store=store,
        acquirer=acquirer,
    )

    ppa = _make_witness_ppa(witness_wheel)
    prepare_calls: list[str] = []

    def _fake_prepare(product_id, *, resolver, fetcher, work_root):
        prepare_calls.append(product_id)
        return ppa

    monkeypatch.setattr(service, "prepare_product_artifact", _fake_prepare)

    # Capture staging path
    captured_staging: list[Path] = []

    original_acquire = acquirer.acquire

    def _tracking_acquire(request, *, staging_dir=None, timeout_seconds=300):
        result = original_acquire(request, staging_dir=staging_dir,
                                  timeout_seconds=timeout_seconds)
        captured_staging.append(result.staging_wheelhouse)
        return result

    monkeypatch.setattr(acquirer, "acquire", _tracking_acquire)

    result = service.install_product(
        "zewitness",
        resolver=_fake_resolver,
        fetcher=_fake_fetcher,
        work_root=tmp_path / "work",
        dependency_wheelhouse=None,
    )

    assert result.success is True
    assert len(captured_staging) == 1
    assert not captured_staging[0].exists(), (
        "auto-acquired staging must be cleaned after install_product returns"
    )


# ─────────────────────────────────────────────────────────────────────────
# Acceptance test 4 — acquisition failure does not mutate state
# ─────────────────────────────────────────────────────────────────────────


def test_witness_acquisition_failure_wraps_error_does_not_mutate_state(
    tmp_path, witness_wheel, monkeypatch,
):
    """When the acquirer raises during auto-acquire, the service wraps
    the failure in ``ProductDependencyAcquisitionError`` and does NOT
    mutate the selection store or runtime.

    The service never receives a ``DependencyAcquisitionResult`` on
    failure, so ``auto_staging`` remains ``None`` and the ``finally``
    block is a no-op.  The acquirer's own exception handler manages
    any staging cleanup the acquirer created internally.  This test
    ensures the test-local fake acquirer does not leak temp dirs by
    placing them under ``tmp_path``.
    """
    catalog = _witness_catalog()
    store = SelectionStore(path=tmp_path / "sel.toml")

    layout = RuntimeLayout(root=tmp_path / "rt")
    runtime = SharedRuntime(layout=layout)

    # Acquirer that raises after creating staging under tmp_path.
    # Because acquire() raises directly, the service never receives a
    # DependencyAcquisitionResult, so auto_staging stays None and the
    # service's finally block does nothing — the fake owns cleanup.
    fake_staging = tmp_path / "fake-staging"
    fake_staging.mkdir(parents=True, exist_ok=True)

    class _FailingAcquirer:
        def acquire(self, request, *, staging_dir=None, timeout_seconds=300):
            # A real acquirer could create a staging dir internally
            # before failing.  Place it under tmp_path so it is
            # cleaned by pytest.
            (fake_staging / ".sentinel").touch()
            raise AcquisitionTransportError("pip-download", "simulated failure")

    failing_acquirer = _FailingAcquirer()
    service = ZeAlfieService(
        catalog=catalog,
        runtime=runtime,
        selection_store=store,
        acquirer=failing_acquirer,
    )

    ppa = _make_witness_ppa(witness_wheel)

    def _fake_prepare(product_id, *, resolver, fetcher, work_root):
        return ppa

    monkeypatch.setattr(service, "prepare_product_artifact", _fake_prepare)

    with pytest.raises(ProductDependencyAcquisitionError) as exc_info:
        service.install_product(
            "zewitness",
            resolver=_fake_resolver,
            fetcher=_fake_fetcher,
            work_root=tmp_path / "work",
            dependency_wheelhouse=None,
        )

    # Error is correctly wrapped.
    assert "dependency acquisition failed for 'zewitness'" in str(exc_info.value)
    assert exc_info.value.__cause__ is not None
    assert isinstance(exc_info.value.__cause__, AcquisitionTransportError)

    # Selection was NOT mutated.
    store.reload()
    assert "zewitness" not in store.selected_product_ids

    # Runtime state was NOT mutated (still ABSENT — never deployed).
    status = runtime.status()
    assert status.state == RuntimeState.ABSENT, (
        f"expected ABSENT, got {status.state}"
    )
