"""ZA-M1-3A.3 — bounded slot retention GC regression tests (H + I).

TEST H (retention regressions):
* multi-activation rt-A → rt-B → rt-C → rt-D through the REAL service
  transaction path (fake activation engine, real slot dirs + stores +
  auto-GC): at the end only ACTIVE + PREVIOUS persist; historical slots
  are reclaimed by the bounded auto-GC; no store keeps an orphan key; a
  re-run plan is READY (never BLOCKED / REPAIR_REQUIRED);
* ``auto_gc=False``: history is retained until the manual GC commands
  reclaim it, and historical slots classify PRUNABLE_CLEAN_METADATA —
  never KEEP;
* cleanup failures (rmtree / metadata write) never change the
  transaction outcome: the transaction stays successful, the active
  runtime stays READY, the previous (rollback) slot stays usable, and
  the failure is observable (log + best-effort GC result);
* ACTIVE/PREVIOUS are never eligible even with a forged plan carrying
  destructive metadata_actions (defence in depth).

TEST I (witness reproduction): the Windows witness state (historical
slots referenced by provenance+lock, by all three stores, and by
accelerated-metadata only).  Baseline (captured BEFORE the fix — see
the mission report): the first two classified REFERENCED (KEEP forever,
~6.8 GB).  Post-fix: every historical slot is PRUNABLE_CLEAN_METADATA
with the exact per-store metadata_actions, REFERENCED is never
produced, and a full gc leaves zero orphan keys with a clean READY
re-plan.

Hermetic: synthetic catalog, fake prepared artifacts, fake activation
engine — no real venv, no wheels, no network (FAST).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

import zealfie.app.service as svc_mod
import zealfie.runtime.gc as gc_module
from zealfie.app import (
    PreparedProductArtifact,
    ProductCatalog,
    ProductDescriptor,
    SelectionStore,
    ZeAlfieService,
)
from zealfie.components.model import EntryPointContract
from zealfie.products.policy import ProductPolicyStore
from zealfie.releases.model import VerifiedArtifact
from zealfie.runtime import (
    RuntimeLayout,
    apply_gc_plan,
    build_gc_plan,
)
from zealfie.runtime.gc import (
    CLEAN_ACCELERATED_METADATA,
    CLEAN_INSTALLED_LOCK,
    CLEAN_PRODUCT_PROVENANCE,
    GcStatus,
    SlotCategory,
)
from zealfie.runtime.model import DeploymentResult, RuntimeState, RuntimeStatus
from zealfie.runtime.state import load_active_state, save_active_state
from zealfie.sources import RemoteSource, ResolvedSource

# -- slot ids (12 hex chars after "rt-") -------------------------------------
A = "rt-111111111111"
B = "rt-222222222222"
C = "rt-333333333333"
D = "rt-444444444444"

W_ACTIVE = "rt-aaaaaaaaaaaa"
W_PREVIOUS = "rt-bbbbbbbbbbbb"
W_OLD_LOCK_PROV = "rt-cccccccccccc"        # installed-lock + provenance
W_OLD_ALL_THREE = "rt-dddddddddddd"        # all three stores
W_OLD_ACCEL_ONLY = "rt-eeeeeeeeeeee"       # accelerated-metadata only

STORE_FILENAMES = (
    "installed-lock.json",
    "product-provenance.json",
    "accelerated-metadata.json",
)

DESTRUCTIVE = (SlotCategory.PRUNABLE, SlotCategory.PRUNABLE_CLEAN_METADATA)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_active(state_dir: Path, active: str, previous: str | None) -> None:
    payload: dict = {"schema_version": 1, "active_slot": active}
    if previous is not None:
        payload["previous_slot"] = previous
    (state_dir / "active.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def _merge_store(state_dir: Path, filename: str, slot_id: str, entry: dict) -> None:
    """Record *entry* for *slot_id* in one store (read-modify-write)."""
    path = state_dir / filename
    payload: dict = {"schema_version": 1, "slots": {}}
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
    slots = payload.setdefault("slots", {})
    slots[slot_id] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _make_slot(slots_root: Path, slot_id: str, payload: bytes = b"x" * 100) -> Path:
    d = slots_root / slot_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "payload.bin").write_bytes(payload)
    return d


def _lock_entry(primary: str) -> dict:
    return {"primary_names": [primary], "dependencies": {}}


def _prov_entry(product: str) -> dict:
    return {
        product: {
            "version": "1.0.0",
            "source_owner": "o",
            "source_repo": "r",
            "requested_ref": "main",
            "commit_sha": "a" * 40,
            "wheel_sha256": "b" * 64,
        }
    }


def _accel_entry() -> dict:
    return {"backend": "NVIDIA_CUDA", "variants": []}


def _entry(plan, slot_id: str):
    for entry in plan.slots:
        if entry.slot_id == slot_id:
            return entry
    raise AssertionError(f"{slot_id!r} not present in plan slots")


def _prunable_ids(plan) -> set[str]:
    return {
        entry.slot_id for entry in plan.slots if entry.category in DESTRUCTIVE
    }


def _store_slot_keys(state_dir: Path, filename: str) -> set[str]:
    path = state_dir / filename
    if not path.is_file():
        return set()
    payload = json.loads(path.read_text(encoding="utf-8"))
    return set(payload.get("slots", {}))


def _assert_no_orphan_store_keys(layout_or_root) -> None:
    """Zero orphan metadata keys: every store key has a directory on disk."""
    layout = (
        layout_or_root
        if isinstance(layout_or_root, RuntimeLayout)
        else RuntimeLayout(root=layout_or_root)
    )
    on_disk = {p.name for p in layout.slots.iterdir()} if layout.slots.is_dir() else set()
    for filename in STORE_FILENAMES:
        keys = _store_slot_keys(layout.state_dir, filename)
        orphan = keys - on_disk
        assert orphan == set(), f"{filename} has orphan keys {sorted(orphan)}"


def _witness_fixture(tmp_path: Path) -> Path:
    """The Windows witness state: ACTIVE + PREVIOUS + three historical
    slots referenced by {lock+prov}, {all three stores}, {accel only}."""
    root = tmp_path / "rt"
    state_dir = root / "state"
    slots_root = root / "slots"
    state_dir.mkdir(parents=True)
    slots_root.mkdir()
    _write_active(state_dir, W_ACTIVE, W_PREVIOUS)
    for sid in (W_ACTIVE, W_PREVIOUS, W_OLD_LOCK_PROV, W_OLD_ALL_THREE, W_OLD_ACCEL_ONLY):
        _make_slot(slots_root, sid)
    _merge_store(state_dir, "installed-lock.json", W_ACTIVE, _lock_entry("zemosaic"))
    _merge_store(state_dir, "installed-lock.json", W_OLD_LOCK_PROV, _lock_entry("zesolver"))
    _merge_store(state_dir, "installed-lock.json", W_OLD_ALL_THREE, _lock_entry("zeold"))
    _merge_store(state_dir, "product-provenance.json", W_ACTIVE, _prov_entry("zemosaic"))
    _merge_store(state_dir, "product-provenance.json", W_OLD_LOCK_PROV, _prov_entry("zesolver"))
    _merge_store(state_dir, "product-provenance.json", W_OLD_ALL_THREE, _prov_entry("zeold"))
    _merge_store(state_dir, "accelerated-metadata.json", W_PREVIOUS, _accel_entry())
    _merge_store(state_dir, "accelerated-metadata.json", W_OLD_ALL_THREE, _accel_entry())
    _merge_store(state_dir, "accelerated-metadata.json", W_OLD_ACCEL_ONLY, _accel_entry())
    return root


# ---------------------------------------------------------------------------
# Service-harness helpers (fake activation engine + fake prepared artifacts)
# ---------------------------------------------------------------------------


class _FakeRuntimeWithLayout:
    """Runtime double with a REAL layout but an ABSENT status, so the
    deployment planner never probes slot pythons.  The fake activation
    engine below owns the real filesystem effects."""

    def __init__(self, layout: RuntimeLayout) -> None:
        self.layout = layout

    def status(self) -> RuntimeStatus:
        return RuntimeStatus(
            state=RuntimeState.ABSENT, runtime_root=self.layout.root
        )


class _FakeActivationEngine:
    """Simulates the deployment engine: creates a real slot directory,
    records an accelerated-metadata entry for it, switches the active
    pointer (new active, old previous) and returns success."""

    def __init__(self, layout: RuntimeLayout, slot_ids) -> None:
        self.layout = layout
        self._ids = list(slot_ids)

    def __call__(self, plan, *, registry, runtime, **kwargs) -> DeploymentResult:
        status = load_active_state(
            self.layout.active_pointer, layout_root=self.layout.root
        )
        old = (
            status.active_slot_id
            if status.state == RuntimeState.READY
            else None
        )
        new = self._ids.pop(0)
        _make_slot(self.layout.slots, new)
        save_active_state(self.layout.active_pointer, new, old)
        _merge_store(
            self.layout.state_dir, "accelerated-metadata.json", new, _accel_entry()
        )
        return DeploymentResult(
            success=True, active_slot_id=new, previous_slot_id=old
        )


def _catalog() -> ProductCatalog:
    return ProductCatalog((
        ProductDescriptor(
            product_id="zewitness",
            display_name="ZeWitness",
            distribution_name="zealfie-witness",
            launch_entry_points=(EntryPointContract("console_scripts", "zewitness"),),
            remote_source=RemoteSource(owner="tinystork", repo="ZeWitness", ref="main"),
        ),
    ))


def _make_ppa(product_id: str = "zewitness") -> PreparedProductArtifact:
    remote = RemoteSource(owner="tinystork", repo="ZeWitness", ref="main")
    resolved = ResolvedSource(source=remote, commit_sha="a" * 40)
    wheel_path = Path(f"/fake/{product_id}-0.0.1.whl")
    return PreparedProductArtifact(
        product_id=product_id,
        component_id=product_id,
        resolved_source=resolved,
        wheel_path=wheel_path,
        verified_artifact=VerifiedArtifact(
            component_id=product_id,
            version="0.0.1",
            path=wheel_path,
            size=100,
            sha256="b" * 64,
            distribution_name="zealfie-witness",
            wheel_version="0.0.1",
        ),
    )


def _make_service(
    tmp_path: Path, layout: RuntimeLayout, *, auto_gc: bool = True
) -> ZeAlfieService:
    return ZeAlfieService(
        catalog=_catalog(),
        runtime=_FakeRuntimeWithLayout(layout),
        selection_store=SelectionStore(path=tmp_path / "desired-products.toml"),
        policy_store=ProductPolicyStore(path=tmp_path / "policy.toml"),
        auto_gc=auto_gc,
    )


def _install_n(
    service: ZeAlfieService, monkeypatch, layout: RuntimeLayout,
    slot_ids, *, count: int,
) -> None:
    """Run *count* successful installs through the real service path with
    the fake activation engine, one activation per call."""
    engine = _FakeActivationEngine(layout, slot_ids)
    monkeypatch.setattr(svc_mod, "apply_deployment_plan", engine)
    ppa = _make_ppa()
    for _ in range(count):
        result = service.install_prepared_product_deployment([ppa])
        assert result.success is True, result.reason


# ---------------------------------------------------------------------------
# TEST H — retention regressions
# ---------------------------------------------------------------------------


def test_multi_activation_auto_gc_retains_only_active_and_previous(
    tmp_path, monkeypatch,
):
    """rt-A → rt-B → rt-C → rt-D through the real transaction path.

    At the end ACTIVE=rt-D and PREVIOUS=rt-C; rt-A / rt-B were already
    reclaimed by the bounded auto-GC (never KEEP-kept because of the
    historical stores); no store keeps an orphan key; a re-run plan is
    READY with nothing prunable and never BLOCKED.
    """
    layout = RuntimeLayout(root=tmp_path / "rt")
    service = _make_service(tmp_path, layout)
    _install_n(service, monkeypatch, layout, [A, B, C, D], count=4)

    status = load_active_state(layout.active_pointer, layout_root=layout.root)
    assert status.state is RuntimeState.READY
    assert status.active_slot_id == D
    assert status.previous_slot_id == C
    assert {p.name for p in layout.slots.iterdir()} == {C, D}

    _assert_no_orphan_store_keys(layout)

    plan = build_gc_plan(layout.root)
    assert plan.status is GcStatus.READY
    assert plan.blocking_reasons == ()
    assert _prunable_ids(plan) == set()
    # the retained stores describe exactly the surviving slots
    assert _store_slot_keys(layout.state_dir, "installed-lock.json") == {C, D}
    assert _store_slot_keys(layout.state_dir, "product-provenance.json") == {C, D}
    assert _store_slot_keys(layout.state_dir, "accelerated-metadata.json") == {C, D}


def test_auto_gc_disabled_keeps_history_as_prunable_clean_metadata(
    tmp_path, monkeypatch,
):
    """auto_gc=False: the historical slot survives on disk AND in the
    stores, and classifies PRUNABLE_CLEAN_METADATA (never KEEP); the
    manual gc then reclaims it cleanly."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    service = _make_service(tmp_path, layout, auto_gc=False)
    _install_n(service, monkeypatch, layout, [A, B, C], count=3)

    assert {p.name for p in layout.slots.iterdir()} == {A, B, C}
    plan = build_gc_plan(layout.root)
    assert plan.status is GcStatus.READY
    a_entry = _entry(plan, A)
    assert a_entry.category is SlotCategory.PRUNABLE_CLEAN_METADATA
    assert a_entry.metadata_actions == (
        CLEAN_INSTALLED_LOCK,
        CLEAN_PRODUCT_PROVENANCE,
        CLEAN_ACCELERATED_METADATA,
    )
    assert _prunable_ids(plan) == {A}  # B is PREVIOUS, C is ACTIVE

    # the manual gc (runtime gc) reclaims the historical slot
    result = apply_gc_plan(layout.root, plan)
    assert A in result.deleted_slots
    assert result.errors == ()
    assert not (layout.slots / A).exists()
    _assert_no_orphan_store_keys(layout)
    plan2 = build_gc_plan(layout.root)
    assert plan2.status is GcStatus.READY
    assert _prunable_ids(plan2) == set()


