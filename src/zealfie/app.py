"""Small application services for the first ZeAlfie milestone."""

from __future__ import annotations

import platform
from dataclasses import dataclass

from . import get_version


FULL_NAME = "Astronomy Launcher For Imaging Engines"
COMPONENT_STATUS = "not yet configured"


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    """Runtime facts that can be reported without probing external components."""

    version: str
    platform_name: str
    python_version: str
    components: str


def startup_message() -> str:
    """Return the default startup text."""
    return "\n".join(
        (
            "Hello, I'm ZeAlfie.",
            FULL_NAME,
            f"Version {get_version()}",
        )
    )


def collect_status() -> RuntimeStatus:
    """Collect real local status without requiring any ZeSoftware component."""
    return RuntimeStatus(
        version=get_version(),
        platform_name=platform.system() or platform.platform(),
        python_version=platform.python_version(),
        components=COMPONENT_STATUS,
    )


def format_status(status: RuntimeStatus) -> str:
    """Format status for CLI output."""
    return "\n".join(
        (
            f"ZeAlfie {status.version}",
            f"Platform: {status.platform_name}",
            f"Python: {status.python_version}",
            f"Components: {status.components}",
        )
    )
