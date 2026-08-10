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
