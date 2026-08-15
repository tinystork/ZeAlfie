"""Tests for M1-2F Phase 5 — product-specific channel availability wiring.

Covers:

* catalog descriptor channel authority (``available_product_channels``);
* service ``product_policy`` / ``set_product_policy`` / ``set_product_channel``
  public API (fail-closed on undeclared channels, pin skips channel check);
* ``_prepare_target_product_artifact`` using product-specific channel refs
  for follow, never resolving for pin, and failing closed before the
  resolver for undeclared channels;
* service ``check_product_update`` obeying product channel availability.

All FAST — fake resolvers/fetchers only, no wheel building, no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zealfie.app import (
    ProductCatalog,
    ProductChannelUnavailableError,
    ProductDescriptor,
    ProductPolicy,
    ProductPolicyStore,
    ProductProvenance,
    ProductProvenanceStore,
    RemoteSourceUnavailableError,
    SelectionStore,
    UpdateStatus,
    ZeAlfieService,
)
from zealfie.components.model import EntryPointContract
from zealfie.runtime.layout import RuntimeLayout
from zealfie.runtime.model import DeploymentResult, RuntimeState, RuntimeStatus
from zealfie.runtime.state import save_active_state
from zealfie.sources import RemoteSource, ResolvedSource
from zealfie.releases.model import VerifiedArtifact


VALID_SHA = "d4a0f1e2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8"  # 40 hex
OTHER_SHA = "e5b1f2a3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9"  # 40 hex
PIN_SHA = "a" * 40

_EP = (EntryPointContract("console_scripts", "zewitness"),)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _descriptor(
    product_id: str,
    *,
    ref: str = "main",
    remote: bool = True,
    channel_refs: tuple[tuple[str, str], ...] | None = None,
) -> ProductDescriptor:
    remote_source = (
        RemoteSource(owner="tinystork", repo=f"Ze{product_id.capitalize()}", ref=ref)
        if remote
        else None
    )
    return ProductDescriptor(
        product_id=product_id,
        display_name=product_id.capitalize(),
        distribution_name=product_id,
        launch_entry_points=_EP,
        remote_source=remote_source,
        channel_refs=channel_refs or (),
    )


def _catalog(*descriptors: ProductDescriptor) -> ProductCatalog:
    return ProductCatalog(tuple(descriptors))


class _FakeAbsentRt:
    def status(self) -> RuntimeStatus:
        return RuntimeStatus(state=RuntimeState.ABSENT, runtime_root=Path("/fake"))


def _service(tmp_path: Path, catalog: ProductCatalog) -> ZeAlfieService:
    return ZeAlfieService(
        catalog=catalog,
        runtime=_FakeAbsentRt(),
        selection_store=SelectionStore(path=tmp_path / "sel.toml"),
        policy_store=ProductPolicyStore(path=tmp_path / "policy.toml"),
    )


def _prov(product_id: str) -> ProductProvenance:
    return ProductProvenance(
        product_id=product_id,
        version="1.0.0",
        source_owner="tinystork",
        source_repo=f"Ze{product_id.capitalize()}",
        requested_ref="main",
        commit_sha=VALID_SHA,
        wheel_sha256="e" * 64,
    )


def _service_with_provenance(
    tmp_path: Path, catalog: ProductCatalog,
) -> ZeAlfieService:
    layout = RuntimeLayout(root=tmp_path / "rt")
    store = ProductProvenanceStore(layout)
    store.record("rt-abc123", [_prov(p.product_id) for p in catalog.list()])
    save_active_state(layout.active_pointer, "rt-abc123", None)
    return ZeAlfieService(
        catalog=catalog,
        runtime=_FakeAbsentRt(),
        selection_store=SelectionStore(path=tmp_path / "sel.toml"),
        provenance_store=store,
        policy_store=ProductPolicyStore(path=tmp_path / "policy.toml"),
    )


def _resolver(sha: str = OTHER_SHA):
    calls: list[tuple[str, str, str]] = []

    def resolve(owner: str, repo: str, ref: str) -> str:
        calls.append((owner, repo, ref))
        return sha

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


def _fake_ppa(product_id: str) -> object:
    from zealfie.app import PreparedProductArtifact

    remote = RemoteSource(owner="tinystork", repo=f"Ze{product_id.capitalize()}", ref="main")
    resolved = ResolvedSource(source=remote, commit_sha=VALID_SHA)
    wheel_path = Path("/fake") / f"{product_id}.whl"
    return PreparedProductArtifact(
        product_id=product_id,
        component_id=product_id,
        resolved_source=resolved,
        wheel_path=wheel_path,
        verified_artifact=VerifiedArtifact(
            component_id=product_id,
            version="1.0.0",
            path=wheel_path,
            size=100,
            sha256="e" * 64,
            distribution_name=product_id,
            wheel_version="1.0.0",
        ),
    )


# ---------------------------------------------------------------------------
# 1. Public policy API
# ---------------------------------------------------------------------------


def test_available_product_channels_returns_declared_only(tmp_path: Path) -> None:
    catalog = _catalog(
        _descriptor("alpha"),
        _descriptor("beta", channel_refs=(("stable", "main"), ("beta", "beta"))),
        _descriptor("offline", remote=False),
    )
    service = _service(tmp_path, catalog)

    assert service.available_product_channels("alpha") == (("stable", "main"),)
    assert service.available_product_channels("beta") == (
        ("stable", "main"),
        ("beta", "beta"),
    )
    assert service.available_product_channels("offline") == ()


def test_available_product_channels_unknown_raises(tmp_path: Path) -> None:
    from zealfie.app import UnknownProductError

    service = _service(tmp_path, _catalog(_descriptor("alpha")))
    with pytest.raises(UnknownProductError):
        service.available_product_channels("ghost")


def test_product_policy_defaults_to_stable_follow(tmp_path: Path) -> None:
    service = _service(tmp_path, _catalog(_descriptor("alpha")))
    policy = service.product_policy("alpha")
    assert policy.channel == "stable"
    assert policy.policy == "follow"


def test_set_product_channel_persists(tmp_path: Path) -> None:
    catalog = _catalog(
        _descriptor("alpha", channel_refs=(("stable", "main"), ("beta", "beta"))),
    )
    service = _service(tmp_path, catalog)
    policy = service.set_product_channel("alpha", "beta")
    assert policy.channel == "beta"
    assert policy.policy == "follow"
    assert service.product_policy("alpha") == policy


def test_set_product_channel_undeclared_channel_raises(tmp_path: Path) -> None:
    catalog = _catalog(_descriptor("alpha"))  # only stable
    service = _service(tmp_path, catalog)
    with pytest.raises(ProductChannelUnavailableError) as exc_info:
        service.set_product_channel("alpha", "beta")
    assert "beta" in str(exc_info.value)
    assert "stable" in str(exc_info.value)


def test_set_product_policy_pin_skips_channel_check(tmp_path: Path) -> None:
    catalog = _catalog(_descriptor("alpha"))  # only stable; no beta
    service = _service(tmp_path, catalog)
    policy = service.set_product_policy(
        ProductPolicy(product_id="alpha", policy="pin", pin_sha=PIN_SHA)
    )
    assert policy.policy == "pin"
    assert policy.pin_sha == PIN_SHA
    assert service.product_policy("alpha") == policy


def test_set_product_policy_pin_requires_remote_source(tmp_path: Path) -> None:
    catalog = _catalog(_descriptor("offline", remote=False))
    service = _service(tmp_path, catalog)

    with pytest.raises(RemoteSourceUnavailableError):
        service.set_product_policy(
            ProductPolicy(product_id="offline", policy="pin", pin_sha=PIN_SHA)
        )

    # The failed policy change must not be persisted.
    assert service.product_policy("offline").policy == "follow"
    assert service.product_policy("offline").channel == "stable"


# ---------------------------------------------------------------------------
# 2. _prepare_target_product_artifact — follow / pin / no-source
# ---------------------------------------------------------------------------


def test_prepare_follow_beta_rejected_before_resolver(tmp_path: Path) -> None:
    catalog = _catalog(_descriptor("alpha"))  # stable only
    service = _service(tmp_path, catalog)
    resolver = _resolver()

    with pytest.raises(ProductChannelUnavailableError):
        service._prepare_target_product_artifact(
            "alpha",
            ProductPolicy(product_id="alpha", channel="beta", policy="follow"),
            resolver=resolver,
            fetcher=lambda o, r, sha: b"",
            work_root=tmp_path / "work",
        )
    assert resolver.calls == []


def test_prepare_follow_beta_resolved_with_product_ref(
    tmp_path: Path, monkeypatch,
) -> None:
    catalog = _catalog(
        _descriptor("alpha", channel_refs=(("stable", "main"), ("beta", "beta"))),
    )
    service = _service(tmp_path, catalog)
    prepare_calls: list[dict] = []

    def _fake_prepare(product_id, *, resolver, fetcher, work_root,
                      progress_callback=None, source_ref=None):
        prepare_calls.append({"product_id": product_id, "source_ref": source_ref})
        return _fake_ppa(product_id)

    monkeypatch.setattr(service, "prepare_product_artifact", _fake_prepare)

    service._prepare_target_product_artifact(
        "alpha",
        ProductPolicy(product_id="alpha", channel="beta", policy="follow"),
        resolver=_resolver(),
        fetcher=lambda o, r, sha: b"",
        work_root=tmp_path / "work",
    )
    assert prepare_calls == [{"product_id": "alpha", "source_ref": "beta"}]


def test_prepare_follow_stable_uses_default_ref(
    tmp_path: Path, monkeypatch,
) -> None:
    catalog = _catalog(_descriptor("alpha", ref="main"))  # stable -> main
    service = _service(tmp_path, catalog)
    prepare_calls: list[dict] = []

    def _fake_prepare(product_id, *, resolver, fetcher, work_root,
                      progress_callback=None, source_ref=None):
        prepare_calls.append({"product_id": product_id, "source_ref": source_ref})
        return _fake_ppa(product_id)

    monkeypatch.setattr(service, "prepare_product_artifact", _fake_prepare)

    service._prepare_target_product_artifact(
        "alpha",
        ProductPolicy(product_id="alpha", channel="stable", policy="follow"),
        resolver=_resolver(),
        fetcher=lambda o, r, sha: b"",
        work_root=tmp_path / "work",
    )
    # stable -> "main" equals remote_source.ref, so no override is passed.
    assert prepare_calls == [{"product_id": "alpha", "source_ref": None}]


def test_prepare_pin_never_resolves_no_channel_required(
    tmp_path: Path, monkeypatch,
) -> None:
    catalog = _catalog(_descriptor("alpha"))  # only stable, no beta
    service = _service(tmp_path, catalog)
    resolver = _resolver()
    at_commit_calls: list[dict] = []

    def _fake_at_commit(product_id, *, commit_sha, source_owner, source_repo,
                        requested_ref, fetcher, work_root, progress_callback=None):
        at_commit_calls.append({
            "product_id": product_id,
            "commit_sha": commit_sha,
            "requested_ref": requested_ref,
        })
        return _fake_ppa(product_id)

    monkeypatch.setattr(service, "prepare_product_artifact_at_commit", _fake_at_commit)

    def _explosive(*a, **k):
        raise AssertionError("prepare_product_artifact must not be called for pin")

    monkeypatch.setattr(service, "prepare_product_artifact", _explosive)

    service._prepare_target_product_artifact(
        "alpha",
        ProductPolicy(product_id="alpha", policy="pin", pin_sha=PIN_SHA),
        resolver=resolver,
        fetcher=lambda o, r, sha: b"",
        work_root=tmp_path / "work",
    )
    assert resolver.calls == []
    assert at_commit_calls == [{
        "product_id": "alpha",
        "commit_sha": PIN_SHA,
        "requested_ref": PIN_SHA,
    }]


def test_prepare_no_remote_source_fails(tmp_path: Path) -> None:
    catalog = _catalog(_descriptor("offline", remote=False))
    service = _service(tmp_path, catalog)

    with pytest.raises(RemoteSourceUnavailableError):
        service._prepare_target_product_artifact(
            "offline",
            ProductPolicy(product_id="offline", channel="stable", policy="follow"),
            resolver=_resolver(),
            fetcher=lambda o, r, sha: b"",
            work_root=tmp_path / "work",
        )


# ---------------------------------------------------------------------------
# 3. check_product_update — product channel availability
# ---------------------------------------------------------------------------


def test_check_update_beta_undeclared_is_check_failed(tmp_path: Path) -> None:
    catalog = _catalog(_descriptor("alpha"))  # stable only
    service = _service_with_provenance(tmp_path, catalog)
    # Persist a beta policy directly in the store (bypassing the service
    # setter, which itself fails closed) to prove check_product_update also
    # fails closed on the read path.
    service._policy_store.set_policy(
        ProductPolicy(product_id="alpha", channel="beta", policy="follow")
    )

    resolver = _resolver()
    result = service.check_product_update("alpha", resolver=resolver)

    # Undeclared channel → CHECK_FAILED before any resolver call.
    assert result.status is UpdateStatus.CHECK_FAILED
    assert "beta" in (result.error or "")
    assert resolver.calls == []


def test_check_update_beta_declared_resolves_beta(tmp_path: Path) -> None:
    catalog = _catalog(
        _descriptor("alpha", channel_refs=(("stable", "main"), ("beta", "beta"))),
    )
    service = _service_with_provenance(tmp_path, catalog)
    service.set_product_channel("alpha", "beta")

    resolver = _resolver()
    result = service.check_product_update("alpha", resolver=resolver)

    # beta declared → resolver sees the beta ref for this product.
    assert result.status is UpdateStatus.UPDATE_AVAILABLE
    assert resolver.calls == [("tinystork", "ZeAlpha", "beta")]


def test_check_update_pin_never_resolves(tmp_path: Path) -> None:
    catalog = _catalog(_descriptor("alpha"))
    service = _service(tmp_path, catalog)
    service.set_product_policy(
        ProductPolicy(product_id="alpha", policy="pin", pin_sha=PIN_SHA)
    )

    resolver = _resolver()
    result = service.check_product_update("alpha", resolver=resolver)

    # No provenance → PROVENANCE_UNKNOWN (never resolves), resolver untouched.
    assert result.status is UpdateStatus.PROVENANCE_UNKNOWN
    assert resolver.calls == []
