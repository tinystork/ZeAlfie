"""Trusted local release manifest and artifact verification for ZeAlfie.

M0-7A introduces a release verification layer that sits **above** the
transaction engine.  It validates local artifacts against a local TOML
manifest before handing a ``VerifiedArtifact`` to the runtime.

M0-7B adds host-compatibility tags, multi-artifact manifests, and
deterministic artifact selection.
"""

from __future__ import annotations

from .manifest import (
    ReleaseManifestError,
    parse_release_manifest,
    parse_release_manifest_file,
    SUPPORTED_SCHEMA_VERSION,
)
from .model import (
    ArtifactEntry,
    HostTarget,
    ReleaseManifest,
    VerifiedArtifact,
)
from .selector import (
    ArtifactSelectionError,
    select_artifact,
)
from .verifier import (
    ArtifactRejectionError,
    verify_artifact,
)

# Re-export the canonical normaliser for convenience.
from zealfie.common import normalise_distribution_name

__all__ = [
    "ArtifactEntry",
    "ArtifactRejectionError",
    "ArtifactSelectionError",
    "HostTarget",
    "ReleaseManifest",
    "ReleaseManifestError",
    "SUPPORTED_SCHEMA_VERSION",
    "VerifiedArtifact",
    "normalise_distribution_name",
    "parse_release_manifest",
    "parse_release_manifest_file",
    "select_artifact",
    "verify_artifact",
]
