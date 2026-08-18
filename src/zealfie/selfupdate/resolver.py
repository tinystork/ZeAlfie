"""ZeAlfie self-update resolution (ZA-M1-4 LOT D §B).

Resolves "what is the newest ZeAlfie release on the chosen channel" to an
*immutable* provenance object: the newest tag becomes a 40-hex commit SHA,
never a mutable branch or abbreviated ref.

The resolution reuses the canonical network transport posture from
``zealfie.net`` (proxy-aware TLS-verified opener, bounded transient-only
retry, reason-code classifier) and the existing
:class:`~zealfie.sources.github.GitHubSourceRefResolver` for ref→SHA
resolution.  Tag discovery uses the same opener/retry/classifier via
:class:`GitHubTagsLister`.
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.request
from dataclasses import dataclass
from urllib.error import HTTPError, URLError

from packaging.version import InvalidVersion, Version

from zealfie.net import (
    build_default_opener,
    classify_exception,
    proxy_hint_for,
    retry_transient,
)
from zealfie.sources import SourceResolutionError
from zealfie.sources.github import (
    _DEFAULT_RETRIES,
    _DEFAULT_RETRY_DELAY,
    _DEFAULT_TIMEOUT,
    _DEFAULT_USER_AGENT,
    _GITHUB_API_BASE,
    _github_token_from_env,
)

from .identity import ZeAlfieIdentity

__all__ = [
    "DEFAULT_SOURCE_OWNER",
    "DEFAULT_SOURCE_REPO",
    "GitHubTagsLister",
    "SelfUpdateResolutionError",
    "UpdateResolution",
    "resolve_available_update",
]

DEFAULT_SOURCE_OWNER = "tinystork"
DEFAULT_SOURCE_REPO = "ZeAlfie"

_VALID_CHANNELS = ("stable", "beta")

_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


class SelfUpdateResolutionError(ValueError):
    """Raised when a self-update cannot be resolved fail-closed."""


@dataclass(frozen=True, slots=True)
class UpdateResolution:
    """A resolved self-update target with immutable provenance.

    ``commit_sha`` is guaranteed to be a 40-hex commit SHA (never a branch
    or abbreviated ref).  ``requested_ref`` is the tag name that was
    resolved.  ``available_version`` is the normalized (PEP 440) version of
    that tag, matching the version recorded in the built wheel's METADATA.
    """

    current_version: str
    available_version: str
    channel: str  # "stable" | "beta"
    source_owner: str
    source_repo: str
    requested_ref: str
    commit_sha: str
    up_to_date: bool


@dataclass(frozen=True, slots=True)
class _TagCandidate:
    tag: str
    version: Version


# ---------------------------------------------------------------------------
# GitHub tags lister (same canonical transport posture as sources.github)
# ---------------------------------------------------------------------------


class GitHubTagsLister:
    """List repository tag names from the GitHub REST API.

    ``GET /repos/{owner}/{repo}/tags`` → ``[{"name": ..., "commit": ...}]``.
    Only the tag *names* are returned; tokens are never leaked into
    messages (the ``Authorization`` header is the same env-token pattern as
    :mod:`zealfie.sources.github`).

    Constructor args mirror :class:`~zealfie.sources.github.GitHubSourceRefResolver`.
    """

    def __init__(
        self,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        user_agent: str = _DEFAULT_USER_AGENT,
        _opener: urllib.request.OpenerDirector | None = None,
        retries: int = _DEFAULT_RETRIES,
        retry_delay: float = _DEFAULT_RETRY_DELAY,
    ) -> None:
        self._timeout = timeout
        self._user_agent = user_agent
        self._opener = _opener
        self._retries = retries
        self._retry_delay = retry_delay

    def __call__(self, owner: str, repo: str) -> list[str]:
        """Return the list of tag names for ``owner/repo``."""
        url = f"{_GITHUB_API_BASE}/repos/{owner}/{repo}/tags"
        try:
            data = self._get_json(url)
        except (HTTPError, URLError, OSError, http.client.HTTPException) as exc:
            reason_code, reason_msg = classify_exception(exc)
            hint = proxy_hint_for(reason_code)
            raise SourceResolutionError(
                f"{reason_msg} listing tags for {owner}/{repo}",
                reason_code=reason_code,
                proxy_hint=hint,
            ) from exc

        if not isinstance(data, list):
            raise SourceResolutionError(
                f"GitHub API returned a non-list response listing tags "
                f"for {owner}/{repo}"
            )
        names: list[str] = []
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                names.append(item["name"])
        return names

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_json(self, url: str) -> object:
        """GET *url* and parse the response as JSON."""
        headers = {"User-Agent": self._user_agent}
        token = _github_token_from_env()
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"

        req = urllib.request.Request(url, headers=headers)
        opener = (
            self._opener
            if self._opener is not None
            else build_default_opener(timeout=self._timeout)
        )
        resp = retry_transient(
            lambda: opener.open(req, timeout=self._timeout),
            retries=self._retries,
            delay=self._retry_delay,
        )
        with resp:
            body = resp.read()
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SourceResolutionError(
                f"GitHub API returned non-UTF-8 response for {url}: {exc}"
            ) from exc
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise SourceResolutionError(
                f"GitHub API returned non-JSON response for {url}: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_available_update(
    identity: ZeAlfieIdentity,
    channel: str = "stable",
    *,
    resolver,
    tags_lister,
) -> UpdateResolution:
    """Resolve the newest available ZeAlfie release for *channel*.

    ``channel`` must be exactly ``"stable"`` or ``"beta"`` (explicit; never
    silently picked).  Tag names are parsed as PEP 440 versions (a leading
    ``v``/``V`` is accepted); non-version tags are ignored.  The highest
    matching tag is compared to the current version; the chosen tag is then
    resolved to an exact 40-hex commit SHA via *resolver*.

    Raises:
        ValueError: for an unknown *channel* (programming error).
        SelfUpdateResolutionError: when no matching tag exists or the
            resolver returns a non-immutable ref.
        SourceResolutionError: on network/tag-listing failures (carries a
            ``reason_code`` + ``proxy_hint``).
    """
    if channel not in _VALID_CHANNELS:
        raise ValueError(
            f"invalid channel {channel!r}; expected one of {_VALID_CHANNELS}"
        )

    tag_names = list(tags_lister(DEFAULT_SOURCE_OWNER, DEFAULT_SOURCE_REPO))
    chosen = _pick_highest(tag_names, channel)
    if chosen is None:
        raise SelfUpdateResolutionError(
            f"no {channel} version tags found for "
            f"{DEFAULT_SOURCE_OWNER}/{DEFAULT_SOURCE_REPO}"
        )

    available = chosen.version
    current = _safe_version(identity.version)
    up_to_date = current >= available

    commit_sha = str(resolver(DEFAULT_SOURCE_OWNER, DEFAULT_SOURCE_REPO, chosen.tag)).strip().lower()
    if not _SHA1_RE.match(commit_sha):
        raise SelfUpdateResolutionError(
            f"resolver returned a non-immutable ref {commit_sha!r} for tag "
            f"{chosen.tag!r}; a 40-hex commit SHA is required"
        )

    return UpdateResolution(
        current_version=identity.version,
        available_version=str(available),
        channel=channel,
        source_owner=DEFAULT_SOURCE_OWNER,
        source_repo=DEFAULT_SOURCE_REPO,
        requested_ref=chosen.tag,
        commit_sha=commit_sha,
        up_to_date=up_to_date,
    )


# ---------------------------------------------------------------------------
# Tag parsing / channel selection
# ---------------------------------------------------------------------------


def _parse_tag_version(name: str) -> Version | None:
    """Parse a tag name as a PEP 440 version; ``None`` if not a version tag."""
    if not isinstance(name, str):
        return None
    tag = name.strip()
    if not tag:
        return None
    if tag[0] in "vV":
        tag = tag[1:]
    if not tag:
        return None
    try:
        return Version(tag)
    except InvalidVersion:
        return None


def _is_stable(version: Version) -> bool:
    """A stable release tag: no pre/dev/post/local component."""
    return (
        not version.is_prerelease
        and not version.is_devrelease
        and not version.is_postrelease
        and version.local is None
    )


def _is_beta(version: Version) -> bool:
    """A beta tag: a prerelease whose kind is ``beta`` (not alpha/rc/dev)."""
    return (
        version.is_prerelease
        and version.pre is not None
        and len(version.pre) > 0
        and version.pre[0] == "b"
    )


def _pick_highest(tag_names: list[str], channel: str) -> _TagCandidate | None:
    candidates: list[_TagCandidate] = []
    for name in tag_names:
        version = _parse_tag_version(name)
        if version is None:
            continue
        if channel == "stable":
            if _is_stable(version):
                candidates.append(_TagCandidate(tag=name, version=version))
        else:  # "beta"
            if _is_beta(version):
                candidates.append(_TagCandidate(tag=name, version=version))
    if not candidates:
        return None
    return max(candidates, key=lambda c: c.version)


def _safe_version(value: str) -> Version:
    try:
        return Version(str(value).strip())
    except InvalidVersion:
        return Version("0")
