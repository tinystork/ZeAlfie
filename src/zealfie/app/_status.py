"""Small application services for ZeAlfie."""

from __future__ import annotations

import platform
from dataclasses import dataclass

from zealfie import get_version
from zealfie.components import ComponentRegistry, ComponentStatus, default_registry


FULL_NAME = "Astronomy Launcher For Imaging Engines"


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    """Runtime facts and known component statuses."""

    version: str
    platform_name: str
    python_version: str
    components: tuple[ComponentStatus, ...]


def startup_message() -> str:
    """Return the default startup text."""
    return "\n".join(
        (
            "Hello, I'm ZeAlfie.",
            FULL_NAME,
            f"Version {get_version()}",
        )
    )


def collect_status(registry: ComponentRegistry | None = None) -> RuntimeStatus:
    """Collect real local status without requiring any ZeSoftware component."""
    active_registry = registry or default_registry()
    return RuntimeStatus(
        version=get_version(),
        platform_name=platform.system() or platform.platform(),
        python_version=platform.python_version(),
        components=active_registry.inspect_all(),
    )


def format_status(status: RuntimeStatus) -> str:
    """Format status for CLI output."""
    lines = [
        f"ZeAlfie {status.version}",
        f"Platform: {status.platform_name}",
        f"Python: {status.python_version}",
        "",
        "Components:",
    ]
    for component in status.components:
        lines.extend(_format_component_status(component))
    return "\n".join(lines)


def format_component_status(component: ComponentStatus) -> str:
    """Format a single component status for CLI output."""
    return "\n".join(_format_component_status(component))


def _format_component_status(component: ComponentStatus) -> list[str]:
    return [
        f" {component.display_name}",
        f" Installed: {_yes_no(component.installed)}",
        f" Version: {component.version or 'unavailable'}",
        f" Launch contract: {_available_unavailable(component.launch_contract_available)}",
        f" Reason: {component.reason or 'none'}",
    ]


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _available_unavailable(value: bool) -> str:
    return "available" if value else "unavailable"
