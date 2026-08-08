"""Strict TOML release manifest parser for M0-7B.

M0-7B extends the parser to handle multiple ``[[artifacts]]`` entries,
each with optional host compatibility tags.

M0-7B hardening adds fail-closed validation on tag values to prevent
overly-permissive prefix matches (e.g. ``python_tag="p"`` matching
``py312``).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from .model import ArtifactEntry, ReleaseManifest

SUPPORTED_SCHEMA_VERSION = 1


class ReleaseManifestError(ValueError):
    """Raised when a release manifest is structurally invalid."""


# ---------------------------------------------------------------------------
# Tag validation patterns (fail-closed — reject unexpectedly permissive tags)
# ---------------------------------------------------------------------------

# Python tag: pyN, cpN (e.g. py3, py312, cp312)
_PYTHON_TAG_RE = re.compile(r"^(?:py|cp)\d+$")

# ABI tag: none, cpN, abiN (e.g. none, cp312, abi3)
_ABI_TAG_RE = re.compile(r"^(?:none|cp\d+|abi\d+)$")

# Platform tag: any, or a non-empty wheel-like token
_PLATFORM_TAG_RE = re.compile(r"^(?:any|[A-Za-z0-9_]+)$")

# Validation map keyed by tag name.
_TAG_VALIDATORS: dict[str, tuple[re.Pattern, str]] = {
    "python_tag": (_PYTHON_TAG_RE, "e.g. py3, py312, cp312"),
    "abi_tag": (_ABI_TAG_RE, "e.g. none, cp312, abi3"),
    "platform_tag": (_PLATFORM_TAG_RE, "e.g. any, linux_x86_64, win_amd64"),
}


def _validate_tag(tag_name: str, value: str) -> None:
    """Validate a tag value against its expected pattern.

    Raises ``ReleaseManifestError`` if the value does not match the
    fail-closed pattern for *tag_name*.
    """
    pattern, expected = _TAG_VALIDATORS[tag_name]
    if not pattern.match(value):
        raise ReleaseManifestError(
            f"{tag_name} value {value!r} is not a recognised tag pattern; "
            f"expected form: {expected}"
        )


def parse_release_manifest(text: str) -> ReleaseManifest:
    """Parse and strictly validate a release manifest from TOML text.

    Unknown top-level keys are rejected.  Each artifact entry must
    declare at least *filename*, *size*, and *sha256*.  Optional host
    compatibility tags (*python_tag*, *abi_tag*, *platform_tag*) are
    accepted and validated against fail-closed patterns; unknown
    per-artifact keys are rejected.
    """
    try:
        payload = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ReleaseManifestError(f"invalid TOML: {exc}") from exc

    if not isinstance(payload, dict):
        raise ReleaseManifestError("release manifest root must be a TOML table")

    # --- schema_version ---
    schema = payload.get("schema_version")
    if not isinstance(schema, int) or isinstance(schema, bool):
        raise ReleaseManifestError("schema_version must be an integer")
    if schema != SUPPORTED_SCHEMA_VERSION:
        raise ReleaseManifestError(
            f"unsupported schema_version: {schema} "
            f"(supported: {SUPPORTED_SCHEMA_VERSION})"
        )

    # --- required string fields ---
    component_id = _required_string(payload, "component_id")
    version = _required_string(payload, "version")

    # --- artifacts list ---
    artifacts_raw = payload.get("artifacts")
    if not isinstance(artifacts_raw, list):
        raise ReleaseManifestError("artifacts must be a list")
    if len(artifacts_raw) == 0:
        raise ReleaseManifestError("artifacts must contain at least one entry")

    artifacts: list[ArtifactEntry] = []
    for i, entry in enumerate(artifacts_raw):
        if not isinstance(entry, dict):
            raise ReleaseManifestError(
                f"artifacts[{i}] must be a table"
            )

        filename = _required_string(entry, "filename")
        size = _required_int(entry, "size")
        sha256 = _required_sha256(entry, "sha256")

        # --- optional host compatibility tags (validated fail-closed) ---
        python_tag = _optional_tag(entry, "python_tag")
        abi_tag = _optional_tag(entry, "abi_tag")
        platform_tag = _optional_tag(entry, "platform_tag")

        # --- reject unknown keys ---
        artifact_known = {"filename", "size", "sha256",
                          "python_tag", "abi_tag", "platform_tag"}
        _reject_unknown_keys(entry, artifact_known, f"artifacts[{i}]")

        artifacts.append(ArtifactEntry(
            filename=filename,
            size=size,
            sha256=sha256,
            python_tag=python_tag,
            abi_tag=abi_tag,
            platform_tag=platform_tag,
        ))

    # --- reject duplicate filenames (ambiguity guard) ---
    seen_filenames: set[str] = set()
    for ae in artifacts:
        if ae.filename in seen_filenames:
            raise ReleaseManifestError(
                f"duplicate artifact filename: {ae.filename!r}"
            )
        seen_filenames.add(ae.filename)

    # --- reject unknown top-level keys ---
    _reject_unknown_keys(
        payload,
        {"schema_version", "component_id", "version", "artifacts"},
        "release manifest",
    )

    return ReleaseManifest(
        schema_version=schema,
        component_id=component_id,
        version=version,
        artifacts=tuple(artifacts),
    )


def parse_release_manifest_file(path: str | Path) -> ReleaseManifest:
    """Load and parse a release manifest from a TOML file."""
    text = Path(path).read_text(encoding="utf-8")
    return parse_release_manifest(text)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _required_string(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ReleaseManifestError(f"{key} must be a non-empty string")
    stripped = value.strip()
    if not stripped:
        raise ReleaseManifestError(f"{key} must not be empty")
    return stripped


def _required_int(payload: dict, key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ReleaseManifestError(f"{key} must be an integer")
    if value < 0:
        raise ReleaseManifestError(f"{key} must not be negative")
    return value


def _required_sha256(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ReleaseManifestError(f"{key} must be a string")
    stripped = value.strip().lower()
    if len(stripped) != 64:
        raise ReleaseManifestError(
            f"{key} must be exactly 64 hex characters, got {len(stripped)}"
        )
    try:
        int(stripped, 16)
    except ValueError:
        raise ReleaseManifestError(f"{key} must be valid hexadecimal")
    return stripped


def _optional_tag(payload: dict, key: str) -> str | None:
    """Parse an optional host compatibility tag.

    When present it must be a non-empty string matching the fail-closed
    pattern for that tag type.  ``None`` means the tag was not declared
    (absent metadata).
    """
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReleaseManifestError(f"{key} must be a string if present")
    stripped = value.strip()
    if not stripped:
        raise ReleaseManifestError(f"{key} must not be empty if present")
    # Validate tag value against fail-closed pattern.
    _validate_tag(key, stripped)
    return stripped


def _reject_unknown_keys(
    payload: dict, known: set[str], label: str
) -> None:
    extra = set(payload) - known
    if extra:
        raise ReleaseManifestError(
            f"unknown key(s) in {label}: {', '.join(sorted(extra))}"
        )
