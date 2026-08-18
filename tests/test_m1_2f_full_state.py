"""Focused tests for M1-2F-P1 — full-state multi-product orchestration.

Covers the Phase 1 acceptance behaviour of ``install_product`` /
``update_product``: reconstructing the complete desired product set
(KEEP active products + target), materializing KEEP products from active
provenance at their exact commit SHA (never re-resolving the mutable ref),
acquiring ONE combined dependency wheelhouse, resolving ONE combined
dependency lock, and applying ONE transaction.

All tests are FAST — no real GitHub/network, no venv, no pip, no
subprocess.  Real wheel *building* is performed only for the resolver/
provenance-digest tests (pure ``zipfile`` wheel synthesis, no pip).
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
    ProductProvenance,
    ProductProvenanceStore,
    SelectionStore,
    ZeAlfieService,
)
from zealfie.app.service import (
    _provenance_entries_for,
)
from zealfie.components.model import EntryPointContract
from zealfie.dependencies.models import DependencyResolutionError
from zealfie.releases.model import VerifiedArtifact
from zealfie.runtime.layout import RuntimeLayout
from zealfie.runtime.model import DeploymentResult, RuntimeState, RuntimeStatus
from zealfie.runtime.state import save_active_state
from zealfie.sources import RemoteSource, ResolvedSource

# 40-char hex commit SHAs (distinct, valid).
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_B_NEW = "d" * 40
VALID_SHA = "d4a0f1e2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _prov(product_id: str, *, commit_sha: str = VALID_SHA,
          version: str = "1.0.0", source_owner: str = "tinystork",
          source_repo: str | None = None, requested_ref: str = "main") -> ProductProvenance:
    if source_repo is None:
        source_repo = f"Ze{product_id.capitalize()}"
    return ProductProvenance(
        product_id=product_id,
        version=version,
        source_owner=source_owner,
        source_repo=source_repo,
        requested_ref=requested_ref,
        commit_sha=commit_sha,
        wheel_sha256="f" * 64,
    )


def _catalog(*product_ids: str) -> ProductCatalog:
    """Catalog with one descriptor per id (distribution name == id)."""
    descs = []
    for pid in product_ids:
        descs.append(
            ProductDescriptor(
                product_id=pid,
                display_name=pid.capitalize(),
                distribution_name=pid,
                launch_entry_points=(EntryPointContract("console_scripts", pid),),
                required_extras=(),
                remote_source=RemoteSource(
                    owner="tinystork",
                    repo=f"Ze{pid.capitalize()}",
                    ref="main",
                ),
            )
        )
    return ProductCatalog(tuple(descs))


class _FakeAbsentRt:
    """Fake runtime without a layout → provenance only via injection."""

    def status(self) -> RuntimeStatus:
        return RuntimeStatus(state=RuntimeState.ABSENT, runtime_root=Path("/fake"))


class _RecordingAcquirer:
    """Fake acquirer that records every acquire call (no pip)."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, Path | None]] = []

    def acquire(self, request, *, staging_dir=None, timeout_seconds=300):
        self.calls.append((request, staging_dir))
        from zealfie.dependencies.acquisition import DependencyAcquisitionResult
        return DependencyAcquisitionResult(
            staging_wheelhouse=staging_dir,
            acquired=(),
        )


