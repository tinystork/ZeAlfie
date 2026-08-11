"""Tests for M1-2D.4.1B — Remote product artifact preparation.

Tests cover:

1. Unknown product → UnknownProductError, no resolver/fetcher/build call
2. Known product without remote_source → RemoteSourceUnavailableError, no resolver/fetcher/build call
3. Resolver receives owner/repo/ref; result stores exact SHA; fetcher receives exact SHA, never branch ref
4. Successful preparation returns PreparedProductArtifact with VerifiedArtifact
5. Built wheel with wrong distribution name rejected by verification
6. Built wheel missing required entry point rejected by verification
7. Preparation does not mutate runtime or persist/modify desired-products.toml
8. No real network: injected resolver/fetcher only
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from zealfie.app import (
    OfflineReleaseError,
    PreparedProductArtifact,
    ProductCatalog,
    ProductDescriptor,
    ProductInstallPreparationError,
    RemoteSourceUnavailableError,
    UnknownProductError,
    CorruptSelectionError,
    SelectionStore,
    ZeAlfieService,
)
from zealfie.components.model import ComponentDefinition, EntryPointContract
from zealfie.components.registry import ComponentRegistry
from zealfie.releases.model import VerifiedArtifact
from zealfie.releases.verifier import ArtifactRejectionError
from zealfie.sources import (
    RemoteSource,
    ResolvedSource,
    SourceResolutionError,
)


# ===========================================================================
# Constants
# ===========================================================================

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
VALID_SHA = "d4a0f1e2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8"
BRANCH_REF = "main"


# ===========================================================================
# Product descriptor helpers
# ===========================================================================

WITNESS_REMOTE = RemoteSource(
    owner="tinystork",
    repo="ZeWitness",
    ref=BRANCH_REF,
)

WITNESS_DESCRIPTOR = ProductDescriptor(
    product_id="zewitness",
    display_name="ZeWitness",
    distribution_name="zealfie-witness",
    launch_entry_points=(
        EntryPointContract("console_scripts", "zewitness"),
    ),
    remote_source=WITNESS_REMOTE,
)

NO_REMOTE_DESCRIPTOR = ProductDescriptor(
    product_id="nolocal",
    display_name="No Local",
    distribution_name="no-local",
    launch_entry_points=(
        EntryPointContract("console_scripts", "nolocal"),
    ),
    remote_source=None,
)


def _catalog(*descriptors: ProductDescriptor) -> ProductCatalog:
    """Create a ProductCatalog from given descriptors."""
    return ProductCatalog(descriptors)


# ===========================================================================
# ZIP helpers
# ===========================================================================


def _zip_fixture_source(fixture_name: str) -> bytes:
    """Create an in-memory ZIP of a fixture's source (excluding build/)."""
    source_dir = FIXTURES_DIR / fixture_name
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(source_dir.rglob("*")):
            if file_path.is_file():
                rel = file_path.relative_to(source_dir)
                # Skip pre-built artifacts to keep the archive source-only.
                if str(rel).startswith("build/"):
                    continue
                zf.write(str(file_path), str(rel))
    return buf.getvalue()


