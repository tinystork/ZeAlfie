"""Release manifest and verified artifact models for M0-7A."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    """A parsed and validated local release manifest.

    The manifest describes exactly one release artefact.  It does **not**
    define the component's identity or launch contract — those remain in
    ``components.toml``.

    ``component_id`` links the release to a :class:`ComponentDefinition`.
    """

    schema_version: int
    component_id: str
    version: str
    filename: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class VerifiedArtifact:
    """Result of complete local release verification.

    All validations (path, size, SHA256, wheel identity, version,
    entry-point contract) have passed.  This object may be handed to the
    transaction engine for installation into a candidate slot.

    **TOCTOU semantics**

    ``VerifiedArtifact`` describes a verification performed at a point in
    time.  It is not a permanent authorization to install the path.  The
    artifact may need to be revalidated immediately before a future
    installation handoff.  No persistent trust cache is implied.
    """

    component_id: str
    version: str
    path: Path
    size: int
    sha256: str
    distribution_name: str
    wheel_version: str
