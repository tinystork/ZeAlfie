from __future__ import annotations

import json
from io import StringIO

import zealfie.cli as cli
from zealfie import get_version
from zealfie.components.model import ComponentStatus, ReasonCode
from zealfie.cli import main, run


class FakeRegistry:
    def __init__(self, status: ComponentStatus) -> None:
        self.status = status

    def inspect_all(self) -> tuple[ComponentStatus, ...]:
        return (self.status,)

    def inspect(self, component_id: str) -> ComponentStatus:
        if component_id != self.status.component_id:
            from zealfie.components import UnknownComponentError

            raise UnknownComponentError(component_id)
        return self.status

    def available_ids(self) -> tuple[str, ...]:
        return (self.status.component_id,)


ABSENT_ZESOLVER = ComponentStatus(
    component_id="zesolver",
    display_name="ZeSolver",
    installed=False,
    version=None,
    launch_contract_available=False,
    matched_entry_point=None,
    reason_code=ReasonCode.DISTRIBUTION_NOT_INSTALLED,
    reason='distribution "ZeSolver" is not installed',
)

PRESENT_ZESOLVER = ComponentStatus(
    component_id="zesolver",
    display_name="ZeSolver",
    installed=True,
    version="1.0.0",
    launch_contract_available=False,
    matched_entry_point=None,
    reason_code=ReasonCode.PUBLIC_ENTRY_POINT_NOT_FOUND,
    reason='expected public entry point "gui_scripts:zesolver" was not found',
)

AVAILABLE_WITNESS = ComponentStatus(
    component_id="witness",
    display_name="ZeWitness",
    installed=True,
    version="0.0.1",
    launch_contract_available=True,
    matched_entry_point=None,
    reason_code=None,
    reason=None,
)


def test_main_returns_success() -> None:
    assert main([]) == 0


def test_version_option_outputs_package_version() -> None:
    stdout = StringIO()

    code = run(["--version"], stdout=stdout)

    assert code == 0
    assert stdout.getvalue().strip() == f"ZeAlfie {get_version()}"


def test_version_json_outputs_json() -> None:
    stdout = StringIO()

    code = run(["--version-json"], stdout=stdout)

    output = stdout.getvalue()
    assert code == 0
    data = json.loads(output)
    assert data == {"product": "ZeAlfie", "version": get_version()}


def test_status_command_outputs_absent_component(monkeypatch) -> None:
    monkeypatch.setattr(cli, "default_registry", lambda: FakeRegistry(ABSENT_ZESOLVER))
    stdout = StringIO()

    code = run(["status"], stdout=stdout)
    output = stdout.getvalue()

    assert code == 0
    assert f"ZeAlfie {get_version()}" in output
    assert "Platform:" in output
    assert "Python:" in output
    assert "Components:" in output
    assert "ZeSolver" in output
    assert "Installed: no" in output
    assert "Version: unavailable" in output
    assert "Launch contract: unavailable" in output
    assert 'Reason: distribution "ZeSolver" is not installed' in output


def test_status_command_outputs_present_component(monkeypatch) -> None:
    monkeypatch.setattr(cli, "default_registry", lambda: FakeRegistry(PRESENT_ZESOLVER))
    stdout = StringIO()

    code = run(["status"], stdout=stdout)
    output = stdout.getvalue()

    assert code == 0
    assert "ZeSolver" in output
    assert "Installed: yes" in output
    assert "Version: 1.0.0" in output
    assert "Launch contract: unavailable" in output
    assert 'Reason: expected public entry point "gui_scripts:zesolver" was not found' in output


def test_status_command_outputs_available_contract(monkeypatch) -> None:
    monkeypatch.setattr(cli, "default_registry", lambda: FakeRegistry(AVAILABLE_WITNESS))
    stdout = StringIO()

    code = run(["status"], stdout=stdout)
    output = stdout.getvalue()

    assert code == 0
    assert "ZeWitness" in output
    assert "Installed: yes" in output
    assert "Version: 0.0.1" in output
    assert "Launch contract: available" in output
    assert "Reason: none" in output


def test_unknown_command_returns_error_code() -> None:
    assert main(["unknown-command"]) == 2


def test_cli_does_not_import_or_require_zesolver(monkeypatch) -> None:
    monkeypatch.setattr(cli, "default_registry", lambda: FakeRegistry(ABSENT_ZESOLVER))
    stdout = StringIO()

    code = run(["status"], stdout=stdout)

    assert code == 0
    assert "ZeSolver" in stdout.getvalue()


def test_status_specific_unknown_component_returns_error_without_traceback(monkeypatch) -> None:
    monkeypatch.setattr(cli, "default_registry", lambda: FakeRegistry(ABSENT_ZESOLVER))
    stdout = StringIO()

    code = run(["status", "missing"], stdout=stdout)
    output = stdout.getvalue()

    assert code == 2
    assert "Unknown component: missing" in output
    assert "zesolver" in output
    assert "Traceback" not in output


def test_status_specific_zesolver_displays_reason(monkeypatch) -> None:
    monkeypatch.setattr(cli, "default_registry", lambda: FakeRegistry(PRESENT_ZESOLVER))
    stdout = StringIO()

    code = run(["status", "zesolver"], stdout=stdout)
    output = stdout.getvalue()

    assert code == 0
    assert "ZeSolver" in output
    assert "Installed: yes" in output
    assert "Version: 1.0.0" in output
    assert "Launch contract: unavailable" in output
    assert 'Reason: expected public entry point "gui_scripts:zesolver" was not found' in output
    assert ReasonCode.PUBLIC_ENTRY_POINT_NOT_FOUND.value not in output
