"""Inspect installed Python distributions for managed components."""

from __future__ import annotations

import importlib.metadata
from typing import Any, Protocol

from .model import ComponentDefinition, ComponentStatus, EntryPointContract, EntryPointInfo, ReasonCode


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
            launch_contract_available=False,
            matched_entry_point=None,
            reason_code=ReasonCode.DISTRIBUTION_NOT_INSTALLED,
            reason=f'distribution "{definition.distribution_name}" is not installed',
        )
    except Exception as exc:
        return ComponentStatus(
            component_id=definition.component_id,
            display_name=definition.display_name,
            installed=False,
            version=None,
            launch_contract_available=False,
            matched_entry_point=None,
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
            launch_contract_available=False,
            matched_entry_point=None,
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
            launch_contract_available=False,
            matched_entry_point=None,
            reason_code=ReasonCode.DISTRIBUTION_METADATA_ERROR,
            reason=f"distribution entry points could not be read: {exc}",
        )

    matched_entry_point = _find_matching_entry_point(definition, entry_points)
    if matched_entry_point is not None:
        return ComponentStatus(
            component_id=definition.component_id,
            display_name=definition.display_name,
            installed=True,
            version=installed_version,
            launch_contract_available=True,
            matched_entry_point=matched_entry_point,
            reason_code=None,
            reason=None,
        )

    return ComponentStatus(
        component_id=definition.component_id,
        display_name=definition.display_name,
        installed=True,
        version=installed_version,
        launch_contract_available=False,
        matched_entry_point=None,
        reason_code=ReasonCode.PUBLIC_ENTRY_POINT_NOT_FOUND,
        reason=f'expected public entry point "{_format_expected_contracts(definition)}" was not found',
    )


def _find_matching_entry_point(
    definition: ComponentDefinition,
    entry_points: tuple[Any, ...],
) -> EntryPointInfo | None:
    expected = set(definition.launch_entry_points)
    if not expected:
        return None
    for entry_point in entry_points:
        group = str(getattr(entry_point, "group", "") or "")
        name = str(getattr(entry_point, "name", "") or "")
        if not group or not name:
            continue
        if EntryPointContract(group=group, name=name) in expected:
            value = getattr(entry_point, "value", None)
            return EntryPointInfo(group=group, name=name, value=value)
    return None


def _format_expected_contracts(definition: ComponentDefinition) -> str:
    return ", ".join(
        f"{entry_point.group}:{entry_point.name}" for entry_point in definition.launch_entry_points
    )
