"""Fresh-startup health confirmation for the shared runtime (ZA-M1-4 LOT A).

Determines whether the persisted ACTIVE runtime is genuinely healthy
before making the rollback (PREVIOUS) slot GC-eligible.

Motivation: :func:`zealfie.runtime.gc.build_gc_plan` hard-protects BOTH
ACTIVE and PREVIOUS forever.  A GPU runtime is ~3.25 GB, so permanent
N-1 retention doubles the long-lived footprint.  The verified artifact
cache already provides an extra recovery mechanism, so PREVIOUS may
become releasable once a fresh, healthy startup has confirmed the
persisted ACTIVE state.

The confirmation is a separate, inert file
(``state/startup-health.json``) that is NOT part of the GC state
fingerprint (which covers only the 4 records + ``slots/``).  It is
written only after every health check below passes; it is never written
blindly at startup and never deletes anything itself.

Health checks are pure read/probe operations (persisted facts + minimal
live probes) and never write.  :func:`confirm_and_record_startup_health`
is the convenience entry point that runs them and records the
confirmation only when healthy.

Import discipline: this module imports only ``runtime.layout``,
``runtime.state``, ``runtime.probe`` and ``runtime.mutation_lock`` —
no store classes (the "readable" checks are strict structural reads so
corrupt files are actually detected), and no import of
:mod:`zealfie.runtime.gc` (which imports this module).  The fingerprint
composition must agree exactly with :mod:`zealfie.runtime.gc`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .layout import validate_slot_id
from .model import RuntimeState
from .mutation_lock import RuntimeMutationLock
from .probe import probe_runtime_distribution, probe_runtime_python_version
from .state import load_active_state

logger = logging.getLogger(__name__)

STARTUP_HEALTH_SCHEMA_VERSION = 1
STARTUP_HEALTH_FILENAME = "startup-health.json"

# Schema version shared by the three observational stores (mirrors
# installed_lock.INSTALLED_LOCK_SCHEMA_VERSION,
# provenance.PROVENANCE_SCHEMA_VERSION and
# acceleration.deployment.ACCELERATED_METADATA_SCHEMA_VERSION).  Kept
# local to stay import-light / cycle-free (same discipline as gc.py).
_STORE_SCHEMA_VERSION = 1

_ACTIVE_FILENAME = "active.json"
_INSTALLED_LOCK_FILENAME = "installed-lock.json"
_PROVENANCE_FILENAME = "product-provenance.json"
_ACCELERATED_METADATA_FILENAME = "accelerated-metadata.json"

# Fixed order for deterministic fingerprint composition.  This is the
# single source of truth; gc.py imports compute_state_fingerprint.
RECORD_FILENAMES: tuple[str, ...] = (
    _ACTIVE_FILENAME,
    _INSTALLED_LOCK_FILENAME,
    _PROVENANCE_FILENAME,
    _ACCELERATED_METADATA_FILENAME,
)

_STORE_FILENAMES: tuple[str, ...] = RECORD_FILENAMES[1:]

_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StartupHealthConfirmation:
    """Persisted proof that one ACTIVE slot passed a fresh startup check.

    ``confirmed_at`` is an ISO-8601 UTC string; ``records_fingerprint``
    is the 64-hex sha256 produced by :func:`compute_state_fingerprint`
    over the 4 state records plus the ``slots/`` directory entries at
    confirmation time (the full GC-relevant state fingerprint, not just
    the 4 records).
    """

    schema_version: int
    active_slot_id: str
    confirmed_at: str
    records_fingerprint: str


@dataclass(frozen=True, slots=True)
class StartupHealthResult:
    """Result of a startup health confirmation probe (read-only)."""

    healthy: bool
    active_slot_id: str | None
    reasons: tuple[str, ...]
    records_fingerprint: str | None


# ---------------------------------------------------------------------------
# State fingerprint (single source of truth, shared with gc.py)
# ---------------------------------------------------------------------------


def _records_fingerprint_parts(
    record_bytes: Mapping[str, bytes | None],
) -> list[bytes]:
    """Build the raw fingerprint parts for the 4 records (fixed order).

    For each record: ``b"A"`` when absent/unreadable (``None``), else
    ``b"F" + sha256(raw)``.  Shared with :func:`compute_state_fingerprint`
    so the record composition is defined in exactly one place.
    """
    parts: list[bytes] = []
    for name in RECORD_FILENAMES:
        raw = record_bytes.get(name)
        if raw is None:
            parts.append(b"A")
        else:
            parts.append(b"F" + hashlib.sha256(raw).digest())
    return parts


def compute_state_fingerprint(
    record_bytes: Mapping[str, bytes | None],
    slots_root: Path,
) -> str:
    """Deterministic sha256 fingerprint of the GC-relevant state.

    ``record_bytes`` maps the 4 record file names to their raw bytes
    (or ``None`` when absent/unreadable).  Composition (must agree
    exactly with :mod:`zealfie.runtime.gc`):

    1. the 4 records in fixed order — for each record ``b"A"`` when
       absent, else ``b"F" + sha256(raw)``;
    2. then the sorted ``"<name>,<mtime_ns>,<size>"`` entries of
       ``slots/`` (via ``os.lstat``, ``?`` on lstat failure).

    This is the single source of truth for the state fingerprint, used
    by both the confirmation (this module) and the GC planner
    (:mod:`zealfie.runtime.gc`).
    """
    parts: list[bytes] = list(_records_fingerprint_parts(record_bytes))

    entry_parts: list[str] = []
    slots_root = Path(slots_root)
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
# Confirmation load
# ---------------------------------------------------------------------------


def load_startup_health(state_dir: Path) -> StartupHealthConfirmation | None:
    """Load a startup-health confirmation, leniently.  Never raises.

    Returns ``None`` for a missing file, invalid JSON, wrong schema,
    invalid ``active_slot_id`` (canonical validator), missing/empty
    ``confirmed_at``, or a malformed 64-hex ``records_fingerprint``.
    """
    path = Path(state_dir) / STARTUP_HEALTH_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != STARTUP_HEALTH_SCHEMA_VERSION:
        return None
    active_id = payload.get("active_slot_id")
    if not isinstance(active_id, str):
        return None
    try:
        validate_slot_id(active_id)
    except ValueError:
        return None
    confirmed_at = payload.get("confirmed_at")
    if not isinstance(confirmed_at, str) or not confirmed_at:
        return None
    fingerprint = payload.get("records_fingerprint")
    if not isinstance(fingerprint, str) or not _FINGERPRINT_RE.match(fingerprint):
        return None
    return StartupHealthConfirmation(
        schema_version=STARTUP_HEALTH_SCHEMA_VERSION,
        active_slot_id=active_id,
        confirmed_at=confirmed_at,
        records_fingerprint=fingerprint,
    )


# ---------------------------------------------------------------------------
# Platform / path helpers
# ---------------------------------------------------------------------------


def _slot_dir_path(runtime_root: Path, active_id: str) -> Path:
    return runtime_root / "slots" / active_id


def _slot_python(slot_dir: Path) -> Path | None:
    """Return the slot's interpreter path when it exists (mirrors manager)."""
    if sys.platform == "win32":
        candidate = slot_dir / "Scripts" / "python.exe"
    else:
        candidate = slot_dir / "bin" / "python"
    return candidate if candidate.is_file() else None


