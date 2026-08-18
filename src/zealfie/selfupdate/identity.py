"""ZeAlfie self-update identity detection (ZA-M1-4 LOT D §A).

Determines *how* ZeAlfie is currently installed so the self-update
mechanism can decide whether it is safe to replace the running package.

The running GUI/CLI process must NEVER install into its own environment.
Self-update is therefore only meaningful for a normal ``pip install``
(:attr:`InstallMode.INSTALLED`).  Source checkouts and editable installs
are refused honestly — the user must ``git pull`` or reinstall instead.

Detection is deliberately conservative: when a mode cannot be *proven*,
the result is :attr:`InstallMode.UNKNOWN` (never a guess of ``INSTALLED``).
"""

from __future__ import annotations

import importlib.metadata as _metadata
import json
import sysconfig
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import zealfie

__all__ = [
    "InstallMode",
    "ZeAlfieIdentity",
    "detect_identity",
    "self_update_supported",
]

_DISTRIBUTION_NAME = "zealfie"


class InstallMode(StrEnum):
    """How the running ZeAlfie package is installed."""

    INSTALLED = "INSTALLED"
    EDITABLE = "EDITABLE"
    SOURCE = "SOURCE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ZeAlfieIdentity:
    """Resolved identity of the running ZeAlfie package.

    ``version`` is the installed distribution version (``"0.0.0"`` when
    metadata is absent).  ``location`` is the resolved package root path
    (the directory containing ``zealfie/__init__.py``).
    """

    version: str
    install_mode: InstallMode
    location: str


def detect_identity() -> ZeAlfieIdentity:
    """Detect the running ZeAlfie package's version, mode, and location."""
    version = _installed_version()
    location = _package_root()
    mode = _detect_install_mode(location)
    return ZeAlfieIdentity(
        version=version,
        install_mode=mode,
        location=str(location),
    )


def self_update_supported(identity: ZeAlfieIdentity) -> tuple[bool, str | None]:
    """Return ``(supported, reason)`` for self-updating *identity*.

    Only a normal site-packages install (``INSTALLED``) is supported.
    Source/editable checkouts and unknown modes return ``False`` with an
    honest, actionable reason.
    """
    if identity.install_mode is InstallMode.INSTALLED:
        return True, None
    if identity.install_mode is InstallMode.SOURCE:
        return (
            False,
            "ZeAlfie is running from a source checkout "
            f"({identity.location}); self-update is disabled — "
            "pull the repository and reinstall instead.",
        )
    if identity.install_mode is InstallMode.EDITABLE:
        return (
            False,
            "ZeAlfie is installed in editable mode "
            f"({identity.location}); self-update is disabled — "
            "reinstall with `pip install zealfie` instead.",
        )
    return (
        False,
        "ZeAlfie's install mode could not be determined; refusing "
        "self-update rather than risk replacing a live checkout.",
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _installed_version() -> str:
    try:
        return _metadata.version(_DISTRIBUTION_NAME)
    except _metadata.PackageNotFoundError:
        return "0.0.0"


def _package_root() -> Path:
    """Resolved directory containing ``zealfie/__init__.py``."""
    file = getattr(zealfie, "__file__", None)
    if not file:
        # Fall back to the parent of the import machinery's record.
        import zealfie as _zealfie

        file = getattr(_zealfie, "__file__", None) or ""
    return Path(str(file)).resolve().parent


def _detect_install_mode(location: Path) -> InstallMode:
    # 1. SOURCE — the package lives inside a git checkout.
    if _has_git_ancestor(location):
        return InstallMode.SOURCE

    # 2. EDITABLE — pip editable-install markers on the distribution.
    if _has_editable_marker():
        return InstallMode.EDITABLE

    # 3. INSTALLED — a normal site-packages install.
    if _under_site_packages(location):
        return InstallMode.INSTALLED

    # 4. Cannot prove any mode.
    return InstallMode.UNKNOWN


def _has_git_ancestor(location: Path) -> bool:
    """True if *location* or any ancestor contains a ``.git`` directory."""
    current = location
    while True:
        if (current / ".git").is_dir():
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _has_editable_marker() -> bool:
    """True if the ``zealfie`` distribution carries an editable marker.

    Checks, in order:
    * ``direct_url.json`` with ``dir_info.editable == true``;
    * any ``.pth`` file whose content references an ``__editable__``
      finder (the setuptools editable-install mechanism).
    """
    try:
        dist = _metadata.distribution(_DISTRIBUTION_NAME)
    except _metadata.PackageNotFoundError:
        return False

    # direct_url.json editable marker.
    try:
        raw = dist.read_text("direct_url.json")
    except Exception:
        raw = None
    if raw:
        try:
            payload = json.loads(raw)
            dir_info = payload.get("dir_info") if isinstance(payload, dict) else None
            if isinstance(dir_info, dict) and dir_info.get("editable") is True:
                return True
        except (ValueError, TypeError):
            pass

    # .pth with an __editable__ finder.
    files = dist.files
    if files:
        for file_entry in files:
            name = str(file_entry)
            if name.endswith(".pth"):
                try:
                    content = file_entry.read_text(encoding="utf-8")
                except Exception:
                    continue
                if content and "__editable__" in content:
                    return True
    return False


def _under_site_packages(location: Path) -> bool:
    """True if *location* resolves under a known site-packages directory."""
    resolved = location.resolve(strict=False)
    for site_path in _site_package_paths():
        try:
            resolved.relative_to(Path(site_path).resolve(strict=False))
            return True
        except ValueError:
            continue
    return False


def _site_package_paths() -> set[str]:
    """Best-effort set of site-packages locations (purelib + platlib + user)."""
    paths: set[str] = set()
    for key in ("purelib", "platlib"):
        try:
            value = sysconfig.get_path(key)
        except Exception:
            value = None
        if value:
            paths.add(value)
    try:
        import site

        paths.update(site.getsitepackages())
    except Exception:
        pass
    try:
        import site

        user = site.getusersitepackages()
        if user:
            paths.add(user)
    except Exception:
        pass
    return paths
