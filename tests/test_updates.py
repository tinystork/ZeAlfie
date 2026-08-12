"""Tests for M1-2E LOT E.2 — read-only update detection.

Covers the pure :func:`zealfie.app.updates.check_product_update` core and
the service-level :meth:`ZeAlfieService.check_product_update` /
:meth:`ZeAlfieService.check_updates` read-only API, using fake resolvers
(no real GitHub/network) and real provenance/selection stores on tmp dirs.

FAST: no ``zealfie_slow`` marker — no wheel building, venv, pip, or network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zealfie.app import (
    ProductCatalog,
    ProductDescriptor,
    ProductProvenance,
    ProductProvenanceStore,
    ProductUpdateResult,
    SelectionStore,
    UpdateStatus,
    ZeAlfieService,
    check_product_update,
)
from zealfie.components.model import EntryPointContract
from zealfie.runtime.layout import RuntimeLayout
from zealfie.runtime.state import save_active_state
from zealfie.sources import SourceResolutionError


VALID_SHA = "d4a0f1e2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8"  # 40 hex
OTHER_SHA = "e5b1f2a3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9"  # 40 hex
WHEEL_SHA = "a" * 64

_EP = (EntryPointContract("console_scripts", "zewitness"),)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prov(
    product_id: str = "zewitness",
    *,
    commit_sha: str = VALID_SHA,
    **kwargs,
) -> ProductProvenance:
    defaults = dict(
        product_id=product_id,
        version="0.0.1",
        source_owner="tinystork",
        source_repo="ZeWitness",
        requested_ref="main",
        commit_sha=commit_sha,
        wheel_sha256=WHEEL_SHA,
    )
    defaults.update(kwargs)
    return ProductProvenance(**defaults)


def _catalog() -> ProductCatalog:
    return ProductCatalog((
        ProductDescriptor(
            product_id="zewitness",
            display_name="ZeWitness",
            distribution_name="zealfie-witness",
            launch_entry_points=_EP,
        ),
    ))


def _fake_layout(tmp_path: Path) -> RuntimeLayout:
    return RuntimeLayout(root=tmp_path / "rt")


def _resolver(sha: str):
    """A fake resolver returning *sha*; records every call on ``.calls``."""
    calls: list[tuple[str, str, str]] = []

    def resolve(owner: str, repo: str, ref: str) -> str:
        calls.append((owner, repo, ref))
        return sha

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


def _failing_resolver(message: str = "network down"):
    """A fake resolver that raises; records every call on ``.calls``."""
    calls: list[tuple[str, str, str]] = []

    def resolve(owner: str, repo: str, ref: str) -> str:
        calls.append((owner, repo, ref))
        raise SourceResolutionError(message)

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


# ---------------------------------------------------------------------------
# Pure core: check_product_update
# ---------------------------------------------------------------------------


def test_no_provenance_is_unknown_and_no_resolver_call() -> None:
    resolver = _resolver(VALID_SHA)
    result = check_product_update("zewitness", None, resolver=resolver)

    assert result.status is UpdateStatus.PROVENANCE_UNKNOWN
    assert result.installed_commit_sha is None
    assert result.latest_commit_sha is None
    assert result.error is None
    # No provenance → resolver never invoked.
    assert resolver.calls == []


def test_same_resolved_commit_is_up_to_date() -> None:
    prov = _prov(commit_sha=VALID_SHA)
    resolver = _resolver(VALID_SHA)

    result = check_product_update("zewitness", prov, resolver=resolver)

    assert result.status is UpdateStatus.UP_TO_DATE
    assert result.installed_commit_sha == VALID_SHA
    assert result.latest_commit_sha == VALID_SHA
    assert result.version == "0.0.1"
    assert result.source_owner == "tinystork"
    assert result.source_repo == "ZeWitness"
    assert result.requested_ref == "main"
    assert result.error is None


def test_different_resolved_commit_is_update_available() -> None:
    prov = _prov(commit_sha=VALID_SHA)
    resolver = _resolver(OTHER_SHA)

    result = check_product_update("zewitness", prov, resolver=resolver)

    assert result.status is UpdateStatus.UPDATE_AVAILABLE
    assert result.installed_commit_sha == VALID_SHA
    assert result.latest_commit_sha == OTHER_SHA
    assert result.source_owner == "tinystork"
    assert result.source_repo == "ZeWitness"
    assert result.requested_ref == "main"
    assert result.version == "0.0.1"


def test_resolver_failure_is_check_failed_no_mutation() -> None:
    prov = _prov(commit_sha=VALID_SHA)
    resolver = _failing_resolver("network down")

    result = check_product_update("zewitness", prov, resolver=resolver)

    assert result.status is UpdateStatus.CHECK_FAILED
    assert "network down" in (result.error or "")
    assert result.installed_commit_sha == VALID_SHA
    assert result.latest_commit_sha is None
    # Source/version context is still carried for diagnostics.
    assert result.version == "0.0.1"
    assert result.source_owner == "tinystork"
    assert result.source_repo == "ZeWitness"
    assert result.requested_ref == "main"


def test_invalid_provenance_source_is_check_failed_without_resolver_call() -> None:
    prov = _prov(commit_sha=VALID_SHA, source_owner="foo..bar")
    resolver = _resolver(OTHER_SHA)

    result = check_product_update("zewitness", prov, resolver=resolver)

    assert result.status is UpdateStatus.CHECK_FAILED
    assert "foo..bar" in (result.error or "")
    assert result.installed_commit_sha == VALID_SHA
    assert result.latest_commit_sha is None
    assert result.source_owner == "foo..bar"
    assert result.source_repo == "ZeWitness"
    assert result.requested_ref == "main"
    assert resolver.calls == []


def test_resolver_receives_provenance_source_and_ref() -> None:
    prov = _prov(
        commit_sha=VALID_SHA,
        source_owner="someorg",
        source_repo="SomeRepo",
        requested_ref="release-2.x",
    )
    resolver = _resolver(OTHER_SHA)

    check_product_update("zewitness", prov, resolver=resolver)

    assert resolver.calls == [("someorg", "SomeRepo", "release-2.x")]


# ---------------------------------------------------------------------------
# Model / enum completeness for future GUI (E.3)
# ---------------------------------------------------------------------------


def test_not_checked_default_state_exists() -> None:
    result = ProductUpdateResult.not_checked("zewitness")

    assert result.product_id == "zewitness"
    assert result.status is UpdateStatus.NOT_CHECKED
    assert result.installed_commit_sha is None
    assert result.latest_commit_sha is None
    assert result.error is None


def test_checking_enum_and_full_status_set_exist() -> None:
    # All six statuses are defined exactly once, with the expected values.
    assert {s.value for s in UpdateStatus} == {
        "NOT_CHECKED",
        "CHECKING",
        "UP_TO_DATE",
        "UPDATE_AVAILABLE",
        "CHECK_FAILED",
        "PROVENANCE_UNKNOWN",
    }
    assert UpdateStatus.CHECKING.value == "CHECKING"
    assert UpdateStatus.NOT_CHECKED.value == "NOT_CHECKED"


# ---------------------------------------------------------------------------
# Service-level API
# ---------------------------------------------------------------------------


def test_service_check_product_update_up_to_date(tmp_path: Path) -> None:
    layout = _fake_layout(tmp_path)
    store = ProductProvenanceStore(layout)
    store.record("rt-abc123", [_prov(commit_sha=VALID_SHA)])
    save_active_state(layout.active_pointer, "rt-abc123", None)

    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=_FakeAbsentRt(),
        selection_store=SelectionStore(path=tmp_path / "sel.toml"),
        provenance_store=store,
    )

    result = service.check_product_update(
        "zewitness", resolver=_resolver(VALID_SHA),
    )

    assert result.status is UpdateStatus.UP_TO_DATE
    assert result.installed_commit_sha == VALID_SHA
    assert result.latest_commit_sha == VALID_SHA


def test_service_check_product_update_no_provenance(tmp_path: Path) -> None:
    store = ProductProvenanceStore(_fake_layout(tmp_path))  # empty

    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=_FakeAbsentRt(),
        selection_store=SelectionStore(path=tmp_path / "sel.toml"),
        provenance_store=store,
    )

    resolver = _resolver(VALID_SHA)
    result = service.check_product_update("zewitness", resolver=resolver)

    assert result.status is UpdateStatus.PROVENANCE_UNKNOWN
    assert resolver.calls == []


def test_service_check_updates_none_checks_catalog(tmp_path: Path) -> None:
    layout = _fake_layout(tmp_path)
    store = ProductProvenanceStore(layout)
    store.record("rt-abc123", [_prov(commit_sha=VALID_SHA)])
    save_active_state(layout.active_pointer, "rt-abc123", None)

    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=_FakeAbsentRt(),
        selection_store=SelectionStore(path=tmp_path / "sel.toml"),
        provenance_store=store,
    )

    results = service.check_updates(resolver=_resolver(OTHER_SHA))

    assert len(results) == 1
    assert results[0].product_id == "zewitness"
    assert results[0].status is UpdateStatus.UPDATE_AVAILABLE


def test_service_check_updates_unknown_id_is_unknown(tmp_path: Path) -> None:
    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=_FakeAbsentRt(),
        selection_store=SelectionStore(path=tmp_path / "sel.toml"),
        provenance_store=ProductProvenanceStore(_fake_layout(tmp_path)),
    )

    resolver = _resolver(VALID_SHA)
    results = service.check_updates(["ghost"], resolver=resolver)

    assert len(results) == 1
    assert results[0].product_id == "ghost"
    assert results[0].status is UpdateStatus.PROVENANCE_UNKNOWN
    assert resolver.calls == []


# ---------------------------------------------------------------------------
# Read-only invariant: checks never mutate persisted state
# ---------------------------------------------------------------------------


def test_check_is_read_only_no_persisted_state_mutation(tmp_path: Path) -> None:
    layout = _fake_layout(tmp_path)
    store = ProductProvenanceStore(layout)
    store.record("rt-abc123", [_prov(commit_sha=VALID_SHA)])
    save_active_state(layout.active_pointer, "rt-abc123", None)

    sel_path = tmp_path / "desired-products.toml"
    selection = SelectionStore(path=sel_path)
    selection.select("zewitness", catalog=_catalog())

    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=_FakeAbsentRt(),
        selection_store=selection,
        provenance_store=store,
    )

    provenance_before = store.path.read_bytes()
    active_before = layout.active_pointer.read_bytes()
    selection_before = sel_path.read_bytes()

    # Run the full read-only surface: single + batch, with a resolver
    # returning a different SHA (exercises the UPDATE_AVAILABLE branch).
    service.check_product_update("zewitness", resolver=_resolver(OTHER_SHA))
    service.check_updates(resolver=_resolver(OTHER_SHA))

    # Nothing persisted changed.
    assert store.path.read_bytes() == provenance_before
    assert layout.active_pointer.read_bytes() == active_before
    assert sel_path.read_bytes() == selection_before

    # In-memory read model still authoritative and unchanged.
    assert store.product_provenance("zewitness").commit_sha == VALID_SHA


# ---------------------------------------------------------------------------
# Fake runtime (no layout attribute → provenance derived store is disabled)
# ---------------------------------------------------------------------------


class _FakeAbsentRt:
    def status(self):
        from zealfie.runtime.model import RuntimeState, RuntimeStatus

        return RuntimeStatus(state=RuntimeState.ABSENT, runtime_root=Path("/fake"))
