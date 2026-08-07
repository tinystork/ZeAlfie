"""Runtime layout paths for the ZeAlfie shared environment — slot-based.

M0-6 replaces the single ``current`` directory with immutable **slots**
and an atomic **active pointer**.  Slot paths are created once and never
renamed; activation swaps only the pointer file.
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimeLayout:
    """Centralised paths for the ZeAlfie shared runtime (slot architecture).

    ``root`` is the top-level runtime directory (e.g.
    ``~/.local/share/zealfie/runtime``).

    ``slots`` is ``root/slots`` — each slot is a subdirectory with a
    stable path that never changes after creation.

    ``state_dir`` is ``root/state``.  The active pointer lives at
    ``state_dir/active.json``.
    """

    root: Path

    # -- directories ----------------------------------------------------------

    @property
    def slots(self) -> Path:
        return self.root / "slots"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    # -- pointer --------------------------------------------------------------

    @property
    def active_pointer(self) -> Path:
        return self.state_dir / "active.json"

    # -- slot helpers ---------------------------------------------------------

    def slot_path(self, slot_id: str) -> Path:
        """Return the absolute path for *slot_id* under ``slots/``.

        *slot_id* is validated strictly: empty, whitespace, path
        separators, parent-directory components, and absolute paths
        are rejected.  The resolved path is verified to be inside
        the canonical ``slots`` directory.

        Raises :class:`ValueError` for invalid slot IDs.
        """
        _validate_slot_id_strict(slot_id)
        candidate = (self.slots / slot_id).resolve(strict=False)
        slots_root = self.slots.resolve(strict=False)
        try:
            candidate.relative_to(slots_root)
        except ValueError:
            raise ValueError(
                f"slot path {candidate} escapes slots root {slots_root}"
            )
        return candidate

    # ------------------------------------------------------------------------

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())


# ---------------------------------------------------------------------------
# Platform-specific defaults (unchanged from M0-5)
# ---------------------------------------------------------------------------


def default_runtime_root() -> Path:
    system = platform.system()
    if system == "Linux":
        base = os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
        return Path(base) / "zealfie" / "runtime"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "zealfie" / "runtime"
    if system == "Windows":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            return Path(local_appdata) / "zealfie" / "runtime"
        return Path.home() / "AppData" / "Local" / "zealfie" / "runtime"
    return Path.home() / ".zealfie" / "runtime"


def default_runtime_layout(*, root: str | Path | None = None) -> RuntimeLayout:
    if root is not None:
        return RuntimeLayout(root=Path(root))
    env_override = os.environ.get("ZEALFIE_RUNTIME_ROOT")
    if env_override:
        return RuntimeLayout(root=Path(env_override))
    return RuntimeLayout(root=default_runtime_root())


def _validate_slot_id_strict(slot_id: str) -> None:
    """Reject slot IDs that look like paths or escape attempts."""
    if not slot_id or slot_id.strip() != slot_id:
        raise ValueError(f"invalid slot id: {slot_id!r}")
    if "/" in slot_id or "\\" in slot_id:
        raise ValueError(f"slot id must not contain path separators: {slot_id!r}")
    if slot_id.startswith(".") or ".." in slot_id:
        raise ValueError(f"slot id must not contain ..: {slot_id!r}")
    if slot_id.startswith("/") or (len(slot_id) >= 2 and slot_id[1] == ":"):
        raise ValueError(f"slot id must not be absolute: {slot_id!r}")


# Canonical, public name for the validator.
validate_slot_id = _validate_slot_id_strict
