"""Parsing of wheel-embedded interoperability declarations.

Scans a primary product wheel for its canonical ``zesoftware_interop.json``
package-data file without importing or executing any product code.  Parses
and validates the declaration, and cross-checks the embedded
``distribution_name`` against the wheel's canonical ``.dist-info/METADATA``
``Name`` using PEP 503 normalization.

This module is product-agnostic: it knows nothing about any particular
provider or consumer product.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Iterable

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from zealfie.building import WheelInspectionError, inspect_wheel
from zealfie.common import normalise_distribution_name

from .model import (
    SCHEMA_V1,
    AnyOfGroup,
    ConsumerRequirement,
    InteropParseStatus,
    InteropRecord,
    ProviderDeclaration,
    WheelInterop,
)

# Canonical package-data filename scanned inside each primary wheel.
INTEROP_FILENAME = "zesoftware_interop.json"


class InteropParseError(ValueError):
    """Raised when an interop declaration fails structural validation.

    Carries a stable ``code`` suitable for machine-readable diagnostics.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def scan_wheel_interop(wheel_path: str | Path) -> WheelInterop:
    """Scan one primary wheel for its interoperability declaration.

    Returns
    -------
    WheelInterop
        A record whose ``status`` is one of ``VALID``, ``ABSENT``, or
        ``INVALID``.  The distribution name is always read from the
        wheel's canonical METADATA ``Name`` (normalized) and is populated
        even when the interop declaration itself is absent or invalid.
    """
    wheel = Path(wheel_path)

    # --- Identity from canonical METADATA (never from the JSON alone). ------
    try:
        inspected = inspect_wheel(wheel)
    except (WheelInspectionError, FileNotFoundError, OSError, ValueError) as exc:
        return WheelInterop(
            wheel_path=wheel,
            distribution_name="",
            status=InteropParseStatus.INVALID,
            reason_code="WHEEL_UNREADABLE",
            reason=f"cannot read wheel metadata: {exc}",
        )
    distribution_name = inspected.distribution_name

    # --- Locate interop members (top-level namespace / filename). -----------
    try:
        with zipfile.ZipFile(wheel, "r") as zf:
            members = _find_interop_members(zf)
            if len(members) == 0:
                return WheelInterop(
                    wheel_path=wheel,
                    distribution_name=distribution_name,
                    status=InteropParseStatus.ABSENT,
                    reason_code="NO_INTEROP_FILE",
                    reason="no interoperability declaration present",
                )
            if len(members) > 1:
                return WheelInterop(
                    wheel_path=wheel,
                    distribution_name=distribution_name,
                    status=InteropParseStatus.INVALID,
                    reason_code="DUPLICATE_INTEROP_FILE",
                    reason=(
                        f"multiple interop declarations in one distribution: "
                        f"{sorted(members)}"
                    ),
                )
            member = members[0]
            try:
                text = zf.read(member).decode("utf-8")
            except UnicodeDecodeError as exc:
                return WheelInterop(
                    wheel_path=wheel,
                    distribution_name=distribution_name,
                    status=InteropParseStatus.INVALID,
                    reason_code="INVALID_JSON",
                    reason=f"interop declaration is not valid UTF-8: {exc}",
                )
    except zipfile.BadZipFile as exc:
        return WheelInterop(
            wheel_path=wheel,
            distribution_name=distribution_name,
            status=InteropParseStatus.INVALID,
            reason_code="WHEEL_UNREADABLE",
            reason=f"invalid wheel archive: {exc}",
        )

    # --- Parse and validate against the wheel's canonical identity. ---------
    try:
        record = _parse_interop_record(text, distribution_name)
    except InteropParseError as exc:
        return WheelInterop(
            wheel_path=wheel,
            distribution_name=distribution_name,
            status=InteropParseStatus.INVALID,
            reason_code=exc.code,
            reason=str(exc),
        )

    return WheelInterop(
        wheel_path=wheel,
        distribution_name=distribution_name,
        status=InteropParseStatus.VALID,
        record=record,
    )


def scan_wheels_interop(wheel_paths: Iterable[str | Path]) -> tuple[WheelInterop, ...]:
    """Scan a sequence of primary wheels for interoperability metadata."""
    return tuple(scan_wheel_interop(p) for p in wheel_paths)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_interop_members(zf: zipfile.ZipFile) -> list[str]:
    """Return interop member names matching ``<top-level>/zesoftware_interop.json``.

    Uses ``infolist`` (not ``namelist``) so duplicate ZIP entries are
    preserved and detected.
    """
    found: list[str] = []
    for info in zf.infolist():
        name = info.filename
        if name.endswith("/"):
            continue
        parts = name.split("/")
        if len(parts) == 2 and parts[1] == INTEROP_FILENAME:
            found.append(name)
    return found


