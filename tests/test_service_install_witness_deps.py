"""Dependency-bearing offline service witness (M1-2D.4.2E — LOT E).

Full service auto-acquire → wheelhouse → dependency lock → real
transactional apply chain with real transitive dependency
(mid-lib → leaf-lib), fully offline via a local PEP 503 ``file://``
simple index.

These tests are marked ``zealfie_slow`` because they create a real
venv and invoke pip.
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
from zealfie.building import build_wheel
from zealfie.components.model import EntryPointContract
from zealfie.dependencies.acquisition import (
    AcquisitionTransportError,
    AcquiredWheel,
)
from zealfie.dependencies.pip_acquirer import (
    PipWheelhouseAcquirer,
)
from zealfie.releases.model import VerifiedArtifact
from zealfie.runtime.artifact_cache import ArtifactCacheStore
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

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MID_SHA = "a000000000000000000000000000000000000000"
LEAF_SHA = "b000000000000000000000000000000000000000"


def _fa_sha256(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _build_fixture_wheels(wheelhouse: Path) -> tuple[Path, Path]:
    """Build mid-lib and leaf-lib wheels from fixtures into *wheelhouse*.

    Returns (mid_lib_wheel, leaf_lib_wheel).
    """
    import shutil

    mid_src = FIXTURES / "mid_lib"
    leaf_src = FIXTURES / "leaf_lib"

    # build_wheel requires an empty output_dir — use separate temp dirs.
    mid_tmp = wheelhouse / "_build_mid"
    leaf_tmp = wheelhouse / "_build_leaf"
    mid_tmp.mkdir()
    leaf_tmp.mkdir()

    mid_built = build_wheel(mid_src, output_dir=mid_tmp)
    leaf_built = build_wheel(leaf_src, output_dir=leaf_tmp)

    mid = wheelhouse / mid_built.name
    leaf = wheelhouse / leaf_built.name
    shutil.copy(mid_built, mid)
    shutil.copy(leaf_built, leaf)

    return mid, leaf


def _make_pep503_index(
    index_root: Path,
    wheelhouse: Path,
    mid_wheel: Path,
    leaf_wheel: Path,
) -> Path:
    """Create a minimal PEP 503 ``simple/`` index directory.

    Returns the index root (``index_root`` itself).  The index will be
    served as ``file://<index_root>`` by pip.
    """
    index_root.mkdir(parents=True, exist_ok=True)

    mid_sha = _fa_sha256(mid_wheel)
    leaf_sha = _fa_sha256(leaf_wheel)

    # mid-lib project page
    mid_dir = index_root / "mid-lib"
    mid_dir.mkdir(exist_ok=True)
    (mid_dir / "index.html").write_text(
        '<!DOCTYPE html>\n<html><body>\n'
        f'<a href="{mid_wheel.as_uri()}#sha256={mid_sha}">'
        f'{mid_wheel.name}</a>\n'
        '</body></html>\n'
    )

    # leaf-lib project page
    leaf_dir = index_root / "leaf-lib"
    leaf_dir.mkdir(exist_ok=True)
    (leaf_dir / "index.html").write_text(
        '<!DOCTYPE html>\n<html><body>\n'
        f'<a href="{leaf_wheel.as_uri()}#sha256={leaf_sha}">'
        f'{leaf_wheel.name}</a>\n'
        '</body></html>\n'
    )

    return index_root


def _make_mid_ppa(mid_wheel: Path) -> PreparedProductArtifact:
    """Build a PreparedProductArtifact from the real mid_lib wheel."""
    assert mid_wheel.exists(), f"mid wheel not found: {mid_wheel}"
    remote = RemoteSource(owner="test", repo="midlib", ref="main")
    resolved = ResolvedSource(source=remote, commit_sha=MID_SHA)
    verified = VerifiedArtifact(
        component_id="midlib",
        version="1.0.0",
        path=mid_wheel,
        size=mid_wheel.stat().st_size,
        sha256=_fa_sha256(mid_wheel),
        distribution_name="mid-lib",
        wheel_version="1.0.0",
    )
    return PreparedProductArtifact(
        product_id="midlib",
        component_id="midlib",
        resolved_source=resolved,
        wheel_path=mid_wheel,
        verified_artifact=verified,
    )


def _mid_catalog() -> ProductCatalog:
    """Catalog with mid-lib as the only product.

    mid-lib has no console_scripts — the test focuses on dependency
    acquisition, not launch.  Runtime readiness is sufficient.
    """
    return ProductCatalog((
        ProductDescriptor(
            product_id="midlib",
            display_name="MidLib",
            distribution_name="mid-lib",
            launch_entry_points=(),
            required_extras=(),
            remote_source=RemoteSource(
                owner="test",
                repo="midlib",
                ref="main",
            ),
        ),
    ))


def _fake_resolver(owner: str, repo: str, ref: str) -> str:
    return MID_SHA


def _fake_fetcher(owner: str, repo: str, commit_sha: str) -> bytes:
    return b"fake-midlib-archive"


# ─────────────────────────────────────────────────────────────────────────
# Witness 1 — full dependency-bearing install cycle (offline local index)
# ─────────────────────────────────────────────────────────────────────────


def test_deps_witness_install_product_offline_local_index(
    tmp_path, monkeypatch,
):
    """Full ``install_product()`` for mid-lib → leaf-lib dependency
    acquisition through a local PEP 503 ``file://`` index.

    Verifies:
    - ``DeploymentResult.success == True``
    - Active runtime is ``READY``
    - mid-lib (product) distribution installed: v1.0.0
    - leaf-lib (dependency) distribution installed: v1.0.0
    - Acquired dependency wheel set contains leaf-lib only
      (product wheel stripped)
    - Selection persisted after success
    - Auto-acquired staging cleaned after service returns
    - Full chain uses real acquirer, real resolver, real apply
    - No network: local file index only
    """
    # --- Build wheels and local index ---
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    mid_wheel, leaf_wheel = _build_fixture_wheels(wheelhouse)

    index_root = tmp_path / "simple"
    _make_pep503_index(index_root, wheelhouse, mid_wheel, leaf_wheel)
    index_url = index_root.as_uri()  # file:///...

    # --- Catalog, selection store, runtime ---
    catalog = _mid_catalog()
    sel_path = tmp_path / "desired-products.toml"
    store = SelectionStore(path=sel_path)

    layout = RuntimeLayout(root=tmp_path / "rt")
    runtime = SharedRuntime(layout=layout)
    assert runtime.status().state == RuntimeState.ABSENT

    # --- Service with production acquirer pointed at local index ---
    acquirer = PipWheelhouseAcquirer(index_url=index_url)
    service = ZeAlfieService(
        catalog=catalog,
        runtime=runtime,
        selection_store=store,
        acquirer=acquirer,
    )

    # --- Monkeypatch prepare_product_artifact ---
    ppa = _make_mid_ppa(mid_wheel)
    prepare_calls: list[str] = []

    def _fake_prepare(product_id, *, resolver, fetcher, work_root):
        prepare_calls.append(product_id)
        return ppa

    monkeypatch.setattr(service, "prepare_product_artifact", _fake_prepare)

    # --- Track staging lifecycle ---
    captured_staging: list[Path] = []

    original_acquire = acquirer.acquire

    def _tracking_acquire(
        request, *, staging_dir=None, timeout_seconds=300,
        cache: ArtifactCacheStore | None = None,
        proven_requirements: tuple[tuple[str, str], ...] = (),
    ):
        result = original_acquire(
            request,
            staging_dir=staging_dir,
            timeout_seconds=timeout_seconds,
            cache=cache,
            proven_requirements=proven_requirements,
        )
        captured_staging.append(result.staging_wheelhouse)
        return result

    monkeypatch.setattr(acquirer, "acquire", _tracking_acquire)

    # --- Install product (auto-acquire via local file index) ---
    result = service.install_product(
        "midlib",
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
    assert prepare_calls[0] == "midlib"

    # ── Verify runtime is READY ──────────────────────────────────────
    status = runtime.status()
    assert status.state == RuntimeState.READY, (
        f"expected READY, got {status.state}: {status.reason}"
    )
    assert status.active_slot_id == result.active_slot_id

    # ── Verify mid-lib (product) distribution installed ──────────────
    active_python = runtime.python()
    assert active_python is not None
    mid_probe = probe_runtime_distribution(active_python, "mid-lib")
    assert mid_probe["installed"] is True, (
        f"mid-lib not installed: {mid_probe}"
    )
    assert mid_probe["version"] == "1.0.0", (
        f"wrong mid-lib version: {mid_probe}"
    )

    # ── Verify leaf-lib (dependency) distribution installed ──────────
    leaf_probe = probe_runtime_distribution(active_python, "leaf-lib")
    assert leaf_probe["installed"] is True, (
        f"leaf-lib not installed: {leaf_probe}"
    )
    assert leaf_probe["version"] == "1.0.0", (
        f"wrong leaf-lib version: {leaf_probe}"
    )

    # ── Verify selection persisted after success ─────────────────────
    store.reload()
    assert "midlib" in store.selected_product_ids, (
        "selection must be persisted after success"
    )

    # ── Verify acquired staging was cleaned ──────────────────────────
    assert len(captured_staging) == 1, "acquirer should have been called once"
    assert not captured_staging[0].exists(), (
        "auto-acquired staging must be cleaned after install_product returns"
    )


# ─────────────────────────────────────────────────────────────────────────
# Witness 2 — staging content: only leaf-lib, product wheel stripped
# ─────────────────────────────────────────────────────────────────────────


def test_deps_witness_staging_contains_only_dependency(
    tmp_path,
):
    """Direct acquirer call across local file index:
    staging contains leaf-lib but NOT mid-lib (product).

    Uses the production ``PipWheelhouseAcquirer`` with the local
    ``file://`` index.  No mocks on subprocess — this is a real
    ``pip download`` execution.
    """
    import zealfie.dependencies.acquisition as acq_mod

    # --- Build wheels and local index ---
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    mid_wheel, leaf_wheel = _build_fixture_wheels(wheelhouse)

    index_root = tmp_path / "simple"
    _make_pep503_index(index_root, wheelhouse, mid_wheel, leaf_wheel)
    index_url = index_root.as_uri()

    # --- Acquire with local index ---
    req = acq_mod.build_acquisition_request(mid_wheel, active_extras=frozenset())
    acquirer = PipWheelhouseAcquirer(index_url=index_url)

    result = acquirer.acquire(req, staging_dir=tmp_path / "staging")

    # ── Staging exists ───────────────────────────────────────────────
    assert result.staging_wheelhouse.exists()
    assert result.staging_wheelhouse == (tmp_path / "staging").resolve()

    # ── Only leaf-lib remains (product wheel stripped) ───────────────
    remaining = list(result.staging_wheelhouse.glob("*.whl"))
    remaining_names = [r.name for r in remaining]
    assert len(remaining) == 1, (
        f"expected exactly 1 dependency wheel, got {len(remaining)}: "
        f"{remaining_names}"
    )
    assert "leaf" in remaining_names[0].lower(), (
        f"expected leaf-lib, got {remaining_names}"
    )

    # ── Acquired record matches ──────────────────────────────────────
    assert len(result.acquired) == 1
    assert result.acquired[0].name == "leaf-lib"
    assert result.acquired[0].version == "1.0.0"
    assert result.acquired[0].filename.endswith(".whl")
    assert len(result.acquired[0].sha256) == 64

    # Compare SHA256 to the real leaf wheel
    expected_sha = _fa_sha256(leaf_wheel)
    assert result.acquired[0].sha256 == expected_sha, (
        f"acquired SHA256 {result.acquired[0].sha256[:16]}... "
        f"≠ expected {expected_sha[:16]}..."
    )


# ─────────────────────────────────────────────────────────────────────────
# Witness 3 — acquisition failure with local index
# ─────────────────────────────────────────────────────────────────────────


def test_deps_witness_acquisition_failure_local_index(
    tmp_path, monkeypatch,
):
    """When the acquirer raises during auto-acquire (e.g. index is
    missing a dependency), the service wraps the error and does NOT
    mutate the selection store or runtime."""

    # --- Build wheels — but create an incomplete index missing leaf-lib ---
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    mid_wheel, _leaf_wheel = _build_fixture_wheels(wheelhouse)

    # Create an index with only mid-lib, not leaf-lib → pip will fail
    bad_index = tmp_path / "incomplete-simple"
    mid_dir = bad_index / "mid-lib"
    mid_dir.mkdir(parents=True)
    mid_sha = _fa_sha256(mid_wheel)
    (mid_dir / "index.html").write_text(
        '<!DOCTYPE html>\n<html><body>\n'
        f'<a href="{mid_wheel.as_uri()}#sha256={mid_sha}">'
        f'{mid_wheel.name}</a>\n'
        '</body></html>\n'
    )
    # Deliberately missing leaf-lib project page

    catalog = _mid_catalog()
    store = SelectionStore(path=tmp_path / "sel.toml")

    layout = RuntimeLayout(root=tmp_path / "rt")
    runtime = SharedRuntime(layout=layout)

    acquirer = PipWheelhouseAcquirer(index_url=bad_index.as_uri())
    service = ZeAlfieService(
        catalog=catalog,
        runtime=runtime,
        selection_store=store,
        acquirer=acquirer,
    )

    ppa = _make_mid_ppa(mid_wheel)

    def _fake_prepare(product_id, *, resolver, fetcher, work_root):
        return ppa

    monkeypatch.setattr(service, "prepare_product_artifact", _fake_prepare)

    with pytest.raises(ProductDependencyAcquisitionError) as exc_info:
        service.install_product(
            "midlib",
            resolver=_fake_resolver,
            fetcher=_fake_fetcher,
            work_root=tmp_path / "work",
            dependency_wheelhouse=None,
        )

    # Error is correctly wrapped.
    assert "dependency acquisition failed for 'midlib'" in str(exc_info.value)
    assert exc_info.value.__cause__ is not None
    assert isinstance(exc_info.value.__cause__, AcquisitionTransportError)

    # Selection was NOT mutated.
    store.reload()
    assert "midlib" not in store.selected_product_ids

    # Runtime state was NOT mutated.
    status = runtime.status()
    assert status.state == RuntimeState.ABSENT
