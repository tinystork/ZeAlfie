"""Installed-runtime lock read model (M1-2F Phase 4 corrective).

Records, per effectively activated slot, the **installed reality** of the
shared runtime's resolved dependency closure — without any transient
install-input artifacts.

This is a *reduced* derivative of the planning-time ``RuntimeLock``.  The
planning ``RuntimeLock`` (``zealfie.dependencies.models.RuntimeLock`` /
``LockedDependency``) carries ``wheel_path`` / ``size`` / ``sha256``, which
point at the wheel files used as install inputs.  Those paths are transient:

* auto-acquired dependency wheels live under a private staging directory
  that ``install_product`` deletes in its ``finally`` block; and
* product wheels are built under the caller's ``work_root`` and are not part
  of the shared runtime's installed state.

Persisting them slot-scoped would therefore fabricate stale/false wheel
paths that do not describe what is actually installed.  This module records
only stable installed-reality data:

* ``schema_version``
* per-``slot_id`` map with:
  * ``primary_names`` — the explicit primary/root distribution names
  * ``dependencies`` — entries keyed by normalised distribution name, each
    with ``name`` / ``version`` / ``extras`` (sorted) / ``required_by``
    (sorted) / ``primary`` (bool)

``wheel_path``, ``size``, and ``sha256`` are **never** persisted.

Readback is observational and lenient: a missing file, corrupt file,
unknown schema, unknown slot, or malformed entry yields ``None`` (UNKNOWN) —
never a fabricated lock.  This store drives no install/update/rollback/KEEP
decision; it is written only after successful activation and successful
selection persistence, alongside provenance, and a write failure leaves the
runtime active (identical non-destructive semantics to provenance).

Storage layout (``RuntimeLayout.state_dir / installed-lock.json``)::

    {
      "schema_version": 1,
      "slots": {
        "<slot_id>": {
          "primary_names": ["zealfie-witness", ...],
          "dependencies": {
            "<normalised_name>": {
              "name": "...",
              "version": "...",
              "extras": ["gui", ...],
              "required_by": ["zealfie-witness", ...],
              "primary": true
            }
          }
        }
      }
    }

This module is pure Python and Qt-free.  It never downloads, builds,
installs, or mutates the runtime.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .layout import RuntimeLayout, validate_slot_id
from .model import RuntimeState
from .state import load_active_state

if TYPE_CHECKING:  # pragma: no cover - import for typing only
    from zealfie.dependencies.models import RuntimeLock

INSTALLED_LOCK_SCHEMA_VERSION = 1
INSTALLED_LOCK_FILENAME = "installed-lock.json"


# ---------------------------------------------------------------------------
# Installed-reality models
# ---------------------------------------------------------------------------


def _sorted_strs(values: object) -> tuple[str, ...]:
    """Return a deterministic sorted tuple of non-empty strings from *values*.

    Lenient: non-strings and empty strings are dropped.  Used to normalise
    ``extras`` / ``required_by`` so the persisted form is always
    deterministic (mirrors the sorted-write discipline used by provenance).
    """
    if not isinstance(values, (list, tuple, set, frozenset)):
        return ()
    out: set[str] = set()
    for value in values:
        if isinstance(value, str) and value.strip():
            out.add(value.strip())
    return tuple(sorted(out))


@dataclass(frozen=True, slots=True)
class InstalledDependency:
    """One installed distribution's stable identity and lock edges.

    Carries no wheel path, size, or digest — those are install-input
    artifacts and are not installed reality.
    """

    name: str
    version: str
    extras: tuple[str, ...] = ()
    required_by: tuple[str, ...] = ()
    primary: bool = False

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        if not name:
            raise ValueError("InstalledDependency.name must not be empty")
        object.__setattr__(self, "name", name)
        version = str(self.version or "").strip()
        if not version:
            raise ValueError("InstalledDependency.version must not be empty")
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "extras", _sorted_strs(self.extras))
        object.__setattr__(self, "required_by", _sorted_strs(self.required_by))
        object.__setattr__(self, "primary", bool(self.primary))


@dataclass(frozen=True, slots=True)
class InstalledRuntimeLock:
    """Reduced installed-reality dependency closure for one slot.

    ``primary_names`` is the explicit set of primary/root distributions;
    ``dependencies`` maps normalised distribution name →
    :class:`InstalledDependency`.  An empty lock (``primary_names == set()``
    and ``dependencies == {}``) is a *known* "no resolved closure was used"
    record — distinct from UNKNOWN (``None``, no record at all).
    """

    primary_names: frozenset[str] = field(default_factory=frozenset)
    dependencies: dict[str, InstalledDependency] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "primary_names", frozenset(sorted(self.primary_names))
        )
        object.__setattr__(self, "dependencies", dict(self.dependencies))

    def __len__(self) -> int:
        return len(self.dependencies)

    def __contains__(self, name: str) -> bool:
        return name in self.dependencies

    def __getitem__(self, name: str) -> InstalledDependency:
        return self.dependencies[name]

    def get(self, name: str) -> InstalledDependency | None:
        return self.dependencies.get(name)


def installed_lock_from_runtime_lock(
    lock: "RuntimeLock | None",
) -> InstalledRuntimeLock:
    """Reduce a planning ``RuntimeLock`` to an installed-reality lock.

    Drops every transient install-input field (``wheel_path`` / ``size`` /
    ``sha256``) and keeps only stable identity + edges.  A ``None`` lock
    (no resolved closure was used) becomes a **known-empty** lock, so a
    caller can distinguish "no closure used" from "unknown".
    """
    if lock is None:
        return InstalledRuntimeLock()

    primary = frozenset(lock.primary_names)
    dependencies: dict[str, InstalledDependency] = {}
    for name in sorted(lock.locked):
        dep = lock.locked[name]
        dependencies[name] = InstalledDependency(
            name=dep.name,
            version=dep.version,
            extras=tuple(sorted(dep.extras)),
            required_by=tuple(sorted(dep.required_by)),
            primary=(name in primary),
        )
    return InstalledRuntimeLock(
        primary_names=primary,
        dependencies=dependencies,
    )


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _dep_to_dict(dep: InstalledDependency) -> dict[str, object]:
    """Render an installed dependency as its JSON object (name is the key)."""
    return {
        "name": dep.name,
        "version": dep.version,
        "extras": list(dep.extras),
        "required_by": list(dep.required_by),
        "primary": dep.primary,
    }


def _lock_to_dict(lock: InstalledRuntimeLock) -> dict[str, object]:
    """Render an installed lock as its JSON object."""
    return {
        "primary_names": sorted(lock.primary_names),
        "dependencies": {
            name: _dep_to_dict(lock.dependencies[name])
            for name in sorted(lock.dependencies)
        },
    }


def _dep_from_dict(
    name: str,
    payload: object,
    primary_names: frozenset[str],
) -> InstalledDependency | None:
    """Reconstruct an installed dependency from a JSON object.

    The dict key *name* is canonical; the optional ``name`` field inside the
    entry is informational and not trusted for indexing.  Returns ``None``
    for any malformed entry — never raises, never fabricates.
    """
    if not isinstance(payload, dict):
        return None

    version = payload.get("version")
    if not isinstance(version, str) or not version.strip():
        return None

    extras = payload.get("extras", ())
    required_by = payload.get("required_by", ())
    if not isinstance(extras, (list, tuple)):
        extras = ()
    if not isinstance(required_by, (list, tuple)):
        required_by = ()
    if not all(isinstance(x, str) for x in extras):
        return None
    if not all(isinstance(x, str) for x in required_by):
        return None

    primary_raw = payload.get("primary", name in primary_names)
    primary = primary_raw if isinstance(primary_raw, bool) else (name in primary_names)

    try:
        return InstalledDependency(
            name=name,
            version=version,
            extras=tuple(extras),
            required_by=tuple(required_by),
            primary=primary,
        )
    except (TypeError, ValueError):
        return None


def _lock_from_dict(payload: object) -> InstalledRuntimeLock | None:
    """Reconstruct an installed lock from a JSON object (lenient)."""
    if not isinstance(payload, dict):
        return None

    primary_raw = payload.get("primary_names", ())
    if not isinstance(primary_raw, (list, tuple)):
        primary_raw = ()
    primary_names = frozenset(
        name.strip()
        for name in primary_raw
        if isinstance(name, str) and name.strip()
    )

    deps_raw = payload.get("dependencies", {})
    if not isinstance(deps_raw, dict):
        deps_raw = {}

    dependencies: dict[str, InstalledDependency] = {}
    for name, dep_payload in deps_raw.items():
        if not isinstance(name, str) or not name.strip():
            continue
        dep = _dep_from_dict(name, dep_payload, primary_names)
        if dep is not None:
            dependencies[name] = dep

    return InstalledRuntimeLock(
        primary_names=primary_names,
        dependencies=dependencies,
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class InstalledLockStore:
    """Persistent, slot-keyed store for installed-runtime locks.

    Thread-unsafe by design — single-owner at the service layer, mirroring
    :class:`~zealfie.runtime.provenance.ProductProvenanceStore`.

    Read paths are lenient (missing/corrupt → ``None`` UNKNOWN).  Write paths
    are atomic (temp file + fsync + ``os.replace``) and validate slot ids.
    This store is observational only: no install/update/rollback/KEEP
    decision may read it.
    """

    def __init__(self, layout: RuntimeLayout) -> None:
        self._layout = layout

    @property
    def path(self) -> Path:
        """Filesystem path of the persisted installed-lock file."""
        return self._layout.state_dir / INSTALLED_LOCK_FILENAME

    @property
    def layout(self) -> RuntimeLayout:
        return self._layout

    # -- read ----------------------------------------------------------------

    def load_slot(self, slot_id: str) -> InstalledRuntimeLock | None:
        """Return the installed lock for *slot_id*, or ``None`` if unknown.

        Missing slot, missing file, corrupt file, unknown schema, or
        malformed slot → ``None``.  Never raises, never fabricates.
        """
        return self._load_all().get(slot_id)

    def load_active(self) -> InstalledRuntimeLock | None:
        """Return the installed lock for the currently active slot.

        Reads the active pointer (``state/active.json``); returns ``None``
        for ABSENT/BROKEN runtimes or when the active slot has no recorded
        lock.  After rollback the pointer references the previous slot, so
        this returns the previous slot's installed lock.
        """
        status = load_active_state(
            self._layout.active_pointer, layout_root=self._layout.root
        )
        if status.state != RuntimeState.READY or status.active_slot_id is None:
            return None
        return self.load_slot(status.active_slot_id)

    # -- write ---------------------------------------------------------------

    def record(self, slot_id: str, lock: InstalledRuntimeLock) -> None:
        """Record the installed lock for *slot_id*.

        The slot's lock is **replaced** (slots are immutable and full-state).
        *slot_id* is validated with the canonical slot-id validator.  An
        empty lock records a known "no resolved closure was used" state.
        """
        validate_slot_id(slot_id)

        all_slots = self._load_all()
        all_slots[slot_id] = lock
        self._write_all(all_slots)

    # -- internal I/O --------------------------------------------------------

    def _load_all(self) -> dict[str, InstalledRuntimeLock]:
        """Load the whole file, leniently.  Never raises."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return {}

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return {}

        if not isinstance(payload, dict):
            return {}
        if payload.get("schema_version") != INSTALLED_LOCK_SCHEMA_VERSION:
            return {}

        slots = payload.get("slots")
        if not isinstance(slots, dict):
            return {}

        result: dict[str, InstalledRuntimeLock] = {}
        for slot_id, slot_payload in slots.items():
            if not isinstance(slot_id, str):
                continue
            lock = _lock_from_dict(slot_payload)
            if lock is not None:
                result[slot_id] = lock
        return result

    def _write_all(self, all_slots: dict[str, InstalledRuntimeLock]) -> None:
        """Atomically write the whole installed-lock file."""
        rendered_slots: dict[str, dict[str, object]] = {}
        for slot_id in sorted(all_slots):
            rendered_slots[slot_id] = _lock_to_dict(all_slots[slot_id])

        payload = {
            "schema_version": INSTALLED_LOCK_SCHEMA_VERSION,
            "slots": rendered_slots,
        }
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"

        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)

        fd, tmp_name = tempfile.mkstemp(
            suffix=".json", prefix=".installed-lock-", dir=str(path.parent)
        )
        try:
            os.write(fd, text.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

        os.replace(tmp_name, str(path))
