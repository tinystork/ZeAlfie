"""Artifact verifier — integrity, identity, and contract checks for M0-7A/M0-7B.

M0-7B adds *artifact_index* support so the verifier operates on the
correct entry from a multi-artifact release manifest.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from zealfie.building import WheelInspectionError, inspect_wheel
from zealfie.common import normalise_distribution_name
from zealfie.components.model import ComponentDefinition, EntryPointContract
from zealfie.components.registry import ComponentRegistry

from .manifest import ReleaseManifest
from .model import ArtifactEntry, HostTarget, VerifiedArtifact
from .selector import select_artifact


class ArtifactRejectionError(ValueError):
    """Raised when an artifact fails verification.  The detail explains why."""


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def _validate_filename(filename: str) -> None:
    """Reject filenames that look like paths, and enforce ``.whl`` extension."""
    if not filename or filename.strip() != filename:
        raise ArtifactRejectionError(f"invalid artifact filename: {filename!r}")
    if "/" in filename or "\\" in filename:
        raise ArtifactRejectionError(
            f"artifact filename must not contain path separators: {filename!r}"
        )
    if filename.startswith(".") or ".." in filename:
        raise ArtifactRejectionError(
            f"artifact filename must not contain ..: {filename!r}"
        )
    if filename.startswith("/") or (len(filename) >= 2 and filename[1] == ":"):
        raise ArtifactRejectionError(
            f"artifact filename must not be absolute: {filename!r}"
        )
    # M0-7A only supports wheels.
    if not filename.endswith(".whl") or filename == ".whl":
        raise ArtifactRejectionError(
            f"artifact filename must end with .whl: {filename!r}"
        )


def _resolve_safe_artifact_path(artifact_root: Path, filename: str) -> Path:
    """Resolve *filename* under *artifact_root*, rejecting escapes and symlinks."""
    _validate_filename(filename)
    lexical = artifact_root / filename
    # Reject symlinks before resolve.
    if lexical.is_symlink():
        raise ArtifactRejectionError(
            f"artifact must not be a symlink: {lexical}"
        )
    candidate = lexical.resolve(strict=False)
    root = artifact_root.resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ArtifactRejectionError(
            f"artifact path {candidate} escapes artifact root {root}"
        )
    if not candidate.is_file():
        raise ArtifactRejectionError(f"artifact file not found: {candidate}")
    return candidate


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------


def _compute_sha256(path: Path) -> str:
    """Stream hash of *path* — never loads the whole file into RAM."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)  # 1 MiB
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Entry-point contract from wheel
# ---------------------------------------------------------------------------


def _wheel_has_contract(
    info: "zealfie.building.InspectedWheel",
    definition: ComponentDefinition,
) -> bool:
    """Check the wheel's entry points for at least one expected contract."""
    expected = set(definition.launch_entry_points)
    if not expected:
        return True
    for ep in info.entry_points:
        if EntryPointContract(group=ep.group, name=ep.name) in expected:
            return True
    return False


# ---------------------------------------------------------------------------
# Verification orchestrator
# ---------------------------------------------------------------------------


