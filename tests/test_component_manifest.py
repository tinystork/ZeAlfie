from __future__ import annotations

import importlib.resources

import pytest

from zealfie.components.manifest import (
    InvalidComponentManifestError,
    UnsupportedManifestSchemaError,
    load_component_definitions_from_file,
    load_component_definitions_from_text,
    load_default_component_definitions,
)
from zealfie.components.model import EntryPointContract


VALID_MANIFEST = """
schema_version = 1

[[components]]
id = "zesolver"
display_name = "ZeSolver"
distribution_name = "ZeSolver"

[[components.launch.entry_points]]
group = "gui_scripts"
name = "zesolver"
"""


def test_loads_valid_manifest() -> None:
    definitions = load_component_definitions_from_text(VALID_MANIFEST)

    assert len(definitions) == 1
    assert definitions[0].component_id == "zesolver"
    assert definitions[0].launch_entry_points == (EntryPointContract("gui_scripts", "zesolver"),)


def test_default_manifest_is_packaged_resource() -> None:
    definitions = load_default_component_definitions()
    resource = importlib.resources.files("zealfie.manifests").joinpath("components.toml")

    assert resource.is_file()
    assert definitions[0].component_id == "zesolver"


def test_manifest_file_missing_or_unreadable(tmp_path) -> None:
    with pytest.raises(InvalidComponentManifestError, match="manifest file could not be read"):
        load_component_definitions_from_file(tmp_path / "missing.toml")


@pytest.mark.parametrize(
    ("text", "message"),
    (
        ("components = []", "schema_version is required"),
        ("schema_version = '1'\ncomponents = []", "schema_version must be an integer"),
        ("schema_version = 1", "components must be a list"),
        ("schema_version = 1\ncomponents = []", "components must not be empty"),
        ("schema_version = 1\ncomponents = 'bad'", "components must be a list"),
        ("schema_version = 1\ncomponents = [1]", "components\\[0\\] must be a table"),
        (
            """
schema_version = 1
[[components]]
id = ""
display_name = "ZeSolver"
distribution_name = "ZeSolver"
""",
            "components\\[0\\].id must not be empty",
        ),
        (
            """
schema_version = 1
[[components]]
id = "zesolver"
display_name = ""
distribution_name = "ZeSolver"
""",
            "components\\[0\\].display_name must not be empty",
        ),
        (
            """
schema_version = 1
[[components]]
id = "zesolver"
display_name = "ZeSolver"
distribution_name = ""
""",
            "components\\[0\\].distribution_name must not be empty",
        ),
        (
            """
schema_version = 1
[[components]]
id = "zesolver"
display_name = "ZeSolver"
distribution_name = "ZeSolver"
launch = "bad"
""",
            "components\\[0\\].launch must be a table",
        ),
        (
            """
schema_version = 1
[[components]]
id = "zesolver"
display_name = "ZeSolver"
distribution_name = "ZeSolver"
[components.launch]
entry_points = "bad"
""",
            "components\\[0\\].launch.entry_points must be a list",
        ),
        (
            """
schema_version = 1
[[components]]
id = "zesolver"
display_name = "ZeSolver"
distribution_name = "ZeSolver"
[components.launch]
entry_points = []
""",
            "components\\[0\\].launch.entry_points must not be empty",
        ),
        (
            """
schema_version = 1
[[components]]
id = "zesolver"
display_name = "ZeSolver"
distribution_name = "ZeSolver"
[[components.launch.entry_points]]
group = ""
name = "zesolver"
""",
            "components\\[0\\].launch.entry_points\\[0\\].group must not be empty",
        ),
        (
            """
schema_version = 1
[[components]]
id = "zesolver"
display_name = "ZeSolver"
distribution_name = "ZeSolver"
[[components.launch.entry_points]]
group = "gui_scripts"
name = ""
""",
            "components\\[0\\].launch.entry_points\\[0\\].name must not be empty",
        ),
        (
            """
schema_version = 1
[[components]]
id = 4
display_name = "ZeSolver"
distribution_name = "ZeSolver"
""",
            "components\\[0\\].id must be a string",
        ),
    ),
)
def test_invalid_manifest_shapes(text: str, message: str) -> None:
    with pytest.raises(InvalidComponentManifestError, match=message):
        load_component_definitions_from_text(text)


def test_toml_invalid() -> None:
    with pytest.raises(InvalidComponentManifestError, match="manifest TOML is invalid"):
        load_component_definitions_from_text("schema_version = ")


def test_unsupported_schema() -> None:
    with pytest.raises(UnsupportedManifestSchemaError, match="unsupported schema_version: 2"):
        load_component_definitions_from_text("schema_version = 2\ncomponents = []")


def test_duplicate_component_id() -> None:
    text = VALID_MANIFEST + VALID_MANIFEST.replace("schema_version = 1", "")

    with pytest.raises(InvalidComponentManifestError, match="duplicate component id: zesolver"):
        load_component_definitions_from_text(text)