def _unhealthy(
    active_id: str | None,
    reason: str,
) -> StartupHealthResult:
    return StartupHealthResult(
        healthy=False,
        active_slot_id=active_id,
        reasons=(reason,),
        records_fingerprint=None,
    )


# ---------------------------------------------------------------------------
# Health confirmation (pure read/probe — never writes)
# ---------------------------------------------------------------------------


def confirm_startup_health(runtime_root: Path) -> StartupHealthResult:
    """Perform every startup health check.  Never writes; never raises.

    Checks (persisted facts + minimal live probes), in order:

    1. ``active.json`` must be READY with an ``active_slot_id``;
    2. the active slot directory must exist and its Python interpreter
       must be probeable;
    3. each of the 3 observational store files that EXISTS must parse as
       valid JSON with ``schema_version == 1`` and a ``slots`` object
       (any failure → "corrupt state store");
    4. every ``primary_names`` distribution of the active slot's
       installed-lock entry must be probeable (installed + version
       equal to the recorded dependency version);
    5. every accelerated variant of the active slot's accelerated-metadata
       entry must be probeable (installed + version equal to the variant
       version) — the "accelerated ACTIVE invalid → PREVIOUS stays
       protected" case;
    6. no in-progress mutation (``RuntimeMutationLock.probe_busy``).

    Returns ``healthy=True`` only when every check passes, with the
    full state fingerprint (the 4 records + ``slots/``, each read exactly
    once for the fingerprint).  Otherwise ``healthy=False`` with a precise
    reason.
    """
    root = Path(runtime_root).resolve()
    state_dir = root / "state"

    # -- 1. persisted ACTIVE pointer must be READY --------------------------
    active_status = load_active_state(
        state_dir / _ACTIVE_FILENAME, layout_root=root
    )
    active_id = active_status.active_slot_id
    if active_status.state != RuntimeState.READY or active_id is None:
        return _unhealthy(
            active_id,
            f"active state is not READY "
            f"({active_status.state.value}: {active_status.reason})",
        )

    # -- 2. active slot directory + usable Python ---------------------------
    slot_dir = _slot_dir_path(root, active_id)
    if not slot_dir.is_dir():
        return _unhealthy(active_id, "active slot directory does not exist")
    python = _slot_python(slot_dir)
    if python is None:
        return _unhealthy(
            active_id, "active slot Python interpreter not found"
        )
    if probe_runtime_python_version(python) is None:
        return _unhealthy(active_id, "active slot Python is not usable")

    # -- 3. strict-read the 3 observational stores (capture slots + raw) ----
    store_slots: dict[str, dict | None] = {}
    record_raw: dict[str, bytes | None] = {}
    for name in _STORE_FILENAMES:
        path = state_dir / name
        if not path.is_file():
            store_slots[name] = None
            record_raw[name] = None
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            return _unhealthy(
                active_id, f"corrupt state store {name}: unreadable ({exc})"
            )
        record_raw[name] = raw
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            return _unhealthy(
                active_id,
                f"corrupt state store {name}: invalid JSON ({exc})",
            )
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != _STORE_SCHEMA_VERSION
        ):
            return _unhealthy(
                active_id,
                f"corrupt state store {name}: bad root or schema_version",
            )
        slots = payload.get("slots")
        if not isinstance(slots, dict):
            return _unhealthy(
                active_id, f"corrupt state store {name}: no 'slots' object"
            )
        store_slots[name] = slots

    # -- 4. expected managed products probeable -----------------------------
    lock_slots = store_slots[_INSTALLED_LOCK_FILENAME]
    if lock_slots is not None:
        lock_entry = lock_slots.get(active_id)
        if isinstance(lock_entry, dict):
            primary_names = lock_entry.get("primary_names", ())
            dependencies = lock_entry.get("dependencies", {})
            if not isinstance(dependencies, dict):
                dependencies = {}
            if isinstance(primary_names, (list, tuple)):
                for name in primary_names:
                    if not isinstance(name, str) or not name.strip():
                        continue
                    dep = dependencies.get(name)
                    if not isinstance(dep, dict):
                        return _unhealthy(
                            active_id,
                            f"expected managed product {name!r} has no "
                            f"recorded dependency version",
                        )
                    recorded = dep.get("version")
                    try:
                        probe = probe_runtime_distribution(str(python), name)
                    except Exception as exc:
                        return _unhealthy(
                            active_id,
                            f"expected managed product {name!r} is not "
                            f"probeable: {exc}",
                        )
                    if (
                        not probe.get("installed")
                        or probe.get("version") != recorded
                    ):
                        return _unhealthy(
                            active_id,
                            f"expected managed product {name!r} is missing "
                            f"or version-mismatched (expected {recorded!r})",
                        )

    # -- 5. acceleration metadata validates exactly as normal readiness ------
    accel_slots = store_slots[_ACCELERATED_METADATA_FILENAME]
    if accel_slots is not None:
        accel_entry = accel_slots.get(active_id)
        if isinstance(accel_entry, dict):
            variants = accel_entry.get("variants")
            if variants is not None:
                if not isinstance(variants, list):
                    return _unhealthy(
                        active_id,
                        "corrupt state store accelerated-metadata.json: "
                        "malformed variants",
                    )
                for variant in variants:
                    if not (
                        isinstance(variant, (list, tuple))
                        and len(variant) == 3
                        and all(isinstance(p, str) and p for p in variant)
                    ):
                        return _unhealthy(
                            active_id,
                            "corrupt state store accelerated-metadata.json: "
                            "malformed variant entry",
                        )
                    distribution, version, _sha = (
                        variant[0],
                        variant[1],
                        variant[2],
                    )
                    try:
                        probe = probe_runtime_distribution(
                            str(python), distribution
                        )
                    except Exception as exc:
                        return _unhealthy(
                            active_id,
                            f"accelerated variant {distribution!r} is not "
                            f"probeable: {exc}",
                        )
                    if (
                        not probe.get("installed")
                        or probe.get("version") != version
                    ):
                        return _unhealthy(
                            active_id,
                            f"accelerated variant {distribution!r} is missing "
                            f"or version-mismatched (expected {version!r})",
                        )

    # -- 6. no in-progress mutation ------------------------------------------
    busy = RuntimeMutationLock(root).probe_busy()
    if busy is not None:
        operation = busy.get("operation")
        pid = busy.get("pid")
        return _unhealthy(
            active_id,
            f"runtime mutation in progress (operation={operation}, "
            f"pid={pid}); refusing to confirm startup health",
        )

    # -- all checks passed: fingerprint the 4 records + slots/ ---------------
    record_raw[_ACTIVE_FILENAME] = (state_dir / _ACTIVE_FILENAME).read_bytes()
    fingerprint = compute_state_fingerprint(
        record_raw, slots_root=root / "slots"
    )
    return StartupHealthResult(
        healthy=True,
        active_slot_id=active_id,
        reasons=(),
        records_fingerprint=fingerprint,
    )


