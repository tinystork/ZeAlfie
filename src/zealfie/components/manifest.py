"""Load and validate local component manifests."""

from __future__ import annotations

import importlib.resources
import tomllib
from pathlib import Path
from typing import Any

from packaging.utils import canonicalize_name

from .model import ComponentDefinition, EntryPointContract


SUPPORTED_SCHEMA_VERSION = 1
DEFAULT_MANIFEST_PACKAGE = "zealfie.manifests"
DEFAULT_MANIFEST_NAME = "components.toml"


class ManifestError(RuntimeError):
    """Base class for component manifest errors."""


class UnsupportedManifestSchemaError(ManifestError):
    """Raised when a manifest uses an unsupported schema version."""


class InvalidComponentManifestError(ManifestError):
    """Raised when a manifest is malformed."""


def load_default_component_definitions() -> tuple[ComponentDefinition, ...]:
    try:
        resource = importlib.resources.files(DEFAULT_MANIFEST_PACKAGE).joinpath(DEFAULT_MANIFEST_NAME)
        return load_component_definitions_from_text(resource.read_text(encoding="utf-8"))
    except ManifestError:
        raise
    except Exception as exc:
        raise InvalidComponentManifestError(f"manifest resource could not be read: {exc}") from exc


def load_component_definitions_from_file(path: str | Path) -> tuple[ComponentDefinition, ...]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception as exc:
        raise InvalidComponentManifestError(f"manifest file could not be read: {exc}") from exc
    return load_component_definitions_from_text(text)


def load_component_definitions_from_text(text: str) -> tuple[ComponentDefinition, ...]:
    try:
        payload = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise InvalidComponentManifestError(f"manifest TOML is invalid: {exc}") from exc
    return load_component_definitions(payload)


def load_component_definitions(payload: dict[str, Any]) -> tuple[ComponentDefinition, ...]:
    schema_version = payload.get("schema_version")
    if schema_version is None:
        raise InvalidComponentManifestError("schema_version is required")
    if not isinstance(schema_version, int):
        raise InvalidComponentManifestError("schema_version must be an integer")
    if schema_version != SUPPORTED_SCHEMA_VERSION:
        raise UnsupportedManifestSchemaError(f"unsupported schema_version: {schema_version}")

    components = payload.get("components")
    if not isinstance(components, list):
        raise InvalidComponentManifestError("components must be a list")
    if not components:
        raise InvalidComponentManifestError("components must not be empty")

    definitions: list[ComponentDefinition] = []
    seen_ids: set[str] = set()
    for index, raw_component in enumerate(components):
        if not isinstance(raw_component, dict):
            raise InvalidComponentManifestError(f"components[{index}] must be a table")
        definition = _component_from_payload(raw_component, index)
        if definition.component_id in seen_ids:
            raise InvalidComponentManifestError(f"duplicate component id: {definition.component_id}")
        seen_ids.add(definition.component_id)
        definitions.append(definition)
    return tuple(definitions)


def _component_from_payload(raw_component: dict[str, Any], index: int) -> ComponentDefinition:
    component_id = _required_string(raw_component, "id", f"components[{index}].id")
    display_name = _required_string(
        raw_component, "display_name", f"components[{index}].display_name"
    )
    distribution_name = _required_string(
        raw_component, "distribution_name", f"components[{index}].distribution_name"
    )
    launch = raw_component.get("launch")
    if not isinstance(launch, dict):
        raise InvalidComponentManifestError(f"components[{index}].launch must be a table")
    entry_points = launch.get("entry_points")
    if not isinstance(entry_points, list):
        raise InvalidComponentManifestError(
            f"components[{index}].launch.entry_points must be a list"
        )
    if not entry_points:
        raise InvalidComponentManifestError(
            f"components[{index}].launch.entry_points must not be empty"
        )

    contracts: list[EntryPointContract] = []
    seen_contracts: set[EntryPointContract] = set()
    for ep_index, raw_entry_point in enumerate(entry_points):
        if not isinstance(raw_entry_point, dict):
            raise InvalidComponentManifestError(
                f"components[{index}].launch.entry_points[{ep_index}] must be a table"
            )
        contract = EntryPointContract(
            group=_required_string(
                raw_entry_point,
                "group",
                f"components[{index}].launch.entry_points[{ep_index}].group",
            ),
            name=_required_string(
                raw_entry_point,
                "name",
                f"components[{index}].launch.entry_points[{ep_index}].name",
            ),
        )
        if contract in seen_contracts:
            raise InvalidComponentManifestError(
                f"duplicate entry point contract: {contract.group}:{contract.name}"
            )
        seen_contracts.add(contract)
        contracts.append(contract)

    # --- required_extras (M1-1A) ---
    required_extras: tuple[str, ...] = ()
    raw_extras = raw_component.get("required_extras")
    if raw_extras is not None:
        if not isinstance(raw_extras, list):
            raise InvalidComponentManifestError(
                f"components[{index}].required_extras must be a list of strings"
            )
        extras_canonical: list[str] = []
        seen_canonical: set[str] = set()
        for ei, raw_extra in enumerate(raw_extras):
            if not isinstance(raw_extra, str):
                raise InvalidComponentManifestError(
                    f"components[{index}].required_extras[{ei}] must be a string"
                )
            extra = raw_extra.strip()
            if not extra:
                raise InvalidComponentManifestError(
                    f"components[{index}].required_extras[{ei}] must not be empty"
                )
            # PEP 685: canonicalize before deduplication.
            # "Gui" and "gui" → both "gui"; "my_extra" → "my-extra".
            canon = canonicalize_name(extra)
            if canon in seen_canonical:
                raise InvalidComponentManifestError(
                    f"components[{index}].required_extras: duplicate extra {extra!r}"
                )
            seen_canonical.add(canon)
            extras_canonical.append(canon)
        required_extras = tuple(extras_canonical)

    return ComponentDefinition(
        component_id=component_id,
        display_name=display_name,
        distribution_name=distribution_name,
        launch_entry_points=tuple(contracts),
        required_extras=required_extras,
    )


def _required_string(payload: dict[str, Any], key: str, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise InvalidComponentManifestError(f"{label} must be a string")
    value = value.strip()
    if not value:
        raise InvalidComponentManifestError(f"{label} must not be empty")
    return value
