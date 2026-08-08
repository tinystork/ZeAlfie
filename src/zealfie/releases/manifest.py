"""Strict TOML release manifest parser for M0-7A."""

from __future__ import annotations

import tomllib
from pathlib import Path

from .model import ReleaseManifest

SUPPORTED_SCHEMA_VERSION = 1


class ReleaseManifestError(ValueError):
    """Raised when a release manifest is structurally invalid."""


def parse_release_manifest(text: str) -> ReleaseManifest:
    """Parse and strictly validate a release manifest from TOML text.

    Unknown top-level keys are rejected.
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
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        raise ReleaseManifestError("artifacts must be a list")
    if len(artifacts) != 1:
        raise ReleaseManifestError(
            f"artifacts must contain exactly one entry, got {len(artifacts)}"
        )

    artifact = artifacts[0]
    if not isinstance(artifact, dict):
        raise ReleaseManifestError("artifacts[0] must be a table")

    filename = _required_string(artifact, "filename")
    size = _required_int(artifact, "size")
    sha256 = _required_sha256(artifact, "sha256")

    # --- reject unknown keys ---
    _reject_unknown_keys(
        payload,
        {"schema_version", "component_id", "version", "artifacts"},
        "release manifest",
    )
    _reject_unknown_keys(
        artifact,
        {"filename", "size", "sha256"},
        "artifact entry",
    )

    return ReleaseManifest(
        schema_version=schema,
        component_id=component_id,
        version=version,
        filename=filename,
        size=size,
        sha256=sha256,
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


def _reject_unknown_keys(
    payload: dict, known: set[str], label: str
) -> None:
    extra = set(payload) - known
    if extra:
        raise ReleaseManifestError(
            f"unknown key(s) in {label}: {', '.join(sorted(extra))}"
        )
