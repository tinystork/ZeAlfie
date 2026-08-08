"""Deterministic artifact selection based on host compatibility.

M0-7B: given a release manifest with one or more artifacts and a
``HostTarget``, return the single compatible artifact index — or
reject clearly.

Rules (documented, testable, fail-closed):

1. An artifact without *any* host-compatibility tags (``python_tag``,
   ``abi_tag``, ``platform_tag`` are all ``None``) is treated as
   **universal**: it matches every host.  This preserves backward
   compatibility with M0-7A single-artifact manifests.

2. An artifact with at least one tag uses **strict matching**:
   *presence* of a tag means the host must match that dimension.
   Missing tags in a partially-tagged artifact are **incompatible**
   (fail-closed — one missing dimension blocks the match).

3. Tag matching rules:

   * *python_tag*: exact match, or the artifact tag is a **prefix**
     of the host tag (e.g. ``py3`` matches ``py312``).

   * *abi_tag*: exact match, **or** the artifact tag is ``"none"``
     (pure-Python wheel — compatible with any host ABI).

   * *platform_tag*: exact match, **or** the artifact tag is
     ``"any"`` (pure-Python / platform-independent wheel).

4. **Zero** compatible artifacts → ``ArtifactSelectionError``.

5. **Multiple** indistinguishable compatible artifacts →
   ``ArtifactSelectionError`` (ambiguity — manual resolution required).
   The order of entries in the TOML is **not** a tiebreaker.

6. The caller is responsible for host detection; the selector only
   compares tags.
"""

from __future__ import annotations

from .model import HostTarget, ReleaseManifest


class ArtifactSelectionError(ValueError):
    """Raised when no compatible artifact can be selected deterministically."""


def select_artifact(
    manifest: ReleaseManifest,
    host: HostTarget,
) -> int:
    """Return the index of the single compatible artifact.

    Raises ``ArtifactSelectionError`` when zero or multiple artifacts
    are compatible.
    """
    compatible: list[int] = []

    for i, ae in enumerate(manifest.artifacts):
        if _artifact_compatible(ae.python_tag, ae.abi_tag, ae.platform_tag, host):
            compatible.append(i)

    if len(compatible) == 0:
        raise ArtifactSelectionError(
            f"no artifact compatible with host "
            f"(python={host.python_tag}, abi={host.abi_tag}, "
            f"platform={host.platform_tag}) "
            f"among {len(manifest.artifacts)} artifact(s)"
        )

    if len(compatible) > 1:
        indices = ", ".join(str(i) for i in compatible)
        raise ArtifactSelectionError(
            f"ambiguous selection: {len(compatible)} artifacts are "
            f"compatible with host "
            f"(python={host.python_tag}, abi={host.abi_tag}, "
            f"platform={host.platform_tag}): "
            f"indices [{indices}]. "
            f"Provide distinct host tags to disambiguate."
        )

    return compatible[0]


# ---------------------------------------------------------------------------
# Compatibility check for a single artifact
# ---------------------------------------------------------------------------


def _artifact_compatible(
    python_tag: str | None,
    abi_tag: str | None,
    platform_tag: str | None,
    host: HostTarget,
) -> bool:
    """Check whether a single artifact entry is compatible with *host*.

    Universal artifact (all tags ``None``): always compatible.
    Partially-tagged artifact: missing tags are incompatible (fail-closed).
    Tagged artifact: apply prefix/exact/special-case matching.
    """
    # Universal artifact — all tags absent.
    if python_tag is None and abi_tag is None and platform_tag is None:
        return True

    # At least one tag is present — every declared tag must match.
    # A missing tag in a partially-tagged artifact → incompatible.
    if python_tag is not None:
        if not _python_tag_compatible(python_tag, host.python_tag):
            return False
    else:
        return False  # fail-closed: missing python_tag in tagged artifact

    if abi_tag is not None:
        if not _abi_tag_compatible(abi_tag, host.abi_tag):
            return False
    else:
        return False

    if platform_tag is not None:
        if not _platform_tag_compatible(platform_tag, host.platform_tag):
            return False
    else:
        return False

    return True


def _python_tag_compatible(artifact_tag: str, host_tag: str) -> bool:
    """Python tag: exact match or artifact is a prefix of host.

    ``py3``  matches ``py312`` (prefix).
    ``py312`` matches ``py312`` (exact).
    ``py312`` does **not** match ``py311``.
    """
    return host_tag == artifact_tag or host_tag.startswith(artifact_tag)


def _abi_tag_compatible(artifact_tag: str, host_tag: str) -> bool:
    """ABI tag: exact match, or artifact ``none`` matches any host.

    ``none``  matches ``cp312`` (pure-Python wheel).
    ``cp312`` matches ``cp312`` (exact).
    ``cp312`` does **not** match ``cp311``.
    """
    if artifact_tag == "none":
        return True
    return artifact_tag == host_tag


def _platform_tag_compatible(artifact_tag: str, host_tag: str) -> bool:
    """Platform tag: exact match, or artifact ``any`` matches any host.

    ``any``           matches ``linux_x86_64`` (pure-Python wheel).
    ``linux_x86_64``  matches ``linux_x86_64`` (exact).
    ``win_amd64``     does **not** match ``linux_x86_64``.
    """
    if artifact_tag == "any":
        return True
    return artifact_tag == host_tag