def _service_with_active_provenance(
    tmp_path: Path,
    catalog: ProductCatalog,
    entries: list[ProductProvenance],
    *,
    slot_id: str = "rt-active",
):
    """Service with a real provenance store whose active slot has *entries*."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    store = ProductProvenanceStore(layout)
    if entries:
        store.record(slot_id, entries)
        save_active_state(layout.active_pointer, slot_id, None)
    selection = SelectionStore(path=tmp_path / "desired-products.toml")
    service = ZeAlfieService(
        catalog=catalog,
        runtime=_FakeAbsentRt(),
        selection_store=selection,
        provenance_store=store,
        acquirer=_RecordingAcquirer(),
    )
    return service, store, selection, layout


def _fake_ppa(
    product_id: str,
    *,
    commit_sha: str = VALID_SHA,
    requested_ref: str = "main",
    version: str = "1.0.0",
    dist_name: str | None = None,
    wheel_path: Path | None = None,
) -> PreparedProductArtifact:
    """Build a synthetic PPA (dummy wheel path unless provided)."""
    if dist_name is None:
        dist_name = product_id
    if wheel_path is None:
        wheel_path = Path("/fake") / f"{product_id}.whl"
    remote = RemoteSource(owner="tinystork", repo=f"Ze{product_id.capitalize()}", ref=requested_ref)
    resolved = ResolvedSource(source=remote, commit_sha=commit_sha)
    return PreparedProductArtifact(
        product_id=product_id,
        component_id=product_id,
        resolved_source=resolved,
        wheel_path=wheel_path,
        verified_artifact=VerifiedArtifact(
            component_id=product_id,
            version=version,
            path=wheel_path,
            size=100,
            sha256="e" * 64,
            distribution_name=dist_name,
            wheel_version=version,
        ),
    )


def _make_wheel(
    output: Path,
    name: str,
    version: str,
    *,
    requires_dist: tuple[str, ...] = (),
    entry_points: tuple[tuple[str, str, str], ...] = (),
) -> Path:
    """Synthesize a minimal, pip-parseable wheel (pure zipfile, no pip)."""
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
    for req in requires_dist:
        metadata += f"Requires-Dist: {req}\n"

    record_lines = [
        f"{dist_info}/WHEEL,,",
        f"{dist_info}/METADATA,,",
    ]
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
        record_lines.append(f"{dist_info}/entry_points.txt,,")

    record_lines.append(f"{dist_info}/RECORD,,")
    record = "\n".join(record_lines) + "\n"
    members.append((f"{dist_info}/RECORD", record))

    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, content in members:
            zf.writestr(arcname, content)
    return wheel_path


def _real_ppa(product_id: str, wheel_path: Path, version: str) -> PreparedProductArtifact:
    """Build a PPA whose VerifiedArtifact matches a real wheel file."""
    dist_name = product_id
    remote = RemoteSource(owner="tinystork", repo=f"Ze{product_id.capitalize()}", ref="main")
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
            size=wheel_path.stat().st_size,
            sha256=_sha256(wheel_path),
            distribution_name=dist_name,
            wheel_version=version,
        ),
    )


# ---------------------------------------------------------------------------
# A. Install first product (mono-product still works)
# ---------------------------------------------------------------------------


def test_a_install_first_product_mono(tmp_path, monkeypatch) -> None:
    catalog = _catalog("alpha")
    service, _store, _selection, _layout = _service_with_active_provenance(
        tmp_path, catalog, []
    )

    prepared_calls: list[list[PreparedProductArtifact]] = []
    monkeypatch.setattr(
        service, "prepare_product_artifact",
        lambda product_id, *, resolver, fetcher, work_root: _fake_ppa(product_id),
    )
    monkeypatch.setattr(
        service, "install_prepared_product_deployment",
        lambda prepared_artifacts, *, dependency_wheelhouse=None,
               probe_distribution=None, progress_callback=None, accelerated_acquirer=None: (
            prepared_calls.append(list(prepared_artifacts))
            or DeploymentResult(success=True, active_slot_id="rt-1")
        ),
    )

    wheelhouse = tmp_path / "wh"
    wheelhouse.mkdir()

    result = service.install_product(
        "alpha",
        resolver=lambda o, r, ref: VALID_SHA,
        fetcher=lambda o, r, sha: b"",
        work_root=tmp_path / "work",
        dependency_wheelhouse=wheelhouse,
    )

    assert result.success is True
    assert len(prepared_calls) == 1
    assert [pa.product_id for pa in prepared_calls[0]] == ["alpha"]


# ---------------------------------------------------------------------------
# B. Install second product; first remains exact active revision
# ---------------------------------------------------------------------------


def test_b_install_second_product_keeps_first_exact_sha(tmp_path, monkeypatch) -> None:
    catalog = _catalog("alpha", "beta")
    service, _store, _selection, _layout = _service_with_active_provenance(
        tmp_path, catalog, [_prov("alpha", commit_sha=SHA_A)]
    )

    at_commit_calls: list[dict] = []
    prepared_calls: list[list[PreparedProductArtifact]] = []

    def _fake_prepare(product_id, *, resolver, fetcher, work_root, progress_callback=None):
        return _fake_ppa(product_id)

    def _fake_at_commit(product_id, *, commit_sha, source_owner, source_repo,
                        requested_ref, fetcher, work_root, progress_callback=None):
        at_commit_calls.append({
            "product_id": product_id,
            "commit_sha": commit_sha,
            "requested_ref": requested_ref,
        })
        return _fake_ppa(product_id, commit_sha=commit_sha, requested_ref=requested_ref)

    monkeypatch.setattr(service, "prepare_product_artifact", _fake_prepare)
    monkeypatch.setattr(service, "prepare_product_artifact_at_commit", _fake_at_commit)
    monkeypatch.setattr(
        service, "install_prepared_product_deployment",
        lambda prepared_artifacts, *, dependency_wheelhouse=None,
               probe_distribution=None, progress_callback=None, accelerated_acquirer=None: (
            prepared_calls.append(list(prepared_artifacts))
            or DeploymentResult(success=True, active_slot_id="rt-2")
        ),
    )

    wheelhouse = tmp_path / "wh"
    wheelhouse.mkdir()

    result = service.install_product(
        "beta",
        resolver=lambda o, r, ref: VALID_SHA,
        fetcher=lambda o, r, sha: b"",
        work_root=tmp_path / "work",
        dependency_wheelhouse=wheelhouse,
    )

    assert result.success is True
    # KEEP (alpha) sorted first, then target (beta).
    assert [pa.product_id for pa in prepared_calls[0]] == ["alpha", "beta"]
    # KEEP alpha materialized from its exact active commit SHA.
    assert len(at_commit_calls) == 1
    assert at_commit_calls[0]["product_id"] == "alpha"
    assert at_commit_calls[0]["commit_sha"] == SHA_A


# ---------------------------------------------------------------------------
# C. Install third fake product; generic full-state (not hard-coded for two)
# ---------------------------------------------------------------------------


def test_c_install_third_product_generic_full_state(tmp_path, monkeypatch) -> None:
    catalog = _catalog("alpha", "beta", "gamma")
    service, _store, _selection, _layout = _service_with_active_provenance(
        tmp_path,
        catalog,
        [_prov("alpha", commit_sha=SHA_A), _prov("beta", commit_sha=SHA_B)],
    )

    at_commit_calls: dict[str, str] = {}
    prepared_calls: list[list[PreparedProductArtifact]] = []

    monkeypatch.setattr(
        service, "prepare_product_artifact",
        lambda product_id, *, resolver, fetcher, work_root, progress_callback=None: _fake_ppa(product_id),
    )

    def _fake_at_commit(product_id, *, commit_sha, source_owner, source_repo,
                        requested_ref, fetcher, work_root, progress_callback=None):
        at_commit_calls[product_id] = commit_sha
        return _fake_ppa(product_id, commit_sha=commit_sha, requested_ref=requested_ref)

    monkeypatch.setattr(service, "prepare_product_artifact_at_commit", _fake_at_commit)
    monkeypatch.setattr(
        service, "install_prepared_product_deployment",
        lambda prepared_artifacts, *, dependency_wheelhouse=None,
               probe_distribution=None, progress_callback=None, accelerated_acquirer=None: (
            prepared_calls.append(list(prepared_artifacts))
            or DeploymentResult(success=True, active_slot_id="rt-3")
        ),
    )

    wheelhouse = tmp_path / "wh"
    wheelhouse.mkdir()

    result = service.install_product(
        "gamma",
        resolver=lambda o, r, ref: VALID_SHA,
        fetcher=lambda o, r, sha: b"",
        work_root=tmp_path / "work",
        dependency_wheelhouse=wheelhouse,
    )

    assert result.success is True
    # Generic: all three products in deterministic order (KEEP sorted, target last).
    assert [pa.product_id for pa in prepared_calls[0]] == ["alpha", "beta", "gamma"]
    assert at_commit_calls == {"alpha": SHA_A, "beta": SHA_B}


# ---------------------------------------------------------------------------
# Combined wheelhouse: ONE staging dir reused for every product
# ---------------------------------------------------------------------------


def test_combined_wheelhouse_single_staging(tmp_path, monkeypatch, witness_wheel) -> None:
    catalog = _catalog("alpha", "beta")
    service, _store, _selection, _layout = _service_with_active_provenance(
        tmp_path, catalog, [_prov("alpha", commit_sha=SHA_A)]
    )

    monkeypatch.setattr(
        service, "prepare_product_artifact",
        lambda product_id, *, resolver, fetcher, work_root, progress_callback=None: _fake_ppa(product_id, wheel_path=witness_wheel),
    )
    monkeypatch.setattr(
        service, "prepare_product_artifact_at_commit",
        lambda product_id, *, commit_sha, source_owner, source_repo, requested_ref,
               fetcher, work_root, progress_callback=None: _fake_ppa(
                   product_id, commit_sha=commit_sha, requested_ref=requested_ref,
                   wheel_path=witness_wheel),
    )
    monkeypatch.setattr(
        service, "install_prepared_product_deployment",
        lambda prepared_artifacts, *, dependency_wheelhouse=None,
               probe_distribution=None, progress_callback=None, accelerated_acquirer=None: DeploymentResult(
                   success=True, active_slot_id="rt-4",
               ),
    )

    result = service.install_product(
        "beta",
        resolver=lambda o, r, ref: VALID_SHA,
        fetcher=lambda o, r, sha: b"",
        work_root=tmp_path / "work",
        dependency_wheelhouse=None,  # auto-acquire
    )

    assert result.success is True
    acquirer = service._acquirer
    assert isinstance(acquirer, _RecordingAcquirer)
    # Two products → two acquire calls into the SAME staging dir.
    assert len(acquirer.calls) == 2
    staging_dirs = {str(call[1]) for call in acquirer.calls}
    assert len(staging_dirs) == 1, "expected ONE shared staging wheelhouse"


# ---------------------------------------------------------------------------
# D. Combined dependency lock includes all primary artifacts
# ---------------------------------------------------------------------------


def test_d_combined_lock_includes_all_primaries(tmp_path) -> None:
    catalog = _catalog("alpha", "beta", "gamma")
    service = ZeAlfieService(catalog=catalog, runtime=_FakeAbsentRt())

    wh = tmp_path / "wheelhouse"
    wh.mkdir()

    wheels = {
        pid: _make_wheel(wh, pid, "1.0.0")
        for pid in ("alpha", "beta", "gamma")
    }
    ppas = [_real_ppa(pid, wheels[pid], "1.0.0") for pid in ("alpha", "beta", "gamma")]

    plan = service.plan_prepared_product_deployment(
        ppas, dependency_wheelhouse=wh,
    )

    assert plan.dependency_lock is not None
    lock = plan.dependency_lock
    assert lock.primary_names == frozenset({"alpha", "beta", "gamma"})
    assert len(lock.locked) == 3
    for pid in ("alpha", "beta", "gamma"):
        assert pid in lock.locked


# ---------------------------------------------------------------------------
# E. Dependency conflict blocks before activation; active unchanged
# ---------------------------------------------------------------------------


def test_e_conflict_blocks_before_activation_active_unchanged(tmp_path, monkeypatch) -> None:
    catalog = _catalog("alpha", "beta")
    service, store, _selection, _layout = _service_with_active_provenance(
        tmp_path, catalog, [_prov("alpha", commit_sha=SHA_A)]
    )

    wh = tmp_path / "wheelhouse"
    wh.mkdir()

    # alpha requires shareddep>=2.0 ; beta requires shareddep<2.0.
    alpha_wheel = _make_wheel(wh, "alpha", "1.0.0", requires_dist=("shareddep>=2.0",))
    beta_wheel = _make_wheel(wh, "beta", "1.0.0", requires_dist=("shareddep<2.0",))
    _make_wheel(wh, "shareddep", "1.5.0")
    _make_wheel(wh, "shareddep", "2.5.0")

    alpha_ppa = _real_ppa("alpha", alpha_wheel, "1.0.0")
    beta_ppa = _real_ppa("beta", beta_wheel, "1.0.0")

    monkeypatch.setattr(
        service, "prepare_product_artifact_at_commit",
        lambda product_id, *, commit_sha, source_owner, source_repo, requested_ref,
               fetcher, work_root, progress_callback=None: alpha_ppa,
    )
    monkeypatch.setattr(
        service, "prepare_product_artifact",
        lambda product_id, *, resolver, fetcher, work_root, progress_callback=None: beta_ppa,
    )

    # The real install_prepared_product_deployment runs planning, which must
    # raise before apply.  Ensure apply is never reached.
    import zealfie.app.service as svc_mod

    apply_calls: list = []

    def _explosive_apply(*args, **kwargs):
        apply_calls.append((args, kwargs))
        raise AssertionError("apply_deployment_plan must not be called on conflict")

    monkeypatch.setattr(svc_mod, "apply_deployment_plan", _explosive_apply)

    provenance_before = store.path.read_bytes()

    with pytest.raises(OfflineReleaseError):
        service.install_product(
            "beta",
            resolver=lambda o, r, ref: VALID_SHA,
            fetcher=lambda o, r, sha: b"",
            work_root=tmp_path / "work",
            dependency_wheelhouse=wh,
        )

    assert apply_calls == []
    assert store.path.read_bytes() == provenance_before


# ---------------------------------------------------------------------------
# F. KEEP does not follow a mutable ref (resolver never called for KEEP)
# ---------------------------------------------------------------------------


def test_f_keep_does_not_follow_mutable_ref(tmp_path, monkeypatch) -> None:
    catalog = _catalog("alpha", "beta")
    service, _store, _selection, _layout = _service_with_active_provenance(
        tmp_path, catalog, [_prov("alpha", commit_sha=SHA_A)]
    )

    resolver_calls: list[tuple[str, str, str]] = []
    prepared_via_target: list[str] = []
    prepared_via_keep: list[str] = []

    def _recording_resolver(owner, repo, ref):
        resolver_calls.append((owner, repo, ref))
        return SHA_B

    def _fake_prepare(product_id, *, resolver, fetcher, work_root, progress_callback=None):
        prepared_via_target.append(product_id)
        # The real target path resolves the source ref — mirror that here.
        resolver("tinystork", f"Ze{product_id.capitalize()}", "main")
        return _fake_ppa(product_id)

    def _fake_at_commit(product_id, *, commit_sha, source_owner, source_repo,
                        requested_ref, fetcher, work_root, progress_callback=None):
        prepared_via_keep.append(product_id)
        return _fake_ppa(product_id, commit_sha=commit_sha, requested_ref=requested_ref)

    monkeypatch.setattr(service, "prepare_product_artifact", _fake_prepare)
    monkeypatch.setattr(service, "prepare_product_artifact_at_commit", _fake_at_commit)
    monkeypatch.setattr(
        service, "install_prepared_product_deployment",
        lambda prepared_artifacts, *, dependency_wheelhouse=None,
               probe_distribution=None, progress_callback=None, accelerated_acquirer=None: DeploymentResult(
                   success=True, active_slot_id="rt-5",
               ),
    )

    wheelhouse = tmp_path / "wh"
    wheelhouse.mkdir()

    service.install_product(
        "beta",
        resolver=_recording_resolver,
        fetcher=lambda o, r, sha: b"",
        work_root=tmp_path / "work",
        dependency_wheelhouse=wheelhouse,
    )

    # Only the target (beta) is ever resolved — never the KEEP product (alpha).
    assert prepared_via_target == ["beta"]
    assert prepared_via_keep == ["alpha"]
    assert resolver_calls == [("tinystork", "ZeBeta", "main")]
    for owner, repo, ref in resolver_calls:
        assert repo != "ZeAlpha", "KEEP product must never be re-resolved"


# ---------------------------------------------------------------------------
# G. update target only; KEEP stays exact active SHA
# ---------------------------------------------------------------------------


def test_g_update_target_only(tmp_path, monkeypatch) -> None:
    catalog = _catalog("alpha", "beta")
    service, _store, _selection, _layout = _service_with_active_provenance(
        tmp_path,
        catalog,
        [_prov("alpha", commit_sha=SHA_A), _prov("beta", commit_sha=SHA_B)],
    )

    resolver_calls: list[tuple[str, str, str]] = []
    at_commit_calls: dict[str, str] = {}
    prepared_calls: list[list[PreparedProductArtifact]] = []

    def _recording_resolver(owner, repo, ref):
        resolver_calls.append((owner, repo, ref))
        return SHA_B_NEW  # beta has an update available

    monkeypatch.setattr(
        service, "prepare_product_artifact",
        lambda product_id, *, resolver, fetcher, work_root, progress_callback=None: _fake_ppa(product_id),
    )

    def _fake_at_commit(product_id, *, commit_sha, source_owner, source_repo,
                        requested_ref, fetcher, work_root, progress_callback=None):
        at_commit_calls[product_id] = commit_sha
        return _fake_ppa(product_id, commit_sha=commit_sha, requested_ref=requested_ref)

    monkeypatch.setattr(service, "prepare_product_artifact_at_commit", _fake_at_commit)
    monkeypatch.setattr(
        service, "install_prepared_product_deployment",
        lambda prepared_artifacts, *, dependency_wheelhouse=None,
               probe_distribution=None, progress_callback=None, accelerated_acquirer=None: (
            prepared_calls.append(list(prepared_artifacts))
            or DeploymentResult(success=True, active_slot_id="rt-6")
        ),
    )

    wheelhouse = tmp_path / "wh"
    wheelhouse.mkdir()

    result = service.update_product(
        "beta",
        resolver=_recording_resolver,
        fetcher=lambda o, r, sha: b"",
        work_root=tmp_path / "work",
        dependency_wheelhouse=wheelhouse,
    )

    assert result.success is True
    assert [pa.product_id for pa in prepared_calls[0]] == ["alpha", "beta"]
    # alpha is KEEP at its exact SHA — never updated, never re-resolved.
    assert at_commit_calls == {"alpha": SHA_A}
    # resolver used only for beta's update preflight.
    assert resolver_calls == [("tinystork", "ZeBeta", "main")]


# ---------------------------------------------------------------------------
# O. Rollback full-state: previous slot provenance becomes authoritative
# ---------------------------------------------------------------------------


def test_o_rollback_full_state(tmp_path) -> None:
    catalog = _catalog("alpha", "beta")

    layout = RuntimeLayout(root=tmp_path / "rt")
    store = ProductProvenanceStore(layout)
    store.record("rt-prev", [_prov("alpha", commit_sha=SHA_A)])
    store.record("rt-curr", [_prov("alpha", commit_sha=SHA_A), _prov("beta", commit_sha=SHA_B)])
    save_active_state(layout.active_pointer, "rt-curr", "rt-prev")

    class _FakeRollbackRt:
        def __init__(self, layout):
            self._layout = layout

        def status(self):
            from zealfie.runtime.state import load_active_state
            return load_active_state(self._layout.active_pointer, layout_root=self._layout.root)

        def rollback(self):
            from zealfie.runtime.state import load_active_state
            current = load_active_state(self._layout.active_pointer, layout_root=self._layout.root)
            save_active_state(self._layout.active_pointer, current.previous_slot_id, current.active_slot_id)
            return RuntimeStatus(
                state=RuntimeState.READY,
                runtime_root=self._layout.root,
                active_slot_id=current.previous_slot_id,
                previous_slot_id=current.active_slot_id,
            )

    service = ZeAlfieService(
        catalog=catalog,
        runtime=_FakeRollbackRt(layout),
        selection_store=SelectionStore(path=tmp_path / "sel.toml"),
        provenance_store=store,
    )

    # Active is the full-state slot (alpha + beta).
    assert set(service.active_provenance()) == {"alpha", "beta"}

    status = service.rollback_runtime()

    assert status.state == RuntimeState.READY
    # After rollback, active provenance is the previous (mono) slot.
    assert set(service.active_provenance()) == {"alpha"}


# ---------------------------------------------------------------------------
# P. Failed candidate leaves active untouched
# ---------------------------------------------------------------------------


def test_p_failed_candidate_leaves_active_untouched(tmp_path, monkeypatch) -> None:
    catalog = _catalog("alpha", "beta")
    service, store, selection, _layout = _service_with_active_provenance(
        tmp_path, catalog, [_prov("alpha", commit_sha=SHA_A)]
    )

    monkeypatch.setattr(
        service, "prepare_product_artifact",
        lambda product_id, *, resolver, fetcher, work_root, progress_callback=None: _fake_ppa(product_id),
    )
    monkeypatch.setattr(
        service, "prepare_product_artifact_at_commit",
        lambda product_id, *, commit_sha, source_owner, source_repo, requested_ref,
               fetcher, work_root, progress_callback=None: _fake_ppa(
                   product_id, commit_sha=commit_sha, requested_ref=requested_ref),
    )
    # apply returns failure (candidate failed) — no selection/provenance writes.
    monkeypatch.setattr(
        service, "install_prepared_product_deployment",
        lambda prepared_artifacts, *, dependency_wheelhouse=None,
               probe_distribution=None, progress_callback=None, accelerated_acquirer=None: DeploymentResult(
                   success=False, reason="simulated apply failure",
               ),
    )

    provenance_before = store.path.read_bytes()
    selection_before = selection.path.read_bytes() if selection.path.exists() else None

    wheelhouse = tmp_path / "wh"
    wheelhouse.mkdir()

    result = service.install_product(
        "beta",
        resolver=lambda o, r, ref: VALID_SHA,
        fetcher=lambda o, r, sha: b"",
        work_root=tmp_path / "work",
        dependency_wheelhouse=wheelhouse,
    )

    assert result.success is False
    # Active provenance still only alpha (never mutated).
    assert set(service.active_provenance()) == {"alpha"}
    assert store.path.read_bytes() == provenance_before
    if selection_before is None:
        assert not selection.path.exists()
    else:
        assert selection.path.read_bytes() == selection_before


# ---------------------------------------------------------------------------
# R/S. Exact source SHA provenance + honest artifact digest
# ---------------------------------------------------------------------------


def _zip_fixture_source(fixture_dir: Path) -> bytes:
    """Zip a fixture source dir (excluding build/) for use as a fetcher payload."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(fixture_dir.rglob("*")):
            if file_path.is_file():
                rel = file_path.relative_to(fixture_dir)
                if str(rel).startswith("build/"):
                    continue
                zf.write(str(file_path), str(rel))
    return buf.getvalue()


