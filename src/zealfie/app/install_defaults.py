"""Default install helpers shared by CLI and GUI."""

from __future__ import annotations

import os
from pathlib import Path


def default_install_work_root() -> Path:
    """Return the default platform-appropriate install work root.

    Linux:   ``$XDG_CACHE_HOME/zealfie/work`` (falls back to
             ``~/.cache/zealfie/work``).
    macOS:   ``~/Library/Caches/zealfie/work``.
    Windows: ``%LOCALAPPDATA%/zealfie/work``.
    """
    import platform

    system = platform.system()
    if system == "Linux":
        base = os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
        return Path(base) / "zealfie" / "work"
    if system == "Darwin":
        return Path.home() / "Library" / "Caches" / "zealfie" / "work"
    if system == "Windows":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "zealfie" / "work"
        return Path.home() / "AppData" / "Local" / "zealfie" / "work"
    return Path.home() / ".cache" / "zealfie" / "work"
