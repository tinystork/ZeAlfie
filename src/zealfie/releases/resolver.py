"""Safe local release resolution for trusted local artifacts.

M0-7C provides the business-level release API:

``ReleaseManifest + HostTarget + ComponentRegistry + artifact_root``
``-> VerifiedArtifact``.

Callers do not pass an artifact index.  The resolver selects the single
host-compatible artifact and verifies that exact selected entry.
"""

from __future__ import annotations

from pathlib import Path

from zealfie.components import UnknownComponentError
from zealfie.components.registry import ComponentRegistry

from .model import ArtifactEntry, HostTarget, ReleaseManifest, VerifiedArtifact
from .selector import ArtifactSelectionError, select_artifact
from .verifier import ArtifactRejectionError, verify_artifact


class ReleaseResolutionError(ValueError):
    """Raised when a local release cannot be resolved fail-closed."""


def resolve_local_release(
    manifest: ReleaseManifest,
    *,
    registry: ComponentRegistry,
    artifact_root: Path,
    host: HostTarget,
) -> VerifiedArtifact:
    """Resolve and verify the single host-compatible local artifact.

    The resolver intentionally hides ``artifact_index`` from callers.  It
    resolves component identity through the trusted registry, validates
    declared wheel filename tags for every artifact entry, selects exactly
    one artifact for *host*, and then verifies that same artifact entry.
    """
    try:
        definition = registry.get(manifest.component_id)
    except UnknownComponentError as exc:
        raise ReleaseResolutionError(
            f"unknown component: {manifest.component_id!r}"
        ) from exc

    if definition.component_id != manifest.component_id:
        raise ReleaseResolutionError(
            f"component mismatch: manifest says {manifest.component_id!r}, "
            f"registry returned {definition.component_id!r}"
        )

    _validate_manifest_declared_tags_match_wheel_filenames(manifest)

    effective_manifest = _normalize_manifest_tags(manifest)

    try:
        artifact_index = select_artifact(effective_manifest, host)
    except ArtifactSelectionError as exc:
        raise ReleaseResolutionError(str(exc)) from exc

    try:
        return verify_artifact(
            manifest,
            registry=registry,
            artifact_root=artifact_root,
            artifact_index=artifact_index,
        )
    except ArtifactRejectionError as exc:
        raise ReleaseResolutionError(str(exc)) from exc


def _validate_manifest_declared_tags_match_wheel_filenames(
    manifest: ReleaseManifest,
) -> None:
    for artifact_entry in manifest.artifacts:
        _validate_declared_tags_match_wheel_filename(artifact_entry)


def _validate_declared_tags_match_wheel_filename(entry: ArtifactEntry) -> None:
    """Check declared manifest tags against the simple wheel filename suffix.

    This deliberately implements only the M0-7C subset: when any compatibility
    tag is declared, all three declared tags must literally match the final
    ``-{python_tag}-{abi_tag}-{platform_tag}.whl`` filename segments.
    Untagged historical artifacts are left to selection and verification.
    """
    declared = (entry.python_tag, entry.abi_tag, entry.platform_tag)
    if declared == (None, None, None):
        return

    if any(tag is None for tag in declared):
        raise ReleaseResolutionError(
            f"partial compatibility tags are not resolvable for "
            f"{entry.filename!r}"
        )

    parsed = _parse_simple_wheel_filename_tags(entry.filename)
    expected_python, expected_abi, expected_platform = parsed

    if entry.python_tag != expected_python:
        raise ReleaseResolutionError(
            f"python_tag mismatch for {entry.filename!r}: manifest declares "
            f"{entry.python_tag!r}, filename declares {expected_python!r}"
        )
    if entry.abi_tag != expected_abi:
        raise ReleaseResolutionError(
            f"abi_tag mismatch for {entry.filename!r}: manifest declares "
            f"{entry.abi_tag!r}, filename declares {expected_abi!r}"
        )
    if entry.platform_tag != expected_platform:
        raise ReleaseResolutionError(
            f"platform_tag mismatch for {entry.filename!r}: manifest declares "
            f"{entry.platform_tag!r}, filename declares {expected_platform!r}"
        )


def _normalize_manifest_tags(manifest: ReleaseManifest) -> ReleaseManifest:
    """Derive effective compatibility tags for fully-untagged artifact entries.

    For entries where all three tags are ``None`` (historical M0-7A style),
    derive ``(python_tag, abi_tag, platform_tag)`` from the wheel filename
    suffix.  This ensures that a platform-specific wheel with no manifest tags
    (e.g. ``py3-none-win_amd64``) is not treated as universally compatible.

    Partially-tagged entries (some tags present, some ``None``) are rejected
    fail-closed.

    Fully-tagged entries are left unchanged.

    Returns a new ``ReleaseManifest``; the original is not mutated.
    """
    normalized: list[ArtifactEntry] = []

    for entry in manifest.artifacts:
        declared = (entry.python_tag, entry.abi_tag, entry.platform_tag)

        if declared == (None, None, None):
            parsed = _parse_simple_wheel_filename_tags(entry.filename)
            normalized.append(
                ArtifactEntry(
                    filename=entry.filename,
                    size=entry.size,
                    sha256=entry.sha256,
                    python_tag=parsed[0],
                    abi_tag=parsed[1],
                    platform_tag=parsed[2],
                )
            )
        elif any(tag is None for tag in declared):
            raise ReleaseResolutionError(
                f"partial compatibility tags are not resolvable in the safe resolver for "
                f"{entry.filename!r}"
            )
        else:
            normalized.append(entry)

    return ReleaseManifest(
        schema_version=manifest.schema_version,
        component_id=manifest.component_id,
        version=manifest.version,
        artifacts=tuple(normalized),
    )


def _parse_simple_wheel_filename_tags(filename: str) -> tuple[str, str, str]:
    if not filename.endswith(".whl"):
        raise ReleaseResolutionError(
            f"cannot parse wheel tags from non-wheel filename: {filename!r}"
        )

    stem = filename[:-4]
    parts = stem.split("-")
    if len(parts) < 4:
        raise ReleaseResolutionError(
            f"cannot parse wheel tags from filename: {filename!r}"
        )

    python_tag, abi_tag, platform_tag = parts[-3:]
    if not python_tag or not abi_tag or not platform_tag:
        raise ReleaseResolutionError(
            f"cannot parse wheel tags from filename: {filename!r}"
        )
    return python_tag, abi_tag, platform_tag
