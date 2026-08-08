"""Release manifest, host target, and verified artifact models.

M0-7B extends the release manifest to support multiple wheel artifacts
per release, each with optional host compatibility tags.

M0-7B hardening adds fail-closed validation on ``HostTarget`` fields
to reject empty or whitespace-only strings.
"""

from __future__ import annotations

import sysconfig
import sys as _sys
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# Host target — describes the current or a synthetic host for artifact
# selection.  Immutable, testable, no runtime dependency.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HostTarget:
    """Machine-readable host compatibility target.

    Tags follow the wheel tag convention (PEP 425) but are stored as
    plain strings to avoid a runtime dependency on ``packaging``.

    All fields must be non-empty, non-whitespace strings.
    """

    python_tag: str
    """e.g. ``py312``, ``py311``."""

    abi_tag: str
    """e.g. ``cp312``, ``cp311``."""

    platform_tag: str
    """e.g. ``linux_x86_64``, ``win_amd64``, ``macosx_14_0_arm64``."""

    def __post_init__(self) -> None:
        """Validate all fields are non-empty, non-whitespace strings."""
        for field_name in ("python_tag", "abi_tag", "platform_tag"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"HostTarget.{field_name} must be a non-empty string, "
                    f"got {value!r}"
                )

    @classmethod
    def from_current_host(cls) -> HostTarget:
        """Build a ``HostTarget`` from the running Python interpreter.

        The detection layer is isolated here; no other module should
        call ``sys.platform`` or ``sysconfig`` directly for host
        compatibility.
        """
        platform = sysconfig.get_platform()  # e.g. "linux-x86_64"
        # Normalise to wheel-tag convention: underscores, no dots.
        platform_tag = platform.replace("-", "_").replace(".", "_")

        major, minor = _sys.version_info[:2]
        python_tag = f"py{major}{minor}"
        abi_tag = f"cp{major}{minor}"

        return cls(
            python_tag=python_tag,
            abi_tag=abi_tag,
            platform_tag=platform_tag,
        )


# ---------------------------------------------------------------------------
# Artifact entry — a single wheel artifact inside a release manifest.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArtifactEntry:
    """One wheel artifact entry in a release manifest.

    *filename*, *size*, and *sha256* are always required (M0-7A).
    *python_tag*, *abi_tag*, and *platform_tag* are optional; when
    present they describe the host platform the wheel was built for.
    """

    filename: str
    size: int
    sha256: str
    python_tag: str | None = None
    abi_tag: str | None = None
    platform_tag: str | None = None


# ---------------------------------------------------------------------------
# Release manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    """A parsed and validated local release manifest.

    The manifest describes one or more release artefacts.  It does **not**
    define the component's identity or launch contract — those remain in
    ``components.toml``.

    ``component_id`` links the release to a :class:`ComponentDefinition`.
    ``artifacts`` is a non-empty tuple of :class:`ArtifactEntry`.
    """

    schema_version: int
    component_id: str
    version: str
    artifacts: tuple[ArtifactEntry, ...]

    # Backward-compatible accessors for single-artifact manifests (M0-7A).
    @property
    def filename(self) -> str:
        if len(self.artifacts) != 1:
            raise AttributeError(
                f"ReleaseManifest has {len(self.artifacts)} artifacts; "
                f"use .artifacts instead of .filename"
            )
        return self.artifacts[0].filename

    @property
    def size(self) -> int:
        if len(self.artifacts) != 1:
            raise AttributeError(
                f"ReleaseManifest has {len(self.artifacts)} artifacts; "
                f"use .artifacts instead of .size"
            )
        return self.artifacts[0].size

    @property
    def sha256(self) -> str:
        if len(self.artifacts) != 1:
            raise AttributeError(
                f"ReleaseManifest has {len(self.artifacts)} artifacts; "
                f"use .artifacts instead of .sha256"
            )
        return self.artifacts[0].sha256


# ---------------------------------------------------------------------------
# Verified artifact
# ---------------------------------------------------------------------------


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
