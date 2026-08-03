"""Inspect installed Python distributions for managed components."""

from __future__ import annotations

import importlib.metadata
from typing import Any, Protocol

from .model import ComponentDefinition, ComponentStatus, ReasonCode


SUPPORTED_ENTRY_POINT_GROUPS = ("console_scripts", "gui_scripts")


class MetadataProvider(Protocol):
    def distribution(self, distribution_name: str) -> Any: ...


class ImportlibMetadataProvider:
    def distribution(self, distribution_name: str) -> importlib.metadata.Distribution:
        return importlib.metadata.distribution(distribution_name)


def inspect_component(
    definition: ComponentDefinition,
    *,
    metadata_provider: MetadataProvider | None = None,
) -> ComponentStatus:
    provider = metadata_provider or ImportlibMetadataProvider()
    try:
        distribution = provider.distribution(definition.distribution_name)
    except importlib.metadata.PackageNotFoundError:
        return ComponentStatus(
            component_id=definition.component_id,
            display_name=definition.display_name,
            installed=False,
            version=None,
            launchable=False,
            reason_code=ReasonCode.DISTRIBUTION_NOT_INSTALLED,
            reason=f'distribution "{definition.distribution_name}" is not installed',
        )
    except Exception as exc:
        return ComponentStatus(
            component_id=definition.component_id,
            display_name=definition.display_name,
            installed=False,
            version=None,
            launchable=False,
            reason_code=ReasonCode.DISTRIBUTION_METADATA_ERROR,
            reason=f"distribution metadata could not be read: {exc}",
        )

    try:
        installed_version = str(distribution.version).strip() or None
    except Exception as exc:
        return ComponentStatus(
            component_id=definition.component_id,
            display_name=definition.display_name,
            installed=True,
            version=None,
            launchable=False,
            reason_code=ReasonCode.VERSION_UNAVAILABLE,
            reason=f"distribution version could not be read: {exc}",
        )

    try:
        entry_points = tuple(distribution.entry_points)
    except Exception as exc:
        return ComponentStatus(
            component_id=definition.component_id,
            display_name=definition.display_name,
            installed=True,
            version=installed_version,
            launchable=False,
            reason_code=ReasonCode.DISTRIBUTION_METADATA_ERROR,
            reason=f"distribution entry points could not be read: {exc}",
        )

    if _has_supported_entry_point(definition, entry_points):
        return ComponentStatus(
            component_id=definition.component_id,
            display_name=definition.display_name,
            installed=True,
            version=installed_version,
            launchable=True,
            reason_code=None,
            reason=None,
        )

    return ComponentStatus(
        component_id=definition.component_id,
        display_name=definition.display_name,
        installed=True,
        version=installed_version,
        launchable=False,
        reason_code=ReasonCode.PUBLIC_ENTRY_POINT_NOT_FOUND,
        reason="no supported public launch entry point",
    )


def _has_supported_entry_point(
    definition: ComponentDefinition,
    entry_points: tuple[Any, ...],
) -> bool:
    supported_names = set(definition.supported_entry_points)
    if not supported_names:
        return False
    for entry_point in entry_points:
        group = str(getattr(entry_point, "group", "") or "")
        name = str(getattr(entry_point, "name", "") or "")
        if group in SUPPORTED_ENTRY_POINT_GROUPS and name in supported_names:
            return True
    return False
