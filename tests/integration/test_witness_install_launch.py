"""Integration test: build, install, detect, and launch the witness component.

This test validates the complete offline cycle described in M0-4:

1. Build the ZeAlfie wheel.
2. Build the witness wheel.
3. Create a temporary isolated venv.
4. Install both wheels without network access.
5. Verify versions through real ``importlib.metadata``.
6. Detect and resolve the witness entry point.
7. Build a LaunchPlan.
8. Execute the witness and capture its output.
9. Clean up.

The test does **not** contact PyPI, GitHub, or any remote resource.
No real ZeSoftware component is installed or launched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zealfie.building import build_wheel
from zealfie.components.manifest import load_component_definitions_from_text
from zealfie.components.model import ComponentDefinition
from zealfie.environment import TemporaryVenv
from zealfie.launching import LaunchPlan, execute_launch_plan, resolve_script


_WITNESS_MANIFEST_TOML = """\
schema_version = 1

[[components]]
id = "zewitness"
display_name = "ZeWitness"
distribution_name = "zealfie-witness"

[[components.launch.entry_points]]
group = "console_scripts"
name = "zewitness"
"""


# ---------------------------------------------------------------------------
# Fixtures (session-scoped to avoid rebuilding wheels for every test)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def zealfie_wheel(tmp_path_factory) -> Path:
    project_root = Path(__file__).resolve().parents[2]  # tests/integration -> tests -> project
    tmp = tmp_path_factory.mktemp("int-zealfie-wheel")
    return build_wheel(project_root, output_dir=tmp)


@pytest.fixture(scope="session")
def witness_wheel(tmp_path_factory) -> Path:
    witness_dir = Path(__file__).resolve().parents[1] / "fixtures" / "witness_component"
    tmp = tmp_path_factory.mktemp("int-witness-wheel")
    return build_wheel(witness_dir, output_dir=tmp)


@pytest.fixture(scope="session")
def witness_definition() -> ComponentDefinition:
    definitions = load_component_definitions_from_text(_WITNESS_MANIFEST_TOML)
    assert len(definitions) == 1
    return definitions[0]


# ---------------------------------------------------------------------------
# The main integration test
# ---------------------------------------------------------------------------


def test_full_witness_cycle(
    zealfie_wheel: Path,
    witness_wheel: Path,
    witness_definition: ComponentDefinition,
) -> None:
    """End-to-end offline validation of the witness lifecycle."""

    zealfie_wheel_path = zealfie_wheel  # local alias for clarity
    witness_wheel_path = witness_wheel

    # 1. Verify the wheels exist
    assert zealfie_wheel_path.is_file(), f"ZeAlfie wheel missing: {zealfie_wheel_path}"
    assert witness_wheel_path.is_file(), f"Witness wheel missing: {witness_wheel_path}"

    # 2. Create temporary venv and install both wheels offline
    with TemporaryVenv() as venv:
        # -- Install ZeAlfie wheel ------------------------------------------
        r1 = venv.install_wheel(zealfie_wheel_path)
        assert r1.returncode == 0, f"ZeAlfie wheel install failed:\n{r1.stderr}"

        # -- Install witness wheel ------------------------------------------
        r2 = venv.install_wheel(witness_wheel_path)
        assert r2.returncode == 0, f"Witness wheel install failed:\n{r2.stderr}"

        # 3. Verify versions via real importlib.metadata
        r3 = venv.run_python([
            "-c",
            "import importlib.metadata\n"
            "print(importlib.metadata.version('zealfie-witness'))\n"
            "print(importlib.metadata.version('zealfie'))\n",
        ])
        assert r3.returncode == 0, f"importlib.metadata check failed:\n{r3.stderr}"
        lines = r3.stdout.strip().splitlines()
        assert lines == ["0.0.1", "0.0.5"], f"Unexpected versions: {lines}"

        # 4. Verify entry points via real importlib.metadata
        r4 = venv.run_python([
            "-c",
            "import importlib.metadata\n"
            "eps = importlib.metadata.entry_points(group='console_scripts', name='zewitness')\n"
            "for ep in eps:\n"
            "    print(f'{ep.group}:{ep.name}={ep.value}')\n",
        ])
        assert r4.returncode == 0, f"Entry point check failed:\n{r4.stderr}"
        ep_output = r4.stdout.strip()
        assert "console_scripts:zewitness=zewitness.__main__:main" in ep_output

        # 5. Inspect the witness component using ZeAlfie's metadata layer
        #    against the real installed distribution in the temp venv.
        r5 = venv.run_python([
            "-c",
            "from zealfie.components.metadata import inspect_component\n"
            "from zealfie.components.model import ComponentDefinition, EntryPointContract\n"
            "\n"
            "definition = ComponentDefinition(\n"
            "    component_id='zewitness',\n"
            "    display_name='ZeWitness',\n"
            "    distribution_name='zealfie-witness',\n"
            "    launch_entry_points=(EntryPointContract('console_scripts', 'zewitness'),),\n"
            ")\n"
            "status = inspect_component(definition)\n"
            "print(f'installed={status.installed}')\n"
            "print(f'version={status.version}')\n"
            "print(f'launch_contract_available={status.launch_contract_available}')\n"
            "if status.matched_entry_point:\n"
            "    print(f'ep_group={status.matched_entry_point.group}')\n"
            "    print(f'ep_name={status.matched_entry_point.name}')\n"
            "    print(f'ep_value={status.matched_entry_point.value}')\n"
            "print(f'reason_code={status.reason_code}')\n"
            "print(f'reason={status.reason}')\n",
        ])
        assert r5.returncode == 0, f"Component inspection failed:\n{r5.stderr}"
        out = r5.stdout

        assert "installed=True" in out
        assert "version=0.0.1" in out
        assert "launch_contract_available=True" in out
        assert "ep_group=console_scripts" in out
        assert "ep_name=zewitness" in out
        assert "zewitness.__main__:main" in out
        assert "reason_code=None" in out
        assert "reason=None" in out

        # 6. Resolve the installed script
        script = resolve_script(venv.scripts_dir, "zewitness")
        assert script.is_file()
        assert str(venv.scripts_dir) in str(script), (
            f"Resolved script {script} must be inside the venv scripts dir"
        )

        # 7. Prepare a LaunchPlan
        plan = LaunchPlan(
            component_id="zewitness",
            executable=script,
        )
        assert plan.component_id == "zewitness"
        assert plan.arguments == ()

        # 8. Execute the witness
        result = execute_launch_plan(plan, timeout_seconds=10)
        assert result.return_code == 0, f"Witness failed: code={result.return_code} stderr={result.stderr}"
        assert result.stdout.strip() == "ZeWitness is present."
        assert result.stderr == ""
        assert result.timed_out is False

    # 9. After the context manager exits, the temp directory is gone.
    assert not venv._tmp_dir.is_dir()
