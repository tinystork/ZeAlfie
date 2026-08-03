"""Local registry for known ZeSoftware components."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from .manifest import load_default_component_definitions
from .metadata import inspect_component
from .model import ComponentDefinition, ComponentStatus


class UnknownComponentError(KeyError):
    """Raised when a requested component id is not known."""


class ComponentRegistry:
    def __init__(self, definitions: Iterable[ComponentDefinition]) -> None:
        ordered = tuple(definitions)
        by_id: dict[str, ComponentDefinition] = {}
        for definition in ordered:
            if definition.component_id in by_id:
                raise ValueError(f"duplicate component id: {definition.component_id}")
            by_id[definition.component_id] = definition
        self._definitions = ordered
        self._by_id = by_id

    def list(self) -> tuple[ComponentDefinition, ...]:
        return self._definitions

    def get(self, component_id: str) -> ComponentDefinition:
        key = str(component_id or "").strip()
        try:
            return self._by_id[key]
        except KeyError as exc:
            raise UnknownComponentError(key) from exc

    def available_ids(self) -> tuple[str, ...]:
        return tuple(definition.component_id for definition in self._definitions)

    def inspect_all(
        self,
        inspector: Callable[[ComponentDefinition], ComponentStatus] = inspect_component,
    ) -> tuple[ComponentStatus, ...]:
        return tuple(inspector(definition) for definition in self._definitions)

    def inspect(
        self,
        component_id: str,
        inspector: Callable[[ComponentDefinition], ComponentStatus] = inspect_component,
    ) -> ComponentStatus:
        return inspector(self.get(component_id))


def default_registry() -> ComponentRegistry:
    return ComponentRegistry(load_default_component_definitions())
