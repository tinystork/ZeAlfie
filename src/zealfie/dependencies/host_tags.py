"""Host-compatibility tag injection for the dependency resolver (M1-1A).

The resolver requires a set of compatible wheel tags to filter
dependency wheels.  This module provides a testable, injectable
interface plus a real-host implementation via ``packaging.tags``.
"""

from __future__ import annotations

from typing import Protocol

from packaging.tags import Tag, sys_tags


class HostTagProvider(Protocol):
    """Protocol for providing host-compatible wheel tags.

    Test implementations can inject synthetic tag sets for
    cross-platform resolution validation.
    """

    def get_compatible_tags(self) -> frozenset[Tag]:
        """Return the set of compatible tags for the target host."""
        ...


class SysTagProvider:
    """Real-host tag provider using ``packaging.tags.sys_tags()``."""

    def get_compatible_tags(self) -> frozenset[Tag]:
        return frozenset(sys_tags())


def default_compatible_tags() -> frozenset[Tag]:
    """Return the compatible tag set for the current host."""
    return frozenset(sys_tags())


def default_marker_env() -> dict[str, str]:
    """Build a marker evaluation environment for the current host.

    Includes the ``extra`` key with an empty string default; callers
    should override ``extra`` for extra-specific evaluation.
    """
    import os
    import platform
    import sys

    major, minor = sys.version_info[:2]
    return {
        "python_version": f"{major}.{minor}",
        "python_full_version": sys.version.split()[0],
        # PEP 508: the os_name marker is os.name ("posix" / "nt" / "java"),
        # never platform.system() ("Linux" / "Windows" / "Darwin").
        "os_name": os.name,
        "sys_platform": sys.platform,
        "platform_system": platform.system(),
        "platform_machine": platform.machine(),
        "platform_release": platform.release(),
        "platform_version": platform.version(),
        "implementation_name": sys.implementation.name,
        "implementation_version": (
            f"{sys.implementation.version.major}."
            f"{sys.implementation.version.minor}."
            f"{sys.implementation.version.micro}"
        ),
        "extra": "",
    }
