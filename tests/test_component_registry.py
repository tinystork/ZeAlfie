from __future__ import annotations

import pytest

from zealfie.components import ComponentDefinition, ComponentStatus, ReasonCode
from zealfie.components.registry import ComponentRegistry, UnknownComponentError, default_registry


def test_default_registry_lists_zesolver() -> None:
    registry = default_registry()

    assert registry.available_ids() == ("zesolver",)
    assert registry.get("zesolver").display_name == "ZeSolver"


def test_registry_unknown_component_raises_clear_error() -> None:
    registry = default_registry()

    with pytest.raises(UnknownComponentError):
        registry.get("missing")


def test_registry_rejects_duplicate_ids() -> None:
    definition = ComponentDefinition("one", "One", "One", ("one",))

    with pytest.raises(ValueError, match="duplicate component id: one"):
        ComponentRegistry((definition, definition))


def test_registry_inspects_all_components_with_injected_inspector() -> None:
    registry = default_registry()

    def inspector(definition: ComponentDefinition) -> ComponentStatus:
        return ComponentStatus(
            component_id=definition.component_id,
            display_name=definition.display_name,
            installed=False,
            version=None,
            launchable=False,
            reason_code=ReasonCode.DISTRIBUTION_NOT_INSTALLED,
            reason="not installed",
        )

    statuses = registry.inspect_all(inspector)

    assert len(statuses) == 1
    assert statuses[0].component_id == "zesolver"
    assert statuses[0].reason_code is ReasonCode.DISTRIBUTION_NOT_INSTALLED
