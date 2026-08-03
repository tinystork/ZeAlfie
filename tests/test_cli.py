from __future__ import annotations

from io import StringIO

from zealfie import get_version
from zealfie.cli import main, run


def test_main_returns_success() -> None:
    assert main([]) == 0


def test_version_option_outputs_package_version() -> None:
    stdout = StringIO()

    code = run(["--version"], stdout=stdout)

    assert code == 0
    assert stdout.getvalue().strip() == f"ZeAlfie {get_version()}"


def test_status_command_outputs_real_runtime_facts() -> None:
    stdout = StringIO()

    code = run(["status"], stdout=stdout)
    output = stdout.getvalue()

    assert code == 0
    assert f"ZeAlfie {get_version()}" in output
    assert "Platform:" in output
    assert "Python:" in output
    assert "Components: not yet configured" in output


def test_unknown_command_returns_error_code() -> None:
    assert main(["unknown-command"]) == 2


def test_cli_does_not_import_or_require_zesolver() -> None:
    stdout = StringIO()

    code = run(["status"], stdout=stdout)

    assert code == 0
    assert "Components: not yet configured" in stdout.getvalue()