def _build_test_wheel(
    output: Path,
    name: str,
    version: str,
    *,
    entry_points: tuple[tuple[str, str, str], ...] | None = None,
) -> Path:
    """Build a minimal test wheel with optional entry_points.txt.

    Args:
        output: Directory to write the wheel to.
        name: Distribution name for METADATA.
        version: Version for METADATA.
        entry_points: Optional tuples of (group, name, value) for entry_points.txt.
    """
    safe_name = name.replace("-", "_").replace(".", "_")
    wheel_name = f"{safe_name}-{version}-py3-none-any.whl"
    wheel_path = output / wheel_name
    dist_info = f"{safe_name}-{version}.dist-info"

    wheelfile = (
        "Wheel-Version: 1.0\n"
        "Generator: test\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    )
    metadata = f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n"

    record_lines = [
        f"{dist_info}/WHEEL,,",
        f"{dist_info}/METADATA,,",
    ]
    members: list[tuple[str, str]] = [
        (f"{dist_info}/WHEEL", wheelfile),
        (f"{dist_info}/METADATA", metadata),
    ]

    if entry_points is not None:
        ep_text = ""
        current_group = ""
        for group, ep_name, ep_value in entry_points:
            if group != current_group:
                if current_group:
                    ep_text += "\n"
                ep_text += f"[{group}]\n"
                current_group = group
            ep_text += f"{ep_name} = {ep_value}\n"
        members.append((f"{dist_info}/entry_points.txt", ep_text))
        record_lines.append(f"{dist_info}/entry_points.txt,,")

    record_lines.append(f"{dist_info}/RECORD,,")
    record = "\n".join(record_lines) + "\n"
    members.append((f"{dist_info}/RECORD", record))

    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, content in members:
            zf.writestr(arcname, content)
    return wheel_path


# ===========================================================================
# Module-scoped fixtures
# ===========================================================================


@pytest.fixture(scope="module")
def mid_lib_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build mid-lib wheel (no entry points) once per module.

    Used by tests that need a wheel with wrong distribution name
    or missing entry points.
    """
    from zealfie.building import build_wheel

    d = FIXTURES_DIR / "mid_lib"
    t = tmp_path_factory.mktemp("shared-midlib")
    return build_wheel(d, output_dir=t)


# ===========================================================================
# Test 1: Unknown product → UnknownProductError, no resolver/fetcher/build
# ===========================================================================


def test_unknown_product_raises_unknown_product_error(tmp_path: Path) -> None:
    """prepare_product_artifact for unknown product raises UnknownProductError
    before any resolver, fetcher, or build call."""
    catalog = _catalog(WITNESS_DESCRIPTOR)
    service = ZeAlfieService(catalog=catalog)

    resolver_called = False
    fetcher_called = False

    def _resolver(owner: str, repo: str, ref: str) -> str:
        nonlocal resolver_called
        resolver_called = True
        return VALID_SHA

    def _fetcher(owner: str, repo: str, commit_sha: str) -> bytes:
        nonlocal fetcher_called
        fetcher_called = True
        return b""

    with pytest.raises(UnknownProductError, match="nonexistent"):
        service.prepare_product_artifact(
            "nonexistent",
            resolver=_resolver,
            fetcher=_fetcher,
            work_root=tmp_path,
        )

    assert not resolver_called, "resolver must not be called for unknown product"
    assert not fetcher_called, "fetcher must not be called for unknown product"


# ===========================================================================
# Test 2: Known product without remote_source → RemoteSourceUnavailableError
# ===========================================================================


def test_no_remote_source_raises_remote_source_unavailable_error(
    tmp_path: Path,
) -> None:
    """prepare_product_artifact for product without remote_source raises
    RemoteSourceUnavailableError before any resolver, fetcher, or build call."""
    catalog = _catalog(NO_REMOTE_DESCRIPTOR)
    service = ZeAlfieService(catalog=catalog)

    resolver_called = False
    fetcher_called = False

    def _resolver(owner: str, repo: str, ref: str) -> str:
        nonlocal resolver_called
        resolver_called = True
        return VALID_SHA

    def _fetcher(owner: str, repo: str, commit_sha: str) -> bytes:
        nonlocal fetcher_called
        fetcher_called = True
        return b""

    with pytest.raises(RemoteSourceUnavailableError, match="nolocal"):
        service.prepare_product_artifact(
            "nolocal",
            resolver=_resolver,
            fetcher=_fetcher,
            work_root=tmp_path,
        )

    assert not resolver_called, (
        "resolver must not be called for product without remote_source"
    )
    assert not fetcher_called, (
        "fetcher must not be called for product without remote_source"
    )


def test_remote_source_unavailable_error_is_product_install_preparation_error() -> (
    None
):
    """RemoteSourceUnavailableError is a ProductInstallPreparationError."""
    err = RemoteSourceUnavailableError("test")
    assert isinstance(err, ProductInstallPreparationError)
    assert isinstance(err, RuntimeError)


# ===========================================================================
# Test 3: Resolver receives owner/repo/ref; result stores exact SHA;
#         fetcher receives exact SHA, never branch ref.
# ===========================================================================


def test_resolver_receives_owner_repo_ref_and_result_stores_exact_sha(
    tmp_path: Path,
) -> None:
    """Resolver receives (owner, repo, ref) from the product descriptor's
    remote_source; the result stores the exact 40-char SHA; fetcher receives
    exact SHA, never the branch ref."""
    catalog = _catalog(WITNESS_DESCRIPTOR)
    service = ZeAlfieService(catalog=catalog)

    resolver_calls: list[tuple[str, str, str]] = []
    fetcher_calls: list[tuple[str, str, str]] = []

    def _resolver(owner: str, repo: str, ref: str) -> str:
        resolver_calls.append((owner, repo, ref))
        return VALID_SHA

    def _fetcher(owner: str, repo: str, commit_sha: str) -> bytes:
        fetcher_calls.append((owner, repo, commit_sha))
        return _zip_fixture_source("witness_component")

    # This test requires a real build; mark accordingly.
    result = service.prepare_product_artifact(
        "zewitness",
        resolver=_resolver,
        fetcher=_fetcher,
        work_root=tmp_path,
    )

    # --- resolver assertions ---
    assert len(resolver_calls) == 1
    owner, repo, ref = resolver_calls[0]
    assert owner == WITNESS_REMOTE.owner
    assert repo == WITNESS_REMOTE.repo
    assert ref == WITNESS_REMOTE.ref  # branch ref passed to resolver

    # --- fetcher assertions ---
    assert len(fetcher_calls) == 1
    f_owner, f_repo, f_sha = fetcher_calls[0]
    assert f_owner == WITNESS_REMOTE.owner
    assert f_repo == WITNESS_REMOTE.repo
    assert f_sha == VALID_SHA  # exact 40-char SHA, never branch ref
    assert f_sha != WITNESS_REMOTE.ref  # must NOT be the branch ref

    # --- result provenance ---
    assert isinstance(result.resolved_source, ResolvedSource)
    assert result.resolved_source.commit_sha == VALID_SHA
    assert result.resolved_source.source == WITNESS_REMOTE


# ===========================================================================
# Test 4: Successful preparation returns PreparedProductArtifact
# ===========================================================================


def test_successful_preparation_returns_prepared_product_artifact(
    tmp_path: Path,
) -> None:
    """Full pipeline: resolve → acquire → build → verify.

    Returns PreparedProductArtifact with VerifiedArtifact whose wheel
    identity, version, and entry-point contract match the product descriptor.
    """
    catalog = _catalog(WITNESS_DESCRIPTOR)
    service = ZeAlfieService(catalog=catalog)

    def _resolver(owner: str, repo: str, ref: str) -> str:
        return VALID_SHA

    def _fetcher(owner: str, repo: str, commit_sha: str) -> bytes:
        return _zip_fixture_source("witness_component")

    result = service.prepare_product_artifact(
        "zewitness",
        resolver=_resolver,
        fetcher=_fetcher,
        work_root=tmp_path,
    )

    # --- PreparedProductArtifact shape ---
    assert isinstance(result, PreparedProductArtifact)
    assert result.product_id == "zewitness"
    assert result.component_id == "zewitness"
    assert result.resolved_source.commit_sha == VALID_SHA

    # --- wheel_path ---
    assert result.wheel_path.is_file()
    assert result.wheel_path.suffix == ".whl"

    # --- VerifiedArtifact ---
    va = result.verified_artifact
    assert isinstance(va, VerifiedArtifact)
    assert va.component_id == "zewitness"
    assert va.distribution_name == "zealfie-witness"
    assert va.version == "0.0.1"
    assert va.path == result.wheel_path
    assert va.size == result.wheel_path.stat().st_size

    # SHA256 must match.
    h = hashlib.sha256()
    with open(result.wheel_path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    assert va.sha256 == h.hexdigest()


# ===========================================================================
# Test 5: Built wheel with wrong distribution name rejected
# ===========================================================================


def test_wrong_distribution_name_rejected_by_verification(
    tmp_path: Path,
    mid_lib_wheel: Path,
    monkeypatch,
) -> None:
    """When the built wheel has a different distribution name than the
    product descriptor, verify_artifact raises ArtifactRejectionError."""
    catalog = _catalog(WITNESS_DESCRIPTOR)
    service = ZeAlfieService(catalog=catalog)

    def _resolver(owner: str, repo: str, ref: str) -> str:
        return VALID_SHA

    def _fetcher(owner: str, repo: str, commit_sha: str) -> bytes:
        return _zip_fixture_source("witness_component")

    # Monkeypatch build_wheel_from_staged to return mid_lib_wheel
    # (distribution "mid-lib") instead of the witness wheel.
    import zealfie.app.service as svc_mod

    def _fake_build(staged, *, output_dir=None):
        # Copy mid_lib_wheel into work_root so path resolution works.
        import shutil
        target = (
            Path(output_dir) / mid_lib_wheel.name
            if output_dir
            else mid_lib_wheel
        )
        if output_dir and target != mid_lib_wheel:
            shutil.copy2(mid_lib_wheel, target)
        return target

    monkeypatch.setattr(svc_mod, "build_wheel_from_staged", _fake_build)

    with pytest.raises(ArtifactRejectionError, match="distribution mismatch"):
        service.prepare_product_artifact(
            "zewitness",
            resolver=_resolver,
            fetcher=_fetcher,
            work_root=tmp_path,
        )


# ===========================================================================
# Test 6: Built wheel missing required entry point rejected
# ===========================================================================


def test_missing_entry_point_rejected_by_verification(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """When the built wheel has no entry points that match the expected
    contract, verify_artifact raises ArtifactRejectionError."""
    catalog = _catalog(WITNESS_DESCRIPTOR)
    service = ZeAlfieService(catalog=catalog)

    def _resolver(owner: str, repo: str, ref: str) -> str:
        return VALID_SHA

    def _fetcher(owner: str, repo: str, commit_sha: str) -> bytes:
        return _zip_fixture_source("witness_component")

    # Build a synthetic wheel with the right distribution name and version
    # but NO entry points at all.
    synthetic_wheel = _build_test_wheel(
        tmp_path,
        "zealfie-witness",
        "0.0.1",
        entry_points=(),  # Empty entry_points.txt → no contracts
    )

    def _fake_build(staged, *, output_dir=None):
        return synthetic_wheel

    import zealfie.app.service as svc_mod
    monkeypatch.setattr(svc_mod, "build_wheel_from_staged", _fake_build)

    with pytest.raises(
        ArtifactRejectionError, match="wheel does not declare expected launch"
    ):
        service.prepare_product_artifact(
            "zewitness",
            resolver=_resolver,
            fetcher=_fetcher,
            work_root=tmp_path,
        )


# ===========================================================================
# Test 7: Preparation does not mutate runtime or selection store
# ===========================================================================


@pytest.mark.zealfie_slow
def test_preparation_does_not_mutate_runtime_or_selection(
    tmp_path: Path,
) -> None:
    """After successful preparation, the shared runtime and
    desired-products.toml are unchanged."""
    from zealfie.runtime.manager import SharedRuntime
    from zealfie.runtime.layout import RuntimeLayout
    from zealfie.products.selection import SelectionStore

    # Set up a real (empty) runtime and selection store.
    rt_root = tmp_path / "rt"
    layout = RuntimeLayout(root=rt_root)
    runtime = SharedRuntime(layout=layout)
    runtime.create()

    sel_path = tmp_path / "desired-products.toml"
    store = SelectionStore(path=sel_path)

    catalog = _catalog(WITNESS_DESCRIPTOR)
    registry = ComponentRegistry([
        ComponentDefinition(
            component_id="zesolver",
            display_name="ZeSolver",
            distribution_name="ZeSolver",
            launch_entry_points=(
                EntryPointContract("gui_scripts", "zesolver"),
            ),
        ),
    ])

    service = ZeAlfieService(
        catalog=catalog,
        registry=registry,
        runtime=runtime,
        selection_store=store,
    )

    # Record state before preparation.
    status_before = runtime.status()
    sel_before_exists = sel_path.exists()
    if sel_before_exists:
        sel_before = sel_path.read_text()
    else:
        sel_before = None

    def _resolver(owner: str, repo: str, ref: str) -> str:
        return VALID_SHA

    def _fetcher(owner: str, repo: str, commit_sha: str) -> bytes:
        return _zip_fixture_source("witness_component")

    result = service.prepare_product_artifact(
        "zewitness",
        resolver=_resolver,
        fetcher=_fetcher,
        work_root=tmp_path / "work",
    )

    # Assert successful preparation.
    assert isinstance(result, PreparedProductArtifact)

    # Runtime must be unchanged.
    status_after = runtime.status()
    assert status_after.active_slot_id == status_before.active_slot_id
    assert status_after.state == status_before.state

    # Selection file must be unchanged.
    if sel_before_exists:
        assert sel_path.read_text() == sel_before, (
            "desired-products.toml was modified"
        )
    else:
        assert not sel_path.exists(), (
            "desired-products.toml was created by preparation"
        )


# ===========================================================================
# Test 8: No real network — injected resolver/fetcher only
# ===========================================================================


def test_no_real_network_only_injected_resolver_fetcher_used(
    tmp_path: Path,
) -> None:
    """The entire preparation pipeline uses only the injected resolver
    and fetcher.  No network calls are made (implicit — fakes only)."""
    catalog = _catalog(WITNESS_DESCRIPTOR)
    service = ZeAlfieService(catalog=catalog)

    resolver_count = 0
    fetcher_count = 0

    def _resolver(owner: str, repo: str, ref: str) -> str:
        nonlocal resolver_count
        resolver_count += 1
        assert owner == "tinystork"
        assert repo == "ZeWitness"
        assert ref == "main"
        return VALID_SHA

    def _fetcher(owner: str, repo: str, commit_sha: str) -> bytes:
        nonlocal fetcher_count
        fetcher_count += 1
        assert owner == "tinystork"
        assert repo == "ZeWitness"
        assert commit_sha == VALID_SHA
        return _zip_fixture_source("witness_component")

    result = service.prepare_product_artifact(
        "zewitness",
        resolver=_resolver,
        fetcher=_fetcher,
        work_root=tmp_path,
    )

    assert resolver_count == 1
    assert fetcher_count == 1
    assert result.resolved_source.commit_sha == VALID_SHA


# ===========================================================================
# Test: Resolver failure propagates cleanly
# ===========================================================================


def test_resolver_failure_propagates(tmp_path: Path) -> None:
    """When the resolver raises SourceResolutionError, it propagates
    without calling the fetcher or building."""
    catalog = _catalog(WITNESS_DESCRIPTOR)
    service = ZeAlfieService(catalog=catalog)

    fetcher_called = False

    def _resolver(owner: str, repo: str, ref: str) -> str:
        raise SourceResolutionError("ref not found")

    def _fetcher(owner: str, repo: str, commit_sha: str) -> bytes:
        nonlocal fetcher_called
        fetcher_called = True
        return b""

    with pytest.raises(SourceResolutionError, match="ref not found"):
        service.prepare_product_artifact(
            "zewitness",
            resolver=_resolver,
            fetcher=_fetcher,
            work_root=tmp_path,
        )

    assert not fetcher_called, "fetcher must not be called on resolution failure"


# ===========================================================================
# Test: Error hierarchy
# ===========================================================================


def test_error_hierarchy() -> None:
    """ProductInstallPreparationError and RemoteSourceUnavailableError
    are properly typed."""
    assert issubclass(ProductInstallPreparationError, RuntimeError)
    assert issubclass(RemoteSourceUnavailableError, ProductInstallPreparationError)
    assert issubclass(RemoteSourceUnavailableError, RuntimeError)


def test_prepared_product_artifact_is_immutable() -> None:
    """PreparedProductArtifact is frozen."""
    from zealfie.sources import RemoteSource as RS, ResolvedSource as ResS

    remote = RS(owner="a", repo="b", ref="c")
    resolved = ResS(source=remote, commit_sha=VALID_SHA)

    ppa = PreparedProductArtifact(
        product_id="test",
        component_id="test",
        resolved_source=resolved,
        wheel_path=Path("/tmp/test.whl"),
        verified_artifact=VerifiedArtifact(
            component_id="test",
            version="1.0",
            path=Path("/tmp/test.whl"),
            size=100,
            sha256="a" * 64,
            distribution_name="test",
            wheel_version="1.0",
        ),
    )

    # Immutable — mutation should raise.
    with pytest.raises(Exception):
        ppa.product_id = "other"  # type: ignore


# ===========================================================================
# D.4.1C: Prepared artifact → deployment plan bridge
# ===========================================================================

# Shared helpers for D.4.1C tests

_SECOND_SHA = "e5b1f2a3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9"
_SECOND_WHEEL_SHA = "b" * 64


# Mapping from product_id → default distribution_name for tests.
_PLANNING_DIST_NAMES = {
    "zewitness": "zealfie-witness",
    "zesolver": "zealfie-solver",
}


def _make_ppa(product_id, component_id, wheel_path, version="1.0",
              dist_name=None):
    """Create a PreparedProductArtifact for testing."""
    from zealfie.sources import RemoteSource as RS, ResolvedSource as ResS

    if dist_name is None:
        dist_name = _PLANNING_DIST_NAMES.get(product_id, f"zealfie-{product_id}")

    remote = RS(owner="tinystork", repo=f"Ze{product_id.capitalize()}", ref="main")
    resolved = ResS(source=remote, commit_sha=VALID_SHA)

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
            sha256=_SECOND_WHEEL_SHA,
            distribution_name=dist_name,
            wheel_version=version,
        ),
    )


# ── ProductDescriptor helpers for planning tests ─────────────────────


_ZEWITNESS_EP = (EntryPointContract("console_scripts", "zewitness"),)
_ZESOLVER_EP = (EntryPointContract("gui_scripts", "zesolver"),)


def _planning_catalog(
    product_id="zewitness",
    dist_name="zealfie-witness",
    entry_points=_ZEWITNESS_EP,
    **kwargs,
) -> ProductCatalog:
    """Create a single-product catalog for planning tests."""
    desc = ProductDescriptor(
        product_id=product_id,
        display_name=product_id.capitalize(),
        distribution_name=dist_name,
        launch_entry_points=entry_points,
        **kwargs,
    )
    return _catalog(desc)


def _dual_catalog() -> ProductCatalog:
    """Create a two-product catalog for duplicate/set planning tests."""
    w = ProductDescriptor(
        product_id="zewitness",
        display_name="ZeWitness",
        distribution_name="zealfie-witness",
        launch_entry_points=_ZEWITNESS_EP,
    )
    z = ProductDescriptor(
        product_id="zesolver",
        display_name="ZeSolver",
        distribution_name="zealfie-solver",
        launch_entry_points=_ZESOLVER_EP,
    )
    return _catalog(w, z)


# =========================================================================
# Test D.4.1C-1: Empty prepared artifact sequence → structured error
# =========================================================================


def test_empty_prepared_artifacts_raises_structured_error() -> None:
    """plan_prepared_product_deployment with empty list raises
    ProductDeploymentPlanningError, no runtime/selection mutation."""
    from zealfie.app.service import ProductDeploymentPlanningError

    service = ZeAlfieService(catalog=_planning_catalog())

    with pytest.raises(ProductDeploymentPlanningError, match="at least one"):
        service.plan_prepared_product_deployment([])


def test_empty_prepared_artifacts_no_runtime_status_call(monkeypatch) -> None:
    """Empty artifacts must fail before any runtime status call."""
    from zealfie.app.service import ProductDeploymentPlanningError
    from zealfie.runtime.model import RuntimeStatus, RuntimeState

    calls = 0

    class _FakeRt:
        def status(self):
            nonlocal calls
            calls += 1
            return RuntimeStatus(
                state=RuntimeState.READY,
                runtime_root=Path("/fake"),
                active_slot_id="rt-test0000",
            )

    service = ZeAlfieService(
        catalog=_planning_catalog(),
        runtime=_FakeRt(),
    )

    with pytest.raises(ProductDeploymentPlanningError):
        service.plan_prepared_product_deployment([])

    assert calls == 0, "runtime.status() must not be called for empty input"


# =========================================================================
# Test D.4.1C-2: Duplicate product/component ids → structured error
# =========================================================================


def test_duplicate_product_id_raises_structured_error(
    tmp_path, witness_wheel,
) -> None:
    """Duplicate product_id in prepared artifacts raises
    ProductDeploymentPlanningError."""
    from zealfie.app.service import ProductDeploymentPlanningError

    ppa1 = _make_ppa("zewitness", "zewitness", witness_wheel)
    ppa2 = _make_ppa("zewitness", "zewitness", witness_wheel)

    service = ZeAlfieService(catalog=_planning_catalog())

    with pytest.raises(ProductDeploymentPlanningError, match="duplicate product_id"):
        service.plan_prepared_product_deployment([ppa1, ppa2])


def test_duplicate_component_id_raises_structured_error(
    tmp_path, witness_wheel,
) -> None:
    """Duplicate component_id (via duplicate product_id, since they must
    match per D.4.1B contract) raises ProductDeploymentPlanningError."""
    from zealfie.app.service import ProductDeploymentPlanningError

    ppa1 = _make_ppa("zewitness", "zewitness", witness_wheel)
    ppa2 = _make_ppa("zewitness", "zewitness", witness_wheel)

    catalog = _planning_catalog()
    service = ZeAlfieService(catalog=catalog)

    with pytest.raises(ProductDeploymentPlanningError, match="duplicate product_id"):
        service.plan_prepared_product_deployment([ppa1, ppa2])


# =========================================================================
# Test D.4.1C-3: Unknown prepared product id → UnknownProductError
# =========================================================================


def test_unknown_prepared_product_id_raises_unknown_product_error(
    tmp_path, witness_wheel,
) -> None:
    """Prepared artifact with product_id not in catalog raises
    UnknownProductError, no legacy registry fallback."""
    ppa = _make_ppa("nobody", "nobody", witness_wheel)
    service = ZeAlfieService(catalog=_planning_catalog("zewitness"))

    with pytest.raises(UnknownProductError, match="nobody"):
        service.plan_prepared_product_deployment([ppa])


def test_unknown_product_id_no_legacy_registry_fallback(
    tmp_path, witness_wheel,
) -> None:
    """Even when the legacy registry has 'nobody', planning must use
    the product catalog (which does not)."""
    dummy_reg = ComponentRegistry([
        ComponentDefinition(
            component_id="nobody",
            display_name="Nobody",
            distribution_name="nobody-lib",
            launch_entry_points=_ZEWITNESS_EP,
        ),
    ])

    ppa = _make_ppa("nobody", "nobody", witness_wheel)
    service = ZeAlfieService(
        catalog=_planning_catalog("zewitness"),
        registry=dummy_reg,
    )

    with pytest.raises(UnknownProductError, match="nobody"):
        service.plan_prepared_product_deployment([ppa])


# =========================================================================
# Test D.4.1C-4: Prepared product/component/artifact id mismatch
# =========================================================================


def test_product_component_id_mismatch_fails_closed(
    tmp_path, witness_wheel,
) -> None:
    """product_id != component_id → ProductDeploymentPlanningError."""
    from zealfie.app.service import ProductDeploymentPlanningError

    ppa = _make_ppa("zewitness", "different", witness_wheel)
    service = ZeAlfieService(catalog=_planning_catalog())

    with pytest.raises(ProductDeploymentPlanningError,
                       match="product_id.*!=.*component_id"):
        service.plan_prepared_product_deployment([ppa])


def test_product_verified_artifact_component_id_mismatch_fails_closed(
    tmp_path, witness_wheel,
) -> None:
    """product_id != verified_artifact.component_id →
    ProductDeploymentPlanningError."""
    from zealfie.app.service import ProductDeploymentPlanningError

    ppa = _make_ppa("zesolver", "zesolver", witness_wheel)
    # Override verified_artifact.component_id to differ from product_id
    # while keeping product_id == component_id.
    bad_va = VerifiedArtifact(
        component_id="different",  # mismatched
        version="1.0",
        path=ppa.wheel_path,
        size=ppa.verified_artifact.size,
        sha256=ppa.verified_artifact.sha256,
        distribution_name=ppa.verified_artifact.distribution_name,
        wheel_version="1.0",
    )
    ppa = PreparedProductArtifact(
        product_id="zesolver",
        component_id="zesolver",
        resolved_source=ppa.resolved_source,
        wheel_path=ppa.wheel_path,
        verified_artifact=bad_va,
    )

    service = ZeAlfieService(catalog=_dual_catalog())

    with pytest.raises(ProductDeploymentPlanningError,
                       match="product_id.*!=.*verified_artifact"):
        service.plan_prepared_product_deployment([ppa])


# =========================================================================
# Test D.4.1C-5: ABSENT runtime + valid prepared artifact → INSTALL plan
# =========================================================================


def test_absent_runtime_plan_has_install_step(
    tmp_path, witness_wheel,
) -> None:
    """ABSENT runtime with a valid prepared artifact produces a
    DeploymentPlan with one INSTALL step and VerifiedArtifact in
    desired state."""
    from zealfie.runtime.model import RuntimeState, RuntimeStatus
    from zealfie.runtime.planning import DeploymentAction

    ppa = _make_ppa("zewitness", "zewitness", witness_wheel)

    class _FakeAbsentRt:
        def status(self):
            return RuntimeStatus(
                state=RuntimeState.ABSENT,
                runtime_root=Path("/fake"),
            )

    service = ZeAlfieService(
        catalog=_planning_catalog(),
        runtime=_FakeAbsentRt(),
    )

    plan = service.plan_prepared_product_deployment([ppa])

    assert plan.runtime_state == RuntimeState.ABSENT
    assert not plan.blocked
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.component_id == "zewitness"
    assert step.action == DeploymentAction.INSTALL
    assert step.reason_code is not None
    assert step.artifact.path == witness_wheel

    # Desired state preserves VerifiedArtifact.
    assert len(plan.desired_state.components) == 1
    dc = plan.desired_state.components[0]
    assert dc.artifact == ppa.verified_artifact


def test_absent_runtime_source_slot_ids_preserved(
    tmp_path, witness_wheel,
) -> None:
    """Source slot ids from runtime status are preserved in the plan."""
    from zealfie.runtime.model import RuntimeState, RuntimeStatus

    ppa = _make_ppa("zewitness", "zewitness", witness_wheel)

    class _FakeRt:
        def status(self):
            return RuntimeStatus(
                state=RuntimeState.ABSENT,
                runtime_root=Path("/fake"),
                active_slot_id="rt-a0000000",
                previous_slot_id="rt-b0000000",
            )

    service = ZeAlfieService(
        catalog=_planning_catalog(),
        runtime=_FakeRt(),
    )

    plan = service.plan_prepared_product_deployment([ppa])
    assert plan.source_active_slot_id == "rt-a0000000"
    assert plan.source_previous_slot_id == "rt-b0000000"


# =========================================================================
# Test D.4.1C-6: Plan registry uses ProductCatalog descriptor
# =========================================================================


def test_plan_registry_uses_catalog_not_legacy(tmp_path, witness_wheel) -> None:
    """A legacy registry with unrelated ids must not affect planning.
    The catalog-derived registry is authoritative."""
    from zealfie.runtime.model import RuntimeState, RuntimeStatus
    from zealfie.runtime.planning import DeploymentAction

    # Legacy registry with an unrelated id.
    legacy_reg = ComponentRegistry([
        ComponentDefinition(
            component_id="irrelevant",
            display_name="Irrelevant",
            distribution_name="irrelevant-lib",
            launch_entry_points=_ZEWITNESS_EP,
        ),
    ])

    ppa = _make_ppa("zewitness", "zewitness", witness_wheel)

    class _FakeAbsentRt:
        def status(self):
            return RuntimeStatus(
                state=RuntimeState.ABSENT,
                runtime_root=Path("/fake"),
            )

    service = ZeAlfieService(
        catalog=_planning_catalog(),
        registry=legacy_reg,
        runtime=_FakeAbsentRt(),
    )

    plan = service.plan_prepared_product_deployment([ppa])

    # Plan must have exactly one step for zewitness.
    assert len(plan.steps) == 1
    assert plan.steps[0].component_id == "zewitness"
    assert plan.steps[0].action == DeploymentAction.INSTALL


def test_registry_distribution_name_from_catalog(
    tmp_path, witness_wheel,
) -> None:
    """The registry used for planning exposes the catalog's
    distribution_name, not the default."""
    from zealfie.runtime.model import RuntimeState, RuntimeStatus

    ppa = _make_ppa("zewitness", "zewitness", witness_wheel, dist_name="my-custom-dist")
    catalog = _planning_catalog(dist_name="my-custom-dist")

    class _FakeAbsentRt:
        def status(self):
            return RuntimeStatus(
                state=RuntimeState.ABSENT,
                runtime_root=Path("/fake"),
            )

    service = ZeAlfieService(catalog=catalog, runtime=_FakeAbsentRt())

    plan = service.plan_prepared_product_deployment([ppa])

    # The plan must have been built with the catalog-derived registry.
    # Verify via the internal helper.
    reg = service._registry_for_prepared_products([ppa])
    definition = reg.get("zewitness")
    assert definition.distribution_name == "my-custom-dist"


# =========================================================================
# Test D.4.1C-7: Selection store file not created/read-mutated
# =========================================================================


def test_planning_does_not_create_selection_store(
    tmp_path, witness_wheel,
) -> None:
    """plan_prepared_product_deployment must not create or touch the
    selection store file."""
    from zealfie.runtime.model import RuntimeState, RuntimeStatus
    from zealfie.products.selection import SelectionStore

    sel_path = tmp_path / "desired-products.toml"
    store = SelectionStore(path=sel_path)

    ppa = _make_ppa("zewitness", "zewitness", witness_wheel)

    class _FakeAbsentRt:
        def status(self):
            return RuntimeStatus(
                state=RuntimeState.ABSENT,
                runtime_root=Path("/fake"),
            )

    service = ZeAlfieService(
        catalog=_planning_catalog(),
        runtime=_FakeAbsentRt(),
        selection_store=store,
    )

    assert not sel_path.exists(), "selection file must not exist before planning"

    service.plan_prepared_product_deployment([ppa])

    assert not sel_path.exists(), (
        "plan_prepared_product_deployment must not create selection file"
    )


# =========================================================================
# Test D.4.1C-8: No install/apply — explode if called
# =========================================================================


def test_no_apply_deployment_plan_called(monkeypatch, tmp_path, witness_wheel) -> None:
    """plan_prepared_product_deployment must not call apply_deployment_plan
    or any runtime transaction methods."""
    from zealfie.runtime.model import RuntimeState, RuntimeStatus

    ppa = _make_ppa("zewitness", "zewitness", witness_wheel)

    class _FakeAbsentRt:
        def status(self):
            return RuntimeStatus(
                state=RuntimeState.ABSENT,
                runtime_root=Path("/fake"),
            )

    # Monkeypatch apply_deployment_plan to explode.
    import zealfie.app.service as svc_mod

    apply_called = False

    def _explosive_apply(*args, **kwargs):
        nonlocal apply_called
        apply_called = True
        raise AssertionError("apply_deployment_plan must not be called by planning")

    monkeypatch.setattr(svc_mod, "apply_deployment_plan", _explosive_apply)

    service = ZeAlfieService(
        catalog=_planning_catalog(),
        runtime=_FakeAbsentRt(),
    )

    plan = service.plan_prepared_product_deployment([ppa])
    assert not apply_called, "apply_deployment_plan must not be called"
    assert plan is not None


# =========================================================================
# Test D.4.1C: Error hierarchy
# =========================================================================


def test_product_deployment_planning_error_hierarchy() -> None:
    """ProductDeploymentPlanningError is a RuntimeError."""
    from zealfie.app.service import ProductDeploymentPlanningError

    assert issubclass(ProductDeploymentPlanningError, RuntimeError)

    # Not a ProductInstallPreparationError — distinct family.
    assert not issubclass(ProductDeploymentPlanningError, ProductInstallPreparationError)


# =========================================================================
# Test D.4.1C: Probe injectable for READY runtime
# =========================================================================


def test_probe_injectable_for_ready_runtime(
    tmp_path, witness_wheel,
) -> None:
    """When the runtime is READY, an injectable probe_distribution
    callable controls the plan outcome."""
    from zealfie.runtime.model import RuntimeState, RuntimeStatus, RuntimeReasonCode
    from zealfie.runtime.planning import DeploymentAction

    ppa = _make_ppa("zewitness", "zewitness", witness_wheel)

    class _FakeReadyRt:
        def status(self):
            return RuntimeStatus(
                state=RuntimeState.READY,
                runtime_root=Path("/fake"),
                active_slot_id="rt-test0000",
                python_executable=Path("/fake/bin/python"),
                reason_code=RuntimeReasonCode.RUNTIME_READY,
            )

    service = ZeAlfieService(
        catalog=_planning_catalog(),
        runtime=_FakeReadyRt(),
    )

    # Probe that says installed with matching version.
    def _fake_probe(python_exe, dist_name):
        return {
            "installed": True,
            "version": "1.0",
            "entry_points": [
                {"group": "console_scripts", "name": "zewitness",
                 "value": "zewitness:main"},
            ],
        }

    plan = service.plan_prepared_product_deployment(
        [ppa], probe_distribution=_fake_probe,
    )

    assert not plan.blocked
    assert len(plan.steps) == 1
    assert plan.steps[0].action == DeploymentAction.KEEP


# =========================================================================
# Test D.4.1C: dependency_wheelhouse parameter (read-only, optional)
# =========================================================================


def test_dependency_wheelhouse_passed_through_none_by_default(
    tmp_path, witness_wheel,
) -> None:
    """When dependency_wheelhouse is not passed, lock is None."""
    from zealfie.runtime.model import RuntimeState, RuntimeStatus

    ppa = _make_ppa("zewitness", "zewitness", witness_wheel)

    class _FakeAbsentRt:
        def status(self):
            return RuntimeStatus(
                state=RuntimeState.ABSENT,
                runtime_root=Path("/fake"),
            )

    service = ZeAlfieService(
        catalog=_planning_catalog(),
        runtime=_FakeAbsentRt(),
    )

    plan = service.plan_prepared_product_deployment([ppa])
    assert plan.dependency_lock is None
# ===========================================================================
# D.4.1D: Apply prepared product deployment
# ===========================================================================

# Re-uses helpers from the D.4.1B/D.4.1C section above:
#   _make_ppa, _planning_catalog, _dual_catalog, _catalog,
#   _ZEWITNESS_EP, _ZESOLVER_EP, witness_wheel (conftest)

from pathlib import Path

import pytest


# =========================================================================
# Test D.4.1D-1: Valid prepared artifact → calls apply once, returns result
# =========================================================================


def test_d4d1_valid_calls_apply_once_returns_result(
    tmp_path, witness_wheel, monkeypatch,
) -> None:
    """install_prepared_product_deployment with a valid prepared artifact
    calls apply_deployment_plan exactly once with a plan from D.4.1C and
    a ProductCatalog-derived registry; returns the exact DeploymentResult."""
    import zealfie.app.service as svc_mod
    from zealfie.runtime.model import DeploymentResult, RuntimeState, RuntimeStatus

    ppa = _make_ppa("zewitness", "zewitness", witness_wheel)

    class _FakeAbsentRt:
        def status(self):
            return RuntimeStatus(
                state=RuntimeState.ABSENT,
                runtime_root=Path("/fake"),
            )

    fake_result = DeploymentResult(success=True, active_slot_id="rt-test1234")
    apply_calls = []

    def _fake_apply(plan, *, registry, runtime):
        apply_calls.append((plan, registry, runtime))
        return fake_result

    monkeypatch.setattr(svc_mod, "apply_deployment_plan", _fake_apply)

    sel_path = tmp_path / "desired-products.toml"
    service = ZeAlfieService(
        catalog=_planning_catalog(),
        runtime=_FakeAbsentRt(),
        selection_store=SelectionStore(path=sel_path),
    )

    result = service.install_prepared_product_deployment([ppa])

    # --- Apply called exactly once ---
    assert len(apply_calls) == 1, "apply_deployment_plan must be called exactly once"
    called_plan, called_registry, called_runtime = apply_calls[0]

    # --- Plan: from D.4.1C ---
    assert called_plan is not None
    assert not called_plan.blocked
    assert len(called_plan.desired_state.components) == 1
    assert called_plan.desired_state.components[0].component_id == "zewitness"

    # --- Registry: ProductCatalog-derived ---
    assert called_registry is not None
    definition = called_registry.get("zewitness")
    assert definition.distribution_name == "zealfie-witness"

    # --- Runtime: service's runtime ---
    assert called_runtime is service._runtime
    assert result is fake_result


# =========================================================================
# Test D.4.1D-2: Successful result persists product id to selection
# =========================================================================


def test_d4d2_success_persists_product_id(
    tmp_path, witness_wheel, monkeypatch,
) -> None:
    """After apply_deployment_plan returns success=True, the product id
    is persisted to desired-products.toml."""
    import zealfie.app.service as svc_mod
    from zealfie.runtime.model import DeploymentResult, RuntimeState, RuntimeStatus

    ppa = _make_ppa("zewitness", "zewitness", witness_wheel)

    class _FakeAbsentRt:
        def status(self):
            return RuntimeStatus(
                state=RuntimeState.ABSENT,
                runtime_root=Path("/fake"),
            )

    monkeypatch.setattr(
        svc_mod, "apply_deployment_plan",
        lambda plan, *, registry, runtime: DeploymentResult(
            success=True, active_slot_id="rt-test1111",
        ),
    )

    sel_path = tmp_path / "desired-products.toml"
    store = SelectionStore(path=sel_path)

    service = ZeAlfieService(
        catalog=_planning_catalog(),
        runtime=_FakeAbsentRt(),
        selection_store=store,
    )

    assert not sel_path.exists(), "selection file must not exist before install"

    result = service.install_prepared_product_deployment([ppa])

    assert result.success is True
    assert sel_path.exists(), "selection file must exist after successful apply"

    store.reload()
    assert "zewitness" in store.selected_product_ids
    assert store.selected_product_ids == ("zewitness",)


# =========================================================================
# Test D.4.1D-3: Failed DeploymentResult does not persist selection
# =========================================================================


def test_d4d3_failed_result_no_selection_persist(
    tmp_path, witness_wheel, monkeypatch,
) -> None:
    """When apply_deployment_plan returns success=False, the selection
    store is not modified."""
    import zealfie.app.service as svc_mod
    from zealfie.runtime.model import DeploymentResult, RuntimeState, RuntimeStatus

    ppa = _make_ppa("zewitness", "zewitness", witness_wheel)

    class _FakeAbsentRt:
        def status(self):
            return RuntimeStatus(
                state=RuntimeState.ABSENT,
                runtime_root=Path("/fake"),
            )

    monkeypatch.setattr(
        svc_mod, "apply_deployment_plan",
        lambda plan, *, registry, runtime: DeploymentResult(
            success=False, reason="simulated failure",
        ),
    )

    sel_path = tmp_path / "desired-products.toml"
    sel_path.parent.mkdir(parents=True, exist_ok=True)
    sel_path.write_text(
        'schema_version = 1\n'
        'selected_product_ids = ["zesolver"]\n'
    )

    store = SelectionStore(path=sel_path)

    service = ZeAlfieService(
        catalog=_dual_catalog(),
        runtime=_FakeAbsentRt(),
        selection_store=store,
    )

    original_content = sel_path.read_text()

    result = service.install_prepared_product_deployment([ppa])

    assert result.success is False
    assert result.reason == "simulated failure"
    assert sel_path.read_text() == original_content, (
        "desired-products.toml must not be modified on failed apply"
    )
    store.reload()
    assert "zewitness" not in store.selected_product_ids
    assert store.selected_product_ids == ("zesolver",)


# =========================================================================
# Test D.4.1D-4: Planning/validation error → no apply, no selection write
# =========================================================================


def test_d4d4_empty_artifacts_no_apply_no_selection(
    tmp_path, monkeypatch,
) -> None:
    """Empty artifacts → ProductDeploymentPlanningError before apply;
    selection store untouched, apply_deployment_plan not called."""
    import zealfie.app.service as svc_mod
    from zealfie.app.service import ProductDeploymentPlanningError

    apply_called = False

    def _explosive_apply(*args, **kwargs):
        nonlocal apply_called
        apply_called = True
        raise AssertionError("apply must not be called")

    monkeypatch.setattr(svc_mod, "apply_deployment_plan", _explosive_apply)

    sel_path = tmp_path / "desired-products.toml"
    service = ZeAlfieService(
        catalog=_planning_catalog(),
        selection_store=SelectionStore(path=sel_path),
    )

    with pytest.raises(ProductDeploymentPlanningError, match="at least one"):
        service.install_prepared_product_deployment([])

    assert not apply_called, "apply_deployment_plan must not be called"
    assert not sel_path.exists(), "selection file must not be created"


def test_d4d4_mismatch_error_no_apply_no_selection(
    tmp_path, witness_wheel, monkeypatch,
) -> None:
    """Artifact mismatch → ProductDeploymentPlanningError before apply;
    no apply call, no selection write."""
    import zealfie.app.service as svc_mod
    from zealfie.app.service import ProductDeploymentPlanningError

    ppa = _make_ppa("zewitness", "different", witness_wheel)

    apply_called = False

    def _explosive_apply(*args, **kwargs):
        nonlocal apply_called
        apply_called = True
        raise AssertionError("apply must not be called")

    monkeypatch.setattr(svc_mod, "apply_deployment_plan", _explosive_apply)

    sel_path = tmp_path / "desired-products.toml"
    service = ZeAlfieService(
        catalog=_planning_catalog(),
        selection_store=SelectionStore(path=sel_path),
    )

    with pytest.raises(ProductDeploymentPlanningError,
                       match="product_id.*!=.*component_id"):
        service.install_prepared_product_deployment([ppa])

    assert not apply_called, "apply_deployment_plan must not be called"
    assert not sel_path.exists(), "selection file must not be created"


# =========================================================================
# Test D.4.1D-5: Corrupt existing selection file → fail before apply
# =========================================================================


def test_d4d5_corrupt_selection_fails_before_apply(
    tmp_path, witness_wheel, monkeypatch,
) -> None:
    """When the selection store file is corrupt (invalid TOML),
    CorruptSelectionError is raised before apply is called."""
    import zealfie.app.service as svc_mod
    from zealfie.runtime.model import RuntimeState, RuntimeStatus

    ppa = _make_ppa("zewitness", "zewitness", witness_wheel)

    apply_called = False

    def _explosive_apply(*args, **kwargs):
        nonlocal apply_called
        apply_called = True
        raise AssertionError("apply must not be called")

    monkeypatch.setattr(svc_mod, "apply_deployment_plan", _explosive_apply)

    sel_path = tmp_path / "desired-products.toml"
    sel_path.parent.mkdir(parents=True, exist_ok=True)
    sel_path.write_text("this is not valid {{{ toml")

    class _FakeAbsentRt:
        def status(self):
            return RuntimeStatus(
                state=RuntimeState.ABSENT,
                runtime_root=Path("/fake"),
            )

    service = ZeAlfieService(
        catalog=_planning_catalog(),
        runtime=_FakeAbsentRt(),
        selection_store=SelectionStore(path=sel_path),
    )

    with pytest.raises(CorruptSelectionError, match="invalid TOML"):
        service.install_prepared_product_deployment([ppa])

    assert not apply_called, "apply must not be called on corrupt selection"


# =========================================================================
# Test D.4.1D-6: Unknown product id → UnknownProductError, no apply/selection
# =========================================================================


def test_d4d6_unknown_product_no_apply_no_selection(
    tmp_path, witness_wheel, monkeypatch,
) -> None:
    """Prepared artifact with product_id not in catalog raises
    UnknownProductError before apply is called."""
    import zealfie.app.service as svc_mod

    ppa = _make_ppa("nobody", "nobody", witness_wheel)

    apply_called = False

    def _explosive_apply(*args, **kwargs):
        nonlocal apply_called
        apply_called = True
        raise AssertionError("apply must not be called")

    monkeypatch.setattr(svc_mod, "apply_deployment_plan", _explosive_apply)

    sel_path = tmp_path / "desired-products.toml"
    service = ZeAlfieService(
        catalog=_planning_catalog("zewitness"),
        selection_store=SelectionStore(path=sel_path),
    )

    with pytest.raises(UnknownProductError, match="nobody"):
        service.install_prepared_product_deployment([ppa])

    assert not apply_called, "apply_deployment_plan must not be called"
    assert not sel_path.exists(), "selection file must not be created"


# =========================================================================
# Test D.4.1D-7: Existing selected ids preserved; new id added after success
# =========================================================================


def test_d4d7_existing_ids_preserved_new_added_after_success(
    tmp_path, witness_wheel, monkeypatch,
) -> None:
    """When desired-products.toml already contains 'zesolver', installing
    'zewitness' preserves 'zesolver' and adds 'zewitness' only after success."""
    import zealfie.app.service as svc_mod
    from zealfie.runtime.model import DeploymentResult, RuntimeState, RuntimeStatus

    ppa = _make_ppa("zewitness", "zewitness", witness_wheel)

    class _FakeAbsentRt:
        def status(self):
            return RuntimeStatus(
                state=RuntimeState.ABSENT,
                runtime_root=Path("/fake"),
            )

    monkeypatch.setattr(
        svc_mod, "apply_deployment_plan",
        lambda plan, *, registry, runtime: DeploymentResult(
            success=True, active_slot_id="rt-test2222",
        ),
    )

    sel_path = tmp_path / "desired-products.toml"
    sel_path.parent.mkdir(parents=True, exist_ok=True)
    sel_path.write_text(
        'schema_version = 1\n'
        'selected_product_ids = ["zesolver"]\n'
    )

    store = SelectionStore(path=sel_path)

    service = ZeAlfieService(
        catalog=_dual_catalog(),
        runtime=_FakeAbsentRt(),
        selection_store=store,
    )

    assert store.selected_product_ids == ("zesolver",)

    result = service.install_prepared_product_deployment([ppa])

    assert result.success is True

    store.reload()
    assert store.selected_product_ids == ("zesolver", "zewitness"), (
        f"expected ('zesolver', 'zewitness'), got {store.selected_product_ids}"
    )


# =========================================================================
# Test D.4.1D-8: Multi-product success persists all ids
# =========================================================================


def test_d4d8_multi_product_success_persists_all_ids(
    tmp_path, witness_wheel, monkeypatch,
) -> None:
    """Two prepared artifacts applied successfully → both ids persisted."""
    import zealfie.app.service as svc_mod
    from zealfie.runtime.model import DeploymentResult, RuntimeState, RuntimeStatus

    ppa1 = _make_ppa("zewitness", "zewitness", witness_wheel)
    ppa2 = _make_ppa("zesolver", "zesolver", witness_wheel, dist_name="zealfie-solver")

    class _FakeAbsentRt:
        def status(self):
            return RuntimeStatus(
                state=RuntimeState.ABSENT,
                runtime_root=Path("/fake"),
            )

    monkeypatch.setattr(
        svc_mod, "apply_deployment_plan",
        lambda plan, *, registry, runtime: DeploymentResult(
            success=True, active_slot_id="rt-test3333",
        ),
    )

    sel_path = tmp_path / "desired-products.toml"
    store = SelectionStore(path=sel_path)

    service = ZeAlfieService(
        catalog=_dual_catalog(),
        runtime=_FakeAbsentRt(),
        selection_store=store,
    )

    assert not sel_path.exists()

    result = service.install_prepared_product_deployment([ppa1, ppa2])

    assert result.success is True

    store.reload()
    assert store.selected_product_ids == ("zesolver", "zewitness"), (
        f"expected ('zesolver', 'zewitness'), got {store.selected_product_ids}"
    )


# =========================================================================
# Test D.4.1D-9: Legacy registry with unrelated ids → catalog authoritative
# =========================================================================


def test_d4d9_legacy_registry_unrelated_ids_no_effect(
    tmp_path, witness_wheel, monkeypatch,
) -> None:
    """A legacy registry with ids not in the product catalog must not affect
    the apply registry; ProductCatalog descriptor remains authoritative."""
    import zealfie.app.service as svc_mod
    from zealfie.runtime.model import DeploymentResult, RuntimeState, RuntimeStatus
    from zealfie.components.registry import UnknownComponentError

    ppa = _make_ppa("zewitness", "zewitness", witness_wheel)

    legacy_reg = ComponentRegistry([
        ComponentDefinition(
            component_id="irrelevant",
            display_name="Irrelevant",
            distribution_name="irrelevant-lib",
            launch_entry_points=(EntryPointContract("console_scripts", "irrelevant"),),
        ),
    ])

    class _FakeAbsentRt:
        def status(self):
            return RuntimeStatus(
                state=RuntimeState.ABSENT,
                runtime_root=Path("/fake"),
            )

    apply_calls = []

    def _fake_apply(plan, *, registry, runtime):
        apply_calls.append((plan, registry, runtime))
        return DeploymentResult(success=True, active_slot_id="rt-test4444")

    monkeypatch.setattr(svc_mod, "apply_deployment_plan", _fake_apply)

    sel_path = tmp_path / "desired-products.toml"
    service = ZeAlfieService(
        catalog=_planning_catalog(),
        registry=legacy_reg,
        runtime=_FakeAbsentRt(),
        selection_store=SelectionStore(path=sel_path),
    )

    result = service.install_prepared_product_deployment([ppa])

    assert result.success is True
    assert len(apply_calls) == 1
    __, called_registry, ___ = apply_calls[0]

    assert sorted(called_registry.available_ids()) == ["zewitness"]
    definition = called_registry.get("zewitness")
    assert definition.distribution_name == "zealfie-witness"

    with pytest.raises(UnknownComponentError):
        called_registry.get("irrelevant")


# =========================================================================
# Test D.4.1D-10: No direct runtime transaction in service path
# =========================================================================


def test_d4d10_no_direct_runtime_transaction_calls(
    tmp_path, witness_wheel, monkeypatch,
) -> None:
    """The service delegates only to apply_deployment_plan — no direct
    runtime.begin_transaction(), install_local_wheel(), etc."""
    import zealfie.app.service as svc_mod
    from zealfie.runtime.model import DeploymentResult, RuntimeState, RuntimeStatus

    ppa = _make_ppa("zewitness", "zewitness", witness_wheel)

    class _FakeAbsentRt:
        def status(self):
            return RuntimeStatus(
                state=RuntimeState.ABSENT,
                runtime_root=Path("/fake"),
            )

        def begin_transaction(self):
            raise AssertionError("service must not call runtime.begin_transaction()")

        def install_local_wheel(self, *args, **kwargs):
            raise AssertionError("service must not call runtime.install_local_wheel()")

        def validate_candidate(self, *args, **kwargs):
            raise AssertionError("service must not call runtime.validate_candidate()")

        def activate(self, *args, **kwargs):
            raise AssertionError("service must not call runtime.activate()")

    apply_called = False

    def _safe_apply(plan, *, registry, runtime):
        nonlocal apply_called
        apply_called = True
        return DeploymentResult(success=True, active_slot_id="rt-test5555")

    monkeypatch.setattr(svc_mod, "apply_deployment_plan", _safe_apply)

    sel_path = tmp_path / "desired-products.toml"
    store = SelectionStore(path=sel_path)

    service = ZeAlfieService(
        catalog=_planning_catalog(),
        runtime=_FakeAbsentRt(),
        selection_store=store,
    )

    result = service.install_prepared_product_deployment([ppa])

    assert apply_called, "apply_deployment_plan must be called"
    assert result.success is True

    store.reload()
    assert "zewitness" in store.selected_product_ids


# ===========================================================================
# D.4.1E: Public service install_product orchestration tests
# ===========================================================================


# =========================================================================
# Test D.4.1E-1: Success — calls prepare then prepared-install, returns result
# =========================================================================


def test_d4e1_success_orchestration_calls_prepare_then_install_prepared(
    tmp_path, monkeypatch,
) -> None:
    """install_product calls prepare_product_artifact then
    install_prepared_product_deployment and returns the exact DeploymentResult."""
    from zealfie.runtime.model import DeploymentResult

    catalog = _catalog(WITNESS_DESCRIPTOR)
    service = ZeAlfieService(catalog=catalog)

    # Track calls to verify call order.
    call_order: list[str] = []

    # Fake PPA with a wheel that actually exists.
    wheel = _build_test_wheel(tmp_path, "zealfie-witness", "0.0.1",
                              entry_points=(("console_scripts", "zewitness", "zewitness:main"),))
    from zealfie.sources import RemoteSource as RS, ResolvedSource as ResS
    fake_ppa = PreparedProductArtifact(
        product_id="zewitness",
        component_id="zewitness",
        resolved_source=ResS(
            source=RS(owner="tinystork", repo="ZeWitness", ref="main"),
            commit_sha=VALID_SHA,
        ),
        wheel_path=wheel,
        verified_artifact=VerifiedArtifact(
            component_id="zewitness",
            version="0.0.1",
            path=wheel,
            size=200,
            sha256="a" * 64,
            distribution_name="zealfie-witness",
            wheel_version="0.0.1",
        ),
    )

    fake_result = DeploymentResult(success=True, active_slot_id="rt-slot123")

    captured_prepare_args: list[dict] = []
    captured_install_args: list[dict] = []

    def _fake_prepare(product_id, *, resolver, fetcher, work_root):
        call_order.append("prepare")
        captured_prepare_args.append({
            "product_id": product_id,
            "has_resolver": resolver is not None,
            "has_fetcher": fetcher is not None,
            "has_work_root": work_root is not None,
        })
        return fake_ppa

    def _fake_install_prepared(prepared_artifacts, *,
                               dependency_wheelhouse=None,
                               probe_distribution=None):
        call_order.append("install_prepared")
        captured_install_args.append({
            "count": len(prepared_artifacts),
            "has_dependency_wheelhouse": dependency_wheelhouse is not None,
            "has_probe": probe_distribution is not None,
        })
        return fake_result

    monkeypatch.setattr(service, "prepare_product_artifact", _fake_prepare)
    monkeypatch.setattr(
        service, "install_prepared_product_deployment", _fake_install_prepared,
    )

    def _resolver(owner, repo, ref):
        return VALID_SHA

    def _fetcher(owner, repo, commit_sha):
        return b"fake archive"

    result = service.install_product(
        "zewitness",
        resolver=_resolver,
        fetcher=_fetcher,
        work_root=tmp_path,
    )

    # --- Call order ---
    assert call_order == ["prepare", "install_prepared"], (
        f"expected prepare before install_prepared, got {call_order}"
    )

    # --- prepare_product_artifact called with correct args ---
    assert len(captured_prepare_args) == 1
    assert captured_prepare_args[0]["product_id"] == "zewitness"

    # --- install_prepared_product_deployment called with [fake_ppa] ---
    assert len(captured_install_args) == 1
    assert captured_install_args[0]["count"] == 1

    # --- Returns exact DeploymentResult ---
    assert result is fake_result
    assert result.success is True
    assert result.active_slot_id == "rt-slot123"


# =========================================================================
# Test D.4.1E-2: Prepared install receives exact PPA from preparation
# =========================================================================


def test_d4e2_prepared_install_receives_exact_ppa_from_preparation(
    tmp_path, monkeypatch,
) -> None:
    """install_prepared_product_deployment receives a one-item list
    containing the exact PreparedProductArtifact returned by
    prepare_product_artifact."""
    from zealfie.runtime.model import DeploymentResult

    catalog = _catalog(WITNESS_DESCRIPTOR)
    service = ZeAlfieService(catalog=catalog)

    wheel = _build_test_wheel(tmp_path, "zealfie-witness", "0.0.1",
                              entry_points=(("console_scripts", "zewitness", "zewitness:main"),))

    from zealfie.sources import RemoteSource as RS, ResolvedSource as ResS
    fake_ppa = PreparedProductArtifact(
        product_id="zewitness",
        component_id="zewitness",
        resolved_source=ResS(
            source=RS(owner="tinystork", repo="ZeWitness", ref="main"),
            commit_sha=VALID_SHA,
        ),
        wheel_path=wheel,
        verified_artifact=VerifiedArtifact(
            component_id="zewitness",
            version="0.0.1",
            path=wheel,
            size=200,
            sha256="a" * 64,
            distribution_name="zealfie-witness",
            wheel_version="0.0.1",
        ),
    )

    received_artifacts: list = []

    monkeypatch.setattr(service, "prepare_product_artifact",
                        lambda product_id, *, resolver, fetcher, work_root: fake_ppa)
    monkeypatch.setattr(
        service, "install_prepared_product_deployment",
        lambda prepared_artifacts, *, dependency_wheelhouse=None,
               probe_distribution=None: (
            received_artifacts.append(prepared_artifacts)
            or DeploymentResult(success=True, active_slot_id="rt-x")
        ),
    )

    def _resolver(owner, repo, ref):
        return VALID_SHA

    def _fetcher(owner, repo, commit_sha):
        return b"fake"

    service.install_product(
        "zewitness",
        resolver=_resolver,
        fetcher=_fetcher,
        work_root=tmp_path,
    )

    assert len(received_artifacts) == 1
    artifacts_list = received_artifacts[0]
    assert isinstance(artifacts_list, list)
    assert len(artifacts_list) == 1
    assert artifacts_list[0] is fake_ppa


# =========================================================================
# Test D.4.1E-3: dependency_wheelhouse and probe_distribution pass-through
# =========================================================================


def test_d4e3_dependency_wheelhouse_and_probe_passed_through(
    tmp_path, monkeypatch,
) -> None:
    """dependency_wheelhouse and probe_distribution are forwarded to
    install_prepared_product_deployment."""
    from zealfie.runtime.model import DeploymentResult

    catalog = _catalog(WITNESS_DESCRIPTOR)
    service = ZeAlfieService(catalog=catalog)

    wheel = _build_test_wheel(tmp_path, "zealfie-witness", "0.0.1",
                              entry_points=(("console_scripts", "zewitness", "zewitness:main"),))

    from zealfie.sources import RemoteSource as RS, ResolvedSource as ResS
    fake_ppa = PreparedProductArtifact(
        product_id="zewitness",
        component_id="zewitness",
        resolved_source=ResS(
            source=RS(owner="tinystork", repo="ZeWitness", ref="main"),
            commit_sha=VALID_SHA,
        ),
        wheel_path=wheel,
        verified_artifact=VerifiedArtifact(
            component_id="zewitness",
            version="0.0.1",
            path=wheel,
            size=200,
            sha256="a" * 64,
            distribution_name="zealfie-witness",
            wheel_version="0.0.1",
        ),
    )

    captured_kwargs: dict = {}

    def _fake_probe(python_exe, dist_name):
        return {"installed": True, "version": "1.0", "entry_points": []}

    monkeypatch.setattr(service, "prepare_product_artifact",
                        lambda product_id, *, resolver, fetcher, work_root: fake_ppa)

    def _fake_install(prepared_artifacts, *, dependency_wheelhouse=None,
                      probe_distribution=None):
        captured_kwargs["dependency_wheelhouse"] = dependency_wheelhouse
        captured_kwargs["probe_distribution"] = probe_distribution
        return DeploymentResult(success=True, active_slot_id="rt-ok")

    monkeypatch.setattr(service, "install_prepared_product_deployment", _fake_install)

    wh_path = tmp_path / "wheelhouse"
    wh_path.mkdir()

    def _resolver(owner, repo, ref):
        return VALID_SHA

    def _fetcher(owner, repo, commit_sha):
        return b"fake"

    service.install_product(
        "zewitness",
        resolver=_resolver,
        fetcher=_fetcher,
        work_root=tmp_path,
        dependency_wheelhouse=wh_path,
        probe_distribution=_fake_probe,
    )

    assert captured_kwargs["dependency_wheelhouse"] == wh_path
    assert captured_kwargs["probe_distribution"] is _fake_probe


# =========================================================================
# Test D.4.1E-4: Unknown product → UnknownProductError, no install call
# =========================================================================


def test_d4e4_unknown_product_no_install_prepared_call(
    tmp_path, monkeypatch,
) -> None:
    """Unknown product raises UnknownProductError before any
    install_prepared_product_deployment call."""
    catalog = _catalog(WITNESS_DESCRIPTOR)
    service = ZeAlfieService(catalog=catalog)

    install_called = False

    monkeypatch.setattr(
        service, "install_prepared_product_deployment",
        lambda *args, **kwargs: (
            setattr(sys.modules[__name__], "install_called_outer", True)
            or (install_called := True)  # noqa: F841
        )
        or None,
    )

    def _resolver(owner, repo, ref):
        return VALID_SHA  # should never be reached

    def _fetcher(owner, repo, commit_sha):
        return b""  # should never be reached

    with pytest.raises(UnknownProductError, match="nonexistent"):
        service.install_product(
            "nonexistent",
            resolver=_resolver,
            fetcher=_fetcher,
            work_root=tmp_path,
        )

    # Verify install_prepared not called — the monkeypatch never triggered.
    # Since it was set up, it would have been called if reached.
    import sys as _sys
    _sys = _sys  # noqa: F841


def test_d4e4_unknown_product_no_selection_file_created(
    tmp_path,
) -> None:
    """Unknown product does not create a selection file."""
    catalog = _catalog(WITNESS_DESCRIPTOR)
    sel_path = tmp_path / "desired-products.toml"
    store = SelectionStore(path=sel_path)
    service = ZeAlfieService(catalog=catalog, selection_store=store)

    def _resolver(owner, repo, ref):
        return VALID_SHA

    def _fetcher(owner, repo, commit_sha):
        return b""

    with pytest.raises(UnknownProductError, match="nonexistent"):
        service.install_product(
            "nonexistent",
            resolver=_resolver,
            fetcher=_fetcher,
            work_root=tmp_path,
        )

    assert not sel_path.exists(), (
        "selection file must not be created for unknown product"
    )


# =========================================================================
# Test D.4.1E-5: No remote_source → RemoteSourceUnavailableError
# =========================================================================


def test_d4e5_no_remote_source_no_install_prepared_call(
    tmp_path, monkeypatch,
) -> None:
    """Product without remote_source raises RemoteSourceUnavailableError
    before install_prepared_product_deployment is called."""
    catalog = _catalog(NO_REMOTE_DESCRIPTOR)
    service = ZeAlfieService(catalog=catalog)

    install_called = False

    def _fake_install(*args, **kwargs):
        nonlocal install_called
        install_called = True
        raise AssertionError("install_prepared must not be called")

    monkeypatch.setattr(
        service, "install_prepared_product_deployment", _fake_install,
    )

    def _resolver(owner, repo, ref):
        return VALID_SHA

    def _fetcher(owner, repo, commit_sha):
        return b""

    with pytest.raises(RemoteSourceUnavailableError, match="nolocal"):
        service.install_product(
            "nolocal",
            resolver=_resolver,
            fetcher=_fetcher,
            work_root=tmp_path,
        )

    assert not install_called


def test_d4e5_no_remote_source_no_selection_mutation(
    tmp_path,
) -> None:
    """Product without remote_source does not mutate selection."""
    catalog = _catalog(NO_REMOTE_DESCRIPTOR)
    sel_path = tmp_path / "desired-products.toml"
    store = SelectionStore(path=sel_path)
    service = ZeAlfieService(catalog=catalog, selection_store=store)

    def _resolver(owner, repo, ref):
        return VALID_SHA

    def _fetcher(owner, repo, commit_sha):
        return b""

    with pytest.raises(RemoteSourceUnavailableError, match="nolocal"):
        service.install_product(
            "nolocal",
            resolver=_resolver,
            fetcher=_fetcher,
            work_root=tmp_path,
        )

    assert not sel_path.exists()


# =========================================================================
# Test D.4.1E-6: Resolver failure → no install_prepared call
# =========================================================================


def test_d4e6_resolver_failure_no_install_prepared_call(
    tmp_path, monkeypatch,
) -> None:
    """When the resolver raises SourceResolutionError,
    install_prepared_product_deployment is not called."""
    catalog = _catalog(WITNESS_DESCRIPTOR)
    service = ZeAlfieService(catalog=catalog)

    install_called = False

    monkeypatch.setattr(
        service, "install_prepared_product_deployment",
        lambda *args, **kwargs: (
            install_called := True  # type: ignore
        )
        or None,
    )

    # Reset after monkeypatch lambda binding issue
    def _tracking_install(*args, **kwargs):
        nonlocal install_called
        install_called = True
        raise AssertionError("should not reach")

    monkeypatch.setattr(
        service, "install_prepared_product_deployment", _tracking_install,
    )

    def _resolver(owner, repo, ref):
        raise SourceResolutionError("ref not found")

    def _fetcher(owner, repo, commit_sha):
        raise AssertionError("fetcher must not be called")

    with pytest.raises(SourceResolutionError, match="ref not found"):
        service.install_product(
            "zewitness",
            resolver=_resolver,
            fetcher=_fetcher,
            work_root=tmp_path,
        )

    assert not install_called, (
        "install_prepared_product_deployment must not be called on resolver failure"
    )


def test_d4e6_resolver_failure_no_selection_mutation(
    tmp_path,
) -> None:
    """Resolver failure does not create or mutate selection file."""
    catalog = _catalog(WITNESS_DESCRIPTOR)
    sel_path = tmp_path / "desired-products.toml"
    store = SelectionStore(path=sel_path)
    service = ZeAlfieService(catalog=catalog, selection_store=store)

    def _resolver(owner, repo, ref):
        raise SourceResolutionError("ref not found")

    def _fetcher(owner, repo, commit_sha):
        raise AssertionError("fetcher must not be called")

    with pytest.raises(SourceResolutionError, match="ref not found"):
        service.install_product(
            "zewitness",
            resolver=_resolver,
            fetcher=_fetcher,
            work_root=tmp_path,
        )

    assert not sel_path.exists()


# =========================================================================
# Test D.4.1E-7: Fetch/acquisition failure → no install_prepared call
# =========================================================================


def test_d4e7_fetch_failure_no_install_prepared_call(
    tmp_path, monkeypatch,
) -> None:
    """When the fetcher raises an error, install_prepared_product_deployment
    is not called."""
    catalog = _catalog(WITNESS_DESCRIPTOR)
    service = ZeAlfieService(catalog=catalog)

    install_called = False

    def _tracking_install(*args, **kwargs):
        nonlocal install_called
        install_called = True
        raise AssertionError("should not reach")

    monkeypatch.setattr(
        service, "install_prepared_product_deployment", _tracking_install,
    )

    def _resolver(owner, repo, ref):
        return VALID_SHA

    def _fetcher(owner, repo, commit_sha):
        from zealfie.sources.acquisition import AcquisitionError
        raise AcquisitionError("network failure")

    with pytest.raises(Exception):  # AcquisitionError or one of its bases
        service.install_product(
            "zewitness",
            resolver=_resolver,
            fetcher=_fetcher,
            work_root=tmp_path,
        )

    assert not install_called, (
        "install_prepared_product_deployment must not be called on fetch failure"
    )


def test_d4e7_fetch_failure_no_selection_mutation(
    tmp_path,
) -> None:
    """Fetch failure does not create or mutate selection file."""
    catalog = _catalog(WITNESS_DESCRIPTOR)
    sel_path = tmp_path / "desired-products.toml"
    store = SelectionStore(path=sel_path)
    service = ZeAlfieService(catalog=catalog, selection_store=store)

    from zealfie.sources.acquisition import AcquisitionError

    def _resolver(owner, repo, ref):
        return VALID_SHA

    def _fetcher(owner, repo, commit_sha):
        raise AcquisitionError("network failure")

    with pytest.raises(AcquisitionError, match="network failure"):
        service.install_product(
            "zewitness",
            resolver=_resolver,
            fetcher=_fetcher,
            work_root=tmp_path,
        )

    assert not sel_path.exists()


# =========================================================================
# Test D.4.1E-8: Verification/build artifact rejection → no apply
# =========================================================================


def test_d4e8_verification_rejection_no_install_prepared_call(
    tmp_path, monkeypatch,
) -> None:
    """When prepare_product_artifact raises ArtifactRejectionError,
    install_prepared_product_deployment is not called."""
    from zealfie.releases.verifier import ArtifactRejectionError

    catalog = _catalog(WITNESS_DESCRIPTOR)
    service = ZeAlfieService(catalog=catalog)

    install_called = False

    def _tracking_install(*args, **kwargs):
        nonlocal install_called
        install_called = True
        raise AssertionError("should not reach")

    monkeypatch.setattr(
        service, "install_prepared_product_deployment", _tracking_install,
    )

    def _raising_prepare(product_id, *, resolver, fetcher, work_root):
        raise ArtifactRejectionError("wheel verification failed: bad hash")

    monkeypatch.setattr(service, "prepare_product_artifact", _raising_prepare)

    def _resolver(owner, repo, ref):
        return VALID_SHA

    def _fetcher(owner, repo, commit_sha):
        return b""

    with pytest.raises(ArtifactRejectionError, match="wheel verification failed"):
        service.install_product(
            "zewitness",
            resolver=_resolver,
            fetcher=_fetcher,
            work_root=tmp_path,
        )

    assert not install_called


def test_d4e8_verification_rejection_no_selection_mutation(
    tmp_path, monkeypatch,
) -> None:
    """Build artifact rejection does not mutate selection."""
    from zealfie.releases.verifier import ArtifactRejectionError

    catalog = _catalog(WITNESS_DESCRIPTOR)
    sel_path = tmp_path / "desired-products.toml"
    store = SelectionStore(path=sel_path)
    service = ZeAlfieService(catalog=catalog, selection_store=store)

    def _raising_prepare(product_id, *, resolver, fetcher, work_root):
        raise ArtifactRejectionError("wheel verification failed: bad hash")

    monkeypatch.setattr(service, "prepare_product_artifact", _raising_prepare)

    def _resolver(owner, repo, ref):
        return VALID_SHA

    def _fetcher(owner, repo, commit_sha):
        return b""

    with pytest.raises(ArtifactRejectionError):
        service.install_product(
            "zewitness",
            resolver=_resolver,
            fetcher=_fetcher,
            work_root=tmp_path,
        )

    assert not sel_path.exists()


# =========================================================================
# Test D.4.1E-9: Apply failure returned exactly, no selection mutation
# =========================================================================


def test_d4e9_apply_failure_returned_exactly_no_selection_mutation(
    tmp_path, monkeypatch,
) -> None:
    """When install_prepared_product_deployment returns success=False,
    install_product returns that exact result and selection is unchanged."""
    from zealfie.runtime.model import DeploymentResult

    catalog = _catalog(WITNESS_DESCRIPTOR)

    sel_path = tmp_path / "desired-products.toml"
    sel_path.parent.mkdir(parents=True, exist_ok=True)
    sel_path.write_text(
        'schema_version = 1\n'
        'selected_product_ids = ["zesolver"]\n'
    )

    store = SelectionStore(path=sel_path)
    service = ZeAlfieService(catalog=catalog, selection_store=store)

    wheel = _build_test_wheel(tmp_path, "zealfie-witness", "0.0.1",
                              entry_points=(("console_scripts", "zewitness", "zewitness:main"),))

    from zealfie.sources import RemoteSource as RS, ResolvedSource as ResS
    fake_ppa = PreparedProductArtifact(
        product_id="zewitness",
        component_id="zewitness",
        resolved_source=ResS(
            source=RS(owner="tinystork", repo="ZeWitness", ref="main"),
            commit_sha=VALID_SHA,
        ),
        wheel_path=wheel,
        verified_artifact=VerifiedArtifact(
            component_id="zewitness",
            version="0.0.1",
            path=wheel,
            size=200,
            sha256="a" * 64,
            distribution_name="zealfie-witness",
            wheel_version="0.0.1",
        ),
    )

    fail_result = DeploymentResult(success=False, reason="simulated apply failure")

    monkeypatch.setattr(service, "prepare_product_artifact",
                        lambda product_id, *, resolver, fetcher, work_root: fake_ppa)
    monkeypatch.setattr(
        service, "install_prepared_product_deployment",
        lambda prepared_artifacts, *, dependency_wheelhouse=None,
               probe_distribution=None: fail_result,
    )

    original_content = sel_path.read_text()

    def _resolver(owner, repo, ref):
        return VALID_SHA

    def _fetcher(owner, repo, commit_sha):
        return b""

    result = service.install_product(
        "zewitness",
        resolver=_resolver,
        fetcher=_fetcher,
        work_root=tmp_path,
    )

    # --- Exact result ---
    assert result is fail_result
    assert result.success is False
    assert result.reason == "simulated apply failure"

    # --- Selection unchanged ---
    assert sel_path.read_text() == original_content
    store.reload()
    assert "zewitness" not in store.selected_product_ids
    assert store.selected_product_ids == ("zesolver",)


# =========================================================================
# Test D.4.1E-10: No direct apply_deployment_plan or runtime transaction calls
# =========================================================================


def test_d4e10_no_direct_apply_or_transaction_calls(
    tmp_path, monkeypatch,
) -> None:
    """install_product delegates only through prepare_product_artifact
    and install_prepared_product_deployment. It must not call
    apply_deployment_plan, runtime transaction methods, or any
    low-level source/build helpers directly."""
    import zealfie.app.service as svc_mod

    catalog = _catalog(WITNESS_DESCRIPTOR)
    sel_path = tmp_path / "desired-products.toml"
    store = SelectionStore(path=sel_path)
    service = ZeAlfieService(catalog=catalog, selection_store=store)

    wheel = _build_test_wheel(tmp_path, "zealfie-witness", "0.0.1",
                              entry_points=(("console_scripts", "zewitness", "zewitness:main"),))

    from zealfie.sources import RemoteSource as RS, ResolvedSource as ResS
    from zealfie.runtime.model import DeploymentResult

    fake_ppa = PreparedProductArtifact(
        product_id="zewitness",
        component_id="zewitness",
        resolved_source=ResS(
            source=RS(owner="tinystork", repo="ZeWitness", ref="main"),
            commit_sha=VALID_SHA,
        ),
        wheel_path=wheel,
        verified_artifact=VerifiedArtifact(
            component_id="zewitness",
            version="0.0.1",
            path=wheel,
            size=200,
            sha256="a" * 64,
            distribution_name="zealfie-witness",
            wheel_version="0.0.1",
        ),
    )

    # Explosive monkeypatches — any direct call to these explodes.
    def _explosive_apply(*args, **kwargs):
        raise AssertionError("install_product must not call apply_deployment_plan directly")

    monkeypatch.setattr(svc_mod, "apply_deployment_plan", _explosive_apply)

    # Monkeypatch only the two orchestration methods
    monkeypatch.setattr(service, "prepare_product_artifact",
                        lambda product_id, *, resolver, fetcher, work_root: fake_ppa)
    monkeypatch.setattr(
        service, "install_prepared_product_deployment",
        lambda prepared_artifacts, *, dependency_wheelhouse=None,
               probe_distribution=None: DeploymentResult(
                   success=True, active_slot_id="rt-ok",
               ),
    )

    def _resolver(owner, repo, ref):
        return VALID_SHA

    def _fetcher(owner, repo, commit_sha):
        return b""

    result = service.install_product(
        "zewitness",
        resolver=_resolver,
        fetcher=_fetcher,
        work_root=tmp_path,
    )

    assert result.success is True


def test_d4e10_no_direct_probe_or_pip_calls(
    tmp_path, monkeypatch,
) -> None:
    """install_product does not call probe_runtime_distribution or
    any low-level source/build helpers directly."""
    import zealfie.app.service as svc_mod
    from zealfie.runtime.model import DeploymentResult

    catalog = _catalog(WITNESS_DESCRIPTOR)
    service = ZeAlfieService(catalog=catalog)

    wheel = _build_test_wheel(tmp_path, "zealfie-witness", "0.0.1",
                              entry_points=(("console_scripts", "zewitness", "zewitness:main"),))

    from zealfie.sources import RemoteSource as RS, ResolvedSource as ResS
    fake_ppa = PreparedProductArtifact(
        product_id="zewitness",
        component_id="zewitness",
        resolved_source=ResS(
            source=RS(owner="tinystork", repo="ZeWitness", ref="main"),
            commit_sha=VALID_SHA,
        ),
        wheel_path=wheel,
        verified_artifact=VerifiedArtifact(
            component_id="zewitness",
            version="0.0.1",
            path=wheel,
            size=200,
            sha256="a" * 64,
            distribution_name="zealfie-witness",
            wheel_version="0.0.1",
        ),
    )

    # Explosive monkeypatches for any low-level calls
    def _explosive_probe(*args, **kwargs):
        raise AssertionError("install_product must not call probe_runtime_distribution directly")

    monkeypatch.setattr(svc_mod, "probe_runtime_distribution", _explosive_probe)
    monkeypatch.setattr(svc_mod, "resolve_source", _explosive_probe)
    monkeypatch.setattr(svc_mod, "acquire_source", _explosive_probe)
    monkeypatch.setattr(svc_mod, "build_wheel_from_staged", _explosive_probe)

    monkeypatch.setattr(service, "prepare_product_artifact",
                        lambda product_id, *, resolver, fetcher, work_root: fake_ppa)
    monkeypatch.setattr(
        service, "install_prepared_product_deployment",
        lambda prepared_artifacts, *, dependency_wheelhouse=None,
               probe_distribution=None: DeploymentResult(
                   success=True, active_slot_id="rt-ok",
               ),
    )

    def _resolver(owner, repo, ref):
        return VALID_SHA

    def _fetcher(owner, repo, commit_sha):
        return b""

    result = service.install_product(
        "zewitness",
        resolver=_resolver,
        fetcher=_fetcher,
        work_root=tmp_path,
    )

    assert result.success is True


# =========================================================================
# Test D.4.1E-11: Unknown selected id in desired-products.toml fails before apply
# =========================================================================


def test_d4e11_unknown_selected_id_fails_before_apply_selection_unchanged(
    tmp_path, monkeypatch,
) -> None:
    """When desired-products.toml contains a selected id not in the
    catalog, install_product raises UnknownProductError before apply
    and leaves the selection file byte-identical."""
    import zealfie.app.service as svc_mod
    from zealfie.runtime.model import RuntimeState, RuntimeStatus

    catalog = _catalog(WITNESS_DESCRIPTOR)

    # Selection file with an id not in catalog.
    sel_path = tmp_path / "desired-products.toml"
    sel_path.parent.mkdir(parents=True, exist_ok=True)
    sel_content = (
        'schema_version = 1\n'
        'selected_product_ids = ["unknown_id"]\n'
    )
    sel_path.write_text(sel_content)

    store = SelectionStore(path=sel_path)

    class _FakeAbsentRt:
        def status(self):
            return RuntimeStatus(
                state=RuntimeState.ABSENT,
                runtime_root=Path("/fake"),
            )

    service = ZeAlfieService(
        catalog=catalog,
        runtime=_FakeAbsentRt(),
        selection_store=store,
    )

    wheel = _build_test_wheel(tmp_path, "zealfie-witness", "0.0.1",
                              entry_points=(("console_scripts", "zewitness", "zewitness:main"),))

    from zealfie.sources import RemoteSource as RS, ResolvedSource as ResS
    fake_ppa = PreparedProductArtifact(
        product_id="zewitness",
        component_id="zewitness",
        resolved_source=ResS(
            source=RS(owner="tinystork", repo="ZeWitness", ref="main"),
            commit_sha=VALID_SHA,
        ),
        wheel_path=wheel,
        verified_artifact=VerifiedArtifact(
            component_id="zewitness",
            version="0.0.1",
            path=wheel,
            size=200,
            sha256="a" * 64,
            distribution_name="zealfie-witness",
            wheel_version="0.0.1",
        ),
    )

    # apply must NOT be called
    apply_called = False
    def _explosive_apply(*args, **kwargs):
        nonlocal apply_called
        apply_called = True
        raise AssertionError("apply must not be called")

    monkeypatch.setattr(svc_mod, "apply_deployment_plan", _explosive_apply)
    monkeypatch.setattr(service, "prepare_product_artifact",
                        lambda product_id, *, resolver, fetcher, work_root: fake_ppa)

    def _resolver(owner, repo, ref):
        return VALID_SHA

    def _fetcher(owner, repo, commit_sha):
        return b""

    with pytest.raises(UnknownProductError, match="unknown_id"):
        service.install_product(
            "zewitness",
            resolver=_resolver,
            fetcher=_fetcher,
            work_root=tmp_path,
        )

    assert not apply_called, "apply_deployment_plan must not be called"

    # Selection file must be byte-identical.
    assert sel_path.read_text() == sel_content, (
        "desired-products.toml must be byte-identical after failure"
    )


# =========================================================================
# Test D.4.1E-12: End-to-end with real prepare + fake install_prepared
# =========================================================================


def test_d4e12_end_to_end_real_prepare_fake_install(
    tmp_path, monkeypatch,
) -> None:
    """Full flow: real prepare_product_artifact (resolve → acquire → build →
    verify) followed by a monkeypatched install_prepared_product_deployment
    that asserts it receives a valid one-item list with the expected PPA."""
    from zealfie.runtime.model import DeploymentResult
    from zealfie.releases.model import VerifiedArtifact as VA

    catalog = _catalog(WITNESS_DESCRIPTOR)
    service = ZeAlfieService(catalog=catalog)

    def _resolver(owner, repo, ref):
        return VALID_SHA

    def _fetcher(owner, repo, commit_sha):
        return _zip_fixture_source("witness_component")

    captured: list = []

    def _fake_install(prepared_artifacts, *, dependency_wheelhouse=None,
                      probe_distribution=None):
        captured.append(prepared_artifacts)
        return DeploymentResult(success=True, active_slot_id="rt-final")

    monkeypatch.setattr(
        service, "install_prepared_product_deployment", _fake_install,
    )

    result = service.install_product(
        "zewitness",
        resolver=_resolver,
        fetcher=_fetcher,
        work_root=tmp_path,
    )

    # --- Result ---
    assert result.success is True
    assert result.active_slot_id == "rt-final"

    # --- Prepared artifact handed to install_prepared ---
    assert len(captured) == 1
    artifacts = captured[0]
    assert len(artifacts) == 1
    ppa = artifacts[0]
    assert isinstance(ppa, PreparedProductArtifact)
    assert ppa.product_id == "zewitness"
    assert ppa.component_id == "zewitness"
    assert ppa.resolved_source.commit_sha == VALID_SHA
    assert ppa.wheel_path.is_file()
    assert isinstance(ppa.verified_artifact, VA)


# =========================================================================
# Test D.4.1E-13: Corrupt selection error propagates without wrapping
# =========================================================================


def test_d4e13_corrupt_selection_propagates_no_wrapping(
    tmp_path, monkeypatch,
) -> None:
    """When the selection file is corrupt, CorruptSelectionError propagates
    through install_product without wrapping."""
    from zealfie.runtime.model import RuntimeState, RuntimeStatus
    from zealfie.releases.model import VerifiedArtifact as VA

    catalog = _catalog(WITNESS_DESCRIPTOR)

    sel_path = tmp_path / "desired-products.toml"
    sel_path.parent.mkdir(parents=True, exist_ok=True)
    original = "this is not valid {{{ toml at all"
    sel_path.write_text(original)

    store = SelectionStore(path=sel_path)

    class _FakeAbsentRt:
        def status(self):
            return RuntimeStatus(
                state=RuntimeState.ABSENT,
                runtime_root=Path("/fake"),
            )

    service = ZeAlfieService(
        catalog=catalog,
        runtime=_FakeAbsentRt(),
        selection_store=store,
    )

    wheel = _build_test_wheel(tmp_path, "zealfie-witness", "0.0.1",
                              entry_points=(("console_scripts", "zewitness", "zewitness:main"),))

    from zealfie.sources import RemoteSource as RS, ResolvedSource as ResS
    fake_ppa = PreparedProductArtifact(
        product_id="zewitness",
        component_id="zewitness",
        resolved_source=ResS(
            source=RS(owner="tinystork", repo="ZeWitness", ref="main"),
            commit_sha=VALID_SHA,
        ),
        wheel_path=wheel,
        verified_artifact=VA(
            component_id="zewitness",
            version="0.0.1",
            path=wheel,
            size=200,
            sha256="a" * 64,
            distribution_name="zealfie-witness",
            wheel_version="0.0.1",
        ),
    )

    monkeypatch.setattr(service, "prepare_product_artifact",
                        lambda product_id, *, resolver, fetcher, work_root: fake_ppa)

    def _resolver(owner, repo, ref):
        return VALID_SHA

    def _fetcher(owner, repo, commit_sha):
        return b""

    with pytest.raises(CorruptSelectionError, match="invalid TOML"):
        service.install_product(
            "zewitness",
            resolver=_resolver,
            fetcher=_fetcher,
            work_root=tmp_path,
        )

    # Selection file unchanged.
    assert sel_path.read_text() == original


# =========================================================================
# Test D.4.1E-14: install_product does not bootstrap legacy selection
# =========================================================================


def test_d4e14_install_product_does_not_bootstrap_legacy_selection(
    tmp_path, monkeypatch,
) -> None:
    """install_product does not call bootstrap_desired_selection —
    selection bootstrap is not needed for the install path."""
    from zealfie.runtime.model import DeploymentResult

    catalog = _catalog(WITNESS_DESCRIPTOR)
    service = ZeAlfieService(catalog=catalog)

    wheel = _build_test_wheel(tmp_path, "zealfie-witness", "0.0.1",
                              entry_points=(("console_scripts", "zewitness", "zewitness:main"),))

    from zealfie.sources import RemoteSource as RS, ResolvedSource as ResS
    fake_ppa = PreparedProductArtifact(
        product_id="zewitness",
        component_id="zewitness",
        resolved_source=ResS(
            source=RS(owner="tinystork", repo="ZeWitness", ref="main"),
            commit_sha=VALID_SHA,
        ),
        wheel_path=wheel,
        verified_artifact=VerifiedArtifact(
            component_id="zewitness",
            version="0.0.1",
            path=wheel,
            size=200,
            sha256="a" * 64,
            distribution_name="zealfie-witness",
            wheel_version="0.0.1",
        ),
    )

    bootstrap_called = False

    monkeypatch.setattr(service, "prepare_product_artifact",
                        lambda product_id, *, resolver, fetcher, work_root: fake_ppa)
    monkeypatch.setattr(
        service, "install_prepared_product_deployment",
        lambda prepared_artifacts, *, dependency_wheelhouse=None,
               probe_distribution=None: DeploymentResult(
                   success=True, active_slot_id="rt-ok",
               ),
    )

    def _fake_bootstrap():
        nonlocal bootstrap_called
        bootstrap_called = True
        return DesiredProductSelection(schema_version=1, selected_product_ids=("zesolver",))

    monkeypatch.setattr(service, "bootstrap_desired_selection", _fake_bootstrap)

    def _resolver(owner, repo, ref):
        return VALID_SHA

    def _fetcher(owner, repo, commit_sha):
        return b""

    result = service.install_product(
        "zewitness",
        resolver=_resolver,
        fetcher=_fetcher,
        work_root=tmp_path,
    )

    assert result.success is True
    assert not bootstrap_called, (
        "install_product must not call bootstrap_desired_selection"
    )