def test_cleanup_rmtree_failure_keeps_transaction_successful(
    tmp_path, monkeypatch, caplog,
):
    """A per-slot rmtree failure inside the bounded auto-GC never changes
    the transaction outcome: the install stays successful, the active
    runtime stays READY, the previous (rollback) slot stays usable, and
    the failure is observable in the log and the best-effort GC result."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    service = _make_service(tmp_path, layout)
    _install_n(service, monkeypatch, layout, [A, B, C], count=2)

    real_rmtree = gc_module.shutil.rmtree

    def flaky_rmtree(path, **kwargs):
        if Path(path).name == A:
            raise OSError("simulated file-locked cleanup failure")
        return real_rmtree(path, **kwargs)

    monkeypatch.setattr(gc_module.shutil, "rmtree", flaky_rmtree)
    with caplog.at_level(logging.WARNING, logger="zealfie.app.service"):
        result = service.install_prepared_product_deployment([_make_ppa()])
    assert result.success is True

    status = load_active_state(layout.active_pointer, layout_root=layout.root)
    assert status.state is RuntimeState.READY
    assert status.active_slot_id == C
    assert status.previous_slot_id == B
    assert (layout.slots / B).is_dir()  # rollback target usable
    assert (layout.slots / A).is_dir()  # cleanup incomplete, non-destructive
    assert "auto-GC" in caplog.text  # failure observed in the log

    # observable in the best-effort result too (retry reports the error)
    gc_result = service._runtime_gc_best_effort()
    assert gc_result is not None
    assert any(A in e for e in gc_result.errors)
    assert A not in gc_result.deleted_slots
    assert (layout.slots / A).is_dir()


def test_cleanup_metadata_write_failure_keeps_transaction_successful(
    tmp_path, monkeypatch, caplog,
):
    """A metadata-store write failure inside the bounded auto-GC preserves
    the slot directory this round (purge-before-delete order): the
    transaction stays successful and the state self-heals — the next plan
    still sees the slot as PRUNABLE_CLEAN_METADATA for the remaining
    stores, never REPAIR_REQUIRED."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    service = _make_service(tmp_path, layout)
    _install_n(service, monkeypatch, layout, [A, B, C], count=2)

    real_replace = gc_module.os.replace

    def failing_replace(src, dst):
        if str(dst).endswith("product-provenance.json"):
            raise OSError("simulated metadata write failure")
        return real_replace(src, dst)

    monkeypatch.setattr(gc_module.os, "replace", failing_replace)
    with caplog.at_level(logging.WARNING, logger="zealfie.app.service"):
        result = service.install_prepared_product_deployment([_make_ppa()])
    assert result.success is True

    status = load_active_state(layout.active_pointer, layout_root=layout.root)
    assert status.state is RuntimeState.READY
    assert status.active_slot_id == C
    assert status.previous_slot_id == B
    assert (layout.slots / B).is_dir()
    assert (layout.slots / A).is_dir()  # purge refused → dir preserved
    assert "auto-GC" in caplog.text

    # self-healing: A is still referenced by provenance + accelerated
    # (installed-lock was already purged in the documented order), the
    # plan stays READY and A stays prunable — never REPAIR_REQUIRED.
    plan = build_gc_plan(layout.root)
    assert plan.status is GcStatus.READY
    a_entry = _entry(plan, A)
    assert a_entry.category is SlotCategory.PRUNABLE_CLEAN_METADATA
    assert a_entry.metadata_actions == (
        CLEAN_PRODUCT_PROVENANCE,
        CLEAN_ACCELERATED_METADATA,
    )


