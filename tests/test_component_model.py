from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from zealfie.components.model import ComponentDefinition, ComponentStatus, ReasonCode


def test_component_definition_is_valid_and_normalized() -> None:
    definition = ComponentDefinition(
        component_id=" zesolver ",
        display_name=" ZeSolver ",
        distribution_name=" ZeSolver ",
        supported_entry_points=("zesolver", "", "  "),
    )

    assert definition.component_id == "zesolver"
    assert definition.display_name == "ZeSolver"
    assert definition.distribution_name == "ZeSolver"
    assert definition.supported_entry_points == ("zesolver",)


def test_component_status_is_valid() -> None:
    status = ComponentStatus(
        component_id="zesolver",
        display_name="ZeSolver",
        installed=False,
        version=None,
        launchable=False,
        reason_code=ReasonCode.DISTRIBUTION_NOT_INSTALLED,
        reason='distribution "ZeSolver" is not installed',
    )

    assert status.reason_code is ReasonCode.DISTRIBUTION_NOT_INSTALLED


def test_component_models_are_immutable() -> None:
    definition = ComponentDefinition("zesolver", "ZeSolver", "ZeSolver", ("zesolver",))

    with pytest.raises(FrozenInstanceError):
        definition.display_name = "Other"  # type: ignore[misc]


def test_component_definition_requires_identity_fields() -> None:
    with pytest.raises(ValueError, match="component_id is required"):
        ComponentDefinition("", "ZeSolver", "ZeSolver", ())


def test_component_status_requires_identity_fields() -> None:
    with pytest.raises(ValueError, match="display_name is required"):
        ComponentStatus("zesolver", "", False, None, False, None, None)