def _parse_interop_record(text: str, expected_distribution_name: str) -> InteropRecord:
    """Parse and validate an interop declaration against the wheel identity."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InteropParseError(
            "INVALID_JSON", f"interop declaration is not valid JSON: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise InteropParseError("INVALID_JSON", "top-level must be a JSON object")

    schema = data.get("schema")
    if schema != SCHEMA_V1:
        raise InteropParseError(
            "UNKNOWN_SCHEMA",
            f"unsupported schema {schema!r} (expected {SCHEMA_V1!r})",
        )

    product_id = _require_str(data, "product_id")
    declared_distribution = _require_str(data, "distribution_name")
    declared_normalised = normalise_distribution_name(declared_distribution)
    if declared_normalised != expected_distribution_name:
        raise InteropParseError(
            "DISTRIBUTION_NAME_MISMATCH",
            f"distribution_name {declared_distribution!r} (normalised "
            f"{declared_normalised!r}) does not match wheel METADATA Name "
            f"normalised to {expected_distribution_name!r}",
        )

    provides = _parse_provides(data.get("provides"))
    consumes = _parse_consumes(data.get("consumes"))

    return InteropRecord(
        distribution_name=expected_distribution_name,
        product_id=product_id,
        schema=schema,
        provides=provides,
        consumes=consumes,
    )


def _parse_provides(raw: object) -> tuple[ProviderDeclaration, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise InteropParseError("INVALID_PROVIDES", "'provides' must be a list")
    out: list[ProviderDeclaration] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise InteropParseError(
                "INVALID_PROVIDES", f"provides[{index}] must be an object"
            )
        api_module = _require_str(item, "api_module")
        api_version = _require_str(item, "api_version")
        _validate_api_version(api_version, f"provides[{index}].api_version")
        capabilities = _parse_capability_list(
            item.get("capabilities"), f"provides[{index}].capabilities"
        )
        out.append(
            ProviderDeclaration(
                api_module=api_module,
                api_version=api_version,
                capabilities=capabilities,
            )
        )
    return tuple(out)


def _parse_consumes(raw: object) -> tuple[ConsumerRequirement, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise InteropParseError("INVALID_CONSUMES", "'consumes' must be a list")
    out: list[ConsumerRequirement] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise InteropParseError(
                "INVALID_CONSUMES", f"consumes[{index}] must be an object"
            )
        provider_distribution_name = _require_str(
            item, "provider_distribution_name"
        )
        provider_distribution_norm = normalise_distribution_name(
            provider_distribution_name
        )
        provider_product_id = _optional_str(item, "provider_product_id")
        optional = _require_bool(item, "optional", f"consumes[{index}]")
        api_module = _require_str(item, "api_module")
        api_version = _require_str(item, "api_version")
        _validate_api_specifier(api_version, f"consumes[{index}].api_version")
        required_capabilities = _parse_capability_list(
            item.get("required_capabilities"), f"consumes[{index}].required_capabilities"
        )
        any_of = _parse_any_of(
            item.get("any_of_capabilities"), f"consumes[{index}].any_of_capabilities"
        )
        optional_capabilities = _parse_capability_list(
            item.get("optional_capabilities"), f"consumes[{index}].optional_capabilities"
        )
        out.append(
            ConsumerRequirement(
                provider_distribution_name=provider_distribution_norm,
                provider_product_id=provider_product_id,
                optional=optional,
                api_module=api_module,
                api_version=api_version,
                required_capabilities=required_capabilities,
                any_of_capabilities=any_of,
                optional_capabilities=optional_capabilities,
            )
        )
    return tuple(out)


def _parse_any_of(raw: object, field: str) -> tuple[AnyOfGroup, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise InteropParseError("INVALID_ANY_OF", f"{field!r} must be a list")
    out: list[AnyOfGroup] = []
    for index, group in enumerate(raw):
        if not isinstance(group, dict):
            raise InteropParseError(
                "INVALID_ANY_OF", f"{field}[{index}] must be an object"
            )
        group_id = _require_str(group, "id")
        capabilities = _parse_capability_list(
            group.get("capabilities"), f"{field}[{index}].capabilities"
        )
        required = group.get("required", True)
        if not isinstance(required, bool):
            raise InteropParseError(
                "INVALID_ANY_OF", f"{field}[{index}].required must be a boolean"
            )
        out.append(AnyOfGroup(id=group_id, capabilities=capabilities, required=required))
    return tuple(out)


def _parse_capability_list(raw: object, field: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise InteropParseError("INVALID_CAPABILITIES", f"{field!r} must be a list")
    caps: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            raise InteropParseError(
                "INVALID_CAPABILITIES",
                f"{field!r} entries must be non-empty strings",
            )
        caps.append(value)
    if len(caps) != len(set(caps)):
        raise InteropParseError(
            "DUPLICATE_CAPABILITY", f"{field!r} contains duplicate capability ids"
        )
    return tuple(caps)


def _validate_api_version(value: str, field: str) -> None:
    try:
        Version(value)
    except InvalidVersion as exc:
        raise InteropParseError(
            "INVALID_API_VERSION", f"{field!r} is not a valid PEP 440 version: {value!r}"
        ) from exc


def _validate_api_specifier(value: str, field: str) -> None:
    try:
        SpecifierSet(value)
    except InvalidSpecifier as exc:
        raise InteropParseError(
            "INVALID_SPECIFIER",
            f"{field!r} is not a valid PEP 440 specifier: {value!r}",
        ) from exc


def _require_str(obj: dict, key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InteropParseError(
            "MISSING_REQUIRED_FIELD", f"missing or empty required field {key!r}"
        )
    return value


def _optional_str(obj: dict, key: str) -> str | None:
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InteropParseError(
            "INVALID_FIELD", f"field {key!r} must be a non-empty string when present"
        )
    return value


def _require_bool(obj: dict, key: str, context: str) -> bool:
    value = obj.get(key)
    if not isinstance(value, bool):
        raise InteropParseError(
            "MISSING_REQUIRED_FIELD",
            f"{context}.{key} must be a boolean",
        )
    return value
