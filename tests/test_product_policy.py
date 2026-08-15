"""Tests for M1-2F Phase 3 (Lot F.2) — Channel / Follow / Pin model.

Covers the minimal channel/follow/pin configuration added to ZeAlfie:

* per-product persistent user configuration (``channel`` / ``policy`` /
  ``pin_sha``) and its fail-closed validation;
* the single central channel→ref mapping and per-product override;
* follow resolution (re-resolve the channel ref) vs pin resolution
  (compare the pinned SHA, never contacting the resolver);
* integration into ``ZeAlfieService.check_product_update`` and
  ``ZeAlfieService.install_product``.

All tests are FAST — no real GitHub/network, no wheel building, no venv,
no pip.  Resolvers/fetchers are fakes; the install apply step is faked by
monkeypatching ``install_prepared_product_deployment``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from zealfie.app import (
    CorruptProductPolicyError,
    DEFAULT_CHANNEL_REFS,
    ProductCatalog,
    ProductDescriptor,
    ProductPolicy,
    ProductPolicyStore,
    ProductProvenance,
    ProductProvenanceStore,
    SelectionStore,
    UpdateStatus,
    ZeAlfieService,
    check_product_update,
    default_product_policy,
    effective_ref,
)
from zealfie.app.service import _provenance_entries_for
from zealfie.components.model import EntryPointContract
from zealfie.releases.model import VerifiedArtifact
from zealfie.runtime.layout import RuntimeLayout
from zealfie.runtime.model import DeploymentResult, RuntimeState, RuntimeStatus
from zealfie.runtime.state import save_active_state
from zealfie.sources import RemoteSource, ResolvedSource, SourceResolutionError


VALID_SHA = "d4a0f1e2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8"  # 40 hex
OTHER_SHA = "e5b1f2a3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9"  # 40 hex
PIN_SHA = "a" * 40  # 40 hex
WHEEL_SHA = "f" * 64

_EP = (EntryPointContract("console_scripts", "zewitness"),)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prov(
    product_id: str = "zewitness",
    *,
    commit_sha: str = VALID_SHA,
    requested_ref: str = "main",
    source_repo: str | None = None,
    **kwargs,
) -> ProductProvenance:
    if source_repo is None:
        source_repo = f"Ze{product_id.capitalize()}"
    defaults = dict(
        product_id=product_id,
        version="0.0.1",
        source_owner="tinystork",
        source_repo=source_repo,
        requested_ref=requested_ref,
        commit_sha=commit_sha,
        wheel_sha256=WHEEL_SHA,
    )
    defaults.update(kwargs)
    return ProductProvenance(**defaults)


def _descriptor(
    product_id: str,
    *,
    ref: str = "main",
    channel_refs: tuple[tuple[str, str], ...] | None = None,
) -> ProductDescriptor:
    return ProductDescriptor(
        product_id=product_id,
        display_name=product_id.capitalize(),
        distribution_name=product_id,
        launch_entry_points=(EntryPointContract("console_scripts", product_id),),
        required_extras=(),
        remote_source=RemoteSource(
            owner="tinystork",
            repo=f"Ze{product_id.capitalize()}",
            ref=ref,
        ),
        channel_refs=channel_refs or (),
    )


def _catalog(*product_ids: str) -> ProductCatalog:
    return ProductCatalog(tuple(_descriptor(pid) for pid in product_ids))


def _resolver(sha: str = OTHER_SHA):
    """A fake resolver returning *sha*; records every call on ``.calls``."""
    calls: list[tuple[str, str, str]] = []

    def resolve(owner: str, repo: str, ref: str) -> str:
        calls.append((owner, repo, ref))
        return sha

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


class _FakeAbsentRt:
    """Fake runtime without a layout → provenance only via injection."""

    def status(self) -> RuntimeStatus:
        return RuntimeStatus(state=RuntimeState.ABSENT, runtime_root=Path("/fake"))


def _fake_layout(tmp_path: Path) -> RuntimeLayout:
    return RuntimeLayout(root=tmp_path / "rt")


def _service(
    tmp_path: Path,
    catalog: ProductCatalog,
    *,
    entries: list[ProductProvenance] | None = None,
    policies: list[ProductPolicy] | None = None,
    policy_path: Path | None = None,
) -> ZeAlfieService:
    """Build a service with an injected provenance store and policy store."""
    layout = _fake_layout(tmp_path)
    store = ProductProvenanceStore(layout)
    if entries:
        store.record("rt-abc123", entries)
        save_active_state(layout.active_pointer, "rt-abc123", None)

    policy_store = ProductPolicyStore(path=policy_path or (tmp_path / "policy.toml"))
    for policy in policies or []:
        policy_store.set_policy(policy)

    return ZeAlfieService(
        catalog=catalog,
        runtime=_FakeAbsentRt(),
        selection_store=SelectionStore(path=tmp_path / "sel.toml"),
        provenance_store=store,
        policy_store=policy_store,
    )


def _fake_ppa(
    product_id: str,
    *,
    commit_sha: str = VALID_SHA,
    requested_ref: str = "main",
    version: str = "1.0.0",
) -> object:
    remote = RemoteSource(
        owner="tinystork",
        repo=f"Ze{product_id.capitalize()}",
        ref=requested_ref,
    )
    resolved = ResolvedSource(source=remote, commit_sha=commit_sha)
    wheel_path = Path("/fake") / f"{product_id}.whl"
    from zealfie.app import PreparedProductArtifact

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
            distribution_name=product_id,
            wheel_version=version,
        ),
    )


# ---------------------------------------------------------------------------
# 1. Follow detects update with no user config (defaults)
# ---------------------------------------------------------------------------


def test_follow_default_detects_update_pure_core() -> None:
    prov = _prov(
        commit_sha=VALID_SHA,
        requested_ref="main",
        source_repo="ZeWitness",
    )
    resolver = _resolver(OTHER_SHA)

    result = check_product_update(
        "zewitness", prov, resolver=resolver,
        policy=default_product_policy("zewitness"),
    )

    assert result.status is UpdateStatus.UPDATE_AVAILABLE
    assert result.requested_ref == "main"
    assert resolver.calls == [("tinystork", "ZeWitness", "main")]


def test_follow_default_detects_update_service_level(tmp_path: Path) -> None:
    catalog = _catalog("alpha")
    service = _service(
        tmp_path, catalog, entries=[_prov(product_id="alpha", commit_sha=VALID_SHA)],
    )

    resolver = _resolver(OTHER_SHA)
    result = service.check_product_update("alpha", resolver=resolver)

    assert result.status is UpdateStatus.UPDATE_AVAILABLE
    # No user policy → default stable/follow → resolve the central "main".
    assert resolver.calls == [("tinystork", "ZeAlpha", "main")]


# ---------------------------------------------------------------------------
# 2. Pin ignores the remote ref — no resolver, compare pin_sha
# ---------------------------------------------------------------------------


def test_pin_check_never_resolves_and_compares_pin_sha() -> None:
    prov = _prov(commit_sha=VALID_SHA, requested_ref="main")
    resolver = _resolver(OTHER_SHA)  # would return OTHER_SHA if ever called

    # pin to a different SHA → UPDATE_AVAILABLE, no resolver call.
    result = check_product_update(
        "zewitness",
        prov,
        resolver=resolver,
        policy=ProductPolicy(product_id="zewitness", policy="pin", pin_sha=OTHER_SHA),
    )
    assert result.status is UpdateStatus.UPDATE_AVAILABLE
    assert result.latest_commit_sha == OTHER_SHA
    assert result.requested_ref == OTHER_SHA
    assert resolver.calls == []

    # pin to the installed SHA → UP_TO_DATE, still no resolver call.
    result2 = check_product_update(
        "zewitness",
        prov,
        resolver=resolver,
        policy=ProductPolicy(product_id="zewitness", policy="pin", pin_sha=VALID_SHA),
    )
    assert result2.status is UpdateStatus.UP_TO_DATE
    assert result2.requested_ref == VALID_SHA
    assert resolver.calls == []


def test_pin_check_unusable_pin_sha_is_check_failed_defensive() -> None:
    """Defensive branch: a policy that somehow carries no pin_sha must fail
    closed (CHECK_FAILED) rather than resolve a mutable ref."""
    prov = _prov(commit_sha=VALID_SHA)
    resolver = _resolver(OTHER_SHA)
    policy = SimpleNamespace(policy="pin", pin_sha=None)  # bypasses validation

    result = check_product_update("zewitness", prov, resolver=resolver, policy=policy)

    assert result.status is UpdateStatus.CHECK_FAILED
    assert resolver.calls == []


# ---------------------------------------------------------------------------
# 3. Stable/beta independence through the central mapping
# ---------------------------------------------------------------------------


def test_effective_ref_central_mapping_and_override() -> None:
    assert DEFAULT_CHANNEL_REFS == {
        "stable": "main",
        "beta": "beta",
        "development": "development",
    }
    assert effective_ref(ProductPolicy(product_id="x", channel="stable")) == "main"
    assert effective_ref(ProductPolicy(product_id="x", channel="beta")) == "beta"
    assert effective_ref(
        ProductPolicy(product_id="x", channel="development")
    ) == "development"
    # Per-product override wins over the central mapping.
    assert effective_ref(
        ProductPolicy(product_id="x", channel="stable", source_ref="custom")
    ) == "custom"
    # Pin returns the immutable SHA.
    assert effective_ref(
        ProductPolicy(product_id="x", policy="pin", pin_sha=PIN_SHA)
    ) == PIN_SHA


def test_stable_beta_resolve_different_refs_service_level(tmp_path: Path) -> None:
    catalog = ProductCatalog((
        _descriptor("alpha", ref="main"),
        _descriptor(
            "beta",
            ref="main",
            channel_refs=(("stable", "main"), ("beta", "beta")),
        ),
    ))
    service = _service(
        tmp_path,
        catalog,
        entries=[
            _prov(product_id="alpha", commit_sha=VALID_SHA, requested_ref="main"),
            _prov(product_id="beta", commit_sha=VALID_SHA, requested_ref="beta"),
        ],
        policies=[
            ProductPolicy(product_id="alpha", channel="stable", policy="follow"),
            ProductPolicy(product_id="beta", channel="beta", policy="follow"),
        ],
    )

    resolver = _resolver(OTHER_SHA)
    service.check_product_update("alpha", resolver=resolver)
    service.check_product_update("beta", resolver=resolver)

    assert resolver.calls == [
        ("tinystork", "ZeAlpha", "main"),
        ("tinystork", "ZeBeta", "beta"),
    ]


# ---------------------------------------------------------------------------
# 4. Pin install/update — exact pin_sha, no resolver, provenance == pin_sha
# ---------------------------------------------------------------------------


def test_pin_install_prepares_exact_sha_no_resolver(
    tmp_path: Path, monkeypatch,
) -> None:
    catalog = _catalog("alpha")
    service = _service(
        tmp_path,
        catalog,
        policies=[ProductPolicy(product_id="alpha", policy="pin", pin_sha=PIN_SHA)],
    )

    at_commit_calls: list[dict] = []
    prepared_calls: list[list] = []
    resolver = _resolver(OTHER_SHA)

    def _fake_at_commit(product_id, *, commit_sha, source_owner, source_repo,
                        requested_ref, fetcher, work_root, progress_callback=None):
        at_commit_calls.append({
            "product_id": product_id,
            "commit_sha": commit_sha,
            "requested_ref": requested_ref,
        })
        return _fake_ppa(product_id, commit_sha=commit_sha, requested_ref=requested_ref)

    monkeypatch.setattr(service, "prepare_product_artifact_at_commit", _fake_at_commit)

    def _explosive_prepare(*args, **kwargs):
        raise AssertionError("prepare_product_artifact must not be called for pin")

    monkeypatch.setattr(service, "prepare_product_artifact", _explosive_prepare)
    monkeypatch.setattr(
        service, "install_prepared_product_deployment",
        lambda prepared_artifacts, *, dependency_wheelhouse=None,
               probe_distribution=None, progress_callback=None: (
            prepared_calls.append(list(prepared_artifacts))
            or DeploymentResult(success=True, active_slot_id="rt-1")
        ),
    )

    wheelhouse = tmp_path / "wh"
    wheelhouse.mkdir()

    result = service.install_product(
        "alpha",
        resolver=resolver,
        fetcher=lambda o, r, sha: b"",
        work_root=tmp_path / "work",
        dependency_wheelhouse=wheelhouse,
    )

    assert result.success is True
    # No mutable-ref resolution: the resolver is never called.
    assert resolver.calls == []
    # The target was prepared from the exact pin_sha (requested_ref == pin_sha).
    assert at_commit_calls == [{
        "product_id": "alpha",
        "commit_sha": PIN_SHA,
        "requested_ref": PIN_SHA,
    }]
    # Provenance records commit_sha == pin_sha (and requested_ref == pin_sha).
    entries = _provenance_entries_for(prepared_calls[0])
    assert len(entries) == 1
    assert entries[0].commit_sha == PIN_SHA
    assert entries[0].requested_ref == PIN_SHA
    # Pin provenance records policy=pin, pin_sha, and no channel.
    assert entries[0].policy == "pin"
    assert entries[0].pin_sha == PIN_SHA
    assert entries[0].channel is None


def test_follow_install_provenance_records_channel_policy_distinct(
    tmp_path: Path, monkeypatch,
) -> None:
    """A follow/stable install records channel, policy, requested_ref, and
    commit_sha as distinct facts: channel is the discovery channel
    ('stable'), requested_ref is the effective mapped ref ('main'), and
    commit_sha is the resolved immutable SHA."""
    catalog = _catalog("alpha")
    service = _service(
        tmp_path,
        catalog,
        policies=[ProductPolicy(product_id="alpha", channel="stable", policy="follow")],
    )

    prepared_calls: list[list] = []
    resolver = _resolver(OTHER_SHA)

    def _fake_prepare(product_id, *, resolver, fetcher, work_root,
                      progress_callback=None):
        return _fake_ppa(product_id, commit_sha=OTHER_SHA, requested_ref="main")

    monkeypatch.setattr(service, "prepare_product_artifact", _fake_prepare)
    monkeypatch.setattr(
        service, "install_prepared_product_deployment",
        lambda prepared_artifacts, *, dependency_wheelhouse=None,
               probe_distribution=None, progress_callback=None: (
            prepared_calls.append(list(prepared_artifacts))
            or DeploymentResult(success=True, active_slot_id="rt-1")
        ),
    )

    wheelhouse = tmp_path / "wh"
    wheelhouse.mkdir()

    result = service.install_product(
        "alpha",
        resolver=resolver,
        fetcher=lambda o, r, sha: b"",
        work_root=tmp_path / "work",
        dependency_wheelhouse=wheelhouse,
    )

    assert result.success is True
    entries = _provenance_entries_for(prepared_calls[0])
    assert len(entries) == 1
    entry = entries[0]
    assert entry.channel == "stable"
    assert entry.policy == "follow"
    assert entry.requested_ref == "main"
    assert entry.commit_sha == OTHER_SHA
    assert entry.pin_sha is None


def test_keep_propagates_policy_never_resolves(
    tmp_path: Path, monkeypatch,
) -> None:
    """A KEEP product is materialized from its exact active commit SHA
    (never re-resolved) and its known policy metadata is carried forward
    onto the rebuilt provenance entry."""
    catalog = _catalog("alpha", "beta")
    service = _service(
        tmp_path,
        catalog,
        entries=[
            _prov(
                product_id="alpha",
                commit_sha=VALID_SHA,
                version="1.0.0",
                requested_ref="beta",
                channel="beta",
                policy="follow",
            ),
        ],
        policies=[ProductPolicy(product_id="beta", channel="stable", policy="follow")],
    )

    keep_calls: list[dict] = []
    resolve_calls: list[str] = []
    prepared_calls: list[list] = []

    def _fake_at_commit(product_id, *, commit_sha, source_owner, source_repo,
                        requested_ref, fetcher, work_root, progress_callback=None):
        keep_calls.append({"product_id": product_id, "commit_sha": commit_sha})
        return _fake_ppa(product_id, commit_sha=commit_sha, requested_ref=requested_ref)

    def _fake_prepare(product_id, *, resolver, fetcher, work_root,
                      progress_callback=None):
        resolve_calls.append(product_id)
        return _fake_ppa(product_id, commit_sha=OTHER_SHA, requested_ref="main")

    monkeypatch.setattr(service, "prepare_product_artifact_at_commit", _fake_at_commit)
    monkeypatch.setattr(service, "prepare_product_artifact", _fake_prepare)
    monkeypatch.setattr(
        service, "install_prepared_product_deployment",
        lambda prepared_artifacts, *, dependency_wheelhouse=None,
               probe_distribution=None, progress_callback=None: (
            prepared_calls.append(list(prepared_artifacts))
            or DeploymentResult(success=True, active_slot_id="rt-2")
        ),
    )

    wheelhouse = tmp_path / "wh"
    wheelhouse.mkdir()

    result = service.install_product(
        "beta",
        resolver=_resolver(OTHER_SHA),
        fetcher=lambda o, r, sha: b"",
        work_root=tmp_path / "work",
        dependency_wheelhouse=wheelhouse,
    )

    assert result.success is True
    # KEEP alpha materialized from exact SHA, never the resolver path.
    assert keep_calls == [{"product_id": "alpha", "commit_sha": VALID_SHA}]
    assert resolve_calls == ["beta"]

    entries = _provenance_entries_for(prepared_calls[0])
    by_id = {e.product_id: e for e in entries}
    keep_entry = by_id["alpha"]
    assert keep_entry.commit_sha == VALID_SHA  # exact SHA preserved
    assert keep_entry.policy == "follow"        # known policy propagated
    assert keep_entry.channel == "beta"
    assert keep_entry.pin_sha is None


# ---------------------------------------------------------------------------
# 5. Fail-closed validation
# ---------------------------------------------------------------------------


def test_pin_requires_valid_pin_sha() -> None:
    with pytest.raises(ValueError, match="pin_sha"):
        ProductPolicy(product_id="x", policy="pin")  # missing
    with pytest.raises(ValueError, match="pin_sha"):
        ProductPolicy(product_id="x", policy="pin", pin_sha="not-hex")
    with pytest.raises(ValueError, match="pin_sha"):
        ProductPolicy(product_id="x", policy="pin", pin_sha="abcd1234")  # too short


def test_follow_rejects_pin_sha() -> None:
    with pytest.raises(ValueError, match="pin_sha"):
        ProductPolicy(product_id="x", policy="follow", pin_sha=VALID_SHA)


def test_unknown_channel_rejected() -> None:
    with pytest.raises(ValueError, match="channel"):
        ProductPolicy(product_id="x", channel="nightly")


def test_unknown_policy_rejected() -> None:
    with pytest.raises(ValueError, match="policy"):
        ProductPolicy(product_id="x", policy="floating")


def test_pin_rejects_source_ref_override() -> None:
    with pytest.raises(ValueError, match="source_ref"):
        ProductPolicy(product_id="x", policy="pin", pin_sha=PIN_SHA, source_ref="main")


def test_store_rejects_invalid_config_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "product-policy.toml"
    path.write_text(
        'schema_version = 1\n\n'
        '[products.x]\n'
        'channel = "stable"\n'
        'policy = "pin"\n',  # missing pin_sha → fail closed
        encoding="utf-8",
    )
    store = ProductPolicyStore(path=path)
    with pytest.raises(CorruptProductPolicyError, match="pin_sha"):
        store.policy_for("x")


def test_store_rejects_unknown_channel_config(tmp_path: Path) -> None:
    path = tmp_path / "product-policy.toml"
    path.write_text(
        'schema_version = 1\n\n'
        '[products.x]\n'
        'channel = "nightly"\n'
        'policy = "follow"\n',
        encoding="utf-8",
    )
    store = ProductPolicyStore(path=path)
    with pytest.raises(CorruptProductPolicyError, match="channel"):
        store.policy_for("x")


# ---------------------------------------------------------------------------
# 6. Defaults regression — unconfigured product == follow/stable
# ---------------------------------------------------------------------------


def test_default_product_policy_is_stable_follow() -> None:
    p = default_product_policy("anything")
    assert p.product_id == "anything"
    assert p.channel == "stable"
    assert p.policy == "follow"
    assert p.pin_sha is None
    assert p.source_ref is None


def test_unconfigured_store_returns_default(tmp_path: Path) -> None:
    store = ProductPolicyStore(path=tmp_path / "missing.toml")
    assert store.policy_for("anything") == default_product_policy("anything")


def test_store_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "product-policy.toml"
    store = ProductPolicyStore(path=path)
    store.set_policy(ProductPolicy(product_id="alpha", channel="beta", policy="follow"))
    store.set_policy(
        ProductPolicy(product_id="gamma", channel="stable", policy="pin", pin_sha=PIN_SHA)
    )

    reloaded = ProductPolicyStore(path=path)
    assert reloaded.policy_for("alpha") == ProductPolicy(
        product_id="alpha", channel="beta", policy="follow"
    )
    assert reloaded.policy_for("gamma") == ProductPolicy(
        product_id="gamma", channel="stable", policy="pin", pin_sha=PIN_SHA
    )
    assert reloaded.policy_for("unknown") == default_product_policy("unknown")
