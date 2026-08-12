"""Tests for M1-2E LOT E.3 — non-blocking in-memory update-check state.

Covers the pure, Qt-free :class:`zealfie.app.update_checks.UpdateCheckCoordinator`:

* initial per-product state is ``NOT_CHECKED``;
* starting a check transitions to ``CHECKING`` before completion;
* terminal results update state and notify observers;
* resolver failure → ``CHECK_FAILED`` (and unexpected check exceptions);
* no provenance → ``PROVENANCE_UNKNOWN``;
* non-blocking: ``start`` returns before the resolver completes;
* out-of-order safety: a stale older result cannot overwrite a newer one;
* read-only invariant: checks never mutate persisted runtime/provenance/
  selection/active-pointer bytes.

FAST: no ``zealfie_slow`` marker — fake check functions and fake resolvers,
no wheel building, venv, pip, or real network.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from zealfie.app import (
    ProductCatalog,
    ProductDescriptor,
    ProductProvenance,
    ProductProvenanceStore,
    ProductUpdateResult,
    SelectionStore,
    UpdateCheckCoordinator,
    UpdateStatus,
    ZeAlfieService,
)
from zealfie.components.model import EntryPointContract
from zealfie.runtime.layout import RuntimeLayout
from zealfie.runtime.state import save_active_state

VALID_SHA = "d4a0f1e2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8"  # 40 hex
OTHER_SHA = "e5b1f2a3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9"  # 40 hex
WHEEL_SHA = "a" * 64

_EP = (EntryPointContract("console_scripts", "zewitness"),)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result(product_id: str, status: UpdateStatus, **kwargs) -> ProductUpdateResult:
    return ProductUpdateResult(product_id=product_id, status=status, **kwargs)


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
    calls: list[tuple[str, str, str]] = []

    def resolve(owner: str, repo: str, ref: str) -> str:
        calls.append((owner, repo, ref))
        return sha

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


class _FakeAbsentRt:
    def status(self):
        from zealfie.runtime.model import RuntimeState, RuntimeStatus

        return RuntimeStatus(state=RuntimeState.ABSENT, runtime_root=Path("/fake"))


# ---------------------------------------------------------------------------
# 1. Initial state
# ---------------------------------------------------------------------------


def test_initial_state_is_not_checked() -> None:
    def check_fn(product_id: str) -> ProductUpdateResult:
        raise AssertionError("check_fn must not be called before a check starts")

    coordinator = UpdateCheckCoordinator(check_fn)

    assert coordinator.state("a").status is UpdateStatus.NOT_CHECKED
    assert coordinator.state("b").status is UpdateStatus.NOT_CHECKED
    assert coordinator.state("a") == ProductUpdateResult.not_checked("a")
    # Nothing has been started, so the coordinator owns no committed state.
    assert coordinator.states() == {}


# ---------------------------------------------------------------------------
# 2. CHECKING transition before completion
# ---------------------------------------------------------------------------


def test_start_transitions_to_checking_before_completion() -> None:
    entered = threading.Event()
    release = threading.Event()

    def check_fn(product_id: str) -> ProductUpdateResult:
        entered.set()
        assert release.wait(timeout=10)
        return _result(product_id, UpdateStatus.UP_TO_DATE)

    coordinator = UpdateCheckCoordinator(check_fn)
    try:
        future = coordinator.start(["p"])[0]
        # The transition to CHECKING is synchronous (before start returns).
        assert coordinator.status("p") is UpdateStatus.CHECKING
        # The check is still in flight — not yet completed.
        assert entered.wait(timeout=5)
        assert not future.done()

        release.set()
        assert future.result(timeout=10).status is UpdateStatus.UP_TO_DATE
        assert coordinator.status("p") is UpdateStatus.UP_TO_DATE
    finally:
        release.set()
        coordinator.shutdown()


# ---------------------------------------------------------------------------
# 3. Successful check updates state and notifies observer
# ---------------------------------------------------------------------------


def test_successful_check_updates_state_and_notifies_observer() -> None:
    observed: list[ProductUpdateResult] = []

    def check_fn(product_id: str) -> ProductUpdateResult:
        return _result(
            product_id, UpdateStatus.UPDATE_AVAILABLE, latest_commit_sha=OTHER_SHA
        )

    coordinator = UpdateCheckCoordinator(check_fn)
    coordinator.add_observer(observed.append)

    result = coordinator.check_one("p")

    assert result.status is UpdateStatus.UPDATE_AVAILABLE
    assert coordinator.status("p") is UpdateStatus.UPDATE_AVAILABLE
    assert coordinator.state("p").latest_commit_sha == OTHER_SHA
    # Observer saw the CHECKING transition then the terminal result.
    assert [r.status for r in observed] == [
        UpdateStatus.CHECKING,
        UpdateStatus.UPDATE_AVAILABLE,
    ]


def test_remove_observer_stops_notifications() -> None:
    observed: list[ProductUpdateResult] = []

    def check_fn(product_id: str) -> ProductUpdateResult:
        return _result(product_id, UpdateStatus.UP_TO_DATE)

    coordinator = UpdateCheckCoordinator(check_fn)
    coordinator.add_observer(observed.append)
    coordinator.remove_observer(observed.append)

    coordinator.check_one("p")
    assert observed == []


# ---------------------------------------------------------------------------
# 4. Resolver failure → CHECK_FAILED
# ---------------------------------------------------------------------------


def test_resolver_failure_is_check_failed() -> None:
    def check_fn(product_id: str) -> ProductUpdateResult:
        return _result(
            product_id, UpdateStatus.CHECK_FAILED, error="network down"
        )

    coordinator = UpdateCheckCoordinator(check_fn)
    result = coordinator.check_one("p")

    assert result.status is UpdateStatus.CHECK_FAILED
    assert "network down" in (result.error or "")
    assert coordinator.status("p") is UpdateStatus.CHECK_FAILED


def test_unexpected_check_exception_becomes_check_failed() -> None:
    def check_fn(product_id: str) -> ProductUpdateResult:
        raise RuntimeError("boom")

    coordinator = UpdateCheckCoordinator(check_fn)
    result = coordinator.check_one("p")

    assert result.status is UpdateStatus.CHECK_FAILED
    assert "boom" in (result.error or "")
    # A product must never be left stuck in CHECKING.
    assert coordinator.status("p") is UpdateStatus.CHECK_FAILED


# ---------------------------------------------------------------------------
# 5. No provenance → PROVENANCE_UNKNOWN (service read path)
# ---------------------------------------------------------------------------


def test_no_provenance_is_provenance_unknown(tmp_path: Path) -> None:
    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=_FakeAbsentRt(),
        selection_store=SelectionStore(path=tmp_path / "sel.toml"),
        provenance_store=ProductProvenanceStore(_fake_layout(tmp_path)),
    )
    resolver = _resolver(VALID_SHA)

    coordinator = UpdateCheckCoordinator(
        lambda product_id: service.check_product_update(
            product_id, resolver=resolver
        )
    )

    result = coordinator.check_one("zewitness")

    assert result.status is UpdateStatus.PROVENANCE_UNKNOWN
    assert coordinator.status("zewitness") is UpdateStatus.PROVENANCE_UNKNOWN
    # No provenance → resolver never invoked.
    assert resolver.calls == []


# ---------------------------------------------------------------------------
# 6. Non-blocking: start returns before resolver completes
# ---------------------------------------------------------------------------


def test_start_returns_before_resolver_completes() -> None:
    entered = threading.Event()
    release = threading.Event()

    def check_fn(product_id: str) -> ProductUpdateResult:
        entered.set()
        assert release.wait(timeout=10)
        return _result(product_id, UpdateStatus.UPDATE_AVAILABLE)

    coordinator = UpdateCheckCoordinator(check_fn)
    try:
        future = coordinator.start(["p"])[0]
        # start() returned; the resolver has begun and is still blocked.
        assert entered.wait(timeout=5)
        assert not release.is_set()
        assert not future.done()

        release.set()
        assert future.result(timeout=10).status is UpdateStatus.UPDATE_AVAILABLE
    finally:
        release.set()
        coordinator.shutdown()


def test_start_after_owned_shutdown_recreates_executor() -> None:
    calls: list[str] = []

    def check_fn(product_id: str) -> ProductUpdateResult:
        calls.append(product_id)
        return _result(product_id, UpdateStatus.UP_TO_DATE)

    coordinator = UpdateCheckCoordinator(check_fn)
    try:
        assert coordinator.start(["before"])[0].result(timeout=10).status is (
            UpdateStatus.UP_TO_DATE
        )
        coordinator.shutdown()

        # A GUI teardown/restart cycle must not leave a product stuck in
        # CHECKING by submitting to a closed owned ThreadPoolExecutor.
        after = coordinator.start(["after"])[0]
        assert after.result(timeout=10).status is UpdateStatus.UP_TO_DATE
        assert coordinator.status("after") is UpdateStatus.UP_TO_DATE
    finally:
        coordinator.shutdown()

    assert calls == ["before", "after"]


# ---------------------------------------------------------------------------
# 7. Out-of-order safety: stale older result cannot overwrite newer
# ---------------------------------------------------------------------------


def test_stale_older_result_cannot_overwrite_newer() -> None:
    # Single worker forces deterministic ordering: the older check (gen 1)
    # runs first and blocks; the newer check (gen 2) is queued behind it.
    events = [threading.Event(), threading.Event()]
    results = [
        _result("p", UpdateStatus.UP_TO_DATE, latest_commit_sha=VALID_SHA),  # gen 1
        _result("p", UpdateStatus.UPDATE_AVAILABLE, latest_commit_sha=OTHER_SHA),  # gen 2
    ]
    state = {"index": 0}

    def check_fn(product_id: str) -> ProductUpdateResult:
        index = state["index"]
        state["index"] += 1
        assert events[index].wait(timeout=10)
        return results[index]

    coordinator = UpdateCheckCoordinator(
        check_fn, executor=ThreadPoolExecutor(max_workers=1)
    )
    try:
        older = coordinator.start(["p"])[0]  # gen 1 → worker starts, blocks
        newer = coordinator.start(["p"])[0]  # gen 2 → queued, generation bumped

        # The older check completes first, but it is now stale (gen 1 < gen 2).
        events[0].set()
        assert older.result(timeout=10).status is UpdateStatus.UP_TO_DATE
        # State is still CHECKING: the newer (gen 2) check is authoritative.
        assert coordinator.status("p") is UpdateStatus.CHECKING

        # The newer check then completes and commits.
        events[1].set()
        assert newer.result(timeout=10).status is UpdateStatus.UPDATE_AVAILABLE
        assert coordinator.status("p") is UpdateStatus.UPDATE_AVAILABLE
        assert coordinator.state("p").latest_commit_sha == OTHER_SHA
    finally:
        for event in events:
            event.set()
        coordinator.shutdown()


# ---------------------------------------------------------------------------
# 8. Read-only invariant: no persisted state mutation
# ---------------------------------------------------------------------------


def test_coordinator_checks_are_read_only(tmp_path: Path) -> None:
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

    resolver = _resolver(OTHER_SHA)
    coordinator = UpdateCheckCoordinator(
        lambda product_id: service.check_product_update(
            product_id, resolver=resolver
        )
    )
    try:
        # Synchronous and non-blocking checks, resolver returns a different
        # SHA (exercises the UPDATE_AVAILABLE branch).
        coordinator.check_one("zewitness")
        futures = coordinator.start(["zewitness"])
        for future in futures:
            future.result(timeout=10)
    finally:
        coordinator.shutdown()

    # Nothing persisted changed.
    assert store.path.read_bytes() == provenance_before
    assert layout.active_pointer.read_bytes() == active_before
    assert sel_path.read_bytes() == selection_before

    # In-memory read model still authoritative and unchanged.
    assert store.product_provenance("zewitness").commit_sha == VALID_SHA
