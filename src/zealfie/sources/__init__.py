"""Remote source models and resolution for ZeSoftware products.

Describes where known products' source code lives (owner, repo, fallback
ref) and resolves those refs to exact immutable commit SHAs.

Network resolution is injectable so unit tests never depend on a real
GitHub connection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from zealfie.net import NetworkReasonCode


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_OWNER_REPO_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9._-]*[a-zA-Z0-9])?$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SourceError(ValueError):
    """Base class for remote source errors."""


class InvalidRemoteSourceError(SourceError):
    """Raised when remote source metadata is invalid."""


class SourceResolutionError(SourceError):
    """Raised when remote source resolution fails (network, missing ref,
    or invalid response from remote).

    Carries an optional machine-readable :attr:`reason_code` (a
    :class:`~zealfie.net.NetworkReasonCode`) and an optional
    :attr:`proxy_hint` diagnostic string.  Both default to ``None`` so
    existing raise sites and callers are unaffected.
    """

    def __init__(
        self,
        message: str,
        *,
        reason_code: "NetworkReasonCode | None" = None,
        proxy_hint: str | None = None,
    ) -> None:
        self.reason_code = reason_code
        self.proxy_hint = proxy_hint
        super().__init__(message)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RemoteSource:
    """Declarative remote repository metadata for a known product.

    Describes *where* the product's source code lives.  The ``ref`` is
    a fallback branch/tag name — resolution always returns an exact
    immutable commit SHA via :func:`resolve_source`.
    """

    owner: str
    repo: str
    ref: str

    def __post_init__(self) -> None:
        for field_name in ("owner", "repo", "ref"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise InvalidRemoteSourceError(
                    f"remote source {field_name} must not be empty"
                )
            object.__setattr__(self, field_name, value)

        # Validate owner and repo format.
        for field_name in ("owner", "repo"):
            value: str = getattr(self, field_name)
            if not _OWNER_REPO_RE.match(value):
                raise InvalidRemoteSourceError(
                    f"remote source {field_name} {value!r} is not a valid "
                    f"GitHub owner or repository name"
                )
            # Reject double-dot and consecutive special chars as edge-case guard.
            if ".." in value or "--" in value or "__" in value:
                raise InvalidRemoteSourceError(
                    f"remote source {field_name} {value!r} contains "
                    f"consecutive special characters"
                )


@dataclass(frozen=True, slots=True)
class ResolvedSource:
    """A remote source resolved to an exact immutable commit SHA.

    The ``commit_sha`` is **guaranteed** to be a 40-character hex
    string.  It is never a branch name, tag name, or abbreviated ref.

    This is the provenance object that downstream install/acquire
    pipelines consume.
    """

    source: RemoteSource
    commit_sha: str

    def __post_init__(self) -> None:
        sha = str(self.commit_sha or "").strip().lower()
        if not _SHA1_RE.match(sha):
            raise InvalidRemoteSourceError(
                f"commit_sha must be a 40-character hex string, "
                f"got {self.commit_sha!r}"
            )
        object.__setattr__(self, "commit_sha", sha)


# ---------------------------------------------------------------------------
# Resolution protocol
# ---------------------------------------------------------------------------


class SourceRefResolver(Protocol):
    """Protocol for resolving a (owner, repo, ref) to an immutable commit SHA.

    The returned string **must** be a 40-character hex SHA-1 commit hash.
    Implementations must never return a branch name, tag, or abbreviated
    ref.
    """

    def __call__(self, owner: str, repo: str, ref: str) -> str:
        """Resolve *ref* in ``owner/repo`` to a full commit SHA.

        Args:
            owner: Repository owner (user or org).
            repo: Repository name.
            ref: Git ref (branch, tag, or commit SHA).

        Returns:
            A 40-character hex commit SHA.

        Raises:
            SourceResolutionError: If the ref cannot be resolved.
        """


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_source(
    source: RemoteSource,
    *,
    resolver: SourceRefResolver,
) -> ResolvedSource:
    """Resolve a remote source to an immutable provenance object.

    The *resolver* callable is injected so that unit tests never touch
    real GitHub.  The resolver receives ``(owner, repo, ref)`` and must
    return a 40-character hex commit SHA.

    Returns a :class:`ResolvedSource` whose ``commit_sha`` is the exact
    40-character hex commit hash — never a branch name or mutable ref.
    """
    commit_sha = resolver(source.owner, source.repo, source.ref)
    return ResolvedSource(source=source, commit_sha=commit_sha)
