"""Safe runtime garbage collection (ZA-M1-2K Phase B+C).

Implements a **pure, read-only planner** (:func:`build_gc_plan`) and a
**stale-checked apply engine** (:func:`apply_gc_plan`) for the M0-6 slot
runtime.  Design inputs are the Phase A slot-lifecycle audit
(``AGENT/reports/m1_2k_phase_a_slot_lifecycle_audit_20260816.md``):

* ``state/active.json`` is the single activation authority (active +
  previous).  Both fields are HARD-protected.
* ``state/product-provenance.json``, ``state/installed-lock.json`` and
  ``state/accelerated-metadata.json`` are observational per-slot
  history (ZA-M1-3A.3).  In steady-state operation only ACTIVE +
  PREVIOUS persist: a slot directory outside {active, previous} that is
  referenced by **any** combination of these stores is
  ``PRUNABLE_CLEAN_METADATA`` — the slot directory and every
  referencing store entry are pruned together, with each store purged
  atomically and the metadata purge performed strictly **before** the
  directory removal (so an interrupted apply never leaves a store
  pointing at a deleted slot → no ``REPAIR_REQUIRED``).
* A slot directory on disk referenced by **no** record → ``PRUNABLE``.

Fail-closed rules (all produce ``GcStatus.BLOCKED`` and never propose
any destructive action):

* ``active.json`` absent/corrupt/malformed, or active/previous invalid;
* any of the 3 store records corrupt (invalid JSON, unexpected
  ``schema_version``, missing/malformed ``slots`` object, invalid slot-id
  key);
* the active or previous slot, or any store-referenced slot, has no
  directory on disk (``REPAIR_REQUIRED`` — the GC never repairs, it
  refuses);
* any ``slots/`` entry that is invalid (name not matching
  ``^rt-[0-9a-f]{12}$``, a symlink, or a non-directory) → ``UNKNOWN``.

Concurrency discipline: there is **no mutation lock** in ZeAlfie (audit
Q9).  The GC's discipline is therefore the *state fingerprint*: the plan
captures a sha256 over the raw bytes of the 4 records plus the sorted
``(name, mtime_ns, size)`` entries of ``slots/``, and
:func:`apply_gc_plan` rebuilds a fresh plan and refuses everything on
fingerprint mismatch (``GcResult.stale``).  Every deletion is re-validated
path-safely immediately before removal, and active/previous are re-checked
at deletion time (defence in depth against forged plans).

Path safety: no user-supplied path is ever accepted.  Only *slot ids* flow
from the plan; :func:`_validate_slot_entry` re-resolves each one strictly
under the canonical ``slots/`` root and rejects any symlink component, and
:func:`_safe_delete_slot` re-validates before ``shutil.rmtree``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Sequence

from .layout import validate_slot_id
from .model import RuntimeState
from .mutation_lock import OPERATION_RUNTIME_GC, RuntimeMutationLock
from .state import clear_previous_slot, load_active_state
from .startup_health import compute_records_fingerprint, load_startup_health

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SLOT_ID_RE = re.compile(r"^rt-[0-9a-f]{12}$")

# Schema versions mirror the canonical store constants
# (installed_lock.INSTALLED_LOCK_SCHEMA_VERSION,
#  provenance.PROVENANCE_SCHEMA_VERSION,
#  acceleration.deployment.ACCELERATED_METADATA_SCHEMA_VERSION).
# Kept local to avoid importing acceleration.* here (import-cycle-free,
# this module stays dependency-light on purpose).
_SCHEMA_VERSION = 1

_ACTIVE_FILENAME = "active.json"
_INSTALLED_LOCK_FILENAME = "installed-lock.json"
_PROVENANCE_FILENAME = "product-provenance.json"
_ACCELERATED_METADATA_FILENAME = "accelerated-metadata.json"

# Order is significant for deterministic fingerprint composition.
_RECORD_FILENAMES: tuple[str, ...] = (
    _ACTIVE_FILENAME,
    _INSTALLED_LOCK_FILENAME,
    _PROVENANCE_FILENAME,
    _ACCELERATED_METADATA_FILENAME,
)

# Metadata-cleanup actions (ZA-M1-3A.3): one per observational store.
# A PRUNABLE_CLEAN_METADATA slot carries one CLEAN_* action per store
# that references it.
CLEAN_ACCELERATED_METADATA = "CLEAN_ACCELERATED_METADATA"
CLEAN_INSTALLED_LOCK = "CLEAN_INSTALLED_LOCK"
CLEAN_PRODUCT_PROVENANCE = "CLEAN_PRODUCT_PROVENANCE"

# Legacy name (ZA-M1-2K) kept as an alias for callers/tests.
METADATA_ACTION_CLEAN_ACCELERATED = CLEAN_ACCELERATED_METADATA

# Inter-store purge order (deterministic; documented in
# :func:`_purge_slot_metadata_entries`).  The order is safe at every
# intermediate point: the slot directory still exists until ALL stores
# have been purged, so "metadata → existing directory" never breaks.
_METADATA_CLEAN_ORDER: tuple[str, ...] = (
    CLEAN_INSTALLED_LOCK,
    CLEAN_PRODUCT_PROVENANCE,
    CLEAN_ACCELERATED_METADATA,
)

_CLEAN_ACTION_TO_FILENAME: dict[str, str] = {
    CLEAN_INSTALLED_LOCK: _INSTALLED_LOCK_FILENAME,
    CLEAN_PRODUCT_PROVENANCE: _PROVENANCE_FILENAME,
    CLEAN_ACCELERATED_METADATA: _ACCELERATED_METADATA_FILENAME,
}
_STORE_FILENAME_TO_CLEAN_ACTION: dict[str, str] = {
    filename: action
    for action, filename in _CLEAN_ACTION_TO_FILENAME.items()
}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class SlotCategory(StrEnum):
    """Classification of one slot entry by the read-only planner."""

    ACTIVE = "ACTIVE"
    PREVIOUS = "PREVIOUS"
    PREVIOUS_RELEASABLE = "PREVIOUS_RELEASABLE"
    # Legacy member (ZA-M1-2K): the planner no longer produces
    # REFERENCED — historical store references now classify as
    # PRUNABLE_CLEAN_METADATA (ZA-M1-3A.3).  Kept for API
    # compatibility with existing consumers; never emitted by
    # build_gc_plan.
    REFERENCED = "REFERENCED"
    PRUNABLE = "PRUNABLE"
    PRUNABLE_CLEAN_METADATA = "PRUNABLE_CLEAN_METADATA"
    UNKNOWN = "UNKNOWN"
    STALE_METADATA = "STALE_METADATA"  # informational: record key w/o dir


class GcStatus(StrEnum):
    READY = "READY"
    BLOCKED = "BLOCKED"


# Destructive slot categories: the apply engine may delete these (and the
# planner counts them toward total_recoverable_bytes).  PREVIOUS_RELEASABLE
# is the ZA-M1-4 LOT A "rollback slot with a verified startup-health
# confirmation" category.
_DESTRUCTIVE_CATEGORIES: tuple[SlotCategory, ...] = (
    SlotCategory.PRUNABLE,
    SlotCategory.PRUNABLE_CLEAN_METADATA,
    SlotCategory.PREVIOUS_RELEASABLE,
)


@dataclass(frozen=True, slots=True)
class GcSlotEntry:
    """One slot entry classified by the planner.

    ``path`` is the canonical directory path for slots present on disk,
    or the *nominal* (non-existent) path for informational
    ``STALE_METADATA`` / ``UNKNOWN`` entries.

    ``references`` lists the state records (file names) that reference
    the slot: ``["active.json"]`` for active/previous, the store file
    names for referenced slots.

    ``estimated_bytes`` is a deterministic filesystem estimate (see
    :func:`_estimate_slot_bytes`); it is 0 for informational entries.
    """

    slot_id: str
    path: Path
    category: SlotCategory
    reason: str
    references: tuple[str, ...] = ()
    metadata_actions: tuple[str, ...] = ()
    estimated_bytes: int = 0

    @property
    def metadata_action(self) -> str | None:
        """Legacy single-store accessor (ZA-M1-2K).

        Returns the sole metadata-cleanup action when exactly one store
        references the slot, ``None`` otherwise (zero stores, or
        several stores — the legacy scalar cannot express multi-store
        cleanup).  New code should read :attr:`metadata_actions`.
        """
        if len(self.metadata_actions) == 1:
            return self.metadata_actions[0]
        return None


@dataclass(frozen=True, slots=True)
class GcPlan:
    """Result of the read-only planner (never mutates anything)."""

    runtime_root: Path
    status: GcStatus
    active_slot_id: str | None
    previous_slot_id: str | None
    slots: tuple[GcSlotEntry, ...]
    total_recoverable_bytes: int
    blocking_reasons: tuple[str, ...]
    stale_metadata: tuple[str, ...]
    state_fingerprint: str


@dataclass(frozen=True, slots=True)
class GcResult:
    """Result of :func:`apply_gc_plan`.

    ``reclaimed_bytes`` is the sum of the *estimated* bytes of the slots
    actually deleted — an estimate, not a measurement.

    ``stale`` is ``True`` only when the plan was refused because the
    state fingerprint changed between plan build and apply (no deletion
    occurred in that case).
    """

    deleted_slots: tuple[str, ...]
    reclaimed_bytes: int
    preserved_slots: tuple[str, ...]
    errors: tuple[str, ...]
    stale: bool


# ---------------------------------------------------------------------------
# Exceptions (private)
# ---------------------------------------------------------------------------


class _SlotDeleteError(Exception):
    """Raised when a slot deletion is refused or partially failed."""


class _MetadataPurgeError(Exception):
    """Raised when an observational-store metadata purge must be refused."""


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def _validate_slot_entry(slots_root: Path, slot_id: str) -> Path | None:
    """Validate a slot entry strictly.  Returns the safe path or ``None``.

    Checks, in order:

    1. ``slot_id`` must match ``^rt-[0-9a-f]{12}$`` exactly;
    2. ``slots_root`` itself must exist, be a real directory, and not be
       a symlink (``lstat``);
    3. the candidate path must resolve to a strict descendant of the
       resolved ``slots_root``;
    4. the resolved candidate must equal the literal candidate (no
       symlink component anywhere between the root and the entry);
    5. the final entry must exist, not be a symlink (``lstat``), and be a
       directory.

    Never raises for adversarial inputs — invalid entries yield ``None``.
    """
    if not isinstance(slot_id, str) or not _SLOT_ID_RE.match(slot_id):
        return None

    raw_root = Path(slots_root)
    try:
        root_st = raw_root.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(root_st.st_mode) or not stat.S_ISDIR(root_st.st_mode):
        return None

    resolved_root = raw_root.resolve()

    candidate = resolved_root / slot_id
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        return None
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        return None
    if resolved != candidate:
        return None

    try:
        st = candidate.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return None

    return candidate


def _safe_delete_slot(slots_root: Path, slot_id: str) -> None:
    """Delete one slot directory with strict re-validation.

    *slot_id* is re-validated with :func:`_validate_slot_entry` (and the
    canonical :func:`validate_slot_id`) immediately before
    ``shutil.rmtree``.  A user-supplied path is NEVER accepted — only a
    slot id, re-resolved under ``slots_root``.

    ``rmtree`` errors are collected via an ``onerror`` collector (deletion
    continues, nothing is hidden); a validation failure or any collected
    error raises :class:`_SlotDeleteError`.
    """
    try:
        validate_slot_id(slot_id)  # canonical validator, defence in depth
    except ValueError as exc:
        raise _SlotDeleteError(f"invalid slot id {slot_id!r}: {exc}") from exc
    path = _validate_slot_entry(slots_root, slot_id)
    if path is None:
        raise _SlotDeleteError(
            f"slot entry {slot_id!r} failed path re-validation under "
            f"{slots_root}; refusing to delete"
        )

    collected: list[str] = []

    def _onerror(func, path_str, exc_info) -> None:
        collected.append(
            f"{getattr(func, '__name__', func)} failed for "
            f"{path_str}: {exc_info[1]}"
        )

    shutil.rmtree(path, onerror=_onerror)
    if collected:
        raise _SlotDeleteError("; ".join(collected))


# ---------------------------------------------------------------------------
# Byte-size estimation (deterministic, documented as an estimate)
# ---------------------------------------------------------------------------


def _estimate_slot_bytes(slot_path: Path) -> int:
    """Deterministic size estimate of a slot directory.

    Walks with ``os.walk(..., followlinks=False)`` — symlinks are never
    traversed — and sums ``st_size`` from ``os.lstat`` for every entry
    encountered (regular files, directories, and symlinks; a symlink
    counts the size of the link itself), plus the slot directory's own
    entry.  This is an **estimate**: it depends on the filesystem (block
    sizes, dir inode sizes) and ignores hard-link multiplicity.
    """
    total = 0
    try:
        total += os.lstat(slot_path).st_size
    except OSError:
        pass
    for dirpath, dirnames, filenames in os.walk(
        slot_path, topdown=True, followlinks=False
    ):
        base = Path(dirpath)
        for name in dirnames:
            try:
                total += os.lstat(base / name).st_size
            except OSError:
                pass
        for name in filenames:
            try:
                total += os.lstat(base / name).st_size
            except OSError:
                pass
    return total


# ---------------------------------------------------------------------------
# Planner (pure, read-only)
# ---------------------------------------------------------------------------


def build_gc_plan(runtime_root: Path) -> GcPlan:
    """Classify every slot of *runtime_root* — **never writes anything**.

    Reads exactly the 4 state records (``active.json``,
    ``installed-lock.json``, ``product-provenance.json``,
    ``accelerated-metadata.json``) and the ``slots/`` directory entries.
    Any inconsistency → ``GcStatus.BLOCKED`` with a precise reason and
    **no** ``PRUNABLE``/``PRUNABLE_CLEAN_METADATA`` slot (fail-closed).
    """
    root = Path(runtime_root).resolve()
    state_dir = root / "state"
    slots_root = root / "slots"

    blocking: list[str] = []
    stale_meta: list[str] = []

    # -- record 1: active.json (canonical loader) ---------------------------
    active_status = load_active_state(
        state_dir / _ACTIVE_FILENAME, layout_root=root
    )
    active_id: str | None = None
    previous_id: str | None = None
    if active_status.state == RuntimeState.ABSENT:
        blocking.append(
            "active.json is absent: the protected set (active/previous) "
            "cannot be established"
        )
    elif active_status.state == RuntimeState.BROKEN:
        blocking.append(
            f"active.json is corrupt or malformed: {active_status.reason}"
        )
    else:
        active_id = active_status.active_slot_id
        previous_id = active_status.previous_slot_id
        for field, value in (
            ("active_slot", active_id),
            ("previous_slot", previous_id),
        ):
            if value is None:
                continue
            if not _SLOT_ID_RE.match(value):
                blocking.append(
                    f"active.json {field} {value!r} does not match the "
                    f"slot id format ^rt-[0-9a-f]{{12}}$"
                )

    # -- records 2-4: the three stores ---------------------------------------
    # raw_bytes[name] -> bytes (present) or None (absent); corrupt records
    # are recorded as blocking reasons and carry NO usable references.
    raw_bytes: dict[str, bytes | None] = {}
    store_slot_refs: dict[str, list[str]] = {}
    for name in (
        _INSTALLED_LOCK_FILENAME,
        _PROVENANCE_FILENAME,
        _ACCELERATED_METADATA_FILENAME,
    ):
        path = state_dir / name
        if not path.is_file():
            raw_bytes[name] = None  # absent → no references
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            blocking.append(f"{name} is unreadable: {exc}")
            raw_bytes[name] = None
            continue
        raw_bytes[name] = raw
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            blocking.append(f"{name} is invalid JSON: {exc}")
            continue
        if not isinstance(payload, dict):
            blocking.append(f"{name} root must be a JSON object")
            continue
        if payload.get("schema_version") != _SCHEMA_VERSION:
            blocking.append(
                f"{name} has unexpected schema_version "
                f"{payload.get('schema_version')!r} (expected "
                f"{_SCHEMA_VERSION})"
            )
            continue
        slots = payload.get("slots")
        if not isinstance(slots, dict):
            blocking.append(f"{name} has no 'slots' object")
            continue
        for key in slots:
            if not isinstance(key, str) or not _SLOT_ID_RE.match(key):
                blocking.append(
                    f"{name} contains an invalid slot id key: {key!r}"
                )
                continue
            store_slot_refs.setdefault(key, []).append(name)

    # -- startup-health confirmation (ZA-M1-4 LOT A) --------------------------
    # active_raw is read here (once) so the confirmation fingerprint can be
    # matched against the exact bytes the planner already saw.  The
    # confirmation file itself is NOT part of the state fingerprint.
    active_raw: bytes | None = None
    active_path = state_dir / _ACTIVE_FILENAME
    if active_path.is_file():
        try:
            active_raw = active_path.read_bytes()
        except OSError:
            active_raw = None

    record_bytes: dict[str, bytes | None] = {
        _ACTIVE_FILENAME: active_raw,
        _INSTALLED_LOCK_FILENAME: raw_bytes.get(_INSTALLED_LOCK_FILENAME),
        _PROVENANCE_FILENAME: raw_bytes.get(_PROVENANCE_FILENAME),
        _ACCELERATED_METADATA_FILENAME: raw_bytes.get(
            _ACCELERATED_METADATA_FILENAME
        ),
    }
    confirmation = load_startup_health(state_dir)
    previous_releasable = (
        active_status.state == RuntimeState.READY
        and previous_id is not None
        and confirmation is not None
        and confirmation.active_slot_id == active_id
        and confirmation.records_fingerprint
        == compute_records_fingerprint(record_bytes)
    )

    # -- slots/ enumeration ---------------------------------------------------
    disk_paths: dict[str, Path] = {}
    disk_invalid: dict[str, str] = {}
    if slots_root.exists():
        if not slots_root.is_dir():
            blocking.append("slots/ exists but is not a directory")
        else:
            try:
                names = sorted(os.listdir(slots_root))
            except OSError as exc:
                blocking.append(f"slots/ is unreadable: {exc}")
                names = []
            for name in names:
                if not _SLOT_ID_RE.match(name):
                    disk_invalid[name] = (
                        f"invalid slot entry name {name!r} (does not match "
                        f"^rt-[0-9a-f]{{12}}$)"
                    )
                    blocking.append(
                        f"slots/ contains an invalid entry name: {name!r} "
                        f"(does not match ^rt-[0-9a-f]{{12}}$)"
                    )
                    continue
                path = _validate_slot_entry(slots_root, name)
                if path is None:
                    disk_invalid[name] = (
                        f"slot entry {name!r} is not a plain directory "
                        f"(symlink or non-directory)"
                    )
                    blocking.append(
                        f"slots/ entry {name!r} is not a plain directory "
                        f"(symlink or non-directory)"
                    )
                    continue
                disk_paths[name] = path

    # -- referenced-but-absent slots (REPAIR_REQUIRED) ------------------------
    for pointer_field, pointer_value, record_name in (
        ("active_slot", active_id, _ACTIVE_FILENAME),
        ("previous_slot", previous_id, _ACTIVE_FILENAME),
    ):
        if pointer_value is None:
            continue
        if pointer_value in disk_paths or pointer_value in disk_invalid:
            continue
        blocking.append(
            f"{record_name} {pointer_field} {pointer_value!r} has no "
            f"directory in slots/ (REPAIR_REQUIRED)"
        )
        stale_meta.append(
            f"{pointer_value!r}: referenced by {record_name} but absent "
            f"from slots/ (REPAIR_REQUIRED)"
        )
    for ref_id in sorted(store_slot_refs):
        if ref_id in disk_paths or ref_id in disk_invalid:
            continue
        ref_names = ", ".join(sorted(store_slot_refs[ref_id]))
        blocking.append(
            f"{ref_names} reference(s) slot {ref_id!r} with no directory "
            f"in slots/ (REPAIR_REQUIRED)"
        )
        stale_meta.append(
            f"{ref_id!r}: referenced by {ref_names} but absent from "
            f"slots/ (REPAIR_REQUIRED)"
        )

    # -- classification --------------------------------------------------------
    all_ids: set[str] = set(disk_paths) | set(disk_invalid) | set(store_slot_refs)
    if active_id is not None:
        all_ids.add(active_id)
    if previous_id is not None:
        all_ids.add(previous_id)

    entries: list[GcSlotEntry] = []
    for slot_id in sorted(all_ids):
        if slot_id in disk_invalid:
            entries.append(
                GcSlotEntry(
                    slot_id=slot_id,
                    path=slots_root / slot_id,
                    category=SlotCategory.UNKNOWN,
                    reason=disk_invalid[slot_id],
                )
            )
            continue
        if slot_id not in disk_paths:
            # Referenced by a record but absent from disk.
            ref_names = sorted(store_slot_refs.get(slot_id, []))
            if slot_id == active_id and active_status.state == RuntimeState.READY:
                ref_names = [_ACTIVE_FILENAME]
            elif slot_id == previous_id and active_status.state == RuntimeState.READY:
                ref_names = [_ACTIVE_FILENAME]
            entries.append(
                GcSlotEntry(
                    slot_id=slot_id,
                    path=slots_root / slot_id,
                    category=SlotCategory.STALE_METADATA,
                    reason=(
                        f"referenced by {', '.join(ref_names) or 'a record'} "
                        f"but no directory exists in slots/ (REPAIR_REQUIRED)"
                    ),
                    references=tuple(ref_names),
                )
            )
            continue
        path = disk_paths[slot_id]
        estimated = _estimate_slot_bytes(path)
        actions: tuple[str, ...] = ()
        if active_status.state == RuntimeState.READY and slot_id == active_id:
            category = SlotCategory.ACTIVE
            reason = "protected: currently active slot"
            refs = (_ACTIVE_FILENAME,)
        elif active_status.state == RuntimeState.READY and slot_id == previous_id:
            if previous_releasable:
                ref_names = tuple(sorted(store_slot_refs.get(slot_id, [])))
                actions = tuple(
                    action
                    for action in _METADATA_CLEAN_ORDER
                    if action
                    in {_STORE_FILENAME_TO_CLEAN_ACTION[n] for n in ref_names}
                )
                category = SlotCategory.PREVIOUS_RELEASABLE
                reason = (
                    "rollback target; releasable (ACTIVE startup health "
                    "confirmed)"
                )
                refs = ref_names
            else:
                category = SlotCategory.PREVIOUS
                reason = "protected: rollback target (previous slot)"
                refs = (_ACTIVE_FILENAME,)
        elif slot_id in store_slot_refs:
            ref_names = tuple(sorted(store_slot_refs[slot_id]))
            actions = tuple(
                action
                for action in _METADATA_CLEAN_ORDER
                if action
                in {_STORE_FILENAME_TO_CLEAN_ACTION[n] for n in ref_names}
            )
            category = SlotCategory.PRUNABLE_CLEAN_METADATA
            reason = (
                "historical slot (outside active/previous) referenced "
                f"by {', '.join(ref_names)}; its metadata entries are "
                "purged before the slot directory is removed"
            )
            refs = ref_names
        else:
            category = SlotCategory.PRUNABLE
            reason = "not referenced by any state record"
            refs = ()
        entries.append(
            GcSlotEntry(
                slot_id=slot_id,
                path=path,
                category=category,
                reason=reason,
                references=refs,
                metadata_actions=(
                    actions
                    if category
                    in (
                        SlotCategory.PRUNABLE_CLEAN_METADATA,
                        SlotCategory.PREVIOUS_RELEASABLE,
                    )
                    else ()
                ),
                estimated_bytes=estimated,
            )
        )

    # -- fail-closed downgrade: no destructive proposal when BLOCKED -----------
    blocked = bool(blocking)
    if blocked:
        downgraded: list[GcSlotEntry] = []
        for entry in entries:
            if entry.category in _DESTRUCTIVE_CATEGORIES:
                downgraded.append(
                    GcSlotEntry(
                        slot_id=entry.slot_id,
                        path=entry.path,
                        category=SlotCategory.UNKNOWN,
                        reason=(
                            "plan is BLOCKED: no destructive categorization "
                            "is performed"
                        ),
                        references=entry.references,
                        metadata_actions=(),
                        estimated_bytes=entry.estimated_bytes,
                    )
                )
            else:
                downgraded.append(entry)
        entries = downgraded

    total_recoverable = sum(
        entry.estimated_bytes
        for entry in entries
        if entry.category in _DESTRUCTIVE_CATEGORIES
    )

    fingerprint = _compute_state_fingerprint(
        slots_root=slots_root,
        raw_bytes=raw_bytes,
        active_raw=active_raw,
    )

    return GcPlan(
        runtime_root=root,
        status=GcStatus.BLOCKED if blocked else GcStatus.READY,
        active_slot_id=active_id,
        previous_slot_id=previous_id,
        slots=tuple(entries),
        total_recoverable_bytes=total_recoverable,
        blocking_reasons=tuple(sorted(blocking)),
        stale_metadata=tuple(sorted(stale_meta)),
        state_fingerprint=fingerprint,
    )


# ---------------------------------------------------------------------------
# State fingerprint
# ---------------------------------------------------------------------------


def _compute_state_fingerprint(
    *,
    slots_root: Path,
    raw_bytes: dict[str, bytes | None],
    active_raw: bytes | None,
) -> str:
    """Deterministic sha256 fingerprint of the GC-relevant state.

    Composition: for each of the 4 records (fixed order) the sha256 of
    its raw bytes (marker ``A`` when absent or unreadable),
    followed by the sorted list of ``"<name>,<mtime_ns>,<size>"`` for
    every ``slots/`` entry (``lstat``).  Two identical states always
    produce the same fingerprint; any record byte or slot-entry change
    (mtime/size) produces a different one.
    """
    parts: list[bytes] = []
    all_raw = dict(raw_bytes)
    all_raw[_ACTIVE_FILENAME] = active_raw
    for name in _RECORD_FILENAMES:
        raw = all_raw.get(name)
        if raw is None:
            # Absent or unreadable record → marker (unreadable records
            # always BLOCK the plan, so the fingerprint only matters for
            # absent-vs-absent comparisons in practice).
            parts.append(b"A")
        else:
            parts.append(b"F" + hashlib.sha256(raw).digest())

    entry_parts: list[str] = []
    if slots_root.is_dir():
        try:
            names = sorted(os.listdir(slots_root))
        except OSError:
            names = []
        for name in names:
            try:
                st = os.lstat(slots_root / name)
            except OSError:
                entry_parts.append(f"{name},?,?")
                continue
            entry_parts.append(f"{name},{st.st_mtime_ns},{st.st_size}")
    for ep in entry_parts:
        parts.append(ep.encode("utf-8") + b"\n")

    return hashlib.sha256(b"".join(parts)).hexdigest()


# ---------------------------------------------------------------------------
# Metadata purge (per-store, fail-closed) — ZA-M1-3A.3
# ---------------------------------------------------------------------------


def _load_store_payload(
    state_dir: Path,
    store_filename: str,
) -> tuple[dict, dict]:
    """Read + structurally validate one observational store file.

    Returns ``(payload, slots)`` — the full JSON object and its
    ``slots`` mapping.  Raises :class:`_MetadataPurgeError` (fail
    closed, no write) when the file is unreadable, invalid JSON, not a
    JSON object, carries an unexpected ``schema_version``, or has no
    ``slots`` object.
    """
    if store_filename not in _STORE_FILENAME_TO_CLEAN_ACTION:
        raise _MetadataPurgeError(
            f"unknown observational store {store_filename!r}; refusing "
            f"metadata purge"
        )
    path = Path(state_dir) / store_filename
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _MetadataPurgeError(
            f"cannot read {store_filename}: {exc}"
        ) from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _MetadataPurgeError(
            f"{store_filename} is invalid JSON: {exc}"
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != _SCHEMA_VERSION
    ):
        raise _MetadataPurgeError(
            f"{store_filename} is corrupt or has an unexpected schema; "
            f"refusing to modify it"
        )
    slots = payload.get("slots")
    if not isinstance(slots, dict):
        raise _MetadataPurgeError(
            f"{store_filename} has no 'slots' object; refusing to modify it"
        )
    return payload, slots


def _validate_purge_targets(
    slots_root: Path,
    slots: dict,
    purge_ids: Sequence[str],
) -> None:
    """Fail-closed pre-write validation of *purge_ids* (no write).

    Raises :class:`_MetadataPurgeError` when:

    * a purge target is absent from ``slots/`` on disk (under the
      ZA-M1-3A.3 apply order the metadata purge runs BEFORE the
      directory removal, so the directory must still exist; if it does
      not, the state changed under us);
    * a purge target is not present in the store (the file changed
      since the plan was built).
    """
    for slot_id in purge_ids:
        validate_slot_id(slot_id)  # defence in depth
        if _validate_slot_entry(slots_root, slot_id) is None:
            raise _MetadataPurgeError(
                f"slot {slot_id!r} is absent from slots/ (state changed "
                f"since the plan was built); refusing metadata purge"
            )
        if slot_id not in slots:
            raise _MetadataPurgeError(
                f"the store changed since the plan was built (entry for "
                f"{slot_id!r} is missing); refusing metadata purge"
            )


def _write_store_payload(
    state_dir: Path,
    store_filename: str,
    payload: dict,
    slots: dict,
    purge_ids: Sequence[str],
) -> None:
    """Atomically rewrite one store without *purge_ids*.

    Everything except the named slots is preserved.  The file is
    written atomically (``mkstemp`` in ``state_dir`` + ``fsync`` +
    ``os.replace``, the same pattern as :mod:`zealfie.runtime.state`).
    A no-op when no target is present.
    """
    purge_set = set(purge_ids)
    remaining = {k: v for k, v in slots.items() if k not in purge_set}
    if len(remaining) == len(slots):
        return  # nothing to purge

    new_payload = dict(payload)
    new_payload["slots"] = remaining
    text = json.dumps(new_payload, indent=2, sort_keys=True) + "\n"

    path = Path(state_dir) / store_filename
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        suffix=".json",
        prefix=f".{Path(store_filename).stem}-",
        dir=str(path.parent),
    )
    try:
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)

    os.replace(tmp_name, str(path))


def _purge_store_slot_entries(
    state_dir: Path,
    slots_root: Path,
    store_filename: str,
    purge_ids: Sequence[str],
) -> None:
    """Remove *purge_ids* from one observational store atomically.

    Generic per-store read-modify-write (ZA-M1-3A.3): the current file
    is re-read, the named slots are removed, **everything else is
    preserved**, and the file is written atomically.  Works for
    ``installed-lock.json``, ``product-provenance.json`` and
    ``accelerated-metadata.json`` (all three share the
    ``schema_version: 1`` / ``slots`` object shape).

    Fail-closed refusals (raise :class:`_MetadataPurgeError`, no write):
    see :func:`_load_store_payload` and :func:`_validate_purge_targets`.

    Never touches ``active.json``.
    """
    payload, slots = _load_store_payload(state_dir, store_filename)
    _validate_purge_targets(slots_root, slots, purge_ids)
    _write_store_payload(state_dir, store_filename, payload, slots, purge_ids)


def _purge_slot_metadata_entries(
    state_dir: Path,
    slots_root: Path,
    slot_id: str,
    actions: Sequence[str],
) -> None:
    """Purge *slot_id* from every store named by *actions* (ZA-M1-3A.3).

    Per-slot orchestrator around :func:`_purge_store_slot_entries`,
    with a strict two-phase discipline.

    **Inter-store order** (documented choice): the stores are purged in
    the fixed deterministic order ``installed-lock.json`` →
    ``product-provenance.json`` → ``accelerated-metadata.json``
    (``_METADATA_CLEAN_ORDER``, the same record order the state
    fingerprint uses).  The order is chosen for determinism; it is
    *safe* at every intermediate point because the slot directory
    still exists until ALL of its stores have been purged — the
    invariant "metadata → existing directory" holds across every
    intermediate state, so an interrupted apply can never produce a
    ``REPAIR_REQUIRED`` orphan reference.

    **Phase 1 — validation (no writes):** every store named by
    *actions* is read and validated (readable, valid JSON, correct
    schema, target present in the file, target still on disk).  Any
    failure raises :class:`_MetadataPurgeError` **before any store is
    modified** — a refused purge never leaves a partial per-store
    purge for the slot.

    **Phase 2 — writes:** each validated store is rewritten without
    the slot, atomically per store (mkstemp + fsync + ``os.replace``).
    A write-phase failure may leave *earlier* stores of this slot
    already purged (per-store atomicity is never broken); the caller
    preserves the slot directory in that case, which keeps the state
    consistent (metadata never points at a deleted directory) and
    self-healing (the next plan re-classifies the directory as
    ``PRUNABLE``).

    Never touches ``active.json``.
    """
    unknown = [a for a in actions if a not in _METADATA_CLEAN_ORDER]
    if unknown:
        raise _MetadataPurgeError(
            f"unknown metadata action(s) {unknown!r}; refusing purge"
        )
    store_filenames = [
        _CLEAN_ACTION_TO_FILENAME[action]
        for action in _METADATA_CLEAN_ORDER
        if action in actions
    ]

    # Phase 1: validate every store BEFORE writing any of them.
    loaded: list[tuple[str, dict, dict]] = []
    for store_filename in store_filenames:
        payload, slots = _load_store_payload(state_dir, store_filename)
        _validate_purge_targets(slots_root, slots, (slot_id,))
        loaded.append((store_filename, payload, slots))

    # Phase 2: write each store atomically (per-store atomicity).
    for store_filename, payload, slots in loaded:
        _write_store_payload(
            state_dir, store_filename, payload, slots, (slot_id,)
        )


# ---------------------------------------------------------------------------
# Apply engine
# ---------------------------------------------------------------------------


def apply_gc_plan(runtime_root: Path, plan: GcPlan) -> GcResult:
    """Apply a :class:`GcPlan` — deletions + metadata hygiene.

    Mandatory sequence:

    1. rebuild a **fresh** plan from disk;
    2. fresh ``BLOCKED`` → refuse (errors, ``stale=False``);
    3. fresh fingerprint != plan fingerprint → refuse ``STALE_PLAN``
       (``stale=True``), **no deletion**;
    4. supplied plan not ``READY`` → refuse;
    5. process every slot categorised ``PRUNABLE`` /
       ``PRUNABLE_CLEAN_METADATA`` **in both** fresh and supplied plan,
       sorted by slot id.  For ``PRUNABLE_CLEAN_METADATA`` slots the
       metadata purge — every store named by the fresh entry's
       ``metadata_actions``, in the documented inter-store order —
       runs strictly **BEFORE** the directory removal; when any store
       purge for a slot fails, the slot directory is **not** removed
       this round and the error is recorded (metadata never points at
       a deleted directory → never ``REPAIR_REQUIRED``);
    6. every deletion is re-validated path-safely immediately before
       ``rmtree``; active/previous of the fresh plan are re-checked
       per slot (defence in depth — a forged plan cannot delete
       them); per-slot errors are recorded and the remaining slots
       are still processed;
    7. a failed directory removal after a successful metadata purge
       leaves an unreferenced directory on disk — self-healing
       (``PRUNABLE`` on the next plan), never ``REPAIR_REQUIRED``.

    Except for the PREVIOUS_RELEASABLE release path (ZA-M1-4 LOT A),
    where ``active.json`` is atomically rewritten to drop ``previous_slot``
    before the rollback slot is removed, ``active.json`` is never touched.

    ZA-M1-2L (D1): the whole apply window — the fresh re-plan and state
    fingerprint revalidation, every slot deletion, and the accelerated-
    metadata purge — runs under the ``runtime-gc`` mutation lease,
    acquired at entry and released on every exit path including
    exceptions.  The M1-2K stale-check machinery is unchanged; it simply
    executes under the lease (mission §15 revalidation).
    """
    with RuntimeMutationLock(runtime_root).acquire(OPERATION_RUNTIME_GC):
        return _apply_gc_plan_locked(runtime_root, plan)


def _apply_gc_plan_locked(runtime_root: Path, plan: GcPlan) -> GcResult:
    """Apply a :class:`GcPlan` — deletions + metadata hygiene.

    Mandatory sequence:

    1. rebuild a **fresh** plan from disk;
    2. fresh ``BLOCKED`` → refuse (errors, ``stale=False``);
    3. fresh fingerprint != plan fingerprint → refuse ``STALE_PLAN``
       (``stale=True``), **no deletion**;
    4. supplied plan not ``READY`` → refuse;
    5. process every slot categorised ``PRUNABLE`` /
       ``PRUNABLE_CLEAN_METADATA`` **in both** fresh and supplied plan,
       sorted by slot id.  For ``PRUNABLE_CLEAN_METADATA`` slots the
       metadata purge — every store named by the fresh entry's
       ``metadata_actions``, in the documented inter-store order —
       runs strictly **BEFORE** the directory removal; when any store
       purge for a slot fails, the slot directory is **not** removed
       this round and the error is recorded (metadata never points at
       a deleted directory → never ``REPAIR_REQUIRED``);
    6. every deletion is re-validated path-safely immediately before
       ``rmtree``; active/previous of the fresh plan are re-checked
       per slot (defence in depth — a forged plan cannot delete
       them); per-slot errors are recorded and the remaining slots
       are still processed;
    7. a failed directory removal after a successful metadata purge
       leaves an unreferenced directory on disk — self-healing
       (``PRUNABLE`` on the next plan), never ``REPAIR_REQUIRED``.

    Except for the PREVIOUS_RELEASABLE release path (ZA-M1-4 LOT A),
    where ``active.json`` is atomically rewritten to drop ``previous_slot``
    before the rollback slot is removed, ``active.json`` is never touched.
    """
    root = Path(runtime_root).resolve()
    state_dir = root / "state"
    slots_root = root / "slots"

    fresh = build_gc_plan(root)

    if fresh.status == GcStatus.BLOCKED:
        return GcResult(
            deleted_slots=(),
            reclaimed_bytes=0,
            preserved_slots=tuple(s.slot_id for s in fresh.slots),
            errors=(
                "refusing to apply: fresh plan is BLOCKED ("
                + "; ".join(fresh.blocking_reasons)
                + ")",
            ),
            stale=False,
        )

    if fresh.state_fingerprint != plan.state_fingerprint:
        return GcResult(
            deleted_slots=(),
            reclaimed_bytes=0,
            preserved_slots=tuple(s.slot_id for s in fresh.slots),
            errors=(
                "refusing to apply: state changed since the plan was built "
                "(stale plan)",
            ),
            stale=True,
        )

    if plan.status != GcStatus.READY:
        return GcResult(
            deleted_slots=(),
            reclaimed_bytes=0,
            preserved_slots=tuple(s.slot_id for s in fresh.slots),
            errors=("refusing to apply: supplied plan is not READY",),
            stale=False,
        )

    if plan.runtime_root != root:
        return GcResult(
            deleted_slots=(),
            reclaimed_bytes=0,
            preserved_slots=tuple(s.slot_id for s in fresh.slots),
            errors=(
                "refusing to apply: plan was built for a different runtime "
                f"root ({plan.runtime_root} != {root})",
            ),
            stale=False,
        )

    fresh_by_id = {entry.slot_id: entry for entry in fresh.slots}
    plan_by_id = {entry.slot_id: entry for entry in plan.slots}
    destructive = (
        SlotCategory.PRUNABLE,
        SlotCategory.PRUNABLE_CLEAN_METADATA,
        SlotCategory.PREVIOUS_RELEASABLE,
    )
    delete_set = {
        slot_id
        for slot_id, entry in fresh_by_id.items()
        if entry.category in destructive
        and slot_id in plan_by_id
        and plan_by_id[slot_id].category in destructive
    }

    deleted: list[str] = []
    errors: list[str] = []
    reclaimed = 0
    for slot_id in sorted(delete_set):
        fresh_entry = fresh_by_id[slot_id]
        # Defence in depth: never delete the active slot; allow deleting
        # the previous slot ONLY when the fresh plan itself classifies it
        # PREVIOUS_RELEASABLE (a forged/stale plan that marks PREVIOUS as
        # destructive without a valid confirmation is never in delete_set
        # — the fresh plan still classifies it PREVIOUS).
        if slot_id == fresh.active_slot_id:
            errors.append(
                f"skipping {slot_id!r}: active guard (defence in depth)"
            )
            continue
        if (
            slot_id == fresh.previous_slot_id
            and fresh_entry.category is not SlotCategory.PREVIOUS_RELEASABLE
        ):
            errors.append(
                f"skipping {slot_id!r}: previous guard (defence in depth; "
                f"not releasable)"
            )
            continue

        if fresh_entry.category is SlotCategory.PREVIOUS_RELEASABLE:
            # Fail-closed release order (ZA-M1-4 LOT A):
            #   1. atomically clear previous_slot in active.json,
            #   2. purge the slot's metadata from the observational stores,
            #   3. delete the slot directory.
            # Clearing the pointer FIRST guarantees active.json never points
            # at a deleted slot: if step 2 or 3 fails, the slot directory
            # still exists and simply becomes an unreferenced PRUNABLE on the
            # next plan (self-healing), never REPAIR_REQUIRED.
            try:
                clear_previous_slot(state_dir / _ACTIVE_FILENAME)
            except Exception as exc:
                errors.append(
                    f"failed to clear previous_slot for {slot_id!r}: {exc}; "
                    f"slot preserved this round"
                )
                continue
            actions = fresh_entry.metadata_actions
            if actions:
                try:
                    _purge_slot_metadata_entries(
                        state_dir, slots_root, slot_id, actions
                    )
                except Exception as exc:
                    errors.append(
                        f"metadata purge for slot {slot_id!r} refused: "
                        f"{exc}; slot directory preserved this round"
                    )
                    continue
            try:
                _safe_delete_slot(slots_root, slot_id)
            except Exception as exc:
                errors.append(f"failed to delete slot {slot_id!r}: {exc}")
                continue
            deleted.append(slot_id)
            reclaimed += fresh_entry.estimated_bytes
            continue

        actions = fresh_entry.metadata_actions
        if actions:
            # Metadata purge FIRST (every referencing store, ordered);
            # only when every store purge succeeded is the directory
            # removed.  A refused purge preserves the directory this
            # round — metadata never outlives the directory it points
            # at (no REPAIR_REQUIRED).
            try:
                _purge_slot_metadata_entries(
                    state_dir, slots_root, slot_id, actions
                )
            except Exception as exc:
                errors.append(
                    f"metadata purge for slot {slot_id!r} refused: {exc}; "
                    f"slot directory preserved this round"
                )
                continue
        try:
            _safe_delete_slot(slots_root, slot_id)
        except Exception as exc:
            errors.append(f"failed to delete slot {slot_id!r}: {exc}")
            continue
        deleted.append(slot_id)
        reclaimed += fresh_entry.estimated_bytes

    preserved = [
        slot_id
        for slot_id in sorted(fresh_by_id)
        if slot_id not in set(deleted)
    ]
    return GcResult(
        deleted_slots=tuple(deleted),
        reclaimed_bytes=reclaimed,
        preserved_slots=tuple(preserved),
        errors=tuple(errors),
        stale=False,
    )
