"""Tests for ZA-M1-3A.3 LOT C+D — shared verified artifact cache.

Covers:

* the content-addressed store + rebuildable index (LOT C core):
  put/reuse, digest-verified reuse, corrupted index fail-safe,
  same-filename-wrong-bytes rejection, tag/identity checks;
* dependency wheelhouse cache reuse (C.2): fail-closed seeding with
  explicit acquisition counting (0 remote on cache hits);
* accelerated GPU cache reuse (C.3): same manifest variant is never
  re-downloaded, corrupt cache re-acquires;
* product KEEP cache reuse (C.1) at the SERVICE layer with explicit
  fetch/build counters: cache hit → 0 GitHub fetch + 0 rebuild; cache
  file missing / bad digest → honest re-acquisition;
* bounded cache GC retention (LOT D): ACTIVE/PREVIOUS state references
  (provenance digests, installed-lock identities, accelerated metadata
  digests) are protected; corrupt state blocks all deletion;
* the ``runtime gc-cache`` CLI.

All tests are FAST and hermetic: no real venv, no network, no pip; the
only subprocess patched is ``pip download`` (simulated), and the only
"builds" are byte copies of prebuilt synthetic wheels.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import shutil
import zipfile
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

import zealfie.cli as cli
from zealfie.acceleration import (
    AcceleratedArtifactEntry,
    AcceleratedArtifactManifest,
    AcceleratedDeploymentPlan,
    AcceleratedPlanStatus,
    AcceleratedVariant,
    HardwareCompatibility,
    HardwareCompatibilityReasonCode,
    HardwareCompatibilityStatus,
    ManifestAcceleratedArtifactAcquirer,
    PlannedAcceleratedDependency,
    VariantStatus,
)
from zealfie.app import (
    ProductCatalog,
    ProductDescriptor,
    SelectionStore,
    ZeAlfieService,
)
from zealfie.components.model import EntryPointContract
from zealfie.dependencies.acquisition import build_acquisition_request
from zealfie.dependencies.host_tags import default_compatible_tags
from zealfie.dependencies.pip_acquirer import PipWheelhouseAcquirer
from zealfie.products.policy import ProductPolicyStore
from zealfie.releases.model import HostTarget
from zealfie.runtime.artifact_cache import (
    ArtifactCacheStore,
    apply_cache_gc_plan,
    build_cache_gc_plan,
    runtime_cache_gc,
)
from zealfie.runtime.installed_lock import InstalledLockStore
from zealfie.runtime.layout import RuntimeLayout
from zealfie.runtime.model import DeploymentResult, RuntimeState, RuntimeStatus
from zealfie.runtime.provenance import ProductProvenanceStore
from zealfie.runtime.state import load_active_state, save_active_state
from zealfie.sources import RemoteSource


# ═════════════════════════════════════════════════════════════════════════
# Synthetic wheel helpers (pure zipfile — no pip, no build)
# ═════════════════════════════════════════════════════════════════════════


def _make_wheel(
    output: Path,
    name: str,
    version: str,
    *,
    requires_dist: tuple[str, ...] = (),
    entry_points: tuple[tuple[str, str, str], ...] = (),
    tags: tuple[str, str, str] = ("py3", "none", "any"),
) -> Path:
    """Synthesize a minimal, pip-parseable wheel via zipfile."""
    safe_name = name.replace("-", "_").replace(".", "_")
    tag_str = "-".join(tags)
    wheel_name = f"{safe_name}-{version}-{tag_str}.whl"
    wheel_path = output / wheel_name
    dist_info = f"{safe_name}-{version}.dist-info"

    wheelfile = (
        "Wheel-Version: 1.0\n"
        "Generator: test\n"
        "Root-Is-Purelib: true\n"
        f"Tag: {tag_str}\n"
    )
    metadata = f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n"
    for req in requires_dist:
        metadata += f"Requires-Dist: {req}\n"

    members: list[tuple[str, str]] = [
        (f"{dist_info}/WHEEL", wheelfile),
        (f"{dist_info}/METADATA", metadata),
    ]
    if entry_points:
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
    record_lines = [f"{dist_info}/RECORD,,"]
    record = "\n".join(record_lines) + "\n"
    members.append((f"{dist_info}/RECORD", record))

    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, content in members:
            zf.writestr(arcname, content)
    return wheel_path


def _sha256(path: Path) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            sha.update(chunk)
    return sha.hexdigest()


# ═════════════════════════════════════════════════════════════════════════
# Part 1 — store: content addressing, verification, index resilience
# ═════════════════════════════════════════════════════════════════════════


def test_store_put_and_digest_verified_reuse(tmp_path: Path) -> None:
    store = ArtifactCacheStore(tmp_path / "cache" / "artifacts")
    wheel = _make_wheel(tmp_path, "leaf-lib", "1.0.0")
    entry = store.put(wheel, kind="dependency")
    assert entry is not None
    assert entry.sha256 == _sha256(wheel)
    assert entry.distribution == "leaf-lib"

    path = store.cached_path_for_digest(entry.sha256)
    assert path is not None
    assert _sha256(path) == entry.sha256

    # Unknown digest → miss.
    assert store.cached_path_for_digest("a" * 64) is None


def test_store_same_filename_wrong_bytes_never_accepted(tmp_path: Path) -> None:
    store = ArtifactCacheStore(tmp_path / "cache" / "artifacts")
    wheel = _make_wheel(tmp_path, "leaf-lib", "1.0.0")
    sha = _sha256(wheel)
    store.put(wheel, kind="dependency")

    # Replace the cached file with different bytes under the SAME filename.
    cached = store.cached_path_for_digest(sha)
    assert cached is not None
    cached.write_bytes(cached.read_bytes() + b"tampered")

    # Digest re-verification must reject it — filename is never a proof.
    assert store.cached_path_for_digest(sha) is None
    assert (
        store.resolve_dependency("leaf-lib", "1.0.0",
                                 compatible_tags=default_compatible_tags())
        is None
    )


def test_store_missing_file_is_miss(tmp_path: Path) -> None:
    store = ArtifactCacheStore(tmp_path / "cache" / "artifacts")
    wheel = _make_wheel(tmp_path, "leaf-lib", "1.0.0")
    sha = _sha256(wheel)
    store.put(wheel, kind="dependency")
    cached = store.cached_path_for_digest(sha)
    assert cached is not None
    cached.unlink()
    assert store.cached_path_for_digest(sha) is None
    assert store.resolve_dependency("leaf-lib", "1.0.0") is None


def test_store_corrupted_index_fail_safe_and_rebuildable(tmp_path: Path) -> None:
    store = ArtifactCacheStore(tmp_path / "cache" / "artifacts")
    wheel = _make_wheel(tmp_path, "leaf-lib", "1.0.0")
    sha = _sha256(wheel)
    store.put(wheel, kind="dependency")

    # Corrupt the index.
    store.index_path.write_text("{not json!!", encoding="utf-8")
    # Digest-based reuse is index-independent and still verifies bytes.
    assert store.cached_path_for_digest(sha) is not None
    # Identity-based reuse fails safe (index unusable → rebuilt from disk,
    # which recovers the entry — but NEVER activates without verification).
    path = store.resolve_dependency(
        "leaf-lib", "1.0.0", compatible_tags=default_compatible_tags()
    )
    if path is not None:
        assert _sha256(path) == sha

    # Rebuild recovers the entry from the content-addressed store.
    store.rebuild_index()
    recovered = store.resolve_dependency(
        "leaf-lib", "1.0.0", compatible_tags=default_compatible_tags()
    )
    assert recovered is not None
    assert _sha256(recovered) == sha

    # A put after corruption still works and heals the index.
    wheel2 = _make_wheel(tmp_path, "mid-lib", "2.0.0")
    entry2 = store.put(wheel2, kind="dependency")
    assert entry2 is not None
    assert store.resolve_dependency(
        "mid-lib", "2.0.0", compatible_tags=default_compatible_tags()
    ) is not None


def test_store_ambiguous_dependency_identity_never_used(tmp_path: Path) -> None:
    store = ArtifactCacheStore(tmp_path / "cache" / "artifacts")
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    wheel_a = _make_wheel(tmp_path / "a", "leaf-lib", "1.0.0")
    wheel_b = _make_wheel(tmp_path / "b", "leaf-lib", "1.0.0")
    # Different bytes, same identity: force distinct content.
    with zipfile.ZipFile(wheel_b, "a") as zf:
        zf.writestr("extra.txt", "x")
    assert _sha256(wheel_a) != _sha256(wheel_b)
    store.put(wheel_a, kind="dependency")
    store.put(wheel_b, kind="dependency")

    # Two entries for one identity → fail-closed (no invention).
    assert store.resolve_dependency(
        "leaf-lib", "1.0.0", compatible_tags=default_compatible_tags()
    ) is None


def test_store_wrong_platform_tag_never_used(tmp_path: Path) -> None:
    store = ArtifactCacheStore(tmp_path / "cache" / "artifacts")
    foreign = _make_wheel(
        tmp_path, "leaf-lib", "1.0.0", tags=("py3", "none", "win_amd64")
    )
    store.put(foreign, kind="dependency")
    # Host tags (this interpreter) never include win_amd64.
    assert store.resolve_dependency(
        "leaf-lib", "1.0.0", compatible_tags=default_compatible_tags()
    ) is None


def test_store_put_refuses_unparseable_filename(tmp_path: Path) -> None:
    store = ArtifactCacheStore(tmp_path / "cache" / "artifacts")
    bogus = tmp_path / "not-a-wheel.bin"
    bogus.write_bytes(b"data")
    assert store.put(bogus, kind="dependency") is None


def test_store_resolve_accelerated_identity_checks(tmp_path: Path) -> None:
    store = ArtifactCacheStore(tmp_path / "cache" / "artifacts")
    wheel = _make_wheel(tmp_path, "fake-accel", "1.0.0")
    sha = _sha256(wheel)
    size = wheel.stat().st_size
    store.put(wheel, kind="accelerated", distribution="fake-accel",
              version="1.0.0")

    good = store.resolve_accelerated(
        sha256=sha, distribution="fake-accel", version="1.0.0",
        filename=wheel.name, size=size,
    )
    assert good is not None
    assert _sha256(good) == sha

    # Wrong filename → never used.
    assert store.resolve_accelerated(
        sha256=sha, distribution="fake-accel", version="1.0.0",
        filename="other-1.0.0-py3-none-any.whl", size=size,
    ) is None
    # Wrong identity → never used.
    assert store.resolve_accelerated(
        sha256=sha, distribution="some-other", version="1.0.0",
        filename=wheel.name, size=size,
    ) is None
    # Wrong size → never used.
    assert store.resolve_accelerated(
        sha256=sha, distribution="fake-accel", version="1.0.0",
        filename=wheel.name, size=size + 1,
    ) is None


# ═════════════════════════════════════════════════════════════════════════
# Part 2 — bounded cache GC retention (LOT D)
# ═════════════════════════════════════════════════════════════════════════


def test_gc_keeps_digest_and_dependency_protected_artifacts(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache" / "artifacts"
    store = ArtifactCacheStore(cache_root)
    product = _make_wheel(tmp_path, "product-x", "0.1.0")
    dep = _make_wheel(tmp_path, "leaf-lib", "1.0.0")
    orphan = _make_wheel(tmp_path, "orphan-lib", "9.9.9")
    product_sha = _sha256(product)
    store.put(product, kind="product", distribution="product-x",
              version="0.1.0")
    store.put(dep, kind="dependency")
    store.put(orphan, kind="dependency")

    plan = build_cache_gc_plan(
        cache_root,
        protected_digests=frozenset({product_sha}),
        protected_dependency_ids=frozenset({("leaf-lib", "1.0.0")}),
    )
    assert len(plan.candidates) == 1
    assert plan.candidates[0].name == orphan.name
    assert plan.retained == 2

    result = apply_cache_gc_plan(cache_root, plan)
    assert len(result.deleted) == 1
    assert result.reclaimed_bytes > 0
    assert not result.errors
    # Protected artifacts survive; the orphan is gone from disk AND index.
    assert store.cached_path_for_digest(product_sha) is not None
    assert store.resolve_dependency(
        "leaf-lib", "1.0.0", compatible_tags=default_compatible_tags()
    ) is not None
    assert _sha256(orphan) not in store.load_index()


def test_runtime_cache_gc_collects_references_from_state_stores(
    tmp_path: Path,
) -> None:
    layout = RuntimeLayout(tmp_path / "rt")
    layout.state_dir.mkdir(parents=True, exist_ok=True)

    product = _make_wheel(tmp_path, "product-x", "0.1.0")
    accel = _make_wheel(tmp_path, "fake-accel", "1.0.0")
    dep = _make_wheel(tmp_path, "leaf-lib", "1.0.0")
    orphan = _make_wheel(tmp_path, "orphan-lib", "9.9.9")
    product_sha = _sha256(product)
    accel_sha = _sha256(accel)

    store = ArtifactCacheStore(layout.artifact_cache_dir)
    store.put(product, kind="product", distribution="product-x",
              version="0.1.0")
    store.put(accel, kind="accelerated", distribution="fake-accel",
              version="1.0.0")
    store.put(dep, kind="dependency")
    store.put(orphan, kind="dependency")

    # State stores: provenance (product wheel digest), installed lock
    # (leaf-lib identity), accelerated metadata (accel wheel digest).
    (layout.state_dir / "product-provenance.json").write_text(
        json.dumps({
            "schema_version": 1,
            "slots": {
                "rt-000000000001": {
                    "product-x": {
                        "version": "0.1.0",
                        "source_owner": "o", "source_repo": "r",
                        "requested_ref": "main",
                        "commit_sha": "d" * 40,
                        "wheel_sha256": product_sha,
                    },
                },
            },
        }),
        encoding="utf-8",
    )
    (layout.state_dir / "installed-lock.json").write_text(
        json.dumps({
            "schema_version": 1,
            "slots": {
                "rt-000000000001": {
                    "primary_names": ["product-x"],
                    "dependencies": {
                        "leaf-lib": {"name": "leaf-lib", "version": "1.0.0"},
                    },
                },
            },
        }),
        encoding="utf-8",
    )
    (layout.state_dir / "accelerated-metadata.json").write_text(
        json.dumps({
            "schema_version": 1,
            "slots": {
                "rt-000000000001": {
                    "backend": "NVIDIA_CUDA",
                    "variants": [["fake-accel", "1.0.0", accel_sha]],
                },
            },
        }),
        encoding="utf-8",
    )

    result = runtime_cache_gc(layout.root)
    assert not result.errors, result.errors
    assert len(result.deleted) == 1
    assert result.deleted[0].name == orphan.name

    assert store.cached_path_for_digest(product_sha) is not None
    assert store.cached_path_for_digest(accel_sha) is not None
    assert store.resolve_dependency(
        "leaf-lib", "1.0.0", compatible_tags=default_compatible_tags()
    ) is not None


def test_runtime_cache_gc_blocks_all_deletion_on_corrupt_state(
    tmp_path: Path,
) -> None:
    layout = RuntimeLayout(tmp_path / "rt")
    layout.state_dir.mkdir(parents=True, exist_ok=True)
    orphan = _make_wheel(tmp_path, "orphan-lib", "9.9.9")
    store = ArtifactCacheStore(layout.artifact_cache_dir)
    store.put(orphan, kind="dependency")
    # Corrupt provenance: the protected set cannot be established.
    (layout.state_dir / "product-provenance.json").write_text(
        "{corrupt", encoding="utf-8"
    )
    result = runtime_cache_gc(layout.root)
    assert len(result.deleted) == 0
    assert result.errors and any("blocked" in e for e in result.errors)
    # The orphan survives (fail-closed: no deletion without a protection set).
    assert _sha256(orphan) in store.load_index()


# ═════════════════════════════════════════════════════════════════════════
# Part 3 — accelerated GPU cache reuse (C.3)
# ═════════════════════════════════════════════════════════════════════════


def _accel_plan(*distributions: str) -> AcceleratedDeploymentPlan:
    entries = tuple(
        PlannedAcceleratedDependency(
            distribution=distribution,
            specifier=None,
            extras=(),
            declaring_products=("zebench",),
            variant=AcceleratedVariant(
                distribution=distribution,
                version="1.0.0",
                backend="NVIDIA_CUDA",
            ),
            variant_status=VariantStatus.SELECTED,
        )
        for distribution in distributions
    )
    return AcceleratedDeploymentPlan(
        status=AcceleratedPlanStatus.PLAN_READY,
        hardware=HardwareCompatibility(
            status=HardwareCompatibilityStatus.SUPPORTED,
            reason_code=HardwareCompatibilityReasonCode.COMPATIBLE.value,
            reason="compatible",
            products_concerned=("zebench",),
        ),
        backend="NVIDIA_CUDA",
        products_concerned=("zebench",),
        keep_products=(),
        added_requirements=entries,
        source_runtime_state="READY",
        source_active_slot_id="rt-a",
        source_previous_slot_id=None,
        target_runtime="accelerated",
        blocked=False,
        blocked_reason=None,
        closure_impact=(),
    )


class _CountingUrlopen:
    def __init__(self, wheel: Path) -> None:
        self.calls = 0
        self._wheel = wheel

    def __call__(self, url: str, timeout=None):
        self.calls += 1
        return open(self._wheel, "rb")


def _accel_manifest(wheel: Path) -> AcceleratedArtifactManifest:
    host = HostTarget.from_current_host()
    return AcceleratedArtifactManifest((
        AcceleratedArtifactEntry(
            distribution="fake-accel",
            version="1.0.0",
            backend="NVIDIA_CUDA",
            platform=host.platform_tag,
            filename=wheel.name,
            url=wheel.resolve().as_uri(),
            size=wheel.stat().st_size,
            sha256=_sha256(wheel),
            python=None,
            requires_python=None,
        ),
    ))


def _accel_acquirer(
    manifest: AcceleratedArtifactManifest, cache: ArtifactCacheStore,
    urlopen,
) -> ManifestAcceleratedArtifactAcquirer:
    host = HostTarget.from_current_host()
    return ManifestAcceleratedArtifactAcquirer(
        manifest,
        platform_tag=host.platform_tag,
        python_tag="cp3",
        urlopen=urlopen,
        cache=cache,
    )


def test_accelerated_cache_hit_never_redownloads_same_variant(
    tmp_path: Path,
) -> None:
    wheel = _make_wheel(tmp_path, "fake-accel", "1.0.0")
    manifest = _accel_manifest(wheel)
    urlopen = _CountingUrlopen(wheel)
    cache = ArtifactCacheStore(tmp_path / "cache" / "artifacts")
    acquirer = _accel_acquirer(manifest, cache, urlopen)
    plan = _accel_plan("fake-accel")

    first = acquirer.acquire(plan, tmp_path / "work1")
    assert len(first) == 1
    assert first[0].sha256 == manifest.entries[0].sha256
    assert urlopen.calls == 1

    # Second deployment of the SAME variant: 0 downloads.
    second = acquirer.acquire(plan, tmp_path / "work2")
    assert len(second) == 1
    assert second[0].sha256 == manifest.entries[0].sha256
    assert urlopen.calls == 1
    # The materialized wheel is a real, verified file in the new work root.
    assert _sha256(second[0].wheel_path) == manifest.entries[0].sha256


def test_accelerated_corrupt_cache_reacquires_and_heals(tmp_path: Path) -> None:
    wheel = _make_wheel(tmp_path, "fake-accel", "1.0.0")
    manifest = _accel_manifest(wheel)
    urlopen = _CountingUrlopen(wheel)
    cache = ArtifactCacheStore(tmp_path / "cache" / "artifacts")
    acquirer = _accel_acquirer(manifest, cache, urlopen)
    plan = _accel_plan("fake-accel")

    acquirer.acquire(plan, tmp_path / "work1")
    assert urlopen.calls == 1

    # Tamper with the cached bytes (index intact).
    cached = cache.cached_path_for_digest(manifest.entries[0].sha256)
    assert cached is not None
    cached.write_bytes(cached.read_bytes() + b"corrupted")

    second = acquirer.acquire(plan, tmp_path / "work2")
    assert urlopen.calls == 2  # reacquired — never activated unverified
    assert _sha256(second[0].wheel_path) == manifest.entries[0].sha256
    # The cache is healed with correct bytes.
    healed = cache.cached_path_for_digest(manifest.entries[0].sha256)
    assert healed is not None
    assert _sha256(healed) == manifest.entries[0].sha256


def test_accelerated_wrong_filename_in_cache_never_used(tmp_path: Path) -> None:
    wheel = _make_wheel(tmp_path, "fake-accel", "1.0.0")
    manifest = _accel_manifest(wheel)
    urlopen = _CountingUrlopen(wheel)
    cache = ArtifactCacheStore(tmp_path / "cache" / "artifacts")
    # Place correct bytes under a WRONG filename in the cache.
    sha = manifest.entries[0].sha256
    wrong_dir = cache.wheels_dir / sha
    wrong_dir.mkdir(parents=True)
    shutil.copy(wheel, wrong_dir / "some-other-1.0.0-py3-none-any.whl")

    acquirer = _accel_acquirer(manifest, cache, urlopen)
    plan = _accel_plan("fake-accel")
    acquired = acquirer.acquire(plan, tmp_path / "work")
    # Identity mismatch → downloaded from the manifest source instead.
    assert urlopen.calls == 1
    assert _sha256(acquired[0].wheel_path) == sha


# ═════════════════════════════════════════════════════════════════════════
# Part 4 — dependency wheelhouse cache reuse (C.2)
# ═════════════════════════════════════════════════════════════════════════


def _fake_pip_run(needed: dict[str, Path], remote_counter: list[int],
                  pip_calls: list[list[str]]):
    """Simulate ``pip download`` faithfully enough for cache tests:

    wheels in the ``--find-links`` seed dir are linked locally (0 remote);
    anything else is "downloaded" from the index (remote counter++).
    """

    def fake_run(argv, **__):
        dest = Path(argv[argv.index("--dest") + 1])
        seed = None
        if "--find-links" in argv:
            seed = Path(argv[argv.index("--find-links") + 1])
        pip_calls.append(list(argv))
        dest.mkdir(parents=True, exist_ok=True)
        for filename, wheel in sorted(needed.items()):
            if seed is not None and (seed / filename).is_file():
                shutil.copy(str(seed / filename), str(dest / filename))
            else:
                remote_counter[0] += 1
                shutil.copy(str(wheel), str(dest / filename))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return fake_run


def test_pip_cache_seeded_hit_zero_remote(tmp_path: Path, monkeypatch) -> None:
    product = _make_wheel(tmp_path, "product-piptest", "0.1.0")
    dep = _make_wheel(tmp_path, "leaf-lib", "1.0.0")
    needed = {dep.name: dep}
    remote = [0]
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "zealfie.dependencies.pip_acquirer.subprocess.run",
        _fake_pip_run(needed, remote, calls),
    )
    cache = ArtifactCacheStore(tmp_path / "cache" / "artifacts")
    acquirer = PipWheelhouseAcquirer()
    req = build_acquisition_request(product)

    # Transaction 1: no proven identity → plain pip (1 remote download).
    first = acquirer.acquire(
        req, staging_dir=tmp_path / "staging1", cache=cache,
        proven_requirements=(),
    )
    assert remote[0] == 1
    assert "--find-links" not in calls[0]
    assert len(first.acquired) == 1
    # Transaction 1 filled the cache.
    assert cache.resolve_dependency(
        "leaf-lib", "1.0.0", compatible_tags=default_compatible_tags()
    ) is not None

    # Transaction 2: proven identity + cache → seeded find-links, 0 remote.
    second = acquirer.acquire(
        req, staging_dir=tmp_path / "staging2", cache=cache,
        proven_requirements=(("leaf-lib", "1.0.0"),),
    )
    assert remote[0] == 1  # unchanged
    assert "--find-links" in calls[1]
    assert len(second.acquired) == 1
    assert _sha256(second.acquired[0].wheel_path) == _sha256(dep)
    # The seed dir is cleaned up after acquisition.
    seed = Path(calls[1][calls[1].index("--find-links") + 1])
    assert not seed.exists()


def test_pip_no_proven_or_unknown_identity_never_seeds(
    tmp_path: Path, monkeypatch,
) -> None:
    product = _make_wheel(tmp_path, "product-piptest", "0.1.0")
    dep = _make_wheel(tmp_path, "leaf-lib", "1.0.0")
    needed = {dep.name: dep}
    remote = [0]
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "zealfie.dependencies.pip_acquirer.subprocess.run",
        _fake_pip_run(needed, remote, calls),
    )
    cache = ArtifactCacheStore(tmp_path / "cache" / "artifacts")
    acquirer = PipWheelhouseAcquirer()
    req = build_acquisition_request(product)

    # Empty proven → byte-identical pre-cache behaviour.
    acquirer.acquire(req, staging_dir=tmp_path / "s1", cache=cache,
                     proven_requirements=())
    assert "--find-links" not in calls[0]

    # Unknown identity → nothing seeded, no find-links.
    acquirer.acquire(req, staging_dir=tmp_path / "s2", cache=cache,
                     proven_requirements=(("ghost-lib", "0.0.1"),))
    assert "--find-links" not in calls[1]
    assert remote[0] == 2


def test_pip_foreign_tag_identity_never_seeds(tmp_path: Path, monkeypatch) -> None:
    product = _make_wheel(tmp_path, "product-piptest", "0.1.0")
    dep = _make_wheel(tmp_path, "leaf-lib", "1.0.0")
    needed = {dep.name: dep}
    remote = [0]
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "zealfie.dependencies.pip_acquirer.subprocess.run",
        _fake_pip_run(needed, remote, calls),
    )
    cache = ArtifactCacheStore(tmp_path / "cache" / "artifacts")
    foreign = _make_wheel(
        tmp_path, "leaf-lib", "1.0.0", tags=("py3", "none", "win_amd64")
    )
    cache.put(foreign, kind="dependency")

    acquirer = PipWheelhouseAcquirer()
    req = build_acquisition_request(product)
    acquirer.acquire(
        req, staging_dir=tmp_path / "s1", cache=cache,
        proven_requirements=(("leaf-lib", "1.0.0"),),
    )
    # Incompatible tags → never seeded → plain pip download.
    assert "--find-links" not in calls[0]
    assert remote[0] == 1


# ═════════════════════════════════════════════════════════════════════════
# Part 5 — product KEEP cache reuse at the SERVICE layer (C.1, test G)
# ═════════════════════════════════════════════════════════════════════════


SHA_A1 = "a" * 40
SHA_A2 = "b" * 40
SHA_B = "c" * 40
SHA_C1 = "d" * 40
SHA_C2 = "e" * 40


def _test_catalog() -> ProductCatalog:
    def descriptor(pid: str, repo: str) -> ProductDescriptor:
        return ProductDescriptor(
            product_id=pid,
            display_name=pid.capitalize(),
            distribution_name=pid,
            launch_entry_points=(EntryPointContract("console_scripts", pid),),
            required_extras=(),
            remote_source=RemoteSource(owner="tinystork", repo=repo, ref="main"),
        )

    return ProductCatalog((
        descriptor("product-a", "ZeProductA"),
        descriptor("product-b", "ZeProductB"),
        descriptor("product-c", "ZeProductC"),
    ))


def _archive_bytes(repo: str) -> bytes:
    """A valid, safe zip archive whose marker names the source repo."""
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{repo}.marker", "x")
    return buf.getvalue()


class _CountingFetcher:
    def __init__(self, shas: dict[str, str]) -> None:
        self.calls_by_repo: dict[str, int] = {}
        self._shas = dict(shas)

    def __call__(self, owner: str, repo: str, commit_sha: str) -> bytes:
        self.calls_by_repo[repo] = self.calls_by_repo.get(repo, 0) + 1
        return _archive_bytes(repo)

    def total(self) -> int:
        return sum(self.calls_by_repo.values())


class _CountingResolver:
    def __init__(self, shas: dict[str, str]) -> None:
        self.calls = 0
        self.sha_by_repo = dict(shas)

    def __call__(self, owner: str, repo: str, ref: str) -> str:
        self.calls += 1
        return self.sha_by_repo[repo]


class _FakeRtWithLayout:
    """Layout-backed fake runtime (ABSENT status, no real slots)."""

    def __init__(self, layout: RuntimeLayout) -> None:
        self.layout = layout

    def status(self) -> RuntimeStatus:
        return RuntimeStatus(state=RuntimeState.ABSENT,
                             runtime_root=self.layout.root)


class _ServiceHarness:
    """Hermetic full-state service with counting fetch/build/pip fakes."""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.tmp_path = tmp_path
        self.layout = RuntimeLayout(tmp_path / "rt")
        self.runtime = _FakeRtWithLayout(self.layout)
        self.catalog = _test_catalog()
        self.selection = SelectionStore(tmp_path / "desired-products.toml")
        self.provenance = ProductProvenanceStore(self.layout)
        self.installed = InstalledLockStore(self.layout)
        self.policy = ProductPolicyStore(tmp_path / "product-policy.toml")

        self.wheels_by_repo: dict[str, Path] = {}
        self.fetcher = _CountingFetcher(
            {"ZeProductA": SHA_A1, "ZeProductB": SHA_B, "ZeProductC": SHA_C1}
        )
        self.resolver = _CountingResolver(
            {"ZeProductA": SHA_A1, "ZeProductB": SHA_B, "ZeProductC": SHA_C1}
        )
        self.build_calls: list[str] = []
        self.pip_remote = [0]
        self.pip_calls: list[list[str]] = []
        self.applied_slots: list[str] = []
        self._slot_counter = itertools.count(1)

        monkeypatch.setattr(
            "zealfie.app.service.build_wheel_from_staged", self._fake_build
        )
        monkeypatch.setattr(
            "zealfie.app.service.apply_deployment_plan", self._fake_apply
        )
        monkeypatch.setattr(
            "zealfie.dependencies.pip_acquirer.subprocess.run",
            self._fake_pip,
        )

        self.service = ZeAlfieService(
            catalog=self.catalog,
            runtime=self.runtime,
            selection_store=self.selection,
            provenance_store=self.provenance,
            installed_lock_store=self.installed,
            policy_store=self.policy,
            acquirer=PipWheelhouseAcquirer(),
        )
        self.cache: ArtifactCacheStore = self.service._artifact_cache
        assert self.cache is not None

    # -- fakes -------------------------------------------------------------

    def set_product_wheel(self, repo: str, wheel: Path, sha: str) -> None:
        self.wheels_by_repo[repo] = wheel
        self.resolver.sha_by_repo[repo] = sha

    def _fake_build(self, staged, *, output_dir=None):
        markers = list(staged.stage_dir.glob("*.marker"))
        assert len(markers) == 1, "archive marker missing"
        repo = markers[0].name[: -len(".marker")]
        self.build_calls.append(repo)
        wheel = self.wheels_by_repo[repo]
        output_dir.mkdir(parents=True, exist_ok=True)
        dest = output_dir / wheel.name
        shutil.copy(wheel, dest)
        return dest

    def _fake_apply(self, plan, *, registry=None, runtime=None,
                    progress_callback=None):
        previous = None
        try:
            status = load_active_state(self.layout.active_pointer,
                                       layout_root=self.layout.root)
            if status.state == RuntimeState.READY:
                previous = status.active_slot_id
        except Exception:
            previous = None
        slot = f"rt-{next(self._slot_counter):012x}"
        save_active_state(self.layout.active_pointer, slot, previous)
        self.applied_slots.append(slot)
        return DeploymentResult(success=True, active_slot_id=slot)

    def _fake_pip(self, argv, **__):
        dest = Path(argv[argv.index("--dest") + 1])
        seed = None
        if "--find-links" in argv:
            seed = Path(argv[argv.index("--find-links") + 1])
        self.pip_calls.append(list(argv))
        dest.mkdir(parents=True, exist_ok=True)
        for repo, wheel in sorted(self.wheels_by_repo.items()):
            # Only dependency wheels live under deps/ (products are never
            # "downloaded" by pip in this harness).
            if repo != "deps":
                continue
            if seed is not None and (seed / wheel.name).is_file():
                shutil.copy(str(seed / wheel.name), str(dest / wheel.name))
            else:
                self.pip_remote[0] += 1
                shutil.copy(str(wheel), str(dest / wheel.name))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    # -- transactions -------------------------------------------------------

    def install(self, product_id: str) -> DeploymentResult:
        return self.service.install_product(
            product_id,
            resolver=self.resolver,
            fetcher=self.fetcher,
            work_root=self.tmp_path / f"work-{len(self.applied_slots)}",
        )


def _dep_wheel(tmp_path: Path) -> Path:
    return _make_wheel(tmp_path, "leaf-lib", "1.0.0")


def _product_wheel(tmp_path: Path, pid: str, version: str) -> Path:
    return _make_wheel(
        tmp_path,
        pid,
        version,
        requires_dist=("leaf-lib",),
        entry_points=(("console_scripts", pid, f"{pid.replace('-', '_')}:main"),),
    )


def test_g_keep_cache_hit_zero_fetch_zero_build(
    tmp_path: Path, monkeypatch,
) -> None:
    """Transaction scenario G:

    t1: install A (fetch/build A; deps 1 remote);
    t2: install B (KEEP A is a cache HIT: 0 fetch/0 build; B acquired);
    t3: update A to 0.2.0 (KEEP B is a cache HIT: 0 fetch/0 build/0
        resolver; A is the only newly acquired artifact; deps 0 remote).
    """
    h = _ServiceHarness(tmp_path, monkeypatch)
    wheel_a1 = _product_wheel(tmp_path, "product-a", "0.1.0")
    wheel_b = _product_wheel(tmp_path, "product-b", "0.1.0")
    dep = _dep_wheel(tmp_path)
    h.set_product_wheel("ZeProductA", wheel_a1, SHA_A1)
    h.set_product_wheel("ZeProductB", wheel_b, SHA_B)
    h.wheels_by_repo["deps"] = dep

    # ---- t1: install A ----------------------------------------------------
    r1 = h.install("product-a")
    assert r1.success, r1.reason
    assert h.fetcher.calls_by_repo.get("ZeProductA") == 1
    assert h.build_calls.count("ZeProductA") == 1
    assert h.pip_remote[0] == 1
    prov_a1 = h.service.active_provenance()["product-a"]
    assert prov_a1.wheel_sha256 == _sha256(wheel_a1)

    # ---- t2: install B; KEEP A must hit the cache -------------------------
    fetches_before = h.fetcher.total()
    builds_before = len(h.build_calls)
    resolver_before = h.resolver.calls
    remote_before = h.pip_remote[0]

    r2 = h.install("product-b")
    assert r2.success, r2.reason

    # A: 0 fetch + 0 rebuild + 0 re-resolution (cache hit).
    assert h.fetcher.total() - fetches_before == 1  # only B
    assert len(h.build_calls) - builds_before == 1  # only B
    assert h.resolver.calls - resolver_before == 1  # only B
    assert h.build_calls[-1] == "ZeProductB"
    # deps: leaf-lib proven by the t1 lock and cached → 0 remote.
    assert h.pip_remote[0] - remote_before == 0

    prov_b = h.service.active_provenance()["product-b"]
    assert prov_b.wheel_sha256 == _sha256(wheel_b)
    assert h.service.active_provenance()["product-a"] == prov_a1

    # ---- t3: update A; KEEP B must hit the cache --------------------------
    wheel_a2 = _product_wheel(tmp_path, "product-a", "0.2.0")
    h.set_product_wheel("ZeProductA", wheel_a2, SHA_A2)
    fetches_before = h.fetcher.total()
    builds_before = len(h.build_calls)
    resolver_before = h.resolver.calls
    remote_before = h.pip_remote[0]
    b_fetches_before = h.fetcher.calls_by_repo.get("ZeProductB", 0)

    def guarded_fetch(owner, repo, commit_sha):
        if repo == "ZeProductB":
            raise AssertionError("KEEP product B must never be re-fetched")
        return h.fetcher(owner, repo, commit_sha)

    r3 = h.service.install_product(
        "product-a",
        resolver=h.resolver,
        fetcher=guarded_fetch,
        work_root=tmp_path / "work-t3",
    )
    assert r3.success, r3.reason

    # A is the ONLY newly acquired/built/resolved artifact.
    assert h.fetcher.total() - fetches_before == 1
    assert len(h.build_calls) - builds_before == 1
    assert h.build_calls[-1] == "ZeProductA"
    assert h.resolver.calls - resolver_before == 1
    # B: 0 download, 0 rebuild.
    assert h.fetcher.calls_by_repo.get("ZeProductB", 0) == b_fetches_before
    # Unchanged deps: 0 remote (seeded from cache).
    assert h.pip_remote[0] - remote_before == 0

    # Provenance immutability: B's record is byte-identical to t2.
    final = h.service.active_provenance()
    assert final["product-b"] == prov_b
    assert final["product-a"].wheel_sha256 == _sha256(wheel_a2)
    assert final["product-a"].commit_sha == SHA_A2

    # The installed lock still proves the dependency identity.
    lock = h.service.active_installed_lock()
    assert lock is not None
    assert "leaf-lib" in lock.dependencies


def test_g_keep_cache_file_missing_reacquires(
    tmp_path: Path, monkeypatch,
) -> None:
    h = _ServiceHarness(tmp_path, monkeypatch)
    wheel_a = _product_wheel(tmp_path, "product-a", "0.1.0")
    wheel_b = _product_wheel(tmp_path, "product-b", "0.1.0")
    wheel_c = _product_wheel(tmp_path, "product-c", "0.1.0")
    dep = _dep_wheel(tmp_path)
    h.set_product_wheel("ZeProductA", wheel_a, SHA_A1)
    h.set_product_wheel("ZeProductB", wheel_b, SHA_B)
    h.set_product_wheel("ZeProductC", wheel_c, SHA_C1)
    h.wheels_by_repo["deps"] = dep

    assert h.install("product-a").success
    assert h.install("product-b").success
    prov_b = h.service.active_provenance()["product-b"]
    b_sha = prov_b.wheel_sha256

    # Delete B's cached file: a cache FILE miss is a normal re-acquisition.
    cached = h.cache.cached_path_for_digest(b_sha)
    assert cached is not None
    cached.unlink()

    b_fetches_before = h.fetcher.calls_by_repo.get("ZeProductB", 0)
    builds_before = len(h.build_calls)
    result = h.install("product-c")
    assert result.success, result.reason

    # B was honestly re-acquired (fetch counter moved) and rebuilt.
    assert h.fetcher.calls_by_repo.get("ZeProductB", 0) == b_fetches_before + 1
    assert len(h.build_calls) - builds_before == 2  # B + C
    # B's provenance is unchanged (same immutable identity).
    assert h.service.active_provenance()["product-b"] == prov_b
    # The cache was refilled with verified bytes.
    refilled = h.cache.cached_path_for_digest(b_sha)
    assert refilled is not None
    assert _sha256(refilled) == b_sha


def test_g_keep_cache_bad_sha_rejected_and_reacquired(
    tmp_path: Path, monkeypatch,
) -> None:
    h = _ServiceHarness(tmp_path, monkeypatch)
    wheel_a = _product_wheel(tmp_path, "product-a", "0.1.0")
    wheel_b = _product_wheel(tmp_path, "product-b", "0.1.0")
    wheel_c = _product_wheel(tmp_path, "product-c", "0.1.0")
    dep = _dep_wheel(tmp_path)
    h.set_product_wheel("ZeProductA", wheel_a, SHA_A1)
    h.set_product_wheel("ZeProductB", wheel_b, SHA_B)
    h.set_product_wheel("ZeProductC", wheel_c, SHA_C1)
    h.wheels_by_repo["deps"] = dep

    assert h.install("product-a").success
    assert h.install("product-b").success
    prov_b = h.service.active_provenance()["product-b"]
    b_sha = prov_b.wheel_sha256

    # Tamper with B's cached bytes (index intact): digest check must reject.
    cached = h.cache.cached_path_for_digest(b_sha)
    assert cached is not None
    cached.write_bytes(cached.read_bytes() + b"corrupt")

    b_fetches_before = h.fetcher.calls_by_repo.get("ZeProductB", 0)
    result = h.install("product-c")
    assert result.success, result.reason

    # Never activated unverified: B was re-acquired and rebuilt.
    assert h.fetcher.calls_by_repo.get("ZeProductB", 0) == b_fetches_before + 1
    assert h.service.active_provenance()["product-b"] == prov_b
    # And the cache now holds correct bytes again.
    healed = h.cache.cached_path_for_digest(b_sha)
    assert healed is not None
    assert _sha256(healed) == b_sha


# ═════════════════════════════════════════════════════════════════════════
# Part 6 — CLI ``runtime gc-cache``
# ═════════════════════════════════════════════════════════════════════════


def test_cli_gc_cache_deletes_unreferenced(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "rt"
    monkeypatch.setenv("ZEALFIE_RUNTIME_ROOT", str(root))
    layout = RuntimeLayout(root)
    store = ArtifactCacheStore(layout.artifact_cache_dir)
    orphan = _make_wheel(tmp_path, "orphan-lib", "9.9.9")
    sha = _sha256(orphan)
    store.put(orphan, kind="dependency")

    stdout = StringIO()
    code = cli.run(["runtime", "gc-cache"], stdout=stdout)
    assert code == 0
    assert "deleted 1" in stdout.getvalue()
    assert store.cached_path_for_digest(sha) is None


def test_cli_gc_cache_blocked_on_corrupt_state(
    tmp_path: Path, monkeypatch,
) -> None:
    root = tmp_path / "rt"
    monkeypatch.setenv("ZEALFIE_RUNTIME_ROOT", str(root))
    layout = RuntimeLayout(root)
    layout.state_dir.mkdir(parents=True, exist_ok=True)
    store = ArtifactCacheStore(layout.artifact_cache_dir)
    orphan = _make_wheel(tmp_path, "orphan-lib", "9.9.9")
    sha = _sha256(orphan)
    store.put(orphan, kind="dependency")
    (layout.state_dir / "product-provenance.json").write_text(
        "{corrupt", encoding="utf-8"
    )

    stdout = StringIO()
    code = cli.run(["runtime", "gc-cache"], stdout=stdout)
    assert code == 3
    assert "blocked" in stdout.getvalue()
    assert store.cached_path_for_digest(sha) is not None
