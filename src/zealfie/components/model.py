"""Immutable component models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ReasonCode(StrEnum):
    DISTRIBUTION_NOT_INSTALLED = "DISTRIBUTION_NOT_INSTALLED"
    DISTRIBUTION_METADATA_ERROR = "DISTRIBUTION_METADATA_ERROR"
    VERSION_UNAVAILABLE = "VERSION_UNAVAILABLE"
    PUBLIC_ENTRY_POINT_NOT_FOUND = "PUBLIC_ENTRY_POINT_NOT_FOUND"


@dataclass(frozen=True, slots=True)
class EntryPointContract:
    group: str
    name: str

    def __post_init__(self) -> None:
        for field_name in ("group", "name"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"{field_name} is required")
            object.__setattr__(self, field_name, value)


@dataclass(frozen=True, slots=True)
class EntryPointInfo:
    group: str
    name: str
    value: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("group", "name"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"{field_name} is required")
            object.__setattr__(self, field_name, value)
        if self.value is not None:
            object.__setattr__(self, "value", str(self.value).strip() or None)


@dataclass(frozen=True, slots=True)
class ComponentDefinition:
    component_id: str
    display_name: str
    distribution_name: str
    launch_entry_points: tuple[EntryPointContract, ...]

    def __post_init__(self) -> None:
        for field_name in ("component_id", "display_name", "distribution_name"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"{field_name} is required")
            object.__setattr__(self, field_name, value)
        entry_points = tuple(self.launch_entry_points)
        object.__setattr__(self, "launch_entry_points", entry_points)


@dataclass(frozen=True, slots=True)
class ComponentStatus:
    component_id: str
    display_name: str
    installed: bool
    version: str | None
    launch_contract_available: bool
    matched_entry_point: EntryPointInfo | None
    reason_code: ReasonCode | None
    reason: str | None

    def __post_init__(self) -> None:
        for field_name in ("component_id", "display_name"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"{field_name} is required")
            object.__setattr__(self, field_name, value)