def test_rs_exact_sha_and_honest_digest(tmp_path) -> None:
    fixtures = Path(__file__).resolve().parent / "fixtures"
    source_dir = fixtures / "witness_component"

    # Catalog descriptor matching the witness_component wheel identity.
    catalog = ProductCatalog((
        ProductDescriptor(
            product_id="zewitness",
            display_name="ZeWitness",
            distribution_name="zealfie-witness",
            launch_entry_points=(EntryPointContract("console_scripts", "zewitness"),),
            required_extras=(),
            remote_source=RemoteSource(owner="tinystork", repo="ZeWitness", ref="main"),
        ),
    ))
    service = ZeAlfieService(catalog=catalog, runtime=_FakeAbsentRt())

    fetcher_calls: list[tuple[str, str, str]] = []

    def _fetcher(owner, repo, commit_sha):
        fetcher_calls.append((owner, repo, commit_sha))
        return _zip_fixture_source(source_dir)

    ppa = service.prepare_product_artifact_at_commit(
        "zewitness",
        commit_sha=SHA_A,
        source_owner="tinystork",
        source_repo="ZeWitness",
        requested_ref="main",
        fetcher=_fetcher,
        work_root=tmp_path / "work",
    )

    # Exact SHA is authoritative: the fetcher receives the commit SHA,
    # never the mutable ref.
    assert fetcher_calls == [("tinystork", "ZeWitness", SHA_A)]
    assert ppa.resolved_source.commit_sha == SHA_A
    assert ppa.resolved_source.source.ref == "main"  # preserved for provenance

    # Honest digest: the recorded wheel sha256 equals the actual built wheel.
    actual_digest = _sha256(ppa.wheel_path)
    assert ppa.verified_artifact.sha256 == actual_digest

    entries = _provenance_entries_for([ppa])
    assert len(entries) == 1
    assert entries[0].commit_sha == SHA_A
    assert entries[0].requested_ref == "main"
    assert entries[0].wheel_sha256 == actual_digest