# ---------------------------------------------------------------------------
# Confirmation record (atomic write)
# ---------------------------------------------------------------------------


def record_startup_health(
    runtime_root: Path,
    confirmation: StartupHealthConfirmation,
) -> None:
    """Atomically write the startup-health confirmation.

    Uses the same mkstemp + fsync + ``os.replace`` pattern as
    :mod:`zealfie.runtime.state` / :mod:`zealfie.runtime.provenance`.
    Raises ``OSError`` on write failure (callers treat it best-effort).
    """
    root = Path(runtime_root).resolve()
    state_dir = root / "state"
    payload = {
        "schema_version": confirmation.schema_version,
        "active_slot_id": confirmation.active_slot_id,
        "confirmed_at": confirmation.confirmed_at,
        "records_fingerprint": confirmation.records_fingerprint,
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    state_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        suffix=".json",
        prefix=".startup-health-",
        dir=str(state_dir),
    )
    try:
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_name, str(state_dir / STARTUP_HEALTH_FILENAME))


def confirm_and_record_startup_health(runtime_root: Path) -> StartupHealthResult:
    """Run the health checks and, only when healthy, record the confirmation.

    Any pre-existing confirmation is invalidated (best-effort unlink)
    BEFORE the checks run, so an unhealthy startup can never leave a stale
    valid confirmation behind.  A crash between the clear and the write
    leaves NO confirmation, and PREVIOUS stays protected (fail-safe).

    The confirmation write is best-effort: a write failure is logged as a
    warning and never raises into the caller (startup must not break).
    """
    root = Path(runtime_root).resolve()
    state_dir = root / "state"
    # Invalidate any pre-existing confirmation BEFORE probing (MF-1): an
    # unhealthy startup must never leave a stale valid confirmation behind.
    try:
        (state_dir / STARTUP_HEALTH_FILENAME).unlink(missing_ok=True)
    except OSError as exc:
        logger.debug(
            "startup-health invalidation unlink failed (best-effort): %s", exc
        )

    result = confirm_startup_health(runtime_root)
    if (
        result.healthy
        and result.active_slot_id is not None
        and result.records_fingerprint is not None
    ):
        confirmation = StartupHealthConfirmation(
            schema_version=STARTUP_HEALTH_SCHEMA_VERSION,
            active_slot_id=result.active_slot_id,
            confirmed_at=datetime.now(timezone.utc).isoformat(),
            records_fingerprint=result.records_fingerprint,
        )
        try:
            record_startup_health(runtime_root, confirmation)
        except OSError as exc:
            logger.warning(
                "startup-health confirmation write failed (best-effort): %s",
                exc,
            )
    return result
