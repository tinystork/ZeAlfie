"""Runtime layout paths for the ZeAlfie shared environment.

All paths are normalised and cross-platform.  The production location is
derived from well-known user-data directories; tests can override it
explicitly via *root*.
"""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimeLayout:
    """Centralised paths for the ZeAlfie shared runtime.

    ``root`` is the top-level runtime directory (e.g.
    ``~/.local/share/zealfie/runtime``).

    ``active`` always points to ``root/current``.  A future ``staging``
    directory is reserved at ``root/staging`` but is **not** created or
    used by M0-5.
    """

    root: Path

    @property
    def current(self) -> Path:
        return self.root / "current"

    @property
    def staging(self) -> Path:
        """Reserved for future staging-based update logic.  Not created by M0-5."""
        return self.root / "staging"

    @property
    def active(self) -> Path:
        """Alias for ``current`` — the directory the active runtime lives in."""
        return self.current

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())


# ---------------------------------------------------------------------------
# Platform-specific defaults
# ---------------------------------------------------------------------------


def default_runtime_root() -> Path:
    """Return the platform-appropriate default runtime root.

    ========  =============================================
    Linux     ``$XDG_DATA_HOME/zealfie/runtime``
              (falls back to ``~/.local/share/zealfie/runtime``)
    macOS     ``~/Library/Application Support/zealfie/runtime``
    Windows   ``%LOCALAPPDATA%/zealfie/runtime``
    ========  =============================================

    The function never raises; if no well-known directory can be
    determined it returns a sensible fallback under ``~/.zealfie``.
    """
    system = platform.system()

    if system == "Linux":
        base = os.environ.get(
            "XDG_DATA_HOME",
            Path.home() / ".local" / "share",
        )
        return Path(base) / "zealfie" / "runtime"

    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "zealfie" / "runtime"

    if system == "Windows":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            return Path(local_appdata) / "zealfie" / "runtime"
        return Path.home() / "AppData" / "Local" / "zealfie" / "runtime"

    # Unknown platform fallback
    return Path.home() / ".zealfie" / "runtime"


def default_runtime_layout(*, root: str | Path | None = None) -> RuntimeLayout:
    """Return a :class:`RuntimeLayout` using the platform default root.

    *root* can be used by tests or by the ``ZEALFIE_RUNTIME_ROOT``
    override to pin a specific location.
    """
    if root is not None:
        return RuntimeLayout(root=Path(root))
    env_override = os.environ.get("ZEALFIE_RUNTIME_ROOT")
    if env_override:
        return RuntimeLayout(root=Path(env_override))
    return RuntimeLayout(root=default_runtime_root())