# ---------------------------------------------------------------------------
# H. M1-2F-P1-C1 — fail-closed guards for provenance/selection divergence
# ---------------------------------------------------------------------------
#
# These guard against silently dropping an already-selected/active product
# when active provenance is missing or stale (e.g. after rollback).  The
# guards fire *before* fetch/build/apply/selection/provenance mutation.


def test_c1_a_first_install_empty_provenance_empty_selection_succeeds(
    tmp_path, monkeypatch,
) -> None:
    """A. First install with empty active provenance and empty selection
    still succeeds (the divergence guard must not fire)."""
    catalog = _catalog("alpha")
    service, _store, _selection, _layout = _service_with_active_provenance(
        tmp_path, catalog, []
    )

    assert service.active_provenance() == {}
    assert _selection.selected_product_ids == ()

    prepared_calls: list[list[PreparedProductArtifact]] = []
    monkeypatch.setattr(
        service, "prepare_product_artifact",
        lambda product_id, *, resolver, fetcher, work_root, progress_callback=None: _fake_ppa(product_id),
    )
    monkeypatch.setattr(
        service, "install_prepared_product_deployment",
        lambda prepared_artifacts, *, dependency_wheelhouse=None,
               probe_distribution=None, progress_callback=None, accelerated_acquirer=None: (
            prepared_calls.append(list(prepared_artifacts))
            or DeploymentResult(success=True, active_slot_id="rt-a")
        ),
    )

    wheelhouse = tmp_path / "wh"
    wheelhouse.mkdir()

    result = service.install_product(
        "alpha",
        resolver=lambda o, r, ref: VALID_SHA,
        fetcher=lambda o, r, sha: b"",
        work_root=tmp_path / "work",
        dependency_wheelhouse=wheelhouse,
    )

    assert result.success is True
    assert [pa.product_id for pa in prepared_calls[0]] == ["alpha"]


