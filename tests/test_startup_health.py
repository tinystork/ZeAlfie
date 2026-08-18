"""ZA-M1-4 LOT A — transient rollback retention (startup-health) tests.

Covers the fresh-startup health confirmation and the GC integration that
makes the PREVIOUS (rollback) slot releasable only after a verified
healthy restart:

1. no confirmation present -> ACTIVE + PREVIOUS stay protected;
2. valid confirmation (matching fingerprint) -> PREVIOUS becomes
   PREVIOUS_RELEASABLE and apply clears ``previous_slot`` + deletes the
   directory (no REPAIR_REQUIRED afterwards);
3. unhealthy / corrupt / mismatched confirmation -> PREVIOUS stays
   protected, apply deletes nothing for it;
4. accelerated ACTIVE healthy -> PREVIOUS released safely;
5. accelerated ACTIVE invalid (variant version mismatch) -> PREVIOUS
   stays protected;
6. interrupted transaction (mutation lease held) -> no confirmation;
7. deletion failure is best-effort/non-destructive.

Hermetic: tmp_path runtime roots, symlinked slot pythons, monkeypatched
probes — no real wheels, no network.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

import zealfie.runtime.startup_health as sh_mod
from zealfie.runtime.gc import (
    CLEAN_ACCELERATED_METADATA,
    GcStatus,
    SlotCategory,
    _safe_delete_slot,
    apply_gc_plan,
    build_gc_plan,
)
from zealfie.runtime.mutation_lock import (
    OPERATION_RUNTIME_GC,
    RuntimeMutationLock,
)
from zealfie.runtime.startup_health import (
    STARTUP_HEALTH_FILENAME,
    STARTUP_HEALTH_SCHEMA_VERSION,
    StartupHealthConfirmation,
    compute_records_fingerprint,
    confirm_and_record_startup_health,
    confirm_startup_health,
    load_startup_health,
    record_startup_health,
)

# -- slot ids ---------------------------------------------------------------
ACTIVE = "rt-aaaaaaaaaaaa"
PREVIOUS = "rt-bbbbbbbbbbbb"

RECORD_NAMES = (
    "active.json",
    "installed-lock.json",
    "product-provenance.json",
    "accelerated-metadata.json",
)

DESTRUCTIVE = (
    SlotCategory.PRUNABLE,
    SlotCategory.PRUNABLE_CLEAN_METADATA,
    SlotCategory.PREVIOUS_RELEASABLE,
)


# ---------------------------------------------------------------------------
# Fixture helpers
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


def _accel_entry(*, variants=None) -> dict:
    return {
        "backend": "NVIDIA_CUDA",
        "variants": [] if variants is None else variants,
    }


def _minimal_fixture(tmp_path: Path) -> Path:
    """ACTIVE + PREVIOUS only, both with a symlinked python, no stores."""
    root = tmp_path / "rt"
    state_dir = root / "state"
    slots_root = root / "slots"
    state_dir.mkdir(parents=True)
    slots_root.mkdir()
    _write_active(state_dir, ACTIVE, PREVIOUS)
    _slot_dir(slots_root, ACTIVE, with_python=True, payload=b"A" * 10)
    _slot_dir(slots_root, PREVIOUS, with_python=True, payload=b"P" * 20)
    return root


def _records_fingerprint(root: Path) -> str:
    state_dir = root / "state"
    record_bytes: dict[str, bytes | None] = {}
    for name in RECORD_NAMES:
        p = state_dir / name
        record_bytes[name] = p.read_bytes() if p.is_file() else None
    return compute_records_fingerprint(record_bytes)


def _write_confirmation(
    root: Path,
    active_id: str,
    *,
    fingerprint: str | None = None,
) -> str:
    if fingerprint is None:
        fingerprint = _records_fingerprint(root)
    record_startup_health(
        root,
        StartupHealthConfirmation(
            schema_version=STARTUP_HEALTH_SCHEMA_VERSION,
            active_slot_id=active_id,
            confirmed_at="2026-08-18T00:00:00+00:00",
            records_fingerprint=fingerprint,
        ),
    )
    return fingerprint


def _entry(plan, slot_id: str):
    for entry in plan.slots:
        if entry.slot_id == slot_id:
            return entry
    raise AssertionError(f"{slot_id!r} not present in plan slots")


def _prunable_ids(plan) -> set[str]:
    return {
        entry.slot_id for entry in plan.slots if entry.category in DESTRUCTIVE
    }


def _active_payload(root: Path) -> dict:
    return json.loads(
        (root / "state" / "active.json").read_text(encoding="utf-8")
    )


def _monkeypatch_healthy_probes(monkeypatch):
    """Make both probes succeed with a fixed, healthy result."""
    monkeypatch.setattr(
        sh_mod, "probe_runtime_python_version", lambda p, timeout=None: "3.12.0"
    )
    monkeypatch.setattr(
        sh_mod,
        "probe_runtime_distribution",
        lambda p, name, timeout=None: {
            "installed": True,
            "version": "1.0.0",
            "entry_points": [],
        },
    )


# ---------------------------------------------------------------------------
# 1. no confirmation -> ACTIVE + PREVIOUS protected
# ---------------------------------------------------------------------------


def test_no_confirmation_previous_stays_protected(tmp_path):
    root = _minimal_fixture(tmp_path)
    plan = build_gc_plan(root)
    assert plan.status is GcStatus.READY
    assert _entry(plan, ACTIVE).category is SlotCategory.ACTIVE
    assert _entry(plan, PREVIOUS).category is SlotCategory.PREVIOUS
    assert _prunable_ids(plan) == set()


# ---------------------------------------------------------------------------
# 2. valid confirmation -> PREVIOUS releasable; apply clears + deletes
# ---------------------------------------------------------------------------


def test_valid_confirmation_previous_releasable_and_applied(tmp_path):
    root = _minimal_fixture(tmp_path)
    _write_confirmation(root, ACTIVE)

    plan = build_gc_plan(root)
    assert plan.status is GcStatus.READY
    entry = _entry(plan, PREVIOUS)
    assert entry.category is SlotCategory.PREVIOUS_RELEASABLE
    assert PREVIOUS in _prunable_ids(plan)
    # references/metadata_actions from store refs (none here)
    assert entry.references == ()
    assert entry.metadata_actions == ()

    result = apply_gc_plan(root, plan)
    assert result.stale is False
    assert result.errors == ()
    assert PREVIOUS in result.deleted_slots

    # active.json no longer has previous_slot; directory gone.
    payload = _active_payload(root)
    assert payload["active_slot"] == ACTIVE
    assert "previous_slot" not in payload
    assert not (root / "slots" / PREVIOUS).exists()

    # no REPAIR_REQUIRED / BLOCKED on the next plan.
    plan2 = build_gc_plan(root)
    assert plan2.status is GcStatus.READY
    assert plan2.previous_slot_id is None
    assert plan2.blocking_reasons == ()
    assert _prunable_ids(plan2) == set()


def test_previous_releasable_purges_store_metadata(tmp_path):
    """PREVIOUS also referenced by a store: its metadata_actions are
    computed like PRUNABLE_CLEAN_METADATA and purged before deletion."""
    root = _minimal_fixture(tmp_path)
    _write_store(
        root / "state",
        "accelerated-metadata.json",
        {PREVIOUS: _accel_entry()},
    )
    _write_confirmation(root, ACTIVE)

    plan = build_gc_plan(root)
    entry = _entry(plan, PREVIOUS)
    assert entry.category is SlotCategory.PREVIOUS_RELEASABLE
    assert entry.references == ("accelerated-metadata.json",)
    assert entry.metadata_actions == (CLEAN_ACCELERATED_METADATA,)

    result = apply_gc_plan(root, plan)
    assert result.errors == ()
    assert PREVIOUS in result.deleted_slots
    meta = json.loads(
        (root / "state" / "accelerated-metadata.json").read_text(
            encoding="utf-8"
        )
    )
    assert PREVIOUS not in meta["slots"]


# ---------------------------------------------------------------------------
# 3. unhealthy / corrupt / mismatched -> PREVIOUS stays protected
# ---------------------------------------------------------------------------


def test_mismatched_fingerprint_keeps_previous_protected(tmp_path):
    root = _minimal_fixture(tmp_path)
    _write_confirmation(root, ACTIVE, fingerprint="0" * 64)
    plan = build_gc_plan(root)
    assert plan.status is GcStatus.READY
    assert _entry(plan, PREVIOUS).category is SlotCategory.PREVIOUS
    assert _prunable_ids(plan) == set()


def test_mismatched_active_slot_keeps_previous_protected(tmp_path):
    root = _minimal_fixture(tmp_path)
    _write_confirmation(root, "rt-cccccccccccc")  # confirmation for another slot
    plan = build_gc_plan(root)
    assert plan.status is GcStatus.READY
    assert _entry(plan, PREVIOUS).category is SlotCategory.PREVIOUS
    assert _prunable_ids(plan) == set()


def test_corrupt_store_keeps_previous_protected(tmp_path):
    root = _minimal_fixture(tmp_path)
    _write_confirmation(root, ACTIVE)
    # corrupt a store AFTER writing a valid confirmation -> fingerprint no
    # longer matches AND the plan is BLOCKED.
    (root / "state" / "installed-lock.json").write_text(
        "{ not json", encoding="utf-8"
    )
    plan = build_gc_plan(root)
    assert plan.status is GcStatus.BLOCKED
    assert _prunable_ids(plan) == set()
    assert _entry(plan, PREVIOUS).category is SlotCategory.PREVIOUS
    # apply refuses everything.
    result = apply_gc_plan(root, plan)
    assert result.deleted_slots == ()
    assert (root / "slots" / PREVIOUS).is_dir()


def test_unhealthy_confirm_does_not_record(tmp_path, monkeypatch):
    """An unhealthy confirm_startup_health result writes no confirmation."""
    root = _minimal_fixture(tmp_path)
    # active state absent -> unhealthy.
    (root / "state" / "active.json").unlink()
    result = confirm_and_record_startup_health(root)
    assert result.healthy is False
    assert not (root / "state" / STARTUP_HEALTH_FILENAME).exists()


# ---------------------------------------------------------------------------
# confirm_startup_health unit coverage
# ---------------------------------------------------------------------------


def test_confirm_startup_health_healthy_writes_confirmation(tmp_path, monkeypatch):
    root = _minimal_fixture(tmp_path)
    _monkeypatch_healthy_probes(monkeypatch)

    result = confirm_startup_health(root)
    assert result.healthy is True
    assert result.active_slot_id == ACTIVE
    assert result.records_fingerprint == _records_fingerprint(root)

    result2 = confirm_and_record_startup_health(root)
    assert result2.healthy is True
    assert (root / "state" / STARTUP_HEALTH_FILENAME).is_file()
    loaded = load_startup_health(root / "state")
    assert loaded is not None
    assert loaded.active_slot_id == ACTIVE
    assert loaded.records_fingerprint == result2.records_fingerprint


def test_confirm_unhealthy_missing_active_slot_dir(tmp_path, monkeypatch):
    root = _minimal_fixture(tmp_path)
    _monkeypatch_healthy_probes(monkeypatch)
    import shutil

    shutil.rmtree(root / "slots" / ACTIVE)
    result = confirm_startup_health(root)
    assert result.healthy is False
    assert any("active slot directory" in r for r in result.reasons)


def test_load_startup_health_lenient(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    assert load_startup_health(state_dir) is None  # missing
    (state_dir / STARTUP_HEALTH_FILENAME).write_text("{ not json", encoding="utf-8")
    assert load_startup_health(state_dir) is None  # invalid JSON
    (state_dir / STARTUP_HEALTH_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "active_slot_id": "rt-aaaaaaaaaaaa",
                "confirmed_at": "2026-08-18T00:00:00+00:00",
                "records_fingerprint": "not-hex",
            }
        )
    )
    assert load_startup_health(state_dir) is None  # malformed fingerprint


# ---------------------------------------------------------------------------
# 4 + 5. accelerated ACTIVE healthy vs invalid variant
# ---------------------------------------------------------------------------


def _accelerated_fixture(tmp_path: Path, variant_version: str) -> Path:
    root = _minimal_fixture(tmp_path)
    state_dir = root / "state"
    _write_store(
        state_dir,
        "installed-lock.json",
        {ACTIVE: {"primary_names": [], "dependencies": {}}},
    )
    _write_store(
        state_dir,
        "accelerated-metadata.json",
        {
            ACTIVE: _accel_entry(
                variants=[["fake-accel", variant_version, "d" * 64]]
            )
        },
    )
    return root


def test_accelerated_active_healthy_releases_previous(tmp_path, monkeypatch):
    root = _accelerated_fixture(tmp_path, "1.0.0")
    _monkeypatch_healthy_probes(monkeypatch)

    result = confirm_and_record_startup_health(root)
    assert result.healthy is True
    assert (root / "state" / STARTUP_HEALTH_FILENAME).is_file()

    plan = build_gc_plan(root)
    assert _entry(plan, PREVIOUS).category is SlotCategory.PREVIOUS_RELEASABLE


def test_accelerated_active_invalid_keeps_previous_protected(tmp_path, monkeypatch):
    root = _accelerated_fixture(tmp_path, "1.0.0")
    monkeypatch.setattr(
        sh_mod, "probe_runtime_python_version", lambda p, timeout=None: "3.12.0"
    )

    def fake_dist(python, name, *, timeout=None):
        if name == "fake-accel":
            return {"installed": True, "version": "9.9.9", "entry_points": []}
        return {"installed": True, "version": "1.0.0", "entry_points": []}

    monkeypatch.setattr(sh_mod, "probe_runtime_distribution", fake_dist)

    result = confirm_startup_health(root)
    assert result.healthy is False
    assert any("accelerated variant" in r for r in result.reasons)
    assert not (root / "state" / STARTUP_HEALTH_FILENAME).exists()

    plan = build_gc_plan(root)
    assert _entry(plan, PREVIOUS).category is SlotCategory.PREVIOUS
    assert _prunable_ids(plan) == set()


# ---------------------------------------------------------------------------
# 6. interrupted transaction -> no premature release
# ---------------------------------------------------------------------------


def test_interrupted_transaction_blocks_confirmation(tmp_path, monkeypatch):
    root = _minimal_fixture(tmp_path)
    _monkeypatch_healthy_probes(monkeypatch)

    lock = RuntimeMutationLock(root)
    acquired = threading.Event()
    release = threading.Event()

    def holder():
        with lock.acquire(OPERATION_RUNTIME_GC):
            acquired.set()
            release.wait(timeout=10)

    t = threading.Thread(target=holder)
    t.start()
    acquired.wait(timeout=10)
    try:
        result = confirm_startup_health(root)
        assert result.healthy is False
        assert any("mutation in progress" in r for r in result.reasons)
    finally:
        release.set()
        t.join(timeout=10)

    # no confirmation was written -> PREVIOUS stays protected.
    assert not (root / "state" / STARTUP_HEALTH_FILENAME).exists()
    plan = build_gc_plan(root)
    assert _entry(plan, PREVIOUS).category is SlotCategory.PREVIOUS


# ---------------------------------------------------------------------------
# 7. deletion failure best-effort / non-destructive
# ---------------------------------------------------------------------------


def test_previous_release_delete_failure_is_non_destructive(tmp_path, monkeypatch):
    """Fail-safe ordering: clear previous_slot FIRST, then purge metadata,
    then delete the directory.  A failed directory removal leaves
    active.json already cleared (never pointing at a deleted slot) and the
    slot directory preserved on disk (self-healing PRUNABLE next round)."""
    root = _minimal_fixture(tmp_path)
    _write_confirmation(root, ACTIVE)
    plan = build_gc_plan(root)

    real_delete = _safe_delete_slot

    def flaky_delete(slots_root, slot_id):
        if slot_id == PREVIOUS:
            raise OSError("simulated deletion failure (file locked)")
        return real_delete(slots_root, slot_id)

    monkeypatch.setattr("zealfie.runtime.gc._safe_delete_slot", flaky_delete)

    result = apply_gc_plan(root, plan)
    assert PREVIOUS not in result.deleted_slots
    assert any("rt-bbbbbbbbbbbb" in e for e in result.errors)

    # active.json no longer points at the (non-deleted) slot.
    payload = _active_payload(root)
    assert "previous_slot" not in payload
    # the slot directory still exists -> self-heals as PRUNABLE next round.
    assert (root / "slots" / PREVIOUS).is_dir()

    plan2 = build_gc_plan(root)
    assert plan2.status is GcStatus.READY
    assert _entry(plan2, PREVIOUS).category is SlotCategory.PRUNABLE


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_cli_confirm_health_parser_and_exit_codes(tmp_path, monkeypatch):
    import io

    import zealfie.cli as cli

    parser = cli.build_parser()
    args = parser.parse_args(["runtime", "confirm-health"])
    assert args.runtime_command == "confirm-health"

    root = _minimal_fixture(tmp_path)
    monkeypatch.setenv("ZEALFIE_RUNTIME_ROOT", str(root))
    _monkeypatch_healthy_probes(monkeypatch)

    out = io.StringIO()
    code = cli.run(["runtime", "confirm-health"], stdout=out)
    assert code == 0
    assert "HEALTHY" in out.getvalue()
    assert (root / "state" / STARTUP_HEALTH_FILENAME).is_file()

    # unhealthy active state -> exit 1
    (root / "state" / "active.json").write_text("{ bad", encoding="utf-8")
    out2 = io.StringIO()
    code2 = cli.run(["runtime", "confirm-health"], stdout=out2)
    assert code2 == 1
    assert "UNHEALTHY" in out2.getvalue()
