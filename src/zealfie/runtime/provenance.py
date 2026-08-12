"""Installed-product source provenance (M1-2E LOT E.1).

Persists, per effectively installed/managed product, the exact source
provenance of the **currently active runtime**:

* ``product_id``
* ``version``
* ``source_owner`` / ``source_repo`` (where the source lives)
* ``requested_ref`` (the mutable branch/tag requested at install time)
* ``commit_sha`` (the resolved, immutable commit that was built)
* ``wheel_sha256`` (SHA-256 of the verified product wheel)

Provenance describes *runtime state*, not *user desire*.  It is therefore
kept strictly separate from the ``SelectionStore`` (``desired-products.toml``)
and keyed by active slot id.  Readback always describes the slot the active
pointer currently references — never a failed candidate — and never fabricates
a commit SHA: a runtime with no provenance (or a corrupt/unknown entry)
returns ``None``.

Storage layout (``RuntimeLayout.state_dir / product-provenance.json``)::

    {
      "schema_version": 1,
      "slots": {
        "<slot_id>": {
          "<product_id>": {
            "version": "...",
            "source_owner": "...",
            "source_repo": "...",
            "requested_ref": "...",
            "commit_sha": "...",
            "wheel_sha256": "..."
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
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .layout import RuntimeLayout, validate_slot_id
from .model import RuntimeState
from .state import load_active_state

PROVENANCE_SCHEMA_VERSION = 1
PROVENANCE_FILENAME = "product-provenance.json"

_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ProductProvenance:
    """Source provenance for one installed/managed product.

    ``commit_sha`` is always a 40-character lowercase hex SHA-1;
    ``wheel_sha256`` is always a 64-character lowercase hex SHA-256.
    """

    product_id: str
    version: str
    source_owner: str
    source_repo: str
    requested_ref: str
    commit_sha: str
    wheel_sha256: str

    def __post_init__(self) -> None:
        for field_name in (
            "product_id",
            "version",
            "source_owner",
            "source_repo",
            "requested_ref",
        ):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(
                    f"product provenance {field_name} must not be empty"
                )
            object.__setattr__(self, field_name, value)

        sha = str(self.commit_sha or "").strip().lower()
        if not _SHA1_RE.match(sha):
            raise ValueError(
                f"product provenance commit_sha must be a 40-character hex "
                f"string, got {self.commit_sha!r}"
            )
        object.__setattr__(self, "commit_sha", sha)

        wheel_sha = str(self.wheel_sha256 or "").strip().lower()
        if not _SHA256_RE.match(wheel_sha):
            raise ValueError(
                f"product provenance wheel_sha256 must be a 64-character hex "
                f"string, got {self.wheel_sha256!r}"
            )
        object.__setattr__(self, "wheel_sha256", wheel_sha)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _entry_to_dict(entry: ProductProvenance) -> dict[str, str]:
    """Render a provenance entry as its JSON object (product_id is the key)."""
    return {
        "version": entry.version,
        "source_owner": entry.source_owner,
        "source_repo": entry.source_repo,
        "requested_ref": entry.requested_ref,
        "commit_sha": entry.commit_sha,
        "wheel_sha256": entry.wheel_sha256,
    }


def _entry_from_dict(product_id: str, payload: object) -> ProductProvenance | None:
    """Reconstruct a provenance entry from a JSON object.

    Returns ``None`` for any malformed entry — never raises.  This keeps
    readback lenient: corrupt or unknown entries become ``None``, never a
    fabricated SHA.
    """
    if not isinstance(payload, dict):
        return None
    try:
        return ProductProvenance(product_id=product_id, **payload)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class ProductProvenanceStore:
    """Persistent, slot-keyed store for installed-product provenance.

    Thread-unsafe by design — single-owner at the service layer, mirroring
    :class:`~zealfie.products.selection.SelectionStore`.

    Read paths are lenient (missing/corrupt → empty mapping, unknown entry →
    ``None``).  Write paths are atomic (temp file + fsync + ``os.replace``)
    and validate slot ids and entry uniqueness.
    """

    def __init__(self, layout: RuntimeLayout) -> None:
        self._layout = layout

    @property
    def path(self) -> Path:
        """Filesystem path of the persisted provenance file."""
        return self._layout.state_dir / PROVENANCE_FILENAME

    @property
    def layout(self) -> RuntimeLayout:
        return self._layout

    # -- read ----------------------------------------------------------------

    def load_slot(self, slot_id: str) -> dict[str, ProductProvenance]:
        """Return provenance for *slot_id* as ``{product_id: ProductProvenance}``.

        Missing slot, missing file, or corrupt file → empty dict.  Never
        raises, never invents a SHA.
        """
        return self._load_all().get(slot_id, {})

    def load_active(self) -> dict[str, ProductProvenance]:
        """Return provenance for the currently active slot.

        Reads the active pointer (``state/active.json``); returns ``{}`` for
        ABSENT/BROKEN runtimes or when the active slot has no recorded
        provenance.
        """
        status = load_active_state(
            self._layout.active_pointer, layout_root=self._layout.root
        )
        if status.state != RuntimeState.READY or status.active_slot_id is None:
            return {}
        return self.load_slot(status.active_slot_id)

    def product_provenance(self, product_id: str) -> ProductProvenance | None:
        """Return provenance for *product_id* in the active runtime, or ``None``."""
        return self.load_active().get(product_id)

    # -- write ---------------------------------------------------------------

    def record(
        self,
        slot_id: str,
        entries: Sequence[ProductProvenance],
    ) -> None:
        """Record provenance for *slot_id*.

        The slot's product map is **replaced** with *entries* (slots are
        immutable and full-state, so a slot's provenance is the exact set of
        products materialized into it).  Duplicate product ids in *entries*
        raise :class:`ValueError`; *slot_id* is validated with the canonical
        slot-id validator.
        """
        validate_slot_id(slot_id)

        slot_map: dict[str, ProductProvenance] = {}
        for entry in entries:
            if entry.product_id in slot_map:
                raise ValueError(
                    f"duplicate product_id in provenance entries: "
                    f"{entry.product_id!r}"
                )
            slot_map[entry.product_id] = entry

        all_slots = self._load_all()
        all_slots[slot_id] = slot_map
        self._write_all(all_slots)

    # -- internal I/O --------------------------------------------------------

    def _load_all(self) -> dict[str, dict[str, ProductProvenance]]:
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
        if payload.get("schema_version") != PROVENANCE_SCHEMA_VERSION:
            return {}

        slots = payload.get("slots")
        if not isinstance(slots, dict):
            return {}

        result: dict[str, dict[str, ProductProvenance]] = {}
        for slot_id, products in slots.items():
            if not isinstance(slot_id, str) or not isinstance(products, dict):
                continue
            slot_map: dict[str, ProductProvenance] = {}
            for product_id, entry in products.items():
                if not isinstance(product_id, str):
                    continue
                parsed = _entry_from_dict(product_id, entry)
                if parsed is not None:
                    slot_map[product_id] = parsed
            result[slot_id] = slot_map
        return result

    def _write_all(
        self, all_slots: dict[str, dict[str, ProductProvenance]]
    ) -> None:
        """Atomically write the whole provenance file."""
        rendered_slots: dict[str, dict[str, dict[str, str]]] = {}
        for slot_id in sorted(all_slots):
            slot_map = all_slots[slot_id]
            rendered_slots[slot_id] = {
                product_id: _entry_to_dict(slot_map[product_id])
                for product_id in sorted(slot_map)
            }

        payload = {
            "schema_version": PROVENANCE_SCHEMA_VERSION,
            "slots": rendered_slots,
        }
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"

        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)

        fd, tmp_name = tempfile.mkstemp(
            suffix=".json", prefix=".product-provenance-", dir=str(path.parent)
        )
        try:
            os.write(fd, text.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

        os.replace(tmp_name, str(path))
