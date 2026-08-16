"""Safe runtime GC tests (ZA-M1-2K Phase B+C).

Covers the §19 decision matrix (26 cases), adversarial path-safety,
defence-in-depth against forged plans, fingerprint determinism,
deterministic deletion order, and the §20 synthetic E2E.

Case → test mapping (§19):
  1  test_case01_active_slot_kept
  2  test_case02_previous_slot_protected_by_default
  3  test_case03_referenced_slots_protected
  4  test_case04_true_orphan_prunable
  5  test_case05_failed_candidate_prunable_clean_metadata
  6  test_case06_unknown_reference_blocks
  7  test_case07_malformed_active_json_blocks
  8  test_case08_active_slot_absent_blocks
  9  test_case09_stale_accelerated_metadata_blocks
  10 test_case10_stale_installed_lock_blocks
  11 test_case11_absent_slot_with_stale_metadata_blocks
  12 test_case12_stale_plan_rejected
  13 test_case13_active_change_rejected
  14 test_case14_previous_change_rejected
  15 test_case15_lock_change_rejected
  16 test_case16_metadata_change_rejected
  17 test_case17_symlink_slot_entry_blocks
  18 test_case18_path_traversal_rejected
  19 test_case19_partial_deletion_failure_preserves_active_and_continues
  20 test_case20_metadata_write_failure_preserves_runtime
  21 cancellation: N/A (documented — the apply engine has no cancellation
     contract; there are no checkpoints by design, see implementation
     report)
  22 test_case22_empty_prune_set_noop_success
  23 test_case23_reclaimed_bytes_accounting
  24 test_case24_cli_gc_plan_read_only
  25 test_case25_cli_gc_blocked_returns_nonzero
  26 test_case26_active_runtime_still_launchable_after_synthetic_gc

Additional:
  - §20 synthetic E2E: test_synthetic_e2e_full_gc_cycle
  - defence in depth (forged plan): test_forged_plan_cannot_delete_protected
  - fingerprint determinism: test_fingerprint_deterministic
  - deletion order determinism: test_deletion_order_deterministic
  - adversarial _validate_slot_entry: test_validate_slot_entry_adversarial
  - corrupt store records / wrong schema / invalid keys: test_store_corruption_matrix
  - absent active.json: test_absent_active_json_blocks
  - CLI gc success: test_cli_gc_applies_and_reports
  - CLI gc stale: test_cli_gc_stale_exits_2
  - parser wiring: test_gc_plan_in_parser
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from io import StringIO
from pathlib import Path

import pytest

import zealfie.cli as cli
from zealfie.runtime import gc as gc_module
from zealfie.runtime.gc import (
    GcPlan,
    GcSlotEntry,
    GcStatus,
    SlotCategory,
    _safe_delete_slot,
    _validate_slot_entry,
    apply_gc_plan,
    build_gc_plan,
)
from zealfie.runtime.probe import probe_runtime_python_version

# -- slot ids (12 hex chars after "rt-") -------------------------------------
ACTIVE = "rt-aaaaaaaaaaaa"
PREVIOUS = "rt-bbbbbbbbbbbb"
REF_OLD = "rt-cccccccccccc"     # referenced by installed-lock + provenance
FAILED_GPU = "rt-dddddddddddd"  # referenced only by accelerated-metadata
ORPHAN = "rt-eeeeeeeeeeee"      # referenced nowhere

RECORD_NAMES = (
    "active.json",
    "installed-lock.json",
    "product-provenance.json",
    "accelerated-metadata.json",
)

DESTRUCTIVE = (SlotCategory.PRUNABLE, SlotCategory.PRUNABLE_CLEAN_METADATA)


# ---------------------------------------------------------------------------
# Fixture helpers (all under tmp_path — never the real witness)
# ---------------------------------------------------------------------------


def _slot_python_path(slot_dir: Path) -> Path:
    if sys.platform == "win32":
        return slot_dir / "Scripts" / "python.exe"
    return slot_dir / "bin" / "python"


def _slot_dir(
    slots_root: Path,
    slot_id: str,
    *,
    with_python: bool = False,
    payload: bytes = b"",
) -> Path:
    """Create a minimal slot directory (optionally a probeable python)."""
    d = slots_root / slot_id
    d.mkdir(parents=True, exist_ok=True)
    if with_python:
        python = _slot_python_path(d)
        python.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(sys.executable, python)
    (d / "payload.bin").write_bytes(payload)
    return d


def _write_active(state_dir: Path, active: str, previous: str | None) -> None:
    payload: dict = {"schema_version": 1, "active_slot": active}
    if previous is not None:
        payload["previous_slot"] = previous
    (state_dir / "active.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def _write_store(state_dir: Path, filename: str, slot_refs: dict) -> None:
    payload = {"schema_version": 1, "slots": slot_refs}
    (state_dir / filename).write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


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


def _accel_entry(backend: str = "NVIDIA_CUDA") -> dict:
    return {"backend": backend, "variants": []}


def _ready_fixture(tmp_path: Path) -> Path:
    """The §20 synthetic fixture.

    ACTIVE (active), PREVIOUS (previous, also referenced by
    accelerated-metadata — mixed reference that must survive the purge),
    REF_OLD (installed-lock + provenance → REFERENCED), FAILED_GPU
    (accelerated-metadata only → PRUNABLE_CLEAN_METADATA), ORPHAN
    (nothing → PRUNABLE).
    """
    root = tmp_path / "rt"
    state_dir = root / "state"
    slots_root = root / "slots"
    state_dir.mkdir(parents=True)
    slots_root.mkdir()
    _write_active(state_dir, ACTIVE, PREVIOUS)
    _slot_dir(slots_root, ACTIVE, with_python=True, payload=b"A" * 10)
    _slot_dir(slots_root, PREVIOUS, with_python=True, payload=b"P" * 20)
    _slot_dir(slots_root, REF_OLD, payload=b"C" * 30)
    _slot_dir(slots_root, FAILED_GPU, payload=b"D" * 40)
    _slot_dir(slots_root, ORPHAN, payload=b"E" * 50)
    _write_store(
        state_dir,
        "installed-lock.json",
        {ACTIVE: _lock_entry("zemosaic"), REF_OLD: _lock_entry("zesolver")},
    )
    _write_store(
        state_dir,
        "product-provenance.json",
        {ACTIVE: _prov_entry("zemosaic"), REF_OLD: _prov_entry("zesolver")},
    )
    _write_store(
        state_dir,
        "accelerated-metadata.json",
        {FAILED_GPU: _accel_entry(), PREVIOUS: _accel_entry()},
    )
    return root


def _entry(plan: GcPlan, slot_id: str) -> GcSlotEntry:
    for entry in plan.slots:
        if entry.slot_id == slot_id:
            return entry
    raise AssertionError(f"{slot_id!r} not present in plan slots")


def _prunable_ids(plan: GcPlan) -> set[str]:
    return {
        entry.slot_id for entry in plan.slots if entry.category in DESTRUCTIVE
    }


def _record_sha256s(root: Path) -> dict[str, str]:
    state_dir = root / "state"
    return {
        name: hashlib.sha256((state_dir / name).read_bytes()).hexdigest()
        for name in RECORD_NAMES
        if (state_dir / name).is_file()
    }


# ---------------------------------------------------------------------------
# §19 matrix
# ---------------------------------------------------------------------------


def test_case01_active_slot_kept(tmp_path):
    plan = build_gc_plan(_ready_fixture(tmp_path))
    assert plan.status is GcStatus.READY
    entry = _entry(plan, ACTIVE)
    assert entry.category is SlotCategory.ACTIVE
    assert entry.references == ("active.json",)
    assert _prunable_ids(plan) == {FAILED_GPU, ORPHAN}


def test_case02_previous_slot_protected_by_default(tmp_path):
    """previous survives via active.json alone (no other record)."""
    root = _ready_fixture(tmp_path)
    state_dir = root / "state"
    _write_store(state_dir, "accelerated-metadata.json", {FAILED_GPU: _accel_entry()})
    plan = build_gc_plan(root)
    assert plan.status is GcStatus.READY
    entry = _entry(plan, PREVIOUS)
    assert entry.category is SlotCategory.PREVIOUS
    assert entry.references == ("active.json",)
    assert PREVIOUS not in _prunable_ids(plan)


def test_case03_referenced_slots_protected(tmp_path):
    plan = build_gc_plan(_ready_fixture(tmp_path))
    entry = _entry(plan, REF_OLD)
    assert entry.category is SlotCategory.REFERENCED
    assert set(entry.references) == {
        "installed-lock.json",
        "product-provenance.json",
    }
    assert REF_OLD not in _prunable_ids(plan)


def test_case04_true_orphan_prunable(tmp_path):
    plan = build_gc_plan(_ready_fixture(tmp_path))
    entry = _entry(plan, ORPHAN)
    assert entry.category is SlotCategory.PRUNABLE
    assert entry.metadata_action is None
    assert entry.references == ()


def test_case05_failed_candidate_prunable_clean_metadata(tmp_path):
    plan = build_gc_plan(_ready_fixture(tmp_path))
    entry = _entry(plan, FAILED_GPU)
    assert entry.category is SlotCategory.PRUNABLE_CLEAN_METADATA
    assert entry.metadata_action == "CLEAN_ACCELERATED_METADATA"
    assert entry.references == ("accelerated-metadata.json",)


def test_case06_unknown_reference_blocks(tmp_path):
    """A provenance reference to a slot with no directory → BLOCKED."""
    root = _ready_fixture(tmp_path)
    state_dir = root / "state"
    refs = {ACTIVE: _prov_entry("zemosaic"), REF_OLD: _prov_entry("zesolver")}
    refs["rt-ffffffffffff"] = _prov_entry("ghost")
    _write_store(state_dir, "product-provenance.json", refs)
    plan = build_gc_plan(root)
    assert plan.status is GcStatus.BLOCKED
    assert any("REPAIR_REQUIRED" in r for r in plan.blocking_reasons)
    assert _prunable_ids(plan) == set()
    assert any(
        entry.slot_id == "rt-ffffffffffff"
        and entry.category is SlotCategory.STALE_METADATA
        for entry in plan.slots
    )
    assert "rt-ffffffffffff" in plan.stale_metadata[0]


def test_case07_malformed_active_json_blocks(tmp_path):
    root = _ready_fixture(tmp_path)
    (root / "state" / "active.json").write_text("{ not json", encoding="utf-8")
    plan = build_gc_plan(root)
    assert plan.status is GcStatus.BLOCKED
    assert any("active.json" in r for r in plan.blocking_reasons)
    assert _prunable_ids(plan) == set()


def test_case08_active_slot_absent_blocks(tmp_path):
    root = _ready_fixture(tmp_path)
    shutil.rmtree(root / "slots" / ACTIVE)
    plan = build_gc_plan(root)
    assert plan.status is GcStatus.BLOCKED
    assert any("active_slot" in r for r in plan.blocking_reasons)
    assert _prunable_ids(plan) == set()


def test_case09_stale_accelerated_metadata_blocks(tmp_path):
    root = _ready_fixture(tmp_path)
    shutil.rmtree(root / "slots" / FAILED_GPU)  # metadata key left behind
    plan = build_gc_plan(root)
    assert plan.status is GcStatus.BLOCKED
    assert any("REPAIR_REQUIRED" in r for r in plan.blocking_reasons)
    assert _prunable_ids(plan) == set()  # ORPHAN is NOT prunable


def test_case10_stale_installed_lock_blocks(tmp_path):
    root = _ready_fixture(tmp_path)
    shutil.rmtree(root / "slots" / REF_OLD)  # lock key left behind
    plan = build_gc_plan(root)
    assert plan.status is GcStatus.BLOCKED
    assert any("REPAIR_REQUIRED" in r for r in plan.blocking_reasons)
    assert _prunable_ids(plan) == set()


def test_case11_absent_slot_with_stale_metadata_blocks(tmp_path):
    """Absent slot dir + stale metadata referencing it → BLOCKED, nothing
    destructive proposed even though an unrelated orphan exists."""
    root = _ready_fixture(tmp_path)
    shutil.rmtree(root / "slots" / FAILED_GPU)
    plan = build_gc_plan(root)
    assert plan.status is GcStatus.BLOCKED
    assert any(
        "rt-dddddddddddd" in r and "REPAIR_REQUIRED" in r
        for r in plan.blocking_reasons
    )
    assert any(
        entry.slot_id == FAILED_GPU
        and entry.category is SlotCategory.STALE_METADATA
        for entry in plan.slots
    )
    assert _prunable_ids(plan) == set()
    assert ORPHAN not in {e.slot_id for e in plan.slots if e.category in DESTRUCTIVE}


def test_case12_stale_plan_rejected(tmp_path):
    root = _ready_fixture(tmp_path)
    plan = build_gc_plan(root)
    # Mutate the state after planning (adds a file → slot dir mtime changes).
    (root / "slots" / ORPHAN / "late.bin").write_bytes(b"x")
    result = apply_gc_plan(root, plan)
    assert result.stale is True
    assert result.deleted_slots == ()
    assert (root / "slots" / ORPHAN).is_dir()
    assert (root / "slots" / FAILED_GPU).is_dir()


def test_case13_active_change_rejected(tmp_path):
    root = _ready_fixture(tmp_path)
    plan = build_gc_plan(root)
    _write_active(root / "state", PREVIOUS, ACTIVE)  # swap pointer
    result = apply_gc_plan(root, plan)
    assert result.stale is True
    assert result.deleted_slots == ()
    assert (root / "slots" / ORPHAN).is_dir()
    assert (root / "slots" / FAILED_GPU).is_dir()


def test_case14_previous_change_rejected(tmp_path):
    root = _ready_fixture(tmp_path)
    plan = build_gc_plan(root)
    _write_active(root / "state", ACTIVE, None)  # drop previous
    result = apply_gc_plan(root, plan)
    assert result.stale is True
    assert result.deleted_slots == ()


def test_case15_lock_change_rejected(tmp_path):
    root = _ready_fixture(tmp_path)
    plan = build_gc_plan(root)
    state_dir = root / "state"
    _write_store(
        state_dir,
        "installed-lock.json",
        {
            ACTIVE: _lock_entry("zemosaic"),
            REF_OLD: _lock_entry("zesolver"),
            ORPHAN: _lock_entry("newref"),
        },
    )
    result = apply_gc_plan(root, plan)
    assert result.stale is True
    assert result.deleted_slots == ()


def test_case16_metadata_change_rejected(tmp_path):
    root = _ready_fixture(tmp_path)
    plan = build_gc_plan(root)
    state_dir = root / "state"
    _write_store(
        state_dir,
        "accelerated-metadata.json",
        {
            FAILED_GPU: _accel_entry(),
            PREVIOUS: _accel_entry(),
            ORPHAN: _accel_entry(),
        },
    )
    result = apply_gc_plan(root, plan)
    assert result.stale is True
    assert result.deleted_slots == ()


def test_case17_symlink_slot_entry_blocks(tmp_path):
    root = _ready_fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, root / "slots" / "rt-999999999999")
    plan = build_gc_plan(root)
    assert plan.status is GcStatus.BLOCKED
    assert any(
        entry.slot_id == "rt-999999999999"
        and entry.category is SlotCategory.UNKNOWN
        for entry in plan.slots
    )
    assert _prunable_ids(plan) == set()
    result = apply_gc_plan(root, plan)
    assert result.deleted_slots == () and result.stale is False
    assert (root / "slots" / ORPHAN).is_dir()


def test_case18_path_traversal_rejected(tmp_path):
    root = _ready_fixture(tmp_path)
    slots_root = root / "slots"
    assert _validate_slot_entry(slots_root, "../etc") is None
    assert _validate_slot_entry(slots_root, "rt-../../etc") is None
    assert _validate_slot_entry(slots_root, "/etc") is None
    assert _validate_slot_entry(slots_root, "rt-zzzzzzzzzzzz") is None
    assert _validate_slot_entry(slots_root, "rt-abcd") is None
    assert _validate_slot_entry(slots_root, "") is None
    assert _validate_slot_entry(slots_root, "rt-" + "A" * 12) is None
    with pytest.raises(gc_module._SlotDeleteError):
        _safe_delete_slot(slots_root, "../etc")
    with pytest.raises(gc_module._SlotDeleteError):
        _safe_delete_slot(slots_root, "rt-zzzzzzzzzzzz")
    assert (root / "slots" / ACTIVE).is_dir()


def test_case19_partial_deletion_failure_preserves_active_and_continues(
    tmp_path, monkeypatch
):
    """A mid-run rmtree failure keeps the active runtime and lets the
    remaining prunable slots be processed."""
    root = _ready_fixture(tmp_path)
    plan = build_gc_plan(root)
    real_rmtree = shutil.rmtree

    def flaky_rmtree(path, **kwargs):
        if Path(path).name == FAILED_GPU:
            raise OSError("simulated rmtree failure")
        return real_rmtree(path, **kwargs)

    monkeypatch.setattr(gc_module.shutil, "rmtree", flaky_rmtree)
    result = apply_gc_plan(root, plan)
    assert FAILED_GPU not in result.deleted_slots
    assert ORPHAN in result.deleted_slots
    assert any("rt-dddddddddddd" in e for e in result.errors)
    assert (root / "slots" / ACTIVE).is_dir()
    assert (root / "slots" / PREVIOUS).is_dir()
    assert (root / "slots" / FAILED_GPU).is_dir()  # deletion failed → kept
    # its metadata entry was NOT purged (slot not actually deleted)
    meta = json.loads(
        (root / "state" / "accelerated-metadata.json").read_text(
            encoding="utf-8"
        )
    )
    assert FAILED_GPU in meta["slots"]


def test_case20_metadata_write_failure_preserves_runtime(tmp_path, monkeypatch):
    root = _ready_fixture(tmp_path)
    active_before = (root / "state" / "active.json").read_bytes()
    meta_before = (root / "state" / "accelerated-metadata.json").read_bytes()
    plan = build_gc_plan(root)
    real_replace = os.replace

    def failing_replace(src, dst):
        if str(dst).endswith("accelerated-metadata.json"):
            raise OSError("simulated metadata write failure")
        return real_replace(src, dst)

    monkeypatch.setattr(gc_module.os, "replace", failing_replace)
    result = apply_gc_plan(root, plan)
    assert FAILED_GPU in result.deleted_slots
    assert ORPHAN in result.deleted_slots
    assert any("purge" in e for e in result.errors)
    # runtime preserved: active/previous intact, pointer byte-identical
    assert (root / "slots" / ACTIVE).is_dir()
    assert (root / "slots" / PREVIOUS).is_dir()
    assert (root / "state" / "active.json").read_bytes() == active_before
    # metadata file untouched (atomic write never happened)
    assert (
        root / "state" / "accelerated-metadata.json"
    ).read_bytes() == meta_before


def test_case22_empty_prune_set_noop_success(tmp_path):
    root = _ready_fixture(tmp_path)
    state_dir = root / "state"
    shutil.rmtree(root / "slots" / FAILED_GPU)
    shutil.rmtree(root / "slots" / ORPHAN)
    _write_store(state_dir, "accelerated-metadata.json", {PREVIOUS: _accel_entry()})
    plan = build_gc_plan(root)
    assert plan.status is GcStatus.READY
    assert _prunable_ids(plan) == set()
    before = _record_sha256s(root)
    result = apply_gc_plan(root, plan)
    assert result.deleted_slots == ()
    assert result.reclaimed_bytes == 0
    assert result.stale is False
    assert result.errors == ()
    assert _record_sha256s(root) == before
    assert result.preserved_slots == tuple(
        sorted({ACTIVE, PREVIOUS, REF_OLD})
    )


def test_case23_reclaimed_bytes_accounting(tmp_path):
    root = _ready_fixture(tmp_path)
    plan = build_gc_plan(root)
    failed_entry = _entry(plan, FAILED_GPU)
    orphan_entry = _entry(plan, ORPHAN)
    # sanity: the estimate at least covers the payload bytes
    assert orphan_entry.estimated_bytes >= 50
    assert failed_entry.estimated_bytes >= 40
    expected = failed_entry.estimated_bytes + orphan_entry.estimated_bytes
    assert plan.total_recoverable_bytes == expected
    result = apply_gc_plan(root, plan)
    assert result.reclaimed_bytes == expected
    assert set(result.deleted_slots) == {FAILED_GPU, ORPHAN}


def test_case24_cli_gc_plan_read_only(tmp_path, monkeypatch):
    root = _ready_fixture(tmp_path)
    monkeypatch.setenv("ZEALFIE_RUNTIME_ROOT", str(root))
    before = _record_sha256s(root)
    stdout = StringIO()
    code = cli.run(["runtime", "gc-plan"], stdout=stdout)
    assert code == 0
    out = stdout.getvalue()
    assert "Safe runtime GC plan:" in out
    assert "Status: READY" in out
    assert "KEEP" in out and "PRUNE" in out
    assert "No changes have been applied (read-only preview)." in out
    assert _record_sha256s(root) == before
    assert (root / "slots" / ORPHAN).is_dir()
    assert (root / "slots" / FAILED_GPU).is_dir()


def test_case25_cli_gc_blocked_returns_nonzero(tmp_path, monkeypatch):
    root = _ready_fixture(tmp_path)
    monkeypatch.setenv("ZEALFIE_RUNTIME_ROOT", str(root))
    (root / "state" / "active.json").write_text("{ broken", encoding="utf-8")
    stdout = StringIO()
    code = cli.run(["runtime", "gc-plan"], stdout=stdout)
    assert code == 1
    assert "Status: BLOCKED" in stdout.getvalue()
    # `runtime gc` refuses too and deletes nothing
    stdout2 = StringIO()
    code2 = cli.run(["runtime", "gc"], stdout=stdout2)
    assert code2 == 1
    assert (root / "slots" / ORPHAN).is_dir()
    assert (root / "slots" / FAILED_GPU).is_dir()


def test_case26_active_runtime_still_launchable_after_synthetic_gc(tmp_path):
    root = _ready_fixture(tmp_path)
    plan = build_gc_plan(root)
    result = apply_gc_plan(root, plan)
    assert set(result.deleted_slots) == {FAILED_GPU, ORPHAN}
    version = probe_runtime_python_version(
        _slot_python_path(root / "slots" / ACTIVE)
    )
    assert version is not None
    prev_version = probe_runtime_python_version(
        _slot_python_path(root / "slots" / PREVIOUS)
    )
    assert prev_version is not None


# ---------------------------------------------------------------------------
# §20 synthetic E2E
# ---------------------------------------------------------------------------


def test_synthetic_e2e_full_gc_cycle(tmp_path):
    root = _ready_fixture(tmp_path)
    state_dir = root / "state"
    lock_before = (state_dir / "installed-lock.json").read_bytes()
    prov_before = (state_dir / "product-provenance.json").read_bytes()
    active_before = (state_dir / "active.json").read_bytes()

    plan = build_gc_plan(root)
    assert plan.status is GcStatus.READY
    assert _prunable_ids(plan) == {FAILED_GPU, ORPHAN}
    assert plan.total_recoverable_bytes > 0

    result = apply_gc_plan(root, plan)
    assert result.stale is False
    assert result.errors == ()
    assert result.deleted_slots == (FAILED_GPU, ORPHAN)  # sorted by slot id

    # only the orphan + failed candidate were deleted
    assert (root / "slots" / ACTIVE).is_dir()
    assert (root / "slots" / PREVIOUS).is_dir()
    assert (root / "slots" / REF_OLD).is_dir()
    assert not (root / "slots" / FAILED_GPU).exists()
    assert not (root / "slots" / ORPHAN).exists()

    # installed-lock / provenance / active.json byte-identical
    assert (state_dir / "installed-lock.json").read_bytes() == lock_before
    assert (state_dir / "product-provenance.json").read_bytes() == prov_before
    assert (state_dir / "active.json").read_bytes() == active_before

    # accelerated-metadata: FAILED_GPU purged, mixed references intact
    meta = json.loads(
        (state_dir / "accelerated-metadata.json").read_text(encoding="utf-8")
    )
    assert meta["schema_version"] == 1
    assert set(meta["slots"]) == {PREVIOUS}
    assert meta["slots"][PREVIOUS]["backend"] == "NVIDIA_CUDA"

    # active runtime still usable, rollback target present
    version = probe_runtime_python_version(
        _slot_python_path(root / "slots" / ACTIVE)
    )
    assert version is not None
    assert (root / "slots" / PREVIOUS / "bin" / "python").exists()

    # a second plan is now a no-op (nothing prunable left, metadata clean)
    plan2 = build_gc_plan(root)
    assert plan2.status is GcStatus.READY
    assert _prunable_ids(plan2) == set()
    result2 = apply_gc_plan(root, plan2)
    assert result2.deleted_slots == () and result2.errors == ()


# ---------------------------------------------------------------------------
# Defence in depth / determinism / adversarial path safety
# ---------------------------------------------------------------------------


def test_forged_plan_cannot_delete_protected(tmp_path):
    """A forged plan (same fingerprint, lying categories) cannot delete
    active/previous: the deletion set is computed from the FRESH plan."""
    root = _ready_fixture(tmp_path)
    plan = build_gc_plan(root)
    forged_slots = []
    for entry in plan.slots:
        if entry.slot_id in (ACTIVE, PREVIOUS):
            forged_slots.append(
                GcSlotEntry(
                    slot_id=entry.slot_id,
                    path=entry.path,
                    category=SlotCategory.PRUNABLE,  # lie
                    reason="forged",
                    references=entry.references,
                    metadata_action=None,
                    estimated_bytes=entry.estimated_bytes,
                )
            )
        else:
            forged_slots.append(entry)
    forged = GcPlan(
        runtime_root=plan.runtime_root,
        status=plan.status,
        active_slot_id=plan.active_slot_id,
        previous_slot_id=plan.previous_slot_id,
        slots=tuple(forged_slots),
        total_recoverable_bytes=plan.total_recoverable_bytes,
        blocking_reasons=plan.blocking_reasons,
        stale_metadata=plan.stale_metadata,
        state_fingerprint=plan.state_fingerprint,  # fingerprint still matches
    )
    result = apply_gc_plan(root, forged)
    assert ACTIVE not in result.deleted_slots
    assert PREVIOUS not in result.deleted_slots
    assert (root / "slots" / ACTIVE).is_dir()
    assert (root / "slots" / PREVIOUS).is_dir()
    # the honest prunables are still deleted
    assert set(result.deleted_slots) == {FAILED_GPU, ORPHAN}


def test_fingerprint_deterministic(tmp_path):
    root = _ready_fixture(tmp_path)
    plan1 = build_gc_plan(root)
    plan2 = build_gc_plan(root)
    assert plan1.state_fingerprint == plan2.state_fingerprint
    assert len(plan1.state_fingerprint) == 64  # sha256 hex
    (root / "slots" / ORPHAN / "new.bin").write_bytes(b"x")
    plan3 = build_gc_plan(root)
    assert plan3.state_fingerprint != plan1.state_fingerprint


def test_deletion_order_deterministic(tmp_path, monkeypatch):
    root = _ready_fixture(tmp_path)
    plan = build_gc_plan(root)
    order: list[str] = []
    real = gc_module._safe_delete_slot

    def spy(slots_root, slot_id):
        order.append(slot_id)
        return real(slots_root, slot_id)

    monkeypatch.setattr(gc_module, "_safe_delete_slot", spy)
    result = apply_gc_plan(root, plan)
    assert order == [FAILED_GPU, ORPHAN] == sorted(order)
    assert result.deleted_slots == tuple(sorted(result.deleted_slots))


def test_validate_slot_entry_adversarial(tmp_path):
    slots_root = tmp_path / "slots"
    slots_root.mkdir()
    # symlinked slots root is rejected
    real_root = tmp_path / "real_slots"
    real_root.mkdir()
    (real_root / "rt-aaaaaaaaaaaa").mkdir()
    link_root = tmp_path / "slots_link"
    os.symlink(real_root, link_root)
    assert _validate_slot_entry(link_root, "rt-aaaaaaaaaaaa") is None
    # non-directory entry rejected
    (slots_root / "rt-bbbbbbbbbbbb").write_text("file")
    assert _validate_slot_entry(slots_root, "rt-bbbbbbbbbbbb") is None
    # symlink entry (even pointing inside) rejected
    os.symlink(real_root / "rt-aaaaaaaaaaaa", slots_root / "rt-cccccccccccc")
    assert _validate_slot_entry(slots_root, "rt-cccccccccccc") is None
    # symlink entry pointing outside rejected
    os.symlink(tmp_path, slots_root / "rt-999999999999")
    assert _validate_slot_entry(slots_root, "rt-999999999999") is None
    # a plain directory validates
    good = slots_root / "rt-eeeeeeeeeeee"
    good.mkdir()
    assert _validate_slot_entry(slots_root, "rt-eeeeeeeeeeee") == good.resolve()


def test_store_corruption_matrix(tmp_path):
    """Corrupt/invalid store records fail closed."""
    root = _ready_fixture(tmp_path)
    state_dir = root / "state"
    # invalid JSON
    (state_dir / "installed-lock.json").write_text("{ nope", encoding="utf-8")
    plan = build_gc_plan(root)
    assert plan.status is GcStatus.BLOCKED
    assert _prunable_ids(plan) == set()
    # unexpected schema version
    _write_store(
        state_dir,
        "installed-lock.json",
        {ACTIVE: _lock_entry("zemosaic"), REF_OLD: _lock_entry("zesolver")},
    )
    (state_dir / "product-provenance.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "slots": {ACTIVE: _prov_entry("zemosaic")},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    plan_schema = build_gc_plan(root)
    assert plan_schema.status is GcStatus.BLOCKED
    assert any("schema_version" in r for r in plan_schema.blocking_reasons)
    assert _prunable_ids(plan_schema) == set()
    # (invalid slot key)
    root2 = _ready_fixture(tmp_path / "root2")
    state_dir2 = root2 / "state"
    (state_dir2 / "product-provenance.json").write_text(
        json.dumps(
            {"schema_version": 1, "slots": {"not-a-slot": {}}}
        )
        + "\n",
        encoding="utf-8",
    )
    plan2 = build_gc_plan(root2)
    assert plan2.status is GcStatus.BLOCKED
    assert _prunable_ids(plan2) == set()


def test_absent_active_json_blocks(tmp_path):
    root = _ready_fixture(tmp_path)
    (root / "state" / "active.json").unlink()
    plan = build_gc_plan(root)
    assert plan.status is GcStatus.BLOCKED
    assert _prunable_ids(plan) == set()


# ---------------------------------------------------------------------------
# CLI gc
# ---------------------------------------------------------------------------


def test_cli_gc_applies_and_reports(tmp_path, monkeypatch):
    root = _ready_fixture(tmp_path)
    monkeypatch.setenv("ZEALFIE_RUNTIME_ROOT", str(root))
    stdout = StringIO()
    code = cli.run(["runtime", "gc"], stdout=stdout)
    assert code == 0
    out = stdout.getvalue()
    assert "Safe runtime GC result:" in out
    assert (
        f"Deleted slots: {FAILED_GPU}, {ORPHAN}" in out
    )
    assert "Stale plan: no" in out
    assert not (root / "slots" / ORPHAN).exists()
    assert not (root / "slots" / FAILED_GPU).exists()
    assert (root / "slots" / ACTIVE).is_dir()
    meta = json.loads(
        (root / "state" / "accelerated-metadata.json").read_text(
            encoding="utf-8"
        )
    )
    assert FAILED_GPU not in meta["slots"]
    assert PREVIOUS in meta["slots"]


def test_cli_gc_stale_exits_2(tmp_path, monkeypatch):
    """CLI gc returns 2 and deletes nothing when the state changed."""
    root = _ready_fixture(tmp_path)
    monkeypatch.setenv("ZEALFIE_RUNTIME_ROOT", str(root))
    stale_plan = build_gc_plan(root)
    (root / "slots" / ORPHAN / "late.bin").write_bytes(b"x")  # mutate
    monkeypatch.setattr(cli, "build_gc_plan", lambda _root: stale_plan)
    stdout = StringIO()
    code = cli.run(["runtime", "gc"], stdout=stdout)
    assert code == 2
    assert "Stale plan: yes" in stdout.getvalue()
    assert (root / "slots" / ORPHAN).is_dir()
    assert (root / "slots" / FAILED_GPU).is_dir()


def test_gc_plan_in_parser():
    parser = cli.build_parser()
    args = parser.parse_args(["runtime", "gc-plan"])
    assert args.runtime_command == "gc-plan"
    args2 = parser.parse_args(["runtime", "gc"])
    assert args2.runtime_command == "gc"