def test_forged_plan_cannot_purge_or_delete_active_or_previous(tmp_path):
    """Defence in depth: a forged plan (matching fingerprint) marking
    ACTIVE and PREVIOUS as PRUNABLE_CLEAN_METADATA with all three
    metadata_actions never purges their store entries nor deletes their
    directories."""
    root = _witness_fixture(tmp_path)
    plan = build_gc_plan(root)
    forged_slots = []
    for entry in plan.slots:
        if entry.slot_id in (W_ACTIVE, W_PREVIOUS):
            forged_slots.append(
                type(entry)(
                    slot_id=entry.slot_id,
                    path=entry.path,
                    category=SlotCategory.PRUNABLE_CLEAN_METADATA,  # lie
                    reason="forged",
                    references=entry.references,
                    metadata_actions=(
                        CLEAN_INSTALLED_LOCK,
                        CLEAN_PRODUCT_PROVENANCE,
                        CLEAN_ACCELERATED_METADATA,
                    ),
                    estimated_bytes=entry.estimated_bytes,
                )
            )
        else:
            forged_slots.append(entry)
    forged = type(plan)(
        runtime_root=plan.runtime_root,
        status=plan.status,
        active_slot_id=plan.active_slot_id,
        previous_slot_id=plan.previous_slot_id,
        slots=tuple(forged_slots),
        total_recoverable_bytes=plan.total_recoverable_bytes,
        blocking_reasons=plan.blocking_reasons,
        stale_metadata=plan.stale_metadata,
        state_fingerprint=plan.state_fingerprint,
    )
    result = apply_gc_plan(root, forged)
    assert W_ACTIVE not in result.deleted_slots
    assert W_PREVIOUS not in result.deleted_slots
    assert (root / "slots" / W_ACTIVE).is_dir()
    assert (root / "slots" / W_PREVIOUS).is_dir()
    # their store entries are untouched
    assert W_ACTIVE in _store_slot_keys(root / "state", "installed-lock.json")
    assert W_ACTIVE in _store_slot_keys(root / "state", "product-provenance.json")
    assert W_PREVIOUS in _store_slot_keys(root / "state", "accelerated-metadata.json")
    # the honest historical slots are still reclaimed
    assert set(result.deleted_slots) == {
        W_OLD_LOCK_PROV, W_OLD_ALL_THREE, W_OLD_ACCEL_ONLY,
    }


