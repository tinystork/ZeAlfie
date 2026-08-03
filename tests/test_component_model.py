from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from zealfie.components.model import (
    ComponentDefinition,
    ComponentStatus,
    EntryPointContract,
    EntryPointInfo,
    ReasonCode,
)


def test_entry_point_contract_is_valid_and_normalized() -> None:
    contract = EntryPointContract(" gui_scripts ", " zesolver ")

    assert contract.group == "gui_scripts"
    assert contract.name == "zesolver"


def test_entry_point_contract_is_immutable() -> None:
    contract = EntryPointContract("gui_scripts", "zesolver")

    with pytest.raises(FrozenInstanceError):
        contract.name = "other"  # type: ignore[misc]


def test_entry_point_contract_rejects_empty_values() -> None:
    with pytest.raises(ValueError, match="group is required"):
        EntryPointContract("", "zesolver")

    with pytest.raises(ValueError, match="name is required"):
        EntryPointContract("gui_scripts", "")


def test_entry_point_contract_equality_includes_group() -> None:
    gui_contract = EntryPointContract("gui_scripts", "zesolver")
    console_contract = EntryPointContract("console_scripts", "zesolver")

    assert gui_contract != console_contract


def test_component_definition_is_valid_and_normalized() -> None:
    definition = ComponentDefinition(
        component_id=" zesolver ",
        display_name=" ZeSolver ",
        distribution_name=" ZeSolver ",
        launch_entry_points=(EntryPointContract("gui_scripts", "zesolver"),),
    )

    assert definition.component_id == "zesolver"
    assert definition.display_name == "ZeSolver"
    assert definition.distribution_name == "ZeSolver"
    assert definition.launch_entry_points == (EntryPointContract("gui_scripts", "zesolver"),)


def test_component_status_is_valid() -> None:
    status = ComponentStatus(
        component_id="zesolver",
        display_name="ZeSolver",
        installed=False,
        version=None,
        launch_contract_available=False,
        matched_entry_point=None,
        reason_code=ReasonCode.DISTRIBUTION_NOT_INSTALLED,
        reason='distribution "ZeSolver" is not installed',
    )

    assert status.reason_code is ReasonCode.DISTRIBUTION_NOT_INSTALLED


def test_component_models_are_immutable() -> None:
    definition = ComponentDefinition(
        "zesolver",
        "ZeSolver",
        "ZeSolver",
        (EntryPointContract("gui_scripts", "zesolver"),),
    )

    with pytest.raises(FrozenInstanceError):
        definition.display_name = "Other"  # type: ignore[misc]


def test_component_definition_requires_identity_fields() -> None:
    with pytest.raises(ValueError, match="component_id is required"):
        ComponentDefinition("", "ZeSolver", "ZeSolver", ())


def test_component_status_requires_identity_fields() -> None:
    with pytest.raises(ValueError, match="display_name is required"):
        ComponentStatus("zesolver", "", False, None, False, None, None, None)


def test_entry_point_info_is_valid() -> None:
    info = EntryPointInfo("console_scripts", "zewitness", "zewitness.__main__:main")

    assert info.group == "console_scripts"
    assert info.name == "zewitness"
    assert info.value == "zewitness.__main__:main"