def test_c1_b_ready_empty_provenance_selection_divergence_fails_closed(
    tmp_path, monkeypatch,
) -> None:
    """B. READY runtime with empty active provenance, selection has alpha,
    installing beta fails before prepare/install/acquirer; selection and
    provenance are unchanged."""
    catalog = _catalog("alpha", "beta")

    layout = RuntimeLayout(root=tmp_path / "rt")
    store = ProductProvenanceStore(layout)
    # READY pointer to a slot with no recorded provenance → empty active
    # provenance while the runtime is READY.
    save_active_state(layout.active_pointer, "rt-ready", None)

    class _FakeReadyRt:
        def status(self) -> RuntimeStatus:
            return RuntimeStatus(
                state=RuntimeState.READY,
                runtime_root=layout.root,
                active_slot_id="rt-ready",
            )

    selection = SelectionStore(path=tmp_path / "sel.toml")
    selection.select("alpha", catalog=catalog)

    acquirer = _RecordingAcquirer()
    service = ZeAlfieService(
        catalog=catalog,
        runtime=_FakeReadyRt(),
        selection_store=selection,
        provenance_store=store,
        acquirer=acquirer,
    )

    assert service.active_provenance() == {}
    assert selection.selected_product_ids == ("alpha",)

    prepare_calls: list = []
    keep_calls: list = []
    install_calls: list = []

    def _explosive_prepare(*args, **kwargs):
        prepare_calls.append(args)
        raise AssertionError("prepare_product_artifact must not be called")

    def _explosive_keep(*args, **kwargs):
        keep_calls.append(args)
        raise AssertionError("prepare_product_artifact_at_commit must not be called")

    def _explosive_install(*args, **kwargs):
        install_calls.append(args)
        raise AssertionError("install_prepared_product_deployment must not be called")

    monkeypatch.setattr(service, "prepare_product_artifact", _explosive_prepare)
    monkeypatch.setattr(service, "prepare_product_artifact_at_commit", _explosive_keep)
    monkeypatch.setattr(service, "install_prepared_product_deployment", _explosive_install)

    provenance_exists = store.path.exists()
    provenance_before = store.path.read_bytes() if provenance_exists else None
    selection_before = selection.path.read_bytes()

    with pytest.raises(ProductInstallPreparationError, match="alpha"):
        service.install_product(
            "beta",
            resolver=lambda o, r, ref: VALID_SHA,
            fetcher=lambda o, r, sha: b"",
            work_root=tmp_path / "work",
            dependency_wheelhouse=None,
        )

    # Nothing beyond the guard ran.
    assert prepare_calls == []
    assert keep_calls == []
    assert install_calls == []
    assert acquirer.calls == []

    # Selection and provenance are byte-identical.
    assert selection.path.read_bytes() == selection_before
    if provenance_exists:
        assert store.path.read_bytes() == provenance_before
    else:
        assert not store.path.exists()


