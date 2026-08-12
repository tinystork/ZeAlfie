"""Tests for M1-2E LOT E.5 — transactional product update API.

Covers :meth:`ZeAlfieService.update_product`: a narrow service-layer
convenience/preflight around the read-only
:meth:`~ZeAlfieService.check_product_update` and the existing transactional
:meth:`~ZeAlfieService.install_product` pipeline.

All tests are FAST — no real GitHub/network, no wheel building, no venv, no
pip, no subprocess.  The actual install step is faked by monkeypatching
``install_product`` (delegation is what we verify here; the existing install
provenance tests cover the real transactional path).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zealfie.app import (
    ProductCatalog,
    ProductDescriptor,
    ProductProvenance,
    ProductProvenanceStore,
    ProductUpdateNotApplicableError,
    ProductUpdateResult,
    SelectionStore,
    UpdateStatus,
    ZeAlfieService,
)
from zealfie.components.model import EntryPointContract
from zealfie.runtime.layout import RuntimeLayout
from zealfie.runtime.model import DeploymentResult
from zealfie.runtime.state import save_active_state
from zealfie.sources import RemoteSource, SourceResolutionError


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
            remote_source=RemoteSource(
                owner="tinystork",
                repo="ZeWitness",
                ref="main",
            ),
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


def _fetcher():
    """A fake fetcher returning a fixed archive payload."""
    return lambda owner, repo, commit_sha: b"fake-archive"


class _FakeAbsentRt:
    """Fake runtime without a layout → provenance is only ever injected."""

    def status(self):
        from zealfie.runtime.model import RuntimeState, RuntimeStatus

        return RuntimeStatus(state=RuntimeState.ABSENT, runtime_root=Path("/fake"))


def _service_with_provenance(
    tmp_path: Path,
    *,
    entries: list[ProductProvenance] | None = None,
) -> tuple[ZeAlfieService, ProductProvenanceStore, SelectionStore, Path]:
    """Build a service with an explicit provenance store and selection file.

    When *entries* is non-empty, the provenance is recorded for a synthetic
    active slot and the active pointer is written so readback succeeds.
    """
    layout = _fake_layout(tmp_path)
    store = ProductProvenanceStore(layout)
    if entries:
        store.record("rt-abc123", entries)
        save_active_state(layout.active_pointer, "rt-abc123", None)

    sel_path = tmp_path / "desired-products.toml"
    selection = SelectionStore(path=sel_path)
    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=_FakeAbsentRt(),
        selection_store=selection,
        provenance_store=store,
    )
    return service, store, selection, sel_path


# ---------------------------------------------------------------------------
# 1. UPDATE_AVAILABLE → delegate exactly once with identical args
# ---------------------------------------------------------------------------


def test_update_available_delegates_to_install_product_once(
    tmp_path: Path, monkeypatch,
) -> None:
    service, store, _selection, _sel_path = _service_with_provenance(
        tmp_path, entries=[_prov(commit_sha=VALID_SHA)]
    )

    sentinel = DeploymentResult(success=True, active_slot_id="slot-9")
    calls: list[dict] = []

    def fake_install(product_id, *, resolver, fetcher, work_root,
                     dependency_wheelhouse=None, probe_distribution=None,
                     progress_callback=None):
        calls.append({
            "product_id": product_id,
            "resolver": resolver,
            "fetcher": fetcher,
            "work_root": work_root,
            "dependency_wheelhouse": dependency_wheelhouse,
            "probe_distribution": probe_distribution,
            "progress_callback": progress_callback,
        })
        return sentinel

    monkeypatch.setattr(service, "install_product", fake_install)

    resolver = _resolver(OTHER_SHA)
    fetcher = _fetcher()
    work_root = tmp_path / "work"
    wheelhouse = tmp_path / "wheelhouse"
    probe = _probe
    progress_events: list = []

    def progress_callback(event) -> None:
        progress_events.append(event)

    result = service.update_product(
        "zewitness",
        resolver=resolver,
        fetcher=fetcher,
        work_root=work_root,
        dependency_wheelhouse=wheelhouse,
        probe_distribution=probe,
        progress_callback=progress_callback,
    )

    # The exact DeploymentResult from install_product is returned verbatim.
    assert result is sentinel

    # install_product called exactly once, with identical injected args.
    assert len(calls) == 1
    call = calls[0]
    assert call["product_id"] == "zewitness"
    assert call["resolver"] is resolver
    assert call["fetcher"] is fetcher
    assert call["work_root"] == work_root
    assert call["dependency_wheelhouse"] == wheelhouse
    assert call["probe_distribution"] is probe
    assert call["progress_callback"] is progress_callback

    # Preflight used the resolver exactly once via check_product_update.
    assert resolver.calls == [("tinystork", "ZeWitness", "main")]


def _probe(distribution_name: str) -> dict:
    return {"installed": False, "version": None, "entry_points": []}


# ---------------------------------------------------------------------------
# 2. UP_TO_DATE → no install, no mutation, clear non-mutating outcome
# ---------------------------------------------------------------------------


def test_up_to_date_raises_and_does_not_install(
    tmp_path: Path, monkeypatch,
) -> None:
    service, store, _selection, _sel_path = _service_with_provenance(
        tmp_path, entries=[_prov(commit_sha=VALID_SHA)]
    )
    provenance_before = store.path.read_bytes()

    install_calls: list = []
    monkeypatch.setattr(
        service,
        "install_product",
        lambda *a, **k: install_calls.append((a, k)) or DeploymentResult(success=True),
    )

    resolver = _resolver(VALID_SHA)  # same commit → UP_TO_DATE
    with pytest.raises(ProductUpdateNotApplicableError) as exc_info:
        service.update_product(
            "zewitness",
            resolver=resolver,
            fetcher=_fetcher(),
            work_root=tmp_path / "work",
        )

    assert exc_info.value.status is UpdateStatus.UP_TO_DATE
    assert "up to date" in str(exc_info.value)
    assert "zewitness" in str(exc_info.value)
    # No install/fetch/apply on the non-applicable path.
    assert install_calls == []
    # No mutation to persisted provenance.
    assert store.path.read_bytes() == provenance_before


# ---------------------------------------------------------------------------
# 3. PROVENANCE_UNKNOWN → no mutation, clear reason
# ---------------------------------------------------------------------------


def test_provenance_unknown_raises_clear_reason_no_mutation(
    tmp_path: Path, monkeypatch,
) -> None:
    # Empty store: no active provenance.
    service, store, _selection, sel_path = _service_with_provenance(tmp_path)

    install_calls: list = []
    monkeypatch.setattr(
        service,
        "install_product",
        lambda *a, **k: install_calls.append((a, k)) or DeploymentResult(success=True),
    )

    resolver = _resolver(VALID_SHA)
    with pytest.raises(ProductUpdateNotApplicableError) as exc_info:
        service.update_product(
            "zewitness",
            resolver=resolver,
            fetcher=_fetcher(),
            work_root=tmp_path / "work",
        )

    assert exc_info.value.status is UpdateStatus.PROVENANCE_UNKNOWN
    assert "no active installed provenance" in str(exc_info.value)
    # No resolver call (nothing to resolve), no install.
    assert resolver.calls == []
    assert install_calls == []
    # Nothing persisted.
    assert not sel_path.exists()


# ---------------------------------------------------------------------------
# 4. CHECK_FAILED → no mutation, carries/mentions check error
# ---------------------------------------------------------------------------


def test_check_failed_raises_with_check_error_no_mutation(
    tmp_path: Path, monkeypatch,
) -> None:
    service, store, _selection, sel_path = _service_with_provenance(
        tmp_path, entries=[_prov(commit_sha=VALID_SHA)]
    )
    provenance_before = store.path.read_bytes()

    install_calls: list = []
    monkeypatch.setattr(
        service,
        "install_product",
        lambda *a, **k: install_calls.append((a, k)) or DeploymentResult(success=True),
    )

    resolver = _failing_resolver("network down")
    with pytest.raises(ProductUpdateNotApplicableError) as exc_info:
        service.update_product(
            "zewitness",
            resolver=resolver,
            fetcher=_fetcher(),
            work_root=tmp_path / "work",
        )

    assert exc_info.value.status is UpdateStatus.CHECK_FAILED
    assert "network down" in str(exc_info.value)
    assert exc_info.value.result.error is not None
    assert "network down" in exc_info.value.result.error
    # No install and no mutation.
    assert install_calls == []
    assert store.path.read_bytes() == provenance_before
    assert not sel_path.exists()


# ---------------------------------------------------------------------------
# 5. Delegation uses install pipeline, never direct apply_deployment_plan
# ---------------------------------------------------------------------------


def test_update_uses_install_pipeline_not_direct_apply(
    tmp_path: Path, monkeypatch,
) -> None:
    service, _store, _selection, _sel_path = _service_with_provenance(
        tmp_path, entries=[_prov(commit_sha=VALID_SHA)]
    )

    sentinel = DeploymentResult(success=True, active_slot_id="slot-9")

    def fake_install(*args, **kwargs):
        return sentinel

    monkeypatch.setattr(service, "install_product", fake_install)

    # The new method must not reach the deployment engine directly.
    import zealfie.app.service as service_mod

    apply_calls: list = []
    monkeypatch.setattr(
        service_mod,
        "apply_deployment_plan",
        lambda *a, **k: apply_calls.append((a, k)) or sentinel,
    )

    result = service.update_product(
        "zewitness",
        resolver=_resolver(OTHER_SHA),
        fetcher=_fetcher(),
        work_root=tmp_path / "work",
    )

    assert result is sentinel
    assert apply_calls == []


# ---------------------------------------------------------------------------
# 6. install_product raising during the actual update propagates unwrapped
# ---------------------------------------------------------------------------


def test_install_product_exception_propagates_unwrapped_no_mutation(
    tmp_path: Path, monkeypatch,
) -> None:
    service, store, _selection, _sel_path = _service_with_provenance(
        tmp_path, entries=[_prov(commit_sha=VALID_SHA)]
    )
    provenance_before = store.path.read_bytes()

    class _Boom(RuntimeError):
        pass

    def fake_install(*args, **kwargs):
        raise _Boom("simulated apply failure")

    monkeypatch.setattr(service, "install_product", fake_install)

    with pytest.raises(_Boom) as exc_info:
        service.update_product(
            "zewitness",
            resolver=_resolver(OTHER_SHA),
            fetcher=_fetcher(),
            work_root=tmp_path / "work",
        )

    # Exact exception type and message — not wrapped by update_product.
    assert str(exc_info.value) == "simulated apply failure"
    # Preflight itself did not mutate unrelated stores.
    assert store.path.read_bytes() == provenance_before


# ---------------------------------------------------------------------------
# 7. Every non-applicable status raises a human message (never a bare enum)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        UpdateStatus.UP_TO_DATE,
        UpdateStatus.PROVENANCE_UNKNOWN,
        UpdateStatus.CHECK_FAILED,
        UpdateStatus.NOT_CHECKED,
        UpdateStatus.CHECKING,
    ],
)
def test_non_update_available_statuses_raise_human_message(
    tmp_path: Path, monkeypatch, status: UpdateStatus,
) -> None:
    service, _store, _selection, _sel_path = _service_with_provenance(tmp_path)

    preflight = ProductUpdateResult(
        product_id="zewitness",
        status=status,
        error=("network down" if status is UpdateStatus.CHECK_FAILED else None),
    )
    monkeypatch.setattr(
        service, "check_product_update", lambda *a, **k: preflight
    )

    install_calls: list = []
    monkeypatch.setattr(
        service,
        "install_product",
        lambda *a, **k: install_calls.append((a, k)) or DeploymentResult(success=True),
    )

    resolver = _resolver(VALID_SHA)
    with pytest.raises(ProductUpdateNotApplicableError) as exc_info:
        service.update_product(
            "zewitness",
            resolver=resolver,
            fetcher=_fetcher(),
            work_root=tmp_path / "work",
        )

    message = str(exc_info.value)
    # Never a bare enum value, and always names the product.
    assert message != status.value
    assert "zewitness" in message
    assert exc_info.value.result is preflight
    assert exc_info.value.status is status
    assert resolver.calls == []
    assert install_calls == []


# ---------------------------------------------------------------------------
# Unknown product → PROVENANCE_UNKNOWN, no invented provenance
# ---------------------------------------------------------------------------


def test_unknown_product_id_raises_provenance_unknown_no_invention(
    tmp_path: Path, monkeypatch,
) -> None:
    service, _store, _selection, sel_path = _service_with_provenance(tmp_path)

    install_calls: list = []
    monkeypatch.setattr(
        service,
        "install_product",
        lambda *a, **k: install_calls.append((a, k)) or DeploymentResult(success=True),
    )

    resolver = _resolver(VALID_SHA)
    with pytest.raises(ProductUpdateNotApplicableError) as exc_info:
        service.update_product(
            "ghost",
            resolver=resolver,
            fetcher=_fetcher(),
            work_root=tmp_path / "work",
        )

    assert exc_info.value.status is UpdateStatus.PROVENANCE_UNKNOWN
    assert resolver.calls == []
    assert install_calls == []
    assert not sel_path.exists()