def verify_artifact(
    manifest: ReleaseManifest,
    *,
    registry: ComponentRegistry,
    artifact_root: Path,
    artifact_index: int | None = None,
) -> VerifiedArtifact:
    """Run the complete verification chain against *manifest*.

    The manifest's ``component_id`` is resolved against *registry*.
    A mismatch between the requested and resolved component is rejected.

    *artifact_index* selects which artifact entry to verify.  If
    ``None`` and the manifest contains exactly one artifact, index 0
    is used (M0-7A backward compatibility).  For multi-artifact
    manifests the caller must pass an explicit index, typically
    obtained via :func:`select_artifact`.
    """
    from zealfie.components import UnknownComponentError

    # Resolve component via trusted registry.
    try:
        definition = registry.get(manifest.component_id)
    except UnknownComponentError:
        raise ArtifactRejectionError(
            f"unknown component: {manifest.component_id!r}"
        )
    if definition.component_id != manifest.component_id:
        raise ArtifactRejectionError(
            f"component mismatch: manifest says {manifest.component_id!r}, "
            f"registry returned {definition.component_id!r}"
        )

    # --- resolve artifact_index ---
    if artifact_index is None:
        if len(manifest.artifacts) == 1:
            artifact_index = 0
        else:
            raise ArtifactRejectionError(
                f"explicit artifact_index required for multi-artifact manifest "
                f"({len(manifest.artifacts)} artifacts); "
                f"use select_artifact() to resolve the correct index"
            )

    if artifact_index < 0 or artifact_index >= len(manifest.artifacts):
        raise ArtifactRejectionError(
            f"artifact_index {artifact_index} out of range "
            f"(manifest has {len(manifest.artifacts)} artifact(s))"
        )
    artifact_entry = manifest.artifacts[artifact_index]
    # --- end artifact_index ---

    # 1. Path
    artifact_path = _resolve_safe_artifact_path(
        artifact_root, artifact_entry.filename
    )

    # 2. Size
    actual_size = artifact_path.stat().st_size
    if actual_size != artifact_entry.size:
        raise ArtifactRejectionError(
            f"size mismatch: expected {artifact_entry.size}, got {actual_size}"
        )

    # 3. SHA256
    actual_hash = _compute_sha256(artifact_path)
    if actual_hash != artifact_entry.sha256:
        raise ArtifactRejectionError(
            f"SHA256 mismatch: expected {artifact_entry.sha256}, got {actual_hash}"
        )

    # 4. Wheel identity from METADATA (canonical inspect_wheel).
    try:
        info = inspect_wheel(artifact_path)
    except WheelInspectionError as exc:
        raise ArtifactRejectionError(f"wheel inspection failed: {exc}") from exc
    wheel_name = info.distribution_name
    wheel_version = info.version

    # 5. Distribution match
    expected_dist = normalise_distribution_name(definition.distribution_name)
    if wheel_name != expected_dist:
        raise ArtifactRejectionError(
            f"distribution mismatch: wheel is {wheel_name!r}, "
            f"expected {expected_dist!r}"
        )

    # 6. Version match
    if manifest.version != wheel_version:
        raise ArtifactRejectionError(
            f"version mismatch: manifest says {manifest.version!r}, "
            f"wheel says {wheel_version!r}"
        )

    # 7. Entry-point contract
    if not _wheel_has_contract(info, definition):
        expected_str = ", ".join(
            f"{ep.group}:{ep.name}" for ep in definition.launch_entry_points
        )
        raise ArtifactRejectionError(
            f"wheel does not declare expected launch contract(s): [{expected_str}]"
        )

    return VerifiedArtifact(
        component_id=manifest.component_id,
        version=manifest.version,
        path=artifact_path,
        size=actual_size,
        sha256=actual_hash,
        distribution_name=wheel_name,
        wheel_version=wheel_version,
    )


# ---------------------------------------------------------------------------
# M0-8B foundation: artifact revalidation primitive
# ---------------------------------------------------------------------------


def revalidate_verified_artifact(
    verified: VerifiedArtifact,
    *,
    registry: ComponentRegistry,
    artifact_root: Path | None = None,
) -> VerifiedArtifact:
    """Re-check a :class:`VerifiedArtifact` immediately before pip handoff.

    Re-runs the full M0-7 verification chain (path safety, size, SHA256,
    wheel identity, version, distribution match, entry-point contract)
    without duplicating any hash or inspection logic.  The caller can use
    this to protect against TOCTOU — a ``VerifiedArtifact`` is a
    point-in-time proof, not a permanent authorization.

    Parameters
    ----------
    verified:
        The previously-verified artifact to revalidate.
    registry:
        The trusted component registry (must contain a definition for
        ``verified.component_id``).
    artifact_root:
        Root directory for path-resolution.  Defaults to
        ``verified.path.parent``, which is correct for the common case
        where the artifact was verified in-place.

    Returns
    -------
    VerifiedArtifact
        A fresh ``VerifiedArtifact`` reflecting the re-validated state.

    Raises
    ------
    ArtifactRejectionError
        If any M0-7 check fails (changed size, changed SHA256, malformed
        wheel, distribution mis-match, version mismatch, broken contract).
    """
    if artifact_root is None:
        artifact_root = verified.path.parent

    # Construct a minimal single-artifact ReleaseManifest from the
    # VerifiedArtifact fields so we can reuse verify_artifact unchanged.
    artifact_entry = ArtifactEntry(
        filename=verified.path.name,
        size=verified.size,
        sha256=verified.sha256,
    )
    manifest = ReleaseManifest(
        schema_version=1,
        component_id=verified.component_id,
        version=verified.version,
        artifacts=(artifact_entry,),
    )

    return verify_artifact(
        manifest,
        registry=registry,
        artifact_root=artifact_root,
    )