def test_c1_c_rollback_divergence_selection_active_mismatch_fails_closed(
    tmp_path, monkeypatch,
) -> None:
    """C. Rollback divergence: active provenance {alpha}, selection
    {alpha, beta}; installing gamma fails before mutation because beta
    lacks active provenance."""
    catalog = _catalog("alpha", "beta", "gamma")
    service, store, selection, _layout = _service_with_active_provenance(
        tmp_path, catalog, [_prov("alpha", commit_sha=SHA_A)]
    )

    # Post-rollback divergence: alpha is active, beta is still selected but
    # no longer active (rollback dropped it from active provenance).
    selection.select("alpha", catalog=catalog)
    selection.select("beta", catalog=catalog)

    assert set(service.active_provenance()) == {"alpha"}
    assert selection.selected_product_ids == ("alpha", "beta")

    prepare_calls: list = []
    keep_calls: list = []
    install_calls: list = []

    def _explosive_prepare(*args, **kwargs):
        prepare_calls.append(args)
        raise AssertionError("prepare_product_artifact must not be called")

    def _explosive_keep(*args, **kwargs):
        keep_calls.append(args)
        raise AssertionError("prepare_product_artifact_at_commit must not be called")

    def _explosive_install(*args, **kwargs):
        install_calls.append(args)
        raise AssertionError("install_prepared_product_deployment must not be called")

    monkeypatch.setattr(service, "prepare_product_artifact", _explosive_prepare)
    monkeypatch.setattr(service, "prepare_product_artifact_at_commit", _explosive_keep)
    monkeypatch.setattr(service, "install_prepared_product_deployment", _explosive_install)

    provenance_before = store.path.read_bytes()
    selection_before = selection.path.read_bytes()

    with pytest.raises(ProductInstallPreparationError, match="beta"):
        service.install_product(
            "gamma",
            resolver=lambda o, r, ref: VALID_SHA,
            fetcher=lambda o, r, sha: b"",
            work_root=tmp_path / "work",
            dependency_wheelhouse=tmp_path / "wh",
        )

    assert prepare_calls == []
    assert keep_calls == []
    assert install_calls == []
    assert store.path.read_bytes() == provenance_before
    assert selection.path.read_bytes() == selection_before


def test_c1_d_keep_version_mismatch_raises_preparation_error(
    tmp_path, monkeypatch,
) -> None:
    """D. Version-mismatch fail-closed branch in
    ``_prepare_keep_product_artifact`` raises ProductInstallPreparationError
    when the rebuilt wheel version differs from active provenance."""
    catalog = _catalog("alpha")
    service, _store, _selection, _layout = _service_with_active_provenance(
        tmp_path, catalog, [_prov("alpha", commit_sha=SHA_A, version="1.0.0")]
    )

    monkeypatch.setattr(
        service, "prepare_product_artifact_at_commit",
        lambda product_id, *, commit_sha, source_owner, source_repo, requested_ref,
               fetcher, work_root, progress_callback=None: _fake_ppa(
                   product_id, commit_sha=commit_sha, version="9.9.9",
               ),
    )

    provenance = service.active_provenance()["alpha"]
    assert provenance.version == "1.0.0"

    with pytest.raises(ProductInstallPreparationError, match="version"):
        service._prepare_keep_product_artifact(
            "alpha",
            provenance,
            fetcher=lambda o, r, sha: b"",
            work_root=tmp_path / "work",
        )
