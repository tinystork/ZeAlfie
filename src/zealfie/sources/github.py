"""Default GitHub source transport for ZeAlfie service install.

Provides production implementations of :class:`SourceRefResolver` and
:class:`ArchiveFetcher` backed by the public GitHub REST API v3.
Anonymous access is supported for public repositories; optional
``GITHUB_TOKEN`` / ``GH_TOKEN`` env-var authentication raises the rate
limit but is never required.

All network objects accept injectable *timeout*, *user_agent*, and
*_opener* constructor arguments so tests can mock ``urllib`` without
ever touching the real GitHub API.

ZA-M1-4 LOT C: transports now use the shared
:class:`~zealfie.net.NetworkReasonCode` classifier, attach a
``reason_code`` + ``proxy_hint`` to raised errors, and apply bounded
transient-only retry (2 retries) around each ``opener.open`` call.  TLS
verification stays ON; tokens are never leaked into messages.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import urllib.request
from urllib.error import HTTPError, URLError

from zealfie.net import (
    build_default_opener,
    classify_exception,
    proxy_hint_for,
    retry_transient,
)
from zealfie.sources import SourceResolutionError
from zealfie.sources.acquisition import AcquisitionError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_TIMEOUT: float = 30.0  # seconds
_DEFAULT_USER_AGENT: str = "ZeAlfie/0.0 (github.com/tinystork/ZeAlfie)"
_DEFAULT_RETRIES: int = 2
_DEFAULT_RETRY_DELAY: float = 0.5
_GITHUB_API_BASE: str = "https://api.github.com"

_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


def _github_token_from_env() -> str | None:
    """Return the GitHub token from the environment, if any.

    Checks ``GITHUB_TOKEN`` first, then ``GH_TOKEN``.  Empty-string
    values are treated as absent.
    """
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        val = os.environ.get(name, "").strip()
        if val:
            return val
    return None


# ---------------------------------------------------------------------------
# GitHub source ref resolver
# ---------------------------------------------------------------------------


class GitHubSourceRefResolver:
    """Resolve a (owner, repo, ref) to an exact commit SHA via the
    GitHub commits API.

    Uses ``GET /repos/{owner}/{repo}/commits/{ref}``.  This endpoint
    handles branches, lightweight tags, and annotated tags (GitHub
    peels annotated tags to their underlying commit).

    If *ref* is already a 40-character hex SHA the resolver returns
    it lowercased with **no network call**.

    Constructor args:

        timeout:
            Socket / read timeout in seconds.  Default 30.

        user_agent:
            User-Agent header sent with every request.  Must comply
            with GitHub's ``User-Agent`` requirement.

        _opener:
            Inject a custom ``urllib.request.OpenerDirector`` for
            testing.  When ``None`` the canonical default opener
            (proxy-aware, TLS-verified) is used.

        retries / retry_delay:
            Bounded transient-only retry policy (default 2 retries,
            0.5s delay).  Permanent failures are never retried.
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

    def __call__(self, owner: str, repo: str, ref: str) -> str:
        """Resolve *ref* to a 40-character hex commit SHA.

        Returns the exact SHA; raises :class:`SourceResolutionError` on
        any failure.
        """
        # Fast-path: ref is already a full SHA.
        ref_lower = ref.strip().lower()
        if _SHA1_RE.match(ref_lower):
            return ref_lower

        # Validate that ref is not a short/abbreviated SHA pretending
        # to be commit-ish.  GitHub will not return the intended commit
        # for ambiguous short SHAs, so we reject them early.
        if len(ref_lower) < 40 and _is_hex_string(ref_lower):
            raise SourceResolutionError(
                f"ref {ref!r} looks like an abbreviated SHA "
                f"({len(ref_lower)} chars); a full 40-char SHA is required "
                f"for deterministic resolution"
            )

        url = f"{_GITHUB_API_BASE}/repos/{owner}/{repo}/commits/{ref}"
        try:
            data = self._get_json(url)
        except (HTTPError, URLError, OSError, http.client.HTTPException) as exc:
            reason_code, reason_msg = classify_exception(exc)
            hint = proxy_hint_for(reason_code)
            message = f"{reason_msg} resolving {owner}/{repo}@{ref}"
            raise SourceResolutionError(
                message, reason_code=reason_code, proxy_hint=hint
            ) from exc

        sha = str(data.get("sha", "")).strip().lower()
        if not _SHA1_RE.match(sha):
            raise SourceResolutionError(
                f"GitHub API returned non-SHA value {sha!r} for "
                f"{owner}/{repo}@{ref}"
            )
        return sha

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_json(self, url: str) -> dict:
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
# GitHub archive fetcher
# ---------------------------------------------------------------------------


class GitHubArchiveFetcher:
    """Fetch a source ZIP archive for an exact commit SHA from GitHub.

    Uses ``GET /repos/{owner}/{repo}/zipball/{commit_sha}``.
    The *commit_sha* must be an exact 40-character hex SHA — this
    class does **not** resolve refs.

    Constructor args are identical to :class:`GitHubSourceRefResolver`.
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

    def __call__(self, owner: str, repo: str, commit_sha: str) -> bytes:
        """Fetch the ZIP archive for *commit_sha* in *owner*/*repo*.

        Returns the raw archive bytes.  Raises :class:`AcquisitionError`
        on HTTP errors, network failures, or empty responses.
        """
        url = f"{_GITHUB_API_BASE}/repos/{owner}/{repo}/zipball/{commit_sha}"
        try:
            data = self._get_bytes(url)
        except (HTTPError, URLError, OSError, http.client.HTTPException) as exc:
            reason_code, reason_msg = classify_exception(exc)
            hint = proxy_hint_for(reason_code)
            message = f"{reason_msg} fetching archive for {owner}/{repo}@{commit_sha}"
            raise AcquisitionError(
                message, reason_code=reason_code, proxy_hint=hint
            ) from exc

        if not data:
            raise AcquisitionError(
                f"empty response fetching archive for "
                f"{owner}/{repo}@{commit_sha}"
            )
        return data

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_bytes(self, url: str) -> bytes:
        """GET *url* and return the raw response body."""
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
            return resp.read()


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _is_hex_string(s: str) -> bool:
    """Return True if *s* consists entirely of hex digits [0-9a-fA-F]."""
    return all(c in "0123456789abcdefABCDEF" for c in s)