# ---------------------------------------------------------------------------
# TEST I — witness reproduction
# ---------------------------------------------------------------------------


def test_witness_reproduction_historical_stores_prunable(tmp_path):
    """The Windows witness (~6.8 GB stuck in REFERENCED slots).

    Baseline (captured BEFORE the fix — see the mission report):

        rt-cccccccccccc -> REFERENCED (installed-lock + provenance)
        rt-dddddddddddd -> REFERENCED (all three stores)
        rt-eeeeeeeeeeee -> PRUNABLE_CLEAN_METADATA (accelerated only)

    Post-fix: every historical slot classifies PRUNABLE_CLEAN_METADATA
    with the exact per-store metadata_actions, REFERENCED is never
    produced, and a full gc leaves zero orphan keys with a clean READY
    re-plan.
    """
    root = _witness_fixture(tmp_path)
    plan = build_gc_plan(root)
    assert plan.status is GcStatus.READY
    for entry in plan.slots:
        assert entry.category is not SlotCategory.REFERENCED

    lock_prov = _entry(plan, W_OLD_LOCK_PROV)
    assert lock_prov.category is SlotCategory.PRUNABLE_CLEAN_METADATA
    assert lock_prov.metadata_actions == (
        CLEAN_INSTALLED_LOCK,
        CLEAN_PRODUCT_PROVENANCE,
    )
    assert lock_prov.metadata_action is None  # legacy scalar: multi-store

    all_three = _entry(plan, W_OLD_ALL_THREE)
    assert all_three.category is SlotCategory.PRUNABLE_CLEAN_METADATA
    assert all_three.metadata_actions == (
        CLEAN_INSTALLED_LOCK,
        CLEAN_PRODUCT_PROVENANCE,
        CLEAN_ACCELERATED_METADATA,
    )

    accel_only = _entry(plan, W_OLD_ACCEL_ONLY)
    assert accel_only.category is SlotCategory.PRUNABLE_CLEAN_METADATA
    assert accel_only.metadata_actions == (CLEAN_ACCELERATED_METADATA,)
    assert accel_only.metadata_action == CLEAN_ACCELERATED_METADATA

    # ACTIVE/PREVIOUS stay protected
    assert _entry(plan, W_ACTIVE).category is SlotCategory.ACTIVE
    assert _entry(plan, W_PREVIOUS).category is SlotCategory.PREVIOUS
    assert _prunable_ids(plan) == {
        W_OLD_LOCK_PROV, W_OLD_ALL_THREE, W_OLD_ACCEL_ONLY,
    }

    result = apply_gc_plan(root, plan)
    assert set(result.deleted_slots) == {
        W_OLD_LOCK_PROV, W_OLD_ALL_THREE, W_OLD_ACCEL_ONLY,
    }
    assert result.errors == ()

    # zero orphan keys: every store key has a directory on disk
    _assert_no_orphan_store_keys(root)

    # re-run: READY, nothing prunable, never BLOCKED
    plan2 = build_gc_plan(root)
    assert plan2.status is GcStatus.READY
    assert plan2.blocking_reasons == ()
    assert _prunable_ids(plan2) == set()