def test_duplicate_entry_point_contract() -> None:
    text = """
schema_version = 1

[[components]]
id = "zesolver"
display_name = "ZeSolver"
distribution_name = "ZeSolver"

[[components.launch.entry_points]]
group = "gui_scripts"
name = "zesolver"

[[components.launch.entry_points]]
group = "gui_scripts"
name = "zesolver"
"""

    with pytest.raises(
        InvalidComponentManifestError,
        match="duplicate entry point contract: gui_scripts:zesolver",
    ):
        load_component_definitions_from_text(text)


# ---------------------------------------------------------------------------
# M1-1A: required_extras
# ---------------------------------------------------------------------------

MANIFEST_WITH_EXTRAS = """
schema_version = 1

[[components]]
id = "zesolver"
display_name = "ZeSolver"
distribution_name = "ZeSolver"
required_extras = ["gui"]

[[components.launch.entry_points]]
group = "gui_scripts"
name = "zesolver"
"""


def test_parses_required_extras() -> None:
    definitions = load_component_definitions_from_text(MANIFEST_WITH_EXTRAS)
    assert len(definitions) == 1
    assert definitions[0].required_extras == ("gui",)


def test_default_manifest_parses_required_extras() -> None:
    definitions = load_default_component_definitions()
    assert definitions[0].required_extras == ("gui",)


def test_required_extras_defaults_to_empty() -> None:
    """A component without required_extras gets an empty tuple."""
    manifest = """
schema_version = 1

[[components]]
id = "zesolver"
display_name = "ZeSolver"
distribution_name = "ZeSolver"

[[components.launch.entry_points]]
group = "gui_scripts"
name = "zesolver"
"""
    definitions = load_component_definitions_from_text(manifest)
    assert definitions[0].required_extras == ()


@pytest.mark.parametrize(
    ("text", "message"),
    (
        (
            """
schema_version = 1
[[components]]
id = "zesolver"
display_name = "ZeSolver"
distribution_name = "ZeSolver"
required_extras = "not_a_list"

[[components.launch.entry_points]]
group = "gui_scripts"
name = "zesolver"
""",
            "required_extras must be a list of strings",
        ),
        (
            """
schema_version = 1
[[components]]
id = "zesolver"
display_name = "ZeSolver"
distribution_name = "ZeSolver"
required_extras = [1, 2]

[[components.launch.entry_points]]
group = "gui_scripts"
name = "zesolver"
""",
            "required_extras\\[0\\] must be a string",
        ),
        (
            """
schema_version = 1
[[components]]
id = "zesolver"
display_name = "ZeSolver"
distribution_name = "ZeSolver"
required_extras = [""]

[[components.launch.entry_points]]
group = "gui_scripts"
name = "zesolver"
""",
            "required_extras\\[0\\] must not be empty",
        ),
        (
            """
schema_version = 1
[[components]]
id = "zesolver"
display_name = "ZeSolver"
distribution_name = "ZeSolver"
required_extras = ["gui", "gui"]

[[components.launch.entry_points]]
group = "gui_scripts"
name = "zesolver"
""",
            "duplicate extra 'gui'",
        ),
    ),
)
def test_invalid_required_extras_shapes(text: str, message: str) -> None:
    with pytest.raises(InvalidComponentManifestError, match=message):
        load_component_definitions_from_text(text)


# ---------------------------------------------------------------------------
# M1-1A hardening: PEP 685 extras canonicalization in manifest
# ---------------------------------------------------------------------------


def test_required_extras_normalized_case_insensitive_dedup() -> None:
    """'Gui' and 'gui' are canonicalised to the same name → duplicate error."""
    manifest = """
schema_version = 1

[[components]]
id = "zesolver"
display_name = "ZeSolver"
distribution_name = "ZeSolver"
required_extras = ["Gui", "gui"]

[[components.launch.entry_points]]
group = "gui_scripts"
name = "zesolver"
"""
    with pytest.raises(InvalidComponentManifestError, match="duplicate extra 'gui'"):
        load_component_definitions_from_text(manifest)


def test_required_extras_normalized_dash_underscore_dedup() -> None:
    """'my-extra' and 'my_extra' are canonicalised to the same name → duplicate error."""
    manifest = """
schema_version = 1

[[components]]
id = "zesolver"
display_name = "ZeSolver"
distribution_name = "ZeSolver"
required_extras = ["my-extra", "my_extra"]

[[components.launch.entry_points]]
group = "gui_scripts"
name = "zesolver"
"""
    with pytest.raises(InvalidComponentManifestError, match="duplicate extra 'my_extra'"):
        load_component_definitions_from_text(manifest)


def test_required_extras_stored_canonical() -> None:
    """'Gui' → stored as 'gui', 'my_extra' → stored as 'my-extra'."""
    manifest = """
schema_version = 1

[[components]]
id = "zesolver"
display_name = "ZeSolver"
distribution_name = "ZeSolver"
required_extras = ["Gui", "my_extra"]

[[components.launch.entry_points]]
group = "gui_scripts"
name = "zesolver"
"""
    definitions = load_component_definitions_from_text(manifest)
    assert definitions[0].required_extras == ("gui", "my-extra")
