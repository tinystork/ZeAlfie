"""ZeAlfie package."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


def get_version() -> str:
    """Return the installed package version."""
    try:
        return version("zealfie")
    except PackageNotFoundError:
        return "0.0.0"


__version__ = get_version()

__all__ = ["__version__", "get_version"]
